"""Isolated overflow tests for TokenSubscriber (no hub dependency).

Covers the T3 three-stage guard (closed → oversized-drop →
overflow-disconnect), the CRITICAL 3 handshake/runtime PHYSICAL queue
separation, and the MAJOR 4 attach-failure registry guard.

Scope:
* Run-time item overflow (queue_items cap) — disconnect + resync + STOP.
* Run-time byte-budget overflow (buffer_bytes cap) — disconnect.
* Run-time oversized frame (max_frame_bytes) — drop, no disconnect.
* CRITICAL 3: handshake pre-fill lands in a BOUNDED deque decoupled from
  the runtime asyncio.Queue. Runtime overflow clears ONLY runtime;
   handshake frames survive. Handshake caps (items + bytes) overflow-fail
   (``closed=True``，不静默 drop)。
* CRITICAL 3: consumer drains handshake buffer FIRST, then runtime.
* ack() byte accounting works in both modes (routes via last_get_handshake).
* MAJOR 4: TokenStreamRegistry.subscribe() does not increment the ledger
  when attach_subscriber leaves the sub closed.
"""

from __future__ import annotations

import json

import pytest

from oc_slimapi.sse.tokenstream.frames import (
    STOP,
    _connected_frame,
    _delta_frame,
    _message_removed_frame,
    _resync_frame,
    _snapshot_frame,
)
from oc_slimapi.sse.tokenstream.models import _TokenMetrics
from oc_slimapi.sse.tokenstream.subscriber import (
    TokenSubscriber,
    TokenStreamRegistry,
    TokenSubscriberCapacityError,
)


# ---------------------------------------------------------------------------
# Helpers (inlined per repo pattern)
# ---------------------------------------------------------------------------

def parse_event(raw: bytes) -> tuple[str | None, dict]:
    text = raw.decode()
    event_name: str | None = None
    data_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return event_name, data


def drain_queue(sub) -> list:
    """Drain both handshake buffer + runtime queue in consumer order."""
    items = []
    while not sub.queue.empty():
        items.append(sub.queue.get_nowait())
    return items


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def metrics() -> _TokenMetrics:
    return _TokenMetrics()


@pytest.fixture
def tight_sub(metrics: _TokenMetrics) -> TokenSubscriber:
    """Subscriber with tight item/byte caps but generous max_frame_bytes.

    ``queue_items=2``, ``buffer_bytes=200``, ``max_frame_bytes=1024``.
    """
    return TokenSubscriber(
        session_id="s1",
        metrics=metrics,
        queue_items=2,
        buffer_bytes=200,
        max_frame_bytes=1024,
    )


@pytest.fixture
def sample_frame() -> bytes:
    return _delta_frame(("s1", "m1", "p1"), "a")  # ~94 bytes


# ===========================================================================
# Run-time T3 overflow
# ===========================================================================

