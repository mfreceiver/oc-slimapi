from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Literal
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime
from datetime import timezone

import orjson
import httpx
from fastapi import APIRouter, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from ..capabilities import parse_capabilities
from ..errors import CodedHTTPException
from ..gzip_util import error_response, json_response
from ..skeleton import skeleton_message
from ..traffic import stash_up_in
from ..transform import (
    TransformBusy,
    project_and_pack,
    project_messages_and_pack,
    read_with_cap,
)
from ..upstream import (
    decoded_body_headers,
    forward_directory_headers,
    strip_hop_by_hop,
)
from ..upstream_errors import fetch_json_mapped
from ..directory import validate_directory
from ..upstream_errors import raise_upstream_status as _raise_upstream_status

router = APIRouter(prefix="/slimapi/messages/{sid}", tags=["messages"])

# Fixed Retry-After for transform admission timeouts. Kept as a module constant
# so tests and the route agree on the wire contract.
TRANSFORM_RETRY_AFTER_SECONDS = 2

# G6 batch stream chunk size for the shared cumulative-byte ledger. Smaller than
# transform.read_with_cap's default (64KiB) so concurrent mids debit the budget
# at finer granularity (pay-as-you-read).
BATCH_CHUNK_SIZE = 16 * 1024


def _parse_upstream_retry_after_seconds(value: str | None) -> int | None:
    """Parse upstream Retry-After header to whole seconds (int). Returns None on
    absent/unparseable. Supports delta-seconds (int) and HTTP-date (RFC 7231).
    Never raises."""
    if not value:
        return None
    value = value.strip()
    try:
        # Try delta-seconds (integer)
        return int(value)
    except ValueError:
        pass
    # Try HTTP-date (RFC 7231)
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            # Normalize naive datetime to UTC before timestamp computation.
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now = time.time()
            return max(0, int((dt.timestamp() - now) + 0.5))  # ceil rounding
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _opt_a_top_level_503(
    request: Request, *, accept_encoding: str | None, retry_after_seconds: int | None,
) -> Response:
    """Return a 503 upstream_unavailable response with optional Retry-After for opt-in."""
    response = error_response("upstream_unavailable", 503, accept_encoding=accept_encoding)
    if retry_after_seconds is not None:
        response.headers["Retry-After"] = str(retry_after_seconds)
    return response


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


async def _drain_error(response, request: Request | None = None) -> Response:
    """Buffer a small upstream error body and pass it through verbatim.

    When ``request`` is given, the buffered body length is stashed for
    traffic accounting so even upstream error responses contribute ``upIn``
    to the request's bucket.
    """
    body = await response.aread()
    if request is not None:
        stash_up_in(request, len(body))
    return Response(
        body, response.status_code,
        headers=decoded_body_headers(response.headers),
        media_type=response.headers.get("content-type"),
    )


def _item_updated(item: dict) -> int | None:
    """Extract message watermark as ``info.time.updated or info.time.created``.

    Mirrors digest ``updatedAt`` derivation (hub.py) without the ``_now_ms()``
    fallback — filtering must compare against the message's real timestamps.
    opencode v1.18.3 message schema has no ``time.updated`` (only ``created``,
    plus ``completed`` on assistant); without the ``created`` fallback the
    A2=A ``/since/{ts}`` filter is a no-op.
    """
    info = item.get("info") or {}
    time_obj = info.get("time") or {}
    raw = time_obj.get("updated") or time_obj.get("created")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _passes_ts_filter(item: dict, ts: int) -> bool:
    """A2=A filter (contract §5): include items with watermark ``>= ts``.

    Watermark is ``info.time.updated or info.time.created`` (see
    ``_item_updated``). Items with neither (or a non-int) are included
    defensively — the client dedups by messageID anyway, and we'd rather
    over-deliver a malformed edge item than silently hide it. Only a
    definitively comparable ``watermark < ts`` excludes an item.
    """
    updated = _item_updated(item)
    if updated is None:
        return True
    return updated >= ts


