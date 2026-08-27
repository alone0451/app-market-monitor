"""版本号归一化与比较"""
import re

_NUM_RE = re.compile(r"(\d+)")


def parse_version(text: str):
    """把 '8.0.76' / 'v2.1.0' / '1.0' 等解析为可比较的数值元组。
    返回 (主要段元组, 原始字符串)。无法解析时返回 ((0,), '')。"""
    if not text:
        return ((0,), "")
    m = re.search(r"\d+(?:\.\d+)*", str(text))
    if not m:
        return ((0,), "")
    parts = tuple(int(x) for x in m.group(0).split("."))
    return (parts, m.group(0))


def version_key(text: str):
    """用于排序/比较的规范化 key。非数值版本取 0，保证不报错。"""
    parts, _ = parse_version(text)
    return parts


def compare(a: str, b: str) -> int:
    """返回 1/0/-1：a > b / a == b / a < b（无法解析视为 0 处理）"""
    ka, _ = parse_version(a)
    kb, _ = parse_version(b)
    if ka > kb:
        return 1
    if ka < kb:
        return -1
    return 0
