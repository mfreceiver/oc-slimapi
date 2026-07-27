"""Stage-D tests for the token-stream HTTP endpoint + admission registry +
subscriber + gzip + lifecycle + observability (design-token-stream.md §5.5 /
§5.6 / §6 / §7 / §16-D).

Scope:

* :class:`TokenSubscriber` — T3 three-stage guard (closed → oversized-drop →
  overflow-disconnect), sessionID-bearing ``subscriber_backpressure`` resync,
  ``dropped_frames_total`` bump on every drop (NB-C5), STOP terminal sentinel.
* :class:`TokenStreamRegistry` — independent admission cap (NOT
  ``MAX_TOTAL_SUBSCRIBERS``), reject → 503-``sse_token_subscriber_limit``,
  NB-B1 grace-removal cancel on subscribe, first-attach start / last-detach
  stop lifecycle (NB-C4), metrics snapshot.
* HTTP ``GET /slimapi/sessions/{sid}/stream`` — §5.5 handshake ordering
  (server.connected → snapshot), ``Last-Event-ID`` → leading
  ``reconnect_no_replay`` resync, NO SSE ``id:`` field, version gate,
  admission 503, disconnect → unsubscribe, identity passthrough.
* Lever 2 gzip — ``Z_SYNC_FLUSH`` alignment to SSE event boundaries,
  ``Content-Encoding: gzip`` negotiation, control-plane ``/slimapi/events``
  NOT gzipped.
* health root-level ``features.tokenStream`` (Q1) + metrics ``sse.tokenStream.*``.
* NB-C1 multi-part large-seed burst → global byte-cap LRU eviction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import zlib

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings, TOKEN_FLUSH_SECONDS
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import events, health, metrics, token_stream
from oc_slimapi.sse.hub import HubRegistry, Subscriber, sse_frame as hub_sse_frame
from oc_slimapi.sse.token_hub import (
    STOP,
    TokenStreamHub,
    TokenStreamRegistry,
    TokenSubscriber,
    TokenSubscriberCapacityError,
    _connected_frame,
    _delta_frame,
    _resync_frame,
    _snapshot_frame,
)
from oc_slimapi.versioning import SlimapiVersionMiddleware

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


# ---------------------------------------------------------------------------
# Shared helpers (inlined per repo pattern — no sibling-test imports).
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


def parse_sse_stream(raw: bytes) -> list[tuple[str | None, dict]]:
    """Parse a concatenated SSE byte stream into (event, data) tuples.

    Splits on the SSE frame terminator ``\\n\\n``; tolerates a trailing
    partial (gzip streams may not end exactly on ``\\n\\n`` if the body was
    cut mid-stream, so trailing non-terminated content is ignored).
    """
    events: list[tuple[str | None, dict]] = []
    for block in raw.split(b"\n\n"):
        if not block.strip():
            continue
        events.append(parse_event(block))
    return events


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        route_secret="x" * 32,
        route_secret_file=None,
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


def _build_app(settings: Settings, *, include_control_events: bool = False) -> FastAPI:
    """Construct a fresh FastAPI app mirroring ``app.lifespan`` token wiring.

    ``hub_registry`` uses ``client=None`` so :meth:`GlobalHub.run` parks on
    backoff sleeps instead of spamming a mock upstream — the token frames
    come from the accumulator directly, not the upstream connection.
    """
    app = FastAPI(title="oc-slimapi-token-stream-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
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
    app.include_router(metrics.router)
    app.include_router(token_stream.router)
    if include_control_events:
        app.include_router(events.router)
    register_error_handlers(app)
    return app


async def _close_app(app: FastAPI) -> None:
    """Tear down hub / registry / token_hub background tasks."""
    app.state.token_hub.stop()
    with contextlib.suppress(Exception):
        await app.state.hubs.close()
    await app.state.upstream.aclose()


# ---------------------------------------------------------------------------
# Manual ASGI streaming driver.
#
# httpx.ASGITransport buffers the FULL response, so an infinite SSE generator
# parks it forever. We instead drive the ASGI app by hand:
#   * ``receive`` delivers the (empty) request body once, then answers every
#     subsequent poll with ``http.disconnect`` — Starlette's
#     is_disconnected() probe would otherwise busy-spin on repeated
#     ``http.request`` returns and starve the event loop.
#   * The token-stream generator parks on ``await queue.get()`` after the
#     handshake, so the task never self-completes; after the park window we
#     cancel the task. Cancellation throws CancelledError into the
#     generator's ``await queue.get()``, which unwinds its ``finally`` →
#     ``registry.unsubscribe`` (so the ledger returns to 0 and the flush loop
#     stops — exactly the production client-disconnect path via uvicorn task
#     cancellation).
# ---------------------------------------------------------------------------

async def _drive_stream(
    app: FastAPI,
    path: str,
    headers_list: list[tuple[str, str]],
    *,
    park_timeout: float = 0.5,
) -> tuple[int, list, bytes]:
    # Split query string off the path (if present) so FastAPI query params
    # like ``?directory=/app`` are visible to the route handler.
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
    # Cancel → CancelledError into the generator's queue.get → finally runs
    # (unsubscribe). shield kept the task alive across the park window.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    await asyncio.sleep(0)  # let finally blocks flush
    assert got_response, "no http.response.start received"
    return status_code, headers, bytes(body)


async def _drive_stream_chunks(
    app: FastAPI,
    path: str,
    headers_list: list[tuple[str, str]],
    *,
    park_timeout: float = 0.5,
) -> tuple[int, list, list[bytes]]:
    """Drive the ASGI app capturing individual ``http.response.body`` chunks.

    NB-D2 per-chunk gzip alignment proof: unlike :func:`_drive_stream` (which
    concatenates into one bytearray), this preserves each ASGI body message's
    ``body`` field as a separate list entry so a test can feed each chunk
    through a persistent ``decompressobj`` and assert flush-aligned SSE
    event boundaries per-chunk (not just whole-body).
    """
    # Split query string off the path (parity with _drive_stream).
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
    }
    status_code = 0
    headers: list = []
    chunks: list[bytes] = []
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
            chunks.append(message.get("body", b""))

    task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=park_timeout)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    await asyncio.sleep(0)
    assert got_response, "no http.response.start received"
    return status_code, headers, chunks


async def _get(app: FastAPI, path: str, extra_headers: dict[str, str] | None = None):
    headers = dict(VERSION_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


# ===========================================================================
# TokenSubscriber — T3 guards + sessionID resync + dropped_frames_total (NB-C5)
# ===========================================================================

class TestTokenSubscriber:
    def test_put_enqueues_frame_and_counts_bytes(self):
        th = TokenStreamHub()
        sub = TokenSubscriber(
            session_id="s1", metrics=th._metrics,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "hi")
        assert sub.put(frame) is True
        assert sub.queued_bytes == len(frame)
        assert not sub.closed
        assert th.dropped_frames_total == 0

    def test_ack_decrements_bytes_floored_at_zero(self):
        th = TokenStreamHub()
        sub = TokenSubscriber(
            session_id="s1", metrics=th._metrics,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "hi")
        sub.put(frame)
        sub.ack(frame)
        assert sub.queued_bytes == 0
        # ack of STOP is a no-op.
        sub.ack(STOP)
        assert sub.queued_bytes == 0

    def test_oversized_frame_drops_and_bumps_metric(self):
        """A single frame larger than max_frame_bytes is dropped (not
        enqueued) and bumps dropped_frames_total (NB-C5)."""
        th = TokenStreamHub()
        sub = TokenSubscriber(
            session_id="s1", metrics=th._metrics,
            queue_items=64, buffer_bytes=4096, max_frame_bytes=50,
        )
        big = _delta_frame(("s1", "m1", "p1"), "x" * 1000)
        assert sub.put(big) is False
        assert sub.dropped_frames == 1
        assert th.dropped_frames_total == 1
        assert not sub.closed  # oversized drop does NOT close the sub

    def test_queue_item_overflow_disconnects_with_sessionid_resync(self):
        """§16-D: queue overflow → resync{subscriber_backpressure, sessionID}
        + STOP + disconnect. The resync MUST carry sessionID (control-plane
        omits it; token subs are per-session)."""
        th = TokenStreamHub()
        sub = TokenSubscriber(
            session_id="s1", metrics=th._metrics,
            queue_items=2, buffer_bytes=4096, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "a")
        # Fill the queue (2 items).
        assert sub.put(frame)
        assert sub.put(frame)
        # Third put → overflow.
        assert sub.put(frame) is False
        assert sub.closed
        assert sub.forced_disconnects == 1
        assert th.dropped_frames_total == 1  # NB-C5
        # Queue now holds exactly resync + STOP (cleared first).
        drained = []
        while not sub.queue.empty():
            drained.append(sub.queue.get_nowait())
        assert drained[0] is not STOP
        ev, data = parse_event(drained[0])
        assert ev == "resync"
        assert data == {"reason": "subscriber_backpressure", "sessionID": "s1"}
        assert drained[1] is STOP

    def test_buffer_byte_overflow_disconnects(self):
        """Buffer-byte cap (not item count) also triggers disconnect."""
        th = TokenStreamHub()
        sub = TokenSubscriber(
            session_id="s1", metrics=th._metrics,
            queue_items=64, buffer_bytes=20, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "0123456789")  # ~40 bytes
        assert sub.put(frame) is False
        assert sub.closed
        assert sub.forced_disconnects == 1

    def test_closed_sub_silently_drops_subsequent_puts(self):
        th = TokenStreamHub()
        sub = TokenSubscriber(
            session_id="s1", metrics=th._metrics,
            queue_items=2, buffer_bytes=4096, max_frame_bytes=1024,
        )
        frame = _delta_frame(("s1", "m1", "p1"), "a")
        sub.put(frame)
        sub.put(frame)
        sub.put(frame)  # overflow → closed
        before = th.dropped_frames_total
        # Subsequent puts silently drop, do NOT re-bump the metric and do NOT
        # re-enqueue another resync (only one terminal pair).
        assert sub.put(frame) is False
        assert th.dropped_frames_total == before
        # Still only one resync on the queue.
        resyncs = 0
        while not sub.queue.empty():
            item = sub.queue.get_nowait()
            if item is not STOP:
                resyncs += 1
        assert resyncs == 1


# ===========================================================================
# TokenStreamRegistry — admission cap + independent ledger + lifecycle
# ===========================================================================

class TestTokenStreamRegistryAdmission:
    async def test_admit_under_cap(self):
        app = _build_app(_settings(token_stream_max_subscribers=2))
        try:
            reg = app.state.token_registry
            sub = reg.subscribe("s1")
            assert isinstance(sub, TokenSubscriber)
            assert reg.total_subscribers == 1
            assert sub.session_id == "s1"
        finally:
            await _close_app(app)

    async def test_reject_over_cap_raises_capacity_error(self):
        app = _build_app(_settings(token_stream_max_subscribers=1))
        try:
            reg = app.state.token_registry
            reg.subscribe("s1")
            with pytest.raises(TokenSubscriberCapacityError) as exc_info:
                reg.subscribe("s2")
            assert exc_info.value.code == "sse_token_subscriber_limit"
            assert exc_info.value.limit == 1
            assert exc_info.value.current == 1
            assert reg.rejected_total == 1
            # The rejected sub was NOT admitted.
            assert reg.total_subscribers == 1
        finally:
            await _close_app(app)

    async def test_independent_ledger_does_not_consume_max_total(self):
        """Token subscribers MUST NOT count against the control-plane
        ``MAX_TOTAL_SUBSCRIBERS`` (design §6)."""
        app = _build_app(_settings(
            token_stream_max_subscribers=3,
            max_total_subscribers=2,
        ))
        try:
            reg = app.state.token_registry
            hubs = app.state.hubs
            # Admit 2 token subs — exceeds max_total_subscribers(2) but the
            # token ledger is independent.
            reg.subscribe("s1")
            reg.subscribe("s2")
            assert reg.total_subscribers == 2
            assert hubs.total_subscribers == 0  # control-plane untouched
            # A 3rd token sub is still admissible (token cap=3).
            reg.subscribe("s3")
            assert reg.total_subscribers == 3
            assert hubs.total_subscribers == 0
        finally:
            await _close_app(app)

    async def test_unsubscribe_decrements_and_stops_flush_on_last_detach(self):
        app = _build_app(_settings(token_stream_max_subscribers=3))
        try:
            reg = app.state.token_registry
            th = app.state.token_hub
            s1 = reg.subscribe("s1")
            s2 = reg.subscribe("s2")
            assert th._flush_task is not None  # running (first-attach)
            reg.unsubscribe(s1)
            assert reg.total_subscribers == 1
            assert th._flush_task is not None  # still running (s2 attached)
            reg.unsubscribe(s2)  # last-detach
            assert reg.total_subscribers == 0
            assert th._flush_task is None  # stopped (NB-C4)
        finally:
            await _close_app(app)

    async def test_unsubscribe_idempotent_floor_zero(self):
        app = _build_app(_settings())
        try:
            reg = app.state.token_registry
            reg.unsubscribe(TokenSubscriber(session_id="x", metrics=app.state.token_hub._metrics))
            assert reg.total_subscribers == 0
        finally:
            await _close_app(app)

    async def test_first_attach_starts_flush_loop(self):
        """NB-C4: the production path was ingest-only (no flush) until the
        first subscriber; subscribe() must now start the flush loop."""
        app = _build_app(_settings())
        try:
            th = app.state.token_hub
            assert th._flush_task is None  # not started before any sub
            reg = app.state.token_registry
            sub = reg.subscribe("s1")
            try:
                assert th._flush_task is not None
                assert not th._flush_task.done()
            finally:
                reg.unsubscribe(sub)
            assert th._flush_task is None  # stopped on last-detach
        finally:
            await _close_app(app)

    async def test_attach_failure_does_not_increment_ledger(self):
        """MAJOR 4: if attach_subscriber leaves sub.closed=True (defensive
        early-exit, oversized-frame guard armed mid-handshake, or a future
        Lane-A change), subscribe() must NOT increment total_subscribers —
        the sub never entered fanout so unsubscribe() would be a no-op
        against the membership guard and the slot would leak forever
        (registry drift / admission skew). The route maps the raised
        capacity error to 503 + Retry-After."""
        app = _build_app(_settings())
        try:
            reg = app.state.token_registry
            th = app.state.token_hub
            original_attach = th.attach_subscriber

            def closing_attach(sid, sub):
                original_attach(sid, sub)
                sub.closed = True  # simulate post-attach defensive close

            th.attach_subscriber = closing_attach  # type: ignore[method-assign]
            try:
                with pytest.raises(TokenSubscriberCapacityError):
                    reg.subscribe("s1")
                # MAJOR 4: ledger NOT incremented.
                assert reg.total_subscribers == 0
                # The rejection IS counted (parity with cap-overflow path).
                assert reg.rejected_total == 1
            finally:
                th.attach_subscriber = original_attach  # type: ignore[method-assign]
        finally:
            await _close_app(app)

    async def test_attach_failure_rolls_back_flush_loop_and_grace(self, monkeypatch):
        """MAJOR 5: attach failure must roll back the subscribe preamble's
        side effects — flush loop stop (iff no other subs) + GlobalHub grace
        re-arm — so no ghost subscriber / leaked flush loop / orphaned
        upstream connection remains (B-D1 ghost-resource leak).

        This is the integration-level proof (real HubRegistry + GlobalHub):
        the unit-level ``test_attach_failure_rolls_back_flush_loop`` in
        ``test_token_subscriber_overflow.py`` covers the flush-stop with a
        stub hub; here we verify the full chain including grace re-arm.
        """
        monkeypatch.setattr("oc_slimapi.sse.hub.GRACE_SECONDS", 0.0)
        app = _build_app(_settings())
        try:
            reg = app.state.token_registry
            th = app.state.token_hub
            hubs = app.state.hubs
            original_attach = th.attach_subscriber

            def closing_attach(sid, sub):
                original_attach(sid, sub)
                sub.closed = True

            th.attach_subscriber = closing_attach  # type: ignore[method-assign]
            try:
                # Pre-attach: no flush task, no global hub yet.
                assert th._flush_task is None

                with pytest.raises(TokenSubscriberCapacityError):
                    reg.subscribe("s1")

                # MAJOR 4: ledger not incremented.
                assert reg.total_subscribers == 0
                # MAJOR 5: flush loop STOPPED (no ghost flush task).
                assert th._flush_task is None, (
                    "MAJOR 5: flush loop must stop after attach failure"
                )
                # MAJOR 5: no ghost subscriber in _subs_by_sid.
                assert not th._subs_by_sid, (
                    "MAJOR 5: _subs_by_sid must be empty after attach failure"
                )
                # MAJOR 5: GlobalHub grace RE-ARMED (subscribe cancelled it
                # on entry; rollback must re-arm so the hub tears down
                # instead of parking on aiter_lines forever).
                # The upstream ensure created a hub; rollback re-armed
                # grace → _removal_task scheduled (0.0s grace → fires
                # immediately on await).
                removal = hubs._removal_task
                assert removal is not None, (
                    "MAJOR 5: grace-removal must be re-armed after attach failure"
                )
                await removal  # grace fires → teardown
                # Hub torn down (no leak).
                assert hubs._global is None, (
                    "MAJOR 5: GlobalHub must be torn down after grace re-arm"
                )
            finally:
                th.attach_subscriber = original_attach  # type: ignore[method-assign]
        finally:
            await _close_app(app)

    async def test_attach_failure_keeps_flush_loop_if_sibling_subscriber(self):
        """MAJOR 5: if another token subscriber is already attached, the
        flush loop must keep running on a sibling's attach failure (the
        sibling does not affect the attached sub's lifecycle)."""
        app = _build_app(_settings(token_stream_max_subscribers=3))
        try:
            reg = app.state.token_registry
            th = app.state.token_hub
            # First sub attaches successfully.
            ok_sub = reg.subscribe("s1")
            try:
                assert th._flush_task is not None
                original_attach = th.attach_subscriber

                def closing_attach(sid, sub):
                    original_attach(sid, sub)
                    sub.closed = True

                th.attach_subscriber = closing_attach  # type: ignore[method-assign]
                try:
                    # Second sub attach fails.
                    with pytest.raises(TokenSubscriberCapacityError):
                        reg.subscribe("s2")
                    # MAJOR 4: failed sub did not increment ledger.
                    assert reg.total_subscribers == 1
                    # MAJOR 5: flush loop STILL running (s1 keeps it alive).
                    assert th._flush_task is not None, (
                        "MAJOR 5: flush loop must NOT stop while a sibling is attached"
                    )
                finally:
                    th.attach_subscriber = original_attach  # type: ignore[method-assign]
            finally:
                reg.unsubscribe(ok_sub)
        finally:
            await _close_app(app)


