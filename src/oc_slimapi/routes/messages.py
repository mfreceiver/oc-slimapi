from __future__ import annotations

import gzip
import re
from urllib.parse import urlparse

import orjson
import httpx
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ..errors import CodedHTTPException
from ..gzip_util import error_response
from ..skeleton import skeleton_messages
from ..traffic import stash_up_in
from ..transform import (
    TransformBusy,
    read_with_cap,
    strip_diagnostics_and_pack,
)
from ..upstream import (
    forward_directory_headers,
)
from ..directory import validate_directory

router = APIRouter(prefix="/slimapi/messages/{sid}", tags=["messages"])

# Fixed Retry-After for transform admission timeouts. Kept as a module constant
# so tests and the route agree on the wire contract.
TRANSFORM_RETRY_AFTER_SECONDS = 2


# ---------------------------------------------------------------------------
# lite-v2 §8 — skeleton list ordering contract.
# ---------------------------------------------------------------------------
#
# The list endpoint MUST return messages sorted by ``info.time.created`` ASC.
# Sidecar sorts defensively after parse and before skeleton projection — it
# does NOT rely on upstream opencode's default ordering. The contract holds
# even if opencode's ``orderBy`` ever changes; clients merging paginated
# skeleton pages depend on the strict-ASC guarantee.

def _created_sort_key(msg: dict) -> int:
    """Sort key: ``info.time.created`` ASC.

    Defaults to ``0`` for missing / malformed fields so degenerate upstream
    rows sort first under Python's stable sort instead of crashing the worker.
    """
    info = msg.get("info") if isinstance(msg, dict) else None
    if not isinstance(info, dict):
        return 0
    time_obj = info.get("time")
    if not isinstance(time_obj, dict):
        return 0
    raw = time_obj.get("created")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return 0


