"""v4 sessions 降级矩阵全量测试（B3a-B4 + rev gate R1；v4-contract §4.2 / §11.3）。

- **144 等价类**（rev gate MAJOR-2：req 12 = archived×parent × db 3 ×
  al 2 × cursor {absent, present}）——期望值来自**独立手推判定函数**
  ``_expect_v4_outcome``（§4.2 formula 逐条手写，不调用生产降级逻辑、
  不复制其分支实现）；行集 oracle = ``v4_fixture.mirror_page``（S-B03
  镜像）。
- db 态注入：avail = fixture DB 上的真实 ``DbAuxiliarySource``；
  disabled/tripped = ``_StubAux``；busy = ``_BusyAux``（rev gate
  BLOCKER-1：查询期 sqlite 异常 → 503 不泄 SQLite 细节）。
- rev gate BLOCKER-3：坏行窗口锚点（items=[] 仍可 cursor 前进）。
- rev gate R2 接口：``request.state`` 降级标记（source=db/http、
  degraded_503）断言。

B12（2026-08-21 v4 自包含 golden 化清点）：本文件**无**「先发 ?v=3 求
期望再比 v4」形态——144 等价类的期望来自独立手推判定函数
``_expect_v4_outcome`` 与 ``v4_fixture.mirror_page`` 镜像 oracle（均非
v3 wire 路径），v4 断言本就自包含；文末 v3 回归面测试为 v3 守护网
（三分处置②，Phase 4 v3 面拆除前保留）。唯一清理：删除两个无消费者
的死常量 ``V3 = {"v": "3"}`` / ``V4 = {"v": "4"}``（各测试均内联
``params={"v": ...}``）。
"""

from __future__ import annotations

import base64
import json
import sqlite3

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi import readiness
from oc_slimapi.config import Settings
from oc_slimapi.dbaux import AuxiliaryUnavailableError, DbAuxiliarySource
from oc_slimapi.dbaux.cursor import allowlist_rev, encode_cursor
from oc_slimapi.dbaux.lifecycle import DbAuxStatus
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, sessions, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

from v4_fixture import DATASET, FIXED_NOW_MS, build_fixture_db, mirror_page

IDENTITY = {"Accept-Encoding": "identity"}
AL_NONEMPTY = ("/foo",)


@pytest.fixture(autouse=True)
def _session_single_revision_gate_off(monkeypatch):
    """§13 修订面（``session.single.projection.v4``）关闭态回归钉。

    本文件锁定 v4 sessions **4.0.0 已发布形态**（§4.2 降级矩阵 ×
    envelope 稀疏 degraded × item 无 partial/degraded 标记）——§13
    canonical 形状（envelope degraded 恒布尔 + item 标记 + 唯一
    canonical projector）由 tests/test_session_single_v4.py 双态覆盖
    （fix-9 集成门控模式：同断言集在门控两态下各自成立）。
    """
    monkeypatch.setattr(
        readiness, "SATISFIED",
        readiness.SATISFIED - {"session.single.projection.v4"},
    )


HTTP_SESSIONS_BODY = orjson.dumps([
    # D2-A: /experimental/session 走 sessions.listGlobal，行形如
    # {...fromRow(row), project: projects.get(row.project_id) ?? null}，
    # 即每行附带 project join（或 null）。mock 忠实复现该形状。
    {"id": "h1", "title": "up one", "directory": "/any",
     "time": {"created": 1, "updated": 2},
     "tokens": {"input": 9, "output": 8, "reasoning": 0,
                "cache": {"read": 1, "write": 2}},
     "project": {"id": "p1", "name": "proj", "worktree": None}},
    {"id": "h2", "title": "up two", "directory": "/any", "project": None},
])


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
    """db 不可用态的路由可见面（disabled / tripped 同形）。"""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=False, mode="http", reason=self._reason)

    async def query(self, sql, params=()):  # pragma: no cover - never reached
        raise AuxiliaryUnavailableError(self._reason)


