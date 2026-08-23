"""v4-contract §16 修订二 — POST 等效动作族（feature ``session.post-actions.v4``）。

被测路由（``routes/write_groups.py`` #18-#20，仅激活态 = v4 wire view ∧
``session.post-actions.v4 ∈ readiness.SATISFIED``）：

* ``POST /slimapi/session/{sid}``        ≡ PATCH（§16.2-a，逐字节等效受控写管线）；
* ``POST /slimapi/session/{sid}/delete`` ≡ DELETE（§16.2-b，实体处理完全同
  DELETE——读取、同 cap 413、CT 透传、逐字节转发，无忽略分支）；
* ``POST /slimapi/session/{sid}/archive`` 便捷动作（§16.2-c）：
  - 缺省判据（octet 层）：实体长度 = 0 → 合成；非空（含 ``{}`` / 仅空白）→
    一律不解析、逐字节透传；Content-Type 不影响判据；
  - 合成体：``Content-Type: application/json`` + 恰
    ``{"time":{"archived":<ms>}}``（无空格紧凑形）；ms = 判空后立即读的
    sidecar wall-clock epoch-ms（``time.time()*1000`` 取整）；
  - 错误映射零偏差：上游 4xx 原样（含拒 null archived）、5xx/网络 → 503。

Harness 双轨（并行线隔离——本线不改 selector.py / readiness.py）：

* **等效面用例（stash 轨）**：``_AdmittedWireStash`` 中间件直接向 scope
  注入 selector 放行后的 wire stash（``SELECTOR_STATE_KEY`` wire="4"——
  ``wire_view_from_scope`` 的唯一权威来源，S-B04 同一手），绕开真实
  selector 的版本裁决。门控经 ``gate_on`` fixture 显式钉住激活态
  （集成批次后 ``SATISFIED`` 全集本就是默认——fixture 使断言不随未来
  翻转漂移；handler 动态读模块属性）。
* **集成断言（full-stack 轨）**：真实 selector + terminal no-passthrough
  boundary 全栈装配（与 ``test_method_boundary_v4.py`` 同构）——retired
  v3 selector → 400、过渡态
  （``transitional_gates`` 回拨九项集）v4 三组合仍由 selector 拦
  ``method_not_applicable`` 405（handler 不接管、零上游 IO）。
"""
from __future__ import annotations

import re
import time

import httpx
import orjson
import pytest
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from oc_slimapi import readiness
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import write_groups
from oc_slimapi.selector import (
    DIRECTORY_STATE_KEY,
    SELECTOR_STATE_KEY,
    SlimapiSelectorMiddleware,
)

IDENTITY = {"Accept-Encoding": "identity"}
DIRECTORY_HEADER = "X-Opencode-Directory"
POST_ACTIONS_FEATURE = "session.post-actions.v4"

# §16.2-c 冻结合成形：恰 {"time":{"archived":<十进制整数>}}，无空格。
ARCHIVE_BYTES_RE = re.compile(rb'\{"time":\{"archived":([0-9]+)\}\}')

SESSION_BODY = orjson.dumps({
    "id": "s1", "title": "one", "parentID": None,
    "directory": "/w", "projectID": "p1", "agent": "build",
    "model": {"modelID": "m"},
    "time": {"created": 1, "updated": 2},
})

# 三条组合（§16 L358-360 路由表行）。
POST_ACTION_PATHS = [
    "/slimapi/session/s1",
    "/slimapi/session/s1/delete",
    "/slimapi/session/s1/archive",
]


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


