"""Traffic plan Batch 4 / B3 — message content fingerprint.

Covers plan §6 Task 4.2 ACs:
- B3-C3: recomputation determinism; content change → fingerprint change;
  merged splice recomputation (full detail change with unchanged list body
  → merged fingerprint changes); the FIVE merged degrade paths do NOT
  recompute (skeleton-period fingerprint retained); cross-"restart"
  determinism; digest-event independence (no/out-of-order/duplicate digest).
- B3-C4: additive compatibility — existing skeleton/messages tests keep
  passing unchanged (pure functions default to NO field); new-field presence
  + format assertions; normalisation (fingerprint input excludes itself).
- B3-C5: message_fingerprint_enabled=false → byte-for-byte today regression
  (plus REP_VERSION linkage with the Batch 2 ETag switch).
"""

from __future__ import annotations

import asyncio
import re

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.etag import representation_version
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import messages as messages_mod
from oc_slimapi.routes import messages
from oc_slimapi.skeleton import (
    FINGERPRINT_VERSION,
    compute_message_fingerprint,
    recompute_fingerprint,
    skeleton_message,
    skeleton_messages,
)
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.registry import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.versioning import SlimapiVersionMiddleware

# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_etag.py upstream mocks)
# ---------------------------------------------------------------------------

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

FINGERPRINT_RE = re.compile(r"^v\d+:[0-9a-f]{64}$")


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
        server_api_version=2,
        accepted_client_versions=(2, 2),
        coalesce_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
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
    app.include_router(messages.router)
    register_error_handlers(app)
    return app

HDRS = {"x-slimapi-version": "2", "x-opencode-client-version": "2"}


@pytest.fixture
async def upstream_factory():
    clients: list[httpx.AsyncClient] = []

    def _make(handler):
        client = httpx.AsyncClient(
            base_url="http://127.0.0.1:4096",
            transport=httpx.MockTransport(handler),
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


def _messages_handler(state: dict | None = None):
    state = state if state is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message/msg_1":
            if "full" in state:
                body = state["full"]
                if isinstance(body, httpx.Response):
                    return body
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_V1))
        if path == "/session/s1/message":
            return httpx.Response(
                200, content=state.get("list", MSG_LIST_BODY),
                headers={"Link": LIST_LINK},
            )
        raise AssertionError(f"unexpected path {path}")

    return handler


def _fingerprint_of(msg: dict) -> str:
    return msg["contentFingerprint"]


async def _get_messages(app, *, merged: bool = False):
    path = "/slimapi/messages/s1" + ("?mode=merged" if merged else "")
    async with _client(app) as client:
        r = await client.get(path, headers=HDRS)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Pure-function group (skeleton.py)
# ---------------------------------------------------------------------------