class TestRuntimeItemOverflow:
    """queue_items cap triggers disconnect (CRITICAL 3 baseline)."""

    def test_fill_then_overflow(self, tight_sub: TokenSubscriber, sample_frame: bytes):
        sub = tight_sub
        # Fill queue to capacity (2 items).
        assert sub.put(sample_frame) is True
        assert sub.put(sample_frame) is True
        assert not sub.closed
        assert sub.queued_bytes > 0
        assert sub.dropped_frames == 0
        # Third put → overflow.
        assert sub.put(sample_frame) is False
        assert sub.closed
        assert sub.forced_disconnects == 1
        assert sub.metrics.dropped_frames_total == 1  # NB-C5

    def test_overflow_clears_queue_and_seals_resync_stop(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        sub = tight_sub
        sub.put(sample_frame)
        sub.put(sample_frame)
        sub.put(sample_frame)  # overflow
        # Runtime queue was cleared; only resync + STOP remain.
        leftover = drain_queue(sub)
        assert len(leftover) == 2
        ev, data = parse_event(leftover[0])
        assert ev == "resync"
        assert data == {"reason": "subscriber_backpressure", "sessionID": "s1"}
        assert leftover[1] is STOP

    def test_overflow_resets_queued_bytes(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        sub = tight_sub
        sub.put(sample_frame)
        sub.put(sample_frame)
        assert sub.queued_bytes > 0
        sub.put(sample_frame)  # overflow → clear_runtime
        # Runtime backlog cleared; the terminal resync+STOP are sealed
        # OUTSIDE the byte ledger (put_runtime_terminal), so queued_bytes
        # reads 0 immediately after overflow (mirrors the pre-CRITICAL-3
        # _clear_queue semantic).
        assert sub.queued_bytes == 0

    def test_closed_sub_drops_subsequent_puts(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        sub = tight_sub
        sub.put(sample_frame)
        sub.put(sample_frame)
        sub.put(sample_frame)  # overflow, closed
        before = sub.metrics.dropped_frames_total
        # Subsequent puts silently drop, do NOT re-bump metric or re-enqueue.
        assert sub.put(sample_frame) is False
        assert sub.metrics.dropped_frames_total == before
        # Still only one resync.
        resyncs = sum(1 for it in drain_queue(sub) if it is not STOP)
        assert resyncs == 1


class TestRuntimeByteBudgetOverflow:
    """buffer_bytes cap also triggers disconnect."""

    def test_byte_budget_overflow_disconnects(self, metrics: _TokenMetrics):
        """Single frame exceeding the remaining byte budget → overflow."""
        # _delta_frame("0123456789") ≈ 103 bytes
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=60, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "0123456789")
        # 0+103 > 60 → first put triggers overflow.
        assert sub.put(frame) is False
        assert sub.closed
        assert sub.forced_disconnects == 1
        assert sub.metrics.dropped_frames_total == 1

    def test_accumulated_bytes_overflow(self, metrics: _TokenMetrics):
        """Cumulative queued_bytes exceeds buffer_bytes on a later put."""
        # _delta_frame("x")=94B, _delta_frame("yyyy")=97B
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=200, max_frame_bytes=1024,
        )
        small = _delta_frame(("s1", "m1", "p1"), "x")
        bigger = _delta_frame(("s1", "m1", "p1"), "yyyy")
        assert sub.put(small) is True   # 0+94=94 <= 200
        assert sub.put(bigger) is True  # 94+97=191 <= 200
        # Third put exceeds 200-byte budget.
        assert sub.put(small) is False  # 191+94=285 > 200
        assert sub.closed

    def test_item_count_under_budget_byte_overflow(self, metrics: _TokenMetrics):
        """Item count is under the cap but byte budget is exceeded."""
        # _delta_frame("x"*10) = 103 bytes
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=10, buffer_bytes=200, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "x" * 10)
        # Two frames (206 bytes) exceed 200 byte budget even though
        # item count (2) < cap (10).
        assert sub.put(frame) is True    # 0+103=103 <= 200
        assert sub.put(frame) is False   # 103+103=206 > 200
        assert sub.closed


