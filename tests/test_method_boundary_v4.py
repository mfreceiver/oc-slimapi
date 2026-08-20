"""v4-contract §16 — method applicability 与 deferred 边界（2026-08-19 正式修订）。

三条 deferred POST 组合在 ``?v=4`` 面得到 coded 405 ``method_not_applicable``：

* ``POST /slimapi/session/{sid}``          → ``Allow: GET, PATCH, DELETE``
  （字面冻结，v4-contract.md §16 L628）；
* ``POST /slimapi/session/{sid}/archive``  → **空 ``Allow:``**（L629）；
* ``POST /slimapi/session/{sid}/delete``   → **空 ``Allow:``**（L630）。

错误体（§16 L626 冻结形状，v3 信封惯例 + gzip 协商）::

    {"code":"method_not_applicable","method":"<METHOD>","allow":[…]}

并加 ``Cache-Control: no-store``（L635）、零上游 IO（L634）。

版本隔离（§16 L632-L633）：v3 / 无 selector / 其他版本维持修订前现状——
本仓实测基线（stub 落地前，2026-08-20）：

* v3（``?v=3``）三条组合 → 404 ``{"code":"thin_route_not_found"}``
  （Starlette 对 ``/session/{sid}`` 只有 PATCH/DELETE partial match，最终
  full-match 到 catch-all ``/{path:path}``，coded 404、无 Allow 头）；
* 无 selector / ``?v=5`` → 400 ``{"code":"unsupported_version","supported":[3,4]}``
  （selector 中间件在路由前拒绝，永远到不了 stub）。

§16 收窄护栏：其余 method/path 组合**不新增** 405 语义（未收编路径照旧
catch-all 404；PATCH/DELETE 既有受控写行为两视图零变化）。

修订二 §16.1 两位合取：405 面存活 ⇔ ``method.boundary.v4 ∈ SATISFIED``
∧ ``session.post-actions.v4 ∉ SATISFIED``（§16.3 四格组合表）。

**双态测试模型（集成批次后）**：

* **激活态 = 默认**（零 monkeypatch）：``session.post-actions.v4`` 已入
  ``SATISFIED``（全集、``ready`` 恢复 True）——selector 对三组合 v4
  **放行**，由等效 handler 接管（路由行为见 ``test_post_actions_v4.py``；
  本文件用无路由/无上游的最小 selector app 断言「穿透、绝无
  ``method_not_applicable``」）；
* **过渡态 = ``transitional_gates`` fixture**（显式回拨九项集）：恢复
  4.2.0 发布期形态——三组合 v4 答 coded 405，字节冻结断言原样保留
  （已发布行为的**永久回归锁**）。

harness 镜像 ``app.py`` 对本组路径的装配顺序：write_groups router →
``install_proxy``（catch-all 在后）→ selector 中间件最外；上游 MockTransport
记录每个到达的请求以断言零转发。
"""
from __future__ import annotations

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi import readiness
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import write_groups
from oc_slimapi.selector import SlimapiSelectorMiddleware

IDENTITY = {"Accept-Encoding": "identity"}

# (path, Allow 头字面冻结值, allow 数组冻结值) —— §16 L628-L630。
DEFERRED_COMBOS = [
    ("/slimapi/session/s1", "GET, PATCH, DELETE", ["GET", "PATCH", "DELETE"]),
    ("/slimapi/session/s1/archive", "", []),
    ("/slimapi/session/s1/delete", "", []),
]

SESSION_BODY = orjson.dumps({
    "id": "s1", "title": "one", "parentID": None,
    "directory": "/w", "projectID": "p1", "agent": "build",
    "model": {"modelID": "m"},
    "time": {"created": 1, "updated": 2},
})


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
async def boundary_app():
    """Per-test app factory mirroring app.py's assembly for these paths:

    write_groups router → install_proxy catch-all（后注册）→ selector
    middleware 最外层（生产装配 app.py:760-762 + add_middleware 顺序）。
    Tracks the mock upstream clients and closes them after the test
    (conftest ``upstream_factory`` 惯例——不留未关闭 AsyncClient）。
    """
    clients: list[httpx.AsyncClient] = []
    apps: list[tuple[FastAPI, list[httpx.Request]]] = []

    def _make(handler):
        seen: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        app = FastAPI(title="method-boundary-v4-test")
        app.state.config = _settings()
        upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(recording),
            base_url=app.state.config.upstream,
        )
        clients.append(upstream)
        app.state.upstream = upstream
        app.state.schema_degraded = False
        app.include_router(write_groups.router)
        register_error_handlers(app)
        app.add_middleware(SlimapiSelectorMiddleware)
        install_proxy(app)
        apps.append((app, seen))
        return app, seen

    yield _make

    for client in clients:
        await client.aclose()