class TestPureFunctions:
    def test_fingerprint_version_constant_is_one(self):
        assert FINGERPRINT_VERSION == 1

    def test_format_version_prefix_and_full_hex(self):
        msg = skeleton_message(MSG_PLAIN)
        fp = compute_message_fingerprint(msg)
        assert FINGERPRINT_RE.match(fp), fp
        assert fp.startswith("v1:")

    def test_deterministic_same_input_same_fingerprint(self):
        a = compute_message_fingerprint(skeleton_message(MSG_PLAIN))
        b = compute_message_fingerprint(skeleton_message(MSG_PLAIN))
        assert a == b

    def test_excludes_fingerprint_field_itself(self):
        base = skeleton_message(MSG_PLAIN)
        clean_fp = compute_message_fingerprint(base)
        stale = dict(base)
        stale["contentFingerprint"] = "v1:" + "0" * 64
        stale2 = dict(base)
        stale2["contentFingerprint"] = "v1:deadbeef"
        assert compute_message_fingerprint(stale) == clean_fp
        assert compute_message_fingerprint(stale2) == clean_fp

    def test_recompute_overwrites_in_place(self):
        msg = skeleton_message(MSG_PLAIN)
        msg["contentFingerprint"] = "v1:" + "0" * 64
        recompute_fingerprint(msg)
        assert msg["contentFingerprint"] == compute_message_fingerprint(
            skeleton_message(MSG_PLAIN))

    def test_content_change_changes_fingerprint(self):
        base = skeleton_message(MSG_PLAIN)
        base_fp = compute_message_fingerprint(base)
        # text change
        text_changed = skeleton_message({
            **MSG_PLAIN,
            "parts": [{"id": "p_text", "type": "text",
                       "messageID": "msg_2", "text": "plain2"}],
        })
        assert compute_message_fingerprint(text_changed) != base_fp
        # part added
        part_added = skeleton_message({
            **MSG_PLAIN,
            "parts": MSG_PLAIN["parts"] + [
                {"id": "p2", "type": "text", "messageID": "msg_2",
                 "text": "extra"}],
        })
        assert compute_message_fingerprint(part_added) != base_fp
        # info field change
        info_changed = skeleton_message({
            **MSG_PLAIN,
            "info": {**MSG_PLAIN["info"], "time": {"created": 1002}},
        })
        assert compute_message_fingerprint(info_changed) != base_fp

    def test_part_order_matters(self):
        m1 = skeleton_message({
            "info": MSG_PLAIN["info"],
            "parts": MSG_PLAIN["parts"] + [
                {"id": "p2", "type": "text", "messageID": "msg_2",
                 "text": "extra"}],
        })
        m2 = skeleton_message({
            "info": MSG_PLAIN["info"],
            "parts": [
                {"id": "p2", "type": "text", "messageID": "msg_2",
                 "text": "extra"},
            ] + MSG_PLAIN["parts"],
        })
        assert (compute_message_fingerprint(m1)
                != compute_message_fingerprint(m2))

    def test_skeleton_message_default_has_no_fingerprint(self):
        msg = skeleton_message(MSG_PLAIN)
        assert "contentFingerprint" not in msg
        msgs = skeleton_messages([MSG_PLAIN])
        assert "contentFingerprint" not in msgs[0]

    def test_skeleton_message_fingerprint_kwarg_injects(self):
        msg = skeleton_message(MSG_PLAIN, fingerprint=True)
        assert FINGERPRINT_RE.match(msg["contentFingerprint"])
        # injected value == compute over the projected message WITHOUT the
        # field (exclusion rule)
        without = {k: v for k, v in msg.items() if k != "contentFingerprint"}
        assert msg["contentFingerprint"] == compute_message_fingerprint(without)

    def test_skeleton_messages_fingerprint_kwarg_per_message(self):
        msgs = skeleton_messages([MSG_PLACEHOLDER, MSG_PLAIN],
                                 fingerprint=True)
        assert len(msgs) == 2
        for m in msgs:
            assert FINGERPRINT_RE.match(m["contentFingerprint"])
        assert (msgs[0]["contentFingerprint"]
                != msgs[1]["contentFingerprint"])

    def test_golden_vector(self):
        """Design-doc §4.5 golden vector — fixed input, fixed fingerprint.

        If this fails after an INTENTIONAL normalisation change, the change
        requires bumping FINGERPRINT_VERSION and updating design doc §4.5.
        """
        msg = {
            "info": {"id": "msg_golden", "role": "user",
                     "time": {"created": 1000}},
            "parts": [{"id": "prt_1", "type": "text", "text": "hello"}],
            "contentFingerprint":
                "v1:0000000000000000000000000000000000000000"
                "0000000000000000000000",
        }
        assert compute_message_fingerprint(msg) == GOLDEN_VECTOR_EXPECTED


# Golden vector — computed from the frozen normalisation rules
# (design doc §4.5). Locked here; changing it means changing the rules,
# which means bumping FINGERPRINT_VERSION.
GOLDEN_VECTOR_EXPECTED = "v1:e8b0deefd04c0f5d293ef1afd54c4f4b9dd0e190f52e07b5a5281fda3dce6f71"


# ---------------------------------------------------------------------------
# Route group (messages.py)
# ---------------------------------------------------------------------------

class TestRouteListFingerprint:
    async def test_list_messages_carry_fingerprint(self, upstream_factory):
        upstream = upstream_factory(_messages_handler())
        app = _build_app(_settings(), upstream)
        try:
            body = await _get_messages(app)
            assert len(body) == 2
            for m in body:
                assert FINGERPRINT_RE.match(m["contentFingerprint"]), m
        finally:
            app.state.transforms.shutdown()

    async def test_route_fingerprint_deterministic_across_requests(
        self, upstream_factory,
    ):
        upstream = upstream_factory(_messages_handler())
        app = _build_app(_settings(), upstream)
        try:
            first = await _get_messages(app)
            second = await _get_messages(app)
            assert ([_fingerprint_of(m) for m in first]
                    == [_fingerprint_of(m) for m in second])
        finally:
            app.state.transforms.shutdown()

    async def test_route_content_change_changes_fingerprint(
        self, upstream_factory,
    ):
        state = {"list": MSG_LIST_BODY}
        upstream = upstream_factory(_messages_handler(state))
        app = _build_app(_settings(), upstream)
        try:
            before = [_fingerprint_of(m) for m in await _get_messages(app)]
            state["list"] = orjson.dumps([
                {**MSG_PLACEHOLDER,
                 "parts": [{"id": "p_empty", "type": "text",
                            "messageID": "msg_1", "text": "now filled"}]},
                MSG_PLAIN,
            ])
            after = await _get_messages(app)
            assert _fingerprint_of(after[0]) != before[0]
            # untouched message keeps its fingerprint
            assert _fingerprint_of(after[1]) == before[1]
        finally:
            app.state.transforms.shutdown()

    async def test_cross_restart_determinism_new_app_same_fingerprint(
        self, upstream_factory,
    ):
        upstream = upstream_factory(_messages_handler())
        app1 = _build_app(_settings(), upstream)
        app2 = _build_app(_settings(), upstream)  # fresh "restart"
        try:
            first = [_fingerprint_of(m) for m in await _get_messages(app1)]
            second = [_fingerprint_of(m) for m in await _get_messages(app2)]
            assert first == second
        finally:
            app1.state.transforms.shutdown()
            app2.state.transforms.shutdown()


