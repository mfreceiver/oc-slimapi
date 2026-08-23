"""Stage-A tests for the token-stream accumulator (design §5.3).

Scope: data structures + ingest + drop_part + injection ONLY.

* ``on_part_updated``: text-start creates a LivePart (regardless of
  subscribers); non-text part → ``_nontext_parts`` (no LivePart);
  repeated start doesn't reset; seed byte_count tracked.
* ``on_part_delta``: ``field`` / text gating; orphan silent drop +
  metric (NO exception, NO resync); ``_nontext`` key dropped;
  ``_disabled`` key dropped; valid → appends to chunks + pending;
  UTF-8 byte counting.
* ``drop_part``: pops pending + live; byte accounting decrement;
  idempotent (2nd call returns False); adds to ``_disabled``.
* ``DeltaAccumulator``: append + drain + UTF-8 byte count.
* ``hub.publish()`` integration: a ``message.part.delta`` upstream event
  routes into the token hub (when wired) and does NOT pollute the
  control-plane (no digest, no fan-out).

Stage-B/C/D behaviours (flush, finish_part, subscribers, _reserve
eviction, HTTP endpoint, fan-out) are explicitly out of scope and
covered by their own stages.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from oc_slimapi.sse.hub import GlobalHub, Subscriber
from oc_slimapi.sse.replay_log import ReplayLog
from oc_slimapi.sse.token_hub import (
    DeltaAccumulator,
    LivePart,
    TokenStreamHub as _TokenStreamHub,
)


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


def _global_hub(client=None, **kwargs) -> GlobalHub:
    assert _TEST_REPLAY_LOG is not None
    return GlobalHub(client=client, replay_log=_TEST_REPLAY_LOG, **kwargs)


def _hub_registry(**kwargs):
    from oc_slimapi.sse.hub import HubRegistry

    assert _TEST_REPLAY_LOG is not None
    return HubRegistry(replay_log=_TEST_REPLAY_LOG, **kwargs)


# ---------------------------------------------------------------------------
# Helpers — inlined from tests/test_hub.py (this repo's pattern is per-file
# helpers; no sibling-test imports).
# ---------------------------------------------------------------------------

def make_global_event(
    directory: str,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties}}."""
    payload: dict = {"type": event_type, "properties": properties or {}}
    return {"directory": directory, "payload": payload}


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


async def drain_queue(subscriber: Subscriber, timeout: float = 0.2) -> list[bytes]:
    frames: list[bytes] = []
    while True:
        try:
            item = await asyncio.wait_for(subscriber.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if item is None:
            continue
        frames.append(item)
    return frames


async def _close_hub(hub: GlobalHub) -> None:
    """Cancel + await every GlobalHub background task (incl. stop_after_grace)."""
    tasks = [
        task
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    hub.task = None
    hub.flush_task = None
    hub.heartbeat_task = None
    hub.stop_task = None


def _delta_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    field: str = "text", delta: str = "x",
) -> dict:
    """Build properties for a message.part.delta event (§4 shape)."""
    return {
        "sessionID": sid, "messageID": mid, "partID": pid,
        "field": field, "delta": delta,
    }


def _updated_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    *, type: str = "text", text: str | None = None, end=None,
) -> dict:
    """Build properties for a message.part.updated event (§4 shape).

    ``end`` controls the part lifecycle: ``None`` → text-start (creates a
    LivePart); a truthy value → text-end (Stage C finish_part).
    """
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


# ---------------------------------------------------------------------------
# DeltaAccumulator unit tests
# ---------------------------------------------------------------------------

class TestDeltaAccumulator:
    def test_append_increments_byte_count_utf8(self):
        acc = DeltaAccumulator()
        acc.append("hello")
        assert acc.byte_count == 5
        # Multi-byte UTF-8: '世' is 3 bytes.
        acc.append("世")
        assert acc.byte_count == 5 + 3
        assert acc.chunks == ["hello", "世"]

    def test_append_empty_is_noop(self):
        acc = DeltaAccumulator()
        acc.append("")
        assert acc.chunks == []
        assert acc.byte_count == 0

    def test_drain_joins_clears_resets(self):
        acc = DeltaAccumulator()
        acc.append("foo")
        acc.append("bar")
        text = acc.drain()
        assert text == "foobar"
        assert acc.chunks == []
        assert acc.byte_count == 0
        # Reusable after drain.
        acc.append("baz")
        assert acc.chunks == ["baz"]
        assert acc.byte_count == 3

    def test_drain_empty_returns_empty_string(self):
        acc = DeltaAccumulator()
        assert acc.drain() == ""
        assert acc.byte_count == 0


