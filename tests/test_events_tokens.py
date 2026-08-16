"""L2-A tests: ``/slimapi/events?tokens=1`` curated-events token frames.

Scope (plan Task L2-A, acceptance A-C1..A-C5):

* A-C1 — ``?tokens=<anything-but-1>`` → 400 ``invalid_tokens``; default and
  ``?tokens=1`` both build the stream with the unchanged ``server.connected``
  first frame.
* A-C2 — ``?tokens=1``: two upstream ``message.part.delta`` for the same
  ``(sessionID, messageID, partID)`` land in one ~100ms flush window and
  arrive as a SINGLE lean ``{type:"token", sessionID, messageID, partID,
  delta}`` frame (delta concat "Hel"+"lo" == "Hello").
* A-C3 — default (no ``tokens``) → zero ``token`` frames even while deltas
  are actively flushed; events behaviour is unchanged.
* A-C4 — events-token backpressure reuses the UNCHANGED ``Subscriber.put``
  T3 guard: an overflowing events-token queue → ``resync``
  ``{subscriber_backpressure}`` + ``STOP`` + forced disconnect (no new path).
* A-C5 — combined flush-loop ledger symmetry: an ONLY-events-token subscriber
  (zero per-session streams) keeps the 100ms flush loop alive and receives
  token frames at cadence; the last detach (events-token + per-session
  ledgers both empty) stops the loop (mirror NB-C4 / NB-D1).

Harness mirrors ``tests/test_token_stream_route.py`` (no sibling-test
imports): a fresh FastAPI app with the events router + hub / token-hub /
token-registry wired exactly like ``app.lifespan``, and a manual ASGI
streaming driver (httpx.ASGITransport buffers the whole body, so an
infinite SSE generator would park it forever).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import CodedHTTPException, register_error_handlers
from oc_slimapi.routes import health
from oc_slimapi.routes.events import events, router as events_router
from oc_slimapi.sse.hub import STOP, HubRegistry, Subscriber
from oc_slimapi.sse.token_hub import (
    TokenStreamHub,
    TokenStreamRegistry,
)

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


# ---------------------------------------------------------------------------
# Shared helpers (inlined per repo pattern — no sibling-test imports).
# ---------------------------------------------------------------------------

def make_global_event(directory: str, event_type: str, properties: dict | None = None,
                      payload_id: str | None = None) -> dict:
    """Mirror tests/test_hub.py:38 — GlobalBus event envelope."""
    payload: dict = {"type": event_type}
    if properties is not None:
        payload["properties"] = properties
    if payload_id is not None:
        payload["id"] = payload_id
    return {"directory": directory, "payload": payload}


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


def parse_event(raw: bytes):
    """Split one SSE frame into ``(event_name, data_dict)`` (test_hub pattern).

    ``event_name`` is ``None`` for frames with no ``event:`` line (the lean
    ``{type:"token", ...}`` frames are emitted without an event name — clients
    dispatch on ``data.type``).
    """
    event = None
    data_lines: list[bytes] = []
    for line in raw.split(b"\n"):
        if line.startswith(b"event:"):
            event = line[6:].strip().decode()
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return event, None
    data = json.loads(b"\n".join(data_lines).decode())
    return event, data


def _sse_frames(body: bytes):
    """Split an SSE byte body into ``(event_name, data)`` frames."""
    for block in body.split(b"\n\n"):
        block = block.strip(b"\n")
        if not block:
            continue
        event, data = parse_event(block)
        if data is not None:
            yield event, data


async def _drain(sub: Subscriber, timeout: float = 0.2):
    """Drain a Subscriber queue until timeout; parse frames, ack each."""
    frames = []
    try:
        while True:
            item = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
            sub.ack(item)
            if item is STOP:
                break
            frames.append(parse_event(item))
    except asyncio.TimeoutError:
        pass
    return frames


async def _drain_raw(sub: Subscriber, timeout: float = 0.2):
    """Drain raw queue items (keeps the STOP sentinel observable)."""
    items = []
    try:
        while True:
            item = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
            items.append(item)
            if item is STOP:
                break
    except asyncio.TimeoutError:
        pass
    return items


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
        max_subscribers_per_directory=8,
        max_total_subscribers=16,
        sse_queue_items=256,
        sse_buffer_bytes=2 * 1024 * 1024,
        sse_max_frame_bytes=256 * 1024,
        token_stream_max_subscribers=2,
        token_stream_queue_items=64,
        token_stream_buffer_bytes=512 * 1024,
        token_stream_max_frame_bytes=1024 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings) -> FastAPI:
    """Fresh FastAPI app mirroring ``app.lifespan`` token wiring.

    ``hub_registry`` uses ``client=None`` so ``GlobalHub.run`` parks on
    backoff sleeps — the token frames come from the accumulator directly,
    not an upstream connection.
    """
    app = FastAPI(title="oc-slimapi-events-tokens-test")
    app.state.config = settings
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    upstream = httpx.AsyncClient()  # unused (hub client is None) but kept for parity
    app.state.upstream = upstream
    hubs = HubRegistry(
        client=None,
        max_subscribers_per_directory=settings.max_subscribers_per_directory,
        max_total_subscribers=settings.max_total_subscribers,
        queue_items=settings.sse_queue_items,
        buffer_bytes=settings.sse_buffer_bytes,
        max_frame_bytes=settings.sse_max_frame_bytes,
    )
    token_hub = TokenStreamHub()
    hubs.set_token_hub(token_hub)
    token_registry = TokenStreamRegistry(
        token_hub,
        hubs,
        max_subscribers=settings.token_stream_max_subscribers,
        queue_items=settings.token_stream_queue_items,
        buffer_bytes=settings.token_stream_buffer_bytes,
        max_frame_bytes=settings.token_stream_max_frame_bytes,
    )
    app.state.hubs = hubs
    app.state.token_hub = token_hub
    app.state.token_registry = token_registry
    app.include_router(health.router)
    app.include_router(events_router)
    register_error_handlers(app)
    return app


async def _close_app(app: FastAPI) -> None:
    """Tear down hub / registry / token_hub background tasks."""
    app.state.token_hub.stop()
    with contextlib.suppress(Exception):
        await app.state.hubs.close()
    await app.state.upstream.aclose()


def _headers() -> list[tuple[str, str]]:
    return [(k.lower(), v) for k, v in VERSION_HEADERS.items()]


class _RequestStub:
    """Minimal Starlette-Request stand-in for direct route calls."""

    def __init__(self, app: FastAPI):
        self.app = app
        self.headers: dict[str, str] = {}


async def _drive_stream(
    app: FastAPI,
    path: str,
    headers_list: list[tuple[str, str]],
    *,
    park_timeout: float = 0.5,
) -> tuple[int, list, bytes]:
    """Manual ASGI streaming driver (mirrors test_token_stream_route.py).

    httpx.ASGITransport buffers the FULL response, so an infinite SSE
    generator parks it forever. Drive the ASGI app by hand: ``receive``
    delivers the (empty) request body once, then answers every subsequent
    poll with ``http.disconnect``. After the park window we cancel the task;
    cancellation unwinds the generator's ``finally`` (detach events-token
    subscriber + hub unsubscribe) — exactly the production disconnect path.
    """
    if "?" in path:
        pure_path, query = path.split("?", 1)
        query_string = query.encode()
    else:
        pure_path, query_string = path, b""
    scope = {
        "type": "http",
        "method": "GET",
        "path": pure_path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers_list],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 0),
        "root_path": "",
        "extensions": {},
        # Starlette 1.3.1: with asgi.spec_version < 2.4 the StreamingResponse
        # runs a concurrent listen_for_disconnect that CANCELS the stream the
        # moment receive() answers http.disconnect (our probe), aborting after
        # the first frame. Declare 2.4 so the generator stays alive until we
        # cancel the task (the token frames then flow during the park window).
        "asgi": {"spec_version": "2.4"},
    }
    status_code = 0
    headers: list = []
    body = bytearray()
    got_response = False
    body_delivered = False

    async def receive():
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        nonlocal status_code, headers, got_response
        if message["type"] == "http.response.start":
            status_code = message["status"]
            headers = message["headers"]
            got_response = True
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    task = asyncio.create_task(app(scope, receive, send))
    # Let the generator emit handshake frames, then park on queue.get.
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=park_timeout)
    except asyncio.TimeoutError:
        pass  # expected: generator parked after the handshake
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    await asyncio.sleep(0)  # let finally blocks flush
    assert got_response, "no http.response.start received"
    return status_code, headers, bytes(body)


# ===========================================================================
# A-C1 — tokens param validation + stream build (unchanged first frame)
# ===========================================================================

async def test_tokens_invalid_value_400():
    """``?tokens=2`` → 400 ``invalid_tokens`` (only literal ``"1"`` is legal)."""
    app = _build_app(_settings())
    try:
        status, _, body = await _drive_stream(
            app, "/slimapi/events?tokens=2", _headers())
        assert status == 400
        assert b"invalid_tokens" in body
    finally:
        await _close_app(app)


async def test_tokens_non_literal_direct_call_400():
    """Direct route call (no FastAPI DI): tokens="abc" → CodedHTTPException 400."""
    app = _build_app(_settings())
    try:
        request = _RequestStub(app)
        with pytest.raises(CodedHTTPException) as ei:
            await events(request, tokens="abc")
        assert ei.value.status_code == 400
        assert ei.value.code == "invalid_tokens"
    finally:
        await _close_app(app)


async def test_default_events_builds_stream_server_connected_first():
    """Default (no tokens) builds the stream; server.connected is the FIRST
    frame (A-C1 — order, not just presence)."""
    app = _build_app(_settings())
    try:
        status, _, body = await _drive_stream(app, "/slimapi/events", _headers())
        assert status == 200
        frames = list(_sse_frames(body))
        assert frames, "expected at least the server.connected frame"
        assert frames[0][0] == "server.connected", (
            f"server.connected must be first, got {frames[0]!r}"
        )
    finally:
        await _close_app(app)


async def test_tokens_one_builds_stream_server_connected_first():
    """``?tokens=1`` builds the stream; server.connected is the FIRST frame
    (A-C1 — order, not just presence; the tokens=1 path must not reorder the
    handshake)."""
    app = _build_app(_settings())
    try:
        status, _, body = await _drive_stream(
            app, "/slimapi/events?tokens=1", _headers())
        assert status == 200
        frames = list(_sse_frames(body))
        assert frames, "expected at least the server.connected frame"
        assert frames[0][0] == "server.connected", (
            f"server.connected must be first, got {frames[0]!r}"
        )
    finally:
        await _close_app(app)


# ===========================================================================
# A-C2 — coalescing: 2 deltas, one (sid, mid, pid) window → single token frame
# ===========================================================================

async def test_tokens_one_coalesces_two_deltas_into_single_frame():
    """End-to-end: two ``message.part.delta`` (same key) coalesce in one ~100ms
    flush window → exactly ONE ``{type:"token", sessionID, messageID, partID,
    delta:"Hello"}`` frame on the stream (A-C2)."""
    app = _build_app(_settings())
    try:
        global_hub = app.state.hubs.get_global()
        # Both deltas land in the SAME pending window: published synchronously
        # before the flush loop (started by the route's tap attach) ticks.
        global_hub.publish(make_global_event("/p", "message.part.updated",
                                             _updated_props(text="")))
        global_hub.publish(make_global_event("/p", "message.part.delta",
                                             _delta_props(delta="Hel")))
        global_hub.publish(make_global_event("/p", "message.part.delta",
                                             _delta_props(delta="lo")))
        status, _, body = await _drive_stream(
            app, "/slimapi/events?tokens=1", _headers(), park_timeout=0.6)
        assert status == 200
        frames = list(_sse_frames(body))
        tokens = [data for ev, data in frames if data.get("type") == "token"]
        assert len(tokens) == 1, f"expected 1 coalesced token frame, got {len(tokens)}"
        assert tokens[0] == {
            "type": "token",
            "sessionID": "s1",
            "messageID": "m1",
            "partID": "p1",
            "delta": "Hello",
        }
    finally:
        await _close_app(app)


async def test_token_frame_shape_via_flush_direct():
    """Registry-unit: attach events subscriber, updated + 2 deltas, manual
    flush() → single token frame with the exact lean shape (A-C2)."""
    th = TokenStreamHub()
    reg = TokenStreamRegistry(
        th, None,
        max_subscribers=2, queue_items=64,
        buffer_bytes=512 * 1024, max_frame_bytes=1024 * 1024,
    )
    sub = Subscriber()
    try:
        reg.attach_events_subscriber(sub)
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="Hel"))
        th.on_part_delta(_delta_props(delta="lo"))
        th.flush()
        frames = await _drain(sub, timeout=0.2)
        tokens = [data for ev, data in frames if data.get("type") == "token"]
        assert len(tokens) == 1, f"expected 1 token frame, got {len(tokens)}"
        assert tokens[0] == {
            "type": "token",
            "sessionID": "s1",
            "messageID": "m1",
            "partID": "p1",
            "delta": "Hello",
        }
    finally:
        reg.detach_events_subscriber(sub)
        th.stop()


# ===========================================================================
# A-C3 — default (no tokens) → zero token frames, events unchanged
# ===========================================================================

async def test_default_events_emits_no_token_frames_while_deltas_flow():
    """A default events subscriber receives NO ``token`` frames even while the
    token flush loop is actively emitting them to a tapped subscriber."""
    app = _build_app(_settings())
    try:
        th = app.state.token_hub
        reg = app.state.token_registry
        global_hub = app.state.hubs.get_global()
        # A separately-tapped subscriber keeps the flush loop alive so the
        # deltas ARE flushed during the default stream's park window.
        tapped = Subscriber()
        reg.attach_events_subscriber(tapped)
        try:
            global_hub.publish(make_global_event("/p", "message.part.updated",
                                                 _updated_props(text="")))
            global_hub.publish(make_global_event("/p", "message.part.delta",
                                                 _delta_props(delta="Hello")))
            status, _, body = await _drive_stream(
                app, "/slimapi/events", _headers(), park_timeout=0.6)
            assert status == 200
            frames = list(_sse_frames(body))
            assert any(ev == "server.connected" for ev, _ in frames)
            assert all(data.get("type") != "token" for _, data in frames), (
                "default events stream must not emit token frames"
            )
            # Meanwhile the tapped subscriber DID receive the token frame.
            got = await _drain(tapped, timeout=0.3)
            assert any(data.get("type") == "token" for _, data in got)
        finally:
            reg.detach_events_subscriber(tapped)
    finally:
        await _close_app(app)


# ===========================================================================
# A-C4 — backpressure reuses the unchanged Subscriber.put T3 guard
# ===========================================================================

async def test_events_tap_backpressure_resync_stop():
    """An overflowing events-token queue → unchanged T3 guard:
    ``resync{subscriber_backpressure}`` + ``STOP`` + forced disconnect."""
    th = TokenStreamHub()
    reg = TokenStreamRegistry(
        th, None,
        max_subscribers=2, queue_items=2,
        buffer_bytes=256, max_frame_bytes=1024,
    )
    sub = Subscriber(queue_items=2, buffer_bytes=256, max_frame_bytes=1024)
    try:
        reg.attach_events_subscriber(sub)
        # Frame >> buffer_bytes → overflow (not oversized-drop; 1024 > frame).
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="x" * 300))
        th.flush()
        items = await _drain_raw(sub, timeout=0.2)
        assert sub.closed, "overflow must force-disconnect the events subscriber"
        assert sub.forced_disconnects >= 1
        assert STOP in items, "overflow must enqueue the STOP sentinel"
        resync = next(
            (parse_event(it)[1] for it in items
             if it is not STOP and parse_event(it)[0] == "resync"),
            None,
        )
        assert resync is not None, "overflow must enqueue a resync frame"
        assert resync["reason"] == "subscriber_backpressure"
    finally:
        reg.detach_events_subscriber(sub)
        th.stop()


# ===========================================================================
# A-C5 — combined flush-loop ledger symmetry (first-attach start / last-detach stop)
# ===========================================================================

async def test_events_only_keeps_flush_loop_alive_then_stops():
    """An ONLY-events-token subscriber (zero per-session streams) keeps the
    100ms flush loop alive and receives token frames at cadence; the last
    detach stops the loop (A-C5 / NB-C4 extension)."""
    th = TokenStreamHub()
    reg = TokenStreamRegistry(
        th, None,
        max_subscribers=2, queue_items=64,
        buffer_bytes=512 * 1024, max_frame_bytes=1024 * 1024,
    )
    sub = Subscriber()
    try:
        reg.attach_events_subscriber(sub)
        # First-attach lifecycle: loop started even with zero per-session subs.
        assert reg.events_tokens == {sub}
        assert reg.total_subscribers == 0
        assert th._flush_task is not None and not th._flush_task.done()
        # Token frame arrives on the REAL flush cadence (no manual flush).
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="Hi"))
        frames = await _drain(sub, timeout=0.7)
        tokens = [data for ev, data in frames if data.get("type") == "token"]
        assert tokens, "events-only subscriber must receive token frames"
        assert tokens[0]["delta"] == "Hi"
        # Last detach → flush loop stops.
        reg.detach_events_subscriber(sub)
        assert reg.events_tokens == set()
        assert th._flush_task is None, "flush loop must stop on last detach"
    finally:
        th.stop()


async def test_events_only_watchdog_restarts_dead_flush_loop(monkeypatch):
    """INV-1 watchdog with an ONLY-events consumer (zero per-session subs).

    Regression: the flush-loop supervisor (:meth:`TokenStreamHub._on_flush_done`)
    must treat the events-token ledger as a liveness source. With the old
    ``subscriber_count > 0`` check, an events-only stream (subscriber_count
    == 0) would let a dead flush loop stay dead — connection alive but token
    frames permanently stopped and ``_pending`` accumulating. The unified
    predicate (per-session subs ∪ events taps) must rebuild, keep producing
    at cadence, and stop cleanly on the last detach.
    """
    th = TokenStreamHub()
    reg = TokenStreamRegistry(
        th, None,
        max_subscribers=2, queue_items=64,
        buffer_bytes=512 * 1024, max_frame_bytes=1024 * 1024,
    )
    sub = Subscriber()
    try:
        # Make the FIRST flush_loop invocation die immediately; the rebuilt
        # task (created by the watchdog) runs the real loop.
        real_flush_loop = th.flush_loop
        deaths = {"n": 0}

        async def _dying_flush_loop():
            deaths["n"] += 1
            if deaths["n"] == 1:
                raise RuntimeError("flush_loop boom")
            await real_flush_loop()

        monkeypatch.setattr(th, "flush_loop", _dying_flush_loop)
        # Attach AFTER patching so the very first task is the dying one.
        reg.attach_events_subscriber(sub)
        first_task = th._flush_task
        assert first_task is not None and not first_task.done()
        assert th.subscriber_count == 0  # events-only: no per-session subs

        # Let the first task die; the watchdog must rebuild because the
        # events ledger is non-empty.
        await asyncio.sleep(0.1)
        assert deaths["n"] >= 2, "watchdog must have restarted the loop"
        assert th._flush_task is not None
        assert th._flush_task is not first_task, (
            "watchdog must create a NEW flush task"
        )
        assert not th._flush_task.done()

        # Rebuilt loop still emits token frames at ~100ms cadence.
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="Hi"))
        frames = await _drain(sub, timeout=0.8)
        tokens = [data for ev, data in frames if data.get("type") == "token"]
        assert tokens, "events-only subscriber must get token frames after rebuild"
        assert tokens[0]["delta"] == "Hi"

        # Last detach → loop stops; no further rebuild.
        reg.detach_events_subscriber(sub)
        assert th._flush_task is None, "flush loop must stop on last detach"
        assert deaths["n"] == 2, "no restart after detach (no consumers left)"
    finally:
        th.stop()
        monkeypatch.undo()


async def test_combined_ledger_last_detach_stops_flush_loop():
    """With a per-session stream + an events-token subscriber, detaching the
    events subscriber keeps the loop running (per-session ledger non-empty);
    the LAST detach (both ledgers empty) stops it (A-C5 symmetry)."""
    th = TokenStreamHub()
    hubs = HubRegistry(client=None)
    hubs.set_token_hub(th)
    reg = TokenStreamRegistry(
        th, hubs,
        max_subscribers=4, queue_items=64,
        buffer_bytes=512 * 1024, max_frame_bytes=1024 * 1024,
    )
    try:
        session_sub = reg.subscribe("s1")
        assert th._flush_task is not None and not th._flush_task.done()
        ev_sub = Subscriber()
        reg.attach_events_subscriber(ev_sub)
        assert th._flush_task is not None and not th._flush_task.done()
        # Detach events while a per-session sub remains → loop KEEPS running.
        reg.detach_events_subscriber(ev_sub)
        assert th._flush_task is not None and not th._flush_task.done()
        # Last detach (per-session) → loop stops.
        reg.unsubscribe(session_sub)
        assert th._flush_task is None, "flush loop must stop on the true last detach"
    finally:
        th.stop()
        with contextlib.suppress(Exception):
            await hubs.close()


async def test_attach_events_subscriber_idempotent():
    """Re-attaching the same subscriber is a no-op: one ledger entry, one tap."""
    th = TokenStreamHub()
    reg = TokenStreamRegistry(
        th, None,
        max_subscribers=2, queue_items=64,
        buffer_bytes=512 * 1024, max_frame_bytes=1024 * 1024,
    )
    sub = Subscriber()
    try:
        reg.attach_events_subscriber(sub)
        reg.attach_events_subscriber(sub)  # same admission slot → no-op
        assert reg.events_tokens == {sub}
        assert len(th.events_tap) == 1
        assert th.events_tap[0] == sub.put
    finally:
        reg.detach_events_subscriber(sub)
        th.stop()
