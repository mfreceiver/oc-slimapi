from __future__ import annotations

import asyncio
import re
from typing import Literal
from urllib.parse import urlparse

import orjson
import httpx
from fastapi import APIRouter, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from ..errors import CodedHTTPException
from ..gzip_util import error_response, json_response
from ..skeleton import skeleton_message
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
from .sessions import _raise_upstream_status, require_directory

router = APIRouter(prefix="/slimapi/messages/{sid}", tags=["messages"])

# Fixed Retry-After for transform admission timeouts. Kept as a module constant
# so tests and the route agree on the wire contract.
TRANSFORM_RETRY_AFTER_SECONDS = 2

# G6 batch stream chunk size for the shared cumulative-byte ledger. Smaller than
# transform.read_with_cap's default (64KiB) so concurrent mids debit the budget
# at finer granularity (pay-as-you-read).
BATCH_CHUNK_SIZE = 16 * 1024


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
    """G7-soft (spec §5): validate query ``directory`` against the allowlist.

    - ``directory is None`` → not blocked (returns None; upstream default applies).
      v1 only trusts query ``directory``; a lone ``X-Opencode-Directory`` header
      is not validated and not forwarded (unchanged behaviour).
    - query present AND header present AND they differ → 400 directory_not_allowed.
    - query present → require_directory (may raise 400 directory_not_allowed /
      503 upstream_unavailable on refresh failure).
    Returns the normalised directory to forward (or None).
    """
    if directory is None:
        return None
    header_dir = request.headers.get("x-opencode-directory")
    if header_dir:  # treat empty header as absent
        if (header_dir.rstrip("/") or "/") != (directory.rstrip("/") or "/"):
            raise CodedHTTPException(400, code="directory_not_allowed")
    return await require_directory(request, directory)


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
    return await request.app.state.upstream.send(upstream_request, stream=True)


async def _drain_error(response) -> Response:
    """Buffer a small upstream error body and pass it through verbatim."""
    body = await response.aread()
    return Response(
        body, response.status_code,
        headers=decoded_body_headers(response.headers),
        media_type=response.headers.get("content-type"),
    )


def _item_updated(item: dict) -> int | None:
    """Extract ``info.time.updated`` as an int, or None when absent/non-int."""
    info = item.get("info") or {}
    time_obj = info.get("time") or {}
    updated = time_obj.get("updated")
    return updated if isinstance(updated, int) and not isinstance(updated, bool) else None


