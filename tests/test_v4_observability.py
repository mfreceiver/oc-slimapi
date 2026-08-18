"""B3a-A4 — v4 observability extension (design-v4-selector §4, v4 §9.1).

Scope: the selectorResult / wireVersion dimension VALUE SETS widen with
"v4"/"4" across access log, traffic ledger matrix, snapshot aggregation
and the SSE active dims. The compatibility iron-rule: shapes stay
identical — old rows / snapshots remain interpretable (new values only
appear for new requests). DB-auxiliary metrics are NOT here (B5 lane).
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.routes import health
from oc_slimapi.selector import (
    SELECTOR_STATE_KEY,
    SELECTOR_V4,
    SSE_RESULT_DIMS,
    SlimapiSelectorMiddleware,
)
from oc_slimapi.sse_observability import sse_close, sse_open
from oc_slimapi.traffic import TrafficLedger
from oc_slimapi.traffic_snapshot import _SSE_DIMS, aggregate_v3_observability

IDENTITY = {"Accept-Encoding": "identity"}
DIRECTORY_HEADER = {"X-Opencode-Directory": "/w"}


# ---------------------------------------------------------------------------
# capture logger fixture (mirrors test_access_log_v3_fields)
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def capture_logger():
    logger = logging.getLogger("oc_slimapi.test.capture4")
    logger.disabled = False
    logger.setLevel(logging.INFO)
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
    app = FastAPI(title="v4-observability-test")
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    if ledger is not None:
        app.state.traffic_ledger = ledger
    app.add_middleware(SlimapiSelectorMiddleware)
    app.add_middleware(TrafficAccountingMiddleware, logger=logger)
    app.include_router(health.router)
    register_error_handlers(app)
    return app


# ---------------------------------------------------------------------------
# dimension constants
# ---------------------------------------------------------------------------

def test_sse_dims_widened_and_in_sync():
    """Both copies of the sseActive dim value set carry "v4" — and nothing
    else changed (the old four survive, ordering is stable)."""
    assert SSE_RESULT_DIMS == ("v2", "v3", "v4", "absent", "not_applicable")
    assert _SSE_DIMS == ("v2", "v3", "v4", "absent", "not_applicable")
    assert SSE_RESULT_DIMS == _SSE_DIMS


def test_selector_v4_enum_flows_to_dim_normalization():
    """The selector's SELECTOR_V4 result string is a member of the SSE dim
    set — a v4-admitted SSE request counts under "v4", never mis-bucketed
    to "absent"."""
    assert SELECTOR_V4 in SSE_RESULT_DIMS


# ---------------------------------------------------------------------------
# access log rows
# ---------------------------------------------------------------------------

async def test_access_log_row_v4_request(capture_logger):
    app = _build_app(capture_logger)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/slimapi/health", params={"v": "4"}, headers=IDENTITY)
    assert r.status_code == 200
    rows = [r_ for r_ in _rows(capture_logger) if r_.get("recordType", "request") == "request"]
    assert len(rows) == 1
    assert rows[0]["selectorResult"] == "v4"
    assert rows[0]["wireVersion"] == "4"
    # health is a non-consuming route (§5.3 static table) → directoryForm
    # null, not "absent" — same as the pre-widening v3 rows.
    assert rows[0]["directoryForm"] is None


async def test_access_log_row_v3_request_unchanged(capture_logger):
    app = _build_app(capture_logger)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/slimapi/health", params={"v": "3"}, headers=IDENTITY)
    assert r.status_code == 200
    rows = [r_ for r_ in _rows(capture_logger) if r_.get("recordType", "request") == "request"]
    assert len(rows) == 1
    assert rows[0]["selectorResult"] == "v3"
    assert rows[0]["wireVersion"] == "3"


async def test_access_log_row_rejected_v5_null_wire(capture_logger):
    """rejected keeps null wireVersion — the widening adds a value only for
    admitted v4, nothing moves for the rejection family."""
    app = _build_app(capture_logger)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/slimapi/health", params={"v": "5"}, headers=IDENTITY)
    assert r.status_code == 400
    rows = [r_ for r_ in _rows(capture_logger) if r_.get("recordType", "request") == "request"]
    assert len(rows) == 1
    assert rows[0]["selectorResult"] == "rejected"
    assert rows[0]["wireVersion"] is None


async def test_access_log_row_v4_directory_retired_counts_4xx(capture_logger):
    """A v4 × directory 400 (selector-intercepted) still lands as a normal
    request row: v4 dims + 4xx status class — no special-casing."""
    app = FastAPI(title="v4-retired-row")
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.add_middleware(SlimapiSelectorMiddleware)
    app.add_middleware(TrafficAccountingMiddleware, logger=capture_logger)

    @app.get("/slimapi/sessions")
    async def sessions():
        return {"ok": True}

    register_error_handlers(app)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/slimapi/sessions",
            params={"v": "4", "directory": "/w"},
            headers=IDENTITY,
        )
    assert r.status_code == 400
    assert r.json()["code"] == "directory_retired_in_v4"
    rows = [r_ for r_ in _rows(capture_logger) if r_.get("recordType", "request") == "request"]
    assert len(rows) == 1
    # Pre-existing semantic (unchanged from v3): a directory-family selector
    # 400 re-stashes as "rejected" with null wire — the retirement error is
    # no exception; the v3 codes (invalid_directory_selector …) behave
    # identically.
    assert rows[0]["selectorResult"] == "rejected"
    assert rows[0]["wireVersion"] is None
    assert 400 <= rows[0]["status"] < 500


# ---------------------------------------------------------------------------
# traffic ledger matrix
# ---------------------------------------------------------------------------

async def test_ledger_matrix_v4_key(capture_logger):
    ledger = TrafficLedger(enabled=True)
    app = _build_app(capture_logger, ledger=ledger)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        await c.get("/slimapi/health", params={"v": "4"}, headers=IDENTITY)
        await c.get("/slimapi/health", params={"v": "3"}, headers=IDENTITY)
    matrix = ledger.snapshot()["v3"]["matrix"]
    assert any(k.startswith("v4|4|") for k in matrix)
    assert any(k.startswith("v3|3|") for k in matrix)
    # Flat-key arity unchanged: 6 segments (schema shape frozen).
    for key in matrix:
        assert len(key.split("|")) == 6


# ---------------------------------------------------------------------------
# snapshot aggregation
# ---------------------------------------------------------------------------

def _row(ts, **extra):
    row = {
        "ts": ts,
        "bucket": "health",
        "status": 200,
        "recordType": "request",
        "selectorResult": "v4",
        "wireVersion": "4",
        "directoryForm": "absent",
    }
    row.update(extra)
    return row


def test_aggregate_v4_rows():
    out = aggregate_v3_observability([
        _row("2026-08-17T10:00:00+08:00"),
        _row("2026-08-17T10:00:01+08:00", selectorResult="v3", wireVersion="3"),
    ])
    assert out["counts"]["v4|4|absent|request|2xx|health"] == 1
    assert out["counts"]["v3|3|absent|request|2xx|health"] == 1
    # jq-friendly stability widened: every day map now carries FIVE dims.
    assert set(out["sseActive"]["2026-08-17"]) == {
        "v2", "v3", "v4", "absent", "not_applicable",
    }
    assert out["sseActive"]["2026-08-17"]["v4"] == 0


def test_aggregate_sse_v4_dim_and_unknown_fallback():
    rows = [
        _row("2026-08-17T10:00:00+08:00", recordType="sse_open",
             selectorResult="v4", bucket="events_sse"),
        # unknown dim strings still normalize to "absent" (fail-safe).
        _row("2026-08-17T10:00:01+08:00", recordType="sse_open",
             selectorResult="v9", bucket="events_sse"),
    ]
    out = aggregate_v3_observability(rows)
    assert out["sseLive"]["v4"] == 1
    assert out["sseOpens"]["2026-08-17"]["v4"] == 1
    assert out["sseOpens"]["2026-08-17"]["absent"] == 1


def test_aggregate_legacy_rows_without_tail_fields():
    """Iron compatibility rule: rows from BEFORE the upgrade (no v4 fields
    at all) still aggregate into the null dims — zero breakage."""
    out = aggregate_v3_observability([
        {"ts": "2026-08-17T10:00:00+08:00", "bucket": "health", "status": 200},
    ])
    assert out["counts"]["null|null|null|request|2xx|health"] == 1


# ---------------------------------------------------------------------------
# SSE lifecycle via selector stash (v4 scope)
# ---------------------------------------------------------------------------

async def test_sse_lifecycle_v4_scope(capture_logger, monkeypatch):
    from oc_slimapi import sse_observability
    monkeypatch.setattr(sse_observability, "_access_logger", lambda: capture_logger)

    ledger = TrafficLedger(enabled=True)
    holder = FastAPI()
    holder.state.traffic_ledger = ledger
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/slimapi/events",
        "state": {SELECTOR_STATE_KEY: {"result": SELECTOR_V4, "wire": "4"}},
        "headers": [],
        "app": holder,
    }
    lid = sse_open(scope, bucket="events_sse")
    sse_close(scope, bucket="events_sse", lifecycle_id=lid)

    snap = ledger.snapshot()["v3"]
    # The "v4" ledger dim exists and paired open/close nets to zero.
    assert snap["sseActive"]["v4"] == 0
    assert snap["sseLifecycle"]["v4"]["opens"] == 1
    assert snap["sseLifecycle"]["v4"]["closes"] == 1
    rows = [r for r in _rows(capture_logger) if r.get("recordType") != "request"]
    assert rows[0]["selectorResult"] == "v4"
    assert rows[0]["wireVersion"] == "4"


async def test_sse_lifecycle_v3_scope_unchanged(capture_logger, monkeypatch):
    from oc_slimapi import sse_observability
    monkeypatch.setattr(sse_observability, "_access_logger", lambda: capture_logger)

    ledger = TrafficLedger(enabled=True)
    holder = FastAPI()
    holder.state.traffic_ledger = ledger
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/slimapi/events",
        "state": {SELECTOR_STATE_KEY: {"result": "v3", "wire": "3"}},
        "headers": [],
        "app": holder,
    }
    lid = sse_open(scope, bucket="events_sse")
    snap = ledger.snapshot()["v3"]
    assert snap["sseActive"]["v3"] == 1  # open, not yet closed
    sse_close(scope, bucket="events_sse", lifecycle_id=lid)
    snap = ledger.snapshot()["v3"]
    assert snap["sseActive"]["v3"] == 0
    rows = [r for r in _rows(capture_logger) if r.get("recordType") != "request"]
    assert rows[0]["selectorResult"] == "v3"
