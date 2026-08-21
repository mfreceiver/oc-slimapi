"""Integration tests for SSE traffic accounting (省流实证).

Exercises the full downstream path (events route → SSE generator →
``record_sse_downstream`` → middleware → ledger) and simulates upstream
byte accounting for the shared ``/global/event`` connection.

Scenarios:
1. **SSE end-to-end 省流**: single subscriber reads curated frames, ledger
   shows ``downOut < upIn`` (curated projection is smaller than raw upstream
   events) with non-zero ``framesEmitted``.
2. **Multi-subscriber fanout**: 2 subscribers share one upstream cost;
   ``downOut`` is ~2× single-subscriber, ``upIn`` unchanged.
3. **SSE error response counting**: non-stream error on SSE path (503
   subscriber capacity) has ``downOut`` counted by the middleware (not zeroed).
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import orjson
from fastapi import FastAPI

from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.routes import events
from oc_slimapi.sse.hub import HubRegistry, GlobalHub, _upstream_line_bytes
from oc_slimapi.traffic import TrafficLedger

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VERSION_HEADERS: list[tuple[str, str]] = [
    ("x-slimapi-version", "1"),
    ("accept-encoding", "identity"),
]


def _make_global_event(
    directory: str,
    event_type: str,
    properties: dict | None = None,
    payload_id: str | None = None,
) -> dict:
    """Build a /global/event frame dict (same as test_hub.make_global_event)."""
    payload: dict = {"type": event_type, "properties": properties or {}}
    if payload_id is not None:
        payload["id"] = payload_id
    return {"directory": directory, "payload": payload}


def _simulate_upstream_lines(global_events: list[dict]) -> list[str]:
    """Return the raw SSE ``data:`` lines that ``hub.run()`` would see for
    these global events, and the empty-line separator between each event.

    This lets the test compute the upstream byte cost exactly as the hub
    would via ``record_sse_upstream``.
    """
    lines: list[str] = []
    for event in global_events:
        raw = orjson.dumps(event).decode()
        lines.append(f"data: {raw}")      # data: <json>
        lines.append("")                   # empty line separator
    return lines


def _upstream_byte_cost(raw_lines: list[str]) -> int:
    """Compute what ``hub.run()`` would count via ``record_sse_upstream``:
    ``len(line.encode("utf-8", "replace")) + 1`` per line (the +1 accounts
    for the stripped ``\\n``). Empty lines (separators) also count.
    """
    total = 0
    for line in raw_lines:
        total += len(line.encode("utf-8", "replace")) + 1
    return total


def _sse_event_bytes(raw: bytes) -> int:
    """Compute the downstream byte cost of one curated SSE frame, as the
    events route generator would via ``record_sse_downstream(bytes_out=len(item))``.
    """
    return len(raw)


def _build_app(
    upstream: httpx.AsyncClient,
    *,
    max_total_subscribers: int = 10,
) -> tuple[FastAPI, TrafficLedger]:
    """Build a FastAPI app with events route + ledger + middleware.

    Returns (app, ledger).

    ``upstream`` is stashed on ``app.state.upstream`` for shape parity, but
    the HubRegistry is constructed with ``client=None``: this test injects
    curated events via ``hub.publish()`` / ``hub.flush()`` and accounts the
    upstream byte cost via ``ledger.record_sse_upstream()`` directly (NOT via
    the ``run()`` aiter_lines loop). With a mock upstream, ``run()`` would
    busy-loop / block synchronously inside httpx on the empty-204 stream and
    starve the event loop (and ``task.cancel()`` could not interrupt a
    synchronous section). ``client=None`` makes ``run()`` park on cancellable
    backoff sleeps instead — the same pattern as test_hub.py /
    test_token_stream_route.py.
    """
    app = FastAPI(title="oc-slimapi-sse-traffic-test")
    app.state.upstream = upstream

    ledger = TrafficLedger()
    app.state.traffic_ledger = ledger
    app.state.hubs = HubRegistry(
        None,
        max_total_subscribers=max_total_subscribers,
        traffic_ledger=ledger,
    )

    app.include_router(events.router)
    register_error_handlers(app)
    app.add_middleware(TrafficAccountingMiddleware)
    return app, ledger


async def _shutdown(app: FastAPI) -> None:
    """Best-effort teardown."""
    await app.state.hubs.close()


async def _close_hub(hub: GlobalHub) -> None:
    """Cancel + await every GlobalHub background task.

    Mirrors ``tests/test_hub.py::_close_hub``: the ``stop_after_grace``
    task (armed by ``unsubscribe`` on a 30s ``GRACE_SECONDS`` timer) is
    cancelled here so the event loop never closes with a pending 30s task
    (which would hang the whole pytest suite). ``HubRegistry.close()``
    already does this, but we keep this as defense-in-depth for tests that
    grab the bare hub reference.
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