# ===========================================================================
# NB-B1 — token subscribe cancels a pending registry grace-removal
# ===========================================================================

class TestNBB1CancelGraceRemoval:
    async def test_token_subscribe_cancels_pending_removal_task(self):
        """A token subscriber arriving during the GRACE_SECONDS idle window
        must not have its hub torn down (NB-B1, design §5.2 / §16-B)."""
        app = _build_app(_settings())
        try:
            hubs = app.state.hubs
            reg = app.state.token_registry
            # Control-plane sub arms the upstream; unsubscribe arms grace removal.
            ctrl = hubs.subscribe()
            hub_ref = hubs.get_global()
            hubs.unsubscribe(ctrl)
            assert hubs._removal_task is not None  # grace-removal armed
            # Token subscriber arrives during grace.
            tsub = reg.subscribe("s1")
            try:
                # Grace removal cancelled (NB-B1).
                assert hubs._removal_task is None
                # Same hub instance survives (not torn down / recreated).
                assert hubs.get_global() is hub_ref
                assert reg.total_subscribers == 1
            finally:
                reg.unsubscribe(tsub)
        finally:
            await _close_app(app)


# ===========================================================================
# B-D1 / NB-D1 / NB-D3 — token unsubscribe grace symmetry + idempotency
# ===========================================================================

