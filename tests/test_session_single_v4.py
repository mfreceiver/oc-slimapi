"""V4 正式修订 §13：canonical projector 统一（rev-cgpt 门禁修复）。

`GET /slimapi/session/{sid}?v=4` 单查 + `GET /slimapi/sessions?v=4` 列表
共用**同一 canonical item projector**（§13.3「同一 projector 不变量」）：

* §13.1 canonical 形状：裸对象（单查）/ envelope degraded 恒布尔（列表，
  含 false）；required nullable 恒发（业务 null → null；来源不可得 →
  null + partial:true + degraded:true，§13.2b 三态）；project 双形态
  （projectID null → 键缺席；join 缺行/无效 → null + 标记，§13.5）。
* §13.4 envelope 聚合：``envelope.degraded == any(item.degraded) ∨
  native fallback``——DB 列表含 partial item（orphan join 失败）→
  envelope degraded:true；全部 item 正常 → false。
* §13.2 类型/约束冻结（:521-575）：required 非 nullable 字段类型/约束
  违约（id/directory 非空字符串、title 字符串可空串、time.created/
  updated 非负数值）→ projector 不可表示 → 整响应 503；nullable 对象
  （summary 数值三元组 / model / revert 子字段类型）畸形 → 整体
  null+partial——**禁发含 null 子值的畸形对象**。
* parity：列表 item 与单查响应**冻结字段全集双向 key-set 全等** +
  逐字段同值（同一 DB fixture 同输入行）——能发现任一侧缺 required 键。
* 三态矩阵：explicit null（不 partial）/ 键 absent（null+partial）/ 有值。
* 门控（§3.3）：``session.single.projection.v4 ∈ SATISFIED`` 时 §13 修订面
  生效（列表 item 标记 + envelope required degraded + 单查 v4 分叉）；
  关闭态回归 4.0.0 已发布形态（列表稀疏 degraded、item 无标记、单查
  v3 skeleton 投影路径）。
"""

from __future__ import annotations

import sqlite3

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi import readiness
from oc_slimapi.config import Settings
from oc_slimapi.dbaux import AuxiliaryUnavailableError, DbAuxiliarySource
from oc_slimapi.dbaux.lifecycle import DbAuxStatus
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, read_groups, sessions, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.skeleton import (
    canonical_session_skeleton_v4,
    native_session_to_record,
    skeleton_session,
)
from oc_slimapi.transform import TransformConfig, TransformPool

from v4_fixture import DATASET, FIXED_NOW_MS, build_fixture_db

IDENTITY = {"Accept-Encoding": "identity"}
V4 = {"v": "4"}

# §13.1 canonical item 冻结字段全集（presence 双向校验用——缺任一键即违约）。
# 注意 ``project`` 条件在场：projectID null → 缺席（§13.5 两形态）。
CANONICAL_ITEM_KEYS = frozenset({
    "id", "directory", "parentID", "projectID", "project", "title",
    "agent", "model", "time", "summary",
    "tokens_input", "tokens_output", "tokens_reasoning",
    "tokens_cache_read", "tokens_cache_write", "revert",
    "partial", "degraded",
})
CANONICAL_ITEM_KEYS_NO_PROJECT = CANONICAL_ITEM_KEYS - {"project"}


def _canonical_keys(project_id) -> frozenset:
    """§13.5：project 键在场 ⇔ projectID 非空（缺席/null 两形态）。"""
    return (CANONICAL_ITEM_KEYS if project_id is not None
            else CANONICAL_ITEM_KEYS_NO_PROJECT)
CANONICAL_KEY_ORDER = [
    "id", "directory", "parentID", "projectID", "project", "title",
    "agent", "model", "time", "summary",
    "tokens_input", "tokens_output", "tokens_reasoning",
    "tokens_cache_read", "tokens_cache_write", "revert",
    "partial", "degraded",
]

# 上游单查 payload（SessionInfo camelCase 全量形态 + 投影外杂键）。
UPSTREAM_SINGLE = {
    "id": "h1", "title": "up one", "directory": "/any",
    "parentID": "p9", "projectID": "prj_alpha",
    "agent": "build", "model": {"id": "m1", "providerID": "prov"},
    "time": {"created": 11, "updated": 22, "archived": 33},
    "summary": {"additions": 1, "deletions": 2, "files": 3},
    "tokens": {"input": 9, "output": 8, "reasoning": 0,
               "cache": {"read": 1, "write": 2}},
    "revert": {"messageID": "m", "partID": "p", "extra": 1},
    "cost": {"total": 5}, "status": "IDLE", "version": "1.2.3",
}
EXPECTED_FALLBACK_SINGLE = {
    "id": "h1", "directory": "/any", "parentID": "p9",
    "projectID": "prj_alpha", "project": None,
    "title": "up one", "agent": "build",
    "model": {"id": "m1", "providerID": "prov"},
    "time": {"created": 11, "updated": 22, "archived": 33},
    "summary": {"additions": 1, "deletions": 2, "files": 3},
    "tokens_input": 9, "tokens_output": 8, "tokens_reasoning": 0,
    "tokens_cache_read": 1, "tokens_cache_write": 2,
    "revert": {"messageID": "m", "partID": "p"},
    "partial": True, "degraded": True,
}


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

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=False, mode="http", reason=self._reason)

    async def query(self, sql, params=()):  # pragma: no cover - never reached
        raise AuxiliaryUnavailableError(self._reason)


