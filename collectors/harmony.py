"""鸿蒙原生应用的公开网页探测器。

华为 AppGallery 的桌面网页搜索当前主要返回 Android 条目。只有响应明确
标记为 HarmonyOS 类型时才记录版本；否则返回 web_limited，避免把 APK
版本误报成鸿蒙原生应用版本。
"""
from . import (BaseCollector, CollectResult, ST_ERROR, ST_WEB_LIMITED,
               register)
from .huawei import HuaweiCollector

HARMONY_CTYPES = {17, 18}


@register
class HarmonyCollector(BaseCollector):
    key = "harmony"
    display_name = "鸿蒙应用市场（原生）"

    def __init__(self):
        self._web = HuaweiCollector()

    def collect(self, package_name: str = "", market_app_id: str = "", **kw) -> CollectResult:
        app_name = str(kw.get("app_name") or "").strip()
        if not app_name:
            return CollectResult(status=ST_ERROR, detail="缺少 App 名称，无法探测鸿蒙原生应用")
        try:
            rows = self._web._search_items(app_name, kw.get("timeout", 15))
        except Exception as exc:
            return CollectResult(status=ST_ERROR, detail=f"华为公开网页请求失败: {exc!r}")
        native = next((x for x in rows if int(x.get("ctype") or 0) in HARMONY_CTYPES), None)
        if native and native.get("version"):
            developer = str(native.get("developer") or "").strip()
            return CollectResult(
                version_name=str(native.get("version") or ""),
                version_code=str(native.get("versionCode") or ""), status="ok",
                detail="华为公开网页明确标记为鸿蒙原生应用",
                extra={"developer": developer,
                       "market_app_id": str(native.get("id") or native.get("appid") or "")},
            )
        return CollectResult(
            status=ST_WEB_LIMITED,
            detail="华为公开网页未提供可独立核验的鸿蒙原生版本；未使用 Android 版本代替。后续可接入 AGC 凭据或鸿蒙设备复核",
        )
