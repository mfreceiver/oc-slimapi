"""v3-contract §8.2 terminal — the catch-all SSE surface (/event,
/global/event) is CLOSED.

The retired forwarder's catch-all SSE observability (sse_open/sse_close
rows on the passthrough bucket, B2/B4/B5 semantics) is unreachable by
construction: /event and /global/event now 404 thin_route_not_found and
the upstream is never contacted. These tests pin the terminal behaviour:

* closed SSE paths → 404 + exactly one plain request row (no lifecycle
  rows, no sseActive movement on any dim);
* the SSE lifecycle rows themselves (open/close pairing, shared
  lifecycleId, dims) live on the surviving /slimapi SSE endpoints —
  covered in tests/test_access_log_v3_fields.py and test_traffic_*.
"""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.proxy import install_proxy
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
    from oc_slimapi.middleware import traffic_accounting as ta
    monkeypatch.setattr(ta, "get_access_logger", lambda: logger)
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


def _build_app(upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-proxy-sse-terminal-test")
    app.state.config = _settings()
    app.state.upstream = upstream
    ledger = TrafficLedger()
    app.state.traffic_ledger = ledger
    install_proxy(app)
    from oc_slimapi.selector import SlimapiSelectorMiddleware
    app.add_middleware(SlimapiSelectorMiddleware)
    app.add_middleware(TrafficAccountingMiddleware)
    return app, ledger


def _never_upstream():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("closed surface must never reach the upstream")
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("path", ["/event", "/global/event"])
async def test_closed_sse_paths_404_one_request_row(capture_logger, path):
    """§8.2 3.0.0: the retired SSE passthrough paths 404 — exactly one
    plain request row, no sse_open/sse_close lifecycle rows."""
    app, _ledger = _build_app(httpx.AsyncClient(transport=_never_upstream()))
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            path, headers={"Accept-Encoding": "identity"})
    assert response.status_code == 404
    assert response.json() == {"code": "thin_route_not_found"}

    rows = _rows(capture_logger)
    assert len(rows) == 1, f"expected exactly the request row, got {rows}"
    row = rows[0]
    assert row["recordType"] == "request"
    assert row["status"] == 404
    assert row["bucket"] == "passthrough"
    assert row["selectorResult"] == "not_applicable"
    assert row["wireVersion"] is None


@pytest.mark.parametrize("path", ["/event", "/global/event"])
async def test_closed_sse_paths_leave_sse_active_zero(capture_logger, path):
    """No lifecycle rows → no sseActive movement on any dim."""
    app, ledger = _build_app(httpx.AsyncClient(transport=_never_upstream()))
    transport = httpx.ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(path, headers={"Accept-Encoding": "identity"})
    snap = ledger.snapshot()
    v3 = snap.get("v3", {})
    assert v3.get("sseActive", {}) in ({}, {
        "v2": 0, "v3": 0, "absent": 0, "not_applicable": 0,
    })
