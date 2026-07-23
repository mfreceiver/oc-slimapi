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
    TOKEN_RESYNC_QUEUE_CAP,
)
from .frames import (
    STOP,
    _connected_frame,
    _delta_frame,
    _heartbeat_frame,
    _now_ms,
    _resync_frame,
    _snapshot_frame,
    _truncated_frame,
)
from .frames import PartKey
from .models import DeltaAccumulator, LivePart, _TokenMetrics

if TYPE_CHECKING:
    from ..hub import HubRegistry


# Number of flush ticks between TTL sweeps (NB-B5: 60s cadence). Floored at 1
# so a misconfigured TOKEN_FLUSH_SECONDS still sweeps.
_TTL_TICK_INTERVAL = max(1, int(round(60.0 / TOKEN_FLUSH_SECONDS)))
# Number of flush ticks between heartbeats (§5.6 frame 6: 15s cadence).
_HEARTBEAT_TICK_INTERVAL = max(1, int(round(TOKEN_HEARTBEAT_SECONDS / TOKEN_FLUSH_SECONDS)))


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

    def __init__(self, *, max_frame_bytes: int = DEFAULT_TOKEN_MAX_FRAME_BYTES) -> None:
        self.live_parts: dict[PartKey, LivePart] = {}
        # Bounded OrderedDicts (§16-B): key → insertion-time-ms.
        self._nontext_parts: OrderedDict[PartKey, int] = OrderedDict()
        self._disabled_parts: OrderedDict[PartKey, int] = OrderedDict()
        self._pending: dict[PartKey, DeltaAccumulator] = {}
        self._total_live_bytes: int = 0
        self._total_pending_bytes: int = 0
        self._metrics: _TokenMetrics = _TokenMetrics()
        # Stage B session-routing state (§16-B).
        self._session_status: dict[str, str] = {}
        self._busy_sids: set[str] = set()
        # Pending resyncs for the flush loop to fan out (bounded, NB-B2).
        self._pending_session_resinks: list[tuple[str, str]] = []
        # Stage C subscriber fanout (§5.5 handshake). Stage D's TokenSubscriber
        # registers here; until then attach_subscriber is exercised by tests.
        self._subs_by_sid: dict[str, set[Any]] = {}
        # Per-frame byte ceiling for safe_put / emit_snapshot_or_truncated.
        self._max_frame_bytes: int = max_frame_bytes
        # Background flush task (None until start(); cancelled by stop()).
        self._flush_task: asyncio.Task | None = None

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
        """
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self.flush_loop())

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
                self._fanout_frame(key, _delta_frame(key, text))
            # Clean up empty accumulators (drain cleared their chunks).
            for key in [k for k, v in self._pending.items() if not v.chunks]:
                self._pending.pop(key, None)
        self._drain_pending_session_resyncs()
        t1 = time.perf_counter()
        self._metrics.flush_duration_ms_total += (t1 - t0) * 1000.0
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
            self._fanout_frame(key, _delta_frame(key, text))
        for key in [k for k, v in self._pending.items() if k[0] == sid and not v.chunks]:
            self._pending.pop(key, None)

    # ------------------------------------------------------------------
    # Ingest (called from GlobalHub.publish)
    # ------------------------------------------------------------------
    def on_part_updated(self, props: dict[str, Any]) -> None:
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
        t = part.get("time")
        if not isinstance(t, dict):
            return
        if part.get("type") != "text":
            # C3: reasoning/tool-input part — record so its field:"text"
            # deltas drop silently. NEVER create a LivePart for these.
            self._remember_nontext(key)
            return
        if t.get("end") is None:
            # NB1: type-guard the seed — part.get("text") may be a non-string
            # in a malformed / future upstream payload; an int/dict seed
            # would corrupt chunk-list byte accounting downstream.
            raw_seed = part.get("text")
            seed = raw_seed if isinstance(raw_seed, str) else ""
            # NB3: post-drop text-start guard — if the key was disabled
            # after the original start (truncate/too_large), a re-issued
            # text-start MUST NOT create a dead LivePart. Late deltas would
            # still drop on _disabled (so no leak to subscribers), but a
            # dangling LivePart would confuse the snapshot fanout into
            # emitting an already-truncated part.
            if key not in self.live_parts and not self._is_disabled(key):
                self._start_part(key, seed)
            return
        # text-end → finish_part: synchronous drain(_pending) +
        # snapshot{done:true} marker (lever 1, no text) fanout + retire (C1).
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
        """
        # TODO(§13.2): confirm live wire key casing for properties fields.
        if props.get("field") != "text":
            return
        sid = props.get("sessionID")
        mid = props.get("messageID")
        pid = props.get("partID")
        if not all(isinstance(x, str) and x for x in (sid, mid, pid)):
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
                self._fanout_frame(key, _delta_frame(key, text))
        # Stage E (§16-C residual): global PENDING budget check. Runs AFTER
        # the per-key early-flush (which may have already popped this key's
        # accumulator). The pending budget is GLOBAL — many small
        # accumulators can collectively exceed the cap even when no single
        # one trips TOKEN_FLUSH_BYTES.
        self._check_pending_budget(key)

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

        If the part was already retired (truncate / eviction / TTL) the
        marker is suppressed — the subscriber already learned about the
        part's fate via ``snapshot{truncated:true}`` or
        ``resync{token_memory_limit}``.
        """
        # C1: synchronous drain of residual pending → fanout delta FIRST.
        acc = self._pending.pop(key, None)
        if acc is not None and acc.byte_count:
            pending_bytes = acc.byte_count
            text = acc.drain()
            self._total_pending_bytes = max(0, self._total_pending_bytes - pending_bytes)
            if text:
                self._fanout_frame(key, _delta_frame(key, text))
        # Lever 1: terminal marker (no text) — only if the LivePart still
        # exists. A truncated / evicted / TTL-retired key has no LivePart
        # and the subscriber already received the appropriate frame.
        if key in self.live_parts:
            marker = _snapshot_frame(key, text=None, done=True)
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
        if status == "busy":
            self._busy_sids.add(sid)
            return
        # idle
        self._busy_sids.discard(sid)
        self._retire_session(sid)
        self._enqueue_session_resync(sid, "session_idle")

    def on_session_deleted(self, sid: str) -> None:
        """Clear all state for a deleted session (§16-B).

        WHY no separate ``reconnect_no_replay`` reason here: deleted
        sessions are signalled to clients via the existing control-plane
        digest (``session.deleted`` in ``session.digest``). Token-stream
        subscribers will be torn down by the Stage-D HTTP layer when they
        try to read a deleted session; recording a ``session_deleted``
        resync note lets the flush loop opportunistically nudge any
        attached token subscriber, but the authoritative eviction path is
        the control plane.

        WHY also clear ``_session_status`` / ``_busy_sids`` here (but NOT
        in ``_retire_session``): per spec ``_retire_session`` only owns
        the 4 part-state structures; the session's status record outlives
        a part-level retire. Deletion, however, is terminal for the whole
        session — there's no future status to remember.
        """
        self._retire_session(sid)
        self._session_status.pop(sid, None)
        self._busy_sids.discard(sid)
        self._enqueue_session_resync(sid, "session_deleted")

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
            retired.append(key)
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
        2. :meth:`flush_sid` — existing subscribers for this sid receive
           any residual ``_pending`` as ``delta`` frames. The new sub is
           NOT yet in ``_subs_by_sid`` so it does not receive them.
        3. For each active text LivePart for this sid (sorted by key):
           emit ``snapshot{done:false}`` with the FULL accumulated text
           (``"".join(chunks)``) to the new sub. Because accumulation is
           decoupled from subscribers (B1), this snapshot has no gap. C6:
           if the snapshot frame exceeds ``max_frame_bytes`` the sub
           receives ``snapshot{truncated:true}`` instead and the part is
           dropped (via :meth:`_emit_snapshot_or_truncated`).
        4. Add the sub to ``_subs_by_sid[sid]`` — only NOW does it enter
           the fanout for future deltas / markers.

        §5.7 completion alignment: the stream-perspective ``done:true``
        marker and the digest → ``/since`` authoritative text are
        independent; the snapshot here is the "stream has caught up to
        the accumulated state" baseline for this subscriber.
        """
        # 1. server.connected first.
        sub.put(_connected_frame(sid))
        # 2. flush_sid drains pending for EXISTING subscribers.
        self.flush_sid(sid)
        # 3. snapshot each active LivePart for this sid (sorted for determinism).
        for key in sorted(k for k in self.live_parts if k[0] == sid):
            live = self.live_parts[key]
            text = "".join(live.chunks)
            self._emit_snapshot_or_truncated(sub, key, text, done=False)
        # 4. enter fanout.
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
    def _fanout_frame(self, key: PartKey, frame: bytes) -> None:
        """Fan a frame to every subscriber of the key's sid + count emits."""
        sid = key[0]
        subs = self._subs_by_sid.get(sid)
        if not subs:
            return
        for sub in tuple(subs):
            sub.put(frame)
        self._metrics.flushed_frames_total += len(subs)

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
        """
        frame = _snapshot_frame(key, text, done)
        if len(frame) <= self._max_frame_bytes:
            sub.put(frame)
            return
        # Oversized → C6 backstop. _truncate_part_for_all fans truncated to
        # EXISTING subs + drop_part. If THIS sub is already in the fanout
        # set, the fanout just delivered to it — no direct put needed. The
        # handshake path (sub not yet in fanout) needs the direct put.
        sid = key[0]
        in_fanout = sub in self._subs_by_sid.get(sid, ())
        self._truncate_part_for_all(key, done)
        if not in_fanout:
            sub.put(_truncated_frame(key, done))

    def _truncate_part_for_all(self, key: PartKey, done: bool) -> None:
        """C6 backstop: fan ``snapshot{truncated:true}`` to ALL subscribers of
        the key's sid, then :meth:`drop_part`.

        Idempotent via :meth:`drop_part` (returns True the first time only)
        — the truncated frame is emitted exactly once per part even if
        multiple code paths race (per-tick snapshot fanout, _reserve
        per-part overflow, finish_part terminal marker).
        """
        if not self.drop_part(key):
            return  # already disabled — truncate frame already fanned.
        sid = key[0]
        trunc = _truncated_frame(key, done)
        for sub in tuple(self._subs_by_sid.get(sid, ())):
            sub.put(trunc)
        self._metrics.truncated_snapshots_total += 1

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
            self._evict_part_for_memory(oldest)
        return True

    def _evict_part_for_memory(self, key: PartKey) -> None:
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
        subs = list(self._subs_by_sid.get(sid, ()))
        if not subs:
            return
        for live_key in sorted(k for k in self.live_parts if k[0] == sid):
            live = self.live_parts[live_key]
            text = "".join(live.chunks)
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
                self._evict_part_for_memory(oldest)

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
            self._evict_part_for_memory(oldest)
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
                self._evict_part_for_memory(oldest)

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
        """
        self.live_parts.clear()
        self._nontext_parts.clear()
        self._disabled_parts.clear()
        self._pending.clear()
        self._total_live_bytes = 0
        self._total_pending_bytes = 0
        # Session-routing state: clear wholesale (epoch reset).
        self._session_status.clear()
        self._busy_sids.clear()
        self._pending_session_resinks.clear()
        # Fan reconnect_no_replay to every sid with an attached subscriber.
        # (If subscribers themselves were torn down by the HTTP layer during
        # the reconnect, _subs_by_sid is already empty — no-op.)
        for sid in list(self._subs_by_sid.keys()):
            self._fanout_resync(sid, "reconnect_no_replay")
