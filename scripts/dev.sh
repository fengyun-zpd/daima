#!/usr/bin/env bash
# CodePilot 本地开发启动脚本（Linux/macOS）
#
#   ./scripts/dev.sh                 # 安装依赖 + 迁移 + 构建沙箱镜像 + 启动 API
#   SKIP_SANDBOX=1 ./scripts/dev.sh  # 跳过沙箱镜像构建
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export CODEPILOT_DATABASE_URL="${CODEPILOT_DATABASE_URL:-postgresql+psycopg://codepilot:codepilot@localhost:55432/codepilot}"
export PYTHONIOENCODING=utf-8
PORT="${PORT:-8099}"

echo "[1/4] 安装依赖"
python -m pip install -e ".[dev]"

echo "[2/4] 执行数据库迁移（$CODEPILOT_DATABASE_URL）"
python -m alembic upgrade head

if [[ "${SKIP_SANDBOX:-0}" == "1" ]]; then
  echo "[3/4] 跳过沙箱镜像构建（Fix/Verify 将返回 SANDBOX_UNAVAILABLE）"
else
  echo "[3/4] 构建沙箱镜像"
  python scripts/build_sandbox_image.py
fi

echo "[4/4] 启动 API：http://127.0.0.1:$PORT"
exec python -m uvicorn apps.api.main:app --host 127.0.0.1 --port "$PORT"