@router.get("/since/{ts}")
async def messages_since(
    request: Request,
    sid: str,
    ts: int,
    limit: int = Query(50, ge=1, le=200),
    before: str | None = None,
    mode: Literal["skeleton", "full"] = "skeleton",
    directory: str | None = None,
):
    """A2=A (contract §5): return skeleton messages with ``(info.time.updated or info.time.created) >= ts``.

    Walks opencode's newest-first ``/session/{sid}/message`` listing backward
    via the ``before`` cursor (opencode's own opaque base64url cursor,
    forwarded verbatim — never decoded/re-encoded by sidecar). A single
    transform-pool admission covers the whole multi-page scan, and a single
    cumulative ``max_response_bytes`` budget is enforced across pages so a
    runaway timestamp scan cannot accumulate more than the cap of upstream
    bodies (413 ``response_too_large`` on overflow — contract §7).

    Because upstream pages are sorted newest→oldest, the scan stops at the
    first item with ``(time.updated or time.created) < ts``: every subsequent item in this page
    and any older page is also below the floor. The boundary (``== ts``) is
    included — clients dedup by messageID.

    Pagination cursor: opencode advertises more pages via the RFC 5988
    ``Link: <...?before=<opaque>; rel="next"`` response header. We extract
    that opaque cursor and surface it verbatim as our own ``X-Next-Cursor``
    response header — emitted only when we filled the client's limit AND
    never tripped the ts floor AND opencode actually advertised another page.
    """
    directory = await _resolve_messages_directory(request, directory)
    if request.app.state.schema_degraded:
        mode = "full"
    config = request.app.state.config
    pool = request.app.state.transforms
    # Single admission covers the whole multi-page scan; cumulative byte
    # budget is enforced across pages so a runaway timestamp scan cannot
    # accumulate more than ``max_response_bytes`` of upstream bodies.
    try:
        async with pool:
            collected: list[dict] = []
            cursor = before
            total_bytes = 0
            hit_ts_floor = False
            # opencode's opaque cursor from the last fetched page. Becomes
            # our X-Next-Cursor iff we end the scan without tripping the ts
            # floor and the limit is filled.
            last_page_cursor: str | None = None
            _exhausted = True
            for _ in range(config.max_since_pages):
                params = {"limit": limit}
                if cursor:
                    params["before"] = cursor
                response = await _stream_upstream(
                    request, f"/session/{sid}/message", params, directory,
                )
                try:
                    if response.status_code >= 400:
                        return await _drain_error(response, request)
                    body, n = await read_with_cap(
                        response, config.max_response_bytes - total_bytes,
                    )
                    if body is None:
                        return error_response(
                            "response_too_large", 413,
                            limit=config.max_response_bytes,
                            accept_encoding=request.headers.get("accept-encoding"),
                        )
                    total_bytes += n
                    # Traffic accounting: per-page upstream bytes.
                    stash_up_in(request, n)
                    # Per-page parse stays inline because we must inspect
                    # info.time.updated per item to apply the A2=A filter and
                    # decide whether older pages still need scanning. The
                    # expensive skeleton projection of the merged list runs
                    # off-thread below.
                    page = orjson.loads(body)
                    # opencode advertises more pages via the Link header
                    # (RFC 5988 rel="next"). Extract the opaque before cursor
                    # verbatim — never synthesise one from a messageID
                    # (opencode's cursor is a base64url JSON envelope, and
                    # passing a bare id would 400 at upstream).
                    last_page_cursor = _parse_link_next_cursor(
                        response.headers.get("Link")
                    )
                finally:
                    await response.aclose()
                if not page:
                    _exhausted = False
                    break
                page_full = False
                for item in page:
                    if _passes_ts_filter(item, ts):
                        collected.append(item)
                        if len(collected) >= limit:
                            page_full = True
                            break
                    else:
                        # Pages are newest→oldest; the first item below the
                        # ts floor means every older item (this page tail and
                        # any further page) is also below ts. Stop scanning.
                        hit_ts_floor = True
                        page_full = True
                        break
                if page_full:
                    _exhausted = False
                    break
                if not last_page_cursor:
                    _exhausted = False
                    break
                cursor = last_page_cursor
            # Emit X-Next-Cursor only when ALL of:
            #   • we filled the limit (more matching items may exist),
            #   • we never tripped the ts floor (older items would be < ts),
            #   • opencode actually advertised another page (opaque cursor).
            # The cursor is opencode's opaque string — passed through verbatim,
            # never decoded or re-encoded.
            base_headers: dict[str, str] = {"Cache-Control": "no-store"}
            if (
                collected
                and len(collected) >= limit
                and not hit_ts_floor
                and last_page_cursor
                and not _exhausted
            ):
                base_headers["X-Next-Cursor"] = last_page_cursor
            if mode == "skeleton":
                encoded, extra = await pool.offload(
                    project_messages_and_pack, collected,
                    accept_encoding=request.headers.get("accept-encoding"),
                )
                return Response(
                    encoded, status_code=200,
                    media_type="application/json",
                    headers={**base_headers, **extra},
                )
            return json_response(
                collected,
                accept_encoding=request.headers.get("accept-encoding"),
                headers=base_headers,
            )
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))


