"""应用宝网页采集器。

页面是 Next.js SSR。不能用正则抓第一个 ``version_name``：详情页同时包含
“应用宝客户端”自身信息，那样会把所有目标都误报成应用宝的版本。
这里解析 ``__NEXT_DATA__``，并按目标包名精确选择对象。
"""
import html as html_lib
import json
import re
import httpx
from urllib.parse import quote

from . import BaseCollector, CollectResult, SearchCandidate, register, ST_OFFLINE, ST_ERROR

URL = "https://sj.qq.com/appdetail/{pkg}"
SEARCH_URL = "https://sj.qq.com/search?q={keyword}"
NON_ANDROID_PREFIXES = ("com.tencent.pcgame.",)


def _is_android_app_package(package_name: str) -> bool:
    """应用宝也返回“电脑版”容器条目，它们不是可安装的 Android APK。"""
    package_name = (package_name or "").strip().lower()
    return bool(package_name) and not package_name.startswith(NON_ANDROID_PREFIXES)


def _next_data(page_html: str) -> dict:
    match = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
    if not match:
        return {}
    try:
        return json.loads(html_lib.unescape(match.group(1)))
    except (TypeError, ValueError):
        return {}


def _walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _app_records(data: dict) -> list[dict]:
    """Return unique app-shaped records from the nested SSR payload."""
    found = []
    seen = set()
    for item in _walk_dicts(data):
        pkg = str(item.get("pkg_name") or "").strip()
        name = str(item.get("name") or "").strip()
        if not _is_android_app_package(pkg) or not name or pkg in seen:
            continue
        if not (item.get("app_id") or item.get("version_name") or item.get("developer")):
            continue
        seen.add(pkg)
        found.append(item)
    return found


@register
class YybCollector(BaseCollector):
    key = "yyb"
    display_name = "应用宝"
    supports_search = True

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            "Referer": "https://sj.qq.com/",
        }

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少包名")
        try:
            r = httpx.get(URL.format(pkg=package_name), headers=self.headers, timeout=kw.get("timeout", 15),
                          follow_redirects=True)
        except Exception as e:
            return CollectResult(status=ST_ERROR, detail=f"请求失败: {e!r}")
        if r.status_code in (404, 410):
            # 应用宝对未收录的包名直接返回 404（“应用不存在”），
            # 这是正常的未上架状态，不应按渠道查询失败处理。
            return CollectResult(status=ST_OFFLINE, detail="应用宝未收录该应用（HTTP 404）")
        if r.status_code != 200:
            return CollectResult(status=ST_ERROR, detail=f"HTTP {r.status_code}")
        page_html = r.text
        if "应用不存在" in page_html or "没有找到" in page_html:
            return CollectResult(status=ST_OFFLINE, detail="应用不存在")
        records = _app_records(_next_data(page_html))
        record = next((x for x in records if x.get("pkg_name") == package_name), None)
        if not record:
            return CollectResult(status=ST_OFFLINE, detail="页面存在，但未找到与包名一致的应用记录")
        version = str(record.get("version_name") or "")
        detail_parts = [str(record.get("developer") or "").strip()]
        return CollectResult(
            version_name=version,
            version_code=str(record.get("version_code") or ""),
            status="ok" if version else ST_OFFLINE,
            detail=" · ".join(x for x in detail_parts if x) or "未解析到版本号",
            extra={"app_name": record.get("name", ""),
                   "developer": record.get("developer", ""),
                   "operator": record.get("operator", ""),
                   "published_at": record.get("update_time", ""),
                   "download_url": record.get("download_url", ""),
                   "declared_size": record.get("apk_size") or 0,
                   "source_url": URL.format(pkg=package_name)},
        )

    def search(self, keyword: str, **kw) -> list[SearchCandidate]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        r = httpx.get(
            SEARCH_URL.format(keyword=quote(keyword)), headers=self.headers,
            timeout=kw.get("timeout", 15), follow_redirects=True,
        )
        if r.status_code in (404, 410):
            return []
        r.raise_for_status()
        hits = []
        for item in _app_records(_next_data(r.text)):
            package_name = str(item.get("pkg_name") or "").strip()
            if (not _is_android_app_package(package_name)
                    or package_name == "com.tencent.android.qqdownloader"):
                continue
            hits.append(SearchCandidate(
                app_name=str(item.get("name") or "").strip(),
                package_name=package_name,
                market_app_id=str(item.get("app_id") or ""),
                developer=str(item.get("developer") or item.get("operator") or "").strip(),
                operator=str(item.get("operator") or "").strip(),
                version_name=str(item.get("version_name") or "").strip(),
                detail_url=URL.format(pkg=package_name),
                icon_url=str(item.get("icon") or ""),
                source_market=self.key,
            ))
        return hits[:20]