class _BusyAux:

    def __init__(self, exc: sqlite3.Error) -> None:
        self._exc = exc

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=True, mode="sqlite", reason="ok")

    async def query(self, sql, params=()):
        raise self._exc


class _RacedAux:

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=True, mode="sqlite", reason="ok")

    async def query(self, sql, params=()):
        raise AuxiliaryUnavailableError("raced disable")


def _build_app(aux, *, settings: Settings | None = None, handler=None,
                 selector: bool = True):
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(
            200, content=orjson.dumps(UPSTREAM_SINGLE),
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
    # selector=False → selector-less direct invocation (route default view 3);
    # used by the v3-branch regression lock below (V2b removes it with the
    # v3-branch teardown).
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


async def _canonical_aux(tmp_path):
    """canonical 列表 fixture：DATASET 去掉不可表示行（§13.2 directory
    非空字符串——``ses_legacy_empty`` 空目录行在 canonical 面为不可表示
    item，混入即整响应 503 §13.2c，由专测锁定；坏 JSON 行由
    ``rows_to_records`` §8 skip，无害）。"""
    rows = [r for r in DATASET if r["id"] != "ses_legacy_empty"]
    db = build_fixture_db(tmp_path / "c.db", session_rows=rows)
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available
    return source


async def _start_aux_on_rows(tmp_path, rows):
    db = build_fixture_db(tmp_path / "s.db", session_rows=rows)
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


def _assert_aux_unavailable(resp) -> None:
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "auxiliary_unavailable"
    assert resp.headers.get("Retry-After") == "30"
    text = resp.text
    for leak in ("/tmp", ".db", "schema", "column", "/foo", "allowlist",
                 "sqlite", "SELECT"):
        assert leak not in text


# ---------------------------------------------------------------------------
# §13.3 同一 projector 不变量：列表 item 与单查双向 key-set + 逐字段全等
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sid", [
    "ses_root_1", "ses_child_a", "ses_orphan_proj", "ses_revert_full",
    "ses_time_zero",
])
async def test_v4_canonical_parity_list_single(tmp_path, sid):
    """同一 DB fixture：单查响应与列表 item 冻结字段全集**双向全等** +
    逐字段同值——任一侧缺 required 键（partial/degraded/revert/…）即失败。"""
    aux = await _canonical_aux(tmp_path)
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            listing = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "archived": "all"}, headers=IDENTITY)
            assert listing.status_code == 200
            envelope = listing.json()
            # §13.4：envelope degraded == any(item.degraded)——DATASET 含
            # orphan join 失败行（partial item）→ 聚合为 true（required 布尔）
            assert envelope["degraded"] is True
            list_item = next(i for i in envelope["items"] if i["id"] == sid)

            resp = await client.get(f"/slimapi/session/{sid}",
                                    params=V4, headers=IDENTITY)
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"
        single = resp.json()
        # 裸对象，无 envelope（§13.1）
        for envelope_key in ("items", "nextCursor", "complete"):
            assert envelope_key not in single
        # 双向 key-set 全等（冻结全集）
        assert set(single.keys()) == CANONICAL_ITEM_KEYS
        assert set(list_item.keys()) == CANONICAL_ITEM_KEYS
        # 同输入逐字段同值（§13.3）
        assert single == list_item
        # 标记（§13.4/§13.5）：DATASET 仅 orphan join 缺行
        if sid == "ses_orphan_proj":
            assert single["project"] is None
            assert single["partial"] is True
            assert single["degraded"] is True
        else:
            assert single["partial"] is False
            assert single["degraded"] is False
        assert seen == []
    finally:
        await aux.stop()


async def test_v4_db_single_canonical_shape(tmp_path):
    rows = [_custom_row(
        "ses_full",
        revert='{"messageID":"msg_1","partID":"prt_1","junk":9}',
        time_archived=FIXED_NOW_MS + 5,
    )]
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/session/ses_full",
                                    params=V4, headers=IDENTITY)
        assert resp.status_code == 200
        single = resp.json()
        # §13.1 canonical 字段序（含 project/revert 全量形态）
        assert list(single.keys()) == CANONICAL_KEY_ORDER
        assert single["project"] == {
            "id": "prj_alpha", "name": "alpha", "worktree": "/wt/alpha"}
        assert single["revert"] == {"messageID": "msg_1", "partID": "prt_1"}
        assert single["time"]["archived"] == FIXED_NOW_MS + 5
        assert single["partial"] is False
        assert single["degraded"] is False
    finally:
        await aux.stop()


async def test_v4_db_required_nullable_always_emitted(tmp_path):
    """§13.2 required nullable 恒发：DB 行 revert NULL / time_archived
    NULL / tokens 计量存在 → ``revert: null``（键在场，非缺席）。"""
    rows = [_custom_row("ses_nulls", revert=None, time_archived=None)]
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/session/ses_nulls",
                                    params=V4, headers=IDENTITY)
        assert resp.status_code == 200
        single = resp.json()
        assert set(single.keys()) == CANONICAL_ITEM_KEYS
        assert single["revert"] is None  # 业务合法 null：键在场值 null
        assert single["time"]["archived"] is None
        assert single["partial"] is False
        assert single["degraded"] is False
    finally:
        await aux.stop()


