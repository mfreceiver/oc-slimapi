"""Curated SSE bridge: single global /global/event subscription.

The hub holds one upstream connection to opencode's process-level GlobalBus
and emits a small, opinionated set of frames to all local subscribers:

* ``session.digest`` — debounced 250ms / session; merges status / messageID /
  updatedAt / archived / deleted fields.
* ``question.*`` / ``permission.*`` — forwarded immediately, no debounce.
* ``server.connected`` — emitted once per subscriber on subscribe.
* ``server.heartbeat`` — every 10s.
* ``resync`` — emitted to every subscriber when the upstream reconnects or
  when an individual subscriber's queue overflows (``subscriber_backpressure``).

T3 hardening (v1 contract §6): each subscriber carries its own byte budget
(``sse_buffer_bytes``) and per-frame ceiling (``sse_max_frame_bytes``). On
overflow the queue is cleared *immediately* and replaced with a single
``resync`` frame + STOP sentinel — old queued frames are NOT delivered
(contrast with the previous behavior of draining the queue to completion).
Admission control runs in :meth:`HubRegistry.subscribe` inside one
synchronous (no-await) critical section so a concurrent coroutine can never
slip in between the capacity check and the increment.

The registry exposes the same ``HubRegistry(client)`` + ``close()`` signatures
that ``app.py`` already calls; the T3 knobs flow in as keyword arguments
populated from :class:`oc_slimapi.config.Settings`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import contextlib
import secrets
import time
from typing import Any

import httpx
import orjson


STOP = object()

# T3 defaults used when a caller constructs GlobalHub / HubRegistry without
# explicit knobs (tests, ad-hoc scripts). Production wiring overrides these
# via oc_slimapi.config.Settings so the values come from environment.
DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY = 8
DEFAULT_MAX_TOTAL_SUBSCRIBERS = 16
DEFAULT_SSE_QUEUE_ITEMS = 256
DEFAULT_SSE_BUFFER_BYTES = 2 * 1024 * 1024  # 2 MiB per subscriber
DEFAULT_SSE_MAX_FRAME_BYTES = 256 * 1024     # 256 KiB per frame

# Blocking signals forwarded the moment they arrive — no debounce.
IMMEDIATE = frozenset({
    "question.asked", "question.v2.asked",
    "permission.asked", "permission.resolved",
    "permission.v2.asked", "permission.v2.resolved",
})

# Session-scoped events folded into the digest window.
SESSION_EVENTS = frozenset({
    "session.status", "session.updated", "session.deleted",
})

# Message-scoped events folded into the digest window.
MESSAGE_EVENTS = frozenset({
    "message.updated", "message.appended",
})

DEBOUNCE_SECONDS = 0.25
HEARTBEAT_SECONDS = 10.0
GRACE_SECONDS = 30.0


def sse_frame(payload: dict[str, Any], event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return prefix.encode() + b"data: " + orjson.dumps(payload) + b"\n\n"


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class DigestFields:
    """Accumulated per-session state within one debounce window.

    ``archived`` holds the epoch-ms timestamp at which the session was
    archived (sourced from ``info.time.archived`` on ``session.updated``).
    It mirrors the sticky semantics of ``deleted``: once a non-null value
    has been observed within a debounce window, the timestamp stays set on
    the emitted digest even if a later event in the same window does not
    itself carry the marker (archived is a permanent state — clients hide
    the session locally once they see it, contract §3).

    Typed ``int | None`` (epoch ms) to match the client's ``Long?`` field
    and to stay consistent with how :pyattr:`updated_at` carries
    ``info.time.updated``/``created`` — both are epoch-ms ints passed
    through from the upstream JSON as-is.
    """

    directory: str | None = None
    status: str | None = None
    message_id: str | None = None
    updated_at: Any = None
    archived: int | None = None
    deleted: bool = False

    def to_payload(self, session_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"sessionID": session_id}
        if self.directory is not None:
            payload["directory"] = self.directory
        if self.status is not None:
            payload["status"] = self.status
        if self.message_id is not None:
            payload["messageID"] = self.message_id
        if self.updated_at is not None:
            payload["updatedAt"] = self.updated_at
        if self.archived is not None:
            payload["archived"] = self.archived
        if self.deleted:
            payload["deleted"] = True
        return payload


@dataclass(eq=False)
class Subscriber:
    """One client's outbound queue with T3 byte/frame guards.

    ``put`` enforces three guards in order:

    1. Already closed (a previous overflow) → silent drop.
    2. Single frame larger than ``max_frame_bytes`` → ``dropped_frames`` bump,
       not enqueued (the frame would otherwise monopolize the byte budget
       and displace many small ones).
    3. Either the queue is at ``queue_items`` OR the buffer would exceed
       ``buffer_bytes`` → **immediate disconnect**: ``closed = True``,
       ``forced_disconnects`` bump, queue drained, queued_bytes reset, and a
       single ``resync{reason:subscriber_backpressure}`` frame + ``STOP``
       sentinel enqueued so the SSE generator tears the connection down
       promptly. Crucially the previously-queued frames are NOT delivered —
       the contract (§6) mandates this so a slow client cannot keep
       draining stale data after the sidecar has decided it is too far
       behind.
    """

    # Configuration (immutable per subscriber once admitted).
    queue_items: int = DEFAULT_SSE_QUEUE_ITEMS
    buffer_bytes: int = DEFAULT_SSE_BUFFER_BYTES
    max_frame_bytes: int = DEFAULT_SSE_MAX_FRAME_BYTES

    # Identity / metrics.
    id: str = field(default_factory=lambda: "sub_" + secrets.token_hex(4))
    queued_bytes: int = 0
    closed: bool = False
    dropped_frames: int = 0
    forced_disconnects: int = 0

    # Backing queue (constructed post-init so maxsize honours queue_items).
    queue: asyncio.Queue = field(default=None)

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=self.queue_items)

    def put(self, frame: Any) -> None:
        if self.closed:
            # Post-disconnect: silently drop. The resync + STOP pair already
            # enqueued by the overflow path is the only thing the SSE
            # generator should see.
            return
        if frame is STOP:
            # Control sentinel — always passes (used by the overflow path
            # itself and by orderly shutdown).
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(STOP)
            return
        size = len(frame)
        if size > self.max_frame_bytes:
            self.dropped_frames += 1
            return
        if (
            self.queue.qsize() < self.queue_items
            and self.queued_bytes + size <= self.buffer_bytes
        ):
            try:
                self.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Lost a race against a concurrent producer (there is none
                # in practice since publish/flush run inline on the loop);
                # treat as overflow below.
                pass
            else:
                self.queued_bytes += size
                return
        # Overflow: immediate disconnect per contract §6.
        self.closed = True
        self.forced_disconnects += 1
        self._clear_queue()
        resync = sse_frame({"reason": "subscriber_backpressure"}, event="resync")
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(resync)
            self.queue.put_nowait(STOP)

    def _clear_queue(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.queued_bytes = 0


class GlobalHub:
    """One process-wide upstream subscription fanning out curated frames."""

    def __init__(
        self,
        client: httpx.AsyncClient | None,
        *,
        queue_items: int = DEFAULT_SSE_QUEUE_ITEMS,
        buffer_bytes: int = DEFAULT_SSE_BUFFER_BYTES,
        max_frame_bytes: int = DEFAULT_SSE_MAX_FRAME_BYTES,
    ):
        self.client = client
        self.subscribers: set[Subscriber] = set()
        self.queue_items = queue_items
        self.buffer_bytes = buffer_bytes
        self.max_frame_bytes = max_frame_bytes
        self.task: asyncio.Task | None = None
        self.flush_task: asyncio.Task | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.stop_task: asyncio.Task | None = None
        self.ever_connected = False
        self.pending: dict[str, DigestFields] = {}
        # T3 observability counters (contract §6 / §2 metrics endpoint).
        self.upstream_events_total = 0
        self.emitted_frames_total = 0
        self.reconnects_total = 0

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(
            queue_items=self.queue_items,
            buffer_bytes=self.buffer_bytes,
            max_frame_bytes=self.max_frame_bytes,
        )
        # Welcome frame first so the client sees it before any digest/heartbeat.
        subscriber.put(sse_frame({}, event="server.connected"))
        self.subscribers.add(subscriber)
        if self.stop_task:
            self.stop_task.cancel()
            self.stop_task = None
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.run())
            self.flush_task = asyncio.create_task(self.flush_loop())
            self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)
        if not self.subscribers and not self.stop_task:
            self.stop_task = asyncio.create_task(self.stop_after_grace())

    async def stop_after_grace(self) -> None:
        await asyncio.sleep(GRACE_SECONDS)
        if not self.subscribers:
            for task in (self.task, self.flush_task, self.heartbeat_task):
                if task:
                    task.cancel()

    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        snapshot, self.pending = self.pending, {}
        for session_id, fields in snapshot.items():
            frame = sse_frame(fields.to_payload(session_id), event="session.digest")
            for subscriber in tuple(self.subscribers):
                subscriber.put(frame)
            if self.subscribers:
                self.emitted_frames_total += len(self.subscribers)

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            frame = sse_frame({}, event="server.heartbeat")
            for subscriber in tuple(self.subscribers):
                subscriber.put(frame)
            if self.subscribers:
                self.emitted_frames_total += len(self.subscribers)

    def publish(self, global_event: dict[str, Any]) -> None:
        # Count every JSON-decoded upstream event we were asked to consider;
        # early-returns below still represent real traffic the GlobalBus saw.
        self.upstream_events_total += 1
        directory = global_event.get("directory")
        payload = global_event.get("payload")
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        props = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}

        # Blocking signals: forward raw, no debounce.
        if event_type in IMMEDIATE:
            frame = sse_frame({
                "directory": directory,
                "type": event_type,
                "properties": props,
            })
            for subscriber in tuple(self.subscribers):
                subscriber.put(frame)
            if self.subscribers:
                self.emitted_frames_total += len(self.subscribers)
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
                status = props.get("status")
                if isinstance(status, str):
                    entry.status = status
            elif event_type == "session.deleted":
                entry.deleted = True
            elif event_type == "session.updated":
                # Contract §3: archived ← session.updated's info.time.archived
                # (epoch-ms int). Pass-through — same handling as
                # info.time.updated/created in the MESSAGE_EVENTS branch below
                # (opencode emits epoch-ms ints; we trust the upstream format
                # and do not coerce). Sticky: a falsy/missing value does NOT
                # clear an already-observed timestamp (archived is permanent,
                # mirroring the deleted-stickiness philosophy). Other
                # session.updated fields flow through subsequent events.
                info = props.get("info") if isinstance(props.get("info"), dict) else {}
                time_obj = info.get("time") if isinstance(info.get("time"), dict) else {}
                archived_val = time_obj.get("archived")
                if archived_val:
                    entry.archived = archived_val
            return

        if event_type in MESSAGE_EVENTS:
            session_id = _extract_session_id(payload, props)
            if not isinstance(session_id, str):
                return
            info = props.get("info") if isinstance(props.get("info"), dict) else {}
            message_id = info.get("id") if isinstance(info.get("id"), str) else props.get("messageID")
            time_obj = info.get("time") if isinstance(info.get("time"), dict) else {}
            updated_at = time_obj.get("updated") or time_obj.get("created") or _now_ms()
            entry = self.pending.setdefault(session_id, DigestFields())
            if isinstance(directory, str):
                entry.directory = directory
            if isinstance(message_id, str):
                entry.message_id = message_id
            entry.updated_at = updated_at
            return

        # Drop text deltas, tool.*, message.part.*, and anything else.

    def resync_all(self) -> None:
        frame = sse_frame({"reason": "reconnect_no_replay"}, event="resync")
        for subscriber in tuple(self.subscribers):
            subscriber.put(frame)
        if self.subscribers:
            self.emitted_frames_total += len(self.subscribers)

    async def run(self) -> None:
        delay = 1.0
        while self.subscribers:
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
                        self.resync_all()
                    self.ever_connected = True
                    delay = 1.0
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                        elif not line and data_lines:
                            try:
                                self.publish(orjson.loads("\n".join(data_lines)))
                            except orjson.JSONDecodeError:
                                pass
                            data_lines.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.ever_connected:
                    self.resync_all()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)


def _extract_session_id(payload: dict[str, Any], props: dict[str, Any]) -> str | None:
    sid = props.get("sessionID")
    if isinstance(sid, str):
        return sid
    info = props.get("info") if isinstance(props.get("info"), dict) else {}
    candidate = info.get("sessionID")
    if isinstance(candidate, str):
        return candidate
    raw_id = payload.get("id")
    if isinstance(raw_id, str):
        return raw_id
    event_type = payload.get("type")
    if (
        isinstance(event_type, str)
        and event_type.startswith("session.")
        and isinstance(info.get("id"), str)
    ):
        return info["id"]
    return None


class SubscriberCapacityError(Exception):
    """Raised when admission would exceed a T3 subscriber cap (contract §7).

    ``code`` is one of ``sse_subscriber_limit_directory`` or
    ``sse_subscriber_limit_total``; ``limit`` / ``current`` are surfaced both
    on the wire (503 body) and via the metrics endpoint.
    """

    def __init__(self, code: str, *, limit: int, current: int):
        self.code = code
        self.limit = limit
        self.current = current
        super().__init__(f"{code}: current={current}, limit={limit}")


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
    ):
        self.client = client
        self._global: GlobalHub | None = None
        self.max_subscribers_per_directory = max_subscribers_per_directory
        self.max_total_subscribers = max_total_subscribers
        self.queue_items = queue_items
        self.buffer_bytes = buffer_bytes
        self.max_frame_bytes = max_frame_bytes
        self.total_subscribers = 0
        self.rejected_total = 0
        self._transforms: Any = None  # TransformPool, wired from app.py for metrics.
        self._removal_task: asyncio.Task | None = None

    def set_transforms(self, pool: Any) -> None:
        """Wire the TransformPool so snapshot_metrics() can report active/waiting.

        The pool lives in ``app.state.transforms``; the registry holds only a
        reference for metrics purposes and never calls into it.
        """
        self._transforms = pool

    def get(self, directory: str | None = None) -> GlobalHub:
        if self._global is None:
            self._global = GlobalHub(
                self.client,
                queue_items=self.queue_items,
                buffer_bytes=self.buffer_bytes,
                max_frame_bytes=self.max_frame_bytes,
            )
        return self._global

    def get_global(self) -> GlobalHub:
        return self.get(None)

    def subscribe(self) -> Subscriber:
        """Admit a new subscriber under T3 caps, then start / reuse the hub.

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
        subscriber = hub.subscribe()
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
        if not hub.subscribers and self._removal_task is None:
            self._removal_task = asyncio.create_task(self._remove_hub_after_grace(hub))

    async def _remove_hub_after_grace(self, hub: GlobalHub) -> None:
        """Tear down an idle hub after GRACE_SECONDS and drop the reference.

        Mirrors :meth:`GlobalHub.stop_after_grace` but additionally nulls the
        registry's strong reference so the hub (and its task handles) can be
        GC'd — avoiding unbounded growth if the process lives long enough
        to cycle through many idle periods. A new ``subscribe()`` arriving
        during the grace window cancels this task.
        """
        try:
            await asyncio.sleep(GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        if hub is not self._global or hub.subscribers:
            # Either a new hub replaced this one, or a new subscriber
            # arrived during grace and re-armed the upstream. Leave it.
            self._removal_task = None
            return
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task):
            if task is not None and not task.done():
                task.cancel()
        self._global = None
        self._removal_task = None

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
        """Read TransformPool counters without importing the module.

        Avoids a circular import (transform.py pulls skeleton.py); we duck-type
        the semaphore's private ``_value`` / ``_waiters``. Skeleton shared cache
        is YAGNI for v1 (contract §10) so cacheEnabled is hard-coded False.
        """
        pool = self._transforms
        if pool is None:
            return {"activeTransforms": 0, "waitingTransforms": 0, "cacheEnabled": False}
        config = pool.config
        semaphore = pool._semaphore  # type: ignore[attr-defined]
        available = getattr(semaphore, "_value", config.max_transforms)
        active = max(0, config.max_transforms - available)
        waiters = getattr(semaphore, "_waiters", None)
        if waiters:
            waiting = sum(1 for fut in waiters if not fut.done())
        else:
            waiting = 0
        return {
            "activeTransforms": active,
            "waitingTransforms": waiting,
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
