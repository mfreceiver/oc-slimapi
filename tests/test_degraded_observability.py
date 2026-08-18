"""v4 sessions 降级观测契约（rev-gate BLOCKER-4；v4-contract §9.1/§9.2）。

消费侧全链路：access log 稀疏字段（sessionsSource / degraded503）→ ledger
flat matrix（degraded|kind|statusClass|bucket）→ snapshot 日图谱聚合 →
/slimapi/metrics sessionsDegraded per-response 计数。

核心断言（评审要求）：per-response 计数 vs dbaux 状态机事件计数——一次
disable 期间 3 次 503 → fail_closed_503 == 3 而非 1。

四类触发（真实路径：真实 sessions 路由 + _StubAux/fixture DB + 真实
selector/traffic 中间件栈）：

* DB 200（source=db，无 degraded）
* Class A 降级 200（source=http）
* 503 fail-closed ×（allowlist 非空 × disabled；Class B × disabled/tripped）

R1 并行态说明：写入侧（routes/sessions.py `_sessions_v4` 设
request.state 标记）合入前，用 ``_MarkerSimulatorMiddleware`` 按接口约定
的键名/值域在 response start 时写同样的标记（纯 ASGI wrapper，模拟 R1
矩阵）；R1 合入后两路写入同键同值（幂等覆盖），本文件无需改动即绿。
"""
from __future__ import annotations

import json
import logging
from datetime import date

import httpx
import orjson
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.dbaux import AuxiliaryUnavailableError, DbAuxiliarySource
from oc_slimapi.dbaux.lifecycle import DbAuxStatus
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, metrics, sessions
from oc_slimapi.selector import SELECTOR_STATE_KEY, SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.traffic import (
    DEGRADED_503_STATE_KEY,
    SESSIONS_DEGRADED_STATE_ATTR,
    SESSIONS_SOURCE_STATE_KEY,
    TrafficLedger,
    ensure_sessions_degraded_counters,
)
from oc_slimapi.traffic_snapshot import (
    TrafficSnapshotter,
    aggregate_v3_observability,
)

from v4_fixture import build_fixture_db

IDENTITY = {"Accept-Encoding": "identity"}
AL_NONEMPTY = ("/foo",)
HTTP_SESSIONS_BODY = orjson.dumps([
    {"id": "h1", "title": "up one", "directory": "/any",
     "time": {"created": 1, "updated": 2}},
    {"id": "h2", "title": "up two", "directory": "/any"},
])

HTTP_KEY = "degraded|http|2xx|sessions"
FAIL_KEY = "degraded|fail_closed|5xx|sessions"


# ---------------------------------------------------------------------------
# capture logger fixture（test_access_log_v3_fields 手法）
# ---------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def capture_logger():
    logger = logging.getLogger("oc_slimapi.test.degraded_capture")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    yield logger
    logger.removeHandler(handler)


def _rows(logger) -> list[dict]:
    return [json.loads(line) for line in logger.handlers[0].lines if line]


# ---------------------------------------------------------------------------
# R1 写入侧模拟（并行态；合入后幂等）
# ---------------------------------------------------------------------------


def _r1_like_policy(aux_available: bool):
    """接口约定的 R1 写入矩阵：仅 wire=4 的 /slimapi/sessions 请求写标记。

    200 → source = "db"（db 可用）/ "http"（Class A 降级）；
    5xx → degraded_503 = True；其余（v3 / 其他路由 / 4xx）不写。
    """

    def policy(scope: dict, status: int) -> dict:
        state = scope.get("state") or {}
        sel = state.get(SELECTOR_STATE_KEY)
        if not isinstance(sel, dict) or sel.get("wire") != "4":
            return {}
        if scope.get("path") != "/slimapi/sessions":
            return {}
        if status == 200:
            return {SESSIONS_SOURCE_STATE_KEY: "db" if aux_available else "http"}
        if 500 <= status < 600:
            return {DEGRADED_503_STATE_KEY: True}
        return {}

    return policy


class _MarkerSimulatorMiddleware:
    """在 response start 时把策略标记写进 scope state（模拟 R1）。"""

    def __init__(self, app, *, policy) -> None:
        self.app = app
        self._policy = policy

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                markers = self._policy(scope, int(message.get("status") or 0))
                if markers:
                    scope.setdefault("state", {}).update(markers)
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ---------------------------------------------------------------------------
# app builder（test_sessions_v4_matrix 的 _StubAux/_build_app 形状 + 观测面）
# ---------------------------------------------------------------------------


