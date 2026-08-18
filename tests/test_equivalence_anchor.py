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

import os
import shutil
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
    REAL_GOLDEN_PATH,
    REAL_UPSTREAM_BINARY,
    REAL_UPSTREAM_VERSION,
    build_fixture_db,
    build_golden_document,
    build_real_golden_document,
    dataset_fingerprint,
    load_golden,
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

_REAL_BIN = REAL_UPSTREAM_BINARY
_REAL_PORT_LO, _REAL_PORT_HI = 14700, 14799
_INJECTABLE = [row for row in DATASET if row.get("directory")]
_INJECTABLE_BY_ID = {row["id"]: row for row in _INJECTABLE}


def _binary_absent_reason() -> str | None:
    if os.path.isfile(_REAL_BIN) and os.access(_REAL_BIN, os.X_OK):
        return None
    return (
        f"真实 opencode 发布二进制缺席（{_REAL_BIN}）——发布门唯一允许的 "
        "skip 理由"
    )


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


def _inject_dataset(client: httpx.Client, dir_map: dict[str, str]) -> dict[str, str]:
    """注入 fixture 数据集；返回 fixture_id → real_id 映射。

    逐条创建间隔 5ms——服务端赋值 time_updated 毫秒级互异，使上游
    x-next-cursor 单键翻页不因 tie 丢行（我们的复合 (t,i) cursor 无此
    约束；tie-break 维度由 EQ-001..006 fixture 面锚定）。parent 先于
    child 注入（两趟拓扑）。
    """
    id_map: dict[str, str] = {}
    pending = list(_INJECTABLE)
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
            response = client.post(
                "/session", params={"directory": dir_map[row["directory"]]},
                json=body,
            )
            assert response.status_code < 400, (
                f"注入失败 {row['id']}：{response.status_code} "
                f"{response.text[:200]}"
            )
            id_map[row["id"]] = response.json()["id"]
            pending.remove(row)
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
    return id_map


def _norm_upstream(info: dict) -> dict:
    """GlobalInfo JSON → 比较形状（HTTP 嵌套/缺席键 → 归一 None/扁平）。"""
    t = info.get("time") or {}
    tokens = info.get("tokens") or {}
    cache = tokens.get("cache") or {}
    project = info.get("project")
    return {
        "id": info.get("id"),
        "parent_id": info.get("parentID"),
        "directory": info.get("directory"),
        "title": info.get("title"),
        "version": info.get("version"),
        "time_created": t.get("created"),
        "time_updated": t.get("updated"),
        "time_archived": t.get("archived"),
        "tokens": (
            tokens.get("input"), tokens.get("output"),
            tokens.get("reasoning"), cache.get("read"), cache.get("write"),
        ),
        "project": None if project is None else (
            project.get("id"), project.get("name"), project.get("worktree")
        ),
        "metadata": info.get("metadata"),
        "agent": info.get("agent"),
        "model": info.get("model"),
    }


def _norm_db(record: dict) -> dict:
    """fetch_sessions_page 记录（DB 列名）→ 同一比较形状。"""
    return {
        "id": record["id"],
        "parent_id": record["parent_id"],
        "directory": record["directory"],
        "title": record["title"],
        "version": record["version"],
        "time_created": record["time_created"],
        "time_updated": record["time_updated"],
        "time_archived": record["time_archived"],
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
        "model": record["model"],
    }


@pytest.fixture(scope="session")
def real_upstream(tmp_path_factory):
    """隔离真实实例：启动 → 注入 → 全量基线 → yield；teardown 全清理。"""
    absent = _binary_absent_reason()
    if absent:
        pytest.skip(absent)
    # 版本门（存在但漂移 = 失败）
    probe = subprocess.run(
        [_REAL_BIN, "--version"], capture_output=True, text=True, timeout=15,
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
        "PATH": f"{os.path.dirname(_REAL_BIN)}:/usr/bin:/bin",
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "config" / "cache"),
        "NO_COLOR": "1",
    }
    log_path = home / "serve.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [_REAL_BIN, "serve", "--port", str(port), "--hostname", "127.0.0.1",
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
            id_map = _inject_dataset(client, dir_map)
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
    # HTTP 面对注入行不暴露的可选投影字段（summary 族 / revert /
    # permission / time_compacting）：GlobalInfo 缺席 → DB 空值（None/0）
    # 一致性（wire 形状分歧面——presence-aware 等价，见汇报偏离声明）。
    _optional_db_only = (
        "summary_additions", "summary_deletions", "summary_files",
        "summary_diffs", "revert", "permission", "time_compacting",
    )
    # 可选暴露字段：HTTP 缺席 ≡ DB 空值（None/0），存在则精确相等
    for u, d, raw in zip(up, db, page.records):
        for field in ("agent", "model", "metadata"):
            if u[field] is None:
                assert d[field] in (None, 0, ""), f"{u['id']}.{field}"
            else:
                assert u[field] == d[field], f"{u['id']}.{field}"
        for field in _optional_db_only:
            assert raw.get(field) in (None, 0, ""), f"{u['id']}.{field}"
        u_core = {
            k: v for k, v in u.items() if k not in ("agent", "model", "metadata")
        }
        d_core = {
            k: v for k, v in d.items() if k not in ("agent", "model", "metadata")
        }
        assert u_core == d_core, f"逐字段漂移：{u['id']}"


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
        }
        for fid, rid in real_upstream.id_map.items()
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
