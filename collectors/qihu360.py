"""360 手机助手新版官方网站结构化搜索接口 + 移动端详情页更新时间。"""
import re

import httpx

from . import (BaseCollector, CollectResult, SearchCandidate, ST_ERROR,
               ST_NOT_PUBLISHED, ST_PACKAGE_MISMATCH, register)

SEARCH_API = "https://openbox.mobilem.360.cn/PcSearch/class"
DETAIL_PAGE = "https://m.app.so.com/detail/index"
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
_UPDATE_RE = re.compile(
    r"更新时间\s*[:：]?\s*(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})"
)


@register
class Qihu360Collector(BaseCollector):
    key = "qihu360"
    display_name = "360 手机助手"
    supports_search = True

    def _rows(self, keyword: str, timeout: int) -> list[dict]:
        response = httpx.get(
            SEARCH_API,
            params={"q": keyword, "type": "alltop", "page": 1, "ch": "200000"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=timeout, follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json() or {}
        if payload.get("code") != 0:
            raise ValueError(f"360 接口返回 code={payload.get('code')}")
        data = payload.get("data") or {}
        return [x for key in ("soft", "game") for x in (data.get(key) or [])
                if isinstance(x, dict)]

    def _published_at(self, package_name: str, app_id, timeout: int) -> str:
        """从 360 移动端详情页读取更新时间（如 更新时间：2026-08-06 14:32:12）。

        搜索接口不含时间字段，详情页不可用时返回空，不影响版本采集。
        """
        try:
            response = httpx.get(
                DETAIL_PAGE,
                params={"pname": package_name, "id": str(app_id or "")},
                headers={"User-Agent": _MOBILE_UA},
                timeout=timeout, follow_redirects=True,
            )
            match = _UPDATE_RE.search(response.text)
            if match:
                return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        except Exception:
            pass
        return ""

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        package_name = (package_name or "").strip()
        keyword = str(kw.get("app_name") or package_name).strip()
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少 Android 包名")
        try:
            rows = self._rows(keyword, kw.get("timeout", 15))
        except Exception as exc:
            return CollectResult(status=ST_ERROR, detail=f"360 网页接口请求失败: {exc!r}")
        item = next((x for x in rows if str(x.get("apkid") or "") == package_name), None)
        if not item:
            same_name = next((x for x in rows if str(x.get("name") or "").strip() == keyword), None)
            if same_name and same_name.get("apkid"):
                observed = str(same_name.get("apkid"))
                return CollectResult(
                    status=ST_PACKAGE_MISMATCH,
                    detail=f"发现同名应用，但市场包名为 {observed}，与目标 {package_name} 不一致",
                    extra={"observed_package": observed,
                           "developer": str(same_name.get("soft_corp_name") or "")},
                )
            return CollectResult(status=ST_NOT_PUBLISHED,
                                 detail="360 手机助手未检索到与目标包名一致的应用")
        version = str(item.get("version_name") or "").strip()
        if not version:
            return CollectResult(status=ST_NOT_PUBLISHED, detail="360记录未返回版本号")
        developer = str(item.get("soft_corp_name") or "").strip()
        published_at = self._published_at(package_name, item.get("id"), kw.get("timeout", 15))
        return CollectResult(
            version_name=version, version_code=str(item.get("version_code") or ""), status="ok",
            detail=" · ".join(x for x in (developer, str(item.get("download_times") or ""),
                                           str(item.get("category") or "")) if x),
            extra={"developer": developer, "market_app_id": str(item.get("id") or ""),
                   "published_at": published_at,
                   "signature_md5": str(item.get("signature_md5") or ""),
                   "download_url": str(item.get("down_url") or ""),
                   "declared_size": item.get("size") or 0,
                   "source_url": f"https://zhushou.360.cn/detail?id={item.get('id') or ''}"},
        )

    def search(self, keyword: str, **kw) -> list[SearchCandidate]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        rows = self._rows(keyword, kw.get("timeout", 15))
        return [SearchCandidate(
            app_name=str(x.get("name") or ""), package_name=str(x.get("apkid") or ""),
            market_app_id=str(x.get("id") or ""), developer=str(x.get("soft_corp_name") or ""),
            version_name=str(x.get("version_name") or ""),
            detail_url=f"https://zhushou.360.cn/detail?id={x.get('id') or ''}",
            icon_url=str(x.get("logo_url") or ""), source_market=self.key,
        ) for x in rows if x.get("apkid")]
