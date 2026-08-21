"""v4 sessions 表示层测试（v4-contract §15：Vary 修正 + ETag/304）。

§15 修订语义经 readiness 门控（§3.3）：``representation.vary.v4 ∈ SATISFIED``
时生效；未 satisfied 时 v4 sessions 维持 4.0.0 已发布行为（§4.4：无
ETag / 无 Vary / INM 不判定）。本文件两个方向都锁：

- 门控关（monkeypatch SATISFIED 排除 representation.vary.v4——4.2.0
  集成收口后默认全 satisfied，关态须显式复现）→ 4.0.0 行为；
- 门控开（monkeypatch SATISFIED 含该 ID；4.2.0 后亦为默认态）→ §15
  冻结口径：
  * Vary: Accept-Encoding 恒在（200/304、identity/gzip）；
  * ETag = sha256(REP_VERSION + NUL + coding + NUL + canonical identity
    body)，identity 强 ``"..."`` / gzip 弱 ``W/"..."``；
  * If-None-Match 弱比较命中 → 304（头集合 = ETag + Vary +
    Cache-Control: no-store；200/304 均 no-store）；
  * validator 域与 v3 隔离（REP_VERSION 含 wire=v4 标记）；
  * OC_SLIMAPI_ETAG_ENABLED=false → 无 ETag / 无 304 但 Vary 仍发；
  * Class A 降级 200（degraded:true）同样发 ETag/Vary（§15 无降级例外
    条款——canonical = 降级 envelope body），ETag 管线不短路（条件请求
    照常执行上游回退）；
  * validator 域隔离（REP_VERSION 含 wire=v4 标记；跨域 validator 永不
    误 304——v3 wire 面已随 (4,4) 窗退役，异域 validator 经 etag 模块
    本地构造）。
"""

from __future__ import annotations

import gzip

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi import etag as etag_mod
from oc_slimapi import readiness
from oc_slimapi.config import Settings
from oc_slimapi.dbaux import DbAuxiliarySource
from oc_slimapi.dbaux.lifecycle import DbAuxStatus
from oc_slimapi.dbaux.path_resolution import ResolvedPath
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, sessions, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

from v4_fixture import build_fixture_db

ROUTE = "/slimapi/sessions"
IDENTITY = {"Accept-Encoding": "identity"}
GZIP_OK = {"Accept-Encoding": "gzip"}
HTTP_SESSIONS_BODY = orjson.dumps([
    {"id": "h1", "title": "up one", "directory": "/any",
     "time": {"created": 1, "updated": 2},
     "tokens": {"input": 9, "output": 8, "reasoning": 0,
                "cache": {"read": 1, "write": 2}}},
    {"id": "h2", "title": "up two", "directory": "/any"},
])

SATISFIED_WITH_REPRESENTATION = frozenset({
    "selector.v4",
    "session.list.global.v4",
    "events.global.replay.v4",
    "events.token.replay.v4",
    "representation.vary.v4",
})

# 集成收口（4.2.0）后默认 SATISFIED 已含全部九项——门控关态改由显式
# monkeypatch 复现（4.0.0 历史四项集，representation.vary.v4 缺席）。
SATISFIED_WITHOUT_REPRESENTATION = frozenset({
    "selector.v4",
    "session.list.global.v4",
    "events.global.replay.v4",
    "events.token.replay.v4",
})


@pytest.fixture
def representation_revision(monkeypatch):
    """§3.3 门控开：把 representation.vary.v4 翻成 satisfied。"""
    monkeypatch.setattr(
        readiness, "SATISFIED", SATISFIED_WITH_REPRESENTATION)


@pytest.fixture
def representation_gate_off(monkeypatch):
    """§3.3 门控关：显式排除 representation.vary.v4（4.0.0 已发布态）。"""
    monkeypatch.setattr(
        readiness, "SATISFIED", SATISFIED_WITHOUT_REPRESENTATION)


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
        from oc_slimapi.dbaux import AuxiliaryUnavailableError
        raise AuxiliaryUnavailableError(self._reason)