class _AdmittedWireStash:
    """Test-only：模拟 selector 放行后的 scope stash（wire view 注入轨）。

    生产 selector 唯一能让 ``wire_view_from_scope`` 报 4 的方式就是写入
    ``state[SELECTOR_STATE_KEY] = {"result": "v4", "wire": "4"}``；本类做
    同一件事（外加可选 directory 消费 stash，模拟消费集单值剥离后的
    ``DIRECTORY_STATE_KEY``）。测试 URL 不携带 ``?v=``（wire view 由
    注入而来，不经解析）——与 ``wire_view_from_scope`` docstring 所述
    「direct route invocation in tests」同一约定。
    """

    def __init__(self, app: ASGIApp, *, wire: str = "4",
                 directory: str | None = None) -> None:
        self.app = app
        self.wire = wire
        self.directory = directory

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            if isinstance(state, dict):
                state[SELECTOR_STATE_KEY] = {
                    "result": "v4" if self.wire == "4" else "v3",
                    "wire": self.wire,
                }
                if self.directory is not None:
                    state[DIRECTORY_STATE_KEY] = self.directory
        await self.app(scope, receive, send)


@pytest.fixture
async def make_app():
    """App factory（write_groups router → error handlers → stash 或真实
    selector（+ terminal no-passthrough boundary））。跟踪 MockTransport 上游 client 并在测试后
    关闭（conftest ``upstream_factory`` 惯例）。"""
    clients: list[httpx.AsyncClient] = []

    def _make(handler, *, settings: Settings | None = None,
              wire: str | None = None, directory: str | None = None,
              ) -> tuple[FastAPI, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        app = FastAPI(title="post-actions-v4-test")
        app.state.config = settings or _settings()
        upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(recording),
            base_url=app.state.config.upstream,
        )
        clients.append(upstream)
        app.state.upstream = upstream
        app.state.schema_degraded = False
        app.include_router(write_groups.router)
        register_error_handlers(app)
        if wire is not None:
            app.add_middleware(_AdmittedWireStash, wire=wire,
                               directory=directory)
        else:
            # 生产装配（app.py 顺序）：selector 中间件最外 + terminal
            # no-passthrough boundary 在后——retired-v3/过渡态集成断言轨。
            app.add_middleware(SlimapiSelectorMiddleware)
            install_proxy(app)
        return app, seen

    yield _make

    for client in clients:
        await client.aclose()


@pytest.fixture
def gate_on(monkeypatch):
    """钉住激活态：``session.post-actions.v4 ∈ SATISFIED``（全集）。

    集成批次后这已是 ``readiness.SATISFIED`` 的出厂默认；handler /
    selector 均在请求期动态读模块属性——显式 monkeypatch 使断言不随
    未来门控翻转漂移。
    """
    monkeypatch.setattr(
        readiness, "SATISFIED",
        frozenset(set(readiness.REQUIRED) | {POST_ACTIONS_FEATURE}),
    )


@pytest.fixture
def transitional_gates(monkeypatch):
    """过渡态（§16.3 第二格：boundary∈∧post∉）＝ 4.2.0 发布期形态。

    集成批次点亮后默认已是激活态；本 fixture 显式回拨九项过渡集
    （合法集：不触犯⑦蕴含），恢复 §16.1 405 面与 handler 门控关的
    裁决环境（已发布行为的永久回归锁）。
    """
    monkeypatch.setattr(
        readiness, "SATISFIED",
        frozenset(set(readiness.REQUIRED) - {POST_ACTIONS_FEATURE}),
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


def _canned(request: httpx.Request) -> httpx.Response:
    """Canned upstream answers（受控写面会到达的调用形态）。"""
    if request.method == "DELETE" and request.url.path == "/session/s1":
        return httpx.Response(204)
    if request.method == "PATCH" and request.url.path == "/session/s1":
        return httpx.Response(200, content=SESSION_BODY,
                              headers={"Content-Type": "application/json"})
    return httpx.Response(200, json={"ok": True})


# ===========================================================================
# §16.2-a — POST /slimapi/session/{sid} ≡ PATCH
# ===========================================================================


async def test_patch_equiv_forwards_body_and_content_type(make_app, gate_on):
    """激活态：POST 透传为上游 PATCH 调用——body/CT 逐字节、成功响应
    受控写语义（no-store + Vary + 无 ETag）。"""
    app, seen = make_app(_canned, wire="4")
    sentinel = b'{"title":"t2"}'
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1", content=sentinel,
            headers={**IDENTITY, "Content-Type": "application/patch+json"})
    assert r.status_code == 200
    assert r.content == SESSION_BODY
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("vary") == "Accept-Encoding"
    assert "etag" not in r.headers
    assert len(seen) == 1
    assert seen[0].method == "PATCH"       # 等效目标动词，非 POST
    assert seen[0].url.path == "/session/s1"
    assert seen[0].read() == sentinel
    assert seen[0].headers["content-type"] == "application/patch+json"


