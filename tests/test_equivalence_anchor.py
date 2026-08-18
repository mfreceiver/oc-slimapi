"""等价性锚定（design-v4-dbaux §10）：EQ-001..EQ-008。

权威源混合（§10.4）：
- ① 真实 opencode **发布二进制**（1.18.18）= 发布门（EQ-007：隔离实例
  注入数据集 → 真实 HTTP handler vs 生产投影直读真实 DB 全量对照；
  二进制缺席才 skip）；
- ② 版本标记 golden JSON = 日常 CI 默认——tests/golden/
  sessions-global-v1.18.18.json（fixture 镜像 oracle 生成，生成器
  mirror-oracle-v1）+ sessions-global-real-v1.18.18.json（真实 handler
  生成，生成器 real-upstream-http-1.18.18，CI 降级校验）。

行集 oracle = tests/v4_fixture.mirror_page（独立谓词/排序/翻页实现）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import orjson
import pytest

from oc_slimapi.dbaux import DbAuxiliarySource, fetch_sessions_page, ROW_KEYS
from oc_slimapi.dbaux.lifecycle import AuxiliaryUnavailableError
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.skeleton import SESSION_KEYS, project_rows_to_v4_skeletons

from v4_fixture import (
    ALIGNED_VERSION,
    DATASET,
    REAL_ENRICH_SUBSTITUTES,
    REAL_GOLDEN_PATH,
    REAL_UPSTREAM_BINARY,
    REAL_UPSTREAM_VERSION,
    build_db_from_real_golden,
    build_fixture_db,
    build_golden_document,
    build_real_golden_document,
    dataset_fingerprint,
    load_golden,
    load_real_golden,
    mirror_page,
    validate_golden,
    validate_real_golden_ci,
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


# --- EQ-007 真实进程权威等价门（rev gate BLOCKER-1；二进制缺席才 skip） ------
#
# 权威源① = 真实**发布二进制** /home/mar/.opencode/bin/opencode（1.18.18，
# 比源码构建更权威）。本机存在 → 默认必跑（session-scoped 隔离实例：
# 随机高位端口 + 独立 HOME/XDG + tmp 目录）；仅二进制缺席允许 skip。
# 二进制存在但起不来 / 版本漂移 = 测试**失败**（不静默 skip）。
#
# v1.18.18 注入 API（实测核对，与旧框架假设不同）：
# - POST /session?directory=<abs>：body CreateInput {parentID?, title?,
#   agent?, model?, metadata?, permission?, workspaceID?}——**无 id、无
#   location**（id 服务端生成，directory 由 query 参数路由）；
# - PATCH /session/:id：{title?, metadata?, permission?, time:{archived?}}
#   （archived 任意整数原值落库）；
# - GET /experimental/session?archived=true&limit=N：全局门面
#   （session.ts listGlobal——ORDER BY time_updated DESC, id DESC 与 v4
#   冻结排序一致；limit+1 窗 + x-next-cursor 单键 cursor 头）。

# OC_SLIMAPI_EQ_BINARY 覆盖二进制路径（rev gate BLOCKER-1d：默认硬编码
# 路径可被 env 覆盖——调用时读取，release 门验证可 monkeypatch）。
def _eq_binary() -> str:
    return os.environ.get("OC_SLIMAPI_EQ_BINARY", REAL_UPSTREAM_BINARY)


_REAL_PORT_LO, _REAL_PORT_HI = 14700, 14799
_INJECTABLE = [row for row in DATASET if row.get("directory")]
_INJECTABLE_BY_ID = {row["id"]: row for row in _INJECTABLE}


def _binary_absent_reason() -> str | None:
    binary = _eq_binary()
    if os.path.isfile(binary) and os.access(binary, os.X_OK):
        return None
    return (
        f"真实 opencode 发布二进制缺席（{binary}）——发布门唯一允许的 "
        "skip 理由"
    )


def _eq007_gate() -> tuple[str, str | None]:
    """EQ-007 验收门：``("ok", None)`` / ``("skip", reason)`` / ``("fail", reason)``。

    rev gate BLOCKER-1d：``OC_SLIMAPI_REQUIRE_EQ007=1``（release gate 语义）
    时二进制缺席 → **fail 不 skip**（版本漂移/起不来在本文件恒 fail）；
    普通 CI 缺席 → skip 真进程部分（golden CI 校验不受影响——
    ``test_real_golden_ci_unconditional`` 不依赖本门）。

    用法（release 门跑法）::

        OC_SLIMAPI_EQ_BINARY=/nonexistent OC_SLIMAPI_REQUIRE_EQ007=1 \\
            .venv/bin/python -m pytest tests/test_equivalence_anchor.py -q
        # → EQ-007 真进程 case FAIL（而非 skip）
    """
    require = os.environ.get("OC_SLIMAPI_REQUIRE_EQ007") == "1"
    reason = _binary_absent_reason()
    if reason is None:
        return "ok", None
    if require:
        return "fail", f"[release gate] {reason}"
    return "skip", reason


def _pick_port() -> int:
    import random
    import socket

    candidates = list(range(_REAL_PORT_LO, _REAL_PORT_HI + 1))
    random.shuffle(candidates)
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("14700-14799 段无可用端口")


def _real_dirs(home) -> dict[str, str]:
    """fixture directory → 隔离 HOME 下的真实绝对路径（保留相对结构：
    /foo vs /foobar 边界、/Foo 大小写、% 字面目录名都原样映射）。"""
    ws = home / "ws"
    mapping: dict[str, str] = {}
    for row in _INJECTABLE:
        fixture_dir = row["directory"]
        if fixture_dir in mapping:
            continue
        target = ws / fixture_dir.lstrip("/")
        target.mkdir(parents=True, exist_ok=True)
        mapping[fixture_dir] = str(target)
    return mapping


# rev gate BLOCKER-2b：API 注入的互异可观察值（非空 agent / 合法 model
# 结构 / 非空 permission Ruleset；值互异——防「全零错列仍通过」）。
_OBS_RULES_A = [{"permission": "bash", "pattern": "rm*", "action": "deny"}]
_OBS_RULES_B = [
    {"permission": "edit", "pattern": "*.env", "action": "ask"},
    {"permission": "webfetch", "pattern": "host:example*", "action": "allow"},
]


def _inject_dataset(
    client: httpx.Client, dir_map: dict[str, str]
) -> tuple[dict[str, str], dict[str, dict]]:
    """注入 fixture 数据集；返回 (fixture_id → real_id, 注入可观察字段)。

    逐条创建间隔 5ms——服务端赋值 time_updated 毫秒级互异，使上游
    x-next-cursor 单键翻页不因 tie 丢行（我们的复合 (t,i) cursor 无此
    约束；tie-break 维度由 EQ-001..006 fixture 面锚定 + 2e 真实 tie
    样本）。parent 先于 child 注入（两趟拓扑）。

    BLOCKER-2b：每行注入非空 agent / 合法 model（``{id, providerID}``
    结构——纯品牌字符串，创建期不解析 provider）+ 部分 Rowset permission
    （值按注入序互异）。注入值记录进 ``observables``（golden 桥）。
    """
    id_map: dict[str, str] = {}
    observables: dict[str, dict] = {}
    pending = list(_INJECTABLE)
    idx = 0
    for _ in range(4):  # 拓扑趟数上限（最深两级父子）
        for row in list(pending):
            parent = row.get("parent_id")
            if parent is not None and parent not in id_map:
                continue
            body: dict = {"title": row["title"]}
            if parent is not None:
                body["parentID"] = id_map[parent]
            if row.get("metadata"):
                body["metadata"] = orjson.loads(row["metadata"])
            agent = "agent-even" if idx % 2 == 0 else "agent-odd"
            model = {
                "id": "fixture-model",
                "providerID": "prov-a" if idx % 2 == 0 else "prov-b",
            }
            body["agent"] = agent
            body["model"] = model
            permission = None
            if idx % 7 == 3:
                permission = _OBS_RULES_A
            elif idx % 7 == 5:
                permission = _OBS_RULES_B
            if permission is not None:
                body["permission"] = permission
            response = client.post(
                "/session", params={"directory": dir_map[row["directory"]]},
                json=body,
            )
            assert response.status_code < 400, (
                f"注入失败 {row['id']}：{response.status_code} "
                f"{response.text[:200]}"
            )
            id_map[row["id"]] = response.json()["id"]
            observables[row["id"]] = {
                "agent": agent,
                "model": (model["id"], model["providerID"], None),
                "permission": None if permission is None else tuple(
                    (r["permission"], r["pattern"], r["action"])
                    for r in permission
                ),
            }
            pending.remove(row)
            idx += 1
            time.sleep(0.005)
    assert not pending, f"拓扑未收敛：{[r['id'] for r in pending]}"
    for row in _INJECTABLE:
        if row.get("time_archived") is not None:
            response = client.patch(
                f"/session/{id_map[row['id']]}",
                json={"time": {"archived": row["time_archived"]}},
            )
            assert response.status_code < 400, (
                f"archived 注入失败 {row['id']}：{response.text[:200]}"
            )
    return id_map, observables


def _sql_enrich(db_path: str, id_map: dict[str, str]) -> None:
    """rev gate BLOCKER-2c：API 不可生成字段的权威生成路径。

    tokens 五列 / summary 四列 / revert / time_compacting 无公开 API 可
    置值——对**本测试自建的一次性隔离实例**（isolated HOME，teardown 全
    毁）的 DB 做直接 UPDATE，写入 fixture 派生值（非零且行间互异）。

    合规声明（S-B03/硬规则）：sidecar 生产代码路径零 DB 写（硬规则不
    变）；写目标仅限本测试自建的 ephemeral 实例，非任何生产/开发库。
    随后 GET 真实 HTTP 输出 vs ``fetch_sessions_page`` 直读同库比较——
    **HTTP handler 仍是权威**（它读这些列并暴露到 wire），证明的是
    列 ↔ wire 字段映射正确，非自证。

    替代值：``ses_bad_json`` 的坏 JSON ``summary_diffs`` 以 None 代
    （上游 drizzle 读路径对坏 JSON 的行为由独立探针测试实证记录）。
    """
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        for row in _INJECTABLE:
            rid = id_map[row["id"]]
            diffs = REAL_ENRICH_SUBSTITUTES.get(row["id"], {}).get(
                "summary_diffs", row["summary_diffs"]
            )
            conn.execute(
                "UPDATE session SET tokens_input=?, tokens_output=?, "
                "tokens_reasoning=?, tokens_cache_read=?, "
                "tokens_cache_write=?, summary_additions=?, "
                "summary_deletions=?, summary_files=?, summary_diffs=?, "
                "revert=?, time_compacting=? WHERE id=?",
                (
                    row["tokens_input"], row["tokens_output"],
                    row["tokens_reasoning"], row["tokens_cache_read"],
                    row["tokens_cache_write"], row["summary_additions"],
                    row["summary_deletions"], row["summary_files"],
                    diffs, row["revert"], row["time_compacting"], rid,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _norm_diffs(value):
    """summary.diffs 归一：HTTP 可能是 parsed 对象（drizzle json 列）或
    字符串；统一成解析后的对象比较（None 透传）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _norm_upstream(info: dict) -> dict:
    """GlobalInfo JSON → 比较形状（HTTP 嵌套/缺席键 → 归一 None/扁平）。

    rev gate BLOCKER-2a：比较**完整投影字段**——summary 族 / revert /
    permission / time.compacting / model 对象（评委锚点：GlobalInfo 建立
    在完整 Info.fields 上，session.ts fromRow:59-115——这些字段 HTTP
    可观察）。置性镜像 fromRow：summary 三列全 null → undefined；
    revert JSON 非空才暴露。
    """
    t = info.get("time") or {}
    tokens = info.get("tokens") or {}
    cache = tokens.get("cache") or {}
    project = info.get("project")
    summary = info.get("summary")
    revert = info.get("revert") or None
    permission = info.get("permission")
    model = info.get("model")
    return {
        "id": info.get("id"),
        "parent_id": info.get("parentID"),
        "directory": info.get("directory"),
        "title": info.get("title"),
        "version": info.get("version"),
        "time_created": t.get("created"),
        "time_updated": t.get("updated"),
        "time_archived": t.get("archived"),
        "time_compacting": t.get("compacting"),
        "tokens": (
            tokens.get("input"), tokens.get("output"),
            tokens.get("reasoning"), cache.get("read"), cache.get("write"),
        ),
        "project": None if project is None else (
            project.get("id"), project.get("name"), project.get("worktree")
        ),
        "metadata": info.get("metadata"),
        "agent": info.get("agent"),
        "model": None if model is None else (
            model.get("id"), model.get("providerID"), model.get("variant")
        ),
        "summary": None if summary is None else (
            summary.get("additions"), summary.get("deletions"),
            summary.get("files"), _norm_diffs(summary.get("diffs")),
        ),
        "revert": None if revert is None else (
            revert.get("messageID"), revert.get("partID"),
            revert.get("snapshot"), revert.get("diff"),
        ),
        "permission": None if permission is None else tuple(
            (rule["permission"], rule["pattern"], rule["action"])
            for rule in permission
        ),
    }


