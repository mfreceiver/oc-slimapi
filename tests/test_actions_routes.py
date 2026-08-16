"""Route-level integration tests for ``routes/actions.py`` (Wave 2).

Exercises the ``/slimapi/actions`` wire contract end-to-end through the
actions router (mirrors the ``_settings`` / ``_build_app`` pattern of
``tests/test_agent_routes.py``):

* ``GET /slimapi/actions`` — discovery shape (enabled true / false).
* ``POST /slimapi/actions/{name}`` — exec/query envelopes (200, ok 判成败),
  raw-body handling (empty / ``{}`` / malformed 422), confirm gating
  (missing→409 / true→pass / ignored when not required), and all seven coded
  error mappings (404 / 409 / 429 / 503×3 / 504) with their
  ``Retry-After`` / ``timeout_s`` payloads.
* Version gate (missing header → 400) and gzip negotiation on both 200 and
  coded-error responses.

The registry is constructed directly from :class:`ActionSpec` — manifest
validation and executor semantics are covered by ``tests/test_actions.py``;
this file locks the **route layer** behaviour.  Client-disconnect cleanup is
exercised at the registry level in ``test_actions.py``
(``test_audit_disconnect``); driving a genuine mid-subprocess ASGI disconnect
deterministically through httpx's ASGITransport is not reliably writable, so
it is noted rather than asserted here.
"""
from __future__ import annotations

import gzip
import sys

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.actions import ActionRegistry, ActionSpec
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import actions

# Wire version 2 is the current contract revision; the middleware accepts
# exactly [2, 2].
VERSION_HEADERS = {"X-Slimapi-Version": "2"}


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


class _ExplodingUpstream:
    """The actions routes are sidecar-local — they must NEVER touch upstream.
    If the catch-all reverse proxy were reached (a routing regression), this
    stub fails loudly instead of masking the bug with a 502."""

    def __getattr__(self, name):
        raise AssertionError(
            f"actions route fell through to the catch-all proxy ({name}!)"
        )


def _build_app(registry: ActionRegistry) -> FastAPI:
    """Construct a fresh FastAPI app with the actions router wired up and
    ``app.state.actions_registry`` pre-populated, mirroring the real wiring
    (version gate + router + catch-all + coded error handler)."""
    app = FastAPI(title="oc-slimapi-test")
    app.state.config = _settings()
    app.state.actions_registry = registry
    app.state.upstream = _ExplodingUpstream()
    app.include_router(actions.router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _spec(
    name: str,
    *,
    kind: str = "exec",
    argv: list[str] | None = None,
    description: str = "d",
    timeout_s: float = 30.0,
    min_interval_s: float = 0.0,
    require_confirm: bool = False,
    max_output_bytes: int | None = None,
    cwd: str | None = None,
) -> ActionSpec:
    return ActionSpec(
        name=name,
        kind=kind,
        argv=tuple(argv or [sys.executable, "-c", "pass"]),
        description=description,
        timeout_s=timeout_s,
        min_interval_s=min_interval_s,
        require_confirm=require_confirm,
        max_output_bytes=max_output_bytes,
        cwd=cwd,
    )


async def _client(registry: ActionRegistry) -> tuple[httpx.AsyncClient, FastAPI]:
    app = _build_app(registry)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), app


# ---------------------------------------------------------------------------
# GET /slimapi/actions — discovery shape
# ---------------------------------------------------------------------------


