"""Token-stream subscriber wiring + fanout/delivery helpers.

Split from :mod:`oc_slimapi.sse.tokenstream.hub` (F-301 five-module split,
pure move — zero behaviour change). Module-level frame-eligibility helpers
(``_V4_INELIGIBLE_FRAME_PREFIX`` / ``_v4_frame_eligible`` /
``_events_token_frame``) live HERE (``hub.py`` re-exports them for import
compatibility).
"""
from __future__ import annotations

from typing import Any, Callable

from ...config import TOKEN_REMOVED_MESSAGES_TTL_MS
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
    _connected_frame,
    _delta_frame,
    _heartbeat_frame,
    _message_removed_frame,
    _now_ms,
    _resync_frame,
    _snapshot_frame,
    _truncated_frame,
    sse_frame,
)


logger = get_logger(__name__)


# rev-gate R2 BLOCKER-1: frames whose SSE event name belongs to the
# ``message.part.snapshot`` family (``snapshot{done:false}`` handshake
# pre-fill, ``snapshot{done:true}`` terminal marker from :meth:`finish_part`,
# ``snapshot{truncated:true}`` cap marker from :meth:`_truncate_part_for_all`)
# are **never eligible for the v4 wire** — the frozen v4 protocol (v4-contract
# §7 / design-v4-sse-replay) mandates the server NEVER sends snapshot frames:
# state alignment is done by the client via HTTP full fetch after resync.
_V4_INELIGIBLE_FRAME_PREFIX = b"event: message.part.snapshot\n"