async def test_patch_equiv_same_source_as_patch(make_app, gate_on):
    """同源断言：同一 app 下 POST 等效与原生 PATCH 的响应逐字段相同、
    上游调用（方法/路径/body/CT）完全一致——同一条管线，非复制。"""
    app, seen = make_app(_canned, wire="4")
    payload = orjson.dumps({"title": "x"})
    ct = {"Content-Type": "application/json", **IDENTITY}
    async with _client(app) as client:
        post_r = await client.post("/slimapi/session/s1",
                                   content=payload, headers=ct)
        patch_r = await client.patch("/slimapi/session/s1",
                                     content=payload, headers=ct)
    assert post_r.status_code == patch_r.status_code == 200
    assert post_r.content == patch_r.content
    assert dict(post_r.headers) == dict(patch_r.headers)
    assert len(seen) == 2
    for up in seen:
        assert up.method == "PATCH"
        assert up.url.path == "/session/s1"
        assert up.read() == payload
        assert up.headers["content-type"] == "application/json"


async def test_patch_equiv_dual_shape_time_archived(make_app, gate_on):
    """双 shape 之 time.archived 形原样透传（sidecar 不区分 shape）。"""
    app, seen = make_app(_canned, wire="4")
    payload = orjson.dumps({"time": {"archived": 123456}})
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1", content=payload,
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 200
    assert seen[0].read() == payload