def _norm_db(record: dict) -> dict:
    """fetch_sessions_page 记录（DB 列名）→ 同一比较形状。

    model 列不在生产 JSON 解析集（rows_to_records 只解析 summary_diffs/
    revert/permission/metadata）——此处按 JSON 字符串 parse；置性镜像
    上游 fromRow（三列全 null → summary None）。
    """
    model_raw = record.get("model")
    model = json.loads(model_raw) if isinstance(model_raw, str) else model_raw
    revert = record.get("revert")
    permission = record.get("permission")
    summary_absent = (
        record["summary_additions"] is None
        and record["summary_deletions"] is None
        and record["summary_files"] is None
    )
    return {
        "id": record["id"],
        "parent_id": record["parent_id"],
        "directory": record["directory"],
        "title": record["title"],
        "version": record["version"],
        "time_created": record["time_created"],
        "time_updated": record["time_updated"],
        "time_archived": record["time_archived"],
        "time_compacting": record.get("time_compacting"),
        "tokens": (
            record["tokens_input"], record["tokens_output"],
            record["tokens_reasoning"], record["tokens_cache_read"],
            record["tokens_cache_write"],
        ),
        "project": None if record["p_id"] is None else (
            record["p_id"], record["p_name"], record["p_worktree"]
        ),
        "metadata": record["metadata"],
        "agent": record["agent"],
        "model": None if model is None else (
            model.get("id"), model.get("providerID"), model.get("variant")
        ),
        "summary": None if summary_absent else (
            record["summary_additions"], record["summary_deletions"],
            record["summary_files"], _norm_diffs(record.get("summary_diffs")),
        ),
        "revert": None if revert is None else (
            revert.get("messageID"), revert.get("partID"),
            revert.get("snapshot"), revert.get("diff"),
        ),
        "permission": None if permission is None else tuple(
            (rule["permission"], rule["pattern"], rule["action"])
            for rule in permission
        ),
    }


