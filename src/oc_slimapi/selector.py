"""v3 wire-contract version selector (v3-contract §2, Batch A).

A pure-ASGI dispatch layer that decides — for ``/slimapi/**`` requests only —
which wire-contract pipeline a request runs:

* **no ``?v=``** → the current v2 pipeline, INCLUDING the
  :class:`~oc_slimapi.versioning.SlimapiVersionMiddleware` header gate
  (missing/out-of-range ``X-Slimapi-Version`` → the same 400s as before).
* **``?v=2``** (explicit) → identical v2 pipeline (header gate still applies).
* **``?v=3``** → the request is marked v3 (``request.state`` via ASGI scope
  state) and **bypasses the header gate**; a simultaneously-present version
  header is ignored (no error).
* **consumption (§5.2)**: once judged, every ``v`` parameter pair is
  **stripped from the downstream query string** on ALL forwarded
  ``/slimapi/**`` requests (v2/v3/exempt/rollback alike) — ``v`` is a
  sidecar-reserved parameter and is never seen by a route or forwarded
  upstream. Remaining parameters keep their original bytes (encoding /
  order / repeats verbatim).
* **lexically valid but unsupported** (``1``, ``4``, ``5``, …) → 400
  ``{"code":"unsupported_version","supported":[2,3]}``.
* **lexically invalid** (``0``, ``03``, ``+3``, `` 3``, ``3.0``, empty, …) or
  **conflicting multi-value** (``?v=3&v=2``) → 400
  ``{"code":"invalid_version_selector"}``; same-value repeats (``?v=3&v=3``)
  fold to one.
* ``GET /slimapi/versions`` (slash-normalised) is **unconditionally exempt**:
  it never passes the selector NOR the header gate. Non-GET on that path →
  ``405`` + ``Allow: GET`` — priority above everything (checked before any
  selector/gate decision).
* **catch-all (non ``/slimapi``) requests are untouched** — zero ``v`` /
  directory consumption, byte-identical passthrough (proxy.py stays frozen).

The middleware owns an inner :class:`SlimapiVersionMiddleware` instance
wrapping the same app, so the existing gate code is reused **unmodified**:
v2/absent requests are routed through ``self.gate``; v3/exempt requests call
the inner app directly, bypassing the gate.

Observability (§9.1): the selector stashes ``selectorResult``
(absent|v2|v3|rejected|exempt|not_applicable), ``wireVersion`` ("2"|"3"|None)
and ``directoryForm`` (query|header|both|absent|None) into ``scope["state"]``
under :data:`SELECTOR_STATE_KEY` / :data:`DIRECTORY_FORM_STATE_KEY` where the
traffic-accounting middleware (which wraps this one) and the health routes
read them at request end. A non-``/slimapi`` request is stashed
``not_applicable`` (directory form ``None`` — the catch-all is not in the §5.3
directory-consuming set).

Rollback (config ``v3_selector_enabled=false``): the selector stops judging
``v`` entirely — ``?v=3`` runs the v2 pipeline, observability records
``absent``, and even lexically invalid values are ignored (full rollback to
pre-selector behaviour on the ``v`` axis).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote_plus

from starlette.types import ASGIApp, Receive, Scope, Send

from .directory import normalize_directory, validate_directory
from .errors import CodedHTTPException
from .gzip_util import json_response
from .versioning import (
    ACCEPTED_CLIENT_VERSIONS,
    SlimapiVersionMiddleware,
    _is_slimapi_path,
)

# Stable scope-state keys (read by traffic accounting + health routes).
SELECTOR_STATE_KEY = "slimapi_selector"
DIRECTORY_FORM_STATE_KEY = "slimapi_directory_form"

# §9.1 selectorResult enum.
SELECTOR_ABSENT = "absent"
SELECTOR_V2 = "v2"
SELECTOR_V3 = "v3"
SELECTOR_REJECTED = "rejected"
SELECTOR_EXEMPT = "exempt"
SELECTOR_NOT_APPLICABLE = "not_applicable"

# §9.1 sseActive dims (§9.2): rejected/exempt have no SSE endpoints.
SSE_RESULT_DIMS = ("v2", "v3", "absent", "not_applicable")

VERSION_QUERY_PARAM = "v"
VERSIONS_PATH = "/slimapi/versions"

# §5 directory consumption (Batch B). The canonical v3 directory input is
# the ``?directory=`` query parameter; the ``X-Opencode-Directory`` header
# is accepted in parallel and only ever cross-checked (§5.4).
DIRECTORY_QUERY_PARAM = "directory"
DIRECTORY_HEADER_NAME = "x-opencode-directory"

# Scope-state key: set ONLY when a v3 request on a §5.3 consuming (non-stream)
# route actually supplied ``?directory=`` values — the value is the validated
# resolved directory (consume succeeded). Routes read it via
# :func:`resolve_route_directory` instead of the (now stripped) query param.
V3_DIRECTORY_STATE_KEY = "slimapi_v3_directory"

# §2 lexical rule: ASCII digits, no leading zero, at least one digit.
_SELECTOR_LEXICAL_RE = re.compile(r"^[1-9][0-9]*$")

# Slash-collapse for the /versions exemption + consuming-set match (P1-14
# parity: routing still sees the raw path; only these decisions normalise).
_SLASH_RE = re.compile(r"/+")

# §5.3 directory-consuming set (Batch A subset — §10 routes join in Batch C).
# NOTE: ``/slimapi/sessions/{sid}/stream`` is included because its §5.6 guard
# consumes the ``directory`` query (structural conflict check), even though
# the directory is a fanout no-op.
_DIRECTORY_CONSUMING_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"^/slimapi/messages/[^/]+$",
        r"^/slimapi/messages/[^/]+/full/[^/]+$",
        r"^/slimapi/sessions$",
        r"^/slimapi/sessions/status$",
        r"^/slimapi/sessions/[^/]+/todo$",
        r"^/slimapi/sessions/[^/]+/children$",
        r"^/slimapi/sessions/[^/]+/diff$",
        r"^/slimapi/sessions/[^/]+/stream$",
        r"^/slimapi/agent$",
        r"^/slimapi/command$",
        # §10.a read groups (v3 Batch C1) — directory-sensitive per group
        # definitions: file (FileQuery/WorkspaceRoutingQuery), vcs
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
        # §10.b write routes (v3 Batch C2) — ALL 12 consume directory
        # (upstream groups/session.ts:203-397 + groups/question.ts:32-48
        # declare WorkspaceRoutingQuery on every write endpoint). The
        # PATCH/DELETE /session/{id} pattern is shared with the §10.a
        # session-single GET above (already present).
        r"^/slimapi/session$",
        r"^/slimapi/session/[^/]+/(prompt_async|abort|summarize|fork|revert|command)$",
        r"^/slimapi/session/[^/]+/permissions/[^/]+$",
        r"^/slimapi/question/[^/]+/(reply|reject)$",
    )
)


def _normalize_path(path: str) -> str:
    return _SLASH_RE.sub("/", path)


def _is_directory_consuming(normalized_path: str) -> bool:
    return any(p.match(normalized_path) is not None for p in _DIRECTORY_CONSUMING_PATTERNS)


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
    for name, value in scope.get("headers") or []:
        if name.lower() == b"x-opencode-directory" and value.strip():
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
    """§3a: the wire view this request runs — 3 for v3-selector requests,
    2 otherwise (absent/v2/rejected/exempt/no-selector legacy stacks)."""
    info = selector_info_from_scope(scope)
    return 3 if info.get("result") == SELECTOR_V3 else 2


def resolve_route_directory(scope: Scope, query_value: str | None) -> str | None:
    """Final directory for a §5.3 consuming route (Batch B).

    * **v3 with consumed ``?directory=``** — the dispatch layer already
      validated + resolved the query values and stripped them from the
      downstream query; the stash replaces the (now-absent) FastAPI query
      param. The stashed value is already validated — routes may re-run
      ``validate_directory`` on it (idempotent, pure).
    * **everything else** (v2, v3-without-directory, no-selector test
      stacks) — the caller's FastAPI-bound query value, unchanged.
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
    """First non-blank ``X-Opencode-Directory`` header value (raw, as sent)."""
    for name, value in scope.get("headers") or []:
        if name.lower() == DIRECTORY_HEADER_NAME.encode("ascii"):
            decoded = value.decode("latin-1")
            if decoded.strip():
                return decoded
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
    """v3-contract §2 selector — dispatches /slimapi/** to the v2 gate or the
    v3-marked path. Owns the (unmodified) version gate as an inner app."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        accepted_client_versions: tuple[int, int] = ACCEPTED_CLIENT_VERSIONS,
        v3_enabled: bool = True,
    ) -> None:
        self.app = app
        self.v3_enabled = v3_enabled
        self.gate = SlimapiVersionMiddleware(
            app, accepted_client_versions=accepted_client_versions
        )

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
            # 405 priority: non-GET → 405 + Allow: GET, BEFORE any selector
            # or gate decision.
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
            # GET /slimapi/versions — unconditional exemption (no selector,
            # no header gate).
            _stash(scope, SELECTOR_EXEMPT, None)
            await self._forward(scope, receive, send, via_gate=False)
            return

        if not self.v3_enabled:
            # Rollback: `v` ignored entirely → v2 pipeline, observability
            # records absent (§A-5). §5.2 stripping still applies — the
            # rollback pipeline is a v2 request, and v2 requests strip `v`.
            _stash(scope, SELECTOR_ABSENT, "2")
            await self._forward(scope, receive, send, via_gate=True)
            return

        values = _collect_v_values(scope)
        if not values:
            _stash(scope, SELECTOR_ABSENT, "2")
            await self._forward(scope, receive, send, via_gate=True)
            return

        if any(_SELECTOR_LEXICAL_RE.fullmatch(v) is None for v in values) or len(set(values)) != 1:
            _stash(scope, SELECTOR_REJECTED, None)
            await json_response(
                {"code": "invalid_version_selector"},
                status_code=400,
                accept_encoding=_header(scope, "accept-encoding"),
            )(scope, receive, send)
            return

        requested = int(values[0])
        minimum, maximum = self.gate.accepted
        if not minimum <= requested <= maximum:
            _stash(scope, SELECTOR_REJECTED, None)
            await json_response(
                {"code": "unsupported_version", "supported": [minimum, maximum]},
                status_code=400,
                accept_encoding=_header(scope, "accept-encoding"),
            )(scope, receive, send)
            return

        if requested == 3:
            # v3 semantics: mark + bypass the header gate (a simultaneously
            # present version header is ignored, not an error — §2).
            _stash(scope, SELECTOR_V3, "3")
            # §5/§5.6: v3 directory consumption (consuming set only). Any
            # directory error is a dispatch-layer 400 → observability
            # records ``rejected`` (same treatment as the §2 judgement
            # errors). The stream route adds ONLY the multi-value pre-check
            # and then inherits its v2 guard verbatim (§5.6).
            error = self._consume_v3_directory(scope, normalized)
            if error is not None:
                _stash(scope, SELECTOR_REJECTED, None)
                await json_response(
                    error,
                    status_code=400,
                    accept_encoding=_header(scope, "accept-encoding"),
                )(scope, receive, send)
                return
            await self._forward(scope, receive, send, via_gate=False)
            return

        # Explicit v2 (or any non-3 supported version in a future range):
        # the full v2 pipeline including the header gate.
        _stash(scope, SELECTOR_V2, "2")
        await self._forward(scope, receive, send, via_gate=True)

    def _consume_v3_directory(
        self, scope: Scope, normalized_path: str,
    ) -> dict[str, Any] | None:
        """§5 v3 ``?directory=`` consumption for THIS request.

        Returns ``None`` on success (including "nothing to consume" and
        every non-consuming / stream path), or an error-body dict for a 400:

        * multi-value distinct (normalized) → ``invalid_directory_selector``
          (§5.6 stream pre-check included — the stream path returns after it);
        * query value invalid → the routes' ``invalid_directory`` shape;
        * dual-present normalized-different → ``directory_conflict``
          (§5.4, frozen field names).

        Success also stashes the resolved directory under
        :data:`V3_DIRECTORY_STATE_KEY` for the route (query value, or the
        compatible header when the query is empty — §5.2 consumes "query or
        compatible header") and, when query values were present on a
        NON-stream consuming route, strips every ``directory`` pair from the
        downstream query (byte-preserving, same scan as the ``v`` strip —
        §5.2). The stream route is exempt from stash+strip: its §5.6 guard
        sees the single-valued query verbatim and directory stays a no-op
        there (header-only on stream is equally a no-op, v2 semantics).
        """
        if not _is_directory_consuming(normalized_path):
            # §5.5 tolerant-ignore set + catch-all: any form accepted, no
            # consumption, no strip, no error.
            return None
        values = _collect_directory_values(scope)
        distinct = {normalize_directory(v) for v in values}
        if len(distinct) > 1:
            return {"code": "invalid_directory_selector"}
        if _is_stream_path(normalized_path):
            # §5.6: only the multi-value pre-check is v3-new on stream; the
            # single-valued query flows to the route's v2 guard verbatim.
            return None
        header_dir = _directory_header_value(scope)
        if values:
            raw = values[0]
            try:
                resolved = validate_directory(raw)
            except CodedHTTPException as exc:
                return {"code": exc.code, **exc.fields}
            if header_dir is not None and normalize_directory(header_dir) != resolved:
                return {
                    "code": "directory_conflict",
                    "queryDirectory": raw,
                    "headerDirectory": header_dir,
                }
            source = resolved
        elif header_dir is not None:
            # Header-only: consume the compatible header (validated).
            try:
                source = validate_directory(header_dir)
            except CodedHTTPException as exc:
                return {"code": exc.code, **exc.fields}
        else:
            return None
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state[V3_DIRECTORY_STATE_KEY] = source
        if values:
            # Strip AFTER the form was recorded (§9.1 directoryForm observes
            # the client-sent query) and only on the success path — a 400
            # request never forwards, so its query bytes are irrelevant.
            scope["query_string"] = _strip_query_keys(
                scope.get("query_string", b"") or b"",
                frozenset({DIRECTORY_QUERY_PARAM}),
            )
        return None

    async def _forward(
        self, scope: Scope, receive: Receive, send: Send, *, via_gate: bool
    ) -> None:
        """Forward a /slimapi/** request downstream after consuming `v`.

        §2/§5.2: `v` is a sidecar-reserved parameter — every parameter pair
        whose key is `v` is stripped from ``scope["query_string"]`` before
        the request reaches the route/gate, and every other segment keeps
        its original bytes. Rejected (400) and 405 paths never forward, so
        they never reach this. The catch-all (non-/slimapi) paths never pass
        here either — their query stays byte-identical (proxy.py frozen).
        """
        scope["query_string"] = _strip_v_segments(
            scope.get("query_string", b"") or b""
        )
        if via_gate:
            await self.gate(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _header(scope: Scope, name: str) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.decode("latin-1").lower() == name:
            return value.decode("latin-1")
    return None
