#!/usr/bin/env python3
r"""路由 ↔ 文档一致性校验（防漂移，enforced）。

断言 `src/oc_slimapi/routes/*.py` 里每个 `/slimapi/**` 路由都在
`docs/specs/INTERFACE_MAP.md` 中有记录。新增路由但漏更文档时 check.sh 失败。

匹配用路径边界（路径后不能跟 `\w` 或 `/`），避免 `/slimapi/sessions` 被
`/slimapi/sessions/status` 这类更长路径误判为已记录。

退出码：0=全部已记录；1=有缺失（列出明细）。
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


def documented(doc_text: str, full_path: str) -> bool:
    # 路径后不能紧跟 word 字符或 '/'：防止前缀假匹配（/a 不应被 /a/b 命中）
    pat = re.escape(full_path) + r"(?![\w/])"
    return re.search(pat, doc_text) is not None


def main() -> int:
    if not DOC.exists():
        print(f"❌ 找不到 {DOC.relative_to(ROOT)}", file=sys.stderr)
        return 2
    doc = DOC.read_text(encoding="utf-8")
    routes = collect_routes()
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
    print(f"✅ 路由↔文档一致：{len(routes)} 条 /slimapi 路由均已在 "
          f"INTERFACE_MAP.md 记录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
