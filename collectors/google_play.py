"""Google Play 公开详情页采集器。"""
import re

import httpx
from bs4 import BeautifulSoup

from . import (BaseCollector, CollectResult, ST_ERROR, ST_NEED_REVIEW,
               ST_NOT_PUBLISHED, register)

DETAIL_URL = "https://play.google.com/store/apps/details"


@register
class GooglePlayCollector(BaseCollector):
    key = "google_play"
    display_name = "Google Play"

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        package_name = (package_name or "").strip()
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少 Android 包名")
        try:
            response = httpx.get(
                DETAIL_URL, params={"id": package_name, "hl": "zh_CN", "gl": "US"},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"},
                timeout=kw.get("timeout", 15), follow_redirects=True,
            )
        except Exception as exc:
            return CollectResult(status=ST_ERROR, detail=f"Google Play 请求失败: {exc!r}")
        if response.status_code == 404:
            return CollectResult(status=ST_NOT_PUBLISHED, detail="Google Play 未发布或当前地区不可见")
        if response.status_code != 200:
            return CollectResult(status=ST_ERROR, detail=f"Google Play HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "html.parser")
        name_tag = soup.select_one('[itemprop="name"]')
        if not name_tag:
            return CollectResult(status=ST_NOT_PUBLISHED, detail="Google Play 没有返回应用详情")

        # Google 未在可见 DOM 中标注 version 属性，当前版本位于同页结构化数据 ds:5。
        match = re.search(r'\[\[\["([^"\\]{1,80})"\]\],\[\[\[\d+\]\]', response.text)
        if not match:
            return CollectResult(status=ST_NEED_REVIEW,
                                 detail="已找到 Google Play 应用，但公开页面未解析到版本号")
        version = match.group(1)
        developer_tag = soup.select_one('a[href*="/store/apps/developer"] span')
        developer = developer_tag.get_text(" ", strip=True) if developer_tag else ""
        operator = ""
        label = soup.find(string=lambda s: s and s.strip() in ("开发者信息", "Developer contact"))
        if label:
            block = label.find_parent("div", class_="Bne0R")
            info = block.find_next_sibling("div") if block else None
            first = info.find("div") if info else None
            operator = first.get_text(" ", strip=True) if first else ""
        return CollectResult(
            version_name=version, status="ok",
            detail=f"{name_tag.get_text(' ', strip=True)} · Google Play 美国区网页",
            extra={"developer": developer, "operator": operator,
                   "market_app_id": package_name},
        )