def _build_app(aux, *, settings: Settings | None = None):
    def recording(request: httpx.Request) -> httpx.Response:
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
    return app, recording


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


async def _real_aux(tmp_path):
    db = build_fixture_db(tmp_path / "m.db")
    source = DbAuxiliarySource(ResolvedPath(path=str(db), source="explicit-env"))
    status = await source.start()
    assert status.available
    return source


# ---------------------------------------------------------------------------
# 门控关（默认 SATISFIED）：4.0.0 已发布行为逐项锁定（§3.3/§4.4）
# ---------------------------------------------------------------------------


async def test_gate_off_keeps_published_40_behavior(
        tmp_path, representation_gate_off):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            r = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
            assert r.status_code == 200
            assert "ETag" not in r.headers
            assert "Vary" not in r.headers
            assert "Cache-Control" not in r.headers
            r2 = await client.get(
                ROUTE, params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": "*"})
            assert r2.status_code == 200  # 无 validator → 永不 304
    finally:
        await aux.stop()


async def test_gate_off_degraded_also_published_40_shape(representation_gate_off):
    app, _ = _build_app(_StubAux("disabled"))
    async with _client(app) as client:
        r = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
        assert r.status_code == 200
        assert r.json().get("degraded") is True
        assert "ETag" not in r.headers
        assert "Vary" not in r.headers


# ---------------------------------------------------------------------------
# 门控开：Vary 恒在 + identity 强 ETag（canonical = wire body）
# ---------------------------------------------------------------------------


async def test_vary_always_identity_strong_etag_no_store(
        tmp_path, representation_revision):
    settings = _settings()
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux, settings=settings)
    try:
        async with _client(app) as client:
            r = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
            assert r.status_code == 200
            # §15(1)：Vary 恒在（修 _v4_json_response 删 Vary 的 bug）
            assert r.headers["Vary"] == "Accept-Encoding"
            # §15(2)：200/304 均 no-store
            assert r.headers["Cache-Control"] == "no-store"
            etag = r.headers["ETag"]
            assert etag.startswith('"') and not etag.startswith("W/")
            # canonical oracle：hash 输入 = 实际下行的 identity 字节 +
            # wire=v4 REP_VERSION（§15 canonical 口径）
            rep4 = etag_mod.response_rep_version(settings, wire_view=4)
            assert b"wire=v4" in rep4
            assert etag == etag_mod.compute_etag(r.content, "identity", rep4)
            # envelope 自含 nextCursor/complete（§15：无 aux 头）
            assert "X-Next-Cursor" not in r.headers
            assert "X-Complete" not in r.headers
    finally:
        await aux.stop()


async def test_gzip_weak_etag_and_representation_equivalence(
        tmp_path, representation_revision):
    settings = _settings()
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux, settings=settings)
    try:
        async with _client(app) as client:
            rid = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
            rgz = await client.get(ROUTE, params={"v": "4"}, headers=GZIP_OK)
            assert rgz.status_code == 200
            # Vary 恒在（gzip 协商下同样）
            assert rgz.headers["Vary"] == "Accept-Encoding"
            assert rgz.headers["Cache-Control"] == "no-store"
            assert rgz.headers["Content-Encoding"] == "gzip"
            wetag = rgz.headers["ETag"]
            # §15(2)：coding 派生弱 validator
            assert wetag.startswith('W/"')
            # 读 RAW wire 字节（httpx 会自动解压 .content）验证 body
            # 真的是 identity 字节的 gzip（gunzip 还原等价）
            async with client.stream("GET", ROUTE, params={"v": "4"},
                                     headers=GZIP_OK) as resp:
                raw = b""
                async for chunk in resp.aiter_raw():
                    raw += chunk
            assert raw[:2] == b"\x1f\x8b"  # gzip magic
            identity_bytes = gzip.decompress(raw)
            assert identity_bytes == rid.content
            # 弱 ETag 的 hash 输入 = identity 字节 + coding gzip
            rep4 = etag_mod.response_rep_version(settings, wire_view=4)
            assert wetag == etag_mod.compute_etag(
                identity_bytes, "gzip", rep4)
            # 跨 coding validator 不同（保守不匹配的构造前提）
            assert wetag != rid.headers["ETag"]
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# 304：命中 / miss / * / 弱比较双向
# ---------------------------------------------------------------------------


