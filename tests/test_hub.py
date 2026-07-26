"""Tests for the v2 curated SSE contract.

Covers:
* digest merges status + messageID into one debounced frame per session
* session.deleted produces a digest with deleted=true
* session.updated with info.time.archived emits the archived epoch-ms int (sticky)
* question/permission events are forwarded immediately (no debounce)
* text deltas / tool.* / message.part.* are dropped
* reconnect emits a resync frame
* sessions across multiple directories all flow into the digest stream
* subscribe() emits server.connected first
* HubRegistry shares one global hub regardless of directory key
* /slimapi/events route wires the SSE response correctly end-to-end
* T3: subscriber queue overflow clears the queue and emits resync + STOP
  (old frames are NOT delivered)
* T3: HubRegistry admission raises SubscriberCapacityError past the caps
* T3: HubRegistry.snapshot_metrics() matches the contract shape
"""

from __future__ import annotations

import asyncio
import json

import pytest

from oc_slimapi.config import Settings
from oc_slimapi.sse.hub import (
    GlobalHub,
    HubRegistry,
    STOP,
    Subscriber,
    SubscriberCapacityError,
    sse_frame,  # noqa: F401  (asserts the symbol still exists for events.py)
)


def make_global_event(
    directory: str,
    event_type: str,
    properties: dict | None = None,
    payload_id: str | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties}}."""
    payload: dict = {"type": event_type, "properties": properties or {}}
    if payload_id is not None:
        payload["id"] = payload_id
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
    loop = True
    while loop:
        try:
            item = await asyncio.wait_for(subscriber.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if item is None or item is sse_frame:
            continue
        frames.append(item)
    return frames


async def _close_hub(hub: GlobalHub) -> None:
    """Cancel + await every GlobalHub background task (including stop_after_grace).

    Tests that construct ``GlobalHub`` directly (bypassing HubRegistry) must
    call this on teardown. Otherwise the 30s grace task scheduled by
    ``unsubscribe()`` is still pending when the event loop closes, producing
    ``Task was destroyed but it is pending!`` on stderr.
    """
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


@pytest.fixture
async def hub():
    """Bare GlobalHub; always tears down background tasks (incl. stop_after_grace)."""
    h = GlobalHub(client=None)
    try:
        yield h
    finally:
        await _close_hub(h)


@pytest.fixture
async def fresh_hub(hub: GlobalHub):
    """GlobalHub with one manually-attached subscriber (no run()/subscribe() side effects)."""
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    return hub, subscriber


def _fresh_hub() -> tuple[GlobalHub, Subscriber]:
    """Sync helper for tests that fully own teardown themselves.

    Prefer the ``fresh_hub`` / ``hub`` fixtures — they cancel stop_after_grace.
    Kept for call sites that already wrap with ``await _close_hub(hub)``.
    """
    hub = GlobalHub(client=None)
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    return hub, subscriber


async def test_digest_merges_status_and_message_into_one_frame(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "busy",
    }))
    hub.publish(make_global_event("/proj", "message.updated", {
        "sessionID": "s1",
        "info": {"id": "msg_1", "time": {"updated": 1700000000000}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = [
        (event, data) for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    _, data = digests[0]
    assert data == {
        "sessionID": "s1",
        "directory": "/proj",
        "status": "busy",
        "messageID": "msg_1",
        "updatedAt": 1700000000000,
    }


async def test_digest_marks_deleted_true(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.deleted", {"sessionID": "s1"}))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = [
        (event, data) for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    _, data = digests[0]
    assert data["sessionID"] == "s1"
    assert data["deleted"] is True


async def test_question_event_is_forwarded_immediately(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "question.asked", {
        "id": "q1", "sessionID": "s1",
    }))
    # Without flush(), the question must already be on the queue.
    frames = await drain_queue(subscriber, timeout=0.1)
    assert len(frames) == 1
    event_name, data = parse_event(frames[0])
    assert event_name is None  # raw passthrough, no `event:` header
    assert data == {
        "directory": "/proj",
        "type": "question.asked",
        "properties": {"id": "q1", "sessionID": "s1"},
    }


async def test_permission_v2_events_are_forwarded_immediately(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "permission.v2.asked", {
        "id": "p1", "sessionID": "s1",
    }))
    frames = await drain_queue(subscriber, timeout=0.1)
    assert len(frames) == 1
    _, data = parse_event(frames[0])
    assert data["type"] == "permission.v2.asked"


async def test_message_part_delta_produces_no_frames(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "message.part.delta", {
        "sessionID": "s1", "messageID": "m1", "partID": "p1",
        "field": "text", "delta": "hi",
    }))
    hub.publish(make_global_event("/proj", "message.part.updated", {
        "sessionID": "s1", "partID": "p1",
    }))
    hub.publish(make_global_event("/proj", "tool.update", {"sessionID": "s1"}))
    # Abort session.error is filtered (G1); non-abort errors produce frames (see G1 tests).
    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber, timeout=0.1)
    assert frames == []


async def test_resync_emits_resync_frame_to_all_subscribers(fresh_hub):
    hub, subscriber = fresh_hub

    hub.resync_all()
    frames = await drain_queue(subscriber, timeout=0.1)
    assert len(frames) == 1
    event_name, data = parse_event(frames[0])
    assert event_name == "resync"
    assert data == {"reason": "reconnect_no_replay"}


async def test_multi_directory_sessions_each_emit_their_own_digest(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj-a", "session.status", {
        "sessionID": "s1", "status": "busy",
    }))
    hub.publish(make_global_event("/proj-b", "session.status", {
        "sessionID": "s2", "status": "idle",
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = {
        data["sessionID"]: data
        for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    }
    assert set(digests) == {"s1", "s2"}
    assert digests["s1"]["directory"] == "/proj-a"
    assert digests["s1"]["status"] == "busy"
    assert digests["s2"]["directory"] == "/proj-b"
    assert digests["s2"]["status"] == "idle"


async def test_subscribe_emits_server_connected_first(hub):
    subscriber = hub.subscribe()
    # First frame must be server.connected; no other frame may precede it.
    first = await asyncio.wait_for(subscriber.queue.get(), timeout=0.2)
    event_name, data = parse_event(first)
    assert event_name == "server.connected"
    assert data == {}


async def test_message_appended_updates_message_id(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "message.appended", {
        "sessionID": "s1",
        "info": {"id": "msg_2", "time": {"created": 1700000001000}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    _, data = parse_event(frames[0])
    assert data["messageID"] == "msg_2"
    assert data["updatedAt"] == 1700000001000


async def test_deleted_flag_persists_across_subsequent_status_changes(fresh_hub):
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.deleted", {"sessionID": "s1"}))
    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "idle",
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    _, data = parse_event(frames[0])
    assert data["deleted"] is True
    assert data["status"] == "idle"


async def test_hub_registry_shares_one_global_hub_across_directories():
    registry = HubRegistry(client=None)
    try:
        h1 = registry.get("/dir-a")
        h2 = registry.get("/dir-b")
        h3 = registry.get_global()
        assert h1 is h2 is h3
    finally:
        await registry.close()


async def test_close_is_safe_when_no_hub_was_created():
    registry = HubRegistry(client=None)
    # Should not raise.
    await registry.close()


class _MockHubs:
    def __init__(self, hub: GlobalHub):
        self._hub = hub

    def get_global(self) -> GlobalHub:
        return self._hub

    def subscribe(self) -> Subscriber:
        # Direct delegation: this mock bypasses admission control so the
        # events-route tests can exercise the SSE generator without a
        # fully-configured registry.
        return self._hub.subscribe()

    def unsubscribe(self, subscriber: Subscriber) -> None:
        # Mirror HubRegistry.unsubscribe signature so events.py teardown
        # (registry-level) works under this fast-path mock. Byte-ledger /
        # total_subscribers accounting is not exercised here — see the
        # real-HubRegistry integration tests below.
        self._hub.unsubscribe(subscriber)


class _MockRequest:
    def __init__(self, hub: GlobalHub, headers: dict[str, str] | None = None):
        self.app = type("App", (), {})()
        self.app.state = type("State", (), {"hubs": _MockHubs(hub)})()
        self.headers = headers or {}


async def _events_route_chunks(hub: GlobalHub, headers: dict[str, str] | None = None):
    from oc_slimapi.routes.events import events

    request = _MockRequest(hub, headers)
    response = await events(request)
    assert response.media_type == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache, no-transform"
    assert response.headers["X-Accel-Buffering"] == "no"
    return response


async def test_events_route_streams_server_connected_first(hub):
    response = await _events_route_chunks(hub)
    iterator = response.body_iterator
    chunks: list[bytes] = []
    try:
        # Pull exactly two frames: server.connected then any queue item.
        first = await asyncio.wait_for(anext(iterator), timeout=0.5)
        chunks.append(first)
    except StopAsyncIteration:
        pass
    finally:
        # aclose() runs generate()'s finally → hub.unsubscribe → stop_after_grace.
        # The hub fixture teardown awaits that grace task so the loop never
        # destroys a pending stop_after_grace coroutine.
        await iterator.aclose()
    assert chunks, "expected at least one frame"
    assert b"event: server.connected" in chunks[0]


async def test_events_route_honours_last_event_id_with_resync(hub):
    response = await _events_route_chunks(hub, headers={"last-event-id": "anything"})
    iterator = response.body_iterator
    first = b""
    try:
        first = await asyncio.wait_for(anext(iterator), timeout=0.5)
    except StopAsyncIteration:
        pass
    finally:
        await iterator.aclose()
    assert b"event: resync" in first
    assert b"reconnect_no_replay" in first


# ---------------------------------------------------------------------------
# Lane-H / Gap 1: digest archived field (contract §3)
# ---------------------------------------------------------------------------

async def test_session_updated_archived_emits_timestamp(fresh_hub):
    """Contract §3 (locked to timestamp option per client fix-11): the
    digest's ``archived`` field is the epoch-ms int from
    ``info.time.archived`` (client types it as ``Long?``)."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"archived": 1700000000000}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = [
        (event, data) for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    _, data = digests[0]
    assert data["archived"] == 1700000000000
    assert isinstance(data["archived"], int)
    assert data["sessionID"] == "s1"