class TestBGraceSymmetry:
    """Stage-D gate fix: token-only last-detach must arm the GlobalHub grace
    symmetrically with subscribe's ``cancel_pending_removal`` /
    ``ensure_upstream`` (B-D1), ``TokenStreamRegistry.unsubscribe`` must be
    truly idempotent on the SAME sub (NB-D1), and ``HubRegistry.unsubscribe``
    must arm on the unified ``has_consumers()`` predicate spanning BOTH
    ledgers (NB-D3)."""

    async def test_token_only_last_detach_arms_grace_and_tears_down(self, monkeypatch):
        """B-D1 scenario A (production main path): open token stream →
        ensure_upstream → unsubscribe → flush stop → ``run()`` would otherwise
        park on ``aiter_lines`` forever. The token last-detach must arm the
        registry grace so the hub tasks + ``_global`` reference are released."""
        monkeypatch.setattr("oc_slimapi.sse.hub.GRACE_SECONDS", 0.0)
        app = _build_app(_settings())
        try:
            hubs = app.state.hubs
            reg = app.state.token_registry
            tsub = reg.subscribe("s1")
            hub_ref = hubs.get_global()
            assert hub_ref.task is not None and not hub_ref.task.done()
            reg.unsubscribe(tsub)          # token-only last-detach
            assert reg.total_subscribers == 0
            task = hubs._removal_task
            assert task is not None        # B-D1: grace armed
            await task                     # grace fires (0.0s) → teardown
            # Let the cancelled hub tasks wind down before asserting done().
            for t in (hub_ref.task, hub_ref.flush_task, hub_ref.heartbeat_task):
                if t is not None:
                    await asyncio.gather(t, return_exceptions=True)
            assert hubs._global is None    # reference released → no leak
            assert hub_ref.task.done()     # run() torn down → no leak
        finally:
            await _close_app(app)

    async def test_control_unsub_then_token_arrives_then_token_leaves_tears_down(
        self, monkeypatch,
    ):
        """B-D1 scenario B: control-plane last-unsub arms grace → token arrives
        during grace (cancels it, NB-B1) → token last-detach must RE-ARM grace
        → teardown. The pre-fix bug: token last-detach never re-armed → leak."""
        monkeypatch.setattr("oc_slimapi.sse.hub.GRACE_SECONDS", 0.0)
        app = _build_app(_settings())
        try:
            hubs = app.state.hubs
            reg = app.state.token_registry
            ctrl = hubs.subscribe()
            hub_ref = hubs.get_global()
            hubs.unsubscribe(ctrl)         # control-plane last-unsub → arm task1
            task1 = hubs._removal_task
            assert task1 is not None
            tsub = reg.subscribe("s1")      # arrives during grace → cancels (NB-B1)
            assert hubs._removal_task is None
            assert hubs.get_global() is hub_ref   # hub survived
            with contextlib.suppress(asyncio.CancelledError):
                await task1                # cancelled task1 winds down cleanly
            reg.unsubscribe(tsub)          # token last-detach → RE-ARM (B-D1)
            task2 = hubs._removal_task
            assert task2 is not None
            await task2                    # grace fires → teardown
            for t in (hub_ref.task, hub_ref.flush_task, hub_ref.heartbeat_task):
                if t is not None:
                    await asyncio.gather(t, return_exceptions=True)
            assert hubs._global is None
            assert hub_ref.task.done()
        finally:
            await _close_app(app)

    async def test_control_last_unsub_with_token_present_does_not_arm(self):
        """NB-D3: when the last control-plane sub leaves but a token sub
        remains, ``has_consumers()`` is True → do NOT arm a doomed-to-no-op
        grace task. The token last-detach arms it (B-D1)."""
        app = _build_app(_settings())
        try:
            hubs = app.state.hubs
            reg = app.state.token_registry
            tsub = reg.subscribe("s1")     # token present first
            ctrl = hubs.subscribe()
            hub_ref = hubs.get_global()
            hubs.unsubscribe(ctrl)         # control last-unsub; token still active
            assert hubs._removal_task is None   # NB-D3: NOT armed
            assert hubs.get_global() is hub_ref  # hub still alive
            reg.unsubscribe(tsub)          # token leaves → NOW armed (B-D1)
            assert hubs._removal_task is not None
        finally:
            await _close_app(app)

    async def test_unsubscribe_same_sub_twice_decrements_once(self):
        """NB-D1: double-unsubscribe of the SAME sub must decrement
        ``total_subscribers`` exactly once. The pre-fix guard
        (``total_subscribers <= 0``) let the second call double-decrement →
        ledger drift / flush mis-stop / admission skew."""
        app = _build_app(_settings(token_stream_max_subscribers=3))
        try:
            reg = app.state.token_registry
            th = app.state.token_hub
            s1 = reg.subscribe("s1")
            s2 = reg.subscribe("s2")
            assert reg.total_subscribers == 2
            reg.unsubscribe(s1)
            assert reg.total_subscribers == 1
            reg.unsubscribe(s1)            # NB-D1: same sub again → no-op
            assert reg.total_subscribers == 1   # NOT 0 → no drift
            assert th._flush_task is not None   # flush still running (s2 attached)
            reg.unsubscribe(s2)            # real last-detach
            assert reg.total_subscribers == 0
            assert th._flush_task is None
        finally:
            await _close_app(app)

    async def test_unsubscribe_unknown_sub_is_noop_no_exception(self):
        """Idempotency: a never-attached ``TokenSubscriber`` (even for a sid
        that DOES have a real sub) is a no-op — the membership guard (identity
        based) rejects it before touching the ledger."""
        app = _build_app(_settings())
        try:
            reg = app.state.token_registry
            real = reg.subscribe("s1")
            unknown = TokenSubscriber(
                session_id="s1", metrics=app.state.token_hub._metrics,
            )
            reg.unsubscribe(unknown)       # NB-D1 membership guard → no-op
            assert reg.total_subscribers == 1   # real still counted
            reg.unsubscribe(real)
            assert reg.total_subscribers == 0
        finally:
            await _close_app(app)


