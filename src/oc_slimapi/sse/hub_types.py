"""Types, constants, and helpers for the SSE hub.

Physically extracted from the former monolithic :mod:`oc_slimapi.sse.hub` so
the base layer (sentinels, config defaults, frame helpers, dataclasses, and
the :class:`Subscriber` T3 queue) can be imported without pulling in
:class:`GlobalHub`, :class:`HubRegistry`, or their transitive dependencies.

Ownership: the hub (this file) → the registry. No reverse dependency.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import contextlib
import re
import secrets
import time
from typing import Any

import orjson

from oc_slimapi.logging_config import get_logger

logger = get_logger(__name__)


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
# ``message.appended`` is retained for wire compatibility; current opencode
# GlobalBus primarily emits ``message.updated`` for new messages, but treating
# appended the same is cheap and harmless.
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


def _upstream_line_bytes(line: str) -> int:
    """Byte length to attribute to one ``aiter_lines()`` line from the shared
    upstream ``/global/event`` stream.

    ``aiter_lines()`` strips the trailing newline and decodes UTF-8, so we
    re-encode for a byte-accurate count and add ``+1`` for the stripped line
    terminator so the 省流 ratio is not under-counted in our favour. Empty
    lines (SSE frame separators between ``data:`` blocks) also count —
    ``_upstream_line_bytes("") == 1``, exactly the stripped terminator byte.

    .. note::
       The ``+1`` assumes an LF (``\\n``) line terminator, which is the SSE
       standard and what opencode ``/global/event`` emits. A CRLF
       (``\\r\\n``) upstream would be under-counted by 1 byte per line
       (``aiter_lines`` strips only the ``\\n``, leaving a trailing ``\\r``
       in ``line`` that the ``+1`` does not compensate for) — a conservative
       bias (we'd slightly under-report upstream bytes, making the ratio look
       marginally better than reality).
    """
    return len(line.encode("utf-8", "replace")) + 1


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
    last_error: Any = _UNSET  # three-state: _UNSET=omit, None=explicit clear, dict=object
    # Turn token fence (S1/S9): server-side causal identifiers stamped onto
    # the digest so ocdroid can fence stale turns/incarnations. Paired:
    # either both are emitted or neither is (header-gated; absent scope →
    # both None → both omitted, ocdroid degrades). Frozen at ingest time
    # (see GlobalHub.publish session.status branch) so a later bump cannot
    # retroactively change an already-stamped digest (contract §7.4, V10).
    turn_incarnation: int | None = None
    turn: int | None = None

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
        if self.last_error is not _UNSET:
            payload["lastError"] = self.last_error
        # Turn token fence: emit BOTH flat top-level fields only when the
        # pair is present. They live at the SAME level as sessionID/status
        # /archived/deleted/lastError — ocdroid parses the slimapi flat
        # root as ``event.payload.properties`` (SSEClient.kt B1 path), so a
        # nested ``properties`` sub-dict (the §3.3 diagram, which is an
        # opencode upstream frame shape) would be UNREADABLE. Flat = legible.
        if self.turn_incarnation is not None and self.turn is not None:
            payload["turnIncarnation"] = self.turn_incarnation
            payload["turn"] = self.turn
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
        logger.warning("sse subscriber forced disconnect (backpressure)",
                      extra={"subscriber_id": self.id})
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


def _extract_session_id(payload: dict[str, Any], props: dict[str, Any]) -> str | None:
    """Resolve session id from a GlobalBus event payload.

    Order: ``properties.sessionID`` → ``properties.info.sessionID`` → for
    ``session.*`` events only, ``properties.info.id`` (the session row id).

    Deliberately does **not** fall back to ``payload.id``: on GlobalBus that
    field is the *event* id, not a session id. Using it would hang digests
    under random event UUIDs and corrupt sticky lastError / pending maps.
    """
    sid = props.get("sessionID")
    if isinstance(sid, str):
        return sid
    info = props.get("info") if isinstance(props.get("info"), dict) else {}
    candidate = info.get("sessionID")
    if isinstance(candidate, str):
        return candidate
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
