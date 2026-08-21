"""L2-CD-2 full-merge family — placeholder/ref candidate discovery,
shared single-flight full fetch, budgeted fan-out + splice, and the ``GET
/full/{mid}`` route (F-302 three-family split of ``routes/messages.py``;
pure move, zero behaviour change — the expand worker's shared fetch comes
from here via ``_fetch_full_shared``).
"""

from __future__ import annotations

import asyncio
import time

import orjson
import httpx
from fastapi import Request
from starlette.responses import Response

from ...errors import CodedHTTPException
from ...gzip_util import error_response
from ...singleflight import full_fetch_key, fulls
from ...skeleton import recompute_fingerprint, strip_diagnostics_message
from ...transform import (
    TransformBusy,
    read_with_cap,
    strip_diagnostics_and_pack,
)
from ...upstream import forward_directory_headers
from ...upstream_errors import raise_upstream_unavailable
from .._catalog_common import read_upstream_response
from ._router import _busy_response, _resolve_messages_directory, router

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


def _expand_ref_pairs(projected: list[dict]) -> list[tuple[int, str]]:
    """(index, mid) of every projected message carrying a part-level
    ``expandRefs`` key — the design-expand §4.3 ref candidate set.

    Only PART-level keys count. Message-level ``info.expandRefs`` (the
    ``info.summary.diffs`` reference) NEVER enters the candidate set: diffs
    average ~105 KB and would exhaust the merged budget instantly, and the
    list view does not need them (§4.3.1). The check is a plain dict-key
    presence test — no skeleton import (lane A owns skeleton.py's expandRefs
    emission; the key's exact location in the projected part is all we need).
    """
    pairs: list[tuple[int, str]] = []
    for index, message in enumerate(projected):
        if not isinstance(message, dict):
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        if not any(
            isinstance(p, dict) and "expandRefs" in p for p in parts
        ):
            continue
        info = message.get("info")
        mid = info.get("id") if isinstance(info, dict) else None
        if isinstance(mid, str) and mid:
            pairs.append((index, mid))
    return pairs