def _parse_sse_frames(body: bytes) -> list[tuple[str | None, dict]]:
    """Parse concatenated SSE body into (event, data) pairs."""
    frames: list[tuple[str | None, dict]] = []
    for block in body.split(b"\n\n"):
        if not block.strip():
            continue
        event_name: str | None = None
        data_lines: list[str] = []
        for line in block.decode("utf-8", "replace").split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        if data_lines:
            data = orjson.loads("\n".join(data_lines))
            frames.append((event_name, data))
    return frames


# ===========================================================================
# Test 1: SSE end-to-end 省流
# ===========================================================================


async def test_sse_end_to_end_traffic_saving(upstream_factory):
    """Single subscriber: events route + middleware produce correct ledger
    with upIn (upstream raw bytes) > downOut (curated SSE bytes) and
    framesEmitted > 0 — the 省流实证.

    The DGAF upstream (unused by the hub run loop in this test) exists only
    to satisfy HubRegistry construction.
    """
    # -- Upstream events (what /global/event would emit) ------------------
    global_events = [
        _make_global_event("/proj", "session.status",
                           {"sessionID": "s1", "status": "busy"}),
        _make_global_event("/proj", "session.status",
                           {"sessionID": "s2", "status": "idle"}),
        _make_global_event("/proj", "question.asked",
                           {"id": "q1", "sessionID": "s3"}),
    ]
    # Terminal §7.2 adds the leading slimapi.meta frame to the downstream
    # bytes; a realistic upstream burst (repeated statuses for the SAME
    # sessions — coalesced into digests downstream) keeps the 省流
    # invariant meaningful rather than calibrating the threshold down.
    for _ in range(10):
        global_events.append(_make_global_event(
            "/proj", "session.status",
            {"sessionID": "s1", "status": "busy"}))
        global_events.append(_make_global_event(
            "/proj", "session.status",
            {"sessionID": "s2", "status": "idle"}))

    # Raw upstream SSE lines the hub would iterate
    raw_lines = _simulate_upstream_lines(global_events)
    expected_up_in = _upstream_byte_cost(raw_lines)
    assert expected_up_in > 100, "test requires non-trivial upstream bytes"

    # -- Build app --------------------------------------------------------
    def dummy(_r):
        return httpx.Response(204)

    upstream = upstream_factory(dummy)
    app, ledger = _build_app(upstream)
    hub = app.state.hubs.get_global()

    try:
        # -- Drive SSE endpoint -------------------------------------------
        # Events SSE endpoint (contract: GET /slimapi/events, requires the
        # X-Slimapi-Version header). No query string in this scenario.
        path = "/slimapi/events"
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
            "headers": [(k.lower().encode(), v.encode())
                         for k, v in VERSION_HEADERS],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 0),
            "root_path": "",
            "extensions": {},
        }
        status_code = 0
        body = bytearray()
        got_response = False
        body_delivered = False
        # Park ``receive`` on this event after the request body so Starlette's
        # ``listen_for_disconnect`` (run concurrently with ``stream_response``
        # in a collapsing task group) does NOT receive ``http.disconnect`` and
        # cancel the streaming task. The generator must stay parked on
        # ``queue.get()`` to receive frames we publish AFTER it parks; an
        # early disconnect would tear it down right after the welcome frame.
        # The test ends by cancelling the task (CancelledError propagates into
        # both the generator and this ``receive``).
        disconnect = asyncio.Event()

        async def receive():
            nonlocal body_delivered
            if not body_delivered:
                body_delivered = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status_code, got_response
            if message["type"] == "http.response.start":
                status_code = message["status"]
                got_response = True
            elif message["type"] == "http.response.body":
                body.extend(message.get("body", b""))

        task = asyncio.create_task(app(scope, receive, send))

        # Phase 1: let the generator send welcome frame, then park on queue.get
        await asyncio.sleep(0.05)
        assert got_response, "SSE endpoint did not respond"
        assert status_code == 200, f"expected 200, got {status_code}"

        # Phase 2: publish upstream events to the hub (simulating what
        # hub.run() would do after parsing /global/event)
        for ev in global_events:
            hub.publish(ev)
        # Flush pending digest events
        hub.flush()

        # Phase 3: account the upstream byte cost (what hub.run() would do
        # via record_sse_upstream in the aiter_lines loop).
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=expected_up_in)

        # Phase 4: let the generator process queued frames before cancellation
        await asyncio.sleep(0.05)

        # Phase 5: cancel
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await asyncio.sleep(0)

        # -- Assert ledger ------------------------------------------------
        snap = ledger.snapshot()
        assert snap["enabled"] is True
        bucket = snap["buckets"]["events_sse"]

        # Upstream bytes counted correctly
        assert bucket["upIn"] == expected_up_in, (
            f"upIn {bucket['upIn']} != {expected_up_in}"
        )
        # Downstream bytes > 0 (curated frames yielded)
        assert bucket["downOut"] > 0, "downOut must be > 0"
        # Frames emitted > 0
        assert bucket["framesEmitted"] > 0, "framesEmitted must be > 0"

        # The headline 省流 assertion: curated downOut < raw upstream upIn.
        assert bucket["downOut"] < bucket["upIn"], (
            f"downOut ({bucket['downOut']}) >= upIn ({bucket['upIn']}) — "
            f"expected curated bytes < raw upstream bytes"
        )

        # Parse frames for structural validation
        frames = _parse_sse_frames(bytes(body))
        frame_events = {e for e, _ in frames}
        assert "slimapi.meta" in frame_events, "missing terminal meta frame"
        # V2b default flip: this selector-less stack now runs the v4 SSE
        # pipeline, which suppresses the v3 server.connected welcome frame.
        assert "server.connected" not in frame_events
        assert "session.digest" in frame_events, "missing digest frame(s)"

        # Ratio < 1.0 for single subscriber
        assert "events_sse" in snap["ratios"]
        ratio = snap["ratios"]["events_sse"]["downOutOverUpIn"]
        assert ratio < 1.0, f"expected ratio < 1.0 (省流), got {ratio}"
    finally:
        # Cancel the generator's finally → unsubscribe (which arms the 30s
        # stop_after_grace); hubs.close() cancels run/flush/heartbeat/stop.
        await _shutdown(app)
        await _close_hub(hub)


