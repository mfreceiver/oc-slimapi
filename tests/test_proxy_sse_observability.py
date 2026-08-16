"""v3-contract §9.2 — catch-all SSE (/event, /global/event) observability (B2).

The catch-all SSE passthrough streams also emit sse_open / sse_close
lifecycle rows (selectorResult=not_applicable, wireVersion=null, bucket
passthrough, shared lifecycleId) and bump the ledger's not_applicable
sseActive dim. Non-SSE catch-all requests emit no lifecycle rows.

B4/B5 (rev re-review): the lifecycle open is judged by RESPONSE NATURE
(upstream 200 + text/event-stream) — not by path; and the close row is
guaranteed on every teardown path (emitted before the aclose await).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.proxy import install_proxy
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi import sse_observability
from oc_slimapi.traffic import TrafficLedger


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def capture_logger(monkeypatch):
    logger = logging.getLogger("oc_slimapi.test.capture_sse")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    monkeypatch.setattr(sse_observability, "_access_logger", lambda: logger)
    yield logger
    logger.removeHandler(handler)


def _rows(logger) -> list[dict]:
    return [json.loads(line) for line in logger.handlers[0].lines if line]


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    upstream: httpx.AsyncClient,
    ledger: TrafficLedger | None,
    *,
    request_logger: logging.Logger | None = None,
) -> FastAPI:
    app = FastAPI(title="proxy-sse-obs-test")
    app.state.config = _settings()
    app.state.upstream = upstream
    if ledger is not None:
        app.state.traffic_ledger = ledger
    # Production stack order: selector stashes not_applicable for catch-all
    # paths, which the sse observability reads for the §9.2 dim. The
    # accounting middleware (outer) is added only when the test asserts
    # request rows.
    app.add_middleware(SlimapiSelectorMiddleware)
    if request_logger is not None:
        app.add_middleware(TrafficAccountingMiddleware, logger=request_logger)
    install_proxy(app)
    return app


def _sse_upstream():
    """Streaming text/event-stream upstream: two frames, then clean close.

    Content-type carries a parameter (charset) — the SSE judgement must
    tolerate parameters per RFC.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            yield b"data: {\"type\":\"a\"}\n\n"
            yield b"data: {\"type\":\"b\"}\n\n"

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
        )

    return handler


async def _consume(app, path: str) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.send(client.build_request("GET", path), stream=True)
        # Drain the whole stream so the generator's finally (sse_close) runs
        # inside this test — mirrors the /slimapi/events SSE test strategy.
        async for _ in response.aiter_bytes():
            pass
        await response.aclose()
        return response


@pytest.mark.parametrize("path", ["/event", "/global/event"])
async def test_catchall_sse_emits_open_and_close_rows(capture_logger, path):
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(_sse_upstream()),
        base_url="http://upstream",
    )
    app = _build_app(upstream, ledger=None)

    response = await _consume(app, path)
    assert response.status_code == 200

    rows = _rows(capture_logger)
    lifecycle = [r for r in rows if r.get("recordType") in ("sse_open", "sse_close")]
    assert len(lifecycle) == 2
    opens = [r for r in lifecycle if r["recordType"] == "sse_open"]
    closes = [r for r in lifecycle if r["recordType"] == "sse_close"]
    assert len(opens) == 1 and len(closes) == 1
    open_row, close_row = opens[0], closes[0]
    # Pairing: same process-monotonic lifecycleId on both rows.
    assert isinstance(open_row["lifecycleId"], int)
    assert open_row["lifecycleId"] == close_row["lifecycleId"]
    # §9.2 dim attribution for the catch-all.
    for row in (open_row, close_row):
        assert row["selectorResult"] == "not_applicable"
        assert row["wireVersion"] is None
        assert row["bucket"] == "passthrough"
        assert row["path"] == path
    assert open_row["status"] == 200


async def test_catchall_sse_ledger_not_applicable_dim(capture_logger):
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(_sse_upstream()),
        base_url="http://upstream",
    )
    ledger = TrafficLedger(enabled=True)
    app = _build_app(upstream, ledger=ledger)

    await _consume(app, "/event")

    v3 = ledger.snapshot()["v3"]
    assert v3["sseLifecycle"]["not_applicable"] == {
        "opens": 1, "closes": 1, "active": 0, "orphanCloses": 0,
    }
    assert v3["sseActive"]["not_applicable"] == 0  # closed within the test


