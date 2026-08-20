"""跨批次集成收口（4.2.0）——§3.3 per-feature 门控接线回归。

六批次落地后的统一验证（v4-contract §3.3 :96 门控模型：「每个 feature
ID 的修订语义当且仅当 ∈ satisfied 时生效；未 satisfied → 该面维持
4.0.0 已发布 v4 行为」）：

- 五个修订面 feature 各一用例：monkeypatch ``readiness.SATISFIED`` 排除
  该 ID → 对应 v4 面逐字节/逐头回退 4.0.0 已发布行为；恢复（全集）→
  修订面生效（双态同测）。
- versions 端点双态：``capabilities["4"].expand`` iff
  ``messages.expand.v4 ∈ SATISFIED``（§14/§3.3 双向不变量）。

各面的「修订语义正确性」由各批次测试文件锁定（providers /
session_single / expand_href / method_boundary / representation 五件），
本文件只锁**门控开关本身**：关 → 4.0.0 行为，开 → 修订行为。
"""
from __future__ import annotations

import pytest
import httpx
import orjson
from fastapi import FastAPI

from oc_slimapi import readiness
from oc_slimapi.config import Settings
from oc_slimapi.dbaux import DbAuxiliarySource  # noqa: F401 (type ref)
from oc_slimapi.dbaux.lifecycle import DbAuxStatus
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import (
    health,
    messages,
    read_groups,
    sessions,
    versions,
    write_groups,
)
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.skeleton import REASONING_INLINE_MAX_BYTES
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}

# --- upstream fixtures ------------------------------------------------------

# providers：§12-valid 最小 doc + provider 条目内嵌透传标记键（§12 校验
# 容忍条目级 junk；projection 必丢弃——_rich_doc 同款手法）。
PROVIDERS_DOC = {
    "providers": [
        {
            "id": "p0",
            "name": "p0",
            "zzz_passthrough_marker": True,
            "models": {
                "m0": {
                    "id": "m0",
                    "providerID": "p0",
                    "api": {"type": "native",
                            "settings": {"seed": "LEAK"}},
                    "name": "name-m0",
                    "capabilities": {"attachment": False},
                    "cost": {"input": 3},
                    "limit": {"context": 8192},
                    "options": {"seed": "LEAK"},
                    "headers": {"x-leak": "1"},
                    "release_date": "2020-01-01",
                },
            },
        },
    ],
    "default": {"p0": "m0"},
}
PROVIDERS_RAW = orjson.dumps(PROVIDERS_DOC)

# session single 上游：v3 skeleton 白名单会丢弃的字段 + §13 native 回退
# 必需的最小字段。
SESSION_SINGLE_RAW = orjson.dumps({
    "id": "s1", "directory": "/d", "title": "t",
    "time": {"created": 1, "updated": 2},
    "cost": {"input": 1, "output": 2},          # v3 skeleton drops
    "tokens": {"input": 9, "output": 8},        # v3 skeleton drops
})

# sessions 列表上游（representation 用例；Class A 降级回退体）。
SESSIONS_LIST_RAW = orjson.dumps([
    {"id": "h1", "title": "up one", "directory": "/any",
     "time": {"created": 1, "updated": 2}},
])

# messages 列表：一条带 message-level + part-level expandRefs 的消息。
def _rich_message(mid="m1"):
    return {
        "info": {
            "id": mid, "role": "assistant",
            "time": {"created": 1000, "updated": 1000},
            "summary": {"diffs": [{"file": "a.ts", "additions": 1,
                                   "deletions": 0}]},
        },
        "parts": [
            {"id": "prt", "type": "reasoning", "messageID": mid,
             "text": "x" * (REASONING_INLINE_MAX_BYTES + 1)},
        ],
    }


MESSAGES_LIST_RAW = orjson.dumps([_rich_message()])

SEEN_POSTS: list[str] = []


def _upstream_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        SEEN_POSTS.append(request.url.path)
    path = request.url.path
    if path == "/config/providers":
        return httpx.Response(200, content=PROVIDERS_RAW,
                              headers={"Content-Type": "application/json"})
    if path == "/session" and request.method == "GET":
        return httpx.Response(200, content=SESSIONS_LIST_RAW,
                              headers={"Content-Type": "application/json"})
    if path == "/session/s1":
        return httpx.Response(200, content=SESSION_SINGLE_RAW,
                              headers={"Content-Type": "application/json"})
    if path == "/session/s1/message":
        return httpx.Response(200, content=MESSAGES_LIST_RAW,
                              headers={"Content-Type": "application/json"})
    return httpx.Response(404, content=b"[]",
                          headers={"Content-Type": "application/json"})


