#!/usr/bin/env python3
"""应用市场版本巡检系统 · 环境检测（CLI 版）
用法: python scripts/check_env.py
说明: 检查 macOS 部署、Python、ADB、手机、OCR 和运行目录。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.env_check import run_doctor  # noqa: E402

GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"
MAP = {"ok": (GREEN, "[OK]"), "warn": (YELLOW, "[提示]"), "fail": (RED, "[失败]")}


def main():
    print("=" * 60)
    print("应用市场版本巡检系统 · 环境检测")
    print("=" * 60)
    problems = 0
    results = run_doctor()
    for item in results:
        color, tag = MAP.get(item["status"], (RED, "[失败]"))
        print(f"\n[{item['step']}/{len(results)}] {item['name']}")
        print(f"{color}{tag}{RESET} {item['message']}")
        for a in item["actions"]:
            print(f"  {YELLOW}→{RESET} {a}")
        if item["status"] == "fail":
            problems += 1
    print("\n" + "=" * 60)
    if problems == 0:
        print(f"{GREEN}[OK]{RESET} 核心环境就绪，可执行 ./start.sh")
    else:
        print(f"{YELLOW}[提示]{RESET} 存在 {problems} 项必须处理；warn 项通常不影响网页巡检")
    print("=" * 60)


if __name__ == "__main__":
    main()