def _build_app(aux, *, settings: Settings | None = None, handler=None,
                selector: bool = True):
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(
            200, content=HTTP_SESSIONS_BODY,
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
    for router in (health.router, versions.router, sessions.router):
        app.include_router(router)
    register_error_handlers(app)
    # selector=False → selector-less direct invocation (route default v3
    # view): keeps the v3-branch guard tests below exercisable until V2b
    # removes the branch (2026-08-21 narrowing note).
    if selector:
        app.add_middleware(SlimapiSelectorMiddleware)
    install_proxy(app)
    return app, seen


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def _real_aux(tmp_path):
    db = build_fixture_db(tmp_path / "m.db")
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available
    return source


async def _aux_for(db_state, tmp_path_factory):
    if db_state == "avail":
        tmp_path = tmp_path_factory.mktemp("db")
        return await _real_aux(tmp_path)
    reason = "disabled" if db_state == "disabled" else "circuit_open"
    return _StubAux(reason)


REQ_CASES = [
    (arch, parent)
    for arch in ("omit", "only", "all")
    for parent in ("all", "none", "only", "ses_root_1")
]
DB_STATES = ("avail", "disabled", "tripped")
AL_STATES = ("unset", "nonempty")
CURSOR_STATES = ("absent", "present")

MATRIX_IDS = [
    f"{arch}-{parent}-{db}-{al}-{cur}"
    for arch, parent in REQ_CASES
    for db in DB_STATES
    for al in AL_STATES
    for cur in CURSOR_STATES
]


def _mint_valid_cursor(arch: str, parent: str, al_entries: tuple[str, ...]) -> str:
    """铸合法 cursor（wire 输入构造——用编码器，而非期望判定）。"""
    return encode_cursor(8000, "ses_tie_c", {
        "archived": arch, "parent": parent,
        "search_hash": "",  # 矩阵 search 轴缺席
        "allowlist_rev": allowlist_rev(al_entries) if al_entries else "",
    })


def _expect_v4_outcome(
    arch: str, parent: str, db_state: str, al_state: str, cursor_state: str,
) -> str:
    """**独立期望判定**（rev gate MAJOR-2；S-B03 纪律）。

    §4.2 formula 手推真值表（本函数独立于 ``routes/sessions.py`` 的降级
    实现逐条重写，二者零共享代码；漂移时测试红即定位分歧）：

    1. db 可用 → 200 DB 投影（全过滤 SQL，cursor 为 keyset 同窗）；
    2. db 不可用：
       a. allowlist 非空 → 503（白名单子集性不可由上游保证）；
       b. cursor 在场 → 503（上游无 (t,i) keyset 等价物）；
       c. Class A（archived∈{omit,all} × parent∈{all,none}）→
          200 HTTP 降级 + degraded:true；
       d. 其余（Class B）→ 503。
    """
    if db_state == "avail":
        return "db_200"
    if al_state == "nonempty":
        return "s503"
    if cursor_state == "present":
        return "s503"
    if arch in ("omit", "all") and parent in ("all", "none"):
        return "http_200_degraded"
    return "s503"


# ---------------------------------------------------------------------------
# §11.3 144 等价类（MAJOR-2：cursor 状态轴入矩阵，逐格独立期望）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("matrix_id", MATRIX_IDS)
async def test_matrix_144_degradation_cells(tmp_path_factory, matrix_id):
    arch, parent, db_state, al_state, cursor_state = matrix_id.rsplit("-", 4)
    aux = await _aux_for(db_state, tmp_path_factory)
    settings = _settings(
        directory_allowlist=None if al_state == "unset" else list(AL_NONEMPTY),
    )
    app, seen = _build_app(aux, settings=settings)
    try:
        async with _client(app) as client:
            params = {"v": "4", "archived": arch, "parent": parent}
            al_entries = () if al_state == "unset" else AL_NONEMPTY
            if cursor_state == "present":
                params["cursor"] = _mint_valid_cursor(arch, parent, al_entries)
            resp = await client.get("/slimapi/sessions",
                                    params=params, headers=IDENTITY)
        expected = _expect_v4_outcome(arch, parent, db_state, al_state,
                                      cursor_state)
        if expected == "db_200":
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert "degraded" not in body
            cursor_arg = (8000, "ses_tie_c") if cursor_state == "present" else None
            exp_records, _ = mirror_page(
                archived=arch, parent=parent, allowlist=al_entries,
                cursor=cursor_arg, limit=100)
            assert [item["id"] for item in body["items"]] == \
                [r["id"] for r in exp_records]
        elif expected == "http_200_degraded":
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body.get("degraded") is True
            assert [item["id"] for item in body["items"]] == ["h1", "h2"]
            assert seen, "degraded path must hit upstream"
        else:  # s503
            assert resp.status_code == 503, resp.text
            _assert_aux_unavailable(resp)
    finally:
        if isinstance(aux, DbAuxiliarySource):
            await aux.stop()


def _assert_aux_unavailable(resp: httpx.Response) -> None:
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "30"
    body = resp.json()
    assert body["code"] == "auxiliary_unavailable"
    text = resp.text
    # 负向断言：不泄露 DB 路径 / schema / 白名单内容（§4.2）
    for leak in ("/tmp", ".db", "schema", "column", "/foo", "allowlist"):
        assert leak not in text


# ---------------------------------------------------------------------------
# cursor 正交轴 ×2（db-avail → 200 keyset；db 不可用 → 503）
# ---------------------------------------------------------------------------


async def test_cursor_axis_db_avail_keyset(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            first = await client.get("/slimapi/sessions", params={
                "v": "4", "limit": "5"}, headers=IDENTITY)
            assert first.status_code == 200
            body = first.json()
            assert body["complete"] is False
            assert body["nextCursor"]
            second = await client.get("/slimapi/sessions", params={
                "v": "4", "limit": "5", "cursor": body["nextCursor"],
            }, headers=IDENTITY)
            assert second.status_code == 200
            body2 = second.json()
            # 行集衔接 oracle：全量 = 第一页 + 第二页 + …（EQ-002 同构）
            ids = [item["id"] for item in body["items"]] + \
                [item["id"] for item in body2["items"]]
            expected, _ = mirror_page(archived="omit", parent="all", limit=100)
            assert ids == [r["id"] for r in expected][:len(ids)]
    finally:
        await aux.stop()


@pytest.mark.parametrize("db_state", ["disabled", "tripped"])
async def test_cursor_axis_db_unavailable_503(db_state):
    from oc_slimapi.dbaux.cursor import encode_cursor

    valid = encode_cursor(8000, "ses_tie_c", {
        "archived": "omit", "parent": "all",
        "search_hash": "", "allowlist_rev": "",
    })
    app, seen = _build_app(_StubAux(db_state))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", params={
            "v": "4", "cursor": valid}, headers=IDENTITY)
    assert resp.status_code == 503
    _assert_aux_unavailable(resp)
    assert not seen


