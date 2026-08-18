"""等价性锚定（design-v4-dbaux §10）：EQ-001..EQ-008。

权威源混合（§10.4）：
- ① 真实 opencode 进程 = 发布门（CI 不可用；本文件落 skip-if-no-server
  框架，EQ-007）；
- ② 版本标记 golden JSON = 日常 CI 默认——tests/golden/
  sessions-global-v1.18.18.json，由 tests/v4_fixture.py 的**镜像 oracle**
  生成（生成器 mirror-oracle-v1；非 sidecar SQL 自证——S-B03）。

行集 oracle = tests/v4_fixture.mirror_page（独立谓词/排序/翻页实现）。
"""

from __future__ import annotations

import os

import httpx
import pytest

from oc_slimapi.dbaux import DbAuxiliarySource, fetch_sessions_page, ROW_KEYS
from oc_slimapi.dbaux.lifecycle import AuxiliaryUnavailableError
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.skeleton import SESSION_KEYS, project_rows_to_v4_skeletons

from v4_fixture import (
    ALIGNED_VERSION,
    DATASET,
    build_fixture_db,
    build_golden_document,
    dataset_fingerprint,
    load_golden,
    mirror_page,
    validate_golden,
)

FULL = dict(archived="all", parent="all", limit=100)


async def _start(tmp_path, **db_kwargs) -> tuple[DbAuxiliarySource, object]:
    db = build_fixture_db(tmp_path / "eq.db", **db_kwargs)
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available, f"fixture db must start: {status.reason}"
    return source, status


def _ids(records) -> list[str]:
    return [r["id"] for r in records]


# --- EQ-001 golden 全量（行集 × 字段 × 序 × N 窗口） ----------------------


async def test_eq001_full_listing_matches_golden(tmp_path):
    source, _ = await _start(tmp_path)
    try:
        golden = load_golden()
        page = await fetch_sessions_page(source, archived="all", parent="all",
                                         limit=len(DATASET) + 10)
        assert page.complete is True
        assert page.records == golden["sessions"]  # 行集×字段×序 全等
        assert [r["id"] for r in page.records] == [
            r["id"] for r in golden["sessions"]
        ]
        # golden 头部契约（§10.6：version / fingerprint / manifest）
        assert golden["version"] == ALIGNED_VERSION
        assert golden["fingerprint"] == dataset_fingerprint()
        assert golden["dataset_manifest"]["sessions"] == len(DATASET)
    finally:
        await source.stop()


# --- EQ-002 cursor 逐页拼接 ≡ 全量 ---------------------------------------


async def test_eq002_cursor_paging_concat_equals_full(tmp_path):
    source, _ = await _start(tmp_path)
    try:
        limit = 5
        seen: list[dict] = []
        cursor = None
        for _ in range(30):
            page = await fetch_sessions_page(
                source, archived="all", parent="all",
                limit=limit, cursor=cursor,
            )
            seen.extend(page.records)
            if page.complete or not page.records:
                break
            last = page.records[-1]
            cursor = (last["time_updated"], last["id"])
        full, complete = mirror_page(**FULL)
        assert complete is True
        assert seen == full  # 拼接（含字段）与全量严格相等
        assert len(seen) == len(full) == 22  # 23 原始 − 1 坏 JSON 行
    finally:
        await source.stop()


# --- EQ-003 过滤维度矩阵（SQL ≡ 镜像 oracle） -----------------------------

FILTER_CASES = [
    dict(archived=arch, parent=parent)
    for arch in ("omit", "only", "all")
    for parent in ("all", "none", "only", "ses_root_1")
] + [
    dict(archived="all", parent="all", search="plain"),
    dict(archived="all", parent="all", search="100%"),
    dict(archived="all", parent="all", allowlist=("/foo",)),
    dict(archived="only", parent="only", allowlist=("/a", "/b")),
    dict(archived="omit", parent="ses_root_3", search="dir"),
    dict(archived="all", parent="all", cursor=None, limit=7),
]


@pytest.mark.parametrize("case", FILTER_CASES)
async def test_eq003_filter_dimensions_match_mirror(tmp_path, case):
    source, _ = await _start(tmp_path)
    try:
        kwargs = {**FULL, **case}
        page = await fetch_sessions_page(source, **kwargs)
        expected, complete = mirror_page(**kwargs)
        assert page.complete is complete
        assert page.records == expected
    finally:
        await source.stop()


# --- EQ-004 tie-break：同 time_updated 内 id DESC ------------------------


async def test_eq004_tiebreak_id_desc(tmp_path):
    source, _ = await _start(tmp_path)
    try:
        page = await fetch_sessions_page(source, archived="all", parent="all",
                                         limit=100)
        ids = _ids(page.records)
        # t=8000 组：tie_c > tie_b > tie_a（字典序 DESC，archived 不参与排序）
        assert ids.index("ses_tie_c") < ids.index("ses_tie_b") < ids.index("ses_tie_a")
        # t=6000 组：child_c > child_b > child_a
        assert (ids.index("ses_child_c") < ids.index("ses_child_b")
                < ids.index("ses_child_a"))
        expected, _ = mirror_page(**FULL)
        assert ids == _ids(expected)
    finally:
        await source.stop()


