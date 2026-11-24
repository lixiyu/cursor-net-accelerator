#!/usr/bin/env bash
# 开发：编译并前台跑菜单栏（需本机已能执行 cursor-net-accelerator）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
BIN="${CURSOR_NET_ACCEL_BIN:-$REPO/.venv/bin/cursor-net-accelerator}"
if [[ ! -x "$BIN" ]]; then
  echo "找不到加速器可执行文件: $BIN" >&2
  echo "先在仓库根目录: python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
  echo "或 export CURSOR_NET_ACCEL_BIN=/path/to/cursor-net-accelerator" >&2
  exit 1
fi
export CURSOR_NET_ACCEL_BIN="$BIN"
cd "$ROOT"
swift build
exec swift run
