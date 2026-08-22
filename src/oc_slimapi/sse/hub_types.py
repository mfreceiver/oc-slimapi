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

from .replay_log import (
    RESYNC_EPOCH_CHANGED,
    RESYNC_REPLAY_EXPIRED,
    RESYNC_REPLAY_GAP,
    RESYNC_RECONNECT_NO_REPLAY,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Resync reason value domain (W1-D / F-122): single frozen import source.
# ---------------------------------------------------------------------------
# rev-gate R3 BLOCKER-1: the frozen v4 ``resync.reason`` value domain
# (v4-contract §7.2 / design §4) — EXACTLY these four values may appear in
# a ``resync`` frame on a v4 wire. Every other legacy v3 reason
# (subscriber_backpressure / token_memory_limit / session_idle /
# session_deleted …) takes the v4 termination route instead: the server
# ends the connection (STOP) WITHOUT emitting an out-of-domain resync
# frame — the disconnect itself is the observable signal, and the client's
# recovery path is Last-Event-ID reconnect → ReplayLog replay (in window)
# or a frozen-reason resync (out of window) → HTTP alignment. This is the
# PRODUCTION allowlist (not a test-only oracle): the subscriber/hub v4
# branches key off it, and the wire tests import the same constant.
#
# This module is the leaf anchor of the reason constants (W1-D): the four
# replay-domain values are imported from :mod:`.replay_log` (their data-
# layer home), the v3-only values are defined here, and everything else
# imports the frozen sets from HERE (``replay_wire`` re-exports
# ``V4_RESYNC_REASONS`` for back-compat).
RESYNC_SUBSCRIBER_BACKPRESSURE = "subscriber_backpressure"
RESYNC_SESSION_IDLE = "session_idle"
RESYNC_SESSION_DELETED = "session_deleted"
RESYNC_TOKEN_MEMORY_LIMIT = "token_memory_limit"

V4_RESYNC_REASONS = frozenset({
    RESYNC_EPOCH_CHANGED,
    RESYNC_REPLAY_EXPIRED,
    RESYNC_REPLAY_GAP,
    RESYNC_RECONNECT_NO_REPLAY,
})

# The COMPLETE value domain any ``resync`` frame ``reason`` may carry on
# ANY wire version: the frozen v4 four plus the v3-only lifecycle reasons
# (v4 expresses those via STOP-termination instead). This is the single
# frozen oracle for tests/test_resync_reason_gate.py (N1 AST scan): a new
# reason value must be added HERE — a visible, reviewable change — before
# it can legally reach an ``sse_frame(..., event="resync")`` construction
# point anywhere under src/.
SSE_RESYNC_REASONS = frozenset(V4_RESYNC_REASONS) | frozenset({
    RESYNC_SUBSCRIBER_BACKPRESSURE,
    RESYNC_SESSION_IDLE,
    RESYNC_SESSION_DELETED,
    RESYNC_TOKEN_MEMORY_LIMIT,
})


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
# F-001 (audit 2026-08-20): upstream opencode v1.18.18 publishes
# ``permission.replied`` (packages/schema/src/v1/permission.ts:61-65) and
# ``permission.v2.replied`` (packages/schema/src/permission.ts:43-45). The
# two former "…resolved" members were ghost names with zero upstream
# emitters — they never matched a real event, so no consumer relies on
# them (the banned literals are deliberately not repeated here; see the
# audit finding for the exact names).
IMMEDIATE = frozenset({
    "question.asked", "question.v2.asked",
    # R-4 (owner ruling 2026-08-21): the question RESOLUTION family joins
    # IMMEDIATE. Upstream opencode v1.18.18 really publishes
    # ``question.replied`` / ``question.rejected`` (packages/schema/src/v1)
    # and ``question.v2.replied`` / ``question.v2.rejected`` — same
    # constructed defect the 4.5.0 permission fix closed: ``asked`` was
    # forwarded instantly but every reply/reject fell through the
    # catch-all drop, so question cards on other clients never
    # disappeared. Clients dispatch on ``data.type``; unknown types are
    # ignore-type additive for unadapted consumers.
    "question.replied", "question.rejected",
    "question.v2.replied", "question.v2.rejected",
    "permission.asked", "permission.replied",
    "permission.v2.asked", "permission.v2.replied",
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
# Q3 owner ruling 2026-08-22: unified with TOKEN_HEARTBEAT_SECONDS (config.py,
# 15s) — one keepalive cadence across control-plane /events and token streams
# (both well under common stunnel/proxy idle thresholds). Was 10.0 pre-Q3.
HEARTBEAT_SECONDS = 15.0
GRACE_SECONDS = 30.0

# F-015 (audit 2026-08-20): hard cap for the shared q/p activity table.
# The table is keyed by directory and, pre-fix, grew unboundedly: the hub's
# IMMEDIATE write point and the sweep's ``record_activity`` both inserted
# without ever evicting. 10_000 entries × ~100 bytes ≈ 1 MiB worst case —
# a deliberate, observable bound. ``hub_types`` is the leaf module both
# write sites already import from, so the helper and the cap live here
# (exactly ONE implementation of the semantics, no import-cycle risk).
QP_LAST_ACTIVITY_MAX = 10_000


def record_qp_activity(table: dict[str, float], directory: str, now: float) -> None:
    """Record one q/p activity touch on ``table`` under an activity-LRU bound.

    F-015 + F-007(half): the two q/p activity write points — GlobalHub's
    IMMEDIATE branch (``global_hub.py``) and ``QpSweepShadow.record_activity``
    (``qp_sweep.py``), which share ONE dict reference by construction
    (app.py wires ``activity=global_hub.qp_last_activity``) — both funnel
    through this helper so the bound holds regardless of which side grows
    the table.

    * Move-to-end: a re-touched directory pops before inserting, so its
      recency is refreshed (a plain ``table[d] = now`` would keep the
      ORIGINAL insertion position in an insertion-ordered dict and the
      entry could be evicted as "old" while actually being the hottest).
      F-273-aligned eviction partner: whoever evicts a directory from the
      sweep tables also pops it from the activity table.
    * Cap: while over ``QP_LAST_ACTIVITY_MAX`` entries, evict the
      least-recently-touched key (front of the insertion order). The cap
      is read from the module global AT CALL TIME so tests (and ops, if
      ever needed) can monkeypatch ``hub_types.QP_LAST_ACTIVITY_MAX``
      once and bound both write points simultaneously.
    """
    table.pop(directory, None)
    table[directory] = now
    while len(table) > QP_LAST_ACTIVITY_MAX:
        table.pop(next(iter(table)))

# Curated-events token frame (L2-A: ``/slimapi/events?tokens=1``). A lean
# projection distinct from the per-session stream's delta frames: carries
# ``type:"token"`` + coalesced ``delta`` only — NO ``partEventRevision`` and
# NO ``directory`` (sessionID is globally unique in single-user T3; the
# authoritative revision / full-text stays on the per-session stream and
# ``/messages/{sid}``). See plan Task L2-A.
TOKEN_FRAME_TYPE = "token"


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
    # either both are emitted or neither is (when the registry is absent —
    # a lifespan-level deployment property — both stay None → both omitted,
    # ocdroid degrades). Once the registry is wired, snapshot always returns
    # a tuple (unobserved sid → (inc, 0)), so every session.status digest
    # carries both fields. Frozen at ingest time (see GlobalHub.publish
    # session.status branch) so a later bump cannot retroactively change an
    # already-stamped digest (contract §3.y.5, V10).
    turn_incarnation: int | None = None
    turn: int | None = None
    # B1a: minimal ``changed`` semantics — a digest frame appearing means
    # that sid changed, so every digest frame carries ``changed: [<this
    # frame's sid>]``. The array shape ``[sid…]`` is kept for future
    # aggregation. ZERO new sidecar state: the value is constructed at flush
    # time from the frame's own sid (never accumulated/tracked here), so the
    # default stays None and ``to_payload`` omits the key for any digest
    # caller that does not opt in.
    changed: list[str] | None = None
    # 4.11.0 Phase A / A3 (P4): messagesRevision — the process-wide
    # monotonic message-universe revision stamped onto MESSAGE windows
    # only (a digest entry that includes message.updated/appended/removed).
    # Session-only digests leave it None → ``to_payload`` omits the key.
    # Lifecycle = the PROCESS: a restart zeroes the counter, so clients
    # MUST NOT compare revisions across processes; upstream resync does
    # NOT reset it (comparable within one process lifetime). Stamped at
    # ingest (overwritten per relevant event — a multi-event debounce
    # window flushes the window-END value).
    messages_revision: int | None = None

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
        # B1a: minimal changed — conditionally included exactly like every
        # other optional digest field (non-None → present).
        if self.changed is not None:
            payload["changed"] = self.changed
        # 4.11.0 Phase A / A3: messagesRevision — message windows only;
        # conditionally emitted exactly like every other optional field.
        if self.messages_revision is not None:
            payload["messagesRevision"] = self.messages_revision
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

    # B3b-2 (v4 SSE replay): set by the /events route when — and only when
    # — the request ran the v4 wire view (selector-admitted ``?v=4``). A
    # ``wire_v4`` subscriber receives business frames WITH their
    # ``id: <domain>:<epoch>:<seq>`` prefix line; v3 subscribers keep the
    # byte-identical id-less frames (the v3 zero-change iron rule). The
    # flag is flipped by the route immediately after ``subscribe()``
    # returns (no await between) so no fanout can race an un-stamped
    # delivery; the subscriber's own welcome ``server.connected`` frame is
    # connection-scoped and never stamped.
    wire_v4: bool = False

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
        # rev-gate R3 BLOCKER-1: ``subscriber_backpressure`` is NOT in the
        # frozen v4 reason domain (V4_RESYNC_REASONS) — a v4 wire never
        # carries it. v4 termination = STOP only (the disconnect itself is
        # the observable signal; recovery = Last-Event-ID reconnect +
        # ReplayLog replay per REPLAY-007). v3 keeps the frozen
        # resync + STOP pair, byte-identical.
        reason = RESYNC_SUBSCRIBER_BACKPRESSURE
        if self.wire_v4 and reason not in V4_RESYNC_REASONS:
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(STOP)
            return False
        resync = sse_frame({"reason": reason}, event="resync")
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


def normalize_session_status(value: Any) -> str | None:
    """Normalize an upstream ``session.status`` value to its string form.

    Upstream ``/global/event`` carries ``properties.status`` in TWO shapes
    (live-wire captured 2026-08-19): the legacy plain string (``"busy"``)
    and the object envelope (``{"type": "busy"}``). Both must resolve to
    the same string so the digest ``status`` fill, the G1 busy-clears-
    sticky path, and the token-hub mirror behave identically regardless
    of which shape arrives.

    Rules:
    * ``str`` → returned as-is (legacy format, full value domain).
    * ``dict`` with a string ``type`` → that string (object envelope).
    * Anything else — dict without a string ``type`` (missing / null /
      non-string), non-dict non-string junk — has **no valid status** →
      ``None`` (the status field is ignored; never a crash).

    Shared by ``global_hub.py`` (digest fill + G1 sticky clear + token-hub
    mirror) and ``tokenstream/hub.py`` (:meth:`on_session_status`) —
    ``hub_types`` is the leaf module both already import from, so there is
    no import-cycle risk and exactly ONE implementation of the semantics.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("type")
        if isinstance(inner, str):
            return inner
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