@pytest.fixture(scope="session")
def real_upstream(tmp_path_factory):
    """隔离真实实例：启动 → 注入（2b 互异可观察值）→ SQL 富化（2c）→
    全量基线 → yield；teardown 全清理。"""
    state, reason = _eq007_gate()
    if state == "skip":
        pytest.skip(reason)
    assert state == "ok", reason
    # 版本门（存在但漂移 = 失败）
    probe = subprocess.run(
        [_eq_binary(), "--version"], capture_output=True, text=True, timeout=15,
    )
    version_line = (probe.stdout + probe.stderr).strip()
    assert REAL_UPSTREAM_VERSION in version_line, (
        f"真实二进制版本漂移：{version_line!r}（期望含 {REAL_UPSTREAM_VERSION}）"
    )
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="eq007-home-"))
    (home / "data").mkdir()
    (home / "config").mkdir()
    port = _pick_port()
    env = {
        "HOME": str(home),
        "PATH": f"{os.path.dirname(_eq_binary())}:/usr/bin:/bin",
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "config" / "cache"),
        "NO_COLOR": "1",
    }
    log_path = home / "serve.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [_eq_binary(), "serve", "--port", str(port), "--hostname", "127.0.0.1",
         "--print-logs"],
        env=env, stdout=log_file, stderr=subprocess.STDOUT, cwd=str(home),
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        # 健康等待（/doc 轮询）；进程死亡或超时 = 失败（带日志尾）
        deadline = time.monotonic() + 40.0
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                if httpx.get(f"{base_url}/doc", timeout=2.0).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        if not ready:
            log_file.flush()
            tail = log_path.read_text(errors="replace")[-2000:]
            raise AssertionError(
                f"真实 opencode 实例未就绪（exit={proc.poll()}）。\n日志尾：\n{tail}"
            )
        dir_map = _real_dirs(home)
        with httpx.Client(base_url=base_url, timeout=15.0) as client:
            id_map, observables = _inject_dataset(client, dir_map)
        # BLOCKER-2c：SQL 富化（tokens/summary/revert/time_compacting 的
        # fixture 派生值）先于基线拉取——全套 EQ-007 与 golden 都在富化后
        # 世界上断言。
        _sql_enrich(str(home / "data" / "opencode" / "opencode.db"), id_map)
        with httpx.Client(base_url=base_url, timeout=15.0) as client:
            response = client.get(
                "/experimental/session",
                params={"archived": "true", "limit": 1000},
            )
            assert response.status_code == 200, response.text[:300]
            l_up = response.json()
        db_path = home / "data" / "opencode" / "opencode.db"
        assert db_path.is_file(), f"真实 DB 未落盘：{db_path}"
        # 隔离实例只含注入行（任何多出 = 实例不洁 → 失败）
        assert {s["id"] for s in l_up} == set(id_map.values()), (
            "上游全量行集 ≠ 注入集（实例不洁或注入丢失）"
        )
        # 落库后真实目录（上游 create 面对 ?directory= 做 %XX 解码归一——
        # 如 `/a%20b/c` 落库为 `/a b/c`；sidecar 投影忠实 DB 值。allowlist
        # 轴与 golden injected 清单必须用**落库值**，fixture 字面另存语义桥）。
        rid_to_fid = {rid: fid for fid, rid in id_map.items()}
        real_dir_of = {
            rid_to_fid[s["id"]]: s["directory"] for s in l_up
        }
        yield SimpleNamespace(
            base_url=base_url, db_path=str(db_path), home=home,
            id_map=id_map, dir_map=dir_map, real_dir_of=real_dir_of,
            l_up=l_up, opencode_version=REAL_UPSTREAM_VERSION,
            observables=observables,
        )
    finally:
        log_file.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(home, ignore_errors=True)