async def test_archived_is_sticky_across_subsequent_session_events(fresh_hub):
    """Once archived is observed in a debounce window it stays set, mirroring
    the existing deleted-stickiness contract (Lane-H spec §3). A subsequent
    event of a *different* type must not un-set the timestamp."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"archived": 1700000000000}},
    }))
    # A subsequent session.status in the SAME window must not un-set archived.
    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "idle",
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    _, data = parse_event(frames[0])
    assert data["archived"] == 1700000000000
    assert data["status"] == "idle"


async def test_archived_sticky_across_subsequent_session_updated_without_archived(fresh_hub):
    """A second session.updated in the SAME debounce window that does NOT
    carry ``info.time.archived`` must not clear the previously-observed
    timestamp (archived is permanent — clients hide the session on first
    sight and would be confused by the field flipping back)."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"archived": 1700000000000}},
    }))
    # Same event type, but no archived marker — only an unrelated time field.
    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"updated": 1700000000001}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    _, data = parse_event(frames[0])
    assert data["archived"] == 1700000000000  # sticky: retained untouched


async def test_session_updated_without_archived_omits_field(fresh_hub):
    """A session.updated that does NOT carry info.time.archived must not
    synthesize an archived field — only the explicit marker sets it. The
    field is omitted entirely (not archived: false, not archived: 0)."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"updated": 1700000000000}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    assert frames, "expected at least one digest frame"
    _, data = parse_event(frames[0])
    assert "archived" not in data


async def test_archived_zero_is_emitted(fresh_hub):
    """archived=0 (epoch-ms) must be written — ``if archived_val:`` would
    drop it; the correct guard is ``is not None`` / isinstance int."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"archived": 0}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    assert digests[0]["archived"] == 0
    assert isinstance(digests[0]["archived"], int)


