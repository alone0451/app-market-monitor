"""Android 测试设备端渠道采集器。

OPPO、vivo、荣耀没有接入稳定的公开网页目录，因此主巡检直接复用 Android 设备端
或模拟器端驱动。每一种前置条件都有独立状态，避免把设备问题误报成“渠道待适配”。
"""
from pathlib import Path

from . import (
    BaseCollector, CollectResult, register, ST_DEVICE_UNAVAILABLE, ST_ERROR,
    ST_LOGIN_REQUIRED, ST_MARKET_APP_MISSING, ST_NEED_REVIEW,
    ST_REGION_UNAVAILABLE,
    ST_FALLBACK_OK,
)


class _DeviceMarketCollector(BaseCollector):
    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        package_name = (package_name or "").strip()
        app_name = str(kw.get("app_name") or "").strip()
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少 Android 包名，无法做设备端精确核验")
        if not app_name:
            return CollectResult(status=ST_ERROR, detail="缺少应用名称，无法在市场客户端搜索")

        from executors.device import DeviceExecutor
        executor = DeviceExecutor()
        ready, message = executor.check_ready()
        if not ready:
            return CollectResult(status=ST_DEVICE_UNAVAILABLE, detail=message)

        screenshot_dir = Path(__file__).resolve().parents[1] / "data" / "screenshots"
        result = executor.inspect_market_detail(
            market_id=self.key, package_name=package_name, app_name=app_name,
            screenshot_dir=str(screenshot_dir),
        )
        status = result.get("status")
        if status == "region_unavailable" and self.key == "honor":
            # The generic AOSP emulator cannot satisfy Honor's regional/device
            # gate. Query the official Huawei AppGallery package search as a
            # transparent fallback (Honor/Huawei distribution metadata may be
            # shared), while retaining the unavailable-client evidence and
            # never presenting it as direct Honor-client confirmation.
            try:
                from .huawei import HuaweiCollector
                fallback = HuaweiCollector().collect(
                    package_name=package_name, app_name=app_name,
                    timeout=kw.get("timeout", 15),
                )
            except Exception as exc:
                fallback = None
                fallback_error = f"{type(exc).__name__}: {exc}"
            else:
                fallback_error = str(getattr(fallback, "detail", "") or "")
            if fallback and fallback.status == "ok" and fallback.version_name:
                extra = dict(fallback.extra or {})
                extra.update({
                    # The client screenshot only proves the regional block. It
                    # is not a screenshot of the Huawei fallback result and
                    # must never be displayed as if it were one.
                    "screenshot": "",
                    "device_screenshot": result.get("screenshot", ""),
                    "evidence_scope": "huawei_appgallery_fallback",
                    "device_detail": result.get("detail", ""),
                })
                return CollectResult(
                    version_name=fallback.version_name,
                    version_code=fallback.version_code,
                    status=ST_FALLBACK_OK,
                    detail=("荣耀客户端因当前模拟器地区不可用；已取得华为/荣耀官方 AppGallery "
                            "网页替代证据，需在荣耀认证环境复核"),
                    extra=extra,
                )
            result["detail"] = (result.get("detail", "") +
                                (f"；官方 AppGallery 替代查询未取得版本：{fallback_error}"
                                 if fallback_error else "；官方 AppGallery 替代查询未取得版本"))
        if result.get("ok") and result.get("version"):
            return CollectResult(
                version_name=str(result["version"]), status="ok",
                detail=str(result.get("detail") or "Android 设备端详情页采集"),
                extra={"screenshot": result.get("screenshot", ""),
                       "developer": result.get("developer", ""),
                       "operator": result.get("operator", ""),
                       "published_at": result.get("published_at", "")},
            )
        if status == "market_app_missing":
            final_status = ST_MARKET_APP_MISSING
        elif status == "device_unavailable":
            final_status = ST_DEVICE_UNAVAILABLE
        elif status == "login_required":
            final_status = ST_LOGIN_REQUIRED
        elif status == "region_unavailable":
            final_status = ST_REGION_UNAVAILABLE
        else:
            final_status = ST_NEED_REVIEW
        return CollectResult(
            status=final_status,
            detail=str(result.get("detail") or "已打开市场客户端，但未读取到版本号"),
            extra={"screenshot": result.get("screenshot", ""),
                   "developer": result.get("developer", ""),
                   "operator": result.get("operator", ""),
                   "published_at": result.get("published_at", "")},
        )


@register
class OppoCollector(_DeviceMarketCollector):
    key = "oppo"
    display_name = "OPPO 软件商店"


@register
class VivoCollector(_DeviceMarketCollector):
    key = "vivo"
    display_name = "vivo 应用商店"


@register
class HonorCollector(_DeviceMarketCollector):
    key = "honor"
    display_name = "荣耀应用市场"


class BaiduDeviceFallbackCollector(_DeviceMarketCollector):
    """百度已安装客户端时的 Android 客户端主路径。

    不注册到常规采集器表，避免覆盖 ``collectors.baidu.BaiduCollector`` 的
    网页采集器；巡检流水线在检测到客户端已安装时显式调用它，失败后再回落网页。
    """
    key = "baidu"
    display_name = "百度手机助手"


def get_device_fallback_collector(market_id: str):
    """Return an optional device-side fallback without changing web priority."""
    return BaiduDeviceFallbackCollector() if market_id == "baidu" else None