# --- EQ-005 complete 两侧（N vs N+1 窗口） -------------------------------


async def test_eq005_complete_window_semantics(tmp_path):
    source, _ = await _start(tmp_path)
    try:
        # 用 archived=only（不含坏 JSON 行 → 原始行数 = 合法行数）钉死
        # 「恰好 N → complete:true / N-1 → false」两侧
        expected_only, _ = mirror_page(archived="only", parent="all", limit=100)
        n = len(expected_only)
        assert n == 4  # root_2 / a_us / tie_b / archived_child
        at_n = await fetch_sessions_page(source, archived="only", parent="all",
                                         limit=n)
        assert at_n.complete is True
        assert _ids(at_n.records) == _ids(expected_only)
        below = await fetch_sessions_page(source, archived="only", parent="all",
                                          limit=n - 1)
        assert below.complete is False
        assert len(below.records) == n - 1
        mirror_below, mirror_complete = mirror_page(archived="only", parent="all",
                                                    limit=n - 1)
        assert mirror_complete is False
        assert _ids(below.records) == _ids(mirror_below)
        # 对照：omit 集含坏行 → 原始窗口 > 合法行数 → 合法行数作 limit 时
        # complete 恒 false（§8 窗口后容忍的保守语义，与镜像一致）
        expected_omit, _ = mirror_page(archived="omit", parent="all", limit=100)
        at_valid = await fetch_sessions_page(source, archived="omit", parent="all",
                                             limit=len(expected_omit))
        assert at_valid.complete is False
        _, mirror_complete_omit = mirror_page(archived="omit", parent="all",
                                              limit=len(expected_omit))
        assert mirror_complete_omit is False
    finally:
        await source.stop()


# --- EQ-006 逐字段（含可选列 空 vs 置值 / project null vs 对象） ----------


async def test_eq006_field_by_field_optional_columns(tmp_path):
    source, _ = await _start(tmp_path)
    try:
        page = await fetch_sessions_page(source, archived="all", parent="all",
                                         limit=100)
        by_id = {r["id"]: r for r in page.records}
        dataset_by_id = {r["id"]: r for r in DATASET}

        for sid, record in by_id.items():
            row = dataset_by_id[sid]
            for key in ROW_KEYS[:-3]:  # session 24 列逐字段
                expected_value = row[key]
                if key in ("summary_diffs", "revert", "permission", "metadata"):
                    # JSON 列：DB 侧返回解析后对象，数据集是原始字符串
                    expected_value = (
                        __import__("json").loads(expected_value)
                        if isinstance(expected_value, str) else expected_value
                    )
                assert record[key] == expected_value, f"{sid}.{key}"

        # 可选列 空 vs 置值
        assert by_id["ses_root_1"]["revert"] is None
        assert by_id["ses_revert_full"]["revert"] == {
            "messageID": "msg_9", "partID": "prt_9",
        }
        assert by_id["ses_revert_full"]["permission"] == {"session": "ask"}
        assert by_id["ses_revert_full"]["metadata"] == {"k": "v"}
        assert by_id["ses_root_3"]["time_compacting"] == 1234
        assert by_id["ses_root_1"]["time_compacting"] is None
        # project join：对象 vs null（§8）
        assert by_id["ses_root_1"]["p_id"] == "prj_alpha"
        assert by_id["ses_root_1"]["p_name"] == "alpha"
        assert by_id["ses_root_1"]["p_worktree"] == "/wt/alpha"
        assert by_id["ses_orphan_proj"]["p_id"] is None
        assert by_id["ses_orphan_proj"]["p_name"] is None
        assert by_id["ses_orphan_proj"]["p_worktree"] is None
        # 极端时间戳透传（毫秒整数）
        assert by_id["ses_time_zero"]["time_updated"] == 0
        assert by_id["ses_time_now"]["time_updated"] > 1_787_000_000_000
        # 坏 JSON 行被组装容忍跳过
        assert "ses_bad_json" not in by_id
    finally:
        await source.stop()


# --- SessionSkeletonV4 投影（交付物 2 的锚定） ----------------------------


