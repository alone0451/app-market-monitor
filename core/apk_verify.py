"""APK 哈希与签名校验（B 路径核心）
- SHA-256：文件哈希，用于与基线比对（防篡改）
- 签名证书指纹：androguard 解析，验证渠道包是否官方签名
"""
import hashlib
import logging
from pathlib import Path

# androguard 使用 loguru 输出大量日志，静默之
try:
    from loguru import logger as _loguru
    _loguru.disable("androguard")
    _loguru.disable("apkInspector")
except ImportError:
    pass
logging.getLogger("androguard").setLevel(logging.ERROR)
logging.getLogger("apkInspector").setLevel(logging.ERROR)


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_apk(path):
    """解析 APK：包名、版本、签名证书指纹。失败返回 None。"""
    from androguard.core.apk import APK
    a = APK(str(path))
    certs = a.get_certificates()
    sig = ""
    if certs:
        raw = certs[0].sha256 or b""
        sig = raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw)
    return {
        "package": a.get_package(),
        "version_name": a.get_androidversion_name() or "",
        "version_code": str(a.get_androidversion_code() or ""),
        "sig_sha256": sig,
    }


def verify(apk_path, baseline_sha256: str = "", baseline_sig: str = "",
           expected_package: str = "", expected_version: str = "") -> dict:
    """校验 APK 与基线一致性。
    返回 {verify_result: ok/diff/need_review, sha256, sig, detail}
    - 有基线：哈希或签名不一致 → diff
    - 无基线：仅记录（need_review，提示可设基线）
    """
    apk_path = Path(apk_path)
    h = sha256(apk_path)
    parse_error = ""
    try:
        info = parse_apk(apk_path)
    except ImportError:
        info = None
        parse_error = "未安装 APK 签名解析组件，请执行 pip install -r requirements-device.txt"
    except Exception as exc:
        info = None
        parse_error = f"APK 解析失败: {type(exc).__name__}"
    result = {
        "sha256": h,
        "sig": (info or {}).get("sig_sha256", ""),
        "version_name": (info or {}).get("version_name", ""),
        "version_code": (info or {}).get("version_code", ""),
        "package": (info or {}).get("package", ""),
        "verify_result": "need_review",
        "detail": "",
    }
    if not info:
        result["verify_result"] = "need_review"
        result["detail"] = parse_error or "APK 解析失败（可能损坏或格式异常）"
        return result
    identity_diffs = []
    if expected_package and result["package"] != expected_package:
        identity_diffs.append(
            f"包名不一致（期望 {expected_package}，实际 {result['package']}）"
        )
    if expected_version and result["version_name"] and result["version_name"] != expected_version:
        identity_diffs.append(
            f"版本与市场页面不一致（页面 {expected_version}，安装包 {result['version_name']}）"
        )
    if identity_diffs:
        result["verify_result"] = "diff"
        result["detail"] = "；".join(identity_diffs)
        return result
    if baseline_sha256 and baseline_sig:
        if h.lower() == baseline_sha256.lower() and result["sig"].lower() == baseline_sig.lower():
            result["verify_result"] = "ok"
            result["detail"] = "哈希与签名均与基线一致"
        else:
            result["verify_result"] = "diff"
            diffs = []
            if h.lower() != baseline_sha256.lower():
                diffs.append("哈希不一致（疑似被篡改或版本不同）")
            if result["sig"].lower() != baseline_sig.lower():
                diffs.append("签名不一致（疑似非官方签名）")
            result["detail"] = "；".join(diffs)
    elif baseline_sha256 and not baseline_sig:
        result["verify_result"] = "ok" if h.lower() == baseline_sha256.lower() else "diff"
        result["detail"] = "哈希与基线一致" if result["verify_result"] == "ok" else "哈希与基线不一致"
    elif baseline_sig:
        result["verify_result"] = (
            "ok" if result["sig"] and result["sig"].lower() == baseline_sig.lower() else "diff"
        )
        result["detail"] = (
            "签名与已有制品一致" if result["verify_result"] == "ok"
            else "签名与已有同包名制品不一致"
        )
    else:
        result["verify_result"] = "need_review"
        result["detail"] = "未设置基线（在 App 管理绑定中录入官方包 SHA-256/签名后自动比对）"
    return result
