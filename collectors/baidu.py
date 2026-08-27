"""百度手机助手官方网页采集器。"""
import json
import re
import time

import httpx

from . import (BaseCollector, CollectResult, SearchCandidate, register, ST_ERROR,
               ST_OFFLINE, ST_PACKAGE_MISMATCH)
from core.version import version_key


BASE = "https://mobile.baidu.com"
SEARCH_API = BASE + "/api/appsearch"
DETAIL_API = BASE + "/api/appDocNew"


def _initial_state_payload(html: str, docid: str = "") -> dict:
    """提取详情页实际渲染使用的 ``window.__INITIAL_STATE__`` 数据。

    百度 ``appDocNew`` 接口在同一 docid/packageid 下可能由不同后端节点返回
    不同历史版本；详情页首屏状态才是用户当前实际看到的市场版本。
    """
    match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html or "")
    if not match:
        return {}
    try:
        state, _ = json.JSONDecoder().raw_decode((html or "")[match.end():])
    except (json.JSONDecodeError, TypeError):
        return {}
    res_list = (((state.get("appDoc") or {}).get("resList")) or {})
    preferred = res_list.get(f"d_{docid}") if docid else None
    if isinstance(preferred, dict):
        return preferred
    return next((value for value in res_list.values()
                 if isinstance(value, dict) and value.get("data")), {})


def _detail_fields(payload: dict) -> dict:
    cards = (((payload.get("data") or {}).get("appDocNew") or {}).get("cardList") or [])
    summary_types = ("detailNatureCardApp", "detailBusinessCardApp")
    download_types = ("detailNatureDownLoadUad", "detailBusinessDownloadUad")
    summary = next((x for x in cards if x.get("cardType") in summary_types), {})
    download = next((x for x in cards if x.get("cardType") in download_types), {})
    app_doc = download.get("appDoc") or {}
    devname = next(
        (card.get("devname") for card in cards if card.get("devname")), ""
    )
    fields = {
        "app_name": app_doc.get("sname") or summary.get("appName") or "",
        "package_name": app_doc.get("package") or "",
        "market_app_id": str(app_doc.get("docid") or ""),
        "package_id": str(app_doc.get("packageid") or ""),
        "version_name": (summary.get("versionNum")
                         or app_doc.get("versionname") or app_doc.get("version") or ""),
        "version_code": str(app_doc.get("versioncode") or ""),
        "developer": app_doc.get("devname") or devname or "",
        "updated_at": summary.get("appUpdateDate") or "",
        "icon_url": app_doc.get("icon") or summary.get("appImg") or "",
        "download_url": app_doc.get("downloadUrl") or app_doc.get("download_url") or
                        (app_doc.get("info") or {}).get("download_url") or "",
        "declared_size": app_doc.get("size") or app_doc.get("package_size") or 0,
    }
    if not fields["package_name"]:
        # 部分应用的详情卡是 detailBusiness* 版式，下载卡里
        # 没有 appDoc；但 appDoc 主体直接挂在 appDocNew.appDoc 下，用它兜底。
        doc = (((payload.get("data") or {}).get("appDocNew") or {}).get("appDoc")) or {}
        fields = {
            "app_name": doc.get("sname") or fields["app_name"],
            "package_name": doc.get("package") or fields["package_name"],
            "market_app_id": str(doc.get("docid") or fields["market_app_id"]),
            "package_id": str(doc.get("packageid") or fields["package_id"]),
            "version_name": (doc.get("versionname") or doc.get("version")
                             or fields["version_name"]),
            "version_code": str(doc.get("versioncode") or fields["version_code"]),
            "developer": doc.get("devname") or fields["developer"],
            "updated_at": fields["updated_at"],
            "icon_url": fields["icon_url"],
            "download_url": (doc.get("downloadUrl") or doc.get("download_url")
                             or fields["download_url"]),
            "declared_size": doc.get("size") or fields["declared_size"],
        }
    return fields