async def test_patch_equiv_upstream_4xx_verbatim(make_app, gate_on):
    err = b'{"error":{"message":"bad title"}}'
    app, _ = make_app(
        lambda r: httpx.Response(
            422, content=err,
            headers={"Content-Type": "application/json", "X-Custom": "no"}),
        wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1", content=b'{"title":1}',
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.content == err
    assert "x-custom" not in r.headers   # 冻结响应头集合


async def test_patch_equiv_upstream_5xx_503(make_app, gate_on):
    app, _ = make_app(
        lambda r: httpx.Response(500, content=b"boom",
                                 headers={"Content-Type": "text/plain"}),
        wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1", content=b"{}",
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 503
    assert orjson.loads(r.content)["code"] == "upstream_unavailable"


async def test_patch_equiv_network_error_503(make_app, gate_on):
    def net_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")
    app, _ = make_app(net_err, wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1", content=b"{}",
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 503
    assert orjson.loads(r.content)["code"] == "upstream_unavailable"


async def test_patch_equiv_request_cap_413_before_upstream(make_app, gate_on):
    """请求 cap ≡ PATCH：超限 413 request_too_large，先于上游调用。"""
    app, seen = make_app(_canned, settings=_settings(max_message_bytes=16),
                         wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1", content=b"x" * 32,
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 413
    assert orjson.loads(r.content)["code"] == "request_too_large"
    assert seen == []


async def test_patch_equiv_directory_stash_forwarded(make_app, gate_on):
    """directory 消费继承 PATCH：消费集 stash（单值剥离后）→ 上游
    X-Opencode-Directory 头。"""
    app, seen = make_app(_canned, wire="4", directory="/w")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1", content=b'{"title":"x"}',
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


# ===========================================================================
# §16.2-b — POST /slimapi/session/{sid}/delete ≡ DELETE
# ===========================================================================


async def test_delete_equiv_empty_entity(make_app, gate_on):
    """空实体：上游 DELETE /session/s1、无 body → 204。"""
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/delete", headers=IDENTITY)
    assert r.status_code == 204
    assert r.content == b""
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/session/s1"
    assert seen[0].read() == b""


async def test_delete_equiv_nonempty_entity_byte_forward(make_app, gate_on):
    """非空实体逐字节转发（无「忽略 body」分支）+ CT 透传——包括对
    DELETE 语义上无意义的实体（上游自行处置）。"""
    sentinel = b'\x00\x01not-json\xff'
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1/delete", content=sentinel,
            headers={**IDENTITY, "Content-Type": "application/x-custom"})
    assert r.status_code == 204
    assert seen[0].method == "DELETE"
    assert seen[0].read() == sentinel
    assert seen[0].headers["content-type"] == "application/x-custom"


async def test_delete_equiv_request_cap_413(make_app, gate_on):
    """cap 同码同序：超限 413、零上游 IO。"""
    app, seen = make_app(_canned, settings=_settings(max_message_bytes=16),
                         wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/delete",
                              content=b"x" * 32,
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 413
    assert orjson.loads(r.content)["code"] == "request_too_large"
    assert seen == []


async def test_delete_equiv_upstream_4xx_verbatim(make_app, gate_on):
    """重复 delete：上游 404（子会话/会话已删）原样透传——非幂等如实继承。"""
    err = b'{"error":"session not found"}'
    app, _ = make_app(
        lambda r: httpx.Response(404, content=err,
                                 headers={"Content-Type": "application/json"}),
        wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/delete", headers=IDENTITY)
    assert r.status_code == 404
    assert r.content == err


async def test_delete_equiv_upstream_5xx_503(make_app, gate_on):
    app, _ = make_app(
        lambda r: httpx.Response(502, content=b"bad gw"),
        wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/delete", headers=IDENTITY)
    assert r.status_code == 503
    assert orjson.loads(r.content)["code"] == "upstream_unavailable"


# ===========================================================================
# §16.2-c — POST /slimapi/session/{sid}/archive（便捷动作）
# ===========================================================================


async def test_archive_zero_length_synthesizes(make_app, gate_on):
    """零长度实体 → 合成：CT application/json + 紧凑 JSON 字节形 + ms 为
    判空后 wall-clock epoch-ms（整数、≈ now）→ PATCH 等效管线。"""
    app, seen = make_app(_canned, wire="4")
    before_ms = int(time.time() * 1000)
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/archive", headers=IDENTITY)
    after_ms = int(time.time() * 1000)
    assert r.status_code == 200
    assert len(seen) == 1
    up = seen[0]
    assert up.method == "PATCH"
    assert up.url.path == "/session/s1"
    assert up.headers["content-type"] == "application/json"
    body = up.read()
    m = ARCHIVE_BYTES_RE.fullmatch(body)
    assert m is not None, body            # 恰 {"time":{"archived":<int>}}，无空格
    ms = int(m.group(1))
    assert before_ms <= ms <= after_ms    # 判空后立即取的 sidecar 时钟


async def test_archive_synthesized_bytes_frozen_form(make_app, gate_on,
                                                     monkeypatch):
    """时钟冻结 → 合成字节形精确断言（十进制毫秒整数、无任何空格）。"""

    class _FrozenClock:
        def time(self) -> float:
            return 1234567890.1234

    monkeypatch.setattr(write_groups, "time", _FrozenClock())
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/archive", headers=IDENTITY)
    assert r.status_code == 200
    assert seen[0].read() == b'{"time":{"archived":1234567890123}}'


async def test_archive_zero_length_with_content_type_still_synthesizes(
        make_app, gate_on):
    """CT 存在不影响判据（octet 层）：零长度 + 客户端 CT → 仍合成，
    客户端 CT 随被替换的空实体丢弃、换 application/json。"""
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1/archive",
            headers={**IDENTITY, "Content-Type": "text/plain"})
    assert r.status_code == 200
    assert seen[0].headers["content-type"] == "application/json"
    assert ARCHIVE_BYTES_RE.fullmatch(seen[0].read())


async def test_archive_nonempty_empty_json_passthrough(make_app, gate_on):
    """``{}`` 非空 → 不解析、不合成：逐字节透传上游验证。"""
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1/archive", content=b"{}",
            headers={**IDENTITY, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert seen[0].method == "PATCH"
    assert seen[0].read() == b"{}"
    assert seen[0].headers["content-type"] == "application/json"


async def test_archive_whitespace_only_passthrough(make_app, gate_on):
    """仅空白字节仍非空 → 逐字节透传（不 trim、不合成）。"""
    whitespace = b"   \n\t "
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1/archive", content=whitespace,
            headers={**IDENTITY, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert seen[0].read() == whitespace
    assert seen[0].headers["content-type"] == "application/json"


async def test_archive_patch_body_null_archived_passthrough(make_app, gate_on):
    """合法 PATCH body 含 null archived（取消归档）→ 原样透传，由上游
    裁决（sidecar 不预验证）。"""
    payload = orjson.dumps({"time": {"archived": None}})
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1/archive", content=payload,
            headers={**IDENTITY, "Content-Type": "application/json"})
    assert r.status_code == 200
    assert seen[0].read() == payload


async def test_archive_upstream_4xx_verbatim(make_app, gate_on):
    """上游拒 null archived（4xx）原样透传——无新错误码。"""
    err = b'{"error":"archived must be a number"}'
    app, _ = make_app(
        lambda r: httpx.Response(400, content=err,
                                 headers={"Content-Type": "application/json"}),
        wire="4")
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1/archive",
            content=orjson.dumps({"time": {"archived": None}}),
            headers={**IDENTITY, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.content == err


async def test_archive_upstream_5xx_503(make_app, gate_on):
    app, _ = make_app(
        lambda r: httpx.Response(500, content=b"boom"), wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/archive", headers=IDENTITY)
    assert r.status_code == 503
    assert orjson.loads(r.content)["code"] == "upstream_unavailable"


async def test_archive_request_cap_413(make_app, gate_on):
    """archive 读实体同 cap：超限 413、零上游 IO（合成/透传均不发生）。"""
    app, seen = make_app(_canned, settings=_settings(max_message_bytes=16),
                         wire="4")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/archive",
                              content=b"x" * 32,
                              headers={**IDENTITY,
                                       "Content-Type": "application/json"})
    assert r.status_code == 413
    assert orjson.loads(r.content)["code"] == "request_too_large"
    assert seen == []


async def test_archive_directory_stash_forwarded(make_app, gate_on):
    """合成体也走 directory 消费继承（stash → 上游头）。"""
    app, seen = make_app(_canned, wire="4", directory="/w")
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/archive", headers=IDENTITY)
    assert r.status_code == 200
    assert seen[0].headers.get(DIRECTORY_HEADER) == "/w"


# ===========================================================================
# 非 v4 / 门控分支：retired v3 → 400；门控关 → handler 不接管
# ===========================================================================


@pytest.mark.parametrize("path", POST_ACTION_PATHS)
async def test_retired_v3_selector_is_rejected(make_app, path):
    """``?v=3`` 一律 400 ``unsupported_version``（supported:[4]），
    且零上游 IO。"""
    app, seen = make_app(_canned)  # full-stack：真实 selector + terminal boundary
    async with _client(app) as client:
        r = await client.post(f"{path}?v=3", content=b"{}", headers=IDENTITY)
    assert r.status_code == 400
    assert orjson.loads(r.content) == {"code": "unsupported_version",
                                       "supported": [4]}
    assert seen == []


@pytest.mark.parametrize("path", POST_ACTION_PATHS)
async def test_transitional_v4_selector_405_unchanged(make_app, path,
                                                      transitional_gates):
    """集成断言（过渡态，§16.3 组合表第二行，经 ``transitional_gates``
    显式回拨——集成批次前曾为默认）：九项集（无 post-actions）时 v4
    三组合仍由 selector 两位合取拦 coded 405 ``method_not_applicable``
    ——handler 不接管、零上游 IO。4.2.0 已发布行为的永久回归锁。"""
    app, seen = make_app(_canned)
    async with _client(app) as client:
        r = await client.post(f"{path}?v=4", content=b"{}", headers=IDENTITY)
    assert r.status_code == 405
    body = orjson.loads(r.content)
    assert body["code"] == "method_not_applicable"
    assert seen == []


@pytest.mark.parametrize(
    "path,upstream_method,expected_status",
    [
        ("/slimapi/session/s1", "PATCH", 200),
        ("/slimapi/session/s1/delete", "DELETE", 204),
        ("/slimapi/session/s1/archive", "PATCH", 200),
    ],
)
async def test_full_stack_gate_on_routes_equivalence(
        make_app, gate_on, path, upstream_method, expected_status):
    """端到端合成断言（真实 selector + 真实 readiness 门控注入）：门控
    激活态（§16.3 第三行）下 v4 三组合经 selector 放行（405 面消失）→
    本模块 handler 等效管线 → 上游收到对应等效调用。两线（selector
    合取 + handler 分支）在此会合。"""
    app, seen = make_app(_canned)  # full-stack：真实 selector + terminal boundary
    async with _client(app) as client:
        r = await client.post(f"{path}?v=4", content=b"{}", headers=IDENTITY)
    assert r.status_code == expected_status
    assert len(seen) == 1
    assert seen[0].method == upstream_method
    assert seen[0].url.path == "/session/s1"
    assert seen[0].read() == b"{}"          # 非空实体：逐字节（archive 不合成）


async def test_full_stack_gate_on_archive_synthesis(make_app, gate_on):
    """端到端：激活态 + 零长度实体 → 真实 selector 放行后合成体到达
    上游（PATCH + application/json + 冻结字节形）。"""
    app, seen = make_app(_canned)
    before_ms = int(time.time() * 1000)
    async with _client(app) as client:
        r = await client.post("/slimapi/session/s1/archive?v=4",
                              headers=IDENTITY)
    after_ms = int(time.time() * 1000)
    assert r.status_code == 200
    assert len(seen) == 1
    up = seen[0]
    assert up.method == "PATCH"
    assert up.headers["content-type"] == "application/json"
    m = ARCHIVE_BYTES_RE.fullmatch(up.read())
    assert m is not None
    assert before_ms <= int(m.group(1)) <= after_ms


@pytest.mark.parametrize("path", POST_ACTION_PATHS)
async def test_gate_off_penetration_fallback_404(make_app, path,
                                                 transitional_gates):
    """handler 穿透分支（防御性，§16.3 不可达行之外的保守回退）：v4
    wire stash 但门控关（请求绕过了 selector 拦截直达路由；过渡态
    fixture 显式回拨）→ handler 不接管等效管线，回 pre-revision 404。"""
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.post(path, content=b"{}", headers=IDENTITY)
    assert r.status_code == 404
    assert orjson.loads(r.content) == {"code": "thin_route_not_found"}
    assert seen == []


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
async def test_primary_patch_delete_unaffected_under_gate(make_app, gate_on,
                                                           method):
    """加性并存（§16「非替代」）：门控激活态下原生 PATCH/DELETE 主路径
    照常受控写（三 POST 组合为新增命中面，不改既有动词）。"""
    app, seen = make_app(_canned, wire="4")
    async with _client(app) as client:
        r = await client.request(
            method, "/slimapi/session/s1", content=b'{"title":"x"}'
            if method == "PATCH" else None,
            headers={**IDENTITY,
                     **({"Content-Type": "application/json"}
                        if method == "PATCH" else {})})
    expected = 204 if method == "DELETE" else 200
    assert r.status_code == expected
    assert len(seen) == 1
    assert seen[0].method == method
    assert seen[0].url.path == "/session/s1"