# ===========================================================================
# HTTP endpoint — handshake ordering, Last-Event-ID, no id:, version gate
# ===========================================================================

class TestTokenStreamHandshake:
    async def test_handshake_emits_connected_then_snapshot(self):
        """§5.5: server.connected{sessionID} first, then snapshot{done:false}
        for each active LivePart."""
        app = _build_app(_settings())
        try:
            th = app.state.token_hub
            # Seed an active part BEFORE subscribe so the handshake snapshots it.
            th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
            th.on_part_delta(_delta_props("s1", "m1", "p1", delta="accumulated"))
            status, headers, body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity")],
            )
            assert status == 200
            events = parse_sse_stream(body)
            # First frame: server.connected bound to s1.
            assert events[0][0] == "server.connected"
            assert events[0][1] == {"sessionID": "s1"}
            # Then a snapshot with the full accumulated text.
            snaps = [e for e in events if e[0] == "message.part.snapshot"]
            assert len(snaps) == 1
            assert snaps[0][1]["text"] == "accumulated"
            assert snaps[0][1]["done"] is False
            # After disconnect the subscriber was detached.
            assert app.state.token_registry.total_subscribers == 0
        finally:
            await _close_app(app)

    async def test_last_event_id_emits_leading_reconnect_no_replay(self):
        """Last-Event-ID (value ignored) → leading resync{reconnect_no_replay,
        sessionID} BEFORE server.connected (§5.5 step 1)."""
        app = _build_app(_settings())
        try:
            status, headers, body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [
                    ("X-Slimapi-Version", "1"),
                    ("Accept-Encoding", "identity"),
                    ("Last-Event-ID", "anything-ignored"),
                ],
            )
            assert status == 200
            events = parse_sse_stream(body)
            assert events[0][0] == "resync"
            assert events[0][1] == {"reason": "reconnect_no_replay", "sessionID": "s1"}
            # server.connected comes right after.
            assert events[1][0] == "server.connected"
        finally:
            await _close_app(app)

    async def test_no_sse_id_field_in_frames(self):
        """Contract: token stream NEVER emits an SSE ``id:`` field (no replay
        buffer; clients must not rely on id for resumption)."""
        app = _build_app(_settings())
        try:
            _status, _headers, body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity")],
            )
            for line in body.decode().split("\n"):
                assert not line.lower().startswith("id:"), \
                    f"SSE id: field present: {line!r}"
        finally:
            await _close_app(app)

    async def test_response_headers_identity(self):
        app = _build_app(_settings())
        try:
            status, headers, _body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity")],
            )
            assert status == 200
            hdr = {k.decode().lower(): v.decode() for k, v in headers}
            assert hdr["content-type"] == "text/event-stream; charset=utf-8"
            assert hdr["cache-control"] == "no-cache, no-transform"
            assert hdr["x-accel-buffering"] == "no"
            assert hdr["vary"] == "Accept-Encoding"
            assert hdr["x-slimapi-subscriber-id"].startswith("tok_")
            # identity → no Content-Encoding.
            assert "content-encoding" not in hdr
        finally:
            await _close_app(app)

    async def test_version_gate_rejects_missing_header(self):
        app = _build_app(_settings())
        try:
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # No X-Slimapi-Version header → middleware 400 (complete
                # JSON response; does not reach the streaming route).
                response = await client.get(
                    "/slimapi/sessions/s1/stream",
                    headers={"Accept-Encoding": "identity"},
                )
            assert response.status_code == 400
            assert response.json()["code"] == "version_required"
        finally:
            await _close_app(app)

    async def test_admission_overflow_returns_503(self):
        app = _build_app(_settings(token_stream_max_subscribers=1))
        try:
            reg = app.state.token_registry
            # Pre-fill the token ledger so the HTTP request overflows.
            holder = reg.subscribe("s1")
            try:
                response = await _get(app, "/slimapi/sessions/s2/stream",
                                      extra_headers={"Accept-Encoding": "identity"})
                assert response.status_code == 503
                assert response.headers["Retry-After"] == "5"
                body = response.json()
                assert body["code"] == "sse_token_subscriber_limit"
                assert body["limit"] == 1
                assert body["current"] == 1
            finally:
                reg.unsubscribe(holder)
        finally:
            await _close_app(app)

    async def test_handshake_overflow_returns_sse_token_handshake_overflow(
        self, monkeypatch,
    ):
        """Handshake buffer overflow → 503 with ``sse_token_handshake_overflow``
        code (not ``sse_token_subscriber_limit``)."""
        from oc_slimapi.sse.tokenstream.subscriber import _SubscriberQueue

        original_init = _SubscriberQueue.__init__

        def tiny_handshake_init(self, *, runtime_max_items, handshake_max_items, handshake_max_bytes):
            original_init(
                self,
                runtime_max_items=runtime_max_items,
                handshake_max_items=0,         # force handshake overflow on first put
                handshake_max_bytes=handshake_max_bytes,
            )

        monkeypatch.setattr(_SubscriberQueue, "__init__", tiny_handshake_init)
        app = _build_app(_settings(token_stream_max_subscribers=2))
        try:
            response = await _get(app, "/slimapi/sessions/s1/stream",
                                  extra_headers={"Accept-Encoding": "identity"})
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "5"
            body = response.json()
            assert body["code"] == "sse_token_handshake_overflow"
            assert body["limit"] == 2
            assert body["current"] == 0
            assert body["bufferBytes"] == 8 * 1024 * 1024
        finally:
            await _close_app(app)

    async def test_disconnect_detaches_subscriber(self):
        """Cancelling the stream (client disconnect) unwinds the generator's
        finally → unsubscribe, returning the ledger to zero."""
        app = _build_app(_settings())
        try:
            await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity")],
            )
            assert app.state.token_registry.total_subscribers == 0
            # And the flush loop stopped (last-detach).
            assert app.state.token_hub._flush_task is None
        finally:
            await _close_app(app)


