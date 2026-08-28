"""通用国产应用市场真机驱动。

OPPO、vivo、荣耀的控件 ID 会随机型和客户端版本变化，因此优先使用语义
文本、content-desc 与 ID 关键词组合定位。正常巡检不会代替用户接受服务协议、
隐私政策或登录授权；首次启动协议仅由显式的一次性初始化流程处理。
"""
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .yyb import LATIN_IME, _pinyin_query


_VERSION_PATTERNS = (
    re.compile(r"(?:版本信息|版本名称|版本号|当前版本|版本|Version)\s*[:：]?\s*[vV]?\s*(\d+(?:\.\d+){1,4})", re.I),
    re.compile(r"^[vV]?(\d+(?:\.\d+){2,4})$"),
)
_STRONG_CONSENT_WORDS = ("同意并继续", "同意并使用", "阅读并同意")
_CONSENT_PAGE_WORDS = (
    "用户协议", "服务协议", "隐私政策", "隐私声明", "隐私通知",
    "user agreement", "privacy notice", "privacy statement", "terms of use",
)
_CONSENT_ACTION_WORDS = (
    "同意并继续", "同意并使用", "阅读并同意", "同意", "接受", "下一步",
    "继续", "启用", "立即开启", "agree", "accept", "next", "continue",
    "enable now", "turn on now",
)
_LOGIN_WORDS = ("登录后", "手机号登录", "验证码登录", "账号登录", "请登录")
_SEARCH_HINTS = ("搜索应用", "搜索游戏", "搜索", "请输入应用名称")
_SEARCH_ID_HINTS = ("search", "query", "edit", "input")
_DATE_LABELS = ("更新日期", "更新时间", "最近更新", "发布日期", "发布时间", "上架时间", "上线日期",
                "Update time", "Updated", "Release date")
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?")
_RELATIVE_RE = re.compile(r"(\d+)\s*(年前|个月前|月前|周前|天前|小时前)")
_REGION_UNAVAILABLE_WORDS = (
    "not available in current region",
    "service is currently unavailable in your country",
    "当前地区不可用", "当前区域不可用", "本地区暂未提供服务", "本区域暂未提供服务",
    "服务区域不可用", "暂未提供服务",
)


def parse_version(texts: list[str]) -> str:
    """Parse a labelled app version without mistaking dates or download counts."""
    cleaned = [str(value).strip() for value in texts if str(value).strip()]
    combined = "  ".join(cleaned)
    for pattern in _VERSION_PATTERNS[:1]:
        match = pattern.search(combined)
        if match:
            return match.group(1)
    # A bare semantic-version node is accepted only when adjacent UI text says
    # this is a version field.
    for index, value in enumerate(cleaned):
        if not any(word in value for word in ("版本", "Version", "version")):
            continue
        for nearby in cleaned[index:index + 3]:
            match = _VERSION_PATTERNS[1].match(nearby)
            if match:
                return match.group(1)
    return ""


def parse_published_at(texts: list[str]) -> str:
    """Extract a publish/update date from labelled UI text.

    Returns a normalized absolute date (YYYY-MM-DD) when a date label is
    present, otherwise the raw relative expression such as “1个月前” so the
    pipeline can convert it to an approximate date.
    """
    cleaned = [str(value).strip() for value in texts if str(value).strip()]
    combined = "  ".join(cleaned)
    for label in _DATE_LABELS:
        index = combined.find(label)
        if index < 0:
            continue
        match = _DATE_RE.search(combined[index: index + 40])
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    for value in cleaned:
        match = _RELATIVE_RE.search(value)
        if match:
            return match.group(0).replace(" ", "")
    return ""


def find_company_name(texts: list[str]) -> str:
    """Extract a company name from UI text (developer/issuer row).

    Prefers names ending in 股份有限公司/有限公司; falls back to any value
    containing 有限公司 when the accessibility tree truncates the suffix.
    Values that are actually record/operator rows (京ICP、主办单位) are skipped.
    """
    values = [str(value).strip() for value in texts if str(value).strip()]
    full = [v for v in values if re.search(r"(?:股份有限公司|有限公司)\s*[….]*$", v)]
    if full:
        return max(full, key=len)
    fallback = [
        v for v in values
        if "有限公司" in v
        and re.search(r"[….]$", v)
        and not re.search(r"主办单位|备案号|京ICP|\bICP\b", v)
    ]
    return max(fallback, key=len) if fallback else ""


