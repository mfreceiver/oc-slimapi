"""Token-stream SSE accumulator (design-token-stream.md §5.3).

Stage scope:

* **Stage A** (DONE, gate-PASSED): data structures + ingest (``on_part_updated``
  / ``on_part_delta``) + idempotent ``drop_part`` + ``_token_hub`` injection.
* **Stage B** (DONE, gate-PASSED): bounded tombstones (``_disabled_parts`` /
  ``_nontext_parts`` with cap + TTL), session routing
  (``on_session_status`` / ``on_session_deleted`` + ``_retire_session``),
  TTL busy-guard (``ttl_sweep`` reading ``_session_status``), and reconnect
  state-clear via ``on_upstream_reconnect``.
* **Stage C** (DONE): ``flush_loop`` (100ms / 4KiB / sorted-by-key +
  60s ttl_sweep tick), ``finish_part`` synchronous drain + terminal
  ``snapshot{done:true}`` marker (lever 1 — no text), ``_reserve`` global
  memory accounting + oldest-eviction, ``safe_put`` / truncate-fanout (C6),
  wire frames (snapshot/delta/truncated/resync/heartbeat/server.connected),
  bounded ``_pending_session_resinks`` drain (NB-B2), and subscribe
  fanout bookkeeping (``attach_subscriber`` / ``detach_subscriber``).
* **Stage D** (DONE): HTTP endpoint, ``TokenSubscriber`` + admission registry,
  health ``features.tokenStream``, metrics, gzip (lever 2), and
  ``HubRegistry._removal_task`` cancel on token subscribe.
* **Stage E** (THIS FILE — code lane): memory budget split 4+4 (Option B,
  §16-C residual). ``TOKEN_LIVEPARTS_MAX_BYTES`` (4MiB) bounds LivePart
  authoritative text; ``TOKEN_PENDING_MAX_BYTES`` (4MiB) bounds the
  DeltaAccumulator transient flush window. Pending overflow → force-flush;
  no-subscribers / still-over → LRU evict oldest LivePart + resync.

This module owns the *part-lifecycle-gated* accumulator that turns the
per-token ``message.part.delta`` firehose into batched SSE frames for the
future ``GET /slimapi/sessions/{sid}/stream`` endpoint.

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
import contextlib
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import orjson

from ..config import (
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

if TYPE_CHECKING:
    from .hub import HubRegistry


# Terminal sentinel enqueued by the overflow path so the SSE generator tears
# the connection down promptly (mirrors hub.STOP; kept local to avoid a
# runtime import cycle — hub.py imports this module only under TYPE_CHECKING).
STOP = object()


# Key for a single text part within a session+message.
PartKey = tuple[str, str, str]  # (sessionID, messageID, partID)

# Number of flush ticks between TTL sweeps (NB-B5: 60s cadence). Floored at 1
# so a misconfigured TOKEN_FLUSH_SECONDS still sweeps.
_TTL_TICK_INTERVAL = max(1, int(round(60.0 / TOKEN_FLUSH_SECONDS)))
# Number of flush ticks between heartbeats (§5.6 frame 6: 15s cadence).
_HEARTBEAT_TICK_INTERVAL = max(1, int(round(TOKEN_HEARTBEAT_SECONDS / TOKEN_FLUSH_SECONDS)))


def _now_ms() -> int:
    """Epoch milliseconds.

    Duplicated from :mod:`oc_slimapi.sse.hub` deliberately: ``hub.py``
    references :class:`TokenStreamHub` only under ``TYPE_CHECKING``, but a
    runtime ``from .hub import _now_ms`` here would still create a cycle at
    import time. The helper is one line; keeping it local avoids the dance.
    """
    return int(time.time() * 1000)


def sse_frame(payload: dict[str, Any], event: str | None = None) -> bytes:
    """Serialize ``payload`` as one SSE frame.

    Duplicated from :mod:`oc_slimapi.sse.hub` for the same import-cycle
    reason as :func:`_now_ms` (and to keep this module's wire format
    self-contained). The format is stable: ``event: <name>\\n`` (optional) +
    ``data: <json>\\n\\n``. Both copies share :mod:`orjson` so JSON encoding
    (key order, UTF-8, escaping) is byte-identical.
    """
    prefix = f"event: {event}\n" if event else ""
    return prefix.encode() + b"data: " + orjson.dumps(payload) + b"\n\n"


# ---------------------------------------------------------------------------
# Wire frame builders (design §5.6). Payload key order matches the spec so
# snapshot/delta frames are byte-stable for snapshot tests. ``text`` is omitted
# from the terminal marker (lever 1).
# ---------------------------------------------------------------------------

def _snapshot_frame(key: PartKey, text: str | None, done: bool) -> bytes:
    payload: dict[str, Any] = {
        "sessionID": key[0],
        "messageID": key[1],
        "partID": key[2],
        "done": done,
    }
    if text is not None:
        payload["text"] = text
    return sse_frame(payload, event="message.part.snapshot")


def _delta_frame(key: PartKey, text: str) -> bytes:
    return sse_frame(
        {"sessionID": key[0], "messageID": key[1], "partID": key[2], "text": text},
        event="message.part.delta",
    )


def _truncated_frame(key: PartKey, done: bool) -> bytes:
    return sse_frame(
        {
            "sessionID": key[0],
            "messageID": key[1],
            "partID": key[2],
            "truncated": True,
            "done": done,
        },
        event="message.part.snapshot",
    )


def _resync_frame(sid: str, reason: str) -> bytes:
    return sse_frame({"reason": reason, "sessionID": sid}, event="resync")


def _connected_frame(sid: str) -> bytes:
    return sse_frame({"sessionID": sid}, event="server.connected")


def _heartbeat_frame() -> bytes:
    return sse_frame({}, event="server.heartbeat")


@dataclass
class LivePart:
    """One in-flight text part (design §5.3).

    ``chunks`` is a list (NOT ``text += delta``) so appending is O(1) and
    the full text is materialized once, on demand, via
    ``"".join(chunks)``. ``byte_count`` is the UTF-8 sum of ``chunks``; it
    is the budget unit for the per-part and global memory caps (Stage C
    ``_reserve``). ``last_delta_ms`` feeds the Stage-B TTL retiree (only
    retires an idle LivePart when the session is known idle, not just
    quiet — bgpt NB#4) AND the Stage-C LRU eviction key (oldest by
    ``last_delta_ms`` is evicted first under global memory pressure).
    """

    chunks: list[str] = field(default_factory=list)
    byte_count: int = 0
    ended: bool = False
    last_delta_ms: int = field(default_factory=lambda: _now_ms())


@dataclass
class DeltaAccumulator:
    """Per-key flush window (design §5.4 C1).

    Chunk-list + UTF-8 byte counter. :meth:`drain` joins the chunks, clears
    the list, and resets ``byte_count`` so the accumulator is reusable for
    the next window. flush_loop calls ``drain()`` when either
    ``TOKEN_FLUSH_SECONDS`` (100ms) or ``TOKEN_FLUSH_BYTES`` (4KiB) trips.
    """

    chunks: list[str] = field(default_factory=list)
    byte_count: int = 0

    def append(self, text: str) -> None:
        """Append ``text`` and bump the UTF-8 byte counter. No-op on empty."""
        if not text:
            return
        self.chunks.append(text)
        self.byte_count += len(text.encode("utf-8"))

    def drain(self) -> str:
        """Join chunks, clear state, return the joined text.

        Resetting ``byte_count`` to 0 keeps the accumulator reusable across
        flush windows without the caller having to reconstruct it.
        """
        if not self.chunks:
            self.byte_count = 0
            return ""
        text = "".join(self.chunks)
        self.chunks.clear()
        self.byte_count = 0
        return text


@dataclass
class _TokenMetrics:
    """Counters surfaced via ``/slimapi/metrics`` (Stage D wires the endpoint).

    Stage A exercised ``orphan_deltas``; Stage C exercises
    ``flushed_frames_total`` / ``truncated_snapshots_total`` /
    ``token_memory_limit_total`` / ``dropped_frames_total``. Stage D will
    expose all of them under ``sse.tokenStream.*``.
    """

    orphan_deltas: int = 0
    flushed_frames_total: int = 0       # Stage C: delta + marker + snapshot emits
    dropped_frames_total: int = 0       # Stage C: oversized non-snapshot frames dropped
    truncated_snapshots_total: int = 0  # Stage C: snapshot{truncated:true} fans
    token_memory_limit_total: int = 0   # Stage C: resync{token_memory_limit} fans


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
        """
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
        """
        if not self.drop_part(key):
            return  # already disabled — eviction resync already fanned.
        self._fanout_resync(key[0], "token_memory_limit")
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


# ===========================================================================
# Stage D — HTTP subscriber + independent admission registry (§5.5 / §6 / §16-D)
# ===========================================================================

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
        token_hub: TokenStreamHub,
        hub_registry: HubRegistry,
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
        """``sse.tokenStream.*`` block for the metrics endpoint (design §7)."""
        th = self.token_hub
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
        }
