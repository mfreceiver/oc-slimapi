"""HubRegistry — T3 admission control and GlobalHub lifecycle.

Physically split from the former monolithic :mod:`oc_slimapi.sse.hub` so the
registry can be imported without pulling in :class:`GlobalHub` or its
transitive dependencies.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..logging_config import get_logger
from .global_hub import GlobalHub
from .hub_types import (
    DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY,
    DEFAULT_MAX_TOTAL_SUBSCRIBERS,
    DEFAULT_SSE_QUEUE_ITEMS,
    DEFAULT_SSE_BUFFER_BYTES,
    DEFAULT_SSE_MAX_FRAME_BYTES,
    GRACE_SECONDS,
    STOP,
    Subscriber,
    SubscriberCapacityError,
)

if TYPE_CHECKING:
    import httpx

    from ..traffic import TrafficLedger
    from ..turn_registry import TurnRegistry
    from .tokenstream import TokenStreamHub

logger = get_logger(__name__)


class HubRegistry:
    """Holds a single process-wide GlobalHub and enforces T3 admission.

    ``get(directory)`` is kept for back-compat with callers that still pass a
    directory key, but the directory is ignored — the same hub is returned
    regardless. ``HubRegistry(client)`` and ``close()`` signatures are
    unchanged.

    Admission runs entirely inside :meth:`subscribe`: the capacity check and
    the ``subscribers.add`` happen with no ``await`` between them, so under
    asyncio's cooperative scheduling no other coroutine can interleave and
    over-admit. ``unsubscribe`` is idempotent (a subscriber already removed
    is a no-op) so retries from a buggy caller cannot drive
    ``total_subscribers`` negative or below the real count.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        *,
        max_subscribers_per_directory: int = DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY,
        max_total_subscribers: int = DEFAULT_MAX_TOTAL_SUBSCRIBERS,
        queue_items: int = DEFAULT_SSE_QUEUE_ITEMS,
        buffer_bytes: int = DEFAULT_SSE_BUFFER_BYTES,
        max_frame_bytes: int = DEFAULT_SSE_MAX_FRAME_BYTES,
        traffic_ledger: "TrafficLedger | None" = None,
    ):
        self.client = client
        self._global: GlobalHub | None = None
        self.max_subscribers_per_directory = max_subscribers_per_directory
        self.max_total_subscribers = max_total_subscribers
        self.queue_items = queue_items
        self.buffer_bytes = buffer_bytes
        self.max_frame_bytes = max_frame_bytes
        self._traffic_ledger = traffic_ledger
        self.total_subscribers = 0
        self.rejected_total = 0
        self._transforms: Any = None  # TransformPool, wired from app.py for metrics.
        # Token-stream accumulator (design-token-stream.md); app.py owns
        # construction, the registry forwards the reference onto any
        # lazily-created GlobalHub.
        self._token_hub: TokenStreamHub | None = None
        # Turn token fence (S9): app.py constructs the TurnRegistry in
        # lifespan and pushes it here; the registry forwards the reference
        # onto any lazily-created GlobalHub (mirrors _token_hub wiring).
        self._turn_registry: TurnRegistry | None = None
        # B3b-2: process-wide replay log (app.state.replay_log), forwarded
        # onto any lazily-created GlobalHub via the ctor kwarg (mirrors
        # _token_hub / _turn_registry wiring). ``None`` = v3-only stack.
        self._replay_log: Any | None = None
        self._removal_task: asyncio.Task | None = None

    def set_transforms(self, pool: Any) -> None:
        """Wire the TransformPool so snapshot_metrics() can report active/waiting.

        The pool lives in ``app.state.transforms``; the registry holds only a
        reference for metrics purposes and never calls into it.
        """
        self._transforms = pool

    def set_token_hub(self, token_hub: TokenStreamHub | None) -> None:
        """Wire the TokenStreamHub onto the registry and any live GlobalHub.

        ``None`` is accepted so Stage B can detach the hub during shutdown
        if needed.
        """
        self._token_hub = token_hub
        if self._global is not None:
            self._global._token_hub = token_hub

    def set_turn_registry(self, registry: TurnRegistry | None) -> None:
        """Wire the :class:`TurnRegistry` onto the registry and any live GlobalHub.

        Mirrors :meth:`set_token_hub`: app.py constructs the registry in
        lifespan and pushes it here; a later ``get()`` forwards it onto the
        lazily-created GlobalHub. ``None`` is accepted for tests / detach.
        """
        self._turn_registry = registry
        if self._global is not None:
            self._global._turn_registry = registry

    def set_replay_log(self, replay_log: Any | None) -> None:
        """Wire the process-wide :class:`ReplayLog` (B3b-2) onto the
        registry and any live GlobalHub.

        Mirrors :meth:`set_token_hub`: app.py constructs the replay log in
        lifespan (``app.state.replay_log``) and pushes it here BEFORE any
        hub exists; ``get()`` forwards it onto the lazily-created GlobalHub
        via the ctor kwarg. ``None`` is accepted for tests / v3-only stacks
        (hub runs the unchanged id-less / un-logged pipeline).
        """
        self._replay_log = replay_log
        if self._global is not None:
            self._global.set_replay_log(replay_log)

    def get(self, directory: str | None = None) -> GlobalHub:
        if self._global is None:
            self._global = GlobalHub(
                self.client,
                queue_items=self.queue_items,
                buffer_bytes=self.buffer_bytes,
                max_frame_bytes=self.max_frame_bytes,
                traffic_ledger=self._traffic_ledger,
                turn_registry=self._turn_registry,
                replay_log=self._replay_log,
            )
            self._global._token_hub = self._token_hub
        return self._global

    def get_global(self) -> GlobalHub:
        return self.get(None)

    def cancel_pending_removal(self) -> None:
        """Cancel a pending grace-removal task (NB-B1, design §5.2 / §16-B).

        Stage-D token-subscribe calls this so a token subscriber arriving
        during the ``GRACE_SECONDS`` idle window does not get its hub torn
        down by a ``_remove_hub_after_grace`` timer armed a moment earlier
        by the last control-plane unsubscribe. Mirrors the cancel the control
        plane does inside :meth:`GlobalHub.subscribe` / :meth:`ensure_upstream`,
        but on the registry side (``_removal_task`` is owned by the registry,
        not the hub). Idempotent.
        """
        if self._removal_task is not None:
            self._removal_task.cancel()
            self._removal_task = None

    def maybe_arm_grace_if_idle(self) -> None:
        """Arm the registry grace-removal iff the global hub has NO consumers.

        Unified idle predicate for BOTH the control-plane and token-stream
        last-detach paths (design §5.2 / §16-B). ``subscribe`` cancels
        ``_removal_task`` (:meth:`cancel_pending_removal`, NB-B1) +
        :meth:`ensure_upstream` arms the upstream connection; the matching
        ``unsubscribe`` must RE-ARM grace when the last consumer across
        EITHER ledger leaves — otherwise a token-only consumer (the common
        opt-in stream path) detaches, ``GlobalHub.run()`` parks forever on
        ``aiter_lines``, and the upstream ``/global/event`` connection + hub
        tasks leak (B-D1).

        Spans BOTH ledgers via :meth:`GlobalHub.has_consumers` so a token
        subscriber keeps a control-plane-idle hub alive (and vice versa) —
        symmetric with the cancel side, and avoids arming a doomed-to-no-op
        task while any consumer remains (NB-D3). Idempotent: a no-op when
        already armed, when the hub is gone, or when consumers remain.
        """
        hub = self._global
        if hub is None or hub.has_consumers():
            return
        if self._removal_task is not None:
            return
        self._removal_task = asyncio.create_task(
            self._remove_hub_after_grace(hub), name="hub-grace-removal"
        )

    def subscribe(self, wire_v4: bool = False) -> Subscriber:
        """Admit a new subscriber under T3 caps, then start / reuse the hub.

        ``wire_v4`` (rev-gate BLOCKER-1 / condition 5): suppresses the
        connection-local ``server.connected`` welcome frame (v4-only —
        the frame is outside the frozen no-``id:`` control set and must
        not bypass the replay log) and stamps ``subscriber.wire_v4`` so
        the fanout choke point id-stamps business frames for this
        connection. v3 callers keep the default ``False`` — the welcome
        frame and raw frame bytes are byte-identical unchanged.

        Capacity check + ``subscribers.add`` happen with no ``await`` between
        them (contract §6 admission critical section). On overflow raises
        :class:`SubscriberCapacityError`; the caller (events route) turns
        that into a 503 with ``Retry-After``.

        We intentionally do NOT cancel a pending ``_removal_task`` here —
        ``subscribe`` must stay synchronous, and cancelling without awaiting
        would orphan the task. Instead the grace task wakes up after
        ``GRACE_SECONDS``, observes that the hub now has subscribers, and
        exits as a no-op. ``close()`` later cancels + awaits it explicitly.
        """
        hub = self.get_global()
        current_hub = len(hub.subscribers)
        # ---- single no-await critical section: check → admit ----
        if current_hub >= self.max_subscribers_per_directory:
            self.rejected_total += 1
            raise SubscriberCapacityError(
                "sse_subscriber_limit_directory",
                limit=self.max_subscribers_per_directory,
                current=current_hub,
            )
        if self.total_subscribers >= self.max_total_subscribers:
            self.rejected_total += 1
            raise SubscriberCapacityError(
                "sse_subscriber_limit_total",
                limit=self.max_total_subscribers,
                current=self.total_subscribers,
            )
        subscriber = hub.subscribe(welcome=not wire_v4)
        subscriber.wire_v4 = wire_v4
        self.total_subscribers += 1
        # ---- end critical section ----
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """Idempotently release a subscriber slot and arm idle teardown.

        Calling twice with the same subscriber is a no-op (the second call
        finds the subscriber already absent from ``hub.subscribers`` and
        returns without decrementing ``total_subscribers``).
        """
        hub = self._global
        if hub is None or subscriber not in hub.subscribers:
            return
        hub.subscribers.discard(subscriber)
        self.total_subscribers -= 1
        if self.total_subscribers < 0:
            # Defensive: should never happen given the idempotency check
            # above, but a misbehaving caller must not corrupt admission.
            self.total_subscribers = 0
        # NB-D3: arm on the unified has_consumers() predicate (spans BOTH
        # ledgers), not just the control-plane set. When a token subscriber
        # still keeps the hub alive, do NOT arm a doomed-to-no-op task — the
        # token last-detach arms it via TokenStreamRegistry.unsubscribe
        # (B-D1). Symmetric with subscribe's cancel_pending_removal /
        # ensure_upstream. For pure control-plane flows this is behaviorally
        # identical to the old ``not hub.subscribers`` guard (no token hub ⇒
        # has_consumers() == bool(self.subscribers)).
        self.maybe_arm_grace_if_idle()

    def _clear_removal_task_if_current(self) -> None:
        """F-011: clear the ``_removal_task`` slot only if THIS task owns it.

        Identity-conditional (``asyncio.current_task()``) so a STALE grace
        task's exit path can never erase a NEWER task's reference. Race
        being closed (audit F-011 / B3): task1 is cancelled (its canceller
        clears the registry slot synchronously), task2 is armed into the
        slot, and only THEN is task1's coroutine scheduled to run its
        cancellation / exit path. An unconditional slot clear executed by
        task1 would drop the registry's only handle on task2 — task2 could
        then never be cancelled by ``close()`` and, sleeping past
        ``GRACE_SECONDS``, would tear down a live hub. Under the identity
        check task1 sees the slot is not itself and leaves it untouched.

        Called from a sync (non-task) context, ``current_task()`` is None
        and only matches an already-empty slot — a deliberate no-op. The
        external SYNC clear paths (``cancel_pending_removal`` /
        ``close``) keep their direct unconditional clears: they run in the
        canceller's frame, not the task's, and own the slot by convention.
        """
        if self._removal_task is asyncio.current_task():
            self._removal_task = None

    async def _remove_hub_after_grace(self, hub: GlobalHub) -> None:
        """Tear down an idle hub after GRACE_SECONDS and drop the reference.

        Mirrors :meth:`GlobalHub.stop_after_grace` but additionally nulls the
        registry's strong reference so the hub (and its task handles) can be
        GC'd — avoiding unbounded growth if the process lives long enough to
        cycle through many idle periods. A new ``subscribe()`` arriving
        during the grace window cancels this task.

        INV-2 (P0-2): epoch strictly serial. After cancelling the hub's 4
        tasks we ``await asyncio.gather(*tasks, return_exceptions=True)``
        (aligned with :meth:`close`) so the old ``run()`` fully exits and
        releases its ``/global/event`` connection BEFORE we null the
        reference. After the gather we re-check ``hub is self._global and
        not hub.has_consumers()`` — a subscriber arriving during the await
        may have revived the hub, in which case removal is abandoned. The
        cleanup→null segment after re-check is a no-await sync block:

        * ``token_hub.on_upstream_reconnect()`` clears old-epoch state
          (live_parts / _session_status / _busy_sids / _retired_messages).
          ``has_consumers()`` is False here → the resync fanout is a natural
          no-op (zero wire impact). ``_part_revisions`` (CRITICAL 1) and
          ``_removed_messages`` (replay queue) are PRESERVED.
        * ``self._global = None`` drops the strong reference.

        F-011: every slot clear inside this coroutine goes through
        :meth:`_clear_removal_task_if_current` (identity-conditional — a
        stale incarnation of this task must not erase a newer task's
        reference), and the whole teardown body is wrapped so an exception
        (e.g. a raising ``on_upstream_reconnect``) can no longer kill the
        task mid-flight and leave the registry holding a dead task it will
        never again re-arm — the failed clear previously disabled
        ``maybe_arm_grace_if_idle`` for the rest of the process lifetime.
        """
        try:
            await asyncio.sleep(GRACE_SECONDS)
        except asyncio.CancelledError:
            self._clear_removal_task_if_current()
            return
        try:
            if hub is not self._global or hub.has_consumers():
                # Either a new hub replaced this one, or a new subscriber
                # arrived during grace and re-armed the upstream. Leave it.
                # Stage B: has_consumers() spans BOTH ledgers — a token-only
                # subscriber (Stage D) keeps the hub alive too (§16-B).
                self._clear_removal_task_if_current()
                return
            # Cancel the hub's 4 tasks.
            tasks = [
                task for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
                if task is not None and not task.done()
            ]
            for task in tasks:
                task.cancel()
            # INV-2: await full exit so the old run() releases /global/event
            # BEFORE we null the reference (aligns with close()'s gather).
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    # A new subscriber (or close()) cancelled this removal
                    # task during the gather. Return without clearing the
                    # slot ourselves — the canceller owns it (and the
                    # finally below only clears it if we still do).
                    return
            # INV-2: re-check after gather — a new subscriber may have arrived
            # during the await, reviving the hub. If so, abandon removal.
            if hub is not self._global or hub.has_consumers():
                self._clear_removal_task_if_current()
                return
            # INV-2: no-await sync segment. on_upstream_reconnect() clears the
            # token hub's old-epoch state so the next hub starts clean.
            # has_consumers()==False → resync fanout is a no-op. CRITICAL 1:
            # _part_revisions and _removed_messages are PRESERVED by
            # on_upstream_reconnect (see its docstring).
            if self._token_hub is not None:
                self._token_hub.on_upstream_reconnect()
            self._global = None
            self._clear_removal_task_if_current()
        except Exception:
            # F-011: teardown must never strand the registry with a dead
            # task in the slot — log and let the finally release it so a
            # later idle period can re-arm. CancelledError is NOT caught
            # here (BaseException): cancellation keeps its propagate-and-
            # return semantics via the branches above.
            logger.warning("hub grace removal failed", exc_info=True)
        finally:
            self._clear_removal_task_if_current()

    def snapshot_metrics(self) -> dict[str, Any]:
        """Return the /slimapi/metrics snapshot (contract §2 / REFINE §3).

        Shape (strict):
            {"sse": {"subscribers": {current, limit, rejectedTotal},
                     "hubs":        [{subscribers, upstreamConnected,
                                      upstreamEventsTotal, emittedFramesTotal,
                                      reconnectsTotal}],
                     "clients":     [{subscriberId, queueItems, bufferBytes,
                                      droppedFramesTotal,
                                      forcedDisconnectsTotal}]},
             "skeleton": {activeTransforms, waitingTransforms, cacheEnabled}}
        """
        hub = self._global
        clients: list[dict[str, Any]] = []
        hubs: list[dict[str, Any]] = []
        if hub is not None:
            hubs.append({
                "subscribers": len(hub.subscribers),
                "upstreamConnected": hub.ever_connected,
                "upstreamEventsTotal": hub.upstream_events_total,
                "emittedFramesTotal": hub.emitted_frames_total,
                "reconnectsTotal": hub.reconnects_total,
            })
            for sub in hub.subscribers:
                clients.append({
                    "subscriberId": sub.id,
                    "queueItems": sub.queue.qsize(),
                    "bufferBytes": sub.queued_bytes,
                    "droppedFramesTotal": sub.dropped_frames,
                    "forcedDisconnectsTotal": sub.forced_disconnects,
                })
        return {
            "sse": {
                "subscribers": {
                    "current": self.total_subscribers,
                    "limit": self.max_total_subscribers,
                    "rejectedTotal": self.rejected_total,
                },
                "hubs": hubs,
                "clients": clients,
            },
            "skeleton": self._snapshot_skeleton(),
        }

    def _snapshot_skeleton(self) -> dict[str, Any]:
        """Read TransformPool counters via its public API.

        Uses ``pool.snapshot_metrics()`` instead of duck-typing the
        semaphore's private ``_value`` / ``_waiters``. Skeleton shared cache
        is YAGNI for v1 (contract §10) so cacheEnabled is hard-coded False.
        """
        pool = self._transforms
        if pool is None:
            return {"activeTransforms": 0, "waitingTransforms": 0, "cacheEnabled": False}
        snap = pool.snapshot_metrics()
        return {
            "activeTransforms": snap["active"],
            "waitingTransforms": snap["waiting"],
            "cacheEnabled": False,
        }

    async def close(self) -> None:
        hub = self._global
        if hub is None:
            if self._removal_task is not None:
                self._removal_task.cancel()
                self._removal_task = None
            return
        tasks = [
            task for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
            if task is not None
        ]
        if self._removal_task is not None:
            tasks.append(self._removal_task)
            self._removal_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._global = None
        self.total_subscribers = 0
