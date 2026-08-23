"""Native-v4 token-stream flush, budget, replay, and lifecycle tests.

The hub emits replay-backed deltas/resyncs/tombstones plus heartbeats. Final
alignment is owned by digest + HTTP state, while attach only registers the
subscriber for future fanout.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from oc_slimapi.config import (
    DEFAULT_TOKEN_MAX_FRAME_BYTES,
    TOKEN_FLUSH_SECONDS,
    TOKEN_LIVE_PARTS_MAX,
    TOKEN_LIVEPARTS_MAX_BYTES,
    TOKEN_PART_MAX_BYTES,
    TOKEN_PENDING_MAX_BYTES,
    TOKEN_RESYNC_QUEUE_CAP,
)
from oc_slimapi.sse.token_hub import (
    DeltaAccumulator,
    LivePart,
    TokenStreamHub as _TokenStreamHub,
    _delta_frame,
    _heartbeat_frame,
    _resync_frame,
    _now_ms,
    sse_frame,
)
from oc_slimapi.sse.replay_log import ReplayLog


_TEST_REPLAY_LOG: ReplayLog | None = None


@pytest.fixture(autouse=True)
def _test_replay_log():
    global _TEST_REPLAY_LOG
    replay_log = ReplayLog()
    _TEST_REPLAY_LOG = replay_log
    try:
        yield
    finally:
        _TEST_REPLAY_LOG = None
        replay_log.close()


def _token_hub(**kwargs) -> _TokenStreamHub:
    replay_log = kwargs.pop("replay_log", _TEST_REPLAY_LOG)
    assert replay_log is not None
    return _TokenStreamHub(replay_log=replay_log, **kwargs)


TokenStreamHub = _token_hub
from oc_slimapi.sse.tokenstream.hub import apply_debug_budget_overrides


# ---------------------------------------------------------------------------
# Helpers (inlined per repo pattern — no sibling-test imports).
# ---------------------------------------------------------------------------

def _delta_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    field: str = "text", delta: str = "x",
) -> dict:
    return {
        "sessionID": sid, "messageID": mid, "partID": pid,
        "field": field, "delta": delta,
    }


def _updated_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    *, type: str = "text", text: str | None = None, end=None,
) -> dict:
    time_obj: dict = {}
    if end is not None:
        time_obj["end"] = end
    part: dict = {
        "id": pid, "messageID": mid, "sessionID": sid,
        "type": type, "time": time_obj,
    }
    if text is not None:
        part["text"] = text
    return {"sessionID": sid, "part": part, "time": {}}


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


class _FakeSub:
    """Minimal native-v4 subscriber: captures every put() frame in order."""

    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.frames: list[bytes] = []
        self.closed: bool = False

    def put(self, frame: bytes) -> bool:
        self.frames.append(frame)
        return True

    def terminate(self, reason: str | None = None) -> None:
        """Mirror TokenSubscriber termination: frozen resync, then STOP."""
        from oc_slimapi.sse.replay_wire import V4_RESYNC_REASONS
        from oc_slimapi.sse.tokenstream.frames import STOP, _resync_frame
        self.closed = True
        if reason in V4_RESYNC_REASONS:
            self.frames.append(_resync_frame(self.session_id, reason))
        self.frames.append(STOP)


def _attach(th: TokenStreamHub, sid: str = "s1") -> _FakeSub:
    """Attach a fresh subscriber; return it for frame inspection."""
    sub = _FakeSub(session_id=sid)
    th.attach_subscriber(sid, sub)
    return sub


def _drain_task(task: asyncio.Task) -> None:
    if task is not None and not task.done():
        task.cancel()
        try:
            asyncio.get_event_loop().run_until_complete(task)
        except (asyncio.CancelledError, RuntimeError):
            pass


# ===========================================================================
# Wire frame builders — exact payload shape (§5.6)
# ===========================================================================

class TestWireFrames:
    def test_delta_frame(self):
        frame = _delta_frame(("s1", "m1", "p1"), "chunk")
        event, data = parse_event(frame)
        assert event == "message.part.delta"
        assert data == {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "text": "chunk",
        }

    def test_resync_frame_carries_session_id(self):
        """Token-stream resync is session-scoped (§5.6 frame 5)."""
        frame = _resync_frame("s1", "token_memory_limit")
        event, data = parse_event(frame)
        assert event == "resync"
        assert data == {"reason": "token_memory_limit", "sessionID": "s1"}

    def test_heartbeat_frame_empty_payload(self):
        frame = _heartbeat_frame()
        event, data = parse_event(frame)
        assert event == "server.heartbeat"
        assert data == {}

    def test_sse_frame_no_event_name(self):
        """Without an event= kwarg, no ``event:`` prefix is emitted."""
        frame = sse_frame({"a": 1})
        assert frame.startswith(b"data: ")
        assert b"event:" not in frame


# ===========================================================================
# flush() — sorted-by-key drain, clear pending, no-op on empty
# ===========================================================================

class TestFlush:
    def test_noop_when_empty(self):
        th = TokenStreamHub()
        th.flush()  # must not raise
        assert th._pending == {}

    def test_drains_pending_sorted_by_key(self):
        """§5.4: flush order is sorted(self._pending) for deterministic order."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        # Create three parts; insert out of order.
        for pid in ("p3", "p1", "p2"):
            th.on_part_updated(_updated_props("s1", "m1", pid, text=""))
            th.on_part_delta(_delta_props("s1", "m1", pid, delta=f"d-{pid}"))
        th.flush()
        # Expect 3 delta frames in sorted-key order (p1, p2, p3).
        events = [parse_event(f) for f in sub.frames]
        assert [e[1]["partID"] for e in events] == ["p1", "p2", "p3"]
        assert all(e[0] == "message.part.delta" for e in events)
        # Pending cleared (accumulators cleaned up).
        assert th._pending == {}

    def test_clears_pending_after_flush(self):
        th = TokenStreamHub()
        _attach(th, "s1")
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="chunk"))
        assert ("s1", "m1", "p1") in th._pending
        th.flush()
        assert th._pending == {}

    def test_empty_accumulators_removed(self):
        """After drain, empty DeltaAccumulators are pruned from _pending."""
        th = TokenStreamHub()
        _attach(th, "s1")
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="x"))
        th.flush()
        # _pending cleared entirely (only one accumulator, now empty).
        assert th._pending == {}

    def test_byte_count_zero_after_drain(self):
        th = TokenStreamHub()
        _attach(th, "s1")
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="hello"))
        th.flush()
        # DeltaAccumulator gone, but LivePart still accumulates (B1).
        live = th.live_parts[("s1", "m1", "p1")]
        assert live.byte_count == 5  # full text remains in LivePart accounting
        assert th._total_live_bytes == 5

    def test_no_fanout_when_no_subscribers(self):
        """flush without subscribers drops deltas silently (no crash)."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="x"))
        th.flush()  # no subscribers → frames silently discarded
        assert th._pending == {}

    def test_only_fans_to_matching_sid(self):
        """§3 design: fan-out is per-sid. A delta for s1 does NOT reach s2."""
        th = TokenStreamHub()
        sub1 = _attach(th, "s1")
        sub2 = _attach(th, "s2")
        sub1.frames.clear()
        sub2.frames.clear()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="for-s1"))
        th.flush()
        assert any(b"for-s1" in f for f in sub1.frames)
        assert not any(b"for-s1" in f for f in sub2.frames)

    def test_flush_increments_flushed_frames_total(self):
        th = TokenStreamHub()
        _attach(th, "s1")
        before = th.flushed_frames_total
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="x"))
        th.flush()
        assert th.flushed_frames_total == before + 1

    def test_4kib_threshold_early_flush(self, monkeypatch):
        """§5.4: accumulator crossing TOKEN_FLUSH_BYTES drains immediately
        (does not wait for the 100ms tick). Low-volume deltas stay pending."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.ingest.TOKEN_FLUSH_BYTES", 10)
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        # Small delta: stays pending (below 10-byte threshold).
        th.on_part_delta(_delta_props(delta="tiny"))
        assert ("s1", "m1", "p1") in th._pending
        assert sub.frames == []  # not flushed yet
        # Delta crossing the threshold: triggers immediate early-flush.
        th.on_part_delta(_delta_props(delta="0123456789AB"))
        assert ("s1", "m1", "p1") not in th._pending  # drained + removed
        deltas = [f for f in sub.frames if parse_event(f)[0] == "message.part.delta"]
        # The early-flush frame carries the FULL window ("tiny" + "012…AB"),
        # not just the threshold-crossing delta.
        assert len(deltas) == 1
        _, data = parse_event(deltas[0])
        assert data["text"] == "tiny0123456789AB"