@register
class BaiduCollector(BaseCollector):
    key = "baidu"
    display_name = "百度手机助手"
    supports_search = True

    def __init__(self):
        self.headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        self.cookies = {}

    def _search_rows(self, keyword: str, timeout: int) -> list[dict]:
        response = httpx.get(SEARCH_API, params={"word": keyword}, headers=self.headers,
                             timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        # 详情节点会参考搜索会话做分流；复用 BAIDUID 等 Cookie，尽量与
        # 用户从搜索结果点击进入详情页的路径保持一致。
        try:
            self.cookies.update(dict(response.cookies))
        except (AttributeError, TypeError, ValueError):
            pass
        return (((response.json().get("data") or {}).get("data")) or [])

    def _page_detail(self, row: dict, timeout: int, probe: int = 0) -> dict:
        params = {
            "docid": row.get("docid"),
            "f0": "search_searchContent@0_appBaseNormal@0",
        }
        if row.get("advitem"):
            params["advitem"] = row["advitem"]
        if probe:
            # 百度详情页存在多节点历史数据不一致，避免连续命中同一缓存副本。
            params["_"] = str(int(time.time() * 1000) + probe)
        response = httpx.get(
            BASE + "/item", params=params,
            headers={**self.headers, "Accept": "text/html,application/xhtml+xml"},
            cookies=self.cookies, timeout=timeout, follow_redirects=True,
        )
        response.raise_for_status()
        payload = _initial_state_payload(response.text, str(row.get("docid") or ""))
        fields = _detail_fields(payload)
        if not fields.get("package_name") or not fields.get("version_name"):
            raise ValueError("百度详情网页未包含完整应用版本数据")
        fields["data_source"] = "visible_page"
        return fields

    def _api_detail(self, row: dict, timeout: int) -> dict:
        response = httpx.get(
            DETAIL_API, params={"docid": row.get("docid"), "pid": row.get("packageid")},
            headers=self.headers, cookies=self.cookies,
            timeout=timeout, follow_redirects=True,
        )
        response.raise_for_status()
        fields = _detail_fields(response.json())
        fields["data_source"] = "api_fallback"
        return fields

    def _detail(self, row: dict, timeout: int, attempts: int = 1) -> dict:
        """以用户可见详情页为准，并处理百度多节点返回历史版本的问题。"""
        samples = []
        page_errors = []
        for probe in range(max(1, attempts)):
            try:
                samples.append(self._page_detail(row, timeout, probe))
            except Exception as exc:
                page_errors.append(str(exc))

        if samples:
            # 同一个包名出现多个可见版本时，应用市场的版本迭代应单调递增，
            # 因而采用最高语义版本，同时完整保留该版本对应的下载记录。
            fields = max(samples, key=lambda item: version_key(item.get("version_name", "")))
            candidates = sorted({item.get("version_name", "") for item in samples
                                 if item.get("version_name")},
                                key=version_key, reverse=True)
            fields["version_candidates"] = candidates
            if len(candidates) > 1:
                fields["data_warning"] = (
                    "百度详情节点返回版本不一致，已采用最高可见版本: "
                    + " / ".join(candidates)
                )
        else:
            page_exc = "; ".join(page_errors[:2]) or "未知解析错误"
            fields = self._api_detail(row, timeout)
            fields["data_warning"] = f"详情网页解析失败，已使用接口兜底: {page_exc}"

        # 页面入口 docid 与下载卡内部 docid 不是同一概念。报告必须保留
        # 搜索结果的稳定入口，不能被接口轮换的内部下载记录覆盖。
        source_docid = str(row.get("docid") or "")
        fields["download_docid"] = fields.get("market_app_id", "")
        fields["market_app_id"] = source_docid or fields.get("market_app_id", "")
        fields["package_id"] = str(row.get("packageid") or fields.get("package_id", ""))
        return fields

    def _resolved(self, keyword: str, timeout: int, limit: int = 10,
                  exact_attempts: int = 5) -> list[dict]:
        resolved = []
        for row in self._search_rows(keyword, timeout)[:limit]:
            try:
                # 精确同名结果是 collect 的首要候选，增加采样以识别百度节点
                # 间的历史版本分歧；其他候选保持单次请求，控制巡检耗时。
                attempts = (max(1, exact_attempts)
                            if str(row.get("sname") or "").strip() == keyword else 1)
                item = self._detail(row, timeout, attempts=attempts)
            except Exception:
                continue
            if item.get("package_name"):
                resolved.append(item)
        return resolved

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        package_name = (package_name or "").strip()
        app_name = str(kw.get("app_name") or "").strip()
        if not package_name:
            return CollectResult(status=ST_ERROR, detail="缺少 Android 包名")
        if not app_name:
            return CollectResult(status=ST_ERROR, detail="百度精确查询需要应用名称")
        try:
            items = self._resolved(app_name, kw.get("timeout", 15))
        except Exception as exc:
            return CollectResult(status=ST_ERROR, detail=f"请求失败: {exc!r}")
        item = next((x for x in items if x["package_name"] == package_name), None)
        if not item:
            same_name = next((x for x in items if x.get("app_name") == app_name), None)
            if same_name and same_name.get("package_name"):
                observed = same_name["package_name"]
                return CollectResult(
                    status=ST_PACKAGE_MISMATCH,
                    detail=f"发现同名应用，但市场包名为 {observed}，与目标 {package_name} 不一致",
                    extra={"observed_package": observed,
                           "developer": same_name.get("developer", "")},
                )
            return CollectResult(status=ST_OFFLINE, detail="百度搜索结果中没有与目标包名一致的应用")
        if not item["version_name"]:
            return CollectResult(status=ST_OFFLINE, detail="百度详情未返回版本号")
        return CollectResult(
            version_name=item["version_name"], version_code=item["version_code"], status="ok",
            detail=item["developer"],
            extra={**item, "source_url":
                   f"{BASE}/item?docid={item['market_app_id']}&pid={item['package_id']}"},
        )

    def search(self, keyword: str, **kw) -> list[SearchCandidate]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        # Candidate discovery only needs one current visible record. Full
        # monitoring still samples exact-name detail nodes in ``collect``.
        items = self._resolved(keyword, kw.get("timeout", 15), exact_attempts=1)
        return [SearchCandidate(
            app_name=x["app_name"], package_name=x["package_name"],
            market_app_id=x["market_app_id"], developer=x["developer"],
            version_name=x["version_name"],
            detail_url=f"{BASE}/item?docid={x['market_app_id']}&pid={x['package_id']}",
            icon_url=x["icon_url"], source_market=self.key,
        ) for x in items]
