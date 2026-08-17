#!/usr/bin/env python3
r"""路由 ↔ 文档一致性校验（防漂移，enforced）。

断言 `src/oc_slimapi/routes/*.py` 里每个 `/slimapi/**` 路由都在
`docs/specs/INTERFACE_MAP.md` 中有记录，且 **HTTP method 一致**。新增路由或改
method 但漏更文档时 check.sh 失败。

**两层校验**：

1. **存在性 + method 一致**（每条路由）：

   - 代码侧：用 ``ast`` 遍历 ``routes/*.py`` 收集每个
     ``@router.<method>(path)`` 装饰器（含 ``api_route`` / ``options`` / 多行
     装饰器），得到 ``(method, prefix+path, file)``。
   - 文档侧：只解析 INTERFACE_MAP **当前接口表行**（以 ``|`` 开头的物理行），
     从首单元格的 ``**<METHOD> `<path>`**`` 标题提取 ``(method, path)``。prose /
     历史段 / 删除区中的路径提及**不**满足校验（防 P1-16 rev-2 (a)：删了路由但
     文档别处还残留路径字符串）。
   - 校验：声明的每条路由的 ``(method, path)`` 必须出现在文档表行中。路径缺失 →
     存在性失败；路径在但 method 不同（如 GET 改 POST）→ method 不一致失败。

2. **语义**（`SEMANTIC_CHECKS` 白名单内的路由）：该路由所在文档表行须包含其
   关键错误码关键词（`session_not_found` / `upstream_http_` / `upstream_unavailable`
   / `transform_busy` 等）。防 P1-2 那类"路由存在但错误映射描述与实现/契约矛盾"
   的语义漂移——存在性门禁检不出。白名单刻意保守：只覆盖有明确错误码契约的
   核心读路由；新增路由如需语义校验，往 `SEMANTIC_CHECKS` 加条目即可。**expand
   两路由（2026-08-17 [3.1.0]；design-expand §8/§12 门禁）**：关键词 = `expand` /
   `EXPAND_CATEGORIES` / `12`——文档表行须携带 12 类目单一事实源引用
   （`traffic.py::EXPAND_CATEGORIES`）与类目计数，防类目表与契约/实现漂移。

**已知局限**：代码侧只扫 ``routes/*.py`` 里静态声明的 ``@router.<method>``。
若某 ``/slimapi`` 路由在别处动态注册（``app.add_api_route`` 等），本脚本看不到。
当前项目所有 ``/slimapi`` 路由都在 ``routes/*.py`` 静态声明，故无遗漏；改用
``app.routes`` 运行时遍历会引入 import 副作用（lifespan / 配置 / 上游连接），
收益不抵风险，故保留静态扫描。

退出码：0=全部通过；1=有缺失 / method 不一致 / 语义不符（列出明细）；2=文档文件缺失。
"""
from __future__ import annotations
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES_DIR = ROOT / "src/oc_slimapi/routes"
DOC = ROOT / "docs/specs/INTERFACE_MAP.md"

# APIRouter(prefix="/slimapi...") —— 取 router 自带前缀（app.include_router 无额外前缀）。
_PREFIX_RE = re.compile(r'APIRouter\(\s*prefix\s*=\s*"([^"]*)"')

# FastAPI router method attributes recognised as route declarations. Includes
# `options`; `api_route` is handled separately (it carries methods=[...]).
_METHOD_ATTRS = {"get", "post", "put", "patch", "delete", "head", "options"}

# 文档表行首单元格的 `**<METHOD> `<path>`**` 标题——提取 method + path。
_DOC_METHOD_PATH_RE = re.compile(
    r"\*\*\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+`([^`]+)`"
)

# 语义校验白名单：full_path -> 该路由文档行须包含的错误码关键词（子串匹配，
# 故 `upstream_http_` 覆盖 `upstream_http_N`）。仅对有明确错误码契约的核心
# 读路由启用；未列入的路由只走存在性 + method 校验。每条都已对照当前
# INTERFACE_MAP 核实通过。
# expand 两路由（[3.1.0]，design-expand §8/§12）：行内须含 `expand` 语义、
# 12 类目单一事实源引用 `EXPAND_CATEGORIES` 与类目计数 `12`——防类目表
# 与契约/实现漂移（路径本身恒含 `expand`，实际守卫为后两者）。
SEMANTIC_CHECKS: dict[str, list[str]] = {
    "/slimapi/sessions": ["upstream_http_", "upstream_unavailable"],
    "/slimapi/messages/{sid}": [
        "session_not_found", "upstream_http_", "upstream_unavailable", "transform_busy",
    ],
    "/slimapi/messages/{sid}/full/{mid}": [
        "session_not_found", "upstream_http_", "upstream_unavailable", "transform_busy",
    ],
    "/slimapi/messages/{sid}/expand/{category}/{mid}": [
        "expand", "EXPAND_CATEGORIES", "12",
    ],
    "/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}": [
        "expand", "EXPAND_CATEGORIES", "12",
    ],
    "/slimapi/command": ["upstream_http_", "upstream_unavailable", "transform_busy"],
    "/slimapi/agent": ["upstream_http_", "upstream_unavailable", "transform_busy"],
}


