"""v3 wire 逐字节回归（rev gate MINOR-1）。

既有测试以语义断言为主，测试名中的 "byte-identical" 声明缺逐字节证据。
本文件补齐：v3 关键响应（sessions 列表 happy / health v3 视图 / versions）
的 **raw body bytes + status + content-type + 关键 headers** 基线锚定。

**已发现的跨进程非确定性（记录，非本任务修复域）**：v3 sessions item
键序来自 ``skeleton._pick`` 对 **set** 的迭代（SESSION_KEYS / time /
summary 子键集）——顺序随进程 PYTHONHASHSEED 漂移，body bytes 与 ETag
仅在**同进程内**稳定。因此 sessions 断言经 ``PYTHONHASHSEED=0`` 固定
seed 的子进程取现场（基线亦在该 seed 下固化）；health / versions 载荷
不含 set 迭代路径，跨进程字节稳定，直接进程内断言。

侧边栏版本字段（``oc_slimapi.__version__``）随发版变化：比较前在基线
与现场**两侧同态替换**为占位符（其余字节仍逐字节比较）。

再生成：``.venv/bin/python tests/test_v3_rawbody_regression.py --capture``
（sessions 走固定 seed 子进程）打印最新基线常量，有意更新时手工回填并
review diff。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
import orjson
from fastapi import FastAPI

from oc_slimapi import __version__
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, sessions, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}
V3 = {"v": "3"}

UPSTREAM_SESSIONS_BODY = orjson.dumps([
    {"id": "h1", "title": "up one", "directory": "/any",
     "time": {"created": 1, "updated": 2},
     "tokens": {"input": 9, "output": 8, "reasoning": 0,
                "cache": {"read": 1, "write": 2}}},
    {"id": "h2", "title": "up two", "directory": "/any"},
])

# --- 基线（2026-08-18 固化，PYTHONHASHSEED=0；--capture 再生成） ------------
BASELINE_SESSIONS_V3_BODY_HEX = (
    "7b226974656d73223a5b7b226964223a226831222c226469726563746f7279223a222f61"
    "6e79222c227469746c65223a227570206f6e65222c2274696d65223a7b22757064617465"
    "64223a322c2263726561746564223a317d7d2c7b226964223a226832222c226469726563"
    "746f7279223a222f616e79222c227469746c65223a2275702074776f227d5d2c22636f6d"
    "706c657465223a747275657d"
)
BASELINE_SESSIONS_V3_ETAG = (
    '"180540b33bf4f0896825a66ce2e4ca3cc0cfdb2be356f66d25d2803ebcb491f8"'
)
BASELINE_HEALTH_V3_BODY = (
    b'{"slimapi_contract":3,"sidecar":{"ok":true,"version":"<SIDECAR_VERSION>"},'
    b'"server":{"api_version":3,"accepted_client_versions":[3,4]},'
    b'"schema":{"degraded":false,"version":3,"clientMin":3,"clientMax":4},'
    b'"features":{"tokenCoalesce":true,"permissionEvents":true,"serverMerge":true,'
    b'"transformAbsorb":true,"tokenStream":true,"thresholdedSkeleton":true,'
    b'"skeletonInlineOutputMaxBytes":4096,"allowlist":{"enabled":false}}}'
)
# 4.2.0 integration close-out state: readiness {ready:true, nine-ID
# normalized required/satisfied} + the §14 expand block follow the four
# static keys on the "4" face; the "3" face is byte-frozen. Regenerate:
#   .venv/bin/python tests/test_v3_rawbody_regression.py --capture
BASELINE_VERSIONS_BODY = (
    b'{"current":4,"available":[3,4],"capabilities":{"3":{"envelope":["messa'
    b'ges","sessions"],"directoryQuery":true,"versionHeaderOptional":true,"w'
    b'riteRoutes":true,"readRoutes":["file","vcs","find","providers","sessio'
    b'nSingle","activeSessions","globalHealth"],"expand":{"categories":["inf'
    b'o_summary_diffs","part_text","part_reasoning","part_state_output","par'
    b't_state_error","part_state_input_full","part_state_metadata_full","par'
    b't_state_attachments","part_url","part_source","part_snapshot","compact'
    b'ion_full"],"fragmentMaxBytes":8388608}},"4":{"globalSessions":true,"au'
    b'xiliaryFilters":true,"sseReplay":true,"qpImmediateFull":true,"readines'
    b's":{"ready":true,"required":["events.global.replay.v4","events.token.r'
    b'eplay.v4","messages.expand.v4","method.boundary.v4","providers.redacte'
    b'd.v4","representation.vary.v4","selector.v4","session.list.global.v4",'
    b'"session.single.projection.v4"],"satisfied":["events.global.replay.v4"'
    b',"events.token.replay.v4","messages.expand.v4","method.boundary.v4","p'
    b'roviders.redacted.v4","representation.vary.v4","selector.v4","session.'
    b'list.global.v4","session.single.projection.v4"]},"expand":{"categories'
    b'":["info_summary_diffs","part_text","part_reasoning","part_state_outpu'
    b't","part_state_error","part_state_input_full","part_state_metadata_ful'
    b'l","part_state_attachments","part_url","part_source","part_snapshot","'
    b'compaction_full"],"fragmentMaxBytes":8388608}}},"sidecarVersion":"<SID'
    b'ECAR_VERSION>"}'
)
# ------------------------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
    )


def _build_app():
    app = FastAPI()
    settings = _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=UPSTREAM_SESSIONS_BODY,
            headers={"Content-Type": "application/json"},
        )),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    for router in (health.router, versions.router, sessions.router):
        app.include_router(router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    install_proxy(app)
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


def _normalize_versioned(body: bytes) -> bytes:
    """基线与现场同态替换版本字段（发版漂移隔离；其余字节逐字节比较）。"""
    return body.replace(__version__.encode("ascii"), b"<SIDECAR_VERSION>")


async def _fetch_all() -> dict[str, httpx.Response]:
    app = _build_app()
    async with _client(app) as client:
        sessions_resp = await client.get("/slimapi/sessions", params=V3,
                                         headers=IDENTITY)
        health_resp = await client.get("/slimapi/health", params=V3,
                                       headers=IDENTITY)
        versions_resp = await client.get("/slimapi/versions", headers=IDENTITY)
    return {"sessions": sessions_resp, "health": health_resp,
            "versions": versions_resp}


_FETCH_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, {tests_dir!r})
import test_v3_rawbody_regression as m

async def main():
    rs = await m._fetch_all()
    out = {{}}
    for name, r in rs.items():
        out[name] = {{"status": r.status_code,
                      "content_type": r.headers.get("content-type"),
                      "etag": r.headers.get("etag"),
                      "vary": r.headers.get("vary"),
                      "body_hex": r.content.hex()}}
    print(json.dumps(out))

asyncio.run(main())
"""