# ---------------------------------------------------------------------------
# on_part_updated
# ---------------------------------------------------------------------------

class TestOnPartUpdated:
    def test_text_start_creates_live_part_regardless_of_subscribers(self):
        th = TokenStreamHub()
        # No subscribers attached — LivePart must still be created (B1).
        th.on_part_updated(_updated_props(text=""))
        assert ("s1", "m1", "p1") in th.live_parts
        assert isinstance(th.live_parts[("s1", "m1", "p1")], LivePart)

    def test_non_text_part_recorded_as_nontext_no_live_part(self):
        th = TokenStreamHub()
        # reasoning part — C3 isolation.
        th.on_part_updated(_updated_props(type="reasoning", text="thinking..."))
        key = ("s1", "m1", "p1")
        assert key in th._nontext_parts
        assert key not in th.live_parts

    def test_repeated_start_does_not_reset_accumulator(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))
        key = ("s1", "m1", "p1")
        # Simulate a delta arriving between two updated frames.
        th.on_part_delta(_delta_props(delta="chunk"))
        # A second text-start (middle updated) must NOT clobber.
        th.on_part_updated(_updated_props(text="OTHER-SEED"))
        live = th.live_parts[key]
        # Original seed + delta preserved; second start ignored.
        assert live.chunks == ["seed", "chunk"]
        assert live.byte_count == len("seed") + len("chunk")

    def test_seed_byte_count_tracked(self):
        th = TokenStreamHub()
        seed = "héllo"  # 5 code points, 6 UTF-8 bytes (é = 2 bytes)
        th.on_part_updated(_updated_props(text=seed))
        key = ("s1", "m1", "p1")
        live = th.live_parts[key]
        assert live.chunks == [seed]
        assert live.byte_count == len(seed.encode("utf-8"))
        assert th._total_live_bytes == live.byte_count

    def test_empty_seed_creates_empty_live_part(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        live = th.live_parts[("s1", "m1", "p1")]
        assert live.chunks == []
        assert live.byte_count == 0
        assert th._total_live_bytes == 0

    def test_missing_part_dict_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated({"sessionID": "s1"})  # no 'part' key
        assert th.live_parts == {}

    def test_part_not_dict_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated({"part": "not-a-dict"})
        assert th.live_parts == {}

    def test_missing_key_components_is_noop(self):
        th = TokenStreamHub()
        # Missing messageID.
        th.on_part_updated({"part": {
            "id": "p1", "sessionID": "s1", "type": "text", "time": {},
        }})
        assert th.live_parts == {}

    def test_empty_key_components_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated({"part": {
            "id": "", "messageID": "m1", "sessionID": "s1",
            "type": "text", "time": {},
        }})
        assert th.live_parts == {}

    def test_missing_time_dict_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated({"part": {
            "id": "p1", "messageID": "m1", "sessionID": "s1",
            "type": "text",
        }})
        assert th.live_parts == {}

    def test_text_end_triggers_finish_part_retire(self):
        """Stage C: text-end calls finish_part → drain + retire (drop_part).

        No subscribers attached → no fanout, but the part MUST be retired
        (no LivePart, key in _disabled so late deltas drop). This replaces
        the Stage-A stub behaviour where text-end was a no-op.
        """
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))
        key = ("s1", "m1", "p1")
        # text-end arrives.
        th.on_part_updated(_updated_props(text="final", end=1700000000000))
        # Part retired.
        assert key not in th.live_parts
        assert key not in th._pending
        assert key in th._disabled_parts  # drop_part disabled it.
        assert th._total_live_bytes == 0


# ---------------------------------------------------------------------------
# on_part_delta
# ---------------------------------------------------------------------------

