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
    """FIX-CORR-3r3 连击迟滞：需连续 recover_probes（默认 3）个好探针
    才转 probation（恢复放行）；任一慢探针清零连击。探针样本不进 P99
    窗口（streak 与窗口解耦）。正式闭合由 probation 内真实查询试用
    （见 test_probation_full_lifecycle）。"""
    br = LatencyBreaker()
    for _ in range(11):
        br.record(1000.0)
    assert br.open
    # 慢探针（≥10ms）→ 不闭合且清零连击。
    assert br.note_probe(15.0) is False
    assert br.open
    # 好探针 ×2：连击 2 < 3 → 不闭合。
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is False
    assert br.open
    # 连击被慢探针打断后须重新数满 3。
    assert br.note_probe(15.0) is False
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is False
    assert br.open
    # 第 3 个连续好探针 → 转入 probation（非直接闭合，r3）。
    assert br.note_probe(2.0) is True
    assert not br.open
    assert br.phase == "probation"


def test_breaker_probe_failure_keeps_open():
    clk = FakeClock()
    br = LatencyBreaker(clock=clk)
    for _ in range(11):
        br.record(1000.0)
    assert br.open
    # 失败探针不计（由 source.probe 处理失败路径）；慢探针（≥阈值）
    # 清零连击 → 不闭合。
    assert br.note_probe(50.0) is False
    assert br.open


def test_trip_keeps_window_samples_r3():
    """FIX-CORR-3r3：trip 不清空窗口（样本保留 = closed 态画像连续性）；
    「误闭合快速收敛」职责由 probation 单样本规则承担（与窗口/间隔
    时序解耦——生产时序版见盲区测试）。"""
    br = LatencyBreaker(trip_threshold_ms=20.0)
    for _ in range(11):
        br.record(1000.0)
    assert br.open
    assert br.snapshot()["samples"] == 11  # 样本保留
    # 3 个好探针 → probation（canary 残余便宜度场景：真实投影仍慢）。
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is True
    assert br.phase == "probation"
    # ……首个慢真实查询：probation 规则（latency ≥ trip_ms）立即复 trip。
    br.record(1000.0)
    assert br.open


async def test_breaker_end_to_end_trip_and_probe(good_db: Path):
    """查询样本推过阈值 → query 拒绝 circuit_open → 半开探针转
    probation → 首个「慢」查询（0.0001ms 阈值下任何真实延迟都 ≥ 阈值）
    立即复 trip（r3 全环端到端）。graduation 见
    test_probation_serves_queries_and_graduates。"""
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
        # 半开探针（r3 迟滞）：前两个好探针不闭合，第 3 个连续好探针
        # 转 probation（恢复放行）。真实 canary 延迟 << 1000ms 阈值。
        assert await src.probe() is False
        assert await src.probe() is False
        assert await src.probe() is True
        assert src.status().available
        assert br.phase == "probation"
        # 真实查询放行（试用期）——0.0001ms 阈值下任何真实延迟 ≥ trip
        # → probation 首查询立即复 trip（r3 语义端到端）。
        assert (await src.query("SELECT 1"))[0][0] == 1
        assert br.phase == "open"
        assert src.status().reason == "circuit_open"
        with pytest.raises(AuxiliaryUnavailableError, match="circuit_open"):
            await src.query("SELECT 1")
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
        # r3：swap 全清含相位（probation 不跨连接存活）+ 试用连击。
        assert snap["phase"] == "closed"
        assert snap["probation_good"] == 0
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# FIX-CORR-3：半开探针两段式 —— SELECT 1 只作存活预检，canary 延迟才
# 参与关断判据（连接健康但投影仍慢 → 熔断器保持 open）。
# ---------------------------------------------------------------------------


def test_canary_sql_shape_frozen():
    """canary 必须与 projection.py 的查询形状**路径同构**（防漂移回归）：
    LEFT JOIN + 双键 ORDER BY + LIMIT；且是纯 SELECT（mode=ro +
    query_only 只读约束）。**p.id 必须在 SELECT 列表** —— EQP 实证无
    p 列引用时 SQLite 会直接消除 LEFT JOIN（join 路径缺口重开）。"""
    import re

    sql = DbAuxiliarySource._CANARY_SQL
    assert sql.strip().upper().startswith("SELECT")
    assert "LEFT JOIN project p ON s.project_id = p.id" in sql
    assert "ORDER BY s.time_updated DESC, s.id DESC" in sql
    assert "LIMIT 1" in sql
    # join 消除防线：SELECT 列表（LIMIT 之前）必须引用 p 列。
    select_list = sql.split("FROM")[0]
    assert "p.id" in select_list
    # 词边界匹配（TIME_UPDATED 内含 UPDATE 子串，不得误伤）。
    forbidden = r"\b(INSERT|UPDATE|DELETE|PRAGMA|CREATE|DROP)\b"
    assert re.search(forbidden, sql.upper()) is None


