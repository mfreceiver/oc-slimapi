"""v3-contract §7.2 / §1 — SSE subscriber-header retirement checks.

B12 (2026-08-21) kept the two header-retirement checks (X-Slimapi-
Subscriber-ID never produced) — the 3.0.0 header retirement applies to
both views, so they are view-agnostic and survive the version window
collapse.

V2b (2026-08-21 Phase-4 guard teardown): the v3-only wire-behavior net
that used to live here (selector-less v3 meta field-set / tokens=1 /
blanket Last-Event-ID resync / always-identity byte anchor / lifecycle
pairing / no-scope direct-invocation defense, plus the v2-form rejection
guards) was deleted with this lane — it pinned the src v3 SSE half that
the physical teardown removes. The v4 wire face (meta first frame,
replay, resync semantics) is locked in test_token_stream_route.py /
test_events_*.py / test_sse_replay_wire.py.

Harness: fake hubs / fake token registry with pre-filled finite queues
(handshake + STOP sentinel) so the generator exits cleanly and httpx can
read the whole body.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import events as events_routes
from oc_slimapi.routes import token_stream as stream_routes
from oc_slimapi.sse.hub import STOP as HUB_STOP
from oc_slimapi.sse.token_hub import STOP as TOKEN_STOP, sse_frame

SID = "s1"


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

    def subscribe(self, wire_v4: bool = False) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass


class _FakeTokenRegistry:
    def __init__(self) -> None:
        self.sub = _FakeSubscriber("tok_test")

    def subscribe(self, sid: str, wire_v4: bool = False) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass

    # events.py tokens=1 ledger hooks (no-op for the header tests — the
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
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    *, hubs: _FakeHubs | None = None, token_registry=None,
) -> FastAPI:
    app = FastAPI(title="v3-sse-meta-test")
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


# ---------------------------------------------------------------------------
# header retirement (view-agnostic since 3.0.0 — asserted on the v4 face)
# ---------------------------------------------------------------------------

async def test_v4_events_response_has_no_subscriber_id_header():
    """B12 ①: the 3.0.0 header retirement is view-agnostic — the v4 face
    never carries X-Slimapi-Subscriber-ID either."""
    hubs = _FakeHubs()
    _put_business_frame(hubs.sub)
    app = _build_app(hubs=hubs)
    response, _ = await _read_stream(app, "/slimapi/events?v=4")
    assert "x-slimapi-subscriber-id" not in response.headers


async def test_v4_stream_response_has_no_subscriber_id_header():
    """B12 ①: the 3.0.0 header retirement is view-agnostic — the v4 face
    never carries X-Slimapi-Subscriber-ID either."""
    registry = _FakeTokenRegistry()
    _put_handshake(registry.sub)
    app = _build_app(token_registry=registry)
    response, _ = await _read_stream(
        app, f"/slimapi/sessions/{SID}/stream?v=4",
    )
    assert "x-slimapi-subscriber-id" not in response.headers