# ===========================================================================
# Lever 2 — streaming gzip Z_SYNC_FLUSH aligned to SSE event boundaries (§7)
# ===========================================================================

class TestGzipLever2:
    def test_flush_aligns_to_event_boundaries_unit(self):
        """Unit proof: compressing each complete SSE frame with Z_SYNC_FLUSH
        means a decompressor fed chunk-by-chunk sees only whole events at
        every flush boundary (no half ``data:`` line)."""
        frames = [
            _connected_frame("s1"),
            _snapshot_frame(("s1", "m1", "p1"), "hello", done=False),
            _delta_frame(("s1", "m1", "p1"), "chunk"),
        ]
        compressor = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
        per_flush: list[bytes] = []
        for f in frames:
            per_flush.append(compressor.compress(f) + compressor.flush(zlib.Z_SYNC_FLUSH))
        # Feed each flush boundary to a persistent decompressobj; after each,
        # accumulated output must end on an event boundary (b"\n\n").
        d = zlib.decompressobj(zlib.MAX_WBITS | 16)
        accumulated = b""
        for chunk in per_flush:
            accumulated += d.decompress(chunk)
            assert accumulated.endswith(b"\n\n"), \
                "gzip flush did not align to an SSE event boundary"
        # Full round-trip equals the concatenated frames.
        assert accumulated == b"".join(frames)

    async def test_endpoint_negotiates_gzip_content_encoding(self):
        """Accept-Encoding: gzip → Content-Encoding: gzip + Vary header."""
        app = _build_app(_settings())
        try:
            status, headers, _body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "gzip")],
            )
            assert status == 200
            hdr = {k.decode().lower(): v.decode() for k, v in headers}
            assert hdr["content-encoding"] == "gzip"
            assert hdr["vary"] == "Accept-Encoding"
        finally:
            await _close_app(app)

    async def test_endpoint_gzip_body_decompresses_to_complete_events(self):
        """The raw gzip body round-trips through a decompressobj into whole
        SSE handshake frames (server.connected + snapshot)."""
        app = _build_app(_settings())
        try:
            th = app.state.token_hub
            th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
            th.on_part_delta(_delta_props("s1", "m1", "p1", delta="seeded"))
            _status, _headers, raw = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "gzip")],
            )
            # Raw body is gzip (magic bytes).
            assert raw[:2] == b"\x1f\x8b"
            d = zlib.decompressobj(zlib.MAX_WBITS | 16)
            decompressed = d.decompress(raw) + d.flush()
            events = parse_sse_stream(decompressed)
            assert events[0][0] == "server.connected"
            snaps = [e for e in events if e[0] == "message.part.snapshot"]
            assert snaps and snaps[0][1]["text"] == "seeded"
        finally:
            await _close_app(app)

    async def test_identity_body_is_not_gzipped(self):
        """Accept-Encoding: identity → no gzip magic, body is plaintext SSE."""
        app = _build_app(_settings())
        try:
            _status, _headers, raw = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity")],
            )
            assert raw[:2] != b"\x1f\x8b"
            events = parse_sse_stream(raw)
            assert events[0][0] == "server.connected"
        finally:
            await _close_app(app)

    async def test_control_plane_events_is_not_gzipped(self):
        """Lever 2 is the SOLE SSE gzip exception — the control-plane
        ``/slimapi/events`` must NOT set Content-Encoding: gzip (§7)."""
        app = _build_app(_settings(), include_control_events=True)
        try:
            status, headers, body = await _drive_stream(
                app, "/slimapi/events",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "gzip")],
            )
            assert status == 200
            hdr = {k.decode().lower(): v.decode() for k, v in headers}
            assert "content-encoding" not in hdr
            # Body is plaintext SSE (server.connected control-plane frame).
            assert body[:2] != b"\x1f\x8b"
            events = parse_sse_stream(body)
            assert events[0][0] == "server.connected"
        finally:
            await _close_app(app)

    async def test_endpoint_gzip_flush_aligns_per_chunk(self):
        """NB-D2 (per-chunk flush proof): each ASGI ``http.response.body``
        chunk fed through a persistent ``decompressobj`` decompresses to
        output that ``endswith(b"\\n\\n")`` — a whole SSE event boundary.
        This mirrors the unit-level ``test_flush_aligns_to_event_boundaries_unit``
        but exercises the real endpoint, proving ``Z_SYNC_FLUSH`` alignment
        is per-event in the live ASGI body stream (not just a happy-path
        whole-body artefact)."""
        app = _build_app(_settings())
        try:
            th = app.state.token_hub
            th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
            th.on_part_delta(_delta_props("s1", "m1", "p1", delta="seeded"))
            _status, _headers, chunks = await _drive_stream_chunks(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "gzip")],
            )
            d = zlib.decompressobj(zlib.MAX_WBITS | 16)
            non_empty = 0
            for chunk in chunks:
                if not chunk:
                    continue
                decompressed = d.decompress(chunk)
                if not decompressed:
                    # First chunk may carry only the gzip header (zlib emits
                    # the 10-byte header on the first compress call before any
                    # data); skip chunks with no decompressed payload.
                    continue
                non_empty += 1
                assert decompressed.endswith(b"\n\n"), \
                    f"chunk did not align to an SSE event boundary: {decompressed!r}"
            # At least one non-empty decompressed chunk was asserted.
            assert non_empty >= 1, "no non-empty decompressed chunks captured"
        finally:
            await _close_app(app)
