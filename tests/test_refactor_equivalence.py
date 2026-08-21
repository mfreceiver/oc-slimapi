"""N6 equivalence gate for the Wave 3 refactors (F-301/F-302/F-304) + B12.

Plan authority: ``docs/ocmar/plans/2026-08-21-batch3-full-rollout.md`` :88 —
"拆分前先落「接口矩阵快照」——路由表（method/path/错误码族）/ SSE 帧形
golden / ETag-Vary 头快照，拆分后逐项 diff 为空才准入收尾"。

Coverage beyond ``test_offload_equivalence.py`` (which already pins the
messages + read-group response bytes end-to-end, ETag/Vary included):

* **route table** — the production app's full (method, path) set, digested;
* **sessions / agent** — the two remaining projection families' wire bytes;
* **questions / permissions** — the F-304 aggregation envelopes, success +
  partial-failure + empty paths (the lane's golden obligation);
* **token-stream frames** — a scripted attach→ingest→flush sequence through
  ``TokenStreamHub`` with a frame-recording subscriber, digested byte-wise
  (the W3-1 split must be a pure move).

Same house rules as the Wave 2 golden: RAW wire bytes via ``aiter_raw()``,
matrix executed in a ``PYTHONHASHSEED=0`` subprocess (skeleton ``_pick``
key order is per-process — see ``test_v3_rawbody_regression._fetch_pinned``).

Record (pre-refactor baseline only)::

    OC_SLIMAPI_TEST_RECORD_REFACTOR_GOLDEN=1 .venv/bin/python -m pytest \
        tests/test_refactor_equivalence.py -k golden
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import agent as agent_routes
from oc_slimapi.routes import permissions as permissions_routes
from oc_slimapi.routes import questions as questions_routes
from oc_slimapi.routes import sessions as sessions_routes
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool

GOLDEN_PATH = Path(__file__).parent / "golden" / "refactor-baseline-v1.json"
RECORD_ENV = "OC_SLIMAPI_TEST_RECORD_REFACTOR_GOLDEN"

IDENTITY = {"Accept-Encoding": "identity"}
GZIP = {"Accept-Encoding": "gzip"}

_FROZEN_HEADERS = (
    "content-type", "content-encoding", "etag", "vary",
    "cache-control", "retry-after", "x-complete",
)

SESSIONS_BODY = orjson.dumps([
    {"id": f"s{n}", "title": f"session {n}",
     "time": {"created": 1000 + n, "updated": 1000 + n}}
    for n in range(3)
])
AGENTS_BODY = orjson.dumps([
    {"name": "build", "description": "b", "mode": "primary", "prompt": "x"},
    {"name": "plan", "description": "p", "mode": "special", "prompt": "y"},
])


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
        questions_max_response_bytes=64 * 1024,
        permissions_max_response_bytes=64 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _app(settings: Settings, upstream: httpx.AsyncClient, *routers) -> FastAPI:
    app = FastAPI(title="oc-slimapi-refactor-golden")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.questions_semaphore = asyncio.Semaphore(
        settings.questions_fanout_concurrency)
    app.state.permissions_semaphore = asyncio.Semaphore(
        settings.permissions_fanout)
    for router in routers:
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


def _teardown(app: FastAPI) -> None:
    app.state.transforms.shutdown()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    )


async def _wire_request(client: httpx.AsyncClient, path: str,
                        headers: dict[str, str], expect: int) -> str:
    request = client.build_request("GET", path, headers=headers)
    response = await client.send(request, stream=True)
    try:
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
        status = response.status_code
        frozen = {
            name: response.headers[name]
            for name in _FROZEN_HEADERS if name in response.headers
        }
    finally:
        await response.aclose()
    assert status == expect, (path, status, raw[:200])
    return hashlib.sha256(
        "\n".join(
            [str(status)]
            + [f"{name}:{value}" for name, value in sorted(frozen.items())]
        ).encode() + b"\n" + raw,
    ).hexdigest()


def _sessions_upstream() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        content = SESSIONS_BODY
        if request.url.path == "/agent":
            content = AGENTS_BODY
        return httpx.Response(200, content=content,
                              headers={"Content-Type": "application/json"})

    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )


QUESTION_ITEM = {
    "id": "que_x", "sessionID": "01HSESSION",
    "questions": [{"id": "que_x", "name": "confirm", "text": "proceed?"}],
    "tool": {"name": "Bash"},
}
PERMISSION_ITEM = {
    "id": "per_01abc", "sessionID": "01HSESSION", "permission": "bash",
    "patterns": ["*"], "metadata": {"tool": "Bash"}, "always": [],
    "tool": {"messageID": "msg_1", "callID": "call_1"},
}


def _aggregate_upstream(item_path: str, item: dict, *,
                        fail_dir: str | None = None,
                        empty: bool = False,
                        dirs: tuple[str, ...] = ("/a", "/b"),
                        discovery_500: bool = False) -> httpx.AsyncClient:
    """Discovery over /experimental/session?roots=true; per-dir GET
    ``item_path`` serves one ``item`` (or ``[]`` when empty), with
    ``fail_dir`` returning 500 for the partial-failure path and
    ``discovery_500`` failing discovery itself (→ 503 upstream_unavailable).
    ``dirs`` of length > _MAX_AGGREGATE_ITEMS (default 8) triggers the
    natural truncated envelope — config-driven, no monkeypatch, so the
    case survives the W3-3 extraction."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            if discovery_500:
                return httpx.Response(500, content=b"boom")
            return httpx.Response(200, content=orjson.dumps([
                {"id": f"ses_{i:04d}", "directory": d,
                 "time": {"updated": 0, "created": 0}}
                for i, d in enumerate(dirs)
            ]), headers={"Content-Type": "application/json"})
        if request.url.path == item_path:
            directory = request.headers.get("x-opencode-directory")
            if fail_dir is not None and directory == fail_dir:
                return httpx.Response(
                    500, content=b'{"error":"boom"}',
                    headers={"Content-Type": "application/json"})
            body = [] if empty else [
                {**item, "id": f"{item['id']}_{directory.strip('/')}"}]
            return httpx.Response(200, content=orjson.dumps(body),
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404, content=b'{"error":"nf"}',
                              headers={"Content-Type": "application/json"})

    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )


async def _scenario_route_table(cases: dict[str, str]) -> None:
    """Full production (method, path) route set — the W3-2 package split and
    W3-1/W3-3 refactors must not move a single route."""
    from oc_slimapi.app import app as production_app

    rows = []
    for route in production_app.routes:
        methods = sorted(getattr(route, "methods", None) or [])
        path = getattr(route, "path", None) or getattr(route, "path_regex", "")
        if methods:
            rows.append(",".join(methods) + " " + path)
    cases["route_table"] = hashlib.sha256(
        "\n".join(sorted(rows)).encode(),
    ).hexdigest()


async def _scenario_sessions_agent(cases: dict[str, str]) -> None:
    app = _app(_settings(), _sessions_upstream(),
               sessions_routes.router, agent_routes.router)
    try:
        async with _client(app) as client:
            cases["sessions_200_gzip"] = await _wire_request(
                client, "/slimapi/sessions?v=3", GZIP, 200)
            cases["sessions_200_identity"] = await _wire_request(
                client, "/slimapi/sessions?v=3", IDENTITY, 200)
            cases["agent_200_gzip"] = await _wire_request(
                client, "/slimapi/agent?v=3", GZIP, 200)
    finally:
        _teardown(app)


async def _scenario_aggregate(cases: dict[str, str]) -> None:
    """F-304 golden obligation: both aggregation routes, success +
    partial-failure + empty paths."""
    for label, module, item_path, item in (
        ("questions", questions_routes, "/question", QUESTION_ITEM),
        ("permissions", permissions_routes, "/permission", PERMISSION_ITEM),
    ):
        for flavour, kwargs, suffix in (
            ("ok", {}, "200_gzip"),
            ("ok_identity", {}, "200_identity"),
            ("partial_failure", {"fail_dir": "/b"}, "partial_200_gzip"),
            ("empty", {"empty": True}, "empty_200_gzip"),
        ):
            upstream = _aggregate_upstream(item_path, item, **kwargs)
            app = _app(_settings(), upstream, module.router)
            try:
                async with _client(app) as client:
                    path = f"/slimapi/{label}?v=3"
                    if flavour == "ok_identity":
                        headers = IDENTITY
                    else:
                        headers = GZIP
                    cases[f"{label}_{flavour}"] = await _wire_request(
                        client, path, headers, 200)
            finally:
                _teardown(app)

    # P-6 additions (questions only — the envelope semantics mirror for
    # permissions and the mirrored success/failure paths are already
    # pinned above): discovery total failure (503), the fanout
    # concurrency window (12 dirs > questions_fanout_concurrency=8 —
    # pins strict-order merging across two concurrency batches), and
    # REAL truncation (aggregate byte budget ≈ 3 items, 12 dirs →
    # truncated envelope). R-1 note: _MAX_AGGREGATE_ITEMS is 10_000 —
    # item-count truncation is NOT reachable here; the byte budget is.
    upstream = _aggregate_upstream("/question", QUESTION_ITEM,
                                   discovery_500=True)
    app = _app(_settings(), upstream, questions_routes.router)
    try:
        async with _client(app) as client:
            cases["questions_discovery_5xx"] = await _wire_request(
                client, "/slimapi/questions?v=3", GZIP, 503)
    finally:
        _teardown(app)

    upstream = _aggregate_upstream(
        "/question", QUESTION_ITEM,
        dirs=tuple(f"/d{i:02d}" for i in range(12)))
    app = _app(_settings(), upstream, questions_routes.router)
    try:
        async with _client(app) as client:
            cases["questions_fanout_window12"] = await _wire_request(
                client, "/slimapi/questions?v=3", GZIP, 200)
    finally:
        _teardown(app)

    upstream = _aggregate_upstream(
        "/question", QUESTION_ITEM,
        dirs=tuple(f"/d{i:02d}" for i in range(12)))
    app = _app(
        _settings(
            questions_max_response_bytes=64 * 1024,
            questions_max_aggregate_bytes=1024,
        ),
        upstream, questions_routes.router,
    )
    try:
        async with _client(app) as client:
            cases["questions_truncated"] = await _wire_request(
                client, "/slimapi/questions?v=3", GZIP, 200)
            # Guard the guard: the digest is only meaningful if this
            # envelope is genuinely the truncated branch (rev2 R-1 — a
            # silently-untriggered case pins the wrong bytes).
            plain = await client.get("/slimapi/questions?v=3",
                                     headers=IDENTITY)
            assert plain.json().get("truncated") is True, (
                "truncated case did not trigger truncation — budget "
                "knob regressed"
            )
    finally:
        _teardown(app)