async def _real_source(db_path: str) -> DbAuxiliarySource:
    source = DbAuxiliarySource(
        ResolvedPath(path=db_path, source="explicit-env"))
    status = await source.start()
    assert status.available, f"真库辅助源不可用：{status.reason}"
    return source


async def _db_records(source, *, limit=1000, **kwargs):
    return await fetch_sessions_page(
        source, archived="all", parent="all", limit=limit, **kwargs)


async def test_eq007_real_identity_full_fields_and_order(real_upstream):
    """核心恒等式：真实 HTTP handler ≡ 生产投影直读真实 DB。

    完整行集双射 + 全投影字段（含时间戳精确比对——同一实例自身一致性）
    + (time_updated, id) DESC 序逐位一致（上游 listGlobal 冻结排序 ≡ v4）。
    """
    source = await _real_source(real_upstream.db_path)
    try:
        page = await _db_records(source)
        assert page.complete is True
    finally:
        await source.stop()
    up = [_norm_upstream(s) for s in real_upstream.l_up]
    db = [_norm_db(r) for r in page.records]
    assert [r["id"] for r in up] == [r["id"] for r in db], "行集/序漂移"
    # rev gate BLOCKER-2a：**完整投影字段**逐行直比——tokens 五列 / summary
    # 族（含 diffs）/ revert / permission / time.compacting / model 对象 /
    # project join / metadata / agent（经 2b/2c 注入+富化，非空代表值在
    # 场——「HTTP 缺席 ≡ DB 空值」的弱断言已删除）。
    assert up == db, "逐字段漂移（首个差异见 pytest diff）"


async def test_eq007_archived_parent_axes(real_upstream):
    """archived{omit,only,all} × parent{all,none,only,<sid>} 全轴：
    生产投影 vs 上游全量在**测试内独立实现**的语义过滤器。"""
    up = [_norm_upstream(s) for s in real_upstream.l_up]

    def _indep(rows, archived, parent):  # 独立比较器（S-B03）
        out = []
        explicit_sid = parent not in ("all", "none", "only")
        for r in rows:
            if archived == "omit" and r["time_archived"] is not None:
                continue
            if archived == "only" and r["time_archived"] is None:
                continue
            if parent == "none" and r["parent_id"] is not None:
                continue
            if parent == "only" and r["parent_id"] is None:
                continue
            if explicit_sid and r["parent_id"] != parent:
                continue
            out.append(r)
        return [r["id"] for r in out]

    sid = real_upstream.id_map["ses_root_1"]  # 三子一孙的父
    source = await _real_source(real_upstream.db_path)
    try:
        for archived in ("omit", "only", "all"):
            for parent in ("all", "none", "only", sid):
                page = await fetch_sessions_page(
                    source, archived=archived, parent=parent, limit=1000)
                got = [r["id"] for r in page.records]
                want = _indep(up, archived, parent)
                assert got == want, f"archived={archived} parent={parent}"
    finally:
        await source.stop()


