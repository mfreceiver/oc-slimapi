"""WAL 陈旧读 CI 测试——守护「immutable=1 完全弃用」决策（B0-6a）。

决策源：
- `docs/system-architecture-proposal-2026-08-17.md` 行 92（实证）与行 96（裁决）
- `docs/refactor-plans/slimapi-refactor-plan.md` §2.1 B0-6 (a)（执行方式/断言）

实证（行 92）：``immutable=1`` **不读** live ``-wal`` 内容——WAL 内已 commit
的行不可见（count=1 vs 2）、表建于 WAL 时甚至 ``no such table``；陈旧读
**不产生任何错误**（连接/查询均成功），探测链无法检测——这是静默数据
过期，非可用性降级。

守护裁决（行 96）：immutable **不作为主路径、不作为降级档**；主路径 =
``mode=ro``（普通只读连接，经 ``-shm`` 正常读 WAL 内容，与 live writer 共存）
+ ``PRAGMA query_only=ON`` 防御层。

构造要点（refactor-plan B0-6a / 工单 B0-6）：
- 临时目录建 SQLite 库：先（默认 journal 模式）建 ``sessions`` 表 + 第 1 行、
  commit 落**主库文件**；再 ``PRAGMA journal_mode=WAL``（既有页 checkpoint 进
  主库）——此后第 2 行 + WAL 期新表 ``wal_era`` 全部 commit 进 ``-wal``
  （主库文件不可见，未 checkpoint）。
- **writer 连接保持打开**：``-wal`` 文件存活依赖活跃连接；关闭可能触发
  auto-checkpoint 清空 ``-wal`` 使 case 失效（fixture teardown 统一关闭）。
- 每 case 断言写入对应断言语义（proposal 行 92 的 count 值 / 异常类型）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest


@pytest.fixture()
def wal_db(tmp_path: Path):
    """构造 WAL 模式、``-wal`` 内已有未 checkpoint 的已 commit 数据。

    - ``sessions``：主库文件快照 = 1 行（``s1``，切 WAL 前写入）；``-wal``
      内有第 2 行 ``s2``（已 commit）——immutable 读主库只见 1 行；ro 读 2 行。
    - ``wal_era``：WAL 期建的表 + 1 行（全在 ``-wal``）——immutable 连表都
      不可见；ro 可见。
    - writer 连接保持打开直至 teardown（保证 ``-wal``/``-shm`` 存活，防
      auto-checkpoint 清空）。
    """
    db_path = tmp_path / "opencode.db"
    writer = sqlite3.connect(db_path)
    second = None
    try:
        # 主库文件：表 + 第 1 行（默认 journal 模式，commit 直接落主库）
        writer.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        writer.execute(
            "INSERT INTO sessions (id, title) VALUES ('s1', 'first')"
        )
        writer.commit()

        # 切换 WAL：既有页收进主库；之后的写全部进 -wal
        (journal_mode,) = writer.execute("PRAGMA journal_mode=WAL").fetchone()
        assert journal_mode == "wal"

        # -wal 内：sessions 第 2 行 + WAL 期新表 wal_era（均已 commit）
        second = sqlite3.connect(db_path)
        second.execute(
            "INSERT INTO sessions (id, title) VALUES ('s2', 'second')"
        )
        second.execute(
            "CREATE TABLE wal_era (id TEXT PRIMARY KEY, note TEXT)"
        )
        second.execute(
            "INSERT INTO wal_era (id, note) VALUES ('w1', 'wal-only')"
        )
        second.commit()

        yield db_path
    finally:
        if second is not None:
            second.close()
        writer.close()


def _immutable_connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{quote(str(db_path))}?immutable=1", uri=True
    )


def _assert_immutable_stale_read(db_path: Path) -> None:
    """case1：immutable 陈旧读（脏读回退）——live ``-wal`` 内容不可见。

    守护 v2.2 行 96 immutable 弃用：陈旧读不产生任何错误（连接/查询均
    成功），探测链无法检测——静默数据过期，故不可作为主路径/降级档。
    """
    conn = _immutable_connect(db_path)
    try:
        count = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()
    # 主库文件快照只有 s1；s2 在 live -wal 内已 commit 但 immutable 不可见
    assert count == 1
    assert count < 2  # 真实已 commit 行数为 2


def _assert_immutable_missing_table(db_path: Path) -> None:
    """case2：immutable 表不可见——WAL 期建的表报 ``no such table``。

    守护 v2.2 行 96 immutable 弃用：表建于 WAL 时连表都不可见（比行级
    陈旧更严重，且同样静默——需显式查询才暴露 OperationalError）。
    """
    conn = _immutable_connect(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            conn.execute("SELECT count(*) FROM wal_era").fetchone()
    finally:
        conn.close()


def _assert_ro_reads_wal(db_path: Path) -> None:
    """case3：ro 主路径正常——经 ``-shm`` 读 WAL 内容（行 + 新表）。

    守护 v2.2 行 96 主路径裁决：``mode=ro`` 与 live writer 共存时可经
    ``-shm`` 读全量（含 ``-wal`` 内已 commit 行、WAL 期建的表）——替代
    immutable 无功能损失，故 immutable 可完全弃用。
    """
    conn = sqlite3.connect(
        f"file:{quote(str(db_path))}?mode=ro", uri=True
    )
    try:
        sessions = conn.execute(
            "SELECT count(*) FROM sessions"
        ).fetchone()[0]
        wal_era = conn.execute(
            "SELECT count(*) FROM wal_era"
        ).fetchone()[0]
    finally:
        conn.close()
    assert sessions == 2  # 含 -wal 内已 commit 的 s2
    assert wal_era == 1   # WAL 期建的表同样可见


_CASES = [
    pytest.param(
        "immutable_stale_read",
        id="case1-immutable-stale-read",
    ),
    pytest.param(
        "immutable_missing_table",
        id="case2-immutable-missing-table",
    ),
    pytest.param(
        "ro_reads_wal",
        id="case3-ro-reads-wal",
    ),
]


@pytest.mark.parametrize("case", _CASES)
def test_wal_immutable_disabled(case: str, wal_db: Path):
    """守护 v2.2 行 96「immutable=1 完全弃用」（出处见模块 docstring）。

    三 case 覆盖弃用依据两面 + 替代路径正确性（逐 case 断言语义见对应
    ``_assert_*`` docstring）：① immutable 陈旧读（脏读回退）；② immutable
    表不可见（WAL 期建表）；③ ro 路径正常读（主路径 = ``mode=ro`` 成立）。
    """
    if case == "immutable_stale_read":
        _assert_immutable_stale_read(wal_db)
    elif case == "immutable_missing_table":
        _assert_immutable_missing_table(wal_db)
    else:
        _assert_ro_reads_wal(wal_db)