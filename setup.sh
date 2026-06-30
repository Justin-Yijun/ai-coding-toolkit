#!/usr/bin/env bash
# =============================================================================
# ai_toolkit 一键环境搭建（Linux / macOS）。
# 用法：
#   ./setup.sh                              # 外网
#   ./setup.sh http://10.144.1.10:8080      # 内网走代理
# 已处理「requirements.txt 含中文注释 → pip 以 gbk 解码报错」的坑（PYTHONUTF8=1）。
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONUTF8=1   # 关键：requirements.txt 含中文注释，强制 UTF-8 避免解码报错

PROXY="${1:-}"
PY=python3

if [ ! -d ".venv" ]; then
  echo "[1/3] 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
PY="./.venv/bin/python"

PIP_PROXY=()
[ -n "$PROXY" ] && PIP_PROXY=(--proxy "$PROXY")

echo "[2/3] 升级 pip ..."
"$PY" -m pip install --upgrade pip "${PIP_PROXY[@]}"
echo "[3/3] 安装依赖（requests / PyYAML / pytest / mypy）..."
"$PY" -m pip install -r requirements.txt "${PIP_PROXY[@]}"

echo ""
echo "[OK] 环境就绪。确保 Ollama 已运行后试跑："
echo "  $PY main.py regex --desc 匹配邮箱 --pos a@b.com --neg not_email"