@pytest.fixture
def transitional_gates(monkeypatch):
    """过渡态（§16.3 第二格：boundary∈∧post∉）＝ 4.2.0 发布期形态。

    集成批次已把 ``session.post-actions.v4`` 点亮进全局 ``SATISFIED``
    （激活态=默认）；本 fixture 显式回拨九项过渡集，恢复 §16.1 405 面
    的裁决环境——下方全部字节冻结 405 断言都挂在本 fixture 上，构成
    4.2.0 已发布行为的永久回归锁（合法集：不触犯⑦蕴含）。"""
    monkeypatch.setattr(
        readiness, "SATISFIED",
        readiness.REQUIRED_SET - {"session.post-actions.v4"},
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


def _canned(request: httpx.Request) -> httpx.Response:
    """Canned upstream answers（仅既有受控写路由会到达这里）。"""
    if request.method == "DELETE" and request.url.path == "/session/s1":
        return httpx.Response(204)
    if request.method == "PATCH" and request.url.path == "/session/s1":
        return httpx.Response(200, content=SESSION_BODY,
                              headers={"Content-Type": "application/json"})
    if request.method == "POST" and request.url.path == "/session":
        return httpx.Response(201, content=SESSION_BODY,
                              headers={"Content-Type": "application/json"})
    return httpx.Response(200, json={"ok": True})


# ---------------------------------------------------------------------------
# §16 v4 面：三条 deferred POST → coded 405（冻结 Allow 字面 + 逐字段错误体）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,allow_header,allow_array", DEFERRED_COMBOS)
async def test_v4_deferred_post_returns_coded_405(path, allow_header, allow_array,
                                                  boundary_app, transitional_gates):
    """过渡态（§16.1 合取成立）：?v=4 → 405 ``method_not_applicable``，
    Allow 头与错误体逐字段等于 §16 冻结值；零上游 IO。4.2.0 已发布
    行为的永久回归锁（激活态默认下经 ``transitional_gates`` 回拨）。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.post(f"{path}?v=4", content=b"{}", headers=IDENTITY)
    assert r.status_code == 405
    # Allow 头字面冻结（§16 L628-L630；archive/delete 为空值头）。
    assert r.headers.get("allow") == allow_header
    # 错误体形状逐字段（§16 L626）：code / method / allow，别无他字段。
    body = orjson.loads(r.content)
    assert set(body) == {"code", "method", "allow"}
    assert body["code"] == "method_not_applicable"
    assert body["method"] == "POST"
    assert body["allow"] == allow_array
    # 信封惯例（v3 coded error 同族）：no-store + JSON + Vary。
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("content-type") == "application/json"
    assert r.headers.get("vary") == "Accept-Encoding"
    # 零上游 IO（§16 L634）。
    assert seen == []


@pytest.mark.parametrize("path,allow_header,allow_array", DEFERRED_COMBOS)
async def test_v4_deferred_post_gzip_negotiation(path, allow_header, allow_array,
                                                 boundary_app, transitional_gates):
    """过渡态 coded 405 走 gzip 协商族（v3 信封惯例）：
    Accept-Encoding: gzip → Content-Encoding: gzip，解压后错误体
    逐字段不变。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.post(f"{path}?v=4", content=b"{}",
                              headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 405
    # json_response 仅在真压缩时设置该头；httpx 已透明解压 r.content。
    assert r.headers.get("content-encoding") == "gzip"
    body = orjson.loads(r.content)
    assert set(body) == {"code", "method", "allow"}
    assert body["code"] == "method_not_applicable"
    assert body["method"] == "POST"
    assert body["allow"] == allow_array
    assert r.headers.get("allow") == allow_header
    assert seen == []


@pytest.mark.parametrize("path", [p for p, _a, _b in DEFERRED_COMBOS])
@pytest.mark.parametrize("query", ["?v=3", "", "?v=5", "?v=4&v=3"])
async def test_non_v4_views_keep_pre_revision_behaviour(path, query, boundary_app):
    """版本隔离（§16 L632-L633）：v3 / 无 selector / v=5 / 冲突多值 ——
    维持修订前现状，绝无 ``method_not_applicable``。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.post(f"{path}{query}", content=b"{}", headers=IDENTITY)
    if query == "?v=3":
        # 现状基线：catch-all coded 404 thin_route_not_found，无 Allow 头。
        assert r.status_code == 404
        assert orjson.loads(r.content) == {"code": "thin_route_not_found"}
        assert "allow" not in r.headers
        assert "cache-control" not in r.headers
    else:
        # selector 族 400（中间件层，先于路由）：无 selector=unsupported_version，
        # v=5=unsupported_version，?v=4&v=3=invalid_version_selector。
        assert r.status_code == 400
        body = orjson.loads(r.content)
        assert body["code"] in {"unsupported_version", "invalid_version_selector"}
        if body["code"] == "unsupported_version":
            assert body["supported"] == [3, 4]
    assert seen == []


@pytest.mark.parametrize(
    "path,query",
    [
        # /session/{sid} 是 directory 消费集成员（§5.3）：合法单值被 selector
        # 消费后照常 405（消费不产生错误，仅剥离 query）。
        ("/slimapi/session/s1", "?v=4&directory=/w"),
        # archive/delete 是 tolerant（非消费集）：directory 任何形态不消费不报错。
        ("/slimapi/session/s1/archive", "?v=4&directory=/w"),
    ],
)
async def test_v4_deferred_post_with_directory_still_405(path, query,
                                                         boundary_app,
                                                         transitional_gates):
    """过渡态：携带合法 directory 输入的 v4 deferred POST 仍答 405
    （不转发、不因 directory 报错）。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.post(f"{path}{query}", content=b"{}", headers=IDENTITY)
    assert r.status_code == 405
    assert orjson.loads(r.content)["code"] == "method_not_applicable"
    assert seen == []


# ---------------------------------------------------------------------------
# §16 收窄护栏：其余 method/path 组合不新增 405 语义
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/slimapi/session/s1/archive"),   # 未收编 path（archive 仅 POST stub）
        ("GET", "/slimapi/session/s1/delete"),    # 未收编 path（delete 同上）
        ("PUT", "/slimapi/session/s1"),           # 已收编 path 的未注册方法
        ("POST", "/slimapi/session/s1/prompt"),   # 同步 prompt 从未收编（M3-3）
    ],
)
@pytest.mark.parametrize("query", ["?v=3", "?v=4"])
async def test_no_new_405_semantics_elsewhere(method, path, query, boundary_app):
    """§16 收窄：只有三条组合获得 405；其他组合照旧 catch-all 404。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.request(method, f"{path}{query}", headers=IDENTITY)
    assert r.status_code == 404
    assert orjson.loads(r.content) == {"code": "thin_route_not_found"}
    assert seen == []


async def test_post_session_create_still_forwards_v4(boundary_app):
    """POST /slimapi/session（无 sid，#1 createSession）不受 stub 影响，
    v4 照常受控写转发。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.post("/slimapi/session?v=4", json={"title": "x"},
                              headers=IDENTITY)
    assert r.status_code == 201
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/session"