def _v4_frame_eligible(frame: bytes) -> bool:
    """v4 wire frame eligibility (rev-gate R2 BLOCKER-1).

    Structural check at the token fanout choke point: a frame carrying the
    ``message.part.snapshot`` event name must NOT

    * be written to the ReplayLog,
    * consume a per-domain seq (no holes are created — the frame simply
      never enters the sequence),
    * be delivered to a v4 subscriber (live fanout, first connect, or
      replay — it is absent from the log, so it can never be replayed).

    Only v3 subscribers receive it (byte-identical v3 wire, zero change).
    The check is on the serialized frame prefix so EVERY fanout path is
    covered regardless of which call site constructed the frame.
    """
    return not frame.startswith(_V4_INELIGIBLE_FRAME_PREFIX)


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
    # Subscribe fanout bookkeeping (§5.5 handshake, §5.7 stream-perspective)
    # ------------------------------------------------------------------

    def attach_subscriber(self, sid: str, sub: Any, wire_v4: bool = False) -> None:
        """Stage D's ``TokenSubscriber`` calls this on HTTP connect.

        **v4 fork (rev-gate BLOCKER-1, first)**: ``wire_v4=True`` runs the
        NO-prefill handshake — the connection joins the fanout directly:

        * ``server.connected`` is **suppressed** — it is not in the frozen
          no-``id:`` control-frame set (meta/resync/heartbeat only), and a
          connection-local frame must not bypass the ReplayLog;
        * historical ``_removed_messages`` tombstones are **not pre-filled**
          — reconnecting clients get them from the ReplayLog WITH their
          ``id:`` exactly once (REPLAY-012; the v3 pre-fill would
          double-send alongside the log replay);
        * live-part ``message.part.snapshot`` pre-fill is **suppressed** —
          the v4 protocol NEVER sends server-originated snapshot frames
          (state alignment after resync is a client HTTP full fetch);
        * no handshake frames are appended to the ReplayLog either —
          connection-private frames must not advance the shared per-sid
          sequence (would pollute other subscribers and future replays).

        Residual ``_pending`` for this sid is left to the regular flush
        loop: the sub is already in ``_subs_by_sid``, so the next flush
        publishes those frames through the normal logged+stamped path —
        nothing is missed and nothing is double-delivered.

        The v3 path below is **byte-identical unchanged** (rev-gate
        condition 8: the frozen handshake order server.connected →
        tombstones → flush_sid → live snapshot → fanout).

        Implements the §5.5 handshake (C2 no-double-count) in strict order
        with NO await window between steps — flush_loop cannot interleave
        a flush mid-handshake and double-count the pending window:

        1. ``server.connected{sessionID}`` — first frame on the wire.
        2. **message.removed replay** (Stage B v0.6 §P.3, MAJOR 4 方案 C):
           replay all un-expired ``message.removed`` tombstones for this
           sid (sorted by timestamp) so a client that attaches AFTER a
           removal (or reconnects post-upstream-loss) learns about it.
           The new sub is NOT yet in ``_subs_by_sid`` so we deliver
           directly via ``sub.put``.
        3. :meth:`flush_sid` — existing subscribers for this sid receive
           any residual ``_pending`` as ``delta`` frames. The new sub is
           NOT yet in ``_subs_by_sid`` so it does not receive them.
        4. For each active text LivePart for this sid (sorted by key):
           emit ``snapshot{done:false}`` with the FULL accumulated text
           (``"".join(chunks)``) to the new sub. Because accumulation is
           decoupled from subscribers (B1), this snapshot has no gap. C6:
           if the snapshot frame exceeds ``max_frame_bytes`` the sub
           receives ``snapshot{truncated:true}`` instead and the part is
           dropped (via :meth:`_emit_snapshot_or_truncated`).
        5. Add the sub to ``_subs_by_sid[sid]`` — only NOW does it enter
           the fanout for future deltas / markers.

        rev-ogpt CRITICAL 3: steps 1–4 run inside ``sub.begin_handshake()
        → finally: sub.end_handshake()`` so the subscriber's T3 overflow
        guard is bypassed for the pre-fill (a legitimately large tombstone
        batch or snapshot must NOT trigger ``subscriber_backpressure``
        disconnect before the sub even enters the fanout). After
        ``end_handshake`` we check ``sub.closed`` — if the sub was already
        closed (e.g. prior oversized frame, or a concurrent disconnect
        armed before ``begin_handshake``), we exit WITHOUT registering to
        fanout and WITHOUT incrementing the registry's subscriber count
        (the caller, :meth:`TokenStreamRegistry.subscribe`, conditionally
        increments on return — but we cannot back-charge it from here;
        instead :meth:`subscribe`'s post-attach check detects
        ``sub.closed`` and calls :meth:`_rollback_failed_attach` to
        symmetrically undo the flush-loop start and GlobalHub grace
        re-arm, then raises a 503 error — no generator is created).

        §5.7 completion alignment: the stream-perspective ``done:true``
        marker and the digest → ``/since`` authoritative text are
        independent; the snapshot here is the "stream has caught up to
        the accumulated state" baseline for this subscriber.
        """
        # CRITICAL 3: handshake mode bypasses subscriber overflow so the
        # initial pre-fill always lands. Lane 3's TokenSubscriber owns
        # the begin/end API (``_in_handshake`` flag); we just bracket
        # the pre-fill with it.
        if wire_v4:
            # v4: NO prefill at all (see docstring). No handshake-mode
            # bracket either — there is nothing to overflow-guard. The
            # closed check mirrors the v3 exit: a sub that arrived closed
            # (prior disconnect / oversized frame) never enters fanout,
            # and TokenStreamRegistry.subscribe's post-attach check
            # rolls the flush-loop/grace side effects back symmetrically.
            # Stamp the sub's wire view HERE as well (idempotent with the
            # registry's pre-attach stamp) — the fanout eligibility
            # checks (_deliver_v3_only & co.) key off ``sub.wire_v4``,
            # so a direct-attach v4 sub must never carry the v3 default.
            sub.wire_v4 = True
            if sub.closed:
                return
            self._subs_by_sid.setdefault(sid, set()).add(sub)
            return
        sub.begin_handshake()
        try:
            # 1. server.connected first.
            sub.put(_connected_frame(sid))
            # 2. Stage B v0.6 §P.3: replay message.removed tombstones for
            # this sid (sorted by timestamp, oldest first). Filter out
            # expired entries (TTL 24h) so a stale tombstone never reaches
            # a new sub.
            now_ms = _now_ms()
            cutoff = now_ms - TOKEN_REMOVED_MESSAGES_TTL_MS
            for (r_sid, r_mid), ts in sorted(
                self._removed_messages.items(), key=lambda x: x[1],
            ):
                if r_sid != sid:
                    continue
                if ts < cutoff:
                    continue  # expired — skip (will be cleaned up by ttl_sweep).
                sub.put(_message_removed_frame(r_sid, r_mid))
            # 3. flush_sid drains pending for EXISTING subscribers.
            self.flush_sid(sid)
            # 4. snapshot each active LivePart for this sid (sorted for determinism).
            for key in sorted(k for k in self.live_parts if k[0] == sid):
                live = self.live_parts[key]
                text = "".join(live.chunks)
                self._emit_snapshot_or_truncated(sub, key, text, done=False)
        finally:
            sub.end_handshake()
        # CRITICAL 3: if the sub was closed (prior disconnect, oversized
        # frame during handshake, etc.) do NOT register to fanout. The
        # registry must not count it as an active subscriber.
        if sub.closed:
            return
        # 5. enter fanout.
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
        if replay is None:  # defensive — callers gate on `self._replay`
            return None
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
        the source, i.e. regardless of whether any (v4 or v3) subscriber
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
        if self._replay is None:
            return
        try:
            self._replay.write_barrier(token_domain(sid))
        except Exception:  # noqa: BLE001 — invalidation never fails on log errors
            logger.warning(
                "replay barrier write failed for sid %r (%s)", sid, why, exc_info=True
            )

    def _deliver_logged(self, sid: str, frame: bytes, id_line: bytes | None) -> int:
        """Deliver a (possibly replay-logged) frame to the sid's subscribers.

        v4 subscribers (``sub.wire_v4``) receive ``id_line + frame``; v3
        subscribers the raw ``frame`` (byte-identical zero-change rule).
        Returns the number of subscribers the frame was delivered to.
        """
        subs = self._subs_by_sid.get(sid)
        if not subs:
            return 0
        for sub in tuple(subs):
            sub.put(id_line + frame if (id_line is not None and sub.wire_v4) else frame)
        return len(subs)

    def _deliver_v3_only(self, sid: str, frame: bytes) -> int:
        """Deliver a v3-ONLY frame (snapshot family) to the sid's v3 subs.

        rev-gate R2 BLOCKER-1: ``message.part.snapshot`` frames are never
        v4-eligible. This is deliberately NOT ``_deliver_logged(...,
        id_line=None)`` — that helper delivers the raw frame to v4
        subscribers too, which is exactly the bypass being closed here.
        v4 subscribers receive nothing (their state alignment is HTTP-based
        per the frozen contract); v3 subscribers get the frame byte-identical.
        Returns the number of v3 subscribers the frame was delivered to.
        """
        subs = self._subs_by_sid.get(sid)
        if not subs:
            return 0
        delivered = 0
        for sub in tuple(subs):
            if not getattr(sub, "wire_v4", False):
                sub.put(frame)
                delivered += 1
        return delivered

    def _fanout_frame(self, key: PartKey, frame: bytes) -> None:
        """Fan a v4-INELIGIBLE (snapshot-family) frame to v3 subs + count.

        4.12.0 修订六 B-1 scope narrowing: this choke point now serves
        ONLY the ``message.part.snapshot`` family (the ``done:true``
        terminal marker from :meth:`finish_part` — handshake / truncated
        snapshots reach subscribers via the per-sub emit helpers, not
        here). rev-gate R2 BLOCKER-1 semantics unchanged: the frame is
        NOT logged, consumes no seq, and is delivered to v3 subscribers
        ONLY via :meth:`_deliver_v3_only` (v4 state alignment is
        HTTP-based per the frozen contract).

        v4-ELIGIBLE frames (``message.part.delta``) MUST go through
        :meth:`_fanout_delta_frame` — their payload embeds the publish
        seq, so they cannot be pre-serialized at the call site anymore.
        A structurally eligible frame arriving here is a programming
        error: logged at ERROR (loud regression signal) and delivered
        v3-only rather than silently entering the un-logged path.
        """
        sid = key[0]
        if _v4_frame_eligible(frame):
            logger.error(
                "v4-eligible frame reached _fanout_frame (sid %r) — use "
                "_fanout_delta_frame; delivering v3-only", sid,
            )
        delivered = self._deliver_v3_only(sid, frame)
        self._metrics.flushed_frames_total += delivered

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
        publish attempt (a dropped frame wastes its revision — an
        accepted gap, same precedent as oversized-snapshot drops; a
        client comparing strict ``>`` still accepts the next delivery).

        No replay log wired (v3-only stacks / minimal test apps): the
        legacy shape — no payload ``seq``, no id line, raw fanout to
        every subscriber — keeps those stacks byte-identical.
        """
        sid = key[0]
        rev = self._next_part_revision(key)
        if self._replay is None:
            frame = _delta_frame(key, text, part_revision=rev)
            delivered = self._deliver_logged(sid, frame, None)
        else:
            published = self._publish_seq_frame(
                sid,
                lambda seq: _delta_frame(key, text, part_revision=rev, seq=seq),
            )
            if published is None:
                return  # dropped + rolled back + counted (B-1).
            id_line, frame = published
            delivered = self._deliver_logged(sid, frame, id_line)
        self._metrics.flushed_frames_total += delivered

    def _fanout_message_removed(self, sid: str, mid: str) -> None:
        """Fan a ``message.removed`` frame to every subscriber of ``sid``.

        Stage B v0.6 §P.2 (MAJOR 4 方案 C): the live fanout half of
        ``on_message_removed``. The replay half is handled by
        ``_removed_messages`` + the handshake replay in
        :meth:`attach_subscriber`.

        B3b-2: the tombstone is appended to the sid's replay domain with
        :data:`FRAME_KIND_TOMBSTONE` — a reconnecting client that missed
        the live frame replays it WITH its ``id:`` (it consumes a seq
        exactly like a business frame, keeping the ID sequence
        hole-free; REPLAY-012).

        4.12.0 修订六 B-1: the tombstone rides the same atomic
        reserve→encode→append path (payload embeds the seq); a publish
        failure drops it + rolls the seq back + counts (the bounded
        ``_removed_messages`` handshake queue still informs later v3
        attaches even when the replay publish failed).
        """
        if self._replay is None:
            frame = _message_removed_frame(sid, mid)
            delivered = self._deliver_logged(sid, frame, None)
        else:
            published = self._publish_seq_frame(
                sid,
                lambda seq: _message_removed_frame(sid, mid, seq=seq),
                kind=FRAME_KIND_TOMBSTONE,
            )
            if published is None:
                return  # dropped + rolled back + counted (B-1).
            id_line, frame = published
            delivered = self._deliver_logged(sid, frame, id_line)
        self._metrics.flushed_frames_total += delivered

    def _fanout_resync(self, sid: str, reason: str) -> None:
        """Fan ``resync{reason, sessionID}`` to every subscriber of sid.

        ROUTE-PRIVATE control-frame path (4.12.0 修订六 B-2 / rev-2 修正
        3): the ONLY reasons legal here are the frozen four
        (:data:`V4_RESYNC_REASONS`) plus v3-only lifecycle reasons
        (``session_idle`` …). The frame carries NO ``id:`` line, NO
        payload ``seq``, and is NEVER appended to the ReplayLog — a
        reconnecting client re-derives it from the classification
        protocol, never from the window.

        rev-gate R3 BLOCKER-1: a v4 subscriber facing a NON-frozen reason
        (``session_idle`` via the pending session-resync batch) is
        TERMINATED instead — :meth:`TokenSubscriber.terminate` suppresses
        the out-of-domain frame on v4 wires (STOP only; the disconnect is
        the observable signal, recovery = Last-Event-ID reconnect →
        ReplayLog replay or a frozen-reason resync). v3 subscribers keep
        the frozen ``resync{reason}`` frame, byte-identical.

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
            if getattr(sub, "wire_v4", False) and reason not in V4_RESYNC_REASONS:
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

        With a replay log wired the frame rides the B-1 atomic
        reserve→encode→append path: it consumes a seq, embeds it in the
        payload, lands in the ReplayLog (``FRAME_KIND_BUSINESS``), and is
        delivered to v4 subscribers WITH its ``id:`` line / v3 subscribers
        raw (payload seq visible on both wires — additive JSON field).
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
          BOTH wires now carry the reason frame (v4: resync + STOP; v3:
          resync + STOP) instead of a bare STOP (round-4 Blocking 1:
          the termination path aligns with the persistent marker, which
          remains the primary mechanism covering zero-subscriber and
          future first-connect scenarios).

        Recovery is the client's reconnect → classification protocol.
        Residual risk: if the log is so broken that BOTH the barrier and
        the flag write fail, a reconnecting client may briefly re-enter
        live mode on a stale view of the evicted part; the next digest /
        part update HTTP fetch corrects it. That is strictly better than
        the guaranteed-silent permanent divergence of the drop path.

        No replay log wired (v3-only stacks): the frame is fanned raw to
        every subscriber (v3-mirror degrade — the same no-log shape the
        delta path uses); nothing is terminated.
        """
        if reason not in REPLAYABLE_RESYNC_REASONS:
            raise ValueError(
                f"reason {reason!r} is not a replayable business resync "
                f"(allowed: {sorted(REPLAYABLE_RESYNC_REASONS)}); use "
                "_fanout_resync for route-private control resyncs"
            )
        if self._replay is None:
            self._deliver_logged(sid, _resync_frame(sid, reason), None)
            return
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

    def _emit_snapshot_or_truncated(
        self, sub: Any, key: PartKey, text: str | None, done: bool
    ) -> None:
        """Per-sub snapshot emit with C6 per-frame size check.

        If the snapshot frame fits ``max_frame_bytes``: deliver it. If not:
        trigger :meth:`_truncate_part_for_all` (which fans
        ``snapshot{truncated:true}`` to every EXISTING subscriber of the sid
        + drop_part) and — iff this sub was NOT in the fanout set (handshake
        path) — deliver the truncated frame directly. The ``in_fanout`` check
        prevents double-delivery when this helper is called on a sub that's
        already in ``_subs_by_sid`` (e.g. a direct emit after attach).

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): the snapshot consumes
        its own revision via :meth:`_next_part_revision`. If oversized,
        ``_truncate_part_for_all`` consumes the NEXT revision for the
        truncated frame — the snapshot's revision is "wasted" (the frame
        was never delivered) but that is the only correct option: the
        snapshot revision is part of the frame payload, so we cannot
        peek the size without consuming. The wasted revision is simply
        a gap in the per-frame sequence; clients using strict ``>``
        accept the truncated frame because it carries a strictly greater
        revision than the previous delivery.
        """
        if getattr(sub, "wire_v4", False):
            # rev-gate BLOCKER-1 principle: v4 subscribers never receive
            # the per-sub snapshot/truncated DIRECT emits — they bypass
            # the ReplayLog (no ``id:``, no seq). An oversized part still
            # goes through :meth:`_truncate_part_for_all`, whose fanout
            # delivers the truncated frame via ``_deliver_v3_only``
            # (budgets.py:141-147): v3 subscribers only, NOT replay-logged
            # and consuming no seq — memory bounding identical to v3; the
            # v4 face just never sees the frame (state alignment is
            # HTTP-based). The size probe omits the revision field, so
            # the v3/v4 truncate boundary can differ by the revision's
            # digit width (~10 bytes) — internal-only.
            probe = _snapshot_frame(key, text, done)
            if len(probe) > self._max_frame_bytes:
                self._truncate_part_for_all(key, done)
            return
        rev = self._next_part_revision(key)
        frame = _snapshot_frame(
            key, text, done, part_revision=rev,
        )
        if len(frame) <= self._max_frame_bytes:
            sub.put(frame)
            return
        # Oversized → C6 backstop. _truncate_part_for_all fans truncated to
        # EXISTING subs + drop_part. If THIS sub is already in the fanout
        # set, the fanout just delivered to it — no direct put needed. The
        # handshake path (sub not yet in fanout) needs the direct put.
        sid = key[0]
        in_fanout = sub in self._subs_by_sid.get(sid, ())
        # _truncate_part_for_all consumes its own revision (strictly
        # greater than the wasted snapshot revision above) and returns
        # it (or None if the part was already disabled — no-op).
        trunc_rev = self._truncate_part_for_all(key, done)
        if not in_fanout and trunc_rev is not None:
            sub.put(_truncated_frame(key, done, part_revision=trunc_rev))

    def _emit_snapshot_or_truncated_nodrop(
        self, sub: Any, key: PartKey, text: str | None, done: bool
    ) -> None:
        """Per-sub snapshot emit for eviction re-snapshot of the **current key**
        (``skip_key``).  ***Never*** calls ``_truncate_part_for_all`` /
        ``drop_part`` — an outer caller (e.g. ``_reserve`` / ``_start_part``,
        which invoked ``_evict_part_for_memory``) holds a stale
        ``live`` reference for this key; dropping it mid-iteration would cause
        ``_total_live_bytes`` drift and orphan deltas (O1 invariant).

        * If the snapshot frame fits ``self._max_frame_bytes``: deliver it
          directly (animation preserved).
        * If the frame is oversized: deliver ``snapshot{truncated:true, done}``
          to **this subscriber only**, but **keep the LivePart intact** (no
          ``drop_part``).  The client clears its local accumulator for this
          part on receipt of ``truncated`` and stops appending further deltas
          — subsequent deltas from the server become orphan on the client side
          (lost).  Animation is unrecoverable (blank until ``/since``
          re-fetch).  This is an acceptable trade-off: only oversized
          current-key snapshots lose animation; small current-key snapshots
          (the common case) preserve it.

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): the snapshot consumes
        its own revision. If oversized, the truncated frame consumes the
        NEXT revision (strictly greater). ``truncated_snapshots_total`` is
        incremented **per-sub** each time an oversized frame is emitted
        (one count per subscriber, unlike ``_truncate_part_for_all`` which
        counts once per part drop).
        """
        if getattr(sub, "wire_v4", False):
            # rev-gate BLOCKER-1 principle: v4 subscribers never receive
            # the per-sub direct snapshot/truncated emits (they bypass
            # the ReplayLog — no ``id:``, no seq). The nodrop path is
            # pure delivery (no state transition), so suppressing it for
            # v4 loses nothing protocol-visible: the v4 client keeps its
            # own accumulated text (every delta was delivered stamped)
            # and re-anchors via HTTP after any resync.
            return
        rev = self._next_part_revision(key)
        frame = _snapshot_frame(
            key, text, done, part_revision=rev,
        )
        if len(frame) <= self._max_frame_bytes:
            sub.put(frame)
            return
        # Oversized → deliver truncated frame directly (no _truncate_part_for_all).
        # Per-frame: consume the NEXT revision so strict-> clients accept it.
        trunc_rev = self._next_part_revision(key)
        sub.put(_truncated_frame(
            key, done, part_revision=trunc_rev,
        ))
        self._metrics.truncated_snapshots_total += 1
