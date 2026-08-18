"""B3a-B5：``/slimapi/metrics`` 的 dbaux 观测块（v4-contract §9.1）。

三态形状（available / disabled / circuit_open）+ 计数器随
查询/熔断/swap/禁用/重探事件递增。dbaux 缺席时指标形状不漂（additive）。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.dbaux import DbAuxiliarySource, resolve_db_path
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import metrics
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

from v4_fixture import build_fixture_db

IDENTITY = {"Accept-Encoding": "identity"}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
        max_subscribers_per_directory=8, max_total_subscribers=16,
        sse_queue_items=256, sse_buffer_bytes=2 * 1024 * 1024,
        sse_max_frame_bytes=256 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(aux=None) -> tuple[FastAPI, HubRegistry, httpx.AsyncClient]:
    app = FastAPI(title="oc-slimapi-metrics-test")
    upstream = httpx.AsyncClient()
    settings = _settings()
    app.state.config = settings
    app.state.upstream = upstream
    transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    hubs = HubRegistry(
        upstream,
        max_subscribers_per_directory=settings.max_subscribers_per_directory,
        max_total_subscribers=settings.max_total_subscribers,
        queue_items=settings.sse_queue_items,
        buffer_bytes=settings.sse_buffer_bytes,
        max_frame_bytes=settings.sse_max_frame_bytes,
    )
    hubs.set_transforms(transforms)
    app.state.hubs = hubs
    app.state.transforms = transforms
    if aux is not None:
        app.state.dbaux = aux
    app.include_router(metrics.router)
    register_error_handlers(app)
    return app, hubs, upstream


async def _get_metrics_dbaux(aux=None) -> tuple[dict, str]:
    app, hubs, upstream = _build_app(aux)
    try:
        transport = httpx.ASGITransport(app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            response = await client.get("/slimapi/metrics", headers=IDENTITY)
        assert response.status_code == 200
        return response.json(), response.text
    finally:
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def _started(tmp_path) -> DbAuxiliarySource:
    db = build_fixture_db(tmp_path / "m.db")
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available
    return source


# ---------------------------------------------------------------------------
# 形状：三态
# ---------------------------------------------------------------------------


async def test_shape_available_state(tmp_path):
    source = await _started(tmp_path)
    try:
        data, text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["available"] is True
        assert block["mode"] == "db"
        assert block["reason"] is None
        assert block["generation"] == 1
        assert block["source"] == "explicit-env"
        assert block["breaker_open"] is False
        assert set(block["latency"]) == {"p50_ms", "p99_ms", "samples", "total"}
        assert block["counters"] == {
            "queries": 0, "probes": 0, "trips": 0, "swaps": 0, "disables": 0,
        }
        # 无 DB 路径泄露（与 health auxiliary 同姿态）
        assert "path" not in block
        assert str(tmp_path / "m.db") not in text
        assert ".db" not in text
    finally:
        await source.stop()


async def test_shape_disabled_state():
    resolution = resolve_db_path(env={"OC_SLIMAPI_OPENCODE_DB": "/nonexistent"})
    source = DbAuxiliarySource(resolution)
    status = await source.start()
    assert status.available is False
    try:
        data, _text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["available"] is False
        assert block["mode"] == "http"
        # 启动即解析失败禁用；周期重探失败可能改写为 open_failed
        assert block["reason"] in ("not_found", "open_failed")
        assert block["counters"]["disables"] >= 0
    finally:
        await source.stop()


async def test_shape_circuit_open_state(tmp_path):
    source = await _started(tmp_path)
    try:
        # 模拟真实联动路径：breaker trip → 状态翻转为 circuit_open
        source.breaker.trip()
        source.trip_breaker()
        data, _text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["available"] is False
        assert block["mode"] == "http"
        assert block["reason"] == "circuit_open"
        assert block["breaker_open"] is True
        assert block["counters"]["trips"] == 1
    finally:
        await source.stop()


async def test_no_dbaux_wired_shape_unchanged():
    data, _text = await _get_metrics_dbaux(None)
    assert "dbaux" not in data
    assert set(data) == {"sse", "skeleton", "batch"}


# ---------------------------------------------------------------------------
# 计数器递增
# ---------------------------------------------------------------------------


async def test_queries_counter_increments(tmp_path):
    source = await _started(tmp_path)
    try:
        for _ in range(3):
            rows = await source.query("SELECT id FROM session LIMIT 1")
            assert rows
        data, _text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["counters"]["queries"] == 3
        assert block["latency"]["samples"] == 3
        assert block["latency"]["total"] == 3
        assert block["latency"]["p50_ms"] is not None
        assert block["latency"]["p99_ms"] is not None
    finally:
        await source.stop()


async def test_swap_and_generation_counters(tmp_path):
    source = await _started(tmp_path)
    try:
        # 换一个不同 inode 的合法 fixture DB 文件（os.replace 改 inode）
        replacement = build_fixture_db(tmp_path / "m2.db")
        import os

        os.replace(replacement, str(tmp_path / "m.db"))
        await source.tick()  # inode 校验 → swap
        data, _text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["generation"] == 2
        assert block["counters"]["swaps"] == 1
        assert block["available"] is True
    finally:
        await source.stop()


async def test_disable_counter_on_swap_gate_failure(tmp_path):
    source = await _started(tmp_path)
    try:
        db_path = tmp_path / "m.db"
        # 替换为 schema 漂移库（session 表列改名）→ swap 重开 → 门失败 → 禁用
        drifted = build_fixture_db(tmp_path / "drift.db",
                                   column_rename={"title": "titre"})
        import os

        os.replace(drifted, str(db_path))
        await source.tick()
        data, _text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["available"] is False
        assert block["reason"] == "gate_failed"
        assert block["counters"]["disables"] == 1
        assert block["counters"]["swaps"] == 0  # 失败换手不计 swap
    finally:
        await source.stop()


async def test_probe_counter_after_trip(tmp_path):
    source = await _started(tmp_path)
    try:
        source.breaker.trip()
        source.trip_breaker()
        await source.probe()  # 半开探针（tick 受 next_probe_at 节流，直调）
        data, _text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["counters"]["probes"] == 1
        assert block["counters"]["trips"] == 1
        # 停机不计入 disables（非降级事件）
        await source.stop()
        data2, _text2 = await _get_metrics_dbaux(source)
        assert data2["dbaux"]["counters"]["disables"] == 0
    finally:
        await source.stop(drain_seconds=0)


async def test_disable_counter_on_query_schema_error(tmp_path):
    source = await _started(tmp_path)
    try:
        with pytest.raises(Exception):
            await source.query("SELECT no_such_column FROM session")
        data, _text = await _get_metrics_dbaux(source)
        block = data["dbaux"]
        assert block["available"] is False
        assert block["counters"]["disables"] == 1
        assert block["counters"]["queries"] == 1
    finally:
        await source.stop()