class TestOnPartDelta:
    def test_non_text_field_dropped(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(field="reasoning", delta="x"))
        # field != "text" → no-op.
        assert th.live_parts[("s1", "m1", "p1")].chunks == []

    def test_orphan_delta_silent_drop_with_metric_no_exception(self):
        """C3: delta with no prior text-start → silent drop + counter.

        MUST NOT raise and MUST NOT trigger any resync. This was the
        v2-rev failure mode (per-token resync storm).
        """
        th = TokenStreamHub()
        # No text-start first.
        th.on_part_delta(_delta_props(delta="orphan"))
        assert th.orphan_deltas == 1
        assert th.live_parts == {}
        assert th._pending == {}

    def test_multiple_orphan_deltas_accumulate_metric(self):
        th = TokenStreamHub()
        for _ in range(3):
            th.on_part_delta(_delta_props(delta="x"))
        assert th.orphan_deltas == 3

    def test_nontext_key_delta_dropped(self):
        """C3: once a part is classified non-text, its deltas drop."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(type="reasoning"))
        key = ("s1", "m1", "p1")
        assert key in th._nontext_parts
        th.on_part_delta(_delta_props(delta="reasoning-chunk"))
        assert th._pending == {}
        # No LivePart ever created for non-text.
        assert key not in th.live_parts
        # Not counted as orphan (it was classified, not missed).
        assert th.orphan_deltas == 0

    def test_disabled_key_delta_dropped(self):
        """C4: after drop_part, late deltas for the key drop silently."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.on_part_delta(_delta_props(delta="before"))
        assert th._pending[key].chunks == ["before"]
        th.drop_part(key)
        # Pending cleared by drop_part.
        assert key not in th._pending
        # Late delta after disable — silent drop.
        th.on_part_delta(_delta_props(delta="after"))
        assert key not in th._pending
        assert key not in th.live_parts

    def test_valid_delta_appends_to_chunks_and_pending(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.on_part_delta(_delta_props(delta="hello"))
        live = th.live_parts[key]
        assert live.chunks == ["hello"]
        assert th._pending[key].chunks == ["hello"]
        # last_delta_ms updated to ~now.
        assert live.last_delta_ms > 0

    def test_utf8_byte_count_tracked(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        # '世' = 3 UTF-8 bytes.
        th.on_part_delta(_delta_props(delta="世"))
        live = th.live_parts[key]
        assert live.byte_count == 3
        assert th._total_live_bytes == 3
        assert th._pending[key].byte_count == 3

    def test_multiple_deltas_accumulate(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.on_part_delta(_delta_props(delta="a"))
        th.on_part_delta(_delta_props(delta="b"))
        th.on_part_delta(_delta_props(delta="c"))
        live = th.live_parts[key]
        assert live.chunks == ["a", "b", "c"]
        assert live.byte_count == 3
        assert th._pending[key].chunks == ["a", "b", "c"]
        assert th._total_live_bytes == 3

    def test_missing_field_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta({"sessionID": "s1", "messageID": "m1", "partID": "p1", "delta": "x"})
        assert th.live_parts[("s1", "m1", "p1")].chunks == []

    def test_missing_key_components_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        # Missing partID.
        th.on_part_delta({"sessionID": "s1", "messageID": "m1", "field": "text", "delta": "x"})
        assert th.live_parts[("s1", "m1", "p1")].chunks == []
        assert th.orphan_deltas == 0

    def test_empty_delta_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta=""))
        assert th.live_parts[("s1", "m1", "p1")].chunks == []

    def test_non_string_delta_is_noop(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta=123))  # type: ignore[arg-type]
        assert th.live_parts[("s1", "m1", "p1")].chunks == []


# ---------------------------------------------------------------------------
# drop_part
# ---------------------------------------------------------------------------

