"""Token subscriber and admission registry (design §5.5 / §6 / §16-D).

Moved from :mod:`oc_slimapi.sse.token_hub`.
"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...config import (
    DEFAULT_TOKEN_MAX_FRAME_BYTES,
    TOKEN_HANDSHAKE_BUFFER_BYTES,
    TOKEN_HANDSHAKE_ITEMS,
)
from .frames import STOP, _resync_frame
from .models import _TokenMetrics

if TYPE_CHECKING:
    from ..hub import HubRegistry


# ---------------------------------------------------------------------------
# _SubscriberQueue — unified consumer-facing queue with PHYSICAL
# handshake / runtime separation (rev-ogpt CRITICAL 3 fix).
#
# Lane 3's previous design used a single UNBOUNDED asyncio.Queue with a
# ``_in_handshake`` flag that bypassed overflow checks during the startup
# handshake pre-fill. The deterministic bug: after ``end_handshake()`` the
# queue held ``N >> queue_items`` frames; the very next runtime ``put()``
# saw ``qsize() >= queue_items`` → overflow → ``_clear_queue()`` wiped
# EVERY handshake frame (server.connected + tombstones + initial snapshot)
# and replaced the lot with ``resync{subscriber_backpressure}`` + STOP.
# A slow client then observed a resync without ever receiving
# server.connected — a protocol violation.
#
# This class physically separates the two phases so runtime overflow can
# NEVER touch handshake state:
#   * ``_handshake`` — bounded ``deque`` (cap_items + cap_bytes).
#   * ``_runtime``   — bounded ``asyncio.Queue`` (T3 backpressure).
# Consumer order invariant: handshake frames drain FIRST, then runtime.
#
# CRITICAL 2 (round-3): the handshake deque is FAIL-ON-OVERFLOW (not
# drop-oldest). A pathological handshake exceeding the cap (default
# 2048 items / 8 MiB, sized to comfortably hold the 1000-tombstone replay
# ceiling + server.connected + per-active-LivePart snapshots) closes the
# sub so attach_subscriber bails and subscribe() maps the failure to a
# 503-retry. The previous drop-oldest strategy evicted server.connected
# (the FIRST frame) on a tombstone-heavy sid, leaving the client in an
# unrecoverable state.
# ---------------------------------------------------------------------------


class _SubscriberQueue:
    """Consumer-facing queue splitting handshake pre-fill from runtime T3.

    Exposes the asyncio.Queue-like surface the route generator depends on
    (``await get()``, ``qsize()``, ``empty()``, ``get_nowait()``) while
    internally keeping handshake frames in a bounded ``deque`` SEPARATE
    from the runtime ``asyncio.Queue``.

    The runtime overflow path (``clear_runtime()``) touches ONLY the
    runtime side; handshake frames survive so the consumer always drains
    the full handshake (server.connected → tombstones → snapshot) before
    observing the terminal ``resync{subscriber_backpressure}`` + STOP pair.

    CRITICAL 2: ``put_handshake`` is FAIL-ON-OVERFLOW. The handshake cap
    is sized to the full §5.5 pre-fill ceiling (see TOKEN_HANDSHAKE_ITEMS
    guard in config.py); exceeding it means a pathologically large
    upstream state, and the correct response is a loud attach failure
    (503 retry) — NOT silent eviction of server.connected.
    """

    __slots__ = (
        "_runtime",
        "_handshake",
        "_handshake_max_items",
        "_handshake_max_bytes",
        "runtime_bytes",
        "handshake_bytes",
        "last_get_handshake",
    )

    def __init__(
        self,
        *,
        runtime_max_items: int,
        handshake_max_items: int,
        handshake_max_bytes: int,
    ) -> None:
        # Bounded runtime asyncio.Queue. The T3 guard in
        # :meth:`TokenSubscriber.put` checks ``qsize() < maxsize`` BEFORE
        # ``put_runtime``, so QueueFull never fires in the happy path; the
        # bound is defence-in-depth against a logic regression.
        self._runtime: asyncio.Queue = asyncio.Queue(maxsize=runtime_max_items)
        self._handshake: deque = deque()
        self._handshake_max_items = handshake_max_items
        self._handshake_max_bytes = handshake_max_bytes
        # Byte ledgers — single source of truth for accounting.
        self.runtime_bytes: int = 0
        self.handshake_bytes: int = 0
        # Remember which side the last ``get()`` pulled from so the
        # caller's ``ack()`` routes the byte decrement to the correct
        # ledger. The route generator always calls ``get()`` then
        # ``ack()`` on the same frame, so this single-slot toggle is
        # sufficient (no per-frame bookkeeping needed).
        self.last_get_handshake: bool = False

    # ------------------------------------------------------------------
    # Handshake buffer (bounded; FAIL-ON-OVERFLOW, NEVER drop-oldest)
    # ------------------------------------------------------------------
    def put_handshake(self, frame: bytes) -> bool:
        """Stage a handshake frame; FAIL-ON-OVERFLOW (not drop-oldest).

        Returns ``True`` if the frame landed; ``False`` if the buffer is
        at cap (items OR bytes). The caller (:meth:`TokenSubscriber.put`)
        sets ``sub.closed=True`` on ``False`` so ``attach_subscriber``
        bails without entering fanout and ``subscribe()`` raises a 503.

        CRITICAL 2 rationale: the previous drop-oldest strategy evicted
        ``server.connected`` (the FIRST handshake frame) when tombstones
        exceeded the cap, leaving the client in an unrecoverable state
        (no connection-establishment frame on the wire, no resync
        marker, no error). Fail-on-overflow turns a pathological
        handshake into a loud 503-retry instead. ``server.connected``
        always lands (it is the first frame; the buffer starts empty).
        """
        size = len(frame)
        if (
            len(self._handshake) >= self._handshake_max_items
            or self.handshake_bytes + size > self._handshake_max_bytes
        ):
            return False  # overflow signal — caller closes the sub
        self._handshake.append(frame)
        self.handshake_bytes += size
        return True

    # ------------------------------------------------------------------
    # Runtime queue (bounded; T3 backpressure enforced by caller)
    # ------------------------------------------------------------------
    def put_runtime(self, frame: Any) -> None:
        """Enqueue onto the bounded runtime asyncio.Queue.

        Caller MUST pre-check ``qsize() < maxsize`` and
        ``runtime_bytes + size <= buffer_bytes`` (the T3 guard in
        :meth:`TokenSubscriber.put`); this method does NOT re-check so
        the production path is a single ``put_nowait`` with no
        ``QueueFull`` surface. ``STOP`` is allowed and is never counted
        in the byte ledger (it is a control sentinel).
        """
        self._runtime.put_nowait(frame)
        if frame is not STOP:
            self.runtime_bytes += len(frame)

    def clear_runtime(self) -> None:
        """Drop every runtime frame (overflow-disconnect path).

        CRITICAL 3: the handshake buffer is NOT touched — the consumer
        still drains handshake frames before observing the ``resync`` +
        ``STOP`` pair that the caller enqueues immediately after this
        returns.
        """
        while True:
            try:
                self._runtime.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.runtime_bytes = 0

    def put_runtime_terminal(self, frame: Any) -> None:
        """Put a terminal frame (``resync`` / ``STOP``) post-overflow.

        These frames are NOT counted in ``runtime_bytes`` — they are the
        terminal marker sealed by the overflow path, not backlog the
        client owes us. Keeping them out of the byte ledger means
        ``queued_bytes`` reads 0 after overflow (mirroring the
        pre-CRITICAL-3 ``_clear_queue`` semantic: backlog cleared,
        terminal pair sealed outside the budget).
        """
        self._runtime.put_nowait(frame)

    def ack_runtime(self, frame: Any) -> None:
        """Mirror of :meth:`put_runtime` for the byte ledger (ack side)."""
        if frame is STOP:
            return
        self.runtime_bytes = max(0, self.runtime_bytes - len(frame))

    def ack_handshake(self, frame: Any) -> None:
        """Mirror of :meth:`put_handshake` for the byte ledger (ack side)."""
        if frame is STOP:
            return
        self.handshake_bytes = max(0, self.handshake_bytes - len(frame))

    # ------------------------------------------------------------------
    # Consumer-facing asyncio.Queue-like API (route generator entry point)
    # ------------------------------------------------------------------
    def qsize(self) -> int:
        """Runtime queue depth ONLY — drives T3 backpressure checks.

        Handshake buffer depth is intentionally excluded: handshake
        frames must NOT count against the runtime ``queue_items`` cap
        (that was the deterministic Lane 3 bug — handshake pre-fill
        exhausted the runtime item budget, so the next runtime put
        unconditionally overflowed).
        """
        return self._runtime.qsize()

    def handshake_qsize(self) -> int:
        """Handshake buffer depth (test / diagnostic surface)."""
        return len(self._handshake)

    def empty(self) -> bool:
        return not self._handshake and self._runtime.empty()

    async def get(self) -> Any:
        # CRITICAL 3 invariant: handshake drains first.
        if self._handshake:
            self.last_get_handshake = True
            return self._handshake.popleft()
        self.last_get_handshake = False
        return await self._runtime.get()

    def get_nowait(self) -> Any:
        if self._handshake:
            self.last_get_handshake = True
            return self._handshake.popleft()
        self.last_get_handshake = False
        return self._runtime.get_nowait()


@dataclass(eq=False)
class TokenSubscriber:
    """One token-stream client's outbound queue (design §5.5 / §5.6 / §16-D).

    Mirrors the control-plane :class:`hub.Subscriber` T3 three-stage guard
    (closed → oversized-drop → overflow-disconnect), but:

    * Bound to a single ``session_id`` (token stream is per-session,
      design §3).
    * The overflow terminal frame is
      ``resync{reason:"subscriber_backpressure", sessionID}`` — §16-D
      requires EVERY token resync to carry ``sessionID`` (the
      control-plane overflow frame omits it because a curated sub spans
      all sessions).
    * On every frame drop (oversized OR runtime-overflow OR handshake-buffer
      overflow-fail) bumps the shared :class:`_TokenMetrics.dropped_frames_total`
      (NB-C5). This is the single authoritative write site: regardless of
      which fanout / handshake path called :meth:`put`, the metric is
      bumped exactly where a frame is actually lost, so it can never
      drift out of sync.

    CRITICAL 3 (handshake / runtime physical separation): the startup
    handshake pre-fill (``server.connected`` → tombstones → snapshot)
    lands in a BOUNDED ``deque`` inside :class:`_SubscriberQueue`,
    decoupled from the runtime ``asyncio.Queue``. Runtime overflow's
    ``clear_runtime()`` clears ONLY the runtime side — handshake frames
    survive so a slow client always sees the full handshake before any
    terminal ``resync`` + ``STOP`` pair.

    CRITICAL 2 (handshake buffer FAIL-ON-OVERFLOW): the handshake deque
    item cap (TOKEN_HANDSHAKE_ITEMS, default 2048) covers the full §5.5
    quantity upper bound (1000 tombstones + server.connected + 32
    snapshots) with a static assertion guarantee. The byte cap (8 MiB)
    is a fail-safe resource limit: in extreme scenarios (32 near-1 MiB
    snapshots with JSON escaping amplification) it may be insufficient
    — overflow triggers a safe 503 ``sse_token_handshake_overflow``
    (no silent frame loss). A pathologically large handshake that
    exceeds the cap FAILS LOUD: ``put_handshake`` returns False,
    ``put`` sets ``closed=True``, and ``subscribe`` raises a 503-retry.
    The previous drop-oldest strategy silently evicted
    ``server.connected`` on a tombstone-heavy sid (deterministic when
    tombstones >= cap), leaving the client unrecoverable — no connection
    frame, no resync, no error.

    Overflow semantics (contract §6 parity): the runtime queue is
    cleared *immediately* and replaced with a single
    ``resync{subscriber_backpressure, sessionID}`` frame + ``STOP``
    sentinel — previously-queued RUNTIME frames are NOT delivered, so a
    slow client cannot keep draining stale data after the sidecar
    decided it is too far behind. The generator dequeues ``STOP`` and
    tears the connection down; :meth:`TokenStreamRegistry.unsubscribe`
    (called from the generator's ``finally``) detaches the sub.
    """

    session_id: str
    metrics: "_TokenMetrics"
    queue_items: int = 64
    buffer_bytes: int = 512 * 1024
    max_frame_bytes: int = DEFAULT_TOKEN_MAX_FRAME_BYTES
    # Handshake buffer caps — independent of the runtime T3 budget so a
    # legitimately large pre-fill (tombstone replay + snapshots) never
    # trips runtime backpressure. Defaults read from config.py (CRITICAL 2):
    # item cap covers the full §5.5 quantity upper bound (static assertion
    # guaranteed); byte cap (8 MiB) is a fail-safe resource limit — in
    # extreme scenarios (32 near-1 MiB snapshots with JSON escaping
    # amplification) it may trigger overflow-fail → 503.
    # Overflow FAILS LOUD (sub.closed → 503 retry), never silent drop.
    handshake_items: int = TOKEN_HANDSHAKE_ITEMS
    handshake_buffer_bytes: int = TOKEN_HANDSHAKE_BUFFER_BYTES

    id: str = field(default_factory=lambda: "tok_" + secrets.token_hex(4))
    closed: bool = False
    # MINOR 1: set by ``put()`` when handshake buffer overflow (not cap
    # limit) caused the close — lets ``subscribe()`` emit distinct error
    # code ``sse_token_handshake_overflow`` vs ``sse_token_subscriber_limit``.
    _handshake_overflow: bool = False
    dropped_frames: int = 0
    forced_disconnects: int = 0

    # B3b-2 (v4 SSE replay): set by the /sessions/{sid}/stream route when
    # the request ran the v4 wire view. A ``wire_v4`` subscriber receives
    # live-fanout business frames (delta / done marker / truncated /
    # message.removed) WITH their ``id: t:<sid>:<epoch>:<seq>`` prefix;
    # v3 subscribers keep the byte-identical id-less frames (v3
    # zero-change). Flipped by the route immediately after ``subscribe()``
    # (no await between); handshake frames (server.connected / tombstone
    # replay / baseline snapshots) are connection-scoped and never
    # stamped.
    wire_v4: bool = False

    queue: _SubscriberQueue = field(default=None)

    # Handshake guard (Lane A's hub.py attach_subscriber brackets the
    # pre-fill with begin_handshake / end_handshake). When True, ``put``
    # routes to the bounded handshake buffer instead of the runtime
    # asyncio.Queue.
    _in_handshake: bool = False

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = _SubscriberQueue(
                runtime_max_items=self.queue_items,
                handshake_max_items=self.handshake_items,
                handshake_max_bytes=self.handshake_buffer_bytes,
            )

    @property
    def queued_bytes(self) -> int:
        """Total buffered bytes (handshake + runtime).

        Public accounting surface — mirrors the pre-CRITICAL-3
        single-ledger field so existing tests / metrics callers keep
        working. The runtime T3 byte-budget check inside :meth:`put`
        reads ``self.queue.runtime_bytes`` DIRECTLY so handshake bytes
        do NOT count against ``buffer_bytes`` (the deterministic Lane 3
        bug was byte-budget parity between the two phases).
        """
        return self.queue.runtime_bytes + self.queue.handshake_bytes

    def begin_handshake(self) -> None:
        """Enter handshake mode — frames route to the bounded handshake buffer."""
        self._in_handshake = True

    def end_handshake(self) -> None:
        """Exit handshake mode — runtime T3 guards restored (fresh budget)."""
        self._in_handshake = False

    def put(self, frame: Any) -> bool:
        """Enqueue ``frame`` under the T3 three-stage guard.

        Returns ``True`` iff the frame actually landed (handshake OR
        runtime queue); ``False`` on every non-success exit (closed,
        oversized drop, handshake overflow, runtime overflow-disconnect).

        Routing (CRITICAL 3 + CRITICAL 2):

        * ``closed`` → silent drop (the resync + STOP pair already
          enqueued by the overflow path is all the generator should see).
        * ``STOP`` → always runtime (terminal sentinel; sized frames go
          through the size / handshake / T3 path below).
        * oversized (``len(frame) > max_frame_bytes``) → drop + NB-C5
          bump (never closes; never counted in any byte ledger).
        * ``_in_handshake`` → bounded handshake buffer. CRITICAL 2:
          fail-on-overflow (NOT drop-oldest) — ``put_handshake`` returns
          False → ``closed=True`` so ``attach_subscriber`` bails without
          entering fanout and ``subscribe()`` raises a 503 retry.
          ``server.connected`` (the first frame) always lands.
        * otherwise → runtime T3 guard (item count + byte budget);
          overflow → ``clear_runtime()`` (handshake survives) + resync
          + STOP + ``closed=True``.
        """
        if self.closed:
            # Post-disconnect: silently drop. The resync + STOP pair
            # already enqueued by the overflow path is all the
            # generator should see.
            return False
        if frame is STOP:
            # STOP is a runtime-only terminal sentinel; the bounded
            # runtime Queue always has room (overflow path clears it
            # first), so put_nowait never raises QueueFull here.
            self.queue.put_runtime(STOP)
            return True
        size = len(frame)
        if size > self.max_frame_bytes:
            # NB-C5: oversized frame dropped (never monopolise the byte
            # budget). Applies in BOTH handshake and runtime modes —
            # even a handshake pre-fill must not accept an oversized
            # snapshot (the C6 backstop in hub.py emits a ``truncated``
            # substitute instead).
            self.dropped_frames += 1
            self.metrics.dropped_frames_total += 1
            return False
        if self._in_handshake:
            # Handshake mode: route to the bounded handshake buffer so
            # runtime T3 overflow can NEVER clear these frames.
            # CRITICAL 2: fail-on-overflow. The cap (TOKEN_HANDSHAKE_ITEMS,
            # default 2048) is sized above the full §5.5 ceiling (1000
            # tombstones + server.connected + snapshots); exceeding it
            # means a pathologically large upstream state. The correct
            # response is a LOUD attach failure (503 retry) — NOT silent
            # eviction of server.connected (the deterministic rev-ogpt
            # CRITICAL 2 bug under the previous drop-oldest strategy).
            if not self.queue.put_handshake(frame):
                self.closed = True
                self._handshake_overflow = True  # MINOR 1: distinguish from cap limit
                self.dropped_frames += 1
                self.metrics.dropped_frames_total += 1
                return False
            return True
        # Runtime T3 backpressure guard (item count + byte budget).
        # CRITICAL 3: reads runtime depth / runtime bytes ONLY —
        # handshake state is excluded so a freshly-ended handshake does
        # not unconditionally overflow on the next runtime put.
        if (
            self.queue.qsize() < self.queue_items
            and self.queue.runtime_bytes + size <= self.buffer_bytes
        ):
            self.queue.put_runtime(frame)
            return True
        # Overflow: immediate disconnect per §6 (NB-C5 bump + sessionID
        # resync). CRITICAL 3: clear ONLY the runtime queue; the
        # handshake buffer is untouched so the consumer still drains
        # server.connected + tombstones + snapshot before observing the
        # terminal resync + STOP pair.
        self.closed = True
        self.forced_disconnects += 1
        self.metrics.dropped_frames_total += 1
        self.queue.clear_runtime()
        resync = _resync_frame(self.session_id, "subscriber_backpressure")
        # Terminal pair: put WITHOUT bumping runtime_bytes (these are the
        # disconnect marker, not backlog — see put_runtime_terminal).
        self.queue.put_runtime_terminal(resync)
        self.queue.put_runtime_terminal(STOP)
        return False

    def terminate(self, reason: str) -> None:
        """INV-4 (P0-3): server-side termination (session.deleted).

        Mirrors the overflow termination idiom (closed=True →
        clear_runtime → put_runtime_terminal(resync) →
        put_runtime_terminal(STOP)) but WITHOUT bumping
        forced_disconnects / dropped_frames_total — this is a clean
        server-side close, not a backpressure disconnect. The generator
        receives the resync frame (so the client learns WHY the stream
        ended) then STOP (so the generator breaks → finally →
        unsubscribe releases the slot / stops flush / arms grace).

        Does NOT detach from the hub's fanout — :meth:`on_session_deleted`
        relies on the sub still being in ``_subs_by_sid`` so the
        generator's finally → :meth:`TokenStreamRegistry.unsubscribe`
        sees ``has_subscriber() == True`` and runs the normal cleanup
        path (detach + decrement + last-detach stop + grace arm).
        """
        self.closed = True
        self.queue.clear_runtime()
        self.queue.put_runtime_terminal(
            _resync_frame(self.session_id, reason)
        )
        self.queue.put_runtime_terminal(STOP)

    def ack(self, frame: Any) -> None:
        """Decrement the byte ledger for a frame consumed via ``queue.get()``.

        Size accounting is the exact mirror of :meth:`put` (``len(frame)``
        for non-STOP frames). The queue remembers whether the last
        ``get()`` pulled from the handshake buffer or the runtime queue
        and routes the decrement to the correct ledger — so the route
        generator's uniform ``ack(item)`` call (which does not know the
        frame's origin) still keeps both ledgers honest. ``STOP`` is a
        control sentinel that ``put`` never adds to any ledger, so
        callers must not ``ack`` it (the no-op guard is defensive).
        """
        if frame is STOP:
            return
        if self.queue.last_get_handshake:
            self.queue.ack_handshake(frame)
        else:
            self.queue.ack_runtime(frame)


class TokenSubscriberCapacityError(Exception):
    """Raised when token-stream admission would exceed the cap (design §6).

    ``code`` is ``sse_token_subscriber_limit`` (capacity) or
    ``sse_token_handshake_overflow`` (handshake buffer overflow);
    ``limit`` / ``current`` are surfaced on the wire (503 body) and via
    the metrics endpoint. ``buffer_bytes`` is included only for
    ``sse_token_handshake_overflow`` (the handshake buffer byte cap).
    Independent ledger — does NOT reuse the control-plane
    :class:`hub.SubscriberCapacityError` codes (those key off
    ``MAX_TOTAL_SUBSCRIBERS``; this one off ``token_stream_max_subscribers``).
    """

    def __init__(self, code: str, *, limit: int, current: int, buffer_bytes: int | None = None) -> None:
        self.code = code
        self.limit = limit
        self.current = current
        self.buffer_bytes = buffer_bytes
        super().__init__(f"{code}: current={current}, limit={limit}")


class TokenStreamRegistry:
    """Independent admission ledger for token-stream subscribers (design §6).

    Own budget — does NOT consume ``HubRegistry.MAX_TOTAL_SUBSCRIBERS``
    (design §6: token subscribers carry their own
    ``token_stream_max_subscribers`` cap; worst case
    ``8 × (512KiB queue + 8MiB handshake) + 4MiB live + 4MiB pending
     = 76 MiB``).

    :meth:`subscribe` admission + handshake run in ONE synchronous
    (no-await) critical section so a concurrent coroutine cannot slip between
    the cap check and the increment — same discipline as
    :meth:`HubRegistry.subscribe`. The ``"同时最多 1 条前台 stream"`` budget
    (CLIENT_CHANGES §7) is a CLIENT-side advisory (design §9 item 7 is
    建议, not 必须); the server enforces ``token_stream_max_subscribers``
    only.
    """

    def __init__(
        self,
        token_hub: "TokenStreamHub",
        hub_registry: "HubRegistry",
        *,
        max_subscribers: int,
        queue_items: int,
        buffer_bytes: int,
        max_frame_bytes: int,
    ) -> None:
        self.token_hub = token_hub
        self.hub_registry = hub_registry
        self.max_subscribers = max_subscribers
        self.queue_items = queue_items
        self.buffer_bytes = buffer_bytes
        self.max_frame_bytes = max_frame_bytes
        self.total_subscribers = 0
        self.rejected_total = 0

        # L2-A (plan Task L2-A / oracle §A-1 BLOCKER): curated-events token
        # consumers on ``/slimapi/events?tokens=1``. Each is a control-plane
        # ``Subscriber`` whose ``put`` is registered on
        # ``TokenStreamHub.events_tap``; this set is the events side of the
        # combined flush-loop ledger (first-attach start / last-detach stop)
        # so the loop keeps running while ONLY events-token consumers remain
        # (ocdroid retires the per-session stream → zero ``total_subscribers``
        # must NOT stop the flush that feeds events?tokens=1). A-C5 ledger
        # symmetry.
        self.events_tokens: set[Any] = set()

    def attach_events_subscriber(self, sub: Any) -> None:
        """L2-A: register a control-plane events subscriber as a first-class
        consumer of the token flush loop (``/slimapi/events?tokens=1``).

        The events subscriber's :meth:`Subscriber.put` is appended to
        :attr:`TokenStreamHub.events_tap`, so every flushed ``(sid, mid,
        pid)`` window concat is enqueued as a lean ``{type:"token", ...}``
        frame. Because the tap reuses ``Subscriber.put``, the UNCHANGED T3
        backpressure guard (overflow → ``resync{subscriber_backpressure}``
        + disconnect, A-C4) applies with no new path.

        Lifecycle (A-C5 / NB-C4 extension): this counts toward the combined
        start/stop ledger — the FIRST events-token attach starts the flush
        loop even with zero per-session stream subs, and the loop keeps
        running until BOTH ledgers are empty.
        """
        if sub in self.events_tokens:
            return  # idempotent (same admission slot re-attached)
        self.events_tokens.add(sub)
        self.token_hub.events_tap.append(sub.put)
        # First-attach lifecycle: start the flush loop (idempotent — a
        # per-session first-attach may already have started it).
        self.token_hub.start()

    def detach_events_subscriber(self, sub: Any) -> None:
        """L2-A: mirror of :meth:`attach_events_subscriber`.

        Removes the events subscriber from the tap; stops the flush loop on
        the true last-detach (both ledgers empty) and re-arms GlobalHub
        grace symmetrically (B-D1, same predicate as :meth:`unsubscribe`).
        """
        if sub not in self.events_tokens:
            return  # idempotent
        self.events_tokens.discard(sub)
        with contextlib.suppress(ValueError):
            self.token_hub.events_tap.remove(sub.put)
        if self.total_subscribers == 0 and not self.events_tokens:
            self.token_hub.stop()
        if self.hub_registry is not None:
            self.hub_registry.maybe_arm_grace_if_idle()

    def subscribe(self, sid: str) -> TokenSubscriber:
        """Admit one token subscriber for ``sid`` under the cap + handshake.

        Order (all synchronous, no ``await`` → no interleaving with another
        coroutine):

        1. cap check — else raise :class:`TokenSubscriberCapacityError`
           (caller maps to 503 ``sse_token_subscriber_limit``).
        2. construct the :class:`TokenSubscriber` (no side effects — just a
           dataclass + queue; safe to construct before the side-effectful
           section).
        3. ensure the single upstream ``/global/event`` is connected
           (design §5.2: ``registry.get_global().ensure_upstream()``) and
           cancel any armed registry grace-removal (NB-B1).
        4. start the token flush loop (first-attach lifecycle, NB-C4).
        5. :meth:`TokenStreamHub.attach_subscriber` runs the §5.5 handshake
           (server.connected → flush_sid → snapshot → enter fanout).
        6. MAJOR 4 + MAJOR 5: if the sub came back from ``attach_subscriber``
           with ``closed=True`` (handshake buffer overflow CRITICAL 2,
           oversized-frame guard armed mid-handshake, or a future Lane-A
           change), DO NOT increment the ledger AND perform a COMPLETE
           ROLLBACK of the side effects from steps 3–4 (flush loop stop +
           GlobalHub grace re-arm). Without the rollback the flush loop
           keeps running for nobody, ``GlobalHub.run()`` parks forever on
           ``aiter_lines``, and the upstream ``/global/event`` connection +
           hub tasks leak (B-D1 ghost-subscriber resource leak).
        7. increment the ledger.

        INV-3 (P1-20): steps 3–5 are wrapped in ``try / except`` so ANY
        exception (QueueFull, serialization error, future handshake logic
        error, etc. — not just the ``closed`` path) triggers the SAME
        symmetric rollback via :meth:`_rollback_failed_attach`.
        :class:`asyncio.CancelledError` is re-raised without being caught
        by ``except Exception``.
        """
        if self.total_subscribers >= self.max_subscribers:
            self.rejected_total += 1
            raise TokenSubscriberCapacityError(
                "sse_token_subscriber_limit",
                limit=self.max_subscribers,
                current=self.total_subscribers,
            )
        # Construct the sub early — it has no side effects (just a dataclass
        # + bounded queue init), so it is safe to create before the
        # side-effectful section. This lets INV-3 wrap ensure_upstream /
        # start / attach in a single try with a uniform rollback.
        sub = TokenSubscriber(
            session_id=sid,
            metrics=self.token_hub._metrics,
            queue_items=self.queue_items,
            buffer_bytes=self.buffer_bytes,
            max_frame_bytes=self.max_frame_bytes,
        )
        # INV-3 (P1-20): wrap the ENTIRE side-effectful section so any
        # exception (not just sub.closed) triggers symmetric rollback.
        # CancelledError is re-raised untouched (not swallowed by the
        # broad except).
        try:
            # Upstream lifecycle: ensure connected + cancel grace removal (NB-B1).
            if self.hub_registry is not None:
                hub = self.hub_registry.get_global()
                self.hub_registry.cancel_pending_removal()
                hub.ensure_upstream()
            # Token flush loop (idempotent start; first-attach lifecycle).
            self.token_hub.start()
            # §5.5 handshake (server.connected first, then flush_sid →
            # snapshot → enter fanout).
            self.token_hub.attach_subscriber(sid, sub)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._rollback_failed_attach(sid, sub)
            self.rejected_total += 1
            raise
        # MAJOR 4 + MAJOR 5: attach_subscriber's membership guard checks
        # ``sub.closed`` and bails without entering fanout if True. The
        # pre-MAJOR-4 bug: subscribe() ALWAYS incremented total_subscribers
        # afterwards, leaking a slot on every closed-attach. The pre-MAJOR-5
        # bug: the side effects from steps 3–4 above (flush loop running,
        # GlobalHub grace cancelled, upstream ensured) were NOT rolled back,
        # leaking a ghost subscriber + flush loop + upstream connection.
        #
        # Now: re-check closed (the authoritative source — attach_subscriber
        # may or may not return bool, but sub.closed is always set before
        # the bail) and on failure call _rollback_failed_attach() to
        # symmetrically undo the start/cancel side effects, then raise so
        # the route maps the failure to a 503 + Retry-After.
        if sub.closed:
            self._rollback_failed_attach(sid, sub)
            self.rejected_total += 1
            # MINOR 1: distinguish handshake overflow (attach bailed due to
            # handshake buffer / oversized-frame guard) from real capacity
            # limit (total_subscribers >= max). Both map to 503 but carry
            # different error codes so ocdroid can distinguish.
            code = (
                "sse_token_handshake_overflow"
                if sub._handshake_overflow
                else "sse_token_subscriber_limit"
            )
            raise TokenSubscriberCapacityError(
                code,
                limit=self.max_subscribers,
                current=self.total_subscribers,
                buffer_bytes=TOKEN_HANDSHAKE_BUFFER_BYTES if sub._handshake_overflow else None,
            )
        self.total_subscribers += 1
        return sub

    def _rollback_failed_attach(self, sid: str, sub: TokenSubscriber) -> None:
        """MAJOR 5: complete cleanup after ``attach_subscriber`` failure.

        Mirrors the post-detach cleanup path in :meth:`unsubscribe`. The
        sub was constructed, upstream ensured, flush loop started, and
        GlobalHub grace cancelled — but the sub came back closed
        (handshake buffer overflow CRITICAL 2, oversized-frame guard,
        or a future Lane-A change). Without this rollback:

        * the flush loop keeps running with zero subscribers (CPU / memory
          waste; the next genuine first-attach would see it already running
          and skip the start — benign for the loop, but the real leak is
          the GlobalHub upstream connection);
        * ``GlobalHub.run()`` never re-arms grace → the upstream
          ``/global/event`` connection + hub tasks leak forever (B-D1
          ghost-subscriber resource leak).

        Idempotent and defensive: every step is a no-op when there is
        nothing to roll back (sub never entered fanout, other subs
        remain, etc.).
        """
        th = self.token_hub
        # Defensive: attach_subscriber checks sub.closed BEFORE adding to
        # fanout, so this SHOULD be a no-op. Belt-and-suspenders against
        # a future Lane-A regression that registers before the closed
        # check.
        if th.has_subscriber(sid, sub):
            th.detach_subscriber(sid, sub)
        # Stop the flush loop iff NO other token subscriber remains. The
        # loop was started unconditionally above; if this was the first-
        # attach attempt and it failed, the loop is running for nobody
        # (mirrors the unsubscribe last-detach stop). total_subscribers
        # was NOT incremented for the failed sub, so this check correctly
        # reflects the pre-attempt count. L2-A: also keep the loop running
        # while events-token consumers (``/slimapi/events?tokens=1``) remain.
        if self.total_subscribers == 0 and not self.events_tokens:
            th.stop()
        # B-D1 symmetric re-arm: we cancelled grace on entry; re-arm it
        # iff no consumer remains across either ledger (control OR token).
        # No-op while any consumer remains (mirrors unsubscribe).
        if self.hub_registry is not None:
            self.hub_registry.maybe_arm_grace_if_idle()

    def unsubscribe(self, sub: TokenSubscriber) -> None:
        """Idempotently release a subscriber slot; arm grace on last-detach.

        NB-C4 lifecycle: the flush loop is started on first-attach and stopped
        on last-detach (and on shutdown via :meth:`TokenStreamHub.stop`).
        Ingest-only accumulator state (live_parts / _pending) is preserved
        across the stop so a re-attach sees no gap (B1: accumulation is
        decoupled from subscribers).

        NB-D1 (TRUE idempotency): the previous guard only checked
        ``total_subscribers <= 0``. A second call with the SAME sub would
        still pass that guard (count was still > 0 from other subs) and
        double-decrement the ledger → drift / flush mis-stop / admission
        skew. Now a membership guard (mirroring
        :meth:`HubRegistry.unsubscribe`) makes a sub already detached (or
        never attached) a genuine no-op BEFORE touching the ledger.

        B-D1 (grace symmetry): on last-detach, RE-ARM the GlobalHub grace
        exactly as subscribe cancelled it (``cancel_pending_removal`` +
        ``ensure_upstream``). Without this, a token-only consumer (the
        common opt-in path) detaching leaves ``GlobalHub.run()`` parked
        forever on ``aiter_lines`` → the upstream ``/global/event``
        connection + hub tasks leak. Uses the unified
        :meth:`HubRegistry.maybe_arm_grace_if_idle` predicate so the hub
        is torn down iff NO consumer remains across either ledger
        (design §5.2 / §16-B).
        """
        th = self.token_hub
        # NB-D1: membership guard — only act iff sub is still in the fanout.
        # MAJOR 4 corollary: a sub that failed attach (closed before fanout)
        # was never added, so this guard correctly no-ops on it without
        # touching the ledger.
        if not th.has_subscriber(sub.session_id, sub):
            return
        th.detach_subscriber(sub.session_id, sub)
        self.total_subscribers -= 1
        if self.total_subscribers < 0:
            # Defensive: should never happen given the membership guard.
            self.total_subscribers = 0
        # L2-A (oracle §A-1): stop the flush loop only when BOTH ledgers are
        # empty — per-session stream subs AND events-token consumers
        # (``/slimapi/events?tokens=1``). A-C5 ledger symmetry.
        if self.total_subscribers == 0 and not self.events_tokens:
            th.stop()
        # B-D1: symmetric arm. No-op while any consumer (control OR token)
        # remains; arms the registry grace-removal on the true last-detach.
        if self.hub_registry is not None:
            self.hub_registry.maybe_arm_grace_if_idle()

    def snapshot_token_metrics(self) -> dict[str, Any]:
        """``sse.tokenStream.*`` block for the metrics endpoint (design §7).

        S-3a additive keys: ``gzipRawBytesTotal``, ``gzipCompressedBytesTotal``,
        ``flushDurationMsTotal``, ``flushTicksTotal``, ``maxSubscriberQueueDepth``.

        ``maxSubscriberQueueDepth`` reflects RUNTIME queue depth only
        (CRITICAL 3): handshake pre-fill depth is intentionally excluded
        so the gauge tracks "how far behind is this sub on live deltas"
        rather than the one-shot handshake burst.
        """
        th = self.token_hub
        m = th._metrics
        # Compute max subscriber queue depth across all attached subs.
        max_qdepth = 0
        for subs in th._subs_by_sid.values():
            for sub in subs:
                qsize = sub.queue.qsize()
                if qsize > max_qdepth:
                    max_qdepth = qsize
        return {
            "current": self.total_subscribers,
            "limit": self.max_subscribers,
            "rejectedTotal": self.rejected_total,
            "pendingAccumulators": len(th._pending),
            "flushedFramesTotal": th.flushed_frames_total,
            "droppedFramesTotal": th.dropped_frames_total,
            "truncatedSnapshotsTotal": th.truncated_snapshots_total,
            "orphanDeltasTotal": th.orphan_deltas,
            "tokenMemoryLimitTotal": th.token_memory_limit_total,
            # S-3a additive
            "gzipRawBytesTotal": m.gzip_raw_bytes_total,
            "gzipCompressedBytesTotal": m.gzip_compressed_bytes_total,
            "flushDurationMsTotal": m.flush_duration_ms_total,
            "flushTicksTotal": m.flush_ticks_total,
            "maxSubscriberQueueDepth": max_qdepth,
        }
