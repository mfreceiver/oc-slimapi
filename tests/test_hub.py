"""Tests for the native-v4 curated SSE contract.

Covers:
* digest merges status + messageID into one debounced frame per session
* session.deleted produces a digest with deleted=true
* session.updated with info.time.archived emits the archived epoch-ms int (sticky)
* question/permission events are forwarded immediately (no debounce)
* text deltas / tool.* / message.part.* are dropped
* reconnect emits a resync frame
* sessions across multiple directories all flow into the digest stream
* subscribe() has no connection-local welcome frame
* HubRegistry shares one global hub regardless of directory key
* /slimapi/events route wires the SSE response correctly end-to-end
* T3: subscriber queue overflow clears the queue and emits STOP only
  (old frames are NOT delivered)
* T3: HubRegistry admission raises SubscriberCapacityError past the caps
* T3: HubRegistry.snapshot_metrics() matches the contract shape
"""

from __future__ import annotations

from conftest import current_replay_log

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
    h = GlobalHub(client=None, replay_log=current_replay_log())
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
    hub = GlobalHub(client=None, replay_log=current_replay_log())
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    return hub, subscriber


async def test_digest_merges_status_and_message_into_one_frame(
        fresh_hub, monkeypatch):
    hub, subscriber = fresh_hub
    # 4.11.0 A3: messagesRevision is a process-global monotonic counter —
    # pin it for the exact-shape lock (restored by monkeypatch).
    monkeypatch.setattr(
        "oc_slimapi.sse.global_hub._message_revision_seq", 0)

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
    # updatedAt is sidecar wall-clock (not upstream timestamp) — validate type + monotonic.
    assert isinstance(data["updatedAt"], int)
    assert data["updatedAt"] > 0
    assert data == {
        "sessionID": "s1",
        "directory": "/proj",
        "status": "busy",
        "messageID": "msg_1",
        "updatedAt": data["updatedAt"],
        # B1a: every digest frame carries changed: [<this frame's sid>].
        "changed": ["s1"],
        # 4.11.0 A3: the message window carries the post-bump revision.
        "messagesRevision": 1,
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


async def test_subscribe_has_no_connection_local_welcome(hub):
    subscriber = hub.subscribe()
    assert subscriber.queue.empty()


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
    # updatedAt is sidecar wall-clock (not upstream created timestamp)
    assert isinstance(data["updatedAt"], int)
    assert data["updatedAt"] > 0


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
    registry = HubRegistry(client=None, replay_log=current_replay_log())
    try:
        h1 = registry.get("/dir-a")
        h2 = registry.get("/dir-b")
        h3 = registry.get_global()
        assert h1 is h2 is h3
    finally:
        await registry.close()


async def test_close_is_safe_when_no_hub_was_created():
    registry = HubRegistry(client=None, replay_log=current_replay_log())
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
        self.app.state = type(
            "State",
            (),
            {"hubs": _MockHubs(hub), "replay_log": current_replay_log()},
        )()
        self.headers = headers or {}


async def _events_route_chunks(hub: GlobalHub, headers: dict[str, str] | None = None):
    from oc_slimapi.routes.events import events

    request = _MockRequest(hub, headers)
    response = await events(request)
    assert response.media_type == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache, no-transform"
    assert response.headers["X-Accel-Buffering"] == "no"
    return response


async def test_events_route_streams_first_native_frame(hub):
    response = await _events_route_chunks(hub)
    iterator = response.body_iterator
    chunks: list[bytes] = []
    try:
        # Pull the first native-v4 frame.
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
    # Terminal §7.2: slimapi.meta precedes the business handshake.
    assert chunks[0].startswith(b"event: slimapi.meta")


async def test_events_route_honours_last_event_id_with_resync(hub):
    epoch = current_replay_log().epoch
    other_epoch = ("0" if epoch[0] != "0" else "1") + epoch[1:]
    response = await _events_route_chunks(
        hub, headers={"last-event-id": f"g:{other_epoch}:0"}
    )
    iterator = response.body_iterator
    chunks: list[bytes] = []
    try:
        # Pull the two leading frames: meta (terminal §7.2) then resync.
        for _ in range(2):
            chunks.append(await asyncio.wait_for(anext(iterator), timeout=0.5))
    except StopAsyncIteration:
        pass
    finally:
        await iterator.aclose()
    assert chunks and chunks[0].startswith(b"event: slimapi.meta")
    combined = b"".join(chunks)
    assert b"event: resync" in combined
    assert b"epoch_changed" in combined


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
# Lane-H / Gap 3: Subscriber overflow — immediate clear + STOP
# (contract §6)
# ---------------------------------------------------------------------------

async def test_subscriber_overflow_clears_queue_and_emits_stop():
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

    # Drain remaining items: ONLY STOP.
    items = []
    while True:
        try:
            item = subscriber.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        items.append(item)
    assert items == [STOP]

    # Critical guarantee (contract §6): old frames NOT still in the queue.
    payload = b"".join(item for item in items if isinstance(item, (bytes, bytearray)))
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
        replay_log=current_replay_log(),
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
        replay_log=current_replay_log(),
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
        replay_log=current_replay_log(),
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
        replay_log=current_replay_log(),
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
        # shape 加性演进：droppedEventsByType（2026-08-21 R-5 裁决，取代
        # 4.5.0 内部-only 决定）——纯加性键，既有五键零改动。
        assert set(hub_entry) == {
            "subscribers", "upstreamConnected",
            "upstreamEventsTotal", "emittedFramesTotal", "reconnectsTotal",
            "droppedEventsByType",
        }
        assert hub_entry["subscribers"] == 1
        assert hub_entry["upstreamConnected"] is False
        # Fresh hub, zero catch-all drops → always-published empty dict.
        assert hub_entry["droppedEventsByType"] == {}
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
        # Native v4 subscribe has no connection-local welcome frame.
        assert client_entry["queueItems"] == 0
        assert client_entry["bufferBytes"] == 0
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
        replay_log=current_replay_log(),
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

    replay_log = current_replay_log()
    registry = HubRegistry(
        client=None,
        replay_log=replay_log,
        max_subscribers_per_directory=10,
        max_total_subscribers=10,
    )
    try:
        for _ in range(5):
            assert registry.total_subscribers == 0
            request = type("Request", (), {})()
            request.app = type("App", (), {})()
            request.app.state = type(
                "State", (), {"hubs": registry, "replay_log": replay_log}
            )()
            request.headers = {}
            response = await events(request)
            assert response.media_type == "text/event-stream"
            iterator = response.body_iterator
            try:
                first = await asyncio.wait_for(anext(iterator), timeout=0.5)
                assert first.startswith(b"event: slimapi.meta")
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
# G1: structured provider-error classification (provider_errors.py wiring)
# ---------------------------------------------------------------------------

async def test_g1_a_last_error_carries_structured_code_and_retry_after(fresh_hub):
    """G1-A digest lastError carries additive classification fields:
    code + retryAfter (text-extracted) + provider/model (data passthrough).
    name/message/at keep their original semantics."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {
            "name": "RateLimitError",
            "data": {
                "message": "rate limit reached, retry after 30s",
                "provider": "openai",
                "model": "gpt-4o-mini",
            },
        },
    }))
    frames = await drain_queue(subscriber)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    le = next(d["lastError"] for d in digests if "lastError" in d)
    assert le["name"] == "RateLimitError"
    assert le["message"] == "rate limit reached, retry after 30s"
    assert isinstance(le.get("at"), int)
    # Additive structured fields.
    assert le["code"] == "provider_rate_limited"
    assert le["retryAfter"] == 30
    assert le["provider"] == "openai"
    assert le["model"] == "gpt-4o-mini"
    # Whitelist only — the raw data dict is not echoed.
    for key in le:
        assert key in {
            "name", "message", "at", "code", "provider", "model",
            "retryAfter", "quotaResetAt",
        }


async def test_g1_b_frame_carries_structured_fields(fresh_hub):
    """G1-B session-less direct frame carries the same additive fields."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "error": {
            "name": "QuotaError",
            "data": {
                "message": "exceeded quota, please retry after 30s",
                "retry_after": 45,  # structural beats text → 45
                "quota_reset_at": 1755302400000,
            },
        },
    }))
    frames = await drain_queue(subscriber)
    err_frames = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.error"
    ]
    assert len(err_frames) == 1
    frame = err_frames[0]
    # Order-sensitive: quota beats rate despite "retry after" in the text.
    assert frame["code"] == "provider_quota_exceeded"
    assert frame["retryAfter"] == 45
    assert frame["quotaResetAt"] == 1755302400000
    # camelCase normalization — snake_case never reaches the wire.
    assert "retry_after" not in frame
    assert "quota_reset_at" not in frame