async def test_eq007_search_axis(real_upstream):
    """search 轴：字面搜索三方一致（生产投影 / 上游 ?search / 独立子串）；
    通配符搜索断言**语义分歧为真**（我们 ESCAPE 字面 vs 上游裸 LIKE）。"""
    import re as _re

    up = [_norm_upstream(s) for s in real_upstream.l_up]

    def _raw_like(title: str, needle: str) -> bool:  # 上游裸 LIKE 独立模拟
        pattern = ".*" + "".join(
            ".*" if ch == "%" else "." if ch == "_" else _re.escape(ch)
            for ch in needle
        ) + ".*"
        return _re.fullmatch(pattern, title, _re.IGNORECASE) is not None

    source = await _real_source(real_upstream.db_path)
    try:
        with httpx.Client(base_url=real_upstream.base_url, timeout=10.0) as client:
            for needle in ("tie", "percent four", "plain"):
                page = await fetch_sessions_page(
                    source, archived="all", parent="all",
                    search=needle, limit=1000)
                ours = {r["id"] for r in page.records}
                response = client.get(
                    "/experimental/session",
                    params={"archived": "true", "search": needle, "limit": 1000},
                )
                assert response.status_code == 200
                upstream_ids = {s["id"] for s in response.json()}
                substring = {
                    r["id"] for r in up
                    if needle.lower() in (r["title"] or "").lower()
                }
                assert ours == substring == upstream_ids, f"字面 search {needle!r}"
            # 通配符分歧面（文档化语义差异，断言双方各自符合己方语义）
            needle = "100%"
            page = await fetch_sessions_page(
                source, archived="all", parent="all", search=needle, limit=1000)
            ours = {r["id"] for r in page.records}
            escaped = {
                r["id"] for r in up if "100%" in (r["title"] or "")
            }  # 我们：字面「100%」
            response = client.get(
                "/experimental/session",
                params={"archived": "true", "search": needle, "limit": 1000},
            )
            upstream_ids = {s["id"] for s in response.json()}
            raw = {r["id"] for r in up if _raw_like(r["title"] or "", needle)}
            assert ours == escaped, "我们=ESCAPE 字面语义"
            assert upstream_ids == raw, "上游=裸 LIKE 语义"
            assert raw != escaped, (
                "分歧面失效：数据集不再区分两种 search 语义（需补对照行）"
            )
    finally:
        await source.stop()


async def test_eq007_allowlist_axis(real_upstream):
    """allowlist 轴：多子树 / 根 / 大小写 / 同层异名——期望 = 测试内
    独立子树过滤器。注意用**落库后真实目录**（上游 create 面对 ?directory=
    有 %XX 解码归一——如 fixture `/a%20b/c` 落库为 `/a b/c`；allowlist
    谓词对落库值做 BINARY 精确匹配，语义桥 = fixture_directory）。"""
    up = [_norm_upstream(s) for s in real_upstream.l_up]
    # fixture 目录字面 → 落库后真实目录（经 fixture_id 桥接）
    dmap = {
        _INJECTABLE_BY_ID[fid]["directory"]: real
        for fid, real in real_upstream.real_dir_of.items()
    }

    def _indep(allowlist):  # 独立子树语义（§9.3：等值或前缀 d+'/'，BINARY；
        # 根 '/' 特例 = 任意非空绝对目录——与生产 substr(1,1)='/' 同规则）
        out = []
        for r in up:
            for a in allowlist:
                if a == "/":
                    hit = r["directory"].startswith("/")
                else:
                    hit = (
                        r["directory"] == a
                        or r["directory"].startswith(a + "/")
                    )
                if hit:
                    out.append(r["id"])
                    break
        return out

    source = await _real_source(real_upstream.db_path)
    try:
        for allowlist in (
            (dmap["/foo"],),                       # 子树（不含 /foobar）
            (dmap["/a"], dmap["/b"]),              # 多子树并集
            ("/",),                                # 根：全部非空绝对目录
            (dmap["/Foo/child"],),                 # 大小写敏感（≠ /foo 树）
            (dmap["/a"], dmap["/a%20b/c"]),        # 同层异名（%20 已解码归一）
        ):
            page = await fetch_sessions_page(
                source, archived="all", parent="all", limit=1000,
                allowlist=tuple(allowlist))
            got = [r["id"] for r in page.records]
            assert got == _indep(tuple(allowlist)), f"allowlist={allowlist}"
        # 边界自证：/foo 不含 /foobar 同层异名（真实目录面）
        page = await fetch_sessions_page(
            source, archived="all", parent="all", limit=1000,
            allowlist=(dmap["/foo"],))
        dirs = {r["directory"] for r in page.records}
        assert dmap["/foobar"] not in dirs
    finally:
        await source.stop()


async def test_eq007_cursor_paging_identity(real_upstream):
    """cursor 翻页恒等：上游 x-next-cursor 单键逐页拼接 ≡ 上游全量；
    生产复合 (t,i) 锚点逐页拼接 ≡ 生产全量；N/N+1 complete 边界。"""
    up_times = [s["time"]["updated"] for s in real_upstream.l_up]
    assert len(set(up_times)) == len(up_times), (
        "上游 time_updated 出现 tie——单键 cursor 翻页前提失效"
        "（注入应保证毫秒互异）"
    )
    # 上游侧
    collected: list[str] = []
    cursor = None
    with httpx.Client(base_url=real_upstream.base_url, timeout=10.0) as client:
        for _ in range(50):
            params = {"archived": "true", "limit": 3}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/experimental/session", params=params)
            assert response.status_code == 200
            page_rows = response.json()
            collected.extend(s["id"] for s in page_rows)
            cursor = response.headers.get("x-next-cursor")
            if cursor is None:
                break
    assert collected == [s["id"] for s in real_upstream.l_up], (
        "上游翻页拼接 ≠ 全量"
    )
    # 生产侧（复合锚点：items 可空仍前进——BLOCKER-3 语义在真库上自洽）
    source = await _real_source(real_upstream.db_path)
    try:
        full = await _db_records(source)
        full_ids = [r["id"] for r in full.records]
        walked: list[str] = []
        cursor_tuple = None
        for _ in range(50):
            page = await fetch_sessions_page(
                source, archived="all", parent="all", limit=3,
                cursor=cursor_tuple)
            walked.extend(r["id"] for r in page.records)
            if page.complete:
                break
            assert page.anchor is not None
            cursor_tuple = page.anchor
        assert walked == full_ids, "生产翻页拼接 ≠ 生产全量"
        # complete 边界：N → true；N-1 → false
        n = len(full_ids)
        assert (await _db_records(source, limit=n)).complete is True
        assert (await _db_records(source, limit=n - 1)).complete is False
    finally:
        await source.stop()


