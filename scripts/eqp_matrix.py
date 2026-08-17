#!/usr/bin/env python3
"""B0-6(b) EQP 全过滤矩阵 + 真库 P99 实证脚本（B0-5 设计文档配套）。

背景
----
v2.2 体系架构方案 §3.1（docs/system-architecture-proposal-2026-08-17.md）：
v4 /slimapi/sessions 的 DB 投影源（行 68-88 投影 SQL 模板、行 90-101 连接生命周期、
行 102-110 索引策略）。本脚本实证两件事：

  1. 48 组合全过滤矩阵的 planner 特征（archived 3 × parent 4 × cursor 2 × search 2）：
     在「sidecar 零索引」前提下，每组合跑 EXPLAIN QUERY PLAN + 实际执行，断言
     SCAN/SEARCH、USE TEMP B-TREE FOR ORDER BY 与返回行数（S-B08：断言结构特征，
     不断言 EQP 全文案——SQLite 版本文案会漂）。
  2. 真库无索引直跑基线：对 ~/.local/share/opencode/opencode.db（mode=ro + query_only=ON）
     跑同一投影 SQL 采样计时，输出 P50/P99（v2.2 行 106 基线 ~0.015ms 量级复测）。

SQL 语义与 v2.2 行 70-82 模板对齐，但列名以真库 PRAGMA table_info 为准（待裁决：
v2.2 行 72 tokens_in/tokens_out vs 真库 tokens_input/tokens_output；行 74 p.directory
vs 真库 project 表无 directory 列——join 列可经 --join-col 切换，默认 worktree 对齐
真库/上游 ProjectInfo，见 design-v4-dbaux.md §6）。

用法
----
  .venv/bin/python scripts/eqp_matrix.py --rows 1000 --out /tmp/eqp.json
  .venv/bin/python scripts/eqp_matrix.py --real-db [path] --reps 30 --out /tmp/eqp-real.json

逐组合断言（草稿库模式）全部通过退出码 0；真库模式为数据采集不断言。
临时库建在 /tmp 下，跑完自动清理（--keep 保留调试）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量（对齐 v2.2 行号 + 真库实测 schema）
# ---------------------------------------------------------------------------

# v2.2 行 146 schema 兼容门（全投影列版）：session 表投影列 + project 表 join 列
SESSION_PROJECTION_COLS = [
    "id", "parent_id", "project_id", "time_archived", "time_updated",
    "directory", "title", "agent", "model", "version",
    "summary_additions", "summary_deletions", "summary_files", "summary_diffs",
    "tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write",
    "time_created", "time_compacting", "revert", "permission", "metadata",
]
# 注：v2.2 行 72 模板列名 tokens_in/tokens_out——真库实为 tokens_input/tokens_output（待裁决 5）；
# summary_*/tokens_*/time_* 为通配展开后的真库列名（行 146）。
PROJECT_JOIN_COLS = ["id", "name", "worktree"]  # 契约冻结 project={id,name,worktree}；v2.2 行 74 project_directory 真库不存在（rev-1 实证）

DEFAULT_REAL_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")

# 投影 SQL 模板（v2.2 行 70-82 工程化：真实列名 + join 列含契约冻结 project.name；rev-1 修订）
SQL_TMPL = """
SELECT s.id, s.parent_id, s.time_archived, s.time_updated, s.directory, s.title,
       s.agent, s.model, s.version, s.summary_diffs,
       s.tokens_input, s.tokens_output, s.time_created,
       s.revert, s.permission, s.metadata,
       p.name AS project_name, p.worktree AS project_worktree
FROM session s LEFT JOIN project p ON s.project_id = p.id
WHERE {archived_pred} AND {parent_pred}
  AND (:search IS NULL OR s.title LIKE :search ESCAPE '\\')
  {cursor_pred}