async def test_v4_db_single_project_variants(tmp_path):
    # 真库 session.project_id NOT NULL → projectID-null 形态由 native
    # 三态测试覆盖；此处锁定 join 失败两形态 + name/对象形。
    rows = [
        _custom_row("ses_dangling", project_id="prj_missing"),
    ]
    projects = [
        {"id": "prj_alpha", "name": "alpha", "worktree": "/wt/alpha"},
        {"id": "prj_nowt", "name": "nowt", "worktree": ""},
        {"id": "prj_noname", "name": None, "worktree": "/wt/noname"},
    ]
    rows.append(_custom_row("ses_emptywt", project_id="prj_nowt"))
    rows.append(_custom_row("ses_noname", project_id="prj_noname"))
    db = build_fixture_db(tmp_path / "v.db", session_rows=rows,
                          project_rows=projects)
    aux = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    assert (await aux.start()).available
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            dangling = await client.get("/slimapi/session/ses_dangling",
                                        params=V4, headers=IDENTITY)
            emptywt = await client.get("/slimapi/session/ses_emptywt",
                                       params=V4, headers=IDENTITY)
            noname = await client.get("/slimapi/session/ses_noname",
                                      params=V4, headers=IDENTITY)

        for resp in (dangling, emptywt):
            assert resp.status_code == 200
            body = resp.json()
            assert body["project"] is None
            assert body["projectID"] is not None
            assert body["partial"] is True and body["degraded"] is True

        assert noname.status_code == 200
        body = noname.json()
        assert body["project"] == {"id": "prj_noname",
                                   "worktree": "/wt/noname"}
        assert "name" not in body["project"]
        assert body["partial"] is False and body["degraded"] is False
    finally:
        await aux.stop()


async def test_v4_db_single_tokens_valued_parity(tmp_path):
    rows = [_custom_row("ses_tok_valued")]
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            listing = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "archived": "all"}, headers=IDENTITY)
            valued = await client.get("/slimapi/session/ses_tok_valued",
                                      params=V4, headers=IDENTITY)
        assert valued.status_code == 200
        items = listing.json()["items"]
        list_item = next(i for i in items if i["id"] == "ses_tok_valued")
        for key in ("tokens_input", "tokens_output", "tokens_reasoning",
                    "tokens_cache_read", "tokens_cache_write"):
            assert valued.json()[key] == list_item[key]
            assert valued.json()[key] is not None
    finally:
        await aux.stop()


def test_v4_canonical_seam_tokens_null_no_partial():
    """§13.2b 三态①：tokens 列 NULL → 五键 null，不置 partial。

    真库 DDL 五列 ``INTEGER NOT NULL``（eqp_matrix PRAGMA 对齐）→ DB
    路径不可达 NULL；wire 语义由此 seam 单测 + native 三态测试锁定。
    """
    row = {
        "id": "ses_nulltok", "directory": "/foo", "parent_id": None,
        "project_id": "prj_alpha", "title": "t", "agent": "a",
        "model": None, "time_created": 1, "time_updated": 2,
        "time_archived": None, "summary_additions": None,
        "summary_deletions": None, "summary_files": None,
        "tokens_input": None, "tokens_output": None,
        "tokens_reasoning": None, "tokens_cache_read": None,
        "tokens_cache_write": None, "revert": None,
        "p_id": "prj_alpha", "p_name": "alpha", "p_worktree": "/wt/alpha",
    }
    single = canonical_session_skeleton_v4(row)
    assert single is not None
    for key in ("tokens_input", "tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write"):
        assert single[key] is None
    assert single["revert"] is None
    assert single["partial"] is False
    assert single["degraded"] is False
    assert list(single.keys()) == CANONICAL_KEY_ORDER


async def test_v4_db_single_unknown_sid_404(tmp_path):
    aux = await _real_aux(tmp_path)
    app, seen = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/session/ses_nope",
                                    params=V4, headers=IDENTITY)
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "session_not_found"
        assert body["sessionID"] == "ses_nope"
        assert seen == []
    finally:
        await aux.stop()


async def test_v4_db_single_bad_row_503(tmp_path):
    """§13.2c：行存在但不可投影（坏 JSON 列）→ 整响应 503，不混装。"""
    rows = [_custom_row("ses_bad", model="{not json")]
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/session/ses_bad",
                                    params=V4, headers=IDENTITY)
        _assert_aux_unavailable(resp)
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# 列表 canonical 面：envelope degraded 恒布尔 + item 标记 + fallback 项
# ---------------------------------------------------------------------------

