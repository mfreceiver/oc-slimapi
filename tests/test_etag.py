"""Traffic plan Batch 2 / B1 — ETag / 304 conditional requests (additive wire).

Algorithm authority: plan §4 (v1.3 unified spec text) —
identity STRONG ``"<sha256hex(REP_VERSION \0 b"identity" \0 identity_body)>"``,
gzip WEAK ``W/"<sha256hex(REP_VERSION \0 b"gzip" \0 identity_body)>"`` (the
canonical hash input is ALWAYS the identity bytes + coding id). RFC 9110
``If-None-Match`` weak-comparison list matching + ``*``. The pipeline always
runs (ETag never short-circuits the fetch/projection — Batch 1 keeps
amortising the upstream); a hit saves the DOWNSTREAM transport body only.

Coverage: B1-C1..C7 (see plan §4 Acceptance Criteria).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi import etag as etag_mod
from oc_slimapi.envelope import messages_envelope_bytes
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.singleflight import LeasedSingleFlight
from oc_slimapi.catalog_cache import CatalogCache
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import (
    agent as agent_routes,
    command as command_routes,
    health,
    messages,
    sessions,
)
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool

HDR = {
    "X-Slimapi-Version": "2",
    # Pin the identity coding: httpx would otherwise advertise gzip by
    # default, and these identity-path tests assert STRONG validators.
    # Gzip tests override this key explicitly (CLIENT_CHANGES: fix coding).
    "Accept-Encoding": "identity",
}


# ---------------------------------------------------------------------------
# Fixtures / payloads
# ---------------------------------------------------------------------------

AGENTS_BODY = orjson.dumps([
    {"name": "build", "description": "b", "mode": "primary", "prompt": "x"},
    {"name": "plan", "description": "p", "mode": "special", "prompt": "y"},
])
COMMANDS_BODY = orjson.dumps([
    {"name": "cmd", "description": "c", "agent": None, "hints": {}},
])
SESSIONS_BODY = orjson.dumps([
    {"id": f"s{n}", "title": f"session {n}",
     "time": {"created": 1000 + n, "updated": 1000 + n}}
    for n in range(3)
])

# A skeleton-collapsed message (single empty text part → thin_placeholder
# marker) plus its merged full body — merged C2 uses two full variants.
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
         "text": "plain"},
    ],
}
FULL_MSG_V1 = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "part_text", "type": "text", "messageID": "msg_1",
         "text": "hello full"},
    ],
}
FULL_MSG_V2 = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "part_text", "type": "text", "messageID": "msg_1",
         "text": "hello full CHANGED"},
    ],
}

MSG_LIST_BODY = orjson.dumps([MSG_PLACEHOLDER, MSG_PLAIN])
LIST_LINK = (
    '<http://127.0.0.1:4096/session/s1/message?before=CURSOR123&limit=40>; '
    'rel="next"'
)


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        # Batch 1 knobs off by default here — ETag is tested in isolation;
        # the A-batch interplay has its own dedicated test below.
        coalesce_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(
    settings: Settings,
    upstream: httpx.AsyncClient,
    *,
    with_registry: bool = False,
    with_catalog_cache: bool = False,
) -> FastAPI:
    app = FastAPI(title="oc-slimapi-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(upstream)
    if with_registry and settings.coalesce_enabled:
        app.state.raw_fetch_registry = LeasedSingleFlight(
            max_bytes=settings.raw_fetch_max_bytes,
            network_concurrency=settings.raw_fetch_concurrency,
        )
    if with_catalog_cache:
        app.state.catalog_cache = CatalogCache(
            ttl_seconds=settings.catalog_cache_ttl_seconds,
            max_entries=settings.catalog_cache_max_entries,
            max_bytes=settings.catalog_cache_max_bytes,
            max_entry_bytes=settings.catalog_cache_max_entry_bytes,
        )
    for router in (health.router, agent_routes.router,
                   command_routes.router, sessions.router, messages.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


def _teardown(app: FastAPI) -> None:
    registry = getattr(app.state, "raw_fetch_registry", None)
    if registry is not None:
        registry.shutdown()
    app.state.transforms.shutdown()


@pytest.fixture
async def upstream_factory():
    clients: list[httpx.AsyncClient] = []

    def _make(handler, *, base_url: str = "http://127.0.0.1:4096"):
        client = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    yield _make
    for client in clients:
        await client.aclose()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    )


def _catalog_handler(state: dict | None = None):
    """Multi-path upstream mock: /agent /command /session /session/status
    /session/s1/message (+Link) /session/s1/message/msg_1.

    ``state`` allows tests to mutate bodies between calls (C2 / merged)."""
    state = state if state is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/agent":
            body = state.get("agent", AGENTS_BODY)
            per_dir = state.get("agent_per_directory")
            if per_dir is not None:
                body = per_dir.get(
                    request.headers.get("x-opencode-directory"), body)
            return httpx.Response(200, content=body)
        if path == "/command":
            return httpx.Response(200, content=COMMANDS_BODY)
        if path == "/session":
            return httpx.Response(200, content=SESSIONS_BODY)
        if path == "/session/s1/message/msg_1":
            return httpx.Response(
                200, content=state.get("full", orjson.dumps(FULL_MSG_V1)))
        if path == "/session/s1/message":
            return httpx.Response(
                200, content=state.get("list", orjson.dumps(
                    [MSG_PLACEHOLDER, MSG_PLAIN])),
                headers={"Link": LIST_LINK},
            )
        raise AssertionError(f"unexpected upstream path {path}")

    return handler


# ---------------------------------------------------------------------------
# Unit — validator algorithm (plan §4 v1.3 unified spec text)
# ---------------------------------------------------------------------------

class TestValidatorAlgorithm:
    def test_identity_strong_etag_exact_formula(self):
        rep = b"rep-version-bytes"
        body = b'{"a":1}'
        tag = etag_mod.compute_etag(body, "identity", rep)
        expected = '"{}"'.format(hashlib.sha256(
            rep + b"\0" + b"identity" + b"\0" + body).hexdigest())
        assert tag == expected
        assert not tag.startswith("W/")
        assert len(tag) == 66  # quotes + 64 full hex (never truncated)

    def test_gzip_weak_etag_canonical_input_is_identity_bytes(self):
        rep = b"rep-version-bytes"
        identity = b'{"a":1}'
        tag = etag_mod.compute_etag(identity, "gzip", rep)
        expected = 'W/"{}"'.format(hashlib.sha256(
            rep + b"\0" + b"gzip" + b"\0" + identity).hexdigest())
        assert tag == expected
        # per-coding validators: the two codings NEVER share an opaque tag
        assert tag != etag_mod.compute_etag(identity, "identity", rep)

    def test_body_change_changes_tag(self):
        rep = b"rep"
        assert (etag_mod.compute_etag(b"a", "identity", rep)
                != etag_mod.compute_etag(b"b", "identity", rep))


class TestIfNoneMatchMatching:
    def test_exact_match(self):
        tag = '"abc123"'
        assert etag_mod.if_none_match_matches('"abc123"', tag)

    def test_star_matches_any(self):
        assert etag_mod.if_none_match_matches("*", '"anything"')

    def test_list_match(self):
        assert etag_mod.if_none_match_matches(
            '"x", "abc123", W/"y"', '"abc123"')

    def test_weak_comparison_ignores_weakness_marker(self):
        # RFC 9110: If-None-Match uses WEAK comparison — a W/ prefix on
        # either side is ignored; opaque tags are compared.
        assert etag_mod.if_none_match_matches('"abc123"', 'W/"abc123"')
        assert etag_mod.if_none_match_matches('W/"abc123"', '"abc123"')

    def test_lowercase_weak_marker_not_matched(self):
        # rev-gpt B1-2: RFC 9110 weakness markers are case-sensitive (only
        # "W/" is a weakness marker). A lowercase "w/" prefix is NOT a
        # valid entity-tag — the candidate is malformed and skipped, so a
        # client sending 'w/"abc123"' gets a conservative 200.
        assert not etag_mod.if_none_match_matches('w/"abc123"', 'W/"abc123"')
        assert not etag_mod.if_none_match_matches('w/"abc123"', '"abc123"')
        # ...but a well-formed sibling candidate in the same list still can.
        assert etag_mod.if_none_match_matches(
            'w/"abc123", "abc123"', 'W/"abc123"')

    def test_mismatch_returns_false(self):
        assert not etag_mod.if_none_match_matches('"other"', '"abc123"')
        assert not etag_mod.if_none_match_matches('"x", "y"', '"abc123"')

    def test_malformed_candidates_skipped(self):
        assert not etag_mod.if_none_match_matches(
            'garbage, "unterminated, "" , "abc123x"', '"abc123"')

    def test_empty_header_no_match(self):
        assert not etag_mod.if_none_match_matches("", '"abc123"')
        assert not etag_mod.if_none_match_matches("   ", '"abc123"')


class TestRepresentationVersion:
    """B1-C3 + config fingerprint (plan §4 REP_VERSION)."""

    def _settings(self, **ov) -> Settings:
        return _settings(**ov)

    def test_monkeypatched_rep_version_changes_etag(self, monkeypatch):
        config = self._settings()
        body = b"same-body"
        tag1 = etag_mod.compute_etag(
            body, "identity", etag_mod.representation_version(config))
        monkeypatch.setattr(
            etag_mod, "SKELETON_REPRESENTATION_VERSION", b"bumped-v2")
        tag2 = etag_mod.compute_etag(
            body, "identity", etag_mod.representation_version(config))
        assert tag1 != tag2  # representation evolution → new validators

    def test_skeleton_limit_fingerprint_changes_etag(self):
        body = b"same-body"
        tag1 = etag_mod.compute_etag(
            body, "identity",
            etag_mod.representation_version(
                self._settings(skeleton_inline_output_max_bytes=1024)))
        tag2 = etag_mod.compute_etag(
            body, "identity",
            etag_mod.representation_version(
                self._settings(skeleton_inline_output_max_bytes=2048)))
        assert tag1 != tag2

    def test_b3_fingerprint_switch_placeholder_reserved(self):
        """The B3 (Batch 4) ``message_fingerprint_enabled`` switch does not
        exist on Settings yet — the fingerprint must READ it via getattr
        (default True) so flipping it in Batch 4 rotates every ETag without
        an etag.py change."""
        body = b"same-body"
        base = etag_mod.compute_etag(
            body, "identity", etag_mod.representation_version(_settings()))
        # absent attr (today) == explicit True (Batch 4 default) → same tag
        class _WithSwitch:
            skeleton_inline_output_max_bytes = 1024 * 1024
            skeleton_inline_output_max_message_bytes = 1024 * 1024
            message_fingerprint_enabled = True

        class _SwitchOff:
            skeleton_inline_output_max_bytes = 1024 * 1024
            skeleton_inline_output_max_message_bytes = 1024 * 1024
            message_fingerprint_enabled = False

        assert etag_mod.compute_etag(
            body, "identity", etag_mod.representation_version(_WithSwitch())
        ) == etag_mod.compute_etag(
            body, "identity", etag_mod.representation_version(
                _settings(skeleton_inline_output_max_bytes=1024 * 1024,
                          skeleton_inline_output_max_message_bytes=1024 * 1024)))
        assert etag_mod.compute_etag(
            body, "identity", etag_mod.representation_version(_WithSwitch())
        ) != etag_mod.compute_etag(
            body, "identity", etag_mod.representation_version(_SwitchOff()))
        del base  # noqa: F841 (kept for symmetry/documentation)

    def test_response_rep_version_none_when_disabled(self):
        assert etag_mod.response_rep_version(
            _settings(etag_enabled=False)) is None
        assert etag_mod.response_rep_version(_settings()) is not None


# ---------------------------------------------------------------------------
# Route level — B1-C1 (messages list: full header set, *, mismatch)
# ---------------------------------------------------------------------------

async def test_c1_messages_list_304_header_set_complete(upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get("/slimapi/messages/s1", headers=HDR)
            assert r1.status_code == 200
            etag_value = r1.headers["ETag"]
            assert not etag_value.startswith("W/")  # identity → STRONG
            assert r1.headers["Vary"] == (
                "Accept-Encoding")
            assert r1.headers["Cache-Control"] == "no-store"
            assert r1.json()["nextCursor"] == "CURSOR123"

            r2 = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR, "If-None-Match": etag_value})
            assert r2.status_code == 304
            assert r2.content == b""  # no body
            assert r2.headers["ETag"] == etag_value
            assert r2.headers["Vary"] == r1.headers["Vary"]
            assert r2.headers["Cache-Control"] == "no-store"
            # §6.4 terminal: 304 carries only ETag/Vary/Cache-Control —
            # the cursor lives in the client's cached envelope.
            assert "X-Next-Cursor" not in r2.headers
            assert "content-length" not in r2.headers

            r3 = await client.get(
                "/slimapi/messages/s1", headers={**HDR, "If-None-Match": "*"})
            assert r3.status_code == 304

            r4 = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR, "If-None-Match": '"0000"'})
            assert r4.status_code == 200
            assert r4.headers["ETag"] == etag_value
            assert r4.content == r1.content
    finally:
        _teardown(app)


async def test_c1_agent_gzip_weak_etag_and_304(upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get(
                "/slimapi/agent",
                headers={**HDR, "Accept-Encoding": "gzip"})
            assert r1.status_code == 200
            assert r1.headers["Content-Encoding"] == "gzip"
            etag_value = r1.headers["ETag"]
            assert etag_value.startswith('W/"')  # gzip → WEAK

            r2 = await client.get(
                "/slimapi/agent",
                headers={**HDR, "Accept-Encoding": "gzip",
                         "If-None-Match": etag_value})
            assert r2.status_code == 304
            assert r2.content == b""
            assert r2.headers["ETag"] == etag_value
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# B1-C2 — upstream content change → new validator (incl. merged special)
# ---------------------------------------------------------------------------

async def test_c2_content_change_new_etag_old_validator_200(upstream_factory):
    state = {"agent": AGENTS_BODY}
    upstream = upstream_factory(_catalog_handler(state))
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get("/slimapi/agent", headers=HDR)
            old_etag = r1.headers["ETag"]
            state["agent"] = orjson.dumps([
                {"name": "build", "description": "b", "mode": "primary",
                 "prompt": "x"},
                {"name": "plan", "description": "CHANGED", "mode": "special",
                 "prompt": "y"},
            ])
            r2 = await client.get(
                "/slimapi/agent",
                headers={**HDR, "If-None-Match": old_etag})
            assert r2.status_code == 200  # no stale 304
            assert r2.headers["ETag"] != old_etag
    finally:
        _teardown(app)


async def test_c2_merged_full_detail_change_rotates_etag(upstream_factory):
    """Merged special: the LIST body is unchanged; only the /full detail
    changes → the final merged body changes → new ETag → the old validator
    gets 200 (never a stale 304)."""
    state = {"full": orjson.dumps(FULL_MSG_V1)}
    upstream = upstream_factory(_catalog_handler(state))
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            path = "/slimapi/messages/s1?mode=merged"
            r1 = await client.get(path, headers=HDR)
            assert r1.status_code == 200
            old_etag = r1.headers["ETag"]
            assert b"hello full" in r1.content
            assert b"CHANGED" not in r1.content

            state["full"] = orjson.dumps(FULL_MSG_V2)
            # Phase-B ``singleflight.fulls`` keeps the V1 body for its 1s
            # result grace (A-batch semantics — unchanged by B1); let it
            # lapse so the changed detail is actually fetched.
            await asyncio.sleep(1.2)
            r2 = await client.get(
                path, headers={**HDR, "If-None-Match": old_etag})
            assert r2.status_code == 200
            assert b"CHANGED" in r2.content
            assert r2.headers["ETag"] != old_etag

            r3 = await client.get(
                path, headers={**HDR, "If-None-Match": r2.headers["ETag"]})
            assert r3.status_code == 304
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# B1-C3 — REP_VERSION bump at the route level
# ---------------------------------------------------------------------------

async def test_c3_rep_version_bump_rotates_route_etag(
        upstream_factory, monkeypatch):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get("/slimapi/command", headers=HDR)
            old_etag = r1.headers["ETag"]

            monkeypatch.setattr(
                etag_mod, "SKELETON_REPRESENTATION_VERSION", b"bumped-v2")
            r2 = await client.get(
                "/slimapi/command",
                headers={**HDR, "If-None-Match": old_etag})
            assert r2.status_code == 200  # same body, new representation
            assert r2.headers["ETag"] != old_etag
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# B1-C4 — directory semantic isolation
# ---------------------------------------------------------------------------

async def test_c4_directory_isolation(upstream_factory):
    per_dir = {
        "/a": orjson.dumps([
            {"name": "build", "description": "dir-a", "mode": "primary"}]),
        "/b": orjson.dumps([
            {"name": "build", "description": "dir-b", "mode": "primary"}]),
    }
    state = {"agent_per_directory": per_dir}
    upstream = upstream_factory(_catalog_handler(state))
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            ra = await client.get("/slimapi/agent",
                                  params={"directory": "/a"}, headers=HDR)
            rb = await client.get("/slimapi/agent",
                                  params={"directory": "/b"}, headers=HDR)
            assert ra.status_code == 200 and rb.status_code == 200
            etag_a = ra.headers["ETag"]
            assert ra.content != rb.content

            # ① A's validator sent to B's request → 200 (no cross-directory
            # false hit), even though both share the same path.
            r = await client.get(
                "/slimapi/agent", params={"directory": "/b"},
                headers={**HDR, "If-None-Match": etag_a})
            assert r.status_code == 200

            # B's own validator still 304s (sanity).
            r = await client.get(
                "/slimapi/agent", params={"directory": "/b"},
                headers={**HDR, "If-None-Match": rb.headers["ETag"]})
            assert r.status_code == 304

            # ② merged Vary list, Accept-Encoding FIRST, both codings.
            assert ra.headers["Vary"] == (
                "Accept-Encoding")
            rg = await client.get(
                "/slimapi/agent", params={"directory": "/a"},
                headers={**HDR, "Accept-Encoding": "gzip"})
            assert rg.headers["Vary"] == (
                "Accept-Encoding")

            # ③ per-coding validator regression: identity ≠ gzip.
            assert rg.headers["ETag"] != etag_a
            assert rg.headers["ETag"].startswith('W/"')
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# B1-C5 — cross-coding validator reuse is a conservative 200
# ---------------------------------------------------------------------------

async def test_c5_cross_coding_both_directions_200(upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r_id = await client.get("/slimapi/agent", headers=HDR)
            r_gz = await client.get(
                "/slimapi/agent",
                headers={**HDR, "Accept-Encoding": "gzip"})
            etag_id, etag_gz = r_id.headers["ETag"], r_gz.headers["ETag"]
            assert etag_id != etag_gz

            r1 = await client.get(
                "/slimapi/agent",
                headers={**HDR, "If-None-Match": etag_gz})
            assert r1.status_code == 200  # gzip validator, identity request

            r2 = await client.get(
                "/slimapi/agent",
                headers={**HDR, "Accept-Encoding": "gzip",
                         "If-None-Match": etag_id})
            assert r2.status_code == 200  # identity validator, gzip request
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# B1-C6 — disable switch (byte-identical today) + access log 304
# ---------------------------------------------------------------------------

async def test_c6_disabled_byte_identical_no_etag_no_304(upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(etag_enabled=False), upstream)
    try:
        async with _client(app) as client:
            for path in ("/slimapi/agent", "/slimapi/command",
                         "/slimapi/sessions", "/slimapi/messages/s1"):
                r = await client.get(
                    path, headers={**HDR, "If-None-Match": '"anything"'})
                assert r.status_code == 200
                assert "ETag" not in r.headers
                # v3-contract §6.2 (gate C3): the directory Vary dimension
                # survives the ETag switch — these routes are
                # directory-sensitive, so Vary stays merged unconditionally
                # (cache-correctness semantics, not an ETag accessory).
                assert r.headers["Vary"] == (
                    "Accept-Encoding")
                assert r.content  # full body — 304 judgement disabled
    finally:
        _teardown(app)


class _CaptureAccessLogger(logging.Logger):
    def __init__(self):
        super().__init__("capture-access")
        self.disabled = False
        self.rows: list[dict] = []

    def info(self, msg, *args, **kwargs):
        self.rows.append(json.loads(msg))


async def test_c6_304_recorded_in_access_log_with_zero_down_out(
        upstream_factory):
    from oc_slimapi.middleware.traffic_accounting import (
        TrafficAccountingMiddleware,
    )

    upstream = upstream_factory(_catalog_handler())
    inner = _build_app(_settings(), upstream)
    capture = _CaptureAccessLogger()
    # Pure-ASGI wrap around the fully-built app (version gate inside, the
    # traffic wrapper outside — mirroring production middleware order).
    transport_app = TrafficAccountingMiddleware(inner, logger=capture)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(transport_app),
            base_url="http://test",
        ) as client:
            r1 = await client.get("/slimapi/agent", headers=HDR)
            assert r1.status_code == 200
            r2 = await client.get(
                "/slimapi/agent",
                headers={**HDR, "If-None-Match": r1.headers["ETag"]})
            assert r2.status_code == 304
        assert len(capture.rows) == 2
        first, second = capture.rows
        assert first["status"] == 200
        assert second["status"] == 304
        assert second["downOut"] == 0  # ≈0: no transport body
        assert second["bucket"] == "agent"
    finally:
        _teardown(inner)


# ---------------------------------------------------------------------------
# B1-C7 — 4-route × 5-state matrix (happy / miss / disable / aux copy /
# dual coding), parameterised.
# ---------------------------------------------------------------------------

ROUTES = [
    "/slimapi/agent",
    "/slimapi/command",
    "/slimapi/sessions?limit=2",
    "/slimapi/messages/s1",
]
ROUTE_IDS = ["agent", "command", "sessions", "messages"]


def _aux_field(path: str) -> tuple[str, object]:
    if path.startswith("/slimapi/messages"):
        return "nextCursor", "CURSOR123"
    if path.startswith("/slimapi/sessions"):
        # 3 sessions, limit=2 → complete: false (value from THIS run)
        return "complete", False
    raise AssertionError(f"no aux field for {path}")


@pytest.mark.parametrize("path", ROUTES, ids=ROUTE_IDS)
async def test_c7_happy_200_then_304(path, upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r1 = await client.get(path, headers=HDR)
            assert r1.status_code == 200
            assert r1.headers["ETag"]
            r2 = await client.get(
                path, headers={**HDR, "If-None-Match": r1.headers["ETag"]})
            assert r2.status_code == 304
            assert r2.content == b""
    finally:
        _teardown(app)


@pytest.mark.parametrize("path", ROUTES, ids=ROUTE_IDS)
async def test_c7_miss_validator_stale_returns_200(path, upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r = await client.get(
                path, headers={**HDR, "If-None-Match": '"deadbeef"'})
            assert r.status_code == 200
            assert r.content
    finally:
        _teardown(app)


@pytest.mark.parametrize("path", ROUTES, ids=ROUTE_IDS)
async def test_c7_disabled_no_etag(path, upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(etag_enabled=False), upstream)
    try:
        async with _client(app) as client:
            r = await client.get(
                path, headers={**HDR, "If-None-Match": '"x"'})
            assert r.status_code == 200
            assert "ETag" not in r.headers
    finally:
        _teardown(app)


async def test_c7_envelope_fields_and_terminal_304_set(upstream_factory):
    """v3 terminal: the completeness/cursor signals live in the envelope
    body; the 304 header set is EXACTLY ETag + Vary + Cache-Control."""
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            for path in ("/slimapi/messages/s1", "/slimapi/sessions?limit=2"):
                r1 = await client.get(path, headers=HDR)
                name, expected = _aux_field(path)
                assert r1.json()[name] == expected
                r2 = await client.get(
                    path, headers={**HDR, "If-None-Match": r1.headers["ETag"]})
                assert r2.status_code == 304
                assert r2.content == b""
                assert set(r2.headers) == {"etag", "vary", "cache-control"}
    finally:
        _teardown(app)


@pytest.mark.parametrize("path", ROUTES, ids=ROUTE_IDS)
async def test_c7_dual_coding_distinct_validators(path, upstream_factory):
    upstream = upstream_factory(_catalog_handler())
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r_id = await client.get(path, headers=HDR)
            r_gz = await client.get(
                path, headers={**HDR, "Accept-Encoding": "gzip"})
            assert r_id.headers["ETag"] != r_gz.headers["ETag"]
            assert not r_id.headers["ETag"].startswith("W/")
            assert r_gz.headers["ETag"].startswith('W/"')
            # each validator 304s its own coding (messages identity body may
            # be gzip-beneficial either way — the tags, not the encodings,
            # are under test here)
            r2 = await client.get(
                path, headers={**HDR, "If-None-Match": r_id.headers["ETag"]})
            assert r2.status_code == 304
            r3 = await client.get(
                path, headers={**HDR, "Accept-Encoding": "gzip",
                               "If-None-Match": r_gz.headers["ETag"]})
            assert r3.status_code == 304
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# A-batch interplay — coalesced GET + per-caller 304 + catalog cache hit
# ---------------------------------------------------------------------------

async def test_coalesced_callers_judge_304_independently(upstream_factory):
    """Joiners of ONE shared upstream GET each evaluate their own
    If-None-Match: the leader (no validator) gets 200 + ETag, the joiner
    replaying that ETag gets 304 — the shared unit is the upstream GET,
    never the conditional decision (plan §4 / Batch 1 interplay).

    Uses the messages LIST (the A2-coalesced route; catalog listings
    coalesce via the TTL cache instead — covered by the next test)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, content=MSG_LIST_BODY, headers={"Link": LIST_LINK})

    upstream = upstream_factory(handler)
    app = _build_app(
        _settings(coalesce_enabled=True, raw_fetch_max_bytes=256 * 1024),
        upstream, with_registry=True)
    try:
        async with _client(app) as client:
            r1 = await client.get("/slimapi/messages/s1", headers=HDR)
            etag_value = r1.headers["ETag"]
            r2, r3 = await asyncio.gather(
                client.get("/slimapi/messages/s1", headers=HDR),
                client.get("/slimapi/messages/s1",
                           headers={**HDR, "If-None-Match": etag_value}),
            )
        # r2 + r3 join r1's retained entry inside the A-batch result grace
        # (deterministic: both fire well within 1s) — ONE upstream GET
        # total, and the two callers still judge If-None-Match
        # independently against that shared body.
        assert calls["n"] == 1
        assert r2.status_code == 200
        assert r3.status_code == 304  # per-caller judgement on the join
        assert r3.headers["ETag"] == etag_value
    finally:
        _teardown(app)


