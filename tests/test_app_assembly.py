"""F-137（L3-4）：生产 app 装配面——默认 docs/openapi 面必须关闭。

``tests/test_proxy.py`` 的 ``_build_app`` 自建默认 FastAPI、不载生产配置，
无法守护本性质——故此处直接 import **生产** ``app`` 断言：/docs /redoc
/openapi.json /docs/oauth2-redirect 全部落入本地终端 404
（``thin_route_not_found``），证明是 sidecar 边界帧、上游零接触
（上游不可能返回该错误体）。

不进入 lifespan（ASGITransport 不发 startup 事件）——终端拒绝路由在
import 期已安装，404 不触上游。
"""

from __future__ import annotations

import httpx
import pytest

from oc_slimapi.app import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


async def test_default_docs_routes_disabled_on_production_app(client):
    """F-137：生产 app 的 /docs /redoc /openapi.json /docs/oauth2-redirect
    必须落入本地终端 404。"""
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = await client.get(path)
        assert r.status_code == 404
        # 证明是 sidecar 边界帧，非上游响应
        assert r.json()["code"] == "thin_route_not_found"


async def test_head_docs_also_404_on_production_app(client):
    """F-137：HEAD /docs 同样 404（不因方法差异漏出方法路由/405）。"""
    r = await client.head("/docs")
    assert r.status_code == 404
