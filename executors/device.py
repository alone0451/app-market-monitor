"""B 路径设备执行器（实体手机/模拟器 + 市场 App 自动化）

流程：
  1. 设备就绪检查（ADB 连接）
  2. 确认市场 App 已安装（未安装则提示：从测试手机提取或安装官方 APK）
  3. 打开市场 App → 搜索公司包名 → 进入详情页
  4. 读取详情页版本号 + 截图留证
  5. 手机端仅做详情复核和截图，不自动搜索、点击或安装应用

安装包下载与校验由 core.artifacts 从官方网页直链完成。
"""
import re
import time
from pathlib import Path

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
        return False, (f"设备未安装市场App（{package_hint}）。请在「监测配置 / USB 手机检测」中确认："
                       f"从已装该市场 App 的手机执行 adb pull 提取，或安装官方 APK")

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
        """旧版手机自动下载入口。

        各市场的搜索结果无法在点击前稳定核对包名，因此禁止自动搜索和
        点击安装。保留方法是为了让旧调用方得到可解释的安全结果。
        """
        result = {"version_name": "", "sha256": "", "sig": "",
                  "verify_result": "need_review", "screenshot": "", "detail": "",
                  "status": "unsafe_device_download_disabled"}
        result["detail"] = (
            "已禁用手机端按名称自动搜索和点击下载，因为无法在点击前证明搜索结果"
            f"就是目标包 {package_name}。请改用官方网页安装包校验。"
        )
        return result
