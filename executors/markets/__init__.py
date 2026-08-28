"""Market-specific Android UI drivers."""

from .yyb import YybDeviceDriver
from .generic import (
    HuaweiDeviceDriver, HonorDeviceDriver, OppoDeviceDriver, VivoDeviceDriver,
    BaiduDeviceDriver,
)


DEVICE_DRIVERS = {
    "yyb": YybDeviceDriver,
    "huawei": HuaweiDeviceDriver,
    "oppo": OppoDeviceDriver,
    "vivo": VivoDeviceDriver,
    "honor": HonorDeviceDriver,
    "baidu": BaiduDeviceDriver,
}


def get_device_driver(market_id: str, device, package: str = ""):
    driver = DEVICE_DRIVERS.get(market_id)
    instance = driver(device) if driver else None
    if instance and package:
        instance.package = package
    return instance
