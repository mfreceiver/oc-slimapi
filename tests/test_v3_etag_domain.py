"""v3-contract §6 ETag domain-isolation tests (Batch B, TDD).

B12 (2026-08-21) three-way split: the two messages-route ETag behavioural
checks (same request → same validator; envelope-content change rotates it)
were rewritten to the ``?v=4`` face — v4 messages ≡ v3 (§10) with the
validator keyed to the REP wire=v4 domain (§15), so the behaviour holds
per-view.

V2b (2026-08-21 Phase-4 guard teardown) deleted the v3-face guardian net:
the wire-marker unit locks (b"wire=v3" fingerprints, the default-view-3
terminal — the src lane re-locks the wire=v4 fingerprint/default when the
physical v3 removal lands), the retired-v2-form validator rejection
guards, and the v3 sessions 304 shape lock (the v4 global sessions list
carries no ETag at all, §4; its representation is locked in
test_sessions_v4_representation / test_vary_directory_unconditional).

Covers (all on the admitted ``?v=4`` face):

* §6.3 — envelope routes' canonical ETag input is the envelope body (same
  request → same ETag; envelope content change → different ETag).
* §6.2 — Vary never names ``?v=``/``?directory=`` (URI inputs, not
  representation dimensions).
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

IDENTITY = {"Accept-Encoding": "identity"}


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
    ])


def _sessions_payload() -> bytes:
    return orjson.dumps([{"id": "s1", "title": "one"}])


def _build_app(handler) -> FastAPI:
    app = FastAPI(title="oc-slimapi-v3-etag-test")
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


async def test_v4_etag_same_request_stable_and_own_view_304(client_factory):
    """§6.3 (B12 ①: v4 messages ≡ v3, §10 + §15 wire=v4 REP domain): same
    v4 request → same ETag; re-sent on the v4 view → 304."""
    client = await client_factory(lambda req: httpx.Response(
        200, content=_message_payload(),
        headers={"Content-Type": "application/json"}))
    try:
        first = await client.get("/slimapi/messages/s1?v=4", headers=IDENTITY)
        second = await client.get("/slimapi/messages/s1?v=4", headers=IDENTITY)
        assert first.headers["ETag"] == second.headers["ETag"]
        reval = await client.get(
            "/slimapi/messages/s1?v=4",
            headers={**IDENTITY, "If-None-Match": first.headers["ETag"]},
        )
        assert reval.status_code == 304
        assert reval.headers["ETag"] == first.headers["ETag"]
    finally:
        await client.aclose()


async def test_v4_etag_changes_with_envelope_content(client_factory):
    """§6.3 (B12 ①: v4 messages ≡ v3, §10): the canonical input is the
    envelope body — a nextCursor change (different envelope bytes, same
    items) rotates the validator."""
    state = {"link": None}
    link_next = '</session/s1/message?limit=40&before=CURSOR123>; rel="next"'

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if state["link"] is not None:
            headers["Link"] = state["link"]
        return httpx.Response(200, content=_message_payload(), headers=headers)

    client = await client_factory(handler)
    try:
        no_cursor = await client.get("/slimapi/messages/s1?v=4", headers=IDENTITY)
        state["link"] = link_next
        with_cursor = await client.get("/slimapi/messages/s1?v=4", headers=IDENTITY)
        assert no_cursor.headers["ETag"] != with_cursor.headers["ETag"]
        # …and the old validator no longer 304s against the new envelope.
        stale = await client.get(
            "/slimapi/messages/s1?v=4",
            headers={**IDENTITY, "If-None-Match": no_cursor.headers["ETag"]},
        )
        assert stale.status_code == 200
    finally:
        await client.aclose()


async def test_vary_never_mentions_v_or_directory_params(client_factory):
    """§6.2: ``?v=``/``?directory=`` are URI inputs — Vary never names
    them (only the HEADER dimension X-Opencode-Directory may appear)."""
    client = await client_factory(lambda req: httpx.Response(
        200, content=_sessions_payload(),
        headers={"Content-Type": "application/json"}))
    try:
        # 2026-08-21 narrowing: run on a consuming non-retired route
        # (messages; sessions retires directory in v4).
        response = await client.get(
            "/slimapi/messages/s1?v=4&directory=/w", headers=IDENTITY)
        assert response.status_code == 200
        vary = response.headers["Vary"]
        # §6.2 terminal: single value — neither v, directory, nor the
        # retired X-Opencode-Directory dimension appears.
        assert vary == "Accept-Encoding"
        assert "v" not in [part.strip() for part in vary.split(",")]
        assert "directory" not in [part.strip() for part in vary.split(",")]
    finally:
        await client.aclose()