async def test_catalog_cache_hit_path_emits_etag_and_304(upstream_factory):
    """A1 × B1: a TTL-fresh cache hit still projects + emits the ETag (the
    cached raw body's projected final body IS the validator input); the
    replay gets a 304 with zero upstream GETs."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=AGENTS_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream, with_catalog_cache=True)
    try:
        async with _client(app) as client:
            r1 = await client.get("/slimapi/agent", headers=HDR)
            assert r1.status_code == 200
            etag_value = r1.headers["ETag"]
            assert calls["n"] == 1
            r2 = await client.get(
                "/slimapi/agent",
                headers={**HDR, "If-None-Match": etag_value})
            assert r2.status_code == 304
            assert calls["n"] == 1  # cache hit → no second upstream GET
    finally:
        _teardown(app)


# ---------------------------------------------------------------------------
# rev-gpt B1-4 regression tests (pre-compression validator discipline)
# ---------------------------------------------------------------------------

async def test_b1_4_gzip_hit_does_not_compress_catalog(upstream_factory, monkeypatch):
    """B1-1 regression: a gzip validator hit must be canonical-hash-only —
    ZERO compression on the 304 path (plan §4). Spies ``gzip.compress``
    (the catalog pack worker's compressor): the 200 compresses once, the
    conditional replay never does."""
    compress_calls = {"n": 0}
    original_compress = gzip.compress

    def spy_compress(data, **kwargs):
        compress_calls["n"] += 1
        return original_compress(data, **kwargs)

    monkeypatch.setattr(gzip, "compress", spy_compress)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=AGENTS_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        gzip_hdr = {**HDR, "Accept-Encoding": "gzip"}
        async with _client(app) as client:
            r1 = await client.get("/slimapi/agent", headers=gzip_hdr)
            assert r1.status_code == 200
            assert r1.headers.get("Content-Encoding") == "gzip"
            assert compress_calls["n"] == 1  # the 200 DID compress
            etag_value = r1.headers["ETag"]
            assert etag_value.startswith("W/")  # gzip → weak validator

            compress_calls["n"] = 0
            r2 = await client.get(
                "/slimapi/agent",
                headers={**gzip_hdr, "If-None-Match": etag_value})
            assert r2.status_code == 304
            assert r2.content == b""
            assert "content-encoding" not in r2.headers
            # THE assertion: the 304 path performed ZERO compression work.
            assert compress_calls["n"] == 0
    finally:
        _teardown(app)


async def test_b1_4_gzip_hit_does_not_compress_messages(upstream_factory, monkeypatch):
    """B1-1 regression on the messages list (lease-less direct path): the
    route judges ``If-None-Match`` on the identity bytes BEFORE calling
    ``compress_if_beneficial`` — a hit never reaches the compressor."""
    compress_calls = {"n": 0}
    from oc_slimapi.gzip_util import compress_if_beneficial as original_cib

    def spy_cib(body, accept_encoding):
        compress_calls["n"] += 1
        return original_cib(body, accept_encoding)

    monkeypatch.setattr(messages, "compress_if_beneficial", spy_cib)

    state: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps([MSG_PLACEHOLDER, MSG_PLAIN]),
            headers={"Link": LIST_LINK},
        )

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        gzip_hdr = {**HDR, "Accept-Encoding": "gzip"}
        async with _client(app) as client:
            r1 = await client.get("/slimapi/messages/s1", headers=gzip_hdr)
            assert r1.status_code == 200
            assert r1.headers.get("Content-Encoding") == "gzip"
            assert compress_calls["n"] == 1
            etag_value = r1.headers["ETag"]
            assert etag_value.startswith("W/")

            compress_calls["n"] = 0
            r2 = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr, "If-None-Match": etag_value})
            assert r2.status_code == 304
            assert r2.content == b""
            # §6.4 terminal: no cursor header on the 304 (envelope cached)
            assert "X-Next-Cursor" not in r2.headers
            assert compress_calls["n"] == 0  # zero compression on the hit
    finally:
        _teardown(app)


async def test_b1_4_head_requests_bypass_etag_304(upstream_factory):
    """B1-4 regression: HEAD keeps today's behaviour — the ETag routes are
    GET-only, so a HEAD falls through to the catch-all proxy (thin 404 for
    an unmatched non-GET): no validator emission, no 304 logic, no upstream
    fetch. Locked so a future HEAD plumbing change is a conscious one."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=AGENTS_BODY)

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r_head = await client.head("/slimapi/agent", headers=HDR)
            assert r_head.status_code == 404  # current GET-only behaviour
            assert "etag" not in r_head.headers
            assert calls["n"] == 0  # neither 304 logic nor upstream GET ran

            # sanity: the same route IS ETag-active on GET
            r_get = await client.get("/slimapi/agent", headers=HDR)
            assert r_get.status_code == 200
            assert "ETag" in r_get.headers
    finally:
        _teardown(app)


async def test_b1_4_error_responses_carry_no_etag(upstream_factory):
    """B1-4 regression: 4xx/5xx never carry a validator and never judge
    ``If-None-Match`` — conditional logic applies to 200-track responses
    only."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"error":"boom"}')

    upstream = upstream_factory(handler)
    app = _build_app(_settings(), upstream)
    try:
        async with _client(app) as client:
            r = await client.get(
                "/slimapi/agent",
                headers={**HDR, "If-None-Match": "*"})
            assert r.status_code == 503  # upstream 500 → 503 upstream_unavailable
            assert "etag" not in r.headers

            r2 = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR, "If-None-Match": "*"})
            assert r2.status_code == 503
            assert "etag" not in r2.headers
    finally:
        _teardown(app)

    # 413 response_too_large (cap overflow) — no ETag either.
    def big_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=orjson.dumps([MSG_PLACEHOLDER, MSG_PLAIN]))

    upstream2 = upstream_factory(big_handler)
    app2 = _build_app(_settings(max_response_bytes=8), upstream2)
    try:
        async with _client(app2) as client:
            r3 = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR, "If-None-Match": "*"})
            assert r3.status_code == 413
            assert "etag" not in r3.headers
    finally:
        _teardown(app2)


async def test_b1_1r_incompressible_body_labels_actual_coding(upstream_factory, monkeypatch):
    """B1-1R (rev-5): an incompressible (≥64B, gzip-expanding) identity body
    with ``Accept-Encoding: gzip`` falls back through compress_if_beneficial's
    benefit gate to an identity 200 — which must carry the STRONG identity
    validator, never a mislabelled ``W/`` gzip tag (plan §4: validators
    label the representation's ACTUAL coding). A replay of that strong tag
    under a gzip-capable request is a CONSERVATIVE 200 (rev-5 rule: the
    served coding is unknowable pre-compression — B1-C5 reverse)."""
    # Genuinely incompressible identity bytes: fresh random data regenerating
    # until gzip provably expands it (urandom typically expands on the first
    # draw; the loop makes the property guaranteed, not probabilistic).
    identity = os.urandom(600)
    while len(gzip.compress(identity, compresslevel=6)) < len(identity):
        identity = os.urandom(600)
    assert len(identity) >= 64

    # Replace the list pack worker: its output IS the identity body the
    # route judges and (maybe) compresses.
    monkeypatch.setattr(
        messages, "_project_list_sorted_and_pack",
        lambda body, *, accept_encoding, limits, sid=None: identity)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    settings = _settings()
    rep = etag_mod.representation_version(settings, wire_view=3)
    enveloped = messages_envelope_bytes(identity, None)
    strong_tag = etag_mod.compute_etag(enveloped, "identity", rep)
    weak_tag = etag_mod.compute_etag(enveloped, "gzip", rep)
    assert strong_tag != weak_tag

    upstream = upstream_factory(handler)
    app = _build_app(settings, upstream)
    try:
        gzip_hdr = {**HDR, "Accept-Encoding": "gzip"}
        async with _client(app) as client:
            r1 = await client.get("/slimapi/messages/s1", headers=gzip_hdr)
            assert r1.status_code == 200
            assert "content-encoding" not in r1.headers  # benefit gate fell back
            assert not r1.headers["ETag"].startswith("W/")  # ACTUAL coding
            assert r1.headers["ETag"] == strong_tag

            # The emitted strong tag under a GZIP-CAPABLE request: the
            # served coding is not statically knowable (the body might have
            # compressed) → conservative 200 with the full body (rev-5).
            r2 = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr, "If-None-Match": strong_tag})
            assert r2.status_code == 200
            assert r2.content

            # The same strong tag under an IDENTITY-ONLY request: the served
            # coding IS statically identity → exact single-candidate 304.
            r3 = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR, "If-None-Match": strong_tag})
            assert r3.status_code == 304
            assert r3.headers["ETag"] == strong_tag
    finally:
        _teardown(app)


async def test_b1_1r_rev5_case_matrix(upstream_factory, monkeypatch):
    """rev-5 single-candidate judgment matrix — every gap the reviewer
    listed, on the benefit-gated messages route (pack worker replaced so
    the identity body is exactly what we choose):

    compressible   = deflate provably shrinks it
    incompressible = deflate provably expands it
    """
    compressible = b'["' + b"x" * 400 + b'"]'
    assert len(gzip.compress(compressible, compresslevel=6)) < len(compressible)
    incompressible = os.urandom(600)
    while len(gzip.compress(incompressible, compresslevel=6)) < len(incompressible):
        incompressible = os.urandom(600)
    small = b"[{}]"  # 3 bytes < MIN_GZIP_BYTES: min gate forces identity

    holder: dict[str, bytes] = {}
    monkeypatch.setattr(
        messages, "_project_list_sorted_and_pack",
        lambda body, *, accept_encoding, limits, sid=None: holder["identity"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    settings = _settings()
    rep = etag_mod.representation_version(settings, wire_view=3)

    def tag(body: bytes, coding: str) -> str:
        # the route envelopes the pack worker's output before judging
        return etag_mod.compute_etag(
            messages_envelope_bytes(body, None), coding, rep)

    gzip_hdr = {**HDR, "Accept-Encoding": "gzip"}
    upstream = upstream_factory(handler)
    app = _build_app(settings, upstream)
    try:
        async with _client(app) as client:
            # (a) compressible + gzip AE + identity strong tag → 200, body
            # actually served gzip, weak validator (C5 reverse direction).
            holder["identity"] = compressible
            ra = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr, "If-None-Match": tag(compressible, "identity")})
            assert ra.status_code == 200
            assert ra.headers.get("Content-Encoding") == "gzip"
            assert ra.headers["ETag"] == tag(compressible, "gzip")
            assert ra.headers["ETag"].startswith("W/")

            # (b) incompressible + gzip AE + identity strong tag → 200, body
            # actually served identity, strong validator.
            holder["identity"] = incompressible
            rb = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr,
                         "If-None-Match": tag(incompressible, "identity")})
            assert rb.status_code == 200
            assert "content-encoding" not in rb.headers
            assert rb.headers["ETag"] == tag(incompressible, "identity")

            # (c) incompressible + identity-only AE + identity strong tag →
            # 304 (rule 1: exact single-candidate on a static coding).
            rc = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR,
                         "If-None-Match": tag(incompressible, "identity")})
            assert rc.status_code == 304
            assert rc.headers["ETag"] == tag(incompressible, "identity")

            # (d) compressible + gzip AE + gzip weak tag → 304, echo the
            # gzip tag, zero compression (soundness: a lawful gzip-tag
            # holder received gzip for this unchanged content).
            holder["identity"] = compressible
            rd = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr,
                         "If-None-Match": tag(compressible, "gzip")})
            assert rd.status_code == 304
            assert rd.headers["ETag"] == tag(compressible, "gzip")

            # (e) gzip weak tag + identity-only AE → 200 (C5 direction one:
            # the only candidate for an identity-only request is the
            # identity tag; a gzip tag never matches it).
            re_ = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR,
                         "If-None-Match": tag(compressible, "gzip")})
            assert re_.status_code == 200
            assert re_.content

            # (f) ``*`` + gzip AE → 304 echoing the ACTUAL coding's tag:
            # compressible body → weak tag; incompressible body → strong.
            holder["identity"] = compressible
            rf1 = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr, "If-None-Match": "*"})
            assert rf1.status_code == 304
            assert rf1.headers["ETag"] == tag(compressible, "gzip")
            assert rf1.headers["ETag"].startswith("W/")

            holder["identity"] = incompressible
            rf2 = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr, "If-None-Match": "*"})
            assert rf2.status_code == 304
            assert rf2.headers["ETag"] == tag(incompressible, "identity")
            assert not rf2.headers["ETag"].startswith("W/")

            # (g) sub-MIN_GZIP_BYTES body + gzip AE + identity strong tag →
            # 304 (rule 2: the min gate makes identity the static coding).
            holder["identity"] = small
            rg = await client.get(
                "/slimapi/messages/s1",
                headers={**gzip_hdr, "If-None-Match": tag(small, "identity")})
            assert rg.status_code == 304
            assert rg.headers["ETag"] == tag(small, "identity")
    finally:
        _teardown(app)


async def test_b1_1r_identity_only_request_excludes_gzip_validator(upstream_factory, monkeypatch):
    """B1-1R / B1-C5 compatibility: an identity-only request (no gzip
    accepted) can produce EXACTLY one coding — identity — so judge_conditional
    matches only the identity tag and a gzip validator held by the client
    yields a conservative 200 on the messages route."""
    identity = os.urandom(600)
    while len(gzip.compress(identity, compresslevel=6)) < len(identity):
        identity = os.urandom(600)
    monkeypatch.setattr(
        messages, "_project_list_sorted_and_pack",
        lambda body, *, accept_encoding, limits, sid=None: identity)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[]")

    settings = _settings()
    rep = etag_mod.representation_version(settings, wire_view=3)
    weak_tag = etag_mod.compute_etag(identity, "gzip", rep)

    upstream = upstream_factory(handler)
    app = _build_app(settings, upstream)
    try:
        async with _client(app) as client:
            r = await client.get(
                "/slimapi/messages/s1",
                headers={**HDR, "If-None-Match": weak_tag})
            assert r.status_code == 200  # gzip tag ∉ {identity} candidates
            assert r.content  # full body served
    finally:
        _teardown(app)
