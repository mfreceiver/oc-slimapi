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
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

import httpx
import orjson

if TYPE_CHECKING:
    from ..children_cache import ChildrenCache
    from .token_hub import TokenStreamHub


STOP = object()

_UNSET = object()  # three-state sentinel for DigestFields.last_error
ABORT_NAME = "MessageAbortedError"

_UNIX_PATH_RE = re.compile(r"/(?:[A-Za-z0-9._\-]+/)*[A-Za-z0-9._\-]+")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:(?:[\\/][A-Za-z0-9._\-]+)+")
_STACK_FRAME_RE = re.compile(r"\s*\bat\s+\S+?:\d+(?::\d+)?", re.IGNORECASE)
# Plan regex left "Authorization: Bearer <tok>" as "<redacted> <tok>" because the
# value class stops at the first space after "Bearer". Optional "Bearer " after
# the separator makes the golden 'Authorization: Bearer abc.def' → '<redacted>'.
_SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|client[_-]?secret|auth[_-]?token|api[_-]?key|token|key|bearer|password|passwd|secret|authorization)\b"
    r"\s*[:=]\s*(?:bearer\s+)?[\"']?[A-Za-z0-9._\-/=+]+"
)


def _sanitize_error_message(message: str | None, fallback_name: str | None) -> str:
    """G1 desensitization (impl-spec §7 硬约束 4): first line → strip abs paths
    → strip stack frames → strip secrets → truncate ≤512. Missing message falls
    back to the error name, else "(no detail)"."""
    if not message or not isinstance(message, str):
        return fallback_name or "(no detail)"
    first_line = message.split("\n", 1)[0]
    first_line = _WIN_PATH_RE.sub("<path>", first_line)   # Windows first (drive letter)
    first_line = _UNIX_PATH_RE.sub("<path>", first_line)
    first_line = _STACK_FRAME_RE.sub("", first_line)
    first_line = _SECRET_RE.sub("<redacted>", first_line)
    first_line = first_line.strip()
    if len(first_line) > 512:
        first_line = first_line[:512]
    return first_line or fallback_name or "(no detail)"

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
    children_version: int | None = None
    deleted: bool = False
    last_error: Any = _UNSET  # three-state: _UNSET=omit, None=explicit clear, dict=object

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
        if self.children_version is not None:
            payload["childrenVersion"] = self.children_version
        if self.deleted:
            payload["deleted"] = True
        if self.last_error is not _UNSET:
            payload["lastError"] = self.last_error
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

    def put(self, frame: Any) -> bool:
        """Enqueue ``frame`` for this subscriber under the T3 guards.

        Returns ``True`` iff the frame was actually accepted onto the queue
        (so the caller can count it as a successfully emitted frame); returns
        ``False`` on every non-success exit (closed, oversized dropped,
        overflow path with self-produced resync+STOP, STOP that could not be
        enqueued). Byte bookkeeping and overflow behaviour are unchanged from
        the v1 contract — only the return value was added in v6 §3.5.
        """
        if self.closed:
            # Post-disconnect: silently drop. The resync + STOP pair already
            # enqueued by the overflow path is the only thing the SSE
            # generator should see.
            return False
        if frame is STOP:
            # Control sentinel — only return True if it actually landed.
            try:
                self.queue.put_nowait(STOP)
            except asyncio.QueueFull:
                return False
            return True
        size = len(frame)
        if size > self.max_frame_bytes:
            self.dropped_frames += 1
            return False
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
                return True
        # Overflow: immediate disconnect per contract §6.
        self.closed = True
        self.forced_disconnects += 1
        self._clear_queue()
        resync = sse_frame({"reason": "subscriber_backpressure"}, event="resync")
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(resync)
            self.queue.put_nowait(STOP)
        return False

    def ack(self, frame: Any) -> None:
        """Decrement ``queued_bytes`` for a frame consumed from the queue.

        Size accounting is the exact mirror of :meth:`put` (``len(frame)`` for
        non-STOP frames). STOP is a control sentinel that ``put`` never adds
        to the byte ledger, so callers must not ``ack`` STOP either. Floor at
        0 so a mis-paired ack cannot under-flow the counter.
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
        self._children_cache: ChildrenCache | None = None
        # Token-stream accumulator (design-token-stream.md). Injected from
        # app.py via set_token_hub() exactly like _children_cache above.
        # When None, message.part.delta/updated fall through to the
        # catch-all drop (Stage A: hub still works without a token hub).
        self._token_hub: TokenStreamHub | None = None
        # Stage B (§16-B): per-epoch upstream-loss notification guard.
        # Set to True the first time _notify_upstream_loss() fires after a
        # successful connect; reset to False on every successful (re)connect.
        # WHY: without this, the ``except Exception`` branch of run() would
        # call _notify_upstream_loss() on EVERY retry-loop iteration, swamping
        # subscribers with redundant resync frames + re-clearing the token
        # hub on each retry. We want once-per-epoch-transition.
        self._upstream_loss_notified: bool = False
        # G1 sticky lastError: sid -> lastError dict (cleared = popped).
        self.sticky_last_error: dict[str, dict] = {}
        # C⑩ tombstone: sids whose session.deleted digest has been emitted.
        # Survives pending eviction so a LATE session.error (arriving after
        # flush() cleared the deleted entry from self.pending) cannot revive
        # the sticky lastError for an already-deleted session. Complements the
        # same-window ``if entry.deleted: return`` guard. Pruned on
        # resync_all() (cold-start semantics — a resync means the client
        # cold-starts anyway; sids are unique in opencode so a tombstone
        # persisting until resync is correct and the set cannot grow
        # unbounded across reconnects).
        self.deleted_tombstones: set[str] = set()
        # T3 observability counters (contract §6 / §2 metrics endpoint).
        self.upstream_events_total = 0
        self.emitted_frames_total = 0
        self.reconnects_total = 0

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
        """
        if self.stop_task:
            self.stop_task.cancel()
            self.stop_task = None
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.run())
            self.flush_task = asyncio.create_task(self.flush_loop())
            self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(
            queue_items=self.queue_items,
            buffer_bytes=self.buffer_bytes,
            max_frame_bytes=self.max_frame_bytes,
        )
        # Welcome frame first so the client sees it before any digest/heartbeat.
        subscriber.put(sse_frame({}, event="server.connected"))
        self.subscribers.add(subscriber)
        self.ensure_upstream()
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)
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
        """
        self.resync_all()
        if self._token_hub is not None:
            self._token_hub.on_upstream_reconnect()

    async def stop_after_grace(self) -> None:
        await asyncio.sleep(GRACE_SECONDS)
        if not self.has_consumers():
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
            # Merge sticky lastError only when entry did not set/clear it this window.
            if fields.last_error is _UNSET and session_id in self.sticky_last_error:
                fields.last_error = self.sticky_last_error[session_id]
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

    def set_token_hub(self, token_hub: TokenStreamHub | None) -> None:
        """Wire the TokenStreamHub so publish() can route
        ``message.part.delta`` / ``message.part.updated`` into it.

        Mirrors :meth:`HubRegistry.set_children_cache`: the registry owns
        the canonical reference and pushes it onto the live GlobalHub (and
        onto any hub constructed later via :meth:`HubRegistry.get`).
        """
        self._token_hub = token_hub

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
                # G1: busy clears sticky lastError with explicit null digest.
                if props.get("status") == "busy" and session_id in self.sticky_last_error:
                    self.sticky_last_error.pop(session_id, None)
                    entry.last_error = None  # explicit null → clear frame
                    self.flush()
            elif event_type == "session.deleted":
                entry.deleted = True
                # C⑩: record a tombstone that survives pending eviction so a
                # LATE session.error (post-flush) cannot revive lastError.
                self.deleted_tombstones.add(session_id)
                # G1: deleted pops sticky; digest omits lastError.
                self.sticky_last_error.pop(session_id, None)
                entry.last_error = _UNSET
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
            # Stage B §16-B: PARALLEL route to the token hub. The digest
            # work above is the control-plane contract (unchanged); this
            # branch mirrors session.status / session.deleted into the
            # token accumulator so it can maintain _busy_sids / retire
            # abandoned LiveParts. It MUST NOT touch entry/flush/subscribers.
            if self._token_hub is not None and event_type in (
                "session.status", "session.deleted",
            ):
                if event_type == "session.status":
                    status = props.get("status")
                    if isinstance(status, str):
                        self._token_hub.on_session_status(session_id, status)
                else:  # session.deleted
                    self._token_hub.on_session_deleted(session_id)
            return

        if event_type == "session.created":
            info = props.get("info") if isinstance(props.get("info"), dict) else {}
            parent_id = info.get("parentID")
            if not isinstance(parent_id, str) or not parent_id or self._children_cache is None:
                return
            self._children_cache.invalidate(parent_id)
            entry = self.pending.setdefault(parent_id, DigestFields())
            entry.children_version = self._children_cache.generation_of(parent_id)
            if isinstance(directory, str):
                entry.directory = directory
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

        # G1: session.error — immediate digest (with sid) or session.error frame (session-less).
        # Sid MUST be props.get("sessionID") only — do NOT use _extract_session_id
        # (it falls back to payload.id = GlobalBus event id).
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
                if isinstance(directory, str):
                    entry.directory = directory
                self.flush()  # G1-A immediate
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
                for subscriber in tuple(self.subscribers):
                    subscriber.put(frame)
                if self.subscribers:
                    self.emitted_frames_total += len(self.subscribers)
            return

        # Token-stream ingest (design-token-stream.md §5.3): route the
        # per-token firehose into the TokenStreamHub BEFORE the catch-all
        # drop. Stage A scope: ingest + data structures only — no flush,
        # no subscribers, no fan-out (Stage B/C/D). Returning here keeps
        # the control-plane branches above untouched AND prevents these
        # high-frequency events from polluting the curated digest. When no
        # token hub is wired, behaviour is unchanged (events are dropped).
        if event_type in ("message.part.delta", "message.part.updated"):
            if self._token_hub is not None:
                if event_type == "message.part.delta":
                    self._token_hub.on_part_delta(props)
                else:
                    self._token_hub.on_part_updated(props)
            return

        # Drop text deltas, tool.*, message.part.*, and anything else.

    def resync_all(self) -> None:
        # C⑩: cold-start semantics — a resync means the client cold-starts
        # anyway, so deleted tombstones are pruned to prevent unbounded growth
        # across reconnects (sids are unique per opencode process run).
        self.deleted_tombstones.clear()
        frame = sse_frame({"reason": "reconnect_no_replay"}, event="resync")
        for subscriber in tuple(self.subscribers):
            subscriber.put(frame)
        if self.subscribers:
            self.emitted_frames_total += len(self.subscribers)

    def notify_reconfigured(self, reason: str) -> int:
        """Push one ``server.reconfigured`` frame to every active subscriber.

        Counter increment is keyed on :meth:`Subscriber.put` returning True
        (frame actually landed on the queue) so a closed/overflowed
        subscriber does not get counted as an emit. Returns the number of
        subscribers that successfully received the frame. Used by
        ``HubRegistry.notify_reconfigured_if_active`` (the only intended
        caller) to signal a discovery-state change; subscribers must treat
        it as a cold-start trigger.
        """
        frame = sse_frame({"reason": reason, "at": _now_ms()}, event="server.reconfigured")
        emitted = 0
        for subscriber in tuple(self.subscribers):
            if subscriber.put(frame):
                emitted += 1
        if emitted:
            self.emitted_frames_total += emitted
        return emitted

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
                        # successful-reconnect path. Once-per-epoch semantics
                        # are preserved because the reconnect itself
                        # establishes a new epoch (the previous loss, if any,
                        # was already notified on the exception path; the
                        # reconnect flag resets below).
                        self._notify_upstream_loss()
                    self.ever_connected = True
                    # New epoch begins — reset the per-epoch loss guard so
                    # the NEXT disconnect's first exception can fire.
                    self._upstream_loss_notified = False
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
                # §16-B: notify upstream loss on the exception path — but
                # ONLY on the FIRST exception after a successful connect.
                # Without ``_upstream_loss_notified`` the retry loop would
                # fire on every iteration, swamping subscribers with
                # redundant resync frames + re-clearing the token hub
                # (each call is idempotent, but the fanout is not free).
                if self.ever_connected and not self._upstream_loss_notified:
                    self._notify_upstream_loss()
                    self._upstream_loss_notified = True
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
        self._children_cache: ChildrenCache | None = None
        # Token-stream accumulator (design-token-stream.md); mirrors
        # _children_cache injection — app.py owns construction, registry
        # forwards the reference onto any lazily-created GlobalHub.
        self._token_hub: TokenStreamHub | None = None
        self._removal_task: asyncio.Task | None = None

    def set_transforms(self, pool: Any) -> None:
        """Wire the TransformPool so snapshot_metrics() can report active/waiting.

        The pool lives in ``app.state.transforms``; the registry holds only a
        reference for metrics purposes and never calls into it.
        """
        self._transforms = pool

    def set_children_cache(self, cache: ChildrenCache) -> None:
        self._children_cache = cache
        if self._global is not None:
            self._global._children_cache = cache

    def set_token_hub(self, token_hub: TokenStreamHub | None) -> None:
        """Wire the TokenStreamHub onto the registry and any live GlobalHub.

        Mirrors :meth:`set_children_cache`. ``None`` is accepted so Stage B
        can detach the hub during shutdown if needed.
        """
        self._token_hub = token_hub
        if self._global is not None:
            self._global._token_hub = token_hub

    def get(self, directory: str | None = None) -> GlobalHub:
        if self._global is None:
            self._global = GlobalHub(
                self.client,
                queue_items=self.queue_items,
                buffer_bytes=self.buffer_bytes,
                max_frame_bytes=self.max_frame_bytes,
            )
            self._global._children_cache = self._children_cache
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
        self._removal_task = asyncio.create_task(self._remove_hub_after_grace(hub))

    def notify_reconfigured_if_active(self, reason: str) -> int:
        """Push a ``server.reconfigured`` frame to active subscribers, if any.

        Returns 0 (and **does not** lazily create a GlobalHub) when no hub
        exists yet or no one is listening. External callers (e.g.
        ``load_products``) use this so a discovery-state change between
        connects does not spin up a hub just to push into an empty room.
        """
        hub = self._global
        if hub is not None and hub.subscribers:
            return hub.notify_reconfigured(reason)
        return 0

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
        # NB-D3: arm on the unified has_consumers() predicate (spans BOTH
        # ledgers), not just the control-plane set. When a token subscriber
        # still keeps the hub alive, do NOT arm a doomed-to-no-op task — the
        # token last-detach arms it via TokenStreamRegistry.unsubscribe
        # (B-D1). Symmetric with subscribe's cancel_pending_removal /
        # ensure_upstream. For pure control-plane flows this is behaviorally
        # identical to the old ``not hub.subscribers`` guard (no token hub ⇒
        # has_consumers() == bool(self.subscribers)).
        self.maybe_arm_grace_if_idle()

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
        if hub is not self._global or hub.has_consumers():
            # Either a new hub replaced this one, or a new subscriber
            # arrived during grace and re-armed the upstream. Leave it.
            # Stage B: has_consumers() spans BOTH ledgers — a token-only
            # subscriber (Stage D) keeps the hub alive too (§16-B).
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
