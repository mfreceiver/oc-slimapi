"""Phase C/P5 tests for the raw file-content projection route."""

from __future__ import annotations

import asyncio
import base64
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import file_raw
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool


def _settings(**overrides) -> Settings:
    values = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.05,
        max_response_bytes=64 * 1024,
        file_raw_max_envelope_bytes=32 * 1024 * 1024,
        smoke_session_id=None,
        directory_allowlist=None,
    )
    values.update(overrides)
    return Settings(**values)


def _legacy_binary(body: bytes, *, mime: str | None = "image/png") -> bytes:
    value: dict[str, object] = {
        "type": "binary",
        "content": base64.b64encode(body).decode("ascii"),
    }
    if mime is not None:
        value["mimeType"] = mime
    return orjson.dumps(value)


def _legacy_text(body: str) -> bytes:
    return orjson.dumps({"type": "text", "content": body})


def _response_handler(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
):
    response_headers = {"Content-Type": "application/json"}
    response_headers.update(headers or {})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=body,
            headers=response_headers,
        )

    return handler


@asynccontextmanager
async def _stack(
    handler,
    *,
    settings: Settings | None = None,
    selector: bool = True,
) -> AsyncIterator[tuple[httpx.AsyncClient, list[httpx.Request], TransformPool]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = settings or _settings()
    app.state.config = settings
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.upstream = upstream
    app.state.schema_degraded = False
    pool = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.transforms = pool
    app.include_router(file_raw.router)
    register_error_handlers(app)
    if selector:
        app.add_middleware(SlimapiSelectorMiddleware)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client, seen, pool
    finally:
        pool.shutdown()
        await upstream.aclose()


async def test_binary_content_is_decoded_to_identity_bytes_and_mime_is_preserved():
    payload = b"\x00\x01PNG\xff\x10"
    async with _stack(_response_handler(_legacy_binary(payload))) as (
        client, seen, _pool,
    ):
        response = await client.get(
            "/slimapi/file/raw?v=4&path=image.png",
            headers={"Accept-Encoding": "gzip"},
        )

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "image/png"
    assert "content-encoding" not in response.headers
    assert not response.headers["etag"].startswith("W/")
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["cache-control"] == "no-store"
    assert seen[0].url.path == "/file/content"
    assert seen[0].url.params["path"] == "image.png"
    assert "v" not in seen[0].url.params


@pytest.mark.parametrize("mime", [None, "not a mime", "image/png; bad"])
async def test_invalid_or_missing_binary_mime_defaults_to_octet_stream(mime):
    async with _stack(_response_handler(_legacy_binary(b"raw", mime=mime))) as (
        client, _seen, _pool,
    ):
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 200
    assert response.content == b"raw"
    assert response.headers["content-type"] == "application/octet-stream"


async def test_text_content_negotiates_gzip_and_uses_a_weak_etag():
    text = "repeated text " * 100
    async with _stack(_response_handler(_legacy_text(text))) as (
        client, _seen, _pool,
    ):
        response = await client.get(
            "/slimapi/file/raw?v=4&path=note.txt",
            headers={"Accept-Encoding": "gzip"},
        )

    assert response.status_code == 200
    assert response.content == text.encode()
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["etag"].startswith("W/\"")
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.headers["cache-control"] == "no-store"


async def test_binary_ignores_gzip_accept_encoding():
    payload = b"binary identity bytes"
    async with _stack(_response_handler(_legacy_binary(payload))) as (
        client, _seen, _pool,
    ):
        response = await client.get(
            "/slimapi/file/raw?v=4&path=x.bmp",
            headers={"Accept-Encoding": "gzip"},
        )

    assert response.content == payload
    assert "content-encoding" not in response.headers
    assert response.headers["etag"].startswith('"')


async def test_raw_etag_returns_a_complete_304_response():
    body = _legacy_binary(b"stable bytes")
    async with _stack(_response_handler(body)) as (client, _seen, _pool):
        first = await client.get("/slimapi/file/raw?v=4&path=x")
        second = await client.get(
            "/slimapi/file/raw?v=4&path=x",
            headers={"If-None-Match": first.headers["etag"]},
        )

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == first.headers["etag"]
    assert second.headers["vary"] == "Accept-Encoding"
    assert second.headers["cache-control"] == "no-store"
    assert "content-encoding" not in second.headers


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        orjson.dumps({"type": "other", "content": "x"}),
        orjson.dumps({"type": "text"}),
        orjson.dumps({"type": "text", "content": 1}),
        orjson.dumps({"type": "binary", "content": "%%%"}),
    ],
)
async def test_malformed_success_envelope_is_a_house_502(body):
    async with _stack(_response_handler(body)) as (client, _seen, _pool):
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 502
    assert orjson.loads(response.content) == {"code": "raw_decode_failed"}
    assert response.headers["cache-control"] == "no-store"
    assert b"%%%" not in response.content


