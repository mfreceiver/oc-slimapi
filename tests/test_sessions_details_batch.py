"""v4 §18（4.10.0）：POST /slimapi/sessions/details 批量 session 详情。

动机：oc-webui 逐 session 轮询 ``GET /slimapi/session/{sid}``（12h 内
6.2k 次）→ 一次 POST 拿多个 session 的 canonical skeleton 详情。

覆盖矩阵（契约 §18 冻结面）：

* 路径 A（dbaux 点查优先）：全命中 canonical items（no-store 头、请求
  顺序保持）、部分不存在 → ``missing[]``、重复 sid 静默去重（保序）、
  不可表示行（坏 JSON 列被 ``rows_to_records`` 跳过）→ 整响应 503
  （§13.2c 同判——**不得**把存在的 session 误报进 missing）。
* 请求体校验：``sids`` 缺失/非 JSON 对象/非字符串数组/空数组/含非字符
  串元素/malformed JSON → 400 ``invalid_body``；去重后 >50 → 400
  ``too_many_sids``（去重先于计数：60 含 10 重复 → 200）。
* 路径 B（native fan-out 回退）：上游 mock 多个 ``GET /session/{sid}``
  200 → items 恒 ``degraded:true``；某 sid 404 → 进 missing 其余正常；
  其他 4xx / 5xx / malformed body → 整响应 503 ``upstream_unavailable``
  （与 §13 单查 4xx 逐字透传不同——批量无 per-sid 透传面）。
* ``sqlite3.Error`` → 503 fail-closed（不落 native）。
* ``?directory=`` / ``X-Opencode-Directory``：tolerant-ignore（不报错、
  不校验、不转发）。
* ``?v=4`` 缺失 → selector 层 400 ``unsupported_version``。
* transform 池饱和 → 503 ``transform_busy`` + Retry-After: 2（admission
  先于 fan-out）。
"""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.dbaux import AuxiliaryUnavailableError, DbAuxiliarySource
from oc_slimapi.dbaux.lifecycle import DbAuxStatus
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, read_groups, sessions, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

from v4_fixture import FIXED_NOW_MS, build_fixture_db

IDENTITY = {"Accept-Encoding": "identity"}


# ---------------------------------------------------------------------------
# fixture / app 构建惯例（对齐 test_session_single_v4.py）
# ---------------------------------------------------------------------------

def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


class _StubAux:
    """dbaux 恒不可用（disabled）——native fan-out 回退驱动。"""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=False, mode="http", reason=self._reason)

    async def query(self, sql, params=()):  # pragma: no cover - never reached
        raise AuxiliaryUnavailableError(self._reason)


class _BusyAux:
    """dbaux available 但 query 抛 sqlite3.Error → fail-closed 503。"""

    def __init__(self, exc: sqlite3.Error) -> None:
        self._exc = exc

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=True, mode="sqlite", reason="ok")

    async def query(self, sql, params=()):
        raise self._exc


def _native_payload(sid: str) -> dict:
    """上游单查 200 payload（SessionInfo camelCase；全字段 explicit-null
    基线 → partial False，fallback 态 degraded 恒 True）。"""
    return {
        "id": sid, "title": f"up {sid}", "directory": "/native",
        "parentID": None, "projectID": None,
        "agent": "build",
        "model": {"id": "m1", "providerID": "prov"},
        "time": {"created": 11, "updated": 22, "archived": None},
        "summary": {"additions": 1, "deletions": 2, "files": 3},
        "tokens": {"input": 9, "output": 8, "reasoning": 0,
                   "cache": {"read": 1, "write": 2}},
        "revert": None,
    }