class TestRuntimeOversizedDrop:
    """max_frame_bytes drops without disconnecting."""

    def test_oversized_frame_dropped_not_closed(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        # Create a subscriber with tiny max_frame_bytes for this test.
        sub = TokenSubscriber(
            session_id="s1", metrics=tight_sub.metrics,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=50,
        )
        big = _delta_frame(("s1", "m1", "p1"), "x" * 1000)  # >> 50 bytes
        assert sub.put(big) is False
        assert sub.dropped_frames == 1
        assert sub.metrics.dropped_frames_total == 1
        assert not sub.closed  # oversized drop does NOT close

    def test_oversized_frame_not_counted_in_queued_bytes(self, metrics: _TokenMetrics):
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=50,
        )
        big = _delta_frame(("s1", "m1", "p1"), "x" * 1000)
        sub.put(big)
        assert sub.queued_bytes == 0

    def test_normal_frame_still_works_after_oversized_drop(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        sub = tight_sub
        # Drop one oversized.
        sub.max_frame_bytes = 50  # temporarily shrink the cap
        big = _delta_frame(("s1", "m1", "p1"), "x" * 1000)
        assert sub.put(big) is False
        # Restore and normal frame still enqueues.
        sub.max_frame_bytes = 1024
        assert sub.put(sample_frame) is True
        assert not sub.closed


# ===========================================================================
# CRITICAL 3 — handshake / runtime PHYSICAL queue separation
# ===========================================================================

class TestHandshakeRuntimeSeparation:
    """CRITICAL 3: handshake pre-fill is PHYSICALLY decoupled from the
    runtime T3 queue so runtime overflow can NEVER clear handshake state.

    Lane 3's previous single-queue + bypass design had a deterministic
    bug: after ``end_handshake`` the queue held ``N >> queue_items``
    frames, so the very next runtime ``put()`` overflowed and
    ``_clear_queue()`` wiped every handshake frame (server.connected +
    tombstones + snapshot) — the client then saw a ``resync`` without
    ever receiving ``server.connected``. These tests pin the fix.
    """

    def test_handshake_frames_land_in_dedicated_buffer(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        """Handshake puts do NOT touch the runtime queue (qsize stays 0)."""
        sub = tight_sub
        sub.begin_handshake()
        for _ in range(10):
            assert sub.put(sample_frame) is True
        assert not sub.closed
        assert sub.dropped_frames == 0
        # Runtime queue depth is 0 — handshake frames are in the buffer.
        assert sub.queue.qsize() == 0
        # Handshake buffer holds all 10.
        assert sub.queue.handshake_qsize() == 10

    def test_handshake_does_not_consume_runtime_item_budget(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        """After a handshake that exceeds queue_items, runtime T3 is FRESH:
        the first runtime puts succeed up to the normal queue_items cap
        before overflowing (Lane 3 bug: they would overflow immediately)."""
        sub = tight_sub  # queue_items=2
        sub.begin_handshake()
        for _ in range(10):  # >> queue_items
            assert sub.put(sample_frame) is True
        sub.end_handshake()
        # Runtime queue still has the full cap=2 available.
        assert sub.put(sample_frame) is True  # runtime 1
        assert sub.put(sample_frame) is True  # runtime 2
        assert not sub.closed
        # 3rd runtime put overflows.
        assert sub.put(sample_frame) is False
        assert sub.closed

    def test_handshake_does_not_consume_runtime_byte_budget(
        self, metrics: _TokenMetrics,
    ):
        """Handshake bytes don't count against runtime buffer_bytes."""
        # _delta_frame("x"*30) ≈ 123 bytes; buffer cap 150.
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=150, max_frame_bytes=1024,
        )
        big = _delta_frame(("s1", "m1", "p1"), "x" * 30)  # 123 bytes
        sub.begin_handshake()
        # 3 handshake frames = ~369 bytes (>> buffer_bytes=150).
        for _ in range(3):
            assert sub.put(big) is True
        sub.end_handshake()
        # Runtime byte budget is FRESH; the first runtime put of a frame
        # that fits within buffer_bytes succeeds (Lane 3 bug: would have
        # overflowed because handshake bytes were counted against the
        # runtime budget, leaving < 123 bytes free).
        assert sub.put(big) is True   # 0+123 <= 150 OK
        assert not sub.closed
        # Second runtime put overflows: 123+123=246 > 150.
        assert sub.put(big) is False
        assert sub.closed

    def test_handshake_frames_survive_runtime_overflow(self, metrics: _TokenMetrics):
        """THE CRITICAL 3 regression: pre-fill many handshake frames, end
        handshake, overflow runtime. Handshake frames MUST remain on the
        queue (consumer drains them BEFORE the terminal resync+STOP)."""
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=2, buffer_bytes=4096, max_frame_bytes=1024,
        )
        connected = _connected_frame("s1")
        tombstone = _message_removed_frame("s1", "m1")
        snapshot = _snapshot_frame(("s1", "m1", "p1"), text="hello", done=False)
        # Handshake pre-fill: 3 frames (>> queue_items=2).
        sub.begin_handshake()
        assert sub.put(connected) is True
        assert sub.put(tombstone) is True
        assert sub.put(snapshot) is True
        sub.end_handshake()
        # Runtime queue is empty (handshake didn't consume runtime budget).
        assert sub.queue.qsize() == 0
        assert not sub.closed
        # Fill runtime to cap (2) then overflow on the 3rd runtime put.
        runtime_frame = _delta_frame(("s1", "m1", "p1"), "x")
        assert sub.put(runtime_frame) is True  # runtime 1
        assert sub.put(runtime_frame) is True  # runtime 2
        assert sub.put(runtime_frame) is False  # runtime overflow
        assert sub.closed
        # CRITICAL 3: drain order = handshake frames FIRST, then resync+STOP.
        drained = drain_queue(sub)
        # 3 handshake + resync + STOP = 5 frames (the 2 runtime frames
        # were cleared by overflow; only resync+STOP sealed in their place).
        assert len(drained) == 5, f"expected 5 frames, got {len(drained)}"
        # First 3 are the handshake frames in put order.
        assert drained[0] == connected
        assert drained[1] == tombstone
        assert drained[2] == snapshot
        # Then the runtime overflow resync + STOP.
        ev, data = parse_event(drained[3])
        assert ev == "resync"
        assert data == {"reason": "subscriber_backpressure", "sessionID": "s1"}
        assert drained[4] is STOP

    def test_real_overflow_scenario_handshake_preserved(self, metrics: _TokenMetrics):
        """CRITICAL 3 real-world scenario: handshake completed; many runtime
        deltas fill the queue; overflow disconnects BUT the handshake
        (server.connected + snapshot) is preserved on the wire order."""
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=4, buffer_bytes=4096, max_frame_bytes=1024,
        )
        # Handshake pre-fill (mimics attach_subscriber ordering).
        sub.begin_handshake()
        sub.put(_connected_frame("s1"))
        sub.put(_snapshot_frame(("s1", "m1", "p1"), text="seed", done=False))
        sub.end_handshake()
        # Runtime deltas fill the queue to cap=4.
        d = _delta_frame(("s1", "m1", "p1"), "x")
        for _ in range(4):
            assert sub.put(d) is True
        # 5th runtime put overflows.
        assert sub.put(d) is False
        assert sub.closed
        assert sub.forced_disconnects == 1
        # Drain — handshake first, then runtime resync + STOP.
        drained = drain_queue(sub)
        # 2 handshake + resync + STOP = 4 frames (4 runtime deltas cleared).
        assert len(drained) == 4
        # Handshake survived.
        assert parse_event(drained[0])[0] == "server.connected"
        assert parse_event(drained[1])[0] == "message.part.snapshot"
        # Then runtime resync + STOP.
        ev, data = parse_event(drained[2])
        assert ev == "resync"
        assert data == {"reason": "subscriber_backpressure", "sessionID": "s1"}
        assert drained[3] is STOP

    def test_consumer_drains_handshake_before_runtime(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        """Order invariant: get() returns handshake frames first, then runtime."""
        sub = tight_sub
        sub.begin_handshake()
        handshake_frame = _connected_frame("s1")
        sub.put(handshake_frame)
        sub.end_handshake()
        # Now put a runtime frame.
        sub.put(sample_frame)
        # Drain: handshake first, then runtime.
        first = sub.queue.get_nowait()
        second = sub.queue.get_nowait()
        assert first == handshake_frame
        assert second == sample_frame

    def test_handshake_buffer_items_cap_fails_loud(self, metrics: _TokenMetrics):
        """CRITICAL 2: handshake buffer item cap FAILS LOUD (not drop-oldest).

        Pre-fix bug: drop-oldest evicted server.connected (the FIRST frame)
        when tombstones exceeded the cap, leaving the client in an
        unrecoverable state. Now overflow → closed + dropped_frames bump,
        and the existing frames (including server.connected) are preserved.
        """
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=10 ** 9, max_frame_bytes=10 ** 9,
            handshake_items=3, handshake_buffer_bytes=10 ** 9,
        )
        f = _delta_frame(("s1", "m1", "p1"), "x")
        sub.begin_handshake()
        # Fill exactly to cap (3 items) — all land, no eviction.
        sub.put(f)
        sub.put(f)
        sub.put(f)
        assert sub.queue.handshake_qsize() == 3
        assert sub.dropped_frames == 0
        assert not sub.closed
        before = sub.metrics.dropped_frames_total
        # 4th put: cap exceeded → FAIL LOUD (closed, NOT drop-oldest).
        assert sub.put(f) is False
        assert sub.closed  # CRITICAL 2: sub closed (attach will bail)
        assert sub.dropped_frames == 1
        assert sub.metrics.dropped_frames_total == before + 1
        # Buffer STILL has exactly 3 frames (no eviction — fail not drop).
        assert sub.queue.handshake_qsize() == 3
        # Subsequent puts are silent drops (closed check first).
        assert sub.put(f) is False
        assert sub.queue.handshake_qsize() == 3  # unchanged

    def test_handshake_buffer_byte_cap_fails_loud(self, metrics: _TokenMetrics):
        """CRITICAL 2: handshake buffer byte cap FAILS LOUD (not drop-oldest)."""
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=10 ** 9, max_frame_bytes=10 ** 9,
            handshake_items=10 ** 9, handshake_buffer_bytes=200,
        )
        f = _delta_frame(("s1", "m1", "p1"), "x" * 50)  # ~143 bytes
        sub.begin_handshake()
        sub.put(f)  # 143 bytes — lands
        assert sub.dropped_frames == 0
        assert not sub.closed
        before = sub.metrics.dropped_frames_total
        # 2nd frame: 143 + 143 = 286 > 200 → FAIL LOUD (closed).
        assert sub.put(f) is False
        assert sub.closed
        assert sub.metrics.dropped_frames_total == before + 1
        assert sub.dropped_frames == 1
        # Buffer STILL has exactly 1 frame (the first; no eviction).
        assert sub.queue.handshake_qsize() == 1

    def test_handshake_default_cap_accommodates_max_tombstone_replay(
        self, metrics: _TokenMetrics,
    ):
        """CRITICAL 2 regression: the default handshake cap
        (TOKEN_HANDSHAKE_ITEMS=2048) comfortably accommodates the full
        §5.5 pre-fill ceiling — 1 server.connected +
        TOKEN_REMOVED_MESSAGES_MAX (1000) tombstones + snapshots — so a
        tombstone-heavy sid never trips the cap."""
        from oc_slimapi.config import (
            TOKEN_HANDSHAKE_ITEMS,
            TOKEN_REMOVED_MESSAGES_MAX,
        )
        # Sanity: default cap is large enough for the worst-case pre-fill.
        assert TOKEN_HANDSHAKE_ITEMS > TOKEN_REMOVED_MESSAGES_MAX + 1
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            # Use the code-level default handshake cap (do NOT override).
            queue_items=64, buffer_bytes=10 ** 9, max_frame_bytes=10 ** 9,
        )
        connected = _connected_frame("s1")
        sub.begin_handshake()
        # 1. server.connected always lands first.
        assert sub.put(connected) is True
        # 2. Replay the FULL tombstone replay ceiling (1000 tombstones).
        for i in range(TOKEN_REMOVED_MESSAGES_MAX):
            tomb = _message_removed_frame("s1", f"m{i}")
            assert sub.put(tomb) is True, f"tombstone {i} did not land"
        # 3. A handful of snapshots still land.
        for i in range(5):
            snap = _snapshot_frame(("s1", f"m{i}", "p1"), text="x", done=False)
            assert sub.put(snap) is True
        assert not sub.closed  # CRITICAL 2: did not trip the cap
        assert sub.dropped_frames == 0
        # All frames accounted for: 1 + 1000 + 5 = 1006.
        assert sub.queue.handshake_qsize() == 1 + TOKEN_REMOVED_MESSAGES_MAX + 5

    def test_handshake_overflow_preserves_server_connected(
        self, metrics: _TokenMetrics,
    ):
        """CRITICAL 2: even when the handshake buffer overflows,
        server.connected (the FIRST frame) is preserved — fail-on-overflow
        does NOT evict existing frames (unlike the old drop-oldest)."""
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=10 ** 9, max_frame_bytes=10 ** 9,
            handshake_items=2, handshake_buffer_bytes=10 ** 9,
        )
        connected = _connected_frame("s1")
        tomb = _message_removed_frame("s1", "m1")
        extra = _message_removed_frame("s1", "m2")
        sub.begin_handshake()
        # server.connected lands (buffer empty → always fits).
        assert sub.put(connected) is True
        # 2nd frame fills cap=2.
        assert sub.put(tomb) is True
        assert sub.queue.handshake_qsize() == 2
        # 3rd frame: cap exceeded → FAIL LOUD.
        assert sub.put(extra) is False
        assert sub.closed
        # CRITICAL 2: server.connected is STILL the first frame in the
        # buffer (fail-on-overflow preserved it; drop-oldest would have
        # evicted it).
        first = sub.queue.get_nowait()
        assert first == connected
        # Second frame also preserved.
        second = sub.queue.get_nowait()
        assert second == tomb

    def test_handshake_overflow_makes_attach_fail(
        self, metrics: _TokenMetrics,
    ):
        """CRITICAL 2 + MAJOR 4: handshake buffer overflow closes the sub
        so attach_subscriber's membership guard bails (no fanout entry)
        and subscribe() raises a 503. Demonstrated at the subscriber level:
        a closed sub during handshake causes the handshake put to return
        False and all subsequent puts to be no-ops."""
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=64, buffer_bytes=10 ** 9, max_frame_bytes=10 ** 9,
            handshake_items=1, handshake_buffer_bytes=10 ** 9,
        )
        sub.begin_handshake()
        assert sub.put(_connected_frame("s1")) is True  # cap=1, fills
        # 2nd put: overflow → closed.
        assert sub.put(_message_removed_frame("s1", "m1")) is False
        assert sub.closed
        # Subsequent puts during handshake are silent drops (closed check).
        assert sub.put(_message_removed_frame("s1", "m2")) is False
        # Only server.connected is in the buffer.
        assert sub.queue.handshake_qsize() == 1

    def test_end_handshake_then_runtime_byte_overflow(
        self, metrics: _TokenMetrics,
    ):
        """After end_handshake, runtime byte-budget overflow still fires
        on accumulated RUNTIME bytes (handshake bytes don't count)."""
        # _delta_frame("x"*5) = 98 bytes
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=100, buffer_bytes=150, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "x" * 5)  # 98 bytes
        sub.begin_handshake()
        sub.put(frame)  # handshake_bytes = 98 (does NOT count for runtime)
        sub.put(frame)  # handshake_bytes = 196 (still does not count)
        sub.end_handshake()
        # Runtime byte budget fresh (handshake didn't consume it).
        # Fill runtime byte budget: 0+98=98 OK, 98+98=196 > 150 → overflow.
        assert sub.put(frame) is True   # runtime 1 (98 bytes)
        assert sub.put(frame) is False  # 98+98=196 > 150 → overflow
        assert sub.closed

    def test_handshake_closed_sub_still_rejects(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        """Even in handshake mode, a closed sub drops frames."""
        sub = tight_sub
        # Overflow it first (runtime path).
        sub.put(sample_frame)
        sub.put(sample_frame)
        sub.put(sample_frame)
        assert sub.closed
        # Now in handshake mode → still rejected (closed check first).
        sub.begin_handshake()
        assert sub.put(sample_frame) is False

    def test_handshake_oversized_still_drops(
        self, tight_sub: TokenSubscriber,
    ):
        """Even in handshake mode, oversized frames are dropped (not closed)."""
        sub = tight_sub
        sub.begin_handshake()
        sub.max_frame_bytes = 50  # temporarily shrink
        big = _delta_frame(("s1", "m1", "p1"), "x" * 1000)  # >> 50 bytes
        assert sub.put(big) is False  # oversized drop
        assert sub.dropped_frames == 1
        assert sub.metrics.dropped_frames_total == 1
        assert not sub.closed
        # Restore and normal frame during handshake still works.
        sub.max_frame_bytes = 1024
        small = _delta_frame(("s1", "m1", "p1"), "a")
        assert sub.put(small) is True

    def test_handshake_preserves_queued_bytes(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        """Handshake puts accumulate handshake_bytes (visible via queued_bytes)."""
        sub = tight_sub
        sub.begin_handshake()
        for _ in range(5):
            sub.put(sample_frame)
        expected = len(sample_frame) * 5
        assert sub.queued_bytes == expected
        # Internal split: all bytes in handshake buffer, none in runtime.
        assert sub.queue.handshake_bytes == expected
        assert sub.queue.runtime_bytes == 0


class TestHandshakeStandalone:
    """begin_handshake/end_handshake lifecycle without overflow."""

    def test_begin_then_end_clears_flag(self, tight_sub: TokenSubscriber):
        sub = tight_sub
        assert sub._in_handshake is False
        sub.begin_handshake()
        assert sub._in_handshake is True
        sub.end_handshake()
        assert sub._in_handshake is False

    def test_double_begin_is_idempotent(self, tight_sub: TokenSubscriber):
        sub = tight_sub
        sub.begin_handshake()
        sub.begin_handshake()  # second call, still True
        assert sub._in_handshake is True

    def test_double_end_is_idempotent(self, tight_sub: TokenSubscriber):
        sub = tight_sub
        sub.begin_handshake()
        sub.end_handshake()
        sub.end_handshake()  # second call, still False
        assert sub._in_handshake is False


# ===========================================================================
# ack() accounting — routes via last_get_handshake flag
# ===========================================================================

class TestAckAccounting:
    """ack() decrements the correct ledger (handshake vs runtime) based on
    which buffer the last get() pulled from."""

    def test_ack_runtime(self, tight_sub: TokenSubscriber, sample_frame: bytes):
        sub = tight_sub
        sub.put(sample_frame)
        before = sub.queued_bytes
        # Production pattern: get() first, then ack().
        drained = sub.queue.get_nowait()
        assert sub.queue.last_get_handshake is False
        sub.ack(drained)
        assert sub.queued_bytes == before - len(sample_frame)

    def test_ack_handshake_frame(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        """ack of a handshake frame (drained via get_nowait) decrements
        handshake_bytes — queued_bytes drops accordingly."""
        sub = tight_sub
        sub.begin_handshake()
        for _ in range(3):
            sub.put(sample_frame)
        sub.end_handshake()
        total = sub.queued_bytes
        # Drain one handshake frame via the queue (sets last_get_handshake).
        drained = sub.queue.get_nowait()
        assert sub.queue.last_get_handshake is True
        assert drained == sample_frame
        sub.ack(drained)
        assert sub.queued_bytes == total - len(sample_frame)

    def test_ack_stop_is_noop(self, tight_sub: TokenSubscriber):
        """ack(STOP) must not decrement (STOP is never counted in put)."""
        sub = tight_sub
        sub.ack(STOP)
        assert sub.queued_bytes == 0

    def test_ack_clamped_at_zero(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        sub = tight_sub
        # ack more bytes than queued → floored at 0.
        sub.ack(sample_frame)
        assert sub.queued_bytes == 0

    def test_ack_after_overflow_reset(
        self, tight_sub: TokenSubscriber, sample_frame: bytes,
    ):
        sub = tight_sub
        sub.put(sample_frame)
        sub.put(sample_frame)
        assert sub.queued_bytes > 0
        sub.put(sample_frame)  # overflow → clear_runtime → runtime_bytes = 0
        assert sub.queued_bytes == 0
        sub.ack(sample_frame)
        assert sub.queued_bytes == 0  # still 0 (clamped)


# ===========================================================================
# MAJOR 4 + MAJOR 5 — attach failure: no ledger increment + full rollback
# ===========================================================================

class _ClosingTokenHub:
    """Minimal TokenStreamHub stub: attach_subscriber closes the sub
    (simulates a defensive early-exit / oversized guard / future Lane-A
    change that aborts the handshake). Tracks start/stop/detach calls so
    MAJOR 5 rollback tests can assert the cleanup ran."""

    def __init__(self, metrics: _TokenMetrics) -> None:
        self._metrics = metrics
        self._subs_by_sid: dict[str, set] = {}
        self._pending: dict = {}
        self.start_calls = 0
        self.stop_calls = 0
        self.detach_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def attach_subscriber(self, sid: str, sub, wire_v4: bool = False) -> None:
        # MAJOR 4 trigger: simulate the sub being closed during / after
        # the handshake pre-fill (defensive guard). attach_subscriber in
        # hub.py checks sub.closed and bails without entering fanout.
        sub.closed = True

    def has_subscriber(self, sid: str, sub) -> bool:
        return False  # never enters fanout (closed before add)

    def detach_subscriber(self, sid: str, sub) -> None:
        self.detach_calls += 1


class TestRegistryAttachFailure:
    """MAJOR 4 + MAJOR 5: TokenStreamRegistry.subscribe() must NOT increment
    total_subscribers when attach_subscriber leaves sub.closed=True (MAJOR 4),
    AND must roll back the side effects from the subscribe preamble (flush
    loop start, GlobalHub grace cancel, upstream ensure) so no ghost
    subscriber / leaked flush loop / orphaned upstream remains (MAJOR 5).

    Pre-MAJOR-4 bug: attach_subscriber already had the membership guard
    (``if sub.closed: return`` before adding to fanout), but subscribe()
    ALWAYS incremented the ledger afterwards. The closed sub never entered
    fanout, so unsubscribe() was a no-op → the slot leaked forever.

    Pre-MAJOR-5 bug: even with the ledger fixed, the flush loop kept
    running (started unconditionally), GlobalHub.run() never re-armed
    grace → upstream /global/event connection + hub tasks leaked.
    """

    def test_attach_failure_does_not_increment_ledger(self, metrics: _TokenMetrics):
        hub = _ClosingTokenHub(metrics)
        reg = TokenStreamRegistry(
            hub,
            hub_registry=None,
            max_subscribers=5,
            queue_items=64,
            buffer_bytes=4096,
            max_frame_bytes=1024,
        )
        with pytest.raises(TokenSubscriberCapacityError):
            reg.subscribe("s1")
        # MAJOR 4: ledger NOT incremented.
        assert reg.total_subscribers == 0
        # The rejection IS counted (mirrors the cap-overflow path).
        assert reg.rejected_total == 1

    def test_attach_failure_rolls_back_flush_loop(self, metrics: _TokenMetrics):
        """MAJOR 5: subscribe() starts the flush loop before attach; on
        attach failure with no other subs, it MUST stop the loop (no
        ghost flush task running for nobody)."""
        hub = _ClosingTokenHub(metrics)
        reg = TokenStreamRegistry(
            hub, hub_registry=None,
            max_subscribers=5,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=1024,
        )
        with pytest.raises(TokenSubscriberCapacityError):
            reg.subscribe("s1")
        # start() was called (subscribe preamble); stop() was called
        # (MAJOR 5 rollback — no other subs, so last-detach stop fires).
        assert hub.start_calls == 1
        assert hub.stop_calls == 1, "MAJOR 5: flush loop must stop on attach failure"

    def test_attach_failure_does_not_stop_flush_loop_if_other_subs_remain(
        self, metrics: _TokenMetrics,
    ):
        """MAJOR 5: if another subscriber is already attached, the flush
        loop must NOT be stopped on a sibling's attach failure."""
        hub = _ClosingTokenHub(metrics)
        reg = TokenStreamRegistry(
            hub, hub_registry=None,
            max_subscribers=5,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=1024,
        )
        # Simulate an already-attached sub (total_subscribers > 0):
        # bump the ledger directly to mimic a prior successful attach.
        reg.total_subscribers = 1
        with pytest.raises(TokenSubscriberCapacityError):
            reg.subscribe("s2")
        # start() called by the failed subscribe; stop() NOT called
        # because the other sub keeps the flush loop alive.
        assert hub.start_calls == 1
        assert hub.stop_calls == 0, (
            "MAJOR 5: flush loop must NOT stop when other subs remain"
        )

    def test_attach_failure_then_succeed_still_admits(
        self, metrics: _TokenMetrics,
    ):
        """After an attach failure, a subsequent subscribe() can still
        admit a fresh sub (no slot leaked from the prior failure)."""

        class _FirstCloseThenSucceed:
            def __init__(self, metrics: _TokenMetrics) -> None:
                self._metrics = metrics
                self._subs_by_sid: dict[str, set] = {}
                self._pending: dict = {}
                self._calls = 0

            def start(self) -> None: pass
            def stop(self) -> None: pass

            def attach_subscriber(self, sid: str, sub, wire_v4: bool = False) -> None:
                self._calls += 1
                if self._calls == 1:
                    sub.closed = True  # first attach fails

            def has_subscriber(self, sid: str, sub) -> bool:
                return sub in self._subs_by_sid.get(sid, set())

            def detach_subscriber(self, sid: str, sub) -> None:
                s = self._subs_by_sid.get(sid)
                if s is not None:
                    s.discard(sub)
                    if not s:
                        self._subs_by_sid.pop(sid, None)

        hub = _FirstCloseThenSucceed(metrics)
        reg = TokenStreamRegistry(
            hub, hub_registry=None,
            max_subscribers=2,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=1024,
        )
        # First subscribe fails (attach closes the sub).
        with pytest.raises(TokenSubscriberCapacityError):
            reg.subscribe("s1")
        assert reg.total_subscribers == 0  # MAJOR 4: no leak
        # Second subscribe succeeds (no leaked slot from the first call).
        sub2 = reg.subscribe("s2")
        assert reg.total_subscribers == 1
        assert not sub2.closed
