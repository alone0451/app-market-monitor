"""B 路径设备执行器（实体手机/模拟器 + 市场 App 自动化）

流程：
  1. 设备就绪检查（ADB 连接）
  2. 确认市场 App 已安装（未安装则提示：在测试设备安装官方 APK）
  3. 打开市场 App → 搜索公司包名 → 进入详情页
  4. 读取详情页版本号 + 截图留证
  5. 设备端仅做详情复核和截图，不自动搜索、点击或安装应用

安装包下载与校验由 core.artifacts 从官方网页直链完成。
"""
import re
import time
from pathlib import Path

import core.db as db
from config import market_display_name
from .adb_device import AdbDevice
from .markets import get_device_driver

# 市场 App 包名映射（安装到设备后自动识别）
MARKET_PACKAGES = {
    "huawei": "com.huawei.appmarket",
    "yyb": "com.tencent.android.qqdownloader",
    "xiaomi": "com.xiaomi.market",
    "oppo": "com.heytap.market",
    "vivo": "com.bbk.appstore",
    "samsung": "com.sec.android.app.samsungapps",
    "meizu": "com.meizu.mstore",
    "honor": "com.hihonor.appmarket",
    "baidu": "com.baidu.appsearch",
    "qihu360": "com.qihoo.appstore",
    "coolapk": "com.coolapk.market",
}

# Some vendors changed the client package across Android/ColorOS generations.
MARKET_PACKAGE_ALIASES = {
    "oppo": ("com.heytap.market", "com.oppo.market"),
}

_VERSION_RE = re.compile(r"(?:^|[\s>])(v?)(\d+(?:\.\d+){1,3})(?:$|[\s<])")