async def test_g1_classification_uses_raw_pre_sanitize_message(fresh_hub):
    """Locks the raw-vs-sanitized decision: a retry-after clause sitting
    beyond the 512-char sanitize truncation point still classifies — the
    digest lastError.message is the truncated sanitized text while
    retryAfter comes from the raw message."""
    hub, subscriber = fresh_hub

    long_msg = "rate limit reached " + "x" * 600 + " retry after 45s"
    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": "RateLimitError", "data": {"message": long_msg}},
    }))
    frames = await drain_queue(subscriber)
    le = next(
        d["lastError"] for event, d in (parse_event(f) for f in frames)
        if event == "session.digest" and "lastError" in d
    )
    assert len(le["message"]) <= 512  # wire message stays sanitized/truncated
    assert "retry after 45" not in le["message"]
    assert le["code"] == "provider_rate_limited"
    assert le["retryAfter"] == 45  # extracted from the RAW message


async def test_g1_abort_still_filtered_with_classifiable_message(fresh_hub):
    """MessageAbortedError stays filtered even when its message would
    classify (no frame, no digest lastError, no structured fields)."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {
            "name": "MessageAbortedError",
            "data": {"message": "429 rate limit retry after 30s"},
        },
    }))
    frames = await drain_queue(subscriber)
    parsed = [parse_event(f) for f in frames]
    assert not any(event == "session.error" for event, _ in parsed)
    assert not any(
        event == "session.digest" and "lastError" in data
        for event, data in parsed
    )


async def test_g1_non_str_name_still_classifies_via_message(fresh_hub):
    """Non-str error.name (already coerced to None) must still produce a
    classified code from the message — and never crash either path."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {"name": 42, "data": {"message": "429 too many requests"}},
    }))
    frames = await drain_queue(subscriber)
    le = next(
        d["lastError"] for event, d in (parse_event(f) for f in frames)
        if event == "session.digest" and "lastError" in d
    )
    assert le["code"] == "provider_rate_limited"


