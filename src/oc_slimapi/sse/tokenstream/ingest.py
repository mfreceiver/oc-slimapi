"""Token-stream upstream-event ingest + retire/cleanup handlers.

Split from :mod:`oc_slimapi.sse.tokenstream.hub` (F-301 five-module split,
pure move — zero behaviour change).
"""
from __future__ import annotations

from typing import Any

from ...config import TOKEN_ACC_IDLE_MS, TOKEN_FLUSH_BYTES
from ..hub_types import (
    RESYNC_RECONNECT_NO_REPLAY,
    RESYNC_SESSION_DELETED,
    RESYNC_SESSION_IDLE,
    normalize_session_status,
)
from .frames import PartKey, _now_ms
from .models import DeltaAccumulator


# P1-21: FIFO cap on session-routing metadata to prevent unbounded growth.
# Aligned with GlobalHub._LAST_UPDATED_AT_BY_SID_MAX (same 10k pattern).
_SESSION_STATUS_MAX = 10_000


class IngestMixin:
    """Ingest event handlers (``on_*``) + retire/cleanup + terminal
    ``finish_part`` (moved verbatim from ``TokenStreamHub``).
    """

    # ------------------------------------------------------------------
    # Ingest (called from GlobalHub.publish)
    # ------------------------------------------------------------------
    def on_part_updated(
        self, props: dict[str, Any], part_revision: int | None = None,
    ) -> None:
        """Handle ``message.part.updated`` (design §5.3).

        ``text-start`` (``part.time.end is None``) creates a
        :class:`LivePart` regardless of subscribers. Repeated starts are
        idempotent (we never reset an existing accumulator: a
        middle ``updated`` frame must not clobber accumulated deltas).
        ``text-end`` triggers :meth:`finish_part` (Stage C: synchronous
        residual drain + retire). Non-text parts are recorded in
        ``_nontext_parts`` so their ``field:"text"`` deltas drop silently
        (C3).

        Stage B v0.6 §Q (per_part_revision independent): the
        ``part_revision`` parameter is **IGNORED** — kept in the signature
        for source back-compat with v0.5 callers. The token hub maintains
        ``_part_revisions[key]`` itself, decoupled from GlobalHub's
        part-level state (removed in lite-v2).

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): ``on_part_updated``
        does NOT bump ``_part_revisions`` itself — bumps happen lazily
        in each emit path via :meth:`_next_part_revision`. Each emitted
        delta frame gets a strictly increasing revision, so a client using
        strict ``>`` on ``partEventRevision`` accepts every delivery frame.

        rev-ogpt MAJOR 5: the structural / type / disabled / lifecycle
        checks below run BEFORE any code that could lead to a frame
        emission. Non-text parts, malformed events (missing ``time``),
        late text-starts for already-disabled keys, and late updates for
        retired messages NEVER create or consume a revision (they short-
        circuit before any emit path). v0.5 wrote the revision
        unconditionally on entry, leaving orphan revisions on every
        reject path and letting late disabled text-starts recreate stale
        watermarks.

        rev-ogpt CRITICAL 2: late ``message.part.updated`` for an
        already-removed message is dropped silently via the
        ``_retired_messages`` gate (no LivePart creation, no frame
        emission, no revision bump).
        """
        # Settled (§13.2 / E8): the live wire envelope for
        # message.part.updated uses camelCase keys — part.sessionID /
        # part.messageID / part.id (opencode v1.18.18
        # packages/schema/src/v1/session.ts part envelope). No snake_case
        # variants are accepted.
        part = props.get("part")
        if not isinstance(part, dict):
            return
        sid = part.get("sessionID")
        mid = part.get("messageID")
        pid = part.get("id")
        if not all(isinstance(x, str) and x for x in (sid, mid, pid)):
            return
        key: PartKey = (sid, mid, pid)
        # P1-22: deleted-sid gate — late part event for a deleted session → drop.
        if self._is_deleted_sid(sid):
            return
        # CRITICAL 2 gate: late update for a removed message → drop.
        if (sid, mid) in self._retired_messages:
            return
        # MAJOR 5: structural / type / disabled checks BEFORE any
        # code path that could emit a frame. v0.5 wrote
        # ``_part_revisions[key] = -1`` here unconditionally, leaving
        # orphan revisions on every reject path.
        t = part.get("time")
        if not isinstance(t, dict):
            return  # malformed → no revision, no LivePart
        if part.get("type") != "text":
            # C3: reasoning/tool-input part — record so its field:"text"
            # deltas drop silently. NEVER create a LivePart for these.
            self._remember_nontext(key)
            return  # non-text → no revision (MAJOR 5)
        # MAJOR 5: late text-start for an already-disabled key (post
        # truncate / too_large / eviction). The part is gone; do not
        # recreate a dead LivePart AND do not consume a revision.
        if self._is_disabled(key):
            return
        # ACCEPTED text part. Option B: NO revision bump here — bumps
        # happen lazily only when a delta is actually published.
        if t.get("end") is None:
            # NB1: type-guard the seed — part.get("text") may be a non-string
            # in a malformed / future upstream payload; an int/dict seed
            # would corrupt chunk-list byte accounting downstream.
            raw_seed = part.get("text")
            seed = raw_seed if isinstance(raw_seed, str) else ""
            # NB3 (relaxed): the disabled check above already handled
            # post-drop text-start. Here a LivePart may already exist
            # (repeated text-start) — we never clobber accumulated chunks.
            if key not in self.live_parts:
                self._start_part(key, seed)
            return
        # text-end → finish_part: synchronous drain(_pending) + retire (C1).
        raw_final = part.get("text")
        final_text = raw_final if isinstance(raw_final, str) else ""
        self.finish_part(key, final_text)
    def on_part_delta(self, props: dict[str, Any]) -> None:
        """Handle ``message.part.delta`` (design §5.3).

        Gates (in order): ``field == "text"``; key components non-empty;
        not in ``_nontext_parts`` (C3); not in ``_disabled_parts`` (C4);
        LivePart exists (orphan → silent drop + counter, NEVER resync,
        C3); LivePart not ended (C1, late delta after text-end). On pass,
        :meth:`_reserve` enforces the per-part + global memory budgets
        (Stage C); on budget pass, append the delta to both the LivePart
        chunk list and the flush window's :class:`DeltaAccumulator`.

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): the emitted delta
        frame consumes its OWN strictly-increasing revision via
        :meth:`_next_part_revision`. Multiple deltas across multiple
        flush windows therefore carry distinct revisions (0, 1, 2, ...)
        — a client using strict ``>`` on ``partEventRevision`` reliably
        accepts every delivery frame (no false-dedup).

        rev-ogpt CRITICAL 2: late delta for a removed message is dropped
        silently via the ``_retired_messages`` gate.
        """
        # Settled (§13.2 / E8): the live wire envelope for
        # message.part.updated carries camelCase top-level keys — field /
        # sessionID / messageID / partID (opencode v1.18.18
        # packages/schema/src/v1/session.ts text-delta event). No
        # snake_case variants are accepted.
        if props.get("field") != "text":
            return
        sid = props.get("sessionID")
        mid = props.get("messageID")
        pid = props.get("partID")
        if not all(isinstance(x, str) and x for x in (sid, mid, pid)):
            return
        # P1-22: deleted-sid gate — late delta for a deleted session → drop.
        if self._is_deleted_sid(sid):
            return
        # CRITICAL 2 gate: late delta for a removed message → drop.
        if (sid, mid) in self._retired_messages:
            return
        delta = props.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        key: PartKey = (sid, mid, pid)
        if self._is_nontext(key):
            return  # C3: reasoning/tool delta — silent drop.
        if self._is_disabled(key):
            return  # C4: post-truncate / post-too_large — silent drop.
        live = self.live_parts.get(key)
        if live is None:
            # C3: orphan (sidecar restarted mid-generation, missed the
            # text-start). Silent drop + counter; NEVER resync (a
            # per-token resync storm was the v2-rev failure mode).
            self._metrics.orphan_deltas += 1
            return
        if live.ended:
            return  # C1: late delta after text-end — drop.
        # Stage C: _reserve enforces per-part cap (TOKEN_PART_MAX_BYTES) +
        # global byte/count caps (TOKEN_LIVEPARTS_MAX_BYTES /
        # TOKEN_LIVE_PARTS_MAX). On failure the delta is dropped (and the
        # appropriate truncate/resync frame fanned).
        n = len(delta.encode("utf-8"))
        if not self._reserve(live, n, key):
            return
        live.chunks.append(delta)
        live.byte_count += n
        live.last_delta_ms = _now_ms()
        self._total_live_bytes += n
        acc = self._pending.setdefault(key, DeltaAccumulator())
        acc.append(delta)
        self._total_pending_bytes += n
        # §5.4: TOKEN_FLUSH_BYTES (4KiB) early-flush threshold — when a single
        # accumulator crosses the threshold, drain it IMMEDIATELY rather than
        # waiting for the 100ms tick. This bounds latency for high-volume
        # parts (a fast generator can produce >4KiB in 100ms) without
        # affecting low-volume parts (still 100ms-batched). Deltas ≤4KiB are
        # always safe on the wire (frame cap is 1MiB).
        if acc.byte_count >= TOKEN_FLUSH_BYTES:
            pending_bytes = acc.byte_count
            text = acc.drain()
            self._pending.pop(key, None)
            self._total_pending_bytes = max(0, self._total_pending_bytes - pending_bytes)
            if text:
                # 4.12.0 修订六 B-1: delta publication rides the atomic
                # reserve→encode→append path (payload embeds the seq).
                self._fanout_delta_frame(key, text)
        # Stage E (§16-C residual): global PENDING budget check. Runs AFTER
        # the per-key early-flush (which may have already popped this key's
        # accumulator). The pending budget is GLOBAL — many small
        # accumulators can collectively exceed the cap even when no single
        # one trips TOKEN_FLUSH_BYTES.
        self._check_pending_budget(key)
    # ------------------------------------------------------------------
    # message.removed tombstone (Stage B v0.6 §P.2, MAJOR 4 方案 C)
    # + message.part.removed routing (MAJOR 4)
    # ------------------------------------------------------------------
    def on_message_removed(self, sid: str, mid: str) -> None:
        """Handle upstream ``message.removed`` (Stage B v0.6 §P.2).

        Called by :meth:`GlobalHub.publish` when a ``message.removed``
        event arrives (flat props ``{sessionID, messageID}``). Order:

        1. **Atomic retire** (rev-ogpt CRITICAL 2): drop every LivePart /
           pending accumulator / tombstone / revision keyed by
           ``(sid, mid, *)`` and record the message in
           ``_retired_messages`` so late ``message.part.updated`` /
           ``.delta`` events cannot revive it. Byte gauges are
           decremented (floored at 0). Subscribers who process the
           ``message.removed`` frame therefore cannot subsequently see
           stale delta frames from a late part event.
        2. Fan ``message.removed`` to every current token subscriber of
           the session.
        The shared replay log is the sole historical tombstone source.
        """
        # CRITICAL 2: retire BEFORE fanout so subscribers cannot observe
        # stale state after the tombstone frame.
        self._retire_message(sid, mid)
        # Live fanout to current subscribers of this session.
        self._fanout_message_removed(sid, mid)
    def on_part_removed(self, sid: str, mid: str, pid: str) -> None:
        """Handle upstream ``message.part.removed`` (rev-ogpt MAJOR 4).

        Routed from :meth:`GlobalHub.publish` (control-plane fingerprint
        maintenance is unchanged there). Idempotently retires the part
        via :meth:`drop_part` — clears LivePart + pending + revision,
        adds the key to ``_disabled_parts`` so any later
        ``message.part.delta`` / residual ``message.part.updated`` for
        this key silently drops. Without this routing the part's
        LivePart / pending / revision would survive the upstream removal
        and continue emitting stale frames.

        CRITICAL 2 gate: no-op if the message is already retired (the
        message-level retire already dropped every part for this
        message).
        """
        # P1-22: deleted-sid gate — late part removal for a deleted session → drop.
        if self._is_deleted_sid(sid):
            return
        # CRITICAL 2 gate: message-level retire already dropped this part.
        if (sid, mid) in self._retired_messages:
            return
        if not (isinstance(pid, str) and pid):
            return  # malformed — defensive (GlobalHub.publish already guards)
        key: PartKey = (sid, mid, pid)
        # drop_part is idempotent: first call pops live_parts / _pending
        # / _part_revisions and adds to _disabled_parts; subsequent
        # calls return False. Both outcomes are correct here.
        self.drop_part(key)
    def _retire_message(self, sid: str, mid: str) -> None:
        """rev-ogpt CRITICAL 2: drop ALL token state for ``(sid, mid)``.

        Atomically clears every structure keyed by ``(sid, mid, *)``:
        ``live_parts``, ``_pending``, ``_nontext_parts``,
        ``_disabled_parts``, ``_part_revisions``. Byte gauges
        (``_total_live_bytes`` / ``_total_pending_bytes``) are
        decremented to floor 0. Records ``(sid, mid)`` in
        ``_retired_messages`` so late part events cannot revive the
        message.

        Called from :meth:`on_message_removed` BEFORE fanout. Scope is
        narrower than :meth:`_retire_session` (which clears the whole
        session) — this preserves other messages' parts in the same
        session.
        """
        # Live parts for this message.
        for key in [k for k in self.live_parts if k[0] == sid and k[1] == mid]:
            live = self.live_parts.pop(key)
            self._total_live_bytes = max(0, self._total_live_bytes - live.byte_count)
        # Pending accumulators for this message.
        for key in [k for k in self._pending if k[0] == sid and k[1] == mid]:
            acc = self._pending.pop(key)
            if acc is not None:
                self._total_pending_bytes = max(
                    0, self._total_pending_bytes - acc.byte_count,
                )
        # Tombstones for this message (no byte budget).
        for key in [k for k in self._nontext_parts if k[0] == sid and k[1] == mid]:
            self._nontext_parts.pop(key)
        for key in [k for k in self._disabled_parts if k[0] == sid and k[1] == mid]:
            self._disabled_parts.pop(key)
        # Revisions for this message (MAJOR 5: cleared on retire).
        for key in [k for k in self._part_revisions if k[0] == sid and k[1] == mid]:
            self._part_revisions.pop(key)
        # CRITICAL 2 gate: block late part events for this message.
        self._retired_messages.add((sid, mid))
    # ------------------------------------------------------------------
    # finish_part — terminal residual drain + retire
    # ------------------------------------------------------------------
    def finish_part(self, key: PartKey, final_text: str) -> None:
        """Synchronously publish residual delta, then retire the part.

        Authoritative completion is reconciled through digest revision plus
        HTTP full state.
        """
        # C1: synchronous drain of residual pending → fanout delta FIRST.
        # CRITICAL 1 (Option B): the residual delta consumes its own
        # strictly-increasing revision.
        acc = self._pending.pop(key, None)
        if acc is not None and acc.byte_count:
            pending_bytes = acc.byte_count
            text = acc.drain()
            self._total_pending_bytes = max(0, self._total_pending_bytes - pending_bytes)
            if text:
                # 4.12.0 修订六 B-1: delta publication rides the atomic
                # reserve→encode→append path (payload embeds the seq).
                self._fanout_delta_frame(key, text)
        # Retire via drop_part (idempotent). Disabling ensures any late
        # delta for this key silently drops on _disabled (no orphan noise).
        self.drop_part(key)
    # ------------------------------------------------------------------
    # Session routing (Stage B, §16-B)
    # ------------------------------------------------------------------
    def on_session_status(self, sid: str, status: "str | dict[str, Any] | None") -> None:
        """Record upstream session.status and trigger idle cleanup (§16-B).

        ``status`` accepts BOTH upstream shapes — legacy plain string
        (``"busy"``) and object envelope (``{"type": "busy"}``; live-wire
        2026-08-19) — normalized via the shared
        :func:`oc_slimapi.sse.hub_types.normalize_session_status` so the
        token hub behaves identically to the digest path regardless of
        which shape the wire carries. The production caller
        (GlobalHub.publish's mirror branch) already passes a normalized
        string; normalizing again here is idempotent and keeps this entry
        point safe for any future direct caller. An invalid shape (no
        valid status) is treated like an unknown status below: ignored.

        WHY only busy/idle are recorded: opencode's session lifecycle uses
        these two states as the authoritative "generation in progress" /
        "generation done" signals; other transient states (e.g. ``shared``)
        carry no actionable meaning for the accumulator and are ignored so
        the :meth:`ttl_sweep` busy-guard keys off a known-good signal.

        WHY idle→retire immediately + barrier + STOP-only termination: the
        upstream has authoritatively said this session is done. Any LiveParts
        still hanging are abandoned text-ends we missed (opencode bug,
        sidecar restart, etc.) — clearing them prevents orphan-LivePart
        leakage. ``RESYNC_SESSION_IDLE`` is an internal lifecycle reason, not
        an active-v4 wire reason: the flush loop routes it through
        ``TokenSubscriber.terminate``, which suppresses the reason and queues
        STOP only. The barrier makes an old-cursor reconnect receive the
        frozen ``reconnect_no_replay`` control resync and reconcile over HTTP.
        """
        normalized = normalize_session_status(status)
        if normalized is None or normalized not in ("busy", "idle"):
            return
        status = normalized
        self._session_status[sid] = status
        self._session_status.move_to_end(sid)
        self._prune_session_status()
        if status == "busy":
            return
        # idle
        self._retire_session(sid)
        # The retire above invalidates the accumulator state for this sid.
        # Write the token-domain replay barrier at the source, including the
        # zero-subscriber case: an old cursor must reconnect through the
        # frozen reconnect_no_replay control reason, never across the gap.
        self._write_replay_barrier(sid, RESYNC_SESSION_IDLE)
        self._enqueue_session_resync(sid, RESYNC_SESSION_IDLE)
    def on_session_deleted(self, sid: str) -> None:
        """Clear all state for a deleted session (§16-B) + terminate token
        subscribers (INV-4 / P0-3).

        The authoritative deletion signal is the global control-plane
        ``session.digest{deleted:true}``. The token domain separately writes a
        replay barrier and terminates each original connection with STOP only:
        ``session_deleted`` is not in ``V4_RESYNC_REASONS``, so
        :meth:`TokenSubscriber.terminate` does not serialize that reason. An
        old-cursor reconnect is classified as the frozen
        ``reconnect_no_replay`` control resync and then reconciles over HTTP.

        The generator receives STOP → breaks → finally →
        :meth:`TokenStreamRegistry.unsubscribe` (normal path: detach +
        decrement + last-detach stop flush + grace arm). Previously only a
        lifecycle reason was enqueued via the flush loop without STOP, so the
        subscriber connection stayed open forever. Direct ``terminate`` now
        closes it immediately without emitting an out-of-domain resync.

        WHY also clear ``_session_status`` here (but NOT
        in ``_retire_session``): per spec ``_retire_session`` only owns
        the 4 part-state structures; the session's status record outlives
        a part-level retire. Deletion, however, is terminal for the whole
        session — there's no future status to remember.

        rev-ogpt CRITICAL 2: also clears ``_retired_messages`` entries
        for this sid (the session is gone — late part events for any of
        its messages cannot arrive anymore). The replay queue entries
        for this sid are left intact (their own TTL/cap will clean them
        up; tearing them down eagerly would surprise a reconnecting
        client that subscribed mid-deletion).

        The hub does NOT detach subscribers or decrement the registry's
        ``total_subscribers`` — the membership guard in
        :meth:`TokenStreamRegistry.unsubscribe` relies on the sub still
        being in the fanout so the generator's finally runs the normal
        cleanup path.
        """
        self._retire_session(sid)
        self._session_status.pop(sid, None)
        # Deletion has its own token-domain retirement path. The barrier keeps
        # old cursors from replaying stale deltas or attaching up-to-date to a
        # dead stream; reconnect goes through reconnect_no_replay and HTTP
        # reconciliation. Global session.digest{deleted:true} remains the
        # authoritative deletion signal.
        self._write_replay_barrier(sid, RESYNC_SESSION_DELETED)
        # CRITICAL 2 gate cleanup for this session.
        for key in [k for k in self._retired_messages if k[0] == sid]:
            self._retired_messages.discard(key)
        # P1-22: record sid in the deleted-sid gate so late part events
        # for this session are dropped (no LivePart resurrection).
        self._remember_deleted_sid(sid)
        # Server-side lifecycle termination: session_deleted is deliberately
        # outside V4_RESYNC_REASONS, so terminate queues STOP only. Do not
        # detach or decrement; the generator's finally handles unsubscribe.
        for sub in tuple(self._subs_by_sid.get(sid, ())):
            sub.terminate(RESYNC_SESSION_DELETED)
    def _retire_session(self, sid: str) -> None:
        """Clear the 4 part-state structures for a session (§16-B).

        Scope (per spec): ``live_parts`` / ``_pending`` / ``_nontext_parts``
        / ``_disabled_parts`` only. ``_session_status``
        is NOT touched — it outlives a part-level retire so the TTL
        busy-guard can still read the session's status for any
        late-arriving parts; only :meth:`on_session_deleted` and
        :meth:`on_upstream_reconnect` clear them.

        WHY floor-0 on the byte gauge: defensive against any double-retire
        or accounting drift (a misbehaving upstream re-emitting
        ``session.idle`` must not corrupt the budget gauge).
        """
        # Live parts: pop + decrement bytes.
        for key in [k for k in self.live_parts if k[0] == sid]:
            live = self.live_parts.pop(key)
            self._total_live_bytes = max(0, self._total_live_bytes - live.byte_count)
        # Pending: discard + decrement the pending byte gauge (Stage E).
        for key in [k for k in self._pending if k[0] == sid]:
            acc = self._pending.pop(key, None)
            if acc is not None:
                self._total_pending_bytes = max(0, self._total_pending_bytes - acc.byte_count)
        # Nontext + disabled tombstones: discard (no byte budget).
        for key in [k for k in self._nontext_parts if k[0] == sid]:
            self._nontext_parts.pop(key, None)
        for key in [k for k in self._disabled_parts if k[0] == sid]:
            self._disabled_parts.pop(key, None)
        # Stage B (P0-3 partEventRevision): drop cached revisions for this
        # session's parts. Mirrors the other 4 ledgers' retire scope.
        for key in [k for k in self._part_revisions if k[0] == sid]:
            self._part_revisions.pop(key, None)
    def ttl_sweep(self, now_ms: int | None = None) -> list[PartKey]:
        """Retire idle LiveParts whose ``last_delta_ms`` exceeds the idle
        TTL (design §5.3 + §16-B busy-guard).

        WHY this is gated on known-idle (bgpt NB#4): a session that has
        gone quiet for >60s while STILL BUSY (per upstream
        ``session.status``) is most likely in a long generation pause —
        retiring its LivePart would drop accumulated text and invalidate the
        incremental baseline, defeating the whole point of the accumulator.
        We only retire when the upstream has explicitly
        told us the session is idle. Unknown status also does NOT retire.

        Returns the list of retired keys. Stage C's flush_loop calls this
        on its ~60s tick (NB-B5).
        """
        if now_ms is None:
            now_ms = _now_ms()
        cutoff = now_ms - TOKEN_ACC_IDLE_MS
        retired: list[PartKey] = []
        for key, live in list(self.live_parts.items()):
            if live.last_delta_ms >= cutoff:
                continue  # still active within the idle window.
            sid = key[0]
            if self._session_status.get(sid) != "idle":
                continue  # unknown or busy → do NOT retire (backstop NB#4).
            # known idle + expired → retire (clear without disabling).
            acc = self._pending.pop(key, None)
            if acc is not None:
                self._total_pending_bytes = max(0, self._total_pending_bytes - acc.byte_count)
            self._total_live_bytes = max(0, self._total_live_bytes - live.byte_count)
            # pop from live_parts last; we hold `live` by reference.
            self.live_parts.pop(key, None)
            self._discard_nontext(key)
            # Stage B: drop cached revision alongside the part.
            self._part_revisions.pop(key, None)
            retired.append(key)
        return retired
    # ------------------------------------------------------------------
    # Upstream lifecycle (Stage B wires reconnect state-clear;
    # Stage C adds per-sid resync fanout to attached subscribers)
    # ------------------------------------------------------------------
    def on_upstream_reconnect(self) -> None:
        """Clear all accumulated state on upstream reconnect (design §5.2).

        The opencode GlobalBus has no replay — on reconnect every
        :class:`LivePart` is stale (the upstream may have finished the
        part while we were disconnected). All per-part + per-session state
        is cleared wholesale. Every sid with an attached token subscriber
        receives ``resync{reconnect_no_replay, sessionID}`` so clients
        drop stale stream state and re-fetch authoritative ``/since``.

        rev-ogpt CRITICAL 1 (3rd-round terminal audit): ``_part_revisions``
        is **PRESERVED** across reconnect. Clearing it would restart the
        per-frame revision counter at 0 in the new epoch, but ocdroid
        (the Android client) retains its last-seen ``partEventRevision``
        watermark across the sidecar reconnect (it only resets *its*
        watermark when IT receives a ``resync`` and re-fetches via
        ``/since``). With the counter zeroed, the next emitted frame for
        an existing PartKey would carry revision 0 → ocdroid's strict
        ``>`` dedup drops it (0 <= last_seen) → silent frame loss.
        Preserving the counter guarantees the next emitted frame for any
        PartKey that already had state pre-reconnect carries a strictly
        greater revision than anything ocdroid saw before. The
        ``_part_revisions`` FIFO cap (``TOKEN_DISABLED_MAX``) still
        applies, bounding growth.

        KNOWN LIMITATION (sidecar process restart, not reconnect): if
        the sidecar process itself restarts (memory lost), the counter
        inevitably restarts at 0. Handling that requires a cross-repo
        protocol design (e.g. persisting the counter, or signalling
        clients to reset their watermark on sidecar cold start) and is
        out of scope for this fix. Documented here so revisitors know
        the invariant is "same sidecar process lifetime" only.

        ``_retired_messages`` is cleared because the new upstream epoch
        starts fresh; historical removals remain solely in ReplayLog.
        """
        self.live_parts.clear()
        self._nontext_parts.clear()
        self._disabled_parts.clear()
        self._pending.clear()
        self._total_live_bytes = 0
        self._total_pending_bytes = 0
        # rev-ogpt CRITICAL 1 (3rd-round): PRESERVE ``_part_revisions``
        # across reconnect. See docstring — clearing it would break
        # ocdroid's strict-``>`` watermark invariant. The FIFO cap on
        # ``_part_revisions`` still bounds memory.
        # Session-routing state: clear wholesale (epoch reset).
        self._session_status.clear()
        self._pending_session_resinks.clear()
        # CRITICAL 2 gate: new epoch, no late events from the previous
        # epoch can arrive.
        self._retired_messages.clear()
        # P1-22: deleted-sid gate cleared (new epoch — deleted sessions
        # from the old epoch cannot send late part events).
        self._deleted_sids.clear()
        # _part_revisions intentionally survives this reconnect.
        # Fan reconnect_no_replay to every sid with an attached subscriber.
        # (If subscribers themselves were torn down by the HTTP layer during
        # the reconnect, _subs_by_sid is already empty — no-op.)
        for sid in list(self._subs_by_sid.keys()):
            self._fanout_resync(sid, RESYNC_RECONNECT_NO_REPLAY)
