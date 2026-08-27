"""魅族应用商店公开网页采集器。"""
import html
from urllib.parse import quote

import httpx

from . import (BaseCollector, CollectResult, SearchCandidate, register, ST_ERROR,
               ST_OFFLINE, ST_PACKAGE_MISMATCH)


SEARCH_API = ("https://app.meizu.com/apps/public/search/page?cat_id=1"
              "&keyword={keyword}&start=0&max=18")
SEARCH_PAGE = "https://app.meizu.com/apps/public/search?keyword={keyword}"


def _clean(value) -> str:
    return html.unescape(str(value or "")).strip()


def _items(payload: dict) -> list[dict]:
    value = payload.get("value") or {}
    rows = value.get("list") or []
    return [item for item in rows if isinstance(item, dict)]


@register
class MeizuCollector(BaseCollector):
    key = "meizu"
    display_name = "魅族应用商店"
    supports_search = True

    def _search_payload(self, keyword: str, timeout: int) -> list[dict]:
        response = httpx.get(
            SEARCH_API.format(keyword=quote(keyword)),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=timeout, follow_redirects=True,
        )
        response.raise_for_status()
        return _items(response.json())

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        package_name = (package_name or "").strip()
        app_name = str(kw.get("app_name") or "").strip()
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少 Android 包名")
        if not app_name:
            return CollectResult(status=ST_ERROR, detail="魅族精确查询需要应用名称")
        try:
            rows = self._search_payload(app_name, kw.get("timeout", 15))
        except Exception as exc:
            return CollectResult(status=ST_ERROR, detail=f"请求失败: {exc!r}")
        item = next((x for x in rows if str(x.get("package_name") or "").strip() == package_name), None)
        if not item:
            same_name = next((x for x in rows if _clean(x.get("name")) == app_name), None)
            if same_name and same_name.get("package_name"):
                observed = _clean(same_name.get("package_name"))
                return CollectResult(
                    status=ST_PACKAGE_MISMATCH,
                    detail=f"发现同名应用，但市场包名为 {observed}，与目标 {package_name} 不一致",
                    extra={"observed_package": observed,
                           "developer": _clean(same_name.get("publisher"))},
                )
            return CollectResult(status=ST_OFFLINE, detail="搜索结果中没有与目标包名一致的应用")
        version = _clean(item.get("version_name"))
        if not version:
            return CollectResult(status=ST_OFFLINE, detail="魅族记录未返回版本号")
        return CollectResult(
            version_name=version,
            version_code=_clean(item.get("version_code")),
            status="ok",
            detail=_clean(item.get("publisher")),
            extra={
                "app_name": _clean(item.get("name")),
                "developer": _clean(item.get("publisher")),
                "market_app_id": _clean(item.get("id")),
                "updated_at": _clean(item.get("sale_time")),
                "declared_size": item.get("size") or 0,
                "source_url": SEARCH_PAGE.format(keyword=quote(app_name)),
            },
        )

    def search(self, keyword: str, **kw) -> list[SearchCandidate]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        rows = self._search_payload(keyword, kw.get("timeout", 15))
        hits = []
        for item in rows:
            package_name = _clean(item.get("package_name"))
            if not package_name:
                continue
            hits.append(SearchCandidate(
                app_name=_clean(item.get("name")),
                package_name=package_name,
                market_app_id=_clean(item.get("id")),
                developer=_clean(item.get("publisher")),
                version_name=_clean(item.get("version_name")),
                detail_url=SEARCH_PAGE.format(keyword=quote(keyword)),
                icon_url=_clean(item.get("icon")),
                source_market=self.key,
            ))
        return hits[:20]