async def test_archived_bool_is_rejected(fresh_hub):
    """``bool`` is a subclass of int — a spurious ``archived: true`` from
    upstream must NOT be coerced to epoch-ms 1 and emitted to clients.
    Only real epoch-ms ints (including 0) pass through; True/False are
    silently dropped and the field is omitted from the digest."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"archived": True}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    # True must NOT be stored as epoch-ms 1.
    assert "archived" not in digests[0]

    # Also verify False is rejected (not stored as 0).
    hub.publish(make_global_event("/proj", "session.updated", {
        "sessionID": "s2",
        "info": {"time": {"archived": False}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "s2"
    assert "archived" not in digests[0]


async def test_archived_zero_passes_while_bool_rejected(fresh_hub):
    """Combined regression guard: archived=0 (valid epoch-ms) passes through,
    archived=True/False are rejected — covers the full matrix in one shot so
    the two guards cannot drift independently."""
    hub, subscriber = fresh_hub

    for sid, val in [("s_zero", 0), ("s_true", True), ("s_false", False)]:
        hub.publish(make_global_event("/proj", "session.updated", {
            "sessionID": sid,
            "info": {"time": {"archived": val}},
        }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = {
        data["sessionID"]: data
        for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    }
    assert len(digests) == 3
    # epoch 0 kept.
    assert digests["s_zero"].get("archived") == 0
    assert isinstance(digests["s_zero"]["archived"], int)
    # bools dropped.
    assert "archived" not in digests["s_true"]
    assert "archived" not in digests["s_false"]


async def test_immediate_flush_sid_leaves_other_pending_intact(fresh_hub):
    """G1-A session.error immediate flush must only pop the target sid;
    other sids' pending digests stay until the debounce flush."""
    hub, subscriber = fresh_hub

    # Seed a pending digest for s2 (not yet flushed).
    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s2", "status": "idle",
    }))
    assert "s2" in hub.pending

    # session.error on s1 → immediate flush_sid("s1") only.
    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))

    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "s1"
    assert digests[0]["lastError"]["name"] == "UnknownError"
    # s2 still pending — not prematurely emitted.
    assert "s2" in hub.pending
    assert "s1" not in hub.pending

    hub.flush()
    frames2 = await drain_queue(subscriber)
    digests2 = [
        data for event, data in (parse_event(f) for f in frames2)
        if event == "session.digest"
    ]
    assert len(digests2) == 1
    assert digests2[0]["sessionID"] == "s2"
    assert digests2[0]["status"] == "idle"
    assert hub.pending == {}


async def test_busy_clear_sticky_flush_sid_leaves_other_pending(fresh_hub):
    """session.status=busy clearing sticky lastError flushes only that sid."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain_queue(subscriber)  # consume G1-A immediate digest

    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s2", "status": "idle",
    }))
    assert "s2" in hub.pending

    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "busy",
    }))
    frames = await drain_queue(subscriber)
    clear_digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest" and data.get("sessionID") == "s1"
    ]
    assert any(d.get("lastError") is None for d in clear_digests)
    assert "s2" in hub.pending
    assert "s1" not in hub.pending


async def test_extract_session_id_ignores_payload_event_id(fresh_hub):
    """payload.id is the GlobalBus *event* id — must not become sessionID.

    A session.status without properties.sessionID / info.sessionID / info.id
    (for session.*) must not hang a digest under the event UUID.
    """
    hub, subscriber = fresh_hub

    hub.publish(make_global_event(
        "/proj",
        "session.status",
        {"status": "busy"},  # no sessionID
        payload_id="evt_global_bus_uuid_xyz",
    ))
    hub.flush()

    frames = await drain_queue(subscriber, timeout=0.1)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert digests == []
    assert "evt_global_bus_uuid_xyz" not in hub.pending
    assert hub.pending == {}


async def test_extract_session_id_uses_info_id_for_session_events(fresh_hub):
    """session.* may carry the session row id at properties.info.id."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.updated", {
        "info": {"id": "ses_from_info", "time": {"archived": 1700000000000}},
    }))
    hub.flush()

    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "ses_from_info"
    assert digests[0]["archived"] == 1700000000000


# ---------------------------------------------------------------------------
# Lane-H / Gap 3: Subscriber overflow — immediate clear + resync + STOP
# (contract §6)
# ---------------------------------------------------------------------------

