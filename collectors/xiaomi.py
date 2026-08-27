"""小米应用商店采集器（实测可用）
页面：https://app.mi.com/details?id={package_name}
结构：<div>版本号</div><div style="float:right;">8.0.76</div>
"""
import re
import httpx
from bs4 import BeautifulSoup

from . import BaseCollector, CollectResult, register, ST_OFFLINE, ST_ERROR

URL = "https://app.mi.com/details?id={pkg}"


@register
class XiaomiCollector(BaseCollector):
    key = "xiaomi"
    display_name = "小米应用商店"
    supports_package_lookup = True

    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        }

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少包名")
        try:
            r = httpx.get(URL.format(pkg=package_name), headers=self.headers, timeout=kw.get("timeout", 15),
                          follow_redirects=True)
        except Exception as e:
            return CollectResult(status=ST_ERROR, detail=f"请求失败: {e!r}")
        if r.status_code != 200:
            return CollectResult(status=ST_ERROR, detail=f"HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "html.parser")
        if "应用不存在" in r.text or "页面不存在" in r.text:
            return CollectResult(status=ST_OFFLINE, detail="页面不存在")
        # 版本号：在"版本号"标签右侧 float:right div
        version = ""
        for tag in soup.find_all(string=lambda s: s and "版本号" in s.strip()):
            parent = tag.find_parent("div")
            if parent:
                nxt = parent.find_next_sibling("div")
                if nxt:
                    version = nxt.get_text(strip=True)
                    break
        if not version:
            version = self._fallback_version(soup)
        developer = self._label_value(soup, "开发者")
        published_at = (self._label_value(soup, "更新时间")
                        or self._label_value(soup, "更新日期"))
        return CollectResult(version_name=version, status=ST_OFFLINE if not version else "ok",
                             detail=developer or ("ok" if version else "未解析到版本号"),
                             extra={"developer": developer,
                                    "published_at": published_at,
                                    "source_url": URL.format(pkg=package_name)})

    @staticmethod
    def _label_value(soup, label):
        for tag in soup.find_all(string=lambda s: s and s.strip() == label):
            parent = tag.find_parent("div")
            if parent:
                nxt = parent.find_next_sibling("div")
                if nxt:
                    value = nxt.get_text(strip=True)
                    if value:
                        return value
        return ""

    @staticmethod
    def _fallback_version(soup):
        for div in soup.find_all("div", style=re.compile(r"float:\s*right")):
            txt = div.get_text(strip=True)
            if re.search(r"^\d+(\.\d+)+$", txt):
                return txt
        return ""