class _StubAux:
    """db 不可用态的路由可见面（disabled / tripped 同形）。

    带 B5 metrics 路由所需的最小 ``snapshot()`` 形状。
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=False, mode="http", reason=self._reason)

    async def query(self, sql, params=()):  # pragma: no cover - never reached
        raise AuxiliaryUnavailableError(self._reason)

    def snapshot(self) -> dict:
        return {
            "available": False,
            "mode": "http",
            "reason": self._reason,
            "generation": 0,
            "breaker": {"open": False, "samples": 0, "total": 0,
                        "p50_ms": None, "p99_ms": None},
            "counters": {},
            "source": None,
        }


class _FakeHubs:
    """metrics 路由所需的最小 hubs 可见面。"""

    def snapshot_metrics(self):
        return {"sse": {}}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    aux,
    *,
    logger: logging.Logger | None = None,
    settings: Settings | None = None,
    ledger: TrafficLedger | None = None,
    with_metrics: bool = False,
    aux_available: bool = False,
):
    app = FastAPI()
    app.state.config = settings or _settings()
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=HTTP_SESSIONS_BODY,
                headers={"Content-Type": "application/json"},
            )
        ),
        base_url=app.state.config.upstream,
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.dbaux = aux
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=app.state.config.max_transforms,
        transform_wait_seconds=app.state.config.transform_wait_seconds,
        max_response_bytes=app.state.config.max_response_bytes,
    ))
    if ledger is not None:
        app.state.traffic_ledger = ledger
    if with_metrics:
        app.state.hubs = _FakeHubs()
    # 生产栈序：Traffic(最外) → marker 模拟 → Selector → 路由。
    app.add_middleware(SlimapiSelectorMiddleware)
    app.add_middleware(
        _MarkerSimulatorMiddleware, policy=_r1_like_policy(aux_available)
    )
    if logger is not None:
        app.add_middleware(TrafficAccountingMiddleware, logger=logger)
    else:
        app.add_middleware(TrafficAccountingMiddleware)
    routers = [health.router, sessions.router]
    if with_metrics:
        routers.append(metrics.router)
    for router in routers:
        app.include_router(router)
    register_error_handlers(app)
    install_proxy(app)
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _real_aux(tmp_path):
    db = build_fixture_db(tmp_path / "m.db")
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available
    return source


def _counters(app) -> dict[str, int]:
    return ensure_sessions_degraded_counters(app.state).snapshot()


# ---------------------------------------------------------------------------
# 四类触发：access log 行 + ledger matrix + counters
# ---------------------------------------------------------------------------


async def test_db_200_source_db_no_degraded(tmp_path, capture_logger):
    aux = await _real_aux(tmp_path)
    ledger = TrafficLedger()
    app = _build_app(aux, logger=capture_logger, ledger=ledger, aux_available=True)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4"}, headers=IDENTITY)
        assert resp.status_code == 200
        assert "degraded" not in resp.json()
    finally:
        await aux.stop()
    row = _rows(capture_logger)[-1]
    assert row["sessionsSource"] == "db"
    assert "degraded503" not in row
    # 稀疏键：无降级响应 → snapshot 不带 "v4"（zero-knowledge additive）。
    assert "v4" not in ledger.snapshot()
    assert _counters(app) == {"degraded_200": 0, "fail_closed_503": 0}


async def test_class_a_degraded_200_http(capture_logger):
    ledger = TrafficLedger()
    app = _build_app(_StubAux("disabled"), logger=capture_logger, ledger=ledger)
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.json()["degraded"] is True
    row = _rows(capture_logger)[-1]
    assert row["sessionsSource"] == "http"
    assert "degraded503" not in row
    matrix = ledger.snapshot()["v4"]["degradedMatrix"]
    assert matrix.get(HTTP_KEY) == 1
    assert FAIL_KEY not in matrix
    assert _counters(app) == {"degraded_200": 1, "fail_closed_503": 0}


async def test_fail_closed_503_allowlist_nonempty(capture_logger):
    ledger = TrafficLedger()
    app = _build_app(
        _StubAux("disabled"), logger=capture_logger, ledger=ledger,
        settings=_settings(directory_allowlist=list(AL_NONEMPTY)),
    )
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 503
    row = _rows(capture_logger)[-1]
    assert row["degraded503"] is True
    assert "sessionsSource" not in row  # 503 无 body 来源语义
    matrix = ledger.snapshot()["v4"]["degradedMatrix"]
    assert matrix.get(FAIL_KEY) == 1
    assert HTTP_KEY not in matrix
    assert _counters(app) == {"degraded_200": 0, "fail_closed_503": 1}


@pytest.mark.parametrize("reason", ["disabled", "circuit_open"])
async def test_fail_closed_503_class_b(reason, capture_logger):
    """Class B（parent=only 无法 HTTP 投影）× db 不可用两因 → 503 计数。"""
    ledger = TrafficLedger()
    app = _build_app(_StubAux(reason), logger=capture_logger, ledger=ledger)
    async with _client(app) as client:
        resp = await client.get(
            "/slimapi/sessions",
            params={"v": "4", "parent": "only"}, headers=IDENTITY)
    assert resp.status_code == 503
    row = _rows(capture_logger)[-1]
    assert row["degraded503"] is True
    assert ledger.snapshot()["v4"]["degradedMatrix"].get(FAIL_KEY) == 1
    assert _counters(app)["fail_closed_503"] == 1


# ---------------------------------------------------------------------------
# per-response vs 状态机事件（评审核心要求）
# ---------------------------------------------------------------------------


async def test_three_503s_count_three_not_one(capture_logger):
    """一次 disable 期间 3 次 503 → 计数 3（disable 事件只有 1 个）。"""
    ledger = TrafficLedger()
    app = _build_app(_StubAux("disabled"), logger=capture_logger, ledger=ledger)
    async with _client(app) as client:
        for _ in range(3):
            # parent=only → Class B（HTTP 投影不可行）→ 503 fail-closed
            resp = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "parent": "only"}, headers=IDENTITY)
            assert resp.status_code == 503
    rows = [r for r in _rows(capture_logger) if r["bucket"] == "sessions"]
    assert len(rows) == 3
    assert all(r["degraded503"] is True for r in rows)
    assert ledger.snapshot()["v4"]["degradedMatrix"][FAIL_KEY] == 3
    assert _counters(app)["fail_closed_503"] == 3


async def test_mixed_degradation_counters_split(capture_logger):
    """2× http 降级 200 + 3× 503 → 两计数器分别 2 / 3，互不串扰。"""
    ledger = TrafficLedger()
    app = _build_app(_StubAux("disabled"), logger=capture_logger, ledger=ledger)
    async with _client(app) as client:
        for _ in range(2):  # Class A（缺省 archived=omit × parent=all）→ 200
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4"}, headers=IDENTITY)
            assert resp.status_code == 200
        for _ in range(3):  # Class B（parent=only）→ 503
            resp = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "parent": "only"}, headers=IDENTITY)
            assert resp.status_code == 503
    assert _counters(app) == {"degraded_200": 2, "fail_closed_503": 3}
    matrix = ledger.snapshot()["v4"]["degradedMatrix"]
    assert matrix[HTTP_KEY] == 2
    assert matrix[FAIL_KEY] == 3


# ---------------------------------------------------------------------------
# 回归：v3 路径 / 其他路由 / 畸形标记值
# ---------------------------------------------------------------------------


async def test_v3_request_fields_absent(capture_logger):
    ledger = TrafficLedger()
    app = _build_app(_StubAux("disabled"), logger=capture_logger, ledger=ledger)
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "3"}, headers=IDENTITY)
    assert resp.status_code == 200
    row = _rows(capture_logger)[-1]
    assert row["wireVersion"] == "3"
    assert "sessionsSource" not in row
    assert "degraded503" not in row
    assert "v4" not in ledger.snapshot()
    assert _counters(app) == {"degraded_200": 0, "fail_closed_503": 0}


async def test_other_route_fields_absent(capture_logger):
    ledger = TrafficLedger()
    app = _build_app(_StubAux("disabled"), logger=capture_logger, ledger=ledger)
    async with _client(app) as client:
        resp = await client.get("/slimapi/health",
                                params={"v": "3"}, headers=IDENTITY)
    assert resp.status_code == 200
    row = _rows(capture_logger)[-1]
    assert row["bucket"] == "health"
    assert "sessionsSource" not in row
    assert "degraded503" not in row
    assert _counters(app) == {"degraded_200": 0, "fail_closed_503": 0}


async def test_garbage_marker_values_ignored(capture_logger):
    """畸形 state 值（非冻结值集 / 非 bool True）不进日志不进计数。"""

    def _garbage_policy(scope, status):
        return {
            SESSIONS_SOURCE_STATE_KEY: "garbage",
            DEGRADED_503_STATE_KEY: "yes",
        }

    app = FastAPI()
    app.state.config = _settings()

    @app.get("/slimapi/sessions")
    async def marker_route(request: Request):
        request.state.slimapi_sessions_source = "garbage"
        request.state.slimapi_degraded_503 = "yes"
        return {"ok": True}

    app.add_middleware(
        _MarkerSimulatorMiddleware, policy=_garbage_policy
    )
    app.add_middleware(TrafficAccountingMiddleware, logger=capture_logger)
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", headers=IDENTITY)
    assert resp.status_code == 200
    row = _rows(capture_logger)[-1]
    assert "sessionsSource" not in row
    assert "degraded503" not in row
    assert _counters(app) == {"degraded_200": 0, "fail_closed_503": 0}


# ---------------------------------------------------------------------------
# /slimapi/metrics sessionsDegraded 块
# ---------------------------------------------------------------------------


async def test_metrics_sessions_degraded_block(capture_logger):
    app = _build_app(_StubAux("disabled"), logger=capture_logger,
                     with_metrics=True)
    async with _client(app) as client:
        for _ in range(2):
            await client.get("/slimapi/sessions",
                             params={"v": "4"}, headers=IDENTITY)
        for _ in range(1):
            await client.get(
                "/slimapi/sessions",
                params={"v": "4", "parent": "only"}, headers=IDENTITY)
        resp = await client.get("/slimapi/metrics",
                                params={"v": "3"}, headers=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessionsDegraded"] == {
        "degraded_200": 2, "fail_closed_503": 1}


async def test_metrics_zero_state_on_fresh_app():
    """zero-knowledge additive：首请求前无块；任一请求过中间件后零值块。"""
    app = _build_app(_StubAux("disabled"), with_metrics=True)
    async with _client(app) as client:
        # metrics handler 先于中间件 _record 执行 → 首个 GET 尚无块。
        resp = await client.get("/slimapi/metrics",
                                params={"v": "3"}, headers=IDENTITY)
        assert resp.status_code == 200
        assert "sessionsDegraded" not in resp.json()
        # 任一请求过后（此处为普通探活路由），零值块出现。
        resp = await client.get("/slimapi/health", params={"v": "3"},
                                headers=IDENTITY)
        assert resp.status_code == 200
        resp = await client.get("/slimapi/metrics",
                                params={"v": "3"}, headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.json()["sessionsDegraded"] == {
        "degraded_200": 0, "fail_closed_503": 0}


async def test_counters_work_without_ledger(capture_logger):
    """无 traffic_ledger（未接线/禁用）时 per-response 计数照常。"""
    app = _build_app(_StubAux("disabled"), logger=capture_logger)  # 无 ledger
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 200
    row = _rows(capture_logger)[-1]
    assert row["sessionsSource"] == "http"
    assert _counters(app)["degraded_200"] == 1


# ---------------------------------------------------------------------------
# snapshot 聚合 + 落盘载体
# ---------------------------------------------------------------------------


async def test_aggregation_degraded_daily(capture_logger):
    """access log 行 → degradedCounts / degradedCountsByDate（含零值 seed）。"""
    app = _build_app(_StubAux("disabled"), logger=capture_logger)
    async with _client(app) as client:
        await client.get("/slimapi/sessions",
                         params={"v": "4"}, headers=IDENTITY)  # http 200
        await client.get("/slimapi/sessions",
                         params={"v": "4", "parent": "only"},
                         headers=IDENTITY)  # 503
        await client.get("/slimapi/sessions",
                         params={"v": "4"}, headers=IDENTITY)  # http 200
        await client.get("/slimapi/sessions",
                         params={"v": "3"}, headers=IDENTITY)  # v3 回归
    result = aggregate_v3_observability(_rows(capture_logger))
    assert result["degradedCounts"] == {HTTP_KEY: 2, FAIL_KEY: 1}
    today = date.today().isoformat()
    per_day = result["degradedCountsByDate"][today]
    assert per_day[HTTP_KEY] == 2
    assert per_day[FAIL_KEY] == 1
    # _full_dims 类 seed：即使某天只有一种 kind，两种 canonical 键都在。
    assert set(per_day) == {HTTP_KEY, FAIL_KEY}


async def test_aggregation_seeds_clean_days():
    """无降级行的日期也带零值 canonical 键（jq 稳定形状）。"""
    rows = [
        {"ts": "2026-08-18T10:00:00", "bucket": "sessions", "status": 200,
         "sessionsSource": "db"},
        {"ts": "2026-08-19T10:00:00", "bucket": "sessions", "status": 200},
    ]
    result = aggregate_v3_observability(rows)
    assert result["degradedCounts"] == {}
    assert result["degradedCountsByDate"]["2026-08-18"] == {
        HTTP_KEY: 0, FAIL_KEY: 0}
    assert result["degradedCountsByDate"]["2026-08-19"] == {
        HTTP_KEY: 0, FAIL_KEY: 0}


async def test_snapshotter_carries_v4(tmp_path):
    """每日 traffic-snapshot JSONL 携带 v4.degradedMatrix（≥RETAIN_DAYS 载体）。"""
    ledger = TrafficLedger()
    ledger.record_sessions_degraded(kind="http", bucket="sessions", status=200)
    ledger.record_sessions_degraded(kind="fail_closed", bucket="sessions",
                                    status=503)
    snap = TrafficSnapshotter(
        ledger=ledger, interval_s=60,
        path=str(tmp_path / "traffic-snapshot.jsonl"),
    )
    assert snap._write_once()
    files = list(tmp_path.glob("traffic-snapshot-*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text().strip())
    assert line["v4"]["degradedMatrix"][HTTP_KEY] == 1
    assert line["v4"]["degradedMatrix"][FAIL_KEY] == 1
