from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse

import orjson
import httpx
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ..errors import CodedHTTPException
from ..gzip_util import compress_if_beneficial, error_response
from ..skeleton import SkeletonLimits, skeleton_messages, strip_diagnostics_message
from ..sse.singleflight import full_fetch_key, fulls
from ..transform import (
    TransformBusy,
    read_with_cap,
    strip_diagnostics_and_pack,
)
from ..upstream import (
    forward_directory_headers,
)
from ..upstream_errors import (
    raise_upstream_unavailable,
)
from ._catalog_common import read_upstream_response
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


def _parse_sort_project(
    body: bytes, *, limits: SkeletonLimits,
) -> list[dict]:
    """Worker entry: parse + sort by ``info.time.created`` ASC + skeleton
    project (no serialization).

    Shared by the default pack worker (:func:`_project_list_sorted_and_pack`)
    and the L2-CD-2 merged path, which needs the projected dicts (to detect
    placeholder messages and later splice inlined fulls) before packing.
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
    return skeleton_messages(parsed, limits=limits)


def _project_list_sorted_and_pack(
    body: bytes, *, accept_encoding: str | None, limits: SkeletonLimits,
) -> tuple[bytes, dict[str, str]]:
    """Worker entry: parse + sort + project + serialize (+ optional gzip).

    lite-v2 §8: skeleton list endpoint must return messages sorted by
    ``info.time.created`` ASC. Sort defensively rather than relying on
    upstream opencode's default ordering. Mirrors ``transform._pack_json``
    inline (kept private to that module) so this stays self-contained.

    ``limits`` carries the per-call inline caps (built by the route from
    ``request.app.state.config``) so two apps with different Settings project
    the same upstream body differently — the worker never reads module-level
    config (T8-C1 / T8-C6).
    """
    projected = _parse_sort_project(body, limits=limits)
    encoded = orjson.dumps(projected)
    return compress_if_beneficial(encoded, accept_encoding)


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
        raise_upstream_unavailable(exc)


# ---------------------------------------------------------------------------
# L2-CD-2 — mode=merged server-side merge (oracle §C-1 / §C-2).
#
# The merged path runs in three phases:
#
#   A. under pool admission (unchanged list flow): upstream list GET +
#      cap-read + ONE offload (parse + sort + skeleton project, no pack);
#   B. WITHOUT any pool slot: fan-out full fetches for the page's
#      placeholder messages — bounded by ``merged_fanout`` concurrency and
#      ``merged_max_fulls_per_page`` per page, deduped with concurrent
#      direct /full requests via ``singleflight.fulls`` (same key), with
#      cumulative ``merged_max_bytes`` accounting;
#   C. under pool admission again (existing busy semantics): ONE final
#      offload splices the fetched fulls into the projected list and packs.
#
# Oracle §C-2: phase B deliberately does NOT take per-full transform-pool
# admission — with the default ``max_transforms=1``, per-full admission
# would serialize up to 16 fulls while each holds the slot across a network
# GET and starve concurrent transforms / direct /full requests.
# ---------------------------------------------------------------------------

# Mirrors skeleton.py's collapse marker ``f"thin_placeholder_{message_id}"``
# (skeleton.py is outside this change's write domain, so the prefix is
# restated here next to its only consumer).
_PLACEHOLDER_PART_ID_PREFIX = "thin_placeholder_"

# Sentinel: per-item full fetch failed (structured upstream error) → that
# message keeps its skeleton projection; the page still merges.
_DEGRADED = object()


class _CapExceeded(Exception):
    """Internal: a shared upstream read hit its per-flight cap.

    Raised INSIDE the single-flight factory (translated from
    ``read_upstream_response``'s ``None``) so the flight entry is DROPPED on
    truncation instead of being grace-retained as a ``None`` result. That
    keeps a merged-budget truncation from poisoning later joiners: a direct
    /full caller (whose cap is the full ``max_message_bytes``) that joins a
    merged-led flight truncated at a smaller budget cap sees ``_CapExceeded``
    with a cap below its own, retries as its own leader at its full cap, and
    — if consecutive small-cap flights exhaust the retry budget — falls back
    to one dedicated GET (see ``_fetch_full_shared``). Direct /full is never
    subject to the merged budget.
    """

    __slots__ = ("cap",)

    def __init__(self, cap: int) -> None:
        super().__init__(cap)
        self.cap = cap


def _placeholder_pairs(projected: list[dict]) -> list[tuple[int, str]]:
    """(index, mid) of every projected message carrying the skeleton
    collapse placeholder part, in page order.

    ``skeleton_message`` appends the ``thin_placeholder_{mid}`` marker part
    when NO part of the upstream message is renderable — exactly the
    messages whose content is invisible in skeleton mode and that
    ``mode=merged`` exists to expand. ``mid`` prefers the placeholder part's
    ``messageID`` (set by the projection to the message id), falling back to
    ``info.id``; messages without a usable id are skipped (cannot fetch).
    """
    pairs: list[tuple[int, str]] = []
    for index, message in enumerate(projected):
        if not isinstance(message, dict):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            if not str(part.get("id", "")).startswith(
                _PLACEHOLDER_PART_ID_PREFIX,
            ):
                continue
            mid = part.get("messageID")
            if not (isinstance(mid, str) and mid):
                info = message.get("info")
                mid = info.get("id") if isinstance(info, dict) else None
            if isinstance(mid, str) and mid:
                pairs.append((index, mid))
            break  # one placeholder part per message, by construction
    return pairs


async def _dedicated_full_get(
    request: Request, sid: str, mid: str, directory: str | None, cap: int,
) -> bytes | None:
    """ONE dedicated upstream GET for a single message, OUTSIDE the
    single-flight map.

    Shared by the per-attempt flight factory (which translates a ``None``
    truncation into ``_CapExceeded`` so the entry is dropped) and the direct
    /full fallback after retry exhaustion. Returns the buffered body, or
    ``None`` when the read was truncated at ``cap``. Raises structured
    ``CodedHTTPException`` on upstream errors.
    """
    upstream_request = request.app.state.upstream.build_request(
        "GET", f"/session/{sid}/message/{mid}",
        headers=forward_directory_headers(directory),
    )
    try:
        response = await request.app.state.upstream.send(
            upstream_request, stream=True,
        )
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    try:
        return await read_upstream_response(
            request, response,
            cap=cap,
            read_with_cap=read_with_cap,
            sid=sid,
        )
    finally:
        await response.aclose()


async def _fetch_full_shared(
    request: Request, pool, sid: str, mid: str, directory: str | None,
    *, cap: int | None = None,
) -> bytes | None:
    """Shared upstream GET for one message via ``singleflight.fulls``
    (L2-CD-1 §C-2). Used by BOTH the direct /full route and the L2-CD-2
    merged fan-out, so a merged fetch and a concurrent direct /full for the
    same ``(sid, mid, directory)`` coalesce onto ONE upstream GET.

    ``cap`` bounds the per-flight read (rev-fix 2): the merged fan-out passes
    the item's budget allotment — ``min(max_message_bytes, remaining)`` — so
    the request-level reservations never exceed ``merged_max_bytes``. The
    direct route passes no cap (always the full ``max_message_bytes``) and is
    never affected by a merged budget: if it joins a flight truncated at a
    SMALLER cap, the dropped-entry retry re-fetches at its own full cap; if
    consecutive small-cap flights exhaust the retry budget (rev-fix 2), the
    final fallback below issues one DEDICATED GET so a direct /full can
    never 413 on a body that fits its own cap.

    Returns the buffered body, or ``None`` when the read was truncated at
    the caller's own requested cap (each caller decides its own 413 /
    degrade). Raises structured ``CodedHTTPException`` on upstream errors —
    the direct route propagates them, the merged fan-out degrades.
    """
    config = request.app.state.config
    # A caller-supplied cap never exceeds the configured per-message cap;
    # ``None`` (direct /full) means the full ``max_message_bytes``.
    full_cap = (
        config.max_message_bytes
        if cap is None else min(cap, config.max_message_bytes)
    )

    # Bounded retry loop: attempt 1 joins/leads at ``full_cap``; a retry only
    # happens when we JOINED a flight whose cap was smaller than ours AND it
    # truncated (entry dropped) — the next attempt then leads at our own cap.
    for _attempt in range(3):
        flight_cap = full_cap

        async def _upstream_get() -> bytes:
            body = await _dedicated_full_get(
                request, sid, mid, directory, flight_cap,
            )
            if body is None:
                # Truncated at THIS flight's cap → drop the entry (never
                # retain a truncation) and let joiners with larger caps retry.
                raise _CapExceeded(flight_cap)
            return body

        try:
            return await fulls.fetch(
                full_fetch_key(pool, sid, mid, directory), _upstream_get,
            )
        except _CapExceeded as exc:
            if exc.cap >= full_cap:
                return None  # truncated at (≥) our own requested cap: terminal
            continue  # joined a smaller-cap flight that dropped — re-lead

    # Retry budget exhausted: ≥3 consecutive join-truncations on smaller-cap
    # flights. For direct /full semantics (``cap is None``) returning None
    # here would be a FALSE 413 — every truncation was at a merged budget
    # cap, and the body may well fit the direct caller's own
    # ``max_message_bytes``. Correctness beats dedup: issue ONE dedicated GET
    # outside the flight map. It is deliberately NOT deduped — joining is
    # exactly what kept failing, so worst case this adds one upstream GET
    # beyond any concurrent same-key flight. A truncation HERE (at
    # ``full_cap == max_message_bytes``) is the genuine 413. Merged callers
    # (explicit small cap) keep ``None`` → their budget degrade.
    if cap is None:
        return await _dedicated_full_get(
            request, sid, mid, directory, full_cap,
        )
    return None


async def _merge_fulls(
    request: Request, pool, config, projected: list[dict],
    sid: str, directory: str | None, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Merged phases B + C: budgeted fan-out fetch, then single-offload
    splice+pack.

    Phase B (no pool slot) — ``merged_max_bytes`` is a TRUE FETCH budget
    (rev-fix 2), not a post-hoc filter. A request-level ``remaining`` pool
    starts at ``merged_max_bytes``; a fetch RESERVES its read cap
    (``min(max_message_bytes, remaining)``) synchronously when it starts and
    is REFUNDED ``cap - len(body)`` on completion. Invariants:

    * an item finds ``remaining <= 0`` at its start → ``_DEGRADED`` with NO
      upstream request at all;
    * RESERVATIONS never exceed the budget: each item's accounting value
      only ever shrinks while it holds bytes (in-flight: cap → completed:
      actual bytes), and every reserve/refund runs at an event-loop serial
      point (no ``await`` inside), so the sum of started items' reservations
      is ``≤ merged_max_bytes`` under any completion interleaving;
    * a single in-flight read may OVERSHOOT its reservation by at most one
      read chunk — ``read_with_cap`` checks the cap only after accumulating
      a whole chunk (``chunk_size``, default 64 KiB; see transform.py).

    Scope of these bounds — three precise layers (do NOT over-claim):

    1. The formula ``merged_max_bytes + merged_fanout × chunk_size``
       (defaults: 8 MiB + 8 × 64 KiB ≈ 8.5 MiB) bounds ONLY the
       INCREMENTAL buffering produced by MERGED-LED cap-reads — i.e. fetches
       issued under this reservation model, each reading at its allotted
       merged cap. It is NOT a strict ``≤ merged_max_bytes``, and NOT a
       whole-page peak.
    2. WINDBALLS are outside that formula: the single-flight key does not
       include the cap, so a merged small-cap waiter can JOIN a direct-led
       flight reading at ``max_message_bytes`` (default 32 MiB) — or a
       grace-retained result from such a flight. The full shared body is
       held in the ``asyncio.gather`` results until the splice below
       excludes it, so the page can TRANSIENTLY hold a shared body on the
       order of ``max_message_bytes`` — well above the 8.5 MiB formula.
    3. The ONLY guarantee covering windfalls is POST-SPLICE: the total
       inlined fulls in the RESPONSE stay ``≤ merged_max_bytes`` (cumulative
       check below). Response size controlled ≠ page-held peak buffer
       controlled.

    Fetches are deduped with concurrent direct /full requests via
    ``singleflight.fulls`` (same key); per-item failures (structured upstream
    errors) degrade that item to its skeleton projection — merging must
    never fail the page (oracle §C-1: additive).

    Phase C: reacquire admission under the EXISTING busy semantics (plain
    pool wait → ``TransformBusy`` → the route's unchanged 503 shape) and do
    ONE final offload (splice + serialize + gzip) instead of N serial
    transforms.
    """
    pairs = _placeholder_pairs(projected)[: config.merged_max_fulls_per_page]
    semaphore = asyncio.Semaphore(config.merged_fanout)
    remaining = [config.merged_max_bytes]  # mutable cell shared by the tasks

    async def _fetch_one(mid: str):
        async with semaphore:
            cap = min(config.max_message_bytes, remaining[0])
            if cap <= 0:
                return _DEGRADED  # budget exhausted before start → no fetch
            remaining[0] -= cap  # reserve (serial point: no await yet)
            body: bytes | None | object = _DEGRADED
            try:
                body = await _fetch_full_shared(
                    request, pool, sid, mid, directory, cap=cap,
                )
            except CodedHTTPException:
                body = _DEGRADED  # per-item degrade, not a page failure
            finally:
                # Refund the un-read reservation. A truncated read (None)
                # consumed its whole cap → no refund; an error buffered
                # nothing → full refund. (Also a serial point.)
                held = len(body) if isinstance(body, (bytes, bytearray)) else 0
                remaining[0] += max(0, cap - held)
            return body

    bodies = await asyncio.gather(
        *(_fetch_one(mid) for _, mid in pairs),
    ) if pairs else []

    fetched: dict[int, bytes] = {}
    cumulative_bytes = 0
    for (index, _mid), body in zip(pairs, bodies):
        if body is _DEGRADED or body is None:
            continue  # fetch error / allotted-cap truncation → degrade
        if cumulative_bytes + len(body) > config.merged_max_bytes:
            continue  # windfall from a joined larger flight → still bounded
        cumulative_bytes += len(body)
        fetched[index] = body

    async with pool:
        return await pool.offload(
            _merge_fulls_and_pack, projected, fetched,
            accept_encoding=accept_encoding,
        )


