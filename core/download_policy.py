"""Explain when obtaining an official market APK for local verification is useful."""


def decide_download(status: str, current_version: str, previous_version: str = "",
                    authority: str = "unknown", policy: str = "on_change") -> tuple[str, str]:
    if status != "ok" or not current_version:
        return "not_needed", "尚未取得可信版本信息"
    if authority == "third_party":
        return "required", "该 App 在此渠道被标记为非官方，需要校验签名"
    if policy == "always":
        return "required", "监测策略设置为每次下载校验"
    if not previous_version:
        return "optional", "首次发现该版本，可下载一次建立签名基线"
    if previous_version != current_version:
        if policy == "manual":
            return "optional", f"版本由 {previous_version} 变为 {current_version}，当前策略为手动下载"
        return "recommended", f"版本由 {previous_version} 变为 {current_version}，建议下载复核"
    return "not_needed", "版本未变化"
