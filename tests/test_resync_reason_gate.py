"""W1-D / F-122: resync ``reason`` value-domain gate (N1 frozen AST scan).

Spec (N1, frozen by the Wave-1 plan): parse every ``.py`` under
``src/oc_slimapi/`` with :mod:`ast`, enumerate

* every resync-path call site carrying a reason — keyword ``reason=`` call
  points plus the positional reason slot of the known resync-frame
  construction/fanout/termination helpers (``_resync_frame``,
  ``_fanout_resync``, ``_enqueue_session_resync``, ``terminate``,
  ``ReplayResync``), and
* every ``sse_frame(...)`` construction point whose ``event`` keyword is
  the literal ``"resync"`` (reason taken from the ``{"reason": ...}`` dict
  in the first positional argument),

and assert each enumerated argument value is a member of the single
frozen module-level frozenset ``SSE_RESYNC_REASONS``
(``oc_slimapi.sse.hub_types`` — the leaf anchor; the frozen v4 four plus
the v3-only lifecycle reasons). Accepted argument forms:

* a string literal that is a member of the frozen set;
* a constant-name reference (``RESYNC_X``) whose module-level assignment
  anywhere under ``src/`` resolves to a member literal — including
  through import aliases and one-hop local ``reason = <member>``
  assignments (constant-name propagation);
* a ``<var>.reason`` attribute where ``<var>`` was assigned from a frozen
  producer (``classify_reconnect(...)`` / ``ReplayResync(<member>)`` —
  the ReplayLog classify domain is the frozen four);
* the bare name ``reason`` when it is the parameter of an allowlisted
  callee (its own call sites are gated — closed system) or a for-loop
  target draining ``_pending_session_resinks`` (whose only append site is
  the gated ``_enqueue_session_resync``; exclusivity is asserted by
  :func:`test_pending_resync_queue_appends_only_in_enqueue_gate` below).

Anything else — free variables, computed expressions, non-member
literals — is a violation. ``tests/`` are NOT scanned (src tree only).

Lane interpretation note (W1-BD): dbaux ``DisabledResolution(reason=...)``
/ lifecycle ``_disable(reason=...)`` / routes ``CodedHTTPException(...,
reason="part_missing")`` are NOT resync-path sites — their reason values
never reach an ``event="resync"`` frame, and those files sit outside this
lane's write domain. Keyword-``reason=`` enumeration is therefore scoped
to the resync-path callee allowlist above rather than literally every
``reason=`` keyword in the tree; the F-122 threat model (a NEW reason
value reaching a resync frame) is fully covered by this scope.

The negative canary cases never write into ``src/`` — they scan
individually written temporary files under ``tmp_path``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from oc_slimapi.sse.hub_types import SSE_RESYNC_REASONS, V4_RESYNC_REASONS

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "oc_slimapi"

# callee name → positional index of the reason argument (-1 = keyword only).
_REASON_CALLEES: dict[str, int] = {
    "_resync_frame": 1,
    "_fanout_resync": 1,
    "_enqueue_session_resync": 1,
    "terminate": 0,
    "ReplayResync": 0,
}
# Frozen-domain producers whose ``.reason`` attribute is acceptable.
_REASON_PRODUCERS = {"classify_reconnect"}
_REPLAY_RESYNC_CTOR = "ReplayResync"


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _build_constant_table(paths: list[Path]) -> dict[str, str | None]:
    """Flat name → literal map over ALL module-level str assignments in src.

    Constant names (``RESYNC_*``) are unique across the tree; a name with
    conflicting definitions maps to ``None`` (ambiguous — any use fails).
    """
    table: dict[str, str | None] = {}
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for stmt in tree.body:  # module level only
            if not isinstance(stmt, ast.Assign):
                continue
            if not (len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                continue
            if not isinstance(stmt.value, ast.Constant) or not isinstance(stmt.value.value, str):
                continue
            name = stmt.targets[0].id
            if name in table and table[name] != stmt.value.value:
                table[name] = None
            else:
                table[name] = stmt.value.value
    return table


def _build_file_bindings(tree: ast.AST) -> tuple[dict[str, list[ast.expr]], set[str], set[tuple[str, str]]]:
    """File-wide simple-assignment map + import aliases + function params.

    The map is file-wide (not per function) on purpose: route handlers
    assign ``replay_plan`` in an outer function and consume it in a nested
    generator. Multiple bindings per name are ALL recorded (the reconnect
    idiom is ``replay_plan = None`` first, then a producer call); the
    ``None`` literal is treated as the isinstance-guard pattern and
    ignored, any non-producer binding is a violation. For-loop targets
    named ``reason`` are recorded separately. Function parameters named
    ``reason`` are recorded per function name.
    """
    assignments: dict[str, list[ast.expr]] = {}
    loop_reasons: set[str] = set()
    reason_params: set[tuple[str, str]] = set()  # (function name, "reason")

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    # alias → original name (resolve via the global table)
                    assignments.setdefault(
                        alias.asname, [ast.Name(id=alias.name, ctx=ast.Load())]
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in list(node.args.args) + list(node.args.posonlyargs) + list(node.args.kwonlyargs):
                if arg.arg == "reason":
                    reason_params.add((node.name, "reason"))
        elif isinstance(node, ast.For):
            target = node.target
            names: list[str] = []
            if isinstance(target, ast.Name):
                names = [target.id]
            elif isinstance(target, ast.Tuple):
                names = [e.id for e in target.elts if isinstance(e, ast.Name)]
            if "reason" in names:
                loop_reasons.add("reason")
    return assignments, loop_reasons, reason_params


def _resolve(expr: ast.expr, *, bindings: dict[str, list[ast.expr]], loop_reasons: set[str],
             reason_params: set[tuple[str, str]], table: dict[str, str | None],
             enclosing: str | None, depth: int = 0) -> tuple[str, str]:
    """Resolve a reason expression to ('ok', why) or ('violation', why)."""
    if depth > 6:
        return ("violation", "resolution depth exceeded (cyclic assignment?)")
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, str):
            if expr.value in SSE_RESYNC_REASONS:
                return ("ok", f"literal member {expr.value!r}")
            return ("violation", f"literal {expr.value!r} not in frozen SSE_RESYNC_REASONS")
        return ("violation", f"non-string constant {expr.value!r}")
    if isinstance(expr, ast.Name):
        name = expr.id
        if name == "reason":
            if (enclosing, "reason") in reason_params and enclosing in _REASON_CALLEES:
                return ("ok", f"parameter passthrough of gated callee {enclosing}()")
            if "reason" in loop_reasons:
                return ("ok", "for-loop target draining the enqueue-gated resync queue")
        if name in bindings:
            saw_ok = False
            for bound in bindings[name]:
                status, why = _resolve(bound, bindings=bindings, loop_reasons=loop_reasons,
                                       reason_params=reason_params, table=table,
                                       enclosing=enclosing, depth=depth + 1)
                if status == "ok":
                    saw_ok = True
            if saw_ok:
                return ("ok", f"name {name!r} has a member-valued binding")
            return ("violation", f"name {name!r} has no member-valued binding")
        if name in table:
            literal = table[name]
            if literal is None:
                return ("violation", f"constant name {name} has conflicting definitions")
            if literal in SSE_RESYNC_REASONS:
                return ("ok", f"constant {name} → {literal!r}")
            return ("violation", f"constant {name} → {literal!r} not in frozen set")
        return ("violation", f"free variable {name!r}")
    if isinstance(expr, ast.Attribute) and expr.attr == "reason":
        base = expr.value
        if isinstance(base, ast.Name) and base.id in bindings:
            saw_ok = False
            violations: list[str] = []
            for bound in bindings[base.id]:
                if isinstance(bound, ast.Constant) and bound.value is None:
                    continue  # isinstance-guard pattern: replay_plan = None first
                if isinstance(bound, ast.Call):
                    callee = _callee_name(bound)
                    if callee in _REASON_PRODUCERS:
                        saw_ok = True
                        continue
                    if callee == _REPLAY_RESYNC_CTOR and _positional(bound, 0) is not None:
                        status, why = _resolve(_positional(bound, 0), bindings=bindings,
                                               loop_reasons=loop_reasons,
                                               reason_params=reason_params, table=table,
                                               enclosing=enclosing, depth=depth + 1)
                        if status == "ok":
                            saw_ok = True
                            continue
                        violations.append(f"ReplayResync reason: {why}")
                        continue
                violations.append(f"non-producer binding {ast.dump(bound)[:40]}")
            if saw_ok and not violations:
                return ("ok", f"attribute of frozen producer of {base.id}")
            return ("violation", f".reason of {base.id}: " + "; ".join(violations))
        return ("violation", "unresolvable .reason attribute (not a frozen producer)")
    return ("violation", f"unsupported expression form {ast.dump(expr)[:60]}")


def _positional(call: ast.Call, index: int) -> ast.expr | None:
    return call.args[index] if len(call.args) > index else None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def scan_reason_sites(paths: list[Path]) -> list[str]:
    """Run the N1 scan over the given files; return violation messages."""
    paths = [p for p in paths if p.suffix == ".py"]
    table = _build_constant_table(paths)
    violations: list[str] = []

    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{path}: unparseable: {exc}")
            continue
        bindings, loop_reasons, reason_params = _build_file_bindings(tree)

        # Track the nearest enclosing function name for param-passthrough.
        def visit(node: ast.AST, enclosing: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                child_enclosing = enclosing
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child_enclosing = child.name
                if isinstance(child, ast.Call):
                    callee = _callee_name(child)
                    sites: list[tuple[ast.expr, str]] = []
                    if callee in _REASON_CALLEES:
                        expr = _keyword(child, "reason")
                        if expr is not None:
                            sites.append((expr, f"{callee}(reason=...)"))
                        else:
                            expr = _positional(child, _REASON_CALLEES[callee])
                            if expr is not None:
                                sites.append((expr, f"{callee}(..., reason)"))
                    elif callee == "sse_frame":
                        event = _keyword(child, "event")
                        if isinstance(event, ast.Constant) and event.value == "resync":
                            payload = _positional(child, 0)
                            if isinstance(payload, ast.Dict):
                                for key, value in zip(payload.keys, payload.values):
                                    if isinstance(key, ast.Constant) and key.value == "reason":
                                        sites.append((value, "sse_frame(..., event='resync')"))
                            elif payload is not None:
                                sites.append((payload, "sse_frame(<non-dict>, event='resync')"))
                    for expr, origin in sites:
                        status, why = _resolve(
                            expr, bindings=bindings, loop_reasons=loop_reasons,
                            reason_params=reason_params, table=table,
                            enclosing=child_enclosing,
                        )
                        if status != "ok":
                            violations.append(
                                f"{path}:{child.lineno}: {origin}: {why}"
                            )
                visit(child, child_enclosing)

        visit(tree, None)
    return violations


# ---------------------------------------------------------------------------
# Main gate: the whole src tree is clean.
# ---------------------------------------------------------------------------

def test_src_tree_resync_reasons_all_in_frozen_domain():
    """Every resync reason reaching a construction point under src/ is a
    member of the frozen SSE_RESYNC_REASONS set (literal or constant-name
    propagation) — no free variables, no non-member literals."""
    paths = sorted(SRC_ROOT.rglob("*.py"))
    assert paths, f"src tree not found at {SRC_ROOT}"
    violations = scan_reason_sites(paths)
    assert not violations, "resync reason domain violations:\n" + "\n".join(violations)


def test_frozen_sets_are_frozen_and_complete():
    """The oracle itself: frozenset instances with the exact frozen members
    (v4 four + v3 lifecycle four). Any change here is a wire-contract
    change and must go through the contract process, not a silent edit."""
    assert type(SSE_RESYNC_REASONS) is frozenset
    assert type(V4_RESYNC_REASONS) is frozenset
    assert V4_RESYNC_REASONS == frozenset({
        "epoch_changed", "replay_expired", "replay_gap", "reconnect_no_replay",
    })
    assert SSE_RESYNC_REASONS == V4_RESYNC_REASONS | frozenset({
        "subscriber_backpressure", "session_idle", "session_deleted",
        "token_memory_limit",
    })


def test_pending_resync_queue_appends_only_in_enqueue_gate():
    """Soundness premise for the for-target passthrough: the ONLY append
    to ``_pending_session_resinks`` lives inside ``_enqueue_session_resync``
    (itself an enumerated, gated callee). If a second writer appears, the
    passthrough acceptance in the scanner must be re-audited."""
    flush_engine = SRC_ROOT / "sse" / "tokenstream" / "flush_engine.py"
    tree = ast.parse(flush_engine.read_text(encoding="utf-8"), filename=str(flush_engine))

    class _FuncVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.current: str | None = None
            self.append_functions: set[str] = set()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            prev, self.current = self.current, node.name
            self.generic_visit(node)
            self.current = prev

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "append"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "_pending_session_resinks"):
                assert self.current is not None
                self.append_functions.add(self.current)
            self.generic_visit(node)

    visitor = _FuncVisitor()
    visitor.visit(tree)
    assert visitor.append_functions == {"_enqueue_session_resync"}, (
        f"_pending_session_resinks.append found outside the gated enqueue: "
        f"{visitor.append_functions}"
    )


# ---------------------------------------------------------------------------
# Canary cases: temporary files under tmp_path (src/ is never written).
# ---------------------------------------------------------------------------

def _scan_snippet(tmp_path: Path, source: str) -> list[str]:
    snippet = tmp_path / "canary_module.py"
    snippet.write_text(source, encoding="utf-8")
    return scan_reason_sites([snippet])


def test_canary_new_reason_literal_goes_red(tmp_path):
    """A brand-new reason literal at a gated call point must be flagged."""
    violations = _scan_snippet(tmp_path, (
        'def f(sid):\n'
        '    return _resync_frame(sid, "canary_new_reason")\n'
    ))
    assert any("canary_new_reason" in v for v in violations), violations
    assert any("_resync_frame" in v for v in violations)


def test_canary_new_reason_in_sse_resync_frame_goes_red(tmp_path):
    """Same for the dict-literal construction inside sse_frame(event='resync')."""
    violations = _scan_snippet(tmp_path, (
        'def f():\n'
        '    return sse_frame({"reason": "canary_new_reason"}, event="resync")\n'
    ))
    assert any("canary_new_reason" in v and "sse_frame" in v for v in violations), violations


def test_canary_free_variable_goes_red(tmp_path):
    """A free variable (no constant/param/loop binding) is rejected."""
    violations = _scan_snippet(tmp_path, (
        'def f(sid):\n'
        '    return _fanout_resync(sid, some_free_variable)\n'
    ))
    assert any("free variable" in v for v in violations), violations


def test_canary_non_resync_event_is_not_enumerated(tmp_path):
    """Scope control: a reason key on a NON-resync sse_frame is not a
    resync construction point and must NOT be flagged."""
    violations = _scan_snippet(tmp_path, (
        'def f():\n'
        '    return sse_frame({"reason": "canary_new_reason"}, event="message.updated")\n'
    ))
    assert violations == [], violations


def test_canary_member_literal_and_constant_pass(tmp_path):
    """Positive controls: member literal + constant-name propagation + the
    gated-param passthrough all stay green."""
    violations = _scan_snippet(tmp_path, (
        'RESYNC_CUSTOM = "replay_gap"\n'
        'def _resync_frame(sid, reason):\n'
        '    return sse_frame({"reason": reason}, event="resync")\n'
        'def g(sid):\n'
        '    return _resync_frame(sid, "reconnect_no_replay")\n'
        'def h(sid):\n'
        '    return _resync_frame(sid, RESYNC_CUSTOM)\n'
    ))
    assert violations == [], violations