async def test_g1_b_out_of_int64_quota_reset_dropped_before_orjson(fresh_hub):
    """P1 integration: quota_reset_at=10**400 must be DROPPED by the
    classifier — carrying it through would make sse_frame's orjson.dumps
    raise ``TypeError: Integer exceeds 64-bit range`` inside publish
    (global_hub.py G1-B direct-frame path), killing the subscriber. The
    frame must still be emitted, just without the quotaResetAt key."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "error": {
            "name": "QuotaError",
            "data": {
                "message": "exceeded quota",
                "quota_reset_at": 10**400,
                "retry_after": 30,
            },
        },
    }))
    frames = await drain_queue(subscriber)
    err_frames = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.error"
    ]
    assert len(err_frames) == 1
    frame = err_frames[0]
    assert frame["code"] == "provider_quota_exceeded"
    assert "quotaResetAt" not in frame  # dropped, not carried, not raised
    assert frame["retryAfter"] == 30    # siblings unaffected


async def test_g1_a_in_range_quota_reset_survives_wire(fresh_hub):
    """P1 counterpart: an in-range (2**62) epoch passes the gate and lands
    verbatim-int in the digest lastError — proves the gate is a range
    check, not a blanket drop."""
    hub, subscriber = fresh_hub

    hub.publish(make_global_event("/proj", "session.error", {
        "sessionID": "s1",
        "error": {
            "name": "QuotaError",
            "data": {"message": "exceeded quota", "quota_reset_at": 2**62},
        },
    }))
    frames = await drain_queue(subscriber)
    le = next(
        d["lastError"] for event, d in (parse_event(f) for f in frames)
        if event == "session.digest" and "lastError" in d
    )
    assert le["quotaResetAt"] == 2**62
    assert type(le["quotaResetAt"]) is int


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


def test_subscriber_put_overflow_returns_false_and_emits_stop_only():
    """The overflow path returns False (the original frame was NOT enqueued);
    the terminal STOP replaces stale data without a synthetic resync."""
    subscriber = Subscriber(
        queue_items=2, buffer_bytes=4096, max_frame_bytes=4096,
    )
    # Fill to capacity.
    assert subscriber.put(sse_frame({"i": 1}, event="test")) is True
    assert subscriber.put(sse_frame({"i": 2}, event="test")) is True
    # Third put triggers the overflow path.
    assert subscriber.put(sse_frame({"i": 3}, event="test")) is False
    assert subscriber.closed is True
    # STOP is the only remaining item; the original overflow frame is absent.
    assert list(subscriber.queue._queue) == [STOP]


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
    makes that explicit so callers do not double-count a dropped STOP as a
    real emit.
    """
    subscriber = Subscriber(
        queue_items=1, buffer_bytes=4096, max_frame_bytes=4096,
    )
    # One item fills the queue; STOP is also bounded by queue_items.
    assert subscriber.put(sse_frame({"i": 1}, event="test")) is True
    assert subscriber.put(STOP) is False
    assert not subscriber.closed  # the original frame is still queued