# ===========================================================================
# Test 2: Multi-subscriber fanout
# ===========================================================================


async def test_sse_multi_subscriber_fanout(upstream_factory):
    """2 SSE subscribers share one upstream cost; downOut ≈ 2× single-
    subscriber bytes; upIn does NOT double."""
    global_events = [
        _make_global_event("/proj", "session.status",
                           {"sessionID": "s1", "status": "busy",
                            "detail": "x" * 500}),
        _make_global_event("/proj", "question.asked",
                           {"id": "q1", "sessionID": "s2"}),
    ]

    raw_lines = _simulate_upstream_lines(global_events)
    expected_up_in = _upstream_byte_cost(raw_lines)

    def dummy(_r):
        return httpx.Response(204)

    upstream = upstream_factory(dummy)
    app, ledger = _build_app(upstream)
    hub = app.state.hubs.get_global()

    tasks: list[asyncio.Task] = []
    try:
        # -- Drive TWO SSE endpoints -------------------------------------
        path = "/slimapi/events"
        if "?" in path:
            pure_path, query = path.split("?", 1)
            query_string = query.encode()
        else:
            pure_path, query_string = path, b""

        scope_base = {
            "type": "http",
            "method": "GET",
            "path": pure_path,
            "raw_path": path.encode(),
            "query_string": query_string,
            "headers": [(k.lower().encode(), v.encode())
                         for k, v in VERSION_HEADERS],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 0),
            "root_path": "",
            "extensions": {},
        }

        bodies: list[bytearray] = [bytearray(), bytearray()]
        got_responses = [False, False]
        # Park each ``receive`` on its own event after the request body so
        # Starlette's concurrent ``listen_for_disconnect`` does NOT cancel
        # the streaming task (see test_sse_end_to_end_traffic_saving for the
        # full rationale). Both generators must stay parked on
        # ``queue.get()`` to receive the frames we publish in Phase 2.
        disconnects = [asyncio.Event(), asyncio.Event()]

        async def make_receive(idx):
            delivered = [False]

            async def receive():
                if not delivered[0]:
                    delivered[0] = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await disconnects[idx].wait()
                return {"type": "http.disconnect"}
            return receive

        async def make_send(idx):
            async def send(message):
                if message["type"] == "http.response.start":
                    got_responses[idx] = True
                elif message["type"] == "http.response.body":
                    bodies[idx].extend(message.get("body", b""))
            return send

        for i in range(2):
            t = asyncio.create_task(
                app(scope_base, await make_receive(i), await make_send(i))
            )
            tasks.append(t)

        # Phase 1: let both generators send welcome frames and park
        await asyncio.sleep(0.05)
        assert all(got_responses), "both SSE endpoints should have responded"

        # Phase 2: publish events once (single upstream cost)
        for ev in global_events:
            hub.publish(ev)
        hub.flush()

        # Phase 3: account upstream bytes once
        ledger.record_sse_upstream(bucket="events_sse", bytes_in=expected_up_in)

        # Phase 4: let generators process queued frames
        await asyncio.sleep(0.05)

        # Phase 5: cancel both
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        tasks.clear()
        await asyncio.sleep(0)

        # -- Assert ledger ------------------------------------------------
        snap = ledger.snapshot()
        assert snap["enabled"] is True
        bucket = snap["buckets"]["events_sse"]

        # upIn is counted exactly once (single shared upstream connection)
        assert bucket["upIn"] == expected_up_in, (
            f"upIn ({bucket['upIn']}) should be single shared cost"
        )

        # Each subscriber got at least some bytes
        assert len(bodies[0]) > 0, "sub 1 got zero bytes"
        assert len(bodies[1]) > 0, "sub 2 got zero bytes"

        # downOut should substantially exceed a single subscriber's bytes
        # since both subscribers' curated frames are aggregated.
        single_bytes = _sse_event_bytes(bytes(bodies[0]))
        both_bytes = bucket["downOut"]
        # With 2 subscribers and the same events, downOut should be close
        # to the sum of both subscribers' frame bytes (downOut ≈ 2× single).
        # Allow generous tolerance for SSE overhead per-subscriber (welcome
        # frame appears once per subscriber, so the sum of individual bodies
        # plus the aggregated downOut should be close).
        sum_both = len(bodies[0]) + len(bodies[1])
        assert both_bytes >= sum_both * 0.9, (
            f"downOut ({both_bytes}) should roughly equal sum of subscriber "
            f"body bytes ({sum_both})"
        )
        # Fanout is symmetric: both subscribers received identical curated
        # frames (welcome + question + digest), so their body lengths match.
        assert len(bodies[0]) == len(bodies[1]), (
            f"fanout asymmetric: sub0={len(bodies[0])} vs sub1={len(bodies[1])}"
        )
        # upIn is NOT doubled — the single shared /global/event connection is
        # accounted once regardless of subscriber count. ``upIn == expected_up_in``
        # (asserted above) is the exact single-cost proof; this makes the
        # "does not scale with subscribers" fanout property explicit and
        # data-independent (curation may strip bytes, so comparing upIn to the
        # aggregated downstream would be a function of payload shape, not of
        # the accounting invariant under test).
        assert bucket["upIn"] < 2 * expected_up_in, (
            "upIn must not scale with subscriber count (one shared upstream)"
        )

        # Each subscriber got frames
        frames_0 = _parse_sse_frames(bytes(bodies[0]))
        frames_1 = _parse_sse_frames(bytes(bodies[1]))
        assert len(frames_0) >= 2, "sub 1 should have multiple frames"
        assert len(frames_1) >= 2, "sub 2 should have multiple frames"
    finally:
        # Cancel any still-live generator tasks (defensive: an early assert
        # may have left them parked on queue.get → generator finally →
        # unsubscribe arms the 30s stop_after_grace).
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await _shutdown(app)
        await _close_hub(hub)