def parse_entities(texts: list[str]) -> dict[str, str]:
    """Read developer and operating/legal entities as separate labelled fields."""
    values = [str(value).strip() for value in texts if str(value).strip()]
    groups = {
        "developer": ("开发者详情", "开发者", "开发商", "发布者", "供应商"),
        "operator": ("运营者", "运营主体", "主办者", "主办单位", "开发运营者"),
    }
    all_labels = tuple(label for labels in groups.values() for label in labels)
    boundary_labels = all_labels + (
        "版本信息", "版本名称", "版本号", "当前版本", "版本", "更新时间", "更新日期", "上线日期",
        "核准主体", "核准号",
        "隐私政策", "权限", "应用权限", "应用分级", "备案号", "核准号",
    )
    boundary_pattern = re.compile(
        r"\s+(?:" + "|".join(map(re.escape, sorted(boundary_labels, key=len, reverse=True)))
        + r")\s*[:：]?"
    )

    def clean_entity(value: str) -> str:
        # Some accessibility trees merge adjacent rows, for example
        # "示例科技控股股份有限公司 版本 8.2.40".  Keep only the entity row.
        candidate = value.lstrip(" ：:").strip()
        return boundary_pattern.split(candidate, maxsplit=1)[0].strip(" ：:|")

    found = {"developer": "", "operator": ""}
    for key, labels in groups.items():
        for index, value in enumerate(values):
            label = next((candidate for candidate in labels if value.startswith(candidate)), None)
            if not label:
                continue
            inline = clean_entity(value[len(label):])
            if inline and not inline.startswith(all_labels):
                found[key] = inline
                break
            for nearby in values[index + 1:index + 4]:
                candidate = clean_entity(nearby)
                if candidate and candidate not in all_labels and not candidate.startswith(all_labels):
                    found[key] = candidate
                    break
            if found[key]:
                break
    return found


