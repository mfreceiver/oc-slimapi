"""Tests for the turn token fence contract (turn_registry + DigestFields +
GlobalHub stamp + proxy commit point).

Covers the 6 required scenarios from the implementation brief:

1. IncarnationStore persistence + fault tolerance.
2. TurnRegistry bump_turn monotonicity / scope independence / snapshot.
3. DigestFields.to_payload flat top-level emission (paired present/absent).
4. GlobalHub.publish ingest-time stamp (with/without scope).
5. proxy forward bump (header-gated; prompt/abort only).
6. V10: ingest snapshot freezes the value — a later bump does not change an
   already-stamped entry; a new ingest stamps the new value.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import (
    _extract_sid_from_path,
    _is_turn_bumping_path,
    install_proxy,
)
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub import Subscriber
from oc_slimapi.sse.hub_types import DigestFields
from oc_slimapi.turn_registry import IncarnationStore, TurnRegistry


# ── shared helpers (mirror tests/test_hub.py) ───────────────────────────────────


def make_global_event(
    directory: str,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    payload: dict = {"type": event_type, "properties": properties or {}}
    return {"directory": directory, "payload": payload}


def parse_event(raw: bytes) -> tuple[str | None, dict]:
    text = raw.decode()
    event_name: str | None = None
    data_lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return event_name, data


async def drain_queue(subscriber: Subscriber, timeout: float = 0.2) -> list[bytes]:
    frames: list[bytes] = []
    while True:
        try:
            item = await asyncio.wait_for(subscriber.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if item is None:
            continue
        frames.append(item)
    return frames


async def _close_hub(hub: GlobalHub) -> None:
    tasks = [
        task
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── 1. IncarnationStore ─────────────────────────────────────────────────────────


def test_incarnation_store_first_run_returns_1(tmp_path):
    """New directory → load_or_bump returns 1 (persisted_last=0 +1)."""
    store = IncarnationStore(state_dir=str(tmp_path))
    assert store.load_or_bump() == 1
    # File persisted with the new value.
    assert (tmp_path / "incarnation").read_text().strip() == "1"


def test_incarnation_store_second_run_increments(tmp_path):
    """A second load reads the persisted value and adds one."""
    store = IncarnationStore(state_dir=str(tmp_path))
    first = store.load_or_bump()
    assert first == 1
    # A fresh store reading the same file must observe +1.
    store2 = IncarnationStore(state_dir=str(tmp_path))
    assert store2.load_or_bump() == 2
    store3 = IncarnationStore(state_dir=str(tmp_path))
    assert store3.load_or_bump() == 3


def test_incarnation_store_corrupt_file_does_not_crash(tmp_path):
    """A corrupt (non-integer) incarnation file degrades to fallback, no raise."""
    (tmp_path / "incarnation").write_text("not-an-int\n", encoding="utf-8")
    store = IncarnationStore(state_dir=str(tmp_path))
    # Fallback = 1; never raises.
    assert store.load_or_bump() == 1


def test_incarnation_store_unwritable_dir_does_not_crash(tmp_path):
    """An unwritable state dir degrades gracefully (warn, return fallback)."""
    store = IncarnationStore(state_dir=str(tmp_path / "missing" / "deep"))
    # Even with mkdir it must not crash; returns a positive int.
    inc = store.load_or_bump()
    assert isinstance(inc, int)
    assert inc >= 1


# ── 2. TurnRegistry ─────────────────────────────────────────────────────────────


def test_bump_turn_is_monotonically_increasing():
    reg = TurnRegistry(incarnation=5)
    assert reg.bump_turn("fp1", "s1") == 1
    assert reg.bump_turn("fp1", "s1") == 2
    assert reg.bump_turn("fp1", "s1") == 3


def test_bump_turn_scopes_are_independent():
    reg = TurnRegistry(incarnation=5)
    reg.bump_turn("fp1", "s1")  # 1
    reg.bump_turn("fp1", "s1")  # 2
    reg.bump_turn("fp2", "s1")  # different fp → independent counter
    assert reg.snapshot("fp1", "s1") == (5, 2)
    assert reg.snapshot("fp2", "s1") == (5, 1)
    # Different sid under same fp is also independent.
    reg.bump_turn("fp1", "s2")
    assert reg.snapshot("fp1", "s1") == (5, 2)
    assert reg.snapshot("fp1", "s2") == (5, 1)


def test_register_scope_then_snapshot_resolves_from_sid():
    """snapshot(fp=None, sid) reverse-resolves the fp via register_scope."""
    reg = TurnRegistry(incarnation=9)
    # No scope registered yet → snapshot returns None (header-gated degrade).
    assert reg.snapshot(None, "s1") is None
    reg.register_scope("s1", "fpX")
    reg.bump_turn("fpX", "s1")
    reg.bump_turn("fpX", "s1")
    # Now the sid resolves to fpX and returns the stamped (inc, turn).
    assert reg.snapshot(None, "s1") == (9, 2)


def test_snapshot_unknown_scope_returns_none():
    reg = TurnRegistry(incarnation=1)
    assert reg.snapshot("unknown-fp", "unknown-sid") == (1, 0)
    assert reg.snapshot(None, "unknown-sid") is None


# ── 3. DigestFields.to_payload ──────────────────────────────────────────────────


def test_digest_fields_omits_turn_pair_when_both_none():
    payload = DigestFields().to_payload("s1")
    assert "turnIncarnation" not in payload
    assert "turn" not in payload


def test_digest_fields_emits_turn_pair_flat_top_level_when_set():
    fields = DigestFields(turn_incarnation=7, turn=3, status="busy")
    payload = fields.to_payload("s1")
    # CRITICAL wire-shape assertion: the fields are at the FLAT top level,
    # alongside sessionID/status — NOT nested in a sub-`properties` dict.
    assert payload["turnIncarnation"] == 7
    assert payload["turn"] == 3
    assert payload["sessionID"] == "s1"
    assert payload["status"] == "busy"
    # Flat sibling keys, not nested.
    assert "properties" not in payload


def test_digest_fields_omits_pair_when_either_is_none():
    # turn None → both omitted (paired presence).
    only_inc = DigestFields(turn_incarnation=7, turn=None)
    payload = only_inc.to_payload("s1")
    assert "turnIncarnation" not in payload
    assert "turn" not in payload
    # turnIncarnation None → both omitted.
    only_turn = DigestFields(turn_incarnation=None, turn=4)
    payload2 = only_turn.to_payload("s1")
    assert "turnIncarnation" not in payload2
    assert "turn" not in payload2


# ── 4. GlobalHub.publish ingest-time stamp ──────────────────────────────────────


async def test_publish_stamps_turn_when_scope_known():
    """session.status ingest stamps turn/inc onto the entry when scope is known."""
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=42)
        reg.register_scope("s1", "fp1")
        reg.bump_turn("fp1", "s1")  # turn = 1
        hub.set_turn_registry(reg)

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        hub.flush()

        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        data = digests[0]
        assert data["turnIncarnation"] == 42
        assert data["turn"] == 1
        assert data["status"] == "busy"
    finally:
        await _close_hub(hub)


async def test_publish_omits_turn_when_scope_unknown():
    """No scope registered → snapshot returns None → fields omitted (degrade)."""
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=42)
        hub.set_turn_registry(reg)
        # No register_scope for s2 → snapshot(None, "s2") is None.

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s2", "status": "idle",
        }))
        hub.flush()

        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        data = digests[0]
        assert "turnIncarnation" not in data
        assert "turn" not in data
        assert data["status"] == "idle"
    finally:
        await _close_hub(hub)


async def test_publish_omits_turn_when_no_registry_wired():
    """No TurnRegistry injected → no stamping at all (legacy behaviour)."""
    hub = GlobalHub(client=None)
    try:
        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        hub.flush()
        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        assert "turnIncarnation" not in digests[0]
        assert "turn" not in digests[0]
    finally:
        await _close_hub(hub)


# ── 5. proxy forward bump (header-gated) ────────────────────────────────────────


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=2, accepted_client_versions=(2, 2),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-turn-test")
    app.state.config = settings
    app.state.upstream = upstream
    register_error_handlers(app)
    install_proxy(app)
    return app


def _passthrough_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            yield b'{"ok":true}'

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body()),
            headers={"Content-Type": "application/json"},
        )

    return handler


async def test_proxy_bumps_turn_on_prompt_with_header(upstream_factory):
    """POST /session/{sid}/prompt with X-Ocdroid-Server-Group-Fp bumps turn."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=3)
    app.state.turn_registry = reg  # inject into state (lifespan does this)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/session/ses_abc/prompt",
            headers={"X-Ocdroid-Server-Group-Fp": "fp1"},
        )
    assert response.status_code == 200
    # Scope registered + turn bumped.
    assert reg.snapshot("fp1", "ses_abc") == (3, 1)
    # A second prompt bumps again (monotonic).
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/session/ses_abc/prompt",
            headers={"X-Ocdroid-Server-Group-Fp": "fp1"},
        )
    assert reg.snapshot("fp1", "ses_abc") == (3, 2)


