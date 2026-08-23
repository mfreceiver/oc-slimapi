"""Token-stream subscriber wiring + fanout/delivery helpers.

Split from :mod:`oc_slimapi.sse.tokenstream.hub` (F-301 five-module split).
The module owns native-v4 replay-backed fanout plus ``_events_token_frame``.
"""
from __future__ import annotations

from typing import Any, Callable

from ...logging_config import get_logger
from ..hub_types import (
    RESYNC_RECONNECT_NO_REPLAY,
    RESYNC_TOKEN_MEMORY_LIMIT,
    TOKEN_FRAME_TYPE,
)
from ..replay_log import (
    FRAME_KIND_BUSINESS,
    FRAME_KIND_TOMBSTONE,
    token_domain,
)
from ..replay_wire import V4_RESYNC_REASONS, sse_id_line
from .frames import (
    PartKey,
    _delta_frame,
    _heartbeat_frame,
    _message_removed_frame,
    _resync_frame,
    sse_frame,
)


logger = get_logger(__name__)


# 4.12.0 修订六 B-2 (rev-2 条款 1 + 修正 3): the resync reason domain is
# now TWO explicitly separated sets — wire-visible unions exist for tests,
# but the implementation branches on the specific subset:
#
# * :data:`V4_RESYNC_REASONS` (unchanged, hub_types) — ROUTE-PRIVATE
#   control resyncs (epoch_changed / replay_expired / replay_gap /
#   reconnect_no_replay). No ``id:`` line, no payload ``seq``, NEVER
#   appended to the ReplayLog, emitted by the reconnect classification
#   path (routes) and :meth:`FanoutMixin._fanout_resync`.
#
# * :data:`REPLAYABLE_RESYNC_REASONS` (below) — REPLAYABLE business
#   resyncs. Currently EXACTLY ``token_memory_limit``: published through
#   the B-1 atomic reserve→encode→append path (id line + payload seq +
#   ReplayLog entry, consumes a seq), replayable on reconnect.
#
# The two sets are disjoint BY CONSTRUCTION here. ``token_memory_limit``
# must NEVER be routed through :meth:`_fanout_resync` (that would make it
# an id-less, un-logged control frame — the exact regression this
# separation forbids), and no frozen-four reason may ever be routed
# through :meth:`_fanout_replayable_resync` (ValueError guard below).
REPLAYABLE_RESYNC_REASONS = frozenset({RESYNC_TOKEN_MEMORY_LIMIT})


def _events_token_frame(key: PartKey, text: str) -> bytes:
    """Curated-events token frame (L2-A, ``/slimapi/events?tokens=1``).

    A lean projection distinct from the per-session stream's
    :func:`_delta_frame`: ``{type:"token", sessionID, messageID, partID,
    delta}`` with NO ``partEventRevision`` and NO ``directory``. SessionID is
    globally unique in single-user T3, so ``directory`` is redundant here;
    authoritative part-revision / full-text tracking stays on the per-session
    stream and ``/messages/{sid}`` (events token frames are the animation
    layer only). No ``event:`` name — the curated-events client dispatches on
    ``data.type`` (mirrors the raw IMMEDIATE passthrough style).
    """
    return sse_frame({
        "type": TOKEN_FRAME_TYPE,
        "sessionID": key[0],
        "messageID": key[1],
        "partID": key[2],
        "delta": text,
    })


