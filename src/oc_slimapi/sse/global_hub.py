"""GlobalHub — one process-wide upstream subscription fanning out curated frames.

Physically split from the former monolithic :mod:`oc_slimapi.sse.hub` into its
own module so the file is easier to work with while preserving the same class
definition identically.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable

import httpx
import orjson

from ..config import (
    TOKEN_REMOVED_MESSAGES_MAX,
    TOKEN_REMOVED_MESSAGES_TTL_MS,
    clear_allowlist_roots_cache,
    directory_allowed,
)
from ..logging_config import get_logger
from .hub_types import (
    ABORT_NAME,
    DEFAULT_SSE_BUFFER_BYTES,
    DEFAULT_SSE_MAX_FRAME_BYTES,
    DEFAULT_SSE_QUEUE_ITEMS,
    DEBOUNCE_SECONDS,
    DigestFields,
    GRACE_SECONDS,
    HEARTBEAT_SECONDS,
    IMMEDIATE,
    MESSAGE_EVENTS,
    RESYNC_RECONNECT_NO_REPLAY,
    SESSION_EVENTS,
    STOP,
    Subscriber,
    _UNSET,
    _extract_session_id,
    _now_ms,
    _sanitize_error_message,
    _upstream_line_bytes,
    normalize_session_status,
    record_qp_activity,
    sse_frame,
)
from .replay_log import GLOBAL_DOMAIN
from .replay_wire import sse_id_line

if TYPE_CHECKING:
    from ..traffic import TrafficLedger
    from ..turn_registry import TurnRegistry
    from .replay_log import ReplayLog
    from .token_hub import TokenStreamHub

logger = get_logger(__name__)


_LAST_UPDATED_AT_BY_SID_MAX = 10_000

# F-216 (audit 2026-08-20): cardinality bound for the per-type drop counter
# table. A hostile or simply novel upstream (new event types after an
# opencode upgrade) must not be able to grow the dict without limit; types
# beyond the bound fold into a single "__other__" bucket. Internal
# observability only — NEVER exposed on the wire (snapshot_metrics shape is
# frozen; see _record_dropped_event).
_DROPPED_TYPES_MAX = 256
# Rate limit for the sampled drop log: at most one aggregated "top" line per
# window, regardless of drop volume (a token firehose upstream must not turn
# this into a log flood).
_DROPPED_LOG_INTERVAL_SECONDS = 60.0


class GlobalHub:
    """One process-wide upstream subscription fanning out curated frames."""

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        *,
        queue_items: int = DEFAULT_SSE_QUEUE_ITEMS,
        buffer_bytes: int = DEFAULT_SSE_BUFFER_BYTES,
        max_frame_bytes: int = DEFAULT_SSE_MAX_FRAME_BYTES,
        traffic_ledger: "TrafficLedger | None" = None,
        turn_registry: "TurnRegistry | None" = None,
        directory_allowlist: list[str] | None = None,
        replay_log: "ReplayLog | None" = None,
    ):
        self.client = client
        self.subscribers: set[Subscriber] = set()
        self.queue_items = queue_items
        self.buffer_bytes = buffer_bytes
        self.max_frame_bytes = max_frame_bytes
        self._traffic_ledger = traffic_ledger
        self.directory_allowlist = directory_allowlist
        # B3b-2 (v4 SSE replay): the process-wide bounded ring replay log
        # (app.state.replay_log, forwarded through HubRegistry so the
        # lazily-created hub gets it too). ``None`` = v3-only stack (no
        # logging, no id stamping — byte-identical v3 behaviour). When
        # wired, EVERY published global business frame (IMMEDIATE q/p,
        # session.digest, session.error) is recorded in the GLOBAL domain
        # ("已发布帧" semantics — logged even with zero v4 / zero live
        # subscribers, so a reconnecting client can replay what it missed
        # while disconnected); per-subscriber delivery then prefixes the
        # ``id:`` line for wire_v4 subscribers only (v3 subscribers keep
        # id-less bytes).
        self._replay = replay_log
        # Turn token fence (S9 ingest-time snapshot stamp). Injected from
        # app.py via set_turn_registry(); ``None`` → no stamping (fields
        # omitted from every digest, ocdroid degrades).
        self._turn_registry: TurnRegistry | None = turn_registry
        self.task: asyncio.Task | None = None
        self.flush_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.stop_task: asyncio.Task | None = None
        self.ever_connected = False
        self.pending: dict[str, DigestFields] = {}
        # B1b stage 1: shared, in-memory q/p activity source for the shadow
        # scheduler. This is observation only; it does not alter frames.
        self.qp_last_activity: dict[str, float] = {}
        # B1b stage 1: optional synchronous directory observer. ``None`` is
        # the normal zero-cost path for hubs without the shadow scheduler.
        self._directory_observer: Callable[[str], None] | None = None
        # Token-stream accumulator (design-token-stream.md). Injected from
        # app.py via set_token_hub().
        # When None, message.part.delta/updated fall through to the
        # catch-all drop (Stage A: hub still works without a token hub).
        self._token_hub: TokenStreamHub | None = None
        # lite-v2-dev (🟠-2): per-session monotonic updated_at tracker for
        # cross-debounce-window sequentiality. Allows _bump_updated_at to
        # guarantee strict monotonicity even across debounce windows.
        self._last_updated_at_by_sid: OrderedDict[str, int] = OrderedDict()
        # Stage B (§16-B): per-epoch upstream-loss notification guard.
        # Set to True the first time _notify_upstream_loss() fires after a
        # successful connect; reset to False on every successful (re)connect.
        # WHY: without this, the ``except Exception`` branch of run() would
        # call _notify_upstream_loss() on EVERY retry-loop iteration, swamping
        # subscribers with redundant resync frames + re-clearing the token
        # hub on each retry. We want once-per-epoch-transition.
        self._upstream_loss_notified: bool = False
        # G1 sticky lastError: sid -> lastError dict (cleared = popped).
        # P1-21: bounded OrderedDict with FIFO cap to prevent unbounded
        # growth across high-churn sessions.
        self.sticky_last_error: OrderedDict[str, dict] = OrderedDict()
        # C⑩ tombstone: sids whose session.deleted digest has been emitted.
        # Survives pending eviction so a LATE session.error (arriving after
        # flush() cleared the deleted entry from self.pending) cannot revive
        # the sticky lastError for an already-deleted session. Complements the
        # same-window ``if entry.deleted: return`` guard. Pruned on
        # resync_all() (cold-start semantics — a resync means the client
        # cold-starts anyway; sids are unique in opencode so a tombstone
        # persisting until resync is correct and the set cannot grow
        # unbounded across reconnects).
        # P1-21: bounded OrderedDict (ordered-set pattern) with FIFO cap.
        self.deleted_tombstones: OrderedDict[str, None] = OrderedDict()
        # T3 observability counters (contract §6 / §2 metrics endpoint).
        self.upstream_events_total = 0
        self.emitted_frames_total = 0
        self.reconnects_total = 0
        self.allowlist_dropped_events = 0
        # F-216: internal per-type counter for the publish() catch-all drop
        # (the `# Drop text deltas, tool.*, message.part.*` fall-through).
        # Bounded by _DROPPED_TYPES_MAX distinct keys with an "__other__"
        # overflow bucket; the sampled log is rate-limited by
        # _DROPPED_LOG_INTERVAL_SECONDS. Purely internal observability —
        # deliberately NOT part of snapshot_metrics() (the /slimapi/metrics
        # wire shape is frozen) and inspectable from tests / a debugger.
        self.upstream_dropped_events_total: dict[str, int] = {}
        self._dropped_last_log_ts = 0.0
        self._dropped_since_log = 0
        # rev-ogpt MAJOR 3 + MAJOR 4 (3rd-round terminal audit): bounded
        # gate of retired (sessionID, messageID) tuples. Populated by
        # ``message.removed``; checked by ``message.part.updated`` to
        # prevent late part events from resurrecting state for a deleted
        # message (token-hub route + digest updatedAt bump).
        #
        # Typed as an ``OrderedDict`` keyed by (sid, mid) → insertion
        # timestamp (epoch-ms) so the gate can be capped + TTL-evicted
        # in lockstep with the token hub's bounded replay queue (same
        # ``TOKEN_REMOVED_MESSAGES_MAX`` FIFO cap and
        # ``TOKEN_REMOVED_MESSAGES_TTL_MS`` TTL). v0.5 used a plain
        # ``set`` which leaked unbounded across long-running processes.
        # Pruned on-insert (see :meth:`_prune_retired_messages`) and
        # opportunistically from :meth:`flush`. Cleared on session
        # deleted and upstream reconnect (``resync_all``) — same lifetime
        # semantics as the token hub's ``_retired_messages``.
        self._retired_messages: OrderedDict[tuple[str, str], int] = OrderedDict()

    def _bump_updated_at(self, session_id: str, entry: "DigestFields") -> None:
        """Guarantee ``entry.updated_at`` is strictly monotonic per-session.

        lite-v2 §4.3 / contract §3: digest ``updatedAt`` is sidecar wall-clock
        (``_now_ms()``). We take ``max(now, previous + 1)`` so two events in
        the same wall-clock ms do not collide. Clients MUST use
        ``(updatedAt, messageID)`` binary-tuple tie-break per contract §5 and
        MUST NOT assume cross-window strict monotonicity — the per-session
        high-water mark is an in-process optimisation, not a wire guarantee
        (cleared on restart/resync/cap-eviction).

        lite-v2-dev (🟠-2): the monotonicity is now per-session and crosses
        debounce windows, not just within a single DigestFields entry. The
        per-session high-water mark ``self._last_updated_at_by_sid[session_id]``
        ensures that events separated by wall-clock time still produce strictly
        increasing values.
        """
        now = _now_ms()
        entry_prev = entry.updated_at if isinstance(entry.updated_at, int) and not isinstance(entry.updated_at, bool) else 0
        session_prev = self._last_updated_at_by_sid.get(session_id, 0)
        previous = max(entry_prev, session_prev)
        updated_at = max(now, previous + 1)
        entry.updated_at = updated_at
        self._last_updated_at_by_sid[session_id] = updated_at
        self._last_updated_at_by_sid.move_to_end(session_id)

    def ensure_upstream(self) -> None:
        """Start the run / flush / heartbeat tasks if not already running.

        Extracted from :meth:`subscribe` so Stage-D token-subscribe
        (:meth:`TokenStreamRegistry.subscribe`) can guarantee the single
        ``/global/event`` connection is live before attaching a token
        subscriber — WITHOUT adding a control-plane subscriber or emitting a
        ``server.connected`` frame (design §5.2: token subscribe must
        ``registry.get_global().ensure_upstream()``). Idempotent: a no-op
        when the tasks are already running, and cancels any armed
        ``stop_after_grace`` so a fresh consumer does not get torn down by a
        grace timer fired a moment earlier.

        INV-1 (P1-19): the run / flush / heartbeat tasks form ONE atomic
        group. Group creation is delegated to :meth:`_spawn_group` so the
        supervisor ``done_callback`` can force a rebuild without waiting
        for the just-cancelled run task to wind down (the ``task.done()``
        check below would otherwise block the rebuild).
        """
        if self.stop_task:
            self.stop_task.cancel()
            self.stop_task = None
        if not self.task or self.task.done():
            self._spawn_group()

    def _spawn_group(self) -> None:
        """INV-1 (P1-19): create a fresh run / flush / heartbeat group.

        Consistency check: cancel any surviving siblings from a previous
        partial group (run done but flush / heartbeat still alive). Do NOT
        await (sync method); the cancelled survivors wind down on their own
        (flush / heartbeat hold no upstream connection — only run does) and
        their own done_callbacks are no-ops once the new group is assigned.

        The group is created as LOCAL variables before being assigned to
        ``self``, so a ``done_callback`` / finally that reads ``self.*``
        cannot act on the new group via a stale reference. Capturing the
        group in the callback closure makes the staleness guard
        ``self.task is run_task`` reliable.
        """
        # Consistency check: cancel survivors from a partial previous group.
        for stale in (self.flush_task, self.heartbeat_task):
            if stale is not None and not stale.done():
                stale.cancel()
        run_task = asyncio.create_task(self.run())
        flush_task = asyncio.create_task(self.flush_loop())
        heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        run_task.add_done_callback(
            self._make_group_done_callback(
                run_task, flush_task, heartbeat_task, is_run=True,
            )
        )
        flush_task.add_done_callback(
            self._make_group_done_callback(
                run_task, flush_task, heartbeat_task, is_run=False,
            )
        )
        heartbeat_task.add_done_callback(
            self._make_group_done_callback(
                run_task, flush_task, heartbeat_task, is_run=False,
            )
        )
        self.task = run_task
        self.flush_task = flush_task
        self.heartbeat_task = heartbeat_task

    def _make_group_done_callback(
        self,
        run_task: asyncio.Task,
        flush_task: asyncio.Task,
        heartbeat_task: asyncio.Task,
        *,
        is_run: bool,
    ) -> Callable[[asyncio.Task], None]:
        """INV-1 (P1-19): supervisor ``done_callback`` for a group member.

        Closed over the group's OWN task references (``run_task`` /
        ``flush_task`` / ``heartbeat_task``) — NEVER reads ``self.task`` /
        ``self.flush_task`` / etc. to decide whether this group is stale
        (those slots may have been replaced by a rebuild). The guard
        ``self.task is run_task`` is the single staleness check: if the run
        slot has been replaced, the whole group is stale and the callback
        is a no-op.

        Behaviour:
        * cancelled task → return (teardown path).
        * normal exit (run only — ``has_consumers()`` went False) → cancel
          flush + heartbeat to stop the small pre-grace spin.
        * exception death → cancel the two siblings + rebuild via
          :meth:`_spawn_group` iff ``has_consumers()``. The cancelled
          siblings' own callbacks then see ``self.task is run_task`` go
          False after the rebuild and return early (no cascading rebuild).
        """
        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                return  # defensive — cancelled() already caught this
            except asyncio.InvalidStateError:
                return  # not done — defensive
            # Stale-group guard: a rebuild replaced self.task → no-op.
            if self.task is not run_task:
                return
            if exc is None:
                # Normal exit. Only run() exits normally (its
                # ``while self.has_consumers()`` loop returned False).
                # Cancel flush + heartbeat so they do not keep spinning
                # until the grace timer fires (small pre-grace leak fix).
                if is_run:
                    for sib in (flush_task, heartbeat_task):
                        if sib is not None and not sib.done():
                            sib.cancel()
                return
            # Exception death → cancel siblings + rebuild if still needed.
            which = (
                "run" if task is run_task
                else "flush" if task is flush_task
                else "heartbeat"
            )
            logger.warning(
                "sse hub task %s died unexpectedly; rebuilding group",
                which, exc_info=exc,
            )
            for sib in (run_task, flush_task, heartbeat_task):
                if sib is not task and sib is not None and not sib.done():
                    sib.cancel()
            if self.has_consumers():
                # Force a rebuild via _spawn_group (NOT ensure_upstream):
                # the just-cancelled run task may not be .done() yet, so
                # ensure_upstream's ``task.done()`` guard would no-op.
                self._spawn_group()
        return _on_done

    def subscribe(self, welcome: bool = True) -> Subscriber:
        """Admit a subscriber; ``welcome=False`` skips the connection-local
        ``server.connected`` frame (rev-gate BLOCKER-1 / condition 5: on v4
        the frame is suppressed — it is not in the frozen no-``id:`` control
        set (meta/resync/heartbeat) and connection-local frames must not
        bypass the replay log. v3 callers keep the default ``True`` —
        byte-identical unchanged)."""
        subscriber = Subscriber(
            queue_items=self.queue_items,
            buffer_bytes=self.buffer_bytes,
            max_frame_bytes=self.max_frame_bytes,
        )
        # Welcome frame first so the client sees it before any digest/heartbeat.
        if welcome:
            subscriber.put(sse_frame({}, event="server.connected"))
        self.subscribers.add(subscriber)
        self.ensure_upstream()
        logger.info("sse subscriber attach", extra={"subscriber_id": subscriber.id})
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """Remove a subscriber and arm grace-stop when the last one leaves.

        Test/maintenance surface: production detach goes through
        ``HubRegistry.unsubscribe`` (which also tears down idle hubs). This
        method is exercised directly only by unit tests constructing a
        bare GlobalHub.
        """
        self.subscribers.discard(subscriber)
        logger.info("sse subscriber detach", extra={"subscriber_id": subscriber.id})
        if not self.has_consumers() and not self.stop_task:
            self.stop_task = asyncio.create_task(self.stop_after_grace())

    def has_consumers(self) -> bool:
        """Active curated subscribers OR token subscribers (design §5.2).

        WHY a method, not a property: the design (§16-B + design v3 次要)
        pins has_consumers as a method because it deliberately spans two
        independent subscriber ledgers (control-plane ``self.subscribers``
        + token-stream ``_token_hub.subscriber_count``). Call sites must
        read it as "are we still needed by anyone across either ledger",
        not as a cheap attribute peek — wrapping it in a property would
        invite future callers to misuse it inside hot loops without
        realizing it crosses ledgers.

        Stage A/B: ``_token_hub.subscriber_count`` is stubbed to 0, so this
        reduces to ``bool(self.subscribers)`` until Stage D wires real
        per-session token subscribers.
        """
        if self.subscribers:
            return True
        th = self._token_hub
        return th is not None and th.subscriber_count > 0

    def _notify_upstream_loss(self) -> None:
        """Canonical upstream-loss hook (design §5.2 + §16-B backstop).

        WHY this exists: previously the success-reconnect and
        exception branches of :meth:`run` each called ``resync_all()``
        directly. Stage B needs the token hub to be cleared on the SAME
        transitions (every stale LivePart is wrong after reconnect — the
        GlobalBus has no replay). Centralizing the call here keeps both
        call sites in sync as Stage C/D add more side effects (per-token-
        subscriber ``resync{reconnect_no_replay, sessionID}`` fanout, etc.).

        Called from BOTH run() reconnect paths; the per-epoch guard
        (``_upstream_loss_notified``) ensures the exception path does not
        fire on every retry-loop iteration.

        B3b-2 (v4 SSE replay, §7.2 S-B01④ frozen): the first confirmed
        upstream loss also writes a replay **barrier** across the GLOBAL
        domain AND every per-sid token domain created in this epoch
        (offline domains included — ``write_barrier(None)`` spans all of
        them). Any reconnect cursor at/below a domain's watermark is
        answered ``resync{reconnect_no_replay}`` (禁跨 barrier 补帧);
        cursors above it fall through to the normal window judgment. The
        epoch does NOT change and per-domain seq does NOT reset —
        post-recovery frames simply continue the sequence above the
        watermark. Best-effort: a barrier-write failure degrades to the
        pre-replay behaviour (resync fanout only) with a warning.
        """
        self.resync_all()
        if self._replay is not None:
            try:
                self._replay.write_barrier()
            except Exception:
                logger.warning("replay barrier write failed", exc_info=True)
        if self._token_hub is not None:
            self._token_hub.on_upstream_reconnect()

    async def stop_after_grace(self) -> None:
        """Grace timer used by ``unsubscribe`` / ``ensure_upstream``; exercised in tests."""
        await asyncio.sleep(GRACE_SECONDS)
        if not self.has_consumers():
            for task in (self.task, self.flush_task, self.heartbeat_task):
                if task:
                    task.cancel()

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self.flush()

    def flush_sid(self, session_id: str) -> None:
        """Flush only the pending digest for ``session_id`` (immediate paths).

        Used by G1-A ``session.error`` and busy-clears-sticky so other sids'
        pending digests stay in the debounce window. Sticky lastError merge
        matches :meth:`flush` for a single entry.
        """
        fields = self.pending.pop(session_id, None)
        if fields is None:
            return
        # Merge sticky lastError only when entry did not set/clear it this window.
        if fields.last_error is _UNSET and session_id in self.sticky_last_error:
            fields.last_error = self.sticky_last_error[session_id]
        # B1a: minimal changed — frame appearance == this sid changed.
        # Constructed from the frame's own sid at flush time (zero new state).
        fields.changed = [session_id]
        frame = sse_frame(fields.to_payload(session_id), event="session.digest")
        self._observe_directory(fields.directory)
        self._emit_directory_frame(frame, fields.directory)

    def flush(self) -> None:
        """Flush every pending digest (debounce loop).

        rev-ogpt MAJOR 4 (3rd-round): opportunistically prunes the
        ``_retired_messages`` gate so its TTL is enforced even when no
        new ``message.removed`` arrives for a long time. The prune is
        O(N) but ``N`` is capped at ``TOKEN_REMOVED_MESSAGES_MAX`` (1000)
        and the loop runs at 4 Hz (DEBOUNCE_SECONDS=0.25s) — negligible.
        """
        # Opportunistic TTL/cap prune (no-op when nothing expired).
        self._prune_retired_messages(_now_ms())
        self._prune_last_updated_at()
        self._prune_sticky_last_error()
        self._prune_deleted_tombstones()
        if not self.pending:
            return
        snapshot, self.pending = self.pending, {}
        for session_id, fields in snapshot.items():
            # Merge sticky lastError only when entry did not set/clear it this window.
            if fields.last_error is _UNSET and session_id in self.sticky_last_error:
                fields.last_error = self.sticky_last_error[session_id]
            # B1a: minimal changed — per-frame, from this entry's own sid.
            # Batch flush stays per-entry/per-sid (one frame, one sid, one
            # changed); no aggregation state is introduced.
            fields.changed = [session_id]
            frame = sse_frame(fields.to_payload(session_id), event="session.digest")
            self._observe_directory(fields.directory)
            self._emit_directory_frame(frame, fields.directory)

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            frame = sse_frame({}, event="server.heartbeat")
            for subscriber in tuple(self.subscribers):
                subscriber.put(frame)
            if self.subscribers:
                self.emitted_frames_total += len(self.subscribers)

    def set_token_hub(self, token_hub: TokenStreamHub | None) -> None:
        """Wire the TokenStreamHub so publish() can route
        ``message.part.delta`` / ``message.part.updated`` into it.

        The registry owns the canonical reference and pushes it onto the
        live GlobalHub (and onto any hub constructed later via
        :meth:`HubRegistry.get`).
        """
        self._token_hub = token_hub

    def set_directory_observer(
        self, observer: Callable[[str], None] | None
    ) -> None:
        """Wire an optional best-effort observer for ingested directories."""
        self._directory_observer = observer

    def _observe_directory(self, directory: Any) -> None:
        observer = self._directory_observer
        if observer is None or not isinstance(directory, str) or not directory:
            return
        try:
            observer(directory)
        except Exception:
            # Observability must never alter the hub's ingest/fan-out path.
            logger.debug("directory observer failed", exc_info=True)

    def set_turn_registry(self, registry: "TurnRegistry | None") -> None:
        """Wire the :class:`TurnRegistry` so publish() can stamp
        ``turnIncarnation`` / ``turn`` onto ``session.digest`` entries.

        Mirrors :meth:`set_token_hub`: the registry owns the canonical
        reference (app.py constructs it in lifespan) and pushes it onto the
        live GlobalHub and any hub constructed later via
        :meth:`HubRegistry.get`. ``None`` is accepted so the fence can be
        detached (tests, shutdown).
        """
        self._turn_registry = registry

    def set_directory_allowlist(self, allowlist: list[str] | None) -> None:
        """Set the process-wide SSE directory filter (None disables it)."""
        self.directory_allowlist = allowlist
        # rev-2 sub-1: config-change signal — every (re-)applied allowlist
        # re-resolves the canonical roots, so a root retargeted on disk
        # takes effect on the next config change (documented ops semantic).
        clear_allowlist_roots_cache()

    def set_replay_log(self, replay_log: "ReplayLog | None") -> None:
        """Wire the process-wide :class:`ReplayLog` (B3b-2).

        Mirrors :meth:`set_token_hub`: ``HubRegistry`` owns the canonical
        reference (``app.state.replay_log``, constructed in lifespan) and
        pushes it onto the live GlobalHub and any hub constructed later
        via ``HubRegistry.get``. ``None`` = v3-only stack (no logging, no
        id stamping — accepted for tests / detach).
        """
        self._replay = replay_log

    def _replay_publish(self, frame: bytes) -> bytes | None:
        """Record one published global business frame in the replay log.

        Returns the frame's ``id: …\\n`` line, or ``None`` when no replay
        log is wired / the append failed. Publishing NEVER fails because
        replay bookkeeping failed: an append error (log closed,
        unexpected) degrades to id-less fan-out with a warning.

        Called ONLY from :meth:`_emit_directory_frame` after the
        allowlist pass — a frame dropped by the directory allowlist was
        never published to anyone and consumes no seq. The append happens
        regardless of subscriber count / subscriber wire views ("已发布
        帧" semantics — REPLAY-007/018: frames published while a client
        was backpressured or fully offline must be replayable).
        """
        replay = self._replay
        if replay is None:
            return None
        try:
            entry = replay.append(GLOBAL_DOMAIN, frame)
        except Exception:
            logger.warning("replay log append failed", exc_info=True)
            return None
        return sse_id_line(GLOBAL_DOMAIN, replay.epoch, entry.seq)

    def _directory_allowed(self, directory: Any) -> bool:
        allowlist = self.directory_allowlist
        if not allowlist:
            return True
        # rev-2 closure: cached canonical ROOTS (config-time resolution)
        # vs REALTIME candidate canonicalisation (never cached — a symlink
        # created/retargeted after a previous verdict cannot ride a stale
        # result). Relative candidates fail closed (authorisation object
        # must equal access object). Non-str / empty directories also fall
        # through to fail-closed False (frame dropped + counted, as before).
        return directory_allowed(allowlist, directory)

    def _emit_directory_frame(self, frame: bytes, directory: Any) -> None:
        if not self._directory_allowed(directory):
            self.allowlist_dropped_events += 1
            return
        # B3b-2: log the published frame (global domain) and — only for
        # wire_v4 subscribers — deliver the id-prefixed bytes. v3
        # subscribers keep receiving the frame VERBATIM (zero-change iron
        # rule: the id line exists solely in the v4 subscriber's copy).
        id_line = self._replay_publish(frame)
        for subscriber in tuple(self.subscribers):
            if id_line is not None and subscriber.wire_v4:
                subscriber.put(id_line + frame)
            else:
                subscriber.put(frame)
        if self.subscribers:
            self.emitted_frames_total += len(self.subscribers)

    def _prune_retired_messages(self, now_ms: int) -> None:
        """rev-ogpt MAJOR 4 (3rd-round terminal audit): enforce FIFO cap +
        TTL on the ``_retired_messages`` gate.

        Mirrors :meth:`TokenStreamHub._prune_removed_messages` so the
        GlobalHub gate and the token-hub replay queue share identical
        lifetime semantics (``TOKEN_REMOVED_MESSAGES_MAX`` FIFO cap +
        ``TOKEN_REMOVED_MESSAGES_TTL_MS`` TTL). Without this alignment the
        GlobalHub gate was a plain ``set`` that leaked unbounded across
        long-running processes (only ``session.deleted`` and
        ``resync_all`` cleared it).

        Called on-insert (from :meth:`publish` ``message.removed`` branch)
        and opportunistically from :meth:`flush` so the TTL is enforced
        even when no new ``message.removed`` arrives for a long time.
        Oldest-first eviction relies on the ``OrderedDict`` preserving
        insertion order (refreshed by ``move_to_end`` on duplicate
        inserts for FIFO correctness).
        """
        cutoff = now_ms - TOKEN_REMOVED_MESSAGES_TTL_MS
        # TTL: drop entries whose timestamp predates the cutoff.
        expired = [k for k, ts in self._retired_messages.items() if ts < cutoff]
        for k in expired:
            self._retired_messages.pop(k, None)
        # FIFO cap: evict oldest until under limit.
        while len(self._retired_messages) > TOKEN_REMOVED_MESSAGES_MAX:
            self._retired_messages.popitem(last=False)

    def _prune_last_updated_at(self) -> None:
        """FIFO cap on ``_last_updated_at_by_sid`` to prevent unbounded growth.

        Sessions that produced message/part events but never observed a
        ``session.deleted`` would accumulate indefinitely. The cap evicts
        least-recently-updated entries (LRU via ``move_to_end`` in
        :meth:`_bump_updated_at`); the next event for an evicted session
        starts a fresh high-water mark — cross-window monotonicity is not
        guaranteed per contract, so this is safe.

        Called from :meth:`flush` at debounce frequency (4 Hz).
        """
        while len(self._last_updated_at_by_sid) > _LAST_UPDATED_AT_BY_SID_MAX:
            self._last_updated_at_by_sid.popitem(last=False)

    def _prune_sticky_last_error(self) -> None:
        """P1-21: FIFO cap on ``sticky_last_error`` to prevent unbounded
        growth across high-churn sessions. Mirrors ``_prune_last_updated_at``.
        """
        while len(self.sticky_last_error) > _LAST_UPDATED_AT_BY_SID_MAX:
            self.sticky_last_error.popitem(last=False)

    def _prune_deleted_tombstones(self) -> None:
        """P1-21: FIFO cap on ``deleted_tombstones`` to prevent unbounded
        growth. Mirrors ``_prune_last_updated_at``.
        """
        while len(self.deleted_tombstones) > _LAST_UPDATED_AT_BY_SID_MAX:
            self.deleted_tombstones.popitem(last=False)

    def _record_dropped_event(self, event_type: Any) -> None:
        """F-216: count one catch-all drop by event type, bounded + sampled.

        Called from the tail of :meth:`publish` for every event that falls
        through all curated branches. Three guarantees:

        * Bounded cardinality: at most ``_DROPPED_TYPES_MAX`` distinct keys;
          a type arriving when the table is full folds into ``__other__``.
          Non-string ``event_type`` (missing/unhashable payload ``type``)
          also lands in ``__other__`` — the counter must never crash on a
          malformed frame.
        * Sampled log: at most one aggregated line per
          ``_DROPPED_LOG_INTERVAL_SECONDS`` window (top-8 by count), so a
          token firehose upstream cannot flood the log.
        * Zero wire impact: the table is internal state; nothing here feeds
          ``snapshot_metrics()`` or any frame (the /slimapi/metrics shape
          is frozen). ``_dropped_since_log`` ticks the per-window volume
          for future metrics extension without changing today's wire.
        """
        key = event_type if isinstance(event_type, str) else "__other__"
        table = self.upstream_dropped_events_total
        if key not in table and len(table) >= _DROPPED_TYPES_MAX:
            key = "__other__"
        table[key] = table.get(key, 0) + 1
        self._dropped_since_log += 1
        now = time.monotonic()
        if now - self._dropped_last_log_ts >= _DROPPED_LOG_INTERVAL_SECONDS:
            top = sorted(table.items(), key=lambda item: -item[1])[:8]
            logger.info("upstream dropped events (top): %s", top)
            self._dropped_last_log_ts = now
            self._dropped_since_log = 0

    def publish(self, global_event: dict[str, Any]) -> None:
        # Count every JSON-decoded upstream event we were asked to consider;
        # early-returns below still represent real traffic the GlobalBus saw.
        self.upstream_events_total += 1
        directory = global_event.get("directory")
        self._observe_directory(directory)
        payload = global_event.get("payload")
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        props = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}

        # Blocking signals: forward raw, no debounce.
        if event_type in IMMEDIATE:
            if (
                isinstance(event_type, str)
                and (event_type.startswith("question.") or event_type.startswith("permission."))
                and isinstance(directory, str)
                and directory
            ):
                # F-015: funnel through the shared activity-LRU helper so
                # the table stays bounded and re-touches move to the tail
                # (both write points — here and QpSweepShadow.record_activity
                # — share this dict reference by app.py construction).
                record_qp_activity(self.qp_last_activity, directory, time.time())
            frame = sse_frame({
                "directory": directory,
                "type": event_type,
                "properties": props,
            })
            self._emit_directory_frame(frame, directory)
            return

        # Session/message events: accumulate into pending digest.
        if event_type in SESSION_EVENTS:
            session_id = _extract_session_id(payload, props)
            if not isinstance(session_id, str):
                return
            entry = self.pending.setdefault(session_id, DigestFields())
            if isinstance(directory, str):
                entry.directory = directory
            if event_type == "session.status":
                # Upstream status arrives in two shapes — legacy plain
                # string ("busy") and object envelope ({"type": "busy"};
                # live-wire 2026-08-19). Normalize ONCE here; the same
                # value feeds the digest fill, the G1 busy-clear below,
                # and the token-hub mirror branch further down (no second
                # raw read).
                status = normalize_session_status(props.get("status"))
                if status is not None:
                    entry.status = status
                # S9: ingest-time snapshot stamp of turn/inc. Stamped HERE
                # (publish/ingest), NOT at flush time, so a turn bump
                # between ingest and flush cannot retroactively change an
                # already-stamped digest (contract §7.4, V10). The snapshot
                # freezes the ints onto the entry (Python int = value copy).
                # Scope is sid alone; snapshot always returns a tuple
                # (unobserved sid → (inc, 0)).
                if self._turn_registry is not None:
                    entry.turn_incarnation, entry.turn = self._turn_registry.snapshot(session_id)
                # G1: busy clears sticky lastError with explicit null digest.
                # Per-sid flush only — other sessions stay in the debounce window.
                # ``status`` is the NORMALIZED value (string | None): the
                # object envelope {"type":"busy"} used to fail the legacy
                # ``props.get("status") == "busy"`` string comparison, which
                # made this the sticky-clear path that never fired (bug A).
                if status == "busy" and session_id in self.sticky_last_error:
                    self.sticky_last_error.pop(session_id, None)
                    entry.last_error = None  # explicit null → clear frame
                    self.flush_sid(session_id)
            elif event_type == "session.deleted":
                entry.deleted = True
                # C⑩: record a tombstone that survives pending eviction so a
                # LATE session.error (post-flush) cannot revive lastError.
                # P1-21: bounded OrderedDict — move_to_end + prune.
                self.deleted_tombstones[session_id] = None
                self.deleted_tombstones.move_to_end(session_id)
                self._prune_deleted_tombstones()
                # G1: deleted pops sticky; digest omits lastError.
                self.sticky_last_error.pop(session_id, None)
                entry.last_error = _UNSET
                # rev-ogpt MAJOR 3: clear retired-message gates for this
                # session — the session is gone, no late part events for
                # its messages can arrive, and the gate set would leak.
                for key in [k for k in self._retired_messages if k[0] == session_id]:
                    self._retired_messages.pop(key, None)
                # lite-v2-dev (🟠-3): pop per-session updated_at high-water
                # mark — a deleted session's clock state is no longer needed
                # and would otherwise leak unbounded.
                self._last_updated_at_by_sid.pop(session_id, None)
            elif event_type == "session.updated":
                # Contract §3: archived ← session.updated's info.time.archived
                # (epoch-ms int). Pass-through — same handling as
                # info.time.updated/created in the MESSAGE_EVENTS branch below
                # (opencode emits epoch-ms ints; we trust the upstream format
                # and do not coerce). Use ``is not None`` so epoch 0 is kept
                # (``if archived_val:`` would drop it). Sticky: a missing
                # value does NOT clear an already-observed timestamp
                # (archived is permanent, mirroring deleted-stickiness).
                # Other session.updated fields flow through subsequent events.
                info = props.get("info") if isinstance(props.get("info"), dict) else {}
                time_obj = info.get("time") if isinstance(info.get("time"), dict) else {}
                archived_val = time_obj.get("archived")
                # Reject bool explicitly: bool is a subclass of int, so a
                # spurious ``archived: true`` from upstream must not be coerced
                # to epoch-ms 1 and emitted to clients. Only real epoch-ms ints
                # (including 0) pass through.
                if isinstance(archived_val, int) and not isinstance(archived_val, bool):
                    entry.archived = archived_val
            # Stage B §16-B: PARALLEL route to the token hub. The digest
            # work above is the control-plane contract (unchanged); this
            # branch mirrors session.status / session.deleted into the
            # token accumulator so it can maintain session status / retire
            # abandoned LiveParts. It MUST NOT touch entry/flush/subscribers.
            if self._token_hub is not None and event_type in (
                "session.status", "session.deleted",
            ):
                if event_type == "session.status":
                    # Reuse the ``status`` normalized at the top of the
                    # session.status branch (same publish() invocation —
                    # that branch always ran first). Object envelope and
                    # legacy string reach the token hub identically; an
                    # invalid shape (None) is skipped, matching the legacy
                    # isinstance(str) guard.
                    if status is not None:
                        self._token_hub.on_session_status(session_id, status)
                else:  # session.deleted
                    self._token_hub.on_session_deleted(session_id)
            return

        if event_type in MESSAGE_EVENTS:
            session_id = _extract_session_id(payload, props)
            if not isinstance(session_id, str):
                return
            info = props.get("info") if isinstance(props.get("info"), dict) else {}
            message_id = info.get("id") if isinstance(info.get("id"), str) else props.get("messageID")
            entry = self.pending.setdefault(session_id, DigestFields())
            if isinstance(directory, str):
                entry.directory = directory
            if isinstance(message_id, str):
                entry.message_id = message_id
            # lite-v2 §4.2: updatedAt is the sidecar's wall-clock observation
            # time, NOT the upstream message timestamp. Using _bump_updated_at
            # ensures strict monotonicity within the debounce window.
            # lite-v2-dev (🟠-2): now per-session cross-debounce monotonic.
            self._bump_updated_at(session_id, entry)
            return

        # G1: session.error — immediate digest (with sid) or session.error frame (session-less).
        # Sid MUST be props.get("sessionID") only — do NOT use _extract_session_id
        # (session.error may lack props.sessionID / info; helper is for session.*
        # / message.* shapes only).
        if event_type == "session.error":
            err = props.get("error") if isinstance(props, dict) else None
            err = err if isinstance(err, dict) else {}
            name = err.get("name")
            # Coerce non-str name → None so ``(name or "")[:128]`` and
            # ``_sanitize_error_message(..., name)`` never TypeError on a
            # truthy dict/int (which would escape publish → resync whole hub).
            name = name if isinstance(name, str) else None
            if name == ABORT_NAME:
                return  # abort silently dropped
            raw_msg = (
                (err.get("data") or {}).get("message")
                if isinstance(err.get("data"), dict)
                else None
            )
            message = _sanitize_error_message(raw_msg, name)
            at = _now_ms()
            sid = props.get("sessionID") if isinstance(props, dict) else None
            if isinstance(sid, str) and sid:
                # C⑩: drop late errors for sessions already deleted + evicted.
                # The ``entry.deleted`` guard below only covers the
                # pre-eviction (same-window) case; this tombstone covers the
                # post-eviction case (the deleted digest has been flushed and
                # self.pending no longer carries the entry).
                if sid in self.deleted_tombstones:
                    return
                entry = self.pending.setdefault(sid, DigestFields())
                if entry.deleted:
                    return
                last_error_obj = {
                    "name": (name or "")[:128],
                    "message": message,
                    "at": at,
                }
                entry.last_error = last_error_obj
                self.sticky_last_error[sid] = last_error_obj
                self.sticky_last_error.move_to_end(sid)
                self._prune_sticky_last_error()
                if isinstance(directory, str):
                    entry.directory = directory
                self.flush_sid(sid)  # G1-A immediate, this sid only
            else:
                # G1-B session-less: immediate direct push (no debounce)
                frame_payload: dict[str, Any] = {
                    "name": (name or "")[:128],
                    "message": message,
                    "at": at,
                }
                if directory:
                    frame_payload["directory"] = directory
                frame = sse_frame(frame_payload, event="session.error")
                self._emit_directory_frame(frame, directory)
            return

        # Token-stream ingest (design-token-stream.md §5.3): route the
        # per-token firehose into the TokenStreamHub BEFORE the catch-all
        # drop. Stage A scope: ingest + data structures only — no flush,
        # no subscribers, no fan-out (Stage B/C/D). Returning here keeps
        # the control-plane branches above untouched AND prevents these
        # high-frequency events from polluting the curated digest. When no
        # token hub is wired, behaviour is unchanged (events are dropped).
        #
        # lite-v2 (contract §3 alignment): per-message part state is no
        # longer tracked by the hub. Per contract §3 (v2 删除的帧), only
        # ``session.*`` / ``message.updated`` / ``message.appended`` drive
        # the digest. ``message.part.updated`` / ``message.part.removed``
        # no longer bump ``digest.updatedAt`` — they only route to the
        # token hub for the delta/snapshot stream. A retired-message gate
        # (``_retired_messages``) prevents late part events from
        # resurrecting token hub state for a deleted message.
        #   * ``message.removed`` records the retired-message gate and
        #     fans out via the token hub; it does NOT bump updatedAt
        #     (skeleton reload is driven by the client's own message-list
        #     state, not by the digest).
        # ``message.part.delta`` (token stream) does NOT touch the digest
        # — clients see deltas in real time.
        if event_type in (
            "message.part.delta", "message.part.updated",
            "message.part.removed", "message.removed",
        ):
            if event_type == "message.part.updated":
                # Key extraction mirrors tokenstream ``on_part_updated``
                # (properties.part.{sessionID, messageID, id}) and uses
                # ``part.sessionID`` as the debounce key (same as
                # ``_extract_session_id`` for MESSAGE_EVENTS).
                part = props.get("part")
                if isinstance(part, dict):
                    psid = part.get("sessionID")
                    pmid = part.get("messageID")
                    ppid = part.get("id")
                    if (
                        isinstance(psid, str) and psid
                        and isinstance(pmid, str) and pmid
                        and isinstance(ppid, str) and ppid
                    ):
                        # rev-ogpt MAJOR 3: retired-message gate — if this
                        # message was removed upstream, late
                        # ``message.part.updated`` must NOT resurrect any
                        # state (token hub routing). Per contract §3, part
                        # events no longer bump digest updatedAt anyway.
                        if (psid, pmid) in self._retired_messages:
                            return
                        # Contract §3: part events NO LONGER trigger
                        # digest — only route to token hub.
                        if self._token_hub is not None:
                            self._token_hub.on_part_updated(props)
                        return
            elif event_type == "message.part.removed":
                # opencode v1.18.4 payload (schema session.ts:604-628):
                # flat ``{sessionID, messageID, partID}`` (NOT nested).
                psid = props.get("sessionID")
                pmid = props.get("messageID")
                ppid = props.get("partID")
                if (
                    isinstance(psid, str) and psid
                    and isinstance(pmid, str) and pmid
                    and isinstance(ppid, str) and ppid
                ):
                    # Contract §3: part events NO LONGER trigger digest —
                    # only route to the token hub so it can
                    # retire the corresponding LivePart / pending
                    # accumulator / revision. Without this routing the
                    # token hub would keep emitting stale delta / snapshot
                    # frames for a part the upstream has removed.
                    # ``on_part_removed`` is idempotent (``drop_part``
                    # returns False on second call) and is gated by the
                    # token hub's ``_retired_messages`` set when the
                    # whole message has already been retired.
                    if self._token_hub is not None:
                        self._token_hub.on_part_removed(psid, pmid, ppid)
                    return
            elif event_type == "message.removed":
                # opencode v1.18.4 payload: flat ``{sessionID, messageID}``.
                psid = props.get("sessionID")
                pmid = props.get("messageID")
                if (
                    isinstance(psid, str) and psid
                    and isinstance(pmid, str) and pmid
                ):
                    # rev-ogpt MAJOR 3: record the retired message so late
                    # ``message.part.updated`` cannot resurrect any state.
                    # rev-ogpt MAJOR 4 (3rd-round): the gate is a bounded
                    # OrderedDict with FIFO cap (``TOKEN_REMOVED_MESSAGES_MAX``)
                    # + TTL (``TOKEN_REMOVED_MESSAGES_TTL_MS``) aligned with
                    # the token hub's replay queue. ``move_to_end`` keeps
                    # duplicate-insert (re-removed message) at the tail so
                    # the cap never evicts the freshest gate entry.
                    now_ms = _now_ms()
                    self._retired_messages[(psid, pmid)] = now_ms
                    self._retired_messages.move_to_end((psid, pmid))
                    self._prune_retired_messages(now_ms)
                    # Stage B v0.6 §P.1 (MAJOR 4 方案 C): route to the
                    # token hub so it can fan a ``message.removed`` frame
                    # to current subscribers AND record the tombstone in
                    # the bounded replay queue for future handshake
                    # replay.
                    if self._token_hub is not None:
                        self._token_hub.on_message_removed(psid, pmid)
                return
            # message.part.delta, OR message.part.updated with a missing
            # / malformed ``part`` dict → original Stage-A token-hub route
            # (no part state mutation, no revision available).
            if self._token_hub is not None:
                if event_type == "message.part.delta":
                    self._token_hub.on_part_delta(props)
                else:
                    self._token_hub.on_part_updated(props)
            return

        # Drop text deltas, tool.*, message.part.*, and anything else.
        # F-216: internal per-type observability for the drop (bounded
        # table + sampled log; zero wire impact — see
        # _record_dropped_event). Curated branches above returned early,
        # so only genuinely dropped types reach this counter.
        self._record_dropped_event(event_type)

    def resync_all(self) -> None:
        # C⑩: cold-start semantics — a resync means the client cold-starts
        # anyway, so deleted tombstones are pruned to prevent unbounded growth
        # across reconnects (sids are unique per opencode process run).
        self.deleted_tombstones.clear()
        # rev-ogpt MAJOR 3: clear retired-message gates — reconnect begins
        # a new epoch; late part events from the dead epoch must be free
        # to create fresh state once the new epoch's events arrive.
        self._retired_messages.clear()
        self._last_updated_at_by_sid.clear()
        frame = sse_frame({"reason": RESYNC_RECONNECT_NO_REPLAY}, event="resync")
        for subscriber in tuple(self.subscribers):
            subscriber.put(frame)
        if self.subscribers:
            self.emitted_frames_total += len(self.subscribers)

    async def run(self) -> None:
        delay = 1.0
        while self.has_consumers():
            try:
                if self.client is None:
                    # Defensive: tests / misconfiguration should not hot-loop.
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
                async with self.client.stream(
                    "GET", "/global/event",
                    headers={"Accept": "text/event-stream"},
                    timeout=timeout,
                ) as response:
                    response.raise_for_status()
                    if self.ever_connected:
                        self.reconnects_total += 1
                        # §16-B: notify token hub of upstream loss on the
                        # successful-reconnect path. INV-6 (P1-18 fix):
                        # the ``not self._upstream_loss_notified`` guard
                        # ensures "each loss notifies exactly once" across
                        # all three loss paths (EOF / exception / reconnect).
                        # Without it, a loss notified on the exception path
                        # would be re-notified here on the next reconnect —
                        # a double-notify. reconnects_total is unaffected
                        # (it counts every reconnect, not every notify).
                        if not self._upstream_loss_notified:
                            self._notify_upstream_loss()
                    self.ever_connected = True
                    # New epoch begins — reset the per-epoch loss guard so
                    # the NEXT disconnect's first exception/EOF can fire.
                    self._upstream_loss_notified = False
                    delay = 1.0
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        # Traffic accounting: count the raw upstream bytes
                        # consumed from the single shared /global/event
                        # stream. Delegated to the pure helper
                        # ``_upstream_line_bytes`` so the empty-line /
                        # separator counting (+1) is unit-tested directly
                        # (driving the real ``run()`` loop with a mock
                        # upstream is avoided — it busy-loops inside httpx
                        # and resists ``task.cancel()``; see
                        # tests/test_traffic_sse.py header). Empty SSE
                        # separator lines are counted too (``"" → 1``).
                        # CRLF caveat: see ``_upstream_line_bytes`` docstring.
                        if self._traffic_ledger is not None:
                            try:
                                self._traffic_ledger.record_sse_upstream(
                                    bucket="events_sse",
                                    bytes_in=_upstream_line_bytes(line),
                                )
                            except Exception:
                                logger.warning("sse traffic accounting failed", exc_info=True)
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif not line and data_lines:
                            try:
                                self.publish(orjson.loads("\n".join(data_lines)))
                            except orjson.JSONDecodeError:
                                logger.debug("upstream sse malformed frame dropped", exc_info=True)
                            data_lines.clear()
                # INV-6 (P1-18): aiter_lines ended normally (EOF) = upstream
                # loss. An SSE stream is long-lived; a normal end means the
                # server closed it. Treat it identically to the exception
                # path: notify (if not already) + sleep + backoff. Without
                # this, the loop would immediately reconnect with no sleep
                # (hot-loop / connect storm on a persistently-closing
                # upstream) and the token hub would not be notified of the
                # loss (stale LiveParts survive the silent reconnect).
                if self.ever_connected and not self._upstream_loss_notified:
                    self._notify_upstream_loss()
                    self._upstream_loss_notified = True
                logger.warning(
                    "upstream sse EOF (stream closed), reconnecting in %.1fs",
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # §16-B: notify upstream loss on the exception path — but
                # ONLY on the FIRST exception after a successful connect.
                # Without ``_upstream_loss_notified`` the retry loop would
                # fire on every iteration, swamping subscribers with
                # redundant resync frames + re-clearing the token hub
                # (each call is idempotent, but the fanout is not free).
                if self.ever_connected and not self._upstream_loss_notified:
                    self._notify_upstream_loss()
                    self._upstream_loss_notified = True
                logger.warning(
                    "upstream sse disconnected, reconnecting in %.1fs",
                    delay, exc_info=True,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