# ---------------------------------------------------------------------------
# §9.3: Digest field convergence / updatedAt monotonicity / bump boundary
# ---------------------------------------------------------------------------

async def test_digest_fields_converged(fresh_hub):
    """§9.3: digest frame JSON must only contain the expected fields.
    Verifies both the integration path (publish → flush → frame) and the
    unit path (DigestFields.to_payload() with all fields populated)."""
    from oc_slimapi.sse.hub import DigestFields

    # --- Unit test: to_payload() with all fields populated ---
    full = DigestFields(
        directory="/proj", status="busy", message_id="msg_1",
        updated_at=1700000000000, archived=1700000001000,
        deleted=True, last_error="boom",
        changed=["s1"],  # B1a: non-None → conditionally included
    )
    unit_payload = full.to_payload("s1")
    # Every field that is set must appear; None/default fields are omitted.
    for key in ("directory", "status", "messageID", "updatedAt", "archived", "deleted", "lastError", "changed"):
        assert key in unit_payload, f"expected {key} in to_payload output"
    assert "contentRevisions" not in unit_payload
    assert "childrenVersion" not in unit_payload

    # --- Integration test: actual digest frame from publish ---
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
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest"
    ]
    assert len(digests) == 1
    payload = digests[0]

    allowed = {"sessionID", "directory", "status", "messageID",
               "updatedAt", "archived", "deleted", "lastError", "changed",
               # 4.11.0 A3: conditional on message windows.
               "messagesRevision"}
    assert set(payload) <= allowed, f"Unexpected keys: {set(payload) - allowed}"
    assert "contentRevisions" not in payload
    assert "childrenVersion" not in payload


async def test_part_updated_bumps_digest_revision(fresh_hub, monkeypatch):
    """修订六（4.12.0）：message.part.updated 经统一 helper 接入 digest
    修订——低频完成态变化（上游每 part 生命周期 2-4 次）可经 digest 感知，
    与 message.updated 同组同 debounce。Token-hub-only 路由时代结束。"""
    hub, subscriber = fresh_hub

    FIXED = 1700000000000
    monkeypatch.setattr("oc_slimapi.sse.global_hub._now_ms", lambda: FIXED)

    from oc_slimapi.sse import global_hub as gh

    before = gh._message_revision_seq
    hub.publish(make_global_event("/proj", "message.part.updated", {
        "part": {"sessionID": "s1", "messageID": "m1", "id": "p1"},
        "sessionID": "s1", "messageID": "m1",
    }))
    assert gh._message_revision_seq == before + 1
    assert "s1" in hub.pending  # 修订六：part 事件现在造 pending entry

    hub.flush()
    frames = await drain_queue(subscriber)
    assert len(frames) == 1
    assert b"session.digest" in frames[0]
    assert b'"messagesRevision"' in frames[0]