class FanoutMixin:
    """Subscriber ledger (attach/detach/has_subscriber) + fanout/delivery
    helpers + read-only properties (moved verbatim from ``TokenStreamHub``).
    """

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def subscriber_count(self) -> int:
        """Active token subscribers across all sids.

        Stage D wires real per-session subscribers via
        :meth:`attach_subscriber`; until then this counts whatever the
        tests have attached. :meth:`GlobalHub.has_consumers` reads this to
        decide whether to keep the upstream connection alive across the
        grace window (§16-B ``has_consumers() 贯穿所有 grace 路径``).

        Stage D's HTTP ``subscribe`` MUST additionally cancel the registry's
        pending ``_removal_task`` so a grace-armed hub does not tear down an
        active token stream — that wiring is deferred to Stage D because it
        needs the live ``HubRegistry`` reference (NB-B1).
        """
        return sum(len(subs) for subs in self._subs_by_sid.values())

    def has_consumers(self) -> bool:
        """True while ANY token consumer remains.

        Unified liveness predicate for the flush-loop watchdog
        (:meth:`_on_flush_done`): per-session token subscribers
        (``_subs_by_sid`` via :attr:`subscriber_count`) **OR** curated-events
        token taps (``events_tap``, i.e. ``/slimapi/events?tokens=1``
        subscribers).

        The events-token ledger (``TokenStreamRegistry.events_tokens``) is
        kept in lockstep with ``events_tap`` — attach/detach add/remove one
        ``put`` per subscriber — so a non-empty ``events_tap`` is exactly
        "events ledger non-empty" (no parallel counter to drift). An
        events-only stream (zero per-session subs) therefore keeps the loop
        alive across an abnormal death: with the old
        ``subscriber_count > 0`` check the loop would never rebuild and
        token frames would stop forever while the connection stayed up.
        """
        return self.subscriber_count > 0 or len(self.events_tap) > 0
    @property
    def orphan_deltas(self) -> int:
        """Cumulative count of orphan ``message.part.delta`` events (C3)."""
        return self._metrics.orphan_deltas
    @property
    def flushed_frames_total(self) -> int:
        return self._metrics.flushed_frames_total
    @property
    def dropped_frames_total(self) -> int:
        return self._metrics.dropped_frames_total
    @property
    def truncated_snapshots_total(self) -> int:
        return self._metrics.truncated_snapshots_total
    @property
    def token_memory_limit_total(self) -> int:
        return self._metrics.token_memory_limit_total
    # ------------------------------------------------------------------
    # Subscribe fanout bookkeeping (§5.7 stream-perspective)
    # ------------------------------------------------------------------

    def attach_subscriber(self, sid: str, sub: Any) -> None:
        """Join one native-v4 subscriber directly to live fanout.

        Historical state is recovered only through ReplayLog replay and
        authoritative HTTP alignment; there is no connection-private prefill.
        """
        if sub.closed:
            return
        self._subs_by_sid.setdefault(sid, set()).add(sub)

    def detach_subscriber(self, sid: str, sub: Any) -> None:
        """Remove a subscriber from the sid's fanout set. Idempotent.

        Stage D's HTTP disconnect path calls this. Does NOT retire any
        LiveParts — accumulation is decoupled from subscribers (B1), so a
        departing subscriber leaves the part accumulating for any
        remaining (or future) subscribers.
        """
        subs = self._subs_by_sid.get(sid)
        if subs is None:
            return
        subs.discard(sub)
        if not subs:
            self._subs_by_sid.pop(sid, None)

    def has_subscriber(self, sid: str, sub: Any) -> bool:
        """True iff ``sub`` is currently in ``sid``'s fanout set (NB-D1).

        Used by :meth:`TokenStreamRegistry.unsubscribe` for TRUE idempotency:
        a sub already detached (or never attached) must NOT decrement the
        registry ledger again. Mirrors the ``subscriber not in
        hub.subscribers`` guard in :meth:`HubRegistry.unsubscribe`. Membership
        is identity-based (subscribers live in a ``set``), so a fresh
        ``TokenSubscriber`` with the same ``session_id`` is correctly reported
        as absent.
        """
        return sub in self._subs_by_sid.get(sid, ())
    # ------------------------------------------------------------------
    # Fanout helpers
    # ------------------------------------------------------------------

    def _publish_seq_frame(
        self,
        sid: str,
        build: Callable[[int], bytes],
        *,
        kind: str = FRAME_KIND_BUSINESS,
    ) -> tuple[bytes, bytes] | None:
        """Atomic reserve→encode→append for a v4-eligible business frame
        (4.12.0 修订六 B-1 / rev-1 B1 + rev-2 条款 1).

        Call order (all synchronous, ONE event-loop step — no interleaving
        publisher can observe the intermediate states):

        1. ``seq = self._replay.reserve_seq(token_domain(sid))`` —
           tentative allocation from the SAME sequence that mints replay
           SSE ids (same ``(epoch, token-domain)``);
        2. ``frame = build(seq)`` — serialize WITH the seq embedded as a
           payload ``seq`` field (the historical defect being fixed: the
           frame used to be serialized BEFORE the seq existed, so the
           payload could never carry it);
        3. ``append(..., seq=seq)`` — confirm into the ReplayLog (memory
           deque + byte bookkeeping, synchronous);
        4. only then does the caller fan out (v4 subscribers receive
           ``id: g:<epoch>:<seq>``-shaped lines whose last segment equals
           the payload ``seq`` by construction).

        Failure handling — the B-1 rule that REPLACES the historical
        degradation: on ANY failure in steps 1–3 the reservation is rolled
        back (:meth:`ReplayLog.rollback_seq` — the domain sequence stays
        hole-free; the next successful frame reuses the value), the frame
        is DROPPED (never fanned out un-logged / without its id), and
        ``seq_publish_failures_total`` is bumped. The old
        ``_replay_publish_token`` contract — "append failure degrades to
        delivering the raw frame with no id line" — is deliberately
        abolished: an un-logged frame on a v4 wire is indistinguishable
        from a lost frame after reconnect.

        Returns ``(id_line, frame)`` on success; ``None`` on failure
        (dropped + counted). Callers that need fail-closed semantics
        instead of drop-and-continue (B-2 eviction resync) branch on the
        ``None`` themselves.
        """
        replay = self._replay
        domain = token_domain(sid)
        seq: int | None = None
        try:
            seq = replay.reserve_seq(domain)
            frame = build(seq)
            entry = replay.append(domain, frame, kind=kind, seq=seq)
        except Exception:  # noqa: BLE001 — publish failure drops the frame
            if seq is not None and not replay.rollback_seq(domain, seq):
                logger.error(
                    "seq rollback refused for sid %r seq %s — domain "
                    "sequence carries a hole (structurally unreachable "
                    "under the synchronous-scope contract)", sid, seq,
                )
            self._metrics.seq_publish_failures_total += 1
            logger.warning(
                "replay publish failed for sid %r; frame dropped", sid,
                exc_info=True,
            )
            return None
        return sse_id_line(domain, replay.epoch, entry.seq), frame

    def _write_replay_barrier(self, sid: str, why: str) -> None:
        """Write a replay barrier for the sid's token domain (rev-gate R4).

        Called at server-side **state invalidation** sources whose resync
        is NOT itself replayable — session idle retire
        (:meth:`on_session_status`), session deletion
        (:meth:`on_session_deleted`), and the B-2 fail-closed path in
        :meth:`_fanout_replayable_resync` (replayable-resync publish
        failure after the state was already cleared) — UNCONDITIONALLY at
        the source, i.e. regardless of whether any subscriber
        is currently online. Rationale (v4-contract §7.2 window semantics
        / R4 BLOCKER-1): after the invalidation the accumulator state that
        produced the logged frames is gone, so a client reconnecting with
        ``Last-Event-ID == last_seq`` must NOT be judged up-to-date (it
        would enter live mode holding a残缺 part). The barrier makes any
        cursor ≤ watermark resolve to ``resync{reconnect_no_replay}`` →
        HTTP full alignment — the frozen recovery path. The still-open
        connection's own delivery stays the R3 semantics (silent STOP for
        non-frozen reasons); this barrier only governs RECONNECTS.

        4.12.0 修订六 B-2 NOTE — memory eviction
        (:meth:`_evict_part_for_memory`) NO LONGER writes a barrier: its
        alignment signal is now the replayable ``token_memory_limit``
        resync frame itself (in the log, at its own seq). A barrier would
        intercept every cursor ≤ watermark and the replayable resync
        (seq = watermark+1) could then never be replayed — the frame IS
        the R4 guarantee for that path.

        Degrade pattern: a log failure is logged and swallowed
        (invalidation must never fail).
        """
        try:
            self._replay.write_barrier(token_domain(sid))
        except Exception:  # noqa: BLE001 — invalidation never fails on log errors
            logger.warning(
                "replay barrier write failed for sid %r (%s)", sid, why, exc_info=True
            )

    def _deliver_logged(self, sid: str, frame: bytes, id_line: bytes) -> int:
        """Deliver one replay-confirmed native-v4 frame."""
        subs = self._subs_by_sid.get(sid)
        if not subs:
            return 0
        for sub in tuple(subs):
            sub.put(id_line + frame)
        return len(subs)

    def _fanout_delta_frame(self, key: PartKey, text: str) -> None:
        """Publish + fan one ``message.part.delta`` frame (B-1 primary path).

        4.12.0 修订六 B-1: the v4-eligible delta publication is
        reserve→encode→append→fanout — the frame bytes are serialized
        AFTER the tentative seq allocation so the payload carries
        ``seq`` (equal to the ``id:`` line's last segment on the v4
        wire). Only a confirmed append fans out; a publish failure drops
        the frame + rolls the seq back + bumps
        ``seq_publish_failures_total`` (no un-logged fanout — see
        :meth:`_publish_seq_frame`).

        The per-frame ``partEventRevision`` is consumed BEFORE the
        publish attempt (a dropped frame wastes its revision; a client
        comparing strict ``>`` still accepts the next delivery).

        """
        sid = key[0]
        rev = self._next_part_revision(key)
        published = self._publish_seq_frame(
            sid,
            lambda seq: _delta_frame(key, text, part_revision=rev, seq=seq),
        )
        if published is None:
            return
        id_line, frame = published
        delivered = self._deliver_logged(sid, frame, id_line)
        self._metrics.flushed_frames_total += delivered

    def _fanout_message_removed(self, sid: str, mid: str) -> None:
        """Fan a ``message.removed`` frame to every subscriber of ``sid``.

        The tombstone is appended to the sid's replay domain with
        :data:`FRAME_KIND_TOMBSTONE` — a reconnecting client that missed
        the live frame replays it WITH its ``id:`` (it consumes a seq
        exactly like a business frame, keeping the ID sequence
        hole-free; REPLAY-012).

        4.12.0 修订六 B-1: the tombstone rides the same atomic
        reserve→encode→append path (payload embeds the seq); a publish
        failure drops it + rolls the seq back + counts. No unlogged private
        cache or connection-prefill path exists.
        """
        published = self._publish_seq_frame(
            sid,
            lambda seq: _message_removed_frame(sid, mid, seq=seq),
            kind=FRAME_KIND_TOMBSTONE,
        )
        if published is None:
            return
        id_line, frame = published
        delivered = self._deliver_logged(sid, frame, id_line)
        self._metrics.flushed_frames_total += delivered

    def _fanout_resync(self, sid: str, reason: str) -> None:
        """Fan ``resync{reason, sessionID}`` to every subscriber of sid.

        ROUTE-PRIVATE control-frame path (4.12.0 修订六 B-2 / rev-2 修正
        3): the ONLY wire-visible reasons legal here are the frozen four
        (:data:`V4_RESYNC_REASONS`). The frame carries NO ``id:`` line, NO
        payload ``seq``, and is NEVER appended to the ReplayLog — a
        reconnecting client re-derives it from the classification
        protocol, never from the window.

        A subscriber facing a NON-frozen reason
        (``session_idle`` via the pending session-resync batch) is
        TERMINATED instead — :meth:`TokenSubscriber.terminate` suppresses
        the out-of-domain frame (STOP only; the disconnect is
        the observable signal, recovery = Last-Event-ID reconnect →
        ReplayLog replay or a frozen-reason resync).

        4.12.0 修订六 B-2: ``token_memory_limit`` is NO LONGER routed
        here — it became a REPLAYABLE business resync and must go through
        :meth:`_fanout_replayable_resync` (id line + payload seq +
        ReplayLog entry). Routing it back here would silently regress it
        to an id-less control frame.
        """
        subs = self._subs_by_sid.get(sid)
        if not subs:
            return
        frame: bytes | None = None
        for sub in tuple(subs):
            if reason not in V4_RESYNC_REASONS:
                sub.terminate(reason)
                continue
            if frame is None:
                frame = _resync_frame(sid, reason)
            sub.put(frame)

    def _fanout_replayable_resync(self, sid: str, reason: str) -> None:
        """Fan a REPLAYABLE business resync (B-2, 4.12.0 修订六).

        Value domain: EXACTLY :data:`REPLAYABLE_RESYNC_REASONS`
        (``token_memory_limit`` today). Anything else — including the
        frozen route-private four — is a programming error and raises
        :class:`ValueError` (the two reason sets must stay disjoint; see
        the REPLAYABLE_RESYNC_REASONS block comment above).

        The frame rides the B-1 atomic
        reserve→encode→append path: it consumes a seq, embeds it in the
        payload, lands in the ReplayLog (``FRAME_KIND_BUSINESS``), and is
        delivered to subscribers WITH its ``id:`` line.
        The stream does NOT terminate: subsequent frames for the sid
        (other parts / future new parts) keep publishing on the same
        sequence.

        🔴 fail-closed (rev-2 修正 1): this method is called AFTER the
        caller already cleared server-side state (eviction dropped the
        LivePart). A silent drop here would leave every ONLINE client
        running on a dead baseline with no signal ever arriving — live or
        on replay. So a publish failure does NOT degrade to drop+count:

        * bumps ``seq_resync_failclosed_total`` + ERROR log;
        * attempts a best-effort replay barrier (so a post-termination
          reconnect WITH any cursor ≤ watermark resolves to
          ``resync{reconnect_no_replay}`` → full HTTP alignment instead
          of replaying a stale window);
        * marks the domain's sticky invalidation flag
          (:meth:`ReplayLog.mark_invalidated`, round-4 Blocking 1) — the
          barrier cannot reach clients that reconnect with NO
          ``Last-Event-ID`` (a fresh domain's first-seq failure leaves
          them cursor-less) nor future first-connects after a
          zero-subscriber eviction; the flag forces every no-cursor
          connect until the domain's next successful publish;
        * TERMINATES every subscriber of the sid with
          ``reconnect_no_replay`` — a member of the frozen v4 domain, so
          native subscribers receive resync + STOP instead of a bare STOP
          (round-4 Blocking 1:
          the termination path aligns with the persistent marker, which
          remains the primary mechanism covering zero-subscriber and
          future first-connect scenarios).

        Recovery is the client's reconnect → classification protocol.
        Residual risk: if the log is so broken that BOTH the barrier and
        the flag write fail, a reconnecting client may briefly re-enter
        live mode on a stale view of the evicted part; the next digest /
        part update HTTP fetch corrects it. That is strictly better than
        the guaranteed-silent permanent divergence of the drop path.
        """
        if reason not in REPLAYABLE_RESYNC_REASONS:
            raise ValueError(
                f"reason {reason!r} is not a replayable business resync "
                f"(allowed: {sorted(REPLAYABLE_RESYNC_REASONS)}); use "
                "_fanout_resync for route-private control resyncs"
            )
        domain = token_domain(sid)
        published = self._publish_seq_frame(
            sid, lambda seq: _resync_frame(sid, reason, seq=seq),
        )
        if published is None:
            self._metrics.seq_resync_failclosed_total += 1
            logger.error(
                "replayable resync publish failed for sid %r (%s) after "
                "state eviction — failing closed: terminating %d "
                "subscriber(s) + best-effort replay barrier + sticky "
                "invalidation flag",
                sid, reason, len(self._subs_by_sid.get(sid, ())),
                exc_info=True,
            )
            self._write_replay_barrier(sid, reason)
            # Sticky invalidation marker (round-4 Blocking 1): covers
            # no-cursor reconnects and future first-connects — the
            # barrier alone cannot. Best-effort like the barrier: the
            # flag write is a plain in-memory set, but the log object
            # may already be in a broken state, so never let it raise.
            try:
                self._replay.mark_invalidated(domain)
            except Exception:  # noqa: BLE001 — best-effort, log already broken
                logger.error(
                    "sticky invalidation flag write failed for domain %r",
                    domain, exc_info=True,
                )
            for sub in tuple(self._subs_by_sid.get(sid, ())):
                sub.terminate(RESYNC_RECONNECT_NO_REPLAY)
            return
        id_line, frame = published
        self._deliver_logged(sid, frame, id_line)

    def _fanout_heartbeat(self) -> None:
        """Fan ``server.heartbeat{}`` to every token subscriber (§5.6 frame 6)."""
        if not self._subs_by_sid:
            return
        frame = _heartbeat_frame()
        for subs in self._subs_by_sid.values():
            for sub in tuple(subs):
                sub.put(frame)
