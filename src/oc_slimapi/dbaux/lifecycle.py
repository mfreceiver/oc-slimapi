"""v4 sessions DB 投影源——连接生命周期 / 所有权 / 熔断 / inode 校验。

实现权威：``docs/specs/design-v4-dbaux.md`` §1（连接生命周期）、§2（S-B02
所有权与并发）、§4（inode/mtime 校验 + 错误分类重探）、§6（schema 门）。
本模块是阶段 B 的地基：B2（投影 SQL）/B3（cursor）/B4（路由分叉）在此
之上构建。

冻结项速查（详见各方法 docstring）：

- §1.1 主路径 ``mode=ro`` URI + ``PRAGMA query_only=ON`` 双层防御；
  ``immutable=1`` 完全弃用（不作主路径不作降级档，§1.3）。
- §1.2 短生命周期只读事务：每查询显式 ``BEGIN … COMMIT``（deferred
  snapshot），异常路径强制 ``ROLLBACK`` + 游标 close（§2.3-2）。
- §2.2 线程亲和（方案 1 冻结）：专属 ``ThreadPoolExecutor(max_workers=1)``；
  连接建立/查询/rollback/重开/关闭全部在专属 worker 内执行；
  ``check_same_thread`` 默认 True 保持；event loop 侧仅 async 封装。
- §2.3-6 P99 熔断：60s 滑窗 + ≥10 样本 + 前 10 次 warmup 豁免 +
  阈值 P99≥20ms + 半开探针（30s 周期）+ hysteresis（P99<10ms 恢复）。
- §4.1 inode/mtime 周期校验（30s，与探针同调度器）→ swap generation。
- §4.2 错误分类：schema/io/cantinit → 禁用+周期重探（不试 immutable）；
  busy → 计入 P99 样本；ProgrammingError → 禁用 + error log。
- §6 schema 门：session 全投影列 + project join 列（id/name/worktree），
  ``PRAGMA table_info`` 只读比对。

状态快照（B5 挂 health/metrics）：``snapshot()`` →
``{"available": bool, "mode": "db"|"http", "reason": str|None, ...}``。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.parse import quote

from ..logging_config import get_logger
from .path_resolution import (
    DisabledResolution,
    ResolvedPath,
    stat_inode_marker,
)

__all__ = [
    "AuxiliaryUnavailableError",
    "DbAuxiliarySource",
    "LatencyBreaker",
    "PROJECT_JOIN_COLUMNS",
    "SESSION_PROJECTION_COLUMNS",
    "classify_sqlite_error",
    "schema_gate_missing",
]


# ---------------------------------------------------------------------------
# §6 schema 门（全投影列版，冻结）
# ---------------------------------------------------------------------------

# session 表投影列（§6.1；真库 29 列中投影消费的 24 列去重清单——
# time_updated/time_archived 只列一次；tokens_* 用真库列名 tokens_input/
# output，R2 实证冻结；模板 tokens_in/out 为 v2.2 撰写笔误）。
SESSION_PROJECTION_COLUMNS: tuple[str, ...] = (
    "id",
    "parent_id",
    "project_id",
    "time_archived",
    "time_updated",
    "directory",
    "title",
    "agent",
    "model",
    "version",
    "summary_additions",
    "summary_deletions",
    "summary_files",
    "summary_diffs",
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "time_created",
    "time_compacting",
    "revert",
    "permission",
    "metadata",
)

# project 表 join 列（§6.2 契约冻结 project={id,name,worktree}；真库无
# directory 列，R1 实证关闭）。
PROJECT_JOIN_COLUMNS: tuple[str, ...] = ("id", "name", "worktree")


def schema_gate_missing(conn: sqlite3.Connection) -> list[str]:
    """§6 门校验（纯只读）：返回缺失列清单（[] = 通过）。

    表不存在 → ``PRAGMA table_info`` 空 → 该表全部列计为缺失。
    缺任一投影列 → 禁用辅助降级 HTTP（§1.4-3）。
    """
    session_cols = {row[1] for row in conn.execute("PRAGMA table_info(session)")}
    missing = [c for c in SESSION_PROJECTION_COLUMNS if c not in session_cols]
    project_cols = {row[1] for row in conn.execute("PRAGMA table_info(project)")}
    missing += [f"project.{c}" for c in PROJECT_JOIN_COLUMNS if c not in project_cols]
    return missing


# ---------------------------------------------------------------------------
# §4.2 错误分类（查询封装层入口）
# ---------------------------------------------------------------------------

def classify_sqlite_error(exc: BaseException) -> str:
    """把查询/探测抛错分类到 §4.2 处置表。

    返回 ∈ {schema, io, cantinit, busy, programming, other}：
    - ``schema``   → SQLITE_SCHEMA / no such table|column（schema 门失效）
    - ``io``       → SQLITE_IOERR*（数据源物理不可用）
    - ``cantinit`` → SQLITE_READONLY_CANTINIT / unable to open（shm 不可达）
    - ``busy``     → SQLITE_BUSY 超 busy_timeout（计入 P99 样本，不禁用）
    - ``programming`` → check_same_thread 违背（实现缺陷信号，error log）
    """
    if isinstance(exc, sqlite3.ProgrammingError):
        return "programming"
    if isinstance(exc, sqlite3.Error):
        msg = str(exc).lower()
        if "no such table" in msg or "no such column" in msg or (
            "database schema has changed" in msg
        ):
            return "schema"
        if "unable to open database file" in msg or "readonly" in msg or (
            "shm" in msg
        ):
            return "cantinit"
        if "disk i/o error" in msg or "i/o error" in msg:
            return "io"
        if "database is locked" in msg or "database table is locked" in msg:
            return "busy"
    return "other"


# ---------------------------------------------------------------------------
# §2.3-6 P99 熔断器（冻结口径；独立小类便于假时钟注入）
# ---------------------------------------------------------------------------

class LatencyBreaker:
    """P99 熔断护栏（§2.3-6 / §5.3，行 106,147）。

    - 滑动窗口 60s（环形样本 ``(monotonic_ts, latency_ms)``，惰性剪枝）；
    - 最小样本 ≥10 才计 P99（样本不足 = 不判熔断）；
    - warmup 豁免：前 10 次（``min_samples`` 次）仅采样不判（与
      「不足 10 次不判」同路径——冷启动 P99 噪声不误熔断）；
    - 熔断阈值：P99 ≥ 20ms → open；
    - 恢复（FIX-CORR-3r3，probation 试用期状态机）：
      ``closed --P99≥trip--> open --K 连好探针--> probation
      --N 连好真实查询--> closed``；probation 中任一真实查询延迟
      ≥ trip 阈值 → **立即复 trip**（豁免 min_samples/warmup——
      与探针间隔/样本窗口时序解耦，误闭合最坏暴露面 = 1 个慢查询
      （r4：其余已排队查询出队时二次检查并降级，不执行 SQL））。
      * 探针连击（``recover_probes``，默认 3）：半开探针需连续
      低于恢复阈值（10ms）才转入 probation（非直接闭合）；任一
      ≥ 阈值探针或探针异常（``probe_failed``）清零连击。探针样本
      **不**进入 ``_samples``（P99 窗口语义只属于 ``record()``）。
      * 试用连击（``recover_queries``，默认 3）：probation 内查询
      放行、逐查询以 ``latency`` 标量判定（< trip_ms 计好、≥ trip_ms
      复 trip——恰补集，阈值与 closed 态触发准则对偶）； probation
      样本照常进窗口（为 closed 态积累画像），但判定**不读**窗口。
    - trip **不清空窗口**（r2 起保留）：职责仅为 closed 态 P99 画像
      连续性；「误闭合快速收敛」职责已移交 probation（r3）。
    """

    def __init__(
        self,
        *,
        window_s: float = 60.0,
        min_samples: int = 10,
        trip_threshold_ms: float = 20.0,
        recover_threshold_ms: float = 10.0,
        recover_probes: int = 3,
        recover_queries: int = 3,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._window_s = window_s
        self._min_samples = min_samples
        self._trip_ms = trip_threshold_ms
        self._recover_ms = recover_threshold_ms
        self._recover_probes = max(1, int(recover_probes))
        self._recover_queries = max(1, int(recover_queries))
        self._clock = clock
        # r4：仅相位转换边沿日志（graduation）；None = 静默（独立构造/
        # 旧单测零影响；DbAuxiliarySource 默认注入自身 logger）。
        self._logger = logger
        self._samples: list[tuple[float, float]] = []
        self._probe_streak = 0
        self._probation_good = 0
        self._total = 0
        # r3 三态相位（替代 r1/r2 的 ``_open: bool``）：probation 不表现
        # 为 open——真实查询必须放行（试用期意义即真实流量试探）。
        self._phase = "closed"

    @property
    def open(self) -> bool:
        return self._phase == "open"

    @property
    def phase(self) -> str:
        """当前相位（closed / open / probation；只读公开——日志与测试）。"""
        return self._phase

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        if self._samples and self._samples[0][0] < cutoff:
            self._samples = [s for s in self._samples if s[0] >= cutoff]

    def _pctl(self, pct: int) -> float | None:
        """最近邻秩百分位（ceil(pct*n)-1；与 p99 同一保守口径）。"""
        if not self._samples:
            return None
        ordered = sorted(lat for _, lat in self._samples)
        rank = max(0, -(-len(ordered) * pct // 100) - 1)
        return ordered[rank]

    def _p99(self) -> float | None:
        return self._pctl(99)

    def record(self, latency_ms: float) -> None:
        """计入一个真实查询样本；按相位分派判定。

        剪枝/追加/计数为**无条件前置步骤**（r3 必改点：probation 分支
        return 于其后 → probation 样本照常进窗口且窗口保持新鲜）。
        """
        now = self._clock()
        self._prune(now)
        self._samples.append((now, latency_ms))
        self._total += 1
        if self._phase == "open":
            return  # open 态不判（恢复路径由探针独占）
        if self._phase == "probation":
            # r3 试用期：逐查询即时判定，只读 latency 标量（不读窗口）。
            if latency_ms >= self._trip_ms:
                self.trip()  # 立即复 trip —— 豁免 min_samples/warmup
            else:
                self._probation_good += 1
                if self._probation_good >= self._recover_queries:
                    self._phase = "closed"
                    self._probation_good = 0
                    # r4 MINOR-1(b)：graduation 边沿日志在**本原子点**发出
                    # （record() 仅由 worker 单线程串行调用 → 恰好一次；
                    # query() 侧的跨线程 phase_before 快照已删——并发下
                    # 多查询会重复/错归因边沿）。
                    if self._logger is not None:
                        self._logger.info(
                            "dbaux circuit closed (probation graduated"
                            " on real queries)"
                        )
            return
        # closed：warmup 豁免（前 min_samples 次仅采样不判，§2.3-6）。
        if self._total <= self._min_samples:
            return
        if len(self._samples) < self._min_samples:
            return  # 样本不足 = 不判（滑窗剪枝后可能低于下限）
        p99 = self._p99()
        if p99 is not None and p99 >= self._trip_ms:
            self.trip()

    def trip(self) -> None:
        """打开熔断（open 相位；r3：不清窗口、清双连击计数）。

        复 trip 两条路径：probation 单样本规则（latency ≥ trip_ms）与
        closed 态 P99 判定。连击计数清零 → 探针/试用各自从零重数。
        """
        self._phase = "open"
        self._probe_streak = 0
        self._probation_good = 0

    def note_probe(self, latency_ms: float) -> bool:
        """半开探针结果（FIX-CORR-3r3 连击迟滞 → probation）。

        连续第 ``recover_probes``（默认 3）个低于恢复阈值的探针 →
        **转入 probation 试用期**（返回 True = 恢复放行；正式闭合需
        probation 内真实查询试用）；任一 ≥ 阈值的探针清零连击。探针
        样本不进入 ``_samples``（P99 窗口语义只属于 ``record()``）。
        """
        if self._phase != "open":
            return True
        if latency_ms < self._recover_ms:
            self._probe_streak += 1
        else:
            self._probe_streak = 0
        if self._probe_streak >= self._recover_probes:
            self._phase = "probation"
            self._probe_streak = 0
            return True
        return False

    def probe_failed(self) -> None:
        """探针异常（存活预检/canary 抛错）→ 清零探针连击。

        异常 = 证据缺失 = 断连击：「连续 K 次成功」要求真实成功，
        「好、好、异常、好」不得在第 4 次后凑满 3 连击（MINOR-1）。
        """
        if self._phase == "open":
            self._probe_streak = 0

    def reset(self) -> None:
        """swap/重开成功后清零（新连接 = 新延迟画像；warmup 重新起算）。

        r3：probation 不跨连接存活——swap 全清回 closed，warmup 重启
        即为保护（文档化取舍，见 fix-plan-corr-r3 §7）。
        """
        self._samples = []
        self._probe_streak = 0
        self._probation_good = 0
        self._total = 0
        self._phase = "closed"

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        self._prune(now)
        return {
            "open": self._phase == "open",
            "phase": self._phase,
            "samples": len(self._samples),
            "total": self._total,
            "p50_ms": self._pctl(50),
            "p99_ms": self._p99(),
            "probe_streak": self._probe_streak,
            "probation_good": self._probation_good,
        }


# ---------------------------------------------------------------------------
# 异常类型（B4 路由分叉消费）
# ---------------------------------------------------------------------------

class AuxiliaryUnavailableError(RuntimeError):
    """辅助源不可用（禁用/熔断）时 ``query`` 的拒绝信号。

    ``reason`` 即 health ``auxiliary.reason`` 同源值（§2.3-6 / §9.3）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"db auxiliary unavailable: {reason}")
        self.reason = reason


