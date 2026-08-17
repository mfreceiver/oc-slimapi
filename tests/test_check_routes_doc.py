"""Unit tests for scripts/check_routes_doc.py (P1-16 rev-2 enhancement).

The route↔doc consistency check was strengthened to:

- parse INTERFACE_MAP **table rows** only (not full-text prose), extracting
  ``(method, path)`` so a stale prose mention of a removed route can no longer
  satisfy the existence check;
- validate the HTTP **method** too (code GET vs doc POST now fails), not just
  path existence;
- collect declared routes via ``ast`` (handles multi-line decorators,
  ``@router.api_route``, ``@router.options``).

These tests drive the pure ``validate`` / ``parse_doc_routes`` functions with
crafted inputs (no real files needed) plus one integration test against the
actual repo.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "scripts" / "check_routes_doc.py"


def _load_script_module():
    """Load scripts/check_routes_doc.py as a module (it is a standalone
    script, not part of a package)."""
    spec = importlib.util.spec_from_file_location("check_routes_doc", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def crd():
    return _load_script_module()


# ---------------------------------------------------------------------------
# parse_doc_routes — table rows only, method + path extraction
# ---------------------------------------------------------------------------


def test_parse_doc_routes_extracts_method_and_path(crd):
    doc = "| **GET `/slimapi/foo`**<br>desc | col2 |"
    result = crd.parse_doc_routes(doc)
    assert result == {"/slimapi/foo": {"GET"}}


def test_parse_doc_routes_ignores_prose_mentions(crd):
    """A path mentioned only in prose (not a table row) must NOT count as
    documented — P1-16 rev-2 (a)."""
    doc = (
        "| **GET `/slimapi/foo`**<br>x | y |\n"
        "Some prose paragraph mentioning `/slimapi/bar` inline.\n"
        "| **POST `/slimapi/baz`**<br>z | w |\n"
    )
    result = crd.parse_doc_routes(doc)
    assert "/slimapi/foo" in result
    assert "/slimapi/baz" in result and "POST" in result["/slimapi/baz"]
    # /slimapi/bar appears only in a non-table prose line → not documented.
    assert "/slimapi/bar" not in result


def test_parse_doc_routes_multi_method_same_path(crd):
    """If a table row lists two methods for one path, both are recorded."""
    doc = (
        "| **GET `/slimapi/foo`**<br>x | y |\n"
        "| **POST `/slimapi/foo`**<br>x | y |\n"
    )
    result = crd.parse_doc_routes(doc)
    assert result["/slimapi/foo"] == {"GET", "POST"}


# ---------------------------------------------------------------------------
# validate — existence, method mismatch, semantic
# ---------------------------------------------------------------------------


def test_validate_method_match_passes(crd):
    routes = [("GET", "/slimapi/foo", "foo.py")]
    doc = "| **GET `/slimapi/foo`**<br>desc | col2 |"
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert missing == []
    assert mismatches == []
    assert semantic == []


def test_validate_method_mismatch_reported(crd):
    """Code declares GET but doc table row says POST → method mismatch
    (P1-16 rev-2 (b): previously only path existence was checked, so this
    would pass silently)."""
    routes = [("GET", "/slimapi/foo", "foo.py")]
    doc = "| **POST `/slimapi/foo`**<br>desc | col2 |"
    missing, mismatches, semantic = crd.validate(routes, doc)
    # Path exists in doc, so this is NOT a missing-path failure...
    assert missing == []
    # ...but the method differs → mismatch reported.
    assert len(mismatches) == 1
    code_method, full, doc_methods, fname = mismatches[0]
    assert code_method == "GET"
    assert full == "/slimapi/foo"
    assert doc_methods == ["POST"]
    assert fname == "foo.py"


def test_validate_path_only_in_prose_is_missing(crd):
    """A path that appears only in prose (not a table row) is treated as
    undocumented → existence failure (P1-16 rev-2 (a))."""
    routes = [("GET", "/slimapi/foo", "foo.py")]
    doc = "Prose paragraph mentioning /slimapi/foo but not in a table row."
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert len(missing) == 1
    assert missing[0][1] == "/slimapi/foo"
    assert mismatches == []


def test_validate_unknown_path_is_missing(crd):
    routes = [("GET", "/slimapi/never", "x.py")]
    doc = "| **GET `/slimapi/other`**<br>x | y |"
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert len(missing) == 1
    assert missing[0][1] == "/slimapi/never"
    assert mismatches == []


def test_validate_semantic_missing_keyword_reported(crd, monkeypatch):
    """A SEMANTIC_CHECKS route whose doc row lacks a required keyword is
    flagged (existing behaviour, preserved)."""
    routes = [("GET", "/slimapi/sessions", "sessions.py")]
    doc = "| **GET `/slimapi/sessions`**<br>no error codes here | y |"
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert missing == []
    assert mismatches == []
    # /slimapi/sessions requires upstream_http_ + upstream_unavailable.
    assert len(semantic) == 1
    assert semantic[0][0] == "/slimapi/sessions"
    assert "upstream_http_" in semantic[0][1]
    assert "upstream_unavailable" in semantic[0][1]


# ---------------------------------------------------------------------------
# expand 路由语义门禁（design-expand §8/§12：12 category 数量一致 +
# EXPAND_CATEGORIES 单一事实源引用）
# ---------------------------------------------------------------------------


def test_validate_expand_row_with_markers_passes(crd):
    """An expand route doc row carrying the full semantic markers (expand
    semantics + ``traffic.py::EXPAND_CATEGORIES`` source-of-truth reference
    + 12-category count) passes — positive case."""
    routes = [("GET", "/slimapi/messages/{sid}/expand/{category}/{mid}", "messages.py")]
    doc = (
        "| **GET `/slimapi/messages/{sid}/expand/{category}/{mid}`**<br>"
        "12 类目，`traffic.py::EXPAND_CATEGORIES` 单一事实源，expand 语义 | y |"
    )
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert missing == []
    assert mismatches == []
    assert semantic == []


def test_validate_expand_row_without_source_of_truth_fails(crd):
    """Stripping the ``EXPAND_CATEGORIES`` source-of-truth reference and the
    12-category count from an expand doc row must be flagged. The path itself
    still contains ``expand``, so the reported missing keywords are the two
    drift guards (design-expand §12 gate: 12 category count consistent)."""
    routes = [("GET", "/slimapi/messages/{sid}/expand/{category}/{mid}", "messages.py")]
    doc = (
        "| **GET `/slimapi/messages/{sid}/expand/{category}/{mid}`**<br>"
        "row without source-of-truth reference or category count | y |"
    )
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert missing == []
    assert mismatches == []
    assert len(semantic) == 1
    assert semantic[0][0] == "/slimapi/messages/{sid}/expand/{category}/{mid}"
    assert semantic[0][1] == ["EXPAND_CATEGORIES", "12"]


def test_validate_part_level_expand_row_without_markers_fails(crd):
    """Part-level expand route is gated the same way."""
    routes = [
        ("GET", "/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}", "messages.py"),
    ]
    doc = (
        "| **GET `/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`**<br>"
        "bare description with no category-table markers | y |"
    )
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert missing == []
    assert mismatches == []
    assert len(semantic) == 1
    assert semantic[0][0] == "/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}"
    assert semantic[0][1] == ["EXPAND_CATEGORIES", "12"]


# ---------------------------------------------------------------------------
# AST collect_routes — api_route / options support
# ---------------------------------------------------------------------------


def test_decorator_methods_path_standard(crd):
    """Standard @router.get('/p') → (['GET'], '/p')."""
    import ast
    tree = ast.parse('@router.get("/p")\nasync def f():\n    pass')
    fn = tree.body[0]
    methods, path = crd._decorator_methods_path(fn.decorator_list[0])
    assert methods == ["GET"]
    assert path == "/p"


def test_decorator_methods_path_api_route(crd):
    """@router.api_route('/p', methods=['GET','POST']) → both methods.
    P1-16 rev-2 (c): the old single-line regex missed api_route entirely."""
    import ast
    tree = ast.parse(
        '@router.api_route("/p", methods=["GET", "POST"])\nasync def f():\n    pass'
    )
    fn = tree.body[0]
    methods, path = crd._decorator_methods_path(fn.decorator_list[0])
    assert sorted(methods) == ["GET", "POST"]
    assert path == "/p"


def test_decorator_methods_path_options(crd):
    """@router.options('/p') → (['OPTIONS'], '/p') (old regex missed options)."""
    import ast
    tree = ast.parse('@router.options("/p")\nasync def f():\n    pass')
    fn = tree.body[0]
    methods, path = crd._decorator_methods_path(fn.decorator_list[0])
    assert methods == ["OPTIONS"]
    assert path == "/p"


def test_decorator_methods_path_non_route_decorator(crd):
    """Non-route decorators (e.g. @property) are ignored."""
    import ast
    tree = ast.parse('@property\ndef f(self):\n    return 1')
    fn = tree.body[0]
    methods, path = crd._decorator_methods_path(fn.decorator_list[0])
    assert methods is None
    assert path is None


# ---------------------------------------------------------------------------
# Integration: the real repo must pass its own check
# ---------------------------------------------------------------------------


def test_real_repo_routes_and_doc_are_consistent(crd):
    """The actual routes/*.py + INTERFACE_MAP.md must validate clean."""
    routes = crd.collect_routes()
    assert len(routes) >= 12  # sanity: we know the project has ≥12 /slimapi routes
    doc = (ROOT / "docs/specs/INTERFACE_MAP.md").read_text(encoding="utf-8")
    missing, mismatches, semantic = crd.validate(routes, doc)
    assert missing == [], f"missing routes: {missing}"
    assert mismatches == [], f"method mismatches: {mismatches}"
    assert semantic == [], f"semantic failures: {semantic}"


def test_real_repo_main_returns_zero(crd):
    """End-to-end: the script's main() exits 0 on the real repo."""
    assert crd.main() == 0


def test_method_mismatch_in_real_doc_would_fail(crd, monkeypatch, tmp_path):
    """Mutate a copy of the real doc so one route's method is wrong → main()
    must report it (proves the method check is wired into the exit code, not
    just the pure function)."""
    real_doc = (ROOT / "docs/specs/INTERFACE_MAP.md").read_text(encoding="utf-8")
    # Flip the /slimapi/sessions row from GET to POST (in the table-row heading).
    mutated = real_doc.replace(
        "**GET `/slimapi/sessions`**", "**POST `/slimapi/sessions`**", 1,
    )
    assert mutated != real_doc  # sanity: the replacement happened
    monkeypatch.setattr(crd, "DOC", tmp_path / "INTERFACE_MAP.md")
    (tmp_path / "INTERFACE_MAP.md").write_text(mutated, encoding="utf-8")
    assert crd.main() == 1