async def test_subscriber_overflow_clears_queue_and_emits_resync_then_stop():
    """Slow client: queue full → immediate disconnect.

    The previously-queued frames MUST be cleared (not drained to completion)
    so the slow client does not keep receiving stale data after the sidecar
    has decided it is too far behind.
    """
    subscriber = Subscriber(
        queue_items=2,
        buffer_bytes=4096,
        max_frame_bytes=4096,
    )
    frame_a = sse_frame({"seq": "aaaa"}, event="test")
    frame_b = sse_frame({"seq": "bbbb"}, event="test")
    overflow = sse_frame({"seq": "cccc"}, event="test")

    # Fill to capacity (queue_items=2).
    subscriber.put(frame_a)
    subscriber.put(frame_b)
    assert not subscriber.closed
    assert subscriber.queue.qsize() == 2

    # Third put triggers the immediate-disconnect path.
    subscriber.put(overflow)
    assert subscriber.closed is True
    assert subscriber.forced_disconnects == 1
    assert subscriber.queued_bytes == 0

    # Drain remaining items: ONLY resync + STOP.
    items = []
    while True:
        try:
            item = subscriber.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        items.append(item)
    assert len(items) == 2  # resync frame + STOP
    assert items[-1] is STOP

    resync = items[0]
    assert b"event: resync" in resync
    assert b"subscriber_backpressure" in resync

    # Critical guarantee (contract §6): old frames NOT still in the queue.
    payload = b"".join(item for item in items[:-1] if isinstance(item, (bytes, bytearray)))
    assert b'"seq": "aaaa"' not in payload
    assert b'"seq": "bbbb"' not in payload
    assert b'"seq": "cccc"' not in payload

    # Subsequent puts are silently dropped (closed=True).
    extra = sse_frame({"seq": "dddd"}, event="test")
    subscriber.put(extra)
    assert subscriber.queue.qsize() == 0  # nothing added after drain


async def test_subscriber_oversized_frame_is_dropped_not_enqueued():
    """A single frame larger than sse_max_frame_bytes is dropped (counter
    bump) and does not occupy the buffer."""
    subscriber = Subscriber(
        queue_items=4,
        buffer_bytes=4096,
        max_frame_bytes=64,
    )
    big = sse_frame({"payload": "x" * 200}, event="test")  # > 64 bytes
    subscriber.put(big)
    assert subscriber.dropped_frames == 1
    assert subscriber.queue.qsize() == 0
    assert not subscriber.closed


async def test_subscriber_buffer_bytes_overflow_triggers_disconnect():
    """Even when the queue has item-capacity left, crossing sse_buffer_bytes
    forces an immediate disconnect (contract §6 byte budget)."""
    subscriber = Subscriber(
        queue_items=64,        # plenty of item slack
        buffer_bytes=32,       # but tiny byte budget
        max_frame_bytes=4096,
    )
    # First frame (~30 bytes) fits; second would push past buffer_bytes=32.
    small = sse_frame({}, event="test")  # ~22 bytes
    subscriber.put(small)
    assert not subscriber.closed
    subscriber.put(small)
    assert subscriber.closed is True
    assert subscriber.forced_disconnects == 1


# ---------------------------------------------------------------------------
# Lane-H / Gap 3: HubRegistry admission (contract §6 / §7)
# ---------------------------------------------------------------------------

async def test_registry_admission_raises_when_per_directory_cap_exceeded():
    registry = HubRegistry(
        client=None,
        max_subscribers_per_directory=2,
        max_total_subscribers=10,
    )
    try:
        s1 = registry.subscribe()
        s2 = registry.subscribe()
        assert s1.id.startswith("sub_")
        assert s2.id != s1.id  # unique ephemeral ids
        with pytest.raises(SubscriberCapacityError) as exc_info:
            registry.subscribe()
        err = exc_info.value
        assert err.code == "sse_subscriber_limit_directory"
        assert err.limit == 2
        assert err.current == 2
        assert registry.rejected_total == 1
    finally:
        await registry.close()


async def test_registry_admission_raises_when_total_cap_exceeded():
    registry = HubRegistry(
        client=None,
        max_subscribers_per_directory=10,
        max_total_subscribers=2,
    )
    try:
        registry.subscribe()
        registry.subscribe()
        with pytest.raises(SubscriberCapacityError) as exc_info:
            registry.subscribe()
        assert exc_info.value.code == "sse_subscriber_limit_total"
        assert exc_info.value.limit == 2
        assert registry.rejected_total == 1
    finally:
        await registry.close()


async def test_registry_unsubscribe_is_idempotent():
    """A double-unsubscribe must not under-flow total_subscribers (contract
    §6 admission would otherwise go negative / over-admit)."""
    registry = HubRegistry(
        client=None,
        max_subscribers_per_directory=2,
        max_total_subscribers=10,
    )
    try:
        s1 = registry.subscribe()
        registry.unsubscribe(s1)
        registry.unsubscribe(s1)  # duplicate — must be a no-op
        assert registry.total_subscribers == 0
        # The freed slot must be reusable for two fresh subscribers.
        registry.subscribe()
        registry.subscribe()
        assert registry.total_subscribers == 2
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# Lane-H / Gap 3: HubRegistry.snapshot_metrics shape (contract §2 / §6)
# ---------------------------------------------------------------------------

