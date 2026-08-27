"""USB 手机端渠道采集器。

OPPO、vivo、荣耀没有接入稳定的公开网页目录，因此主巡检直接复用手机端
驱动。每一种前置条件都有独立状态，避免把设备问题误报成“渠道待适配”。
"""
from pathlib import Path

from . import (
    BaseCollector, CollectResult, register, ST_DEVICE_UNAVAILABLE, ST_ERROR,
    ST_LOGIN_REQUIRED, ST_MARKET_APP_MISSING, ST_NEED_REVIEW,
)


class _DeviceMarketCollector(BaseCollector):
    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        package_name = (package_name or "").strip()
        app_name = str(kw.get("app_name") or "").strip()
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少 Android 包名，无法做手机端精确核验")
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
        if result.get("ok") and result.get("version"):
            return CollectResult(
                version_name=str(result["version"]), status="ok",
                detail=str(result.get("detail") or "手机端详情页采集"),
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
