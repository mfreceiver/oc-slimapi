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
# P1-15: non-ASCII request-id → rejected → fresh uuid generated.
# The proxy forwards request-id into an httpx header via build_request();
# a non-ASCII value raises at BUILD time (before the send try/except) → bare
# 500 instead of upstream_unavailable. Fail-closed: reject → fresh uuid.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_ascii_inbound_generates_new():
    """Non-ASCII (multibyte UTF-8 like Chinese) → rejected → fresh uuid.

    The proxy's client.build_request() would raise on a non-ASCII header
    value; rejecting here prevents a bare 500."""
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", "请求标识符123".encode("utf-8"))],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        rid = scope["state"][REQUEST_ID_KEY]
        assert rid is not None
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    rid = scope["state"][REQUEST_ID_KEY]
    # Must be pure ASCII (a freshly-generated uuid hex).
    assert rid.isascii()
    assert rid != "请求标识符123"


@pytest.mark.asyncio
async def test_high_ascii_byte_inbound_generates_new():
    """A byte in the 0x80-0xFF range (invalid standalone ASCII / start of a
    multibyte sequence) → rejected → fresh uuid."""
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", b"abc\xff\xfe")],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    rid = scope["state"][REQUEST_ID_KEY]
    assert rid.isascii()


@pytest.mark.asyncio
async def test_printable_ascii_inbound_used_verbatim():
    """Valid printable ASCII request-id (including embedded spaces, dashes,
    dots) is accepted and used verbatim."""
    expected = "abc-DEF 123.xyz"
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", expected.encode("ascii"))],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        rid = scope["state"][REQUEST_ID_KEY]
        assert rid == expected
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)


@pytest.mark.asyncio
async def test_del_byte_inbound_generates_new():
    """Byte 0x7f (DEL) is not printable ASCII → rejected → fresh uuid."""
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", b"abc\x7f")],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)

    rid = scope["state"][REQUEST_ID_KEY]
    assert "\x7f" not in rid


@pytest.mark.asyncio
async def test_boundary_length_128_accepted():
    """Exactly 128 printable ASCII chars is the boundary — must be accepted."""
    value = "a" * 128
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(b"x-request-id", value.encode("ascii"))],
    }

    async def app(scope: dict, receive: Receive, send: Send) -> None:
        rid = scope["state"][REQUEST_ID_KEY]
        assert rid == value
        await send({"type": "http.response.start", "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RequestIdMiddleware(app)
    await middleware(scope, _noop_receive, _collect_send)


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