async def test_snapshot_metrics_matches_contract_shape():
    registry = HubRegistry(
        client=None,
        max_subscribers_per_directory=8,
        max_total_subscribers=16,
    )
    try:
        s1 = registry.subscribe()
        snap = registry.snapshot_metrics()
        # Top level
        assert set(snap) == {"sse", "skeleton"}
        # SSE subtree
        sse = snap["sse"]
        assert set(sse) == {"subscribers", "hubs", "clients"}
        # subscribers sub-object
        assert set(sse["subscribers"]) == {"current", "limit", "rejectedTotal"}
        assert sse["subscribers"]["current"] == 1
        assert sse["subscribers"]["limit"] == 16
        assert sse["subscribers"]["rejectedTotal"] == 0
        # hubs sub-array
        assert len(sse["hubs"]) == 1
        hub_entry = sse["hubs"][0]
        assert set(hub_entry) == {
            "subscribers", "upstreamConnected",
            "upstreamEventsTotal", "emittedFramesTotal", "reconnectsTotal",
        }
        assert hub_entry["subscribers"] == 1
        assert hub_entry["upstreamConnected"] is False
        # Welcome frame is per-subscriber, not fanned out from publish/flush;
        # the counters track fan-out only, so they read zero immediately
        # after subscribe().
        assert hub_entry["upstreamEventsTotal"] == 0
        assert hub_entry["emittedFramesTotal"] == 0
        assert hub_entry["reconnectsTotal"] == 0
        # clients sub-array
        assert len(sse["clients"]) == 1
        client_entry = sse["clients"][0]
        assert set(client_entry) == {
            "subscriberId", "queueItems", "bufferBytes",
            "droppedFramesTotal", "forcedDisconnectsTotal",
        }
        assert client_entry["subscriberId"] == s1.id
        # Welcome frame is sitting in the queue waiting for the SSE generator.
        welcome = sse_frame({}, event="server.connected")
        assert client_entry["queueItems"] == 1
        assert client_entry["bufferBytes"] == len(welcome)
        assert client_entry["droppedFramesTotal"] == 0
        assert client_entry["forcedDisconnectsTotal"] == 0
        # Skeleton subtree (no transform pool wired in this test)
        skel = snap["skeleton"]
        assert set(skel) == {"activeTransforms", "waitingTransforms", "cacheEnabled"}
        assert skel["cacheEnabled"] is False
        assert skel["activeTransforms"] == 0
        assert skel["waitingTransforms"] == 0
    finally:
        await registry.close()


async def test_snapshot_metrics_counts_rejects_and_current_subscribers():
    """rejectedTotal accumulates across admission denials; current tracks
    live subscribers only."""
    registry = HubRegistry(
        client=None,
        max_subscribers_per_directory=1,
        max_total_subscribers=10,
    )
    try:
        s1 = registry.subscribe()
        with pytest.raises(SubscriberCapacityError):
            registry.subscribe()  # rejected: per-directory cap hit
        registry.unsubscribe(s1)
        snap = registry.snapshot_metrics()
        assert snap["sse"]["subscribers"]["current"] == 0
        assert snap["sse"]["subscribers"]["rejectedTotal"] == 1
    finally:
        await registry.close()


async def test_publish_increments_upstream_events_and_emitted_frames_counters(fresh_hub):
    """publish() bumps upstream_events_total per event; flush() bumps
    emitted_frames_total by len(subscribers) per fanned-out frame."""
    hub, subscriber = fresh_hub
    hub.publish(make_global_event("/proj", "question.asked", {
        "id": "q1", "sessionID": "s1",
    }))
    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "busy",
    }))
    hub.flush()
    # 2 upstream events observed.
    assert hub.upstream_events_total == 2
    # 1 question frame fanned out to 1 subscriber + 1 digest frame → 2 emits.
    assert hub.emitted_frames_total == 2
    assert hub.reconnects_total == 0


def test_sanitize_strips_unix_paths():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("open(/home/bob/secret.txt) failed", None) == "open(<path>) failed"


def test_sanitize_strips_windows_paths():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("load C:\\Users\\bob\\file.txt failed", None) == "load <path> failed"


def test_sanitize_strips_stack_frames():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("boom at app.ts:10:5", None) == "boom"
    assert _sanitize_error_message("err at module.js:42", None) == "err"


def test_sanitize_strips_secrets():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("token=abc123-xyz leaked", None) == "<redacted> leaked"
    assert _sanitize_error_message('Authorization: Bearer abc.def', None) == "<redacted>"


def test_sanitize_takes_first_line():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("main error\n  at a:1:1\n  at b:2:2", None) == "main error"


def test_sanitize_missing_message_uses_fallback_name():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message(None, "UnknownError") == "UnknownError"
    assert _sanitize_error_message("", "UnknownError") == "UnknownError"


def test_sanitize_missing_message_and_name():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message(None, None) == "(no detail)"


def test_sanitize_truncates_long():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert len(_sanitize_error_message("x" * 600, None)) == 512


def test_sanitize_strips_access_token():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("auth failed access_token=eyJhbGci.xyz", None) == "auth failed <redacted>"


def test_sanitize_strips_refresh_token():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("refresh_token=rt_123-abc expired", None) == "<redacted> expired"


def test_sanitize_strips_client_secret():
    from oc_slimapi.sse.hub import _sanitize_error_message
    assert _sanitize_error_message("load client_secret=topsecret-xyz done", None) == "load <redacted> done"


# ---------------------------------------------------------------------------
# G1: session.error → digest lastError / session-less frame / sticky / clear
# ---------------------------------------------------------------------------

