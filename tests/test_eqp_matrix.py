"""EQP 矩阵测试（B3a-B2）：48 组合 planner 特征断言。

复用 scripts/eqp_matrix.py（B0 冻结）的 fixture 构造 / 行集 oracle /
EQP 解析——import 复用，禁复制粘贴漂移。被测对象 = B2 组装器
``build_sessions_query`` 产出的**真实 SQL**（draft DB 无索引 → SCAN +
TEMP B-TREE 全组合成立；真库 SEARCH 差异见 B0 记录，属上游索引面）。
"""

from __future__ import annotations

import sqlite3

import pytest

from oc_slimapi.dbaux import build_sessions_query, rows_to_records

from v4_fixture import load_eqp_matrix

LIMIT = 25
ROWS = 600
SEED = 0


@pytest.fixture(scope="module")
def draft():
    eqp = load_eqp_matrix()
    db_path, session_rows, meta = eqp.build_draft_db(rows=ROWS, seed=SEED)
    # R5 BLOCKER-1 连带：model 现为生产 JSON 解析列（真库 drizzle json
    # 列）；B0 草稿库写纯文本 "model-x" 会让 rows_to_records 按 §8 全跳。
    # scripts/eqp_matrix.py B0 冻结不改——测试侧归一为合法 JSON 文本。
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "UPDATE session SET model = "
            "'{\"id\":\"' || model || '\",\"providerID\":\"prov-draft\"}'"
        )
        con.commit()
    finally:
        con.close()
    yield eqp, db_path, session_rows, meta
    eqp.cleanup_draft(db_path, keep=False)


def _kwargs(combo, anchor):
    return dict(
        archived=combo.archived,
        parent=combo.parent,
        search="grp1" if combo.search else None,
        cursor=(anchor["t"], anchor["id"]) if combo.cursor else None,
        limit=LIMIT,
    )


@pytest.mark.parametrize("combo_id", [c.id for c in load_eqp_matrix().all_combos()])
def test_eqp_matrix_48_combos(draft, combo_id):
    eqp, db_path, session_rows, meta = draft
    combo = next(c for c in eqp.all_combos() if c.id == combo_id)
    anchor = meta["cursor_anchor"]

    query = build_sessions_query(**_kwargs(combo, anchor))
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        plan_rows = con.execute(
            "EXPLAIN QUERY PLAN " + query.sql, query.params
        ).fetchall()
        fetched = con.execute(query.sql, query.params).fetchall()
    finally:
        con.close()

    plan_text = "\n".join(" ".join(str(cell) for cell in row[3:]) for row in plan_rows)
    features = eqp.parse_eqp(plan_text)

    # planner 特征（draft DB 无索引）：SCAN session + TEMP B-TREE FOR ORDER BY
    assert features["session_access"] == "SCAN session", plan_text
    assert features["temp_b_tree_order_by"] is True, plan_text

    # 行集精确匹配 oracle（B0 镜像实现——limit+1 窗口同界）
    expected_count, expected_ids = eqp.expected_window(
        session_rows, combo, LIMIT, anchor
    )
    got_ids = [row[0] for row in fetched]
    assert got_ids == expected_ids
    assert len(got_ids) == expected_count

    # 组装容忍管道不改变行集（draft 数据 JSON 全合法 → 零跳行）
    assert [r["id"] for r in rows_to_records(fetched)] == expected_ids
