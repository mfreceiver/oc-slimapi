#!/usr/bin/env bash
# scripts/release.sh — oc-slimapi 发版唯一入口（semver + tag + changelog 门禁）
# 规范权威：docs/release.md；入口索引：AGENTS.md。
# 借鉴 ocdroid scripts/release.sh，适配 Python 包（pyproject.toml 写版本）。
#
# 用法: ./scripts/release.sh <patch|minor|major>
#
# 流程：
#   1. 分支=main、已跟踪工作区干净
#   2. ./scripts/check.sh
#   3. 由 pyproject.toml 当前 version 推算下一版本
#   4. CHANGELOG.md 必须已有 ## [X.Y.Z] 节
#   5. 写回 pyproject.toml version
#   6. commit: release: vX.Y.Z
#   7. annotated tag vX.Y.Z
#   8. 打印 push 命令（不自动 push）
#
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TYPE="${1:?用法: release.sh <patch|minor|major>}"
[[ "$TYPE" =~ ^(patch|minor|major)$ ]] || { echo "❌ 类型必须是 patch|minor|major"; exit 1; }

# --- 1. git 前置 ---
BRANCH=$(git branch --show-current)
[[ "$BRANCH" == "main" ]] || { echo "❌ 当前分支=$BRANCH，发版必须在 main"; exit 1; }

if ! git diff --quiet HEAD || ! git diff --cached --quiet; then
  echo "❌ 工作区有未提交的已跟踪改动，请先 commit 或 stash"
  git status --short
  exit 1
fi

# --- 2. 质量门禁 ---
echo "==> 质量门禁"
./scripts/check.sh

# --- 3. 读当前版本并推算 ---
PYPROJECT="pyproject.toml"
[[ -f "$PYPROJECT" ]] || { echo "❌ 缺少 $PYPROJECT"; exit 1; }
CUR=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$PYPROJECT" | head -1)
[[ -n "$CUR" ]] || { echo "❌ 无法从 $PYPROJECT 解析 version"; exit 1; }
IFS='.' read -r MAJOR MINOR PATCH <<<"$CUR"
case "$TYPE" in
  patch) PATCH=$((PATCH + 1)) ;;
  minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
  major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
esac
VERSION="$MAJOR.$MINOR.$PATCH"
TAG="v$VERSION"
echo "==> 版本：$CUR → $VERSION（tag $TAG）"

# --- 4. CHANGELOG 必须含目标版本节 ---
if ! grep -qE "^## \[${VERSION}\]" CHANGELOG.md; then
  echo "❌ CHANGELOG.md 中没有 '## [${VERSION}]' 节"
  echo "   请先把 [Unreleased] 内容整理进 ## [${VERSION}] - YYYY-MM-DD，再跑 release.sh"
  exit 1
fi

# --- 5. 写回 pyproject.toml ---
# 仅替换 project.version 行（第一个 version = "..."）
if [[ "$(uname)" == Darwin ]]; then
  sed -i '' "0,/^version = \".*\"/{s/^version = \".*\"/version = \"${VERSION}\"/;}" "$PYPROJECT"
else
  sed -i "0,/^version = \".*\"/{s/^version = \".*\"/version = \"${VERSION}\"/;}" "$PYPROJECT"
fi
grep -q "^version = \"${VERSION}\"" "$PYPROJECT" || { echo "❌ 写回 version 失败"; exit 1; }

# --- 6. commit ---
git add "$PYPROJECT" CHANGELOG.md
# 若发版提交还包含契约等，由调用方事先 commit；此处只收版本+changelog
git commit -m "release: v${VERSION}"

# --- 7. annotated tag（注释 = CHANGELOG 该节，取到下一 ## 之前）---
NOTE_FILE="$(mktemp)"
trap 'rm -f "$NOTE_FILE"' EXIT
# 提取 ## [VERSION] 到下一个 ## [ 或文件尾
awk -v ver="$VERSION" '
  $0 ~ "^## \\[" ver "\\]" {p=1}
  p && $0 ~ /^## \[/ && $0 !~ "^## \\[" ver "\\]" {exit}
  p {print}
' CHANGELOG.md > "$NOTE_FILE"
[[ -s "$NOTE_FILE" ]] || { echo "❌ 无法从 CHANGELOG 提取 ${VERSION} 节"; exit 1; }
git tag -a "$TAG" -F "$NOTE_FILE"
echo "✅ Tag 创建: $TAG"

# --- 8. 人工 push ---
echo ""
echo "════════════════════════════════════════════════════════════"
echo "✅ 仓库发版准备完成: $VERSION (tag $TAG)"
echo ""
echo "确认无误后执行:"
echo "  git push origin main && git push origin $TAG"
echo "（可选）在 Gitea 为 tag 建 Release，body 贴 CHANGELOG 该节"
echo "════════════════════════════════════════════════════════════"