async def test_upstream_4xx_is_returned_verbatim():
    upstream_body = b"upstream validation body"
    async with _stack(_response_handler(
        upstream_body,
        status=422,
        headers={"Content-Type": "application/problem+json"},
    )) as (client, _seen, _pool):
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 422
    assert response.content == upstream_body
    assert response.headers["content-type"] == "application/problem+json"
    # Gate-MAJOR-2.2: every error frame on this route is no-store — the
    # verbatim 4xx pass-through included.
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_upstream_5xx_maps_to_upstream_unavailable(status):
    async with _stack(_response_handler(b"upstream secret", status=status)) as (
        client, _seen, _pool,
    ):
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 503
    assert orjson.loads(response.content)["code"] == "upstream_unavailable"
    assert b"upstream secret" not in response.content
    assert response.headers["cache-control"] == "no-store"


async def test_upstream_network_error_maps_to_upstream_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private upstream detail", request=request)

    async with _stack(handler) as (client, _seen, _pool):
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 503
    assert orjson.loads(response.content)["code"] == "upstream_unavailable"
    assert b"private upstream detail" not in response.content
    # Gate-MAJOR-2.3: the initial-send network failure renders a LOCAL 503
    # (the global CodedHTTPException renderer stamps no no-store).
    assert response.headers["cache-control"] == "no-store"


async def test_midstream_network_error_maps_to_local_no_store_503():
    """Gate-MAJOR-2.3: a 2xx response whose body read breaks mid-stream
    (partial bytes already attributed) must surface as the local no-store
    503 — not a global-rendered bare 503."""

    async def flaky_body():
        yield b"partial envelope bytes"
        raise httpx.ReadError("mid-stream break")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=flaky_body(),
        )

    async with _stack(handler) as (client, _seen, _pool):
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 503
    assert orjson.loads(response.content)["code"] == "upstream_unavailable"
    assert b"partial envelope bytes" not in response.content
    assert response.headers["cache-control"] == "no-store"


async def test_missing_path_is_400_invalid_params_not_422():
    """Gate-MAJOR-2.1 (§19): ``path`` is required — its absence is a sidecar
    400 ``invalid_params`` (with no-store), never FastAPI's default 422,
    and never reaches upstream."""
    async with _stack(_response_handler(_legacy_text("x"))) as (
        client, seen, _pool,
    ):
        response = await client.get("/slimapi/file/raw?v=4")

    assert response.status_code == 400
    assert orjson.loads(response.content) == {"code": "invalid_params"}
    assert response.headers["cache-control"] == "no-store"
    assert seen == []


@pytest.mark.parametrize("url", [
    "/slimapi/file/raw?path=x",
    "/slimapi/file/raw?v=3&path=x",
    "/slimapi/file/raw?v=3&v=4&path=x",
])
async def test_v4_selector_rejects_missing_wrong_and_conflicting_versions(url):
    async with _stack(_response_handler(_legacy_text("x"))) as (
        client, seen, _pool,
    ):
        response = await client.get(url)

    assert response.status_code == 400
    assert seen == []