# ===========================================================================

class TestHealthFeaturesTokenStream:
    async def test_health_root_features_token_stream_true(self):
        """Q1 freeze: features.tokenStream is TOP-LEVEL (parallel to
        sidecar/server/schema), not nested under server.*."""
        app = _build_app(_settings())
        try:
            response = await _get(app, "/slimapi/health")
            assert response.status_code == 200
            body = response.json()
            assert "features" in body
            # tokenStream is a top-level feature key (sibling of the
            # additive thresholdedSkeleton diagnostic — not an exact-dict
            # equality so future additive feature keys don't break this).
            assert body["features"]["tokenStream"] is True
            # It is a sibling of sidecar/server/schema (not nested).
            assert set(body) >= {"sidecar", "server", "schema", "features"}
        finally:
            await _close_app(app)


class TestMetricsTokenStream:
    async def test_metrics_exposes_sse_token_stream_block(self):
        app = _build_app(_settings(token_stream_max_subscribers=3))
        try:
            reg = app.state.token_registry
            sub = reg.subscribe("s1")
            try:
                response = await _get(app, "/slimapi/metrics")
                assert response.status_code == 200
                ts = response.json()["sse"]["tokenStream"]
                assert set(ts) == {
                    "current", "limit", "rejectedTotal",
                    "pendingAccumulators", "flushedFramesTotal",
                    "droppedFramesTotal", "truncatedSnapshotsTotal",
                    "orphanDeltasTotal", "tokenMemoryLimitTotal",
                    "gzipRawBytesTotal", "gzipCompressedBytesTotal",
                    "flushDurationMsTotal", "flushTicksTotal",
                    "maxSubscriberQueueDepth",
                }
                assert ts["current"] == 1
                assert ts["limit"] == 3
                assert ts["rejectedTotal"] == 0
            finally:
                reg.unsubscribe(sub)
        finally:
            await _close_app(app)

    async def test_metrics_reflects_rejected_and_counters(self):
        app = _build_app(_settings(token_stream_max_subscribers=1))
        try:
            reg = app.state.token_registry
            th = app.state.token_hub
            # Admit one (fills the cap=1), then the next overflows.
            holder = reg.subscribe("s1")
            try:
                with pytest.raises(TokenSubscriberCapacityError):
                    reg.subscribe("overflow")
            finally:
                reg.unsubscribe(holder)
            # Bump some counters directly via ingest.
            th.on_part_delta(_delta_props(delta="orphan"))  # orphan drop
            assert th.orphan_deltas == 1
            response = await _get(app, "/slimapi/metrics")
            ts = response.json()["sse"]["tokenStream"]
            assert ts["rejectedTotal"] == 1
            assert ts["orphanDeltasTotal"] == 1
        finally:
            await _close_app(app)

    async def test_dropped_frames_total_bumped_on_overflow(self):
        """NB-C5: a subscriber overflow bumps dropped_frames_total and it is
        surfaced on /slimapi/metrics."""
        app = _build_app(_settings())
        try:
            reg = app.state.token_registry
            th = app.state.token_hub
            sub = reg.subscribe("s1")
            try:
                # Overflow the sub manually (queue_items default 64).
                frame = _delta_frame(("s1", "m1", "p1"), "a")
                for _ in range(sub.queue_items + 5):
                    sub.put(frame)
                assert th.dropped_frames_total >= 1
                response = await _get(app, "/slimapi/metrics")
                assert response.json()["sse"]["tokenStream"]["droppedFramesTotal"] >= 1
            finally:
                reg.unsubscribe(sub)
        finally:
            await _close_app(app)

    async def test_metrics_absent_without_token_registry(self):
        """Control-plane metrics shape is unchanged when no token registry is
        wired (test app parity with test_metrics.py)."""
        app = FastAPI(title="no-token")
        app.add_middleware(
            SlimapiVersionMiddleware, accepted_client_versions=(1, 1),
        )
        app.state.config = _settings()
        app.state.upstream = httpx.AsyncClient()
        hubs = HubRegistry(client=None)
        app.state.hubs = hubs
        app.include_router(metrics.router)
        register_error_handlers(app)
        try:
            response = await _get(app, "/slimapi/metrics")
            sse = response.json()["sse"]
            assert set(sse) == {"subscribers", "hubs", "clients"}
            assert "tokenStream" not in sse
        finally:
            await hubs.close()
            await app.state.upstream.aclose()

    async def test_gzip_flush_bumps_compression_counters(self):
        """T2-C1: after a gzip flush, gzipRawBytesTotal>0 and
        gzipCompressedBytesTotal>0."""
        app = _build_app(_settings())
        try:
            th = app.state.token_hub
            # Seed a part so the handshake snapshot has data.
            th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
            th.on_part_delta(_delta_props("s1", "m1", "p1", delta="hello" * 100))
            # Drive the gzip stream.
            _status, _headers, raw = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "gzip")],
            )
            # The stream round-tripped, so at least one raw frame was compressed.
            response = await _get(app, "/slimapi/metrics")
            ts = response.json()["sse"]["tokenStream"]
            assert ts["gzipRawBytesTotal"] > 0
            assert ts["gzipCompressedBytesTotal"] > 0
        finally:
            await _close_app(app)

    async def test_non_gzip_does_not_bump_gzip_counters(self):
        """T2-C2: identity (non-gzip) connection does NOT bump gzip counters."""
        app = _build_app(_settings())
        try:
            th = app.state.token_hub
            th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
            th.on_part_delta(_delta_props("s1", "m1", "p1", delta="data"))
            _status, _headers, _body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity")],
            )
            response = await _get(app, "/slimapi/metrics")
            ts = response.json()["sse"]["tokenStream"]
            assert ts["gzipRawBytesTotal"] == 0
            assert ts["gzipCompressedBytesTotal"] == 0
        finally:
            await _close_app(app)

    async def test_flush_ticks_and_duration_metrics(self):
        """T2-C3: a flush bumps flushTicksTotal and records a STRICTLY
        increasing wall-clock duration (not the always-true >= 0)."""
        app = _build_app(_settings())
        try:
            th = app.state.token_hub
            # Seed a part + pending delta so flush() does real drain work.
            th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
            th.on_part_delta(_delta_props("s1", "m1", "p1", delta="x" * 200))
            before = th._metrics.flush_duration_ms_total
            ticks_before = th._metrics.flush_ticks_total
            th.flush()  # synchronous drain
            after = th._metrics.flush_duration_ms_total
            ticks_after = th._metrics.flush_ticks_total
            assert ticks_after == ticks_before + 1
            assert after > before, (
                "flushDurationMsTotal must strictly increase after a real flush"
            )
            # Surfaced via /slimapi/metrics too.
            response = await _get(app, "/slimapi/metrics")
            ts = response.json()["sse"]["tokenStream"]
            assert ts["flushTicksTotal"] >= 1
            assert ts["flushDurationMsTotal"] > 0
        finally:
            await _close_app(app)

    async def test_max_subscriber_queue_depth_value_level(self):
        """2-M2: maxSubscriberQueueDepth is a LIVE gauge of attached subs'
        queue depth — it grows as frames enqueue (no drain) and drops to 0
        once no sub is attached (value-level, not just key-presence)."""
        app = _build_app(_settings(token_stream_max_subscribers=3))
        try:
            reg = app.state.token_registry
            sub = reg.subscribe("s1")
            try:
                resp0 = await _get(app, "/slimapi/metrics")
                d0 = resp0.json()["sse"]["tokenStream"]["maxSubscriberQueueDepth"]
                # Enqueue 5 frames directly; no HTTP generator is draining in
                # this test, so qsize grows by exactly 5.
                frame = _delta_frame(("s1", "m1", "p1"), "a")
                for _ in range(5):
                    sub.put(frame)
                resp1 = await _get(app, "/slimapi/metrics")
                d1 = resp1.json()["sse"]["tokenStream"]["maxSubscriberQueueDepth"]
                assert d1 >= d0 + 5, (
                    f"depth {d1} should reflect 5 enqueued frames (was {d0})"
                )
            finally:
                reg.unsubscribe(sub)
            # Last sub detached → depth 0.
            resp2 = await _get(app, "/slimapi/metrics")
            assert resp2.json()["sse"]["tokenStream"]["maxSubscriberQueueDepth"] == 0
        finally:
            await _close_app(app)


