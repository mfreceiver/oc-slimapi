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


# --- EQ-007 真实进程：数据集注入全量对照框架（发布门；CI 无 bun 跳过） ------

_EQ_UPSTREAM = os.environ.get("OC_SLIMAPI_EQ_UPSTREAM", "").rstrip("/")
_EQ_DB = os.environ.get("OC_SLIMAPI_EQ_DB", "").strip()
_EQ_SKIP_REASON = (
    "需上游进程（bun 构建），本机不可用——设 OC_SLIMAPI_EQ_UPSTREAM="
    "<base_url> 启用（可选 OC_SLIMAPI_EQ_DB=<进程 SQLite 路径> 走发布门"
    " DB 投影对照）"
)

# 可注入子集：上游 Location.Ref 的 directory 是 AbsolutePath——空串
# （legacy 空 directory 维度）不可注入，该维度由 golden/EQ-001..006 锚定。
_EQ_INJECTABLE = [row for row in DATASET if row.get("directory")]
_EQ_INJECTABLE_IDS = {row["id"] for row in _EQ_INJECTABLE}


def _real_server_available() -> tuple[bool, str]:
    if not _EQ_UPSTREAM:
        return False, _EQ_SKIP_REASON
    try:
        response = httpx.get(f"{_EQ_UPSTREAM}/session?limit=1", timeout=2.0)
    except httpx.HTTPError as exc:
        return False, f"上游不可达：{exc}"
    if response.status_code != 200:
        return False, f"上游状态 {response.status_code}"
    return True, ""


def _eq_inject_dataset(client: httpx.Client) -> None:
    """经上游 API 注入 fixture 数据集（幂等：已存在则跳过创建）。

    - ``POST /session``：``{id, location: {directory}}``（v1.18.18
      CreateInput 尊重 passthrough id）；
    - ``PATCH /session/:id``：``{title}`` 与 ``{time: {archived}}``。
    - parent_id 上游 create 不收——父子维度对照属 golden/EQ-003 面。
    """
    for row in _EQ_INJECTABLE:
        response = client.post(
            "/session",
            json={"id": row["id"],
                  "location": {"directory": row["directory"]}},
        )
        if response.status_code >= 400:
            # 幂等：已存在（重复运行）→ get 验证；真失败 → 断言失败。
            probe = client.get(f"/session/{row['id']}")
            assert probe.status_code == 200, (
                f"注入 {row['id']} 失败：create={response.status_code} "
                f"{response.text[:200]}; get={probe.status_code}"
            )
    for row in _EQ_INJECTABLE:
        if row.get("title"):
            response = client.patch(
                f"/session/{row['id']}", json={"title": row["title"]})
            assert response.status_code < 400, (
                f"title 注入失败 {row['id']}：{response.status_code} "
                f"{response.text[:200]}"
            )
        if row.get("time_archived") is not None:
            response = client.patch(
                f"/session/{row['id']}",
                json={"time": {"archived": row["time_archived"]}})
            assert response.status_code < 400, (
                f"archived 注入失败 {row['id']}：{response.status_code} "
                f"{response.text[:200]}"
            )


def _eq_upstream_sessions(client: httpx.Client) -> list[dict]:
    """上游全量列表中属于注入集的行（上游 list 不过滤 archived——
    v1.18.18 session.ts list() 无 time_archived 谓词）。"""
    response = client.get("/session", params={"limit": 1000})
    assert response.status_code == 200, response.text[:200]
    payload = response.json()
    assert isinstance(payload, list)
    return [s for s in payload if s.get("id") in _EQ_INJECTABLE_IDS]


def _eq_v4_order(items: list[dict]) -> list[dict]:
    """本测试内独立重写的 v4 冻结排序（不 import 生产/镜像任何排序）：
    ``(time.updated, id)`` DESC。"""
    return sorted(
        items,
        key=lambda s: (s["time"]["updated"], s["id"]),
        reverse=True,
    )


