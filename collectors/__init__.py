"""渠道采集器注册表与基类"""
from dataclasses import dataclass, field

# 采集结果状态
ST_OK = "ok"              # 正常拿到版本
ST_OFFLINE = "offline"    # 未上架/页面不存在
ST_NOT_PUBLISHED = "not_published"  # 目标平台没有发布记录（正常业务状态）
ST_WEB_LIMITED = "web_limited"  # 平台网页未公开足够字段，不能可靠判断
ST_NEED_ADAPT = "need_adapt"  # 采集器待适配
ST_NEED_REVIEW = "need_review"  # 已到达目标页面，但版本需人工确认
ST_DEVICE_UNAVAILABLE = "device_unavailable"  # 未检测到可用 Android 设备
ST_MARKET_APP_MISSING = "market_app_missing"  # 设备未安装对应市场客户端
ST_LOGIN_REQUIRED = "login_required"  # 市场要求登录、验证码或首次授权
ST_REGION_UNAVAILABLE = "region_unavailable"  # 官方客户端因地区/设备环境不可用
ST_FALLBACK_OK = "fallback_ok"  # 官方替代来源取得版本，非目标客户端直采
ST_ERROR = "error"        # 请求/解析错误
ST_PACKAGE_MISMATCH = "package_mismatch"  # 找到同名应用，但包名不是目标包


@dataclass
class CollectResult:
    version_name: str = ""
    version_code: str = ""
    status: str = ST_OK
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def ok(self) -> bool:
        return self.status in (ST_OK, ST_OFFLINE, ST_FALLBACK_OK)


@dataclass
class SearchCandidate:
    """A market search hit that can be confirmed as a monitoring target."""
    app_name: str
    package_name: str = ""
    platform: str = "android"
    bundle_id: str = ""
    market_app_id: str = ""
    developer: str = ""
    operator: str = ""
    version_name: str = ""
    detail_url: str = ""
    icon_url: str = ""
    source_market: str = ""


class BaseCollector:
    """A 路径采集器基类。子类实现 collect() 与可选 search()。"""
    key = ""          # 渠道 key（与 markets.id 对应）
    display_name = ""

    def collect(self, package_name: str, market_app_id: str = "", **kw) -> CollectResult:
        raise NotImplementedError

    supports_search = False
    # A collector may not expose public name search but can still confirm an
    # already discovered canonical Android package on its official detail API.
    supports_package_lookup = False

    def search(self, keyword: str, **kw) -> list[SearchCandidate]:
        """模糊搜索候选列表：[{name, package_name, market_app_id}]。未实现返回空。"""
        return []


_REGISTRY = {}


def register(cls):
    _REGISTRY[cls.key] = cls()
    return cls


def get_collector(key: str):
    return _REGISTRY.get(key)


def all_collectors():
    return dict(_REGISTRY)


def _need_adapt_result(note: str = "接口待适配") -> CollectResult:
    return CollectResult(status=ST_NEED_ADAPT, detail=note)


# 触发各采集器模块注册（必须放在基类与注册表定义之后）
from . import xiaomi  # noqa: E402,F401
from . import yyb     # noqa: E402,F401
from . import huawei  # noqa: E402,F401
from . import samsung  # noqa: E402,F401
from . import meizu  # noqa: E402,F401
from . import baidu  # noqa: E402,F401
from . import device_markets  # noqa: E402,F401
from . import appstore  # noqa: E402,F401
from . import qihu360  # noqa: E402,F401