# ===========================================================================
# NB-C1 — multi-part large-seed burst → global byte-cap LRU eviction
# ===========================================================================

class TestNBC1MultiSeedEviction:
    def test_multi_part_large_seeds_collectively_evicted(self, monkeypatch):
        """NB-C1: many text-starts each carrying a large seed can collectively
        breach the global byte cap (TOKEN_LIVEPARTS_MAX_BYTES) even when each
        seed individually sits under the per-part cap. The per-delta _reserve
        never sees this (no delta appended) — _start_part must run the same
        LRU while-evict, never evicting the key being admitted."""
        # Per-part cap large (so each seed is legal); global cap small.
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_LIVEPARTS_MAX_BYTES", 24)
        th = TokenStreamHub()
        sub_frames: list[bytes] = []

        class _Spy:
            """Minimal sub stub for NB-C1 hub-level eviction tests.

            Mirrors the hub→sub contract (``begin_handshake`` /
            ``end_handshake`` / ``put`` / ``closed``); the CRITICAL 3
            handshake/runtime queue physical separation lives inside
            ``TokenSubscriber`` and is exercised separately by
            ``test_token_subscriber_overflow.py``.
            """
            _in_handshake = False
            closed = False

            def begin_handshake(self):
                self._in_handshake = True

            def end_handshake(self):
                self._in_handshake = False

            def put(self, frame):
                sub_frames.append(frame)
                return True

        th.attach_subscriber("s1", _Spy())
        sub_frames.clear()
        # Each seed is 8 bytes (under the huge per-part cap). Three of them
        # = 24 bytes (== cap); a 4th pushes to 32 > 24 → evict oldest.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="AAAAAAAA"))  # 8
        th.on_part_updated(_updated_props("s1", "m1", "p2", text="BBBBBBBB"))  # 16
        th.on_part_updated(_updated_props("s1", "m1", "p3", text="CCCCCCCC"))  # 24
        assert th._total_live_bytes == 24
        th.on_part_updated(_updated_props("s1", "m1", "p4", text="DDDDDDDD"))  # 32 > 24
        # p1 (oldest) evicted; p2,p3,p4 survive (24 bytes).
        assert ("s1", "m1", "p1") not in th.live_parts
        for pid in ("p2", "p3", "p4"):
            assert ("s1", "m1", pid) in th.live_parts
        assert th._total_live_bytes == 24
        # Eviction fanned token_memory_limit resync (with sessionID) to the sub.
        resyncs = [parse_event(f) for f in sub_frames if parse_event(f)[0] == "resync"]
        assert len(resyncs) >= 1
        assert resyncs[0] == ("resync", {"reason": "token_memory_limit", "sessionID": "s1"})
        assert th.token_memory_limit_total >= 1

    def test_never_evicts_current_key_on_seed_admission(self, monkeypatch):
        """The admitted key is never evicted by its own seed (mirrors _reserve
        'never evict current key')."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_PART_MAX_BYTES", 10 ** 9)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_LIVEPARTS_MAX_BYTES", 8)
        th = TokenStreamHub()
        # First part with a seed equal to the cap — admitted, nothing to evict.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="AAAAAAAA"))  # 8 == cap
        assert ("s1", "m1", "p1") in th.live_parts
        assert th._total_live_bytes == 8

    def test_single_seed_over_per_part_cap_truncates(self, monkeypatch):
        """Regression guard: the pre-existing single-seed > per-part cap path
        still truncates (NB-C1 did not remove it)."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_PART_MAX_BYTES", 4)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_LIVEPARTS_MAX_BYTES", 10 ** 9)
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text="ABCDEFGH"))  # 8 > 4
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") in th._disabled_parts
        assert th.truncated_snapshots_total == 1