@pytest.mark.skipif(not _EQ_UPSTREAM, reason=_EQ_SKIP_REASON)
def test_eq007_real_process_dataset_equivalence():
    """真实 opencode 进程 × 数据集注入全量对照（发布门，rev gate 升级版）。

    对照面（三轴 + 可选第四轴）：

    1. **行集**：注入集 id ⇔ 上游返回 id 逐一双射；
    2. **逐字段**：id / directory / title / time.archived 置性一致；
    3. **排序/分页**：以**上游真实数据**（time.updated 取自上游响应）过
       本文件独立重写的 (time_updated, id) DESC 比较器——窗口切分自洽
       （每窗与锚点 keyset 关系成立），并验证上游自身返回序与其
       (time_created, id) DESC 实现一致（交叉校验读到的数据是活的）；
    4.（可选，发布门完整对照）``OC_SLIMAPI_EQ_DB`` 指向该进程的 SQLite
       时：sidecar ``fetch_sessions_page`` 直读该库 → 行集/序与上游响应
       全量对照（archived=omit 面）。
    """
    available, reason = _real_server_available()
    if not available:
        pytest.skip(reason)
    with httpx.Client(base_url=_EQ_UPSTREAM, timeout=15.0) as client:
        _eq_inject_dataset(client)
        upstream_rows = _eq_upstream_sessions(client)

    # 1) 行集双射
    got_ids = {s["id"] for s in upstream_rows}
    assert got_ids == _EQ_INJECTABLE_IDS, (
        f"行集漂移：缺失 {sorted(_EQ_INJECTABLE_IDS - got_ids)} / "
        f"多出 {sorted(got_ids - _EQ_INJECTABLE_IDS)}"
    )

    # 2) 逐字段（title / directory / archived 置性）
    by_id = {s["id"]: s for s in upstream_rows}
    for row in _EQ_INJECTABLE:
        item = by_id[row["id"]]
        assert item.get("directory") == row["directory"], row["id"]
        if row.get("title"):
            assert item.get("title") == row["title"], row["id"]
        archived_up = (item.get("time") or {}).get("archived")
        if row.get("time_archived") is not None:
            assert archived_up is not None, row["id"]
        else:
            assert archived_up is None, row["id"]

    # 3) 排序 / 分页（独立比较器 + 上游真实时间数据）
    ordered = _eq_v4_order(upstream_rows)
    for prev, nxt in zip(ordered, ordered[1:]):
        key_prev = (prev["time"]["updated"], prev["id"])
        key_next = (nxt["time"]["updated"], nxt["id"])
        assert key_prev > key_next, f"v4 冻结序被违反：{key_prev} ≤ {key_next}"
    # 上游自身序一致性（time_created, id) DESC——交叉证明时间数据是活的
    for prev, nxt in zip(upstream_rows, upstream_rows[1:]):
        key_prev = (prev["time"]["created"], prev["id"])
        key_next = (nxt["time"]["created"], nxt["id"])
        assert key_prev >= key_next, (
            f"上游返回序与其 (time_created, id) DESC 实现不一致："
            f"{key_prev} < {key_next}"
        )
    # 窗口切分自洽：任意 limit 窗 + 下一窗锚点 = keyset 谓词成立
    for limit in (1, 3, 7):
        for start in range(0, min(len(ordered), 12), limit):
            window = ordered[start:start + limit]
            rest = ordered[start + limit:start + limit + limit]
            if not window or not rest:
                continue
            anchor = (window[-1]["time"]["updated"], window[-1]["id"])
            for item in rest:
                assert (item["time"]["updated"], item["id"]) < anchor, (
                    f"keyset 违反：anchor={anchor} item={item['id']}"
                )

    # 4)（可选）发布门 DB 投影对照
    if not _EQ_DB:
        return

    async def _db_side() -> list[str]:
        source = DbAuxiliarySource(
            ResolvedPath(path=_EQ_DB, source="explicit-env"))
        status = await source.start()
        try:
            assert status.available, f"真库辅助源不可用：{status.reason}"
            page = await fetch_sessions_page(
                source, archived="omit", parent="all", limit=1000)
            return [r["id"] for r in page.records]
        finally:
            await source.stop()

    import asyncio

    db_ids = asyncio.run(_db_side())
    expected_db = [
        s["id"] for s in ordered
        if (s.get("time") or {}).get("archived") is None
    ]
    assert db_ids == expected_db, "DB 投影行集/序与上游响应漂移"


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
