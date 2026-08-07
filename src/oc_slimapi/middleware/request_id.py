"""Pure-ASGI middleware that injects/extracts X-Request-ID.

Reads inbound ``X-Request-ID`` header (case-insensitive); if present, uses it;
otherwise generates a new ``uuid.uuid4().hex``. Stores the ID in
``scope["state"][REQUEST_ID_KEY]`` and, for HTTP responses, injects the same
value into the response headers as ``X-Request-ID``.

This middleware must be registered *after* the traffic-accounting middleware
(``TrafficAccountingMiddleware``) so the request_id is available in
``scope["state"]`` when the traffic logger writes the access log.  It is
also registered *before* the reverse-proxy catch-all so that the proxy can
read the request_id from ``scope["state"]`` and forward it upstream.

Pure-ASGI (not BaseHTTPMiddleware) to keep SSE / StreamingResponse streaming
unbuffered, exactly like ``TrafficAccountingMiddleware``.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

REQUEST_ID_KEY = "request_id"  # scope["state"][REQUEST_ID_KEY] stores str

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _find_request_id(scope: dict[str, Any]) -> str | None:
    """Best-effort lookup of existing X-Request-ID from inbound headers.

    Validates the value: if empty / whitespace-only, longer than 128
    characters, or contains non-printable-ASCII bytes, returns ``None`` (so
    the caller generates a fresh id).

    P1-15: the value is restricted to **printable ASCII** (0x20–0x7e).
    Non-ASCII multibyte sequences (e.g. UTF-8 Chinese) are rejected because
    the catch-all proxy forwards the request-id into an httpx request header
    via ``client.build_request()``; a non-ASCII header value raises an
    encoding exception at BUILD time (before the ``send`` try/except that
    maps to ``upstream_unavailable``), surfacing as a bare 500 rather than a
    structured error. Fail-closed: reject → caller generates a fresh uuid.
    """
    headers: list[tuple[bytes, bytes]] = scope.get("headers") or []
    for name_bytes, value_bytes in headers:
        if name_bytes.lower() == b"x-request-id":
            # Strip leading/trailing ASCII whitespace (space, tab, CR, LF, etc.)
            # then validate every remaining byte is printable ASCII.
            stripped = value_bytes.strip()
            if not stripped or len(stripped) > 128:
                return None
            # P1-15: only accept printable ASCII (0x20-0x7e). This implicitly
            # rejects all control chars (the prior CR/LF check is subsumed)
            # AND non-ASCII multibyte UTF-8 (each byte of a multibyte sequence
            # is >= 0x80, which is > 0x7e → rejected).
            for byte in stripped:
                if byte < 0x20 or byte > 0x7E:
                    return None
            return stripped.decode("ascii")
    return None


class RequestIdMiddleware:
    """Outermost pure-ASGI middleware that manages X-Request-ID.

    Injects a fixed request ID into ``scope["state"]`` and, for HTTP responses,
    into the response headers.  For WebSocket origins, the ID is still stored
    in ``scope["state"]`` but no response header injection is needed (the
    WebSocket upgrade response is not instrumented by this middleware; it
    passes through unchanged).
    """

    __slots__ = ("app",)

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Determine the request ID
        rid = _find_request_id(scope)
        if rid is None:
            rid = uuid.uuid4().hex
        # Store into scope state
        state = scope.setdefault("state", {})
        state[REQUEST_ID_KEY] = rid

        # For HTTP, we need to inject the X-Request-ID into response headers.
        if scope.get("type") == "http":

            async def send_with_rid(message: dict[str, Any]) -> None:
                if message.get("type") == "http.response.start":
                    raw_headers: list[tuple[bytes, bytes]] = message.get("headers", [])
                    # Filter out any pre-existing X-Request-ID (case-insensitive)
                    new_headers = [
                        (n, v)
                        for n, v in raw_headers
                        if n.lower() != b"x-request-id"
                    ]
                    # Append our single request ID
                    new_headers.append((b"x-request-id", rid.encode("utf-8")))
                    message = dict(message, headers=new_headers)
                await send(message)

            await self.app(scope, receive, send_with_rid)
        else:
            # WebSocket — pass through, no header injection
            await self.app(scope, receive, send)