# ---------------------------------------------------------------------------
# search 轴
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("db_state", ["disabled", "tripped"])
async def test_search_wildcard_db_unavailable_503_class_a_too(db_state):
    # 含通配 × Class A（omit×all）× al 空 → 仍 503（过滤语义永不降级）
    for wildcard in ("100%", "under_score", "back\\slash"):
        app, seen = _build_app(_StubAux(db_state))
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4", "search": wildcard}, headers=IDENTITY)
        assert resp.status_code == 503
        _assert_aux_unavailable(resp)
        assert not seen


async def test_search_literal_class_a_degraded_passthrough():
    # 纯字面 × Class A × db 不可用 → 200+degraded；上游收 normalized
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", params={
            "v": "4", "search": "  plain  "}, headers=IDENTITY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["degraded"] is True
    assert len(seen) == 1
    # D2-A: 降级端点锚定（search 参数走 /experimental/session）
    assert seen[0].url.path == "/experimental/session"
    assert seen[0].url.params.get("search") == "plain"  # trimmed


async def test_search_db_avail_rowset(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4", "search": "100%"}, headers=IDENTITY)
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["items"]]
        expected, _ = mirror_page(archived="omit", parent="all",
                                  search="100%", limit=100)
        assert ids == [r["id"] for r in expected]
        assert "ses_child_a" in ids  # “fix the 100% bug”
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# §8.3 优先级真值表（路由侧）
# ---------------------------------------------------------------------------


async def test_malformed_cursor_beats_503():
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", params={
            "v": "4", "cursor": "!!!not-base64url!!!"}, headers=IDENTITY)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_cursor"
    assert not seen


async def test_fingerprint_mismatch_beats_503():
    from oc_slimapi.dbaux.cursor import encode_cursor

    # 指纹按 archived=omit 构造，请求 archived=only → 不匹配 × db 不可用 → 400
    other = encode_cursor(1, "s", {
        "archived": "omit", "parent": "all",
        "search_hash": "", "allowlist_rev": "",
    })
    app, seen = _build_app(_StubAux("circuit_open"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", params={
            "v": "4", "archived": "only", "cursor": other}, headers=IDENTITY)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_cursor"
    assert not seen


async def test_fingerprint_mismatch_db_avail_400(tmp_path):
    from oc_slimapi.dbaux.cursor import encode_cursor

    other = encode_cursor(1, "s", {
        "archived": "all", "parent": "all",
        "search_hash": "", "allowlist_rev": "",
    })
    aux = await _real_aux(tmp_path)
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4", "cursor": other}, headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_cursor"
        assert not seen
    finally:
        await aux.stop()