async def test_eq007_real_golden_provenance(real_upstream):
    """真实 handler golden：provenance 头 + CI 降级校验 + 同运行全字段。

    OC_SLIMAPI_EQ_WRITE_REAL_GOLDEN=1 时再生成落盘（时间戳跨环境不可复现，
    日常运行只做降级校验：server-assigned 字段存在性 + 排序一致性，其余
    经注入清单桥接 fixture 语义全量比对）。
    """
    sessions = [_norm_upstream(s) for s in real_upstream.l_up]
    injected = [
        {
            "fixture_id": fid,
            "real_id": rid,
            "fixture_directory": _INJECTABLE_BY_ID[fid]["directory"],
            # 落库后真实值（上游 create 面 %XX 解码归一——与
            # fixture_directory 的差 = 该上游行为的显式记录）
            "real_directory": real_upstream.real_dir_of[fid],
            # BLOCKER-2b 注入可观察值（validator 的 agent/model/permission 桥）
            "agent": obs["agent"],
            "model": list(obs["model"]),
            "permission": None if obs["permission"] is None else [
                list(rule) for rule in obs["permission"]
            ],
        }
        for fid, rid, obs in (
            (fid, rid, real_upstream.observables[fid])
            for fid, rid in real_upstream.id_map.items()
        )
    ]
    document = build_real_golden_document(
        sessions, injected,
        opencode_version=real_upstream.opencode_version,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    if os.environ.get("OC_SLIMAPI_EQ_WRITE_REAL_GOLDEN") == "1":
        REAL_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        REAL_GOLDEN_PATH.write_text(
            orjson.dumps(document, option=orjson.OPT_INDENT_2).decode() + "\n",
            encoding="utf-8",
        )
        return
    assert REAL_GOLDEN_PATH.is_file(), (
        f"真实 golden 缺失：{REAL_GOLDEN_PATH}（再生成：见 regenerate_hint）"
    )
    stored = orjson.loads(REAL_GOLDEN_PATH.read_bytes())
    problems = validate_real_golden_ci(stored)
    assert not problems, f"真实 golden CI 降级校验失败：{problems}"
    # 跨运行语义交叉：real_id / 时间戳是 server-assigned（每实例不同，
    # 不可跨运行比对）；语义字段（title / archived 置性 / parent 结构）
    # 经 fixture 桥接后跨实例稳定——当前真实例与落盘 golden 必须一致。
    stored_fid_of = {e["real_id"]: e["fixture_id"] for e in stored["injected_sessions"]}
    stored_semantic = {}
    for row in stored["sessions"]:
        fid = stored_fid_of.get(row["id"])
        if fid is None:
            continue
        parent_real = row.get("parent_id")
        stored_semantic[fid] = (
            row.get("title"),
            row.get("time_archived") is not None,
            stored_fid_of.get(parent_real) if parent_real else None,
        )
    live_fid_of = {rid: fid for fid, rid in real_upstream.id_map.items()}
    live_semantic = {}
    for info in real_upstream.l_up:
        fid = live_fid_of[info["id"]]
        parent_real = info.get("parentID")
        live_semantic[fid] = (
            info.get("title"),
            (info.get("time") or {}).get("archived") is not None,
            live_fid_of.get(parent_real) if parent_real else None,
        )
    assert stored_semantic == live_semantic, (
        "真实 golden 语义面与当前实例漂移（title/archived/parent 结构）"
    )


# --- rev gate BLOCKER-1：真实 golden 进日常 CI 权威链路 -------------------


def test_real_golden_ci_unconditional():
    """BLOCKER-1a：真实 golden CI 校验**无条件必跑**。

    不依赖 ``real_upstream`` fixture / 真实二进制——二进制缺席只 skip
    真进程 case，golden 校验（provenance + 载荷指纹 + 稳定语义字段全量）
    永不被连带 skip。
    """
    document = load_real_golden()
    assert document["sessions"], "真实 golden 载荷非空"


_TAMPER_CASES = {
    "tokens": lambda doc: doc["sessions"][0].update(
        tokens=[v + 1 for v in doc["sessions"][0]["tokens"]]),
    "project": lambda doc: doc["sessions"][0].update(
        project=["p0", "tampered", "/tampered"]),
    "project_none": lambda doc: doc["sessions"][0].update(project=None),
    "metadata": lambda doc: doc["sessions"][0].update(
        metadata={"tampered": True}),
    "title": lambda doc: doc["sessions"][0].update(title="tampered"),
    "time_compacting": lambda doc: doc["sessions"][0].update(
        time_compacting=999999),
    "revert": lambda doc: doc["sessions"][0].update(
        revert=["tampered", None, None, None]),
    "summary": lambda doc: doc["sessions"][0].update(
        summary=[7, 7, 7, None]),
    "agent": lambda doc: doc["sessions"][0].update(agent="tampered"),
    "permission": lambda doc: doc["sessions"][0].update(
        permission=[["x", "y", "allow"]]),
    "fingerprint": lambda doc: doc.update(response_fingerprint="0" * 16),
    "generated_at": lambda doc: doc.pop("generated_at"),
    "query_sort": lambda doc: doc["query"].update(sort="tampered"),
    "injected_empty": lambda doc: doc.update(injected_sessions=[]),
    "dataset_digest": lambda doc: doc.update(dataset_digest="0" * 16),
}


@pytest.mark.parametrize("case", sorted(_TAMPER_CASES))
def test_real_golden_tamper_negative(case):
    """BLOCKER-1b 自测：篡改任一字段（tokens/project/metadata/…）必须使
    CI 校验失败——载荷指纹重算层 + 语义桥层双保险。"""
    document = load_real_golden()
    _TAMPER_CASES[case](document)
    problems = validate_real_golden_ci(document)
    assert problems, f"篡改 {case} 未被检出"


def test_eq007_gate_release_mode_fail_not_skip(monkeypatch):
    """BLOCKER-1d：release 门语义。

    - ``OC_SLIMAPI_EQ_BINARY=/nonexistent`` + 无 REQUIRE → skip（普通 CI）；
    - + ``OC_SLIMAPI_REQUIRE_EQ007=1`` → **fail 不 skip**；
    - env 指回真实二进制 → ok（路径可覆盖）。
    """
    monkeypatch.setenv("OC_SLIMAPI_EQ_BINARY", "/nonexistent/eq-opencode")
    monkeypatch.delenv("OC_SLIMAPI_REQUIRE_EQ007", raising=False)
    state, _ = _eq007_gate()
    assert state == "skip"
    monkeypatch.setenv("OC_SLIMAPI_REQUIRE_EQ007", "1")
    state, reason = _eq007_gate()
    assert state == "fail"
    assert "release" in reason
    monkeypatch.setenv("OC_SLIMAPI_EQ_BINARY", REAL_UPSTREAM_BINARY)
    state, _ = _eq007_gate()
    assert state == "ok"


# --- rev gate BLOCKER-1c：EQ-001..006 生产投影 vs real golden ------------


async def test_real_golden_projection_identity_eq001_eq006(tmp_path):
    """EQ-001/006（real golden 权威期望）：golden 内容重建 DB → 生产
    ``fetch_sessions_page`` 全量投影 ≡ golden sessions（逐字段 + 序）。

    server-assigned 字段（id/时间戳/directory/project）显式写入 fixture
    行；mirror oracle 降为辅助（不再是投影等价的唯一权威期望）。
    """
    document = load_real_golden()
    db = build_db_from_real_golden(document, tmp_path / "real-golden.db")
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available, f"golden 派生 DB 不可用：{status.reason}"
    try:
        page = await fetch_sessions_page(
            source, archived="all", parent="all",
            limit=len(document["sessions"]) + 5,
        )
        assert page.complete is True
        assert len(page.records) == len(document["sessions"])
        # tuple（_norm_db）vs list（golden JSON）→ canonical 文本逐行比对
        for got, want in zip(
            (_norm_db(r) for r in page.records), document["sessions"]
        ):
            assert json.dumps(got, sort_keys=True) == json.dumps(
                want, sort_keys=True
            ), f"逐字段漂移：{want['id']}"
    finally:
        await source.stop()


async def test_real_golden_cursor_paging_eq002_eq005(tmp_path):
    """EQ-002/005（real golden 权威期望）：复合锚点逐页拼接 ≡ 全量；
    N/N+1 complete 边界。"""
    document = load_real_golden()
    db = build_db_from_real_golden(document, tmp_path / "real-golden.db")
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    await source.start()
    try:
        full = await fetch_sessions_page(
            source, archived="all", parent="all", limit=1000)
        full_ids = [r["id"] for r in full.records]
        walked: list[str] = []
        cursor = None
        for _ in range(200):
            page = await fetch_sessions_page(
                source, archived="all", parent="all", limit=3, cursor=cursor)
            walked.extend(r["id"] for r in page.records)
            if page.complete:
                break
            assert page.anchor is not None
            cursor = page.anchor
        assert walked == full_ids, "golden 期望：翻页拼接 ≡ 全量"
        n = len(full_ids)
        assert (await fetch_sessions_page(
            source, archived="all", parent="all", limit=n)).complete is True
        assert (await fetch_sessions_page(
            source, archived="all", parent="all", limit=n - 1)).complete is False
    finally:
        await source.stop()


async def test_real_golden_filter_axes_eq003_eq004(tmp_path):
    """EQ-003/004（real golden 权威期望）：archived×parent 全轴 vs **测试内
    独立比较器**（期望源 = golden sessions 本身，不调用生产谓词/降级）。

    EQ-004 tie-break：注入保证 time_updated 毫秒互异 → golden 无并列，
    tie 维度断言「无 tie + 排序确定」（tie-break 权威锚定在 fixture 面
    + 2e 真实 tie 样本）。
    """
    document = load_real_golden()
    db = build_db_from_real_golden(document, tmp_path / "real-golden.db")
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    await source.start()
    try:
        sessions = document["sessions"]
        times = [s["time_updated"] for s in sessions]
        assert len(set(times)) == len(times), "golden 出现 tie（注入前提失效）"

        def _indep(rows, archived, parent):
            out = []
            explicit_sid = parent not in ("all", "none", "only")
            for r in rows:
                if archived == "omit" and r["time_archived"] is not None:
                    continue
                if archived == "only" and r["time_archived"] is None:
                    continue
                if parent == "none" and r["parent_id"] is not None:
                    continue
                if parent == "only" and r["parent_id"] is None:
                    continue
                if explicit_sid and r["parent_id"] != parent:
                    continue
                out.append(r["id"])
            return out

        with_parent = next(
            s for s in sessions if s["parent_id"] is not None)
        sid = with_parent["parent_id"]
        for archived in ("omit", "only", "all"):
            for parent in ("all", "none", "only", sid):
                page = await fetch_sessions_page(
                    source, archived=archived, parent=parent, limit=1000)
                assert [r["id"] for r in page.records] == _indep(
                    sessions, archived, parent), (
                    f"archived={archived} parent={parent}"
                )
    finally:
        await source.stop()


# --- rev gate BLOCKER-2c/2e：真实实例上的富化暴露 / 坏 JSON 实证 / tie ---


async def test_eq007_sql_enrichment_exposed_on_wire(real_upstream):
    """BLOCKER-2c 证据面：API 不可生成字段（tokens/summary/revert/
    time_compacting）经 SQL 富化后**真实 HTTP handler 暴露到 wire**——
    非零互异代表值在场（防「全零错列仍通过」）；恒等式由全字段
    identity 测试锚定。"""
    by_id = {s["id"]: s for s in real_upstream.l_up}
    # tokens：行内 input≠output 恒成立（100+n vs 50+n）——列映射错误必被
    # 捕获；行间互异（n 按 sid 哈希分布）
    for s in real_upstream.l_up:
        tokens = s["tokens"]
        assert tokens["input"] != tokens["output"], s["id"]
    distinct_inputs = {s["tokens"]["input"] for s in real_upstream.l_up}
    assert len(distinct_inputs) >= len(real_upstream.l_up) // 2, (
        "tokens 行间互异性不足（错列防护失效）"
    )
    # summary 族（含 diffs）暴露
    sample = by_id[real_upstream.id_map["ses_root_1"]]
    assert sample["summary"]["additions"] is not None
    assert "diffs" in sample["summary"]
    # revert 暴露（fixture 置值行）
    revert_id = by_id[real_upstream.id_map["ses_revert_full"]]["revert"]
    assert revert_id["messageID"] == "msg_9"
    assert revert_id["partID"] == "prt_9"
    # time_compacting 暴露
    assert by_id[real_upstream.id_map["ses_root_3"]]["time"]["compacting"] == 1234


