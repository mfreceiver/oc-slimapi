#!/usr/bin/env python3
r"""路由 ↔ 文档一致性校验（防漂移，enforced）。

断言 `src/oc_slimapi/routes/*.py` 里每个 `/slimapi/**` 路由都在
`docs/specs/INTERFACE_MAP.md` 中有记录。新增路由但漏更文档时 check.sh 失败。

匹配用路径边界（路径后不能跟 `\w` 或 `/`），避免 `/slimapi/sessions` 被
`/slimapi/sessions/status` 这类更长路径误判为已记录。

**两层校验**：
1. **存在性**（默认，每条路由）：路径字符串须出现在 INTERFACE_MAP。
2. **语义**（`SEMANTIC_CHECKS` 白名单内的路由）：该路由所在文档行须包含其
   关键错误码关键词（`session_not_found` / `upstream_http_` / `upstream_unavailable`
   / `transform_busy` 等）。这是为了防 P1-2 那类"路由存在但错误映射描述与实现/契约
   矛盾"的语义漂移——存在性门禁检不出。白名单刻意保守：只覆盖有明确错误码契约
   的核心读路由；新增路由如需语义校验，往 `SEMANTIC_CHECKS` 加条目即可。

退出码：0=全部通过；1=有缺失/语义不符（列出明细）；2=文档文件缺失。
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = ROOT / "src/oc_slimapi/routes"
DOC = ROOT / "docs/specs/INTERFACE_MAP.md"

# APIRouter(prefix="/slimapi...") —— 取 router 自带前缀（app.include_router 无额外前缀）
_PREFIX_RE = re.compile(r'APIRouter\(\s*prefix\s*=\s*"([^"]*)"')
# @router.get("/path", ...) —— method + 装饰器首个字符串字面量（即路由 path）
_DECORATOR_RE = re.compile(
    r'@router\.(get|post|put|patch|delete|head)\(\s*"([^"]*)"'
)

# 语义校验白名单：full_path -> 该路由文档行须包含的错误码关键词（子串匹配，
# 故 `upstream_http_` 覆盖 `upstream_http_N`）。仅对有明确错误码契约的核心
# 读路由启用；未列入的路由只走存在性校验。每条都已对照当前 INTERFACE_MAP 核实通过。
SEMANTIC_CHECKS: dict[str, list[str]] = {
    "/slimapi/sessions": ["upstream_http_", "upstream_unavailable"],
    "/slimapi/messages/{sid}": [
        "session_not_found", "upstream_http_", "upstream_unavailable", "transform_busy",
    ],
    "/slimapi/messages/{sid}/full/{mid}": [
        "session_not_found", "upstream_http_", "upstream_unavailable", "transform_busy",
    ],
    "/slimapi/command": ["upstream_http_", "upstream_unavailable", "transform_busy"],
    "/slimapi/agent": ["upstream_http_", "upstream_unavailable", "transform_busy"],
}


def collect_routes():
    """Return list of (method, full_path, file) for every declared route."""
    out = []
    for f in sorted(ROUTES_DIR.glob("*.py")):
        text = f.read_text(encoding="utf-8")
        pm = _PREFIX_RE.search(text)
        if not pm:
            continue  # 无 router 定义（如 __init__）
        prefix = pm.group(1)
        for dm in _DECORATOR_RE.finditer(text):
            method = dm.group(1).upper()
            path = dm.group(2)
            full = prefix + path  # path=="" → full 就是前缀本身
            out.append((method, full, f.name))
    return out


def _path_boundary_pattern(full_path: str) -> re.Pattern:
    # 路径后不能紧跟 word 字符或 '/'：防止前缀假匹配（/a 不应被 /a/b 命中）
    return re.compile(re.escape(full_path) + r"(?![\w/])")


def documented(doc_text: str, full_path: str) -> bool:
    return _path_boundary_pattern(full_path).search(doc_text) is not None


def semantic_missing(doc_text: str, full_path: str, required: list[str]) -> list[str]:
    """Return the subset of ``required`` keywords absent from the doc line(s)
    that document ``full_path``.

    Each INTERFACE_MAP route is a single markdown table row (one physical line,
    possibly long). We collect every line where the path appears (boundary
    match) and require ALL ``required`` keywords to be present somewhere in
    that joined text — so the error-mapping description cannot silently drift
    from the implementation while the path stays "documented".
    """
    pat = _path_boundary_pattern(full_path)
    matched_lines = [line for line in doc_text.splitlines() if pat.search(line)]
    if not matched_lines:
        # 存在性已由 documented() 兜底；此处视作无语义信息可校验（不报缺失）。
        return []
    joined = "\n".join(matched_lines)
    return [kw for kw in required if kw not in joined]


def main() -> int:
    if not DOC.exists():
        print(f"❌ 找不到 {DOC.relative_to(ROOT)}", file=sys.stderr)
        return 2
    doc = DOC.read_text(encoding="utf-8")
    routes = collect_routes()
    # 1) 存在性校验（每条路由）
    missing = [(m, p, fn) for (m, p, fn) in routes if not documented(doc, p)]
    if missing:
        print("❌ INTERFACE_MAP.md 缺失以下 /slimapi 路由文档（防漂移校验失败）：")
        for method, full, fname in missing:
            print(f"  - {method} {full}   (routes/{fname})")
        print(f"\n共 {len(missing)}/{len(routes)} 条未在 "
              f"{DOC.relative_to(ROOT)} 出现。")
        print("修复：在 INTERFACE_MAP.md 对应章节补该端点行；"
              "若该路由确应走 catch-all 透传，说明它不该在 routes/ 下声明。")
        return 1
    # 2) 语义校验（白名单路由的关键错误码关键词）
    semantic_failures: list[tuple[str, list[str]]] = []
    for _method, full, _fn in routes:
        required = SEMANTIC_CHECKS.get(full)
        if not required:
            continue
        miss = semantic_missing(doc, full, required)
        if miss:
            semantic_failures.append((full, miss))
    if semantic_failures:
        print("❌ INTERFACE_MAP.md 语义漂移：以下路由的文档行缺少关键错误码关键词：")
        for full, miss in semantic_failures:
            print(f"  - {full}：缺少 {miss}")
        print("\n修复：在该路由的 INTERFACE_MAP 行补齐对应错误码描述"
              "（与 docs/specs/v2-contract.md §7 对齐）。")
        return 1
    sem_count = sum(1 for _m, p, _f in routes if p in SEMANTIC_CHECKS)
    print(f"✅ 路由↔文档一致：{len(routes)} 条 /slimapi 路由均已在 "
          f"INTERFACE_MAP.md 记录（其中 {sem_count} 条通过语义校验）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