async def test_proxy_bumps_turn_on_abort_with_header(upstream_factory):
    """POST /session/{sid}/abort also bumps (contract §4.1 two forwards)."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=1)
    app.state.turn_registry = reg

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/session/ses_abc/abort",
            headers={"X-Ocdroid-Server-Group-Fp": "fp1"},
        )
    assert response.status_code == 200
    assert reg.snapshot("fp1", "ses_abc") == (1, 1)


async def test_proxy_does_not_bump_without_header(upstream_factory):
    """No X-Ocdroid-Server-Group-Fp header → no bump, no scope (header-gated)."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=1)
    app.state.turn_registry = reg

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/session/ses_abc/prompt")
    assert response.status_code == 200
    # No scope registered for ses_abc → snapshot from sid returns None.
    assert reg.snapshot(None, "ses_abc") is None
    assert reg._turns == {}


async def test_proxy_registers_scope_for_non_bumping_session_request(upstream_factory):
    """A scoped GET /session/{sid}/message registers scope but does NOT bump."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    reg = TurnRegistry(incarnation=1)
    app.state.turn_registry = reg

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/session/ses_abc/message",
            headers={"X-Ocdroid-Server-Group-Fp": "fp1"},
        )
    assert response.status_code == 200
    # Scope registered (so a later digest stamp can resolve), turn NOT bumped.
    assert reg.snapshot("fp1", "ses_abc") == (1, 0)


async def test_proxy_no_turn_registry_in_state_still_works(upstream_factory):
    """Absence of app.state.turn_registry (getattr default None) → no-op, no crash."""
    upstream = upstream_factory(_passthrough_handler())
    app = _build_app(_settings(), upstream)
    # Deliberately do NOT set app.state.turn_registry.

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/session/ses_abc/prompt",
            headers={"X-Ocdroid-Server-Group-Fp": "fp1"},
        )
    assert response.status_code == 200


# ── path helpers ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected_sid", [
    ("/session/ses_abc/prompt", "ses_abc"),
    ("/session/ses_abc/abort", "ses_abc"),
    ("/session/01HQXXXX/message", "01HQXXXX"),
    ("/session/ses_abc", "ses_abc"),
    ("/session", None),
    ("/global/event", None),
    ("/slimapi/health", None),
])
def test_extract_sid_from_path(path, expected_sid):
    assert _extract_sid_from_path(path) == expected_sid


@pytest.mark.parametrize("path,expected", [
    ("/session/ses_abc/prompt", True),
    ("/session/ses_abc/abort", True),
    ("/session/ses_abc/prompt/", True),   # trailing slash tolerant
    ("/session/ses_abc/abort/", True),
    ("/session/ses_abc/message", False),
    ("/session/ses_abc", False),
    ("/session/ses_abc/prompt/sub", False),
    ("/global/event", False),
])
def test_is_turn_bumping_path(path, expected):
    assert _is_turn_bumping_path(path) is expected


# ── 6. V10: ingest snapshot freezes the value ───────────────────────────────────


async def test_v10_ingest_snapshot_freezes_value_against_later_bump():
    """A bump AFTER ingest must NOT change an already-stamped entry's value.

    Contract §7.4 / V10: stamp happens at ingest (publish), reading the
    *current* turn int. The entry stores a Python int (value copy), so a
    later bump cannot retroactively mutate it. A subsequent ingest stamps
    the new (higher) value.
    """
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=5)
        reg.register_scope("s1", "fp1")
        hub.set_turn_registry(reg)

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)

        # Bump to turn=3, then ingest a session.status → entry frozen at 3.
        for _ in range(3):
            reg.bump_turn("fp1", "s1")
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        entry = hub.pending["s1"]
        assert entry.turn_incarnation == 5
        assert entry.turn == 3  # frozen

        # Now bump to turn=4 AFTER the stamp. The pending entry must NOT
        # change (no reference held — it's a copied int).
        reg.bump_turn("fp1", "s1")
        assert reg.snapshot("fp1", "s1") == (5, 4)  # registry moved on
        assert entry.turn == 3  # still frozen at the ingest-time value

        # Flush the first window — the emitted digest carries the frozen 3.
        hub.flush()
        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        assert digests[0]["turn"] == 3
        assert digests[0]["turnIncarnation"] == 5

        # A NEW session.status ingest stamps the now-current turn=4.
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "idle",
        }))
        new_entry = hub.pending["s1"]
        assert new_entry.turn == 4
    finally:
        await _close_hub(hub)


async def test_v10_busy_flush_carries_frozen_stamp():
    """The G1 busy-clears-sticky flush_sid path also carries the frozen stamp.

    The stamp runs BEFORE the flush_sid() call in publish(), so the
    immediate flush emits the stamped turn/inc. Regression guard for the
    ordering of the stamp block relative to the busy flush.
    """
    hub = GlobalHub(client=None)
    try:
        reg = TurnRegistry(incarnation=8)
        reg.register_scope("s1", "fp1")
        reg.bump_turn("fp1", "s1")  # turn=1
        hub.set_turn_registry(reg)

        subscriber = Subscriber()
        hub.subscribers.add(subscriber)
        # Establish a sticky lastError first so the busy path clears it.
        hub.sticky_last_error["s1"] = {
            "name": "err", "message": "boom", "at": 1,
        }
        hub.publish(make_global_event("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        # busy → flush_sid immediate (no debounce wait).
        frames = await drain_queue(subscriber)
        digests = [
            data for event, data in (parse_event(f) for f in frames)
            if event == "session.digest"
        ]
        assert len(digests) == 1
        assert digests[0]["turnIncarnation"] == 8
        assert digests[0]["turn"] == 1
        # busy clear sets lastError to explicit null.
        assert digests[0]["lastError"] is None
    finally:
        await _close_hub(hub)
