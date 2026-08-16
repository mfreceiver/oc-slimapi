"""v3-contract §4 envelope tests (Batch B, TDD).

Covers:

* ``GET /slimapi/messages/{sid}`` v3 — ``{"items":[<v2 bare array bytes>],
  "nextCursor":<string|null>}`` (byte-verbatim splice) + no ``X-Next-Cursor``
  header on 200 or 304.
* ``GET /slimapi/sessions`` v3 — ``{"items":[...],"complete":<bool>}`` both
  complete states + no ``X-Complete`` header on 200 or 304.
* ``GET /slimapi/sessions/status`` v3 — NOT enveloped (map passthrough).
* Edge cases — error responses not enveloped; 304 has no body (§6.4).
* v2 regression — no ``v`` AND explicit ``?v=2`` keep the exact v2 wire
  shape (bare array + pagination headers).
"""

from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import messages, sessions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}  # strong ETags, byte-verbatim bodies
V2_HEADERS = {"X-Slimapi-Version": "2", **IDENTITY}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _message_payload() -> bytes:
    return orjson.dumps([
        {
            "info": {"id": "m1", "role": "user", "time": {"created": 1}},
            "parts": [
                {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
            ],
        },
        {
            "info": {"id": "m2", "role": "assistant", "time": {"created": 2}},
            "parts": [
                {"id": "p2", "type": "text", "messageID": "m2", "text": "hi"},
            ],
        },
    ])


def _sessions_payload() -> bytes:
    return orjson.dumps([
        {"id": "s1", "title": "one"},
        {"id": "s2", "title": "two"},
    ])


def _build_app(handler) -> FastAPI:
    app = FastAPI(title="oc-slimapi-v3-envelope-test")
    app.state.config = _settings()
    app.state.upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=app.state.config.max_transforms,
        transform_wait_seconds=app.state.config.transform_wait_seconds,
        max_response_bytes=app.state.config.max_response_bytes,
    ))
    app.include_router(messages.router)
    app.include_router(sessions.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


def _message_handler(*, link: str | None = None, body: bytes | None = None):
    payload = body if body is not None else _message_payload()
    headers = {"Content-Type": "application/json"}
    if link is not None:
        headers["Link"] = link

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/message"):
            return httpx.Response(200, content=payload, headers=headers)
        if request.url.path == "/session":
            return httpx.Response(
                200, content=_sessions_payload(),
                headers={"Content-Type": "application/json"},
            )
        if request.url.path == "/session/status":
            return httpx.Response(
                200, content=b'{"s1": {"type": "idle"}}',
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(404, content=b'{"error":"nf"}')

    return handler


@pytest.fixture
async def client_factory():
    apps: list[FastAPI] = []

    async def make(handler) -> httpx.AsyncClient:
        app = _build_app(handler)
        apps.append(app)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    yield make
    for app in apps:
        app.state.transforms.shutdown()
        await app.state.upstream.aclose()


# ---------------------------------------------------------------------------
# messages envelope
# ---------------------------------------------------------------------------

async def test_messages_v3_envelope_null_cursor_byte_verbatim(client_factory):
    """Terminal §4: the messages list body is the
    {"items":…,"nextCursor":null} envelope (bare arrays no longer exist on
    the wire); the pagination header is NOT produced (the client reads the
    cursor from the envelope)."""
    client = await client_factory(_message_handler())
    try:
        v3 = await client.get("/slimapi/messages/s1?v=3", headers=IDENTITY)
        assert v3.status_code == 200
        assert v3.content.startswith(b'{"items":')
        assert "x-next-cursor" not in v3.headers
        assert "X-Next-Cursor" not in v3.headers
        body = orjson.loads(v3.content)
        assert list(body.keys()) == ["items", "nextCursor"]
        assert body["nextCursor"] is None
        assert "complete" not in body
        # Projection passes through both fixed-payload messages.
        assert [item["info"]["id"] for item in body["items"]] == ["m1", "m2"]
    finally:
        await client.aclose()


async def test_messages_v3_envelope_non_null_cursor(client_factory):
    """Upstream Link → envelope nextCursor carries the opaque cursor
    verbatim."""
    link = '</session/s1/message?limit=40&before=CURSOR123>; rel="next"'
    client = await client_factory(_message_handler(link=link))
    try:
        v3 = await client.get("/slimapi/messages/s1?v=3", headers=IDENTITY)
        assert orjson.loads(v3.content)["nextCursor"] == "CURSOR123"
        assert "x-next-cursor" not in v3.headers
    finally:
        await client.aclose()


async def test_messages_retired_v2_forms_rejected(client_factory):
    """Terminal §2: implicit and explicit v2 requests are both rejected with
    unsupported_version — the bare-array wire shape no longer exists."""
    link = '</session/s1/message?limit=40&before=CURSOR123>; rel="next"'
    client = await client_factory(_message_handler(link=link))
    try:
        implicit = await client.get("/slimapi/messages/s1", headers=V2_HEADERS)
        explicit = await client.get(
            "/slimapi/messages/s1?v=2", headers=V2_HEADERS)
        assert implicit.status_code == explicit.status_code == 400
        expected = {"code": "unsupported_version", "supported": [3]}
        assert orjson.loads(implicit.content) == expected
        assert orjson.loads(explicit.content) == expected
    finally:
        await client.aclose()


async def test_messages_v3_error_response_not_enveloped(client_factory):
    """§4.4: error bodies keep the v2 shape (code, no items)."""
    client = await client_factory(
        _message_handler(body=b'{"error": "not found"}'))
    try:
        # body is not a JSON array → projection failure → 502
        response = await client.get("/slimapi/messages/s1?v=3", headers=IDENTITY)
        assert response.status_code in (502, 503)
        body = orjson.loads(response.content)
        assert "items" not in body
        assert "code" in body
    finally:
        await client.aclose()


async def test_messages_v3_304_empty_body_no_aux_headers(client_factory):
    """§6.4: v3 304 = no body + only the ETag/Vary/Cache-Control set — the
    pagination headers are NOT copied (client reads them from the cached
    envelope)."""
    link = '</session/s1/message?limit=40&before=CURSOR123>; rel="next"'
    client = await client_factory(_message_handler(link=link))
    try:
        first = await client.get("/slimapi/messages/s1?v=3", headers=IDENTITY)
        etag = first.headers["ETag"]
        assert etag  # validators enabled by default
        assert "x-next-cursor" not in first.headers
        reval = await client.get(
            "/slimapi/messages/s1?v=3",
            headers={**IDENTITY, "If-None-Match": etag},
        )
        assert reval.status_code == 304
        assert reval.content == b""
        assert reval.headers["ETag"] == etag
        assert reval.headers["Cache-Control"] == "no-store"
        assert "Accept-Encoding" in reval.headers["Vary"]
        assert "x-next-cursor" not in reval.headers
        assert "X-Next-Cursor" not in reval.headers
    finally:
        await client.aclose()


async def test_messages_retired_v2_304_form_rejected(client_factory):
    """Terminal: no v2 pipeline → no v2 304 aux headers exist; the retired
    form is rejected before any validator is consulted."""
    link = '</session/s1/message?limit=40&before=CURSOR123>; rel="next"'
    client = await client_factory(_message_handler(link=link))
    try:
        first = await client.get("/slimapi/messages/s1?v=3", headers=IDENTITY)
        etag = first.headers["ETag"]
        reval = await client.get(
            "/slimapi/messages/s1",
            headers={**V2_HEADERS, "If-None-Match": etag},
        )
        assert reval.status_code == 400
        assert orjson.loads(reval.content)["code"] == "unsupported_version"
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# sessions envelope
# ---------------------------------------------------------------------------

async def test_sessions_v3_envelope_complete_true(client_factory):
    """Terminal §4: the sessions list body is the
    {"items":…,"complete":true} envelope; X-Complete is never produced."""
    client = await client_factory(_message_handler())
    try:
        v3 = await client.get("/slimapi/sessions?v=3", headers=IDENTITY)
        assert v3.status_code == 200
        assert v3.content.startswith(b'{"items":')
        body = orjson.loads(v3.content)
        assert list(body.keys()) == ["items", "complete"]
        assert body["complete"] is True
        assert body["items"] == [{"id": "s1", "title": "one"},
                                 {"id": "s2", "title": "two"}]
        assert "nextCursor" not in body
        assert "x-complete" not in v3.headers
    finally:
        await client.aclose()


async def test_sessions_v3_envelope_complete_false(client_factory):
    """?limit=1 with a 2-item upstream page → complete=false in the envelope."""
    client = await client_factory(_message_handler())
    try:
        response = await client.get(
            "/slimapi/sessions?v=3&limit=1", headers=IDENTITY)
        assert response.status_code == 200
        body = orjson.loads(response.content)
        assert body["complete"] is False
        assert len(body["items"]) == 2
        assert "x-complete" not in response.headers
    finally:
        await client.aclose()


async def test_sessions_v3_304_no_x_complete_header(client_factory):
    """§6.4: the 304 carries no X-Complete (the client reads ``complete``
    from the cached envelope)."""
    client = await client_factory(_message_handler())
    try:
        v3_first = await client.get("/slimapi/sessions?v=3", headers=IDENTITY)
        etag = v3_first.headers["ETag"]
        v3_reval = await client.get(
            "/slimapi/sessions?v=3",
            headers={**IDENTITY, "If-None-Match": etag},
        )
        assert v3_reval.status_code == 304
        assert v3_reval.content == b""
        assert "x-complete" not in v3_reval.headers
        assert "X-Complete" not in v3_reval.headers
    finally:
        await client.aclose()


async def test_sessions_status_v3_not_enveloped(client_factory):
    """§4.3: /slimapi/sessions/status keeps its map shape under v3."""
    client = await client_factory(_message_handler())
    try:
        response = await client.get(
            "/slimapi/sessions/status?v=3", headers=IDENTITY)
        assert response.status_code == 200
        body = orjson.loads(response.content)
        assert "items" not in body
        assert "complete" not in body
        assert body == {"s1": {"type": "idle"}}
    finally:
        await client.aclose()


async def test_sessions_error_response_not_enveloped(client_factory):
    """§4.4: upstream failure → v2 error shape under v3 too."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = await client_factory(handler)
    try:
        response = await client.get("/slimapi/sessions?v=3", headers=IDENTITY)
        assert response.status_code == 503  # upstream 5xx → upstream_unavailable
        body = orjson.loads(response.content)
        assert "items" not in body
        assert "code" in body
    finally:
        await client.aclose()