# ---------------------------------------------------------------------------
# PATCH/DELETE 既有受控写行为：v3/v4 零变化（回归）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["?v=3", "?v=4"])
async def test_patch_session_regression(query, boundary_app):
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.patch(f"/slimapi/session/s1{query}", json={"title": "x"},
                               headers=IDENTITY)
    assert r.status_code == 200
    assert orjson.loads(r.content)["id"] == "s1"
    assert r.headers.get("cache-control") == "no-store"
    assert len(seen) == 1
    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/session/s1"


@pytest.mark.parametrize("query", ["?v=3", "?v=4"])
async def test_delete_session_regression(query, boundary_app):
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.delete(f"/slimapi/session/s1{query}", headers=IDENTITY)
    assert r.status_code == 204
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/session/s1"


# ---------------------------------------------------------------------------
# §8.3 优先级链（P0-3）：selector version 400 → **method 405** → directory 400
# （v4-contract.md:306 + §16 L638——405 判定不依赖 query 参数，插列在
# 「② selector version 族 400」与「③ selector directory 族 400」之间）。
# 复合请求（v4 + directory 违约 + deferred POST）必须 405 胜出；v3 / 门控关
# 维持各自现状（版本判定在先；门控关 = 4.0.0 现状）。
# ---------------------------------------------------------------------------

# 两种 directory 违约形态（消费路径 /slimapi/session/{sid} 上的 v3 梯子，
# selector.py L35-L40）：查询多值异值 → invalid_directory_selector；
# header-only → directory_header_retired。
_DIRECTORY_VIOLATIONS = [
    ("query-multi-distinct", {}),
    ("header-only", {"X-Opencode-Directory": "alpha"}),
]


