"""三星 Galaxy Store 公开网页采集器。

官方详情页会调用 ``/api/detail/{package}`` 获取数据；接口可在非三星设备上
读取版本元数据，因此版本巡检不需要安装 Galaxy Store 客户端。
"""
import httpx

from . import (BaseCollector, CollectResult, register, ST_ERROR, ST_OFFLINE,
               ST_PACKAGE_MISMATCH)


DETAIL_API = "https://apps.galaxyappstore.com/api/detail/{pkg}"
DETAIL_URL = "https://apps.galaxyappstore.com/detail/{pkg}"


@register
class SamsungCollector(BaseCollector):
    key = "samsung"
    display_name = "三星 Galaxy Store"
    supports_package_lookup = True

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        package_name = (package_name or market_app_id or "").strip()
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少 Android 包名")
        try:
            response = httpx.get(
                DETAIL_API.format(pkg=package_name),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=kw.get("timeout", 15), follow_redirects=True,
            )
        except Exception as exc:
            return CollectResult(status=ST_ERROR, detail=f"请求失败: {exc!r}")
        if response.status_code == 404:
            return CollectResult(status=ST_OFFLINE, detail="Galaxy Store 未找到该包名")
        if response.status_code != 200:
            return CollectResult(status=ST_ERROR, detail=f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            return CollectResult(status=ST_ERROR, detail="三星接口响应不是 JSON")
        info = payload.get("DetailMain") or {}
        returned_package = str(info.get("appId") or payload.get("appId") or "").strip()
        err_code = str(payload.get("errCode") or "").strip()
        if err_code or not returned_package:
            # 接口返回错误码或空包名（如 errCode 9901 内容已下架/状态未知），
            # 表示该应用未收录或已下架，不应误判为“同名错包”。
            err_msg = str(payload.get("errMsg") or "").strip()
            suffix = f"{err_code} {err_msg[:80]}".strip()
            return CollectResult(
                status=ST_OFFLINE,
                detail=f"Galaxy Store 未收录或已下架该应用（{suffix}）",
            )
        if returned_package != package_name:
            return CollectResult(
                status=ST_PACKAGE_MISMATCH,
                detail=f"详情接口返回包名 {returned_package or '空'}，与目标 {package_name} 不一致",
                extra={"observed_package": returned_package},
            )
        version = str(info.get("contentBinaryVersion") or "").strip()
        if not version:
            return CollectResult(status=ST_OFFLINE, detail="Galaxy Store 未返回版本信息")
        detail = " · ".join(x for x in (
            str(info.get("contentName") or "").strip(),
            str(info.get("sellerName") or "").strip(),
        ) if x)
        return CollectResult(
            version_name=version, status="ok", detail=detail,
            extra={
                "app_name": info.get("contentName", ""),
                "developer": info.get("sellerName", ""),
                "detail_url": DETAIL_URL.format(pkg=package_name),
                "published_at": info.get("modifyDate", ""),
                "source_url": DETAIL_URL.format(pkg=package_name),
            },
        )