def _project_list_sorted_and_pack(
    body: bytes, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entry: parse + sort by ``info.time.created`` ASC + skeleton
    project + serialize (+ optional gzip).

    lite-v2 §8: skeleton list endpoint must return messages sorted by
    ``info.time.created`` ASC. Sort defensively rather than relying on
    upstream opencode's default ordering. Mirrors ``transform._pack_json``
    inline (kept private to that module) so this stays self-contained.
    """
    parsed = orjson.loads(body)
    if not isinstance(parsed, list) or not all(
        isinstance(m, dict) for m in parsed
    ):
        # Mirrors sessions.py non-list guard: a non-list body (dict/null)
        # OR a list with non-dict elements (scalar-list like [1,2,"x"])
        # would make skeleton_messages / _created_sort_key call .get() on
        # the wrong type → AttributeError. Treat as malformed upstream →
        # route maps to 503.
        raise ValueError("upstream message body is not a list of message dicts")
    parsed.sort(key=_created_sort_key)
    projected = skeleton_messages(parsed)
    encoded = orjson.dumps(projected)
    headers: dict[str, str] = {"Vary": "Accept-Encoding"}
    if "gzip" in (accept_encoding or "").lower():
        encoded = gzip.compress(encoded, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return encoded, headers


_REL_PARAM_RE = re.compile(
    # Match the ``rel`` link-param anywhere in a Link entry's attribute
    # string. Pre-anchor on start/whitespace/``;`` so we don't false-match
    # a ``rel=`` substring tucked inside another param's quoted value
    # (e.g. ``title="rel=next"``). IGNORECASE covers the param NAME;
    # value tokens are lowercased by the caller per RFC 5988 §3.
    r'(?:^|[\s;])rel\s*=\s*(?:"([^"]*)"|([^;\s]+))',
    re.IGNORECASE,
)


def _link_rel_tokens(attrs: str) -> list[str]:
    """Extract the ``rel`` link-param value as a list of lowercased tokens.

    RFC 5988 §3 allows multiple whitespace-separated relation types
    (``rel="prev next"``) and treats relation types as case-insensitive.
    Returns ``[]`` when ``rel`` is absent or has no value.
    """
    match = _REL_PARAM_RE.search(attrs)
    if not match:
        return []
    raw = match.group(1) if match.group(1) is not None else match.group(2)
    return [tok.lower() for tok in raw.split() if tok]


def _extract_before_verbatim(query: str) -> str | None:
    """Return the raw ``before=<value>`` substring from a URL query string
    WITHOUT any percent-decoding or ``+``→space substitution.

    opencode's cursor is an opaque base64url JSON envelope that the upstream
    ``cursor.decode`` consumes byte-for-byte. ``parse_qs`` / ``unquote_plus``
    would transform ``%2B``→``+`` and ``+``→space, silently corrupting the
    round-trip — the value might happen to look unchanged on the base64url
    charset today, but that would be luck, not design. We MUST return the
    original substring as it appeared on the wire.

    Returns None when the param is absent OR present without a value
    (``?before`` with no ``=``) — both are treated as "no cursor" so the
    caller fails safe (no X-Next-Cursor emitted).
    """
    if not query:
        return None
    for part in query.split("&"):
        if part == "before":
            # ``?before`` with no value → treat as absent (fail-safe).
            return None
        if part.startswith("before="):
            # Slice after ``before=`` to end of this ``&``-segment. Any
            # percent-escapes / ``+`` are preserved verbatim.
            return part[len("before="):]
    return None


def _parse_link_next_cursor(link_header: str | None) -> str | None:
    """RFC 5988 ``Link`` parser — extract the ``before`` query param from the
    ``rel="next"`` URL. Returns the opaque cursor string verbatim, or None
    when no next-page link is present.

    opencode advertises pagination via
    ``Link: <...?before=<opaque>&limit=N>; rel="next"`` and recognises the
    same opaque cursor as ``?before`` on the next request. We surface that
    cursor verbatim as our own ``X-Next-Cursor`` response header so clients
    never need to know opencode's cursor format — and crucially, never
    substitute a bare messageID (opencode rejects those with 400 because
    the cursor is a base64url JSON envelope, not a raw id).

    The ``before`` value is sliced out of the raw query string via
    :func:`_extract_before_verbatim` — never ``parse_qs`` / ``unquote``,
    which would percent-decode and corrupt the opaque token.

    ``rel`` matching follows RFC 5988 §3: case-insensitive on both the param
    name and the relation-type tokens, and supports multi-token values
    (``rel="prev next"`` is recognised as a next link). Entries are still
    split on ``,`` — safe because opencode's base64url cursors do not
    contain commas.
    """
    if not link_header:
        return None
    for raw in link_header.split(","):
        segment = raw.strip()
        if not segment.startswith("<"):
            continue
        end = segment.find(">")
        if end < 0:
            continue
        url = segment[1:end]
        attrs = segment[end + 1:]
        # RFC 5988 §3: relation types are case-insensitive tokens. Match
        # any entry whose ``rel`` value contains ``next`` as one of its
        # whitespace-separated tokens — handles ``rel="next"``,
        # ``rel="prev next"``, ``rel=next``, ``REL="Next"`` uniformly.
        if "next" not in _link_rel_tokens(attrs):
            continue
        # urlparse splits structurally without decoding the query — safe.
        cursor = _extract_before_verbatim(urlparse(url).query)
        if cursor is not None:
            return cursor
    return None


def _busy_response(accept_encoding: str | None = None) -> Response:
    """503 + ``Retry-After`` — emitted when the transform pool admission
    times out.

    Routed through :func:`error_response` so the body honours gzip
    negotiation (contract §9) when the client sent ``Accept-Encoding: gzip``.
    ``error_response`` sets ``Vary: Accept-Encoding`` (and Content-Encoding
    when gzip is negotiated); ``Retry-After`` is appended afterward because
    it is a transport header, not a body field.
    """
    response = error_response(
        "transform_busy", 503,
        accept_encoding=accept_encoding,
        retry_after=TRANSFORM_RETRY_AFTER_SECONDS,
    )
    response.headers["Retry-After"] = str(TRANSFORM_RETRY_AFTER_SECONDS)
    return response


async def _resolve_messages_directory(request: Request, directory: str | None) -> str | None:
    """Resolve query ``directory`` to a normalised value to forward upstream.

    slimapi no longer gates directories — any directory is forwarded to
    upstream opencode (which decides whether it can serve it). The two
    structural checks below are kept:

    - ``directory is None`` → not blocked (returns None; upstream default applies).
      v1 only trusts query ``directory``; a lone ``X-Opencode-Directory`` header
      is not validated and not forwarded (unchanged behaviour).
    - query present AND header present AND they differ → 400 ``directory_not_allowed``
      (defensive: the conflict is structurally ambiguous, regardless of which
      directories are involved — slimapi refuses to guess which one to forward).

    Returns the normalised directory to forward (or None).
    """
    if directory is None:
        return None
    header_dir = request.headers.get("x-opencode-directory")
    if header_dir:  # treat empty header as absent
        if (header_dir.rstrip("/") or "/") != (directory.rstrip("/") or "/"):
            raise CodedHTTPException(400, code="directory_not_allowed")
    return validate_directory(directory)


async def _stream_upstream(
    request: Request, path: str, params: dict, directory: str | None,
):
    """Build & send a streaming GET so we can cap-read the body instead of buffering.

    Caller MUST ``await response.aclose()`` — typically inside a ``finally``
    block — to release the underlying httpx connection.
    """
    upstream_request = request.app.state.upstream.build_request(
        "GET", path, params=params,
        headers=forward_directory_headers(directory),
    )
    try:
        return await request.app.state.upstream.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc


@router.get("")
async def messages(
    request: Request,
    sid: str,
    limit: int = Query(40, ge=1, le=200),
    before: str | None = None,
    directory: str | None = None,
):
    """Skeleton projection of upstream opencode's message listing.

    lite-v2 §2: ``?mode=full`` list branch removed; only skeleton projection
    remains. ``?mode`` query parameter is silently ignored (clients in
    transition may still send it). ``?limit=`` and ``?before=`` pagination
    are preserved (cursor forwarded verbatim).

    lite-v2 §8: response is sorted by ``info.time.created`` ASC — see
    ``_project_list_sorted_and_pack``.
    """
    directory = await _resolve_messages_directory(request, directory)
    params = {"limit": limit}
    if before:
        params["before"] = before
    config = request.app.state.config
    pool = request.app.state.transforms
    try:
        # Admission BEFORE the upstream GET: this is the key fix. The prior
        # code buffered the entire upstream body and only then tried to
        # acquire the semaphore, so N concurrent clients could each buffer a
        # large body before any of them failed admission.
        async with pool:
            response = await _stream_upstream(
                request, f"/session/{sid}/message", params, directory,
            )
            next_cursor: str | None = None
            try:
                if response.status_code >= 400:
                    # Drain upstream error body for connection reuse.
                    body = await response.aread()
                    stash_up_in(request, len(body))
                    # Contract §7: map upstream errors to structured codes.
                    if response.status_code == 404:
                        raise CodedHTTPException(
                            404, code="session_not_found", sessionID=sid,
                        )
                    if response.status_code < 500:
                        raise CodedHTTPException(
                            502, code=f"upstream_http_{response.status_code}",
                        )
                    raise CodedHTTPException(503, code="upstream_unavailable")
                body, n_read = await read_with_cap(response, config.max_response_bytes)
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        limit=config.max_response_bytes,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
                # Traffic accounting: skeleton-mode upstream bytes.
                stash_up_in(request, n_read)
                # Translate opencode's Link header into our X-Next-Cursor
                # (opaque passthrough). Capture BEFORE aclose() for clarity,
                # though httpx headers remain readable afterward. Never
                # leak the upstream Link header itself — the sidecar's
                # pagination contract is X-Next-Cursor, so clients see one
                # consistent shape whether they consume list or since.
                next_cursor = _parse_link_next_cursor(response.headers.get("Link"))
                # Full parse + sort (§8) + project + serialize/gzip chain in
                # the worker pool.
                try:
                    encoded, extra = await pool.offload(
                        _project_list_sorted_and_pack, body,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
            finally:
                await response.aclose()
        base_headers: dict[str, str] = {"Cache-Control": "no-store"}
        if next_cursor:
            base_headers["X-Next-Cursor"] = next_cursor
        return Response(
            encoded, status_code=200, media_type="application/json",
            headers={**base_headers, **extra},
        )
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))


@router.get("/full/{mid}")
async def message(
    request: Request, sid: str, mid: str,
    directory: str | None = None,
):
    """Single-message on-demand expand — full projection (strip LSP
    diagnostics only) of one upstream opencode message.

    lite-v2 §2: downgraded to a pure on-demand expand endpoint.
    - No 304 short-circuit (removed: ``?known.*`` Query params, fingerprint
      cache lookup, ``None, status_code=304`` path).
    - No ``X-Message-Event-Seq`` response header (removed: ``seq_pre`` /
      ``seq_post`` double-sampling logic).
    - Always returns 200 on success (no cache-validation path).
    - ``mode`` parameter removed; behaviour is hard-coded to full projection
      (clients sending ``?mode=...`` are silently tolerated — the param is
      ignored). ``?known.*`` query params from prior callers are likewise
      ignored rather than rejected, so a client in transition does not see
      a 422.
    """
    directory = await _resolve_messages_directory(request, directory)
    config = request.app.state.config
    pool = request.app.state.transforms
    accept_encoding = request.headers.get("accept-encoding")
    try:
        # G8: stream + cap-read so a single oversized upstream body cannot
        # spike sidecar RSS (MemoryMax=384M). Cap metric = decompressed
        # logical bytes (httpx auto-decompresses). Aborting the read early
        # requires closing the upstream response — done in the finally. The
        # body is buffered + parsed to strip the never-consumed LSP
        # ``state.metadata.diagnostics`` map (ocdroid deletes it on
        # deserialise); every other field is preserved. The strip runs
        # off-thread under admission acquired before the upstream GET so the
        # event loop stays free and saturation surfaces as 503 transform_busy.
        async with pool:
            upstream_request = request.app.state.upstream.build_request(
                "GET", f"/session/{sid}/message/{mid}",
                headers=forward_directory_headers(directory),
            )
            try:
                response = await request.app.state.upstream.send(upstream_request, stream=True)
            except httpx.RequestError as exc:
                raise CodedHTTPException(503, code="upstream_unavailable") from exc
            status_code = 200
            try:
                # Wrap mid-stream upstream I/O failures (httpx.RequestError
                # raised by _drain_error.aread() or read_with_cap
                # .aiter_bytes()) into a structured 503 instead of bubbling
                # up as an unhandled FastAPI 500. The finally below still
                # runs to release the connection.
                try:
                    if response.status_code >= 400:
                        # Drain upstream error body for connection reuse.
                        body = await response.aread()
                        stash_up_in(request, len(body))
                        # Contract §7: map upstream errors to structured codes.
                        if response.status_code == 404:
                            raise CodedHTTPException(
                                404, code="session_not_found", sessionID=sid,
                            )
                        if response.status_code < 500:
                            raise CodedHTTPException(
                                502, code=f"upstream_http_{response.status_code}",
                            )
                        raise CodedHTTPException(503, code="upstream_unavailable")
                    # Contract §2: /full/{mid} always returns 200 on success.
                    status_code = 200
                    body, n_read = await read_with_cap(
                        response, config.max_message_bytes,
                    )
                except httpx.RequestError as exc:
                    raise CodedHTTPException(503, code="upstream_unavailable") from exc
                # Traffic accounting: cap-read upstream bytes (counted even
                # on cap-bail, matching the list convention).
                stash_up_in(request, n_read)
                if body is None:
                    return error_response(
                        "message_too_large", 413,
                        limitBytes=config.max_message_bytes,
                        accept_encoding=accept_encoding,
                    )
                # Empty / non-JSON upstream 200 → 503 upstream_unavailable
                # (same code as sessions bad-JSON), never a bare 500.
                try:
                    encoded, extra = await pool.offload(
                        strip_diagnostics_and_pack, body,
                        accept_encoding=accept_encoding,
                    )
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
            finally:
                await response.aclose()
        return Response(
            encoded, status_code=status_code, media_type="application/json",
            headers={"Cache-Control": "no-store", **extra},
        )
    except TransformBusy:
        return _busy_response(accept_encoding)
