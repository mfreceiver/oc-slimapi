"""Token-stream memory budgets + part-lifecycle/tombstone accounting.

Split from :mod:`oc_slimapi.sse.tokenstream.hub` (F-301 five-module split,
pure move — zero behaviour change).

This module owns the ``TOKEN_*`` budget constants family and
:func:`apply_debug_budget_overrides`. The rebinding via ``global`` below
mutates THIS module's namespace, which is also where every runtime reader
(:meth:`BudgetMixin._reserve` / :meth:`BudgetMixin._start_part` /
:meth:`BudgetMixin._check_pending_budget`) resolves the names — test
patches and debug overrides must target ``...tokenstream.budgets``.
``hub.py`` re-exports the names for import compatibility only.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from ...config import (
    TOKEN_DISABLED_MAX,
    TOKEN_DISABLED_TTL_MS,
    TOKEN_LIVE_PARTS_MAX,
    TOKEN_LIVEPARTS_MAX_BYTES,
    TOKEN_PART_MAX_BYTES,
    TOKEN_PENDING_MAX_BYTES,
    TOKEN_REMOVED_MESSAGES_MAX,
    TOKEN_REMOVED_MESSAGES_TTL_MS,
)
from ..hub_types import RESYNC_TOKEN_MEMORY_LIMIT
from .frames import PartKey, _now_ms
from .ingest import _SESSION_STATUS_MAX
from .models import LivePart


def apply_debug_budget_overrides(settings: Any) -> None:
    """Debug/联调-only: override LIVE budget caps from env settings, so
    memory-limit eviction (MB-P-S1 current-key nodrop path) can be triggered
    with small data volumes during development / integration testing.

    Overrides the module-level ``TOKEN_LIVEPARTS_MAX_BYTES``,
    ``TOKEN_PART_MAX_BYTES``, and ``TOKEN_LIVE_PARTS_MAX`` globals — i.e.
    the same names that :meth:`TokenStreamHub._reserve` and
    :meth:`TokenStreamHub._start_part` read.  This approach preserves both:

    * **Runtime effect**: the cap change takes effect for every hub instance
      without plumbing a new constructor parameter through ``TokenStreamHub``,
      ``TokenStreamRegistry``, and every app wiring point.
    * **Test compatibility**: the ~10 existing tests that use
      ``monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_...", val)``
      continue to work because they patch the same module global.

    Should be called **exactly once** during app lifespan startup, after
    ``settings.validate()`` and before any hub method that reads these caps.
    When a setting field is ``None`` (env unset), the corresponding code-level
    default is left unchanged — zero behaviour change.

    Integration testing MUST run through the real app lifespan (or call this
    function explicitly); minimal app fixtures used by route/unit tests (e.g.
    ``_build_app``) do NOT invoke it, so ``DEBUG_*`` env is ignored there.

    Production deployments should NOT set ``OC_SLIMAPI_TOKEN_STREAM_DEBUG_*``
    env vars.  This is a debug/ops break-glass tool only.
    """
    global TOKEN_LIVEPARTS_MAX_BYTES, TOKEN_PART_MAX_BYTES, TOKEN_LIVE_PARTS_MAX
    if settings.token_stream_debug_live_budget_bytes is not None:
        TOKEN_LIVEPARTS_MAX_BYTES = settings.token_stream_debug_live_budget_bytes
    if settings.token_stream_debug_part_max_bytes is not None:
        TOKEN_PART_MAX_BYTES = settings.token_stream_debug_part_max_bytes
    if settings.token_stream_debug_live_parts_max is not None:
        TOKEN_LIVE_PARTS_MAX = settings.token_stream_debug_live_parts_max


class BudgetMixin:
    """Truncation / memory-budget group + part-lifecycle & tombstone
    bookkeeping group (moved verbatim from ``TokenStreamHub``). Expects the
    full container set initialised by :class:`TokenStreamHub.__init__`; all
    cross-group calls go through ``self`` (composed single-instance hub).
    """

    def _next_part_revision(self, key: PartKey) -> int:
        """Consume and return the next per-frame revision for ``key``.

        rev-ogpt CRITICAL 1 (Option B — per-FRAME) + MAJOR 5: this is
        the SINGLE increment site for ``_part_revisions`` (besides
        pop-on-retire paths). Every emitted delta calls this so no two
        delivered frames for the same part share a revision. Initializes
        to -1 internally so the first
        emitted frame for a new part yields 0. Move-to-end + FIFO cap
        (``TOKEN_DISABLED_MAX``) keep the map bounded (MAJOR 5) and LRU-
        correct.
        """
        rev = self._part_revisions.get(key, -1) + 1
        self._part_revisions[key] = rev
        self._part_revisions.move_to_end(key)
        # FIFO cap (MAJOR 5): evict oldest when over limit. Aligned with
        # ``_nontext_parts`` / ``_disabled_parts`` for consistency.
        while len(self._part_revisions) > TOKEN_DISABLED_MAX:
            self._part_revisions.popitem(last=False)
        return rev
    def _truncate_part_for_all(self, key: PartKey, done: bool) -> int | None:
        """Retire an oversized part without constructing legacy wire frames.

        The method name/signature remain internal-call compatible. Native v4
        clients converge through revision reconciliation and authoritative
        HTTP state; ``truncatedSnapshotsTotal`` therefore remains zero.
        """
        return 0 if self.drop_part(key) else None
    # ------------------------------------------------------------------
    # Memory accounting (Stage C live budget + Stage E pending budget split)
    # ------------------------------------------------------------------
    def _reserve(self, live: LivePart, n: int, key: PartKey) -> bool:
        """Per-part + global LIVE budget check before appending ``n`` bytes
        (Stage C; Stage E renamed to "live budget" after the 4+4 split).

        Returns True iff the delta may be appended; False if it was dropped
        after the appropriate state transition. Failure modes (§6 / §5.8):

        * **Per-part cap** (``TOKEN_PART_MAX_BYTES``): this delta would
          push the part over 1 MiB → drop/disable the part. The client
          converges through digest revision and authoritative HTTP state.
        * **Global LIVE byte cap** (``TOKEN_LIVEPARTS_MAX_BYTES``, 4MiB
          after Stage E split): this delta would push the global
          accumulator over the cap → LRU-evict the oldest LivePart (by
          ``last_delta_ms``, never the current key) and fan
          ``resync{token_memory_limit, sessionID}`` to the evicted sid;
          repeat until under budget. If after eviction the delta still
          cannot fit (only this part left, or the delta alone exceeds the
          cap — unreachable in practice because the per-part cap is
          smaller), retire + drop. Returns False iff the current key was
          ultimately retired.

        Note: this method ONLY checks the LIVE budget (LivePart chunks).
        The PENDING budget (DeltaAccumulator chunks) is checked separately
        by :meth:`_check_pending_budget` — the two budgets are independent
        gauges that each protect their own physical buffer.
        """
        # Per-part accumulation cap.
        if live.byte_count + n > TOKEN_PART_MAX_BYTES:
            self._truncate_part_for_all(key, done=False)
            return False
        # Global LIVE byte cap: evict oldest (LRU by last_delta_ms) until room.
        # Never evict the current key — we're reserving FOR it; evicting it
        # would invalidate the `live` reference the caller is about to
        # append to.
        while self._total_live_bytes + n > TOKEN_LIVEPARTS_MAX_BYTES:
            candidates = [k for k in self.live_parts if k != key]
            if not candidates:
                # Only this part left and it alone exceeds the global cap.
                # Per-part cap (1 MiB) < global cap (4 MiB), so this is
                # only reachable if TOKEN_LIVEPARTS_MAX_BYTES was
                # misconfigured below TOKEN_PART_MAX_BYTES. Defensive.
                self._truncate_part_for_all(key, done=False)
                return False
            oldest = min(candidates, key=lambda k: self.live_parts[k].last_delta_ms)
            self._evict_part_for_memory(oldest, skip_key=key)
        return True
    def _evict_part_for_memory(
        self, key: PartKey, skip_key: PartKey | None = None
    ) -> None:
        """LRU-evict a LivePart under global memory pressure (§6 / §16-C).

        Retires the part via :meth:`drop_part` (idempotent — late deltas
        for the key silently drop on ``_disabled``) and publishes
        ``resync{token_memory_limit, sessionID}`` as a REPLAYABLE business
        frame to the sid (4.12.0 修订六 protocol v2 — advisory resync):
        the client keeps consuming subsequent frames by the normal seq
        rules (no state drop, no dedicated recovery GET, no replay
        buffering on receipt); the evicted part's final state converges
        via the existing revision channel (part.updated → digest
        messagesRevision bump → client ?since/GET) plus the final-state
        REST merge rule (rest text overrides once the same part carries
        a non-empty ``time.end`` on the reconciliation GET).

        ``skip_key`` is the key the caller is currently reserving/admitting
        for. Candidate selection excludes it, preserving the caller's live
        object reference and preventing gauge drift or orphan deltas.

        4.12.0 修订六 B-2 (rev-2 修正 2, 语义精化): the eviction clears
        the part's live/pending state (``drop_part`` — unchanged) and
        publishes the ``token_memory_limit`` resync as a REPLAYABLE
        business frame via :meth:`_fanout_replayable_resync` (B-1 atomic
        path: id line + payload seq + ReplayLog entry) — replacing the
        historical connection-private control degradation. The stream does NOT
        terminate: later frames for the sid (other, un-evicted parts /
        future new parts) keep publishing on the same seq sequence.

        🟠 The EVICTED part itself never resumes its incremental stream in
        this process lifetime: ``drop_part`` put the key in
        ``_disabled_parts``, so every later ``message.part.updated`` /
        ``message.part.delta`` for it is silently dropped at the ingest
        gates. The part is REST-owned from here on — never a server-side
        incremental state rebuild. v4 final-state closure face =
        ``/full/{mid}`` (the ``time.end`` predicate; CLIENT_CHANGES
        five-step algorithm): the ``/messages`` skeleton face carries no
        part ``time`` at all.

        4.12.0 修订六 B-2 (barrier removal): no replay barrier is written
        on this path anymore. The replayable resync frame itself now
        carries the R4 guarantee — it sits in the window at its own seq,
        so ANY reconnecting cursor below it replays it (and any cursor at
        it means the client already consumed the eviction signal). A
        barrier (watermark = last_seq) would intercept every cursor ≤
        watermark and the resync at watermark+1 could then never be
        replayed. Barriers remain on the idle/deleted paths whose resyncs
        are NOT replayable.
        """
        if not self.drop_part(key):
            return  # already disabled — eviction resync already fanned.
        sid = key[0]
        # Drain pending for this sid before the replayable resync.
        # The EVICTED key's own pending accumulator was already popped and
        # DISCARDED by drop_part above — its un-flushed tail delta is never
        # published. flush_sid therefore only publishes the OTHER live parts'
        # pending deltas of this sid (B-1 path, taking seqs BEFORE the resync)
        # — the eviction signal is ordered strictly after every published
        # delta of this sid on the replay sequence, and the evicted part has
        # no frame of its own after its last flushed delta.
        self.flush_sid(sid)
        # B-2: replayable resync (fail-closed on publish failure — see
        # _fanout_replayable_resync; the state is already cleared here).
        self._fanout_replayable_resync(sid, RESYNC_TOKEN_MEMORY_LIMIT)
        self._metrics.token_memory_limit_total += 1
    def _check_pending_budget(self, current_key: PartKey) -> None:
        """Stage E (§16-C residual split): global PENDING budget overflow handler.

        The pending budget (``TOKEN_PENDING_MAX_BYTES``, 4MiB) bounds the
        global sum of ``DeltaAccumulator.byte_count`` — the transient
        pre-flush window. Overflow handling (design §16-C residual):

        1. **Force-flush**: drain ALL pending accumulators to subscriber
           queues via :meth:`flush`. This always clears
           ``_total_pending_bytes`` to 0 (drain resets byte_count
           regardless of subscriber presence — frames are silently
           dropped when no sub is attached).
        2. **No-subscribers / still-over fallback**: if there were NO
           subscribers when the overflow was detected (the flushed deltas
           were silently dropped, not delivered) OR pending is somehow
           still over cap after flush (defensive — structurally
           unreachable since flush clears ALL pending), LRU-evict the
           oldest LivePart (never ``current_key``) + resync. Rationale: a
           pending overflow without consumers signals unbounded LivePart
           growth (every pending byte is also a live byte); evicting the
           oldest relieves the LIVE budget too and the resync lets any
           future subscriber know it must re-fetch via ``/since``.

        ``current_key`` is the key being processed in :meth:`on_part_delta`
        (the caller); it is NEVER evicted here (mirrors ``_reserve``'s
        "never evict the current key" contract — evicting it would
        invalidate the ``live`` reference the caller just appended to).
        """
        if self._total_pending_bytes <= TOKEN_PENDING_MAX_BYTES:
            return
        had_subs = self.subscriber_count > 0
        # Force-flush ALL pending → drains to subscribers (or drops if none).
        self.flush()
        # No subscribers, or defensive still-over → LRU-evict oldest
        # (never the current key) + resync{token_memory_limit}.
        if (not had_subs) or self._total_pending_bytes > TOKEN_PENDING_MAX_BYTES:
            candidates = [k for k in self.live_parts if k != current_key]
            if candidates:
                oldest = min(candidates, key=lambda k: self.live_parts[k].last_delta_ms)
                self._evict_part_for_memory(oldest, skip_key=current_key)
    # ------------------------------------------------------------------
    # Part lifecycle helpers
    # ------------------------------------------------------------------
    def _start_part(self, key: PartKey, seed: str = "") -> None:
        """Create a :class:`LivePart` with optional seed text (design §5.3).

        The seed is appended to ``chunks`` and counted in ``byte_count`` /
        ``_total_live_bytes`` so the Stage-C ``_reserve`` budget sees it.
        Stage C additionally enforces the global LivePart COUNT cap
        (``TOKEN_LIVE_PARTS_MAX``) by LRU-evicting the oldest part before
        creating a new one — the byte cap is enforced per-delta in
        :meth:`_reserve`.

        NB-C1 (Stage D harden): a burst of text-starts each carrying a large
        seed can collectively breach the global LIVE byte cap
        (``TOKEN_LIVEPARTS_MAX_BYTES``, 4MiB after Stage E split) even when
        every seed individually sits under the per-part cap
        (``TOKEN_PART_MAX_BYTES``). The per-delta ``_reserve`` while-evict
        never sees this because no delta is appended — the seed itself is
        the admission. We therefore run the SAME LRU while-evict here,
        never evicting the key we are admitting (mirrors ``_reserve``'s
        ``never evict the current key`` contract). Seeds do NOT contribute
        to the pending budget because they are never buffered in
        ``DeltaAccumulator`` or emitted as delta frames; ReplayLog plus HTTP
        is the native-v4 alignment source.
        """
        # Global part COUNT cap: evict oldest (LRU) before creating.
        while len(self.live_parts) >= TOKEN_LIVE_PARTS_MAX:
            oldest = min(self.live_parts, key=lambda k: self.live_parts[k].last_delta_ms)
            self._evict_part_for_memory(oldest, skip_key=key)
        live = LivePart()
        self.live_parts[key] = live
        if seed:
            seed_bytes = len(seed.encode("utf-8"))
            # A seed alone exceeding the per-part cap means upstream
            # re-sent a finished part's full text in the text-start
            # (rare). Truncate immediately rather than admit an oversized
            # part.
            if seed_bytes > TOKEN_PART_MAX_BYTES:
                self._truncate_part_for_all(key, done=False)
                return
            live.chunks.append(seed)
            live.byte_count = seed_bytes
            self._total_live_bytes += seed_bytes
            # NB-C1: multi-part large-seed burst — collectively evict the
            # oldest OTHER LiveParts until under the global byte budget.
            # Never evict ``key`` (we are admitting it); if it alone exceeds
            # the cap the per-part guard above already handled it.
            while self._total_live_bytes > TOKEN_LIVEPARTS_MAX_BYTES:
                candidates = [k for k in self.live_parts if k != key]
                if not candidates:
                    break
                oldest = min(candidates, key=lambda k: self.live_parts[k].last_delta_ms)
                self._evict_part_for_memory(oldest, skip_key=key)
    def drop_part(self, key: PartKey) -> bool:
        """Retire a part (C4: oversized / finished / evicted).

        Pops ``_pending`` (decrementing ``_total_pending_bytes``, floored at
        0 — Stage E) and ``live_parts`` (decrementing ``_total_live_bytes``,
        floored at 0) and discards the key from ``_nontext_parts``.
        Idempotent: the FIRST call records the key in ``_disabled_parts``
        and returns ``True`` so callers perform any associated replayable
        resync exactly once; subsequent calls return ``False``. Calling
        ``drop_part`` on a key that was never seen is legal and still
        marks it disabled (no future deltas will be accepted for it).
        """
        acc = self._pending.pop(key, None)
        if acc is not None:
            self._total_pending_bytes = max(0, self._total_pending_bytes - acc.byte_count)
        live = self.live_parts.pop(key, None)
        if live is not None:
            self._total_live_bytes = max(0, self._total_live_bytes - live.byte_count)
        self._discard_nontext(key)
        # Stage B (P0-3 partEventRevision): drop the cached revision so a
        # recycled key (opencode reuses IDs only across sessions; within a
        # session IDs are unique) cannot carry a stale watermark.
        self._part_revisions.pop(key, None)
        if self._is_disabled(key):
            return False  # idempotent: already disabled.
        self._remember_disabled(key)
        return True
    # ------------------------------------------------------------------
    # Bounded tombstone helpers (Stage B, §16-B)
    # ------------------------------------------------------------------
    def _remember_disabled(self, key: PartKey) -> None:
        """Record a tombstone in the bounded ``_disabled_parts`` map.

        WHY bounded: an unbounded ``set`` would grow forever across a
        long-running sidecar (every oversized part stays
        forever). We cap at ``TOKEN_DISABLED_MAX`` entries with a
        ``TOKEN_DISABLED_TTL_S`` expiry, evicting oldest first
        (insertion-ordered ``OrderedDict``). Idempotent: re-recording an
        existing key is a no-op (we do NOT refresh the TTL — a tombstone's
        age is when it was first disabled).
        """
        if key in self._disabled_parts:
            return
        now_ms = _now_ms()
        self._disabled_parts[key] = now_ms
        self._prune_bounded(self._disabled_parts, now_ms)
    def _remember_nontext(self, key: PartKey) -> None:
        """Record a non-text part in the bounded ``_nontext_parts`` map.

        Same bounded-OrderedDict shape as :meth:`_remember_disabled` — caps
        unbounded growth across long sessions with many reasoning/tool
        parts. Idempotent (existing key is a no-op).
        """
        if key in self._nontext_parts:
            return
        now_ms = _now_ms()
        self._nontext_parts[key] = now_ms
        self._prune_bounded(self._nontext_parts, now_ms)
    def _discard_nontext(self, key: PartKey) -> None:
        """Remove a non-text tombstone (:meth:`drop_part` /
        :meth:`_retire_session` reuse for defensive cleanup)."""
        self._nontext_parts.pop(key, None)
    def _is_disabled(self, key: PartKey) -> bool:
        return key in self._disabled_parts
    def _is_nontext(self, key: PartKey) -> bool:
        return key in self._nontext_parts
    def _prune_bounded(
        self,
        store: OrderedDict[PartKey, int],
        now_ms: int,
    ) -> None:
        """Evict expired (TTL) + overflow (cap) entries from a tombstone map.

        Called after each insert. Oldest-first eviction relies on the
        ``OrderedDict`` preserving insertion order; we expire-from-front
        for TTL (oldest timestamps first) then pop-from-front for the
        size cap. Both passes are bounded by ``TOKEN_DISABLED_MAX`` so the
        prune itself is O(cap) worst case (only right after a long
        disconnect where many tombstones TTL-out at once).
        """
        cutoff = now_ms - TOKEN_DISABLED_TTL_MS
        # TTL: oldest-first (front of insertion order = oldest timestamp).
        while store:
            oldest_key, oldest_ts = next(iter(store.items()))
            if oldest_ts < cutoff:
                store.popitem(last=False)
            else:
                break
        # Cap: evict oldest until under limit.
        while len(store) > TOKEN_DISABLED_MAX:
            store.popitem(last=False)
    def _remember_deleted_sid(self, sid: str) -> None:
        """P1-22: record a sid in the bounded deleted-sid gate.

        Late part events for a deleted session are dropped at the entry of
        ``on_part_updated`` / ``on_part_delta`` / ``on_part_removed`` so they
        cannot resurrect a LivePart. Cap + TTL use the shared late-event gate
        retention budget (``TOKEN_REMOVED_MESSAGES_MAX`` /
        ``TOKEN_REMOVED_MESSAGES_TTL_MS``).
        ``move_to_end`` on re-delete keeps the freshest entry at the tail.
        """
        now_ms = _now_ms()
        self._deleted_sids[sid] = now_ms
        self._deleted_sids.move_to_end(sid)
        self._prune_deleted_sids(now_ms)
    def _is_deleted_sid(self, sid: str) -> bool:
        """P1-22: check the deleted-sid gate (with lazy TTL expiry)."""
        ts = self._deleted_sids.get(sid)
        if ts is None:
            return False
        if ts < _now_ms() - TOKEN_REMOVED_MESSAGES_TTL_MS:
            self._deleted_sids.pop(sid, None)
            return False
        return True
    def _prune_deleted_sids(self, now_ms: int) -> None:
        """P1-22: enforce FIFO cap + TTL on the deleted-sid gate."""
        cutoff = now_ms - TOKEN_REMOVED_MESSAGES_TTL_MS
        expired = [k for k, ts in self._deleted_sids.items() if ts < cutoff]
        for k in expired:
            self._deleted_sids.pop(k, None)
        while len(self._deleted_sids) > TOKEN_REMOVED_MESSAGES_MAX:
            self._deleted_sids.popitem(last=False)
    def _prune_session_status(self) -> None:
        """P1-21: FIFO cap on ``_session_status`` to prevent unbounded growth."""
        while len(self._session_status) > _SESSION_STATUS_MAX:
            self._session_status.popitem(last=False)