class TestDropPart:
    def test_pops_pending_and_live(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))
        key = ("s1", "m1", "p1")
        th.on_part_delta(_delta_props(delta="chunk"))
        assert key in th._pending
        assert key in th.live_parts
        result = th.drop_part(key)
        assert result is True
        assert key not in th._pending
        assert key not in th.live_parts

    def test_byte_accounting_decrement(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))  # 4 bytes
        key = ("s1", "m1", "p1")
        th.on_part_delta(_delta_props(delta="chunk"))    # 5 bytes
        assert th._total_live_bytes == 9
        th.drop_part(key)
        assert th._total_live_bytes == 0

    def test_byte_accounting_decrement_with_multiple_parts(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s1", "m1", "p2", text=""))
        k1 = ("s1", "m1", "p1")
        k2 = ("s1", "m1", "p2")
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="aaa"))
        th.on_part_delta(_delta_props("s1", "m1", "p2", delta="bb"))
        assert th._total_live_bytes == 5
        th.drop_part(k1)
        assert th._total_live_bytes == 2  # only p2's bytes remain
        th.drop_part(k2)
        assert th._total_live_bytes == 0

    def test_idempotent_second_call_returns_false(self):
        """drop_part applies state retirement exactly once."""
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        first = th.drop_part(key)
        second = th.drop_part(key)
        assert first is True
        assert second is False
        assert key in th._disabled_parts

    def test_adds_to_disabled(self):
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        th.drop_part(key)
        assert key in th._disabled_parts

    def test_drop_part_discards_from_nontext(self):
        """drop_part on a non-text key removes it from _nontext_parts too
        (defensive: Stage-B bounded-set prune reuses this path)."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(type="reasoning"))
        key = ("s1", "m1", "p1")
        assert key in th._nontext_parts
        th.drop_part(key)
        assert key not in th._nontext_parts
        assert key in th._disabled_parts

    def test_drop_unseen_key_still_marks_disabled(self):
        """A drop for a never-seen key is legal and disables it."""
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        result = th.drop_part(key)
        assert result is True
        assert key in th._disabled_parts
        # And subsequent deltas drop silently.
        th.on_part_delta(_delta_props(delta="late"))
        assert th._pending == {}
        assert th.orphan_deltas == 0  # disabled short-circuits before orphan check

    def test_byte_floor_at_zero(self):
        """Defensive: byte accounting never goes negative on a malformed
        sequence (e.g. manual state tamper in tests)."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.on_part_delta(_delta_props(delta="abc"))
        # Tamper: inflate _total_live_bytes then drop — must floor at 0.
        th._total_live_bytes = 1
        th.drop_part(key)
        assert th._total_live_bytes == 0


# ---------------------------------------------------------------------------
# subscriber_count + on_upstream_reconnect (Stage-A stubs)
# ---------------------------------------------------------------------------

class TestStubs:
    def test_subscriber_count_is_zero_in_stage_a(self):
        th = TokenStreamHub()
        assert th.subscriber_count == 0

    def test_on_upstream_reconnect_clears_all_state(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))
        th.on_part_delta(_delta_props(delta="x"))
        th.on_part_updated(_updated_props("s2", "m2", "p2", type="reasoning"))
        assert th.live_parts and th._nontext_parts and th._pending
        assert th._total_live_bytes > 0
        th.on_upstream_reconnect()
        assert th.live_parts == {}
        assert not th._nontext_parts
        assert not th._disabled_parts
        assert th._pending == {}
        assert th._total_live_bytes == 0


# ---------------------------------------------------------------------------
# GlobalHub.publish() integration — token routing + no control-plane pollution
# ---------------------------------------------------------------------------

@pytest.fixture
async def bare_hub():
    """Bare GlobalHub; tears down background tasks."""
    h = _global_hub(client=None)
    try:
        yield h
    finally:
        await _close_hub(h)