async def test_eq007_bad_json_probe_upstream_behavior(real_upstream):
    """BLOCKER-2c 附带实证（不静默放弃）：坏 ``summary_diffs`` 写入单行 →
    观察上游真实行为并记录（200 行级容忍 / 500 整列表失败）；探针结束
    恢复原值。sidecar 的 §8 跳行容忍是**我们侧**语义；上游事实在此锚定。"""
    target = real_upstream.id_map["ses_child_a"]
    conn = sqlite3.connect(real_upstream.db_path, timeout=10.0)
    original = conn.execute(
        "SELECT summary_diffs FROM session WHERE id=?", (target,)
    ).fetchone()[0]
    status = None
    snippet = ""
    try:
        conn.execute(
            "UPDATE session SET summary_diffs='not-json{' WHERE id=?", (target,)
        )
        conn.commit()
        with httpx.Client(
            base_url=real_upstream.base_url, timeout=10.0
        ) as client:
            response = client.get(
                "/experimental/session",
                params={"archived": "true", "limit": 1000},
            )
            status = response.status_code
            snippet = response.text[:200]
    finally:
        conn.execute(
            "UPDATE session SET summary_diffs=? WHERE id=?", (original, target)
        )
        conn.commit()
        conn.close()
    # 实证断言：二选一（任何其他状态 = 上游行为超出已知面，需上报）
    assert status in (200, 500), f"未预期的上游行为：{status} {snippet}"
    if status == 500:
        print(f"[EQ-007 实证] 上游坏 JSON → 500（整列表失败）：{snippet}")
    else:
        print("[EQ-007 实证] 上游坏 JSON → 200（列表仍可用）")