@pytest.mark.parametrize("path,allow_header,allow_array", DEFERRED_COMBOS)
@pytest.mark.parametrize("violation_kind,extra_headers", _DIRECTORY_VIOLATIONS)
async def test_v4_method_405_outranks_directory_400(
    path, allow_header, allow_array, violation_kind, extra_headers,
    boundary_app, transitional_gates
):
    """过渡态：?v=4 + directory 违约 + 三条 deferred POST → 405
    ``method_not_applicable`` **先于** directory 族 400；冻结错误体/头
    逐字段不变；零上游 IO。"""
    app, seen = boundary_app(_canned)
    query = "?v=4&directory=alpha&directory=beta"
    headers = {**IDENTITY, **extra_headers}
    async with _client(app) as client:
        r = await client.post(f"{path}{query}", content=b"{}", headers=headers)
    assert r.status_code == 405, (violation_kind, r.status_code, r.text)
    body = orjson.loads(r.content)
    assert set(body) == {"code", "method", "allow"}
    assert body["code"] == "method_not_applicable"
    assert body["method"] == "POST"
    assert body["allow"] == allow_array
    assert r.headers.get("allow") == allow_header
    assert r.headers.get("cache-control") == "no-store"
    assert seen == []


# v3 + directory 违约 + deferred POST：版本隔离在先——v3 面照旧。消费路径
# /session/{sid}：query 多值异值 → 400 invalid_directory_selector、
# header-only → 400 directory_header_retired；tolerant archive/delete 无
# directory 错误，两形态均落 catch-all 404。§16 修订不为 v3 引入新语义。
_V3_DIRECTORY_EXPECT: dict[tuple[str, str], tuple[int, str]] = {
    ("/slimapi/session/s1", "query-multi-distinct"):
        (400, "invalid_directory_selector"),
    ("/slimapi/session/s1", "header-only"):
        (400, "directory_header_retired"),
    ("/slimapi/session/s1/archive", "query-multi-distinct"):
        (404, "thin_route_not_found"),
    ("/slimapi/session/s1/archive", "header-only"):
        (404, "thin_route_not_found"),
    ("/slimapi/session/s1/delete", "query-multi-distinct"):
        (404, "thin_route_not_found"),
    ("/slimapi/session/s1/delete", "header-only"):
        (404, "thin_route_not_found"),
}


@pytest.mark.parametrize("path,allow_header,allow_array", DEFERRED_COMBOS)
async def test_v3_directory_ladder_unchanged(path, allow_header, allow_array,
                                             boundary_app):
    app, seen = boundary_app(_canned)
    cases = [
        ("query-multi-distinct", f"{path}?v=3&directory=alpha&directory=beta",
         IDENTITY),
        ("header-only", f"{path}?v=3",
         {**IDENTITY, "X-Opencode-Directory": "alpha"}),
    ]
    async with _client(app) as client:
        for kind, url, headers in cases:
            r = await client.post(url, content=b"{}", headers=headers)
            status, code = _V3_DIRECTORY_EXPECT[(path, kind)]
            assert r.status_code == status, (path, kind, r.status_code, r.text)
            assert orjson.loads(r.content)["code"] == code
            assert r.headers.get("allow") is None
    assert seen == []