# ===========================================================================
# Test 3: SSE error response 补计
# ===========================================================================


async def test_sse_error_response_counts_downout(upstream_factory):
    """SSE path returning a non-stream error (503 subscriber limit) must
    have downOut counted by the middleware (resp_bytes NOT zeroed)."""
    global_events = [  # unused but needed for hub construction
        _make_global_event("/proj", "session.status",
                           {"sessionID": "s1", "status": "busy"}),
    ]

    def dummy(_r):
        return httpx.Response(204)

    upstream = upstream_factory(dummy)
    # max_total_subscribers=0 forces all subscribe() calls to raise
    # SubscriberCapacityError → 503.
    app, ledger = _build_app(upstream, max_total_subscribers=0)

    try:
        # -- Make an SSE request via httpx (non-streaming) ----------------
        # httpx.ASGITransport works fine here because the response is NOT a
        # real SSE stream — it's a short JSON error body.
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get(
                "/slimapi/events",
                headers={k.capitalize(): v for k, v in VERSION_HEADERS},
            )

        assert resp.status_code == 503, (
            f"expected 503 (subscriber limit), got {resp.status_code}"
        )
        body = resp.json()
        assert "code" in body, f"expected error code in body: {body}"

        # The middleware must have counted the response body bytes as downOut
        # (it should NOT have passed resp_bytes=0 for this non-stream response).
        snap = ledger.snapshot()
        assert snap["enabled"] is True
        bucket = snap["buckets"]["events_sse"]

        # The error JSON body must be counted in downOut
        assert bucket["downOut"] > 0, (
            "SSE error response must count downOut (non-stream)"
        )
        # The downOut should equal or exceed the JSON error body
        assert bucket["downOut"] >= len(resp.content), (
            f"downOut ({bucket['downOut']}) should cover error body "
            f"({len(resp.content)})"
        )
        # The request itself is counted
        assert bucket["requests"] == 1, "the failed request must be counted"
    finally:
        # No SSE generator task is live here (subscribe raised before any
        # subscriber was admitted), but hubs.close() still tears down any
        # lazily-created hub + pending removal task for a clean loop exit.
        await _shutdown(app)