class TestPublishIntegration:
    async def test_message_part_delta_routes_to_token_hub(self, bare_hub):
        """A message.part.delta upstream event reaches on_part_delta when
        a token hub is wired."""
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        # Start the part first.
        bare_hub.publish(make_global_event("/proj", "message.part.updated", {
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text", "text": "", "time": {},
            },
            "time": {},
        }))
        bare_hub.publish(make_global_event("/proj", "message.part.delta", {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "field": "text", "delta": "hi",
        }))
        key = ("s1", "m1", "p1")
        assert key in th.live_parts
        assert th.live_parts[key].chunks == ["hi"]
        assert th._pending[key].chunks == ["hi"]

    async def test_message_part_updated_routes_to_token_hub(self, bare_hub):
        """A message.part.updated upstream event reaches on_part_updated."""
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/proj", "message.part.updated", {
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text", "text": "", "time": {},
            },
            "time": {},
        }))
        assert ("s1", "m1", "p1") in th.live_parts

    async def test_token_routing_no_digest_pollution(self, bare_hub):
        """Token ingest must NOT add digest pollution beyond 修订六's
        single legitimate bump.

        4.12.0（修订六）：``message.part.updated`` now bumps the digest
        revision (completion-state visibility) — exactly ONE digest frame
        per debounce window. ``message.part.delta`` (per-chunk firehose)
        still contributes ZERO digest output."""
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/proj", "message.part.updated", {
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text", "text": "", "time": {},
            },
            "time": {},
        }))
        bare_hub.publish(make_global_event("/proj", "message.part.delta", {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "field": "text", "delta": "hi",
        }))
        bare_hub.flush()
        frames = await drain_queue(subscriber, timeout=0.1)
        # 修订六：updated 恰产一帧 digest；delta 零贡献（不重复、不污染）。
        assert len(frames) == 1, f"expected exactly 1 digest, got {frames!r}"
        assert b"session.digest" in frames[0]
        assert b'"messagesRevision"' in frames[0]

    async def test_message_part_delta_alone_produces_no_digest(self, bare_hub):
        """``message.part.delta`` (token stream only) must NOT touch the
        debounce window — Stage A invariant preserved into Stage B. The
        messageEventSeq bump lives on ``message.part.updated`` /
        ``message.part.removed``; deltas are real-time tokens that need
        no fingerprint signal."""
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/proj", "message.part.delta", {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "field": "text", "delta": "hi",
        }))
        bare_hub.flush()
        frames = await drain_queue(subscriber, timeout=0.1)
        assert frames == []

    async def test_no_token_hub_keeps_legacy_drop_behaviour(self, bare_hub):
        """Without a token hub, ``message.part.*`` events do not crash and
        do not route to any token accumulator.

        4.12.0（修订六）：``message.part.updated`` bumps the digest even
        with no token hub wired (helper runs outside the token route) —
        exactly one digest frame. ``message.part.delta`` alone remains a
        full drop (no digest, no state) — unchanged.
        """
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        # Crucially: NO set_token_hub() call.
        bare_hub.publish(make_global_event("/proj", "message.part.delta", {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "field": "text", "delta": "hi",
        }))
        bare_hub.publish(make_global_event("/proj", "message.part.updated", {
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text", "text": "", "time": {},
            },
            "time": {},
        }))
        bare_hub.flush()
        frames = await drain_queue(subscriber, timeout=0.1)
        # 修订六：delta 全 drop；updated 恰一帧 digest（不依赖 token hub）。
        assert len(frames) == 1, f"expected exactly 1 digest, got {frames!r}"
        assert b"session.digest" in frames[0]
        assert b'"messagesRevision"' in frames[0]

    async def test_control_plane_branches_untouched(self, bare_hub):
        """Sanity: the curated branches above the token-routing still
        fire when a token hub is wired (regression guard for the
        'do NOT touch control-plane' constraint)."""
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/proj", "question.asked", {
            "id": "q1", "sessionID": "s1",
        }))
        frames = await drain_queue(subscriber, timeout=0.1)
        assert len(frames) == 1
        event_name, data = parse_event(frames[0])
        assert event_name is None  # raw passthrough
        assert data["type"] == "question.asked"

    async def test_orphan_delta_via_publish_no_exception(self, bare_hub):
        """A delta arriving before its text-start must not crash publish
        (C3 silent-drop path)."""
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/proj", "message.part.delta", {
            "sessionID": "s1", "messageID": "m1", "partID": "p1",
            "field": "text", "delta": "orphan",
        }))
        assert th.orphan_deltas == 1
        assert th.live_parts == {}


# ---------------------------------------------------------------------------
# HubRegistry injection wiring (mirror set_token_hub pattern)
# ---------------------------------------------------------------------------