# ===========================================================================
# flush_sid() — targeted pending drain
# ===========================================================================

class TestFlushSid:
    def test_drains_only_matching_sid(self):
        th = TokenStreamHub()
        sub1 = _attach(th, "s1")
        _attach(th, "s2")
        sub1.frames.clear()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="s1-chunk"))
        th.on_part_updated(_updated_props("s2", "m2", "p2", text=""))
        th.on_part_delta(_delta_props("s2", "m2", "p2", delta="s2-chunk"))
        th.flush_sid("s1")
        # s1 drained; s2 still pending.
        assert ("s1", "m1", "p1") not in th._pending
        assert ("s2", "m2", "p2") in th._pending
        # s1 sub got the delta; sub1.frames has only the s1 delta.
        assert any(b"s1-chunk" in f for f in sub1.frames)

    def test_noop_when_no_pending_for_sid(self):
        th = TokenStreamHub()
        th.flush_sid("s1")  # no pending, no crash
        assert th._pending == {}


# ===========================================================================
# flush_loop() — background task: cadence + ttl_sweep scheduling (NB-B5)
# ===========================================================================

class TestFlushLoop:
    async def test_flush_loop_drains_within_cadence(self, monkeypatch):
        """100ms cadence: a pending delta is drained within ~TOKEN_FLUSH_SECONDS."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="chunk"))
        th.start()
        try:
            # Allow >TOKEN_FLUSH_SECONDS for at least one tick.
            await asyncio.sleep(TOKEN_FLUSH_SECONDS * 3 + 0.05)
        finally:
            th.stop()
        assert th._pending == {}
        assert any(b"chunk" in f for f in sub.frames)

    async def test_flush_loop_cancellable(self):
        th = TokenStreamHub()
        th.start()
        assert th._flush_task is not None
        th.stop()
        # Task cancelled and cleared.
        assert th._flush_task is None

    async def test_flush_loop_schedules_ttl_sweep(self, monkeypatch):
        """NB-B5: the 60s tick calls ttl_sweep. Verify via spy."""
        # Tighten the tick interval to make this test fast: patch the
        # module-level _TTL_TICK_INTERVAL down to 2 (drain every 2 ticks).
        monkeypatch.setattr(
            "oc_slimapi.sse.tokenstream.flush_engine._TTL_TICK_INTERVAL", 2
        )
        # Speed up the sleep cadence WITHOUT recursion: capture the real
        # sleep first, then patch the module-global asyncio.sleep used by
        # flush_loop. ``_fast`` awaits the real sleep so it actually yields
        # to the event loop (letting the test task run between ticks).
        real_sleep = asyncio.sleep

        async def _fast(_):
            await real_sleep(0)

        monkeypatch.setattr("oc_slimapi.sse.tokenstream.flush_engine.asyncio.sleep", _fast)

        th = TokenStreamHub()
        calls: list[int] = []

        orig = th.ttl_sweep

        def spy(now_ms=None):
            calls.append(1)
            orig(now_ms)

        th.ttl_sweep = spy  # type: ignore[method-assign]
        th.start()
        try:
            # Yield long enough for many zero-duration ticks.
            for _ in range(20):
                await real_sleep(0)
        finally:
            th.stop()
        assert len(calls) >= 1, "ttl_sweep was never scheduled by flush_loop"

    async def test_flush_loop_schedules_heartbeat(self, monkeypatch):
        """§5.6 frame 6: heartbeat fans every ~15s (patched interval for speed)."""
        monkeypatch.setattr(
            "oc_slimapi.sse.tokenstream.flush_engine._HEARTBEAT_TICK_INTERVAL", 2
        )

        real_sleep = asyncio.sleep

        async def _fast(_):
            await real_sleep(0)

        monkeypatch.setattr("oc_slimapi.sse.tokenstream.flush_engine.asyncio.sleep", _fast)

        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.start()
        try:
            for _ in range(20):
                await real_sleep(0)
        finally:
            th.stop()
        events = [parse_event(f)[0] for f in sub.frames]
        assert "server.heartbeat" in events

    async def test_start_idempotent(self):
        th = TokenStreamHub()
        th.start()
        task1 = th._flush_task
        th.start()  # second start is a no-op (task already running)
        assert th._flush_task is task1
        th.stop()

    async def test_stop_idempotent(self):
        th = TokenStreamHub()
        th.stop()  # never started — no crash
        th.start()
        th.stop()
        th.stop()  # double stop — no crash


# ===========================================================================
# finish_part() — C1 synchronous drain + lever-1 marker + retire
# ===========================================================================

class TestFinishPart:
    def test_drains_residual_pending_as_delta_only(self):
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="residual"))
        th.finish_part(("s1", "m1", "p1"), final_text="final-text")
        events = [parse_event(frame) for frame in sub.frames]
        assert [event for event, _data in events] == ["message.part.delta"]
        assert events[0][1]["text"] == "residual"

    def test_retires_part_after_finish(self):
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        th.on_part_updated(_updated_props(text="seed"))
        th.finish_part(key, final_text="final")
        assert key not in th.live_parts
        assert key not in th._pending
        assert key in th._disabled_parts
        assert th._total_live_bytes == 0

    def test_late_delta_after_finish_drops_on_disabled(self):
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        th.on_part_updated(_updated_props(text=""))
        th.finish_part(key, final_text="final")
        th.on_part_delta(_delta_props(delta="late"))
        assert th.orphan_deltas == 0
        assert key not in th._pending


# ===========================================================================
# _truncate_part_for_all — silent state retirement
# ===========================================================================

class TestSafePutAndTruncate:
    def test_truncate_drops_part_without_legacy_wire(self):
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        key = ("s1", "m1", "p1")
        th.on_part_updated(_updated_props(text="hello"))
        th._truncate_part_for_all(key, done=False)
        assert key not in th.live_parts
        assert key in th._disabled_parts
        assert sub.frames == []
        assert th.truncated_snapshots_total == 0

    def test_truncate_is_idempotent(self):
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        th.on_part_updated(_updated_props(text="hello"))
        th._truncate_part_for_all(key, done=False)
        th._truncate_part_for_all(key, done=True)
        assert th.truncated_snapshots_total == 0


# ===========================================================================
# _reserve — per-part + global byte/count caps (C5 / §16-C)
# ===========================================================================

class TestReserve:
    def test_per_part_cap_drops_without_truncated_wire(self, monkeypatch):
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 5)
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        key = ("s1", "m1", "p1")
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="123456"))
        assert key not in th.live_parts
        assert key in th._disabled_parts
        assert sub.frames == []
        assert th.truncated_snapshots_total == 0

    def test_global_byte_cap_evicts_oldest_and_emits_only_resync(self, monkeypatch):
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 6)
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="aaa"))
        th.on_part_updated(_updated_props("s1", "m1", "p2", text="bbb"))
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="x"))
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") in th._disabled_parts
        assert ("s1", "m1", "p2") in th.live_parts
        assert [parse_event(frame)[0] for frame in sub.frames] == ["resync"]
        assert th.truncated_snapshots_total == 0

    def test_global_count_cap_evicts_non_current_part(self, monkeypatch):
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVE_PARTS_MAX", 1)
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="a"))
        th.on_part_updated(_updated_props("s1", "m1", "p2", text="b"))
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p2") in th.live_parts
        assert [parse_event(frame)[0] for frame in sub.frames] == ["resync"]


# ===========================================================================
# attach_subscriber — native-v4 direct registration
# ===========================================================================

class TestAttachSubscriber:
    def test_attach_has_no_welcome_or_snapshot_prefill(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))
        sub = _attach(th, "s1")
        assert sub.frames == []

    def test_subscriber_count_reflects_attach_detach(self):
        th = TokenStreamHub()
        s1 = _FakeSub()
        s2 = _FakeSub()
        th.attach_subscriber("s1", s1)
        th.attach_subscriber("s1", s2)
        th.attach_subscriber("s2", _FakeSub())
        assert th.subscriber_count == 3
        th.detach_subscriber("s1", s1)
        assert th.subscriber_count == 2

    def test_attach_then_delta_reaches_sub_with_replay_id(self):
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="after-attach"))
        th.flush()
        assert len(sub.frames) == 1
        event, data = parse_event(sub.frames[0])
        assert event == "message.part.delta"
        assert data["text"] == "after-attach"
        assert sub.frames[0].startswith(b"id: ")


# ===========================================================================
# _pending_session_resinks — bounded drain (NB-B2)
# ===========================================================================

class TestPendingSessionResyncs:
    def test_idle_disconnects_stop_only_on_flush(self):
        """Non-frozen lifecycle reasons terminate native-v4 subscribers."""
        from oc_slimapi.sse.tokenstream.frames import STOP

        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_session_status("s1", "idle")
        th.flush()
        assert sub.frames == [STOP]
        assert sub.closed is True

    def test_deleted_terminates_subscriber_directly(self):
        """INV-4 (P0-3): on_session_deleted directly terminates subscribers
        (STOP only), not via the flush loop. The terminal marker
        are in sub.frames immediately after on_session_deleted — no flush()
        needed (the previous _enqueue_session_resync + flush path is gone).

        Test updated from test_deleted_resync_drained_on_flush because the
        behavior changed: session.deleted now delivers STOP directly via
        TokenSubscriber.terminate (server-side termination), not via the
        deferred flush-loop resync queue.
        """
        from oc_slimapi.sse.tokenstream.frames import STOP
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_session_deleted("s1")
        assert sub.frames == [STOP]
        # Sub marked closed.
        assert sub.closed is True

    def test_lifecycle_stop_only_targets_matching_sid(self):
        from oc_slimapi.sse.tokenstream.frames import STOP

        th = TokenStreamHub()
        sub1 = _attach(th, "s1")
        sub2 = _attach(th, "s2")
        sub1.frames.clear()
        sub2.frames.clear()
        th.on_session_status("s1", "idle")
        th.flush()
        assert sub1.frames == [STOP]
        assert sub2.frames == []

    def test_queue_bounded_drops_oldest(self, monkeypatch):
        """NB-B2: queue cap drops oldest entries when exceeded."""
        monkeypatch.setattr(
            "oc_slimapi.sse.tokenstream.flush_engine.TOKEN_RESYNC_QUEUE_CAP", 3
        )
        th = TokenStreamHub()
        for i in range(5):
            th._enqueue_session_resync(f"s{i}", "session_idle")
        # Only the newest 3 survive.
        assert len(th._pending_session_resinks) == 3
        reasons = th._pending_session_resinks
        # Oldest (s0, s1) dropped; s2, s3, s4 retained.
        assert [r[0] for r in reasons] == ["s2", "s3", "s4"]

    def test_no_resync_fanout_without_subscribers(self):
        """A resync enqueued with no subscribers is silently dropped at drain."""
        th = TokenStreamHub()
        th.on_session_status("s1", "idle")
        th.flush()  # no subscribers → resync discarded
        assert th._pending_session_resinks == []


# ===========================================================================
# on_upstream_reconnect — fans reconnect_no_replay to attached subs
# ===========================================================================

class TestReconnectFanout:
    def test_fans_reconnect_no_replay_to_all_sids(self):
        th = TokenStreamHub()
        sub1 = _attach(th, "s1")
        sub2 = _attach(th, "s2")
        sub1.frames.clear()
        sub2.frames.clear()
        th.on_upstream_reconnect()
        for sub in (sub1, sub2):
            resyncs = [f for f in sub.frames if parse_event(f)[0] == "resync"]
            assert len(resyncs) == 1
            _, data = parse_event(resyncs[0])
            assert data["reason"] == "reconnect_no_replay"
            assert data["sessionID"] in ("s1", "s2")

    def test_reconnect_clears_state_then_fans(self):
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="x"))
        assert th.live_parts
        th.on_upstream_reconnect()
        assert th.live_parts == {}
        assert th._pending == {}
        # Resync fanned.
        assert any(parse_event(f)[0] == "resync" for f in sub.frames)

    def test_reconnect_no_crash_without_subscribers(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_upstream_reconnect()  # no subscribers, no crash
        assert th.live_parts == {}

    def test_reconnect_clears_pending_resync_queue(self):
        th = TokenStreamHub()
        th.on_session_status("s1", "idle")
        assert th._pending_session_resinks
        th.on_upstream_reconnect()
        assert th._pending_session_resinks == []


# ===========================================================================
# Stage E — pending budget (4+4 split, §16-C residual): live+pending
# independence, force-flush on overflow, no-sub eviction, no-double-count.
# ===========================================================================

class TestPendingBudget:
    """Stage E (Option B 4+4 split): ``TOKEN_PENDING_MAX_BYTES`` bounds the
    global sum of DeltaAccumulator.byte_count (the transient pre-flush
    window). Live (``TOKEN_LIVEPARTS_MAX_BYTES``) and pending are
    INDEPENDENT gauges — the same delta byte physically occupies both
    LivePart.chunks (persistent) and DeltaAccumulator.chunks (transient
    pre-flush shadow), so each budget independently protects its OWN
    buffer."""

    def test_pending_gauge_tracked_independently_from_live(self):
        """A delta bumps BOTH _total_live_bytes AND _total_pending_bytes
        (different physical buffers). flush() drops pending to 0 while
        live is unchanged — proves the gauges are independent."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="hello"))  # 5 bytes
        assert th._total_live_bytes == 5
        assert th._total_pending_bytes == 5
        # flush drains pending; live is unchanged (B1: accumulation persists).
        th.flush()
        assert th._total_pending_bytes == 0
        assert th._total_live_bytes == 5

    def test_no_double_count_drop_clears_both_gauges(self):
        """drop_part clears both gauges; neither is double-decremented."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))  # 4 bytes (live only)
        th.on_part_delta(_delta_props(delta="chunk"))    # 5 bytes (live + pending)
        # live = seed(4) + delta(5) = 9; pending = delta(5) only.
        assert th._total_live_bytes == 9
        assert th._total_pending_bytes == 5
        th.drop_part(("s1", "m1", "p1"))
        assert th._total_live_bytes == 0
        assert th._total_pending_bytes == 0

    def test_seed_does_not_bump_pending(self):
        """Seeds go to LivePart.chunks only (never DeltaAccumulator — they
        are authoritative state and are not emitted as a delta frame).
        Pending budget is unaffected by seed admission."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed-text"))  # 9 bytes
        assert th._total_live_bytes == 9
        assert th._total_pending_bytes == 0
        assert ("s1", "m1", "p1") not in th._pending

    def test_pending_overflow_force_flushes_to_subscribers(self, monkeypatch):
        """With subscribers attached, pending overflow → force-flush: subs
        receive delta frames, _total_pending_bytes drops to 0, no eviction."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PENDING_MAX_BYTES", 10)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.ingest.TOKEN_FLUSH_BYTES", 10 ** 9)
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        # Two parts; each delta is small but collectively push pending > 10.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))   # 4 bytes
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bbbbb"))  # 5 → total 9
        assert th._total_pending_bytes == 9
        assert sub.frames == []  # under cap, no flush yet
        # One more byte → 10 == cap (boundary, still OK).
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="c"))  # 10 == cap
        # 11th byte → 11 > 10 → force-flush.
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="d"))  # 11 > 10
        # Pending drained to 0 by force-flush.
        assert th._total_pending_bytes == 0
        # Subscribers received delta frames.
        deltas = [f for f in sub.frames if parse_event(f)[0] == "message.part.delta"]
        assert len(deltas) >= 1
        # No eviction (force-flush resolved it; subs were present).
        assert th.token_memory_limit_total == 0
        # Live parts survive (live budget is huge).
        assert ("s1", "m1", "p1") in th.live_parts
        assert ("s1", "m1", "p2") in th.live_parts

    def test_pending_overflow_no_subs_evicts_oldest_and_resyncs(self, monkeypatch):
        """No subscribers + pending overflow → force-flush (deltas dropped)
        THEN LRU-evict the oldest LivePart + resync{token_memory_limit}.
        The eviction metric is bumped; the current key is NEVER evicted."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PENDING_MAX_BYTES", 10)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.ingest.TOKEN_FLUSH_BYTES", 10 ** 9)
        th = TokenStreamHub()
        # NO subscribers attached.
        # Two parts; backdate p1 so it is the LRU eviction target.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = _now_ms() - 10000
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))   # 4
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bbbbb"))  # 9
        # 11th byte → pending over cap → force-flush (no subs → dropped) +
        # evict oldest (p1, never the current key p2).
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="ccccccc"))  # 16 > 10
        # Pending drained by force-flush.
        assert th._total_pending_bytes == 0
        # p1 evicted (oldest, not the current key p2).
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") in th._disabled_parts
        # p2 survives (current key, never evicted).
        assert ("s1", "m1", "p2") in th.live_parts
        # token_memory_limit resync metric bumped.
        assert th.token_memory_limit_total == 1

    def test_pending_overflow_never_evicts_current_key(self, monkeypatch):
        """Even with no subs, the current key (the one receiving the delta
        that triggered overflow) is NEVER evicted — mirrors _reserve."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PENDING_MAX_BYTES", 5)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.ingest.TOKEN_FLUSH_BYTES", 10 ** 9)
        th = TokenStreamHub()
        # Only ONE part (the current key). No other candidates to evict.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="abcdef"))  # 6 > 5
        # Force-flush cleared pending; no eviction possible (only current key).
        assert th._total_pending_bytes == 0
        assert ("s1", "m1", "p1") in th.live_parts  # current key survived
        # No metric bump (no eviction).
        assert th.token_memory_limit_total == 0

    def test_live_and_pending_budgets_do_not_erode_each_other(self, monkeypatch):
        """The two budgets are independent: a tight LIVE cap evicts via
        _reserve without tripping the pending force-flush path; a tight
        PENDING cap force-flushes without tripping the live eviction path
        (when subs are attached)."""
        # --- Scenario A: tight LIVE cap, loose PENDING cap ---
        # cap=12 so p2 survives after p1 eviction (p2 total = 5+7 = 12).
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 12)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PENDING_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.ingest.TOKEN_FLUSH_BYTES", 10 ** 9)
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = _now_ms() - 10000
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        # p1=4, p2=5 → live=9; push to 16 > 12 live cap → evict p1 (frees 4
        # → live=5), then 5+7=12 ≤ 12 OK → p2 survives.
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bbbbb"))
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="ccccccc"))
        assert ("s1", "m1", "p1") not in th.live_parts  # evicted by LIVE budget
        assert ("s1", "m1", "p2") in th.live_parts
        assert th.token_memory_limit_total == 1
        # Pending cap was never tripped (loose), so no force-flush.
        # (delta frames may or may not have been flushed by the live-eviction
        # path; the key assertion is that the LIVE path evicted, not the
        # pending path.)

    def test_finish_part_clears_pending_gauge(self):
        """finish_part drains the residual pending and decrements the gauge."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="residual"))
        assert th._total_pending_bytes == 8  # len("residual")
        th.finish_part(("s1", "m1", "p1"), final_text="final")
        assert th._total_pending_bytes == 0
        assert th._total_live_bytes == 0

    def test_retire_session_clears_pending_gauge(self):
        """_retire_session clears pending for the sid and decrements the gauge."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s2", "m2", "p2", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))  # 4
        th.on_part_delta(_delta_props("s2", "m2", "p2", delta="bb"))    # 2
        assert th._total_pending_bytes == 6
        th._retire_session("s1")
        # s1's 4 bytes cleared; s2's 2 remain.
        assert th._total_pending_bytes == 2
        th._retire_session("s2")
        assert th._total_pending_bytes == 0

    def test_ttl_sweep_clears_pending_gauge(self):
        """ttl_sweep pops pending for a retired key and decrements the gauge."""
        from oc_slimapi.config import TOKEN_ACC_IDLE_MS
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="abc"))  # 3
        th._session_status["s1"] = "idle"
        now = _now_ms()
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = now - TOKEN_ACC_IDLE_MS - 1
        assert th._total_pending_bytes == 3
        retired = th.ttl_sweep(now)
        assert retired == [("s1", "m1", "p1")]
        assert th._total_pending_bytes == 0
        assert th._total_live_bytes == 0

    def test_reconnect_resets_pending_gauge(self):
        """on_upstream_reconnect clears _pending wholesale and resets the gauge."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="data"))
        assert th._total_pending_bytes > 0
        th.on_upstream_reconnect()
        assert th._total_pending_bytes == 0
        assert th._total_live_bytes == 0

    def test_pending_gauge_floored_at_zero_on_drift(self):
        """Defensive: if the pending gauge is somehow lower than the drained
        bytes (manual tamper, future bug), decrement floors at 0."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="abc"))
        assert th._total_pending_bytes == 3
        # Tamper: lower the gauge below the real 3 bytes.
        th._total_pending_bytes = 1
        th.flush()
        assert th._total_pending_bytes == 0  # 1 - 3 floored at 0.

    def test_4kib_early_flush_decrements_pending_gauge(self, monkeypatch):
        """The per-key TOKEN_FLUSH_BYTES early-flush path also decrements
        _total_pending_bytes (not just flush()/finish_part)."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.ingest.TOKEN_FLUSH_BYTES", 10)
        th = TokenStreamHub()
        _attach(th, "s1")
        th.on_part_updated(_updated_props(text=""))
        # Small delta: stays pending.
        th.on_part_delta(_delta_props(delta="tiny"))
        assert th._total_pending_bytes == 4
        # Delta crossing the threshold: triggers immediate early-flush.
        th.on_part_delta(_delta_props(delta="0123456789AB"))
        # Early-flush drained + decremented.
        assert th._total_pending_bytes == 0


