"""Apple App Store 公开搜索接口采集器（中国区）。"""
import re

import httpx

from . import (BaseCollector, CollectResult, SearchCandidate, ST_ERROR,
               ST_NEED_REVIEW, ST_NOT_PUBLISHED, register)

SEARCH_API = "https://itunes.apple.com/search"
LOOKUP_API = "https://itunes.apple.com/lookup"


def _compact(value: str) -> str:
    return re.sub(r"[\s\-—_·:：]+", "", (value or "")).casefold()


@register
class AppStoreCollector(BaseCollector):
    key = "appstore"
    display_name = "Apple App Store"
    supports_search = True

    def _request(self, *, app_name: str, market_app_id: str, timeout: int) -> list[dict]:
        if market_app_id.isdigit():
            params = {"id": market_app_id, "country": "cn"}
            response = httpx.get(LOOKUP_API, params=params, timeout=timeout,
                                 follow_redirects=True)
        else:
            params = {"term": app_name, "country": "cn", "entity": "software", "limit": 25}
            response = httpx.get(SEARCH_API, params=params, timeout=timeout,
                                 follow_redirects=True)
        response.raise_for_status()
        return list((response.json() or {}).get("results") or [])

    @staticmethod
    def _choose(rows: list[dict], app_name: str, company_name: str) -> dict | None:
        target = _compact(app_name)
        company = _compact(company_name)
        scored = []
        for row in rows:
            name = _compact(str(row.get("trackName") or ""))
            seller = _compact(" ".join(str(row.get(k) or "")
                                       for k in ("sellerName", "artistName")))
            score = 0
            if name == target:
                score += 200
            elif name.startswith(target) or target.startswith(name):
                score += 150
            elif target and target in name:
                score += 90
            if company and company in seller:
                score += 80
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return None
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return {"_ambiguous": True, "_count": len(scored)}
        return scored[0][1]

    def collect(self, package_name: str = "", market_app_id: str = "", **kw) -> CollectResult:
        app_name = str(kw.get("app_name") or "").strip()
        company_name = str(kw.get("company_name") or "").strip()
        if not app_name and not (market_app_id or "").isdigit():
            return CollectResult(status=ST_ERROR, detail="缺少 App 名称，无法查询 Apple App Store")
        try:
            rows = self._request(app_name=app_name, market_app_id=(market_app_id or "").strip(),
                                 timeout=kw.get("timeout", 15))
        except Exception as exc:
            return CollectResult(status=ST_ERROR, detail=f"Apple 公开接口请求失败: {exc!r}")
        item = self._choose(rows, app_name, company_name)
        if not item:
            return CollectResult(status=ST_NOT_PUBLISHED,
                                 detail="中国区 Apple App Store 未检索到匹配应用")
        if item.get("_ambiguous"):
            return CollectResult(status=ST_NEED_REVIEW,
                                 detail="Apple App Store 出现多个同名候选，需要绑定准确的 App ID")
        version = str(item.get("version") or "").strip()
        if not version:
            return CollectResult(status=ST_NEED_REVIEW, detail="已找到 iOS 应用，但页面未返回版本号")
        developer = str(item.get("artistName") or "").strip()
        operator = str(item.get("sellerName") or "").strip()
        released = str(item.get("currentVersionReleaseDate") or "")[:10]
        return CollectResult(
            version_name=version, status="ok",
            detail=" · ".join(x for x in (str(item.get("trackName") or ""),
                                           str(item.get("bundleId") or "")) if x),
            extra={"developer": developer, "operator": operator,
                   "market_app_id": str(item.get("trackId") or ""),
                   "bundle_id": str(item.get("bundleId") or ""),
                   "published_at": item.get("currentVersionReleaseDate") or released,
                   "source_url": str(item.get("trackViewUrl") or "")},
        )

    def search(self, keyword: str, **kw) -> list[SearchCandidate]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        rows = self._request(app_name=keyword, market_app_id="",
                             timeout=kw.get("timeout", 15))
        hits = []
        for item in rows:
            app_id = str(item.get("trackId") or "").strip()
            bundle_id = str(item.get("bundleId") or "").strip()
            name = str(item.get("trackName") or "").strip()
            if not name or not (app_id or bundle_id):
                continue
            hits.append(SearchCandidate(
                app_name=name,
                platform="ios",
                bundle_id=bundle_id,
                market_app_id=app_id,
                developer=str(item.get("artistName") or "").strip(),
                operator=str(item.get("sellerName") or "").strip(),
                version_name=str(item.get("version") or "").strip(),
                detail_url=str(item.get("trackViewUrl") or ""),
                icon_url=str(item.get("artworkUrl100") or ""),
                source_market=self.key,
            ))
        return hits