def _merge_fulls_and_pack(
    projected: list[dict], fetched: dict[int, bytes],
    *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entry (phase C): splice fetched fulls into the projected list,
    then serialize (+ optional gzip) — the merged analogue of
    ``_project_list_sorted_and_pack``.

    For each fetched message: parse the full body, strip the never-consumed
    LSP diagnostics map (same ``strip_diagnostics_message`` as /full), and
    replace the message's ``parts`` with the full parts. The message keeps
    the LIST's ``info`` (order key unchanged, byte-parity with the default
    projection elsewhere). Malformed per-item bodies (bad JSON / non-dict /
    non-list parts) degrade that item — never the page.
    """
    for index, body in fetched.items():
        try:
            full = orjson.loads(body)
        except orjson.JSONDecodeError:
            continue  # per-item degrade
        if not isinstance(full, dict):
            continue
        parts = strip_diagnostics_message(full).get("parts")
        if isinstance(parts, list):
            projected[index]["parts"] = parts
    encoded = orjson.dumps(projected)
    return compress_if_beneficial(encoded, accept_encoding)


@router.get("")
async def messages(
    request: Request,
    sid: str,
    limit: int = Query(40, ge=1, le=200),
    before: str | None = None,
    directory: str | None = None,
    mode: str | None = None,
):
    """Skeleton projection of upstream opencode's message listing.

    lite-v2 §2: ``?mode=full`` list branch removed; only skeleton projection
    remains. ``?mode`` values other than the literal ``merged`` are silently
    ignored (clients in transition may still send them; oracle §C-1 keeps
    this additive — never a 400). ``?limit=`` and ``?before=`` pagination
    are preserved (cursor forwarded verbatim).

    L2-CD-2: ``?mode=merged`` (literal, case-sensitive) additionally expands
    the page's skeleton-collapsed messages in place: their placeholder parts
    are replaced by the messages' full projections (diagnostics stripped),
    fetched fan-out style under ``merged_fanout`` / ``merged_max_fulls_per_page``
    / ``merged_max_bytes`` budgets and deduped with concurrent direct /full
    requests via ``singleflight.fulls``. Over-budget / failed items
    progressively keep their skeleton projection; ``X-Next-Cursor`` and all
    non-placeholder messages are byte-identical to the default mode. The
    fan-out holds no per-full transform-pool slot (oracle §C-2), so a merged
    page cannot starve concurrent transforms — see ``_merge_fulls``.

    lite-v2 §8: response is sorted by ``info.time.created`` ASC — see
    ``_project_list_sorted_and_pack``.
    """
    directory = await _resolve_messages_directory(request, directory)
    params = {"limit": limit}
    if before:
        # `before` is opencode's opaque base64url pagination cursor (a
        # base64url JSON envelope — see _extract_before_verbatim /
        # _parse_link_next_cursor). base64url uses only [-_A-Za-z0-9] (no
        # "+" / "/" / space), so FastAPI's percent-decoding of this query
        # param round-trips safely; forward it verbatim to upstream.
        params["before"] = before
    config = request.app.state.config
    pool = request.app.state.transforms
    merged_mode = mode == "merged"
    projected: list[dict] | None = None
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
                # Shared drain-or-cap-read skeleton (status mapping with sid +
                # read_with_cap + mid-stream RequestError → 503).
                body = await read_upstream_response(
                    request, response,
                    cap=config.max_response_bytes,
                    read_with_cap=read_with_cap,
                    sid=sid,
                )
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
                # Full parse + sort (§8) + project (+ serialize/gzip for the
                # default mode) in the worker pool. SkeletonLimits is built
                # per-request from this app's config so two apps with
                # different caps project the same upstream body differently
                # (P1-3 config de-double-tracking; T8-C4 / T8-C6).
                #
                # L2-CD-2: merged keeps the PROJECTED dicts (no pack yet) —
                # the fan-out + splice + pack run AFTER this admission is
                # released (see _merge_fulls; oracle §C-2: the fan-out must
                # not hold the slot across per-full network GETs).
                try:
                    if merged_mode:
                        projected = await pool.offload(
                            _parse_sort_project, body,
                            limits=SkeletonLimits(
                                field_bytes=config.skeleton_inline_output_max_bytes,
                                message_bytes=config.skeleton_inline_output_max_message_bytes,
                            ),
                        )
                    else:
                        encoded, extra = await pool.offload(
                            _project_list_sorted_and_pack, body,
                            accept_encoding=request.headers.get("accept-encoding"),
                            limits=SkeletonLimits(
                                field_bytes=config.skeleton_inline_output_max_bytes,
                                message_bytes=config.skeleton_inline_output_max_message_bytes,
                            ),
                        )
                except (orjson.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                    raise_upstream_unavailable(exc)
            finally:
                await response.aclose()
        if projected is not None:
            # Merged phases B+C: fan-out (no slot) → single splice/pack
            # offload under admission with the EXISTING busy semantics.
            encoded, extra = await _merge_fulls(
                request, pool, config, projected, sid, directory,
                accept_encoding=request.headers.get("accept-encoding"),
            )
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

    L2-CD-1 (oracle §C-2 / §D-1):

    - **Single-flight.** The upstream GET for ``(sid, mid, directory)`` goes
      through the process-level ``singleflight.fulls`` registry, so
      concurrent /full requests for the same message (and, from CD-2 on,
      merged fan-out fetches) share ONE upstream GET. Only the raw fetch is
      shared — each caller keeps its own pool admission + offload around
      the shared body. The key embeds the app's transform-pool identity so
      distinct app instances never share a flight.
    - **Budget absorb.** Admission is retried inside the total
      ``transform_absorb_budget_seconds`` window, each attempt narrowed to
      the remaining budget: transient slot occupancy longer than
      ``transform_wait_seconds`` but shorter than the budget is absorbed
      instead of 503ing, and the worst-case cumulative pool wait never
      exceeds the budget. Budget exhaustion falls through to the unchanged
      503 ``transform_busy`` shape.
    """
    directory = await _resolve_messages_directory(request, directory)
    config = request.app.state.config
    pool = request.app.state.transforms
    accept_encoding = request.headers.get("accept-encoding")
    try:
        # G8: stream + cap-read so a single oversized upstream body cannot
        # spike sidecar RSS (MemoryMax=384M). Cap metric = decompressed
        # logical bytes (httpx auto-decompresses). Aborting the read early
        # requires closing the upstream response — done in the factory's
        # finally. The body is buffered + parsed to strip the never-consumed
        # LSP ``state.metadata.diagnostics`` map (ocdroid deletes it on
        # deserialise); every other field is preserved. The strip runs
        # off-thread under admission acquired BEFORE the upstream GET so the
        # event loop stays free and saturation surfaces as 503
        # transform_busy.
        #
        # L2-CD-1 §D-1: admission retry loop with per-attempt narrowing.
        # Each attempt waits at most min(transform_wait_seconds, remaining
        # budget); a naive retry at the full wait could block up to N× the
        # budget. The loop exits with TransformBusy exactly when the budget
        # is spent, preserving the invariant that a 503 transform_busy never
        # emitted an upstream request (the GET below only runs once
        # admission succeeded), so absorb retries cannot amplify upstream
        # load.
        deadline = time.monotonic() + config.transform_absorb_budget_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransformBusy()
            try:
                await pool.acquire(min(config.transform_wait_seconds, remaining))
            except TransformBusy:
                continue  # narrow the next attempt to the remaining budget
            break
        try:
            # L2-CD-1 §C-2: shared upstream GET (see _fetch_full_shared) —
            # the leader (first caller for this key) executes the factory
            # under the admission we just acquired; concurrent same-key
            # callers (and, since L2-CD-2, merged fan-out fetches for the
            # same key) join the in-flight result instead of issuing their
            # own GET. The factory raises structured CodedHTTPExceptions
            # (mapped statuses, network errors, mid-stream failures) which
            # propagate to every waiter.
            body = await _fetch_full_shared(request, pool, sid, mid, directory)
            if body is None:
                return error_response(
                    "message_too_large", 413,
                    limitBytes=config.max_message_bytes,
                    accept_encoding=accept_encoding,
                )
            # Empty / non-JSON upstream 200 → 503 upstream_unavailable
            # (same code as sessions bad-JSON), never a bare 500. Each
            # caller transforms the shared body under its own admission.
            try:
                encoded, extra = await pool.offload(
                    strip_diagnostics_and_pack, body,
                    accept_encoding=accept_encoding,
                )
            except (orjson.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                raise_upstream_unavailable(exc)
        finally:
            pool.release()
        return Response(
            encoded, status_code=200, media_type="application/json",
            headers={"Cache-Control": "no-store", **extra},
        )
    except TransformBusy:
        return _busy_response(accept_encoding)