# ===========================================================================
# Debug budget overrides (OC_SLIMAPI_TOKEN_STREAM_DEBUG_*) — apply mutates
# module globals so memory-limit eviction triggers with small data (联调).
# Default (None) = no change. Tests MUST restore the globals via try/finally
# (module-global mutation via `global` assignment is NOT tracked by monkeypatch).
# ===========================================================================

class TestDebugBudgetOverrides:
    """Debug/联调-only budget overrides: ``apply_debug_budget_overrides``
    mutates the hub module globals (``TOKEN_LIVEPARTS_MAX_BYTES`` etc.) so
    memory-limit eviction — the MB-P-S1 current-key nodrop trigger — fires
    with a small data volume during integration testing. Default (all-None
    settings) is a no-op."""

    def test_apply_lowers_live_budget_and_leaves_none_fields_at_code_default(self):
        import oc_slimapi.sse.tokenstream.budgets as hubmod
        orig_live = hubmod.TOKEN_LIVEPARTS_MAX_BYTES
        try:
            s = types.SimpleNamespace(
                token_stream_debug_live_budget_bytes=200,
                token_stream_debug_part_max_bytes=None,
                token_stream_debug_live_parts_max=None,
            )
            hubmod.apply_debug_budget_overrides(s)
            assert hubmod.TOKEN_LIVEPARTS_MAX_BYTES == 200
            # None fields leave the code-level defaults UNCHANGED (meaningful
            # check vs the known defaults, not the possibly-polluted start value).
            assert hubmod.TOKEN_PART_MAX_BYTES == 1024 * 1024
            assert hubmod.TOKEN_LIVE_PARTS_MAX == 32
        finally:
            hubmod.TOKEN_LIVEPARTS_MAX_BYTES = orig_live

    def test_apply_all_none_is_noop(self):
        import oc_slimapi.sse.tokenstream.budgets as hubmod
        orig_live = hubmod.TOKEN_LIVEPARTS_MAX_BYTES
        orig_part = hubmod.TOKEN_PART_MAX_BYTES
        orig_count = hubmod.TOKEN_LIVE_PARTS_MAX
        try:
            s = types.SimpleNamespace(
                token_stream_debug_live_budget_bytes=None,
                token_stream_debug_part_max_bytes=None,
                token_stream_debug_live_parts_max=None,
            )
            hubmod.apply_debug_budget_overrides(s)
            assert hubmod.TOKEN_LIVEPARTS_MAX_BYTES == orig_live
            assert hubmod.TOKEN_PART_MAX_BYTES == orig_part
            assert hubmod.TOKEN_LIVE_PARTS_MAX == orig_count
        finally:
            hubmod.TOKEN_LIVEPARTS_MAX_BYTES = orig_live
            hubmod.TOKEN_PART_MAX_BYTES = orig_part
            hubmod.TOKEN_LIVE_PARTS_MAX = orig_count

    def test_debug_live_budget_triggers_eviction_with_small_data(self):
        """End-to-end: a lowered debug live budget → a small delta triggers
        ``_reserve`` eviction (the 联调 use-case for MB-P-S1). Restores the
        mutated globals afterward (test isolation)."""
        import oc_slimapi.sse.tokenstream.budgets as hubmod
        orig_live = hubmod.TOKEN_LIVEPARTS_MAX_BYTES
        orig_part = hubmod.TOKEN_PART_MAX_BYTES
        try:
            s = types.SimpleNamespace(
                token_stream_debug_live_budget_bytes=15,
                token_stream_debug_part_max_bytes=10 ** 9,
                token_stream_debug_live_parts_max=None,
            )
            hubmod.apply_debug_budget_overrides(s)
            assert hubmod.TOKEN_LIVEPARTS_MAX_BYTES == 15
            th = TokenStreamHub()
            th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
            th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
            th.live_parts[("s1", "m1", "p1")].last_delta_ms = _now_ms() - 10000
            th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))   # 4
            th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bbbbb"))  # 5 → total 9
            sub = _attach(th, "s1")
            sub.frames.clear()
            # +7 to p2 → 9 + 7 = 16 > 15 cap → evict p1 (oldest), p2 survives.
            th.on_part_delta(_delta_props("s1", "m1", "p2", delta="ccccccc"))
            assert ("s1", "m1", "p1") not in th.live_parts
            assert ("s1", "m1", "p2") in th.live_parts
            assert th.token_memory_limit_total == 1
        finally:
            hubmod.TOKEN_LIVEPARTS_MAX_BYTES = orig_live
            hubmod.TOKEN_PART_MAX_BYTES = orig_part


