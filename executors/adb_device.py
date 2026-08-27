"""ADB 设备驱动（零额外依赖：uiautomator dump + input 命令）
封装：设备发现、启动 App、UI 层次解析、点击/输入/滑动、截图、安装/卸载、APK 提取。
"""
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path


from core.env_check import find_adb, parse_adb_devices


def _run(cmd, timeout=30):
    r = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class AdbDevice:
    def __init__(self, serial: str = ""):
        self.adb = find_adb()[0] or "adb"
        self.serial = serial or self._pick()

    def _pick(self) -> str:
        code, out = _run([self.adb, "devices"])
        devices = [d for d in parse_adb_devices(out) if d["state"] == "device"]
        for device in devices:
            if not device["is_emulator"]:
                return device["serial"]
        for device in devices:
            if device["is_emulator"]:
                return device["serial"]
        return ""

    def ready(self) -> tuple[bool, str]:
        if not self.serial:
            return False, "未检测到可用设备（USB 手机或模拟器）"
        code, out = _run([self.adb, "-s", self.serial, "shell", "echo", "ok"])
        return ("ok" in out), f"设备 {self.serial}"

    def interaction_ready(self, wake: bool = True) -> tuple[bool, str]:
        """Check whether UI automation can safely interact with the device.

        ADB can stay connected while a phone is asleep or locked.  In that
        state taps and text input either go nowhere or hit the lock screen, so
        it must not be reported as a market query failure.
        """
        ok, message = self.ready()
        if not ok:
            return False, message
        power = self.shell("dumpsys power")
        awake = bool(re.search(
            r"mWakefulness=Awake|Display Power:\s*state=ON|mScreenOn=true",
            power, re.I,
        ))
        if not awake and wake:
            self.shell("input keyevent 224")  # KEYCODE_WAKEUP; never enters a PIN/password.
            time.sleep(0.8)
            power = self.shell("dumpsys power")
            awake = bool(re.search(
                r"mWakefulness=Awake|Display Power:\s*state=ON|mScreenOn=true",
                power, re.I,
            ))
        if not awake:
            return False, "手机屏幕未点亮，请点亮并解锁手机后重试该渠道"

        trust = self.shell("dumpsys trust")
        window = self.shell("dumpsys window policy")
        locked = bool(re.search(
            r"deviceLocked=1|isDeviceLocked=true|mKeyguardShowing=true|"
            r"isStatusBarKeyguard=true|showingLockscreen=true",
            trust + "\n" + window, re.I,
        ))
        if locked:
            return False, ("手机已锁定。请手工解锁并在单渠道查询完成前保持亮屏；"
                           "系统不会尝试输入密码或绕过锁屏。")
        return True, f"设备 {self.serial} 已亮屏并解锁"

    def shell(self, cmd: str, timeout=30) -> str:
        code, out = _run([self.adb, "-s", self.serial, "shell", cmd], timeout=timeout)
        return out

    def reverse(self, device_port: int, host_port: int | None = None) -> bool:
        """Expose a localhost-only host service to this USB-connected device."""
        target_port = host_port or device_port
        code, _ = _run([
            self.adb, "-s", self.serial, "reverse",
            f"tcp:{device_port}", f"tcp:{target_port}",
        ])
        return code == 0

    def start_app(self, package: str, activity: str = ""):
        if activity:
            self.shell(f"am start -n {package}/{activity}")
        else:
            # 通过 monkey 或 am start 主 Activity
            self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
        time.sleep(4)

    def dump_ui(self) -> str:
        """返回 UI 层次 XML；失败自动重试，避免动画期间的瞬时空结果。"""
        xml = ""
        for _ in range(3):
            # 先删除旧文件，防止上一次 dump 的残留 XML 被误读为当前页面。
            self.shell("rm -f /sdcard/ui.xml")
            dump_out = self.shell("uiautomator dump /sdcard/ui.xml")
            time.sleep(1.2)
            code, xml = _run(
                [self.adb, "-s", self.serial, "shell", "cat /sdcard/ui.xml"],
                timeout=20,
            )
            if "<hierarchy" in xml and "could not get idle" not in dump_out:
                return xml
            time.sleep(1.5)
        return xml if "<hierarchy" in xml else ""

    def find_node(self, xml: str, text=None, desc=None, rid=None, cls=None):
        """在 UI XML 中查找节点。返回 {text, bounds:[x1,y1,x2,y2]} 或 None"""
        if not xml:
            return None
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return None
        for node in root.iter("node"):
            t = node.get("text") or ""
            d = node.get("content-desc") or ""
            r = node.get("resource-id") or ""
            c = node.get("class") or ""
            hit = True
            if text and text not in t:
                hit = False
            if hit and desc and desc not in d:
                hit = False
            if hit and rid and rid != r:
                hit = False
            if hit and cls and cls != c:
                hit = False
            if hit and (text or desc or rid or cls):
                b = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
                if b:
                    x1, y1, x2, y2 = map(int, b.groups())
                    return {"text": t, "desc": d, "rid": r,
                            "bounds": [x1, y1, x2, y2], "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2}
        return None

    def all_texts(self, xml: str) -> list:
        if not xml:
            return []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        return [n.get("text", "") for n in root.iter("node") if n.get("text")]

    def nodes(self, xml: str) -> list[dict]:
        """Return normalized UI nodes for market-specific heuristic drivers."""
        if not xml:
            return []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        result = []
        for node in root.iter("node"):
            bounds = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if not bounds:
                continue
            x1, y1, x2, y2 = map(int, bounds.groups())
            result.append({
                "text": node.get("text") or "", "desc": node.get("content-desc") or "",
                "rid": node.get("resource-id") or "", "class": node.get("class") or "",
                "package": node.get("package") or "",
                "clickable": node.get("clickable") == "true", "enabled": node.get("enabled") != "false",
                "bounds": [x1, y1, x2, y2], "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            })
        return result

    def tap(self, x: int, y: int):
        self.shell(f"input tap {x} {y}")
        time.sleep(1.5)

    def input_text(self, s: str):
        # ADB's input command needs spaces encoded, while shell metacharacters
        # must be quoted for the Android-side shell.
        safe = shlex.quote(s.replace(" ", "%s"))
        self.shell(f"input text {safe}")
        time.sleep(0.8)

    def key_back(self):
        self.shell("input keyevent 4")
        time.sleep(1.2)

    def screenshot(self, path: str) -> bool:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                           shell=False, capture_output=True, timeout=20)
        if r.returncode == 0 and r.stdout:
            Path(path).write_bytes(r.stdout)
        return Path(path).exists() and Path(path).stat().st_size > 0

    def install(self, apk_path: str) -> tuple[bool, str]:
        code, out = _run([self.adb, "-s", self.serial, "install", "-r", str(apk_path)], timeout=120)
        return ("Success" in out), out[-200:]

    def uninstall(self, package: str):
        self.shell(f"pm uninstall {package}")
        time.sleep(1)

    def extract_apk(self, package: str, out_dir: str) -> str:
        """从设备提取已安装 APK（用于获取市场 App 本体）。返回本地路径。"""
        out = self.shell(f"pm path {package}")
        m = re.search(r"package:(\S+)", out)
        if not m:
            return ""
        remote = m.group(1)
        name = package.split(".")[-1] + ".apk"
        local = str(Path(out_dir) / name)
        code, out2 = _run([self.adb, "-s", self.serial, "pull", remote, local], timeout=120)
        return local if Path(local).exists() else ""

    def wait_for_download(self, seconds: int = 30) -> str:
        """等待系统下载完成（Download 目录出现 apk）。返回文件路径或空。"""
        for _ in range(int(seconds / 2)):
            out = self.shell("ls /sdcard/Download/*.apk 2>/dev/null || ls /sdcard/*.apk 2>/dev/null")
            if ".apk" in out:
                return out.splitlines()[0].strip()
            time.sleep(2)
        return ""

    def wait_for_package(self, package: str, seconds: int = 90) -> bool:
        """Wait until a market has installed the target package."""
        for _ in range(max(1, int(seconds / 2))):
            if "package:" in self.shell(f"pm path {package}"):
                return True
            time.sleep(2)
        return False
