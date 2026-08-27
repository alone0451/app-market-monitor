"""华为应用市场采集器（实测可用）
链路：appgallery.huawei.com 种 cookie → webedge/getInterfaceCode 拿 JWT 令牌 →
      uowap getTabDetail 拿详情 → layoutData 解析版本号。

注意：华为详情接口用「市场 AppID（C100xxx）」而非包名定位。
AppID 获取方式：华为应用市场网页/App 打开应用详情，地址栏 appgallery.huawei.com/app/C100xxx。
"""
import json
import time
from urllib.parse import quote

import httpx

from . import (BaseCollector, CollectResult, SearchCandidate, register, ST_OFFLINE,
               ST_ERROR, ST_PACKAGE_MISMATCH)

HOME = "https://appgallery.huawei.com/"
TOKEN_API = "https://web-drcn.hispace.dbankcloud.com/webedge/getInterfaceCode"
DETAIL_API = "https://web-drcn.hispace.dbankcloud.com/edge/uowap/index"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://appgallery.huawei.com",
    "Referer": "https://appgallery.huawei.com/",
}


@register
class HuaweiCollector(BaseCollector):
    key = "huawei"
    display_name = "华为应用市场"
    supports_search = True

    def __init__(self):
        self._client = None
        self._code = ""

    def _get_code(self) -> str:
        """获取 Interface-Code 令牌（JWT），约 1 分钟有效"""
        if not self._client:
            self._client = httpx.Client(follow_redirects=True, timeout=20, headers=HEADERS)
            self._client.get(HOME)  # 种 WAF cookie
        r = self._client.post(TOKEN_API)
        text = r.text.strip()
        if text.startswith('"'):
            return json.loads(text)
        return (r.json() or {}).get("data", "")

    def _reset_session(self) -> None:
        """Discard Huawei's short-lived token and its WAF cookie together."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._code = ""

    def _request(self, params: dict, timeout: int = 15):
        """Call AppGallery and refresh the one-minute token once on auth expiry."""
        response = None
        for attempt in range(2):
            if not self._code:
                self._code = self._get_code()
            icode = f"{self._code}_{int(time.time() * 1000)}"
            response = self._client.get(
                DETAIL_API, params=params,
                headers={**HEADERS, "Interface-Code": icode}, timeout=timeout,
            )
            if response.status_code not in (401, 403) or attempt:
                return response
            self._reset_session()
        return response

    def _search_items(self, keyword: str, timeout: int = 15) -> list[dict]:
        response = self._request(
            {"method": "internal.completeSearchWord", "serviceType": "20",
             "keyword": keyword}, timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        rows = []
        if isinstance(payload.get("app"), dict):
            rows.append(payload["app"])
        rows.extend(x for x in (payload.get("appList") or []) if isinstance(x, dict))
        unique = {}
        for row in rows:
            package_name = str(row.get("package") or "").strip()
            if package_name:
                unique[package_name] = row
        return list(unique.values())

    def collect(self, package_name: str = "", market_app_id: str = "", **kw) -> CollectResult:
        appid = (market_app_id or "").strip()
        app_name = str(kw.get("app_name") or "").strip()
        if package_name and app_name:
            try:
                items = self._search_items(app_name, kw.get("timeout", 15))
                item = next((x for x in items
                             if str(x.get("package") or "") == package_name), None)
            except Exception as exc:
                return CollectResult(status=ST_ERROR, detail=f"华为搜索请求失败: {exc!r}")
            if not item:
                same_name = next((x for x in items
                                  if str(x.get("name") or "").strip() == app_name), None)
                if same_name and same_name.get("package"):
                    observed = str(same_name.get("package"))
                    return CollectResult(
                        status=ST_PACKAGE_MISMATCH,
                        detail=f"发现同名应用，但市场包名为 {observed}，与目标 {package_name} 不一致",
                        extra={"observed_package": observed,
                               "developer": same_name.get("developer", "")},
                    )
                return CollectResult(status=ST_OFFLINE, detail="华为搜索结果中没有与目标包名一致的应用")
            version = str(item.get("version") or item.get("appVersionName") or "")
            if not version:
                return CollectResult(status=ST_OFFLINE, detail="华为搜索记录未返回版本号")
            return CollectResult(
                version_name=version, version_code=str(item.get("versionCode") or ""), status="ok",
                detail=str(item.get("developer") or "").strip(),
                extra={"app_name": item.get("name", ""), "developer": item.get("developer", ""),
                       "market_app_id": item.get("id") or item.get("appid") or "",
                       "published_at": item.get("releaseDate") or "",
                       "source_url": f"https://appgallery.huawei.com/app/{item.get('id') or item.get('appid') or ''}"},
            )
        if not appid:
            return CollectResult(status=ST_ERROR, detail="缺少应用名称，无法自动查询华为应用市场")
        try:
            detail_link = f"https://appgallery.huawei.com/#/app/{appid}"
            params = {
                "method": "internal.getTabDetail",
                "serviceType": "20",
                "reqPageNum": "1",
                "maxResults": "25",
                "uri": f"app|{appid}",
                "shareTo": "",
                "currentUrl": quote(quote(detail_link)),
                "accessId": "",
                "appid": appid,
                "zone": "",
                "locale": "zh",
            }
            r = self._request(params, timeout=kw.get("timeout", 15))
        except Exception as e:
            return CollectResult(status=ST_ERROR, detail=f"请求失败: {e!r}")
        if r.status_code != 200:
            return CollectResult(status=ST_ERROR, detail=f"HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            return CollectResult(status=ST_ERROR, detail="响应非 JSON")
        # layoutData 中取含 versionName 的条目
        info = None
        for ld in data.get("layoutData", []):
            for item in (ld.get("dataList") or []):
                if isinstance(item, dict) and item.get("versionName"):
                    info = item
                    break
            if info:
                break
        if not info:
            return CollectResult(status=ST_OFFLINE, detail="未找到版本信息（appid 可能无效或应用已下线）")
        return CollectResult(
            version_name=str(info.get("versionName", "")),
            version_code=str(info.get("versionCode", "")),
            status="ok",
            detail=" · ".join(x for x in (
                str(info.get("package") or ""), str(info.get("sizeDesc") or "")
            ) if x),
            extra={"developer": info.get("developer") or info.get("author") or "",
                   "operator": info.get("operator") or "",
                   "published_at": info.get("releaseDate") or ""},
        )

    def search(self, keyword: str, **kw) -> list[SearchCandidate]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        items = self._search_items(keyword, kw.get("timeout", 15))
        return [SearchCandidate(
            app_name=str(item.get("name") or ""),
            package_name=str(item.get("package") or ""),
            market_app_id=str(item.get("id") or item.get("appid") or ""),
            developer=str(item.get("developer") or ""),
            version_name=str(item.get("version") or item.get("appVersionName") or ""),
            detail_url=f"https://appgallery.huawei.com/app/{item.get('id') or item.get('appid') or ''}",
            icon_url=str(item.get("icon") or ""), source_market=self.key,
        ) for item in items]