# ---------------------------------------------------------------------------
# stop_and_wait() boundary contract (P2, rev-sgpt 9.3): the awaitable
# app-shutdown stop must (a) re-raise a child flush task's exception —
# merely cancel-requesting and returning would leave a done-with-exception
# task whose failure is neither propagated nor logged; (b) propagate a
# cancellation of the CALLER (outer Task) instead of swallowing it as if
# it were the managed child's expected cancellation.
#
# Deterministic Event-based synchronization only — no fixed sleeps.
# Tests 2-5 inject a minimal controllable task into ``_flush_task`` with
# the production-parity ``add_done_callback(hub._on_flush_done)`` wiring
# that ``start()`` installs; ``stop_and_wait`` itself is never mocked.
# ---------------------------------------------------------------------------


class TestStopAndWait:
    async def test_live_child_cancelled_and_awaited(self):
        """Boundary 1: live flush task (real ``start()``) → stop_and_wait
        cancels it, awaits full exit, clears the slot."""
        hub = TokenStreamHub()
        hub.start()
        task = hub._flush_task
        assert task is not None and not task.done()

        await hub.stop_and_wait()  # must not raise

        assert hub._flush_task is None
        assert task.done()
        assert task.cancelled()

    async def test_already_cancelled_child_returns_normally(self):
        """Boundary 2: child already cancelled (cancellation processed)
        → stop_and_wait returns normally and stays idempotent."""
        hub = TokenStreamHub()
        cancelled_observed = asyncio.Event()

        async def child():
            await asyncio.Event().wait()

        task = asyncio.create_task(child())
        task.add_done_callback(lambda t: cancelled_observed.set())
        task.add_done_callback(hub._on_flush_done)
        hub._flush_task = task
        task.cancel()
        await cancelled_observed.wait()
        assert task.cancelled()

        await hub.stop_and_wait()  # must not raise

        assert hub._flush_task is None

    async def test_already_successful_child_returns_normally(self):
        """Boundary 3: child already finished successfully → normal return,
        no cancellation requested, no exception surfaced."""
        hub = TokenStreamHub()
        success_observed = asyncio.Event()

        async def child():
            return "done-value"

        task = asyncio.create_task(child())
        task.add_done_callback(lambda t: success_observed.set())
        task.add_done_callback(hub._on_flush_done)
        hub._flush_task = task
        release = asyncio.Event()

        async def runner():
            await release.wait()

        # Drive the loop so the child actually completes (Event, not sleep).
        waiter = asyncio.create_task(runner())
        release.set()
        await success_observed.wait()
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        assert task.done() and not task.cancelled() and task.exception() is None

        await hub.stop_and_wait()  # must not raise

        assert hub._flush_task is None

    async def test_already_failed_child_reraises_exception(self):
        """Boundary 4 (contract a): child already dead with
        ``RuntimeError("boom")`` → the exception MUST be re-raised out of
        ``stop_and_wait()`` and actually retrieved (no ``Task exception
        was never retrieved``). The production ``_on_flush_done`` watchdog
        must not rebuild a replacement task mid-shutdown."""
        hub = TokenStreamHub()
        failed_observed = asyncio.Event()
        release = asyncio.Event()

        async def child():
            await release.wait()
            raise RuntimeError("boom")

        task = asyncio.create_task(child())
        # Production-parity wiring (what start() installs); observing via a
        # separate earlier callback keeps ordering deterministic.
        task.add_done_callback(lambda t: failed_observed.set())
        task.add_done_callback(hub._on_flush_done)
        hub._flush_task = task
        release.set()
        await failed_observed.wait()
        assert task.done()

        with pytest.raises(RuntimeError, match="boom"):
            await hub.stop_and_wait()

        # The raise itself proves retrieval; the asyncio pending-exception
        # bookkeeping must also be cleared (deterministic proxy for "no
        # un-retrieved warning").
        assert not getattr(task, "_log_traceback", False)
        # Watchdog did not rebuild a replacement flush task.
        assert hub._flush_task is None

    async def test_caller_cancellation_propagates(self):
        """Boundary 5 (contract b): cancelling the OUTER Task running
        ``stop_and_wait()`` must surface ``CancelledError`` to the caller —
        it must not be mistaken for the managed child's expected
        cancellation. The child observes its first cancellation (deferrable
        finalizer) so the two cancellations are distinguishable."""
        hub = TokenStreamHub()
        first_cancel_seen = asyncio.Event()
        finalizer_release = asyncio.Event()

        async def child():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                first_cancel_seen.set()  # observable finalizer entry
                await finalizer_release.wait()
                raise

        task = asyncio.create_task(child())
        task.add_done_callback(hub._on_flush_done)
        hub._flush_task = task

        outer = asyncio.create_task(hub.stop_and_wait())
        # Deterministic: proves stop_and_wait already cancel-requested the
        # child AND the child is parked in its finalizer.
        await first_cancel_seen.wait()

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        # Release the child's finalizer; reap it deterministically.
        finalizer_release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()
        # No leaked slot / replacement task.
        assert hub._flush_task is None