async def test_directory_allowlist_authorizes_canonical_query_directory(tmp_path: Path):
    allowed = tmp_path / "workspace"
    allowed.mkdir()

    async with _stack(
        _response_handler(_legacy_text("ok")),
        settings=_settings(directory_allowlist=[str(allowed)]),
    ) as (client, seen, _pool):
        response = await client.get(
            "/slimapi/file/raw?v=4&path=x&directory=" + str(allowed),
        )

    assert response.status_code == 200
    assert seen[0].headers["x-opencode-directory"] == str(allowed.resolve())
    assert "directory" not in seen[0].url.params


async def test_directory_allowlist_rejects_unauthorized_query_directory(tmp_path: Path):
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()

    async with _stack(
        _response_handler(_legacy_text("ok")),
        settings=_settings(directory_allowlist=[str(allowed)]),
    ) as (client, seen, _pool):
        response = await client.get(
            "/slimapi/file/raw?v=4&path=x&directory=" + str(denied),
        )

    assert response.status_code == 403
    assert orjson.loads(response.content)["code"] == "directory_not_allowed"
    # Gate-R2-MAJOR (§19): the allowlist 403 is rendered route-locally, so
    # like every error frame on this route it carries no-store.
    assert response.headers["cache-control"] == "no-store"
    assert seen == []


async def test_effective_cap_uses_the_smaller_global_or_raw_limit():
    body = _legacy_text("payload")
    async with _stack(
        _response_handler(body),
        settings=_settings(
            max_response_bytes=len(body) - 1,
            file_raw_max_envelope_bytes=len(body) + 100,
        ),
    ) as (client, _seen, _pool):
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 413
    assert orjson.loads(response.content)["code"] == "response_too_large"
    assert response.headers["cache-control"] == "no-store"


async def test_envelope_cap_exact_boundary_succeeds_and_one_byte_over_is_413():
    body = _legacy_text("exact")
    async with _stack(
        _response_handler(body),
        settings=_settings(
            max_response_bytes=len(body),
            file_raw_max_envelope_bytes=len(body),
        ),
    ) as (client, _seen, _pool):
        exact = await client.get("/slimapi/file/raw?v=4&path=x")

    async with _stack(
        _response_handler(body + b"!"),
        settings=_settings(
            max_response_bytes=len(body) + 1,
            file_raw_max_envelope_bytes=len(body),
        ),
    ) as (client, _seen, _pool):
        over = await client.get("/slimapi/file/raw?v=4&path=x")

    assert exact.status_code == 200
    assert over.status_code == 413


def test_phase_a_startup_budget_includes_file_raw_peak():
    settings = _settings(
        max_transforms=2,
        max_response_bytes=64 * 1024 * 1024,
        file_raw_max_envelope_bytes=64 * 1024 * 1024,
    )

    with pytest.raises(RuntimeError, match="raw-fetch and transform budgets"):
        settings.validate()


async def test_permit_is_acquired_before_upstream_get_read_and_submit(monkeypatch):
    events: list[str] = []
    body = _legacy_text("ordering")

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("get")
        return httpx.Response(200, content=body)

    async with _stack(handler) as (client, _seen, pool):
        original_acquire = pool.acquire

        async def acquire(*args, **kwargs):
            events.append("permit")
            return await original_acquire(*args, **kwargs)

        pool.acquire = acquire
        original_submit = pool._executor.submit

        def submit(*args, **kwargs):
            events.append("submit")
            return original_submit(*args, **kwargs)

        pool._executor.submit = submit
        original_read = file_raw.read_with_cap

        async def read(*args, **kwargs):
            events.append("read")
            return await original_read(*args, **kwargs)

        monkeypatch.setattr(file_raw, "read_with_cap", read)
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 200
    assert events.index("permit") < events.index("get")
    assert events.index("get") < events.index("read")
    assert events.index("read") < events.index("submit")