async def test_304_hit_miss_star_weak_both_directions(
        tmp_path, representation_revision):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            r1 = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
            etag = r1.headers["ETag"]

            # 命中：同 validator → 304，头集合 = ETag + Vary + no-store
            hit = await client.get(
                ROUTE, params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": etag})
            assert hit.status_code == 304
            assert hit.content == b""
            assert hit.headers["ETag"] == etag
            assert hit.headers["Vary"] == r1.headers["Vary"]
            assert hit.headers["Cache-Control"] == "no-store"
            assert "content-length" not in hit.headers
            assert "X-Next-Cursor" not in hit.headers
            assert "X-Complete" not in hit.headers

            # miss：未知 validator → 200 全量
            miss = await client.get(
                ROUTE, params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": '"0000"'})
            assert miss.status_code == 200
            assert miss.headers["ETag"] == etag
            assert miss.content == r1.content

            # `*`：任意现行表示 → 304
            star = await client.get(
                ROUTE, params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": "*"})
            assert star.status_code == 304

            # 弱比较方向 A：服务端强 validator，客户端发弱形 W/"<hex>"
            hex_part = etag[1:-1]  # strip quotes
            weak_form = f'W/"{hex_part}"'
            wa = await client.get(
                ROUTE, params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": weak_form})
            assert wa.status_code == 304

            # 弱比较方向 B：gzip 视图服务端弱 W/"hex"，客户端发强形 "hex"
            rgz = await client.get(ROUTE, params={"v": "4"}, headers=GZIP_OK)
            wetag = rgz.headers["ETag"]
            assert wetag.startswith('W/"')
            strong_form = wetag[2:]  # strip W/ → "hex"
            wb = await client.get(
                ROUTE, params={"v": "4"},
                headers={**GZIP_OK, "If-None-Match": strong_form})
            assert wb.status_code == 304
            assert wb.headers["ETag"] == wetag
            assert wb.headers["Vary"] == "Accept-Encoding"
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# validator 域隔离：v3 ↔ v4 互不匹配
# ---------------------------------------------------------------------------


async def test_v4_validator_domain_isolated_from_foreign_rep(
        tmp_path, representation_revision):
    """跨域 validator 隔离（v4-only wire）：wire 标记（``wire=v4``）进入
    REP_VERSION hash 输入——异域 validator（``wire=v3`` rep version，经
    etag 模块本地构造；(4,4) 窗下已无 v3 wire 请求）对同一 body 必产生
    不同 validator，打到 v4 视图 → 保守 200（不误 304）。"""
    settings = _settings()
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux, settings=settings)
    try:
        async with _client(app) as client:
            r4 = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
            v4tag = r4.headers["ETag"]
            assert v4tag

            # 异域 validator 打到 v4 → 保守 200（不误 304）
            rep3 = etag_mod.response_rep_version(settings, wire_view=3)
            rep4 = etag_mod.response_rep_version(settings, wire_view=4)
            foreign = etag_mod.compute_etag(r4.content, "identity", rep3)
            assert foreign != v4tag
            cross = await client.get(
                ROUTE, params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": foreign})
            assert cross.status_code == 200

            # 同体控制：wire 标记进入 hash 输入（rep3 ≠ rep4 → validator
            # 必不同，即使 body 完全一致）
            assert rep3 != rep4
            assert b"wire=v3" in rep3 and b"wire=v4" in rep4
            same_body = b'{"items":[],"nextCursor":null,"complete":true}'
            assert (etag_mod.compute_etag(same_body, "identity", rep3)
                    != etag_mod.compute_etag(same_body, "identity", rep4))
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# OC_SLIMAPI_ETAG_ENABLED=false：无 ETag / 无 304，Vary 仍发（§15/§12.6）
# ---------------------------------------------------------------------------


async def test_etag_switch_off_no_etag_no_304_vary_kept(
        tmp_path, representation_revision):
    settings = _settings(etag_enabled=False)
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux, settings=settings)
    try:
        async with _client(app) as client:
            r = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
            assert r.status_code == 200
            assert "ETag" not in r.headers
            # Vary 与 ETag 正交：开关关闭仍发（§12.6 口径）
            assert r.headers["Vary"] == "Accept-Encoding"
            assert r.headers["Cache-Control"] == "no-store"
            r2 = await client.get(
                ROUTE, params={"v": "4"},
                headers={**IDENTITY, "If-None-Match": "*"})
            assert r2.status_code == 200  # 无 validator → 永不 304
            assert r2.content == r.content
    finally:
        await aux.stop()


# ---------------------------------------------------------------------------
# Class A 降级路径：§15 无降级例外条款 → 同样发 ETag/Vary；管线不短路
# ---------------------------------------------------------------------------


async def test_degraded_class_a_etag_vary_304_pipeline_not_shortcircuited(
        representation_revision):
    app, recording = _build_app(_StubAux("disabled"))
    calls = []

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, content=HTTP_SESSIONS_BODY,
            headers={"Content-Type": "application/json"},
        )

    # 替换 upstream transport handler 为计数版
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(counting),
        base_url=app.state.config.upstream,
    )
    async with _client(app) as client:
        r = await client.get(ROUTE, params={"v": "4"}, headers=IDENTITY)
        assert r.status_code == 200
        assert r.json().get("degraded") is True  # Class A HTTP 回退
        # §15 无降级例外条款：降级 200 同样发 ETag/Vary/no-store
        # （canonical = 降级 envelope body，自然区别于 db 域 envelope）
        assert r.headers["Vary"] == "Accept-Encoding"
        assert r.headers["Cache-Control"] == "no-store"
        etag = r.headers["ETag"]
        assert etag.startswith('"')

        upstream_before = len(calls)
        hit = await client.get(
            ROUTE, params={"v": "4"},
            headers={**IDENTITY, "If-None-Match": etag})
        assert hit.status_code == 304
        assert hit.headers["ETag"] == etag
        assert hit.headers["Vary"] == "Accept-Encoding"
        assert hit.headers["Cache-Control"] == "no-store"
        # ETag 管线不短路：条件请求照常执行上游回退（fresh 计算后判 304）
        assert len(calls) == upstream_before + 1


# ---------------------------------------------------------------------------
# 数据变更驱动 validator 轮换（canonical = body ⇒ body 变则 ETag 变）
# ---------------------------------------------------------------------------


async def test_body_change_rotates_validator_no_stale_304(
        tmp_path, representation_revision):
    aux = await _real_aux(tmp_path)
    app, _ = _build_app(aux)
    try:
        async with _client(app) as client:
            r1 = await client.get(ROUTE, params={"v": "4"},
                                  headers=IDENTITY)
            etag1 = r1.headers["ETag"]
            # 变更数据源：上游 body 换成单 session 精简形态
            # （db 路径数据未变 → 用 v3 上游路径反向验证同域机制不适用；
            # 直接构造不同 limit 的 v4 请求产生不同 envelope）
            r2 = await client.get(
                ROUTE, params={"v": "4", "limit": "1"}, headers=IDENTITY)
            assert r2.status_code == 200
            etag2 = r2.headers["ETag"]
            assert etag2 != etag1  # body 变 → validator 变
            stale = await client.get(
                ROUTE, params={"v": "4", "limit": "1"},
                headers={**IDENTITY, "If-None-Match": etag1})
            assert stale.status_code == 200  # 旧 validator 不误 304
    finally:
        await aux.stop()
