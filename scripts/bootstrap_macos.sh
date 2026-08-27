#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
TOOLS_DIR="$PROJECT_DIR/.tools/android"
ADB_BIN="$TOOLS_DIR/platform-tools/adb"
PLATFORM_TOOLS_URL="https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "[失败] 第一阶段安装脚本仅支持 macOS。"
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[失败] 未找到 Python 3。请先从 https://www.python.org/downloads/macos/ 安装 Python 3.10 或更高版本。"
  exit 2
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "[失败] 需要 Python 3.10 或更高版本。"
  exit 2
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "[1/3] 创建项目虚拟环境..."
  python3 -m venv "$VENV_DIR"
fi

echo "[2/3] 安装或更新 Python 依赖..."
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_DIR/requirements-device.txt"

if [[ ! -x "$ADB_BIN" ]]; then
  echo "[3/3] 下载 Google 官方 Android Platform-Tools..."
  TEMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TEMP_DIR"' EXIT
  curl --fail --location --retry 2 --silent --show-error \
    "$PLATFORM_TOOLS_URL" -o "$TEMP_DIR/platform-tools.zip"
  unzip -q -t "$TEMP_DIR/platform-tools.zip"
  mkdir -p "$TOOLS_DIR"
  unzip -q "$TEMP_DIR/platform-tools.zip" -d "$TOOLS_DIR"
  chmod +x "$ADB_BIN"
else
  echo "[3/3] 项目 Platform-Tools 已存在。"
fi

"$ADB_BIN" version | head -1
echo "[完成] macOS 运行环境已准备好。"
