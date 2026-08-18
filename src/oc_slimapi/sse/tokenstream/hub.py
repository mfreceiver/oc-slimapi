"""Token-stream accumulator — part-lifecycle-gated accumulator + flush engine.

Moved from :mod:`oc_slimapi.sse.token_hub`.

Key invariants (design §5.3 / §5.4 / §5.6):

* **Accumulation is decoupled from subscribers** — a ``text-start``
  (``part.time.end is None``) creates a :class:`LivePart` immediately,
  even if nobody is subscribed yet. This eliminates the
  "subscribe-mid-generation" race: by the time a subscriber attaches,
  ``live_parts`` already holds the authoritative accumulated text and the
  handshake snapshot has no gap.
* ``field != "text"`` deltas and ``part.type != "text"`` parts are
  silently dropped and counted (C3). Reasoning/tool-input parts reuse
  ``field:"text"`` upstream, so we MUST key the non-text decision off
  ``part.type`` from ``message.part.updated`` — hence the
  ``_nontext_parts`` ledger.
* Orphan deltas (no text-start observed — e.g. sidecar restart mid gen)
  are silently dropped + counted, NEVER triggering a resync storm (C3).
* :meth:`drop_part` (truncated / too_large, C4) is idempotent: the first
  call records the key in ``_disabled_parts`` and returns ``True``; the
  C6 truncate-fanout helper relies on this idempotency to emit the
  ``snapshot{truncated:true}`` frame exactly once per part.
* **Terminal order invariant (wire-strong)**: for a given
  ``(sid, mid, pid)``, all ``message.part.delta`` frames precede the
  matching ``snapshot{done:true}`` marker; after the marker the part
  never emits another token frame. :meth:`finish_part` enforces this by
  synchronously draining ``_pending`` (no await window) before fanning
  the marker.
* **Lever 1 (§16-C)**: the terminal marker is ``snapshot{done:true}``
  WITHOUT a ``text`` field — the authoritative part text is delivered by
  the existing digest → ``/since`` path. This drops the redundant
  terminal full-text re-send that dominated wire overhead.

Stage-C flush / memory contract (§16-C + Stage E 4+4 split):

* ``flush_loop`` runs at ``TOKEN_FLUSH_SECONDS`` (100ms); each tick drains
  ``_pending`` (sorted by key — deterministic intra-tick order, §5.4) and
  every ~60s calls :meth:`ttl_sweep` (NB-B5) plus the bounded
  ``_pending_session_resinks`` drain.
* ``_reserve`` enforces the per-part cap (``TOKEN_PART_MAX_BYTES``) and
  the global LIVE byte/count caps (``TOKEN_LIVEPARTS_MAX_BYTES`` /
  ``TOKEN_LIVE_PARTS_MAX``). Per-part overflow → truncate-fanout +
  ``drop_part``; global LIVE overflow → LRU evict the oldest LivePart +
  ``resync{token_memory_limit, sessionID}`` to its sid.
* Stage E ``_check_pending_budget`` enforces the global PENDING byte cap
  (``TOKEN_PENDING_MAX_BYTES``). Pending overflow → force-flush (drain
  ALL pending to subscriber queues); if no subscribers / still over →
  LRU evict the oldest LivePart + ``resync{token_memory_limit}``.
* Live and pending are INDEPENDENT budgets — the same delta byte
  physically occupies ``LivePart.chunks`` (persistent authoritative copy)
  AND ``DeltaAccumulator.chunks`` (transient pre-flush shadow), so each
  budget independently protects its OWN buffer (no double-count of a
  single memory region).
* ``_pending_session_resinks`` is bounded (``TOKEN_RESYNC_QUEUE_CAP``);
  overflow drops the oldest entry (NB-B2) so a flapping sid cannot starve
  others.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

from typing import TYPE_CHECKING, Any

from ...config import (
    DEFAULT_TOKEN_MAX_FRAME_BYTES,
    TOKEN_ACC_IDLE_MS,
    TOKEN_DISABLED_MAX,
    TOKEN_DISABLED_TTL_MS,
    TOKEN_FLUSH_SECONDS,
    TOKEN_FLUSH_BYTES,
    TOKEN_HEARTBEAT_SECONDS,
    TOKEN_LIVE_PARTS_MAX,
    TOKEN_LIVEPARTS_MAX_BYTES,
    TOKEN_PART_MAX_BYTES,
    TOKEN_PENDING_MAX_BYTES,
    TOKEN_REMOVED_MESSAGES_MAX,
    TOKEN_REMOVED_MESSAGES_TTL_MS,
    TOKEN_RESYNC_QUEUE_CAP,
)
from ...logging_config import get_logger
from ..hub_types import TOKEN_FRAME_TYPE
from ..replay_log import (
    FRAME_KIND_BUSINESS,
    FRAME_KIND_TOMBSTONE,
    ReplayLog,
    token_domain,
)
from ..replay_wire import sse_id_line
from .frames import (
    STOP,
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
from .frames import PartKey
from .models import DeltaAccumulator, LivePart, _TokenMetrics

if TYPE_CHECKING:
    from ..hub import HubRegistry


logger = get_logger(__name__)


# P1-21: FIFO cap on session-routing metadata to prevent unbounded growth.
# Aligned with GlobalHub._LAST_UPDATED_AT_BY_SID_MAX (same 10k pattern).
_SESSION_STATUS_MAX = 10_000


# Number of flush ticks between TTL sweeps (NB-B5: 60s cadence). Floored at 1
# so a misconfigured TOKEN_FLUSH_SECONDS still sweeps.
_TTL_TICK_INTERVAL = max(1, int(round(60.0 / TOKEN_FLUSH_SECONDS)))
# Number of flush ticks between heartbeats (§5.6 frame 6: 15s cadence).
_HEARTBEAT_TICK_INTERVAL = max(1, int(round(TOKEN_HEARTBEAT_SECONDS / TOKEN_FLUSH_SECONDS)))


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
      ``monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_...", val)``
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


class TokenStreamHub:
    """Part-lifecycle-gated accumulator + flush engine for the token stream.

    The hub holds:

    * ``live_parts`` — active text :class:`LivePart`\\ s keyed by
      :data:`PartKey`.
    * ``_nontext_parts`` — bounded insertion-ordered tombstones for
      reasoning/tool-input parts (C3, §16-B). Their ``field:"text"`` deltas
      are dropped silently to avoid the ``part_state_missing`` resync storm.
    * ``_disabled_parts`` — bounded tombstones retired by :meth:`drop_part`
      (truncated, too_large, C4, §16-B). Late deltas for these keys are
      dropped silently.
    * ``_pending`` — per-key :class:`DeltaAccumulator` awaiting the next
      flush window.
    * ``_total_live_bytes`` — sum of ``LivePart.byte_count``; the global
      LIVE memory budget gauge (Stage C ``_reserve``, Stage E 4MiB cap).
    * ``_total_pending_bytes`` — sum of ``DeltaAccumulator.byte_count``; the
      global PENDING memory budget gauge (Stage E 4MiB cap, §16-C residual
      split). Live and pending are INDEPENDENT gauges: the same delta byte
      physically occupies both ``LivePart.chunks`` (persistent authoritative
      copy) and ``DeltaAccumulator.chunks`` (transient pre-flush shadow),
      so each budget independently protects its OWN buffer — no double-count
      of a single memory region.
    * ``_session_status`` — last known upstream status per sid (busy/idle);
      drives the :meth:`ttl_sweep` busy-guard (§16-B NB#4).
    * ``_busy_sids`` — O(1) busy lookup mirror of ``_session_status``.
    * ``_pending_session_resinks`` — bounded queue of ``(sid, reason)``
      turned into per-subscriber ``resync`` frames by the flush loop (NB-B2).
    * ``_subs_by_sid`` — Stage-C subscriber fanout bookkeeping. Stage D's
      ``TokenSubscriber`` HTTP class calls :meth:`attach_subscriber` /
      :meth:`detach_subscriber` to enter/leave this ledger.
    """

    def __init__(
        self,
        *,
        max_frame_bytes: int = DEFAULT_TOKEN_MAX_FRAME_BYTES,
        replay_log: ReplayLog | None = None,
    ) -> None:
        self.live_parts: dict[PartKey, LivePart] = {}
        # B3b-2: process-wide replay log (design-v4-sse-replay §3.4). When
        # attached, every LIVE-fanout business frame (delta / snapshot done
        # marker / truncated) and every ``message.removed`` tombstone is
        # appended to the sid's token domain ("published frames" semantics —
        # logged even with zero subscribers, REPLAY-007/018) and v4
        # subscribers receive the frame with its ``id: t:<sid>:<epoch>:<seq>``
        # line prepended. ``None`` (v3-only stacks / minimal test apps) keeps
        # the pipeline byte-identical to the pre-v4 terminal state: no
        # logging, no id stamping. Per-sub handshake frames (server.connected
        # / handshake snapshots / handshake tombstone replay) and resync /
        # heartbeat frames are connection-scoped or control frames — they are
        # NEVER logged and NEVER id-stamped.
        self._replay: ReplayLog | None = replay_log
        # Bounded OrderedDicts (§16-B): key → insertion-time-ms.
        self._nontext_parts: OrderedDict[PartKey, int] = OrderedDict()
        self._disabled_parts: OrderedDict[PartKey, int] = OrderedDict()
        self._pending: dict[PartKey, DeltaAccumulator] = {}
        self._total_live_bytes: int = 0
        self._total_pending_bytes: int = 0
        self._metrics: _TokenMetrics = _TokenMetrics()
        # Stage B session-routing state (§16-B).
        # P1-21: bounded OrderedDicts with FIFO cap to prevent unbounded
        # growth across high-churn sessions.
        self._session_status: OrderedDict[str, str] = OrderedDict()
        self._busy_sids: OrderedDict[str, None] = OrderedDict()
        # Pending resyncs for the flush loop to fan out (bounded, NB-B2).
        self._pending_session_resinks: list[tuple[str, str]] = []
        # Stage C subscriber fanout (§5.5 handshake). Stage D's TokenSubscriber
        # registers here; until then attach_subscriber is exercised by tests.
        self._subs_by_sid: dict[str, set[Any]] = {}
        # L2-A (plan Task L2-A / oracle §A-1): curated-events token taps.
        # Control-plane subscribers on ``/slimapi/events?tokens=1`` register
        # their ``put`` (a :class:`~oc_slimapi.sse.hub_types.Subscriber`
        # method) here so every flushed ``(sid, mid, pid)`` window concat is
        # enqueued as a lean ``{type:"token", ...}`` frame. Reusing
        # ``Subscriber.put`` means the unchanged T3 backpressure guard
        # (overflow → ``resync{subscriber_backpressure}`` + disconnect)
        # applies with no new path. Empty list = zero per-flush overhead
        # (the ``if self.events_tap:`` gate in :meth:`flush`).
        self.events_tap: list[Any] = []
        # Per-frame byte ceiling for safe_put / emit_snapshot_or_truncated.
        self._max_frame_bytes: int = max_frame_bytes
        # Background flush task (None until start(); cancelled by stop()).
        self._flush_task: asyncio.Task | None = None
        # Stage B v0.6 §Q (per_part_revision independent): per-part
        # revision maintained LOCALLY by the token hub to resist being
        # clobbered by GlobalHub's LRU cap eviction mid-generation).
        # Typed as an ``OrderedDict`` so the FIFO cap
        # (``TOKEN_DISABLED_MAX``) can be enforced (MAJOR 5: cap/TTL
        # aligned with ``_nontext_parts`` / ``_disabled_parts``).
        #
        # rev-ogpt CRITICAL 1 (Option B — per-FRAME semantics): every
        # token frame with independent delivery semantics (snapshot /
        # delta / done marker / truncated) consumes the NEXT revision
        # via :meth:`_next_part_revision` (increment-and-return). No
        # two frames emitted for the same part ever share a revision,
        # so a client using strict ``>`` on ``partEventRevision`` never
        # drops a frame because two delivery frames happened to carry
        # the same value. Multiple deltas across multiple flush windows
        # of one ``message.part.updated`` event each get a distinct
        # (incrementing) revision; the residual-delta-then-done-marker
        # pair from text-end likewise get distinct revisions; a
        # snapshot-then-truncated pair (oversized path) get distinct
        # revisions. ``on_part_updated`` itself does NOT bump — bumps
        # happen only when a frame is actually emitted (MAJOR 5:
        # non-text / disabled / malformed events create no revision).
        self._part_revisions: OrderedDict[PartKey, int] = OrderedDict()
        # Stage B v0.6 §P.2 (MAJOR 4 方案 C): bounded replay queue for
        # upstream ``message.removed`` tombstones. Keyed by (sessionID,
        # messageID) → insertion-time-ms (OrderedDict preserves FIFO order).
        # Global FIFO cap (``TOKEN_REMOVED_MESSAGES_MAX``) + 24h TTL
        # (``TOKEN_REMOVED_MESSAGES_TTL_MS``). Pruned on-insert via
        # :meth:`_prune_removed_messages` and periodically by
        # :meth:`ttl_sweep`. Survives ``on_upstream_reconnect`` (replay is
        # the whole point — a reconnecting client must learn about
        # messages removed while it was disconnected).
        self._removed_messages: OrderedDict[tuple[str, str], int] = OrderedDict()
        # rev-ogpt CRITICAL 2: gate set of (sessionID, messageID) whose
        # upstream ``message.removed`` has been processed. Ingest paths
        # (``on_part_updated`` / ``on_part_delta`` / ``on_part_removed``)
        # check this BEFORE accepting the event, so a late part update
        # cannot revive a removed message. Lifetime coupled to the
        # replay queue: when ``_prune_removed_messages`` evicts an entry
        # (TTL or FIFO cap), the matching gate entry is discarded too.
        # Cleared wholesale by ``on_upstream_reconnect`` (new epoch) and
        # ``on_session_deleted(sid)``.
        self._retired_messages: set[tuple[str, str]] = set()
        # P1-22: bounded deleted-sid gate. Records sids whose
        # ``session.deleted`` has been processed so late part events
        # (``message.part.updated`` / ``.delta`` / ``.removed``) for a
        # deleted session are dropped before they can resurrect a LivePart.
        # Cap + TTL aligned with ``TOKEN_REMOVED_MESSAGES_MAX`` /
        # ``TOKEN_REMOVED_MESSAGES_TTL_MS`` (same constants as the replay
        # queue — a session whose deletion tombstone has aged out of the
        # replay queue is acceptably treated as "new" again). Cleared on
        # ``on_upstream_reconnect`` (new epoch).
        self._deleted_sids: OrderedDict[str, int] = OrderedDict()

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
                self._fanout_frame(
                    key,
                    _delta_frame(
                        key, text,
                        part_revision=self._next_part_revision(key),
                    ),
                )
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
            self._fanout_frame(
                key,
                _delta_frame(
                    key, text,
                    part_revision=self._next_part_revision(key),
                ),
            )
        for key in [k for k, v in self._pending.items() if k[0] == sid and not v.chunks]:
            self._pending.pop(key, None)

    # ------------------------------------------------------------------
    # Ingest (called from GlobalHub.publish)
    # ------------------------------------------------------------------
    def on_part_updated(
        self, props: dict[str, Any], part_revision: int | None = None,
    ) -> None:
        """Handle ``message.part.updated`` (design §5.3).

        ``text-start`` (``part.time.end is None``) creates a
        :class:`LivePart` regardless of subscribers — this is the B1
        invariant that lets a subscriber attach mid-generation and still
        see the full accumulated text via the handshake snapshot. Repeated
        starts are idempotent (we never reset an existing accumulator: a
        middle ``updated`` frame must not clobber accumulated deltas).
        ``text-end`` triggers :meth:`finish_part` (Stage C: synchronous
        drain + terminal marker + retire). Non-text parts are recorded in
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
        frame (delta / snapshot / done / truncated) gets a strictly
        increasing revision, so a client using strict ``>`` on
        ``partEventRevision`` reliably accepts every delivery frame.

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
        # TODO(§13.2): confirm live wire key casing for properties.part.
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
        # happen lazily in the emit paths (flush / finish_part / snapshot
        # / truncated) via ``_next_part_revision``.
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
        # text-end → finish_part: synchronous drain(_pending) +
        # snapshot{done:true} marker (lever 1, no text) fanout + retire (C1).
        raw_final = part.get("text")
        final_text = raw_final if isinstance(raw_final, str) else ""
        self.finish_part(key, final_text)

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
        # TODO(§13.2): confirm live wire key casing for properties fields.
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
                self._fanout_frame(
                    key,
                    _delta_frame(
                        key, text,
                        part_revision=self._next_part_revision(key),
                    ),
                )
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
           stale delta / snapshot frames from a late part event.
        2. Fan ``message.removed`` to every current token subscriber of
           the session.
        3. Record the tombstone in the bounded replay queue so a client
           that attaches AFTER the removal (or reconnects post-upstream-
           loss) learns about it during the handshake.

        rev-ogpt MAJOR 6: a duplicate ``message.removed`` for an
        already-recorded (sid, mid) refreshes the replay-queue timestamp
        AND ``move_to_end``s the key, so the "newest" tombstone is never
        the oldest in FIFO order (v0.5 only refreshed the timestamp,
        leaving the duplicate at its original insertion position → the
        cap could evict the freshest data).

        The replay queue is global (not per-session) and survives
        ``on_upstream_reconnect`` — its whole purpose is to bridge the
        gap for clients that were disconnected when the removal happened.
        """
        # CRITICAL 2: retire BEFORE fanout so subscribers cannot observe
        # stale state after the tombstone frame.
        self._retire_message(sid, mid)
        # Live fanout to current subscribers of this session.
        self._fanout_message_removed(sid, mid)
        # Record in replay queue. MAJOR 6: always update value + move to
        # end (refresh TTL + FIFO position) so duplicates do not leave a
        # stale insertion-order entry that the cap could evict prematurely.
        now_ms = _now_ms()
        self._removed_messages[(sid, mid)] = now_ms
        self._removed_messages.move_to_end((sid, mid))
        self._prune_removed_messages(now_ms)

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
    # finish_part (C1 + lever 1) — terminal drain + marker + retire
    # ------------------------------------------------------------------
    def finish_part(self, key: PartKey, final_text: str) -> None:
        """Synchronous drain + terminal ``snapshot{done:true}`` + retire (C1).

        Lever 1 (§16-C): the terminal marker carries NO ``text`` — the
        authoritative text is delivered by the existing digest → ``/since``
        path. ``final_text`` is accepted for API symmetry (it is the
        ``part.text`` from the text-end ``message.part.updated``) but is
        NOT put on the wire; we only use the part-existence check to decide
        whether to emit the marker at all.

        Order (§5.6 wire-strong invariant): residual ``_pending`` is
        drained and fanned as a ``delta`` frame BEFORE the ``done:true``
        marker — same-tick, synchronous, no await window. Then the marker
        fans to every subscriber of the sid. Then :meth:`drop_part` retires
        the part (idempotent — late deltas for the key silently drop on
        ``_disabled``).

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): the residual delta
        frame consumes its own revision via :meth:`_next_part_revision`,
        and the ``done:true`` marker consumes the NEXT revision (strictly
        greater). A client using strict ``>`` therefore accepts both
        frames (the pre-fix per-event design gave them the same revision
        → strict ``>`` would have silently dropped the done marker).

        If the part was already retired (truncate / eviction / TTL) the
        marker is suppressed — the subscriber already learned about the
        part's fate via ``snapshot{truncated:true}`` or
        ``resync{token_memory_limit}``.
        """
        # C1: synchronous drain of residual pending → fanout delta FIRST.
        # CRITICAL 1 (Option B): the residual delta consumes its own
        # strictly-increasing revision (distinct from the done marker
        # below — both are independent delivery frames).
        acc = self._pending.pop(key, None)
        if acc is not None and acc.byte_count:
            pending_bytes = acc.byte_count
            text = acc.drain()
            self._total_pending_bytes = max(0, self._total_pending_bytes - pending_bytes)
            if text:
                self._fanout_frame(
                    key,
                    _delta_frame(
                        key, text,
                        part_revision=self._next_part_revision(key),
                    ),
                )
        # Lever 1: terminal marker (no text) — only if the LivePart still
        # exists. A truncated / evicted / TTL-retired key has no LivePart
        # and the subscriber already received the appropriate frame.
        # CRITICAL 1 (Option B): the done marker consumes the NEXT
        # revision (strictly greater than the residual delta above when
        # both are emitted).
        if key in self.live_parts:
            marker = _snapshot_frame(
                key, text=None, done=True,
                part_revision=self._next_part_revision(key),
            )
            self._fanout_frame(key, marker)
        # Retire via drop_part (idempotent). Disabling ensures any late
        # delta for this key silently drops on _disabled (no orphan noise).
        self.drop_part(key)

    # ------------------------------------------------------------------
    # Session routing (Stage B, §16-B)
    # ------------------------------------------------------------------
    def on_session_status(self, sid: str, status: str) -> None:
        """Record upstream session.status and trigger idle cleanup (§16-B).

        WHY only busy/idle are recorded: opencode's session lifecycle uses
        these two states as the authoritative "generation in progress" /
        "generation done" signals; other transient states (e.g. ``shared``)
        carry no actionable meaning for the accumulator and are ignored so
        the :meth:`ttl_sweep` busy-guard keys off a known-good signal.

        WHY idle→retire immediately + pending resync: the upstream has
        authoritatively said this session is done. Any LiveParts still
        hanging are abandoned text-ends we missed (opencode bug, sidecar
        restart, etc.) — clearing them prevents orphan-LivePart leakage.
        We enqueue a ``session_idle`` resync (new reason; clients already
        handle it per ocdroid §3.9) for the flush loop to fan out so
        subscribers drop stale stream state and re-fetch authoritative
        /since.
        """
        if status not in ("busy", "idle"):
            return
        self._session_status[sid] = status
        self._session_status.move_to_end(sid)
        self._prune_session_status()
        if status == "busy":
            self._busy_sids[sid] = None
            self._busy_sids.move_to_end(sid)
            self._prune_busy_sids()
            return
        # idle
        self._busy_sids.pop(sid, None)
        self._retire_session(sid)
        self._enqueue_session_resync(sid, "session_idle")

    def on_session_deleted(self, sid: str) -> None:
        """Clear all state for a deleted session (§16-B) + terminate token
        subscribers (INV-4 / P0-3).

        WHY no separate ``reconnect_no_replay`` reason here: deleted
        sessions are signalled to clients via the existing control-plane
        digest (``session.deleted`` in ``session.digest``).

        INV-4 (P0-3): session.deleted is a **server-side termination signal**
        for token subscribers. Each subscriber for this sid receives
        ``resync{session_deleted} → STOP`` via :meth:`TokenSubscriber.terminate`.
        The generator receives STOP → breaks → finally →
        :meth:`TokenStreamRegistry.unsubscribe` (normal path: detach +
        decrement + last-detach stop flush + grace arm). Previously only a
        resync was enqueued via the flush loop (no STOP) — the subscriber
        connection stayed open forever (resource leak). The direct
        ``terminate`` replaces ``_enqueue_session_resync`` so the frames
        are delivered immediately in the correct order (resync THEN STOP),
        not deferred to the next flush tick where STOP could precede resync.

        WHY also clear ``_session_status`` / ``_busy_sids`` here (but NOT
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
        self._busy_sids.pop(sid, None)
        # CRITICAL 2 gate cleanup for this session.
        for key in [k for k in self._retired_messages if k[0] == sid]:
            self._retired_messages.discard(key)
        # P1-22: record sid in the deleted-sid gate so late part events
        # for this session are dropped (no LivePart resurrection).
        self._remember_deleted_sid(sid)
        # INV-4 (P0-3): server-side termination — directly terminate each
        # subscriber (resync{session_deleted} → STOP). Do NOT detach or
        # decrement; the generator's finally → unsubscribe handles that.
        for sub in tuple(self._subs_by_sid.get(sid, ())):
            sub.terminate("session_deleted")

    def _retire_session(self, sid: str) -> None:
        """Clear the 4 part-state structures for a session (§16-B).

        Scope (per spec): ``live_parts`` / ``_pending`` / ``_nontext_parts``
        / ``_disabled_parts`` only. ``_session_status`` / ``_busy_sids``
        are NOT touched — they outlive a part-level retire so the TTL
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
        retiring its LivePart would drop accumulated text and force a
        snapshot resync on the next delta, defeating the whole point of
        the accumulator. We only retire when the upstream has explicitly
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
        # Stage B v0.6 §P.2: prune expired + overflow entries from the
        # message.removed replay queue (TTL 24h + FIFO cap 1000).
        self._prune_removed_messages(now_ms)
        return retired

    # ------------------------------------------------------------------
    # Subscribe fanout bookkeeping (§5.5 handshake, §5.7 stream-perspective)
    # ------------------------------------------------------------------
    def attach_subscriber(self, sid: str, sub: Any) -> None:
        """Stage D's ``TokenSubscriber`` calls this on HTTP connect.

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
    def _replay_publish_token(
        self, sid: str, frame: bytes, kind: str = FRAME_KIND_BUSINESS
    ) -> bytes | None:
        """Append ``frame`` to the sid's replay domain; return its id line.

        B3b-2 choke point for token-domain business frames. Called on the
        LIVE fanout path (``_fanout_frame`` / ``_fanout_message_removed`` /
        ``_truncate_part_for_all``) BEFORE the no-subscriber early return —
        the log records *published* frames, not *delivered* ones, so frames
        emitted while a subscriber was overflowed/disconnected still replay
        (REPLAY-007). Returns ``None`` when no replay log is wired or the
        append degrades (bookkeeping must never fail publishing); the
        caller then delivers the raw frame unchanged.
        """
        if self._replay is None:
            return None
        try:
            entry = self._replay.append(token_domain(sid), frame, kind=kind)
        except Exception:  # noqa: BLE001 — publishing never fails on log errors
            logger.warning("replay log append failed for sid %r", sid, exc_info=True)
            return None
        return sse_id_line(token_domain(sid), self._replay.epoch, entry.seq)

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

    def _fanout_frame(self, key: PartKey, frame: bytes) -> None:
        """Fan a frame to every subscriber of the key's sid + count emits.

        B3b-2: the frame is appended to the sid's replay domain FIRST
        (published semantics — logged even with zero subscribers), then
        delivered with per-sub id stamping for v4 connections.
        """
        sid = key[0]
        id_line = self._replay_publish_token(sid, frame)
        delivered = self._deliver_logged(sid, frame, id_line)
        self._metrics.flushed_frames_total += delivered

    def _fanout_message_removed(self, sid: str, mid: str) -> None:
        """Fan a ``message.removed`` frame to every subscriber of ``sid``.

        Stage B v0.6 §P.2 (MAJOR 4 方案 C): the live fanout half of
        ``on_message_removed``. The replay half is handled by
        ``_removed_messages`` + the handshake replay in
        :meth:`attach_subscriber`.

        B3b-2: the tombstone is ALSO appended to the sid's replay domain
        with :data:`FRAME_KIND_TOMBSTONE` — a reconnecting client that
        missed the live frame replays it WITH its ``id:`` (it consumes a
        seq exactly like a business frame, keeping the ID sequence
        hole-free; REPLAY-012).
        """
        frame = _message_removed_frame(sid, mid)
        id_line = self._replay_publish_token(sid, frame, kind=FRAME_KIND_TOMBSTONE)
        delivered = self._deliver_logged(sid, frame, id_line)
        self._metrics.flushed_frames_total += delivered

    def _fanout_resync(self, sid: str, reason: str) -> None:
        """Fan ``resync{reason, sessionID}`` to every subscriber of sid."""
        subs = self._subs_by_sid.get(sid)
        if not subs:
            return
        frame = _resync_frame(sid, reason)
        for sub in tuple(subs):
            sub.put(frame)

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
        # B3b-2: the truncated frame is a live-fanout business frame — it is
        # replay-logged (a reconnecting client must learn the part was
        # dropped) and id-stamped for v4 subscribers.
        id_line = self._replay_publish_token(sid, trunc)
        self._deliver_logged(sid, trunc, id_line)
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
        self._fanout_resync(sid, "token_memory_limit")
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

    def _prune_busy_sids(self) -> None:
        """P1-21: FIFO cap on ``_busy_sids`` to prevent unbounded growth."""
        while len(self._busy_sids) > _SESSION_STATUS_MAX:
            self._busy_sids.popitem(last=False)

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

        rev-ogpt CRITICAL 2: ``_retired_messages`` is also cleared — the
        new upstream epoch starts fresh, late events from the previous
        epoch cannot arrive (GlobalBus has no replay). ``_removed_messages``
        (replay queue) is INTENTIONALLY preserved so a client reconnecting
        post-upstream-loss still learns about prior message removals
        during its handshake.
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
        self._busy_sids.clear()
        self._pending_session_resinks.clear()
        # CRITICAL 2 gate: new epoch, no late events from the previous
        # epoch can arrive.
        self._retired_messages.clear()
        # P1-22: deleted-sid gate cleared (new epoch — deleted sessions
        # from the old epoch cannot send late part events).
        self._deleted_sids.clear()
        # NOTE: _removed_messages (replay queue) AND _part_revisions are
        # intentionally NOT cleared — see docstring.
        # Fan reconnect_no_replay to every sid with an attached subscriber.
        # (If subscribers themselves were torn down by the HTTP layer during
        # the reconnect, _subs_by_sid is already empty — no-op.)
        for sid in list(self._subs_by_sid.keys()):
            self._fanout_resync(sid, "reconnect_no_replay")
