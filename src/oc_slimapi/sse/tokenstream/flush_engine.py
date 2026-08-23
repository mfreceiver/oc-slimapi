"""Token-stream background flush engine + pending session-resync queue.

Split from :mod:`oc_slimapi.sse.tokenstream.hub` (F-301 five-module split,
pure move — zero behaviour change). Module-level ``_TTL_TICK_INTERVAL`` /
``_HEARTBEAT_TICK_INTERVAL`` live HERE — patch targets follow the move
(``hub.py`` re-exports for import compatibility).
"""
from __future__ import annotations

import asyncio
import time

from ...config import (
    TOKEN_FLUSH_SECONDS,
    TOKEN_HEARTBEAT_SECONDS,
    TOKEN_RESYNC_QUEUE_CAP,
)
from ...logging_config import get_logger
from .fanout import _events_token_frame
from .frames import _now_ms


logger = get_logger(__name__)


# Number of flush ticks between TTL sweeps (NB-B5: 60s cadence). Floored at 1
# so a misconfigured TOKEN_FLUSH_SECONDS still sweeps.
_TTL_TICK_INTERVAL = max(1, int(round(60.0 / TOKEN_FLUSH_SECONDS)))
# Number of flush ticks between heartbeats (§5.6 frame 6: 15s cadence).
_HEARTBEAT_TICK_INTERVAL = max(1, int(round(TOKEN_HEARTBEAT_SECONDS / TOKEN_FLUSH_SECONDS)))


