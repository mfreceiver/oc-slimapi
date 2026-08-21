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
from .frames import PartKey, _now_ms, _truncated_frame
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
        pop-on-retire paths). Every token frame with independent
        delivery semantics (snapshot / delta / done marker / truncated)
        calls this so no two frames emitted for the same part ever
        share a revision. Initializes to -1 internally so the first
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
        """C6 backstop: fan ``snapshot{truncated:true}`` to ALL subscribers of
        the key's sid, then :meth:`drop_part`.

        Idempotent via the ``_is_disabled`` check up front (``drop_part``
        would return False for an already-disabled key) — the truncated
        frame is emitted exactly once per part even if multiple code paths
        race (per-tick snapshot fanout, _reserve per-part overflow,
        finish_part terminal marker).

        Returns the per-frame revision consumed for THIS truncated frame
        (or ``None`` if the part was already disabled — second-call no-op).
        Callers use the returned value to deliver a direct-put truncated
        frame to a handshake sub that is not yet in the fanout set
        (:meth:`_emit_snapshot_or_truncated`).

        Stage B v0.4 (MAJOR 4 fix): the per-part revision is captured
        BEFORE :meth:`drop_part` clears ``_part_revisions[key]``, so the
        truncated frame still carries a valid ``partEventRevision``.

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): the truncated frame
        consumes its OWN revision via :meth:`_next_part_revision`
        (strictly greater than the previous delivery), so a client using
        strict ``>`` reliably accepts it. The idempotency check
        (``_is_disabled``) runs BEFORE the increment so a second-call
        no-op does not waste a revision.
        """
        # Idempotency: if the key is already disabled, a previous call
        # already fanned the truncated frame — no-op (do NOT increment
        # the revision for a no-op).
        if self._is_disabled(key):
            return None
        # Per-frame (Option B): consume the NEXT revision BEFORE
        # drop_part clears ``_part_revisions[key]``. The captured value
        # is what the truncated frame carries on the wire.
        captured_rev = self._next_part_revision(key)
        # drop_part now disables the key (so subsequent calls hit the
        # idempotency branch above) and clears ``_part_revisions[key]``.
        self.drop_part(key)
        sid = key[0]
        trunc = _truncated_frame(key, done, part_revision=captured_rev)
        # rev-gate R2 BLOCKER-1: the truncated marker belongs to the
        # ``message.part.snapshot`` family — v4-INELIGIBLE. It is NOT
        # replay-logged (no seq), and delivered to v3 subscribers only.
        # A v4 reconnect therefore never sees it in the replay window;
        # v4 state alignment is HTTP-based per the frozen contract.
        self._deliver_v3_only(sid, trunc)
        self._metrics.truncated_snapshots_total += 1
        return captured_rev
    # ------------------------------------------------------------------
    # Memory accounting (Stage C live budget + Stage E pending budget split)
    # ------------------------------------------------------------------
    def _reserve(self, live: LivePart, n: int, key: PartKey) -> bool:
        """Per-part + global LIVE budget check before appending ``n`` bytes
        (Stage C; Stage E renamed to "live budget" after the 4+4 split).

        Returns True iff the delta may be appended; False if it was dropped
        (after the appropriate fanout). Failure modes (§6 / §5.8):

        * **Per-part cap** (``TOKEN_PART_MAX_BYTES``): this delta would
          push the part over 1 MiB → :meth:`_truncate_part_for_all` fans
          ``snapshot{truncated:true, done:false}`` to the sid + drop_part.
          Returns False.
        * **Global LIVE byte cap** (``TOKEN_LIVEPARTS_MAX_BYTES``, 4MiB
          after Stage E split): this delta would push the global
          accumulator over the cap → LRU-evict the oldest LivePart (by
          ``last_delta_ms``, never the current key) and fan
          ``resync{token_memory_limit, sessionID}`` to the evicted sid;
          repeat until under budget. If after eviction the delta still
          cannot fit (only this part left, or the delta alone exceeds the
          cap — unreachable in practice because the per-part cap is
          smaller), truncate + drop. Returns False iff the current key
          was ultimately truncated.

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
        for the key silently drop on ``_disabled``) and fans
        ``resync{token_memory_limit, sessionID}`` to every subscriber of
        the evicted sid. The client drops all stream state for that sid
        and re-fetches authoritative text via ``/since``.

        After the eviction, re-emit a ``snapshot{done:false}`` for each
        REMAINING live part of the same sid to every already-attached
        subscriber, so existing subscribers get a fresh snapshot anchor
        without needing to reconnect (S-2 method B).

        ``skip_key`` (MB-P-S1): the key the CALLER is currently
        reserving/admitting for (e.g. ``_reserve``'s ``key``,
        ``_start_part``'s new ``key``). It is **re-included** in the
        re-snapshot loop via the nodrop path
        (:meth:`_emit_snapshot_or_truncated_nodrop`), which delivers the
        snapshot or truncated frame **without** calling ``drop_part``. This
        closes the client-anchor gap for clear-only (method B,
        ``triggersReconnect=false``) eviction: the current key's client-side
        anchor is restored by the re-snapshot, just like any other remaining
        live part.

        O1 invariant (still holds): the current key (``skip_key``) is
        ***never*** passed to ``_truncate_part_for_all`` or ``drop_part``
        during re-snapshot — the nodrop path emits truncated frames directly
        and keeps the LivePart alive. The caller's stale ``live`` reference
        remains valid; no gauge drift or orphan deltas.

        Under the older ``triggersReconnect=true`` model this re-snapshot of
        the current key is redundant (the reconnect handshake restores all
        anchors), but it is harmless — the client simply ignores the
        redundant frame.
        """
        if not self.drop_part(key):
            return  # already disabled — eviction resync already fanned.
        sid = key[0]
        # I1: drain pending for this sid before resync + re-snapshot
        # (mirrors attach_subscriber handshake step 2, preventing C2 double-count).
        self.flush_sid(sid)
        # rev-gate R4 BLOCKER-1: the evicted part's server-side state is
        # gone. Write the barrier AFTER flush_sid so the evicted part's
        # own drained deltas fall at/below the watermark — a client that
        # consumed them (cursor == last_seq) reconnects into
        # resync{reconnect_no_replay} instead of an up-to-date live mode
        # on a part the server can no longer complete.
        self._write_replay_barrier(sid, RESYNC_TOKEN_MEMORY_LIMIT)
        self._fanout_resync(sid, RESYNC_TOKEN_MEMORY_LIMIT)
        self._metrics.token_memory_limit_total += 1
        # Re-snapshot remaining live parts of this sid to existing subs.
        # MB-P-S1: include skip_key via nodrop path (no drop_part).
        subs = list(self._subs_by_sid.get(sid, ()))
        if not subs:
            return
        for live_key in sorted(
            k for k in self.live_parts if k[0] == sid
        ):
            live = self.live_parts[live_key]
            text = "".join(live.chunks)
            if live_key == skip_key:
                for sub in subs:
                    self._emit_snapshot_or_truncated_nodrop(
                        sub, live_key, text, done=False
                    )
            else:
                for sub in subs:
                    self._emit_snapshot_or_truncated(sub, live_key, text, done=False)
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
        to the pending budget (they are never buffered in
        ``DeltaAccumulator`` — delivered via the handshake snapshot, not a
        delta frame).
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
        """Retire a part (C4: truncated / finished / evicted).

        Pops ``_pending`` (decrementing ``_total_pending_bytes``, floored at
        0 — Stage E) and ``live_parts`` (decrementing ``_total_live_bytes``,
        floored at 0) and discards the key from ``_nontext_parts``.
        Idempotent: the FIRST call records the key in ``_disabled_parts``
        and returns ``True`` so the caller (Stage C truncate-fanout /
        finish_part / memory-evict) emits the resync / truncated frame
        exactly once; subsequent calls return ``False``. Calling
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
        long-running sidecar (every truncated / too_large part stays
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
        cannot resurrect a LivePart. Cap + TTL aligned with the replay queue
        (``TOKEN_REMOVED_MESSAGES_MAX`` / ``TOKEN_REMOVED_MESSAGES_TTL_MS``).
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
    def _prune_removed_messages(self, now_ms: int) -> None:
        """Enforce FIFO cap + TTL on the ``_removed_messages`` replay queue.

        Stage B v0.6 §P.2 (MAJOR 4 方案 C). Called on-insert (from
        :meth:`on_message_removed`) for cap enforcement and periodically
        from :meth:`ttl_sweep` for TTL cleanup. Oldest-first eviction
        relies on the ``OrderedDict`` preserving insertion order
        (refreshed by ``move_to_end`` in :meth:`on_message_removed` for
        duplicate-tombstone correctness — rev-ogpt MAJOR 6).

        rev-ogpt CRITICAL 2: every evicted key is ALSO discarded from
        ``_retired_messages`` so the gate's lifetime is coupled to the
        replay queue (the gate cannot outlive the tombstone — once the
        client can no longer be told about the removal, late events for
        that message are acceptably processed as "new" events; the
        digest fingerprint mechanism catches the inconsistency).
        """
        cutoff = now_ms - TOKEN_REMOVED_MESSAGES_TTL_MS
        # TTL: remove entries older than the 24h TTL.
        expired = [k for k, ts in self._removed_messages.items() if ts < cutoff]
        for k in expired:
            self._removed_messages.pop(k, None)
            self._retired_messages.discard(k)  # CRITICAL 2 gate cleanup
        # FIFO cap: evict oldest until under limit.
        while len(self._removed_messages) > TOKEN_REMOVED_MESSAGES_MAX:
            evicted_key, _ = self._removed_messages.popitem(last=False)
            self._retired_messages.discard(evicted_key)  # CRITICAL 2 gate cleanup