class _FrameSub:
    """Minimal hub→subscriber contract (mirrors test_token_hub_flush's
    _FakeSub): record every put() frame in order."""

    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.frames: list[bytes] = []
        self.closed = False

    def begin_handshake(self) -> None:
        return None

    def end_handshake(self) -> None:
        return None

    def put(self, frame: bytes) -> bool:
        self.frames.append(frame)
        return True

    def terminate(self, reason: str) -> None:
        self.closed = True


def _updated_props(text: str | None = None, *, end=None) -> dict:
    """Nested §4 envelope (rev2 fix): ``on_part_updated`` requires
    ``props["part"]`` to be a dict with a ``time`` dict — the flat shape
    used in rev1 never created a LivePart and degenerated the frame
    golden to a single handshake frame."""
    time_obj: dict = {}
    if end is not None:
        time_obj["end"] = end
    part: dict = {
        "id": "p1", "messageID": "m1", "sessionID": "s1",
        "type": "text", "time": time_obj,
    }
    if text is not None:
        part["text"] = text
    return {"sessionID": "s1", "part": part, "time": {}}


def _delta_props(delta: str) -> dict:
    return {
        "sessionID": "s1", "messageID": "m1", "partID": "p1",
        "field": "text", "delta": delta,
    }


async def _scenario_token_frames(cases: dict[str, str]) -> None:
    """W3-1 golden: scripted attach → ingest → flush sequence; every wire
    frame digested. The hub split must be a pure move (byte-identical
    frame stream for the same event sequence)."""
    from oc_slimapi.sse.tokenstream.hub import TokenStreamHub

    hub = TokenStreamHub()
    sub = _FrameSub("s1")
    hub.attach_subscriber("s1", sub)
    hub.on_part_updated(_updated_props(text=""))
    hub.on_part_delta(_delta_props(delta="Hello "))
    hub.on_part_delta(_delta_props(delta="refactor "))
    hub.flush()
    hub.on_part_delta(_delta_props(delta="golden"))
    # Terminal part end (finish_part drains residuals synchronously —
    # the ingest→flush→terminal chain in one digest).
    hub.on_part_updated(_updated_props(end=1234))
    hub.flush()
    frames = sub.frames
    assert len(frames) >= 3, (
        f"degenerate frame snapshot ({len(frames)} frames) — the ingest "
        "envelope shape regressed again"
    )
    cases["token_frames"] = hashlib.sha256(
        b"\n".join(frames),
    ).hexdigest()
    cases["token_frames_count"] = str(len(frames))


_CHILD_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, {tests_dir!r})
import test_refactor_equivalence as m

async def main():
    cases = {{}}
    await m._scenario_route_table(cases)
    await m._scenario_sessions_agent(cases)
    await m._scenario_aggregate(cases)
    await m._scenario_token_frames(cases)
    print(json.dumps(cases))

asyncio.run(main())
"""


def _fetch_cases() -> dict[str, str]:
    tests_dir = str(Path(__file__).resolve().parent)
    env = dict(os.environ, PYTHONHASHSEED="0")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT.format(tests_dir=tests_dir)],
        capture_output=True, text=True, env=env, timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_refactor_golden_matrix():
    """N6 gate: record on the pre-refactor baseline, replay hash-identical
    after every Wave 3 lane."""
    cases = _fetch_cases()

    if os.environ.get(RECORD_ENV) == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "_meta": {
                "matrix": 1,
                "recorded_on": "pre-Wave-3 baseline",
                "digest_basis": "raw wire bytes / raw token frames",
                "hashseed": "PYTHONHASHSEED=0 subprocess (skeleton _pick "
                            "key order is per-process)",
            },
            "cases": dict(sorted(cases.items())),
        }
        GOLDEN_PATH.write_text(
            orjson.dumps(document, option=orjson.OPT_INDENT_2).decode()
            + "\n", encoding="utf-8",
        )
        return

    assert GOLDEN_PATH.is_file(), (
        f"golden missing: {GOLDEN_PATH} (record with {RECORD_ENV}=1 on the "
        "pre-refactor baseline)"
    )
    stored = orjson.loads(GOLDEN_PATH.read_bytes())["cases"]
    mismatches = [
        name for name in sorted(set(stored) | set(cases))
        if stored.get(name) != cases.get(name)
    ]
    assert not mismatches, f"refactor changed pinned bytes: {mismatches}"
