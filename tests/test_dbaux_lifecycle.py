"""B3a-B1 — dbaux 连接生命周期（design-v4-dbaux §1/§2/§4/§6 冻结清单）。

覆盖：schema 门（§6）、P99 熔断全口径（§2.3-6）、inode swap（§4.1）、
错误分类重探（§4.2）、线程亲和三用例（§2.3-7）、短事务异常收尾（§2.3-2）、
health auxiliary 三态、启动 log。假时钟/注入钩子，不真 sleep。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.dbaux import (
    AuxiliaryUnavailableError,
    DbAuxiliarySource,
    DbAuxStatus,
    LatencyBreaker,
    PROJECT_JOIN_COLUMNS,
    SESSION_PROJECTION_COLUMNS,
    classify_sqlite_error,
    resolve_db_path,
)
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health
from oc_slimapi.selector import SlimapiSelectorMiddleware

IDENTITY = {"Accept-Encoding": "identity"}


# ---------------------------------------------------------------------------
# fixtures：真形状 schema 库（§6.1 全投影列 + project join 列）
# ---------------------------------------------------------------------------

def _create_db(path: Path, *, session_cols=None, project_cols=None, rows=1) -> None:
    cols = session_cols if session_cols is not None else list(
        SESSION_PROJECTION_COLUMNS
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            f"CREATE TABLE session ({', '.join(f'{c} TEXT' for c in cols)})"
        )
        conn.execute(
            "CREATE TABLE project ("
            + ", ".join(
                f"{c} TEXT" for c in (
                    project_cols if project_cols is not None
                    else list(PROJECT_JOIN_COLUMNS)
                )
            )
            + ")"
        )
        for i in range(rows):
            conn.execute(
                "INSERT INTO session (id, title) VALUES (?, ?)",
                (f"s{i}", f"t{i}"),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def good_db(tmp_path: Path) -> Path:
    p = tmp_path / "opencode.db"
    _create_db(p, rows=3)
    return p


def _resolved(path: Path):
    from oc_slimapi.dbaux import ResolvedPath

    return ResolvedPath(path=str(path), source="explicit-env")


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _logger() -> tuple[logging.Logger, _ListHandler]:
    logger = logging.getLogger("oc_slimapi.test.dbaux")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _ListHandler()
    logger.addHandler(handler)
    return logger, handler


async def _started_source(db_path: Path, **kw) -> DbAuxiliarySource:
    src = DbAuxiliarySource(_resolved(db_path), **kw)
    await src.start()
    return src


# ---------------------------------------------------------------------------
# schema 门（§6）
# ---------------------------------------------------------------------------

async def test_schema_gate_passes_full_projection(good_db: Path):
    src = await _started_source(good_db)
    try:
        st = src.status()
        assert st.available and st.mode == "db" and st.reason is None
        assert st.generation == 1
        rows = await src.query("SELECT id FROM session ORDER BY id")
        assert [r[0] for r in rows] == ["s0", "s1", "s2"]
    finally:
        await src.stop()


async def test_schema_gate_missing_session_column(tmp_path: Path):
    p = tmp_path / "opencode.db"
    cols = [c for c in SESSION_PROJECTION_COLUMNS if c != "tokens_input"]
    _create_db(p, session_cols=cols)
    logger, handler = _logger()
    src = DbAuxiliarySource(_resolved(p), logger=logger)
    st = await src.start()
    try:
        assert not st.available and st.mode == "http"
        assert st.reason == "gate_failed"
        assert any("dbaux startup" in m and "tokens_input" in m for m in handler.lines)
        with pytest.raises(AuxiliaryUnavailableError):
            await src.query("SELECT 1")
    finally:
        await src.stop()


async def test_schema_gate_missing_project_join_column(tmp_path: Path):
    p = tmp_path / "opencode.db"
    _create_db(p, project_cols=["id", "worktree"])  # 缺 name
    src = DbAuxiliarySource(_resolved(p))
    st = await src.start()
    try:
        assert not st.available
        assert st.reason == "gate_failed"
    finally:
        await src.stop()


async def test_schema_gate_missing_tables_entirely(tmp_path: Path):
    p = tmp_path / "opencode.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE other (x TEXT)")
    conn.commit()
    conn.close()
    src = DbAuxiliarySource(_resolved(p))
    st = await src.start()
    try:
        assert not st.available and st.reason == "gate_failed"
    finally:
        await src.stop()


async def test_open_failure_disables_not_crashes(tmp_path: Path):
    src = DbAuxiliarySource(_resolved(tmp_path / "missing.db"))
    st = await src.start()
    try:
        assert not st.available and st.reason == "open_failed"
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# rev gate MAJOR-1：门查询抛异常 → 局部连接关闭，不泄漏 fd
# ---------------------------------------------------------------------------


class _ConnSpy:
    """包装 sqlite3.connect：记录创建的连接与其存活状态。"""

    def __init__(self, real) -> None:
        self._real = real
        self.created: list[sqlite3.Connection] = []

    def __call__(self, *a, **kw):
        conn = self._real(*a, **kw)
        self.created.append(conn)
        return conn

    @property
    def open_count(self) -> int:
        return sum(1 for c in self.created if not _conn_is_closed(c))


def _conn_is_closed(conn) -> bool:
    # sqlite3.Connection 无公开 closed 属性；用 total_changes 探活（readonly
    # 连接上合法且常量；已关闭连接抛 ProgrammingError）。
    try:
        conn.total_changes
        return False
    except sqlite3.ProgrammingError:
        return True


async def test_gate_exception_closes_local_connection(tmp_path: Path, monkeypatch):
    """门 PRAGMA 查询抛异常（非「缺列」结论）→ 连接必须被关闭（MAJOR-1）。"""
    import oc_slimapi.dbaux.lifecycle as lifecycle_mod

    p = tmp_path / "opencode.db"
    _create_db(p)

    spy = _ConnSpy(lifecycle_mod.sqlite3.connect)
    monkeypatch.setattr(lifecycle_mod.sqlite3, "connect", spy)

    def _boom(conn):
        raise RuntimeError("gate query exploded")

    monkeypatch.setattr(lifecycle_mod, "schema_gate_missing", _boom)

    src = DbAuxiliarySource(_resolved(p))
    st = await src.start()
    try:
        assert not st.available
        # RuntimeError 属 classify "other" → open_failed；sqlite 族门异常
        # → gate_failed。泄漏断言与 reason 分类正交。
        assert st.reason in ("gate_failed", "open_failed")
        # 门抛异常路径：打开过的连接必须全部关闭（局部所有权 finally 纪律）。
        assert len(spy.created) >= 1
        assert spy.open_count == 0, f"leaked {spy.open_count} connection(s)"
    finally:
        await src.stop()


async def test_repeated_gate_failures_no_fd_accumulation(tmp_path: Path, monkeypatch):
    """连续多次 reprobe 均在门处抛异常 → 每次都关闭，无 fd 泄漏累积。"""
    import oc_slimapi.dbaux.lifecycle as lifecycle_mod

    p = tmp_path / "opencode.db"
    _create_db(p)

    spy = _ConnSpy(lifecycle_mod.sqlite3.connect)
    monkeypatch.setattr(lifecycle_mod.sqlite3, "connect", spy)

    def _boom(conn):
        raise RuntimeError("gate boom")

    monkeypatch.setattr(lifecycle_mod, "schema_gate_missing", _boom)

    src = DbAuxiliarySource(_resolved(p))
    st = await src.start()
    try:
        assert not st.available
        for _ in range(5):
            await src.reprobe()
            assert spy.open_count == 0
        assert len(spy.created) >= 6  # start + 每 reprobe 各开过一次
    finally:
        await src.stop()
    assert spy.open_count == 0


async def test_disabled_resolution_stays_disabled(tmp_path):
    from oc_slimapi.dbaux import DisabledResolution

    logger, handler = _logger()
    src = DbAuxiliarySource(
        DisabledResolution(reason="explicit-memory"), logger=logger
    )
    st = await src.start()
    try:
        assert not st.available and st.reason == "explicit-memory"
        assert any("dbaux startup: disabled" in m for m in handler.lines)
        # 配置性禁用不重探（tick 无副作用）
        await src.tick()
        assert src.status().reason == "explicit-memory"
        assert await src.reprobe() is False
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# 线程亲和三用例（§2.3-7，冻结）
# ---------------------------------------------------------------------------

async def test_affinity_outside_worker_thread_rejected(good_db: Path):
    """① worker 外线程直接访问连接 → sqlite3 ProgrammingError（期望安全
    性质：check_same_thread 证明线程归属恒定）。"""
    src = await _started_source(good_db)
    try:
        conn = src.connection
        assert conn is not None
        assert (
            threading.current_thread()
            is not threading.Thread(
                target=lambda: None, daemon=True
            )  # sanity: we're on a loop thread, not the dbaux worker
        )
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1").fetchall()
    finally:
        await src.stop()


async def test_affinity_worker_async_call_succeeds(good_db: Path):
    """② 经专属 worker 的 async 封装 → 成功（唯一合法通道）。"""
    src = await _started_source(good_db)
    try:
        rows = await src.query("SELECT count(*) FROM session")
        assert rows[0][0] == 3
    finally:
        await src.stop()


async def test_affinity_old_connection_invalid_after_swap(good_db: Path):
    """③ 换代后旧连接引用失效。"""
    src = await _started_source(good_db)
    try:
        old = src.connection
        gen = await src.swap()
        assert gen == src.generation == 2
        with pytest.raises(sqlite3.ProgrammingError):
            old.execute("SELECT 1")  # closed connection → 拒绝
        # 新连接照常服务
        assert (await src.query("SELECT count(*) FROM session"))[0][0] == 3
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# 短事务 + 并发（§1.2 / §2.3-2）
# ---------------------------------------------------------------------------

async def test_query_error_rolls_back_no_dirty_state(good_db: Path):
    """查询异常（BEGIN 后失败）→ 强制 ROLLBACK + 后续查询无
    ``cannot start a transaction within a transaction``。"""
    src = await _started_source(good_db)
    try:
        with pytest.raises(AuxiliaryUnavailableError) as ei:
            await src.query("SELECT no_such_col FROM session")
        assert ei.value.reason == "query_schema"
        # 禁用后拒绝
        with pytest.raises(AuxiliaryUnavailableError):
            await src.query("SELECT 1")
        # 重探恢复（门列仍齐——门只看投影列，不看该 SQL 的列）
        assert await src.reprobe() is True
        rows = await src.query("SELECT count(*) FROM session")
        assert rows[0][0] == 3
    finally:
        await src.stop()


async def test_concurrent_queries_serialized_cleanly(good_db: Path):
    """并发查询 × 事务重叠 → 全部成功（worker 串行 + 每查询独立事务）。"""
    src = await _started_source(good_db)
    try:
        results = await asyncio.gather(*[
            src.query("SELECT count(*) FROM session") for _ in range(20)
        ])
        assert all(r[0][0] == 3 for r in results)
    finally:
        await src.stop()


async def test_swap_during_active_query_no_deadlock(good_db: Path):
    """swap 期间活跃查询不挂死：FIFO——查询先入队，swap 排其后完成锁交接。"""
    release = threading.Event()
    entered = threading.Event()
    src = DbAuxiliarySource(
        _resolved(good_db), in_transaction_pause=lambda: (entered.set(), release.wait(5))[1]
    )
    await src.start()
    try:
        query_task = asyncio.create_task(
            src.query("SELECT count(*) FROM session")
        )
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 2)
        swap_task = asyncio.create_task(src.swap())
        await asyncio.sleep(0.05)  # swap 已入队（排在查询后）
        assert not swap_task.done()
        release.set()
        rows = await asyncio.wait_for(query_task, timeout=5)
        assert rows[0][0] == 3
        assert await asyncio.wait_for(swap_task, timeout=5) == 2
        assert src.status().available
    finally:
        release.set()
        await src.stop()


# ---------------------------------------------------------------------------
# inode/mtime 校验 + swap（§4.1）
# ---------------------------------------------------------------------------

async def _replace_file(path: Path, rows: int) -> None:
    """tmp + rename 技巧换 inode（§3.4 runtime 步骤）。"""
    tmp = path.parent / (path.name + ".new")
    _create_db(tmp, rows=rows)
    os.replace(tmp, path)


async def test_inode_change_triggers_swap_via_tick(good_db: Path):
    src = await _started_source(good_db)
    try:
        gen0 = src.generation
        await _replace_file(good_db, rows=5)
        await src.tick()
        assert src.generation == gen0 + 1
        assert src.status().available
        assert (await src.query("SELECT count(*) FROM session"))[0][0] == 5
    finally:
        await src.stop()


async def test_inode_unchanged_no_swap(good_db: Path):
    src = await _started_source(good_db)
    try:
        gen0 = src.generation
        await src.tick()
        assert src.generation == gen0
    finally:
        await src.stop()


async def test_swap_to_bad_schema_disables_then_reprobe_recovers(good_db: Path):
    src = await _started_source(good_db)
    try:
        # 换成缺列库（swap 门重探失败 → 禁用）
        tmp = good_db.parent / (good_db.name + ".bad")
        cols = [c for c in SESSION_PROJECTION_COLUMNS if c != "metadata"]
        _create_db(tmp, session_cols=cols)
        os.replace(tmp, good_db)
        await src.tick()
        assert not src.status().available
        assert src.status().reason in ("gate_failed",)
        # 换回好库 → 周期重探自愈（§1.4 禁用重探）
        await _replace_file(good_db, rows=2)
        assert await src.reprobe() is True
        assert src.status().available
        assert (await src.query("SELECT count(*) FROM session"))[0][0] == 2
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# P99 熔断（§2.3-6 冻结口径）——假时钟
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def test_breaker_min_samples_no_judgement_below_10():
    clk = FakeClock()
    br = LatencyBreaker(clock=clk)
    for _ in range(10):
        br.record(1000.0)  # 超慢但不判（<10 样本 + warmup 前 10 次）
        assert not br.open
    br.record(1000.0)
    assert br.open  # 第 11 次（>min_samples，窗口 ≥10）→ trip


def test_breaker_p99_threshold_and_window_slide():
    clk = FakeClock()
    br = LatencyBreaker(clock=clk)
    # 10 个快样本（warmup）+ 老化滑出窗口 → 样本不足不判
    for _ in range(10):
        br.record(0.1)
    clk.advance(61.0)  # 全部滑出 60s 窗口
    for _ in range(5):
        br.record(1000.0)
    assert not br.open  # 窗口内 <10 样本 → 不判
    for _ in range(5):
        br.record(1000.0)
    assert br.open


def test_breaker_hysteresis_probe_recovery():
    clk = FakeClock()
    br = LatencyBreaker(clock=clk)
    for _ in range(11):
        br.record(1000.0)
    assert br.open
    # 探针 15ms：成功但 P99 未回落到 <10ms → 不闭合（hysteresis）。
    assert br.note_probe(15.0) is False
    assert br.open
    # nearest-rank P99：窗口内单个 15ms 样本把 P99 钉在 15——更多 2ms
    # 探针样本也不稀释（max 秩）。
    for _ in range(5):
        assert br.note_probe(2.0) is False
    assert br.open
    # 慢样本滑出 60s 窗口后，快探针 → P99 回落 <10ms → 闭合。
    clk.advance(61.0)
    assert br.note_probe(2.0) is True
    assert not br.open


def test_breaker_probe_failure_keeps_open():
    clk = FakeClock()
    br = LatencyBreaker(clock=clk)
    for _ in range(11):
        br.record(1000.0)
    assert br.open
    # 失败探针不计样本（由 source.probe 处理失败路径）；慢探针不闭合
    assert br.note_probe(50.0) is False
    assert br.open


async def test_breaker_end_to_end_trip_and_probe(good_db: Path):
    """查询样本推过阈值 → query 拒绝 circuit_open → 半开探针恢复。"""
    br = LatencyBreaker(trip_threshold_ms=0.0001, recover_threshold_ms=1000.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        for _ in range(10):
            await src.query("SELECT 1")  # warmup 期不判
        await src.query("SELECT 1")  # 第 11 次：真实延迟 > 0.0001ms → trip
        st = src.status()
        assert not st.available and st.mode == "http"
        assert st.reason == "circuit_open"
        with pytest.raises(AuxiliaryUnavailableError, match="circuit_open"):
            await src.query("SELECT 1")
        # 半开探针：真实 SELECT 1 延迟 << 1000ms → 恢复
        assert await src.probe() is True
        assert src.status().available
        assert (await src.query("SELECT 1"))[0][0] == 1
    finally:
        await src.stop()


async def test_breaker_swap_resets_warmup(good_db: Path):
    """swap 后 breaker reset：warmup 重新起算（新连接新延迟画像）。"""
    br = LatencyBreaker(trip_threshold_ms=0.0001, recover_threshold_ms=1000.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        for _ in range(11):
            await src.query("SELECT 1")
        assert src.status().reason == "circuit_open"
        # swap 成功 → reset → available
        await src.swap()
        assert src.status().available
        snap = src.breaker.snapshot()
        assert snap["total"] == 0 and snap["open"] is False
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# 错误分类（§4.2 处置表）
# ---------------------------------------------------------------------------

def test_classify_sqlite_error_table():
    assert classify_sqlite_error(
        sqlite3.OperationalError("no such table: session")) == "schema"
    assert classify_sqlite_error(
        sqlite3.OperationalError("no such column: foo")) == "schema"
    assert classify_sqlite_error(
        sqlite3.OperationalError("database schema has changed")) == "schema"
    assert classify_sqlite_error(
        sqlite3.OperationalError("disk I/O error")) == "io"
    assert classify_sqlite_error(
        sqlite3.OperationalError("unable to open database file")) == "cantinit"
    assert classify_sqlite_error(
        sqlite3.OperationalError("database is locked")) == "busy"
    assert classify_sqlite_error(
        sqlite3.ProgrammingError("SQLite objects created in a thread...")) == "programming"
    assert classify_sqlite_error(ValueError("x")) == "other"


async def test_query_time_schema_change_disables_then_reprobes(good_db: Path):
    """查询期 no such column → 熔断禁用 → 重探恢复（§4.2 第一行）。"""
    src = await _started_source(good_db)
    try:
        with pytest.raises(AuxiliaryUnavailableError) as ei:
            await src.query("SELECT ghost FROM session")
        assert ei.value.reason == "query_schema"
        assert src.status().reason == "query_schema"
        assert await src.reprobe() is True
        assert src.status().available
    finally:
        await src.stop()


async def test_readonly_connection_never_writes(good_db: Path):
    """双层防御：mode=ro 之上的 query_only=ON——任何写语句被拒。"""
    src = await _started_source(good_db)
    try:
        with pytest.raises(AuxiliaryUnavailableError) as ei:
            await src.query("INSERT INTO session (id) VALUES ('x')")
        # "attempt to write a readonly database" → cantinit 族分类 → 禁用
        assert ei.value.reason == "query_cantinit"
        # 禁用后重探仍可恢复（写从未生效）
        await src.reprobe()
        assert (await src.query("SELECT count(*) FROM session"))[0][0] == 3
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# 启动 log（§1.4-4）
# ---------------------------------------------------------------------------

async def test_startup_log_records_path_and_state(good_db: Path):
    logger, handler = _logger()
    src = DbAuxiliarySource(_resolved(good_db), logger=logger)
    await src.start()
    try:
        msgs = [m for m in handler.lines if "dbaux startup" in m]
        assert msgs and good_db.name in msgs[0]
        assert "explicit-env" in msgs[0]
        assert "available:True" in msgs[0]
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# health auxiliary 三态（B5 前置；v4-contract §3.2）
# ---------------------------------------------------------------------------

class _StubAux:
    def __init__(self, status: DbAuxStatus) -> None:
        self._st = status

    def status(self) -> DbAuxStatus:
        return self._st


def _health_app(aux, *, selector: bool = True) -> FastAPI:
    app = FastAPI(title="dba-health")
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.dbaux = aux
    # selector=False → selector-less direct invocation (route default view
    # 3); used by the v3-view shape lock in the placeholder test below
    # (V2b removes that lock with the v3-view teardown).
    if selector:
        app.add_middleware(SlimapiSelectorMiddleware)
    app.include_router(health.router)
    register_error_handlers(app)
    return app


def _settings():
    from oc_slimapi.config import Settings

    return Settings(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )


async def _get_aux(app: FastAPI) -> dict:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/slimapi/health", params={"v": "4"}, headers=IDENTITY)
    assert r.status_code == 200
    return r.json()["auxiliary"]


async def test_health_auxiliary_available_db():
    aux = _get_aux_sync = None
    app = _health_app(_StubAux(DbAuxStatus(available=True, mode="db", reason=None)))
    view = await _get_aux(app)
    assert view == {"available": True, "mode": "db"}


async def test_health_auxiliary_disabled_http_with_reason():
    app = _health_app(
        _StubAux(DbAuxStatus(available=False, mode="http", reason="not_found"))
    )
    view = await _get_aux(app)
    assert view == {"available": False, "mode": "http", "reason": "not_found"}


async def test_health_auxiliary_circuit_open():
    app = _health_app(
        _StubAux(DbAuxStatus(available=False, mode="http", reason="circuit_open"))
    )
    view = await _get_aux(app)
    assert view["reason"] == "circuit_open"


async def test_health_auxiliary_absent_dbaux_placeholder(good_db: Path):
    """未装配 dbaux 的 app（既有测试形态）→ 冻结占位 {false, http}。"""
    app = _health_app(None)
    app.state.dbaux = None
    view = await _get_aux(app)
    assert view == {"available": False, "mode": "http"}
    # v4 视图（唯一准入面）：auxiliary 占位键在响应根级呈现
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/slimapi/health", params={"v": "4"}, headers=IDENTITY)
    assert r.json()["auxiliary"] == {"available": False, "mode": "http"}
    # selector-less 直调（V2b 默认翻转后跑 v4 视图）→ 占位键同样呈现
    # （v3 视图的零 auxiliary 键形态随 v3 面拆除消亡）
    app3 = _health_app(None, selector=False)
    app3.state.dbaux = None
    transport3 = ASGITransport(app=app3)
    async with httpx.AsyncClient(transport=transport3, base_url="http://t") as c:
        r3 = await c.get("/slimapi/health", headers=IDENTITY)
    assert r3.json()["auxiliary"] == {"available": False, "mode": "http"}


async def test_health_auxiliary_real_source_states(good_db: Path):
    """真实 dbaux 三态（可用 db / 禁用 http）直挂 health。"""
    src = await _started_source(good_db)
    try:
        app = _health_app(src)
        assert (await _get_aux(app)) == {"available": True, "mode": "db"}
        await _replace_file(good_db, rows=1)
        await src.tick()  # swap → 仍可用
        assert (await _get_aux(app)) == {"available": True, "mode": "db"}
    finally:
        await src.stop()
    # stop 后（连接关闭）状态明确转禁用视图（reason=stopped）
    app = _health_app(src)
    assert (await _get_aux(app)) == {
        "available": False, "mode": "http", "reason": "stopped",
    }


# ---------------------------------------------------------------------------
# stop / 幂等
# ---------------------------------------------------------------------------

async def test_stop_idempotent_and_query_after_stop(good_db: Path):
    src = await _started_source(good_db)
    await src.stop()
    await src.stop()  # 幂等
    st = src.status()
    assert not st.available and st.reason == "stopped"
    with pytest.raises(AuxiliaryUnavailableError, match="stopped"):
        await src.query("SELECT 1")


async def test_snapshot_shape(good_db: Path):
    src = await _started_source(good_db)
    try:
        snap = src.snapshot()
        assert snap["available"] is True
        assert snap["mode"] == "db"
        assert snap["generation"] == 1
        assert snap["source"] == "explicit-env"
        assert snap["path"] == str(good_db)
        assert set(snap["breaker"]) >= {"open", "samples", "total", "p99_ms"}
    finally:
        await src.stop()