class _GateStream(httpx.AsyncByteStream):
    def __init__(
        self,
        body: bytes,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.body = body
        self.started = started
        self.release = release

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.release.wait()
        yield self.body


async def _wait_started(*events: asyncio.Event) -> None:
    await asyncio.gather(*(event.wait() for event in events))


async def test_one_blocked_work_item_leaves_a_second_permit_available():
    body = _legacy_text("old")
    old_started = asyncio.Event()
    old_release = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["path"] == "old":
            return httpx.Response(
                200,
                stream=_GateStream(body, old_started, old_release),
            )
        return httpx.Response(200, content=_legacy_text("new"))

    tasks: list[asyncio.Task[httpx.Response]] = []
    async with _stack(
        handler,
        settings=_settings(max_transforms=2),
    ) as (client, _seen, pool):
        try:
            tasks.append(asyncio.create_task(
                client.get("/slimapi/file/raw?v=4&path=old"),
            ))
            await asyncio.wait_for(old_started.wait(), timeout=1)
            fresh = await client.get("/slimapi/file/raw?v=4&path=new")
            assert fresh.status_code == 200
            assert pool.snapshot_metrics()["active"] == 1
        finally:
            old_release.set()
            if tasks:
                await tasks[0]


async def test_full_admission_returns_503_without_get_read_or_submit():
    body = _legacy_text("blocked")
    started = (asyncio.Event(), asyncio.Event())
    release = (asyncio.Event(), asyncio.Event())

    def handler(request: httpx.Request) -> httpx.Response:
        index = 0 if request.url.params["path"] == "old-1" else 1
        if request.url.params["path"] in {"old-1", "old-2"}:
            return httpx.Response(
                200,
                stream=_GateStream(body, started[index], release[index]),
            )
        raise AssertionError("saturated admission must not call upstream")

    async with _stack(
        handler,
        settings=_settings(max_transforms=2),
    ) as (client, seen, pool):
        old_tasks = [
            asyncio.create_task(
                client.get(f"/slimapi/file/raw?v=4&path=old-{index}"),
            )
            for index in (1, 2)
        ]
        await asyncio.wait_for(_wait_started(*started), timeout=1)
        submit_count = 0
        original_submit = pool._executor.submit

        def submit(*args, **kwargs):
            nonlocal submit_count
            submit_count += 1
            return original_submit(*args, **kwargs)

        pool._executor.submit = submit
        try:
            first = await client.get("/slimapi/file/raw?v=4&path=new-1")
            second = await client.get("/slimapi/file/raw?v=4&path=new-2")
            new_requests_submit_count = submit_count
        finally:
            for event in release:
                event.set()
            await asyncio.gather(*old_tasks)

    assert first.status_code == second.status_code == 503
    assert orjson.loads(first.content)["code"] == "transform_busy"
    assert [request.url.params["path"] for request in seen] == ["old-1", "old-2"]
    assert new_requests_submit_count == 0


async def test_cancelled_raw_request_keeps_permit_until_worker_finishes(monkeypatch):
    body = _legacy_text("cancel me")
    started = threading.Event()
    finished = threading.Event()

    original = file_raw._decode_legacy_content

    def blocked_decode(raw: bytes):
        started.set()
        finished.wait(timeout=2)
        return original(raw)

    async with _stack(_response_handler(body), settings=_settings()) as (
        client, seen, pool,
    ):
        monkeypatch.setattr(file_raw, "_decode_legacy_content", blocked_decode)
        task = asyncio.create_task(client.get("/slimapi/file/raw?v=4&path=old"))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert pool.snapshot_metrics()["active"] == 1
        blocked = await client.get("/slimapi/file/raw?v=4&path=new")
        assert blocked.status_code == 503
        assert len(seen) == 1
        finished.set()
        for _ in range(100):
            if pool.snapshot_metrics()["active"] == 0:
                break
            await asyncio.sleep(0.01)
        assert pool.snapshot_metrics()["active"] == 0


async def test_raw_route_uses_offload_strict(monkeypatch):
    calls = 0
    async with _stack(_response_handler(_legacy_text("strict"))) as (
        client, _seen, pool,
    ):
        original = pool.offload_strict

        async def strict(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)

        pool.offload_strict = strict
        response = await client.get("/slimapi/file/raw?v=4&path=x")

    assert response.status_code == 200
    assert calls == 1