class TestRegistryInjection:
    async def test_set_token_hub_propagates_to_existing_global(self):
        from oc_slimapi.sse.hub import HubRegistry
        registry = _hub_registry(client=None)
        try:
            # Force-create the GlobalHub first.
            hub = registry.get_global()
            assert hub._token_hub is None
            th = TokenStreamHub()
            registry.set_token_hub(th)
            assert hub._token_hub is th
        finally:
            await registry.close()

    async def test_set_token_hub_propagates_to_lazily_created_global(self):
        """When set BEFORE the first get(), the hub picks it up on construction."""
        from oc_slimapi.sse.hub import HubRegistry
        registry = _hub_registry(client=None)
        try:
            th = TokenStreamHub()
            registry.set_token_hub(th)
            hub = registry.get_global()
            assert hub._token_hub is th
        finally:
            await registry.close()

    async def test_publish_routes_through_registry_wired_hub(self):
        from oc_slimapi.sse.hub import HubRegistry
        registry = _hub_registry(client=None)
        try:
            th = TokenStreamHub()
            registry.set_token_hub(th)
            hub = registry.get_global()
            hub.publish(make_global_event("/proj", "message.part.updated", {
                "sessionID": "s1",
                "part": {
                    "id": "p1", "messageID": "m1", "sessionID": "s1",
                    "type": "text", "text": "", "time": {},
                },
                "time": {},
            }))
            assert ("s1", "m1", "p1") in th.live_parts
        finally:
            await registry.close()


# ---------------------------------------------------------------------------
# rev-ogpt Stage B v0.6 fixes — CRITICAL 1/2/3 + MAJOR 4/5/6 hub-level tests.
#
# Wire-level / cross-component tests live in test_stage_b_part_revision.py;
# the cases here are direct hub-level invariants that fit the existing
# test_token_hub.py style.
# ---------------------------------------------------------------------------

class TestPartRevisionSemantics:
    """rev-ogpt CRITICAL 1 (Option B): per-FRAME revision, not per-event.

    ``_part_revisions[key]`` is bumped lazily in emit paths via
    ``_next_part_revision``. ``on_part_updated`` itself does NOT bump.
    Each emitted delta consumes the next strictly-increasing revision so a
    client using strict ``>`` never drops a frame.
    """

    def test_on_part_updated_does_not_bump(self):
        """``on_part_updated`` does NOT bump ``_part_revisions`` — bumps
        happen lazily in emit paths. After a text-start with no emit,
        the key is absent from ``_part_revisions``."""
        th = TokenStreamHub()
        # Text-start: creates LivePart, does NOT create _part_revisions entry.
        th.on_part_updated(_updated_props(text=""))
        assert ("s1", "m1", "p1") not in th._part_revisions
        # First emit (via _next_part_revision) produces revision 0.
        assert th._next_part_revision(("s1", "m1", "p1")) == 0
        # Subsequent emits produce strictly increasing revisions.
        assert th._next_part_revision(("s1", "m1", "p1")) == 1
        assert th._next_part_revision(("s1", "m1", "p1")) == 2

    def test_revision_param_is_ignored(self):
        """v0.6 §Q: ``part_revision`` parameter is IGNORED entirely; the
        token hub self-increments per-frame in emit paths (decoupled
        from GlobalHub's vulnerable cache)."""
        th = TokenStreamHub()
        # Parameter has no effect — _part_revisions stays empty (no emit).
        th.on_part_updated(_updated_props(text=""), part_revision=99)
        assert ("s1", "m1", "p1") not in th._part_revisions
        th.on_part_updated(_updated_props(text="more"), part_revision=None)
        assert ("s1", "m1", "p1") not in th._part_revisions
        th.on_part_updated(_updated_props(text="x"), part_revision=42)
        assert ("s1", "m1", "p1") not in th._part_revisions
        # First emit → 0 (regardless of the ignored params above).
        assert th._next_part_revision(("s1", "m1", "p1")) == 0

    def test_residual_delta_consumes_one_revision_and_finish_retires(self):
        """Native v4 text-end emits only the residual delta, then retires."""
        th = TokenStreamHub()
        captured_frames: list[bytes] = []

        class _SpySub:
            closed = False

            def put(self, frame):
                captured_frames.append(frame)
                return True

        spy = _SpySub()
        th._subs_by_sid.setdefault("s1", set()).add(spy)
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="abc"))
        captured_frames.clear()
        # text-end → finish_part emits residual delta (rev=0), no done marker.
        th.on_part_updated(_updated_props(text="final", end=1700))
        delta_frames = [f for f in captured_frames
                        if parse_event(f)[0] == "message.part.delta"]
        assert len(delta_frames) == 1
        d_rev = parse_event(delta_frames[0])[1]["partEventRevision"]
        assert d_rev == 0, f"residual delta should be rev=0, got {d_rev}"
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") in th._disabled_parts


