"""Tests for the pure-ASGI ``RequestIdMiddleware``.

Covers:
* No inbound X-Request-ID → new uuid, stored in scope, injected into response.
* Inbound X-Request-ID (mixed case) → used verbatim.
* Non-HTTP messages passed through unchanged.
* Response headers dedup: when inner app already set one, only one appears.
* Inbound empty/whitespace-only string → new id generated.
* Inbound value longer than 128 chars → new id generated.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

import pytest

from oc_slimapi.middleware.request_id import REQUEST_ID_KEY, RequestIdMiddleware


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


@pytest.mark.asyncio
async def test_no_inbound_header_generates_new_id():
    """When no X-Request-ID is present, a uuid hex is generated."""
    rid: str | None = None
    scope: dict[str, Any] = {"type": "http", "headers": []}

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        nonlocal rid
        rid = scope["state"][REQUEST_ID_KEY]
        # Simulate sending a response start
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    assert rid is not None
    assert isinstance(rid, str)
    assert len(rid) > 0
    # Response headers should contain the rid
    assert any(
        (n, v) == (b"x-request-id", rid.encode())
        for n, v in _sent_headers
    )


@pytest.mark.asyncio
async def test_inbound_header_used_verbatim():
    """An existing X-Request-ID header is preserved (mixed case)."""
    expected = "my-request-id-123"
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"X-REQUEST-ID", expected.encode())],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        rid = scope["state"][REQUEST_ID_KEY]
        assert rid == expected
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    assert any(
        (n, v) == (b"x-request-id", expected.encode())
        for n, v in _sent_headers
    )


@pytest.mark.asyncio
async def test_non_http_passthrough():
    """WebSocket / lifespan messages are forwarded unchanged by send_with_rid."""
    scope: dict[str, Any] = {"type": "websocket"}
    original_send = _SentinelSend()

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        # The middleware should not wrap send, so send is original_send
        assert send is original_send
        await send({"type": "websocket.send"})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, original_send)


@pytest.mark.asyncio
async def test_response_dedup():
    """When inner app already set X-Request-ID, middleware does not duplicate."""
    inner_rid = "inner-rid"
    scope: dict[str, Any] = {"type": "http", "headers": []}

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        # Inner app sets the header itself
        await send({
            "type": "http.response.start",
            "headers": [(b"x-request-id", inner_rid.encode())],
        })
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    # Only one X-Request-ID header in the final response
    matching = [
        (n, v)
        for n, v in _sent_headers
        if n.lower() == b"x-request-id"
    ]
    assert len(matching) == 1
    # The middleware's value wins (outermost middleware)
    outer_rid = scope["state"][REQUEST_ID_KEY]
    assert matching[0][1].decode() == outer_rid


@pytest.mark.asyncio
async def test_empty_inbound_generates_new():
    """Whitespace-only inbound header → treat as absent → generate new."""
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", b"  ")],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        rid = scope["state"][REQUEST_ID_KEY]
        assert rid is not None
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    # Response header should be the NEW generated id
    rid = scope["state"][REQUEST_ID_KEY]
    matching = [
        (n, v) for n, v in _sent_headers if n.lower() == b"x-request-id"
    ]
    assert len(matching) == 1
    assert matching[0][1].decode() == rid


@pytest.mark.asyncio
async def test_long_inbound_generates_new():
    """Inbound header longer than 128 chars → treat as absent → generate new."""
    long_value = "x" * 200
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", long_value.encode())],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        rid = scope["state"][REQUEST_ID_KEY]
        assert rid is not None
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    # Response header should be the NEW generated id
    rid = scope["state"][REQUEST_ID_KEY]
    matching = [
        (n, v) for n, v in _sent_headers if n.lower() == b"x-request-id"
    ]
    assert len(matching) == 1
    # The new id is shorter than 128 and not the long_value
    assert len(rid) <= 128
    assert matching[0][1].decode() == rid
    assert matching[0][1].decode() != long_value


@pytest.mark.asyncio
async def test_control_char_inbound_generates_new():
    """Inbound header with CR/LF/control chars → rejected → generate new id.

    Guards against header injection: the value is echoed in the response
    X-Request-ID header and forwarded upstream.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", "abc\r\nX-Evil: 1".encode())],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        rid = scope["state"][REQUEST_ID_KEY]
        assert rid is not None
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    rid = scope["state"][REQUEST_ID_KEY]
    matching = [
        (n, v) for n, v in _sent_headers if n.lower() == b"x-request-id"
    ]
    assert len(matching) == 1
    # The CR/LF-laden inbound value must NOT be echoed — a fresh id is used.
    assert matching[0][1].decode() == rid
    assert "\r" not in rid and "\n" not in rid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_sent_headers: list[tuple[bytes, bytes]] = []


async def _noop_receive() -> dict[str, Any]:
    return {}


async def _collect_send(message: dict[str, Any]) -> None:
    global _sent_headers
    if message.get("type") == "http.response.start":
        _sent_headers = message.get("headers", [])


class _SentinelSend:
    """Send that records calls for assertion."""
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.calls.append(message)