# ---------------------------------------------------------------------------
# 代码侧：AST 收集声明的路由（比正则更稳——多行装饰器 / api_route / options）
# ---------------------------------------------------------------------------


def _router_prefix(text: str) -> str | None:
    """Return the ``APIRouter(prefix=...)`` value, or None if the module
    declares no router (e.g. ``__init__.py``)."""
    m = _PREFIX_RE.search(text)
    return m.group(1) if m else None


def _decorator_methods_path(dec: ast.expr) -> tuple[list[str] | None, str | None]:
    """Extract ``(methods, path)`` from a ``@router.<attr>(...)`` decorator.

    Returns ``(None, None)`` when the decorator is not a route declaration.

    - ``@router.get("/p")`` → (["GET"], "/p"); likewise post/put/patch/delete/
      head/options.
    - ``@router.api_route("/p", methods=["GET","POST"])`` → (["GET","POST"], "/p")
      (methods pulled from the ``methods=`` keyword; missing/empty → None).
    - the first positional arg must be a string literal (the path).
    """
    if not isinstance(dec, ast.Call):
        return None, None
    func = dec.func
    if not (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "router"):
        return None, None
    attr = func.attr
    if attr == "api_route":
        if not dec.args:
            return None, None
        path_node = dec.args[0]
        if not (isinstance(path_node, ast.Constant)
                and isinstance(path_node.value, str)):
            return None, None
        methods = _api_route_methods(dec)
        return (methods, path_node.value) if methods else (None, None)
    if attr not in _METHOD_ATTRS:
        return None, None
    if not dec.args:
        return None, None
    path_node = dec.args[0]
    if not (isinstance(path_node, ast.Constant)
            and isinstance(path_node.value, str)):
        return None, None
    return [attr.upper()], path_node.value


def _api_route_methods(call: ast.Call) -> list[str]:
    """Extract uppercased method names from ``api_route(methods=[...])``."""
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            out: list[str] = []
            for elt in kw.value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value.upper())
            return out
    return []