async def test_eq007_real_tie_break_and_upstream_cursor_boundary(real_upstream):
    """BLOCKER-2e：真实 tie 样本——SQL 制造 time_updated 并列（顶两行）→

    - 生产 (t,i) keyset：tie-break id DESC 确定 + limit=1 逐页两行都到
      （复合锚点不丢行）；
    - 上游单键 cursor：``lt(time_updated)`` 跳过并列兄弟——**边界按预期
      断言为真**（上游事实记录；fixture 面已覆盖我方 tie-break 权威）。

    finally 恢复原始 time_updated（对后续运行零残留）。
    """
    source = await _real_source(real_upstream.db_path)
    conn = sqlite3.connect(real_upstream.db_path, timeout=10.0)
    originals: dict[str, int] = {}
    try:
        full = await _db_records(source)
        top1, top2 = full.records[0], full.records[1]
        t1, t2 = top1["time_updated"], top2["time_updated"]
        tie_t = max(t1, t2)
        originals = {top1["id"]: t1, top2["id"]: t2}
        for rid in originals:
            conn.execute(
                "UPDATE session SET time_updated=? WHERE id=?", (tie_t, rid)
            )
        conn.commit()
        # 生产侧：tie-break (t, id) DESC 确定性
        page = await _db_records(source)
        top_keys = [
            (r["time_updated"], r["id"]) for r in page.records[:2]
        ]
        assert top_keys == sorted(top_keys, reverse=True)
        assert all(t == tie_t for t, _ in top_keys)
        # 生产侧：limit=1 翻页——复合锚点两行都到
        walked: list[str] = []
        cursor = None
        for _ in range(60):
            p = await fetch_sessions_page(
                source, archived="all", parent="all", limit=1, cursor=cursor)
            walked.extend(r["id"] for r in p.records)
            if p.complete:
                break
            cursor = p.anchor
        assert set(originals) <= set(walked), "复合锚点丢并列行"
        # 上游侧：单键 cursor 边界——第一页后 cursor=tie_t → lt() 丢兄弟
        with httpx.Client(
            base_url=real_upstream.base_url, timeout=10.0
        ) as client:
            first = client.get(
                "/experimental/session", params={"archived": "true", "limit": 1}
            )
            assert first.status_code == 200
            first_id = first.json()[0]["id"]
            cursor_header = first.headers.get("x-next-cursor")
            assert cursor_header is not None
            second = client.get(
                "/experimental/session",
                params={"archived": "true", "limit": 1, "cursor": cursor_header},
            )
            assert second.status_code == 200
            second_ids = [s["id"] for s in second.json()]
        assert first_id == max(originals), "上游首行应为并列组 id 最大者"
        sibling = next(rid for rid in originals if rid != first_id)
        assert sibling not in second_ids, (
            "预期边界失效：上游单键 cursor 未跳过并列行（行为漂移，需上报）"
        )
    finally:
        for rid, t in originals.items():
            conn.execute(
                "UPDATE session SET time_updated=? WHERE id=?", (t, rid)
            )
        conn.commit()
        conn.close()
        await source.stop()


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
