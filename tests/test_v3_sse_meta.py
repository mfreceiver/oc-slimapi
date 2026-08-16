"""v3-contract §7.2 — SSE meta first frame (Batch D).

Covers the v3-only ``slimapi.meta`` opening event on BOTH SSE endpoints:

* ``GET /slimapi/events?v=3`` — first frame ``slimapi.meta`` with
  ``subscriberId`` (non-empty) + ``tokens`` = the ``tokens=1`` param value
  (False by default); meta precedes ANY business frame and the
  ``Last-Event-ID`` resync replay.
* ``GET /slimapi/sessions/{sid}/stream?v=3`` — first frame meta with
  ``tokens: true`` (frozen); meta precedes the subscribe() handshake frames
  and the resync replay; rides the negotiated gzip stream like every other
  frame.
* v3 SSE responses do NOT carry ``X-Slimapi-Subscriber-ID`` (§7.2 / §1).
* v2 (explicit ``?v=2`` or absent + version header): NO meta frame, header
  kept, stream bytes unchanged (first frame is the v2 business/handshake
  frame; resync-first behaviour preserved).
* Observability: a stream that ends right after the meta frame still emits
  the paired ``sse_open``/``sse_close`` rows (no orphan lifecycle).

Harness: fake hubs / fake token registry with pre-filled finite queues
(business frame / handshake + STOP sentinel) so the generator exits cleanly
and httpx can read the whole body — mirrors the deterministic pattern in
``tests/test_access_log_v3_fields.py`` (no manual ASGI driver needed).
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import events as events_routes
from oc_slimapi.routes import token_stream as stream_routes
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.sse.hub import STOP as HUB_STOP
from oc_slimapi.sse.token_hub import STOP as TOKEN_STOP, sse_frame

V2_HEADER = {"X-Slimapi-Version": "2"}
SID = "s1"


# ---------------------------------------------------------------------------
# capture logger fixture (mirror tests/test_access_log_v3_fields.py)
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def capture_logger():
    logger = logging.getLogger("oc_slimapi.test.capture.meta")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    yield logger
    logger.removeHandler(handler)


def _rows(logger) -> list[dict]:
    return [json.loads(line) for line in logger.handlers[0].lines if line]


# ---------------------------------------------------------------------------
# fake hubs / token registry (deterministic finite streams)
# ---------------------------------------------------------------------------

class _FakeSubscriber:
    def __init__(self, sub_id: str = "sub_test") -> None:
        self.id = sub_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.metrics = SimpleNamespace(
            gzip_raw_bytes_total=0, gzip_compressed_bytes_total=0,
        )

    def ack(self, item: bytes) -> None:  # pragma: no cover - no-op
        pass


class _FakeHubs:
    def __init__(self) -> None:
        self.sub = _FakeSubscriber()

    def subscribe(self) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass


class _FakeTokenRegistry:
    def __init__(self) -> None:
        self.sub = _FakeSubscriber("tok_test")

    def subscribe(self, sid: str) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass

    # events.py tokens=1 ledger hooks (no-op for the meta-frame tests — the
    # real flush-loop behaviour is covered by test_events_tokens.py).
    def attach_events_subscriber(self, subscriber) -> None:
        pass

    def detach_events_subscriber(self, subscriber) -> None:
        pass


# ---------------------------------------------------------------------------
# app / helpers
# ---------------------------------------------------------------------------

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
        server_api_version=2,
        accepted_client_versions=(2, 3),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    *, hubs: _FakeHubs | None = None, token_registry=None,
) -> FastAPI:
    app = FastAPI(title="v3-sse-meta-test")
    app.add_middleware(
        SlimapiSelectorMiddleware,
        accepted_client_versions=(2, 3),
        v3_enabled=True,
    )
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = hubs if hubs is not None else _FakeHubs()
    app.state.token_registry = (
        token_registry if token_registry is not None else _FakeTokenRegistry()
    )
    app.include_router(events_routes.router)
    app.include_router(stream_routes.router)
    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test")


def _parse_frame(block: bytes):
    event = None
    data_lines: list[bytes] = []
    for line in block.split(b"\n"):
        if line.startswith(b"event:"):
            event = line[6:].strip().decode()
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return event, None
    return event, json.loads(b"\n".join(data_lines).decode())


def _frames(body: bytes):
    for block in body.split(b"\n\n"):
        block = block.strip(b"\n")
        if not block:
            continue
        event, data = _parse_frame(block)
        if data is not None:
            yield event, data


async def _read_stream(app: FastAPI, path: str, headers: dict | None = None):
    """Open the (finite) stream and return (response, full body bytes)."""
    async with _client(app) as client:
        async with client.stream("GET", path, headers=headers or {}) as response:
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
            return response, body


def _put_business_frame(sub: _FakeSubscriber) -> None:
    # events.py compares against sse.hub's STOP sentinel — use THAT object.
    sub.queue.put_nowait(
        sse_frame({"sessionID": SID}, event="server.heartbeat"))
    sub.queue.put_nowait(HUB_STOP)


def _put_handshake(sub: _FakeSubscriber) -> None:
    # token_stream.py compares against token_hub's STOP sentinel.
    sub.queue.put_nowait(sse_frame({"sessionID": SID}, event="server.connected"))
    sub.queue.put_nowait(TOKEN_STOP)


def _assert_meta(frame, *, tokens: bool, subscriber_id: str | None = None):
    event, data = frame
    assert event == "slimapi.meta"
    assert set(data.keys()) == {"subscriberId", "tokens"}  # frozen fields only
    assert isinstance(data["subscriberId"], str) and data["subscriberId"]
    if subscriber_id is not None:
        assert data["subscriberId"] == subscriber_id
    assert data["tokens"] is tokens


# ---------------------------------------------------------------------------
# /events v3
# ---------------------------------------------------------------------------

async def test_v3_events_meta_first_frame_default_tokens_false():
    hubs = _FakeHubs()
    _put_business_frame(hubs.sub)
    app = _build_app(hubs=hubs)
    response, body = await _read_stream(app, "/slimapi/events?v=3")
    assert response.status_code == 200
    frames = list(_frames(body))
    _assert_meta(frames[0], tokens=False, subscriber_id="sub_test")
    # meta precedes the first business frame
    assert frames[1] == ("server.heartbeat", {"sessionID": SID})


async def test_v3_events_meta_tokens_true_with_tokens_param():
    hubs = _FakeHubs()
    _put_business_frame(hubs.sub)
    app = _build_app(hubs=hubs)
    response, body = await _read_stream(app, "/slimapi/events?v=3&tokens=1")
    assert response.status_code == 200
    frames = list(_frames(body))
    _assert_meta(frames[0], tokens=True, subscriber_id="sub_test")


async def test_v3_events_meta_before_resync_replay():
    """meta precedes the Last-Event-ID reconnect_no_replay resync frame."""
    hubs = _FakeHubs()
    hubs.sub.queue.put_nowait(HUB_STOP)
    app = _build_app(hubs=hubs)
    response, body = await _read_stream(
        app, "/slimapi/events?v=3", headers={"Last-Event-ID": "anything"},
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    _assert_meta(frames[0], tokens=False)
    assert frames[1] == ("resync", {"reason": "reconnect_no_replay"})


async def test_v3_events_response_has_no_subscriber_id_header():
    hubs = _FakeHubs()
    _put_business_frame(hubs.sub)
    app = _build_app(hubs=hubs)
    response, _ = await _read_stream(app, "/slimapi/events?v=3")
    assert "x-slimapi-subscriber-id" not in response.headers


# ---------------------------------------------------------------------------
# /events v2 regression (byte shape unchanged)
# ---------------------------------------------------------------------------

async def test_v2_events_no_meta_and_header_kept():
    hubs = _FakeHubs()
    _put_business_frame(hubs.sub)
    app = _build_app(hubs=hubs)
    response, body = await _read_stream(
        app, "/slimapi/events", headers=V2_HEADER,
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    # no meta anywhere; first frame is the business frame (v2 bytes)
    assert all(event != "slimapi.meta" for event, _ in frames)
    assert frames[0] == ("server.heartbeat", {"sessionID": SID})
    assert response.headers.get("x-slimapi-subscriber-id") == "sub_test"


async def test_v2_events_resync_first_without_meta():
    hubs = _FakeHubs()
    hubs.sub.queue.put_nowait(HUB_STOP)
    app = _build_app(hubs=hubs)
    response, body = await _read_stream(
        app, "/slimapi/events",
        headers={**V2_HEADER, "Last-Event-ID": "anything"},
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    assert frames[0] == ("resync", {"reason": "reconnect_no_replay"})
    assert all(event != "slimapi.meta" for event, _ in frames)
    assert response.headers.get("x-slimapi-subscriber-id") == "sub_test"


# ---------------------------------------------------------------------------
# /stream v3
# ---------------------------------------------------------------------------

async def test_v3_stream_meta_first_tokens_true():
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream?v=3",
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    _assert_meta(frames[0], tokens=True, subscriber_id="tok_test")
    # meta precedes the subscribe() handshake frame
    assert frames[1] == ("server.connected", {"sessionID": SID})


async def test_v3_stream_meta_before_resync_replay():
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream?v=3",
        headers={"Last-Event-ID": "anything"},
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    _assert_meta(frames[0], tokens=True)
    assert frames[1] == ("resync", {"reason": "reconnect_no_replay", "sessionID": SID})
    assert frames[2] == ("server.connected", {"sessionID": SID})


async def test_v3_stream_response_has_no_subscriber_id_header():
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, _ = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream?v=3",
    )
    assert "x-slimapi-subscriber-id" not in response.headers


async def test_v3_stream_identity_despite_gzip_accept():
    """§7.2 freeze: v3 SSE does no content-encoding — frames are raw bytes.

    Even with ``Accept-Encoding: gzip`` a v3 /stream stays identity: no
    ``Content-Encoding`` header, and the body is literally readable SSE
    bytes (byte-exact meta + handshake sequence)."""
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream?v=3",
        headers={"Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    # always-identity representation is Accept-Encoding-independent → no Vary
    assert "vary" not in response.headers
    # Byte-exact raw frame sequence (no meta on... wait, meta IS first on v3):
    assert body == (
        b'event: slimapi.meta\n'
        b'data: {"subscriberId":"tok_test","tokens":true}\n\n'
        b'event: server.connected\n'
        b'data: {"sessionID":"s1"}\n\n'
    )


# ---------------------------------------------------------------------------
# /stream v2 regression
# ---------------------------------------------------------------------------

async def test_v2_stream_no_meta_header_kept_handshake_first():
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream", headers=V2_HEADER,
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    assert all(event != "slimapi.meta" for event, _ in frames)
    assert frames[0] == ("server.connected", {"sessionID": SID})
    assert response.headers.get("x-slimapi-subscriber-id") == "tok_test"


async def test_v2_explicit_selector_stream_no_meta():
    """Explicit ``?v=2`` (+ version header, gate applies) — v2 bytes."""
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream?v=2", headers=V2_HEADER,
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    assert all(event != "slimapi.meta" for event, _ in frames)
    assert response.headers.get("x-slimapi-subscriber-id") == "tok_test"


# ---------------------------------------------------------------------------
# observability: no orphan lifecycle when the stream ends right after meta
# ---------------------------------------------------------------------------

async def test_v3_events_close_after_meta_pairs_lifecycle(capture_logger, monkeypatch):
    from oc_slimapi import sse_observability

    monkeypatch.setattr(
        sse_observability, "_access_logger", lambda: capture_logger)
    hubs = _FakeHubs()
    hubs.sub.queue.put_nowait(HUB_STOP)  # meta, then immediate end-of-stream
    app = _build_app(hubs=hubs)
    response, body = await _read_stream(app, "/slimapi/events?v=3")
    assert response.status_code == 200
    frames = list(_frames(body))
    _assert_meta(frames[0], tokens=False)  # the meta frame was delivered

    rows = _rows(capture_logger)
    sse_rows = [r for r in rows if r.get("recordType") in ("sse_open", "sse_close")]
    assert len(sse_rows) == 2
    open_row, close_row = sse_rows
    assert open_row["recordType"] == "sse_open"
    assert close_row["recordType"] == "sse_close"
    assert open_row["lifecycleId"] == close_row["lifecycleId"]
    assert open_row["selectorResult"] == "v3"


async def test_v3_stream_close_after_meta_pairs_lifecycle(capture_logger, monkeypatch):
    from oc_slimapi import sse_observability

    monkeypatch.setattr(
        sse_observability, "_access_logger", lambda: capture_logger)
    registry = _FakeTokenRegistry()
    registry.sub.queue.put_nowait(TOKEN_STOP)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream?v=3",
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    _assert_meta(frames[0], tokens=True)

    rows = _rows(capture_logger)
    sse_rows = [r for r in rows if r.get("recordType") in ("sse_open", "sse_close")]
    assert len(sse_rows) == 2
    open_row, close_row = sse_rows
    assert open_row["recordType"] == "sse_open"
    assert close_row["recordType"] == "sse_close"
    assert open_row["lifecycleId"] == close_row["lifecycleId"]
    assert open_row["selectorResult"] == "v3"
    assert open_row["bucket"] == "token_stream_sse"


# ---------------------------------------------------------------------------
# D3 — v2 byte-exact regressions (full frame byte sequences)
# ---------------------------------------------------------------------------

_V2_STREAM_EXPECTED = (
    b'event: server.connected\n'
    b'data: {"sessionID":"s1"}\n\n'
)
_V2_EVENTS_EXPECTED = (
    b'event: server.heartbeat\n'
    b'data: {"sessionID":"s1"}\n\n'
)


async def test_v2_stream_identity_bytes_exact():
    """D3: v2 /stream (no AE) — the FULL body equals the fixed expected
    frame bytes (no meta anywhere; handshake first)."""
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream",
        headers={**V2_HEADER, "Accept-Encoding": "identity"},
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert response.headers.get("x-slimapi-subscriber-id") == "tok_test"
    assert body == _V2_STREAM_EXPECTED


async def test_v2_stream_gzip_negotiation_bytes_exact():
    """D3: v2 /stream gzip negotiation unchanged — Content-Encoding: gzip +
    Vary, and the decompressed full frame byte sequence equals the fixed
    expected bytes (Lever-2 v2 behaviour is frozen)."""
    import zlib

    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, body = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream",
        headers={**V2_HEADER, "Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert response.headers.get("vary") == "Accept-Encoding"
    assert response.headers.get("x-slimapi-subscriber-id") == "tok_test"
    # httpx transparently decompresses `body`; to prove the WIRE bytes were
    # gzip we re-request raw... simpler: decompress is identity here, so
    # assert the (already-decompressed) full sequence byte-exactly — the
    # Content-Encoding header above proves the gzip negotiation happened.
    assert body == _V2_STREAM_EXPECTED


async def test_v2_events_identity_bytes_exact():
    """D3: v2 /events — full body equals the fixed expected frame bytes
    (control-plane events were never gzipped; unchanged)."""
    hubs = _FakeHubs()
    _put_business_frame(hubs.sub)
    app = _build_app(hubs=hubs)
    response, body = await _read_stream(
        app, "/slimapi/events", headers=V2_HEADER,
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers
    assert body == _V2_EVENTS_EXPECTED


# ---------------------------------------------------------------------------
# D5 — stream route tolerates direct-invocation requests without .scope
# ---------------------------------------------------------------------------

async def test_stream_route_no_scope_request_ok():
    """D5: direct route invocation with a mock request lacking ``.scope``
    must not AttributeError (v2 view; wire_view defensive getattr)."""
    from oc_slimapi.routes.token_stream import token_stream

    class _NoScopeRequest:
        headers: dict[str, str] = {}
        query_params = httpx.QueryParams()
        app = _build_app()

    # ``app`` on the mock is a plain attribute (no .state access needed by
    # the route before the token_registry lookup — provide state via the
    # built app). The route reads request.app.state.token_registry.
    req = _NoScopeRequest()
    result = await token_stream(req, SID)
    assert result.status_code == 200
    assert result.headers.get("x-slimapi-subscriber-id") == "tok_test"
