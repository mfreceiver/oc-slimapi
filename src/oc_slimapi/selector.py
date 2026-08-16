"""v3 wire-contract version selector — **terminal state** (v3-contract §2).

A pure-ASGI dispatch layer that decides — for ``/slimapi/**`` requests only —
whether a request may run the (only) v3 pipeline:

* **``?v=3``** → the request is marked v3 (ASGI scope state) and forwarded;
  a simultaneously-present ``X-Slimapi-Version`` header is **not read** and
  never an error (§1 retirement: the header is dead input).
* **no ``v`` / ``v=2`` / any other value** → 400
  ``{"code":"unsupported_version","supported":[3]}`` — the endpoint exists,
  the protocol version is retired; never a silent 404 (§2 退役后).
* **lexically invalid** (``0``, ``03``, ``+3``, `` 3``, ``3.0``, empty, …) or
  **conflicting multi-value** (``?v=3&v=2``) → 400
  ``{"code":"invalid_version_selector"}``; same-value repeats (``?v=3&v=3``)
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

Terminal directory rules (§5.7/§8.3 ③) for ``v=3`` requests on the §5.3
consuming set — evaluated in the frozen priority order:

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

The stream route (``/slimapi/sessions/{sid}/stream``) keeps its §5.6
exception for the LAST case only: a single-valued query-only directory is
accepted as a no-op (not consumed, not stripped, forwarded verbatim); the
three error cases above apply unchanged (the endpoint is in the consuming
set, so header presence retires). Tolerant (§5.5) routes never consume and
never error.

Observability (§9.1, enum frozen): the selector stashes ``selectorResult``
(v3|rejected|exempt|not_applicable — the ``absent``/``v2`` dims no longer
occur by construction), ``wireVersion`` ("3"|None) and ``directoryForm``
(query|header|both|absent|None) into ``scope["state"]`` under
:data:`SELECTOR_STATE_KEY` / :data:`DIRECTORY_FORM_STATE_KEY` where the
traffic-accounting middleware (which wraps this one) reads them at request
end. A non-``/slimapi`` request is stashed ``not_applicable``.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote_plus

from starlette.types import ASGIApp, Receive, Scope, Send

from .directory import normalize_directory, validate_directory
from .errors import CodedHTTPException
from .gzip_util import json_response
from .versioning import ACCEPTED_CLIENT_VERSIONS, _is_slimapi_path

# Stable scope-state keys (read by traffic accounting + health routes).
SELECTOR_STATE_KEY = "slimapi_selector"
DIRECTORY_FORM_STATE_KEY = "slimapi_directory_form"

# §9.1 selectorResult enum (frozen — historical values kept so old access-log
# rows and snapshot dims stay interpretable; absent/v2 are simply no longer
# produced).
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

# The single supported wire version (terminal §2/§3: available == [3]).
SUPPORTED_WIRE_VERSION = ACCEPTED_CLIENT_VERSIONS[1]

# §5.3 directory-consuming set. NOTE:
# ``/slimapi/sessions/{sid}/stream`` is included because §5.6/§5.7 give its
# directory inputs consuming-set error semantics (multi-value / dual-present
# / retired header); only its query-only happy case is a no-op.
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
    """§3a: the wire view this request runs — always 3 (v3-only terminal).

    The selector rejects every non-v3 request before routes run, so the
    only scopes reaching route code are v3; selector-less stacks (direct
    route invocation in tests) get the terminal view too. Kept as a
    function so the call sites stay explicit about where the view comes
    from.
    """
    return 3


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
    """v3-contract §2 selector — terminal state: ``?v=3`` is the only
    admitted pipeline; everything else on ``/slimapi/**`` is a 400."""

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

        if int(values[0]) != SUPPORTED_WIRE_VERSION:
            # §2 退役后: v=2 and every other lexically-valid value are
            # unsupported — including the header-based v2 era (which never
            # reaches here: the header is not read).
            await self._reject_version(scope, receive, send)
            return

        # v3 semantics: mark + directory consumption (§5.2/§5.6/§5.7/§8.3 ③).
        _stash(scope, SELECTOR_V3, "3")
        error = self._consume_v3_directory(scope, normalized)
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
        """400 ``unsupported_version`` with the terminal supported set ([3])."""
        _stash(scope, SELECTOR_REJECTED, None)
        await json_response(
            {"code": "unsupported_version", "supported": [SUPPORTED_WIRE_VERSION]},
            status_code=400,
            accept_encoding=_header(scope, "accept-encoding"),
        )(scope, receive, send)

    def _consume_v3_directory(
        self, scope: Scope, normalized_path: str,
    ) -> dict[str, Any] | None:
        """Terminal §5.7/§8.3 ③ directory consumption for a v3 request.

        Returns ``None`` on success (including "nothing to consume" and every
        tolerant route), or an error-body dict for a 400, evaluated in the
        frozen priority order:

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
        if not _is_directory_consuming(normalized_path):
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