@pytest.mark.parametrize("method", ["GET", "PATCH"])
async def test_v4_non_deferred_methods_keep_directory_400(method, boundary_app):
    """GET/PATCH 非本节 405 适用对象（§16 收窄）：v4 + directory 违约照旧
    directory 400 先行（GET 由 session-single 读面消费，PATCH 由写面消费）。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.request(
            method, "/slimapi/session/s1?v=4&directory=alpha&directory=beta",
            headers=IDENTITY, json={} if method == "PATCH" else None)
    assert r.status_code == 400
    assert orjson.loads(r.content)["code"] == "invalid_directory_selector"
    assert seen == []


async def test_gate_closed_v4_directory_400_restores_precedence(
    monkeypatch, boundary_app
):
    """门控关（``method.boundary.v4 ∉ SATISFIED``，合法组合：post-actions
    一并移出——⑦ 蕴含禁止 post∈∧boundary∉）：§16 修订面整体失效，
    v4 复合请求回到 4.0.0 现状——directory 400 先行（消费路径）。"""
    monkeypatch.setattr(
        readiness, "SATISFIED",
        readiness.REQUIRED_SET - {"method.boundary.v4",
                                  "session.post-actions.v4"})
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.post(
            "/slimapi/session/s1?v=4&directory=alpha&directory=beta",
            content=b"{}", headers=IDENTITY)
    assert r.status_code == 400
    assert orjson.loads(r.content)["code"] == "invalid_directory_selector"
    assert seen == []


# ---------------------------------------------------------------------------
# §16.1 两位合取（修订二）：405 面存活 ⇔ method.boundary.v4 ∈ SATISFIED
# ∧ session.post-actions.v4 ∉ SATISFIED；§16.3 四格组合表。
# 激活态（post∈）= 集成批次后的**默认**：selector 层**放行**——不再产出
# coded 405，请求穿透到路由注册表（等效 handler 接管；本层断言点 = 绝无
# method_not_applicable，用无路由/无上游的最小 app）。过渡态（boundary∈∧
# post∉）= ``transitional_gates`` fixture——上面全部 405 用例的字节冻结。
# ---------------------------------------------------------------------------


def _selector_only_app() -> FastAPI:
    """最小 selector 层应用（无任何路由/无 catch-all/无上游）：穿透后落到
    框架 404（无 coded body）——与等效 handler 装配解耦，只测 selector 层。"""
    app = FastAPI(title="method-boundary-selector-only")
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


_COMBO_PATHS = [p for p, _a, _b in DEFERRED_COMBOS]


@pytest.mark.parametrize("path", _COMBO_PATHS)
async def test_activated_default_selector_passes_through(path):
    """激活态 = 默认（零 monkeypatch，集成批次后 SATISFIED 全集）：三组合
    v4 请求被 selector 放行——无 coded 405、无 Allow 头；穿透到路由层
    （本 harness 无路由 → 框架 404 plain body；等效管线行为见
    ``test_post_actions_v4.py``）。"""
    async with _client(_selector_only_app()) as client:
        r = await client.post(f"{path}?v=4", content=b"{}", headers=IDENTITY)
    assert r.status_code == 404
    assert b"method_not_applicable" not in r.content
    assert r.headers.get("allow") is None


@pytest.mark.parametrize("path", _COMBO_PATHS)
async def test_two_condition_conjunction_boundary_off_passes_through(
    path, monkeypatch
):
    """合取另一半（§16.3 第一格的合法复现：boundary∉∧post∉）：405 面
    关——与激活态同样穿透（4.0.0 现状行为）。"""
    monkeypatch.setattr(
        readiness, "SATISFIED",
        readiness.REQUIRED_SET - {"method.boundary.v4",
                                  "session.post-actions.v4"})
    async with _client(_selector_only_app()) as client:
        r = await client.post(f"{path}?v=4", content=b"{}", headers=IDENTITY)
    assert r.status_code == 404
    assert b"method_not_applicable" not in r.content
    assert r.headers.get("allow") is None


@pytest.mark.parametrize("path", _COMBO_PATHS)
async def test_transitional_gated_coded_405(path, boundary_app, transitional_gates):
    """过渡态（§16.1 合取成立，经 ``transitional_gates`` 显式回拨——
    集成批次前曾为默认态）：三组合 v4 仍答 coded 405，冻结值不变——
    两位合取的语义锁（405 面只随 post-actions 缺席而存活）。"""
    app, seen = boundary_app(_canned)
    async with _client(app) as client:
        r = await client.post(f"{path}?v=4", content=b"{}", headers=IDENTITY)
    assert r.status_code == 405
    body = orjson.loads(r.content)
    assert body["code"] == "method_not_applicable"
    assert body["method"] == "POST"
    assert seen == []


@pytest.mark.parametrize("query", ["?v=3", ""])
async def test_activation_does_not_leak_to_v3_or_selectorless(
    query, monkeypatch
):
    """激活态（post∈）不向 v3 / 无 selector 面泄漏：v3 组合照旧 404
    thin_route_not_found（catch-all 不在此最小 harness——直接断言非
    coded 405）；无 selector 照旧 selector 族 400。"""
    monkeypatch.setattr(readiness, "SATISFIED", readiness.REQUIRED_SET)
    async with _client(_selector_only_app()) as client:
        r = await client.post(f"/slimapi/session/s1{query}",
                              content=b"{}", headers=IDENTITY)
    if query == "?v=3":
        assert r.status_code == 404
        assert b"method_not_applicable" not in r.content
    else:
        assert r.status_code == 400
        body = orjson.loads(r.content)
        assert body["code"] == "unsupported_version"
        assert body["supported"] == [3, 4]