# ===========================================================================
# Test 4: empty-line counting via the pure _upstream_line_bytes helper
# ===========================================================================


def test_upstream_line_bytes_counts_empty_separators():
    """Unit-test the pure helper that ``GlobalHub.run()`` uses to attribute
    bytes to each ``aiter_lines()`` line from the shared ``/global/event``
    stream.

    This pins fix#2 (empty SSE separator lines are counted, not skipped)
    through the REAL code path's counting formula. We deliberately do NOT
    drive ``hub.run()`` with a mock upstream here: per the module header and
    the hang warning in this repo, a mock upstream busy-loops inside httpx
    and resists ``task.cancel()``, which would hang the whole pytest suite.
    Extracting the counting into ``_upstream_line_bytes`` (called unchanged
    by ``run()``) lets us assert the empty-line behaviour deterministically
    and without any event loop / teardown risk.

    Assertions:
      * An empty line (SSE frame separator) costs exactly 1 byte (the
        stripped ``\\n``) — this is the fix#2 guarantee.
      * A ``data:`` line costs ``len(encoded) + 1``.
      * A multi-byte UTF-8 line counts its byte length, not its char length.
    """
    # Empty separator line → 1 byte (the stripped LF). This is the fix#2
    # invariant: separators are NOT skipped.
    assert _upstream_line_bytes("") == 1

    # A short data line → len(encoded) + 1 (for the stripped LF).
    line = "data: {\"type\":\"session.status\"}"
    assert _upstream_line_bytes(line) == len(line.encode("utf-8")) + 1

    # Multi-byte UTF-8 counts BYTES, not chars (é = 2 bytes in UTF-8).
    multi = "data: café"
    assert _upstream_line_bytes(multi) == len(multi.encode("utf-8")) + 1
    # Explicit: char count != byte count here.
    assert len(multi) != len(multi.encode("utf-8"))

    # A representative mixed stream (data line + separator + data line +
    # separator) sums to the same value the test helper _upstream_byte_cost
    # computes — proving run()'s per-line accounting matches the documented
    # total used by the 省流 evidence tests above.
    stream = ["data: {\"a\":1}", "", "data: {\"b\":2}", ""]
    total = sum(_upstream_line_bytes(l) for l in stream)
    assert total == _upstream_byte_cost(stream)
    # And the separators contributed exactly 2 bytes (two empty lines).
    assert _upstream_line_bytes("") + _upstream_line_bytes("") == 2