async def test_v4_list_db_canonical_face(tmp_path):
    aux = await _canonical_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "archived": "all"}, headers=IDENTITY)
        assert resp.status_code == 200
        envelope = resp.json()
        # §13.4：orphan partial item 存在 → envelope degraded 聚合为 true
        assert envelope["degraded"] is True
        items = envelope["items"]
        assert items
        for item in items:
            assert set(item.keys()) == CANONICAL_ITEM_KEYS
            assert isinstance(item["partial"], bool)
            assert isinstance(item["degraded"], bool)
            assert item["revert"] is None or isinstance(item["revert"], dict)
            # §13.4 单向蕴含：partial ⇒ degraded（DB 态无 fallback 位）
            if item["partial"]:
                assert item["degraded"] is True
        orphan = next(i for i in items if i["id"] == "ses_orphan_proj")
        assert orphan["partial"] is True and orphan["degraded"] is True
        clean = next(i for i in items if i["id"] == "ses_root_1")
        assert clean["partial"] is False and clean["degraded"] is False
    finally:
        await aux.stop()


async def test_v4_list_db_envelope_degraded_false_when_all_items_clean(tmp_path):
    """§13.4 反向：全部 item 正常（join 成功、无缺列）→ envelope
    ``degraded:false``（required 布尔恒发，false 不省略）。"""
    rows = [
        _custom_row("ses_clean_a"),
        _custom_row("ses_clean_b", time_archived=FIXED_NOW_MS + 1),
    ]
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "archived": "all"}, headers=IDENTITY)
        assert resp.status_code == 200
        envelope = resp.json()
        assert envelope["degraded"] is False
        assert len(envelope["items"]) == 2
        for item in envelope["items"]:
            assert item["partial"] is False
            assert item["degraded"] is False
    finally:
        await aux.stop()


async def test_v4_db_single_legacy_empty_directory_503(tmp_path):
    """§13.2 directory 非空字符串：空目录行 = canonical 不可表示 →
    单查整响应 503（§13.2a；禁占位值/砍字段）。"""
    rows = [_custom_row("ses_le", directory="")]
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get("/slimapi/session/ses_le",
                                    params=V4, headers=IDENTITY)
        _assert_aux_unavailable(resp)
    finally:
        await aux.stop()


async def test_v4_list_unrepresentable_row_503(tmp_path):
    """§13.2c：列表窗口混入不可表示行（空 directory）→ 整响应 503，
    不发残 item。"""
    rows = [
        _custom_row("ses_ok"),
        _custom_row("ses_bad_dir", directory=""),
    ]
    aux = await _start_aux_on_rows(tmp_path, rows)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "archived": "all"}, headers=IDENTITY)
        _assert_aux_unavailable(resp)
    finally:
        await aux.stop()