class TestMergedSpliceRecompute:
    async def test_merged_splice_recomputes_over_skeleton_fingerprint(
        self, upstream_factory, monkeypatch,
    ):
        calls: list[str] = []

        def spy(msg):
            calls.append(msg["info"].get("id", "?"))
            return recompute_fingerprint(msg)

        monkeypatch.setattr(messages_mod, "recompute_fingerprint", spy)
        upstream = upstream_factory(_messages_handler())
        app = _build_app(_settings(), upstream)
        try:
            default_body = await _get_messages(app)
            merged_body = await _get_messages(app, merged=True)
            default_fp = _fingerprint_of(default_body[0])
            merged_fp = _fingerprint_of(merged_body[0])
            # merged recomputed exactly the spliced message
            assert calls == ["msg_1"]
            # cross-mode difference: spliced full parts ≠ skeleton parts
            assert merged_fp != default_fp
            # recomputed value == compute over the spliced message
            spliced = dict(merged_body[0])
            without = {k: v for k, v in spliced.items()
                       if k != "contentFingerprint"}
            assert merged_fp == compute_message_fingerprint(without)
        finally:
            app.state.transforms.shutdown()

    async def test_merged_full_detail_change_changes_fingerprint(
        self, upstream_factory,
    ):
        """B3-C3 merged: list body UNCHANGED, /full detail changes → merged
        response fingerprint changes (skeleton-period value would not)."""
        state = {"full": orjson.dumps(FULL_MSG_V1)}
        upstream = upstream_factory(_messages_handler(state))
        app = _build_app(_settings(), upstream)
        try:
            before = _fingerprint_of((await _get_messages(app, merged=True))[0])
            state["full"] = orjson.dumps(FULL_MSG_V2)
            # Phase-B ``singleflight.fulls`` keeps the V1 body for its 1s
            # result grace (A-batch semantics); let it lapse so the changed
            # detail is actually fetched.
            await asyncio.sleep(1.2)
            after = _fingerprint_of((await _get_messages(app, merged=True))[0])
            assert after != before
        finally:
            app.state.transforms.shutdown()

    async def test_default_mode_fingerprint_unaffected_by_full_change(
        self, upstream_factory,
    ):
        state = {"full": orjson.dumps(FULL_MSG_V1)}
        upstream = upstream_factory(_messages_handler(state))
        app = _build_app(_settings(), upstream)
        try:
            before = [_fingerprint_of(m)
                      for m in await _get_messages(app)]
            state["full"] = orjson.dumps(FULL_MSG_V2)
            after = [_fingerprint_of(m) for m in await _get_messages(app)]
            assert after == before
        finally:
            app.state.transforms.shutdown()


