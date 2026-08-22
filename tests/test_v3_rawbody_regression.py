"""versions 逐字节回归（rev gate MINOR-1；V2b 收缩后仅剩 versions 面）。

既有测试以语义断言为主，测试名中的 "byte-identical" 声明缺逐字节证据。
本文件补齐 /slimapi/versions 的 **raw body bytes + status + content-type +
关键 headers** 基线锚定（versions 是 selector-exempt 路由，载荷跨进程
字节稳定，可进程内直接断言）。

V2b（2026-08-21 Phase-4 守护锁拆除）删除了本文件的 v3 字节锚
（sessions 列表 happy / health v3 视图 —— 两者在 (4,4) 窗口下已是
400 unsupported_version 面）与 sessions 键序非确定性发现锚
（PYTHONHASHSEED 固定 seed 子进程机制随之退役）。versions 基线是
v4-only 面的现行锚，保留。

侧边栏版本字段（``oc_slimapi.__version__``）随发版变化：比较前在基线
与现场**两侧同态替换**为占位符（其余字节仍逐字节比较）。

再生成：``.venv/bin/python tests/test_v3_rawbody_regression.py --capture``
打印最新基线常量，有意更新时手工回填并 review diff。
"""

from __future__ import annotations

import asyncio
import sys

import httpx
from fastapi import FastAPI

from oc_slimapi import __version__
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

IDENTITY = {"Accept-Encoding": "identity"}

# --- 基线（2026-08-18 固化；--capture 再生成；4.11.0 Phase A/A1 + 能力键更新） ----
# Revision-2 ACTIVATED state (integration close-out): readiness
# {ready:true, required = satisfied = normalized universe} + the §14 expand
# block follow the static keys on the "4" face. 2026-08-21 narrowing
# (intentional wire change, v4-contract §0.3 revision): the "3" face key is
# GONE and available collapsed to [4]. 4.11.0 Phase A / A1 (P3): the
# universe grows additively 10 → 11 IDs (sessions.details.v4 appended —
# byte-order last). 4.11.0 capability keys (S0): two static booleans
# appended additively after qpImmediateFull — messagesSince (§10.3) /
# fileRaw (§19), same-batch with their implementations. Regenerate:
#   .venv/bin/python tests/test_v3_rawbody_regression.py --capture
BASELINE_VERSIONS_BODY = (
    b'{"current":4,"available":[4],"capabilities":{"4":{"globalSessions":true,"auxiliaryFilters":true,"sseReplay":true,"qpImmediateFull":true,"messagesSince":true,"fileRaw":true,"readiness":{"ready":true,"required":["events.global.replay.v4","events.token.replay.v4","messages.expand.v4","method.boundary.v4","providers.redacted.v4","representation.vary.v4","selector.v4","session.list.global.v4","session.post-actions.v4","session.single.projection.v4","sessions.details.v4"],"satisfied":["events.global.replay.v4","events.token.replay.v4","messages.expand.v4","method.boundary.v4","providers.redacted.v4","representation.vary.v4","selector.v4","session.list.global.v4","session.post-actions.v4","session.single.projection.v4","sessions.details.v4"]},"expand":{"categories":["info_summary_diffs","part_text","part_reasoning","part_state_output","part_state_error","part_state_input_full","part_state_metadata_full","part_state_attachments","part_url","part_source","part_snapshot","compaction_full"],"fragmentMaxBytes":8388608}}},"sidecarVersion":"<SIDECAR_VERSION>"}'
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
            200, content=b"[]",
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
    app.include_router(versions.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://t")


def _normalize_versioned(body: bytes) -> bytes:
    """基线与现场同态替换版本字段（发版漂移隔离；其余字节逐字节比较）。"""
    return body.replace(__version__.encode("ascii"), b"<SIDECAR_VERSION>")


async def _fetch_versions() -> httpx.Response:
    app = _build_app()
    async with _client(app) as client:
        return await client.get("/slimapi/versions", headers=IDENTITY)


async def test_versions_raw_body_bytes():
    resp = await _fetch_versions()
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers.get("Cache-Control") == "no-store"
    live = _normalize_versioned(resp.content)
    base = _normalize_versioned(BASELINE_VERSIONS_BODY)
    assert live == base, "versions body 逐字节漂移"


def _capture() -> int:  # pragma: no cover - 维护入口
    resp = asyncio.run(_fetch_versions())
    print(f"BASELINE_VERSIONS_BODY = {resp.content!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if "--capture" in sys.argv:
        raise SystemExit(_capture())
    raise SystemExit("本文件是 pytest 测试模块；基线再生成用 --capture")