def collect_routes() -> list[tuple[str, str, str]]:
    """Return list of ``(method, full_path, file)`` for every declared route.

    Walks each ``routes/*.py`` with :mod:`ast` so multi-line decorators,
    ``@router.api_route(..., methods=[...])`` and ``@router.options(...)`` are
    all handled (a single-line regex would miss multi-line decorators and
    ``api_route``'s ``methods=`` keyword).
    """
    out: list[tuple[str, str, str]] = []
    for f in sorted(ROUTES_DIR.glob("*.py")):
        text = f.read_text(encoding="utf-8")
        prefix = _router_prefix(text)
        if prefix is None:
            continue  # 无 router 定义（如 __init__）
        try:
            tree = ast.parse(text, filename=str(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                methods, path = _decorator_methods_path(dec)
                if methods is None or path is None:
                    continue
                for method in methods:
                    out.append((method, prefix + path, f.name))
    return out


# ---------------------------------------------------------------------------
# 文档侧：只解析当前接口表行（method + path）
# ---------------------------------------------------------------------------


def parse_doc_routes(doc_text: str) -> dict[str, set[str]]:
    """Return ``{path: {methods}}`` documented in INTERFACE_MAP **table rows**.

    Only markdown table rows (lines starting with ``|``) are scanned — prose,
    history blocks, and deleted sections are ignored so a stale mention of a
    removed route cannot satisfy the existence check (P1-16 rev-2 (a)). Method
    + path are pulled from the ``**<METHOD> `<path>`**`` heading in the row's
    first cell.
    """
    by_path: dict[str, set[str]] = {}
    for line in doc_text.splitlines():
        if not line.startswith("|"):
            continue
        for m in _DOC_METHOD_PATH_RE.finditer(line):
            method, path = m.group(1).upper(), m.group(2)
            by_path.setdefault(path, set()).add(method)
    return by_path


def _path_boundary_pattern(full_path: str) -> re.Pattern:
    # 路径后不能紧跟 word 字符或 '/'：防止前缀假匹配（/a 不应被 /a/b 命中）
    return re.compile(re.escape(full_path) + r"(?![\w/])")


def semantic_missing(doc_text: str, full_path: str, required: list[str]) -> list[str]:
    """Return the subset of ``required`` keywords absent from the doc table
    row(s) that document ``full_path``.

    Each INTERFACE_MAP route is a single markdown table row (one physical line,
    possibly long). We collect every **table row** (line starting with ``|``)
    where the path appears (boundary match) and require ALL ``required``
    keywords somewhere in that joined text — so the error-mapping description
    cannot silently drift from the implementation while the path stays
    "documented". Restricted to table rows (P1-16 rev-2 (a)) so prose mentions
    cannot satisfy the keyword check.
    """
    pat = _path_boundary_pattern(full_path)
    matched_lines = [
        line for line in doc_text.splitlines()
        if line.startswith("|") and pat.search(line)
    ]
    if not matched_lines:
        # 存在性已由存在性校验兜底；此处视作无语义信息可校验（不报缺失）。
        return []
    joined = "\n".join(matched_lines)
    return [kw for kw in required if kw not in joined]


# ---------------------------------------------------------------------------
# 校验核心（纯函数，无 I/O —— 便于单测用构造输入驱动）
# ---------------------------------------------------------------------------


def validate(
    routes: list[tuple[str, str, str]], doc_text: str,
) -> tuple[
    list[tuple[str, str, str]],
    list[tuple[str, str, list[str], str]],
    list[tuple[str, list[str]]],
]:
    """Return ``(missing, method_mismatches, semantic_failures)``.

    - ``missing``: ``(method, full_path, file)`` for declared routes whose path
      is absent from the doc table rows entirely (existence failure).
    - ``method_mismatches``: ``(method, full_path, doc_methods, file)`` for
      declared routes whose path IS documented but under a different method
      (e.g. code GET vs doc POST).
    - ``semantic_failures``: ``(full_path, [missing_keywords])`` for
      SEMANTIC_CHECKS routes whose doc row lacks a required error-code keyword.
    """
    doc_routes = parse_doc_routes(doc_text)
    missing: list[tuple[str, str, str]] = []
    method_mismatches: list[tuple[str, str, list[str], str]] = []
    for method, full, fname in routes:
        doc_methods = doc_routes.get(full)
        if doc_methods is None:
            missing.append((method, full, fname))
        elif method not in doc_methods:
            method_mismatches.append((method, full, sorted(doc_methods), fname))
    semantic_failures: list[tuple[str, list[str]]] = []
    for _method, full, _fn in routes:
        required = SEMANTIC_CHECKS.get(full)
        if not required:
            continue
        miss = semantic_missing(doc_text, full, required)
        if miss:
            semantic_failures.append((full, miss))
    return missing, method_mismatches, semantic_failures


def main() -> int:
    if not DOC.exists():
        print(f"❌ 找不到 {DOC.relative_to(ROOT)}", file=sys.stderr)
        return 2
    doc = DOC.read_text(encoding="utf-8")
    routes = collect_routes()
    missing, method_mismatches, semantic_failures = validate(routes, doc)

    if missing:
        print("❌ INTERFACE_MAP.md 缺失以下 /slimapi 路由文档（防漂移校验失败）：")
        for method, full, fname in missing:
            print(f"  - {method} {full}   (routes/{fname})")
        print(f"\n共 {len(missing)}/{len(routes)} 条未在 "
              f"{DOC.relative_to(ROOT)} 表行中出现。")
        print("修复：在 INTERFACE_MAP.md 对应章节补该端点表行（首单元格格式"
              " `**<METHOD> \\<path>**`）；若该路由确应走 catch-all 透传，"
              "说明它不该在 routes/ 下声明。")
        return 1

    if method_mismatches:
        print("❌ INTERFACE_MAP.md 与代码 HTTP method 不一致：")
        for method, full, doc_methods, fname in method_mismatches:
            print(f"  - 代码 routes/{fname} 声明 {method} {full}，"
                  f"但文档表行记为 {doc_methods}")
        print("\n修复：核对 INTERFACE_MAP 该端点表行的 method（GET/POST/...），"
              "与 routes/*.py 的 @router.<method> 装饰器对齐。")
        return 1

    if semantic_failures:
        print("❌ INTERFACE_MAP.md 语义漂移：以下路由的文档行缺少关键错误码关键词：")
        for full, miss in semantic_failures:
            print(f"  - {full}：缺少 {miss}")
        print("\n修复：在该路由的 INTERFACE_MAP 行补齐对应错误码描述"
              "（与 docs/specs/v2-contract.md §7 对齐）。")
        return 1

    sem_count = sum(1 for _m, p, _f in routes if p in SEMANTIC_CHECKS)
    print(f"✅ 路由↔文档一致：{len(routes)} 条 /slimapi 路由均已在 "
          f"INTERFACE_MAP.md 表行记录且 method 一致"
          f"（其中 {sem_count} 条通过语义校验）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
