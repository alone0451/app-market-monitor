"""应用宝真机驱动。

应用宝 9.2.x 的详情主体是自绘页面，版本文字不在 uiautomator 树中。因此用
稳定控件完成导航，再对详情截图做 OCR；包名仅用于后续安装结果核验。
"""
import re
import shutil
import subprocess
import time
from pathlib import Path


PACKAGE = "com.tencent.android.qqdownloader"
LATIN_IME = "com.android.inputmethod.latin/.LatinIME"
_VERSION_RE = re.compile(r"版本\s*[:：]\s*(\d+(?:\.\d+){1,3})")


def _pinyin_query(app_name: str) -> str:
    if not re.search(r"[\u3400-\u9fff]", app_name):
        return app_name.replace(" ", "")
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return ""
    return "".join(lazy_pinyin(app_name))


def _ocr_version(path: str) -> tuple[str, str]:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return "", "未安装 tesseract，已保存详情截图但无法读取自绘版本文字"
    try:
        run = subprocess.run(
            [tesseract, path, "stdout", "-l", "chi_sim+eng", "--psm", "6"],
            shell=False, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"详情截图 OCR 失败: {exc}"
    text = (run.stdout or "") + (run.stderr or "")
    match = _VERSION_RE.search(text)
    if match:
        return match.group(1), text
    return "", text


class YybDeviceDriver:
    market_id = "yyb"
    package = PACKAGE

    def __init__(self, device):
        self.dev = device

    def inspect(self, package_name: str, app_name: str, screenshot_dir: str) -> dict:
        if not app_name:
            return {"ok": False, "detail": "应用宝真机搜索需要已确认的应用名称"}
        query = _pinyin_query(app_name)
        if not query:
            return {"ok": False, "detail": "中文应用名转拼音依赖缺失，请安装 requirements-device.txt"}

        original_ime = self.dev.shell("settings get secure default_input_method").strip()
        try:
            self.dev.shell(f"am force-stop {self.package}")
            self.dev.start_app(self.package)
            xml = self.dev.dump_ui()
            search = self.dev.find_node(xml, rid=f"{self.package}:id/awt") or \
                     self.dev.find_node(xml, desc="搜索")
            if not search:
                return {"ok": False, "detail": "应用宝首页搜索入口未找到，可能需要处理首次启动弹窗"}
            self.dev.tap(search["cx"], search["cy"])

            xml = self.dev.dump_ui()
            field = self.dev.find_node(xml, rid=f"{self.package}:id/yv")
            if not field:
                return {"ok": False, "detail": "应用宝搜索输入框未找到，客户端界面可能已更新"}
            clear = self.dev.find_node(xml, desc="清除")
            if clear:
                self.dev.tap(clear["cx"], clear["cy"])
            self.dev.shell(f"ime set {LATIN_IME}")
            self.dev.tap(field["cx"], field["cy"])
            self.dev.input_text(query)

            xml = self.dev.dump_ui()
            button = self.dev.find_node(xml, rid=f"{self.package}:id/a5t") or \
                     self.dev.find_node(xml, text="搜索")
            if not button:
                return {"ok": False, "detail": "应用宝搜索按钮未找到，客户端界面可能已更新"}
            self.dev.tap(button["cx"], button["cy"])
            time.sleep(2)

            xml = self.dev.dump_ui()
            hit = self.dev.find_node(xml, text=app_name)
            if not hit:
                return {"ok": False, "detail": f"应用宝未精确召回“{app_name}”（查询词 {query}）"}
            self.dev.tap(hit["cx"], hit["cy"])
            time.sleep(3)

            Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
            shot = str(Path(screenshot_dir) / f"yyb_{int(time.time())}.png")
            if not self.dev.screenshot(shot):
                return {"ok": False, "detail": "应用宝详情页截图失败"}
            version, ocr_text = _ocr_version(shot)
            if not version:
                return {"ok": False, "screenshot": shot,
                        "detail": "详情页已打开，但未从截图识别到版本号；需要人工复核"}
            return {
                "ok": True,
                "version": version,
                "screenshot": shot,
                "detail": (f"应用宝真机详情页 OCR：版本 {version}；"
                           f"目标包名 {package_name} 用于安装后核验"),
                "ocr_text": ocr_text,
            }
        finally:
            if original_ime and original_ime != "null":
                self.dev.shell(f"ime set {original_ime}")