async def test_valid_cursor_fingerprint_roundtrip(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            first = await client.get("/slimapi/sessions", params={
                "v": "4", "limit": "3"}, headers=IDENTITY)
            cursor = first.json()["nextCursor"]
            assert cursor
            second = await client.get("/slimapi/sessions", params={
                "v": "4", "limit": "3", "cursor": cursor}, headers=IDENTITY)
            assert second.status_code == 200
            page1 = [i["id"] for i in first.json()["items"]]
            page2 = [i["id"] for i in second.json()["items"]]
            assert not set(page1) & set(page2)
    finally:
        await aux.stop()


async def test_repeated_v_same_value_routes_normally(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions?v=4&v=4",
                                    headers=IDENTITY)
        assert resp.status_code == 200
        assert "degraded" not in resp.json()
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# v4 参数域
# ---------------------------------------------------------------------------


async def test_v4_roots_422():
    app, _ = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        for query in ("v=4&roots=true", "v=4&roots=false", "v=4&start=0"):
            resp = await client.get(f"/slimapi/sessions?{query}",
                                    headers=IDENTITY)
            assert resp.status_code == 422, query
            assert resp.json()["code"] == "param_version_mismatch", query
        # 空值形态走 FastAPI 声明式 422（bool 解析失败）——同为 422 域
        resp = await client.get("/slimapi/sessions?v=4&roots=",
                                headers=IDENTITY)
        assert resp.status_code == 422


async def test_v4_invalid_archived_422():
    app, _ = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4", "archived": "xyz"},
                                headers=IDENTITY)
    assert resp.status_code == 422
    assert resp.json()["code"] == "param_version_mismatch"


async def test_v4_limit_501_422_and_500_ok(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp_501 = await client.get("/slimapi/sessions",
                                        params={"v": "4", "limit": "501"},
                                        headers=IDENTITY)
            # F-025：501..1000 通过 FastAPI 声明域（le=1000）后由 handler
            # 接住 → coded 422 形状（{"code","hint"}，非框架 detail）。
            assert resp_501.status_code == 422
            assert resp_501.json()["code"] == "param_version_mismatch"
            assert "v4 limit domain is 1.." in resp_501.json()["hint"]
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4", "limit": "500"},
                                    headers=IDENTITY)
            assert resp.status_code == 200
    finally:
        await aux.stop()


async def test_v4_limit_1001_framework_422_shape():
    """F-025：limit=1001 在 FastAPI 声明域（ge=1, le=1000）外 → 框架 422。

    body 是 FastAPI 默认校验形状 ``{"detail": [...]}`` 且无 ``code`` 字段
    ——与 501..1000 的 coded 422 构成同族双形状，本用例锁定现状
    （N6：只断言键存在性，不断言框架文案全文）。
    """
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4", "limit": "1001"},
                                headers=IDENTITY)
    assert resp.status_code == 422
    body = resp.json()
    assert isinstance(body.get("detail"), list)
    assert "code" not in body
    assert not seen  # 校验层拒绝，未触上游


async def test_v4_archived_invalid_coded_422():
    """F-025：archived 非三态值 → 422 param_version_mismatch（coded 形状）。"""
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4", "archived": "sometimes"},
                                headers=IDENTITY)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "param_version_mismatch"
    assert "detail" not in body  # coded 形状（区别于框架 422）
    assert not seen


async def test_v4_parent_empty_coded_422():
    """F-025（N2）：parent="" → 422 param_version_mismatch（coded 形状）。"""
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4", "parent": ""},
                                headers=IDENTITY)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "param_version_mismatch"
    assert "detail" not in body
    assert not seen


