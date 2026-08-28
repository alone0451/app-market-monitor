"""环境检测模块（CLI 与网页共用）
返回结构化结果 [{step, name, status, message, actions:[...]}]
status: ok / warn / fail
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PORTABLE_ADB = BASE / ".tools" / "android" / "platform-tools" / "adb"


def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def parse_adb_devices(output: str) -> list[dict]:
    devices = []
    valid_states = {"device", "offline", "unauthorized", "recovery", "sideload", "bootloader"}
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state not in valid_states:
            continue
        details = " ".join(parts[2:])
        is_emulator = serial.startswith("emulator-") or "product:sdk_" in details or "device:generic" in details
        devices.append({"serial": serial, "state": state, "details": details,
                        "is_emulator": is_emulator})
    return devices


def find_adb():
    if PORTABLE_ADB.exists() and os.access(PORTABLE_ADB, os.X_OK):
        return str(PORTABLE_ADB), "项目内置 Platform-Tools"
    adb = shutil.which("adb")
    if adb:
        return adb, "系统 PATH"
    for cand in [Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb"]:
        if cand.exists():
            return str(cand), str(cand.parent)
    return None, None


def check_platform():
    system = platform.system()
    machine = platform.machine() or "未知架构"
    if system == "Darwin":
        return ("ok", f"macOS · {machine}", [])
    return ("warn", f"当前系统为 {system} · {machine}；第一阶段仅验证 macOS",
            ["网页功能可能仍可运行，但启动脚本和 USB 环境尚未在该系统完成回归"])


def check_python_runtime():
    version = sys.version_info
    message = f"Python {version.major}.{version.minor}.{version.micro} · {sys.executable}"
    if version >= (3, 10):
        return ("ok", message, [])
    return ("fail", message, ["请安装 Python 3.10 或更高版本后重新运行 ./start.sh"])


def check_deps():
    try:
        import flask, httpx, bs4, yaml  # noqa
        return ("ok", "核心依赖已安装 (flask/httpx/bs4/yaml)", [])
    except ImportError as e:
        return ("fail", f"缺少依赖: {e.name}",
                ["执行: python3 -m pip install -r requirements.txt"])


def check_adb():
    adb, src = find_adb()
    if adb:
        code, out = _run(f'"{adb}" version')
        ver = (out.splitlines() or ["?"])[0][:60]
        return ("ok", f"ADB 可用 ({src}): {ver}", [])
    return ("fail", "未找到 adb",
            ["运行 ./scripts/bootstrap_macos.sh，自动下载 Google 官方 Platform-Tools",
             "也可以自行安装 Android Studio 或执行 brew install android-platform-tools"])


def check_usb_phone():
    """检查可执行市场客户端复核的 Android 测试设备（实体机或模拟器）。"""
    adb, _ = find_adb()
    if not adb:
        return ("warn", "跳过（ADB 未安装，先解决 ADB 再检测 Android 设备）", [])
    code, out = _run(f'"{adb}" devices -l')
    if code != 0:
        detail = next((line.strip() for line in reversed(out.splitlines()) if line.strip()), "未知错误")
        return ("fail", f"ADB 无法启动：{detail[:160]}",
                ["关闭其他占用 ADB 的程序后重试",
                 "执行项目内 .tools/android/platform-tools/adb kill-server 后重新连接 Android 设备"])
    devices = parse_adb_devices(out)
    usable = [d for d in devices if d["state"] == "device"]
    if not usable:
        return ("fail", "未检测到可用 Android 测试设备", [
            "可连接实体手机并开启 USB 调试，或启动一个可用的 Android 模拟器",
            "设备连接后请保持亮屏并解锁，市场客户端才能进行 UI 查询",
        ])
    summary = []
    ok_any = False
    for device in usable:
        sid, state = device["serial"], device["state"]
        kind = "模拟器" if device["is_emulator"] else "实体手机"
        ok_any = True
        summary.append(f"{kind} {sid} 已连接 ✓")
    status = "ok" if ok_any else "warn"
    msg = "；".join(summary)
    actions = [] if ok_any else ["确认设备已启动且 ADB 状态为 device 后重新检测"]
    return (status, msg, actions)


def check_emulator():
    # A running AVD is sufficient for巡检，即使 emulator CLI 不在 PATH 中（例如
    # 由 Android Studio 或隐私检测工具单独启动的模拟器）。
    adb, _ = find_adb()
    if adb:
        code, out = _run(f'"{adb}" devices -l')
        active = [d["serial"] for d in parse_adb_devices(out)
                  if d["state"] == "device" and d["is_emulator"]]
        if active:
            return ("ok", f"检测到运行中的 Android 模拟器：{'、'.join(active)}", [])
    emu = shutil.which("emulator")
    if emu and os.path.exists(emu):
        code, out = _run(f'"{emu}" -list-avds', timeout=10)
        avds = [a for a in out.splitlines() if a.strip()]
        msg = f"模拟器可用，AVD 列表: {avds or '（无 AVD）'}"
        actions = [] if avds else ["可用 app_privacy_checker 的 scripts/create_dynamic_avd.sh 创建 AVD"]
        return ("ok", msg, actions)
    return ("warn", "未安装 emulator（可选）",
            ["仅在开发真机/模拟器市场适配器时需要"])


def check_apk_verify():
    try:
        import androguard  # noqa: F401
        return ("ok", "APK 签名解析组件已安装", [])
    except ImportError:
        return ("warn", "尚未安装 APK 签名解析组件（网页查版本不受影响）",
                ["需要校验网页 APK 的包名、哈希或签名时执行: .venv/bin/python -m pip install -r requirements-device.txt"])


def check_pinyin():
    try:
        import pypinyin  # noqa: F401
        return ("ok", "中文搜索转换组件已安装", [])
    except ImportError:
        return ("warn", "未安装中文搜索转换组件",
                ["执行 ./scripts/bootstrap_macos.sh 安装可选 Python 依赖"])


def check_tesseract():
    binary = shutil.which("tesseract")
    if not binary:
        return ("warn", "未安装 Tesseract OCR；网页巡检不受影响",
                ["需要应用宝手机截图识别时执行: brew install tesseract tesseract-lang"])
    code, output = _run(f'"{binary}" --list-langs')
    languages = set(output.split())
    if "chi_sim" in languages:
        return ("ok", "Tesseract OCR 与简体中文语言包已安装", [])
    return ("warn", "Tesseract 已安装，但缺少简体中文语言包 chi_sim",
            ["执行: brew install tesseract-lang"])


def check_runtime_storage():
    target = BASE / "data"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return ("ok", f"运行数据目录可写：{target}", [])
    except OSError as exc:
        return ("fail", f"运行数据目录不可写：{exc}",
                ["请将项目复制到当前用户拥有写权限的目录"])


def run_all(cfg=None):
    """检测 Android 市场客户端复核所需环境，不混入桌面 APK 解析。"""
    checks = [
        ("Python 依赖", check_deps),
        ("ADB 工具", check_adb),
        ("Android 测试设备", check_usb_phone),
    ]
    results = []
    for step, (name, fn) in enumerate(checks, 1):
        status, message, actions = fn()
        results.append({"step": step, "name": name, "status": status,
                        "message": message, "actions": actions})
    return results


def run_doctor():
    """完整部署诊断；网页中同时展示实体手机和模拟器。"""
    checks = [
        ("运行系统", check_platform),
        ("Python 版本", check_python_runtime),
        ("Python 核心依赖", check_deps),
        ("APK 解析依赖", check_apk_verify),
        ("中文搜索依赖", check_pinyin),
        ("ADB 工具", check_adb),
        ("Android 测试设备", check_usb_phone),
        ("Android 模拟器", check_emulator),
        ("OCR 环境", check_tesseract),
        ("运行数据目录", check_runtime_storage),
    ]
    results = []
    for step, (name, fn) in enumerate(checks, 1):
        status, message, actions = fn()
        results.append({"step": step, "name": name, "status": status,
                        "message": message, "actions": actions})
    return results