class DeviceExecutor:
    def __init__(self, serial: str = ""):
        self.dev = AdbDevice(serial)

    def check_ready(self) -> tuple[bool, str]:
        report = self.compatibility_report()
        if not report["ready"]:
            return False, report["message"]
        have = [item["market_id"] for item in report["markets"] if item["installed"]]
        profile = report["device"]
        model = " ".join(filter(None, (profile.get("brand"), profile.get("model"))))
        msg = f"设备 {profile.get('serial') or self.dev.serial}"
        if model:
            msg += f"（{model} · Android {profile.get('android') or '未知'}）"
        if have:
            extra = " 已安装市场客户端：" + "、".join(
                market_display_name(market_id) for market_id in have
            )
        else:
            extra = "；未检测到已安装的市场客户端"
        return True, msg + extra

    def compatibility_report(self, market_ids=None) -> dict:
        """Return a read-only phone/client compatibility snapshot."""
        ok, message = self.dev.ready()
        if not ok:
            return {"ready": False, "message": message, "device": {}, "markets": []}
        package_output = self.dev.shell("pm list packages")
        installed_packages = {
            line.removeprefix("package:").strip()
            for line in package_output.splitlines()
            if line.startswith("package:")
        }
        requested = list(market_ids or MARKET_PACKAGES.keys())
        markets = []
        for market_id in requested:
            primary = MARKET_PACKAGES.get(market_id)
            if not primary:
                continue
            candidates = MARKET_PACKAGE_ALIASES.get(market_id, (primary,))
            installed_package = next(
                (candidate for candidate in candidates if candidate in installed_packages), ""
            )
            markets.append({
                "market_id": market_id,
                "market_name": market_display_name(market_id),
                "installed": bool(installed_package),
                "package": installed_package or primary,
            })
        device = {
            "serial": self.dev.serial,
            "brand": self.dev.shell("getprop ro.product.manufacturer").strip(),
            "model": self.dev.shell("getprop ro.product.model").strip(),
            "android": self.dev.shell("getprop ro.build.version.release").strip(),
            "sdk": self.dev.shell("getprop ro.build.version.sdk").strip(),
        }
        return {"ready": True, "message": message, "device": device, "markets": markets}

    def _ensure_market_app(self, market_id: str) -> tuple[bool, str]:
        primary = MARKET_PACKAGES.get(market_id)
        if not primary:
            return False, "未知应用市场"
        candidates = MARKET_PACKAGE_ALIASES.get(market_id, (primary,))
        for pkg in candidates:
            out = self.dev.shell(f"pm path {pkg}")
            if "package:" in out:
                return True, pkg
        package_hint = " / ".join(candidates)
        return False, (f"设备未安装市场 App（{package_hint}）。请在「监测配置 / Android 设备检测」中确认："
                       f"在 Android 测试设备安装官方 APK，或从已装该市场 App 的设备执行 adb pull 提取")

    def _package_identity(self, package_name: str) -> dict[str, str]:
        """Read the installed client version used to scope one-time consent."""
        output = self.dev.shell(f"dumpsys package {package_name}")
        code = re.search(r"versionCode=(\d+)", output)
        name = re.search(r"versionName=([^\s]+)", output)
        return {
            "package_name": package_name,
            "version_code": code.group(1) if code else "",
            "version_name": name.group(1) if name else "",
        }

    def market_initialization_status(self, market_id: str) -> dict:
        """Return persisted one-time bootstrap state without opening the client."""
        ok, package_or_message = self._ensure_market_app(market_id)
        if not ok:
            return {"status": "market_app_missing", "detail": package_or_message}
        identity = self._package_identity(package_or_message)
        # A regional client failure is a channel capability state, not a
        # pending consent state. Reuse the latest recorded device result so
        # the UI does not keep asking for authorization on every refresh.
        unavailable = db.query(
            """SELECT detail, screenshot_url, checked_at FROM results
               WHERE market_id=? AND status='region_unavailable'
               ORDER BY checked_at DESC, id DESC LIMIT 1""",
            (market_id,), one=True,
        )
        if unavailable:
            return {
                "status": "region_unavailable",
                **identity,
                "consented_at": "",
                "screenshot": unavailable["screenshot_url"] or "",
                "detail": unavailable["detail"] or "市场客户端在当前模拟器地区不可用",
            }
        fallback = db.query(
            """SELECT detail, source_url, checked_at FROM results
               WHERE market_id=? AND status='fallback_ok'
               ORDER BY checked_at DESC, id DESC LIMIT 1""",
            (market_id,), one=True,
        )
        if fallback:
            return {
                "status": "fallback_available",
                **identity,
                "consented_at": "",
                "detail": fallback["detail"] or "已取得官方替代证据",
                "source_url": fallback["source_url"] or "",
            }
        row = db.query(
            """SELECT consented_at, detail FROM market_initializations
               WHERE device_serial=? AND market_id=? AND package_name=?
                 AND version_name=? AND version_code=?""",
            (self.dev.serial, market_id, identity["package_name"],
             identity["version_name"], identity["version_code"]), one=True,
        )
        return {
            "status": "initialized" if row else "pending",
            "package_name": identity["package_name"],
            "version_name": identity["version_name"],
            "version_code": identity["version_code"],
            "consented_at": row["consented_at"] if row else "",
            "detail": row["detail"] if row else "首次启动协议尚未确认",
        }

    def initialize_market(self, market_id: str, confirm: bool = False,
                          screenshot_dir: str = "data/screenshots") -> dict:
        """Perform an explicit, one-time client bootstrap.

        ``confirm`` is deliberately required by the caller.  Without it this
        method only reports the detected agreement page.  It never grants
        Android runtime permissions or bypasses a login screen.
        """
        interactive, detail = self.dev.interaction_ready(wake=True)
        if not interactive:
            return {"ok": False, "status": "device_unavailable", "detail": detail}
        ok, package_or_message = self._ensure_market_app(market_id)
        if not ok:
            return {"ok": False, "status": "market_app_missing", "detail": package_or_message}
        package_name = package_or_message
        identity = self._package_identity(package_name)
        existing = self.market_initialization_status(market_id)
        if existing.get("status") == "initialized":
            return {"ok": True, "status": "already_initialized", **existing}

        driver = get_device_driver(market_id, self.dev, package=package_name)
        if not driver or not hasattr(driver, "consent_gate"):
            return {"ok": False, "status": "need_review", "detail": "该市场客户端暂无初始化适配器"}

        self.dev.start_app(package_name)
        nodes = driver._wait_for_nodes(package=package_name)
        before = ""
        after = ""
        setup_actions = []
        for _ in range(5):
            page = driver._page_text(nodes).lower()
            if any(phrase in page for phrase in (
                "not available in current region",
                "service is currently unavailable in your country",
                "当前地区不可用", "当前区域不可用", "本地区暂未提供服务",
                "本区域暂未提供服务", "服务区域不可用", "暂未提供服务",
            )):
                shot = driver._capture(screenshot_dir)
                return {"ok": False, "status": "region_unavailable", "screenshot": shot,
                        **identity, "detail": f"{market_display_name(market_id)}在当前模拟器地区不可用，无法打开市场详情"}
            gate, action, gate_detail = driver.consent_gate(nodes)
            if gate:
                if action is None:
                    shot = driver._capture(screenshot_dir)
                    return {"ok": False, "status": "need_review", "screenshot": shot,
                            "detail": gate_detail}
                if not confirm:
                    shot = driver._capture(screenshot_dir)
                    return {"ok": False, "status": "confirmation_required", "screenshot": shot,
                            **identity, "detail": gate_detail + "；请在巡检系统中确认一次后继续"}
                if not before:
                    before = driver._capture(screenshot_dir)
                setup_actions.append(action.get("text") or action.get("desc") or "协议确认")
                self.dev.tap(action["cx"], action["cy"])
                # The package stays the same across a vendor's permission
                # dialog, so _wait_for_nodes(package=...) can return stale
                # consent nodes. Force a fresh dump after each click.
                time.sleep(2)
                nodes = driver.dev.nodes(driver.dev.dump_ui())
                continue

            # Some clients show their own permission sheet immediately after
            # the agreement.  The caller has explicitly authorized this
            # one-time bootstrap, so allow only an unambiguous allow/deny sheet
            # owned by the market client; never grant permissions via pm grant.
            permission_words = ("permission", "permissions", "allow", "deny",
                                "权限", "应用列表", "通知", "相机", "存储")
            if any(word in page for word in permission_words):
                permission = driver._best_node(
                    nodes, text_words=("允许", "Allow", "同意", "Agree", "继续", "Continue"),
                    id_words=("permit", "allow", "agree", "continue"),
                )
                if permission is None:
                    shot = driver._capture(screenshot_dir)
                    return {"ok": False, "status": "need_review", "screenshot": shot,
                            **identity, "detail": "市场客户端出现权限确认页，但未找到安全的允许按钮"}
                if not confirm:
                    shot = driver._capture(screenshot_dir)
                    return {"ok": False, "status": "confirmation_required", "screenshot": shot,
                            **identity, "detail": "市场客户端需要一次性确认权限，请在巡检系统中明确确认"}
                setup_actions.append(permission.get("text") or permission.get("desc") or "权限确认")
                self.dev.tap(permission["cx"], permission["cy"])
                time.sleep(2)
                nodes = driver.dev.nodes(driver.dev.dump_ui())
                continue

            page_packages = {node.get("package") for node in nodes if node.get("package")}
            if package_name not in page_packages:
                shot = driver._capture(screenshot_dir)
                return {"ok": False, "status": "need_review", "screenshot": shot,
                        "detail": "市场客户端未进入可验证的首页，可能出现了系统权限弹窗"}
            after = driver._capture(screenshot_dir)
            detail_text = "市场客户端首次启动协议已确认，后续巡检无需重复授权"
            if setup_actions:
                detail_text += "；已处理：" + "、".join(setup_actions)
            db.execute(
                """INSERT OR REPLACE INTO market_initializations
                   (device_serial, market_id, package_name, version_name,
                    version_code, screenshot_before, screenshot_after, detail)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (self.dev.serial, market_id, identity["package_name"],
                 identity["version_name"], identity["version_code"],
                 before, after, detail_text),
            )
            return {"ok": True, "status": "initialized", **identity,
                    "screenshot_before": before, "screenshot_after": after,
                    "detail": detail_text}

        shot = driver._capture(screenshot_dir)
        return {"ok": False, "status": "need_review", "screenshot": shot,
                **identity, "detail": "已尝试完成首次启动协议，但客户端仍未进入稳定首页"}

    def inspect_market_detail(self, market_id: str, package_name: str,
                              screenshot_dir: str, app_name: str = "") -> dict:
        """打开市场 App → 搜索应用名 → 读详情版本 + 截图。返回结果 dict。"""
        interactive, detail = self.dev.interaction_ready(wake=True)
        if not interactive:
            return {"ok": False, "status": "device_unavailable", "detail": detail}
        ok, msg = self._ensure_market_app(market_id)
        if not ok:
            return {"ok": False, "status": "market_app_missing", "detail": msg}
        driver = get_device_driver(market_id, self.dev, package=msg)
        if driver:
            return driver.inspect(package_name=package_name, app_name=app_name,
                                  screenshot_dir=screenshot_dir)
        pkg = msg
        self.dev.start_app(pkg)
        # 等待首页，尝试进入搜索
        xml = self.dev.dump_ui()
        search = self.dev.find_node(xml, rid="search_edit_text") or \
                 self.dev.find_node(xml, desc="搜索") or \
                 self.dev.find_node(xml, text="搜索")
        if search:
            self.dev.tap(search["cx"], search["cy"])
            time.sleep(1.5)
            self.dev.input_text(app_name or package_name)
            self.dev.shell("input keyevent 66")  # 回车搜索
            time.sleep(4)
        xml = self.dev.dump_ui()
        # 收集页面文本找版本号
        texts = self.dev.all_texts(xml)
        version = ""
        for t in texts:
            m = _VERSION_RE.search(t)
            if m and len(m.group(2)) >= 3:
                version = m.group(2)
                break
        # 点击匹配的结果项（含包名或名称的节点）
        hit = None
        for kw in (app_name, package_name):
            if not kw:
                continue
            hit = self.dev.find_node(xml, text=kw[:8])
            if hit:
                break
        if hit:
            self.dev.tap(hit["cx"], hit["cy"])
            time.sleep(3)
            xml = self.dev.dump_ui()
            texts = self.dev.all_texts(xml)
            for t in texts:
                m = _VERSION_RE.search(t)
                if m and len(m.group(2)) >= 3:
                    version = m.group(2)
                    break
        # 截图留证
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        shot = str(Path(screenshot_dir) / f"{market_id}_{int(time.time())}.png")
        self.dev.screenshot(shot)
        return {"ok": True, "version": version, "screenshot": shot,
                "detail": f"市场App内读取（{pkg}）" if version else "未在详情页解析到版本号"}

    def download_and_verify(self, market_id: str, package_name: str,
                            baseline_sha256: str = "", baseline_sig: str = "",
                            screenshot_dir: str = "data/screenshots", app_name: str = "") -> dict:
        """旧版设备端自动下载入口。

        各市场的搜索结果无法在点击前稳定核对包名，因此禁止自动搜索和
        点击安装。保留方法是为了让旧调用方得到可解释的安全结果。
        """
        result = {"version_name": "", "sha256": "", "sig": "",
                  "verify_result": "need_review", "screenshot": "", "detail": "",
                  "status": "unsafe_device_download_disabled"}
        result["detail"] = (
            "已禁用 Android 设备端按名称自动搜索和点击下载，因为无法在点击前证明搜索结果"
            f"就是目标包 {package_name}。请改用官方网页安装包校验。"
        )
        return result
