"""Token-stream accumulator — part-lifecycle-gated accumulator + flush engine.

Moved from :mod:`oc_slimapi.sse.token_hub`.

Key invariants (design §5.3 / §5.4 / §5.6):

* **Accumulation is decoupled from subscribers** — a ``text-start``
  (``part.time.end is None``) creates a :class:`LivePart` immediately,
  even if nobody is subscribed yet. A newly attached native-v4 subscriber
  aligns historical state through ReplayLog replay and authoritative HTTP.
* ``field != "text"`` deltas and ``part.type != "text"`` parts are
  silently dropped and counted (C3). Reasoning/tool-input parts reuse
  ``field:"text"`` upstream, so we MUST key the non-text decision off
  ``part.type`` from ``message.part.updated`` — hence the
  ``_nontext_parts`` ledger.
* Orphan deltas (no text-start observed — e.g. sidecar restart mid gen)
  are silently dropped + counted, NEVER triggering a resync storm (C3).
* :meth:`drop_part` (oversized / too_large, C4) is idempotent: the first
  call records the key in ``_disabled_parts`` and returns ``True``.
* **Terminal order invariant (wire-strong)**: for a given
  ``(sid, mid, pid)``, :meth:`finish_part` synchronously publishes any
  residual ``message.part.delta`` before retiring the part; digest + HTTP
  full state owns final alignment.

Stage-C flush / memory contract (§16-C + Stage E 4+4 split):

* ``flush_loop`` runs at ``TOKEN_FLUSH_SECONDS`` (100ms); each tick drains
  ``_pending`` (sorted by key — deterministic intra-tick order, §5.4) and
  every ~60s calls :meth:`ttl_sweep` (NB-B5) plus the bounded
  ``_pending_session_resinks`` drain.
* ``_reserve`` enforces the per-part cap (``TOKEN_PART_MAX_BYTES``) and
  the global LIVE byte/count caps (``TOKEN_LIVEPARTS_MAX_BYTES`` /
  ``TOKEN_LIVE_PARTS_MAX``). Per-part overflow → ``drop_part``; global
  LIVE overflow → LRU evict the oldest LivePart +
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

F-301 five-module split (2026-08-21, pure move — zero behaviour change):
the implementation now lives in four sibling mixins composed into
:class:`TokenStreamHub` below — :mod:`.budgets` (memory budgets +
part-lifecycle/tombstone accounting + the ``TOKEN_*`` budget constants and
:func:`~oc_slimapi.sse.tokenstream.budgets.apply_debug_budget_overrides`,
whose ``global`` rebinding targets that module's namespace),
:mod:`.flush_engine` (background flush loop + pending resync queue +
``_TTL_TICK_INTERVAL`` / ``_HEARTBEAT_TICK_INTERVAL``),
:mod:`.ingest` (upstream event handlers + retire/cleanup), and
:mod:`.fanout` (subscriber wiring + replay-backed fanout/delivery helpers).
All moved module-level symbols are re-exported here for import
compatibility; runtime readers and test patch targets live in the owning
module."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from ...config import DEFAULT_TOKEN_MAX_FRAME_BYTES
from ...logging_config import get_logger
from ..replay_log import ReplayLog
from .budgets import (
    TOKEN_LIVE_PARTS_MAX,
    TOKEN_LIVEPARTS_MAX_BYTES,
    TOKEN_PART_MAX_BYTES,
    BudgetMixin,
    apply_debug_budget_overrides,
)
from .fanout import FanoutMixin, _events_token_frame
from .flush_engine import (
    FlushEngineMixin,
    _HEARTBEAT_TICK_INTERVAL,
    _TTL_TICK_INTERVAL,
)
from .frames import PartKey
from .ingest import IngestMixin, _SESSION_STATUS_MAX
from .models import DeltaAccumulator, LivePart, _TokenMetrics

if TYPE_CHECKING:
    from ..hub import HubRegistry


logger = get_logger(__name__)


class TokenStreamHub(BudgetMixin, FlushEngineMixin, IngestMixin, FanoutMixin):
    """Part-lifecycle-gated accumulator + flush engine for the token stream.

    The hub holds:

    * ``live_parts`` — active text :class:`LivePart`\\ s keyed by
      :data:`PartKey`.
    * ``_nontext_parts`` — bounded insertion-ordered tombstones for
      reasoning/tool-input parts (C3, §16-B). Their ``field:"text"`` deltas
      are dropped silently to avoid the ``part_state_missing`` resync storm.
    * ``_disabled_parts`` — bounded tombstones retired by :meth:`drop_part`
      (oversized, too_large, C4, §16-B). Late deltas for these keys are
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
     * ``_pending_session_resinks`` — bounded queue of ``(sid, reason)``
      turned into per-subscriber ``resync`` frames by the flush loop (NB-B2).
    * ``_subs_by_sid`` — Stage-C subscriber fanout bookkeeping. Stage D's
      ``TokenSubscriber`` HTTP class calls :meth:`attach_subscriber` /
      :meth:`detach_subscriber` to enter/leave this ledger.
    """

    def __init__(
        self,
        *,
        replay_log: ReplayLog,
        max_frame_bytes: int = DEFAULT_TOKEN_MAX_FRAME_BYTES,
    ) -> None:
        self.live_parts: dict[PartKey, LivePart] = {}
        # B3b-2: process-wide replay log (design-v4-sse-replay §3.4). When
        # attached, every token business frame (delta) and
        # every ``message.removed`` tombstone is appended to the sid's token
        # domain ("published frames" semantics — logged even with zero
        # subscribers, REPLAY-007/018) and v4 subscribers receive the frame
        # with its ``id: t:<sid>:<epoch>:<seq>`` line prepended.
        # 4.12.0 修订六 B-1: publication is reserve→encode→append→fanout —
        # the seq is allocated BEFORE serialization and embedded as a
        # payload ``seq`` field (id-line last segment == payload seq). The B-2
        # ``token_memory_limit`` eviction resync rides the same path as a
        # REPLAYABLE business frame (see fanout.REPLAYABLE_RESYNC_REASONS).
        # Route-private resync and heartbeat frames are connection-scoped
        # control frames: never logged and never id-stamped.
        self._replay: ReplayLog = replay_log
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
        # Pending resyncs for the flush loop to fan out (bounded, NB-B2).
        self._pending_session_resinks: list[tuple[str, str]] = []
        # Stage C subscriber fanout. Stage D's TokenSubscriber registers here.
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
        # Retained constructor compatibility; subscriber queues enforce it.
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
        # Every emitted delta consumes the next per-part revision. State-only
        # retirement paths consume no revision, so native-v4 delivery does
        # not manufacture gaps for state-only retirement.
        self._part_revisions: OrderedDict[PartKey, int] = OrderedDict()
        # rev-ogpt CRITICAL 2: gate set of (sessionID, messageID) whose
        # upstream ``message.removed`` has been processed. Ingest paths
        # (``on_part_updated`` / ``on_part_delta`` / ``on_part_removed``)
        # check this BEFORE accepting the event, so a late part update
        # cannot revive a removed message. Cleared wholesale by
        # ``on_upstream_reconnect`` (new epoch) and
        # ``on_session_deleted(sid)``.
        self._retired_messages: set[tuple[str, str]] = set()
        # P1-22: bounded deleted-sid gate. Records sids whose
        # ``session.deleted`` has been processed so late part events
        # (``message.part.updated`` / ``.delta`` / ``.removed``) for a
        # deleted session are dropped before they can resurrect a LivePart.
        # Cap + TTL use the shared late-event gate retention budget
        # (``TOKEN_REMOVED_MESSAGES_MAX`` / ``TOKEN_REMOVED_MESSAGES_TTL_MS``).
        # Cleared on ``on_upstream_reconnect`` (new epoch).
        self._deleted_sids: OrderedDict[str, int] = OrderedDict()