@router.get("")
async def messages(
    request: Request,
    sid: str,
    limit: int = Query(40, ge=1, le=200),
    before: str | None = None,
    mode: Literal["skeleton", "full"] = "skeleton",
    directory: str | None = None,
):
    directory = await _resolve_messages_directory(request, directory)
    if request.app.state.schema_degraded:
        mode = "full"
    params = {"limit": limit}
    if before:
        params["before"] = before
    if mode == "full":
        # Full mode is a verbatim streaming passthrough; no transform work,
        # so admission is not needed and the event loop stays free.
        upstream_request = request.app.state.upstream.build_request(
            "GET", f"/session/{sid}/message", params=params,
            headers=forward_directory_headers(directory),
        )
        try:
            response = await request.app.state.upstream.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            raise CodedHTTPException(503, code="upstream_unavailable") from exc
        # Wrap the upstream iterator so the response body bytes are counted
        # (``upIn`` for the messages bucket). ``len(chunk)`` only — body is
        # not buffered.
        async def _counted_full_messages():
            n = 0
            try:
                async for chunk in response.aiter_raw():
                    n += len(chunk)
                    yield chunk
            finally:
                if n > 0:
                    stash_up_in(request, n)

        return StreamingResponse(
            _counted_full_messages(), status_code=response.status_code,
            headers={**strip_hop_by_hop(response.headers), "Cache-Control": "no-store"},
            background=BackgroundTask(response.aclose),
        )
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
                    return await _drain_error(response, request)
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
                # Full parse/project/serialize/gzip chain in the worker pool.
                encoded, extra = await pool.offload(
                    project_and_pack, body,
                    single=False,
                    accept_encoding=request.headers.get("accept-encoding"),
                )
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