class FlushEngineMixin:
    """Background flush lifecycle group + bounded pending-resync queue
    (moved verbatim from ``TokenStreamHub``).
    """

    # ------------------------------------------------------------------
    # Background flush lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background :meth:`flush_loop` task. Idempotent.

        The task self-cancels on unhandled exceptions (defensive: a dead
        flush loop would silently let ``_pending`` grow unbounded). Stage D
        wires this into the HTTP endpoint lifecycle (start on first
        subscriber, stop on last unsubscribe); for Stage C, tests call it
        directly to exercise the 100ms cadence + 60s TTL tick.

        INV-1 (P1-19): a supervisor ``done_callback`` is attached so a
        non-cancelled death (``flush()`` raising) while subscribers remain
        is logged at CRITICAL and the loop is rebuilt — otherwise deltas
        would silently stop forever and ``_pending`` would grow unbounded
        (the TTL sweep lives inside the same loop, so it dies too). The
        callback guards on ``self._flush_task is task`` so a stale task
        (replaced by a later ``start()``) is a no-op.
        """
        if self._flush_task is None or self._flush_task.done():
            flush_task = asyncio.create_task(self.flush_loop())
            flush_task.add_done_callback(self._on_flush_done)
            self._flush_task = flush_task
    def _on_flush_done(self, task: asyncio.Task) -> None:
        """INV-1 (P1-19): watchdog for the token flush loop.

        * cancelled task → return (teardown via :meth:`stop` / registry
          close — expected).
        * normal exit → return (``flush_loop`` is ``while True`` so this is
          unreachable; defensive).
        * exception death → if any token consumer remains (per-session subs
          OR events-token taps, via :meth:`has_consumers`), log CRITICAL and
          rebuild via :meth:`start` so deltas do not silently stop; else
          leave it dead (no consumers → no point rebuilding; the next
          first-attach :meth:`start` will create a fresh task).
        """
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except asyncio.InvalidStateError:
            return
        # Stale-task guard: a newer start() replaced the slot → no-op.
        if self._flush_task is not task:
            return
        if exc is None:
            return  # defensive — flush_loop is while True
        logger.critical(
            "token flush_loop died unexpectedly; %d per-session + %d events-token "
            "consumer(s) remain",
            self.subscriber_count, len(self.events_tap), exc_info=exc,
        )
        if self.has_consumers():
            self._flush_task = None
            self.start()
    def stop(self) -> None:
        """Cancel the background flush loop. Idempotent."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None
    async def flush_loop(self) -> None:
        """Drain ``_pending`` every ``TOKEN_FLUSH_SECONDS`` (§5.4) + TTL tick.

        Cadence:

        * every tick (100ms): :meth:`flush` (sorted-by-key drain +
          ``_pending_session_resinks`` drain).
        * every ``_TTL_TICK_INTERVAL`` (~600 ticks = 60s): :meth:`ttl_sweep`
          (NB-B5 — Stage B implemented it but never scheduled it; Stage C
          owns the scheduling).
        * every ``_HEARTBEAT_TICK_INTERVAL`` (~150 ticks = 15s): fan
          ``server.heartbeat`` to every subscriber (§5.6 frame 6).

        The loop swallows :class:`asyncio.CancelledError` re-raises (so
        :meth:`stop` and registry teardown work) but lets any other
        exception kill the task (a silent infinite retry would leak
        ``_pending``).
        """
        ttl_ticks = 0
        heartbeat_ticks = 0
        try:
            while True:
                await asyncio.sleep(TOKEN_FLUSH_SECONDS)
                self.flush()
                ttl_ticks += 1
                heartbeat_ticks += 1
                if ttl_ticks >= _TTL_TICK_INTERVAL:
                    ttl_ticks = 0
                    self.ttl_sweep(_now_ms())
                if heartbeat_ticks >= _HEARTBEAT_TICK_INTERVAL:
                    heartbeat_ticks = 0
                    self._fanout_heartbeat()
        except asyncio.CancelledError:
            raise
    def flush(self) -> None:
        """Drain ALL pending accumulators (sorted-by-key) → fan delta frames.

        Also drains the bounded ``_pending_session_resinks`` queue. Safe to
        call manually (tests) or from :meth:`flush_loop`. No-op when both
        ``_pending`` and the resync queue are empty.

        Stage E: each drained accumulator decrements ``_total_pending_bytes``
        by its pre-drain ``byte_count`` (captured before :meth:`DeltaAccumulator.drain`
        resets it to 0).

        S-3a: records wall-clock duration and tick count on each call.

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): each emitted delta
        frame consumes its own strictly-increasing revision via
        :meth:`_next_part_revision`. Multiple deltas across multiple
        flush windows of one part therefore get distinct revisions, so
        a client using strict ``>`` on ``partEventRevision`` accepts
        every frame (no false-dedup).
        """
        t0 = time.perf_counter()
        if self._pending:
            # §5.4: sorted(self._pending) for deterministic intra-tick order.
            for key in sorted(self._pending):
                acc = self._pending[key]
                if not acc.byte_count:
                    continue
                pending_bytes = acc.byte_count
                text = acc.drain()
                self._total_pending_bytes = max(0, self._total_pending_bytes - pending_bytes)
                if not text:
                    continue
                # 4.12.0 修订六 B-1: delta publication rides the atomic
                # reserve→encode→append path (payload embeds the seq).
                self._fanout_delta_frame(key, text)
                # L2-A (plan Task L2-A): curated-events token tap — every
                # completed (sid, mid, pid) window concat is fanned to the
                # ``/slimapi/events?tokens=1`` subscribers as a lean
                # ``{type:"token", ...}`` frame. Fires on the 100ms
                # flush_loop cadence (NOT the handshake-only flush_sid) so
                # events-token consumers see live tokens. Empty list → no-op
                # (zero per-flush overhead when no events-token subscriber).
                # The tap reuses ``Subscriber.put`` so the unchanged T3
                # backpressure guard applies (A-C4).
                if self.events_tap:
                    token_frame = _events_token_frame(key, text)
                    for tap in self.events_tap:
                        tap(token_frame)
            # Clean up empty accumulators (drain cleared their chunks).
            for key in [k for k, v in self._pending.items() if not v.chunks]:
                self._pending.pop(key, None)
        self._drain_pending_session_resyncs()
        t1 = time.perf_counter()
        self._metrics.flush_duration_ms_total += (t1 - t0) * 1000.0
        # O2: counts EVERY flush() invocation — flush_loop ticks AND
        # _check_pending_budget force-flushes — not just the 100ms loop ticks.
        self._metrics.flush_ticks_total += 1
    def flush_sid(self, sid: str) -> None:
        """Drain pending accumulators for ONE sid only (§5.5 handshake step 3).

        Called by :meth:`attach_subscriber` BEFORE snapshotting the new
        subscriber. Existing subscribers receive the residual deltas; the
        new subscriber (not yet in ``_subs_by_sid``) does NOT — so the
        subsequent snapshot reflects already-flushed state (C2: no
        double-count). This is the "clear-pending" half of the handshake.

        Stage E: decrements ``_total_pending_bytes`` for each drained
        accumulator (same pattern as :meth:`flush`).

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): delta frames consume
        their own strictly-increasing revision via
        :meth:`_next_part_revision`.
        """
        for key in sorted(k for k in self._pending if k[0] == sid):
            acc = self._pending[key]
            if not acc.byte_count:
                continue
            pending_bytes = acc.byte_count
            text = acc.drain()
            self._total_pending_bytes = max(0, self._total_pending_bytes - pending_bytes)
            if not text:
                continue
            # 4.12.0 修订六 B-1: delta publication rides the atomic
            # reserve→encode→append path (payload embeds the seq).
            self._fanout_delta_frame(key, text)
        for key in [k for k, v in self._pending.items() if k[0] == sid and not v.chunks]:
            self._pending.pop(key, None)
    # ------------------------------------------------------------------
    # Pending session-resync queue (NB-B2 bounded)
    # ------------------------------------------------------------------
    def _enqueue_session_resync(self, sid: str, reason: str) -> None:
        """Append a (sid, reason) to the pending resync queue (NB-B2 bounded).

        Stage B recorded these but never drained them (no subscribers, no
        flush loop). Stage C's :meth:`flush` drains them. NB-B2: the queue
        is capped at ``TOKEN_RESYNC_QUEUE_CAP`` — overflow drops the OLDEST
        entry (the newest reason is the most relevant; clients cold-start
        on any resync regardless of reason).
        """
        self._pending_session_resinks.append((sid, reason))
        while len(self._pending_session_resinks) > TOKEN_RESYNC_QUEUE_CAP:
            self._pending_session_resinks.pop(0)
    def _drain_pending_session_resyncs(self) -> None:
        """Fan all pending session resyncs (called by :meth:`flush`).

        Each ``(sid, reason)`` becomes a ``resync{reason, sessionID}`` frame
        to every subscriber of the sid. The batch is snapshotted + cleared
        before fanout so resyncs enqueued concurrently by ingest (same
        loop tick, no await) wait for the NEXT flush.
        """
        if not self._pending_session_resinks:
            return
        batch = self._pending_session_resinks[:]
        self._pending_session_resinks.clear()
        for sid, reason in batch:
            self._fanout_resync(sid, reason)
