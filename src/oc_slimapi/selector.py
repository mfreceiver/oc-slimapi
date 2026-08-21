"""Wire-contract version selector — **v4-only window (4, 4)** (v4-contract §2,
2026-08-21 narrowing revision).

A pure-ASGI dispatch layer that decides — for ``/slimapi/**`` requests only —
which wire pipeline a request runs:

* **``?v=4``** → the v4 pipeline (marked ``v4`` in ASGI scope state,
  ``wire="4"``). Since the 2026-08-21 version-window narrowing (target
  5.0.0) this is the ONLY admitted wire version.
* **``?v=3``** / **no ``v``** / **lexically valid but not in {4}** → 400
  ``{"code":"unsupported_version","supported":[4]}`` — the endpoint
  exists, the protocol version is unsupported; never a silent 404.
* **lexically invalid** (``0``, ``03``, ``+3``, `` 3``, ``3.0``, empty, …) or
  **conflicting multi-value** (``?v=3&v=4``) → 400
  ``{"code":"invalid_version_selector"}``; same-value repeats (``?v=4&v=4``)
  fold to one.
* **consumption (§5.2)**: every ``v`` parameter pair is stripped from the
  downstream query string on ALL forwarded ``/slimapi/**`` requests — ``v``
  is a sidecar-reserved parameter, never seen by a route or forwarded
  upstream. Remaining parameters keep their original bytes (encoding /
  order / repeats verbatim).
* ``GET /slimapi/versions`` (slash-normalised) is **unconditionally exempt**:
  it never passes the selector judgement. Non-GET on that path → ``405`` +
  ``Allow: GET`` — priority above everything (checked first, §8.3 ①).
* **catch-all (non ``/slimapi``) requests are untouched** — zero ``v`` /
  directory consumption; the (closed) proxy answers them (§8.2).

Directory rules (§5/§8.3 ③) for admitted requests on the consuming set —
evaluated in the frozen priority order. One slot AHEAD of them (§16,
2026-08-19 revision): an admitted ``?v=4`` request whose (method, path) is
one of the three deferred POST combos answers the coded 405
``method_not_applicable`` BEFORE directory consumption (the §8.3 chain:
① versions 405 → ② version 400s → method 405 → ③ directory 400s — the
method judgement reads no query parameter):

**directory ladder** (§5.1 — unchanged terminal semantics, identical for
every admitted request since the window collapsed to v4-only):

1. ``?directory=`` multi-value distinct (normalised) → 400
   ``invalid_directory_selector``;
2. query + ``X-Opencode-Directory`` header dual-present, normalised
   different → 400 ``directory_conflict`` (frozen ``queryDirectory`` /
   ``headerDirectory`` fields);
3. header present in any other form (header-only, or dual-present
   normalised-same) → 400 ``directory_header_retired`` — the header is
   retired input; ``?directory=`` is the only channel;
4. query-only single value → consumed: validated, stashed for the route,
   and stripped from the downstream query (§5.2).

**v4 consuming-set fork (§5.2)**: the fork removes ONLY the global
sessions list (``^/slimapi/sessions$``) from the baseline consuming set.
Every other route keeps the consumption semantics above verbatim. A v4
request on the retired route carrying ``directory`` in ANY form (query
single / query multi / header any / query+header mixed) → 400
``directory_retired_in_v4`` (uniform body + hint, no directory-existence
leak), and the retirement error takes priority over the multi-value /
conflict / header validation ladder. A v4 sessions request WITHOUT any
directory input forwards untouched (global facade, nothing to consume).

The stream route (``/slimapi/sessions/{sid}/stream``) keeps its §5.6
exception for the LAST case only: a single-valued query-only directory is
accepted as a no-op (not consumed, not stripped, forwarded verbatim); the
three error cases above apply unchanged. Tolerant (§5.5) routes never
consume and never error.

Observability (§9.1, enum frozen): the selector stashes ``selectorResult``
(v3|v4|rejected|exempt|not_applicable — the ``v3`` dim no longer occurs
by construction since the narrowing; the historical ``absent``/``v2``
dims never occur either, keeping old access-log rows interpretable),
``wireVersion`` ("3"|"4"|None) and ``directoryForm``
(query|header|both|absent|None) into ``scope["state"]`` under
:data:`SELECTOR_STATE_KEY` / :data:`DIRECTORY_FORM_STATE_KEY` where
the traffic-accounting middleware (which wraps this one) reads them at
request end. A non-``/slimapi`` request is stashed ``not_applicable``.
Routes read the per-request view back via :func:`wire_view_from_scope`
(same stash, same value — S-B04: no mismatched 3/4 combinations).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote_plus

from starlette.types import ASGIApp, Receive, Scope, Send

from . import readiness as readiness_mod
from .directory import normalize_directory, validate_directory
from .errors import CodedHTTPException
from .gzip_util import json_response
from .versioning import ACCEPTED_CLIENT_VERSIONS, _is_slimapi_path

# Stable scope-state keys (read by traffic accounting + health routes).
SELECTOR_STATE_KEY = "slimapi_selector"
DIRECTORY_FORM_STATE_KEY = "slimapi_directory_form"

# §9.1 selectorResult enum (frozen — v4/rejected/exempt/not_applicable are
# the producible values under the v4-only (4, 4) window; the v3 producer was
# removed with the 2026-08-21 narrowing — the "v3" dim lives on in
# SSE_RESULT_DIMS below, and the historical absent/v2 dims live on only as
# sseActive literals, keeping old access-log rows interpretable).
SELECTOR_V4 = "v4"
SELECTOR_REJECTED = "rejected"
SELECTOR_EXEMPT = "exempt"
SELECTOR_NOT_APPLICABLE = "not_applicable"

# §9.1 sseActive dims (§9.2): rejected/exempt have no SSE endpoints.
# Dual-window (v3,v4): the "v4" dim appears when a v4-admitted request
# opens an SSE stream — the value set widened, the shape did not.
SSE_RESULT_DIMS = ("v2", "v3", "v4", "absent", "not_applicable")

VERSION_QUERY_PARAM = "v"
VERSIONS_PATH = "/slimapi/versions"

# §5.3/§5.7: the canonical (and only) v3 directory input is the
# ``?directory=`` query parameter; the ``X-Opencode-Directory`` header is
# retired input — presence on a consuming route is a 400.
DIRECTORY_QUERY_PARAM = "directory"
DIRECTORY_HEADER_NAME = "x-opencode-directory"

# Scope-state key: set ONLY when a v3 request on a §5.3 consuming (non-stream)
# route actually supplied a usable ``?directory=`` value — the value is the
# validated resolved directory (consume succeeded). Routes read it via
# :func:`resolve_route_directory` instead of the (now stripped) query param.
V3_DIRECTORY_STATE_KEY = "slimapi_v3_directory"

# §2 lexical rule: ASCII digits, no leading zero, at least one digit.
_SELECTOR_LEXICAL_RE = re.compile(r"^[1-9][0-9]*$")

# Slash-collapse for the /versions exemption + consuming-set match (P1-14
# parity: routing still sees the raw path; only these decisions normalise).
_SLASH_RE = re.compile(r"/+")

# The admitted wire versions, ascending (v4-contract §2, 2026-08-21
# narrowing: [4] — the window collapsed to v4-only; single source of
# truth = versioning pin).
SUPPORTED_WIRE_VERSIONS: tuple[int, ...] = tuple(
    range(ACCEPTED_CLIENT_VERSIONS[0], ACCEPTED_CLIENT_VERSIONS[1] + 1)
)

# §5.3 directory-consuming set. NOTE:
# ``/slimapi/sessions/{sid}/stream`` is included because §5.6/§5.7 give its
# directory inputs consuming-set error semantics (multi-value / dual-present
# / retired header); only its query-only happy case is a no-op.
_DIRECTORY_CONSUMING_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"^/slimapi/messages/[^/]+$",
        r"^/slimapi/messages/[^/]+/full/[^/]+$",
        # Expand fragments (design-expand.md §5): message-level
        # ``/expand/{category}/{mid}`` and part-level
        # ``/expand/{category}/{mid}/{partID}`` — both directory-consuming
        # (the upstream full-message GET forwards the directory).
        r"^/slimapi/messages/[^/]+/expand/[^/]+/[^/]+$",
        r"^/slimapi/messages/[^/]+/expand/[^/]+/[^/]+/[^/]+$",
        r"^/slimapi/sessions$",
        r"^/slimapi/sessions/status$",
        r"^/slimapi/sessions/[^/]+/todo$",
        r"^/slimapi/sessions/[^/]+/children$",
        r"^/slimapi/sessions/[^/]+/diff$",
        r"^/slimapi/sessions/[^/]+/stream$",
        r"^/slimapi/agent$",
        r"^/slimapi/command$",
        # §10.a read groups — directory-sensitive per group definitions:
        # file (FileQuery/WorkspaceRoutingQuery), vcs
        # (WorkspaceRoutingQuery/VcsDiffQuery), find (FindFileQuery),
        # providers (WorkspaceRoutingQuery), session single
        # (WorkspaceRoutingQuery). NOT here (tolerant): active
        # (/api/session/active — no query in protocol) and globalHealth
        # (/global/health — no query).
        r"^/slimapi/file$",
        r"^/slimapi/file/content$",
        r"^/slimapi/file/status$",
        r"^/slimapi/vcs$",
        r"^/slimapi/vcs/status$",
        r"^/slimapi/vcs/diff$",
        r"^/slimapi/find/file$",
        r"^/slimapi/config/providers$",
        r"^/slimapi/session/[^/]+$",
        # §10.b write routes — ALL 12 consume directory (upstream
        # groups/session.ts + groups/question.ts declare
        # WorkspaceRoutingQuery on every write endpoint). The
        # PATCH/DELETE /session/{id} pattern is shared with the §10.a
        # session-single GET above (already present).
        r"^/slimapi/session$",
        r"^/slimapi/session/[^/]+/(prompt_async|abort|summarize|fork|revert|command)$",
        r"^/slimapi/session/[^/]+/permissions/[^/]+$",
        r"^/slimapi/question/[^/]+/(reply|reject)$",
    )
)


# §5.2 v4 consuming-set fork (SET DIFFERENCE, not a re-definition): the v4
# directory-retirement table lists ONLY what leaves the v3 consuming set —
# the global sessions list. Every pattern not listed here inherits the v3
# consumption semantics verbatim, so the two sets can never drift apart
# (both are checked against the shared `_DIRECTORY_CONSUMING_PATTERNS`
# source above).
_DIRECTORY_V4_RETIRED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/slimapi/sessions$"),
)

# Uniform v4 retirement error body (§5.2/§8.1): code + hint only — never a
# directory echo, never an existence leak. Shared constant so every
# triggering form (query single/multi, header any, mixed) answers
# byte-identically.
_DIRECTORY_RETIRED_IN_V4_BODY: dict[str, Any] = {
    "code": "directory_retired_in_v4",
    "hint": (
        "v4 sessions is a global facade; remove the directory parameter "
        "(and the X-Opencode-Directory header). Token/per-session routes "
        "still accept ?directory=."
    ),
}

# §16.1 method applicability / transitional 405 boundary (2026-08-19
# formal revision, revision 2: two-condition conjunction). Three deferred
# POST combos answer a coded 405 ``method_not_applicable`` on the ``?v=4``
# face ONLY while the conjunction holds (§16.1, frozen):
#
#     method.boundary.v4 ∈ SATISFIED  ∧  session.post-actions.v4 ∉ SATISFIED
#
# §16.3 four-combination table: boundary∈∧post∉ → the coded 405 below
# (the 4.2.0 published behavior); boundary∈∧post∈ (ACTIVATION) → the
# selector PASSES the three combos through to the route registry — the
# §16.2 equivalence handlers take over and the 405 face disappears;
# boundary∉ → the face is off regardless (4.0.0 behavior; the
# boundary∉∧post∈ cell is unreachable — §3.3 implication ⑦ is enforced at
# construction in readiness.py).
#
# Enforced HERE — after the selector's version-family admission (②) and
# BEFORE directory consumption (③) — because §8.3 (v4-contract.md:306 +
# §16 "优先级") slots this 405 between the two: the judgement depends on
# (method, path) alone, never on a query parameter, so a degenerate
# directory input must NOT win the race (the §8.3 frozen chain:
# ① versions 405 → ② version 400s → method 405 → ③ directory 400s).
#
# Frozen literals (§16.1): the Allow header text mirrors the allow array;
# archive/delete carry an EMPTY Allow: (RFC 9110 §10.2.1 — no method is
# applicable on the un-annexed sub-action paths). Both feature gates are
# read dynamically at request time (mirrors the routes' helper
# convention), so a readiness flip reassigns behavior with zero edits in
# this module. Conjunction closed, or any non-v4 wire → the request keeps
# the 4.0.0 published answer (the directory ladder, then the route
# pipeline → catch-all 404 ``thin_route_not_found``).
_V4_METHOD_BOUNDARY_FEATURE = "method.boundary.v4"
_V4_POST_ACTIONS_FEATURE = "session.post-actions.v4"

_METHOD_BOUNDARY_POST_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    # POST /slimapi/session/{sid} — deferred POST-only update; the collected
    # methods on that path are the Allow hint.
    (re.compile(r"^/slimapi/session/[^/]+$"), ("GET", "PATCH", "DELETE")),
    # Deferred cascade sub-actions — un-annexed paths, nothing is applicable.
    (re.compile(r"^/slimapi/session/[^/]+/archive$"), ()),
    (re.compile(r"^/slimapi/session/[^/]+/delete$"), ()),
)


def _v4_method_boundary_405_live() -> bool:
    """§16.1 two-condition conjunction (revision 2, frozen): the
    transitional coded 405 is live iff the fallback face exists
    (``method.boundary.v4 ∈ SATISFIED``) AND the equivalence family is not
    activated yet (``session.post-actions.v4 ∉ SATISFIED``). With
    post-actions satisfied the selector passes the three combos through —
    the §16.2 handlers take over (§16.3 third cell)."""
    satisfied = readiness_mod.SATISFIED
    return (_V4_METHOD_BOUNDARY_FEATURE in satisfied
            and _V4_POST_ACTIONS_FEATURE not in satisfied)


def _method_boundary_allow(
    normalized_path: str, method: str
) -> tuple[str, ...] | None:
    """Frozen ``allow`` tuple when (method, path) is one of the three §16
    deferred POST combos, else ``None`` (every other combo — including
    GET/PATCH/DELETE on the same paths — inherits the published behavior)."""
    if method != "POST":
        return None
    for pattern, allow in _METHOD_BOUNDARY_POST_PATTERNS:
        if pattern.match(normalized_path) is not None:
            return allow
    return None


def _normalize_path(path: str) -> str:
    return _SLASH_RE.sub("/", path)


def _is_directory_consuming(normalized_path: str) -> bool:
    return any(p.match(normalized_path) is not None for p in _DIRECTORY_CONSUMING_PATTERNS)


def _is_v4_directory_retired(normalized_path: str) -> bool:
    return any(
        p.match(normalized_path) is not None
        for p in _DIRECTORY_V4_RETIRED_PATTERNS
    )


def _directory_consuming_for(normalized_path: str) -> bool:
    """§5.2 consuming-set membership under the v4-only window.

    v4 = the historical v3 set − the retirement table (only the global
    sessions list leaves); callers must still handle the retired route
    SEPARATELY (any directory input on it is a 400
    ``directory_retired_in_v4``, not tolerant passthrough). (The
    ``wire_version`` parameter of the (3, 4) dual-window era was removed
    with the 2026-08-21 narrowing — only the v4 set remains.)
    """
    if _is_v4_directory_retired(normalized_path):
        return False
    return _is_directory_consuming(normalized_path)


def _has_query_key(query_string: bytes, key: str) -> bool:
    """True iff the raw query string carries ``key`` with a non-empty value.

    ``keep_blank_values=True`` so the key is seen at all; empty values do not
    count as a directory form (a degenerate ``?directory=`` is not a usable
    directory input).
    """
    try:
        pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
    except Exception:
        return False
    return any(k == key and v for k, v in pairs)


def _has_directory_header(scope: Scope) -> bool:
    """PRESENCE-based (M3-1 / §5.7 terminal): True iff the request carries
    an ``X-Opencode-Directory`` header AT ALL — an empty or whitespace-only
    value still counts (``directoryForm`` observes arrival, not usability).
    """
    for name, _value in scope.get("headers") or []:
        if name.lower() == b"x-opencode-directory":
            return True
    return False


def _directory_form(scope: Scope) -> str | None:
    """§9.1 directoryForm for this request (None ⇒ non-consuming route)."""
    normalized = _normalize_path(scope.get("path", "") or "")
    if not _is_directory_consuming(normalized):
        return None
    has_query = _has_query_key(scope.get("query_string", b"") or b"", "directory")
    has_header = _has_directory_header(scope)
    if has_query and has_header:
        return "both"
    if has_query:
        return "query"
    if has_header:
        return "header"
    return "absent"


def _stash(scope: Scope, result: str, wire: str | None) -> None:
    """Record the selector outcome into ``scope["state"]`` (§9.1)."""
    state = scope.setdefault("state", {})
    if isinstance(state, dict):
        state[SELECTOR_STATE_KEY] = {"result": result, "wire": wire}


def selector_info_from_scope(scope: Scope) -> dict[str, Any]:
    """Read the stashed selector info ({} when the selector did not run)."""
    state = scope.get("state")
    if not isinstance(state, dict):
        return {}
    info = state.get(SELECTOR_STATE_KEY)
    return info if isinstance(info, dict) else {}


def wire_view_from_scope(scope: Scope) -> int:
    """§2/S-B04: the wire view this request runs — 4 (the only live view).

    The selector stashes ``"4"`` for admitted ``?v=4`` requests; every
    other scope — rejected / exempt / not-applicable (which never reach
    routes) and selector-less direct invocation in tests — ALSO observes
    4: the historical default-3 fallback was the v3-face teardown surface
    and was flipped to 4 with the 2026-08-21 V2b narrowing teardown (the
    stash-read mechanism is retained for a future widened window; under
    the (4, 4) window every leg resolves to 4, so the value is returned
    directly).

    Kept as a function so the call sites stay explicit about where the
    view comes from (health/versions/routes must all read THIS value —
    mismatched view combinations are structurally impossible, S-B04).
    """
    return 4


def _has_directory_query_pair(query_string: bytes) -> bool:
    """True iff the raw query string carries a ``directory`` KEY at all.

    Unlike :func:`_has_query_key` (which requires a non-empty value for
    the §9.1 directoryForm dim), the v4 retirement judgement is key
    PRESENCE — a degenerate ``?directory=`` is still the retired channel
    being exercised, so it retires too (uniform single error body).
    """
    try:
        pairs = parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
    except Exception:
        return False
    return any(k == DIRECTORY_QUERY_PARAM for k, _v in pairs)


def resolve_route_directory(scope: Scope, query_value: str | None) -> str | None:
    """Final directory for a §5.3 consuming route.

    * **consumed ``?directory=``** — the dispatch layer already validated +
      resolved the query values and stripped them from the downstream query;
      the stash replaces the (now-absent) FastAPI query param. The stashed
      value is already validated — routes may re-run ``validate_directory``
      on it (idempotent, pure).
    * **everything else** (no directory supplied, tolerant routes, direct
      route invocation in tests) — the caller's FastAPI-bound query value,
      unchanged.
    """
    state = scope.get("state")
    if isinstance(state, dict) and V3_DIRECTORY_STATE_KEY in state:
        return state[V3_DIRECTORY_STATE_KEY]
    return query_value


def _collect_v_values(scope: Scope) -> list[str]:
    try:
        pairs = parse_qsl(
            (scope.get("query_string", b"") or b"").decode("latin-1"),
            keep_blank_values=True,
        )
    except Exception:
        return []
    return [value for key, value in pairs if key == VERSION_QUERY_PARAM]


def _collect_directory_values(scope: Scope) -> list[str]:
    """Every ``directory`` query value (blank values kept — §5 consumption
    judges them like the routes' FastAPI binding does)."""
    try:
        pairs = parse_qsl(
            (scope.get("query_string", b"") or b"").decode("latin-1"),
            keep_blank_values=True,
        )
    except Exception:
        return []
    return [value for key, value in pairs if key == DIRECTORY_QUERY_PARAM]


def _directory_header_value(scope: Scope) -> str | None:
    """First ``X-Opencode-Directory`` header value (raw, as sent).

    PRESENCE-based (M3-1 / §5.7 terminal): the value is returned even
    when empty or whitespace-only — header presence alone is retired
    input on the consuming set; blank values are no escape hatch.
    """
    for name, value in scope.get("headers") or []:
        if name.lower() == DIRECTORY_HEADER_NAME.encode("ascii"):
            return value.decode("latin-1")
    return None


def _is_stream_path(normalized_path: str) -> bool:
    return normalized_path.endswith("/stream")


def _segment_key_in(key_raw: str, keys: frozenset[str]) -> bool:
    """True iff a raw query segment key decodes to one of ``keys``.

    Mirrors the ``parse_qsl`` decoding used for the selector judgement so the
    two can never disagree (``%76=3`` is judged as ``v=3``, so it must be
    consumed as ``v`` too); lookalike keys (``vv``/``av``/``V``) stay.
    """
    try:
        return unquote_plus(key_raw) in keys
    except Exception:
        return False


def _strip_query_keys(query_string: bytes, keys: frozenset[str]) -> bytes:
    """§5.2: remove every parameter pair whose key is in ``keys``.

    All other segments stay **byte-identical** — encoding (``%20``/``+``),
    order, repeats, empty segments and trailing separators are preserved by
    scanning ``&``-separated segments and rejoining the survivors verbatim
    (no ``urlencode`` rebuild — that would re-canonicalise encodings).
    """
    if not query_string:
        return query_string
    text = query_string.decode("latin-1")
    kept = [
        segment
        for segment in text.split("&")
        if not _segment_key_in(segment.split("=", 1)[0], keys)
    ]
    return "&".join(kept).encode("latin-1")


def _strip_v_segments(query_string: bytes) -> bytes:
    """§5.2: remove every ``v`` parameter pair from the raw query string."""
    return _strip_query_keys(query_string, frozenset({VERSION_QUERY_PARAM}))


class SlimapiSelectorMiddleware:
    """v4-contract §2 selector (2026-08-21 narrowing): ``?v=4`` is the
    only admitted wire version; every other ``/slimapi/**`` version form
    (including ``?v=3``) is a 400 ``unsupported_version``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if not _is_slimapi_path(path):
            # Catch-all: zero-touch. Only the §9.1 observability mark.
            _stash(scope, SELECTOR_NOT_APPLICABLE, None)
            state = scope.setdefault("state", {})
            if isinstance(state, dict) and DIRECTORY_FORM_STATE_KEY not in state:
                state[DIRECTORY_FORM_STATE_KEY] = None
            await self.app(scope, receive, send)
            return

        # Directory form is computed for every /slimapi request (even ones the
        # selector then rejects) so the access log can attribute it (§9.1).
        directory_form = _directory_form(scope)
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state[DIRECTORY_FORM_STATE_KEY] = directory_form

        normalized = _normalize_path(path)
        if normalized == VERSIONS_PATH:
            # 405 priority (§8.3 ①): non-GET → 405 + Allow: GET, BEFORE any
            # selector decision.
            if (scope.get("method", "") or "").upper() != "GET":
                _stash(scope, SELECTOR_REJECTED, None)
                accept_encoding = _header(scope, "accept-encoding")
                await json_response(
                    {"code": "method_not_allowed"},
                    status_code=405,
                    headers={"Allow": "GET"},
                    accept_encoding=accept_encoding,
                )(scope, receive, send)
                return
            # GET /slimapi/versions — unconditional exemption (§3).
            _stash(scope, SELECTOR_EXEMPT, None)
            await self._forward(scope, receive, send)
            return

        values = _collect_v_values(scope)
        if not values:
            # §2 退役后: no selector at all is a retired-version request —
            # the endpoint exists; answer the version error, never a 404.
            await self._reject_version(scope, receive, send)
            return

        if any(_SELECTOR_LEXICAL_RE.fullmatch(v) is None for v in values) or len(set(values)) != 1:
            _stash(scope, SELECTOR_REJECTED, None)
            await json_response(
                {"code": "invalid_version_selector"},
                status_code=400,
                accept_encoding=_header(scope, "accept-encoding"),
            )(scope, receive, send)
            return

        if int(values[0]) not in SUPPORTED_WIRE_VERSIONS:
            # §2: lexically-valid values outside {3, 4} are unsupported —
            # including the header-based v2 era (which never reaches here:
            # the header is not read).
            await self._reject_version(scope, receive, send)
            return

        # Admitted: mark the wire view (§2 request-scope wireVersion — under
        # the v4-only (4, 4) window only SELECTOR_V4 can be stashed here;
        # routes/health read it back via wire_view_from_scope) and run the
        # version-forked directory consumption (§5.1/§5.2/§8.3 ③).
        # (Historical: the 4.0.0 (3, 4) dual window stashed SELECTOR_V3
        # here for ?v=3 — removed by the 2026-08-21 narrowing.)
        wire = values[0]
        _stash(scope, SELECTOR_V4, wire)

        # §8.3/§16.1: the method 405 slots between ② (the version-family
        # 400s above) and ③ (directory consumption below). v4 face +
        # two-condition conjunction live (boundary∈SATISFIED ∧
        # post-actions∉SATISFIED) + one of the three deferred POST combos
        # → coded 405 with the frozen body/Allow literals; zero upstream IO
        # (the combo is deferred, never forwarded). With post-actions
        # satisfied this whole block steps aside (§16.3 activation) and
        # the request flows to the route registry. The admitted stash
        # above stays — the selector itself succeeded and §9.1 records the
        # truthful selectorResult=v4; the 405 is a method-level boundary,
        # not a selector rejection.
        if wire == "4" and _v4_method_boundary_405_live():
            method = (scope.get("method", "") or "").upper()
            allow = _method_boundary_allow(normalized, method)
            if allow is not None:
                await json_response(
                    {
                        "code": "method_not_applicable",
                        "method": method,
                        "allow": list(allow),
                    },
                    status_code=405,
                    headers={
                        "Allow": ", ".join(allow),
                        "Cache-Control": "no-store",
                    },
                    accept_encoding=_header(scope, "accept-encoding"),
                )(scope, receive, send)
                return

        error = self._consume_directory(scope, normalized)
        if error is not None:
            _stash(scope, SELECTOR_REJECTED, None)
            await json_response(
                error,
                status_code=400,
                accept_encoding=_header(scope, "accept-encoding"),
            )(scope, receive, send)
            return
        await self._forward(scope, receive, send)

    async def _reject_version(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """400 ``unsupported_version`` with the admitted set ([4])."""
        _stash(scope, SELECTOR_REJECTED, None)
        await json_response(
            {
                "code": "unsupported_version",
                "supported": list(SUPPORTED_WIRE_VERSIONS),
            },
            status_code=400,
            accept_encoding=_header(scope, "accept-encoding"),
        )(scope, receive, send)

    def _consume_directory(
        self, scope: Scope, normalized_path: str,
    ) -> dict[str, Any] | None:
        """§5.1/§5.2/§8.3 ③ directory consumption for an admitted request.

        Returns ``None`` on success (including "nothing to consume" and
        every tolerant route), or an error-body dict for a 400.

        **v4 fork (checked FIRST — the retirement error outranks the whole
        v3 validation ladder)**: on a retired route (global sessions list)
        ANY directory input — query key present (single/multi/blank) or
        header present in any form — is a uniform 400
        ``directory_retired_in_v4``; without any directory input the route
        forwards untouched (global facade). Priority chain stays intact:
        the version-family 400s above have already run before this.

        **Otherwise the v3 ladder applies verbatim** (identical for wire
        3 and wire 4 on every non-retired route):

        1. multi-value distinct (normalised) → ``invalid_directory_selector``;
        2. dual-present normalised-different → ``directory_conflict``;
        3. header present otherwise (header-only / dual-present same) →
           ``directory_header_retired``;
        4. query-only single value → consume: validate, stash under
           :data:`V3_DIRECTORY_STATE_KEY`, strip every ``directory`` pair
           from the downstream query (byte-preserving, same scan as the
           ``v`` strip).

        The stream route (§5.6) differs ONLY in case 4: query-only is an
        accepted no-op (no stash, no strip — the query flows to the route
        verbatim). Header-form errors (cases 2/3) apply to it unchanged.
        """
        if _is_v4_directory_retired(normalized_path):
            if _has_directory_query_pair(
                scope.get("query_string", b"") or b""
            ) or _has_directory_header(scope):
                return dict(_DIRECTORY_RETIRED_IN_V4_BODY)
            return None
        if not _directory_consuming_for(normalized_path):
            # §5.5 tolerant-ignore set: any form accepted, no consumption,
            # no strip, no error.
            return None
        values = _collect_directory_values(scope)
        distinct = {normalize_directory(v) for v in values}
        if len(distinct) > 1:
            return {"code": "invalid_directory_selector"}
        header_dir = _directory_header_value(scope)
        if values:
            raw = values[0]
            try:
                resolved = validate_directory(raw)
            except CodedHTTPException as exc:
                return {"code": exc.code, **exc.fields}
            if header_dir is not None:
                if normalize_directory(header_dir) != resolved:
                    return {
                        "code": "directory_conflict",
                        "queryDirectory": raw,
                        "headerDirectory": header_dir,
                    }
                # Dual-present normalised-same: the header is still retired
                # input (§5.7) — presence alone is the error.
                return {"code": "directory_header_retired"}
            if _is_stream_path(normalized_path):
                # §5.6: query-only single value on stream = accepted no-op.
                return None
        elif header_dir is not None:
            # Header-only: retired (§5.7) — ?directory= is the only channel.
            return {"code": "directory_header_retired"}
        else:
            return None
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state[V3_DIRECTORY_STATE_KEY] = resolved
        # Strip AFTER the form was recorded (§9.1 directoryForm observes
        # the client-sent query) and only on the success path — a 400
        # request never forwards, so its query bytes are irrelevant.
        scope["query_string"] = _strip_query_keys(
            scope.get("query_string", b"") or b"",
            frozenset({DIRECTORY_QUERY_PARAM}),
        )
        return None

    async def _forward(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward a /slimapi/** request downstream after consuming `v`.

        §2/§5.2: `v` is a sidecar-reserved parameter — every parameter pair
        whose key is `v` is stripped from ``scope["query_string"]`` before
        the request reaches the route, and every other segment keeps its
        original bytes. Rejected (400) and 405 paths never forward, so they
        never reach this. The catch-all (non-/slimapi) paths never pass here
        either — their query stays untouched.
        """
        scope["query_string"] = _strip_v_segments(
            scope.get("query_string", b"") or b""
        )
        await self.app(scope, receive, send)


def _header(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.decode("latin-1").lower() == name:
            return value.decode("latin-1")
    return None