def _fetch_pinned() -> dict:
    """PYTHONHASHSEED=0 子进程取现场（sessions 键序唯一确定性来源）。"""
    tests_dir = str(Path(__file__).resolve().parent)
    env = dict(os.environ, PYTHONHASHSEED="0")
    proc = subprocess.run(
        [sys.executable, "-c", _FETCH_SCRIPT.format(tests_dir=tests_dir)],
        capture_output=True, text=True, env=env, timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    import json as _json

    return _json.loads(proc.stdout.strip().splitlines()[-1])


async def test_sessions_v3_raw_body_bytes():
    pinned = _fetch_pinned()["sessions"]
    assert pinned["status"] == 200
    assert pinned["content_type"] == "application/json"
    assert pinned["vary"] == "Accept-Encoding"
    assert bytes.fromhex(pinned["body_hex"]) == \
        bytes.fromhex(BASELINE_SESSIONS_V3_BODY_HEX), (
            "v3 sessions body 逐字节漂移（有意变更请 --capture 回填 + review）"
        )
    assert pinned["etag"] == BASELINE_SESSIONS_V3_ETAG


async def test_health_v3_raw_body_bytes():
    resp = (await _fetch_all())["health"]
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    live = _normalize_versioned(resp.content)
    base = _normalize_versioned(BASELINE_HEALTH_V3_BODY)
    assert live == base, "v3 health body 逐字节漂移"


async def test_versions_raw_body_bytes():
    resp = (await _fetch_all())["versions"]
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers.get("Cache-Control") == "no-store"
    live = _normalize_versioned(resp.content)
    base = _normalize_versioned(BASELINE_VERSIONS_BODY)
    assert live == base, "versions body 逐字节漂移"


async def test_v3_sessions_key_order_nondeterminism_recorded():
    """跨进程键序非确定性的**发现锚**（skeleton._pick set 迭代）。

    固定 seed 子进程与当前测试进程（种子随机）产出**可能**不同的 item
    键序——本测试不失败地记录该事实（两者恰相同时也通过）。修复归属
    skeleton.py 写域（非本任务）；修复后本测试应升级为无条件逐字节。
    """
    pinned_items = orjson.loads(
        bytes.fromhex(_fetch_pinned()["sessions"]["body_hex"]))["items"]
    live_resp = (await _fetch_all())["sessions"]
    live_items = orjson.loads(live_resp.content)["items"]
    # 语义等价恒成立；键序可能不同（记录性断言，不构成回归门槛）
    assert [sorted(i) for i in pinned_items] == [sorted(i) for i in live_items]
    # rev gate MINOR-2：dict == 忽略键序（恒真断言）——改为比较键**序列**
    # （序列化字节等价：orjson 序列化按插入序输出键）。
    pinned_key_seq = [list(i.keys()) for i in pinned_items]
    live_key_seq = [list(i.keys()) for i in live_items]
    same_order = pinned_key_seq == live_key_seq
    print(f"v3 sessions item key order identical across seeds: {same_order}")


def _capture() -> int:  # pragma: no cover - 维护入口
    pinned = _fetch_pinned()
    responses = asyncio.run(_fetch_all())
    print(f"BASELINE_SESSIONS_V3_BODY_HEX = {pinned['sessions']['body_hex']!r}")
    print(f"BASELINE_SESSIONS_V3_ETAG = {pinned['sessions']['etag']!r}")
    print(f"BASELINE_HEALTH_V3_BODY = {responses['health'].content!r}")
    print(f"BASELINE_VERSIONS_BODY = {responses['versions'].content!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if "--capture" in sys.argv:
        raise SystemExit(_capture())
    raise SystemExit("本文件是 pytest 测试模块；基线再生成用 --capture")
