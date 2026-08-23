"""N3 byte-equivalence gate for the Wave 2 response-tail offload (F-201/F-271/F-202).

Design authority: ``docs/ocmar/reviews/2026-08-21-wave2-offload-design.md`` §4
(frozen N3 spec from ``docs/ocmar/plans/2026-08-21-batch3-full-rollout.md`` :78).

The harness records, on the PRE-offload baseline, the ``sha256(status +
frozen-header subset + RAW wire bytes)`` of a representative response matrix
(list 200 × Accept-Encoding variants, merged 200, ETag hit/miss/*, empty/
single/16-item boundaries, 422/503 error bodies, lease-path flows, etag-off,
and the §10.a read-group tails incl. a >1 MiB raw body). After the offload
lands, the same matrix must replay hash-identical — the tail work moved to
a worker thread must not change one byte on the wire.

RAW wire bytes: requests are streamed and hashed via ``aiter_raw()`` —
httpx transparently decompresses ``response.content``, so the digest must
bypass it to actually pin the gzip artifacts (``content-encoding`` /
``etag`` headers + identity hash alone would not).

Hash-seed pinning: the skeleton projection emits part/session dicts by
iterating SETS (``_pick`` in skeleton.py), so wire key order — and with it
every projected-body digest and ETag — varies per process (PYTHONHASHSEED).
The matrix therefore runs in a ``PYTHONHASHSEED=0`` subprocess for BOTH
record and verify, mirroring ``test_v3_rawbody_regression._fetch_pinned``
("sessions 键序唯一确定性来源" — the established in-repo workaround). The
production-side instability itself is logged in the follow-up backlog.

Record mode (run on the pre-offload baseline only)::

    OC_SLIMAPI_TEST_RECORD_GOLDEN=1 .venv/bin/python -m pytest \
        tests/test_offload_equivalence.py -k golden

Verify mode is the default run of :func:`test_golden_matrix`. The offload
count proofs (:func:`test_messages_tail_offload_proof`,
:func:`test_readgroup_tail_to_thread_proof`) are N3(3) — they only pass once
the tail helpers exist and demonstrably leave the event loop.
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

from conftest import current_replay_log
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health, messages, read_groups, sessions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.singleflight import LeasedSingleFlight
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

GOLDEN_PATH = Path(__file__).parent / "golden" / "offload-baseline-v1.json"
RECORD_ENV = "OC_SLIMAPI_TEST_RECORD_GOLDEN"

IDENTITY = {"Accept-Encoding": "identity"}
GZIP = {"Accept-Encoding": "gzip"}

# Frozen header subset for the golden digest: every header the tail owns or
# influences. Dynamic headers (date, server) and derived ones
# (content-length) are excluded — the raw wire bytes already pin the entity.
_FROZEN_HEADERS = (
    "content-type", "content-encoding", "etag", "vary",
    "cache-control", "retry-after",
)

# --- deterministic upstream payloads ---------------------------------------

_TEXT = "tail-golden deterministic text block. " * 8  # ~300 B → gzip-worthy page

MSG_PLACEHOLDER = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "p_empty", "type": "text", "messageID": "msg_1", "text": ""},
    ],
}
MSG_PLAIN = {
    "info": {"id": "msg_2", "role": "assistant",
             "time": {"created": 1001, "updated": 1001}},
    "parts": [
        {"id": "p_text", "type": "text", "messageID": "msg_2",
         "text": _TEXT},
    ],
}
FULL_MSG_V1 = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "part_text", "type": "text", "messageID": "msg_1",
         "text": "hello full " + _TEXT},
    ],
}
LIST_BODY = orjson.dumps([MSG_PLAIN, MSG_PLACEHOLDER])  # unsorted on purpose
SINGLE_BODY = orjson.dumps([MSG_PLAIN])
EMPTY_BODY = orjson.dumps([])
PAGE16_BODY = orjson.dumps([
    {
        "info": {"id": f"msg_{n:02d}", "role": "user",
                 "time": {"created": 2000 + n, "updated": 2000 + n}},
        "parts": [
            {"id": f"p_{n:02d}", "type": "text", "messageID": f"msg_{n:02d}",
             "text": f"{n:02d} " + _TEXT},
        ],
    }
    for n in range(16)
])
LIST_LINK = (
    '<http://127.0.0.1:4096/session/s1/message?before=CURSOR123&limit=40>; '
    'rel="next"'
)
VCS_BODY = orjson.dumps(
    {"sourceControl": {"agent": False, "workflow": False}})
# > 1 MiB compressible raw body: exercises the read-group tail's large-body
# branch (post-offload: asyncio.to_thread; see _TAIL_OFFLOAD_MIN_BYTES).
VCS_LARGE_BODY = orjson.dumps(
    {"sourceControl": {"agent": False, "workflow": False},
     "blob": "x" * (1024 * 1024)})
SESSION_S1_BODY = orjson.dumps({
    "id": "s1", "title": "one", "parentID": None,
    "directory": "/w", "projectID": "p1", "agent": "build",
    "model": {"modelID": "m"},
    "time": {"created": 1, "updated": 2},
    "repoPath": "/w", "commit": "c", "branch": "main",
    "status": "ACTIVE", "version": "v1",
    "cost": {"total": 9}, "tokens": {"input": 9, "output": 9},
    "location": {"repo": "/w", "subpath": "/"}, "subpath": "/",
})


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024, smoke_session_id=None,
        coalesce_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _messages_upstream() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return httpx.Response(200, content=LIST_BODY, headers={
                "Content-Type": "application/json", "Link": LIST_LINK})
        if path == "/session/s1/message/msg_1":
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_V1),
                                  headers={"Content-Type": "application/json"})
        if path == "/session/s_empty/message":
            return httpx.Response(200, content=EMPTY_BODY,
                                  headers={"Content-Type": "application/json"})
        if path == "/session/s_single/message":
            return httpx.Response(200, content=SINGLE_BODY,
                                  headers={"Content-Type": "application/json"})
        if path == "/session/s16/message":
            return httpx.Response(200, content=PAGE16_BODY,
                                  headers={"Content-Type": "application/json"})
        if path == "/session/err/message":
            return httpx.Response(500, content=b'{"error":"boom"}',
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404, content=b'{"error":"nf"}',
                              headers={"Content-Type": "application/json"})

    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )


def _readgroup_upstream() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/vcs":
            content = VCS_BODY
        elif path == "/vcs/diff":
            content = VCS_LARGE_BODY
        elif path == "/session/s1":
            content = SESSION_S1_BODY
        else:
            return httpx.Response(404, content=b'{"error":"nf"}',
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(200, content=content,
                              headers={"Content-Type": "application/json"})

    return httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )


def _messages_app(settings: Settings, upstream: httpx.AsyncClient,
                  *, with_registry: bool = False) -> FastAPI:
    app = FastAPI(title="oc-slimapi-tail-golden")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(
        upstream, replay_log=current_replay_log())
    if with_registry and settings.coalesce_enabled:
        app.state.raw_fetch_registry = LeasedSingleFlight(
            max_bytes=settings.raw_fetch_max_bytes,
            network_concurrency=settings.raw_fetch_concurrency,
        )
    for router in (health.router, sessions.router, messages.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _readgroup_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-tail-golden-rg")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app


def _teardown(app: FastAPI) -> None:
    registry = getattr(app.state, "raw_fetch_registry", None)
    if registry is not None:
        registry.shutdown()
    app.state.transforms.shutdown()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    )


async def _wire_request(
    client: httpx.AsyncClient, path: str, headers: dict[str, str],
    expect: int,
) -> tuple[str, str | None]:
    """Stream one GET and digest its RAW wire bytes (pre-decompression).

    Returns ``(digest, etag)`` so conditional flows can chain on the
    validator without a second helper."""
    request = client.build_request("GET", path, headers=headers)
    response = await client.send(request, stream=True)
    try:
        raw = b"".join(
            [chunk async for chunk in response.aiter_raw()])
        status = response.status_code
        frozen = {
            name: response.headers[name]
            for name in _FROZEN_HEADERS if name in response.headers
        }
        etag = response.headers.get("etag")
    finally:
        await response.aclose()
    assert status == expect, (path, status, raw[:200])
    digest = hashlib.sha256(
        "\n".join(
            [str(status)]
            + [f"{name}:{value}" for name, value in sorted(frozen.items())]
        ).encode() + b"\n" + raw,
    ).hexdigest()
    return digest, etag


async def _etag_flow(client: httpx.AsyncClient, path: str,
                     accept: dict[str, str]) -> tuple[str, str, str]:
    """One conditional round: 200 (capture validator) → 304 (echo) →
    200 (mismatched validator). Returns the three digests."""
    first, etag = await _wire_request(client, path, accept, 200)
    assert etag, f"expected ETag on {path}"
    hit, _ = await _wire_request(
        client, path, {**accept, "If-None-Match": etag}, 304)
    miss, _ = await _wire_request(
        client, path, {**accept, "If-None-Match": '"deadbeef"'}, 200)
    return first, hit, miss


async def _scenario_messages(cases: dict[str, str]) -> None:
    """Direct-path matrix + boundaries + errors (coalesce off, ETag on)."""
    app = _messages_app(_settings(), _messages_upstream())
    try:
        async with _client(app) as client:
            for name, headers in (
                ("list_200_identity", IDENTITY),
                ("list_200_gzip", GZIP),
                ("list_200_wildcard", {"Accept-Encoding": "*"}),
                ("list_200_gzip_q0", {"Accept-Encoding": "gzip;q=0"}),
                ("list_200_xgzip", {"Accept-Encoding": "x-gzip"}),
            ):
                cases[name], _ = await _wire_request(
                    client, "/slimapi/messages/s1", headers, 200)

            cases["merged_200_gzip"], _ = await _wire_request(
                client, "/slimapi/messages/s1?mode=merged", GZIP, 200)
            cases["merged_200_identity"], _ = await _wire_request(
                client, "/slimapi/messages/s1?mode=merged", IDENTITY, 200)

            a, b, c = await _etag_flow(client, "/slimapi/messages/s1", IDENTITY)
            cases["etag_identity_200"], cases["etag_identity_304"], \
                cases["etag_identity_miss_200"] = a, b, c
            a, b, c = await _etag_flow(client, "/slimapi/messages/s1", GZIP)
            cases["etag_gzip_200"], cases["etag_gzip_304"], \
                cases["etag_gzip_miss_200"] = a, b, c
            cases["etag_star_304_gzip"], _ = await _wire_request(
                client, "/slimapi/messages/s1",
                {**GZIP, "If-None-Match": "*"}, 304)

            for name, sid in (("list_empty_200_gzip", "s_empty"),
                              ("list_single_200_gzip", "s_single"),
                              ("list_16_200_gzip", "s16")):
                cases[name], _ = await _wire_request(
                    client, f"/slimapi/messages/{sid}", GZIP, 200)

            cases["list_422"], _ = await _wire_request(
                client, "/slimapi/messages/s1?limit=-1", GZIP, 422)
            cases["list_503"], _ = await _wire_request(
                client, "/slimapi/messages/err", GZIP, 503)
    finally:
        _teardown(app)


async def _scenario_lease(cases: dict[str, str]) -> None:
    """Lease-path flows (coalesce on + registry). MINOR-10: each request is
    asserted to actually take the lease path (spy on ``_messages_via_lease``)
    — a silently-bypassed registry would degenerate these into re-samples of
    the direct path and void the lease-tail gate."""
    app = _messages_app(
        _settings(coalesce_enabled=True), _messages_upstream(),
        with_registry=True,
    )
    lease_calls: list[int] = []
    # W3-2 (F-302): the lease helper lives in the _list submodule — the spy
    # must wrap THAT namespace binding to observe the route's lookup.
    original = messages._list._messages_via_lease

    async def _spy(*args, **kwargs):
        lease_calls.append(1)
        return await original(*args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(messages._list, "_messages_via_lease", _spy)
    try:
        async with _client(app) as client:
            cases["lease_200_gzip"], _ = await _wire_request(
                client, "/slimapi/messages/s1", GZIP, 200)

            _, etag = await _wire_request(
                client, "/slimapi/messages/s1", GZIP, 200)
            cases["lease_304"], _ = await _wire_request(
                client, "/slimapi/messages/s1",
                {**GZIP, "If-None-Match": etag}, 304)

            cases["lease_star_304"], _ = await _wire_request(
                client, "/slimapi/messages/s1",
                {**GZIP, "If-None-Match": "*"}, 304)

            assert len(lease_calls) >= 3, (
                "lease-path golden cases silently bypassed the registry "
                "(MINOR-10 guard)"
            )
    finally:
        mp.undo()
        _teardown(app)


async def _scenario_etag_off(cases: dict[str, str]) -> None:
    app = _messages_app(_settings(etag_enabled=False), _messages_upstream())
    try:
        async with _client(app) as client:
            digest, _ = await _wire_request(
                client, "/slimapi/messages/s1", GZIP, 200)
            cases["etag_off_200_gzip"] = digest
    finally:
        _teardown(app)


async def _scenario_readgroup(cases: dict[str, str]) -> None:
    """§10.a read-group tails (F-202): raw 200 × codings + 304, projected
    session-single, and the >1 MiB raw body that crosses the offload
    threshold post-change."""
    app = _readgroup_app(
        _settings(max_response_bytes=8 * 1024 * 1024),
        _readgroup_upstream(),
    )
    try:
        async with _client(app) as client:
            cases["vcs_200_identity"], _ = await _wire_request(
                client, "/slimapi/vcs?v=4", IDENTITY, 200)
            cases["vcs_200_gzip"], _ = await _wire_request(
                client, "/slimapi/vcs?v=4", GZIP, 200)

            a, b, _ = await _etag_flow(client, "/slimapi/vcs?v=4", GZIP)
            cases["vcs_etag_200"], cases["vcs_etag_304"] = a, b

            cases["session_single_200_gzip"], _ = await _wire_request(
                client, "/slimapi/session/s1?v=4", GZIP, 200)

            # The compressed wire body must be far below the ~1 MiB identity
            # (highly compressible "x" blob) — sanity-pins the coding before
            # the digest (the digest itself covers the exact gzip bytes).
            request = client.build_request(
                "GET", "/slimapi/vcs/diff?v=4", headers=GZIP)
            response = await client.send(request, stream=True)
            try:
                raw = b"".join(
                    [chunk async for chunk in response.aiter_raw()])
                assert response.status_code == 200
                assert response.headers.get("content-encoding") == "gzip"
                assert len(raw) < len(VCS_LARGE_BODY) / 100
            finally:
                await response.aclose()
            digest, _ = await _wire_request(
                client, "/slimapi/vcs/diff?v=4", GZIP, 200)
            cases["vcs_large_200_gzip"] = digest
    finally:
        _teardown(app)


_CHILD_SCRIPT = """
import asyncio, json, sys
sys.path.insert(0, {tests_dir!r})
from conftest import _CURRENT_REPLAY_LOG
from oc_slimapi.sse.replay_log import ReplayLog
import test_offload_equivalence as m