def test_v4_skeleton_shape_and_project_object():
    golden = load_golden()
    skeletons = project_rows_to_v4_skeletons(golden["sessions"])
    assert len(skeletons) == len(golden["sessions"])
    by_id = {s["id"]: s for s in skeletons}
    base = by_id["ses_root_1"]
    assert set(SESSION_KEYS) <= set(base)  # v3 投影键全保留
    assert base["parentID"] is None
    assert base["time"] == {"created": 1000, "updated": 5000, "archived": None}
    assert set(base["summary"]) == {"additions", "deletions", "files"}
    assert base["project"] == {"id": "prj_alpha", "name": "alpha",
                               "worktree": "/wt/alpha"}
    # v4-only：tokens 五列平铺（键名 = 真库列名，R2）
    for key in ("tokens_input", "tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write"):
        assert key in base
    # join 缺行 → project null（session.ts:595 ?? null 同语义）
    assert by_id["ses_orphan_proj"]["project"] is None
    # revert 子投影仅保留 messageID/partID
    assert by_id["ses_revert_full"]["revert"] == {
        "messageID": "msg_9", "partID": "prt_9",
    }
    assert "revert" not in by_id["ses_root_1"]


def test_v4_skeleton_row_tolerance():
    rows = [
        {"id": "ok1", "p_id": "p1", "p_name": "n", "p_worktree": "/w",
         "revert": {"messageID": "m", "partID": "p", "extra": "x"}},
        {"id": None, "p_id": "p1"},          # 缺 id → 跳行
        "not-a-dict",                          # 非 dict → 跳行
        {"p_id": "p1"},                        # 无 id 键 → 跳行
    ]
    skeletons = project_rows_to_v4_skeletons(rows)  # type: ignore[arg-type]
    assert len(skeletons) == 1
    assert skeletons[0]["id"] == "ok1"
    assert skeletons[0]["revert"] == {"messageID": "m", "partID": "p"}


# --- golden 头部校验逻辑（版本标记 + 指纹 + 载荷） ------------------------


def test_golden_version_mismatch_detected():
    document = build_golden_document()
    document["version"] = "v9.9.9"
    ok, reason = validate_golden(document)
    assert not ok and "version mismatch" in reason


def test_golden_fingerprint_mismatch_detected():
    document = build_golden_document()
    document["fingerprint"] = "deadbeefdeadbeef"
    ok, reason = validate_golden(document)
    assert not ok and "fingerprint mismatch" in reason
    assert "--write-golden" in reason  # 再生成指引


def test_golden_payload_mismatch_detected():
    document = build_golden_document()
    document["sessions"][0]["title"] = "tampered"
    ok, reason = validate_golden(document)
    assert not ok and "payload mismatch" in reason


def test_golden_in_repo_file_is_current():
    # 仓库内 golden 与数据集/镜像同步（防提交后数据集漂移）
    ok, reason = validate_golden(load_golden.__self__ if False else __import__(
        "json").loads(__import__("pathlib").Path(
            __import__("v4_fixture").GOLDEN_PATH).read_text(encoding="utf-8")))
    assert ok, reason


# --- EQ-007 真实进程（skip-if-no-server 框架；发布门完整版） --------------

_EQ_UPSTREAM = os.environ.get("OC_SLIMAPI_EQ_UPSTREAM")


def _real_server_available() -> tuple[bool, str]:
    if not _EQ_UPSTREAM:
        return False, "OC_SLIMAPI_EQ_UPSTREAM 未设置（真实进程框架未启用）"
    try:
        response = httpx.get(f"{_EQ_UPSTREAM}/experimental/session?limit=1",
                             timeout=1.0)
    except httpx.HTTPError as exc:
        return False, f"上游不可达：{exc}"
    if response.status_code != 200:
        return False, f"上游状态 {response.status_code}"
    return True, ""


@pytest.mark.skipif(not _EQ_UPSTREAM, reason="OC_SLIMAPI_EQ_UPSTREAM 未设置")
def test_eq007_real_process_framework():
    """真实 opencode 进程对照（发布门）。

    CI 框架版：进程可达时校验 schema 权威面（/experimental/session 200 +
    行字段存在性）。**数据集注入的行集全量对照**属发布门完整版（拉起
    opencode + 指向 fixture DB），超出 CI 范围——框架就位，发布时补全。
    """
    available, reason = _real_server_available()
    if not available:
        pytest.skip(reason)
    response = httpx.get(f"{_EQ_UPSTREAM}/experimental/session?limit=5",
                         timeout=2.0)
    assert response.status_code == 200
    payload = response.json()
    items = payload if isinstance(payload, list) else payload.get("sessions", [])
    assert isinstance(items, list)
    for item in items:
        assert "id" in item and "title" in item


# --- EQ-008 schema 漂移哨兵 ------------------------------------------------


async def test_eq008_schema_drift_disables_auxiliary(tmp_path):
    """上游 schema 变更（列重命名）→ schema 门失效 → 辅助源禁用 + 503 面。"""
    db = build_fixture_db(tmp_path / "drift.db", column_rename={"title": "titre"})
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    try:
        assert status.available is False
        assert status.reason == "gate_failed"
        with pytest.raises(AuxiliaryUnavailableError):
            await source.query("SELECT id FROM session LIMIT 1")
    finally:
        await source.stop()