async def test_get_actions_enabled_shape():
    reg = ActionRegistry(
        enabled=True,
        actions={
            "run": _spec("run", description="runs things", require_confirm=True),
            "q": _spec("q", kind="query", description="queries"),
        },
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.get("/slimapi/actions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    body = orjson.loads(response.content)
    assert body["enabled"] is True
    by_name = {entry["name"]: entry for entry in body["actions"]}
    assert set(by_name) == {"run", "q"}
    assert by_name["run"] == {
        "name": "run", "kind": "exec", "description": "runs things",
        "requireConfirm": True,
    }
    assert by_name["q"]["kind"] == "query"
    assert by_name["q"]["requireConfirm"] is False
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Vary"] == "Accept-Encoding"


async def test_get_actions_disabled_shape():
    reg = ActionRegistry(enabled=False, actions={}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        response = await client.get("/slimapi/actions", headers=VERSION_HEADERS)
    assert response.status_code == 200
    assert orjson.loads(response.content) == {"enabled": False, "actions": []}
    assert response.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# POST /slimapi/actions/{name} — exec / query envelopes
# ---------------------------------------------------------------------------


async def test_post_exec_success_200():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run")},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"",
        )
    assert response.status_code == 200
    body = orjson.loads(response.content)
    assert body["kind"] == "exec"
    assert body["ok"] is True
    assert body["exit_code"] == 0
    assert body["message"] is None
    assert isinstance(body["duration_ms"], int) and body["duration_ms"] >= 0
    # query-only fields excluded from the exec envelope.
    assert "markdown" not in body and "truncated" not in body
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Vary"] == "Accept-Encoding"


async def test_post_exec_nonzero_200():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run", argv=[sys.executable, "-c", "import sys; sys.exit(3)"])},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 200  # sidecar-level success; ok 判成败
    body = orjson.loads(response.content)
    assert body["kind"] == "exec"
    assert body["ok"] is False
    assert body["exit_code"] == 3
    assert body["message"] == "non-zero exit"


async def test_post_query_success_200():
    reg = ActionRegistry(
        enabled=True,
        actions={
            "q": _spec(
                "q", kind="query",
                argv=[sys.executable, "-c", "import sys; sys.stdout.write('hello md')"],
            ),
        },
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/q", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 200
    body = orjson.loads(response.content)
    assert body["kind"] == "query"
    assert body["ok"] is True
    assert body["markdown"] == "hello md"
    assert body["exit_code"] == 0
    assert body["truncated"] is False
    assert body["message"] is None


async def test_post_query_nonzero_200():
    reg = ActionRegistry(
        enabled=True,
        actions={
            "q": _spec(
                "q", kind="query",
                argv=[sys.executable, "-c", "import sys; sys.stderr.write('x'); sys.exit(2)"],
            ),
        },
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/q", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 200
    body = orjson.loads(response.content)
    assert body["ok"] is False
    assert body["markdown"] == ""
    assert body["exit_code"] == 2


# ---------------------------------------------------------------------------
# POST — coded error mappings (404 / 409 / 429 / 503×3 / 504)
# ---------------------------------------------------------------------------


async def test_post_action_not_found_404():
    reg = ActionRegistry(enabled=True, actions={"run": _spec("run")}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/nope", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 404
    assert orjson.loads(response.content) == {"code": "action_not_found"}
    # coded error also pinned no-store (contract §5).
    assert response.headers["Cache-Control"] == "no-store"


async def test_post_confirm_required_409():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run", require_confirm=True)},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 409
    assert orjson.loads(response.content) == {"code": "action_confirm_required"}


async def test_post_confirm_true_passes():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run", require_confirm=True)},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run",
            headers=VERSION_HEADERS, content=b'{"confirm": true}',
        )
    assert response.status_code == 200
    assert orjson.loads(response.content)["ok"] is True


async def test_post_confirm_ignored_when_not_required():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run", require_confirm=False)},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run",
            headers=VERSION_HEADERS, content=b'{"confirm": true}',
        )
    assert response.status_code == 200  # confirm received → ignored
    assert orjson.loads(response.content)["ok"] is True


async def test_post_throttled_429_with_retry_after():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run", min_interval_s=60.0)},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        first = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"{}",
        )
        assert first.status_code == 200
        second = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"{}",
        )
    assert second.status_code == 429
    assert orjson.loads(second.content)["code"] == "action_throttled"
    retry = int(second.headers["Retry-After"])
    assert 1 <= retry <= 60


async def test_post_actions_disabled_503():
    reg = ActionRegistry(enabled=False, actions={}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 503
    assert orjson.loads(response.content) == {"code": "actions_disabled"}


async def test_post_action_unavailable_503():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run", argv=["/nonexistent/definitely/missing"])},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 503
    assert orjson.loads(response.content) == {"code": "action_unavailable"}


async def test_post_action_busy_503_with_retry_after():
    """Service-level admission: saturate the single spawn slot, then POST a
    second (different) action → 503 action_busy + Retry-After: 2."""
    import asyncio

    reg = ActionRegistry(
        enabled=True,
        actions={
            "holder": _spec(
                "holder", argv=[sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_s=15.0,
            ),
            "b": _spec("b"),
        },
        max_concurrent=1,
    )
    # Hold the only admission slot deterministically (registry-level busy
    # semantics are covered in test_actions.py; here we just need the route
    # to surface ActionBusy as 503).
    await reg._semaphore.acquire()
    try:
        client, _ = await _client(reg)
        async with client:
            response = await client.post(
                "/slimapi/actions/b", headers=VERSION_HEADERS, content=b"{}",
            )
        assert response.status_code == 503
        assert orjson.loads(response.content) == {"code": "action_busy"}
        assert response.headers["Retry-After"] == "2"
    finally:
        reg._semaphore.release()


async def test_post_action_timeout_504():
    reg = ActionRegistry(
        enabled=True,
        actions={
            "run": _spec(
                "run", argv=[sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_s=1.0,
            ),
        },
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"{}",
        )
    assert response.status_code == 504
    body = orjson.loads(response.content)
    assert body["code"] == "action_timeout"
    assert body["timeout_s"] == 1.0
    assert response.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# POST — raw body handling (empty / {} / malformed 422)
# ---------------------------------------------------------------------------


async def test_post_empty_body_treated_as_object():
    reg = ActionRegistry(enabled=True, actions={"run": _spec("run")}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"",
        )
    assert response.status_code == 200  # empty body == {}


async def test_post_malformed_body_422():
    reg = ActionRegistry(enabled=True, actions={"run": _spec("run")}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        garbage = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"not json{",
        )
        non_object = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"[1,2]",
        )
        non_bool_confirm = await client.post(
            "/slimapi/actions/run",
            headers=VERSION_HEADERS, content=b'{"confirm": "yes"}',
        )
    for response in (garbage, non_object, non_bool_confirm):
        assert response.status_code == 422
        assert orjson.loads(response.content) == {"code": "invalid_request_body"}
        assert response.headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------------------
# POST — 1 KiB request-body cap (plaintext memory-DoS guard, rev-13)
# ---------------------------------------------------------------------------


async def test_post_body_content_length_over_cap_413():
    """An advertised Content-Length over the 1 KiB body cap is rejected 413
    ``request_too_large`` before any body byte is read."""
    reg = ActionRegistry(enabled=True, actions={"run": _spec("run")}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"x" * 4096,
        )
    assert response.status_code == 413
    assert orjson.loads(response.content) == {"code": "request_too_large"}
    assert response.headers["Cache-Control"] == "no-store"


async def test_post_body_at_cap_boundary_not_413():
    """A body exactly at the cap (1024) passes the size gate — its malformed
    JSON then yields 422, proving the 413 gate is strictly ``> cap``."""
    reg = ActionRegistry(enabled=True, actions={"run": _spec("run")}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        response = await client.post(
            "/slimapi/actions/run", headers=VERSION_HEADERS, content=b"x" * 1024,
        )
    assert response.status_code == 422  # malformed JSON, NOT size-rejected
    assert orjson.loads(response.content) == {"code": "invalid_request_body"}


async def test_post_body_chunked_over_cap_413():
    """Chunked transfer (no usable Content-Length) is capped too: the stream is
    read up to cap+1 bytes and rejected 413 the instant it overruns."""
    from starlette.requests import Request

    from oc_slimapi.errors import CodedHTTPException
    from oc_slimapi.routes.actions import _read_body

    chunks = [b"y" * 600, b"z" * 600]

    async def receive():
        if chunks:
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk,
                    "more_body": len(chunks) > 0}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/slimapi/actions/run",
        "raw_path": b"/slimapi/actions/run", "query_string": b"",
        "root_path": "", "headers": [],  # no Content-Length → chunked path
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    request = Request(scope, receive)
    with pytest.raises(CodedHTTPException) as ei:
        await _read_body(request)
    assert ei.value.status_code == 413
    assert ei.value.code == "request_too_large"
    assert ei.value.headers == {"Cache-Control": "no-store"}


async def test_post_body_single_huge_chunk_413():
    """A SINGLE chunk larger than the cap is rejected 413 without ever being
    buffered (rev-14): the pre-fix code appended the chunk to ``raw`` before
    checking the cap, so one oversized chunk transiently buffered its full
    size — this locks the check-before-append ordering."""
    from starlette.requests import Request

    from oc_slimapi.errors import CodedHTTPException
    from oc_slimapi.routes.actions import _read_body

    async def receive():
        return {"type": "http.request", "body": b"y" * 8192, "more_body": False}

    scope = {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/slimapi/actions/run",
        "raw_path": b"/slimapi/actions/run", "query_string": b"",
        "root_path": "", "headers": [],  # no Content-Length → chunked path
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    request = Request(scope, receive)
    with pytest.raises(CodedHTTPException) as ei:
        await _read_body(request)
    assert ei.value.status_code == 413
    assert ei.value.code == "request_too_large"
    assert ei.value.headers == {"Cache-Control": "no-store"}


# ---------------------------------------------------------------------------
# Version gate + gzip negotiation
# ---------------------------------------------------------------------------


async def test_retired_version_header_ignored_at_route_level():
    """§1 terminal: the X-Slimapi-Version header is dead input — the route
    (selector owns admission) neither requires nor interprets it."""
    reg = ActionRegistry(enabled=True, actions={"run": _spec("run")}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        response = await client.get("/slimapi/actions",
                                    headers={"X-Slimapi-Version": "garbage"})
    assert response.status_code == 200
    assert "actions" in response.json()


async def test_gzip_negotiation_on_coded_error():
    """A coded error (404 action_not_found) requested with
    Accept-Encoding: gzip must be genuinely gzipped on the wire."""
    reg = ActionRegistry(enabled=True, actions={"run": _spec("run")}, max_concurrent=4)
    client, _ = await _client(reg)
    async with client:
        async with client.stream(
            "POST", "/slimapi/actions/nope",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
            content=b"{}",
        ) as response:
            assert response.status_code == 404
            assert response.headers["Content-Encoding"] == "gzip"
            raw = b""
            async for chunk in response.aiter_raw():
                raw += chunk
    assert raw[:2] == b"\x1f\x8b"  # gzip magic
    assert orjson.loads(gzip.decompress(raw)) == {"code": "action_not_found"}


async def test_gzip_negotiation_on_200():
    reg = ActionRegistry(
        enabled=True,
        actions={"run": _spec("run", description="x" * 200)},
        max_concurrent=4,
    )
    client, _ = await _client(reg)
    async with client:
        async with client.stream(
            "GET", "/slimapi/actions",
            headers={**VERSION_HEADERS, "Accept-Encoding": "gzip"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["Content-Encoding"] == "gzip"
            raw = b""
            async for chunk in response.aiter_raw():
                raw += chunk
    assert raw[:2] == b"\x1f\x8b"
    assert orjson.loads(gzip.decompress(raw))["enabled"] is True