async def main():
    replay_log = ReplayLog()
    token = _CURRENT_REPLAY_LOG.set(replay_log)
    try:
        cases = {{}}
        await m._scenario_messages(cases)
        await m._scenario_lease(cases)
        await m._scenario_etag_off(cases)
        await m._scenario_readgroup(cases)
        print(json.dumps(cases))
    finally:
        _CURRENT_REPLAY_LOG.reset(token)
        replay_log.close()

asyncio.run(main())
"""


def _fetch_cases() -> dict[str, str]:
    """Run the matrix in a ``PYTHONHASHSEED=0`` subprocess (see module
    docstring: projected key order is hash-seed dependent)."""
    tests_dir = str(Path(__file__).resolve().parent)
    env = dict(os.environ, PYTHONHASHSEED="0")
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT.format(tests_dir=tests_dir)],
        capture_output=True, text=True, env=env, timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_golden_matrix():
    """N3(1)/(2): record the matrix digest on the pre-offload baseline,
    replay hash-identical afterwards."""
    cases = _fetch_cases()

    if os.environ.get(RECORD_ENV) == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "_meta": {
                "matrix": 1,
                "recorded_on": "pre-offload baseline (Wave 2 design §4)",
                "digest_basis": "raw wire bytes via aiter_raw (gzip pinned)",
                "hashseed": "matrix runs under PYTHONHASHSEED=0 (skeleton "
                            "_pick key order is per-process; same workaround "
                            "as test_v3_rawbody_regression._fetch_pinned)",
                "gzip_mtime0": True,
                "same_env_note": (
                    "deflate bytes vary with the zlib build — record and "
                    "verify must run on the same machine/venv (single-host "
                    "check.sh satisfies this)"
                ),
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
        "pre-offload baseline)"
    )
    stored = orjson.loads(GOLDEN_PATH.read_bytes())["cases"]
    mismatches = []
    for name in sorted(set(stored) | set(cases)):
        if stored.get(name) != cases.get(name):
            mismatches.append(name)
    assert not mismatches, f"tail offload changed wire bytes: {mismatches}"


# --- N3(3): the tail demonstrably leaves the event loop ---------------------
#
# These proofs reference the post-offload helpers by design; they are not
# part of the record-mode selection (-k golden).


async def test_messages_tail_offload_proof():
    """Every messages-list request (direct + lease, 200 and 304) submits the
    shared tail worker through ``pool.offload``."""
    from oc_slimapi.routes.messages import _judge_pack_tail

    submissions: list = []
    original = TransformPool.offload

    async def _spy_offload(self, func, /, *args, **kwargs):
        submissions.append(func)
        return await original(self, func, *args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(TransformPool, "offload", _spy_offload)
    try:
        # Direct path: 200 then conditional 304.
        app = _messages_app(_settings(), _messages_upstream())
        try:
            async with _client(app) as client:
                first = await client.get("/slimapi/messages/s1",
                                         headers=GZIP)
                assert first.status_code == 200
                assert _judge_pack_tail in submissions, (
                    "direct-path 200 tail never offloaded")
                submissions.clear()
                hit = await client.get("/slimapi/messages/s1", headers={
                    **GZIP, "If-None-Match": first.headers["etag"]})
                assert hit.status_code == 304
                assert _judge_pack_tail in submissions, (
                    "direct-path 304 tail never offloaded")
        finally:
            _teardown(app)

        # Lease path: 200 and 304 both go through the same offloaded tail.
        app = _messages_app(
            _settings(coalesce_enabled=True), _messages_upstream(),
            with_registry=True,
        )
        try:
            async with _client(app) as client:
                submissions.clear()
                first = await client.get("/slimapi/messages/s1",
                                         headers=GZIP)
                assert first.status_code == 200
                assert _judge_pack_tail in submissions, (
                    "lease-path 200 tail never offloaded")
                submissions.clear()
                hit = await client.get("/slimapi/messages/s1", headers={
                    **GZIP, "If-None-Match": first.headers["etag"]})
                assert hit.status_code == 304
                assert _judge_pack_tail in submissions, (
                    "lease-path 304 tail never offloaded")
        finally:
            _teardown(app)
    finally:
        mp.undo()


async def test_readgroup_tail_to_thread_proof():
    """Read-group tails: bodies at/above the threshold leave the loop via
    ``asyncio.to_thread``; small bodies stay inline (0 calls)."""
    from oc_slimapi.routes._read_passthrough import _tail_encode

    calls: list = []
    original = asyncio.to_thread

    async def _spy_to_thread(func, /, *args, **kwargs):
        calls.append(func)
        return await original(func, *args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(asyncio, "to_thread", _spy_to_thread)
    try:
        app = _readgroup_app(
            _settings(max_response_bytes=8 * 1024 * 1024),
            _readgroup_upstream(),
        )
        try:
            async with _client(app) as client:
                small = await client.get("/slimapi/vcs?v=4", headers=GZIP)
                assert small.status_code == 200
                assert not calls, "small body must stay inline"

                large = await client.get("/slimapi/vcs/diff?v=4",
                                         headers=GZIP)
                assert large.status_code == 200
                assert _tail_encode in calls, (
                    "large-body tail never left the event loop")
        finally:
            _teardown(app)
    finally:
        mp.undo()