async def test_probe_canary_slow_keeps_breaker_open(
    good_db: Path, monkeypatch
):
    """FIX-CORR-3 核心场景：连接健康（SELECT 1 快）但投影路径仍慢 →
    探针不得关断熔断器（旧实现的 SELECT 1 单段计时在此会误关断）。"""
    br = LatencyBreaker(trip_threshold_ms=20.0, recover_threshold_ms=10.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        src.trip_breaker()
        br.trip()  # source 状态与 breaker 本体都要处于 open
        assert src.status().reason == "circuit_open"

        import time as _time

        def slow_canary() -> None:
            _time.sleep(0.05)  # 50ms — P99 钉在慢档，≥ recover 10ms

        monkeypatch.setattr(src, "_canary_sync", slow_canary)

        for _ in range(3):
            assert await src.probe() is False
            st = src.status()
            assert not st.available and st.reason == "circuit_open"
    finally:
        await src.stop()


async def test_probe_canary_failure_stays_open(good_db: Path, monkeypatch):
    """canary 执行失败（锁/schema 异常等）→ 保持熔断（fail-safe）。"""
    br = LatencyBreaker(trip_threshold_ms=20.0, recover_threshold_ms=10.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        src.trip_breaker()
        br.trip()

        def boom_canary() -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(src, "_canary_sync", boom_canary)
        assert await src.probe() is False
        assert src.status().reason == "circuit_open"
    finally:
        await src.stop()


async def test_probe_liveness_failure_skips_canary(good_db: Path, monkeypatch):
    """SELECT 1 存活预检失败 → 直接保持 open，canary 不执行。"""
    br = LatencyBreaker(trip_threshold_ms=20.0, recover_threshold_ms=10.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        src.trip_breaker()
        br.trip()

        canary_calls: list[int] = []

        def spy_canary() -> None:
            canary_calls.append(1)

        def boom_probe() -> None:
            raise sqlite3.OperationalError("connection closed")

        monkeypatch.setattr(src, "_canary_sync", spy_canary)
        monkeypatch.setattr(src, "_probe_sync", boom_probe)

        assert await src.probe() is False
        assert canary_calls == []  # canary 未被触碰
        assert src.status().reason == "circuit_open"
    finally:
        await src.stop()


async def test_probe_canary_fast_recovers(good_db: Path):
    """两段都快 → 连续第 3 个好探针转 probation；随后 3 个好真实查询
    （good_db 亚毫秒 << 20ms trip）graduation → closed（r3 全链）。"""
    br = LatencyBreaker(trip_threshold_ms=20.0, recover_threshold_ms=1000.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        src.trip_breaker()
        br.trip()
        assert src.status().reason == "circuit_open"
        # good_db 极小：SELECT 1 与 canary（join+全扫+排序）都远低于阈值。
        assert await src.probe() is False
        assert await src.probe() is False
        assert await src.probe() is True
        assert br.phase == "probation"
        for _ in range(3):
            await src.query("SELECT 1")
        assert br.phase == "closed"
        assert src.status().available
    finally:
        await src.stop()


async def test_canary_fast_real_slow_probation_retrips_production_timing(
    good_db: Path,
):
    """FIX-CORR-3r3 盲区场景（rev-2 gate 点名，生产时序版）：canary 快
    但真实投影慢 —— 生产钩子 ``in_transaction_pause`` 只作用于
    ``_run_query``（真实查询）路径，canary/存活预检不经过它。

    与 r2 版的关键差别：探针由 **30s 周期任务驱动**（FakeClock
    advance(30)+tick()，而非手动连调 probe()），如实呈现生产时序下
    r2「窗口样本复 trip」机制失效（warmup 慢样本滑出 60s 窗）——
    收敛职责由 probation 单样本规则承担（与窗口/间隔时序解耦）。

    断言链：(a) 慢真实查询 trip；(b) 3 个周期 tick 的好探针转
    probation；(c) **诚实断言窗口已老化**（samples==0 → P99 复 trip
    路径失效现场）；(d) 首个慢真实查询（50ms ≥ 20ms）仍**立即复
    trip**（probation 规则，样本仅 1 个 << min_samples=10 亦成立）。"""
    import time as _time

    def slow_real_query() -> None:
        _time.sleep(0.05)  # 50ms — 远超 trip 20ms / recover 10ms

    clk = FakeClock()
    br = LatencyBreaker(
        trip_threshold_ms=20.0, recover_threshold_ms=10.0, clock=clk
    )
    src = DbAuxiliarySource(
        _resolved(good_db),
        breaker=br,
        clock=clk,
        in_transaction_pause=slow_real_query,
    )
    await src.start()
    try:
        for _ in range(10):
            await src.query("SELECT 1")  # warmup（每个都真 50ms，不判）
        await src.query("SELECT 1")  # 第 11 个 → P99 ≥ 20ms → trip
        assert src.status().reason == "circuit_open"

        # 生产时序：30s 周期任务驱动探针（trip_breaker 已排程 +30s）。
        clk.advance(30.0)
        await src.tick()  # probe #1 好（canary 无钩子路径，真快）
        assert src.status().reason == "circuit_open"
        clk.advance(30.0)
        await src.tick()  # probe #2 → 连击 2
        assert src.status().reason == "circuit_open"
        clk.advance(30.0)
        await src.tick()  # probe #3 → 转 probation（恢复放行）
        assert src.status().available
        assert br.phase == "probation"

        # 诚实断言窗口已老化（r2 机制失效现场）：FakeClock now=1090，
        # 11 个 warmup 慢样本（ts=1000.0）已全部滑出 60s 窗。
        assert br.snapshot()["samples"] == 0

        # 首个慢真实查询：窗口仅得 1 个新样本（< min_samples=10 →
        # P99 路径必不触发），但 probation 单样本规则立即复 trip。
        await src.query("SELECT 1")
        assert br.snapshot()["samples"] == 1
        assert br.phase == "open"
        assert src.status().reason == "circuit_open"
        with pytest.raises(AuxiliaryUnavailableError, match="circuit_open"):
            await src.query("SELECT 1")
    finally:
        await src.stop()


# ---------------------------------------------------------------------------
# FIX-CORR-3r3：probation 试用期状态机 —— open →（K 连好探针）→
# probation →（N 连好真实查询）closed；probation 任一慢真实查询立即
# 复 trip（豁免 min_samples/warmup，与探针间隔/样本窗口时序解耦）。
# ---------------------------------------------------------------------------


def test_probation_full_lifecycle():
    """breaker 级全生命周期：closed → open → probation → closed，
    graduation 后 closed 态 P99 语义回归。"""
    br = LatencyBreaker()
    assert br.phase == "closed"
    for _ in range(11):
        br.record(1000.0)
    assert br.phase == "open"
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is True
    assert br.phase == "probation" and not br.open
    # 好真实查询 ×2 → 仍 probation（试用连击渐进）。
    br.record(5.0)
    assert br.phase == "probation"
    br.record(5.0)
    assert br.phase == "probation"
    assert br.snapshot()["probation_good"] == 2
    # 第 3 个好真实查询 → graduation → closed。
    br.record(5.0)
    assert br.phase == "closed"
    # closed 语义回归：慢样本积满 → P99 ≥ 20ms → trip。
    for _ in range(11):
        br.record(1000.0)
    assert br.phase == "open"


def test_probation_graduation_threshold_boundary():
    """graduation 阈值 = trip_ms（20）且恰补集：19.9 计好、20.0（>=）
    立即复 trip（与 closed 态触发准则对偶，边界固化）。"""
    # 19.9ms < 20ms → 计好 ×3 → graduation。
    br = LatencyBreaker(trip_threshold_ms=20.0)
    for _ in range(11):
        br.record(1000.0)
    for _ in range(2):
        assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is True
    assert br.phase == "probation"
    br.record(19.9)
    br.record(19.9)
    br.record(19.9)
    assert br.phase == "closed"
    # 20.0ms ≥ 20ms → probation 首查询立即复 trip。
    br2 = LatencyBreaker(trip_threshold_ms=20.0)
    for _ in range(11):
        br2.record(1000.0)
    for _ in range(2):
        assert br2.note_probe(2.0) is False
    assert br2.note_probe(2.0) is True
    br2.record(20.0)
    assert br2.phase == "open"


def test_probation_retrip_resets_probe_streak():
    """probation 复 trip 清双连击计数 → 探针从零重数（好×2 不闭合，
    第 3 个才回 probation）。"""
    br = LatencyBreaker()
    for _ in range(11):
        br.record(1000.0)
    for _ in range(2):
        br.note_probe(2.0)
    assert br.note_probe(2.0) is True
    assert br.phase == "probation"
    # 慢真实查询 → 复 trip（连击全清）。
    br.record(1000.0)
    assert br.phase == "open"
    assert br.snapshot()["probe_streak"] == 0
    # 探针连击从零重数。
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is False
    assert br.note_probe(2.0) is True
    assert br.phase == "probation"


async def test_probe_exception_resets_streak(good_db: Path):
    """MINOR-1：探针异常（存活预检/canary 抛错）清零探针连击——
    「好、好、异常、好」不得在第 4 次凑满 3 连击（异常后须重新数满
    3 个连续成功）。canary 与 liveness 两变体。"""
    br = LatencyBreaker(trip_threshold_ms=20.0, recover_threshold_ms=1000.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        src.trip_breaker()
        br.trip()
        # 好探针 ×2 建立连击。
        assert await src.probe() is False
        assert await src.probe() is False

        # 变体一：canary 抛错 → probe_failed() 清零。
        def boom_canary() -> None:
            raise RuntimeError("canary boom")

        orig_canary = src._canary_sync
        src._canary_sync = boom_canary
        assert await src.probe() is False
        src._canary_sync = orig_canary

        # 无清零的话此处 streak 会到 3 → True；有清零 → 重新从 1 数。
        assert await src.probe() is False
        assert await src.probe() is False
        assert br.snapshot()["probe_streak"] == 2
        assert await src.probe() is True
        assert br.phase == "probation"

        # 变体二：liveness（SELECT 1）抛错 → 同样清零（canary 不执行）。
        br.trip()
        src.trip_breaker()
        assert await src.probe() is False
        assert await src.probe() is False

        def boom_probe() -> None:
            raise RuntimeError("liveness boom")

        orig_probe = src._probe_sync
        src._probe_sync = boom_probe
        assert await src.probe() is False
        src._probe_sync = orig_probe

        assert await src.probe() is False
        assert await src.probe() is False
        assert br.snapshot()["probe_streak"] == 2
        assert await src.probe() is True
        assert br.phase == "probation"
    finally:
        await src.stop()


async def test_probation_serves_queries_and_graduates(
    good_db: Path, caplog
):
    """probation 服务面：真实查询照常放行（试用期即真实流量试探）；
    无查询时 probation 稳定驻留（tick 探针仅 circuit_open 态跑，状态
    不漂移）；3 个好真实查询 graduation → closed + 可观测日志。"""
    clk = FakeClock()
    br = LatencyBreaker(
        trip_threshold_ms=20.0, recover_threshold_ms=1000.0, clock=clk
    )
    src = DbAuxiliarySource(_resolved(good_db), breaker=br, clock=clk)
    await src.start()
    try:
        src.trip_breaker()
        br.trip()
        for _ in range(2):
            assert await src.probe() is False
        assert await src.probe() is True
        assert br.phase == "probation"

        # 驻留不漂移：即使推进 300s，tick 走 available 分支（inode 校验），
        # 不跑探针（探针仅 circuit_open 态）→ probation 稳定。
        clk.advance(300.0)
        await src.tick()
        assert br.phase == "probation"

        # probation 期查询照常放行；good_db 亚毫秒 << 20ms → 计好。
        rows = await src.query("SELECT 1")
        assert rows[0][0] == 1
        assert br.phase == "probation"

        # 3 个好真实查询 → graduation → closed（可观测日志）。
        with caplog.at_level("INFO"):
            await src.query("SELECT 1")
            await src.query("SELECT 1")
        assert br.phase == "closed"
        assert src.status().available
        assert any(
            "probation graduated on real queries" in r.message
            for r in caplog.records
        )
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


# ---------------------------------------------------------------------------
# FIX-CORR-3r4：dequeue-recheck —— probation 复 trip 后已排队查询在
# worker 出队时二次检查熔断器（open → 执行 SQL 前降级，零计时零样本）；
# graduation 边沿日志移入 record() 原子点（MINOR-1(b)）。
# ---------------------------------------------------------------------------


async def test_probation_retrip_degrades_queued_queries_before_sql(good_db: Path):
    """rev-3 gate MAJOR 反例（确定性事件门控版）：probation 首个慢真实
    查询复 trip 时，其余已排队请求若不复查将逐个执行慢 SQL（暴露面 =
    队列深度）。r4 出队二次检查 → 排队查询全部 bail（与入口拒绝同
    reason ``circuit_open``）、**零 SQL 执行**（``in_transaction_pause``
    钩子计数即真实 SQL 执行计数——bail 在 BEGIN 之前，不触发钩子）。

    阶段：① 慢钩子顺序 10 warmup + 第 11 个 → P99 trip；② FakeClock
    30s×3 tick 好 canary → probation；③ 切换事件钩子门控首查询 + 4 个
    排队 → release 复 trip → 断言 4 个排队全 bail、sql_execs==12。
    """
    import time as _time

    release = threading.Event()
    entered = threading.Event()
    sql_execs = [0]

    def slow_warmup() -> None:
        sql_execs[0] += 1
        _time.sleep(0.05)  # 50ms — 远超 trip 20ms

    def gate_first_probation_query() -> None:
        sql_execs[0] += 1
        entered.set()
        release.wait(5)

    clk = FakeClock()
    br = LatencyBreaker(
        trip_threshold_ms=20.0, recover_threshold_ms=10.0, clock=clk
    )
    src = DbAuxiliarySource(
        _resolved(good_db),
        breaker=br,
        clock=clk,
        in_transaction_pause=slow_warmup,
    )
    await src.start()
    try:
        for _ in range(10):
            await src.query("SELECT count(*) FROM session")  # warmup
        await src.query("SELECT count(*) FROM session")  # 第 11 个 → trip
        assert src.status().reason == "circuit_open"

        # 生产时序：3 个周期 tick 的好 canary → probation。
        clk.advance(30.0)
        await src.tick()
        clk.advance(30.0)
        await src.tick()
        clk.advance(30.0)
        await src.tick()
        assert br.phase == "probation"
        assert src.status().available

        # 事件钩子门控首查询（直接属性替换，r3 先例）。
        src._in_txn_pause = gate_first_probation_query
        loop = asyncio.get_running_loop()
        first = asyncio.create_task(src.query("SELECT count(*) FROM session"))
        await loop.run_in_executor(None, entered.wait, 2)
        rest = [
            asyncio.create_task(src.query("SELECT count(*) FROM session"))
            for _ in range(4)
        ]
        await asyncio.sleep(0.05)  # 4 个已入 FIFO（worker 忙于 first）
        release.set()  # first 完成：50ms ≥ 20ms → probation 复 trip → open
        rows = await asyncio.wait_for(first, timeout=5)
        assert rows[0][0] == 3  # 首查询成功返回（probation 放行语义）
        results = await asyncio.wait_for(
            asyncio.gather(*rest, return_exceptions=True), timeout=5
        )

        assert len(results) == 4
        for r in results:
            assert isinstance(r, AuxiliaryUnavailableError)
            assert r.reason == "circuit_open"  # 与入口拒绝同信号
        # 真实 SQL 执行计数：warmup 10 + trip 1 + probation 首 1 = 12；
        # 4 个排队查询零 SQL 执行（bail 在 BEGIN 之前）。
        assert sql_execs[0] == 12
        assert br.snapshot()["open"] is True
        # 入口已受理（queries=「被受理的投影查询数」语义不变）。
        assert src.snapshot()["counters"]["queries"] >= 16
    finally:
        release.set()
        await src.stop()


async def test_graduation_log_single_edge_under_concurrent_queries(
    good_db: Path, caplog
):
    """r4 MINOR-1(b)：graduation 边沿日志由 ``record()`` 相位转换原子点
    发出（worker 单线程串行 → 恰好一次）；并发提交恰在第 N 个好真实
    查询处转换，日志**恰 1 条**（旧 query() 侧 phase_before 跨线程快照
    在并发下可重复/错归因边沿——已删）。"""
    br = LatencyBreaker(trip_threshold_ms=20.0, recover_threshold_ms=1000.0)
    src = DbAuxiliarySource(_resolved(good_db), breaker=br)
    await src.start()
    try:
        src.trip_breaker()
        br.trip()
        assert await src.probe() is False
        assert await src.probe() is False
        assert await src.probe() is True
        assert br.phase == "probation"
        with caplog.at_level("INFO"):
            results = await asyncio.gather(
                *[src.query("SELECT 1") for _ in range(3)]
            )
        assert len(results) == 3
        assert br.phase == "closed"
        graduated = [
            r for r in caplog.records if "probation graduated" in r.message
        ]
        assert len(graduated) == 1  # 真边沿、单点、恰好一次
    finally:
        await src.stop()
