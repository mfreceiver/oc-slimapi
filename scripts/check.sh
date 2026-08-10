#!/usr/bin/env bash
# scripts/check.sh — 改动校验（质量门禁）
# 详见 AGENTS.md 与 docs/release.md。
#
# 用法:
#   ./scripts/check.sh           # 默认：pytest + 路由↔INTERFACE_MAP 一致性 gate + compileall
#   ./scripts/check.sh --full    # 兼容别名（行为等价于默认）
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "❌ 未找到 .venv/bin/python；请先: python -m venv .venv && .venv/bin/pip install -e '.[test]'"
  exit 1
fi

MODE="${1:-default}"

echo "==> pytest tests/"
"$PY" -m pytest tests/ -q

echo "==> 路由↔文档一致性（防漂移）"
"$PY" "$ROOT/scripts/check_routes_doc.py"

echo "==> compileall src"
"$PY" -m compileall -q src

case "$MODE" in
  --full|default|"")
    ;;
  *)
    echo "用法: check.sh [--full]"; exit 1 ;;
esac

echo "✅ check.sh 通过"