def _ocr_version(path: str) -> tuple[str, str]:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return "", "未安装 tesseract，已保存详情页截图供人工确认"
    try:
        run = subprocess.run(
            [tesseract, path, "stdout", "-l", "chi_sim+eng", "--psm", "6"],
            shell=False, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"截图 OCR 失败: {exc}"
    text = (run.stdout or "") + (run.stderr or "")
    return parse_version(text.splitlines()), text


class GenericStoreDriver:
    market_id = ""
    package = ""
    display_name = "应用市场"
    detail_scheme = "market://details?id={package_name}"
    allow_pinyin_fallback = False

    def __init__(self, device):
        self.dev = device

    @staticmethod
    def _page_text(nodes: list[dict]) -> str:
        return " ".join(filter(None, (
            " ".join((node.get("text", ""), node.get("desc", ""))).strip()
            for node in nodes
        )))

    def _blocked(self, nodes: list[dict]) -> tuple[bool, str]:
        page = self._page_text(nodes)
        if any(word in page.lower() for word in _REGION_UNAVAILABLE_WORDS):
            return True, f"{self.display_name}在当前模拟器地区不可用，无法打开市场详情"
        has_terms = any(word in page for word in ("用户协议", "服务协议", "隐私政策"))
        has_consent_action = any(word in page for word in ("不同意", "暂不同意", "退出应用", "同意并"))
        if any(word in page for word in _STRONG_CONSENT_WORDS) or (has_terms and has_consent_action):
            return True, "检测到首次启动协议或隐私授权页，请在 Android 设备上阅读并手工处理后重试"
        if any(word in page for word in _LOGIN_WORDS):
            return True, "市场客户端要求登录或短信验证码，请先在 Android 设备上完成登录后重试"
        return False, ""

    @staticmethod
    def _status_for_reason(reason: str) -> str:
        lowered = (reason or "").lower()
        if "地区不可用" in reason or "region" in lowered or "country" in lowered:
            return "region_unavailable"
        if "登录" in reason:
            return "login_required"
        return "need_review"

    def consent_gate(self, nodes: list[dict]) -> tuple[bool, dict | None, str]:
        """Detect a first-run market agreement without accepting it.

        The normal inspection path must remain consent-safe.  The explicit
        bootstrap endpoint calls this helper before clicking a button, so a
        similarly labelled action on a normal detail page cannot be accepted
        accidentally.
        """
        page = self._page_text(nodes).lower()
        has_policy = any(word.lower() in page for word in _CONSENT_PAGE_WORDS)
        if not has_policy:
            return False, None, ""
        action = self._best_node(
            nodes, text_words=_CONSENT_ACTION_WORDS,
            id_words=("agree", "consent", "agreement", "next"),
        )
        if not action:
            return True, None, "检测到市场客户端协议页，但未找到确认按钮"
        return True, action, "检测到市场客户端首次启动协议页"

    @staticmethod
    def _best_node(nodes: list[dict], *, text_words=(), id_words=(), editable=False):
        candidates = []
        for node in nodes:
            if not node.get("enabled", True):
                continue
            label = f"{node.get('text', '')} {node.get('desc', '')}".lower()
            rid = node.get("rid", "").lower()
            score = 0
            if any(word.lower() in label for word in text_words):
                score += 8
            if any(word.lower() in rid for word in id_words):
                score += 5
            if editable and "edittext" in node.get("class", "").lower():
                score += 6
            if node.get("clickable"):
                score += 2
            if score:
                candidates.append((score, node))
        return max(candidates, key=lambda value: value[0])[1] if candidates else None

    def _safe_dismiss(self, nodes: list[dict]) -> bool:
        """Dismiss non-consent overlays only; never accept terms or permissions."""
        node = self._best_node(
            nodes, text_words=("以后再说", "暂不", "取消", "跳过", "知道了", "我知道了", "关闭"),
        )
        if not node:
            return False
        self.dev.tap(node["cx"], node["cy"])
        return True

    def _capture(self, screenshot_dir: str) -> str:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        shot = str(Path(screenshot_dir) / f"{self.market_id}_{int(time.time())}.png")
        return shot if self.dev.screenshot(shot) else ""

    def _wait_for_nodes(self, package: str = "", attempts: int = 8,
                        delay: float = 1.5) -> list[dict]:
        """Re-dump the UI until nodes are available (optionally from a market
        package).  Vendor clients render slowly on cold start, and a single
        dump taken too early would otherwise be treated as a failure."""
        last = []
        for _ in range(attempts):
            last = self.dev.nodes(self.dev.dump_ui())
            if not last:
                time.sleep(delay)
                continue
            if not package:
                return last
            page_packages = {node.get("package") for node in last if node.get("package")}
            if package in page_packages:
                return last
            time.sleep(delay)
        return last

    def _open_package_detail(self, package_name: str) -> tuple[bool, list[dict]]:
        """Open a market-owned package detail route without using any IME."""
        uri = self.detail_scheme.format(package_name=package_name)
        self.dev.shell(f"am force-stop {self.package}")
        output = self.dev.shell(
            f"am start -S -W -a android.intent.action.VIEW -d '{uri}' -p {self.package}",
            timeout=30,
        )
        failed = any(word in output for word in ("Error:", "unable to resolve", "does not exist"))
        if failed:
            return False, []
        nodes = self._wait_for_nodes(package=self.package)
        return bool(nodes), nodes

    def _read_opened_detail(self, package_name: str, app_name: str,
                            screenshot_dir: str, nodes: list[dict]):
        """Read a directly opened detail page, revealing folded metadata if needed."""
        page_packages = {node.get("package") for node in nodes if node.get("package")}
        if page_packages and self.package not in page_packages:
            return None
        # Honor (and some regional vendor builds) can accept the URI but render
        # a service-region block instead of a detail page.  Surface that exact
        # condition before the title/deep-link validation so it is not reported
        # as the generic "upgrade client" error.
        page_lower = self._page_text(nodes).lower()
        if any(word in page_lower for word in _REGION_UNAVAILABLE_WORDS):
            return self._result(
                False, f"{self.display_name}在当前模拟器地区不可用，无法打开市场详情",
                screenshot_dir, status="region_unavailable",
            )

        def target_is_primary(current_nodes):
            # A recommendation far below the fold must never be mistaken for the
            # currently opened app. A package deep-link is trusted, but some market
            # clients omit the title from accessibility nodes and expose only the
            # English/Chinese detail metadata (for example ``Version: 2.1.4``).
            prominent = [value for node in current_nodes
                         for value in (node.get("text", ""), node.get("desc", ""))
                         if value][:8]
            if any(app_name in value for value in prominent):
                return True
            texts = [value for node in current_nodes
                     for value in (node.get("text", ""), node.get("desc", ""))
                     if value]
            # Several clients render the title below the first viewport.  Accept
            # it when the accessibility resource identifies a detail-title row,
            # but do not accept an arbitrary recommendation containing the name.
            title_ids = ("package_detail_title", "downloader_app_name", "app_name",
                         "detail_title", "app_title")
            if any(app_name in (node.get("text", "") or node.get("desc", ""))
                   and any(token in node.get("rid", "") for token in title_ids)
                   for node in current_nodes):
                return True
            marker_count = sum(any(marker.lower() in value.lower()
                                   for marker in ("version", "版本", "app introduction", "应用介绍",
                                                  "update time", "更新时间"))
                               for value in texts)
            detail_ids = ("package_detail", "detail_content", "app_detail",
                          "tv_ver_and_time", "version")
            has_detail_id = any(any(token in node.get("rid", "")
                                    for token in detail_ids)
                                for node in current_nodes)
            return has_detail_id and (bool(parse_version(texts)) or marker_count >= 2)

        if not target_is_primary(nodes):
            # 详情页可能仍在加载：先重试几次，再决定是否放弃直达路径。
            for _ in range(3):
                time.sleep(1.5)
                nodes = self.dev.nodes(self.dev.dump_ui())
                if target_is_primary(nodes):
                    break
            else:
                return None
        clicked_app = any(
            "应用详情" in (node.get("text", "") or node.get("desc", ""))
            for node in nodes
        )
        for _ in range(4):
            blocked, reason = self._blocked(nodes)
            if blocked:
                return self._result(False, reason, screenshot_dir,
                                    status=self._status_for_reason(reason))
            texts = [value for node in nodes
                     for value in (node.get("text", ""), node.get("desc", ""))]
            version = parse_version(texts)
            if version:
                entities = parse_entities(texts)
                if self.market_id == "oppo" and not entities["developer"]:
                    entities["developer"] = next((
                        value.strip() for value in texts
                        if re.search(r"(?:股份有限公司|有限公司)$", value.strip())
                    ), "")
                published_at = parse_published_at(texts)
                if self.market_id == "honor" and not published_at:
                    # 荣耀详情页把最近更新日期折叠在“更多”里，展开后再解析一次。
                    for _ in range(3):
                        more = self._best_node(nodes, text_words=("更多",), id_words=())
                        if not more:
                            break
                        self.dev.tap(more["cx"], more["cy"])
                        time.sleep(2)
                        nodes = self.dev.nodes(self.dev.dump_ui())
                        texts = [value for node in nodes
                                 for value in (node.get("text", ""), node.get("desc", ""))]
                        published_at = parse_published_at(texts)
                        if published_at:
                            break
                    if not published_at:
                        # 部分应用的日期在介绍展开后的下方，
                        # 需要向下滚动几屏才能读到“最近更新”。
                        for _ in range(6):
                            self.dev.shell("input swipe 540 1700 540 700 400")
                            time.sleep(1.2)
                            nodes = self.dev.nodes(self.dev.dump_ui())
                            texts = [value for node in nodes
                                     for value in (node.get("text", ""), node.get("desc", ""))]
                            published_at = parse_published_at(texts)
                            if published_at:
                                break
                return self._result(
                    True, f"{self.display_name}按包名直达详情：版本 {version}；目标包名 {package_name}",
                    screenshot_dir, status="ok", version=version,
                    published_at=published_at, **entities,
                )

            reveal = self._best_node(
                nodes, text_words=("查看应用详情", "应用详情", "详细信息"), id_words=(),
            )
            if reveal:
                self.dev.tap(reveal["cx"], reveal["cy"])
            elif not clicked_app:
                target = self._best_node(nodes, text_words=(app_name,), id_words=())
                if not target:
                    break
                self.dev.tap(target["cx"], target["cy"])
                clicked_app = True
            else:
                self.dev.shell("input swipe 500 1700 500 1150 450")
                time.sleep(1)
            nodes = self.dev.nodes(self.dev.dump_ui())

        shot = self._capture(screenshot_dir)
        version, ocr_detail = _ocr_version(shot) if shot else ("", "")
        if version:
            entities = parse_entities(texts)
            return {"ok": True, "status": "ok", "version": version, "screenshot": shot,
                    **entities,
                    "detail": f"{self.display_name}按包名直达详情并经截图识别：版本 {version}"}
        entities = parse_entities(texts)
        return {"ok": False, "status": "need_review", "screenshot": shot, **entities,
                "detail": (f"已按包名打开{self.display_name}中的“{app_name}”，但未自动读取版本号；"
                           f"请查看现场截图。{ocr_detail[:100]}")}

    def _result(self, ok: bool, detail: str, screenshot_dir: str,
                status: str = "need_review", version: str = "", **extra) -> dict:
        result = {"ok": ok, "status": status, "version": version,
                  "screenshot": self._capture(screenshot_dir), "detail": detail}
        result.update(extra)
        return result

    def inspect(self, package_name: str, app_name: str, screenshot_dir: str) -> dict:
        if not app_name:
            return {"ok": False, "status": "need_review", "detail": "Android 设备端搜索需要应用名称"}
        query = _pinyin_query(app_name)
        if not query:
            return {"ok": False, "status": "need_review",
                    "detail": "中文应用名转拼音依赖缺失，请安装 requirements-device.txt"}

        original_ime = self.dev.shell("settings get secure default_input_method").strip()
        try:
            direct_ok, nodes = self._open_package_detail(package_name)
            if direct_ok:
                direct_result = self._read_opened_detail(
                    package_name, app_name, screenshot_dir, nodes,
                )
                if direct_result is not None:
                    return direct_result

            if not self.allow_pinyin_fallback:
                reason = (f"{self.display_name}客户端未能按包名打开“{app_name}”详情；"
                          "为避免拼音召回错误，已停止自动搜索，请升级市场客户端后重试")
                return self._result(False, reason, screenshot_dir)

            # Compatibility fallback for old market clients that do not support
            # package-detail schemes. This path may use a pinyin query.
            self.dev.shell(f"am force-stop {self.package}")
            self.dev.start_app(self.package)
            nodes = self._wait_for_nodes(package=self.package)
            blocked, reason = self._blocked(nodes)
            if blocked:
                return self._result(False, reason, screenshot_dir,
                                    status=self._status_for_reason(reason))
            if self._safe_dismiss(nodes):
                nodes = self._wait_for_nodes(package=self.package)

            search = self._best_node(nodes, text_words=_SEARCH_HINTS,
                                     id_words=_SEARCH_ID_HINTS, editable=True)
            if not search:
                return self._result(
                    False, f"{self.display_name}首页未找到搜索入口，客户端界面可能已更新",
                    screenshot_dir,
                )
            self.dev.tap(search["cx"], search["cy"])
            xml = self.dev.dump_ui()
            nodes = self.dev.nodes(xml)
            blocked, reason = self._blocked(nodes)
            if blocked:
                return self._result(False, reason, screenshot_dir,
                                    status=self._status_for_reason(reason))

            field = self._best_node(nodes, text_words=_SEARCH_HINTS,
                                    id_words=_SEARCH_ID_HINTS, editable=True)
            if not field:
                return self._result(False, f"{self.display_name}搜索输入框未找到", screenshot_dir)
            self.dev.shell(f"ime set {LATIN_IME}")
            self.dev.tap(field["cx"], field["cy"])
            self.dev.shell("input keyevent 28")
            self.dev.input_text(query)
            self.dev.shell("input keyevent 66")
            time.sleep(4)

            xml = self.dev.dump_ui()
            nodes = self.dev.nodes(xml)
            blocked, reason = self._blocked(nodes)
            if blocked:
                return self._result(False, reason, screenshot_dir,
                                    status=self._status_for_reason(reason))
            hit = self._best_node(nodes, text_words=(app_name,), id_words=())
            if not hit:
                return self._result(
                    False, f"{self.display_name}未精确召回“{app_name}”（搜索词 {query}）",
                    screenshot_dir,
                )
            self.dev.tap(hit["cx"], hit["cy"])
            time.sleep(3)

            xml = self.dev.dump_ui()
            nodes = self.dev.nodes(xml)
            blocked, reason = self._blocked(nodes)
            if blocked:
                status = "login_required" if "登录" in reason else "need_review"
                return self._result(False, reason, screenshot_dir, status=status)
            texts = [value for node in nodes for value in (node.get("text", ""), node.get("desc", ""))]
            version = parse_version(texts)
            if not version:
                # Version information often lives below the fold on a detail page.
                self.dev.shell("input swipe 500 1600 500 650 500")
                time.sleep(2)
                xml = self.dev.dump_ui()
                nodes = self.dev.nodes(xml)
                texts = [value for node in nodes for value in (node.get("text", ""), node.get("desc", ""))]
                version = parse_version(texts)

            shot = self._capture(screenshot_dir)
            ocr_detail = ""
            if not version and shot:
                version, ocr_detail = _ocr_version(shot)
            if not version:
                return {"ok": False, "status": "need_review", "screenshot": shot,
                        "detail": (f"已打开{self.display_name}中的“{app_name}”详情页，但未自动识别版本号；"
                                   f"请查看现场截图。{ocr_detail[:100]}")}
            return {"ok": True, "status": "ok", "version": version, "screenshot": shot,
                    "detail": f"{self.display_name} Android 设备端详情页采集：版本 {version}；目标包名 {package_name}"}
        finally:
            if original_ime and original_ime != "null":
                self.dev.shell(f"ime set {original_ime}")


class OppoDeviceDriver(GenericStoreDriver):
    market_id = "oppo"
    package = "com.heytap.market"
    display_name = "OPPO 软件商店"
    browser_packages = (
        "org.mozilla.firefox", "com.android.chrome", "com.heytap.browser",
        "com.coloros.browser", "com.android.browser",
    )
    helper_package = "com.local.appmarketbridge"
    helper_activity = ".BridgeActivity"

    def _read_opened_detail(self, package_name: str, app_name: str,
                            screenshot_dir: str, nodes: list[dict]):
        """Read OPPO 12.x using stable resource IDs, not ambiguous detail text."""
        page_packages = {node.get("package") for node in nodes if node.get("package")}
        if page_packages and self.package not in page_packages:
            return None
        prominent = [value for node in nodes
                     for value in (node.get("text", ""), node.get("desc", ""))
                     if value][:12]
        if not any(app_name in value for value in prominent):
            # 详情页可能仍在加载：先重试几次，再决定是否放弃直达路径。
            for _ in range(3):
                time.sleep(1.5)
                nodes = self.dev.nodes(self.dev.dump_ui())
                prominent = [value for node in nodes
                             for value in (node.get("text", ""), node.get("desc", ""))
                             if value][:12]
                if any(app_name in value for value in prominent):
                    break
            else:
                return None

        for step in range(4):
            blocked, reason = self._blocked(nodes)
            if blocked:
                status = "login_required" if "登录" in reason else "need_review"
                return self._result(False, reason, screenshot_dir, status=status)
            texts = [value for node in nodes
                     for value in (node.get("text", ""), node.get("desc", ""))]
            version = parse_version(texts)
            if version:
                entities = parse_entities(texts)
                published_at = parse_published_at(texts)
                if not entities["developer"]:
                    entities["developer"] = find_company_name(texts)
                if not entities["developer"]:
                    # 公司行通常位于版本行下方一屏，轻滑后重读一次。
                    self.dev.shell("input swipe 540 1500 540 1000 350")
                    time.sleep(1.2)
                    more_nodes = self.dev.nodes(self.dev.dump_ui())
                    more_texts = [value for node in more_nodes
                                  for value in (node.get("text", ""), node.get("desc", ""))]
                    more_entities = parse_entities(more_texts)
                    if not more_entities["developer"]:
                        more_entities["developer"] = find_company_name(more_texts)
                    for key in ("developer", "operator"):
                        if not entities.get(key) and more_entities.get(key):
                            entities[key] = more_entities[key]
                    published_at = published_at or parse_published_at(more_texts)
                return self._result(
                    True, f"OPPO 软件商店官方包名详情：版本 {version}；"
                          f"目标包名 {package_name}",
                    screenshot_dir, status="ok", version=version,
                    published_at=published_at, **entities,
                )

            if step == 0:
                toggle = next((node for node in nodes
                               if "show_more_area_ll" in node.get("rid", "") and
                               node.get("clickable")), None)
                if not toggle:
                    return None
                self.dev.tap(toggle["cx"], toggle["cy"])
                time.sleep(2)
            else:
                self.dev.shell("input swipe 500 1700 500 1150 450")
                time.sleep(1)
            nodes = self.dev.nodes(self.dev.dump_ui())

        return self._result(
            False, f"已打开 OPPO 软件商店中的“{app_name}”，但未读取到版本",
            screenshot_dir,
        )

    def inspect(self, package_name: str, app_name: str, screenshot_dir: str) -> dict:
        """Try the official package route, then an exact Chinese clipboard search."""
        if not app_name:
            return {"ok": False, "status": "need_review", "detail": "Android 设备端搜索需要应用名称"}
        direct_ok, nodes = self._open_package_detail(package_name)
        if direct_ok:
            direct_result = self._read_opened_detail(
                package_name, app_name, screenshot_dir, nodes,
            )
            if direct_result is not None:
                return direct_result
        return self._search_with_chinese_clipboard(
            package_name, app_name, screenshot_dir, nodes,
        )

    def _search_with_chinese_clipboard(self, package_name: str, app_name: str,
                                       screenshot_dir: str,
                                       nodes: list[dict]) -> dict:
        if "package:" not in self.dev.shell(f"pm path {self.helper_package}"):
            return self._result(
                False, "OPPO 中文搜索桥未安装，无法避免拼音输入造成的误召回",
                screenshot_dir,
            )

        safe_name = shlex.quote(app_name)
        self.dev.shell(f"am force-stop {self.helper_package}")
        self.dev.shell(
            f"am start -n {self.helper_package}/{self.helper_activity} "
            f"--es target_package {package_name} --es query_text {safe_name}",
            timeout=30,
        )
        nodes = self._wait_for_nodes(package=self.package)
        page_packages = {node.get("package") for node in nodes if node.get("package")}
        if self.package not in page_packages:
            return self._result(False, "OPPO 软件商店未成功打开", screenshot_dir)

        search = self._best_node(nodes, text_words=_SEARCH_HINTS,
                                 id_words=_SEARCH_ID_HINTS, editable=True)
        if not search:
            return self._result(False, "OPPO 软件商店首页未找到搜索入口", screenshot_dir)
        self.dev.tap(search["cx"], search["cy"])
        nodes = self._wait_for_nodes(package=self.package)
        field = self._best_node(nodes, text_words=_SEARCH_HINTS,
                                id_words=_SEARCH_ID_HINTS, editable=True)
        if not field:
            return self._result(False, "OPPO 软件商店搜索输入框未找到", screenshot_dir)

        # Long-press and paste the Chinese name copied by the permission-free
        # helper.  This avoids pinyin and does not change the phone's IME.
        self.dev.tap(field["cx"], field["cy"])
        self.dev.shell(
            f"input swipe {field['cx']} {field['cy']} {field['cx']} {field['cy']} 900"
        )
        time.sleep(1)
        menu_nodes = self.dev.nodes(self.dev.dump_ui())
        paste = self._best_node(menu_nodes, text_words=("粘贴", "Paste"), id_words=())
        if not paste:
            return self._result(False, "OPPO 搜索框未出现粘贴操作", screenshot_dir)
        self.dev.tap(paste["cx"], paste["cy"])
        self.dev.shell("input keyevent 66")
        time.sleep(5)

        nodes = self._wait_for_nodes(package=self.package)
        page_packages = {node.get("package") for node in nodes if node.get("package")}
        if self.package not in page_packages:
            return self._result(False, "OPPO 搜索结果页所属渠道校验失败", screenshot_dir)
        hit = self._best_node(nodes, text_words=(app_name,), id_words=())
        if not hit:
            return self._result(
                False, f"OPPO 软件商店未精确召回“{app_name}”",
                screenshot_dir, status="offline",
            )
        self.dev.tap(hit["cx"], hit["cy"])
        time.sleep(4)
        detail_nodes = self._wait_for_nodes(package=self.package)
        detail_result = self._read_opened_detail(
            package_name, app_name, screenshot_dir, detail_nodes,
        )
        if detail_result is not None:
            detail_result["detail"] = detail_result.get("detail", "").replace(
                "按包名直达详情", "中文精确名称搜索详情（包名未由客户端展示）"
            )
            return detail_result
        return self._result(
            False, f"OPPO 已精确召回“{app_name}”，但未能读取详情版本",
            screenshot_dir,
        )

    def _open_package_detail(self, package_name: str) -> tuple[bool, list[dict]]:
        """Open OPPO detail from a foreground browser Activity.

        OPPO's documented deep link explicitly requires startActivityForResult
        without FLAG_ACTIVITY_NEW_TASK.  ``adb shell am start`` always launches
        as a new task, so the 12.x client falls back to its home page.  A tiny
        localhost bridge gives the browser a real foreground click; the browser
        then launches the same official ``market://details?id=...`` URI.
        """
        # Preferred path: an installed, permission-free Activity performs the
        # exact startActivityForResult call required by OPPO 12.x.
        if "package:" in self.dev.shell(f"pm path {self.helper_package}"):
            self.dev.shell(f"am force-stop {self.package}")
            self.dev.shell(f"am force-stop {self.helper_package}")
            output = self.dev.shell(
                f"am start -n {self.helper_package}/{self.helper_activity} "
                f"--es target_package {package_name}", timeout=30,
            )
            failed = any(word in output for word in
                         ("Error:", "unable to resolve", "does not exist"))
            nodes = [] if failed else self._wait_for_nodes(package=self.package)
            page_packages = {node.get("package") for node in nodes if node.get("package")}
            if not failed and self.package in page_packages:
                return True, nodes

        # Compatibility fallback for machines where the helper APK has not
        # been installed yet.  Newer clients may accept a foreground browser.
        if not self.dev.reverse(5001):
            return super()._open_package_detail(package_name)

        browser = ""
        for candidate in self.browser_packages:
            if "package:" in self.dev.shell(f"pm path {candidate}"):
                browser = candidate
                break
        if not browser:
            return super()._open_package_detail(package_name)

        self.dev.shell(f"am force-stop {self.package}")
        bridge_url = (
            "http://127.0.0.1:5001/device/oppo-bridge"
            f"?package_name={package_name}&nonce={int(time.time() * 1000)}"
        )
        output = self.dev.shell(
            "am start -W -a android.intent.action.VIEW "
            f"-d '{bridge_url}' -p {browser}",
            timeout=30,
        )
        failed = any(word in output for word in ("Error:", "unable to resolve", "does not exist"))
        if failed:
            return super()._open_package_detail(package_name)
        nodes = self._wait_for_nodes(package=browser)

        bridge = self._best_node(
            nodes, text_words=("打开 OPPO 软件商店",), id_words=(),
        )
        if not bridge:
            return super()._open_package_detail(package_name)
        self.dev.tap(bridge["cx"], bridge["cy"])
        time.sleep(4)

        # Browsers may show a navigation-only confirmation.  Never accept
        # permissions, terms, logins or downloads here.
        for _ in range(2):
            nodes = self.dev.nodes(self.dev.dump_ui())
            confirm = next((node for node in nodes if node.get("enabled", True) and
                            (node.get("text") or node.get("desc")) in
                            ("打开", "打开链接", "继续打开", "仅此一次", "仅此次")), None)
            if not confirm:
                break
            self.dev.tap(confirm["cx"], confirm["cy"])
            time.sleep(4)
        page_packages = {node.get("package") for node in nodes if node.get("package")}
        if self.package not in page_packages:
            nodes = self._wait_for_nodes(package=self.package)
            page_packages = {node.get("package") for node in nodes if node.get("package")}
        return bool(nodes) and self.package in page_packages, nodes


class VivoDeviceDriver(GenericStoreDriver):
    market_id = "vivo"
    package = "com.bbk.appstore"
    display_name = "vivo 应用商店"


class HonorDeviceDriver(GenericStoreDriver):
    market_id = "honor"
    package = "com.hihonor.appmarket"
    display_name = "荣耀应用市场"
    # 荣耀市场注册的深链 authority 是 app_details；写成 details 会匹配失败，
    # am start 报 unable to resolve，导致“未能按包名打开详情”。
    detail_scheme = "honormarket://app_details?id={package_name}"


class BaiduDeviceDriver(GenericStoreDriver):
    """百度手机助手设备端驱动。

    百度网页接口仍是主路径；仅在设备上已经安装百度手机助手且网页查询
    不可用时，使用 Android 标准 ``market://`` 详情深链做补充复核。这样
    不会把模拟器缺少客户端误报成“百度未上架”，也不会替换稳定的网页证据。
    """
    market_id = "baidu"
    package = "com.baidu.appsearch"
    display_name = "百度手机助手"
    detail_scheme = "market://details?id={package_name}"

    def _read_opened_detail(self, package_name: str, app_name: str,
                            screenshot_dir: str, nodes: list[dict]):
        # 百度客户端的详情深链在部分版本会恢复上一次的截图查看器，
        # 该页面只有 ImageView，不能作为应用详情证据。退回一层后再读取
        # 带有应用名、版本信息和上线日期的详情页。
        has_image_view = any(
            "imageView" in (node.get("rid") or "")
            for node in nodes
        )
        has_detail_title = any(
            "app_detail_header_app_name" in (node.get("rid") or "")
            for node in nodes
        )
        if has_image_view and not has_detail_title:
            self.dev.key_back()
            time.sleep(1.5)
            nodes = self._wait_for_nodes(package=self.package)

        result = super()._read_opened_detail(
            package_name, app_name, screenshot_dir, nodes,
        )
        if result is not None and result.get("ok"):
            result["detail"] = (
                f"百度手机助手客户端按包名打开详情：版本 {result.get('version', '')}；"
                f"目标包名 {package_name}"
            )
        return result


class HuaweiDeviceDriver(GenericStoreDriver):
    market_id = "huawei"
    package = "com.huawei.appmarket"
    display_name = "华为应用市场"
    detail_scheme = "appmarket://details?id={package_name}"