async def test_catchall_non_sse_no_lifecycle_rows(capture_logger):
    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            yield b'{"ok":true}'

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream",
    )
    app = _build_app(upstream, ledger=None)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/session/ses_probe/messages")
    assert response.status_code == 200
    assert _rows(capture_logger) == []


# ---------------------------------------------------------------------------
# B4: response-natured SSE judgement — SSE PATH + non-SSE RESPONSE must not
# emit lifecycle rows (open is judged on 200 + text/event-stream, not path)
# ---------------------------------------------------------------------------


def _non_stream_upstream(status: int, content_type: str, payload: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            if payload:
                yield payload

        return httpx.Response(
            status,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": content_type},
        )

    return handler


def _lifecycle_rows(rows):
    return [r for r in rows if r.get("recordType") in ("sse_open", "sse_close")]


async def _get_plain(app, path: str) -> httpx.Response:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.parametrize(
    "status,content_type,payload",
    [
        (404, "text/plain", b"not found"),
        (503, "application/json", b'{"code":"busy"}'),
        (200, "application/json", b'{"ok":true}'),  # 200 but JSON, not SSE
    ],
)
async def test_sse_path_non_sse_response_no_lifecycle_rows(
    capture_logger, status, content_type, payload
):
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            _non_stream_upstream(status, content_type, payload)
        ),
        base_url="http://upstream",
    )
    app = _build_app(upstream, ledger=None, request_logger=capture_logger)

    response = await _get_plain(app, "/event")
    assert response.status_code == status

    rows = _rows(capture_logger)
    # No lifecycle rows — the stream was never an SSE stream.
    assert _lifecycle_rows(rows) == []
    # Plain request-row accounting only (B4: 按普通 request 行记账).
    requests = [r for r in rows if r.get("recordType", "request") == "request"]
    assert len(requests) == 1
    assert requests[0]["status"] == status
    assert requests[0]["bucket"] == "passthrough"
    assert requests[0]["selectorResult"] == "not_applicable"


# ---------------------------------------------------------------------------
# B5: close-row guarantee on abnormal teardown — emitted BEFORE the aclose
# await, so aclose failures / cancellation at that point cannot skip it
# ---------------------------------------------------------------------------


def _boom_aclose(monkeypatch, exc_factory):
    """Make httpx.Response.aclose raise — but ONLY for the proxied upstream
    response (host "upstream"), not the test client's own response."""
    original = httpx.Response.aclose

    async def patched(self):
        request = getattr(self, "request", None)
        if request is not None and request.url.host == "upstream":
            raise exc_factory()
        return await original(self)

    monkeypatch.setattr(httpx.Response, "aclose", patched)


@pytest.mark.parametrize(
    "exc_factory,exc_type",
    [
        (lambda: RuntimeError("aclose boom"), RuntimeError),
        (lambda: asyncio.CancelledError(), asyncio.CancelledError),
    ],
)
async def test_aclose_failure_still_emits_close_row(
    capture_logger, monkeypatch, exc_factory, exc_type
):
    _boom_aclose(monkeypatch, exc_factory)
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(_sse_upstream()),
        base_url="http://upstream",
    )
    app = _build_app(upstream, ledger=None)

    # The aclose failure surfaces through the ASGI stack — but the close
    # row must already have been written (open/close stay paired).
    with pytest.raises(exc_type):
        await _get_plain(app, "/event")

    lifecycle = _lifecycle_rows(_rows(capture_logger))
    assert len(lifecycle) == 2
    open_row = next(r for r in lifecycle if r["recordType"] == "sse_open")
    close_row = next(r for r in lifecycle if r["recordType"] == "sse_close")
    assert open_row["lifecycleId"] == close_row["lifecycleId"]


async def test_client_cancel_midstream_still_emits_close_row(capture_logger):
    """True client disconnect: the drain task is cancelled while the
    upstream stream is live — the finally must still write the close row."""

    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            while True:
                yield b"data: {\"t\":1}\n\n"
                await asyncio.sleep(0.01)

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "text/event-stream"},
        )

    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream",
    )
    app = _build_app(upstream, ledger=None)

    async def _drain() -> None:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/event")

    task = asyncio.create_task(_drain())
    # Let the stream establish and flow a few frames.
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    lifecycle = _lifecycle_rows(_rows(capture_logger))
    assert len(lifecycle) == 2
    open_row = next(r for r in lifecycle if r["recordType"] == "sse_open")
    close_row = next(r for r in lifecycle if r["recordType"] == "sse_close")
    assert open_row["lifecycleId"] == close_row["lifecycleId"]