async def test_g1_a_immediate_flush_with_last_error(fresh_hub):
    """session.error with sid (non-abort) → immediate digest with lastError."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {
            "name": "UnknownError",
            "data": {"message": "boom at app.ts:1:1"},
        },
    }))
    # No hub.flush() — publish must immediate-flush for G1-A.
    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert any(
        d.get("lastError", {}).get("name") == "UnknownError" for d in digests
    )
    # message desensitized (stack frame stripped)
    assert all(
        "app.ts" not in d.get("lastError", {}).get("message", "") for d in digests
    )
    le = next(d["lastError"] for d in digests if "lastError" in d)
    assert le["message"] == "boom"
    assert isinstance(le.get("at"), int)


async def test_g1_b_session_less_frame(fresh_hub):
    """session.error without sid → immediate session.error frame (not digest)."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "error": {
            "name": "UnknownError",
            "data": {"message": "plugin load failed"},
        },
    }))
    frames = await drain_queue(subscriber)
    err_frames = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.error"
    ]
    assert len(err_frames) == 1
    assert err_frames[0]["name"] == "UnknownError"
    assert err_frames[0]["message"] == "plugin load failed"
    assert err_frames[0].get("directory") == "/proj"
    assert isinstance(err_frames[0].get("at"), int)


async def test_g1_abort_filtered(fresh_hub):
    """MessageAbortedError → no digest lastError and no session.error frame."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}},
    }))
    frames = await drain_queue(subscriber)
    parsed = [parse_event(f) for f in frames]
    assert not any(
        event == "session.digest" and "lastError" in data
        for event, data in parsed
    )
    assert not any(event == "session.error" for event, _ in parsed)


async def test_g1_sticky_across_windows(fresh_hub):
    """lastError sticks across debounce windows via sticky_last_error."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain_queue(subscriber)  # clear immediate digest

    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "idle",
    }))
    hub.flush()
    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest" and data.get("sessionID") == "s1"
    ]
    assert any(
        d.get("lastError", {}).get("name") == "UnknownError" for d in digests
    )


async def test_g1_clear_on_busy(fresh_hub):
    """session.status busy clears sticky lastError with explicit null frame."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain_queue(subscriber)

    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "busy",
    }))
    frames = await drain_queue(subscriber)
    clear_digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
        and data.get("sessionID") == "s1"
        and "lastError" in data
    ]
    assert any(d["lastError"] is None for d in clear_digests)

    # Subsequent status must not carry lastError.
    hub.publish(make_global_event("/proj", "session.status", {
        "sessionID": "s1", "status": "idle",
    }))
    hub.flush()
    frames2 = await drain_queue(subscriber)
    digests2 = [
        data for event, data in (parse_event(f) for f in frames2)
        if event == "session.digest" and data.get("sessionID") == "s1"
    ]
    assert digests2, "expected a digest for the idle status"
    assert all("lastError" not in d for d in digests2)


async def test_g1_deleted_clears(fresh_hub):
    """session.deleted pops sticky; digest omits lastError entirely."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain_queue(subscriber)

    hub.publish(make_global_event("/proj", "session.deleted", {"sessionID": "s1"}))
    hub.flush()
    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest" and data.get("sessionID") == "s1"
    ]
    assert digests, "expected a deleted digest"
    assert all("lastError" not in d for d in digests)
    assert all(d.get("deleted") is True for d in digests)


# ---------------------------------------------------------------------------
# SSE lifecycle regressions (teardown registry slot / queued_bytes ack / G1 name)
# ---------------------------------------------------------------------------

async def test_events_teardown_releases_registry_slot():
    """events.py must call HubRegistry.unsubscribe on generator teardown.

    Subscribe goes through HubRegistry (total_subscribers += 1); teardown that
    only hits GlobalHub.unsubscribe would leak the registry counter and
    permanently 503 once max_total_subscribers is reached. Cycle connect /
    disconnect N times and assert the counter returns to zero each round.
    """
    from oc_slimapi.routes.events import events

    registry = HubRegistry(
        client=None,
        max_subscribers_per_directory=10,
        max_total_subscribers=10,
    )
    try:
        for _ in range(5):
            assert registry.total_subscribers == 0
            request = type("Request", (), {})()
            request.app = type("App", (), {})()
            request.app.state = type("State", (), {"hubs": registry})()
            request.headers = {}
            response = await events(request)
            assert response.media_type == "text/event-stream"
            iterator = response.body_iterator
            try:
                first = await asyncio.wait_for(anext(iterator), timeout=0.5)
                assert b"event: server.connected" in first
                # Slot held while the generator is live.
                assert registry.total_subscribers == 1
            finally:
                await iterator.aclose()
            # aclose → generate() finally → registry.unsubscribe → counter 0.
            assert registry.total_subscribers == 0
    finally:
        await registry.close()