class _StubAuxDisabled:
    """db 不可用态（§13 native 回退 / sessions Class A 降级）。"""

    def status(self) -> DbAuxStatus:
        return DbAuxStatus(available=False, mode="http", reason="disabled")

    async def query(self, sql, params=()):  # pragma: no cover
        from oc_slimapi.dbaux import AuxiliaryUnavailableError
        raise AuxiliaryUnavailableError("disabled")


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        transform_absorb_budget_seconds=2.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
        merged_fanout=8, merged_max_fulls_per_page=16,
        merged_max_bytes=8 * 1024 * 1024,
        max_expand_response_bytes=8 * 1024 * 1024,
    )


def _build_app() -> FastAPI:
    app = FastAPI(title="readiness-gating-integration-test")
    settings = _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_handler),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.dbaux = _StubAuxDisabled()
    for router in (health.router, versions.router, sessions.router,
                   read_groups.router, messages.router, write_groups.router):
        app.include_router(router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    install_proxy(app)
    return app


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """singleflight 全局注册表逐测隔离（镜像 test_expand_href_v4.py）。"""
    SEEN_POSTS.clear()
    yield
    messages.fulls.shutdown()


def _gate_off(monkeypatch, feature_id: str) -> None:
    monkeypatch.setattr(
        readiness, "SATISFIED", readiness.REQUIRED_SET - {feature_id})


def _gate_on(monkeypatch) -> None:
    monkeypatch.setattr(readiness, "SATISFIED", readiness.REQUIRED_SET)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


# ---------------------------------------------------------------------------
# ① providers.redacted.v4 → GET /slimapi/config/providers
# ---------------------------------------------------------------------------

async def test_gate_providers_redacted(monkeypatch):
    app = _build_app()
    async with _client(app) as client:
        # 关态：v4 = 4.0.0 已发布行为（受控透传，与 ?v=3 逐字节相同，
        # 上游原始字节原样到达——含透传标记键）。
        _gate_off(monkeypatch, "providers.redacted.v4")
        v3 = await client.get("/slimapi/config/providers",
                              params={"v": "3"}, headers=IDENTITY)
        v4 = await client.get("/slimapi/config/providers",
                              params={"v": "4"}, headers=IDENTITY)
        assert v4.status_code == v3.status_code == 200
        assert v4.content == v3.content == PROVIDERS_RAW

        # 开态：§12 修订投影生效（canonical 形状，标记键被丢弃）。
        _gate_on(monkeypatch)
        v4r = await client.get("/slimapi/config/providers",
                               params={"v": "4"}, headers=IDENTITY)
        assert v4r.status_code == 200
        assert b"zzz_passthrough_marker" not in v4r.content
        assert v4r.content != PROVIDERS_RAW


# ---------------------------------------------------------------------------
# ② session.single.projection.v4 → GET /slimapi/session/{sid}
# ---------------------------------------------------------------------------

async def test_gate_session_single_projection(monkeypatch):
    app = _build_app()
    async with _client(app) as client:
        # 关态：v4 走 v3 skeleton 投影路径（与 ?v=3 逐字节相同）。
        _gate_off(monkeypatch, "session.single.projection.v4")
        v3 = await client.get("/slimapi/session/s1",
                              params={"v": "3"}, headers=IDENTITY)
        v4 = await client.get("/slimapi/session/s1",
                              params={"v": "4"}, headers=IDENTITY)
        assert v4.status_code == v3.status_code == 200
        assert v4.content == v3.content
        assert "degraded" not in v4.json()  # v3 面无 degraded 键

        # 开态：§13 修订面生效（dbaux disabled → native 回退 +
        # degraded:true，§13 冻结回退语义）。
        _gate_on(monkeypatch)
        v4r = await client.get("/slimapi/session/s1",
                               params={"v": "4"}, headers=IDENTITY)
        assert v4r.status_code == 200
        assert v4r.json().get("degraded") is True
        assert v4r.content != v3.content


# ---------------------------------------------------------------------------
# ③ messages.expand.v4 → GET /slimapi/messages/{sid}（expandRefs href）
# ---------------------------------------------------------------------------

async def test_gate_messages_expand_href(monkeypatch):
    app = _build_app()
    async with _client(app) as client:
        # 关态：v4 的 expandRefs href 维持 ?v=3（4.0.0 已发布行为——
        # 整响应与 ?v=3 逐字节相同）。
        _gate_off(monkeypatch, "messages.expand.v4")
        v3 = await client.get("/slimapi/messages/s1",
                              params={"v": "3"}, headers=IDENTITY)
        v4 = await client.get("/slimapi/messages/s1",
                              params={"v": "4"}, headers=IDENTITY)
        assert v4.status_code == v3.status_code == 200
        assert v4.content == v3.content
        assert b"?v=3" in v4.content
        assert b"?v=4" not in v4.content

        # 开态：§14 修订面生效——href 翻为 ?v=4。
        _gate_on(monkeypatch)
        v4r = await client.get("/slimapi/messages/s1",
                               params={"v": "4"}, headers=IDENTITY)
        assert v4r.status_code == 200
        assert b"?v=4" in v4r.content
        assert b"?v=3" not in v4r.content


# ---------------------------------------------------------------------------
# ④ method.boundary.v4 → POST /slimapi/session/{sid}（deferred 组合）
# ---------------------------------------------------------------------------

async def test_gate_method_boundary(monkeypatch):
    app = _build_app()
    async with _client(app) as client:
        # 关态：v4 复现 4.0.0 现状——catch-all 404 thin_route_not_found。
        # （修订二后 REQUIRED_SET 含第 10 项；关态集合须同时移出
        # post-actions——§3.3 蕴含 ⑦ 禁止 post∈∧boundary∉。）
        monkeypatch.setattr(
            readiness, "SATISFIED",
            readiness.REQUIRED_SET - {"method.boundary.v4",
                                      "session.post-actions.v4"})
        r = await client.post("/slimapi/session/s1",
                              params={"v": "4"}, headers=IDENTITY)
        assert r.status_code == 404
        assert r.json()["code"] == "thin_route_not_found"
        assert "Allow" not in r.headers

        # 开态（修订二过渡态）：boundary∈SATISFIED ∧ post-actions∉SATISFIED
        # ——§16.1 两位合取成立，coded 405 + 冻结 Allow 字面。全量
        # REQUIRED_SET（post∈）会触发 §16.3 激活格：selector 放行、
        # 405 面消失（由 test_method_boundary_v4.py 激活分支锁定）。
        monkeypatch.setattr(
            readiness, "SATISFIED",
            readiness.REQUIRED_SET - {"session.post-actions.v4"})
        rr = await client.post("/slimapi/session/s1",
                               params={"v": "4"}, headers=IDENTITY)
        assert rr.status_code == 405
        assert rr.json()["code"] == "method_not_applicable"
        assert rr.headers["Allow"] == "GET, PATCH, DELETE"

        # 两态都零上游转发（deferred 语义，§16）。
        assert SEEN_POSTS == []


# ---------------------------------------------------------------------------
# ⑤ representation.vary.v4 → GET /slimapi/sessions（ETag/Vary）
# ---------------------------------------------------------------------------

async def test_gate_representation_vary(monkeypatch):
    app = _build_app()
    async with _client(app) as client:
        # 关态：v4 sessions 维持 4.0.0 已发布行为（§4.4：无 ETag / 无
        # Vary / INM 不判定）。
        _gate_off(monkeypatch, "representation.vary.v4")
        r = await client.get("/slimapi/sessions",
                             params={"v": "4"}, headers=IDENTITY)
        assert r.status_code == 200
        assert "ETag" not in r.headers
        assert "Vary" not in r.headers
        r304 = await client.get(
            "/slimapi/sessions", params={"v": "4"},
            headers={**IDENTITY, "If-None-Match": '"anything"'})
        assert r304.status_code == 200  # 无 validator → 永不 304

        # 开态：§15 修订面生效——Vary 恒在 + ETag（Class A 降级 200 同样
        # 发，§15 无降级例外条款）。
        _gate_on(monkeypatch)
        rr = await client.get("/slimapi/sessions",
                              params={"v": "4"}, headers=IDENTITY)
        assert rr.status_code == 200
        assert rr.headers.get("Vary") == "Accept-Encoding"
        assert rr.headers.get("ETag")
        assert rr.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# versions 端点双态：expand 键 iff（§14/§3.3 双向不变量）
# ---------------------------------------------------------------------------

async def test_versions_expand_key_iff_gate(monkeypatch):
    app = _build_app()
    async with _client(app) as client:
        # 关态：messages.expand.v4 ∉ SATISFIED → expand 缺席，readiness
        # 在场且 ready=false（派生值随全集翻）。
        _gate_off(monkeypatch, "messages.expand.v4")
        caps = (await client.get("/slimapi/versions")).json()["capabilities"]
        assert "expand" not in caps["4"]
        assert caps["4"]["readiness"]["ready"] is False
        assert "messages.expand.v4" not in caps["4"]["readiness"]["satisfied"]

        # 开态（默认 = 4.2.0 集成收口终态）：expand 出现且与 "3" 面
        # 同源同形，ready=true。
        _gate_on(monkeypatch)
        caps2 = (await client.get("/slimapi/versions")).json()["capabilities"]
        assert "expand" in caps2["4"]
        assert caps2["4"]["expand"] == caps2["3"]["expand"]
        assert caps2["4"]["readiness"]["ready"] is True
        assert "messages.expand.v4" in caps2["4"]["readiness"]["satisfied"]
