"""Search markets first, then let the user confirm a canonical Android package."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from difflib import SequenceMatcher
import re

from collectors import ST_OK, all_collectors


MAX_PACKAGE_LOOKUP_CANDIDATES = 12


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _app_name(value: str) -> str:
    name = _normalize(value)
    for suffix in ("手机客户端", "客户端", "手机版", "安卓版", "app"):
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            name = name[:-len(suffix)]
            break
    return name


def _app_search_terms(value: str) -> list[str]:
    """Expand only harmless spelling variants that upstream stores may distinguish.

    Candidate ranking remains case-insensitive; these variants are sent because some
    market search endpoints are case-sensitive (for example ``示例ai`` vs ``示例AI``).
    """
    raw = str(value or "").strip()
    if not raw:
        return []
    without_suffix = re.sub(
        r"\s*(?:手机客户端|客户端|手机版|安卓版|app)\s*$", "", raw,
        flags=re.IGNORECASE,
    ).strip()
    compact = re.sub(r"\s+", "", without_suffix or raw)
    variants = [raw, without_suffix, compact]
    if re.search(r"[A-Za-z]", compact):
        variants.extend([compact.upper(), compact.lower()])
    # Avoid multiplying requests for punctuation/case-equivalent duplicates.
    return list(dict.fromkeys(x for x in variants if x))[:5]


_REGION_PREFIXES = (
    "北京市", "上海市", "天津市", "重庆市", "北京", "上海", "天津", "重庆",
    "深圳市", "广州市", "杭州市", "南京市", "成都市", "武汉市", "深圳", "广州",
    "杭州", "南京", "成都", "武汉", "中国",
)
_LEGAL_SUFFIXES = ("股份有限公司", "有限责任公司", "集团有限公司", "有限公司", "集团", "公司")
_INDUSTRY_WORDS = (
    "电子商务", "信息技术", "网络技术", "网络科技", "科技", "技术", "互联网",
    "软件", "商务", "贸易", "控股", "数字",
)


def _company_search_terms(value: str) -> list[str]:
    """Return the full legal name plus a conservative brand-style recall term."""
    full = _normalize(value)
    core = full
    for prefix in _REGION_PREFIXES:
        if core.startswith(prefix) and len(core) - len(prefix) >= 2:
            core = core[len(prefix):]
            break
    for suffix in _LEGAL_SUFFIXES:
        if core.endswith(suffix) and len(core) - len(suffix) >= 2:
            core = core[:-len(suffix)]
            break
    for word in _INDUSTRY_WORDS:
        if len(core.replace(word, "")) >= 2:
            core = core.replace(word, "")
    # 注册主体中常见的中文数字修饰词通常不是用户识别 App 的品牌词。
    without_number = re.sub(
        r"[零〇一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟]+度?", "", core
    )
    if len(without_number) >= 2:
        core = without_number
    terms = [str(value or "").strip()]
    if 2 <= len(core) <= 12 and core != full:
        terms.append(core)
    return list(dict.fromkeys(x for x in terms if x))


def _entities(item: dict) -> list[str]:
    values = list(item.get("entities") or [])
    values.extend([item.get("developer") or "", item.get("operator") or ""])
    return list(dict.fromkeys(_normalize(x) for x in values if _normalize(x)))


def _relevance(item: dict, query: str, search_type: str = "app") -> int:
    """Return zero for unrelated rows; app and company searches use distinct evidence."""
    q = _app_name(query) if search_type == "app" else _normalize(query)
    name = _app_name(item.get("app_name") or "")
    entities = _entities(item)
    if not q:
        return 0
    if search_type == "app":
        # App 搜索必须由名称本身证明相关，开发者相同不能把旗下所有 App 混进来。
        if name == q:
            return 300
        if q in name:
            return 220 - min(80, len(name) - len(q))
        # Market search APIs often return a useful one-character variant. Keep
        # conservative fuzzy recall for names of at least four characters; the
        # user must still confirm the package name before monitoring begins.
        if min(len(q), len(name)) >= 4:
            similarity = SequenceMatcher(None, q, name).ratio()
            if similarity >= 0.76:
                return 100 + int(similarity * 100)
        return 0
    # 公司搜索同时核对开发者和运营者；完整主体命中优先于品牌名召回。
    if q in entities:
        return 280
    if any(q in entity or entity in q for entity in entities):
        return 250
    aliases = [_normalize(x) for x in _company_search_terms(query)[1:]]
    if any(alias and alias in entity for alias in aliases for entity in entities):
        return 210
    if any(name == alias for alias in aliases):
        return 150
    if any(alias and alias in name for alias in aliases):
        return 110
    return 0


def _package_lookup_enrichment(candidates: list[dict], collectors: dict,
                               timeout: int) -> dict:
    """Use canonical Android packages to enrich non-searchable web markets.

    This does not infer identity from a similar name: only an already discovered
    exact package is queried, and only an ``ok`` response is added as a hit.
    """
    lookup_collectors = {
        key: collector for key, collector in collectors.items()
        if getattr(collector, "supports_package_lookup", False) is True
    }
    eligible = [
        item for item in candidates
        if item.get("platform") == "android" and item.get("package_name")
    ][:MAX_PACKAGE_LOOKUP_CANDIDATES]
    stats = {
        key: {"market_id": key,
              "market_name": collector.display_name or key,
              "attempted": 0, "matched": 0, "failed": 0}
        for key, collector in lookup_collectors.items()
    }
    jobs = []
    for item in eligible:
        existing = {match.get("market_id") for match in item.get("matches", [])}
        for market_id, collector in lookup_collectors.items():
            if market_id not in existing:
                jobs.append((market_id, collector, item))
                stats[market_id]["attempted"] += 1
    if not jobs:
        return {"markets": list(stats.values()), "attempted": 0,
                "matched": 0, "failed": 0}

    def lookup(job):
        market_id, collector, item = job
        result = collector.collect(
            item["package_name"], app_name=item.get("app_name", ""), timeout=timeout,
        )
        return market_id, item, result

    workers = min(6, len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(lookup, job): job[0] for job in jobs}
        for future in as_completed(futures):
            try:
                market_id, item, result = future.result()
            except Exception:
                # A package lookup is enrichment only; it must not turn a valid
                # name-search response into a failed discovery request.
                stats[futures[future]]["failed"] += 1
                continue
            if result.status != ST_OK or not result.version_name:
                if result.status not in ("offline", "not_published"):
                    stats[market_id]["failed"] += 1
                continue
            extra = result.extra or {}
            item["matches"].append({
                "market_id": market_id,
                "market_name": stats[market_id]["market_name"],
                "market_app_id": str(extra.get("market_app_id") or
                                     item["package_name"]),
                "version_name": result.version_name,
                "detail_url": str(extra.get("source_url") or
                                  extra.get("detail_url") or ""),
            })
            if not item.get("developer") and extra.get("developer"):
                item["developer"] = str(extra["developer"])
            stats[market_id]["matched"] += 1
    values = list(stats.values())
    return {
        "markets": values,
        "attempted": sum(item["attempted"] for item in values),
        "matched": sum(item["matched"] for item in values),
        "failed": sum(item["failed"] for item in values),
    }


def search_apps(query: str, timeout: int = 15, search_type: str = "app") -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        raise ValueError("请输入至少 2 个字符的 App 名称或公司名称")
    if search_type not in ("app", "company"):
        raise ValueError("搜索类型必须是 App 名称或公司名称")

    merged = {}
    sources = []
    search_terms = (_company_search_terms(query) if search_type == "company"
                    else _app_search_terms(query))
    collectors = all_collectors()
    searchable = [(market_id, collector)
                  for market_id, collector in collectors.items()
                  if getattr(collector, "supports_search", False)]

    def search_market(entry):
        market_id, collector = entry
        candidates = []
        errors = []
        successful_terms = 0
        seen_market_packages = set()
        for term in search_terms:
            try:
                term_candidates = collector.search(term, timeout=timeout)
                successful_terms += 1
            except Exception as exc:
                errors.append(f"{term}: {type(exc).__name__}")
                continue
            for candidate in term_candidates:
                identity = str(candidate.package_name or candidate.bundle_id or
                               candidate.market_app_id or "").strip()
                identity_key = f"{candidate.platform}:{identity}"
                if not identity or identity_key in seen_market_packages:
                    continue
                seen_market_packages.add(identity_key)
                candidates.append(candidate)
        source = {
            "market_id": market_id,
            "market_name": collector.display_name or market_id,
            "ok": successful_terms > 0,
            "count": len(candidates),
            "searched_terms": successful_terms,
            **({"message": "；".join(errors)} if errors else {}),
        }
        return market_id, collector, candidates, source

    # Different markets are independent and often have multi-second network
    # latency. Search them concurrently while keeping each market's variants
    # sequential to avoid races in stateful clients such as Huawei/Baidu.
    workers = min(6, len(searchable)) or 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        market_results = list(executor.map(search_market, searchable))
    for market_id, collector, market_candidates, source in market_results:
        sources.append(source)
        for candidate in market_candidates:
            item = asdict(candidate)
            platform = item.get("platform") or "android"
            package_name = item["package_name"]
            identity = package_name if platform == "android" else (
                item.get("bundle_id") or item.get("market_app_id")
            )
            if not identity:
                continue
            merge_key = f"{platform}:{identity}"
            current = merged.setdefault(merge_key, {
                "app_name": item["app_name"],
                "platform": platform,
                "package_name": package_name,
                "bundle_id": item.get("bundle_id", ""),
                "ios_app_id": item.get("market_app_id", "") if platform == "ios" else "",
                "developer": item["developer"],
                "operator": item.get("operator", ""),
                "icon_url": item["icon_url"],
                "entities": [],
                "matches": [],
            })
            for entity in (item.get("developer"), item.get("operator")):
                if entity and entity not in current["entities"]:
                    current["entities"].append(entity)
            if _relevance(item, query, search_type) > _relevance(current, query, search_type):
                current.update({
                    "app_name": item["app_name"],
                    "developer": item["developer"],
                    "operator": item.get("operator", ""),
                    "icon_url": item["icon_url"],
                })
            current["matches"].append({
                "market_id": market_id,
                "market_name": collector.display_name or "未知应用市场",
                "market_app_id": item["market_app_id"],
                "version_name": item["version_name"],
                "detail_url": item["detail_url"],
            })

    raw_total = len(merged)
    if search_type == "company":
        q = _normalize(query)
        aliases = [_normalize(x) for x in search_terms[1:]]
        for item in merged.values():
            ranked_entities = sorted(
                item["entities"],
                key=lambda value: (
                    _normalize(value) == q,
                    q in _normalize(value) or _normalize(value) in q,
                    any(alias and alias in _normalize(value) for alias in aliases),
                ),
                reverse=True,
            )
            if ranked_entities:
                item["developer"] = ranked_entities[0]
    candidates = [
        item for item in merged.values()
        if _relevance(item, query, search_type) > 0
    ]
    candidates.sort(
        key=lambda item: (_relevance(item, query, search_type), len(item["matches"])),
        reverse=True,
    )
    if search_type == "app":
        query_name = _app_name(query)
        for item in candidates:
            candidate_name = _app_name(item.get("app_name") or "")
            direct_match = candidate_name == query_name or query_name in candidate_name
            item["entity_match"] = direct_match
            if candidate_name == query_name:
                item["match_reason"] = "名称精确匹配"
            elif query_name in candidate_name:
                item["match_reason"] = "名称包含搜索词"
            else:
                item["match_reason"] = "名称近似，需核对"
    else:
        for item in candidates:
            q = _normalize(query)
            item["entity_match"] = any(
                q == entity or q in entity or entity in q for entity in _entities(item)
            )
            if item["entity_match"]:
                item["match_reason"] = "公司主体匹配"
            else:
                item["match_reason"] = "品牌名匹配，主体待确认"
    package_lookup = _package_lookup_enrichment(candidates, collectors, timeout)
    successful = sum(1 for source in sources if source["ok"])
    return {
        "query": query,
        "search_type": search_type,
        "search_terms": search_terms,
        # 候选数量由各市场公开搜索接口决定，通常只有几十条。完整返回给
        # 浏览器后再按 20 条分页，翻页时不重复请求所有市场。
        "candidates": candidates,
        "total": len(candidates),
        "raw_total": raw_total,
        "filtered_out": raw_total - len(candidates),
        "sources": sources,
        "package_lookup": package_lookup,
        "source_summary": {
            "searched": len(sources),
            "successful": successful,
            "failed": len(sources) - successful,
        },
    }
