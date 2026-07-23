"""Token subscriber and admission registry (design §5.5 / §6 / §16-D).

Moved from :mod:`oc_slimapi.sse.token_hub`.
"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...config import DEFAULT_TOKEN_MAX_FRAME_BYTES
from .frames import STOP, _resync_frame
from .models import _TokenMetrics

if TYPE_CHECKING:
    from ..hub import HubRegistry


@dataclass(eq=False)
class TokenSubscriber:
    """One token-stream client's outbound queue (design §5.5 / §5.6 / §16-D).

    Mirrors the control-plane :class:`hub.Subscriber` T3 three-stage guard
    (closed → oversized-drop → overflow-disconnect), but:

    * Bound to a single ``session_id`` (token stream is per-session, design §3).
    * The overflow terminal frame is
      ``resync{reason:"subscriber_backpressure", sessionID}`` — §16-D
      requires EVERY token resync to carry ``sessionID`` (the control-plane
      overflow frame omits it because a curated sub spans all sessions).
    * On every frame drop (oversized OR overflow) bumps the shared
      :class:`_TokenMetrics.dropped_frames_total` (NB-C5). This is the
      single authoritative write site: regardless of which fanout /
      handshake path called :meth:`put`, the metric is bumped exactly where
      a frame is actually lost, so it can never drift out of sync.

    Overflow semantics (contract §6 parity): the queue is cleared
    *immediately* and replaced with a single ``resync{subscriber_backpressure,
    sessionID}`` frame + ``STOP`` sentinel — previously-queued frames are NOT
    delivered, so a slow client cannot keep draining stale data after the
    sidecar decided it is too far behind. The generator dequeues ``STOP`` and
    tears the connection down; :meth:`TokenStreamRegistry.unsubscribe`
    (called from the generator's ``finally``) detaches the sub.
    """

    session_id: str
    metrics: "_TokenMetrics"
    queue_items: int = 64
    buffer_bytes: int = 512 * 1024
    max_frame_bytes: int = DEFAULT_TOKEN_MAX_FRAME_BYTES

    id: str = field(default_factory=lambda: "tok_" + secrets.token_hex(4))
    queued_bytes: int = 0
    closed: bool = False
    dropped_frames: int = 0
    forced_disconnects: int = 0

    queue: asyncio.Queue = field(default=None)

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=self.queue_items)

    def put(self, frame: Any) -> bool:
        """Enqueue ``frame`` under the T3 three-stage guard.

        Returns ``True`` iff the frame actually landed on the queue (so
        fanout callers can count successful emits); ``False`` on every
        non-success exit.
        """
        if self.closed:
            # Post-disconnect: silently drop. The resync + STOP pair already
            # enqueued by the overflow path is all the generator should see.
            return False
        if frame is STOP:
            try:
                self.queue.put_nowait(STOP)
            except asyncio.QueueFull:
                return False
            return True
        size = len(frame)
        if size > self.max_frame_bytes:
            # NB-C5: oversized frame dropped (never monopolise the byte budget).
            self.dropped_frames += 1
            self.metrics.dropped_frames_total += 1
            return False
        if (
            self.queue.qsize() < self.queue_items
            and self.queued_bytes + size <= self.buffer_bytes
        ):
            try:
                self.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Lost a race against a concurrent producer (none in
                # practice since fanout/flush run inline on the loop).
                pass
            else:
                self.queued_bytes += size
                return True
        # Overflow: immediate disconnect per §6 (NB-C5 bump + sessionID resync).
        self.closed = True
        self.forced_disconnects += 1
        self.metrics.dropped_frames_total += 1
        self._clear_queue()
        resync = _resync_frame(self.session_id, "subscriber_backpressure")
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(resync)
            self.queue.put_nowait(STOP)
        return False

    def ack(self, frame: Any) -> None:
        """Decrement ``queued_bytes`` for a frame consumed from the queue.

        Size accounting is the exact mirror of :meth:`put` (``len(frame)``
        for non-STOP frames). ``STOP`` is a control sentinel that ``put``
        never adds to the byte ledger, so callers must not ``ack`` it.
        """
        if frame is STOP:
            return
        size = len(frame)
        self.queued_bytes = max(0, self.queued_bytes - size)

    def _clear_queue(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.queued_bytes = 0


class TokenSubscriberCapacityError(Exception):
    """Raised when token-stream admission would exceed the cap (design §6).

    ``code`` is ``sse_token_subscriber_limit``; ``limit`` / ``current`` are
    surfaced on the wire (503 body) and via the metrics endpoint. Independent
    ledger — does NOT reuse the control-plane
    :class:`hub.SubscriberCapacityError` codes (those key off
    ``MAX_TOTAL_SUBSCRIBERS``; this one off ``token_stream_max_subscribers``).
    """

    def __init__(self, code: str, *, limit: int, current: int) -> None:
        self.code = code
        self.limit = limit
        self.current = current
        super().__init__(f"{code}: current={current}, limit={limit}")


class TokenStreamRegistry:
    """Independent admission ledger for token-stream subscribers (design §6).

    Own budget — does NOT consume ``HubRegistry.MAX_TOTAL_SUBSCRIBERS``
    (design §6: token subscribers carry their own
    ``token_stream_max_subscribers`` cap; worst case
    ``8 × 512KiB queues + 8MiB accumulator = 12MiB``).

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

    def subscribe(self, sid: str) -> TokenSubscriber:
        """Admit one token subscriber for ``sid`` under the cap + handshake.

        Order (all synchronous, no ``await`` → no interleaving with another
        coroutine):

        1. cap check — else raise :class:`TokenSubscriberCapacityError`
           (caller maps to 503 ``sse_token_subscriber_limit``).
        2. ensure the single upstream ``/global/event`` is connected
           (design §5.2: ``registry.get_global().ensure_upstream()``) and
           cancel any armed registry grace-removal (NB-B1, Stage-B TODO).
        3. start the token flush loop (first-attach lifecycle, NB-C4 — the
           current production path is ingest-only until the first subscriber
           arrives; this makes the flush loop actually run).
        4. construct the :class:`TokenSubscriber`.
        5. :meth:`TokenStreamHub.attach_subscriber` runs the §5.5 handshake
           (server.connected → flush_sid → snapshot → enter fanout).
        6. increment the ledger.
        """
        if self.total_subscribers >= self.max_subscribers:
            self.rejected_total += 1
            raise TokenSubscriberCapacityError(
                "sse_token_subscriber_limit",
                limit=self.max_subscribers,
                current=self.total_subscribers,
            )
        # Upstream lifecycle: ensure connected + cancel grace removal (NB-B1).
        if self.hub_registry is not None:
            hub = self.hub_registry.get_global()
            self.hub_registry.cancel_pending_removal()
            hub.ensure_upstream()
        # Token flush loop (idempotent start; first-attach lifecycle).
        self.token_hub.start()
        sub = TokenSubscriber(
            session_id=sid,
            metrics=self.token_hub._metrics,
            queue_items=self.queue_items,
            buffer_bytes=self.buffer_bytes,
            max_frame_bytes=self.max_frame_bytes,
        )
        # §5.5 handshake (server.connected first, then flush_sid → snapshot
        # → enter fanout). Implemented in Stage C attach_subscriber.
        self.token_hub.attach_subscriber(sid, sub)
        self.total_subscribers += 1
        return sub

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
        if not th.has_subscriber(sub.session_id, sub):
            return
        th.detach_subscriber(sub.session_id, sub)
        self.total_subscribers -= 1
        if self.total_subscribers < 0:
            # Defensive: should never happen given the membership guard.
            self.total_subscribers = 0
        if self.total_subscribers == 0:
            th.stop()
        # B-D1: symmetric arm. No-op while any consumer (control OR token)
        # remains; arms the registry grace-removal on the true last-detach.
        if self.hub_registry is not None:
            self.hub_registry.maybe_arm_grace_if_idle()

    def snapshot_token_metrics(self) -> dict[str, Any]:
        """``sse.tokenStream.*`` block for the metrics endpoint (design §7).

        S-3a additive keys: ``gzipRawBytesTotal``, ``gzipCompressedBytesTotal``,
        ``flushDurationMsTotal``, ``flushTicksTotal``, ``maxSubscriberQueueDepth``.
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