def _build_app(aux, *, settings: Settings | None = None, handler=None):
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(
            200, content=orjson.dumps(_native_payload("h1")),
            headers={"Content-Type": "application/json"},
        )

    app = FastAPI()
    settings = settings or _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.dbaux = aux
    for router in (health.router, versions.router, sessions.router,
                   read_groups.router):
        app.include_router(router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    install_proxy(app)
    return app, seen


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def _aux_on_rows(tmp_path, rows, projects=None):
    db = build_fixture_db(
        tmp_path / "s.db", session_rows=rows, project_rows=projects)
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available
    return source


def _custom_row(sid: str, **overrides):
    from v4_fixture import _row
    row = _row(sid, "prj_alpha", None, "/foo", f"title {sid}",
               FIXED_NOW_MS + 10_000, FIXED_NOW_MS + 100_000)
    row.update(overrides)
    return row


def _native_handler(payloads: dict[str, httpx.Response | dict]):
    """按 path 返回固定 payload 的上游 mock（200 dict 自动序列化）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        target = payloads.get(request.url.path)
        if target is None:
            return httpx.Response(404, content=b'{"message":"not found"}')
        if isinstance(target, httpx.Response):
            return target
        return httpx.Response(
            200, content=orjson.dumps(target),
            headers={"Content-Type": "application/json"},
        )
    return handler


async def _post(client, sids, *, params=None, headers=None, content=None):
    if content is None:
        content = orjson.dumps({"sids": sids})
    # params={} → 不带 ?v=（selector 层 400 用例）；None → 默认 ?v=4
    query = {"v": "4"} if params is None else params
    return await client.post(
        "/slimapi/sessions/details", params=query,
        headers=headers or IDENTITY, content=content,
    )


# ---------------------------------------------------------------------------
# 路径 A：dbaux 点查优先
# ---------------------------------------------------------------------------

async def test_batch_dbaux_happy_path(tmp_path):
    """多 sid 全命中 → canonical items（无降级标记）+ 空 missing +
    no-store 头 + **请求顺序保持**（非词典序）。"""
    rows = [_custom_row("ses_b"), _custom_row("ses_a"),
            _custom_row("ses_c", time_archived=FIXED_NOW_MS + 5)]
    aux = await _aux_on_rows(tmp_path, rows)
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, ["ses_c", "ses_a", "ses_b"])
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"
        body = resp.json()
        assert body["missing"] == []
        assert [i["id"] for i in body["sessions"]] == ["ses_c", "ses_a", "ses_b"]
        for item in body["sessions"]:
            assert item["partial"] is False
            assert item["degraded"] is False
            assert item["project"] == {
                "id": "prj_alpha", "name": "alpha", "worktree": "/wt/alpha"}
            assert item["directory"] == "/foo"
        assert body["sessions"][0]["time"]["archived"] == FIXED_NOW_MS + 5
        assert seen == []  # dbaux 命中 → 零上游 IO
    finally:
        await aux.stop()


async def test_batch_dbaux_partial_missing(tmp_path):
    rows = [_custom_row("ses_x"), _custom_row("ses_y")]
    aux = await _aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, ["ses_x", "ses_nope", "ses_y"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["missing"] == ["ses_nope"]
        assert [i["id"] for i in body["sessions"]] == ["ses_x", "ses_y"]
    finally:
        await aux.stop()


async def test_batch_dedup_preserves_first_seen_order(tmp_path):
    """重复 sid 静默去重；响应顺序 = 首现顺序。"""
    rows = [_custom_row("ses_a"), _custom_row("ses_b")]
    aux = await _aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, ["ses_b", "ses_a", "ses_b", "ses_a"])
        assert resp.status_code == 200
        body = resp.json()
        assert [i["id"] for i in body["sessions"]] == ["ses_b", "ses_a"]
        assert body["missing"] == []
    finally:
        await aux.stop()


async def test_batch_dbaux_skipped_row_whole_503(tmp_path):
    """§13.2c 批量化：行存在但 ``rows_to_records`` 跳行（坏 JSON 列）→
    整响应 503——不得把存在的 session 误报进 missing。"""
    rows = [_custom_row("ses_ok"),
            _custom_row("ses_bad", model="{not json")]
    aux = await _aux_on_rows(tmp_path, rows)
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, ["ses_ok", "ses_bad"])
        assert resp.status_code == 503
        assert resp.json()["code"] == "auxiliary_unavailable"
        assert resp.headers.get("Retry-After") == "30"
        assert seen == []
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# 请求体校验（400 invalid_body / too_many_sids）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", [
    b"{}",                          # sids 缺失
    b'{"sids": "ses_a"}',           # 非数组
    b'{"sids": []}',                # 空数组
    b'{"sids": [1, 2]}',            # 非字符串元素
    b'{"sids": ["ses_a", null]}',   # 含 null 元素
    b'{"sids": ["ses_a", 3]}',      # 混入数字
    b"[1, 2, 3]",                   # 非 JSON 对象
    b'"just a string"',             # 非 JSON 对象
    b'{"sids": ["ses_',             # malformed JSON
    b"",                            # 空 body
])
async def test_batch_invalid_body_400(tmp_path, content):
    aux = await _aux_on_rows(tmp_path, [_custom_row("ses_a")])
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, None, content=content)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_body"
        assert seen == []
    finally:
        await aux.stop()


async def test_batch_too_many_sids_400(tmp_path):
    aux = await _aux_on_rows(tmp_path, [])
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, [f"ses_{i:03d}" for i in range(51)])
        assert resp.status_code == 400
        assert resp.json()["code"] == "too_many_sids"
    finally:
        await aux.stop()


async def test_batch_dedup_before_count_60_raw_50_unique_ok(tmp_path):
    """去重先于计数：60 个原始 sid（10 重复 → 50 唯一）→ 200，非 400。"""
    sids = [f"ses_{i:03d}" for i in range(50)] + [f"ses_{i:03d}" for i in range(10)]
    aux = await _aux_on_rows(tmp_path, [_custom_row("ses_000")])
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, sids)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["sessions"]) == 1
        assert len(body["missing"]) == 49
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# 路径 B：native fan-out 回退（dbaux 不可用 / 竞态禁用）
# ---------------------------------------------------------------------------

async def test_batch_native_fanout_all_found():
    """上游逐 sid 200 → items 恒 ``degraded:true``；上游请求不带
    directory（query/header 均无）。"""
    payloads = {f"/session/{sid}": _native_payload(sid)
                for sid in ("h1", "h2", "h3")}
    app, seen = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h2", "h1", "h3"])
    assert resp.status_code == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    body = resp.json()
    assert body["missing"] == []
    assert [i["id"] for i in body["sessions"]] == ["h2", "h1", "h3"]
    for item in body["sessions"]:
        assert item["degraded"] is True       # native 来源恒 degraded
        assert item["partial"] is False       # 全字段 explicit 基线
        assert item["title"] == f"up {item['id']}"
    assert len(seen) == 3
    for upstream_request in seen:
        assert upstream_request.url.query == b""
        assert "x-opencode-directory" not in upstream_request.headers


async def test_batch_native_one_404_rest_ok():
    payloads = {
        "/session/h1": _native_payload("h1"),
        "/session/h2": httpx.Response(404, content=b'{"message":"no"}'),
        "/session/h3": _native_payload("h3"),
    }
    app, _ = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2", "h3"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["missing"] == ["h2"]
    assert [i["id"] for i in body["sessions"]] == ["h1", "h3"]
    for item in body["sessions"]:
        assert item["degraded"] is True


async def test_batch_native_all_404():
    payloads = {"/session/h1": httpx.Response(404, content=b"{}"),
                "/session/h2": httpx.Response(404, content=b"{}")}
    app, _ = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["sessions"] == []
    assert body["missing"] == ["h1", "h2"]


@pytest.mark.parametrize("status", [500, 503, 400, 409])
async def test_batch_native_non404_error_whole_503(status):
    """某 sid 5xx / 其他 4xx → 整响应 503 ``upstream_unavailable``——不
    混装部分数据（与 §13 单查 4xx 逐字透传不同，批量无 per-sid 透传）。"""
    payloads = {
        "/session/h1": _native_payload("h1"),
        "/session/h2": httpx.Response(status, content=b'{"message":"err"}'),
    }
    app, _ = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"scalar"'])
async def test_batch_native_malformed_200_body_whole_503(raw):
    """200 但 body malformed / 非 dict → 整响应 503。"""
    payloads = {
        "/session/h1": _native_payload("h1"),
        "/session/h2": httpx.Response(
            200, content=raw, headers={"Content-Type": "application/json"}),
    }
    app, _ = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"


async def test_batch_native_unrepresentable_item_whole_503():
    """native item required 字段不可表示（title 非字符串）→ §13.2a 同判
    整响应 503（fail-closed，不混装）。"""
    bad = _native_payload("h2")
    bad["title"] = 42
    payloads = {"/session/h1": _native_payload("h1"),
                "/session/h2": bad}
    app, _ = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "auxiliary_unavailable"


async def test_batch_native_body_over_cap_whole_503():
    """某 sid 200 body 超 ``max_response_bytes`` → 整响应 503。"""
    huge = b'{"id":"h2","title":"' + b"x" * 300 + b'"}'
    payloads = {
        "/session/h1": _native_payload("h1"),
        "/session/h2": httpx.Response(
            200, content=huge, headers={"Content-Type": "application/json"}),
    }
    settings = _settings(max_response_bytes=128)
    app, _ = _build_app(
        _StubAux("disabled"), settings=settings,
        handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"


async def test_batch_native_transform_busy():
    """admission 先于 fan-out：池满 → 503 ``transform_busy`` +
    Retry-After: 2，零上游 GET。"""
    payloads = {"/session/h1": _native_payload("h1")}
    app, seen = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        async with app.state.transforms:  # 占满唯一 admission 槽
            resp = await _post(client, ["h1"])
        assert resp.status_code == 503
        assert resp.json()["code"] == "transform_busy"
        assert resp.headers["Retry-After"] == "2"
        assert seen == []


# ---------------------------------------------------------------------------
# dbaux 层错误：sqlite3.Error fail-closed（不落 native）
# ---------------------------------------------------------------------------

async def test_batch_sqlite_error_fail_closed_503():
    app, seen = _build_app(_BusyAux(sqlite3.OperationalError("db locked")))
    async with _client(app) as client:
        resp = await _post(client, ["ses_a"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "auxiliary_unavailable"
    assert resp.headers.get("Retry-After") == "30"
    assert seen == []  # BLOCKER-1 同规：sqlite 层错误不回退 native


async def test_batch_dbaux_raced_disable_falls_to_native(tmp_path):
    """dbaux status available 但 query 抛 AuxiliaryUnavailableError（竞态
    禁用）→ 落 native fan-out。"""
    class _RacedAux(_StubAux):
        def status(self) -> DbAuxStatus:
            return DbAuxStatus(available=True, mode="sqlite", reason="ok")

        async def query(self, sql, params=()):
            raise AuxiliaryUnavailableError("raced disable")

    payloads = {"/session/h1": _native_payload("h1")}
    app, seen = _build_app(_RacedAux("raced"),
                           handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["missing"] == []
    assert body["sessions"][0]["id"] == "h1"
    assert body["sessions"][0]["degraded"] is True
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# ?directory= tolerant-ignore / ?v= selector
# ---------------------------------------------------------------------------

async def test_batch_directory_query_tolerant_ignore(tmp_path):
    """``?directory=`` 出现 → 不报错、不校验、不转发（B4 惯例）；
    ``X-Opencode-Directory`` 头同样忽略，无冲突检查。"""
    aux = await _aux_on_rows(tmp_path, [_custom_row("ses_a")])
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.post(
                "/slimapi/sessions/details?v=4&directory=/some/dir",
                headers={**IDENTITY, "X-Opencode-Directory": "/other/dir"},
                content=orjson.dumps({"sids": ["ses_a"]}),
            )
        assert resp.status_code == 200
        assert resp.json()["missing"] == []
        assert seen == []  # dbaux 命中，零上游 IO；directory 从未转发
    finally:
        await aux.stop()


async def test_batch_directory_query_tolerant_ignore_native():
    """native 路径同样 tolerant-ignore：上游请求不带 directory
    query/header。"""
    payloads = {"/session/h1": _native_payload("h1")}
    app, seen = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await client.post(
            "/slimapi/sessions/details?v=4&directory=/some/dir",
            headers=IDENTITY,
            content=orjson.dumps({"sids": ["h1"]}),
        )
    assert resp.status_code == 200
    assert resp.json()["missing"] == []
    assert len(seen) == 1
    assert seen[0].url.query == b""
    assert "x-opencode-directory" not in seen[0].headers


async def test_batch_missing_v_selector_400(tmp_path):
    """``?v=4`` 缺失 → selector 层 400 ``unsupported_version``
    （v4-only 窗口；路由侧零工作）。"""
    aux = await _aux_on_rows(tmp_path, [_custom_row("ses_a")])
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, ["ses_a"], params={})
        assert resp.status_code == 400
        assert resp.json()["code"] == "unsupported_version"
        assert seen == []
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# rev 8.8 评审整改回归（3 MAJOR）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_sid", [
    "status?directory=/x",       # ? 重解释为 query（MAJOR-1：URL 拼接）
    "ses/a",                     # / 重解释为路径段
    "ses#a",                     # # 重解释为 fragment
    "ses a",                     # 空格
    "ses\x01ctrl",               # 控制字符（httpx InvalidURL → 裸 500 面）
    "ses%20a",                   # 百分号（编码重解释风险）
    "a" * 129,                   # 超长（>128）
    "",                          # 空串
])
async def test_batch_sid_charset_rejected_entry_400(tmp_path, bad_sid):
    """MAJOR-1：非法字符 sid 在**入口**拒绝（400 ``invalid_body`` + hint
    说明字符集约束）——dbaux 可用时同样拒绝（两路径语义一致），零上游
    IO。合法 sid 形态（上游 ``ses_`` + 26 位 hex/base62；fixture
    ``ses_a_pct`` 类）全部落在 ``^[A-Za-z0-9_-]{1,128}$``。"""
    aux = await _aux_on_rows(tmp_path, [_custom_row("ses_a")])
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, ["ses_a", bad_sid])
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "invalid_body"
        assert "sid" in body.get("hint", "")
        assert seen == []  # 入口拒绝——从未触达 dbaux 之外的任何路径
    finally:
        await aux.stop()


@pytest.mark.parametrize("good_sid", [
    "ses_0123456789abcdefABCXYZ",   # 上游真实形态（hex+base62 混排）
    "ses_a_pct",                    # fixture 形态（下划线）
    "a",                            # 最短合法
    "a" * 128,                      # 长度上界
    "ses-1_2",                      # 连字符
])
async def test_batch_sid_charset_legal_passes(tmp_path, good_sid):
    """合法字符集 sid 不被入口拒绝（不存在 → ``missing``，非 400）。"""
    aux = await _aux_on_rows(tmp_path, [])
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await _post(client, [good_sid])
        assert resp.status_code == 200
        assert resp.json()["missing"] == [good_sid]
    finally:
        await aux.stop()


async def test_batch_native_error_cancels_sibling_tasks():
    """MAJOR-2：某 sid 网络错（ConnectError）→ 整响应 503，且**慢响应
    sibling 任务被 cancel**——异常传播前先受控收束（无孤儿上游 IO）。

    判别法：慢 handler 在 sleep 后置 done 标记；修复版任务被取消 →
    标记永不置位；若 gather 未收束（缺陷版），孤儿任务会在响应返回
    后继续跑完 0.25s sleep 并置位。"""
    slow_done: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/h1":
            raise httpx.ConnectError("boom")
        assert request.url.path == "/session/h2"
        await asyncio.sleep(0.25)
        slow_done.append(True)
        return httpx.Response(
            200, content=orjson.dumps(_native_payload("h2")),
            headers={"Content-Type": "application/json"},
        )

    app, _ = _build_app(_StubAux("disabled"), handler=handler)
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
        assert resp.status_code == 503
        assert resp.json()["code"] == "upstream_unavailable"
        # 给潜在孤儿任务留足跑完 sleep 的窗口——修复版此间无事可发生
        await asyncio.sleep(0.4)
        assert slow_done == []  # 慢任务被取消，从未完成


async def test_batch_native_404_body_over_cap_whole_503():
    """MAJOR-3：404 + body 超 cap → 整响应 503（cap 优先于状态码语
    义），绝不把该 sid 误判 missing、绝不 200。"""
    huge_404 = b"x" * 300
    payloads = {
        "/session/h1": httpx.Response(
            404, content=huge_404,
            headers={"Content-Type": "application/json"}),
        "/session/h2": _native_payload("h2"),
    }
    settings = _settings(max_response_bytes=128)
    app, _ = _build_app(
        _StubAux("disabled"), settings=settings,
        handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"


async def test_batch_native_404_small_body_still_missing():
    """MAJOR-3 对照：404 + 小 body（含空 body 边界）→ 仍正常进
    ``missing``——cap 检查只拦超限，不改变 404 语义。"""
    payloads = {
        "/session/h1": httpx.Response(404, content=b"nope"),
        "/session/h2": httpx.Response(404, content=b""),
        "/session/h3": _native_payload("h3"),
    }
    app, _ = _build_app(
        _StubAux("disabled"), handler=_native_handler(payloads))
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2", "h3"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["missing"] == ["h1", "h2"]
    assert [i["id"] for i in body["sessions"]] == ["h3"]


async def test_batch_body_cap_256kib_boundary(tmp_path):
    """MINOR-1：请求体恰 256 KiB → 正常处理（合法 sid + pad 键填充至
    精确边界）；256 KiB + 1 → 400 ``invalid_body``。"""
    aux = await _aux_on_rows(tmp_path, [_custom_row("ses_a")])
    app, _ = _build_app(aux)
    prefix = b'{"sids":["ses_a"],"pad":"'
    suffix = b'"}'
    exact = prefix + b"x" * (256 * 1024 - len(prefix) - len(suffix)) + suffix
    assert len(exact) == 256 * 1024
    over = exact + b"x"
    try:
        async with _client(app) as client:
            resp = await _post(client, None, content=exact)
            assert resp.status_code == 200
            body = resp.json()
            assert [i["id"] for i in body["sessions"]] == ["ses_a"]
            assert body["missing"] == []

            resp = await _post(client, None, content=over)
            assert resp.status_code == 400
            assert resp.json()["code"] == "invalid_body"
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# rev 8.9 整改回归：mid-stream 网络错 → 503（非裸 500）
# ---------------------------------------------------------------------------

def _mid_stream_breaker(*, chunks_before: int = 1, delay: float = 0.0):
    """构造流式 200 body：先 yield 若干 chunk，再抛 ``httpx.ReadError``
    （模拟连接建立后流式读取期网络中断——transform.py ``read_with_cap``
    契约下该异常原样上抛）。"""
    async def stream():
        for _ in range(chunks_before):
            yield b'{"id":"h1","title":"partial"'
        if delay:
            await asyncio.sleep(delay)
        raise httpx.ReadError("mid-stream network break")

    return stream()


async def test_batch_native_mid_stream_error_503():
    """rev 8.9 MAJOR：**读体期** ``httpx.ReadError``（流中断）→ 503
    ``upstream_unavailable``（经 CodedHTTPException 渲染，错误体走
    gzip/Vary 惯例），非裸 500。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_mid_stream_breaker(),
            headers={"Content-Type": "application/json"},
        )

    app, _ = _build_app(_StubAux("disabled"), handler=handler)
    async with _client(app) as client:
        resp = await _post(client, ["h1"])
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"
    # 错误体惯例：CodedHTTPException handler 经 json_response 渲染
    # （gzip 协商 + Vary）
    assert "accept-encoding" in resp.headers.get("vary", "").lower()


async def test_batch_native_mid_stream_error_cancels_siblings():
    """rev 8.9 MAJOR × MAJOR-2 交叉：读体期网络错同样先受控收束
    siblings——慢响应任务被 cancel，永不完成；整响应 503。"""
    slow_done: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/h1":
            # 读体期中断（延迟少许让 h2 先启动）
            return httpx.Response(
                200, content=_mid_stream_breaker(delay=0.05),
                headers={"Content-Type": "application/json"},
            )
        assert request.url.path == "/session/h2"
        await asyncio.sleep(0.25)
        slow_done.append(True)
        return httpx.Response(
            200, content=orjson.dumps(_native_payload("h2")),
            headers={"Content-Type": "application/json"},
        )

    app, _ = _build_app(_StubAux("disabled"), handler=handler)
    async with _client(app) as client:
        resp = await _post(client, ["h1", "h2"])
        assert resp.status_code == 503
        assert resp.json()["code"] == "upstream_unavailable"
        # 留足窗口让潜在孤儿任务跑完 sleep——修复版此间无事可发生
        await asyncio.sleep(0.4)
        assert slow_done == []  # 慢任务被取消，从未完成