# ===========================================================================
# NB-D7 — directory query conflict 400 (design §5.1)
# ===========================================================================

class TestNBD7DirectoryConflict:
    """Structural guard mirroring the messages route: query ``directory``
    conflicting with ``X-Opencode-Directory`` header → 400
    ``directory_not_allowed``. When not conflicting, ``directory`` is a
    no-op (the accumulator fans by ``sid`` which is globally unique in
    single-user T3)."""

    async def test_query_header_conflict_returns_400(self):
        app = _build_app(_settings())
        try:
            response = await _get(
                app, "/slimapi/sessions/s1/stream?directory=/app",
                extra_headers={"X-Opencode-Directory": "/other"},
            )
            assert response.status_code == 400
            assert response.json()["code"] == "directory_not_allowed"
        finally:
            await _close_app(app)

    async def test_query_header_match_no_conflict_200(self):
        """Same directory in query and header (trailing-slash normalised) →
        no 400; stream proceeds normally (directory is a no-op)."""
        app = _build_app(_settings())
        try:
            status, _headers, _body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream?directory=/app",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity"),
                 ("X-Opencode-Directory", "/app/")],
            )
            assert status == 200
        finally:
            await _close_app(app)

    async def test_query_alone_no_header_no_conflict(self):
        """Query ``directory`` present, no header → no conflict."""
        app = _build_app(_settings())
        try:
            status, _headers, _body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream?directory=/app",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity")],
            )
            assert status == 200
        finally:
            await _close_app(app)

    async def test_header_alone_no_query_no_conflict(self):
        """Header present, no query → no conflict (token stream ignores
        a lone header — v1 only trusts query ``directory``)."""
        app = _build_app(_settings())
        try:
            status, _headers, _body = await _drive_stream(
                app, "/slimapi/sessions/s1/stream",
                [("X-Slimapi-Version", "1"), ("Accept-Encoding", "identity"),
                 ("X-Opencode-Directory", "/app")],
            )
            assert status == 200
        finally:
            await _close_app(app)

    async def test_conflict_400_does_not_admit_subscriber(self):
        """A 400 directory conflict must NOT consume a token subscriber
        slot (the conflict check runs BEFORE admission)."""
        app = _build_app(_settings(token_stream_max_subscribers=1))
        try:
            reg = app.state.token_registry
            assert reg.total_subscribers == 0
            response = await _get(
                app, "/slimapi/sessions/s1/stream?directory=/app",
                extra_headers={"X-Opencode-Directory": "/other"},
            )
            assert response.status_code == 400
            # Still at zero — the 400 path returned before subscribe().
            assert reg.total_subscribers == 0
        finally:
            await _close_app(app)
