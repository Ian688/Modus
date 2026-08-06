#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# ── 配置 ──
PORT="${PORT:-3000}"
HOST="${HOST:-127.0.0.1}"

# 可选加载 .env；没有环境变量时仍允许启动，再从 Desktop 模型仓库配置。
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# ── 启动 ──
echo "🚀 Modus Desktop"
echo "   http://${HOST}:${PORT}"
echo ""

# 优先尝试 Electron，否则用浏览器
if [ -x electron/node_modules/.bin/electron ]; then
  echo "📦 启动桌面窗口..."
  MODUS_DESKTOP_PORT="${PORT}" MODUS_DESKTOP_HOST="${HOST}" \
  electron/node_modules/.bin/electron electron/
else
  echo "🌐 启动 Web 服务（浏览器访问）..."
  if [ -x .venv/bin/python ]; then
    PYTHONPATH=src .venv/bin/python -m modus serve --port "${PORT}" --host "${HOST}"
  else
    uv run modus serve --port "${PORT}" --host "${HOST}"
  fi
fi