async def test_part_removed_bumps_digest_revision(fresh_hub, monkeypatch):
    """修订六（4.12.0）：message.part.removed 同 bump（revert 场景）——
    同 message.updated 组同 debounce 语义。"""
    hub, subscriber = fresh_hub

    FIXED = 1700000000000
    monkeypatch.setattr("oc_slimapi.sse.global_hub._now_ms", lambda: FIXED)

    from oc_slimapi.sse import global_hub as gh

    before = gh._message_revision_seq
    hub.publish(make_global_event("/proj", "message.part.removed", {
        "sessionID": "s1", "messageID": "m1", "partID": "p1",
    }))
    assert gh._message_revision_seq == before + 1
    assert "s1" in hub.pending

    hub.flush()
    frames = await drain_queue(subscriber)
    assert len(frames) == 1
    assert b"session.digest" in frames[0]
    assert b'"messagesRevision"' in frames[0]


async def test_bump_updated_at_same_ms_collision(monkeypatch, fresh_hub):
    """§9.3: deterministic same-ms collision + clock rollback coverage.
    _bump_updated_at uses max(now, previous+1) — never goes backward.
    Now testing per-session cross-debounce monotonicity (🟠-2)."""
    hub, _ = fresh_hub

    FIXED = 1234567890
    monkeypatch.setattr("oc_slimapi.sse.global_hub._now_ms", lambda: FIXED)

    from oc_slimapi.sse.hub import DigestFields

    entry = DigestFields()
    # Fresh: previous=0 → max(FIXED, 1) = FIXED
    hub._bump_updated_at("s1", entry)
    assert entry.updated_at == FIXED
    assert hub._last_updated_at_by_sid.get("s1") == FIXED

    # Same-ms: previous=FIXED, now=FIXED → max(FIXED, FIXED+1) = FIXED+1
    hub._bump_updated_at("s1", entry)
    assert entry.updated_at == FIXED + 1
    assert hub._last_updated_at_by_sid.get("s1") == FIXED + 1

    # Clock rollback: now=FIXED-1000, previous=FIXED+1 → max(FIXED-1000, FIXED+2) = FIXED+2
    monkeypatch.setattr("oc_slimapi.sse.global_hub._now_ms", lambda: FIXED - 1000)
    hub._bump_updated_at("s1", entry)
    assert entry.updated_at == FIXED + 2
    assert hub._last_updated_at_by_sid.get("s1") == FIXED + 2

    # Cross-debounce (🟠-2): fresh DigestFields for same session must still
    # produce a value > last session high-water mark.
    entry2 = DigestFields()
    hub._bump_updated_at("s1", entry2)
    assert entry2.updated_at == FIXED + 3  # max(FIXED-1000, max(0, FIXED+2) + 1) = FIXED+3
    assert entry2.updated_at > FIXED + 2
    assert hub._last_updated_at_by_sid.get("s1") == entry2.updated_at

    # Different session is independent.
    entry3 = DigestFields()
    hub._bump_updated_at("s2", entry3)
    assert entry3.updated_at == FIXED - 1000  # fresh start for s2


async def test_message_removed_does_not_bump_updatedAt(fresh_hub):
    """§9.3: message.removed must NOT bump digest.updatedAt (design
    decision: message.removed is a pure retirement signal that does not
    touch the digest's pending entry or updatedAt)."""
    hub, subscriber = fresh_hub

    # Seed a digest with a known updatedAt via message.updated.
    hub.publish(make_global_event("/proj", "message.updated", {
        "sessionID": "s1",
        "info": {"id": "msg_1", "time": {"updated": 1700000000000}},
    }))
    hub.flush()
    await drain_queue(subscriber)  # drain the first digest
    assert hub.pending == {}  # evicted

    # Now send message.removed — must NOT create a pending entry or emit a digest.
    hub.publish(make_global_event("/proj", "message.removed", {
        "sessionID": "s1", "messageID": "msg_1",
    }))
    hub.flush()
    frames = await drain_queue(subscriber, timeout=0.1)
    digests = [
        data for event, data in (parse_event(f) for f in frames)
        if event == "session.digest" and data.get("sessionID") == "s1"
    ]
    # message.removed does NOT create a pending digest entry — no frame emitted.
    assert len(digests) == 0
    # But the retirement IS recorded for the safety gate.
    assert ("s1", "msg_1") in hub._retired_messages
    assert digests == [], f"message.removed should not emit a digest, got: {digests}"