def _passes_ts_filter(item: dict, ts: int) -> bool:
    """A2=A filter (contract §5): include items with ``info.time.updated >= ts``.

    Items whose ``info.time.updated`` is missing or not a plain int are included
    defensively — the client dedups by messageID anyway, and we'd rather
    over-deliver a malformed edge item than silently hide it. Only a
    definitively comparable ``updated < ts`` excludes an item.
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
    """A2=A (contract §5): return skeleton messages with ``time.updated >= ts``.

    Walks opencode's newest-first ``/session/{sid}/message`` listing backward
    via the ``before`` cursor (opencode's own opaque base64url cursor,
    forwarded verbatim — never decoded/re-encoded by sidecar). A single
    transform-pool admission covers the whole multi-page scan, and a single
    cumulative ``max_response_bytes`` budget is enforced across pages so a
    runaway timestamp scan cannot accumulate more than the cap of upstream
    bodies (413 ``response_too_large`` on overflow — contract §7).

    Because upstream pages are sorted newest→oldest, the scan stops at the
    first item with ``time.updated < ts``: every subsequent item in this page
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
            for _ in range(config.max_since_pages):
                params = {"limit": limit}
                if cursor:
                    params["before"] = cursor
                response = await _stream_upstream(
                    request, f"/session/{sid}/message", params, directory,
                )
                try:
                    if response.status_code >= 400:
                        return await _drain_error(response)
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
                    break
                if not last_page_cursor:
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
        response = await request.app.state.upstream.send(upstream_request, stream=True)
        return StreamingResponse(
            response.aiter_raw(), status_code=response.status_code,
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
                    return await _drain_error(response)
                body, _ = await read_with_cap(response, config.max_response_bytes)
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        limit=config.max_response_bytes,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
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

    # discover 先行（带 directory 头，spec §8 L266）
    try:
        resp = await request.app.state.upstream.get(
            f"/session/{sid}", headers=forward_directory_headers(directory),
        )
    except httpx.RequestError:
        raise CodedHTTPException(503, code="upstream_unavailable")
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_upstream_status(exc, sid=sid)  # 404→session_not_found (no mid fetch); 其它→502/503
    # 200 + malformed body must not proceed to mid expand (→ 503).
    try:
        resp.json()
    except (ValueError, UnicodeDecodeError):
        raise CodedHTTPException(503, code="upstream_unavailable")

    sem = asyncio.Semaphore(4)
    # Shared ledger: total is debited per decoded chunk under a no-await
    # critical section so concurrent mids cannot TOCTOU the cumulative cap.
    # network_failed and budget_exceeded are distinct: 503 beats 413.
    state = {
        "total": 0,
        "network_failed": False,
        "budget_exceeded": False,
    }
    succeeded: dict[str, dict] = {}
    errors: list[dict] = []

    def _aborted() -> bool:
        return state["network_failed"] or state["budget_exceeded"]

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
                state["network_failed"] = True
                return
            if _aborted():  # send() 是 await 点
                return
            if response.status_code == 404:
                errors.append({"messageID": mid, "code": "message_not_found"})
                return
            if response.status_code >= 400:
                errors.append({
                    "messageID": mid,
                    "code": f"upstream_http_{response.status_code}",
                })
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
                state["network_failed"] = True
                return
            body = bytes(buf)
        finally:
            if response is not None:
                await response.aclose()
            sem.release()
        if body is None or _aborted():  # aclose/sem 释放都是调度边界
            return
        # Mid-level malformed JSON → envelope errors[] (whole request still 200).
        try:
            if mode == "skeleton":
                parsed = await pool.offload(
                    lambda b=body: skeleton_message(orjson.loads(b)),
                )
                if _aborted():  # offload 是 await 点
                    return
                succeeded[mid] = parsed
            else:
                succeeded[mid] = orjson.loads(body)
        except (orjson.JSONDecodeError, ValueError):
            errors.append({"messageID": mid, "code": "upstream_error"})
            return

    try:
        if mode == "skeleton":
            async with pool:
                await asyncio.gather(*(fetch_one(mid) for mid in order))
        else:
            await asyncio.gather(*(fetch_one(mid) for mid in order))
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))

    # 503 优先于 413：网络失败与累计超限同时成立时返 503。
    if state["network_failed"]:
        return error_response(
            "upstream_unavailable", 503,
            accept_encoding=request.headers.get("accept-encoding"),
        )
    if state["budget_exceeded"]:
        return error_response(
            "response_too_large", 413, limit=config.max_response_bytes,
            accept_encoding=request.headers.get("accept-encoding"),
        )

    items = [succeeded[mid] for mid in order if mid in succeeded]
    return json_response(
        {"items": items, "errors": errors},
        headers={"Cache-Control": "no-store"},
        accept_encoding=request.headers.get("accept-encoding"),
    )


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
        response = await request.app.state.upstream.send(upstream_request, stream=True)
        try:
            # Wrap mid-stream upstream I/O failures (httpx.RequestError raised by
            # _drain_error.aread() or read_with_cap.aiter_bytes()) into a structured
            # 503 instead of bubbling up as an unhandled FastAPI 500. The finally
            # below still runs to release the connection.
            try:
                if response.status_code >= 400:
                    return await _drain_error(response)
                body, _ = await read_with_cap(
                    response, request.app.state.config.max_message_bytes,
                )
            except httpx.RequestError:
                raise CodedHTTPException(503, code="upstream_unavailable")
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
                    return await _drain_error(response)
                body, _ = await read_with_cap(response, config.max_response_bytes)
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        limit=config.max_response_bytes,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
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