def _merged_candidate_pairs(
    projected: list[dict], config,
) -> list[tuple[int, str]]:
    """Merged candidate list — design-expand §4.3 (R3-B model).

    Placeholder-first: the skeleton collapse placeholders form the HIGH
    priority queue and claim the page/full budget exactly as before
    (``merged_max_fulls_per_page`` slots, page order — the current /full
    behavior, unchanged). Ref candidates (messages whose ANY part carries a
    part-level ``expandRefs``) fill only the REMAINING slots, page order,
    best-effort — they never displace a placeholder.

    Intersection dedup (R4-min1): a message belonging to both classes is
    counted once. The placeholder identity wins (dedup by mid, reserved for
    the placeholder queue) — it occupies exactly ONE slot and triggers only
    ONE full fetch, which the renderability of either class would produce
    (§4.3.1). ``info.expandRefs`` (the diffs reference) never contributes
    candidates — see ``_expand_ref_pairs``.
    """
    page_cap = config.merged_max_fulls_per_page
    placeholder_pairs = _placeholder_pairs(projected)[:page_cap]
    placeholder_mids = {mid for _, mid in placeholder_pairs}
    ref_pairs = [
        (index, mid) for index, mid in _expand_ref_pairs(projected)
        if mid not in placeholder_mids  # intersection → placeholder identity
    ]
    remaining_slots = max(0, page_cap - len(placeholder_pairs))
    return placeholder_pairs + ref_pairs[:remaining_slots]


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
    the item's budget allotment — ``min(max_message_bytes, remaining,
    share)`` with the F-006 equal share — so the request-level reservations
    never exceed ``merged_max_bytes``. The
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
    fingerprint: bool = False,
) -> bytes:
    """Merged phases B + C: budgeted fan-out fetch, then single-offload
    splice to identity bytes.

    (Batch 2 / B1: returns the pre-gzip identity body — the route judges
    ``If-None-Match`` on it before any compression, plan §4.)

    Phase B (no pool slot) — ``merged_max_bytes`` is a TRUE FETCH budget
    (rev-fix 2), not a post-hoc filter. A request-level ``remaining`` pool
    starts at ``merged_max_bytes``; a fetch RESERVES its read cap
    (``min(max_message_bytes, remaining, share)`` with the F-006 equal
    share ``share = max(1, merged_max_bytes // candidates)``) synchronously
    when it starts and is REFUNDED ``cap - len(body)`` on completion.
    Invariants:

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
    pairs = _merged_candidate_pairs(projected, config)
    semaphore = asyncio.Semaphore(config.merged_fanout)
    remaining = [config.merged_max_bytes]  # mutable cell shared by the tasks

    async def _fetch_one(mid: str):
        async with semaphore:
            # F-006 anti-starvation: EQUAL-SHARE reservation. Every candidate
            # reserves ``min(max_message_bytes, remaining, share)`` with
            # ``share = max(1, merged_max_bytes // max(1, len(pairs)))`` — the
            # first candidate can no longer reserve the WHOLE page budget and
            # zero-start the rest (the old defect: the default 32 MiB / 8 MiB
            # combo deterministically degraded every multi-candidate page to
            # a single inline).
            #
            # I1 (segmented start guarantee): strict segment M >= N
            # (M = merged_max_bytes, N = len(pairs); the production defaults
            # 8 MiB >= 16 slots live here) gives N * share <= M, so
            # ``remaining`` cannot hit 0 before the Nth candidate passes the
            # gate → ALL N candidates start with a positive cap. Floor
            # segment M < N pins share to 1: exactly min(N, M) candidates
            # start (1 byte each, serial worst case) — an anti-monopoly
            # promise, not all-start (M bytes cannot fund N > M positive
            # shares).
            #
            # I2 (peak): concurrent in-flight reservations sum to
            # <= N * share — strictly <= M in the M >= N segment; the floor
            # segment's share = 1 adds at most N - M bytes of slack.
            cap = min(
                config.max_message_bytes,
                remaining[0],
                max(1, config.merged_max_bytes // max(1, len(pairs))),
            )
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
                # Refund the un-read reservation (serial point). A successful
                # read refunds ``cap - len(body)``; a truncated read (None)
                # and a per-item error (_DEGRADED) return no body →
                # held = 0 → the FULL reservation is refunded (a truncated
                # body is discarded — the item degrades anyway — so its
                # bytes are released for later candidates).
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
            fingerprint=fingerprint,
        )


def _merge_fulls_and_pack(
    projected: list[dict], fetched: dict[int, bytes],
    *, accept_encoding: str | None, fingerprint: bool = False,
) -> bytes:
    """Worker entry (phase C): splice fetched fulls into the projected list,
    then serialize to identity bytes — the merged analogue of
    ``_project_list_sorted_and_pack``.

    For each fetched message: parse the full body, strip the never-consumed
    LSP diagnostics map (same ``strip_diagnostics_message`` as /full), and
    replace the message's ``parts`` with the full parts. The message keeps
    the LIST's ``info`` (order key unchanged, byte-parity with the default
    projection elsewhere). Malformed per-item bodies (bad JSON / non-dict /
    non-list parts) degrade that item — never the page.

    Batch 2 / B1 (pre-compression validator): returns the identity bytes
    only; the route judges ``If-None-Match`` on them BEFORE compressing (a
    changed /full detail changes the merged body → a new validator, even
    when the list page body is unchanged). ``accept_encoding`` is retained
    for call-site symmetry.

    Batch 4 / B3 (merged fingerprint recomputation): after a successful
    splice the message's final representation CHANGED (skeleton parts →
    full parts), so the skeleton-period ``contentFingerprint`` is stale —
    recompute over the spliced message. The FIVE degrade paths below
    (fetch error / budget skip happen upstream in ``_merge_fulls`` and never
    reach ``fetched``; bad JSON / non-dict / non-list ``parts`` bail before
    the splice) do NOT recompute: the final representation IS the original
    skeleton, whose fingerprint is already correct.
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
            if fingerprint:
                recompute_fingerprint(projected[index])
    return orjson.dumps(projected)


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
                    merge_directory_vary=True,
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