ORDER BY s.time_updated DESC, s.id DESC
LIMIT :limit + 1
"""


@dataclass(frozen=True)
class Combo:
    archived: str   # omit | only | all
    parent: str     # all | none | only | <sid>
    cursor: bool
    search: bool

    @property
    def id(self) -> str:
        return f"a={self.archived}|p={self.parent}|c={'y' if self.cursor else 'n'}|s={'y' if self.search else 'n'}"


def all_combos() -> list[Combo]:
    return [
        Combo(archived, parent, cursor, search)
        for archived in ("omit", "only", "all")
        for parent in ("all", "none", "only", "s0000")
        for cursor in (False, True)
        for search in (False, True)
    ]


# ---------------------------------------------------------------------------
# 谓词构造（与 design-v4-dbaux.md §9 SQL 语义冻结一致）
# ---------------------------------------------------------------------------

def archived_pred(combo: Combo) -> str:
    return {
        "omit": "s.time_archived IS NULL",
        "only": "s.time_archived IS NOT NULL",
        "all":  "1=1",
    }[combo.archived]


def parent_pred(combo: Combo) -> str:
    return {
        "all":  "1=1",
        "none": "s.parent_id IS NULL",
        "only": "s.parent_id IS NOT NULL",
        "s0000": "s.parent_id = :parent_id",
    }[combo.parent]


def cursor_pred(combo: Combo) -> str:
    # v2.2 行 79-80：keyset 下推 (s.time_updated, s.id) < (:t, :i)（复合谓词，SQLite ≥3.15）
    return "AND (s.time_updated, s.id) < (:cursor_t, :cursor_i)" if combo.cursor else ""


def search_pattern(search_on: bool) -> Optional[str]:
    # with-search 组合用确定性子串：标题 "grp{i%4}-i" → "%grp1%" 命中 25% 行
    return "%grp1%" if search_on else None


def build_sql(combo: Combo, join_col: str = "worktree") -> str:
    if join_col != "worktree":
        raise ValueError(f"--join-col 仅支持 worktree（真库/上游 ProjectInfo 对齐，v2.2 行 74 directory 列已实证不存在，rev-1 关闭），got {join_col!r}")
    return SQL_TMPL.format(
        archived_pred=archived_pred(combo),
        parent_pred=parent_pred(combo),
        cursor_pred=cursor_pred(combo),
    )


# ---------------------------------------------------------------------------
# 草稿库构造（对齐真库 schema：投影列 + 主键；WAL 复刻上游 database.ts:27）
# ---------------------------------------------------------------------------

SESSION_DDL_COLS = [
    # (name, decl) —— 类型/约束对齐真库 PRAGMA table_info（INTEGER/TEXT + NOT NULL + PK）
    ("id", "TEXT PRIMARY KEY"),
    ("project_id", "TEXT NOT NULL"),
    ("parent_id", "TEXT"),
    ("directory", "TEXT NOT NULL"),
    ("title", "TEXT NOT NULL"),
    ("version", "TEXT NOT NULL"),
    ("summary_additions", "INTEGER"),
    ("summary_deletions", "INTEGER"),
    ("summary_files", "INTEGER"),
    ("summary_diffs", "TEXT"),
    ("revert", "TEXT"),
    ("permission", "TEXT"),
    ("time_created", "INTEGER NOT NULL"),
    ("time_updated", "INTEGER NOT NULL"),
    ("time_compacting", "INTEGER"),
    ("time_archived", "INTEGER"),
    ("agent", "TEXT"),
    ("model", "TEXT"),
    ("tokens_input", "INTEGER NOT NULL"),
    ("tokens_output", "INTEGER NOT NULL"),
    ("tokens_reasoning", "INTEGER NOT NULL"),
    ("tokens_cache_read", "INTEGER NOT NULL"),
    ("tokens_cache_write", "INTEGER NOT NULL"),
    ("metadata", "TEXT"),
]

PROJECT_DDL_COLS = [
    ("id", "TEXT PRIMARY KEY"),
    ("name", "TEXT"),
    ("worktree", "TEXT NOT NULL"),
]

SESSION_COL_NAMES = [c[0] for c in SESSION_DDL_COLS]
PROJECT_COL_NAMES = [c[0] for c in PROJECT_DDL_COLS]


def build_draft_db(rows: int, seed: int) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """构造临时 WAL 草稿库；返回 (path, session_rows, meta)。

    数据分布（确定性，seed 仅作将来扩展）：
    - 每 5 行 1 行根会话（parent_id NULL），其余 4 行挂同一根（父 = 最近 5 的倍数的 id）
    - title = "grp{i%4}-{i:04d}" → search "%grp1%" 命中 25%
    - 每 10 行 3 行归档（time_archived 非空）→ archived=omit 保留 70%，only 30%
    - project_id = "p{i%38}"，38 个 project 行（对齐真库 38 行），全部有效（LEFT JOIN 不缺行）
    - time_updated = base + i（单调递增，keyset 锚点取中位）
    """
    tmp = tempfile.mkdtemp(prefix="eqp_matrix_")
    db_path = os.path.join(tmp, "draft.db")

    base = 1_700_000_000
    session_rows: list[dict[str, Any]] = []
    for i in range(rows):
        parent_id = None if i % 5 == 0 else f"s{i - (i % 5):04d}"
        session_rows.append({
            "id": f"s{i:04d}",
            "project_id": f"p{i % 38:02d}",
            "parent_id": parent_id,
            "directory": f"/work/d{i % 10}",
            "title": f"grp{i % 4}-{i:04d}",
            "version": "1.18.18",
            "summary_additions": i % 7,
            "summary_deletions": i % 3,
            "summary_files": i % 2,
            "summary_diffs": '{"files":1,"additions":2,"deletions":3}',
            "revert": None,
            "permission": None,
            "time_created": base + i,
            "time_updated": base + i,
            "time_compacting": None,
            "time_archived": base + i if i % 10 < 3 else None,
            "agent": "agent-x",
            "model": "model-x",
            "tokens_input": 100 + i,
            "tokens_output": 50 + i,
            "tokens_reasoning": 0,
            "tokens_cache_read": 0,
            "tokens_cache_write": 0,
            "metadata": "{}",
        })
    project_rows = [{"id": f"p{i:02d}", "name": f"project-{i}", "worktree": f"/work/p{i}"} for i in range(38)]

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=WAL")  # 复刻上游 database.ts:27
        con.execute(
            "CREATE TABLE session ("
            + ", ".join(f'"{n}" {d}' for n, d in SESSION_DDL_COLS) + ")"
        )
        con.execute(
            "CREATE TABLE project ("
            + ", ".join(f'"{n}" {d}' for n, d in PROJECT_DDL_COLS) + ")"
        )
        con.executemany(
            "INSERT INTO session (" + ",".join(SESSION_COL_NAMES) + ") VALUES ("
            + ",".join("?" * len(SESSION_COL_NAMES)) + ")",
            [tuple(r[c] for c in SESSION_COL_NAMES) for r in session_rows],
        )
        con.executemany(
            "INSERT INTO project (" + ",".join(PROJECT_COL_NAMES) + ") VALUES ("
            + ",".join("?" * len(PROJECT_COL_NAMES)) + ")",
            [tuple(r[c] for c in PROJECT_COL_NAMES) for r in project_rows],
        )
        con.commit()
    finally:
        con.close()

    # keyset 锚点：全行按 (time_updated DESC, id DESC) 排序取中位行 —— (anchor_t, anchor_id)
    sorted_rows = sorted(session_rows, key=lambda r: (r["time_updated"], r["id"]), reverse=True)
    anchor = sorted_rows[len(sorted_rows) // 2]
    meta = {
        "rows": rows,
        "project_rows": len(project_rows),
        "archived_pct": 30,
        "roots": sum(1 for r in session_rows if r["parent_id"] is None),
        "search_match_pct": 25,
        "cursor_anchor": {"t": anchor["time_updated"], "id": anchor["id"]},
    }
    return db_path, session_rows, meta


def cleanup_draft(db_path: str, keep: bool) -> None:
    if not keep:
        shutil.rmtree(os.path.dirname(db_path), ignore_errors=True)


# ---------------------------------------------------------------------------
# 期望行 oracle（Python 内镜像 SQL 谓词——行集精确匹配断言基准）
# ---------------------------------------------------------------------------

def expected_window(rows: list[dict[str, Any]], combo: Combo, limit: int,
                    anchor: dict[str, Any]) -> tuple[int, list[str]]:
    """返回 (应返回行数, 期望前 K 个 id)。"""

    def _match(r: dict[str, Any]) -> bool:
        if combo.archived == "omit" and r["time_archived"] is not None:
            return False
        if combo.archived == "only" and r["time_archived"] is None:
            return False
        if combo.parent == "none" and r["parent_id"] is not None:
            return False
        if combo.parent == "only" and r["parent_id"] is None:
            return False
        if combo.parent == "s0000" and r["parent_id"] != "s0000":
            return False
        if combo.search and not r["title"].startswith("grp1-"):
            return False
        return True

    matched = [r for r in rows if _match(r)]
    matched.sort(key=lambda r: (r["time_updated"], r["id"]), reverse=True)  # ORDER BY 冻结
    if combo.cursor:
        matched = [r for r in matched if (r["time_updated"], r["id"]) <
                   (anchor["t"], anchor["id"])]
    k = min(len(matched), limit + 1)
    return k, [r["id"] for r in matched[:k]]


# ---------------------------------------------------------------------------
# EQP 解析 + 执行
# ---------------------------------------------------------------------------

def parse_eqp(text: str) -> dict[str, Any]:
    """从 EXPLAIN QUERY PLAN 文本提取结构性特征（S-B08：不断言全文案）。

    SQLite 查询计划使用 FROM 别名（`FROM session s` → "SCAN s"）；不同版本对
    别名/表名的呈现一致，但索引文案会漂（USE INDEX 名字等）。因此只断言
    SCAN/SEARCH 表访问形态 + 临时排序标记，均以正则匹配（接受别名或表名）。
    """
    import re

    def _access(tbl_alias: str, tbl_name: str) -> str:
        if re.search(rf"SEARCH {tbl_alias}\b", text) or re.search(rf"SEARCH {tbl_name}\b", text):
            return f"SEARCH {tbl_name}"
        if re.search(rf"SCAN {tbl_alias}\b", text) or re.search(rf"SCAN {tbl_name}\b", text):
            return f"SCAN {tbl_name}"
        return "?"

    return {
        "session_access": _access("s", "session"),
        "project_access": _access("p", "project"),
        "temp_b_tree_order_by": "USE TEMP B-TREE FOR ORDER BY" in text,
    }


def bind_params(combo: Combo, search_on: bool, anchor: dict[str, Any], limit: int) -> dict[str, Any]:
    return {
        "search": search_pattern(search_on),
        "parent_id": "s0000",
        "cursor_t": anchor["t"],
        "cursor_i": anchor["id"],
        "limit": limit,
    }


def run_combo(con: sqlite3.Connection, combo: Combo, anchor: dict[str, Any],
              limit: int, join_col: str) -> dict[str, Any]:
    sql = build_sql(combo, join_col)
    params = bind_params(combo, combo.search, anchor, limit)

    eqp_text = con.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    eqp = parse_eqp("\n".join(row[3] for row in eqp_text))

    t0 = time.perf_counter_ns()
    fetched = con.execute(sql, params).fetchall()
    t1 = time.perf_counter_ns()

    return {
        "combo": combo.id,
        "archived": combo.archived,
        "parent": combo.parent,
        "cursor": combo.cursor,
        "search": combo.search,
        "eqp": eqp,
        "elapsed_ms": (t1 - t0) / 1e6,
        "rows_returned": len(fetched),             # LIMIT+1 上限内的实际行数
        "first_ids": [row[0] for row in fetched],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_draft_matrix(rows: int, limit: int, join_col: str, seed: int,
                     keep: bool) -> dict[str, Any]:
    db_path, session_rows, meta = build_draft_db(rows, seed)
    try:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA query_only=ON")  # 防御层（v2.2 行 94）
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for combo in all_combos():
            res = run_combo(con, combo, meta["cursor_anchor"], limit, join_col)
            exp_count, exp_ids = expected_window(session_rows, combo, limit, meta["cursor_anchor"])
            res["rows_expected"] = exp_count
            res["expected_first_ids"] = exp_ids
            res["rowcount_ok"] = res["rows_returned"] == exp_count
            res["order_ok"] = res["first_ids"] == exp_ids
            res["assert_scan"] = res["eqp"]["session_access"] == "SCAN session"
            res["assert_temp_b_tree"] = res["eqp"]["temp_b_tree_order_by"] is True
            res["passed"] = all([
                res["rowcount_ok"], res["order_ok"],
                res["assert_scan"], res["assert_temp_b_tree"],
            ])
            if not res["passed"]:
                failures.append({k: v for k, v in res.items() if k != "first_ids" and k != "expected_first_ids"})
            results.append(res)
        con.close()

        passed = sum(1 for r in results if r["passed"])
        report = {
            "mode": "draft",
            "rows": rows,
            "limit": limit,
            "limit_plus_one": limit + 1,
            "seed": seed,
            "combo_count": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "assertions": {
                "no_user_index_expect": "SCAN session + USE TEMP B-TREE FOR ORDER BY（sidecar 零索引直跑，v2.2 行 106）",
            },
            "planner_summary": {
                "scan_session_combos": sum(1 for r in results if r["eqp"]["session_access"] == "SCAN session"),
                "search_session_combos": sum(1 for r in results if r["eqp"]["session_access"] == "SEARCH session"),
                "temp_b_tree_combos": sum(1 for r in results if r["eqp"]["temp_b_tree_order_by"]),
            },
            "combos": results,
            "draft_meta": meta,
        }
        if failures:
            print("FAILURES:", json.dumps(failures, ensure_ascii=False, indent=2))
        return report
    finally:
        cleanup_draft(db_path, keep)


def run_real_db(real_db: str, limit: int, reps: int, join_col: str,
                out: Optional[str]) -> dict[str, Any]:
    if not os.path.exists(real_db):
        sys.exit(f"ERROR: real DB not found: {real_db}")
    uri = f"file:{real_db}?mode=ro"   # 真库只读铁律（绝不写）
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")   # 防御层（v2.2 行 94）
    con.execute("PRAGMA busy_timeout=5000")  # 与上游 database.ts:29 对齐

    # schema 兼容门核对（v2.2 行 146 全投影列版）——记录，不写
    session_cols = {r[1] for r in con.execute("PRAGMA table_info(session)").fetchall()}
    project_cols = {r[1] for r in con.execute("PRAGMA table_info(project)").fetchall()}
    session_rows = con.execute("SELECT count(*) FROM session").fetchone()[0]
    missing_session = [c for c in SESSION_PROJECTION_COLS if c not in session_cols]
    missing_project = [c for c in PROJECT_JOIN_COLS if c not in project_cols]
    gate = {
        "session_cols_total": len(session_cols),
        "project_cols_total": len(project_cols),
        "session_rows": session_rows,
        "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0],
        "missing_projection_cols": missing_session,
        "missing_project_join_cols": missing_project,
        "gate_passes": not missing_session and not missing_project,
    }

    # 48 组合：EQP 记录 + 采样计时（warmup 3 + reps 次）
    results: list[dict[str, Any]] = []
    samples_ns: list[int] = []
    for combo in all_combos():
        sql = build_sql(combo, join_col)
        # 锚点：真库 time_updated 中位行（只读采样）
        mids = con.execute(
            "SELECT time_updated, id FROM session ORDER BY time_updated DESC, id DESC "
            f"LIMIT 1 OFFSET {max(session_rows // 2 - 1, 0)}"
        ).fetchone()
        anchor = {"t": mids[0], "id": mids[1]}
        params = bind_params(combo, combo.search, anchor, limit)

        eqp_text = con.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        eqp = parse_eqp("\n".join(row[3] for row in eqp_text))

        for _ in range(3):  # warmup
            con.execute(sql, params).fetchall()
        combo_samples = []
        for _ in range(reps):
            t0 = time.perf_counter_ns()
            rows_n = len(con.execute(sql, params).fetchall())
            t1 = time.perf_counter_ns()
            combo_samples.append(t1 - t0)
        samples_ns.extend(combo_samples)
        results.append({
            "combo": combo.id,
            "eqp": eqp,
            "rows_returned": rows_n,
            "p50_ms": statistics.median(combo_samples) / 1e6,
            "p99_ms": _percentile(combo_samples, 99) / 1e6,
            "max_ms": max(combo_samples) / 1e6,
        })

    con.close()

    all_p99 = _percentile(samples_ns, 99) / 1e6
    report = {
        "mode": "real_db",
        "db_path": real_db,
        "gate": gate,
        "limit": limit,
        "reps_per_combo": reps,
        "combos": results,
        "aggregate": {
            "samples": len(samples_ns),
            "p50_ms": statistics.median(samples_ns) / 1e6,
            "p99_ms": all_p99,
            "mean_ms": statistics.mean(samples_ns) / 1e6,
            "min_ms": min(samples_ns) / 1e6,
            "max_ms": max(samples_ns) / 1e6,
        },
        # 无索引直跑基线印证（v2.2 行 106：~0.015ms 量级；planner 无 time_updated 索引事实）
        "baseline_note": "session 表无 time_updated 索引（真库 PRAGMA index_list 实测），keyset 排序由 ORDER BY 承担（恒成立，行 104）",
    }
    return report


def _percentile(samples: list[int], p: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(len(s) * p / 100.0)))
    return float(s[k])


def main() -> int:
    ap = argparse.ArgumentParser(description="B0-6(b) EQP 全矩阵 + 真库 P99 实证（design-v4-dbaux.md 配套）")
    ap.add_argument("--rows", type=int, default=1000, help="草稿库 session 行数（默认 1000）")
    ap.add_argument("--limit", type=int, default=100, help="v4 limit（SQL 内 LIMIT limit+1，默认 100）")
    ap.add_argument("--out", type=str, default="/tmp/eqp.json", help="JSON 报告路径")
    ap.add_argument("--seed", type=int, default=0, help="rng seed（预留）")
    ap.add_argument("--keep", action="store_true", help="保留临时草稿库（调试）")
    ap.add_argument("--join-col", default="worktree",
                    help="project join 列（B0 已实证冻结 worktree：真库 project 无 directory 列；name/worktree 均已进 SELECT 与 schema 门）")
    ap.add_argument("--real-db", nargs="?", const=DEFAULT_REAL_DB, default=None,
                    metavar="PATH", help="真库 mode=ro 采样模式（可选；默认路径 ~/.local/share/opencode/opencode.db）")
    ap.add_argument("--reps", type=int, default=30, help="真库模式每组合采样次数（warmup 3）")
    args = ap.parse_args()

    if args.rows < 10:
        sys.exit("ERROR: --rows 必须 >= 10（保证归档/search/keyset 分布有效）")

    if args.real_db:
        report = run_real_db(args.real_db, args.limit, args.reps, args.join_col, args.out)
        print(f"[real_db] {args.real_db}")
        print(f"  schema gate: {report['gate']}")
        print(f"  48 组合采样 {report['aggregate']['samples']} 次 → "
              f"P50={report['aggregate']['p50_ms']:.4f}ms  P99={report['aggregate']['p99_ms']:.4f}ms  "
              f"mean={report['aggregate']['mean_ms']:.4f}ms  max={report['aggregate']['max_ms']:.4f}ms")
        scan_n = sum(1 for r in report["combos"] if r["eqp"]["session_access"] == "SCAN session")
        search_n = sum(1 for r in report["combos"] if r["eqp"]["session_access"] == "SEARCH session")
        tb_n = sum(1 for r in report["combos"] if r["eqp"]["temp_b_tree_order_by"])
        print(f"  EQP 特征：SCAN session×{scan_n}  SEARCH session×{search_n}  临时排序×{tb_n}")
        if report["gate"]["gate_passes"]:
            print("  schema 门：通过（投影列 + project join 列齐全）")
    else:
        report = run_draft_matrix(args.rows, args.limit, args.join_col, args.seed, args.keep)
        print(f"[draft] rows={args.rows} limit={args.limit} (SQL LIMIT {args.limit + 1})")
        print(f"  48 组合断言：PASS {report['passed']} / FAIL {report['failed']}")
        print(f"  planner 特征：SCAN session×{report['planner_summary']['scan_session_combos']}  "
              f"SEARCH session×{report['planner_summary']['search_session_combos']}  "
              f"临时排序×{report['planner_summary']['temp_b_tree_combos']}")
        if report["failed"]:
            sys.exit(f"exit 1: {report['failed']} combos failed assertions")
        print("  OK：全部组合 SCAN + USE TEMP B-TREE FOR ORDER BY + 行集精确匹配")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"报告已写: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())