async def test_v4_gate_off_no_etag_vary_304(tmp_path, monkeypatch):
    """门控关态（monkeypatch 排除 representation.vary.v4——4.2.0 集成收口
    后默认全 satisfied，关态须显式复现）：v4 sessions 维持 4.0.0 已发布
    行为（§4.4：无 ETag / 无 Vary / INM 不判定）。开态（ETag/Vary/304）
    由 test_sessions_v4_representation.py 锁定。"""
    monkeypatch.setattr(readiness, "SATISFIED", frozenset({
        "selector.v4",
        "session.list.global.v4",
        "events.global.replay.v4",
        "events.token.replay.v4",
    }))
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4"}, headers=IDENTITY)
            assert resp.status_code == 200
            assert "ETag" not in resp.headers
            assert "Vary" not in resp.headers
            resp304 = await client.get(
                "/slimapi/sessions", params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": '"anything"'})
            assert resp304.status_code == 200  # 无 validator → 永不 304
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# v3 回归面 — (REMOVED with the V2b src teardown: these five locks drove
# the selector-less default-3 view into the physically removed sessions v3
# leg — v4-only-param 422s on the v3 side, roots/start passthrough, the
# v3 ETag contrast, the v3 limit=1000 domain, and the no-request-state-
# marker regression. Under the v4-only window every scope runs the v4
# facade; the v4-side counterparts (roots/start → 422 param_version_
# mismatch, the v4 limit domain, §15 ETag/Vary) are locked above and in
# tests/test_sessions_v4_representation.py.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# happy path：骨架形状 + 分页 + complete
# ---------------------------------------------------------------------------


async def test_v4_skeleton_shape_and_paging(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4", "limit": "2"},
                                    headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert list(body) == ["items", "nextCursor", "complete"]
        assert body["complete"] is False
        assert body["nextCursor"]
        item = body["items"][0]
        # SessionSkeletonV4：v3 键 + project + tokens 平铺
        for key in ("id", "directory", "parentID", "projectID", "title",
                    "agent", "model", "time", "summary", "project",
                    "tokens_input", "tokens_output"):
            assert key in item, key
        assert "tokens" not in item
        # 逐页拼满 ≡ 全量（EQ-002 路由级）
        ids: list[str] = [i["id"] for i in body["items"]]
        cursor = body["nextCursor"]
        while cursor:
            async with _client(app) as client:
                page = await client.get("/slimapi/sessions", params={
                    "v": "4", "limit": "2", "cursor": cursor,
                }, headers=IDENTITY)
            page_body = page.json()
            ids.extend(i["id"] for i in page_body["items"])
            cursor = page_body["nextCursor"]
        expected, _ = mirror_page(archived="omit", parent="all", limit=100)
        assert ids == [r["id"] for r in expected]
    finally:
        await aux.stop()


async def test_v4_project_object_and_null(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4", "archived": "all"},
                                    headers=IDENTITY)
        items = {i["id"]: i for i in resp.json()["items"]}
        assert items["ses_root_1"]["project"] == {
            "id": "prj_alpha", "name": "alpha", "worktree": "/wt/alpha"}
        assert items["ses_orphan_proj"]["project"] is None
        # R5 MAJOR-1：project_id 与 project 两字段**独立**——orphan 行
        # projectID 非空 + project=null 同现（不从 join 反推）。
        assert items["ses_orphan_proj"]["projectID"] == "prj_missing"
        assert items["ses_revert_full"]["revert"] == {
            "messageID": "msg_9", "partID": "prt_9"}
    finally:
        await aux.stop()


async def test_v4_model_object_on_wire_bad_model_row_skipped(
    tmp_path, caplog
):
    """rev gate R5 BLOCKER-1 路由级断言：model 在 v4 wire 上是**对象**。

    - 每行 model 均为 dict（``isinstance(model, str)`` 绝不成立），id /
      providerID 值正确（= fixture model JSON 解析值）；
    - 坏 model JSON 行（ses_bad_model）按 §8 跳行 + warning——不出现在
      items，其余行正常。
    """
    import logging

    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        with caplog.at_level(logging.WARNING):
            async with _client(app) as client:
                resp = await client.get("/slimapi/sessions",
                                        params={"v": "4", "archived": "all"},
                                        headers=IDENTITY)
        assert resp.status_code == 200
        items = {i["id"]: i for i in resp.json()["items"]}
        dataset_by_id = {r["id"]: r for r in DATASET}
        assert "ses_bad_model" not in items
        assert "ses_bad_json" not in items
        assert len(items) == 22  # 24 原始 − 2 坏 JSON 行
        for sid, item in items.items():
            model = item["model"]
            assert not isinstance(model, str), (
                f"{sid}: model 是字符串（JSON 解析缺失回归）"
            )
            assert isinstance(model, dict), sid
            expected = orjson.loads(dataset_by_id[sid]["model"])
            assert model == expected, sid
            assert "id" in model and "providerID" in model
        # §8 跳行 warning（含 sid）
        assert any("ses_bad_model" in r.getMessage() for r in caplog.records), (
            "坏 model 行跳行未记 warning"
        )
    finally:
        await aux.stop()


@pytest.mark.parametrize(
    "raw_model",
    ["[]", '"scalar"', "123", "true"],
    ids=["array", "string-scalar", "number", "bool"],
)
async def test_v4_model_shape_gate_legal_json_non_dict_skipped(
    tmp_path, caplog, raw_model
):
    """rev gate R6：**合法 JSON 但非 dict 形状**的 model（数组/标量：
    ``[]`` / ``\"scalar\"`` / ``123`` / ``true``）同样违反冻结判据
    「v4 wire 的 model 必须是对象或 null」（v4-contract §4.1
    SessionSkeletonV4 + design §8 键集义务）→ 与语法错误同一 §8 跳行
    路径（跳行 + warning 含 sid 与 model 字样）。

    - 形状行不出现在 items；其余会话 200 正常（25 原始 − 2 坏 JSON −
      1 形状行 = 22 可见）；
    - wire 上所有非 null model 均为 dict（str/list/标量绝不出现）；
    - 行集与镜像 oracle（§8 形状语义独立重写，S-B03）一致。
    """
    import logging

    shape_sid = "zz_model_shape"
    shape_row = dict(DATASET[0])
    shape_row.update(
        id=shape_sid,
        title="legal json non-dict model",
        time_created=FIXED_NOW_MS + 10_000,
        time_updated=FIXED_NOW_MS + 100_000,
        model=raw_model,
    )
    rows = [shape_row] + list(DATASET)
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        with caplog.at_level(logging.WARNING):
            async with _client(app) as client:
                resp = await client.get("/slimapi/sessions",
                                        params={"v": "4", "archived": "all"},
                                        headers=IDENTITY)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert shape_sid not in {i["id"] for i in items}
        assert len(items) == 22
        for item in items:
            model = item["model"]
            assert model is None or isinstance(model, dict), (
                f"{item['id']}: model 非 dict|null（形状门泄漏）"
            )
            assert not isinstance(model, str), item["id"]
        # 与语法错误同一 §8 跳行路径：warning 含 sid 与 model 字样
        warns = [
            r.getMessage() for r in caplog.records
            if shape_sid in r.getMessage()
        ]
        assert len(warns) == 1 and "model" in warns[0], (
            f"形状行跳行 warning 缺失/不唯一: {warns}"
        )
        # 镜像 oracle 行集一致（§8 形状门独立重写）
        expected, _complete = mirror_page(
            archived="all", parent="all", limit=100, session_rows=rows,
        )
        assert [i["id"] for i in items] == [r["id"] for r in expected]
    finally:
        await aux.stop()


async def test_v3_model_object_passthrough_from_upstream():
    """rev gate R5 BLOCKER-1 第 6 条：v3 不受影响——v3 model 来自上游
    HTTP（已是对象），skeleton 透传对象而非字符串。"""
    upstream_session = {
        "id": "h1", "title": "up one", "directory": "/any",
        "model": {"id": "m-up", "providerID": "prov-up"},
        "time": {"created": 1, "updated": 2},
    }

    def handler(request):
        return httpx.Response(
            200, content=orjson.dumps([upstream_session]),
            headers={"Content-Type": "application/json"},
        )

    app, _ = _build_app(_StubAux("disabled"), handler=handler,
                        selector=False)
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", headers=IDENTITY)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["model"] == {"id": "m-up", "providerID": "prov-up"}
    assert isinstance(items[0]["model"], dict)

    # rev gate R6：v3 面对非 dict model（数组）照常透传——形状门只属于
    # dbaux v4 投影组装层；v3 契约形状由上游 wire 决定（回归确认）。
    list_session = dict(
        upstream_session, id="h2", model=["list", "from-upstream"],
    )

    def handler2(request):
        return httpx.Response(
            200, content=orjson.dumps([list_session]),
            headers={"Content-Type": "application/json"},
        )

    app2, _ = _build_app(_StubAux("disabled"), handler=handler2,
                         selector=False)
    async with _client(app2) as client:
        resp2 = await client.get("/slimapi/sessions", headers=IDENTITY)
    assert resp2.status_code == 200
    assert resp2.json()["items"][0]["model"] == ["list", "from-upstream"]


async def test_v4_degraded_upstream_param_mapping():
    # parent=none → roots=true；parent=all → 无 roots。
    # archived=omit → 不透传；archived=all → archived=true（rev-sgpt 终审
    # 补丁 2026-08-22：上游仅在真值时含 archived，session.ts:564
    # `if (!input?.archived)`——缺参必然排除，all 漏传即退化 omit）。
    # D2-A 端点锚定：Class A 降级必须打 /experimental/session（契约 §4.2
    # schema 权威/降级路径；/session 走 listByProject 会混入 archived 且
    # 塌缩到单一 project）。旧断言只锚参数不锚端点，漏掉了这一分歧——
    # mock 曾对两者都应答，端点换错时测试照常绿。
    cases = (
        # (parent, archived, expect_roots, expect_archived)
        ("none", "omit", "true", None),
        ("all", "omit", None, None),
        ("none", "all", "true", "true"),
        ("all", "all", None, "true"),
    )
    for parent, archived, expect_roots, expect_archived in cases:
        app, seen = _build_app(_StubAux("disabled"))
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4", "parent": parent, "archived": archived},
                headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.json()["degraded"] is True
        assert len(seen) == 1
        # 端点锚定 + 旧端点守卫（不再调用 /session）
        assert seen[0].url.path == "/experimental/session"
        assert seen[0].method == "GET"
        assert seen[0].url.path != "/session"
        assert seen[0].url.params.get("limit") == "100"
        assert seen[0].url.params.get("roots") == expect_roots
        assert seen[0].url.params.get("archived") == expect_archived
        assert "directory" not in str(seen[0].url.params)


async def test_v4_degraded_items_projection_shape():
    app, _ = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["nextCursor"] is None
    item = body["items"][0]
    assert item["id"] == "h1"
    # D2-A: 上游行现带 project join（见 HTTP_SESSIONS_BODY），门控关的
    # 4.0.0 已发布降级 item 形态仍冻结 project=None（degraded:true 标记）
    assert item["project"] is None
    assert item["tokens_input"] == 9 and item["tokens_output"] == 8
    assert item["tokens_cache_read"] == 1 and item["tokens_cache_write"] == 2
    assert item["time"] == {"created": 1, "updated": 2}


# ---------------------------------------------------------------------------
# rev gate BLOCKER-1：busy 族 sqlite 异常 → 路由边界 503（不泄 SQLite 细节）
# ---------------------------------------------------------------------------


class _BusyAux:
    """db 可用面 + query 抛 busy（lifecycle busy 分类原样上抛的路由可见形态）。

    rev gate MINOR-1：存下**参数化的**异常实例并原样抛出——三个参数化
    case 各自真正执行（此前的硬编码使参数未用、断言面退化）。
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=True, mode="db", reason=None)

    async def query(self, sql, params=()):  # pragma: no cover - raise only
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database table is locked"),
        sqlite3.DatabaseError("file is not a database"),
    ],
)
async def test_busy_sqlite_error_maps_to_503_without_leak(exc):
    app, seen = _build_app(_BusyAux(exc))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "30"
    body = resp.json()
    assert body["code"] == "auxiliary_unavailable"
    text = resp.text.lower()
    for leak in ("sqlite", "locked", "database error", "operationalerror",
                 "not a database"):
        assert leak not in text, leak
    assert not seen


# ---------------------------------------------------------------------------
# rev gate BLOCKER-2：空 i cursor 优先级（400 先于 503）
# ---------------------------------------------------------------------------


def _raw_cursor(doc: dict) -> str:
    blob = json.dumps(doc).encode("utf-8")
    return base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


async def test_empty_anchor_cursor_400_beats_db_unavailable():
    # 结构合法 + 指纹合法，仅 i=""：解码层拒绝 → 400（先于 503）
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions", params={
            "v": "4",
            "cursor": _raw_cursor({
                "t": 8000, "i": "",
                "f": {"archived": "omit", "parent": "all",
                      "search_hash": "", "allowlist_rev": ""},
            }),
        }, headers=IDENTITY)
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_cursor"
    assert not seen


async def test_empty_anchor_cursor_400_db_avail_too(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4",
                "cursor": _raw_cursor({
                    "t": 8000, "i": "",
                    "f": {"archived": "omit", "parent": "all",
                          "search_hash": "", "allowlist_rev": ""},
                }),
            }, headers=IDENTITY)
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_cursor"
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# rev gate BLOCKER-3：坏行窗口锚点（items 可空仍可前进）
# ---------------------------------------------------------------------------


def _rows_with_bad_head(bad_count: int) -> list[dict]:
    """在数据集顶部插入 bad_count 行坏 JSON（time_updated 递减占据窗口头）。

    坏行时间戳必须高于 ``FIXED_NOW_MS``（数据集最大合法时间），否则
    排不到窗口头。
    """
    base = dict(DATASET[0])
    rows: list[dict] = []
    for k in range(bad_count):
        row = dict(base)
        row.update(
            id=f"zz_bad_{k:02d}",
            title=f"broken {k}",
            time_created=FIXED_NOW_MS + 10_000 + k,
            time_updated=_BAD_BASE - k,
            summary_diffs="not-json{",
        )
        rows.append(row)
    return rows + list(DATASET)


_BAD_BASE = FIXED_NOW_MS + 100_000  # 坏行时间基线（高于数据集全部合法行）


async def _start_aux_on_rows(tmp_path, rows):
    db = build_fixture_db(tmp_path / "bad.db", session_rows=rows)
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available
    return source


async def test_all_bad_window_items_empty_but_cursor_advances(tmp_path):
    """整窗口全坏 JSON → items=[] + nextCursor 非空 + complete:false。"""
    rows = _rows_with_bad_head(3)
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4", "limit": "3"}, headers=IDENTITY)
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["complete"] is False
        assert body["nextCursor"], "坏行不丢锚点——必须可前进"
        # 下一页到达合法行（期望 = 镜像 oracle 同 cursor 同窗）
        anchor = (_BAD_BASE - 2, "zz_bad_02")
        async with _client(app) as client:
            nxt = await client.get("/slimapi/sessions", params={
                "v": "4", "limit": "3", "cursor": body["nextCursor"],
            }, headers=IDENTITY)
        assert nxt.status_code == 200
        nxt_body = nxt.json()
        expected, _ = mirror_page(archived="omit", parent="all", limit=3,
                                  cursor=anchor, session_rows=rows)
        assert [i["id"] for i in nxt_body["items"]] == [r["id"] for r in expected]
    finally:
        await aux.stop()


async def test_multi_page_bad_rows_reach_legal_and_complete(tmp_path):
    """连续多页坏行最终到达合法行 / complete:true；拼接 ≡ 镜像全量。"""
    rows = _rows_with_bad_head(5)
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        ids: list[str] = []
        cursor: str | None = None
        for _ in range(30):  # 上限防死循环（卡死回归即红）
            async with _client(app) as client:
                params = {"v": "4", "limit": "2"}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get("/slimapi/sessions", params=params,
                                        headers=IDENTITY)
            assert resp.status_code == 200
            body = resp.json()
            ids.extend(i["id"] for i in body["items"])
            cursor = body["nextCursor"]
            if body["complete"]:
                break
        else:
            pytest.fail("分页未收敛（BLOCKER-3 回归：卡死在坏行窗口）")
        expected, _ = mirror_page(archived="omit", parent="all", limit=100,
                                  session_rows=rows)
        assert ids == [r["id"] for r in expected]
    finally:
        await aux.stop()


async def test_first_window_no_visible_items_still_cursored(tmp_path):
    """首窗口可见 items 为空（limit=1 且顶行坏）仍产出 nextCursor。"""
    aux = await _start_aux_on_rows(tmp_path, _rows_with_bad_head(1))
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4", "limit": "1"}, headers=IDENTITY)
        body = resp.json()
        assert body["items"] == []
        assert body["nextCursor"] is not None
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# rev gate R2 观测接口：request.state 降级标记
# ---------------------------------------------------------------------------


def _state_capturing(app, sink: list[dict]):
    """外层 ASGI 包装：请求完成后快照 scope["state"]（标记写入可见）。"""
    async def wrapped(scope, receive, send):
        await app(scope, receive, send)
        if scope["type"] == "http":
            sink.append(dict(scope.get("state") or {}))
    return wrapped


async def test_marker_db_200_source_db(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    sink: list[dict] = []
    try:
        async with _client(_state_capturing(app, sink)) as client:
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4"}, headers=IDENTITY)
        assert resp.status_code == 200
        state = sink[-1]
        assert state.get("slimapi_sessions_source") == "db"
        assert "slimapi_degraded_503" not in state
    finally:
        await aux.stop()


async def test_marker_class_a_http_source():
    app, _ = _build_app(_StubAux("disabled"))
    sink: list[dict] = []
    async with _client(_state_capturing(app, sink)) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.json()["degraded"] is True
    state = sink[-1]
    assert state.get("slimapi_sessions_source") == "http"
    assert "slimapi_degraded_503" not in state


async def test_marker_503_degraded_flag():
    # Class B（archived=only）× db 不可用 → 503 fail-closed 标记
    app, _ = _build_app(_StubAux("circuit_open"))
    sink: list[dict] = []
    async with _client(_state_capturing(app, sink)) as client:
        resp = await client.get("/slimapi/sessions", params={
            "v": "4", "archived": "only"}, headers=IDENTITY)
    assert resp.status_code == 503
    state = sink[-1]
    assert state.get("slimapi_degraded_503") is True
    assert "slimapi_sessions_source" not in state


# (test_marker_v3_path_fields_absent was removed with the V2b src teardown:
# it locked that the selector-less default-3 sessions path set NO request
# state markers — that leg no longer exists; the v4 paths' marker
# discipline (slimapi_sessions_source / slimapi_degraded_503) is locked
# by the degraded-503 and Class-A tests above.)