async def test_v4_list_fallback_canonical_face():
    """Class A fallback：items 经同一 canonical projector——标记恒在、
    required nullable 恒发、project join 不可用 → null+partial。"""
    upstream_items = [
        {"id": "h1", "title": "one", "directory": "/any",
         "projectID": "prj_x", "time": {"created": 1, "updated": 2},
         "agent": None, "revert": None,
         "tokens": {"input": 5}},
        {"id": "h2", "title": "two", "directory": "/any",
         "time": {"created": 1, "updated": 2}},
    ]
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(
            200, content=orjson.dumps(upstream_items),
            headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["degraded"] is True
    items = envelope["items"]
    assert len(items) == 2
    h1, h2 = items
    # h1：projectID 非空 + native 无 join → project:null + partial
    assert set(h1.keys()) == CANONICAL_ITEM_KEYS
    assert h1["project"] is None
    assert h1["agent"] is None and h1["partial"] is True
    assert h1["tokens_input"] == 5
    assert h1["degraded"] is True
    # h2：projectID 缺失（来源不可得）→ null+partial；project 缺席
    assert set(h2.keys()) == _canonical_keys(h2["projectID"])
    assert h2["projectID"] is None
    assert "project" not in h2
    assert h2["revert"] is None  # required nullable 恒发
    assert h2["partial"] is True and h2["degraded"] is True


async def test_v4_list_fallback_unrepresentable_item_503():
    """§13.2c：fallback 载荷含不可表示 item（缺 title）→ 整响应 503。"""
    upstream_items = [
        {"id": "h1", "title": "one", "directory": "/any",
         "time": {"created": 1, "updated": 2}},
        {"id": "h2", "directory": "/any",
         "time": {"created": 1, "updated": 2}},  # title 不可得
    ]
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(
            200, content=orjson.dumps(upstream_items),
            headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    _assert_aux_unavailable(resp)


# ---------------------------------------------------------------------------
# §13.2b 三态矩阵（native 单查）：explicit null / absent / valued
# ---------------------------------------------------------------------------

def _native_session(**overrides) -> dict:
    """三态测试基线：全字段 explicit（含 null）——仅覆盖被测轴。"""
    base = {
        "id": "h1", "title": "t", "directory": "/d",
        "parentID": None, "projectID": None,
        "agent": None, "model": None,
        "time": {"created": 1, "updated": 2, "archived": None},
        "summary": None,
        "tokens": None,
        "revert": None,
    }
    base.update(overrides)
    return base


async def _native_single(payload: dict):
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(
            200, content=orjson.dumps(payload),
            headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    return resp


@pytest.mark.parametrize("field,null_value,valued_value", [
    ("agent", None, "build"),
    ("revert", None, {"messageID": "m", "partID": "p"}),
    ("summary", None, {"additions": 1, "deletions": 2, "files": 3}),
    ("model", None, {"id": "m1", "providerID": "prov"}),
    ("parentID", None, "p9"),
])
async def test_v4_native_three_states(field, null_value, valued_value):
    """三态：① explicit null → null 不 partial；② absent → null+partial；
    ③ valued → 值。基线全 explicit-null → partial False（fallback degraded
    恒 true 与 partial 正交）。"""
    # ① explicit null（基线即全 null）
    resp = await _native_single(_native_session())
    assert resp.status_code == 200
    body = resp.json()
    assert body[field] is None if field != "summary" else body["summary"] is None
    assert body["partial"] is False
    assert body["degraded"] is True  # fallback 态 item degraded 恒 true

    # ② absent（仅被测字段抽掉）
    payload = _native_session()
    del payload[field]
    resp = await _native_single(payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body[field] is None
    assert body["partial"] is True
    assert body["degraded"] is True

    # ③ valued
    payload = _native_session(**{field: valued_value})
    resp = await _native_single(payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body[field] == valued_value
    assert body["partial"] is False
    assert body["degraded"] is True


async def test_v4_native_tokens_three_states():
    tokens_full = {"input": 9, "output": 8, "reasoning": 0,
                   "cache": {"read": 1, "write": 2}}
    flat_keys = {"tokens_input": 9, "tokens_output": 8,
                 "tokens_reasoning": 0, "tokens_cache_read": 1,
                 "tokens_cache_write": 2}
    # ① explicit null
    resp = await _native_single(_native_session(tokens=None))
    body = resp.json()
    for key in flat_keys:
        assert body[key] is None
    assert body["partial"] is False
    # ② absent
    payload = _native_session()
    del payload["tokens"]
    body = (await _native_single(payload)).json()
    for key in flat_keys:
        assert body[key] is None
    assert body["partial"] is True
    # ③ valued（部分子键缺失 → 该子键 null+partial，其余有值）
    resp = await _native_single(_native_session(tokens=tokens_full))
    body = resp.json()
    for key, value in flat_keys.items():
        assert body[key] == value
    assert body["partial"] is False
    partial_tokens = {"input": 9}  # 其余子键不可得
    body = (await _native_single(
        _native_session(tokens=partial_tokens))).json()
    assert body["tokens_input"] == 9
    assert body["tokens_output"] is None
    assert body["partial"] is True


async def test_v4_native_project_id_states():
    # projectID explicit null → project 键缺席 + 无 partial
    body = (await _native_single(_native_session())).json()
    assert body["projectID"] is None
    assert "project" not in body
    assert body["partial"] is False
    # projectID absent → 来源不可得：null + partial（project 缺席）
    payload = _native_session()
    del payload["projectID"]
    body = (await _native_single(payload)).json()
    assert body["projectID"] is None
    assert "project" not in body
    assert body["partial"] is True
    # projectID 非空 + native 无 join → project:null + partial（§13.5）
    body = (await _native_single(_native_session(projectID="prj_x"))).json()
    assert body["projectID"] == "prj_x"
    assert body["project"] is None
    assert body["partial"] is True


async def test_v4_native_time_archived_states():
    # archived explicit null → null 不 partial
    body = (await _native_single(
        _native_session(time={"created": 1, "updated": 2, "archived": None}))
    ).json()
    assert body["time"]["archived"] is None
    assert body["partial"] is False
    # archived 子键缺失 → null + partial
    body = (await _native_single(
        _native_session(time={"created": 1, "updated": 2}))).json()
    assert body["time"]["archived"] is None
    assert body["partial"] is True


# ---------------------------------------------------------------------------
# §13.2 类型/约束冻结（rev 二轮 P0-2）：required 违约 → 整响应 503；
# nullable 对象畸形 → 整体 null+partial（禁发含 null 子值的畸形对象）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"id": 42},                       # 非字符串
    {"id": ""},                       # 空字符串
    {"id": None},                     # explicit null
    {"directory": 123},               # 非字符串
    {"directory": ""},                # 空字符串（全局列表强制非空）
    {"directory": None},
    {"title": 42},                    # 非字符串（可空串但不可非串/null）
    {"title": None},
    {"time": {"created": "2024-01-01", "updated": 2}},   # created 字符串
    {"time": {"created": 1, "updated": "3"}},            # updated 字符串
    {"time": {"created": -5, "updated": 2}},             # created 负数
    {"time": {"created": 1, "updated": -0.5}},           # updated 负数
    {"time": {"created": True, "updated": 2}},           # bool 非 JSON number
])
async def test_v4_native_required_malformed_503(overrides):
    """§13.2a + 类型冻结（:555-575）：required 非 nullable 字段类型/约束
    违约 → canonical 不可表示 → 整响应 503（不伪装值、不砍字段）。"""
    payload = dict(UPSTREAM_SINGLE)
    payload.update(overrides)
    resp = await _native_single(payload)
    _assert_aux_unavailable(resp)


def _project_native_item(item: dict):
    """seam：native item → canonical projector（与 wire 同一代码路径）。"""
    return canonical_session_skeleton_v4(
        native_session_to_record(item), fallback=True)


def _clean_native(**overrides) -> dict:
    base = {
        "id": "h1", "title": "t", "directory": "/d",
        "parentID": None, "projectID": None,
        "agent": None, "model": None,
        "time": {"created": 1, "updated": 2, "archived": None},
        "summary": None, "tokens": None, "revert": None,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("summary_value", [
    {"additions": 1},                                  # 子键部分缺失
    {"additions": 1, "deletions": 2},                  # 缺 files
    {"additions": 1, "deletions": "2", "files": 3},    # 子值类型错
    {"additions": None, "deletions": 2, "files": 3},   # null 子值
    42,                                                # 非对象非 null
])
def test_v4_native_summary_malformed_null_partial(summary_value):
    """§13.2 summary 对象时三子键均为数值——可读但畸形 → 整体
    null + partial（禁发含 null 子值的畸形对象）。"""
    single = _project_native_item(_clean_native(summary=summary_value))
    assert single is not None
    assert single["summary"] is None
    assert single["partial"] is True
    assert single["degraded"] is True


def test_v4_native_summary_valid_triple_object():
    single = _project_native_item(_clean_native(
        summary={"additions": 0, "deletions": 0, "files": 0}))
    assert single["summary"] == {"additions": 0, "deletions": 0, "files": 0}
    assert single["partial"] is False


def test_v4_native_summary_explicit_null_no_partial():
    single = _project_native_item(_clean_native(summary=None))
    assert single["summary"] is None
    assert single["partial"] is False


def test_v4_native_summary_absent_null_partial():
    payload = _clean_native()
    del payload["summary"]
    single = _project_native_item(payload)
    assert single["summary"] is None
    assert single["partial"] is True


def test_v4_native_tokens_type_malformed_null_partial():
    # 子值类型错（字符串计量）→ 该字段 null + partial；其余子键正常
    single = _project_native_item(_clean_native(tokens={
        "input": "9", "output": 8, "reasoning": 0,
        "cache": {"read": "1", "write": 2},
    }))
    assert single["tokens_input"] is None
    assert single["tokens_output"] == 8
    assert single["tokens_reasoning"] == 0
    assert single["tokens_cache_read"] is None
    assert single["tokens_cache_write"] == 2
    assert single["partial"] is True


def test_v4_native_tokens_zero_values_legal():
    # 合法边界：全 0 计量 → 数值照常（0 是值不是缺位）→ 不 partial
    single = _project_native_item(_clean_native(tokens={
        "input": 0, "output": 0, "reasoning": 0,
        "cache": {"read": 0, "write": 0},
    }))
    for key in ("tokens_input", "tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write"):
        assert single[key] == 0
    assert single["partial"] is False


def test_v4_native_tokens_subkey_null_business_null():
    # 子键 explicit null = 上游确无计量 → null 不 partial（§13.2b ①）
    single = _project_native_item(_clean_native(tokens={
        "input": 5, "output": None, "reasoning": None,
        "cache": {"read": None, "write": None},
    }))
    assert single["tokens_input"] == 5
    for key in ("tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write"):
        assert single[key] is None
    assert single["partial"] is False


@pytest.mark.parametrize("model_value", [
    {"id": "m1"},                                    # 缺 providerID
    {"id": 5, "providerID": "prov"},                 # id 类型错
    {"id": "m1", "providerID": None},                # providerID 类型错
    {"id": "m1", "providerID": "prov", "variant": 7},  # variant 类型错
    42,                                              # 非对象非 null
])
def test_v4_native_model_malformed_null_partial(model_value):
    single = _project_native_item(_clean_native(model=model_value))
    assert single["model"] is None
    assert single["partial"] is True


def test_v4_native_model_valid_variant_optional():
    # variant?: string——缺席 → 不置键（合法）；显式 null = 对象畸形
    #（专测锁定）；字符串则发
    base = _project_native_item(_clean_native(
        model={"id": "m1", "providerID": "prov"}))
    assert base["model"] == {"id": "m1", "providerID": "prov"}
    assert base["partial"] is False
    with_variant = _project_native_item(_clean_native(
        model={"id": "m1", "providerID": "prov", "variant": "v1"}))
    assert with_variant["model"] == {"id": "m1", "providerID": "prov",
                                     "variant": "v1"}
    assert with_variant["partial"] is False


@pytest.mark.parametrize("revert_value", [
    {"partID": "p"},                          # 缺 messageID
    {"messageID": 5},                         # messageID 类型错
    {"messageID": "m", "partID": 9},          # partID 类型错
    42,                                       # 非对象非 null
])
def test_v4_native_revert_malformed_null_partial(revert_value):
    single = _project_native_item(_clean_native(revert=revert_value))
    assert single["revert"] is None
    assert single["partial"] is True


def test_v4_native_revert_partid_optional():
    # partID absent 不置 null（键缺席，非 null 值）
    single = _project_native_item(_clean_native(revert={"messageID": "m"}))
    assert single["revert"] == {"messageID": "m"}
    assert single["partial"] is False


@pytest.mark.parametrize("field,bad_value", [
    ("model", {"id": "m1", "providerID": "prov", "variant": None}),
    ("revert", {"messageID": "m", "partID": None}),
])
def test_v4_native_optional_subkey_explicit_null_malformed(field, bad_value):
    """§13.2（:521-535,564-570）``variant?: string`` / ``partID?: string``：
    允许 **absent 或 string**，不允许**在场 null**——对象在场时成员须
    符合声明类型，类型违约 = 对象畸形 → 整体 null + partial:true,
    degraded:true（rev 三轮残留子缺陷 1：曾静默删键不置 partial）。"""
    single = _project_native_item(_clean_native(**{field: bad_value}))
    assert single[field] is None
    assert single["partial"] is True
    assert single["degraded"] is True


def test_v4_native_summary_all_null_subkeys_malformed():
    """§13.2 summary 对象三子键均须 number——**全 null 子值 = 畸形对象**
    （来源不可用）→ 整体 null + partial:true + degraded:true；不得与
    业务 ``summary: null``（不 partial）混同（rev 三轮残留子缺陷 2：
    曾命中「全 null → 业务 null」分支）。"""
    single = _project_native_item(_clean_native(
        summary={"additions": None, "deletions": None, "files": None}))
    assert single["summary"] is None
    assert single["partial"] is True
    assert single["degraded"] is True


def test_v4_native_summary_partial_null_subkeys_malformed():
    # 混合 null（部分子键 null / 部分数值）→ 畸形：整体 null+partial
    single = _project_native_item(_clean_native(
        summary={"additions": 1, "deletions": None, "files": 3}))
    assert single["summary"] is None
    assert single["partial"] is True


@pytest.mark.parametrize("field,bad_value", [
    ("agent", 42),
    ("parentID", 42),
    ("projectID", 42),
])
def test_v4_native_nullable_string_type_malformed(field, bad_value):
    # nullable string 字段类型错 → null + partial（§13.2b ②同级）
    single = _project_native_item(_clean_native(**{field: bad_value}))
    assert single[field] is None
    assert single["partial"] is True
    if field == "projectID":
        # projectID null → project 键缺席（§13.5 两形态）
        assert "project" not in single


def test_v4_native_time_archived_type_malformed():
    single = _project_native_item(_clean_native(
        time={"created": 1, "updated": 2, "archived": "x"}))
    assert single["time"]["archived"] is None
    assert single["partial"] is True


async def test_v4_native_legal_boundaries_wire():
    """合法边界 wire 回归：CJK title 逐字节、空 title、float 时间、
    tokens/summary 全 0——照常投影不 partial。"""
    payload = {
        "id": "h9", "directory": "/d", "title": "会話セッション — ✅",
        "parentID": None, "projectID": None, "agent": None,
        "model": None, "revert": None,
        "time": {"created": 1.5, "updated": 2.0, "archived": None},
        "summary": {"additions": 0, "deletions": 0, "files": 0},
        "tokens": {"input": 0, "output": 0, "reasoning": 0,
                   "cache": {"read": 0, "write": 0}},
    }
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(
            200, content=orjson.dumps(payload),
            headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h9",
                                params=V4, headers=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "会話セッション — ✅"
    assert body["time"]["created"] == 1.5
    assert body["summary"] == {"additions": 0, "deletions": 0, "files": 0}
    assert body["partial"] is False
    assert body["degraded"] is True  # fallback 态 item degraded 恒 true
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "会話セッション — ✅"
    assert body["time"]["created"] == 1.5
    assert body["summary"] == {"additions": 0, "deletions": 0, "files": 0}
    assert body["partial"] is False
    assert body["degraded"] is True  # fallback 态 item degraded 恒 true


async def test_v4_native_empty_title_legal_wire():
    # §13.2：title 可为空串（不可 null）——空串是合法值非不可表示
    payload = _clean_native(title="")
    resp = await _native_single(payload)
    assert resp.status_code == 200
    assert resp.json()["title"] == ""


# ---------------------------------------------------------------------------
# 降级矩阵（继承 §4.2）：fallback / fail-closed / 404 / 4xx / 5xx
# ---------------------------------------------------------------------------

async def test_v4_dbaux_disabled_fallback_degraded():
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    assert resp.status_code == 200
    # 整响应 = native 回退投影（逐字段锁定；禁跨源拼接——无 DB 字段混入）
    assert resp.json() == EXPECTED_FALLBACK_SINGLE
    assert len(seen) == 1
    assert seen[0].url.path == "/session/h1"


async def test_v4_fallback_minimal_session_source_unavailable():
    """minimal 会话：required 齐备；其余 required nullable 全部来源不可得
    → 恒发 null + partial:true + degraded:true（§13.2b ②）。"""
    minimal = {"id": "h2", "directory": "/d", "title": "t",
               "time": {"created": 1, "updated": 2}}
    app, _ = _build_app(
        _StubAux("circuit_open"),
        handler=lambda request: httpx.Response(
            200, content=orjson.dumps(minimal),
            headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h2",
                                params=V4, headers=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == _canonical_keys(body["projectID"])
    for key in ("parentID", "projectID", "agent", "model", "summary",
                "tokens_input", "tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write", "revert"):
        assert body[key] is None
    assert body["time"] == {"created": 1, "updated": 2, "archived": None}
    assert "project" not in body  # projectID null → 缺席
    assert body["partial"] is True
    assert body["degraded"] is True


@pytest.mark.parametrize("drop", ["title", "directory", "time"])
async def test_v4_fallback_required_unrepresentable_503(drop):
    payload = {k: v for k, v in UPSTREAM_SINGLE.items() if k != drop}
    if drop == "time":
        payload["time"] = {"created": 11}  # 缺 updated
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(
            200, content=orjson.dumps(payload),
            headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    _assert_aux_unavailable(resp)


async def test_v4_fallback_upstream_5xx_503():
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(500, content=b"boom"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"


async def test_v4_fallback_upstream_4xx_verbatim():
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(
            404, content=b"no such session",
            headers={"Content-Type": "text/plain"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    assert resp.status_code == 404
    assert resp.content == b"no such session"


async def test_v4_busy_aux_fail_closed_503():
    app, seen = _build_app(_BusyAux(sqlite3.OperationalError(
        "database is locked")))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    _assert_aux_unavailable(resp)
    assert seen == []


async def test_v4_raced_aux_falls_back():
    app, _ = _build_app(_RacedAux())
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.json() == EXPECTED_FALLBACK_SINGLE


async def test_v4_directory_consumed_and_forwarded_on_fallback():
    """§13.2：单查 directory 消费沿 v3——query 单值消费剥离 + 回退时以
    ``X-Opencode-Directory`` 头转发上游。"""
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params={"v": "4", "directory": "/foo"},
                                headers=IDENTITY)
    assert resp.status_code == 200
    assert resp.json() == EXPECTED_FALLBACK_SINGLE
    assert len(seen) == 1
    assert "directory" not in str(seen[0].url)
    assert seen[0].headers.get("X-Opencode-Directory") == "/foo"


# ---------------------------------------------------------------------------
# 门控（§3.3）：session.single.projection.v4 关闭态 → 4.0.0 已发布形态
# ---------------------------------------------------------------------------

@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.setattr(
        readiness, "SATISFIED",
        readiness.SATISFIED - {"session.single.projection.v4"},
    )


async def test_gate_off_single_returns_v3_skeleton_face(gate_off):
    """门控关：?v=4 单查维持 4.0.0 发布态 = v3 skeleton 投影路径。"""
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1",
                                params=V4, headers=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert body == skeleton_session(UPSTREAM_SINGLE)
    for key in ("tokens_input", "partial", "degraded", "project"):
        assert key not in body
    assert len(seen) == 1


async def test_gate_off_list_returns_4_0_0_shape(tmp_path, gate_off):
    """门控关：列表 item 无标记、envelope 稀疏 degraded（false 省略）、
    revert NULL 键缺席——4.0.0 形态逐字节回归。"""
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            resp = await client.get(
                "/slimapi/sessions",
                params={"v": "4", "archived": "all"}, headers=IDENTITY)
        assert resp.status_code == 200
        envelope = resp.json()
        assert "degraded" not in envelope
        for item in envelope["items"]:
            assert "partial" not in item
            assert "degraded" not in item
        # 4.0.0：revert 仅在 DB 列有 dict 值时在场（NULL 行缺席——与
        # canonical「恒发」形态相对照的双态回归）
        by_id = {i["id"]: i for i in envelope["items"]}
        assert "revert" in by_id["ses_revert_full"]
        assert "revert" not in by_id["ses_root_1"]
    finally:
        await aux.stop()


async def test_gate_off_list_fallback_sparse_degraded(gate_off):
    """门控关：Class A fallback envelope degraded:true 仍在（4.0.0 行为）。"""
    upstream_items = [
        {"id": "h1", "title": "one", "directory": "/any"},
    ]
    app, _ = _build_app(
        _StubAux("disabled"),
        handler=lambda request: httpx.Response(
            200, content=orjson.dumps(upstream_items),
            headers={"Content-Type": "application/json"}))
    async with _client(app) as client:
        resp = await client.get("/slimapi/sessions",
                                params={"v": "4"}, headers=IDENTITY)
    assert resp.status_code == 200
    envelope = resp.json()
    assert envelope["degraded"] is True
    item = envelope["items"][0]
    assert "partial" not in item and "degraded" not in item


# ---------------------------------------------------------------------------
# v3 回归 / selector-less
# ---------------------------------------------------------------------------

async def test_v3_regression_skeleton_shape():
    """B12-②-style v3-branch lock (selector-less): the frozen v3 skeleton
    shape. V2b removes the v3 branch (and this lock) with the teardown."""
    app, seen = _build_app(_StubAux("disabled"), selector=False)
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1", headers=IDENTITY)
    assert resp.status_code == 200
    body = resp.json()
    assert body == skeleton_session(UPSTREAM_SINGLE)
    for key in ("tokens_input", "tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write", "project",
                "partial", "degraded"):
        assert key not in body
    assert len(seen) == 1
    assert seen[0].url.path == "/session/h1"


async def test_selectorless_never_routes_v4():
    """selector-less（无 ``v``）不进 v4：显式版本缺失 → 400。"""
    app, seen = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        resp = await client.get("/slimapi/session/h1", headers=IDENTITY)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "unsupported_version"
    assert body["supported"] == [4]
    assert seen == []
