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
from ..replay_wire import V4_RESYNC_REASONS
from .models import _TokenMetrics

if TYPE_CHECKING:
    from ..hub import HubRegistry


# ---------------------------------------------------------------------------
# _SubscriberQueue — one native-v4 runtime queue.
# ---------------------------------------------------------------------------


class _SubscriberQueue:
    """Bounded FIFO queue with one byte ledger for native-v4 delivery."""

    __slots__ = ("_queue", "runtime_bytes")

    def __init__(self, *, runtime_max_items: int) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=runtime_max_items)
        self.runtime_bytes = 0

    def put_runtime(self, frame: Any) -> None:
        self._queue.put_nowait(frame)
        if frame is not STOP:
            self.runtime_bytes += len(frame)

    def clear_runtime(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.runtime_bytes = 0

    def put_runtime_terminal(self, frame: Any) -> None:
        self._queue.put_nowait(frame)

    def ack_runtime(self, frame: Any) -> None:
        if frame is not STOP:
            self.runtime_bytes = max(0, self.runtime_bytes - len(frame))

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    async def get(self) -> Any:
        return await self._queue.get()

    def get_nowait(self) -> Any:
        return self._queue.get_nowait()


@dataclass(eq=False)
class TokenSubscriber:
    """One native-v4 token-stream client's bounded outbound queue."""

    session_id: str
    metrics: "_TokenMetrics"
    queue_items: int = 64
    buffer_bytes: int = 512 * 1024
    max_frame_bytes: int = DEFAULT_TOKEN_MAX_FRAME_BYTES
    id: str = field(default_factory=lambda: "tok_" + secrets.token_hex(4))
    closed: bool = False
    dropped_frames: int = 0
    forced_disconnects: int = 0

    queue: _SubscriberQueue = field(default=None)

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = _SubscriberQueue(runtime_max_items=self.queue_items)

    @property
    def queued_bytes(self) -> int:
        return self.queue.runtime_bytes

    def put(self, frame: Any) -> bool:
        """Enqueue ``frame`` under the T3 three-stage guard.

        Overflow clears the backlog and seals a single unaccounted STOP.
        Reconnect + ReplayLog owns recovery; no private resync is queued.
        """
        if self.closed:
            # Post-disconnect: silently drop. The terminal STOP already
            # queued by the overflow path is all the generator should see.
            return False
        if frame is STOP:
            # STOP is a runtime-only terminal sentinel; the bounded
            # runtime Queue always has room (overflow path clears it
            # first), so put_nowait never raises QueueFull here.
            self.queue.put_runtime(STOP)
            return True
        size = len(frame)
        if size > self.max_frame_bytes:
            # NB-C5: oversized frame drops without monopolising the byte
            # budget or disconnecting the subscriber.
            self.dropped_frames += 1
            self.metrics.dropped_frames_total += 1
            return False
        if (
            self.queue.qsize() < self.queue_items
            and self.queue.runtime_bytes + size <= self.buffer_bytes
        ):
            self.queue.put_runtime(frame)
            return True
        self.closed = True
        self.dropped_frames += 1
        self.forced_disconnects += 1
        self.metrics.dropped_frames_total += 1
        self.queue.clear_runtime()
        self.queue.put_runtime_terminal(STOP)
        return False

    def terminate(self, reason: str) -> None:
        """Seal the queue with control frames ahead of stale data.

        Frozen v4 reasons replace queued data with resync + STOP. Other
        lifecycle termination replaces queued data with STOP only. Clean
        server-side termination does not count as backpressure loss.

        Does NOT detach from the hub's fanout — :meth:`on_session_deleted`
        relies on the sub still being in ``_subs_by_sid`` so the
        generator's finally → :meth:`TokenStreamRegistry.unsubscribe`
        sees ``has_subscriber() == True`` and runs the normal cleanup
        path (detach + decrement + last-detach stop + grace arm).
        """
        self.closed = True
        self.queue.clear_runtime()
        if reason in V4_RESYNC_REASONS:
            self.queue.put_runtime_terminal(
                _resync_frame(self.session_id, reason)
            )
        self.queue.put_runtime_terminal(STOP)

    def ack(self, frame: Any) -> None:
        """Decrement the byte ledger for a frame consumed via ``queue.get()``.

        Size accounting is the exact mirror of :meth:`put` (``len(frame)``
        for non-STOP frames). ``STOP`` is a control sentinel that ``put``
        never adds to the byte ledger, so callers must not ``ack`` it (the
        no-op guard is defensive).
        """
        if frame is STOP:
            return
        self.queue.ack_runtime(frame)


class TokenSubscriberCapacityError(Exception):
    """Raised when token-stream admission would exceed the cap (design §6).

    ``code`` is ``sse_token_subscriber_limit``; ``limit`` / ``current``
    are surfaced on the wire (503 body) and via the metrics endpoint.
    Independent ledger — does NOT reuse the control-plane
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
    ``token_stream_max_subscribers`` cap.

    :meth:`subscribe` admission + attach run in ONE synchronous
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
        """Admit one native-v4 token subscriber for ``sid`` under the cap.

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
        5. :meth:`TokenStreamHub.attach_subscriber` joins live fanout.
        6. If the sub came back closed, do not increment the ledger and
           perform a complete rollback of the side effects from steps 3–4.
           GlobalHub grace re-arm). Without the rollback the flush loop
           keeps running for nobody, ``GlobalHub.run()`` parks forever on
           ``aiter_lines``, and the upstream ``/global/event`` connection +
           hub tasks leak (B-D1 ghost-subscriber resource leak).
        7. increment the ledger.

        INV-3 (P1-20): steps 3–5 are wrapped in ``try / except`` so ANY
        exception (QueueFull, serialization error, future attach logic
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
            raise TokenSubscriberCapacityError(
                "sse_token_subscriber_limit",
                limit=self.max_subscribers,
                current=self.total_subscribers,
            )
        self.total_subscribers += 1
        return sub

    def _rollback_failed_attach(self, sid: str, sub: TokenSubscriber) -> None:
        """MAJOR 5: complete cleanup after ``attach_subscriber`` failure.

        Mirrors the post-detach cleanup path in :meth:`unsubscribe`. The
        sub was constructed, upstream ensured, flush loop started, and
        GlobalHub grace cancelled — but the sub came back closed
        (for example, a defensive attach close or a future hub change).
        Without this rollback:

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

        ``maxSubscriberQueueDepth`` reflects the single runtime queue.
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
            # 4.12.0 修订六 B-1/B-2 additive
            "seqPublishFailuresTotal": m.seq_publish_failures_total,
            "seqResyncFailclosedTotal": m.seq_resync_failclosed_total,
            # S-3a additive
            "gzipRawBytesTotal": m.gzip_raw_bytes_total,
            "gzipCompressedBytesTotal": m.gzip_compressed_bytes_total,
            "flushDurationMsTotal": m.flush_duration_ms_total,
            "flushTicksTotal": m.flush_ticks_total,
            "maxSubscriberQueueDepth": max_qdepth,
        }
