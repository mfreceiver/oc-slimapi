"""§9.5 SQL 语义测试（B3a-B2）：19 case 逐条对照镜像 oracle。

行集断言基准 = tests/v4_fixture.mirror_page（S-B03 独立镜像实现，
不 import 生产谓词函数）。⑬⑭ 的 503/降级路径归 B4 路由层；此处测
判定函数（has_wildcard）与 pattern 构造。
"""

from __future__ import annotations

import sqlite3

import pytest

from oc_slimapi.dbaux import (
    build_sessions_query,
    escape_like,
    has_wildcard,
    normalized_search,
    rows_to_records,
)
from oc_slimapi.dbaux.cursor import search_hash

from v4_fixture import build_fixture_db, mirror_page

ALL = dict(archived="all", parent="all", limit=100)


def _fetch(db_path, **kwargs) -> list[dict]:
    query = build_sessions_query(**{**ALL, **kwargs})
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        rows = con.execute(query.sql, query.params).fetchall()
    finally:
        con.close()
    return rows_to_records(rows)


def _ids(records: list[dict]) -> list[str]:
    return [r["id"] for r in records]


# --- ①-④ search 转义四连（§9.5 ①-④） ----------------------------------


def test_search_plain_substring(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, search="plain")
    expected, _ = mirror_page(**{**ALL, "search": "plain"})
    # ASCII 大小写折叠（SQLite LIKE 默认）：plain / PLAIN TITLE UPPER 双向命中
    assert _ids(got) == _ids(expected)
    assert set(_ids(got)) == {"ses_root_1", "ses_root_2", "ses_case_fold"}


def test_search_percent_literal(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, search="100%")
    expected, _ = mirror_page(**{**ALL, "search": "100%"})
    assert _ids(got) == _ids(expected)
    # 字面 %：命中 "fix the 100% bug"；"1004 percent four" 不含字面 "100%"
    assert _ids(got) == ["ses_child_a"]


def test_search_underscore_literal(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, search="under_score")
    expected, _ = mirror_page(**{**ALL, "search": "under_score"})
    assert _ids(got) == _ids(expected)
    # _ 失去单字符通配语义："an underscore dir"（无下划线）不命中
    assert _ids(got) == ["ses_child_b"]


def test_search_backslash_literal(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, search="back\\slash")
    expected, _ = mirror_page(**{**ALL, "search": "back\\slash"})
    assert _ids(got) == _ids(expected)
    assert _ids(got) == ["ses_child_c"]


# --- ⑤-⑦ allowlist 子树三连（§9.5 ⑤-⑦） --------------------------------


def test_allowlist_subtree_excludes_sibling_prefix(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, allowlist=("/foo",))
    expected, _ = mirror_page(**{**ALL, "allowlist": ("/foo",)})
    assert _ids(got) == _ids(expected)
    dirs = {r["directory"] for r in got}
    assert all(d == "/foo" or d.startswith("/foo/") for d in dirs)
    assert "/foobar" not in dirs


def test_allowlist_narrowing_deeper_subtree(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, allowlist=("/foo/bar",))
    expected, _ = mirror_page(**{**ALL, "allowlist": ("/foo/bar",)})
    assert _ids(got) == _ids(expected)
    assert {r["directory"] for r in got} == {"/foo/bar/deep"}


def test_allowlist_multi_union(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, allowlist=("/a", "/b"))
    expected, _ = mirror_page(**{**ALL, "allowlist": ("/a", "/b")})
    assert _ids(got) == _ids(expected)
    dirs = {r["directory"] for r in got}
    assert dirs == {"/a", "/a/%20b/c", "/a/dir_%_x", "/b", "/b/sub", "/b/late"}


# --- ⑧-⑨ complete 边界（§9.5 ⑧-⑨） -------------------------------------


async def test_complete_at_exact_limit(tmp_path):
    # 经 B1 query() 通道端到端：LIMIT+1 同窗口 complete 判定
    # 数据集 23 原始行（含 1 行 JSON 坏行）→ limit=23 恰好全窗 → True
    from oc_slimapi.dbaux import DbAuxiliarySource, fetch_sessions_page
    from oc_slimapi.dbaux.path_resolution import ResolvedPath

    db = build_fixture_db(tmp_path / "s.db")
    src = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    await src.start()
    try:
        page = await fetch_sessions_page(src, archived="all", parent="all", limit=23)
        assert page.complete is True  # ⑧ 恰好 limit → true
        assert len(page.records) == 22  # 23 原始 − 1 坏 JSON 行
        page2 = await fetch_sessions_page(src, archived="all", parent="all", limit=22)
        assert page2.complete is False  # ⑨ limit+1 行命中 → false（坏行保守计入）
        assert len(page2.records) == 21
        # 同 limit 镜像一致性（含 complete）
        for limit in (23, 22, 5, 1):
            got = await fetch_sessions_page(src, archived="all", parent="all", limit=limit)
            exp_records, exp_complete = mirror_page(archived="all", parent="all", limit=limit)
            assert got.complete is exp_complete
            assert [r["id"] for r in got.records] == [r["id"] for r in exp_records]
    finally:
        await src.stop()


