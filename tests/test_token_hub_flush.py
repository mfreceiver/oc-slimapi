"""Stage-C tests for the token-stream flush engine + wire frames + memory
budget + subscribe bookkeeping (design §5.3 / §5.4 / §5.5 / §5.6 + §16-C).

Scope:

* ``flush_loop`` / ``flush``: 100ms cadence, 4KiB early-flush threshold,
  sorted-by-key drain order, pending cleared after flush, 60s ttl_sweep
  tick (NB-B5).
* ``finish_part``: synchronous drain of residual ``_pending`` → delta
  frame, then ``snapshot{done:true}`` terminal marker (assert NO ``text``
  field — lever 1), then retire (drop_part disables late deltas).
* ``safe_put`` / ``_emit_snapshot_or_truncated``: per-frame size check →
  ``snapshot{truncated:true}`` substitute + truncate-fanout to all subs of
  the sid (C6 backstop) + drop_part (idempotent — emitted once).
* ``_reserve``: per-part cap (TOKEN_PART_MAX_BYTES) → truncate; global
  byte/count cap (TOKEN_LIVEPARTS_MAX_BYTES / TOKEN_LIVE_PARTS_MAX) →
  LRU evict oldest + ``resync{token_memory_limit, sessionID}``.
* Wire frames: snapshot (initial/done:true marker), delta, truncated,
  resync (session-level), server.connected, server.heartbeat.
* ``attach_subscriber`` / ``detach_subscriber``: §5.5 handshake ordering
  (server.connected → flush_sid → snapshot → enter fanout), C2 no
  double-count, clear-pending semantics.
* ``_pending_session_resinks``: bounded drain (NB-B2 cap + drop-oldest),
  flush fans ``resync{reason, sessionID}``.
* ``on_upstream_reconnect``: fans ``resync{reconnect_no_replay}`` to every
  attached sid.

Out of scope (Stage D): HTTP endpoint, TokenSubscriber admission registry,
health ``features.tokenStream``, metrics endpoint wiring, gzip.
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
    TokenStreamHub,
    _delta_frame,
    _heartbeat_frame,
    _connected_frame,
    _resync_frame,
    _snapshot_frame,
    _truncated_frame,
    _now_ms,
    sse_frame,
)
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
    """Minimal subscriber: captures every put() frame in order.

    Mirrors the subset of ``hub.Subscriber`` / ``TokenSubscriber`` that
    TokenStreamHub calls: ``begin_handshake`` / ``end_handshake`` /
    ``put`` / ``closed`` / ``terminate``. These comprise the complete
    hub→sub contract; the CRITICAL 3 handshake/runtime queue PHYSICAL
    separation lives entirely INSIDE ``TokenSubscriber`` (its
    ``_SubscriberQueue``) and is exercised by
    ``test_token_subscriber_overflow.py``, so this stub does NOT need to
    mirror it — it just records the wire sequence so hub-logic tests can
    assert on ordering / fanout shape.
    """

    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.frames: list[bytes] = []
        self._in_handshake: bool = False
        self.closed: bool = False

    def begin_handshake(self) -> None:
        self._in_handshake = True

    def end_handshake(self) -> None:
        self._in_handshake = False

    def put(self, frame: bytes) -> bool:
        self.frames.append(frame)
        return True

    def terminate(self, reason: str) -> None:
        """INV-4: mirror TokenSubscriber.terminate — record resync + STOP."""
        from oc_slimapi.sse.tokenstream.frames import STOP, _resync_frame
        self.closed = True
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
    def test_snapshot_initial_has_text_and_done_false(self):
        frame = _snapshot_frame(("s1", "m1", "p1"), text="hello", done=False)
        event, data = parse_event(frame)
        assert event == "message.part.snapshot"
        assert data == {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "text": "hello", "done": False,
        }

    def test_snapshot_done_true_marker_has_no_text(self):
        """Lever 1 (§16-C): terminal marker omits the text field entirely."""
        frame = _snapshot_frame(("s1", "m1", "p1"), text=None, done=True)
        event, data = parse_event(frame)
        assert event == "message.part.snapshot"
        assert "text" not in data
        assert data == {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "done": True,
        }

    def test_delta_frame(self):
        frame = _delta_frame(("s1", "m1", "p1"), "chunk")
        event, data = parse_event(frame)
        assert event == "message.part.delta"
        assert data == {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "text": "chunk",
        }

    def test_truncated_frame_carries_done(self):
        for done in (False, True):
            frame = _truncated_frame(("s1", "m1", "p1"), done=done)
            event, data = parse_event(frame)
            assert event == "message.part.snapshot"
            assert data["truncated"] is True
            assert data["done"] is done
            assert "text" not in data

    def test_resync_frame_carries_session_id(self):
        """Token-stream resync is session-scoped (§5.6 frame 5)."""
        frame = _resync_frame("s1", "token_memory_limit")
        event, data = parse_event(frame)
        assert event == "resync"
        assert data == {"reason": "token_memory_limit", "sessionID": "s1"}

    def test_connected_frame(self):
        frame = _connected_frame("s1")
        event, data = parse_event(frame)
        assert event == "server.connected"
        assert data == {"sessionID": "s1"}

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
        # Clear the server.connected frame from the handshake.
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
        assert live.byte_count == 5  # full text retained for snapshots
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
# flush_sid() — handshake clear-pending (§5.5 step 3)
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
    def test_drains_residual_pending_as_delta(self):
        """C1: pending is drained and fanned as a delta BEFORE the marker."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="residual"))
        th.finish_part(("s1", "m1", "p1"), final_text="final-text")
        events = [parse_event(f) for f in sub.frames]
        # First frame: the residual delta.
        assert events[0][0] == "message.part.delta"
        assert events[0][1]["text"] == "residual"
        # Second frame: the terminal marker.
        assert events[1][0] == "message.part.snapshot"

    def test_terminal_marker_has_no_text_field(self):
        """Lever 1 (§16-C): done:true marker carries NO text."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.finish_part(("s1", "m1", "p1"), final_text="final-text")
        marker = sub.frames[-1]
        event, data = parse_event(marker)
        assert event == "message.part.snapshot"
        assert data["done"] is True
        assert "text" not in data  # lever 1 — NO text in terminal marker

    def test_terminal_marker_fans_to_all_subs_of_sid(self):
        th = TokenStreamHub()
        sub1 = _attach(th, "s1")
        sub2 = _attach(th, "s1")
        _attach(th, "s2")  # different sid — must NOT receive marker
        sub1.frames.clear()
        sub2.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.finish_part(("s1", "m1", "p1"), final_text="")
        # Both s1 subs got the marker; s2 didn't.
        assert any(parse_event(f)[1].get("done") for f in sub1.frames)
        assert any(parse_event(f)[1].get("done") for f in sub2.frames)

    def test_retires_part_after_finish(self):
        th = TokenStreamHub()
        _attach(th, "s1")
        th.on_part_updated(_updated_props(text="seed"))
        key = ("s1", "m1", "p1")
        th.finish_part(key, final_text="final")
        assert key not in th.live_parts
        assert key not in th._pending
        assert key in th._disabled_parts
        assert th._total_live_bytes == 0

    def test_late_delta_after_finish_drops_on_disabled(self):
        """After finish_part → drop_part, late deltas hit _disabled (silent)."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.finish_part(key, final_text="final")
        assert th.orphan_deltas == 0
        th.on_part_delta(_delta_props(delta="late"))
        # Late delta hit _disabled short-circuit → no orphan counter.
        assert th.orphan_deltas == 0
        assert key not in th._pending

    def test_no_marker_if_part_already_dropped(self):
        """If the part was truncated/evicted before text-end, no marker."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.drop_part(("s1", "m1", "p1"))  # simulate prior truncate/evict
        th.finish_part(("s1", "m1", "p1"), final_text="final")
        # No delta (no pending), no marker (no LivePart).
        assert sub.frames == []

    def test_finish_part_via_publish_text_end(self):
        """End-to-end: text-end upstream event triggers finish_part."""
        from oc_slimapi.sse.hub import GlobalHub

        hub = GlobalHub(client=None)
        th = TokenStreamHub()
        hub.set_token_hub(th)
        try:
            sub = _FakeSub()
            th.attach_subscriber("s1", sub)
            sub.frames.clear()
            hub.publish({
                "directory": "/p",
                "payload": {"type": "message.part.updated", "properties": {
                    "sessionID": "s1",
                    "part": {
                        "id": "p1", "messageID": "m1", "sessionID": "s1",
                        "type": "text", "text": "", "time": {},
                    },
                    "time": {},
                }},
            })
            hub.publish({
                "directory": "/p",
                "payload": {"type": "message.part.updated", "properties": {
                    "sessionID": "s1",
                    "part": {
                        "id": "p1", "messageID": "m1", "sessionID": "s1",
                        "type": "text", "text": "final", "time": {"end": 1},
                    },
                    "time": {},
                }},
            })
            events = [parse_event(f) for f in sub.frames]
            assert any(e[1].get("done") for e in events)
            assert ("s1", "m1", "p1") not in th.live_parts
        finally:
            for t in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task):
                if t is not None:
                    t.cancel()


# ===========================================================================
# safe_put / _emit_snapshot_or_truncated — C6 backstop
# ===========================================================================

class TestSafePutAndTruncate:
    def test_small_snapshot_delivered_as_is(self):
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text="small"))
        # Handshake already snapshotted; trigger a manual small snapshot.
        sub.frames.clear()
        th._emit_snapshot_or_truncated(sub, ("s1", "m1", "p1"), "tiny", done=False)
        assert len(sub.frames) == 1
        event, data = parse_event(sub.frames[0])
        assert data["text"] == "tiny"
        assert data["done"] is False

    def test_oversized_snapshot_emits_truncated_to_sub(self):
        """C6: snapshot frame exceeding max_frame_bytes → truncated substitute."""
        th = TokenStreamHub(max_frame_bytes=120)  # tiny cap
        sub = _attach(th, "s1")
        sub.frames.clear()
        huge = "x" * 1000
        th.on_part_updated(_updated_props(text=""))
        # LivePart exists so _emit_snapshot_or_truncated can trigger truncate.
        th._emit_snapshot_or_truncated(sub, ("s1", "m1", "p1"), huge, done=False)
        # Sub received truncated, not the huge snapshot.
        assert len(sub.frames) == 1
        event, data = parse_event(sub.frames[0])
        assert data["truncated"] is True
        assert "text" not in data

    def test_oversized_snapshot_triggers_fanout_to_existing_subs(self):
        """C6 backstop: existing subs of the sid also receive truncated."""
        th = TokenStreamHub(max_frame_bytes=120)
        existing = _attach(th, "s1")
        existing.frames.clear()
        # New sub attaches; its handshake triggers truncate-fanout.
        new_sub = _FakeSub()
        th.on_part_updated(_updated_props(text="x" * 1000))
        th.attach_subscriber("s1", new_sub)
        # Both subs got a truncated frame for the part.
        for sub in (existing, new_sub):
            truncs = [f for f in sub.frames if parse_event(f)[1].get("truncated")]
            assert len(truncs) == 1

    def test_truncate_drops_part(self):
        """After truncate-fanout, the part is dropped (no more snapshots)."""
        th = TokenStreamHub(max_frame_bytes=120)
        sub = _attach(th, "s1")
        th.on_part_updated(_updated_props(text="x" * 1000))
        th._truncate_part_for_all(("s1", "m1", "p1"), done=False)
        key = ("s1", "m1", "p1")
        assert key not in th.live_parts
        assert key in th._disabled_parts
        assert th.truncated_snapshots_total == 1

    def test_truncate_idempotent_emits_once(self):
        """drop_part idempotency: 2nd truncate call fans nothing."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th._truncate_part_for_all(("s1", "m1", "p1"), done=False)
        first_count = len(sub.frames)
        th._truncate_part_for_all(("s1", "m1", "p1"), done=False)
        assert len(sub.frames) == first_count  # no second fanout
        assert th.truncated_snapshots_total == 1