async def test_subscriber_queued_bytes_decrements_on_consume():
    """queued_bytes must track *currently queued* bytes, not lifetime total.

    put() increments on enqueue; ack() (called by events.py after queue.get)
    must decrement by the same size. Without ack, a healthy consumer that
    drains frames still hits buffer_bytes and is false-positive
    subscriber_backpressure-disconnected.
    """
    # Keep queue_items high so overflow is driven by buffer_bytes only.
    buffer = 64 * 1024  # 64 KiB
    queue_items = 1024
    subscriber = Subscriber(
        queue_items=queue_items,
        buffer_bytes=buffer,
        max_frame_bytes=buffer,
    )
    # Frames of known size so we can assert exact ledger math.
    frame = sse_frame({"payload": "x" * 200}, event="test")
    size = len(frame)
    # Cap by both budgets so neither queue_items nor buffer_bytes overflows.
    n = min(buffer // size, queue_items)
    assert n >= 2
    assert n * size <= buffer

    for _ in range(n):
        subscriber.put(frame)
    assert not subscriber.closed
    assert subscriber.queued_bytes == n * size
    assert subscriber.queue.qsize() == n

    # Consume + ack every frame → ledger returns to 0.
    for _ in range(n):
        item = subscriber.queue.get_nowait()
        subscriber.ack(item)
    assert subscriber.queued_bytes == 0
    assert subscriber.queue.qsize() == 0

    # Same volume again must NOT overflow (old bug: second fill would trip
    # buffer_bytes because queued_bytes never decremented).
    for _ in range(n):
        subscriber.put(frame)
    assert not subscriber.closed
    assert subscriber.queued_bytes == n * size
    assert subscriber.forced_disconnects == 0

    # Contrast: without ack the second fill overflows once cumulative
    # (stale) queued_bytes + new frames exceed buffer_bytes.
    no_ack = Subscriber(
        queue_items=queue_items,
        buffer_bytes=buffer,
        max_frame_bytes=buffer,
    )
    for _ in range(n):
        no_ack.put(frame)
    for _ in range(n):
        no_ack.queue.get_nowait()  # drain without ack — old events.py path
    assert no_ack.queued_bytes == n * size  # ledger stuck high
    # Next put: real queue is empty so queue_items would allow it, but
    # queued_bytes + size > buffer → overflow (the false-positive path).
    assert no_ack.queued_bytes + size > buffer
    no_ack.put(frame)
    assert no_ack.closed is True
    assert no_ack.forced_disconnects == 1


async def test_g1_publish_non_string_error_name_does_not_crash(fresh_hub):
    """Non-str error.name must not TypeError out of publish (would resync hub).

    Upstream may send name as dict/int; ``(name or "")[:128]`` on a truthy
    non-str raises TypeError. Coerce to None so sanitize + slice stay safe.
    """
    hub, subscriber = fresh_hub

    # Must not raise.
    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {
            "name": {"weird": True},
            "data": {"message": "x"},
        },
    }))

    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest" and data.get("sessionID") == "s1"
    ]
    # Coerced name → "" in lastError; message still sanitized from data.
    assert digests, "expected an immediate G1-A digest despite non-str name"
    le = digests[0].get("lastError")
    assert le is not None
    assert le["name"] == ""  # (None or "")[:128]
    assert le["message"] == "x"
    assert isinstance(le.get("at"), int)

    # Session-less path likewise must not crash.
    hub.publish(make_global_event("/proj", "session.error", {
        "error": {
            "name": 42,
            "data": {"message": "y"},
        },
    }))
    frames2 = await drain_queue(subscriber)
    err_frames = [
        data for event, data in (parse_event(f) for f in frames2)
        if event == "session.error"
    ]
    assert len(err_frames) == 1
    assert err_frames[0]["name"] == ""
    assert err_frames[0]["message"] == "y"


# ---------------------------------------------------------------------------
# Config: sse_queue_items must be >= 2 (overflow enqueues resync + STOP)
# ---------------------------------------------------------------------------

def test_settings_rejects_sse_queue_items_of_one():
    """queue_items=1 cannot hold both resync and STOP after overflow clear.

    Settings.validate() must reject this so the SSE backpressure contract
    (clear + resync + STOP) holds for every legal configuration.
    """
    settings = Settings(sse_queue_items=1)
    with pytest.raises(RuntimeError, match=r">= 2|resync \+ STOP"):
        settings.validate()


def test_settings_accepts_sse_queue_items_of_two():
    """Minimum legal queue size is 2 (resync + STOP after clear)."""
    settings = Settings(sse_queue_items=2)
    settings.validate()  # must not raise


# ---------------------------------------------------------------------------
# v6 §3.5: Subscriber.put returns bool — True iff the frame actually landed
# on the queue; False on closed / oversized-drop / overflow-self-resync /
# STOP-QueueFull. Byte ledger and overflow behaviour are unchanged.
# ---------------------------------------------------------------------------

def test_subscriber_put_returns_true_on_normal_enqueue():
    """Normal enqueue returns True; queued_bytes still increments."""
    subscriber = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=4096)
    frame = sse_frame({"x": 1}, event="test")
    assert subscriber.put(frame) is True
    assert subscriber.queue.qsize() == 1
    assert subscriber.queued_bytes == len(frame)
    assert not subscriber.closed


def test_subscriber_put_returns_false_when_closed():
    """Once a subscriber is closed (overflow path), subsequent puts drop and
    return False; the queue stays empty of the dropped frame."""
    subscriber = Subscriber(queue_items=2, buffer_bytes=4096, max_frame_bytes=4096)
    # Force-closed without going through the overflow enqueue (avoid STOP).
    subscriber.closed = True
    assert subscriber.put(sse_frame({"x": 1}, event="test")) is False
    assert subscriber.queue.qsize() == 0


def test_subscriber_put_oversized_frame_returns_false():
    """Frame > max_frame_bytes: dropped, counter bumped, no enqueue, False."""
    subscriber = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=32)
    big = sse_frame({"payload": "x" * 200}, event="test")
    assert len(big) > 32
    assert subscriber.put(big) is False
    assert subscriber.dropped_frames == 1
    assert subscriber.queue.qsize() == 0
    assert not subscriber.closed


