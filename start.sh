#!/bin/bash
# 应用市场版本巡检系统 · 一键启动
# 用法: ./start.sh  （首次会自动安装依赖）
# 启动后浏览器打开 http://127.0.0.1:5001
# 提示: 在你自己终端运行本脚本可保持服务常驻；关闭终端即停止。
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

"$PROJECT_DIR/scripts/bootstrap_macos.sh"
echo "[诊断] 检查部署与手机环境..."
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/check_env.py" || true
echo "[启动] http://127.0.0.1:5001  (Ctrl+C 停止)"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/app.py"
