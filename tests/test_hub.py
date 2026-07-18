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
    hub.publish(make_global_event("/proj", "session.error", {"sessionID": "s1"}))
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