def test_subscriber_put_overflow_returns_false_and_emits_resync_stop():
    """The overflow path returns False (the original frame was NOT enqueued);
    the self-produced resync + STOP are on the queue but are not counted
    by the new return value (caller shouldn't see ``True`` for them)."""
    subscriber = Subscriber(
        queue_items=2, buffer_bytes=4096, max_frame_bytes=4096,
    )
    # Fill to capacity.
    assert subscriber.put(sse_frame({"i": 1}, event="test")) is True
    assert subscriber.put(sse_frame({"i": 2}, event="test")) is True
    # Third put triggers the overflow path.
    assert subscriber.put(sse_frame({"i": 3}, event="test")) is False
    assert subscriber.closed is True
    # resync + STOP present, original overflow frame NOT on the queue.
    assert subscriber.queue.qsize() == 2


def test_subscriber_put_stop_sentinel_returns_true_when_enqueued():
    """STOP enqueued successfully → True; same byte ledger rules as before."""
    subscriber = Subscriber(queue_items=4, buffer_bytes=4096, max_frame_bytes=4096)
    assert subscriber.put(STOP) is True
    # STOP must NOT be counted in queued_bytes (caller invariant: ack(STOP) is
    # a no-op and put never adds to the byte ledger for STOP).
    assert subscriber.queued_bytes == 0
    assert subscriber.queue.qsize() == 1


def test_subscriber_put_stop_sentinel_returns_false_on_queue_full():
    """STOP can still fail to enqueue when the queue is full → False.

    The pre-v6 implementation suppressed QueueFull and returned None; v6
    makes that explicit so callers (e.g. notify_reconfigured) do not
    double-count a dropped STOP as a real emit.
    """
    subscriber = Subscriber(
        queue_items=1, buffer_bytes=4096, max_frame_bytes=4096,
    )
    # One item fills the queue; STOP is also bounded by queue_items.
    assert subscriber.put(sse_frame({"i": 1}, event="test")) is True
    assert subscriber.put(STOP) is False
    assert not subscriber.closed  # the original frame is still queued


# ---------------------------------------------------------------------------
# v6 §3.1 + §3.2: GlobalHub.notify_reconfigured pushes a
# ``server.reconfigured`` frame per active subscriber; HubRegistry variant
# does not lazily create a hub when nobody is listening.
# ---------------------------------------------------------------------------

async def test_notify_reconfigured_pushes_frame_to_active_subscribers(fresh_hub):
    """Each active subscriber gets one ``server.reconfigured`` frame with the
    declared ``reason`` and an epoch-ms ``at``; the count is the number of
    subscribers that actually received it (put returned True)."""
    hub, subscriber = fresh_hub

    before = hub.emitted_frames_total
    emitted = hub.notify_reconfigured("discovery_changed")
    assert emitted == 1

    frame = await asyncio.wait_for(subscriber.queue.get(), timeout=0.2)
    event_name, data = parse_event(frame)
    assert event_name == "server.reconfigured"
    assert data["reason"] == "discovery_changed"
    assert isinstance(data["at"], int)
    # Counter increments by the number of *successfully* emitted frames.
    assert hub.emitted_frames_total == before + 1


async def test_notify_reconfigured_returns_zero_when_no_subscribers(fresh_hub):
    """Empty subscriber set → 0 emitted, counter untouched, no exception."""
    hub, _ = fresh_hub
    # Remove the only subscriber so the hub is empty.
    hub.subscribers.clear()
    assert hub.notify_reconfigured("discovery_changed") == 0
    assert hub.emitted_frames_total == 0


async def test_hub_registry_notify_reconfigured_if_active_no_hub_is_noop():
    """If no hub has been created yet, the registry must NOT lazily spin one
    up just to push a reconfigured notification (v6 §3.2)."""
    registry = HubRegistry(client=None)
    try:
        assert registry._global is None
        assert registry.notify_reconfigured_if_active("discovery_changed") == 0
        # Crucially still None — no lazy hub creation.
        assert registry._global is None
    finally:
        await registry.close()


async def test_hub_registry_notify_reconfigured_if_active_hub_with_no_subscribers_noop():
    """Hub exists but nobody is subscribed → 0, no work done."""
    registry = HubRegistry(client=None)
    try:
        # Force-create a hub without subscribing.
        hub = registry.get_global()
        assert hub.subscribers == set()
        assert registry.notify_reconfigured_if_active("discovery_changed") == 0
        assert hub.emitted_frames_total == 0
    finally:
        await registry.close()


async def test_hub_registry_notify_reconfigured_if_active_fans_out_to_subscribers():
    """Hub exists with one subscriber → exactly one frame emitted, counter +1."""
    registry = HubRegistry(client=None)
    try:
        sub = registry.subscribe()
        hub = registry.get_global()
        before = hub.emitted_frames_total
        emitted = registry.notify_reconfigured_if_active("discovery_changed")
        assert emitted == 1
        assert hub.emitted_frames_total == before + 1
        # Subscriber's first queued frame (after the welcome at index 0) is
        # the reconfigured notification.
        await subscriber_first_after_welcome(sub)
    finally:
        await registry.close()


async def subscriber_first_after_welcome(sub):
    # The welcome frame is index 0; index 1 is whatever the test pushed.
    _ = await asyncio.wait_for(sub.queue.get(), timeout=0.2)  # welcome
    second = await asyncio.wait_for(sub.queue.get(), timeout=0.2)
    event_name, data = parse_event(second)
    assert event_name == "server.reconfigured"
    assert data["reason"] == "discovery_changed"