# ---------------------------------------------------------------------------
# §1/§2 连接生命周期主体
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DbAuxStatus:
    """状态快照（§1.4-4 / B5 挂 health）。"""

    available: bool
    mode: str  # "db" | "http"
    reason: str | None
    generation: int = 0

    def auxiliary_view(self) -> dict[str, Any]:
        """v4 health ``auxiliary`` 字段视图（v4-contract §3.2）。"""
        view: dict[str, Any] = {"available": self.available, "mode": self.mode}
        if not self.available and self.reason:
            view["reason"] = self.reason
        return view


class DbAuxiliarySource:
    """单连接 + 专属 worker 的只读 DB 辅助源（§2.1 方案 ② 的线程亲和
    实现 = §2.2 方案 1，冻结）。

    - 连接的建立/查询/rollback/重开/关闭**全部**在专属
      ``ThreadPoolExecutor(max_workers=1)`` worker 内执行；
    - event loop 侧仅 async 封装（``run_in_executor``）；
    - ``check_same_thread`` 默认 True 保持（线程归属恒定 = 永不跨线程
      访问；worker 外线程使用连接 → ``sqlite3.ProgrammingError``，
      §2.3-7① 期望安全性质）；
    - swap generation（§2.3-1）：inode 变化 → swap 任务提交同一 worker
      FIFO（等活跃查询完成后锁交接），close 旧 → 开新（query_only +
      busy_timeout + schema 门重探）→ generation+1；查询不得持锁跨
      swap（worker 串行保证，无需显式锁）。
    """

    BUSY_TIMEOUT_MS = 5000  # §2.3-3：与上游 database.ts:29 同值（冻结）

    # FIX-CORR-3r2: 代表性半开 canary —— 与 projection.py 投影查询**路径
    # 同构**：LEFT JOIN project + 无索引全扫 + TEMP B-TREE 排序（EQP 于
    # 真实上游 DB 实证：SCAN session / SEARCH p USING COVERING INDEX
    # sqlite_autoindex_project_1 / USE TEMP B-TREE FOR ORDER BY）。
    # **load-bearing**：SELECT 必须引用至少一个 ``p`` 列 —— EQP 实证无
    # p 列引用时 SQLite 直接消除 LEFT JOIN（只剩 SCAN，join 缺口重开）。
    # 谓词省略是刻意的（谓词只会减少参与排序的行数 → 无谓词 = 上界
    # 代表，保守方向正确）；LIMIT 1 vs LIMIT n 在排序全量完成后边际
    # 可忽略；宽列物化的残余差距由连击迟滞 + probation 逐查询判定
    # 兜底：首个 ≥trip 阈值真实查询立即复 trip；r4 起已排队查询出队
    # 时二次检查熔断器，open 即在执行 SQL 前降级。纯 SELECT，符合
    # mode=ro +
    # PRAGMA query_only=ON 只读约束。SELECT 1 只保留为连接存活预检，
    # 其延迟对延迟型故障零分辨力，不喂 breaker。
    _CANARY_SQL = (
        "SELECT s.id, p.id FROM session s "
        "LEFT JOIN project p ON s.project_id = p.id "
        "ORDER BY s.time_updated DESC, s.id DESC LIMIT 1"
    )

    def __init__(
        self,
        resolution: ResolvedPath | DisabledResolution,
        *,
        probe_interval_s: float = 30.0,
        breaker: LatencyBreaker | None = None,
        clock: Callable[[], float] = time.monotonic,
        in_transaction_pause: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._resolution = resolution
        self._probe_interval_s = probe_interval_s
        # r4：logger 先行初始化——默认 breaker 构造注入自身 logger，
        # 使 graduation 边沿日志从 record() 原子点发出。显式传入的
        # breaker 若未配置 logger（None）→ 补注入（同模块亲密赋值；
        # 已配置则尊重调用方）——保证任何经 source 使用的 breaker 都
        # 有边沿日志（既有显式 breaker 测试零改动）。
        self._logger = logger if logger is not None else get_logger("dbaux")
        if breaker is not None and breaker._logger is None:
            breaker._logger = self._logger
        self._breaker = (
            breaker
            if breaker is not None
            else LatencyBreaker(logger=self._logger)
        )
        self._clock = clock
        # 测试钩子：worker 内事务中段暂停（模拟慢查询 / swap 期间活跃
        # 查询）。生产恒 None。
        self._in_txn_pause = in_transaction_pause

        # §2.2：专属单 worker。max_workers 必须固定 1——共享多 worker 池
        # 会并发访问同一连接 = 线程错误（§2.2 表 TransformPool 关系行）。
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="oc-slimapi-dbaux"
        )
        # 连接引用只在 worker 内读写（创建/使用/关闭）；测试从外部线程
        # 「读引用并使用」正是 §2.3-7① 要断言被拒的路径。
        self._conn: sqlite3.Connection | None = None
        self._generation = 0
        self._inode: tuple[int, int] | None = None
        self._state = "disabled"
        self._reason: str | None = None
        self._reason_detail: str | None = None
        # B5 观测计数器（v4-contract §9.1）：queries=被受理的投影查询数；
        # probes=半开/禁用重探事件；trips=熔断翻转事件；swaps=连接换手
        # （含 inode swap）成功次数；disables=禁用事件（不含停机 stop）。
        # 降级响应计数在路由层（sessions.py 禁改域）——此处以
        # disables+trips 作为降级源事件的代理计数（任务批注允许）。
        self._counters: dict[str, int] = {
            "queries": 0,
            "probes": 0,
            "trips": 0,
            "swaps": 0,
            "disables": 0,
        }
        # 显式 :memory: 禁用 = 配置性永久态，不周期重探（重探也无果）。
        self._reprobe_allowed = not (
            isinstance(resolution, DisabledResolution)
            and resolution.reason in ("explicit-memory", "upstream-memory")
        )
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._next_probe_at: float | None = None
        self._worker_thread_id: int | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> DbAuxStatus:
        """§1.4 启动探测：路径解析 → ro 打开 → query_only → schema 门。

        任一失败 → 禁用辅助（不崩溃，全降级态）+ 周期重探；启动 log
        记录解析路径/来源/门结果/辅助状态。幂等（重复 start no-op）。
        """
        if self._task is not None:
            return self.status()
        if isinstance(self._resolution, DisabledResolution):
            self._state, self._reason = "disabled", self._resolution.reason
        else:
            await self._open_and_gate(why="startup")
        self._log_startup()
        # 周期任务（§4.1/§4.2）：inode 校验 + 熔断探针 + 禁用重探共用
        # 同一调度器（asyncio task，30s 间隔）。
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._periodic(self._stop_event), name="oc-slimapi-dbaux-probe"
        )
        return self.status()

    async def stop(self, *, drain_seconds: float = 5.0) -> None:
        """关闭：停周期任务 → worker 内 close 连接 → drain 有界超时
        （对齐 ``_shutdown_transforms`` 先例，app.py:298-309）。幂等。"""
        if self._stop_event is not None:
            self._stop_event.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=drain_seconds)
            except Exception:  # noqa: BLE001 — best-effort drain, then cancel
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001
                    pass
        try:
            await self._submit(self._close_conn)
        except Exception as exc:  # noqa: BLE001 — best-effort
            self._logger.warning("dbaux close failed", exc_info=exc)
        self._bounded_executor_shutdown(drain_seconds)
        # stop 后状态明确转禁用：health/后续 query 不再宣称可用（stop 是
        # 生命周期终点，非可恢复降级——重启用新实例）。
        self._disable(reason="stopped")

    def _bounded_executor_shutdown(self, wait_seconds: float) -> None:
        """有界 drain（transform.py:291 同型）：cancel pending → 守护线程
        bounded wait → 超时也返回（不阻塞事件循环/进程退出）。"""
        self._executor.shutdown(wait=False, cancel_futures=True)
        done = threading.Event()

        def _drain() -> None:
            try:
                self._executor.shutdown(wait=True)
            finally:
                done.set()

        watcher = threading.Thread(
            target=_drain, daemon=True, name="oc-slimapi-dbaux-drain"
        )
        watcher.start()
        done.wait(timeout=wait_seconds)

    # ------------------------------------------------------------------
    # 查询通道（唯一合法通道，§2.3-7②）
    # ------------------------------------------------------------------

    async def query(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[tuple]:
        """执行一条只读投影 SQL（§1.2 短事务：BEGIN → execute → COMMIT）。

        - 可用态才受理；禁用/熔断 → ``AuxiliaryUnavailableError``；
        - 异常路径强制 ``ROLLBACK`` + 游标 close（§2.3-2 try/finally）；
        - 延迟（含 busy 等待）计入 P99 样本（§2.3-3/§4.2 busy 行）；
        - 错误按 §4.2 分类：schema/io/cantinit/programming → 禁用+周期
          重探；busy → 不禁用（P99 路径自洽）。
        """
        status = self.status()
        if not status.available:
            raise AuxiliaryUnavailableError(status.reason or "disabled")
        self._counters["queries"] += 1
        try:
            rows = await self._submit(
                self._run_query, sql, tuple(params)
            )
        except sqlite3.Error as exc:
            kind = classify_sqlite_error(exc)
            if kind == "busy":
                # §4.2：BUSY 超时计入延迟样本（已计）→ P99 超限熔断路径；
                # 短事务设计下罕见，不禁用。
                raise
            # schema 门失效 / IO / WAL-SHM 不可达 / 线程亲和破坏 → 熔断禁用
            # + 周期重探（§4.2 处置表）。错误细节只进日志/metrics（§7.4：
            # HTTP 侧由 B4 统一 503 auxiliary_unavailable，不泄露 DB 细节）。
            log = self._logger.error if kind == "programming" else self._logger.warning
            log("dbaux query error classified=%s", kind, exc_info=exc)
            self._disable(reason=f"query_{kind}")
            raise AuxiliaryUnavailableError(f"query_{kind}") from exc
        # 本查询样本可能刚把 P99 推过阈值 → 立即联动熔断态（后续请求被拒）。
        self._check_breaker_state()
        # r4：graduation 边沿日志由 breaker.record() 原子点发出（此处
        # 的跨线程 phase_before 快照已删——并发下不可靠，见 MINOR-1）。
        return rows

    def _run_query(self, sql: str, params: tuple) -> list[tuple]:
        """worker 内同步执行：显式 BEGIN … COMMIT + finally ROLLBACK。

        FIX-CORR-3r4（dequeue-recheck）：入口检查（query()）与实际执行
        之间存在排队窗口；probation 首个慢查询复 trip 后，已排队请求若
        不复查将逐个执行慢 SQL（暴露面 = 队列深度）。此处出队后、执行
        前（t0 计时之前、BEGIN 之前、finally 之外）二次检查：breaker
        open → 不执行 SQL、零计时零样本，抛与入口拒绝**完全同信号**的
        ``AuxiliaryUnavailableError("circuit_open")``（路由层降级路径
        逐字节一致）。probation **不算 open**（试用期语义 = 真实查询
        放行）。
        """
        if self._breaker.open:
            raise AuxiliaryUnavailableError("circuit_open")
        assert self._conn is not None
        conn = self._conn
        t0 = time.perf_counter()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")  # deferred → snapshot 语义（§1.2）
            if self._in_txn_pause is not None:
                self._in_txn_pause()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.execute("COMMIT")
            return rows
        finally:
            # §2.3-2：异常路径强制 ROLLBACK（防 ``cannot start a
            # transaction within a transaction`` 脏状态）+ 游标 close。
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            cur.close()
            # 延迟计入 P99 样本（busy 等待同样计入，§2.3-3）。
            self._breaker.record((time.perf_counter() - t0) * 1000.0)

    # ------------------------------------------------------------------
    # 打开 / schema 门 / swap / 重探（全部 worker 内）
    # ------------------------------------------------------------------

    def _submit(self, fn, /, *args):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._executor, fn, *args)

    def _open_conn(self) -> sqlite3.Connection:
        """§1.1：``file:{path}?mode=ro`` URI 打开 + 立即双层防御 PRAGMA。

        immutable 完全弃用（§1.3）——不作主路径不作降级档；shm 不可达
        场景直接失败走禁用（不试 immutable）。
        """
        path = self._resolution.path  # type: ignore[union-attr]
        uri = f"file:{quote(path, safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)  # check_same_thread 默认 True
        self._worker_thread_id = threading.get_ident()
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        return conn

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _open_and_gate_sync(self) -> None:
        """worker 内：关旧（若有）→ 开新 → 门。失败抛（旧已关，状态由
        调用方置禁用）。

        rev gate MAJOR-1：局部所有权纪律——连接在门**全部成功**前只归
        本栈帧所有；门查询自身抛异常（非「缺列」结论——如 PRAGMA 被锁/
        IO 错）时 finally 关闭局部连接，绝不泄漏给进程 fd 表。只有门过
        后才转移 ``self._conn`` 并推进 generation。
        """
        self._close_conn()
        conn = self._open_conn()
        try:
            missing = schema_gate_missing(conn)
            if missing:
                raise sqlite3.OperationalError(
                    f"schema gate failed, missing columns: {missing}"
                )
        except BaseException:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            raise
        self._conn = conn
        self._generation += 1
        marker = stat_inode_marker(self._resolution.path)  # type: ignore[union-attr]
        self._inode = marker

    async def _open_and_gate(self, *, why: str) -> None:
        try:
            await self._submit(self._open_and_gate_sync)
        except Exception as exc:  # noqa: BLE001 — 禁用而非崩溃（§1.4-3）
            reason = "gate_failed" if "schema gate" in str(exc) else "open_failed"
            self._disable(reason=reason)
            self._reason_detail = str(exc)
            self._logger.warning(
                "dbaux %s failed (reason=%s)", why, reason, exc_info=exc
            )
            return
        self._state, self._reason = "available", None
        self._reason_detail = None
        # 新连接 = 新延迟画像：warmup 重新起算（§2.3-6 冷启动豁免同源）。
        self._breaker.reset()
        self._next_probe_at = None

    async def swap(self) -> int:
        """§2.3-1 swap generation（inode 变化触发；测试可直调）。

        swap 任务提交同一 worker FIFO：等活跃查询完成后锁交接 →
        close 旧 → 开新（query_only + busy_timeout + schema 门重探）→
        generation+1。失败 → 禁用 + 周期重试（§4.1）。返回新 generation。
        """
        if not isinstance(self._resolution, ResolvedPath):
            return self._generation
        await self._open_and_gate(why="swap")
        if self._state == "available":
            self._counters["swaps"] += 1
        return self._generation

    # ------------------------------------------------------------------
    # 周期任务：inode 校验 + 熔断探针 + 禁用重探（同调度器，§4.1）
    # ------------------------------------------------------------------

    async def _periodic(self, stop: asyncio.Event) -> None:
        interval = self._probe_interval_s
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break  # stop set
            except asyncio.TimeoutError:
                pass
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — 周期任务永不因单次失败退出
                self._logger.warning("dbaux tick failed", exc_info=exc)

    async def tick(self) -> None:
        """单次周期：①inode/mtime 校验（§4.1）②熔断半开探针（§2.3-6）
        ③禁用重探（§1.4）。测试可直调（不启周期任务）。"""
        now = self._clock()
        # ① 活跃连接 → inode/mtime 校验；-wal/-shm 不参与（§4.1）。
        if self._state == "available" and self._conn is not None:
            marker = stat_inode_marker(self._resolution.path)  # type: ignore[union-attr]
            if marker is not None and self._inode is not None and marker != self._inode:
                self._logger.info(
                    "dbaux inode/mtime change detected — swapping connection"
                )
                await self.swap()
                return
        # ② 熔断半开：周期单次探针；连续第 recover_probes 个好探针
        # （r3）→ 进入 probation 试用期（真实查询试用，非直接闭合）。
        if self._state == "circuit_open":
            if self._next_probe_at is None or now >= self._next_probe_at:
                await self.probe()
            return
        # ③ 禁用重探（启动失败多为上游未就绪/路径错配，冷启动竞态自愈）。
        if self._state == "disabled" and self._reprobe_allowed:
            await self.reprobe()

    async def probe(self) -> bool:
        """半开单次探针（§2.3-6，FIX-CORR-3r2 两段式）。返回是否恢复闭合。

        Step 1 — 存活预检（``SELECT 1``）：只捕获连接层故障（被锁/
        关闭/损坏），失败 → 保持熔断、不喂任何延迟样本；其延迟对延迟
        型故障零分辨力，**不纳入计时**（oracle 定死：SELECT 1 延迟不得
        参与 breaker 关断判据）。

        Step 2 — 路径同构 canary（``_CANARY_SQL``）：LEFT JOIN + 扫+排序
        路径；**只包此段计时**，其延迟喂 ``note_probe``。闭合需连续
        ``recover_probes``（默认 3）个好探针（迟滞）—— 连接健康但投影
        仍慢时熔断器保持 open（本修复的核心语义）；canary 相对宽列投影
        的残余便宜度由迟滞 + probation 逐查询判定 + r4 出队二次检查
        兜底（误闭合暴露面 = 1 个慢查询，排队请求执行前降级）。
        """
        self._counters["probes"] += 1
        self._next_probe_at = self._clock() + self._probe_interval_s
        try:
            # Step 1 — liveness only (locked/closed/broken conn). Its
            # latency is deliberately NOT measured: SELECT 1 cannot
            # distinguish a healthy connection from a degraded-scan one.
            await self._submit(self._probe_sync)
        except Exception:  # noqa: BLE001 — 失败 → 保持熔断
            self._breaker.probe_failed()  # MINOR-1：异常断探针连击
            self._logger.info("dbaux half-open probe failed — staying open")
            return False
        t0 = time.perf_counter()
        try:
            # Step 2 — representative canary; ONLY its latency certifies
            # recovery of the latency fault.
            await self._submit(self._canary_sync)
        except Exception as exc:  # noqa: BLE001 — 失败 → 保持熔断
            self._breaker.probe_failed()  # MINOR-1：异常断探针连击
            self._logger.info(
                "dbaux half-open canary failed — staying open: %s", exc
            )
            return False
        latency_ms = (time.perf_counter() - t0) * 1000.0
        recovered = self._breaker.note_probe(latency_ms)
        if recovered:
            self._state, self._reason = "available", None
            self._logger.info(
                "dbaux circuit recovered to probation (canary streak"
                " good) — real queries on trial"
            )
        return recovered

    def _probe_sync(self) -> None:
        assert self._conn is not None
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.execute("COMMIT")
        finally:
            try:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            cur.close()

    def _canary_sync(self) -> None:
        """代表性投影形 canary（worker 线程内执行，FIX-CORR-3）。

        连接/游标纪律与 ``_probe_sync`` 完全一致（复用专属长连接
        ``self._conn``、BEGIN/COMMIT、finally rollback+close）；区别仅在
        SQL —— 它走与 projection.py 相同的无索引全扫 + TEMP B-TREE
        排序路径（见 ``_CANARY_SQL`` 注释）。schema 门已校验 ``s.id``
        等列；gate 拦截时本方法抛错 → probe 判失败保持 open（fail-safe
        方向正确）。
        """
        assert self._conn is not None
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN")
            cur.execute(self._CANARY_SQL)
            cur.fetchall()
            cur.execute("COMMIT")
        finally:
            try:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            cur.close()

    async def reprobe(self) -> bool:
        """禁用态重探（§1.4/§4.2）：重开 + 门。返回是否恢复。"""
        if not self._reprobe_allowed:
            return False
        self._counters["probes"] += 1
        await self._open_and_gate(why="reprobe")
        if self._state == "available":
            self._logger.info("dbaux reprobe succeeded — auxiliary recovered")
            return True
        return False

    # ------------------------------------------------------------------
    # 状态（B5 挂 health/metrics）
    # ------------------------------------------------------------------

    def _disable(self, *, reason: str) -> None:
        """禁用（§1.4-3）：不崩溃，全降级态；周期重探自愈。"""
        if reason != "stopped":  # 停机不属降级事件
            self._counters["disables"] += 1
        self._state = "disabled"
        self._reason = reason
        self._next_probe_at = None

    def trip_breaker(self) -> None:
        """显式熔断（P99 判定由 breaker 内部触发；本方法供状态联动）。"""
        if self._state != "circuit_open":
            self._counters["trips"] += 1
        self._state = "circuit_open"
        self._reason = "circuit_open"
        self._next_probe_at = self._clock() + self._probe_interval_s

    def _check_breaker_state(self) -> None:
        """breaker 内部 trip 后把状态联动到 circuit_open（query 路径调用）。"""
        if self._breaker.open and self._state == "available":
            self.trip_breaker()
            self._logger.warning(
                "dbaux circuit OPEN (latency over threshold) — degrading"
                " to http"
            )

    def status(self) -> DbAuxStatus:
        self._check_breaker_state()
        if self._state == "available":
            return DbAuxStatus(
                available=True,
                mode="db",
                reason=None,
                generation=self._generation,
            )
        return DbAuxStatus(
            available=False,
            mode="http",
            reason=self._reason,
            generation=self._generation,
        )

    def snapshot(self) -> dict[str, Any]:
        """完整快照（B5 metrics 消费；含 breaker 样本统计 + 事件计数器）。"""
        st = self.status()
        return {
            "available": st.available,
            "mode": st.mode,
            "reason": st.reason,
            "generation": st.generation,
            "breaker": self._breaker.snapshot(),
            "counters": dict(self._counters),
            "source": (
                self._resolution.source
                if isinstance(self._resolution, ResolvedPath)
                else None
            ),
            "path": (
                self._resolution.path
                if isinstance(self._resolution, ResolvedPath)
                else None
            ),
        }

    def _log_startup(self) -> None:
        res = self._resolution
        if isinstance(res, DisabledResolution):
            self._logger.info(
                "dbaux startup: disabled (reason=%s detail=%s)", res.reason, res.detail
            )
            return
        st = self.status()
        gate = (
            "pass" if st.available
            else f"fail(reason={st.reason}"
            + (f"; {self._reason_detail}" if self._reason_detail else "")
            + ")"
        )
        self._logger.info(
            "dbaux startup: path=%s source=%s warning=%s gate=%s "
            "auxiliary={available:%s mode:%s}",
            res.path,
            res.source,
            res.warning or "-",
            gate,
            st.available,
            st.mode,
        )

    # 测试支持 --------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection | None:
        """测试专用：暴露连接引用以断言 §2.3-7①（worker 外线程使用 →
        ``sqlite3.ProgrammingError``）。生产代码不得经此访问连接。"""
        return self._conn

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def breaker(self) -> LatencyBreaker:
        return self._breaker
