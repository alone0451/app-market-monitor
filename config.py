"""应用市场版本巡检系统 · 配置加载"""
import os
from pathlib import Path
import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AMM_CONFIG", BASE_DIR / "config.yaml"))
DB_PATH = Path(os.environ.get("AMM_DB", BASE_DIR / "data" / "amm.db"))

DEFAULT_MARKETS = [
    # (key, 显示名, 采集器, 默认trust_level, 说明)
    ("huawei",  "华为应用市场", "huawei",  "official",     "官方网页接口自动采集"),
    ("xiaomi",  "小米应用商店", "xiaomi",  "official",     "app.mi.com 免登录"),
    ("yyb",     "应用宝",       "yyb",     "official",     "sj.qq.com 免登录"),
    ("oppo",    "OPPO 软件商店", "oppo",    "official",     "Android 设备客户端采集"),
    ("vivo",    "vivo 应用商店", "vivo",    "official",     "Android 设备客户端采集"),
    ("samsung", "三星 Galaxy Store", "samsung", "official", "官方网页接口自动采集"),
    ("baidu",   "百度手机助手", "baidu",   "official",     "Android 客户端优先，网页备用"),
    ("meizu",   "魅族应用商店", "meizu",   "official",     "官方网页接口自动采集"),
    ("honor",   "荣耀应用市场", "honor",   "official",     "Android 设备客户端采集"),
    ("appstore", "Apple App Store", "appstore", "official", "Apple 公开搜索接口，覆盖 iOS"),
    ("qihu360", "360 手机助手", "qihu360", "official", "360 官方网页接口自动采集"),
]

MARKET_DISPLAY_NAMES = {market_id: name for market_id, name, *_ in DEFAULT_MARKETS}


def market_display_name(market_id: str) -> str:
    """Return the product-facing market name; IDs remain internal only."""
    return MARKET_DISPLAY_NAMES.get(market_id, "未知应用市场")

MARKET_PLATFORMS = {
    "appstore": "ios",
}

# First-run defaults: these channels can resolve an exact Android package without
# extra market IDs or a connected phone. Other channels remain available but are
# opt-in until their adapter/device workflow is configured.
PACKAGE_READY_MARKETS = {
    "huawei", "xiaomi", "yyb", "samsung", "baidu", "meizu",
    "appstore", "qihu360",
}
# These clients need an explicit one-time first-run confirmation before the
# device-side adapter can operate. Baidu uses the client as the preferred local
# evidence path when installed; its consent gate is tracked separately.
DEVICE_READY_MARKETS = {"oppo", "vivo", "honor", "baidu"}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {"http": {}}
    # 定时监测暂不提供，忽略旧配置，防止旧文件在后台重新启用调度。
    cfg.pop("schedule", None)
    return cfg


def save_config(cfg: dict):
    cfg = dict(cfg)
    cfg.pop("schedule", None)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