@router.get("/full")
async def message_batch(
    request: Request,
    sid: str,
    ids: str,
    mode: Literal["skeleton", "full"] = "full",
    directory: str | None = None,
):
    """G6 batch multi-mid expand (impl-spec §8). discover-first; mid-level
    partial failures into errors[]; cumulative byte budget 413 via a shared
    chunk ledger (pay-as-you-read). Registered BEFORE /full/{mid} per spec
    MUST (segment count differs so no actual collision, but order is
    spec-mandated)."""
    directory = await _resolve_messages_directory(request, directory)
    if request.app.state.schema_degraded:
        mode = "full"
    # ids parse: split + strip + dedupe保序 + 1–20 guard (no charset check)
    order = list(dict.fromkeys(s.strip() for s in ids.split(",") if s.strip()))
    if not order or len(order) > 20:
        raise CodedHTTPException(400, code="invalid_ids")

    config = request.app.state.config
    pool = request.app.state.transforms
    ledger = getattr(request.app.state, "batch_ledger", None)
    cap = parse_capabilities(request.headers.get("x-slimapi-capabilities"))
    if ledger is not None:
        ledger.record_capability_parse(conflict=cap.duplicate_conflict, malformed_tokens=cap.malformed_tokens)
    opt_in = bool(
        config.opt_a_partial_envelope_enabled
        and cap.opt_in
        and not cap.duplicate_conflict
    )
    if opt_in and ledger is not None:
        if config.opt_a_auto_rollback_enabled:
            ledger.evaluate_rollback(
                auto_enabled=True,
                min_sample=config.opt_a_rollback_min_sample,
                envelope_5xx_zero_baseline_rate=config.opt_a_rollback_envelope_5xx_zero_baseline_rate,
                unknown_code_rate_threshold=config.opt_a_rollback_unknown_code_rate,
            )
        if ledger.disabled:
            opt_in = False

    # discover 先行（带 directory 头，spec §8 L266）
    try:
        resp = await request.app.state.upstream.get(
            f"/session/{sid}", headers=forward_directory_headers(directory),
        )
    except httpx.RequestError as exc:
        if opt_in:
            accept_enc = request.headers.get("accept-encoding")
            # Intentionally not recorded to Opt-A ledger: discover is pre-envelope /
            # orthogonal to Opt-A (shared infra); rollback envelope_5xx measures
            # Opt-A-specific regressions only.
            return _opt_a_top_level_503(
                request,
                accept_encoding=accept_enc,
                retry_after_seconds=max(1, math.ceil(config.opt_a_retry_after_ms_conservative / 1000)),
            )
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    # Traffic accounting: discover response body (small, but counts toward
    # the messages bucket upIn — the discover GET is part of the batch
    # request's upstream work).
    stash_up_in(request, len(resp.content))
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            _raise_upstream_status(exc, sid=sid)
        elif exc.response.status_code >= 500:
            if opt_in:
                accept_enc = request.headers.get("accept-encoding")
                parsed_s = _parse_upstream_retry_after_seconds(
                    exc.response.headers.get("retry-after")
                )
                retry_s = (
                    max(1, parsed_s)
                    if parsed_s is not None
                    else max(1, math.ceil(config.opt_a_retry_after_ms_conservative / 1000))
                )
                # Intentionally not recorded to Opt-A ledger: discover is pre-envelope /
                # orthogonal to Opt-A (shared infra); rollback envelope_5xx measures
                # Opt-A-specific regressions only.
                return _opt_a_top_level_503(
                    request,
                    accept_encoding=accept_enc,
                    retry_after_seconds=retry_s,
                )
            _raise_upstream_status(exc, sid=sid)
        else:
            _raise_upstream_status(exc, sid=sid)
    # 200 + malformed body must not proceed to mid expand (→ 503).
    try:
        resp.json()
    except (ValueError, UnicodeDecodeError) as exc:
        if opt_in:
            accept_enc = request.headers.get("accept-encoding")
            # Intentionally not recorded to Opt-A ledger: discover is pre-envelope /
            # orthogonal to Opt-A (shared infra); rollback envelope_5xx measures
            # Opt-A-specific regressions only.
            return _opt_a_top_level_503(
                request,
                accept_encoding=accept_enc,
                retry_after_seconds=None,
            )
        raise CodedHTTPException(503, code="upstream_unavailable") from exc

    sem = asyncio.Semaphore(4)
    # Shared ledger: total is debited per decoded chunk under a no-await
    # critical section so concurrent mids cannot TOCTOU the cumulative cap.
    # network_failed and budget_exceeded are distinct: 503 beats 413.
    state = {
        "total": 0,
        "network_failed": False,
        "budget_exceeded": False,
        "network_mids": set(),
    }
    succeeded: dict[str, dict] = {}
    errors: list[dict] = []

    def _aborted() -> bool:
        if state["budget_exceeded"]:
            return True
        return (not opt_in) and state["network_failed"]

    async def fetch_one(mid: str) -> None:
        if _aborted():
            return  # 阻止尚未排队的任务
        await sem.acquire()
        response = None
        # body is set only on a complete successful stream read. Early
        # exits (404 / 4xx / too-large / aborted / network) leave it None
        # so the post-finally path never submits a partial into succeeded.
        body: bytes | None = None
        try:
            if _aborted():  # 获取 sem 后必须二次检查
                return
            upstream_request = request.app.state.upstream.build_request(
                "GET", f"/session/{sid}/message/{mid}",
                headers=forward_directory_headers(directory),
            )
            try:
                response = await request.app.state.upstream.send(
                    upstream_request, stream=True,
                )
            except httpx.RequestError:
                if opt_in:
                    state["network_mids"].add(mid)
                else:
                    state["network_failed"] = True
                return
            if _aborted():  # send() 是 await 点
                return
            if response.status_code == 404:
                # Drain the streaming response body so it contributes to
                # traffic accounting (upIn) and releases the connection.
                err_body = await response.aread()
                stash_up_in(request, len(err_body))
                errors.append({"messageID": mid, "code": "message_not_found"})
                return
            if response.status_code >= 400:
                # Drain the streaming response body so it contributes to
                # traffic accounting (upIn) and releases the connection.
                err_body = await response.aread()
                stash_up_in(request, len(err_body))
                code = f"upstream_http_{response.status_code}"
                entry = {"messageID": mid, "code": code}
                if response.status_code == 429 or response.status_code >= 500:
                    if opt_in:
                        parsed_s = _parse_upstream_retry_after_seconds(
                            response.headers.get("retry-after")
                        )
                        ms = (
                            (parsed_s * 1000)
                            if parsed_s is not None
                            else config.opt_a_retry_after_ms_conservative
                        )
                        entry["retryAfterMs"] = min(max(0, ms), config.opt_a_retry_after_ms_cap)
                errors.append(entry)
                return
            buf = bytearray()
            mid_total = 0
            try:
                async for chunk in response.aiter_bytes(BATCH_CHUNK_SIZE):
                    # ↓↓↓ 从此处到 buf.extend 之间严禁 await（同步临界段）↓↓↓
                    if _aborted():
                        return
                    next_batch_total = state["total"] + len(chunk)
                    # 1) budget 优先（累计超限 → terminal；此 chunk 不计入）
                    if next_batch_total > config.max_response_bytes:
                        state["budget_exceeded"] = True
                        return
                    # 2) 先扣账（chunk 已读 → 计入累计预算，即使随后 per-mid 超限）
                    state["total"] = next_batch_total
                    # 3) 再查 per-mid（超限 → message_too_large envelope，字节已计入）
                    next_mid_total = mid_total + len(chunk)
                    if next_mid_total > config.max_message_bytes:
                        errors.append({
                            "messageID": mid, "code": "message_too_large",
                        })
                        return
                    mid_total = next_mid_total
                    buf.extend(chunk)
                    # ↑↑↑ 临界段结束 ↑↑↑
            except httpx.RequestError:
                if opt_in:
                    state["network_mids"].add(mid)
                else:
                    state["network_failed"] = True
                return
            body = bytes(buf)
        finally:
            if response is not None:
                await response.aclose()
            sem.release()
        if body is None or _aborted():  # aclose/sem 释放都是调度边界
            return
        # Mid-level malformed JSON / bad shape → envelope errors[] (whole
        # request still 200). C⑨: valid JSON that is NOT a usable
        # MessageWithParts shape (non-dict, or dict with missing/malformed
        # info/parts) must NOT escape as HTTP 500 — skeleton_message raises
        # KeyError/TypeError/AttributeError on such shapes, which the prior
        # ``except (orjson.JSONDecodeError, ValueError)`` did not catch. Map
        # every such case to the EXISTING upstream_error code (no new code).
        try:
            raw = orjson.loads(body)
        except (orjson.JSONDecodeError, ValueError):
            errors.append({"messageID": mid, "code": "upstream_error"})
            return
        if not (
            isinstance(raw, dict)
            and isinstance(raw.get("info"), dict)
            and isinstance(raw.get("parts"), list)
        ):
            errors.append({"messageID": mid, "code": "upstream_error"})
            return
        if mode == "skeleton":
            try:
                parsed = await pool.offload(lambda r=raw: skeleton_message(r))
            except (KeyError, TypeError, AttributeError):
                # Defense-in-depth: the shape guard above catches every known
                # bad shape, but any residual failure still maps to
                # upstream_error rather than escaping as a 500.
                errors.append({"messageID": mid, "code": "upstream_error"})
                return
            if _aborted():  # offload 是 await 点
                return
            succeeded[mid] = parsed
        else:
            succeeded[mid] = raw

    try:
        if mode == "skeleton":
            async with pool:
                await asyncio.gather(*(fetch_one(mid) for mid in order))
        else:
            await asyncio.gather(*(fetch_one(mid) for mid in order))
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))

    # Traffic accounting: the batch's shared cumulative upstream byte counter
    # already sums every fetched mid body chunk (BATCH_CHUNK_SIZE granularity,
    # pay-as-you-read). Stash it once here so the middleware attributes the
    # full batch upIn to this request's bucket.
    stash_up_in(request, state["total"])

    KNOWN_ENVELOPE_CODES = {"message_not_found", "message_too_large", "upstream_error", "upstream_unavailable"}
    accept = request.headers.get("accept-encoding")

    if not opt_in:
        # LEGACY — wire-equivalent to pre-deploy (only ledger recording added).
        if state["network_failed"]:
            if ledger is not None:
                ledger.record_legacy_outcome(top_level_503=True, mode=mode)
            return error_response("upstream_unavailable", 503, accept_encoding=accept)
        if state["budget_exceeded"]:
            if ledger is not None:
                ledger.record_legacy_outcome(top_level_503=False, mode=mode)
            return error_response("response_too_large", 413, limit=config.max_response_bytes, accept_encoding=accept)
        items = [succeeded[mid] for mid in order if mid in succeeded]
        resp = json_response({"items": items, "errors": errors}, headers={"Cache-Control":"no-store"}, accept_encoding=accept)
        if ledger is not None:
            ledger.record_legacy_outcome(top_level_503=False, mode=mode)
        return resp

    # OPT-IN path
    # C1: cumulative 413 stays top-level for opt-in too.
    if state["budget_exceeded"]:
        resp = error_response("response_too_large", 413, limit=config.max_response_bytes, accept_encoding=accept)
        if ledger is not None:
            ledger.record_opt_in_outcome(outcome="top_level_413", envelope_5xx=False,
                unknown_codes=0, network_mid_errors=len(state["network_mids"]),
                items_count=0, errors_count=0, bytes_fetched=state["total"],
                bytes_delivered_skeleton=len(resp.body), mode=mode, retry_after_ms_emitted=0)
        return resp

    # Row 6: ALL requested IDs network-failed, no items, no other errors → top-level 503.
    all_network = len(state["network_mids"]) == len(order)
    if all_network and not succeeded and not errors:
        retry_s = max(1, -(-config.opt_a_retry_after_ms_conservative // 1000))  # ceil
        resp = _opt_a_top_level_503(request, accept_encoding=accept, retry_after_seconds=retry_s)
        if ledger is not None:
            ledger.record_opt_in_outcome(outcome="top_level_503", envelope_5xx=True,
                unknown_codes=0, network_mid_errors=len(state["network_mids"]),
                items_count=0, errors_count=0, bytes_fetched=state["total"],
                bytes_delivered_skeleton=len(resp.body), mode=mode, retry_after_ms_emitted=0)
        return resp

    # 200 envelope (success / partial / errors-only). Materialize network mids.
    conservative_ms = config.opt_a_retry_after_ms_conservative
    cap_ms = config.opt_a_retry_after_ms_cap
    retry_after_emitted = 0
    for mid in state["network_mids"]:
        errors.append({"messageID": mid, "code": "upstream_unavailable",
                       "retryAfterMs": min(conservative_ms, cap_ms)})
        retry_after_emitted += 1
    # Defensive cap pass for upstream_http_N entries (already set in fetch_one)
    if opt_in:
        for entry in errors:
            code = entry.get("code", "")
            if code.startswith("upstream_http_") and entry.get("retryAfterMs") is not None:
                entry["retryAfterMs"] = min(int(entry["retryAfterMs"]), cap_ms)
                retry_after_emitted += 1

    # Build items
    items = [succeeded[mid] for mid in order if mid in succeeded]

    # Invariant assertion
    succeeded_ids = set(succeeded.keys())
    error_ids = {e["messageID"] for e in errors}
    if not succeeded_ids.isdisjoint(error_ids):
        raise RuntimeError(f"invariant violation: {succeeded_ids & error_ids}")

    # Classify outcome
    if not items and not errors:
        outcome = "errors_only"
    elif items and errors:
        outcome = "partial"
    elif items and not errors:
        outcome = "success"
    else:  # not items and errors
        outcome = "errors_only"

    unknown_codes = sum(1 for e in errors if e.get("code") not in KNOWN_ENVELOPE_CODES
                    and not e.get("code","").startswith("upstream_http_"))

    resp = json_response({"items": items, "errors": errors},
                         headers={"Cache-Control":"no-store"}, accept_encoding=accept)
    if ledger is not None:
        ledger.record_opt_in_outcome(outcome=outcome, envelope_5xx=False,
            unknown_codes=unknown_codes, network_mid_errors=len(state["network_mids"]),
            items_count=len(items), errors_count=len(errors),
            bytes_fetched=state["total"], bytes_delivered_skeleton=len(resp.body),
            mode=mode, retry_after_ms_emitted=retry_after_emitted)
    return resp


@router.get("/full/{mid}")
async def message(
    request: Request, sid: str, mid: str,
    mode: Literal["skeleton", "full"] = "full",
    directory: str | None = None,
):
    directory = await _resolve_messages_directory(request, directory)
    if request.app.state.schema_degraded:
        mode = "full"
    if mode == "full":
        # G8: stream + cap-read so a single oversized upstream body cannot spike
        # sidecar RSS (MemoryMax=384M). Cap metric = decompressed logical bytes
        # (httpx auto-decompresses), matching list/since. Aborting the read
        # early requires closing the upstream response — done in the finally.
        upstream_request = request.app.state.upstream.build_request(
            "GET", f"/session/{sid}/message/{mid}",
            headers=forward_directory_headers(directory),
        )
        try:
            response = await request.app.state.upstream.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            raise CodedHTTPException(503, code="upstream_unavailable") from exc
        try:
            # Wrap mid-stream upstream I/O failures (httpx.RequestError raised by
            # _drain_error.aread() or read_with_cap.aiter_bytes()) into a structured
            # 503 instead of bubbling up as an unhandled FastAPI 500. The finally
            # below still runs to release the connection.
            try:
                if response.status_code >= 400:
                    return await _drain_error(response, request)
                body, n_read = await read_with_cap(
                    response, request.app.state.config.max_message_bytes,
                )
            except httpx.RequestError as exc:
                raise CodedHTTPException(503, code="upstream_unavailable") from exc
            # Traffic accounting: cap-read upstream bytes.
            stash_up_in(request, n_read)
            if body is None:
                return error_response(
                    "message_too_large", 413,
                    limitBytes=request.app.state.config.max_message_bytes,
                    accept_encoding=request.headers.get("accept-encoding"),
                )
            return Response(
                body, response.status_code,
                headers={**decoded_body_headers(response.headers), "Cache-Control": "no-store"},
            )
        finally:
            await response.aclose()
    config = request.app.state.config
    pool = request.app.state.transforms
    try:
        async with pool:
            response = await _stream_upstream(
                request, f"/session/{sid}/message/{mid}", {}, directory,
            )
            try:
                if response.status_code >= 400:
                    return await _drain_error(response, request)
                body, n_read = await read_with_cap(response, config.max_response_bytes)
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        limit=config.max_response_bytes,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
                # Traffic accounting: skeleton-mode upstream bytes.
                stash_up_in(request, n_read)
                encoded, extra = await pool.offload(
                    project_and_pack, body,
                    single=True,
                    accept_encoding=request.headers.get("accept-encoding"),
                )
            finally:
                await response.aclose()
        return Response(
            encoded, status_code=200, media_type="application/json",
            headers={"Cache-Control": "no-store", **extra},
        )
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))