class TestMessageRemovedRetiresState:
    """rev-ogpt CRITICAL 2: ``on_message_removed`` atomically retires
    every part for ``(sid, mid)`` and gates future ingest via
    ``_retired_messages``.
    """

    def test_retire_drops_live_pending_and_revisions(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text="seed"))
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="x"))
        key = ("s1", "m1", "p1")
        assert key in th.live_parts
        assert key in th._pending
        # Option B: pre-bump to simulate a frame emit (creates the entry
        # in _part_revisions so _retire_message has something to clear).
        th._next_part_revision(key)
        assert key in th._part_revisions
        assert th._total_live_bytes > 0
        assert th._total_pending_bytes > 0
        th.on_message_removed("s1", "m1")
        assert key not in th.live_parts
        assert key not in th._pending
        assert key not in th._part_revisions
        assert th._total_live_bytes == 0
        assert th._total_pending_bytes == 0

    def test_retire_other_messages_in_session_preserved(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        th.on_part_updated(_updated_props(sid="s1", mid="m2", pid="p1", text=""))
        th.on_message_removed("s1", "m1")
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m2", "p1") in th.live_parts

    def test_late_part_updated_after_retire_dropped(self):
        th = TokenStreamHub()
        th.on_message_removed("s1", "m1")
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") not in th._part_revisions

    def test_late_part_delta_after_retire_dropped(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        th.on_message_removed("s1", "m1")
        # Late delta — gated by _retired_messages.
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="late"))
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") not in th._pending


class TestPartRemovedRouting:
    """rev-ogpt MAJOR 4: ``on_part_removed`` retires the part; the
    GlobalHub.publish ``message.part.removed`` branch routes here."""

    def test_on_part_removed_drops_live_part(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        key = ("s1", "m1", "p1")
        th.on_part_removed("s1", "m1", "p1")
        assert key not in th.live_parts
        assert key in th._disabled_parts

    def test_on_part_removed_blocks_late_delta(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        th.on_part_removed("s1", "m1", "p1")
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="late"))
        assert ("s1", "m1", "p1") not in th._pending


class TestRevisionGating:
    """rev-ogpt MAJOR 5: ``_part_revisions`` is bumped only for accepted
    text parts. Non-text, malformed, and disabled-late events never
    create or consume a revision."""

    def test_non_text_does_not_create_revision(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(type="reasoning"))
        assert ("s1", "m1", "p1") not in th._part_revisions

    def test_malformed_missing_time_does_not_create_revision(self):
        th = TokenStreamHub()
        th.on_part_updated({
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text",  # no time dict
            },
        })
        assert ("s1", "m1", "p1") not in th._part_revisions

    def test_disabled_late_text_start_does_not_recreate_revision(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.drop_part(key)
        assert key not in th._part_revisions
        # Late text-start for disabled key — MAJOR 5: no revision recreated.
        th.on_part_updated(_updated_props(text=""))
        assert key not in th._part_revisions


class TestNativeV4Attach:
    """Native v4 attach registers without connection-private prefill."""

    def test_attach_registers_without_emitting_frames(self):
        th = TokenStreamHub()

        class _StateSpy:
            def __init__(self):
                self.frames = []
                self.closed = False

            def put(self, frame):
                self.frames.append(frame)
                return True

        spy = _StateSpy()
        th.attach_subscriber("s1", spy)
        assert spy in th._subs_by_sid.get("s1", set())
        assert spy.frames == []

    def test_closed_sub_not_registered(self):
        th = TokenStreamHub()
        class _ClosedSub:
            def __init__(self):
                self.frames = []
                self.closed = True  # prior disconnect

            def put(self, frame):
                self.frames.append(frame)
                return True

        sub = _ClosedSub()
        th.attach_subscriber("s1", sub)
        assert sub not in th._subs_by_sid.get("s1", set())