def _fetch_limit(db_path, limit):
    query = build_sessions_query(archived="all", parent="all", limit=limit)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(query.sql, query.params).fetchall()
    finally:
        con.close()
    return rows_to_records(rows)


def test_complete_boundary_via_raw_window(tmp_path):
    # 不经 source 的同窗口判定等价检查：LIMIT+1 行判定与镜像一致
    db = build_fixture_db(tmp_path / "s.db")
    expected, complete = mirror_page(**{**ALL, "limit": 3})
    got3 = _fetch_limit(db, 3)
    assert len(got3) == 4  # 原始窗口 = limit+1（complete:false 的来源）
    assert [r["id"] for r in got3[:3]] == [r["id"] for r in expected]
    assert complete is False  # 23 > 3 → false


# --- ⑩-⑪ legacy 空 directory（§9.5 ⑩-⑪） -------------------------------


def test_legacy_empty_directory_present_without_allowlist(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db)
    assert "ses_legacy_empty" in _ids(got)
    expected, _ = mirror_page(**ALL)
    assert _ids(got) == _ids(expected)


def test_legacy_empty_directory_excluded_by_allowlist(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, allowlist=("/foo",))
    assert "ses_legacy_empty" not in _ids(got)
    expected, _ = mirror_page(**{**ALL, "allowlist": ("/foo",)})
    assert _ids(got) == _ids(expected)


# --- ⑫ keyset 下界（§9.5 ⑫） --------------------------------------------


def test_keyset_floor_at_earliest_anchor(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, cursor=(0, "ses_time_zero"))  # 全序最小 (t,id)
    assert got == []
    expected, complete = mirror_page(**{**ALL, "cursor": (0, "ses_time_zero")})
    assert expected == [] and complete is True


# --- ⑬⑭ search×降级判定（§9.5 ⑬⑭；503 归 B4，此处测判定+构造） --------


def test_has_wildcard_determinations():
    assert has_wildcard("100%") is True
    assert has_wildcard("a_b") is True
    assert has_wildcard("back\\slash") is True
    assert has_wildcard("plain") is False
    assert has_wildcard("") is False
    assert has_wildcard(None) is False
    # 确定性：同输入两次执行一致
    assert has_wildcard("50%_off\\") == has_wildcard("50%_off\\")


def test_pattern_construction_escapes_literals():
    # archived=all/parent=all 时 search 谓词 binds 位于 params[0]/[1]
    q_wild = build_sessions_query(**{**ALL, "search": "100%"})
    pattern = q_wild.params[0]
    assert pattern == "%100\\%%"
    assert q_wild.params[1] == pattern
    assert q_wild.params[-1] == 100  # limit 尾参
    q_plain = build_sessions_query(**{**ALL, "search": "plain"})
    assert q_plain.params[0] == "%plain%"
    q_none = build_sessions_query(**ALL)
    assert q_none.params[0] is None and q_none.params[1] is None
    assert escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"


# --- ⑮⑯⑰ allowlist 二进制前缀边界（§9.5 ⑮⑯⑰） -------------------------


def test_allowlist_binary_case_sensitive(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, allowlist=("/Foo",))
    expected, _ = mirror_page(**{**ALL, "allowlist": ("/Foo",)})
    assert _ids(got) == _ids(expected)
    assert {r["directory"] for r in got} == {"/Foo/child"}  # /foo 树不命中


def test_allowlist_root_matches_all_absolute(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, allowlist=("/",))
    expected, _ = mirror_page(**{**ALL, "allowlist": ("/",)})
    assert _ids(got) == _ids(expected)
    dirs = {r["directory"] for r in got}
    assert "" not in dirs
    assert all(d.startswith("/") for d in dirs)


def test_allowlist_literal_percent_underscore_segments(tmp_path):
    db = build_fixture_db(tmp_path / "s.db")
    got = _fetch(db, allowlist=("/a",))
    expected, _ = mirror_page(**{**ALL, "allowlist": ("/a",)})
    assert _ids(got) == _ids(expected)
    dirs = {r["directory"] for r in got}
    # /a 子树含字面 %/_ 段的目录照常命中；同层异名 /a%20b/c 不命中
    assert dirs == {"/a", "/a/%20b/c", "/a/dir_%_x"}
    assert "/a%20b/c" not in dirs


# --- ⑱⑲ 指纹确定性（§9.5 ⑱⑲） ------------------------------------------


def test_search_hash_deterministic():
    a = search_hash(normalized_search("  fix the 100% bug  "))
    b = search_hash(normalized_search("fix the 100% bug"))
    assert a == b  # trim 后唯一输入源
    assert a != search_hash("fix the 100% bug!")  # 输入变 → 指纹变
    assert search_hash(None) == ""  # 哨兵
    assert len(a) == 16


def test_normalized_search_single_source():
    assert normalized_search("  x  ") == "x"
    assert normalized_search(None) is None
    assert normalized_search("") == ""
    with pytest.raises(TypeError):
        normalized_search(123)
