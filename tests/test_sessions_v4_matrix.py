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
"""

from __future__ import annotations

import base64
import json
import sqlite3

import httpx
import orjson
import pytest
from fastapi import FastAPI

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
V3 = {"v": "3"}
V4 = {"v": "4"}
AL_NONEMPTY = ("/foo",)
HTTP_SESSIONS_BODY = orjson.dumps([
    {"id": "h1", "title": "up one", "directory": "/any",
     "time": {"created": 1, "updated": 2},
     "tokens": {"input": 9, "output": 8, "reasoning": 0,
                "cache": {"read": 1, "write": 2}}},
    {"id": "h2", "title": "up two", "directory": "/any"},
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


def _build_app(aux, *, settings: Settings | None = None, handler=None):
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
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4", "limit": "501"},
                                    headers=IDENTITY)
            assert resp.status_code == 422
            resp = await client.get("/slimapi/sessions",
                                    params={"v": "4", "limit": "500"},
                                    headers=IDENTITY)
            assert resp.status_code == 200
    finally:
        await aux.stop()


async def test_v4_no_etag_vary_304(tmp_path):
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
# v3 回归面（新 422 + 既有语义不变）
# ---------------------------------------------------------------------------


async def test_v3_v4_only_params_422():
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        for extra in ("archived=omit", "parent=all", "cursor=abc",
                      "archived=xyz", "archived=", "cursor="):
            resp = await client.get(f"/slimapi/sessions?v=3&{extra}",
                                    headers=IDENTITY)
            assert resp.status_code == 422, extra
            assert resp.json()["code"] == "param_version_mismatch"
    assert not seen


async def test_v3_roots_start_still_work():
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "3", "roots": "true",
                                        "start": "0"},
                                headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.json()["complete"] is True
    assert len(seen) == 1
    assert seen[0].url.params.get("roots") == "true"
    assert seen[0].url.params.get("start") == "0"


async def test_v3_etag_present_vs_v4_absent(tmp_path):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            v3 = await client.get("/slimapi/sessions", params={"v": "3"},
                                  headers=IDENTITY)
            v4 = await client.get("/slimapi/sessions", params={"v": "4"},
                                  headers=IDENTITY)
        assert v3.status_code == v4.status_code == 200
        assert v3.headers.get("ETag")
        assert v3.headers.get("Vary") == "Accept-Encoding"
        assert "ETag" not in v4.headers
        assert "Vary" not in v4.headers
    finally:
        await aux.stop()


async def test_v3_limit_1000_domain():
    app, _ = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "3", "limit": "1000"},
                                headers=IDENTITY)
        assert resp.status_code == 200
        resp = await client.get("/slimapi/sessions",
                                params={"v": "3", "limit": "1001"},
                                headers=IDENTITY)
        assert resp.status_code == 422  # FastAPI declarative domain


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
        assert items["ses_revert_full"]["revert"] == {
            "messageID": "msg_9", "partID": "prt_9"}
    finally:
        await aux.stop()


async def test_v4_degraded_upstream_param_mapping():
    # parent=none → roots=true；parent=all → 无 roots；archived 不透传
    for parent, expect_roots in (("none", "true"), ("all", None)):
        app, seen = _build_app(_StubAux("disabled"))
        async with _client(app) as client:
            resp = await client.get("/slimapi/sessions", params={
                "v": "4", "parent": parent}, headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.json()["degraded"] is True
        assert len(seen) == 1
        assert seen[0].url.params.get("roots") == expect_roots
        assert "archived" not in str(seen[0].url.params)
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
    assert item["project"] is None
    assert item["tokens_input"] == 9 and item["tokens_output"] == 8
    assert item["tokens_cache_read"] == 1 and item["tokens_cache_write"] == 2
    assert item["time"] == {"created": 1, "updated": 2}


# ---------------------------------------------------------------------------
# rev gate BLOCKER-1：busy 族 sqlite 异常 → 路由边界 503（不泄 SQLite 细节）
# ---------------------------------------------------------------------------


class _BusyAux:
    """db 可用面 + query 抛 busy（lifecycle busy 分类原样上抛的路由可见形态）。"""

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=True, mode="db", reason=None)

    async def query(self, sql, params=()):  # pragma: no cover - raise only
        raise sqlite3.OperationalError("database is locked")


@pytest.mark.parametrize(
    "exc",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database table is locked"),
        sqlite3.DatabaseError("file is not a database"),
    ],
)
async def test_busy_sqlite_error_maps_to_503_without_leak(exc):
    app, seen = _build_app(_BusyAux())
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


async def test_marker_v3_path_fields_absent():
    app, _ = _build_app(_StubAux("disabled"))
    sink: list[dict] = []
    async with _client(_state_capturing(app, sink)) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "3"}, headers=IDENTITY)
    assert resp.status_code == 200
    state = sink[-1]
    assert "slimapi_sessions_source" not in state
    assert "slimapi_degraded_503" not in state
