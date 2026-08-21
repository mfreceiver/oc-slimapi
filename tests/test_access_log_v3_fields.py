"""v3-contract §9.1 — access-log observability fields (Batch A).

Covers: wireVersion / selectorResult / directoryForm / recordType /
lifecycleId on request rows; sse_open + sse_close rows for the events and
token-stream endpoints (lifecycle pairing); legacy-shape regression (old
ocdroid form byte-identical modulo additive tail fields).
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.routes import health, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.sse_observability import next_lifecycle_id, sse_close, sse_open
from oc_slimapi.traffic import TrafficLedger

V2_HEADER = {"X-Slimapi-Version": "2"}

# ---------------------------------------------------------------------------
# capture logger fixture
# ---------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def capture_logger():
    logger = logging.getLogger("oc_slimapi.test.capture")
    logger.disabled = False
    logger.setLevel(logging.INFO)  # INFO rows are the payload — must pass level
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    yield logger
    logger.removeHandler(handler)


def _rows(logger) -> list[dict]:
    return [json.loads(line) for line in logger.handlers[0].lines if line]


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


def _build_app(logger, *, ledger: TrafficLedger | None = None) -> FastAPI:
    """Stack order mirrors production: Traffic(outer) → Selector → routes."""
    app = FastAPI(title="access-v3-test")

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    if ledger is not None:
        app.state.traffic_ledger = ledger
    # Add in production order: selector first (inner), traffic last (outer).
    app.add_middleware(SlimapiSelectorMiddleware)
    app.add_middleware(TrafficAccountingMiddleware, logger=logger)
    app.include_router(health.router)
    app.include_router(versions.router)
    register_error_handlers(app)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test")


# ---------------------------------------------------------------------------
# request rows: selectorResult × wireVersion × directoryForm × recordType
# ---------------------------------------------------------------------------

async def test_row_no_v_rejected(capture_logger):
    """Terminal: no selector at all → 400, row records rejected/null."""
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/health", headers=V2_HEADER)
        assert r.status_code == 400  # header not read; retired-version request
    rows = _rows(capture_logger)
    assert len(rows) == 1
    row = rows[0]
    assert row["selectorResult"] == "rejected"
    assert row["wireVersion"] is None
    assert row["directoryForm"] is None  # health is not a directory consumer
    assert row["recordType"] == "request"
    assert row["lifecycleId"] is None


async def test_row_v2_explicit_rejected(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/health?v=2", headers=V2_HEADER)
        assert r.status_code == 400
    row = _rows(capture_logger)[0]
    assert row["selectorResult"] == "rejected"
    assert row["wireVersion"] is None


async def test_row_v4(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/health?v=4")
        assert r.status_code == 200
    row = _rows(capture_logger)[0]
    assert row["selectorResult"] == "v4"
    assert row["wireVersion"] == "4"


async def test_row_rejected(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/health?v=9")
        assert r.status_code == 400
    row = _rows(capture_logger)[0]
    assert row["selectorResult"] == "rejected"
    assert row["wireVersion"] is None


async def test_row_exempt(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/versions")
        assert r.status_code == 200
    row = _rows(capture_logger)[0]
    assert row["selectorResult"] == "exempt"
    assert row["wireVersion"] is None


async def test_row_not_applicable_for_catch_all(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/plain?v=3")
        assert r.status_code == 200
    row = _rows(capture_logger)[0]
    assert row["selectorResult"] == "not_applicable"
    assert row["wireVersion"] is None
    assert row["directoryForm"] is None


# ---------------------------------------------------------------------------
# directoryForm on consuming vs non-consuming routes (gate 400 keeps the row)
# ---------------------------------------------------------------------------

async def test_directory_form_query(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        # No version header → gate 400, but the selector already stashed the
        # directoryForm from the query.
        r = await client.get("/slimapi/sessions?directory=/proj")
        assert r.status_code == 400
    row = _rows(capture_logger)[0]
    assert row["directoryForm"] == "query"


async def test_directory_form_header(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/messages/ses_1",
            headers={"X-Opencode-Directory": "/proj"},
        )
        assert r.status_code == 400
    row = _rows(capture_logger)[0]
    assert row["directoryForm"] == "header"


async def test_directory_form_both(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get(
            "/slimapi/messages/ses_1?directory=/proj",
            headers={"X-Opencode-Directory": "/proj"},
        )
        assert r.status_code == 400
    row = _rows(capture_logger)[0]
    assert row["directoryForm"] == "both"


async def test_directory_form_absent_on_consuming_route(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/agent")
        assert r.status_code == 400
    row = _rows(capture_logger)[0]
    assert row["directoryForm"] == "absent"


async def test_directory_form_null_on_non_consuming_route(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        # v=4 admitted; health is tolerant — the header form is ignored
        # (not an error), directoryForm stays None (non-consuming route).
        r = await client.get(
            "/slimapi/health?directory=/proj&v=4",
            headers={"X-Opencode-Directory": "/proj"},
        )
        assert r.status_code == 200
    row = _rows(capture_logger)[0]
    assert row["directoryForm"] is None


# ---------------------------------------------------------------------------
# legacy-shape regression: old row prefix unchanged (additive tail only)
# ---------------------------------------------------------------------------

async def test_legacy_row_key_prefix_preserved(capture_logger):
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/health?v=4")
        assert r.status_code == 200
    row = _rows(capture_logger)[0]
    assert list(row.keys())[:14] == [
        "ts", "method", "path", "bucket", "status", "durationMs",
        "downIn", "downOut", "upIn", "upOut", "requestId",
        "client", "clientVer", "clientId",
    ]
    # New additive fields trail the legacy set.
    for key in ("wireVersion", "selectorResult", "directoryForm", "recordType", "lifecycleId"):
        assert key in row


async def test_legacy_old_ocdroid_form_rejected(capture_logger):
    """Old ocdroid form (no `v` + header 2): terminal outcome is the version
    retirement 400 — the endpoint exists, the protocol version does not."""
    app = _build_app(capture_logger)
    async with _client(app) as client:
        r = await client.get("/slimapi/health", headers=V2_HEADER)
        assert r.status_code == 400
        assert r.json() == {"code": "unsupported_version", "supported": [4]}


# ---------------------------------------------------------------------------
# SSE lifecycle rows via the events endpoint (integration)
# ---------------------------------------------------------------------------


class _FakeSubscriber:
    def __init__(self) -> None:
        self.id = "sub_test"
        self.queue: asyncio.Queue = asyncio.Queue()

    def ack(self, item: bytes) -> None:  # pragma: no cover - not exercised
        pass


class _FakeHubs:
    def __init__(self) -> None:
        self.sub = _FakeSubscriber()

    def subscribe(self, wire_v4: bool = False) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass


async def test_events_sse_open_close_rows(capture_logger, monkeypatch):
    from oc_slimapi import sse_observability
    from oc_slimapi.routes import events as events_routes
    from oc_slimapi.sse.hub import STOP

    # Route the SSE lifecycle rows into the capture logger instead of the
    # production access-log singleton.
    monkeypatch.setattr(sse_observability, "_access_logger", lambda: capture_logger)

    app = _build_app(capture_logger)
    app.include_router(events_routes.router)
    hubs = _FakeHubs()
    # One real frame, then the STOP sentinel — the generator drains the
    # frame, yields it, sees STOP and exits CLEANLY (finally → sse_close).
    # Deterministic: no reliance on client-disconnect propagation through
    # ASGITransport (which is timing-dependent under pytest-asyncio).
    hubs.sub.queue.put_nowait(b"event: server.heartbeat\ndata: {}\n\n")
    hubs.sub.queue.put_nowait(STOP)
    app.state.hubs = hubs
    app.state.token_registry = None

    async with _client(app) as client:
        async with client.stream(
            "GET", "/slimapi/events?v=4",
        ) as response:
            assert response.status_code == 200
            # Read the whole (finite) stream.
            async for _ in response.aiter_bytes():
                pass

    rows = _rows(capture_logger)
    sse_rows = [r for r in rows if r.get("recordType") in ("sse_open", "sse_close")]
    assert len(sse_rows) == 2
    open_row, close_row = sse_rows
    assert open_row["recordType"] == "sse_open"
    assert close_row["recordType"] == "sse_close"
    # Same lifecycleId on open/close (process-monotonic pairing).
    assert open_row["lifecycleId"] == close_row["lifecycleId"]
    assert isinstance(open_row["lifecycleId"], int)
    # Selector context propagated to the lifecycle rows.
    assert open_row["selectorResult"] == "v4"
    assert open_row["wireVersion"] == "4"
    assert open_row["bucket"] == "events_sse"
    # The request row for the same connection is still exactly one.
    req_rows = [r for r in rows if r.get("recordType") == "request"]
    assert len(req_rows) == 1
    assert req_rows[0]["selectorResult"] == "v4"


async def test_sse_helpers_no_selector_scope_defaults_absent(capture_logger, monkeypatch):
    """A scope without selector state (legacy stack) → SSE dims = absent."""
    from oc_slimapi import sse_observability

    monkeypatch.setattr(sse_observability, "_access_logger", lambda: capture_logger)

    ledger = TrafficLedger(enabled=True)
    holder = FastAPI()
    holder.state.traffic_ledger = ledger
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/slimapi/events",
        "state": {},
        "headers": [],
        "app": holder,
    }

    lid = sse_open(scope, bucket="events_sse")
    assert isinstance(lid, int)
    sse_close(scope, bucket="events_sse", lifecycle_id=lid)

    snap = ledger.snapshot()
    v3 = snap["v3"]
    assert v3["sseActive"]["absent"] == 0
    assert v3["sseLifecycle"]["absent"]["opens"] == 1
    assert v3["sseLifecycle"]["absent"]["closes"] == 1
    # Both lifecycle rows landed in the capture logger with the same id.
    rows = [r for r in _rows(capture_logger) if r.get("recordType", "request") != "request"]
    assert len(rows) == 2
    assert rows[0]["lifecycleId"] == rows[1]["lifecycleId"] == lid
    assert rows[0]["recordType"] == "sse_open"
    assert rows[1]["recordType"] == "sse_close"
    # Honest row: no selector ran → selectorResult null on the row; the
    # sseActive LEDGER dim (§9.2) is what maps to "absent".
    assert rows[0]["selectorResult"] is None


def test_lifecycle_ids_monotonic():
    ids = [next_lifecycle_id() for _ in range(5)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5