class TestMergedDegradePathsNoRecompute:
    """B3-C3 v1.3 — the FIVE degrade paths must NOT recompute; the message
    keeps its skeleton-period fingerprint (the final representation IS the
    skeleton). Each case asserts the recompute spy was never invoked."""

    async def _run_merged(self, upstream_factory, monkeypatch, state,
                          *, merged_max_bytes=None):
        calls: list[str] = []

        def spy(msg):
            calls.append(msg["info"].get("id", "?"))
            return recompute_fingerprint(msg)

        monkeypatch.setattr(messages_mod, "recompute_fingerprint", spy)
        upstream = upstream_factory(_messages_handler(state))
        overrides = {}
        if merged_max_bytes is not None:
            overrides["merged_max_bytes"] = merged_max_bytes
        app = _build_app(_settings(**overrides), upstream)
        try:
            default_body = await _get_messages(app)
            merged_body = await _get_messages(app, merged=True)
            return default_body, merged_body, calls
        finally:
            app.state.transforms.shutdown()

    async def test_degrade_full_fetch_error(self, upstream_factory,
                                            monkeypatch):
        state = {"full": httpx.Response(500, text="boom")}
        default_body, merged_body, calls = await self._run_merged(
            upstream_factory, monkeypatch, state)
        assert calls == []
        assert (_fingerprint_of(merged_body[0])
                == _fingerprint_of(default_body[0]))

    async def test_degrade_budget_exhausted(self, upstream_factory,
                                            monkeypatch):
        state = {"full": orjson.dumps(FULL_MSG_V1)}
        # merged_max_bytes=1 → every full body exceeds the budget → skipped
        default_body, merged_body, calls = await self._run_merged(
            upstream_factory, monkeypatch, state, merged_max_bytes=1)
        assert calls == []
        assert (_fingerprint_of(merged_body[0])
                == _fingerprint_of(default_body[0]))

    async def test_degrade_bad_json_full(self, upstream_factory, monkeypatch):
        state = {"full": b"not-json{"}
        default_body, merged_body, calls = await self._run_merged(
            upstream_factory, monkeypatch, state)
        assert calls == []
        assert (_fingerprint_of(merged_body[0])
                == _fingerprint_of(default_body[0]))

    async def test_degrade_full_not_dict(self, upstream_factory, monkeypatch):
        state = {"full": orjson.dumps([1, 2])}
        default_body, merged_body, calls = await self._run_merged(
            upstream_factory, monkeypatch, state)
        assert calls == []
        assert (_fingerprint_of(merged_body[0])
                == _fingerprint_of(default_body[0]))

    async def test_degrade_full_parts_not_list(self, upstream_factory,
                                               monkeypatch):
        state = {"full": orjson.dumps({"info": FULL_MSG_V1["info"],
                                       "parts": "nope"})}
        default_body, merged_body, calls = await self._run_merged(
            upstream_factory, monkeypatch, state)
        assert calls == []
        assert (_fingerprint_of(merged_body[0])
                == _fingerprint_of(default_body[0]))


class TestDigestIndependence:
    """B3-C3 — fingerprint is a pure function of content: no digest /
    out-of-order digest / duplicate digest leave it unchanged."""

    async def test_digest_events_do_not_affect_fingerprint(
        self, upstream_factory,
    ):
        upstream = upstream_factory(_messages_handler())
        app = _build_app(_settings(), upstream)
        hub = GlobalHub(client=None)
        app.state.hubs._global = hub  # wire a live hub onto this app
        try:
            baseline = [_fingerprint_of(m)
                        for m in await _get_messages(app)]

            def _status(sid: str, ts: int) -> dict:
                return {
                    "directory": "/proj",
                    "payload": {
                        "type": "session.status",
                        "properties": {
                            "sessionID": sid, "status": "busy",
                            "updatedAt": ts,
                        },
                    },
                }

            # out-of-order: later timestamp first, then an EARLIER one
            hub.publish(_status("s1", 5000))
            hub.publish(_status("s1", 4000))
            after_disorder = [_fingerprint_of(m)
                              for m in await _get_messages(app)]
            assert after_disorder == baseline

            # duplicate: same frame twice
            frame = _status("s1", 5000)
            hub.publish(frame)
            hub.publish(frame)
            after_duplicate = [_fingerprint_of(m)
                               for m in await _get_messages(app)]
            assert after_duplicate == baseline
        finally:
            app.state.transforms.shutdown()


class TestDisabledSwitch:
    async def test_disabled_response_is_byte_for_byte_today(
        self, upstream_factory,
    ):
        upstream = upstream_factory(_messages_handler())
        app = _build_app(_settings(message_fingerprint_enabled=False),
                         upstream)
        try:
            async with _client(app) as client:
                r = await client.get("/slimapi/messages/s1", headers=HDRS)
            assert r.status_code == 200
            body = r.json()
            for m in body:
                assert "contentFingerprint" not in m
            # byte-for-byte: the body equals the plain projection dump
            assert r.content == orjson.dumps(
                skeleton_messages(orjson.loads(MSG_LIST_BODY)))
        finally:
            app.state.transforms.shutdown()

    async def test_disabled_merged_also_omits_field(self, upstream_factory):
        upstream = upstream_factory(_messages_handler())
        app = _build_app(_settings(message_fingerprint_enabled=False),
                         upstream)
        try:
            body = await _get_messages(app, merged=True)
            for m in body:
                assert "contentFingerprint" not in m
        finally:
            app.state.transforms.shutdown()

    def test_config_default_is_enabled(self):
        assert _settings().message_fingerprint_enabled is True
        assert _settings(
            message_fingerprint_enabled=False
        ).message_fingerprint_enabled is False

    def test_rep_version_links_fingerprint_switch(self):
        """B3 v1.2 — REP_VERSION embeds the fingerprint switch state, so a
        flip invalidates every ETag (no stale 304s across the flip)."""
        on = representation_version(_settings())
        off = representation_version(_settings(message_fingerprint_enabled=False))
        assert on != off
