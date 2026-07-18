#!/usr/bin/env bash
# scripts/check.sh — 改动校验（质量门禁）
# 详见 AGENTS.md 与 docs/release.md。
#
# 用法:
#   ./scripts/check.sh           # pytest（默认，每次改动必跑）
#   ./scripts/check.sh --full    # pytest + compileall
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "❌ 未找到 .venv/bin/python；请先: python -m venv .venv && .venv/bin/pip install -e './sidecar[test]'"
  exit 1
fi

MODE="${1:-default}"

echo "==> pytest sidecar/tests/"
"$PY" -m pytest sidecar/tests/ -q

case "$MODE" in
  --full)
    echo "==> compileall sidecar/src"
    "$PY" -m compileall -q sidecar/src
    ;;
  default|"")
    ;;
  *)
    echo "用法: check.sh [--full]"; exit 1 ;;
esac

echo "✅ check.sh 通过"