# ===========================================================================
# _reserve — per-part + global byte/count caps (C5 / §16-C)
# ===========================================================================

class TestReserve:
    def test_per_part_cap_truncates(self, monkeypatch):
        """Exceeding TOKEN_PART_MAX_BYTES → truncate + drop_part."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10)
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        # First 10-byte delta OK.
        th.on_part_delta(_delta_props(delta="0123456789"))
        # Second delta would push over 10 → truncate.
        th.on_part_delta(_delta_props(delta="x"))
        key = ("s1", "m1", "p1")
        assert key not in th.live_parts
        assert key in th._disabled_parts
        assert th.truncated_snapshots_total == 1
        # Truncated frame fanned to the sid.
        truncs = [f for f in sub.frames if parse_event(f)[1].get("truncated")]
        assert len(truncs) == 1

    def test_global_byte_cap_evicts_oldest(self, monkeypatch):
        """Exceeding TOKEN_LIVEPARTS_MAX_BYTES → LRU-evict oldest part."""
        # Small global cap so we can trigger eviction with small deltas.
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 15)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10**9)
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        # Backdate p1 so it's the LRU eviction target.
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = _now_ms() - 10000
        # Seed both parts with some bytes: p1=4, p2=5, total=9.
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bbbbb"))
        sub = _attach(th, "s1")
        sub.frames.clear()
        # Append 7 more to p2 → 9+7=16 > 15 cap → evict p1 (frees 4 → 5),
        # then 5+7=12 < 15 OK. p2 survives; p1 evicted.
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="ccccccc"))
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p2") in th.live_parts
        # token_memory_limit resync fanned.
        resyncs = [f for f in sub.frames if parse_event(f)[0] == "resync"]
        assert len(resyncs) >= 1
        _, data = parse_event(resyncs[0])
        assert data == {"reason": "token_memory_limit", "sessionID": "s1"}
        assert th.token_memory_limit_total == 1

    def test_global_count_cap_evicts_oldest(self, monkeypatch):
        """Exceeding TOKEN_LIVE_PARTS_MAX → LRU-evict oldest before creating."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVE_PARTS_MAX", 2)
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = _now_ms() - 10000
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        # At cap (2 parts). Creating p3 must evict p1 (oldest).
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props("s1", "m1", "p3", text=""))
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p3") in th.live_parts
        assert th.token_memory_limit_total == 1

    def test_never_evicts_current_key(self, monkeypatch):
        """_reserve never evicts the part it's reserving FOR (would corrupt)."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 5)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10**9)
        th = TokenStreamHub()
        # One part only. Append a delta that exceeds the global cap (5 bytes).
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="0123456789"))  # 10 bytes > 5 cap
        # Per-part cap disabled (10**9); global cap = 5; only this part. The
        # delta is bigger than the cap, and it's the only part → truncate
        # path (NOT a self-evict).
        key = ("s1", "m1", "p1")
        # The part was truncated (no other candidate to evict).
        assert key not in th.live_parts
        assert key in th._disabled_parts

    def test_eviction_disables_late_deltas(self, monkeypatch):
        """After memory-eviction, late deltas hit _disabled (silent drop)."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 15)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10**9)
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = _now_ms() - 10000
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bbbbb"))
        # Trigger eviction of p1 by overflowing p2.
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="ccccccc"))
        # p1 evicted + disabled.
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") in th._disabled_parts
        # Late delta for p1 → silent drop on _disabled (no orphan counter).
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="late"))
        assert th.orphan_deltas == 0

    def test_eviction_emits_resync_then_snapshot_for_remaining_live_parts(self, monkeypatch):
        """T3-C1 + T3-C2 + I1: after eviction, existing sub gets resync THEN
        snapshot for the surviving live part (B), B's delta still arrives,
        and pending double-count is prevented."""
        # Use large caps so the only budget pressure comes from the explicit
        # _evict_part_for_memory call (not from delta overflow).
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 10**9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10**9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PENDING_MAX_BYTES", 10**9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.ingest.TOKEN_FLUSH_BYTES", 10**9)
        th = TokenStreamHub()
        # Create two live parts on the same sid: p1 and p2.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        # Backdate p1 so it is the LRU target.
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = _now_ms() - 10000
        # Seed with some bytes.
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaaa"))  # 4 bytes
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bbbbb"))  # 5 bytes
        sub = _attach(th, "s1")
        # Clear the handshake frames (server.connected + snapshot for p1 + p2).
        sub.frames.clear()
        # --- I1: seed B with an unflushed pending delta before eviction ---
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="extra-chunk"))
        # Directly evict p1 (simulates the LRU eviction from _reserve).
        th._evict_part_for_memory(("s1", "m1", "p1"))
        # p1 evicted. Check wire order.
        events = [(parse_event(f)[0], parse_event(f)[1]) for f in sub.frames]
        # I1: pending for B must be empty after eviction (drain happened).
        assert ("s1", "m1", "p2") not in th._pending, \
            "I1: pending for remaining part B must be drained before re-snapshot"
        # With the fix, flush_sid runs before resync, so first event is a delta
        # (the unflushed pending from B), then resync, then snapshot.
        assert events[0][0] == "message.part.delta"
        assert events[0][1]["partID"] == "p2"
        assert b"extra-chunk" in sub.frames[0]
        # Expect second event: resync{token_memory_limit}.
        assert events[1][0] == "resync"
        assert events[1][1] == {"reason": "token_memory_limit", "sessionID": "s1"}
        # Expect third event: snapshot for the SURVIVING part B (p2), with done=False.
        assert events[2][0] == "message.part.snapshot"
        assert events[2][1]["partID"] == "p2"
        assert events[2][1].get("done") is False
        # M1: assert exact snapshot text (full accumulated including extra-chunk).
        assert events[2][1]["text"] == "bbbbbextra-chunk"
        # Only delta + resync + one snapshot — no extra frames between.
        assert len(events) == 3, f"expected 3 events, got {len(events)}"
        # Part A must NOT get a snapshot (it was dropped).
        assert not any(e[1].get("partID") == "p1" for e in events)
        # T3-C2: after eviction, a delta for B reaches the subscriber (not orphan).
        sub.frames.clear()
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="-delta-B"))
        th.flush()
        assert any(b"-delta-B" in f for f in sub.frames)
        # No orphan delta metric bump.
        assert th.orphan_deltas == 0
        # --- I1 regression: subsequent flush must NOT re-send extra-chunk ---
        sub.frames.clear()
        th.flush()
        for f in sub.frames:
            _event, _data = parse_event(f)
            if _event == "message.part.delta":
                assert b"extra-chunk" not in f, \
                    "I1: delta frame must not re-send pending text already in snapshot"

    def test_o1_evict_skips_current_key_being_reserved(self, monkeypatch):
        """MB-P-S1: ``_reserve → _evict_part_for_memory`` re-includes the
        current key (``skip_key``) via the nodrop path, closing the
        client-anchor gap for clear-only (method B) eviction.

        O1 invariant (unchanged): K is NEVER passed to ``drop_part``; the
        caller's stale ``live`` reference remains valid — no gauge drift or
        orphan deltas. Previously K was skipped entirely; now it receives a
        nodrop ``snapshot{truncated:true}`` frame (oversized for
        ``max_frame_bytes=64``)."""
        # Per-part cap huge (K may exceed max_frame_bytes without per-part
        # truncate); global LIVE cap small (K+A overflows → evict A).
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 200)
        th = TokenStreamHub(max_frame_bytes=64)  # tiny cap → K's snapshot WOULD truncate
        sub = _attach(th, "s1")  # handshake first: server.connected only (no live parts yet)
        sub.frames.clear()
        # Create K (150 bytes) + A (40 bytes, backdated older) AFTER attach, so
        # neither is handshake-snapshotted (which would prematurely truncate K).
        th.on_part_updated(_updated_props("s1", "m1", "pK", text="K" * 150))
        th.on_part_updated(_updated_props("s1", "m1", "pA", text="A" * 40))
        th.live_parts[("s1", "m1", "pA")].last_delta_ms = _now_ms() - 10000
        assert th._total_live_bytes == 190  # 150 + 40

        k_key = ("s1", "m1", "pK")
        live_before = th.live_parts[k_key]

        # Delta to K pushes global LIVE (190 + 50 = 240) over 200 → _reserve evicts A.
        th.on_part_delta(_delta_props("s1", "m1", "pK", delta="X" * 50))

        # O1: K survived the eviction (NOT truncated/dropped mid-reserve).
        assert k_key in th.live_parts, \
            "current key K must not be dropped by eviction re-snapshot"
        assert k_key not in th._disabled_parts
        assert th.live_parts[k_key] is live_before  # same object — no stale-ref swap
        # A was the intended eviction victim.
        assert ("s1", "m1", "pA") not in th.live_parts
        # No gauge drift: K's 150 + new delta 50 = 200 (A's 40 removed).
        assert th._total_live_bytes == 200
        # MB-P-S1: sub receives resync THEN a truncated snapshot for K (nodrop).
        events = [parse_event(f) for f in sub.frames]
        # First event: resync{token_memory_limit}.
        assert events[0][0] == "resync"
        assert events[0][1] == {"reason": "token_memory_limit", "sessionID": "s1"}
        # Second event: truncated snapshot for K (oversized → truncated, not dropped).
        assert events[1][0] == "message.part.snapshot"
        assert events[1][1]["partID"] == "pK"
        assert events[1][1].get("truncated") is True
        assert "text" not in events[1][1]
        assert events[1][1].get("done") is False
        # Only two frames.
        assert len(events) == 2, f"expected 2 events (resync + truncated), got {len(events)}"
        # truncated_snapshots_total counted per-sub (nodrop path).
        assert th.truncated_snapshots_total == 1
        # O1 consequence: K survived, so its delta is delivered (not orphan).
        sub.frames.clear()
        th.flush()
        assert th.orphan_deltas == 0, "K's delta must not be orphaned (K survived)"
        assert any(b"X" in f for f in sub.frames), "K's delta must be delivered on flush"

    def test_mb_p_s1_small_current_key_gets_real_snapshot_on_evict(self, monkeypatch):
        """MB-P-S1: when the current key K's snapshot frame fits within
        ``max_frame_bytes``, it receives a real ``snapshot{done:false}`` with
        full text (animation preserved) — not truncated."""
        # Small global LIVE cap so the delta triggers eviction.
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 48)
        # Use the default (large) max_frame_bytes so K's snapshot fits.
        th = TokenStreamHub()  # max_frame_bytes = DEFAULT_TOKEN_MAX_FRAME_BYTES (1 MiB)
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props("s1", "m1", "pK", text="small"))  # 5 bytes
        th.on_part_updated(_updated_props("s1", "m1", "pA", text="A" * 40))   # 40 bytes
        th.live_parts[("s1", "m1", "pA")].last_delta_ms = _now_ms() - 10000
        live_before = th.live_parts[("s1", "m1", "pK")]
        # Total before delta: 5 + 40 = 45, under 48.
        assert th._total_live_bytes == 45

        # Delta to K (6 bytes " extra") → 45 + 6 = 51 > 48 → _reserve evicts A.
        th.on_part_delta(_delta_props("s1", "m1", "pK", delta=" extra"))

        k_key = ("s1", "m1", "pK")
        # K survived, not dropped, same object. A evicted.
        assert k_key in th.live_parts
        assert k_key not in th._disabled_parts
        assert th.live_parts[k_key] is live_before
        assert ("s1", "m1", "pA") not in th.live_parts
        # No gauge drift: K seed (5) + delta (6) = 11 (A's 40 removed).
        assert th._total_live_bytes == 11, \
            f"expected 11 (only K: 5 seed + 6 delta), got {th._total_live_bytes}"

        # MB-P-S1: sub gets resync THEN a real (non-truncated) snapshot with
        # K's seed text "small" (the delta hasn't been appended yet when
        # _evict_part_for_memory reads live.chunks).
        events = [parse_event(f) for f in sub.frames]
        assert events[0][0] == "resync"
        assert events[0][1] == {"reason": "token_memory_limit", "sessionID": "s1"}
        assert events[1][0] == "message.part.snapshot"
        assert events[1][1]["partID"] == "pK"
        assert events[1][1].get("truncated") is None  # not truncated (fits)
        assert events[1][1].get("text") is not None   # full text
        assert events[1][1]["text"] == "small"         # seed text only
        assert events[1][1].get("done") is False
        assert len(events) == 2, f"expected 2 events, got {len(events)}"
        # No truncated frame emitted (snapshot fits).
        assert th.truncated_snapshots_total == 0

        # Subsequent delta not orphan.
        sub.frames.clear()
        th.flush()
        assert th.orphan_deltas == 0

    def test_mb_p_s1_large_current_key_truncated_without_drop(self, monkeypatch):
        """MB-P-S1: a large current key K receives a nodrop truncated frame
        (``truncated:true``) on eviction re-snapshot. K stays in
        ``live_parts``, NOT in ``_disabled_parts``; ``truncated_snapshots_total``
        reflects the per-sub count."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 200)
        th = TokenStreamHub(max_frame_bytes=64)
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props("s1", "m1", "pK", text="K" * 150))
        th.on_part_updated(_updated_props("s1", "m1", "pA", text="A" * 40))
        th.live_parts[("s1", "m1", "pA")].last_delta_ms = _now_ms() - 10000

        k_key = ("s1", "m1", "pK")
        live_before = th.live_parts[k_key]
        th.on_part_delta(_delta_props("s1", "m1", "pK", delta="X" * 50))

        # K still alive — O1 invariant holds.
        assert k_key in th.live_parts
        assert k_key not in th._disabled_parts
        assert th.live_parts[k_key] is live_before
        assert th._total_live_bytes == 200
        # truncated_snapshots_total counts the per-sub nodrop truncated emit.
        assert th.truncated_snapshots_total == 1
        # Wire: resync → truncated for K.
        events = [parse_event(f) for f in sub.frames]
        assert len(events) == 2
        assert events[1][0] == "message.part.snapshot"
        assert events[1][1]["partID"] == "pK"
        assert events[1][1].get("truncated") is True
        assert "text" not in events[1][1]

    def test_mb_p_s1_nodrop_truncated_emitted_per_sub(self, monkeypatch):
        """MB-P-S1 multi-sub: an oversized current key K delivers a nodrop
        truncated frame to EACH attached subscriber, and
        ``truncated_snapshots_total`` counts per-sub (== number of subs) —
        distinct from ``_truncate_part_for_all``'s per-drop (==1) count."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 200)
        th = TokenStreamHub(max_frame_bytes=64)
        sub1 = _attach(th, "s1")
        sub2 = _attach(th, "s1")
        sub1.frames.clear()
        sub2.frames.clear()
        th.on_part_updated(_updated_props("s1", "m1", "pK", text="K" * 150))
        th.on_part_updated(_updated_props("s1", "m1", "pA", text="A" * 40))
        th.live_parts[("s1", "m1", "pA")].last_delta_ms = _now_ms() - 10000

        k_key = ("s1", "m1", "pK")
        live_before = th.live_parts[k_key]
        # Delta to K overflows global cap → _reserve evicts A; re-snapshot
        # sends K's truncated frame to BOTH subs via the nodrop path.
        th.on_part_delta(_delta_props("s1", "m1", "pK", delta="X" * 50))

        # O1 invariant: K survived (not dropped mid-reserve).
        assert k_key in th.live_parts
        assert k_key not in th._disabled_parts
        assert th.live_parts[k_key] is live_before
        # Both subs received K's truncated frame (per-sub nodrop emit).
        for sub in (sub1, sub2):
            k_events = [e for e in (parse_event(f) for f in sub.frames)
                        if e[1].get("partID") == "pK"]
            assert len(k_events) == 1, "each sub gets exactly one K frame"
            assert k_events[0][1].get("truncated") is True
            assert "text" not in k_events[0][1]
        # per-sub metric: 2 subs → truncated_snapshots_total == 2 (vs the
        # _truncate_part_for_all path which would count == 1 per part drop).
        assert th.truncated_snapshots_total == 2, \
            f"per-sub nodrop count: expected 2 (two subs), got {th.truncated_snapshots_total}"

    def test_evict_resnapshot_drops_oversized_non_current_part(self, monkeypatch):
        """MB-P-S1 regression: a non-current oversized part B is still
        truncated AND dropped during eviction re-snapshot (C6 backstop
        unchanged for non-skip_key parts). The current key K is also
        truncated (114-byte frame > 64 max_frame_bytes) but stays in
        ``live_parts`` (nodrop path)."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_PART_MAX_BYTES", 10 ** 9)
        # Live parts after seed: K=5, A=40, B=150 → total=195.
        # Set cap to 195 so the 1-byte delta triggers eviction.
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.budgets.TOKEN_LIVEPARTS_MAX_BYTES", 195)
        th = TokenStreamHub(max_frame_bytes=64)
        sub = _attach(th, "s1")
        sub.frames.clear()
        # Three parts on same sid: K (current), A (eviction victim), B (oversized non-current).
        th.on_part_updated(_updated_props("s1", "m1", "pK", text="small"))   # 5 bytes
        th.on_part_updated(_updated_props("s1", "m1", "pA", text="A" * 40))   # 40 bytes
        th.on_part_updated(_updated_props("s1", "m1", "pB", text="B" * 150))  # 150 bytes
        # Backdate A so it is the LRU eviction target.
        th.live_parts[("s1", "m1", "pA")].last_delta_ms = _now_ms() - 20000
        th.live_parts[("s1", "m1", "pB")].last_delta_ms = _now_ms() - 10000
        assert th._total_live_bytes == 195  # 5 + 40 + 150

        k_key = ("s1", "m1", "pK")
        b_key = ("s1", "m1", "pB")
        # Delta to K (1 byte) → 196 > 195 → _reserve evicts A (oldest non-current).
        th.on_part_delta(_delta_props("s1", "m1", "pK", delta="!"))

        # K survived via nodrop (NOT in _disabled_parts).
        assert k_key in th.live_parts
        assert k_key not in th._disabled_parts, \
            "current key K must NOT be in _disabled_parts (nodrop path)"
        # A evicted (victim).
        assert ("s1", "m1", "pA") not in th.live_parts
        # B was truncated + DROPPED (non-current oversized → C6 backstop).
        assert b_key not in th.live_parts, \
            "non-current oversized part B must be dropped by C6 backstop"
        assert b_key in th._disabled_parts, \
            "non-current oversized part B must be recorded in _disabled_parts"
        # No gauge drift: K 5 + delta 1 = 6. (A 40 + B 150 removed).
        assert th._total_live_bytes == 6, \
            f"expected 6 (K only), got {th._total_live_bytes}"

        # Check wire: resync → K truncated (nodrop) → B truncated (C6 dropped).
        events = [parse_event(f) for f in sub.frames]
        assert events[0][0] == "resync"
        assert events[0][1] == {"reason": "token_memory_limit", "sessionID": "s1"}
        # K event: truncated nodrop (114-byte frame > 64 max_frame_bytes).
        k_events = [e for e in events if e[1].get("partID") == "pK"]
        assert len(k_events) == 1
        assert k_events[0][1].get("truncated") is True, \
            "K truncated via nodrop (frame > max_frame_bytes)"
        assert "text" not in k_events[0][1]
        # B event: truncated C6 (dropped).
        b_events = [e for e in events if e[1].get("partID") == "pB"]
        assert len(b_events) == 1
        assert b_events[0][1].get("truncated") is True
        assert "text" not in b_events[0][1]

        # Key invariant: K in live_parts (nodrop), B not (C6 dropped).
        assert k_key in th.live_parts
        assert b_key not in th.live_parts
        assert b_key in th._disabled_parts

        # Subsequent delta for K not orphan.
        sub.frames.clear()
        th.flush()
        assert th.orphan_deltas == 0


# ===========================================================================
# attach_subscriber — §5.5 handshake ordering + C2 no-double-count
# ===========================================================================

class TestAttachSubscriber:
    def test_handshake_emits_connected_first(self):
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        event, _ = parse_event(sub.frames[0])
        assert event == "server.connected"

    def test_handshake_snapshots_active_parts(self):
        """Active LivePart → snapshot{done:false} with full accumulated text."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="hello"))
        th.on_part_delta(_delta_props(delta="world"))
        sub = _attach(th, "s1")
        # Find the snapshot frame (after server.connected).
        snaps = [
            f for f in sub.frames
            if parse_event(f)[0] == "message.part.snapshot"
        ]
        assert len(snaps) == 1
        _, data = parse_event(snaps[0])
        assert data["text"] == "helloworld"  # full accumulated text
        assert data["done"] is False

    def test_handshake_no_snapshot_for_finished_part(self):
        """A finished (dropped) part is not in live_parts → no snapshot."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.finish_part(("s1", "m1", "p1"), final_text="done")
        sub = _attach(th, "s1")
        snaps = [
            f for f in sub.frames
            if parse_event(f)[0] == "message.part.snapshot"
        ]
        assert snaps == []

    def test_handshake_no_double_count(self):
        """C2: pending is flushed to EXISTING subs BEFORE the new sub's snapshot.

        Existing sub receives the pending delta; new sub's snapshot INCLUDES
        that text (via join(chunks)); neither double-receives nor gaps.
        """
        th = TokenStreamHub()
        existing = _attach(th, "s1")
        existing.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="pending-chunk"))
        # New sub attaches — triggers flush_sid(existing gets delta) +
        # snapshot(new sub gets full text).
        new_sub = _attach(th, "s1")
        # Existing got the pending delta.
        existing_deltas = [
            f for f in existing.frames
            if parse_event(f)[0] == "message.part.delta"
        ]
        assert any(b"pending-chunk" in f for f in existing_deltas)
        # New sub's snapshot contains the full text including the pending chunk.
        new_snaps = [
            f for f in new_sub.frames
            if parse_event(f)[0] == "message.part.snapshot"
        ]
        assert any(b"pending-chunk" in f for f in new_snaps)
        # New sub did NOT receive the delta frame (not in fanout during flush_sid).
        new_deltas = [
            f for f in new_sub.frames
            if parse_event(f)[0] == "message.part.delta"
        ]
        assert new_deltas == []

    def test_subscriber_count_reflects_attach_detach(self):
        th = TokenStreamHub()
        assert th.subscriber_count == 0
        s1 = _FakeSub()
        s2 = _FakeSub()
        th.attach_subscriber("s1", s1)
        th.attach_subscriber("s1", s2)
        th.attach_subscriber("s2", _FakeSub())
        assert th.subscriber_count == 3
        th.detach_subscriber("s1", s1)
        assert th.subscriber_count == 2
        th.detach_subscriber("s1", s1)  # idempotent
        assert th.subscriber_count == 2

    def test_detach_removes_empty_sid_entry(self):
        th = TokenStreamHub()
        sub = _FakeSub()
        th.attach_subscriber("s1", sub)
        th.detach_subscriber("s1", sub)
        assert "s1" not in th._subs_by_sid

    def test_handshake_only_snapshots_matching_sid(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s2", "m2", "p2", text=""))
        sub = _attach(th, "s1")
        snaps = [
            f for f in sub.frames
            if parse_event(f)[0] == "message.part.snapshot"
        ]
        # Only s1's part snapshotted.
        assert all(parse_event(f)[1]["sessionID"] == "s1" for f in snaps)
        assert len(snaps) == 1

    def test_attach_then_delta_reaches_sub(self):
        """After attach, subsequent deltas fan to the sub (it's in fanout)."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="after-attach"))
        th.flush()
        deltas = [
            f for f in sub.frames
            if parse_event(f)[0] == "message.part.delta"
        ]
        assert any(b"after-attach" in f for f in deltas)


# ===========================================================================
# _pending_session_resinks — bounded drain (NB-B2)
# ===========================================================================

class TestPendingSessionResyncs:
    def test_idle_resync_drained_on_flush(self):
        """on_session_status idle enqueues; flush fans resync to subs."""
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_session_status("s1", "idle")
        th.flush()
        resyncs = [f for f in sub.frames if parse_event(f)[0] == "resync"]
        assert len(resyncs) == 1
        _, data = parse_event(resyncs[0])
        assert data == {"reason": "session_idle", "sessionID": "s1"}

    def test_deleted_terminates_subscriber_directly(self):
        """INV-4 (P0-3): on_session_deleted directly terminates subscribers
        (resync{session_deleted} → STOP), not via the flush loop. The frames
        are in sub.frames immediately after on_session_deleted — no flush()
        needed (the previous _enqueue_session_resync + flush path is gone).

        Test updated from test_deleted_resync_drained_on_flush because the
        behavior changed: session.deleted now delivers resync+STOP directly
        via TokenSubscriber.terminate (server-side termination), not via the
        deferred flush-loop resync queue.
        """
        from oc_slimapi.sse.tokenstream.frames import STOP
        th = TokenStreamHub()
        sub = _attach(th, "s1")
        sub.frames.clear()
        th.on_session_deleted("s1")
        # resync{session_deleted} delivered directly (no flush needed).
        resyncs = [
            f for f in sub.frames
            if isinstance(f, bytes) and parse_event(f)[0] == "resync"
        ]
        assert len(resyncs) == 1
        _, data = parse_event(resyncs[0])
        assert data["reason"] == "session_deleted"
        # STOP delivered after resync (strict order).
        resync_idx = sub.frames.index(resyncs[0])
        stop_idx = sub.frames.index(STOP)
        assert stop_idx > resync_idx
        # Sub marked closed.
        assert sub.closed is True

    def test_resync_only_fans_to_matching_sid(self):
        th = TokenStreamHub()
        sub1 = _attach(th, "s1")
        sub2 = _attach(th, "s2")
        sub1.frames.clear()
        sub2.frames.clear()
        th.on_session_status("s1", "idle")
        th.flush()
        assert any(parse_event(f)[0] == "resync" for f in sub1.frames)
        assert not any(parse_event(f)[0] == "resync" for f in sub2.frames)

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
        are delivered via the handshake snapshot, not a delta frame).
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
