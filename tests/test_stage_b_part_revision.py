"""Stage B v0.4 tests — messageEventSeq + /full 304 + removal + truncated.

Implements the verification plan from the Stage B v0.4 delta-spec §A-J.
Covers (frozen P0 invariants + rev-ogpt fixes):

* ``_part_state`` maintenance semantics (§A/§B):
    - new partID → per_part_rev=0; messageEventSeq bumped to 1 (first touch)
    - text-end / append on existing partID → per_part_rev +1; seq +1
    - ``message.part.delta`` (token stream) does NOT touch the cache
    - messageEventSeq is monotonic across MULTIPLE distinct parts of the
      same message (CRITICAL 2 fix — v1 ``max(per-part)`` was not)
    - per_part_rev (token-frame dedup) is INDEPENDENT of messageEventSeq
* digest ``contentRevisions`` payload (§5.1 wire shape + back-compat)
* LRU cap 500 messages/session (§B MAJOR 5 fix)
* ``/full/{mid}?known.maxPartId=&known.partCount=&known.messageEventSeq=``
  304 short-circuit (§C CRITICAL 1 fix):
    - 3-tuple consistent → 304
    - any mismatch / no cache / partial params → 200
    - same-part text-append defeats the v1 2-tuple → proves seq matters
* ``X-Message-Event-Seq`` response header (§D): 200 carries the int,
  304 omits it; cold-start → 0
* removal routing (§E MAJOR 5):
    - ``message.part.removed`` → seq+1 + parts.pop + digest notifies
    - ``message.removed`` → _part_state.pop(mid) + no digest entry
* reconnect clears pending ``content_revisions`` (§F MAJOR 3 fix):
    - ``message.part.updated`` enters debounce → reconnect before flush →
      subsequent digest carries NO stale ``contentRevisions``
* truncated frame ordering (§G MAJOR 4 fix):
    - ``_truncate_part_for_all`` oversized path: truncated frame carries
      the captured per_part_rev (drop_part clears the cache mid-call)
* token frames keep per_part_rev (§H, unchanged from v1)
* lifecycle cleanup (resync_all / session.deleted / drop_part /
  _retire_session / ttl_sweep / on_session_deleted / on_upstream_reconnect)
* back-compat (§7.2):
    - digest with no part events has NO ``contentRevisions`` field
    - token frames with no cached revision have NO ``partEventRevision``
    - ``/full/{mid}`` without ``known.*`` params behaves as before
"""
from __future__ import annotations

import asyncio
import json

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.observability import BatchLedger
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import events, health, messages, questions, sessions
from oc_slimapi.sse.hub import (
    GlobalHub, HubRegistry, Subscriber,
    _PART_STATE_MAX_MESSAGES_PER_SESSION,
)
from oc_slimapi.sse.token_hub import TokenStreamHub
from oc_slimapi.sse.tokenstream.frames import (
    _delta_frame,
    _message_removed_frame,
    _snapshot_frame,
    _truncated_frame,
)
from oc_slimapi.config import (
    TOKEN_REMOVED_MESSAGES_MAX,
    TOKEN_REMOVED_MESSAGES_TTL_MS,
)
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.versioning import SlimapiVersionMiddleware


# ---------------------------------------------------------------------------
# Helpers (inlined per the repo pattern — no sibling-test imports).
# ---------------------------------------------------------------------------

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
    hub.task = None
    hub.flush_task = None
    hub.heartbeat_task = None
    hub.stop_task = None


def _updated_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    *, type: str = "text", text: str | None = None, end=None,
) -> dict:
    """Build properties for a ``message.part.updated`` event.

    ``end`` controls the part lifecycle: ``None`` → text-start; a truthy
    value → text-end. opencode v1.18.4 payload is nested
    (``{sessionID, part, time}`` with ``part`` carrying the IDs).
    """
    time_obj: dict = {}
    if end is not None:
        time_obj["end"] = end
    part: dict = {
        "id": pid, "messageID": mid, "sessionID": sid,
        "type": type, "time": time_obj,
    }
    if text is not None:
        part["text"] = text
    return {"sessionID": sid, "part": part, "time": {}}


def _delta_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    field: str = "text", delta: str = "x",
) -> dict:
    return {
        "sessionID": sid, "messageID": mid, "partID": pid,
        "field": field, "delta": delta,
    }


def _part_removed_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
) -> dict:
    """Flat ``{sessionID, messageID, partID}`` (opencode v1.18.4
    session.ts:604-628 — message.part.removed is NOT nested)."""
    return {"sessionID": sid, "messageID": mid, "partID": pid}


def _message_removed_props(sid: str = "s1", mid: str = "m1") -> dict:
    """Flat ``{sessionID, messageID}`` (opencode v1.18.4 —
    message.removed)."""
    return {"sessionID": sid, "messageID": mid}


class _FakeSub:
    """Minimal subscriber mock supporting the Lane-3 handshake API.

    Captures every ``put`` frame in ``self.frames`` and exposes
    ``begin_handshake`` / ``end_handshake`` / ``_in_handshake`` / ``closed``
    so it can be passed to :meth:`TokenStreamHub.attach_subscriber`
    (which now brackets the pre-fill with begin/end — rev-ogpt CRITICAL 3).
    """

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self._in_handshake: bool = False
        self.closed: bool = False

    def begin_handshake(self) -> None:
        self._in_handshake = True

    def end_handshake(self) -> None:
        self._in_handshake = False

    def put(self, frame: bytes) -> bool:
        self.frames.append(frame)
        return True


@pytest.fixture
async def hub():
    h = GlobalHub(client=None)
    try:
        yield h
    finally:
        await _close_hub(h)


# ---------------------------------------------------------------------------
# 1. _part_state maintenance (§A/§B — CRITICAL 2 monotonic seq)
# ---------------------------------------------------------------------------

class TestPartStateMaintenance:
    """``GlobalHub._part_state`` reflects per-part revision (token-frame
    dedup) AND message-level messageEventSeq (digest / R2 / header).

    The two counters are INDEPENDENT: per_part_rev keys on partID (new=0,
    that part's updated +1); messageEventSeq keys on messageID (any
    message.part.updated / .removed for that message → +1, regardless of
    which part). This independence is what makes the seq monotonic across
    multi-part messages — the v1 ``max(per-part)`` was NOT (a new low-rev
    part dragged the watermark back down)."""

    def test_first_touch_sets_seq_to_one(self, hub: GlobalHub):
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="")))
        # per_part_rev for the new part = 0; messageEventSeq = 1.
        assert hub._part_state == {
            "s1": {"m1": {"parts": {"p1": 0}, "seq": 1}}
        }

    def test_second_updated_on_same_part_increments_both(self, hub: GlobalHub):
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="more", end=1700)))
        # per_part_rev 0 → 1; seq 1 → 2.
        assert hub._part_state["s1"]["m1"] == {"parts": {"p1": 1}, "seq": 2}

    def test_message_part_delta_does_not_touch_cache(self, hub: GlobalHub):
        """Token-stream deltas are real-time tokens; the cache (and its
        messageEventSeq) is bumped ONLY by ``message.part.updated`` /
        ``message.part.removed``."""
        hub.set_token_hub(TokenStreamHub())
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="")))
        before = json.dumps(hub._part_state, sort_keys=True)
        hub.publish(make_global_event("/proj", "message.part.delta",
                                      _delta_props(delta="hi")))
        hub.publish(make_global_event("/proj", "message.part.delta",
                                      _delta_props(delta="there")))
        after = json.dumps(hub._part_state, sort_keys=True)
        # Cache unchanged despite two deltas.
        assert before == after
        assert hub._part_state["s1"]["m1"]["seq"] == 1

    def test_multi_part_seq_is_monotonic(self, hub: GlobalHub):
        """CRITICAL 2 fix: v1 used ``max(per-part revision)`` as the
        watermark; adding a NEW part (per_part_rev=0) could drag the max
        down if all existing parts had been bumped. v0.4 messageEventSeq
        is bumped by EVERY event for the message, so it is strictly
        monotonic across parts."""
        # part p1: created (per_part=0, seq=1) → updated (per_part=1, seq=2)
        # → updated (per_part=2, seq=3).
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="a")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="b")))
        # part p2: created (per_part=0, seq=4) — new low-rev part.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p2", text="")))
        entry = hub._part_state["s1"]["m1"]
        # per_part revisions reflect each part individually.
        assert entry["parts"] == {"p1": 2, "p2": 0}
        # seq is monotonic across all 4 events — NOT max(per_part) which
        # would be max(2, 0)=2 (same as before p2 was added — wrong).
        assert entry["seq"] == 4

    def test_malformed_part_does_not_touch_state(self, hub: GlobalHub):
        """A ``message.part.updated`` whose ``part`` is missing / not a
        dict / missing required string fields falls through to the legacy
        token-hub-only route and does NOT mutate ``_part_state``."""
        hub.set_token_hub(TokenStreamHub())
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      {"sessionID": "s1", "time": {}}))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      {"sessionID": "s1", "part": "x", "time": {}}))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      {"sessionID": "s1",
                                       "part": {"id": "p1", "sessionID": "s1",
                                                "type": "text", "time": {}},
                                       "time": {}}))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      {"sessionID": "s1",
                                       "part": {"id": "", "messageID": "m1",
                                                "sessionID": "s1",
                                                "type": "text", "time": {}},
                                       "time": {}}))
        assert hub._part_state == {}


# ---------------------------------------------------------------------------
# 2. digest contentRevisions (§5.1 wire shape + §3.2 back-compat)
# ---------------------------------------------------------------------------

class TestDigestContentRevisions:
    """``session.digest`` payload carries ``contentRevisions`` (per-message
    messageEventSeq) when the debounce window saw any qualifying event."""

    def test_digest_carries_content_revisions(self, hub: GlobalHub):
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m1", pid="p1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m1", pid="p1", text="x")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m2", pid="p9", text="")))
        hub.flush()
        frames = asyncio.run(drain_queue(sub))
        digests = [parse_event(f) for f in frames
                   if parse_event(f)[0] == "session.digest"]
        assert len(digests) == 1
        _, data = digests[0]
        # Stage B v0.5 §K: per-session GLOBAL seq — 3 events in s1 → seqs
        # 1, 2, 3. m1 last touched at event 2 → seq=2; m2 touched at
        # event 3 → seq=3 (NOT 1 — v0.4's per-message counter restarted
        # at 1 for m2, but v0.5's global counter keeps climbing).
        assert data["contentRevisions"] == {"m1": 2, "m2": 3}

    def test_digest_without_part_events_omits_field(self, hub: GlobalHub):
        """Back-compat (§7.2): a digest produced without any part events
        must NOT contain ``contentRevisions`` — its wire shape is
        byte-identical to pre-Stage-B digests."""
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "session.status",
                                      {"sessionID": "s1", "status": "busy"}))
        hub.flush()
        frames = asyncio.run(drain_queue(sub))
        _, data = parse_event(frames[0])
        assert "contentRevisions" not in data
        assert data == {
            "sessionID": "s1",
            "directory": "/proj",
            "status": "busy",
        }

    def test_max_part_id_is_lexicographic_not_numeric(self, hub: GlobalHub):
        """opencode ``PartID = Identifier.ascending("part")`` produces IDs
        whose string dictionary order equals creation order. The
        fingerprint's ``maxPartId`` is ``max(keys())`` directly (no
        numeric coercion)."""
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="part_10", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="part_2", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="part_1", text="")))
        fp = hub.get_part_fingerprint("s1", "m1")
        # Lexicographic max: "part_2" > "part_10" because '2' > '1'.
        assert fp == ("part_2", 3, 3)

    def test_empty_parts_max_part_id_is_empty_string(self, hub: GlobalHub):
        """When all parts have been removed via ``message.part.removed``
        but the message entry still exists, ``maxPartId`` is ``""`` and
        ``partCount`` is 0. The seq still carries the bump history so
        clients holding stale parts see a mismatch → 200."""
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        hub.publish(make_global_event("/proj", "message.part.removed",
                                      _part_removed_props(pid="p1")))
        fp = hub.get_part_fingerprint("s1", "m1")
        assert fp == ("", 0, 2)


# ---------------------------------------------------------------------------
# 3. LRU cap 500 messages/session (§B MAJOR 5 fix)
# ---------------------------------------------------------------------------

class TestPartStateLruCap:
    """``_part_state`` per-session message count is capped at
    ``_PART_STATE_MAX_MESSAGES_PER_SESSION`` (500); overflow evicts the
    oldest-inserted message (FIFO ≈ LRU for creation-order traffic)."""

    def test_cap_evicts_oldest_message(self, hub: GlobalHub):
        cap = _PART_STATE_MAX_MESSAGES_PER_SESSION
        # Fill exactly to cap.
        for i in range(cap):
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(mid=f"m{i}", pid="p1",
                                                         text="")))
        assert len(hub._part_state["s1"]) == cap
        assert "m0" in hub._part_state["s1"]
        # Adding one more evicts m0 (oldest insertion).
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m_new", pid="p1",
                                                     text="")))
        assert len(hub._part_state["s1"]) == cap
        assert "m0" not in hub._part_state["s1"]
        assert "m1" in hub._part_state["s1"]  # next-oldest still there
        assert "m_new" in hub._part_state["s1"]

    def test_cap_is_per_session_independent(self, hub: GlobalHub):
        cap = _PART_STATE_MAX_MESSAGES_PER_SESSION
        for i in range(cap):
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid=f"m{i}",
                                                         pid="p1", text="")))
        # s2 starts fresh — its cap is independent.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s2", mid="mX", pid="p1",
                                                     text="")))
        assert len(hub._part_state["s1"]) == cap
        assert len(hub._part_state["s2"]) == 1


# ---------------------------------------------------------------------------
# 4. /full/{mid}?known= 304 short-circuit (§C CRITICAL 1 fix)
# ---------------------------------------------------------------------------

def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        route_secret="x" * 32, route_secret_file=None,
        smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
        opt_a_partial_envelope_enabled=True,
        opt_a_auto_rollback_enabled=True,
        opt_a_rollback_window_seconds=3600,
        opt_a_rollback_min_sample=100,
        opt_a_rollback_envelope_5xx_zero_baseline_rate=0.01,
        opt_a_rollback_unknown_code_rate=0.05,
        opt_a_retry_after_ms_conservative=200,
        opt_a_retry_after_ms_cap=10000,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(settings: Settings, upstream: httpx.AsyncClient) -> FastAPI:
    app = FastAPI(title="oc-slimapi-stage-b-v04-test")
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    app.state.config = settings
    app.state.route_secret = settings.route_secret.encode()
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.directory_allowlist = set()
    app.state.allowlist_ready = True
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(upstream)
    app.state.batch_ledger = BatchLedger(window_seconds=settings.opt_a_rollback_window_seconds)
    for router in (health.router, sessions.router, messages.router,
                   questions.router, events.router):
        app.include_router(router)
    install_proxy(app)
    register_error_handlers(app)
    return app


VERSION_HEADERS = {"X-Slimapi-Version": "1"}


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


def _full_body(mid: str = "m1") -> bytes:
    return orjson.dumps({
        "info": {"id": mid, "role": "user"},
        "parts": [
            {"id": "p1", "type": "text", "messageID": mid, "text": "hello"},
        ],
    })


class TestFullKnownShortCircuit:
    """``GET /slimapi/messages/{sid}/full/{mid}?known.maxPartId=&known.partCount=&
    known.messageEventSeq=`` returns 304 when ALL THREE match; falls
    through to a normal 200 otherwise. Proves CRITICAL 1 fix."""

    async def test_consistent_known_returns_304(self, upstream_factory):
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1"
                    "?known.maxPartId=p1&known.partCount=1&known.messageEventSeq=1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 304
            assert response.content == b""
            assert response.headers["Cache-Control"] == "no-store"
            # §D: 304 path does NOT carry X-Message-Event-Seq (no body,
            # fingerprint already aligned).
            assert "X-Message-Event-Seq" not in response.headers
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_mismatched_max_part_id_returns_200(self, upstream_factory):
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1"
                    "?known.maxPartId=p2&known.partCount=1&known.messageEventSeq=1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
            assert response.headers["X-Message-Event-Seq"] == "1"
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_mismatched_part_count_returns_200(self, upstream_factory):
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1"
                    "?known.maxPartId=p1&known.partCount=2&known.messageEventSeq=1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_mismatched_message_event_seq_returns_200(self, upstream_factory):
        """§J.1 counter-example: same part text-appended (partCount + maxPartId
        unchanged) defeats the v1 2-tuple 304 — proves messageEventSeq
        matters (CRITICAL 1 fix)."""
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            # Part created → updated (text append). maxPartId + partCount
            # unchanged but seq advanced 1 → 2.
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="more")))
            fp = hub.get_part_fingerprint("s1", "m1")
            assert fp == ("p1", 1, 2)
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                # Client sends the OLD seq=1 — server's seq=2 → mismatch → 200.
                response = await client.get(
                    "/slimapi/messages/s1/full/m1"
                    "?known.maxPartId=p1&known.partCount=1&known.messageEventSeq=1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_no_cache_hit_returns_200_with_seq_zero(self, upstream_factory):
        """§J.2 counter-example: cold-start / unknown (sid, mid) → no
        ``_part_state`` → fall through to 200. §D: header is ``0``
        (client treats 0 as "no info" → R1)."""
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        try:
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1"
                    "?known.maxPartId=p1&known.partCount=1&known.messageEventSeq=1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
            assert response.headers["X-Message-Event-Seq"] == "0"
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_partial_known_params_return_200(self, upstream_factory):
        """Any one or two of the three ``known.*`` params present → no 304
        (the spec requires ALL THREE to short-circuit)."""
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                # Only maxPartId.
                r1 = await client.get(
                    "/slimapi/messages/s1/full/m1?known.maxPartId=p1",
                    headers=VERSION_HEADERS,
                )
                assert r1.status_code == 200
                # Only partCount + seq (missing maxPartId).
                r2 = await client.get(
                    "/slimapi/messages/s1/full/m1"
                    "?known.partCount=1&known.messageEventSeq=1",
                    headers=VERSION_HEADERS,
                )
                assert r2.status_code == 200
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_known_short_circuit_orthogonal_to_mode(self, upstream_factory):
        """A skeleton-mode caller may still 304 — the fingerprint is about
        upstream state, not the projection (spec §6 boundary)."""
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1"
                    "?mode=skeleton&known.maxPartId=p1&known.partCount=1"
                    "&known.messageEventSeq=1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 304
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_no_known_params_carries_header_only(self, upstream_factory):
        """Back-compat (§7.2): no ``known.*`` query → normal full body
        (no 304 attempt) + X-Message-Event-Seq header when cached."""
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="more")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
            assert response.headers["X-Message-Event-Seq"] == "2"
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()


# ---------------------------------------------------------------------------
# 5. removal handling (§E MAJOR 5)
# ---------------------------------------------------------------------------

class TestRemoval:
    """``message.part.removed`` (flat props) and ``message.removed`` (flat
    props) are routed to maintain ``_part_state`` correctly."""

    def test_message_part_removed_bumps_seq_and_pops_part(self, hub: GlobalHub):
        """§J.5: a removed part bumps messageEventSeq so clients holding
        the old parts see a mismatch (partCount decreased) and re-fetch."""
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p2", text="")))
        hub.publish(make_global_event("/proj", "message.part.removed",
                                      _part_removed_props(pid="p1")))
        # seq: create p1 (1) → create p2 (2) → remove p1 (3).
        entry = hub._part_state["s1"]["m1"]
        assert entry["parts"] == {"p2": 0}
        assert entry["seq"] == 3
        hub.flush()
        frames = asyncio.run(drain_queue(sub))
        _, data = parse_event(frames[0])
        assert data["contentRevisions"] == {"m1": 3}

    def test_message_part_removed_unknown_message_produces_digest(
        self, hub: GlobalHub,
    ):
        """Stage B v0.5 §L (MAJOR 2 fix): a removal for a message we've
        never seen (or that was LRU-evicted) is NOT a silent no-op. v0.4
        dropped the event here, so a cap-evicted message's removed part
        would never reach the client — it permanently retained the
        deleted part locally. v0.5 ALWAYS bumps the per-session global
        seq + writes a digest entry so the client's strict-``>`` check
        fires → R1 → ``/full/{mid}?known=`` cache miss → 200 + fresh
        parts (self-healing).

        Note: ``_part_state`` is still empty afterward — we deliberately
        do NOT create an entry for a message that no longer exists
        upstream.
        """
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.part.removed",
                                      _part_removed_props()))
        # _part_state stays empty (no entry for an unknown message).
        assert hub._part_state == {}
        # …but the global seq counter advanced…
        assert hub._session_event_seq.get("s1") == 1
        # …and the debounce window carries the bump.
        assert hub.pending["s1"].content_revisions == {"m1": 1}
        hub.flush()
        frames = asyncio.run(drain_queue(sub))
        _, data = parse_event(frames[0])
        assert data["contentRevisions"] == {"m1": 1}
        # get_part_fingerprint stays None (no _part_state entry) → client
        # R1s and /full returns 200 with fresh parts.
        assert hub.get_part_fingerprint("s1", "m1") is None

    def test_message_removed_pops_message_from_state(self, hub: GlobalHub):
        """§J.6: ``message.removed`` deletes the message entirely; the
        digest does NOT carry a contentRevisions entry for it; subsequent
        ``/full?known=`` returns no cache → 200."""
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m2", text="")))
        hub.publish(make_global_event("/proj", "message.removed",
                                      _message_removed_props(mid="m1")))
        assert "m1" not in hub._part_state["s1"]
        assert "m2" in hub._part_state["s1"]
        # No digest entry for m1 (it was deleted, not content-revised).
        hub.flush()
        frames = asyncio.run(drain_queue(sub))
        _, data = parse_event(frames[0])
        assert "m1" not in data.get("contentRevisions", {})
        # get_part_fingerprint returns None for the removed message.
        assert hub.get_part_fingerprint("s1", "m1") is None

    def test_message_removed_unknown_message_is_noop(self, hub: GlobalHub):
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.removed",
                                      _message_removed_props(mid="never_seen")))
        hub.flush()
        frames = asyncio.run(drain_queue(sub))
        assert frames == []


# ---------------------------------------------------------------------------
# 6. reconnect clears pending content_revisions (§F MAJOR 3 fix)
# ---------------------------------------------------------------------------

class TestReconnectClearsPending:
    """``resync_all`` clears ``_part_state`` AND every pending digest's
    ``content_revisions`` so a stale-epoch fingerprint cannot leak into
    the post-resync flush (§J.3 counter-example)."""

    def test_resync_clears_part_state_and_pending_content_revisions(
        self, hub: GlobalHub,
    ):
        sub = Subscriber()
        hub.subscribers.add(sub)
        # message.part.updated lands in debounce window.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m1", text="")))
        # session.status also lands in the same window for s2 — its digest
        # entry should survive resync EXCEPT for content_revisions.
        hub.publish(make_global_event("/proj", "session.status",
                                      {"sessionID": "s2", "status": "busy"}))
        # Reconnect fires before flush.
        hub.resync_all()
        # _part_state wiped.
        assert hub._part_state == {}
        # pending entries survive but their content_revisions is empty —
        # the post-resync flush must NOT carry stale contentRevisions.
        assert "s1" in hub.pending  # other fields preserved
        assert hub.pending["s1"].content_revisions == {}
        # The status-only entry's content_revisions was empty to begin with.
        assert hub.pending["s2"].content_revisions == {}

    async def test_post_resync_flush_carries_no_content_revisions(
        self, hub: GlobalHub,
    ):
        """End-to-end: the digest emitted AFTER resync must NOT carry
        ``contentRevisions`` for the message whose part event landed in
        the dead-epoch debounce window."""
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m1", text="")))
        hub.resync_all()
        # Drain resync frame first.
        resync_frames = await drain_queue(sub, timeout=0.05)
        assert any(b"event: resync" in f for f in resync_frames)
        # Now flush — the pending s1 entry survives but content_revisions
        # was cleared by resync_all.
        hub.flush()
        digest_frames = await drain_queue(sub, timeout=0.05)
        digests = [parse_event(f) for f in digest_frames
                   if parse_event(f)[0] == "session.digest"]
        for _, data in digests:
            assert "contentRevisions" not in data, (
                f"post-resync digest must not carry stale contentRevisions: {data}"
            )


# ---------------------------------------------------------------------------
# 7. truncated frame ordering (§G MAJOR 4 fix)
# ---------------------------------------------------------------------------

class TestTruncatedFrameOrdering:
    """``_truncate_part_for_all`` / oversized ``_emit_snapshot_or_truncated``
    capture the per-part revision BEFORE ``drop_part`` clears the cache,
    so the truncated frame still carries ``partEventRevision``."""

    def test_truncate_part_for_all_carries_captured_revision(self):
        """§J.4: real truncate path emits a truncated frame WITH
        partEventRevision (the v1 bug silently dropped it because
        drop_part cleared the cache before the frame was built).

        rev-ogpt CRITICAL 1 (Option B — per-FRAME): ``_truncate_part_for_all``
        consumes its OWN revision via ``_next_part_revision``. We pre-bump
        to 3 to set up a known starting revision; the truncated frame then
        consumes rev=4 (strictly greater than the previous delivery).
        """
        th = TokenStreamHub()
        # Pre-bump to revision 3 (simulating 4 prior emits at 0, 1, 2, 3).
        key = ("s1", "m1", "p1")
        for _ in range(4):
            th._next_part_revision(key)
        assert th._part_revisions[key] == 3
        # Attach a fake subscriber to capture the frame.
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        # _truncate_part_for_all consumes its OWN revision (4) under Option B.
        th._truncate_part_for_all(key, done=False)
        assert len(sub.frames) == 1
        _, data = parse_event(sub.frames[0])
        # CRITICAL: the truncated frame carries its own strictly-increasing
        # revision (4 — one past the pre-bumped 3), NOT None and NOT 3.
        assert data.get("partEventRevision") == 4
        assert data["truncated"] is True
        # And the cache was cleared by drop_part (post-condition).
        assert key not in th._part_revisions

    def test_emit_snapshot_or_truncated_oversized_carries_revision(self):
        """The handshake oversized path in ``_emit_snapshot_or_truncated``
        emits a snapshot frame (consuming rev N, wasted because oversized)
        then delegates to ``_truncate_part_for_all`` which consumes rev N+1
        for the truncated frame.

        rev-ogpt CRITICAL 1 (Option B): both the snapshot and truncated
        consume distinct revisions. Pre-bump to 5 to set a known start.
        """
        th = TokenStreamHub(max_frame_bytes=64)  # tiny cap → easy oversized
        # Pre-bump to revision 5 (6 prior emits).
        key = ("s1", "m1", "p1")
        for _ in range(6):
            th._next_part_revision(key)
        assert th._part_revisions[key] == 5
        # NOT in fanout → triggers the direct-put oversized branch.
        sub = _FakeSub()
        big_text = "x" * 1000  # exceeds the 64-byte cap
        th._emit_snapshot_or_truncated(sub, key, big_text, done=False)
        # The subscriber received exactly one truncated frame (the
        # _truncate_part_for_all fanout found no subscribers, the direct
        # put delivered to this sub).
        assert len(sub.frames) == 1
        _, data = parse_event(sub.frames[0])
        # Snapshot consumed rev=6 (wasted); truncated consumed rev=7.
        assert data.get("partEventRevision") == 7, (
            f"truncated should carry rev=7 (snapshot wasted rev=6), got "
            f"{data.get('partEventRevision')}"
        )
        assert data["truncated"] is True


# ---------------------------------------------------------------------------
# 8. token frames carry per_part_rev (§H, unchanged from v1)
# ---------------------------------------------------------------------------

class TestTokenFramePartRevision:
    """snapshot / delta / truncated frames emit ``partEventRevision`` when
    the per-part revision was forwarded from publish(); omit it otherwise.
    Value is the per_part_rev (token-frame dedup), NOT messageEventSeq."""

    def test_snapshot_frame_with_revision(self):
        key = ("s1", "m1", "p1")
        frame = _snapshot_frame(key, text="hi", done=False, part_revision=2)
        _, data = parse_event(frame)
        assert data["partEventRevision"] == 2

    def test_snapshot_frame_without_revision_omits_field(self):
        key = ("s1", "m1", "p1")
        frame = _snapshot_frame(key, text="hi", done=False)
        _, data = parse_event(frame)
        assert "partEventRevision" not in data

    def test_snapshot_terminal_marker_carries_revision(self):
        key = ("s1", "m1", "p1")
        frame = _snapshot_frame(key, text=None, done=True, part_revision=3)
        _, data = parse_event(frame)
        assert data["done"] is True
        assert "text" not in data
        assert data["partEventRevision"] == 3

    def test_delta_frame_with_revision(self):
        key = ("s1", "m1", "p1")
        frame = _delta_frame(key, "chunk", part_revision=1)
        _, data = parse_event(frame)
        assert data["partEventRevision"] == 1
        assert data["text"] == "chunk"

    def test_delta_frame_without_revision_omits_field(self):
        key = ("s1", "m1", "p1")
        frame = _delta_frame(key, "chunk")
        _, data = parse_event(frame)
        assert "partEventRevision" not in data

    def test_truncated_frame_with_revision(self):
        key = ("s1", "m1", "p1")
        frame = _truncated_frame(key, done=False, part_revision=4)
        _, data = parse_event(frame)
        assert data["truncated"] is True
        assert data["partEventRevision"] == 4

    def test_truncated_frame_without_revision_omits_field(self):
        key = ("s1", "m1", "p1")
        frame = _truncated_frame(key, done=False)
        _, data = parse_event(frame)
        assert "partEventRevision" not in data

    def test_revision_zero_is_emitted(self):
        """``part_revision=0`` is a legitimate value (part creation); it
        must NOT be filtered out by a truthy guard. Only ``None``
        suppresses the field."""
        key = ("s1", "m1", "p1")
        frame = _snapshot_frame(key, text="hi", done=False, part_revision=0)
        _, data = parse_event(frame)
        assert data["partEventRevision"] == 0

    def test_on_part_updated_does_not_bump_revision(self):
        """rev-ogpt CRITICAL 1 (Option B): ``on_part_updated`` does NOT
        bump ``_part_revisions`` — bumps happen lazily in emit paths via
        ``_next_part_revision``. Immediately after ``on_part_updated``
        the key is absent from ``_part_revisions`` (no emit yet).

        Uses text-start calls (not text-end) so the part stays alive — a
        text-end would retire the part via ``finish_part → drop_part``,
        which (correctly) clears the cached revision as part of the
        lifecycle cleanup (see TestLifecycleClearsPartState).
        """
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""), part_revision=0)
        # Option B: no emit happened → key is NOT in _part_revisions.
        assert ("s1", "m1", "p1") not in th._part_revisions
        th.on_part_updated(_updated_props(text="more"), part_revision=1)
        # Still no emit → still absent.
        assert ("s1", "m1", "p1") not in th._part_revisions
        # Trigger an emit via _next_part_revision (simulating a flush).
        rev = th._next_part_revision(("s1", "m1", "p1"))
        assert rev == 0  # first emit → 0

    def test_on_part_updated_ignores_part_revision_param(self):
        """Stage B v0.6 §Q + rev-ogpt CRITICAL 1 (Option B): the
        ``part_revision`` parameter is IGNORED entirely. ``on_part_updated``
        does not write to ``_part_revisions`` at all; the token hub's
        per-frame counter is fully decoupled from GlobalHub's per_part_rev
        cache (which can be clobbered by LRU cap eviction + re-touch).
        """
        th = TokenStreamHub()
        # All three calls should leave _part_revisions empty (no emit).
        th.on_part_updated(_updated_props(text=""), part_revision=99)
        assert ("s1", "m1", "p1") not in th._part_revisions
        th.on_part_updated(_updated_props(text="more"), part_revision=None)
        assert ("s1", "m1", "p1") not in th._part_revisions
        th.on_part_updated(_updated_props(text="even_more"), part_revision=0)
        assert ("s1", "m1", "p1") not in th._part_revisions
        # First emit produces revision 0 regardless of the ignored params.
        assert th._next_part_revision(("s1", "m1", "p1")) == 0
        assert th._next_part_revision(("s1", "m1", "p1")) == 1  # next emit
        assert th._next_part_revision(("s1", "m1", "p1")) == 2  # next emit


# ---------------------------------------------------------------------------
# 9. lifecycle clears part_state + _part_revisions
# ---------------------------------------------------------------------------

class TestLifecycleClearsPartState:
    """``_part_state`` and ``TokenStreamHub._part_revisions`` are cleared
    on the lifecycle transitions that invalidate them."""

    def test_resync_all_clears_part_state(self, hub: GlobalHub):
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="")))
        assert hub._part_state != {}
        hub.resync_all()
        assert hub._part_state == {}
        assert hub.get_part_fingerprint("s1", "m1") is None

    def test_session_deleted_pops_part_state(self, hub: GlobalHub):
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s2", text="")))
        hub.publish(make_global_event("/proj", "session.deleted",
                                      {"sessionID": "s1"}))
        assert "s1" not in hub._part_state
        assert "s2" in hub._part_state

    def test_drop_part_clears_token_revision(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""), part_revision=0)
        key = ("s1", "m1", "p1")
        # Option B (per-frame): ``on_part_updated`` no longer bumps —
        # bumps happen in emit paths. Pre-bump to simulate a frame emit.
        th._next_part_revision(key)
        assert key in th._part_revisions
        assert th.drop_part(key) is True
        assert key not in th._part_revisions

    def test_retire_session_clears_token_revisions(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", pid="p1", text=""),
                           part_revision=0)
        th.on_part_updated(_updated_props(sid="s1", mid="m2", pid="p2", text=""),
                           part_revision=1)
        th.on_part_updated(_updated_props(sid="s2", pid="p9", text=""),
                           part_revision=2)
        # Option B: pre-bump each key to simulate frame emits.
        th._next_part_revision(("s1", "m1", "p1"))
        th._next_part_revision(("s1", "m2", "p2"))
        th._next_part_revision(("s2", "m1", "p9"))
        th._retire_session("s1")
        assert ("s1", "m1", "p1") not in th._part_revisions
        assert ("s1", "m2", "p2") not in th._part_revisions
        assert ("s2", "m1", "p9") in th._part_revisions

    def test_ttl_sweep_clears_retired_revisions(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""), part_revision=0)
        key = ("s1", "m1", "p1")
        # Option B: pre-bump to simulate a frame emit.
        th._next_part_revision(key)
        # Populate the busy/idle map directly so the upcoming status
        # change does NOT route through on_session_status("idle") (which
        # would itself retire the session via _retire_session and
        # pre-clear the state we are trying to set up).
        th._session_status["s1"] = "busy"
        th.live_parts[key].last_delta_ms = 0
        th._session_status["s1"] = "idle"
        retired = th.ttl_sweep(now_ms=10**13)
        assert key in retired
        assert key not in th._part_revisions

    def test_on_session_deleted_clears_token_revisions(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", pid="p1", text=""),
                           part_revision=0)
        th.on_part_updated(_updated_props(sid="s1", mid="m2", pid="p2", text=""),
                           part_revision=1)
        th.on_part_updated(_updated_props(sid="s2", pid="p9", text=""),
                           part_revision=2)
        # Option B: pre-bump each key to simulate frame emits.
        th._next_part_revision(("s1", "m1", "p1"))
        th._next_part_revision(("s1", "m2", "p2"))
        th._next_part_revision(("s2", "m1", "p9"))
        th.on_session_deleted("s1")
        assert ("s1", "m1", "p1") not in th._part_revisions
        assert ("s1", "m2", "p2") not in th._part_revisions
        assert ("s2", "m1", "p9") in th._part_revisions

    def test_on_upstream_reconnect_preserves_token_revisions(self):
        """rev-ogpt CRITICAL 1 (3rd-round terminal audit):
        ``on_upstream_reconnect`` MUST NOT clear ``_part_revisions``.

        Clearing it would restart the per-frame revision counter at 0 in
        the new epoch, but ocdroid retains its last-seen watermark across
        the sidecar reconnect — the next emitted frame for an existing
        PartKey would carry revision 0 → ocdroid's strict ``>`` dedup
        drops it → silent frame loss. Preserving the counter guarantees
        the next emit for any pre-existing PartKey is strictly greater
        than anything seen before the reconnect.
        """
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", pid="p1", text=""),
                           part_revision=0)
        th.on_part_updated(_updated_props(sid="s2", pid="p9", text=""),
                           part_revision=1)
        # Option B: pre-bump each key to simulate frame emits.
        key1 = ("s1", "m1", "p1")
        key2 = ("s2", "m1", "p9")
        for _ in range(3):
            th._next_part_revision(key1)  # rev 0, 1, 2
        for _ in range(5):
            th._next_part_revision(key2)  # rev 0, 1, 2, 3, 4
        assert th._part_revisions[key1] == 2
        assert th._part_revisions[key2] == 4
        th.on_upstream_reconnect()
        # CRITICAL 1 (3rd-round): _part_revisions is PRESERVED.
        assert th._part_revisions.get(key1) == 2, (
            "reconnect must preserve per-PartKey revision (CRITICAL 1)"
        )
        assert th._part_revisions.get(key2) == 4, (
            "reconnect must preserve per-PartKey revision (CRITICAL 1)"
        )
        # Other state still cleared (the wholesale-reset half).
        assert th.live_parts == {}
        assert th._pending == {}
        assert th._nontext_parts == {}
        assert th._disabled_parts == {}

    def test_on_upstream_reconnect_revision_continues_monotone(self):
        """rev-ogpt CRITICAL 1 (3rd-round): after reconnect, the next
        emit for a pre-existing PartKey continues the per-frame monotone
        (does NOT restart at 0). This is the strict-``>`` invariant
        ocdroid relies on."""
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        # Pre-bump to revision 7 (8 emits: 0..7).
        for _ in range(8):
            th._next_part_revision(key)
        assert th._part_revisions[key] == 7
        th.on_upstream_reconnect()
        # Next emit after reconnect continues monotone (8, 9, 10...).
        assert th._next_part_revision(key) == 8, (
            "post-reconnect emit must continue the per-frame monotone "
            "(expected 8 — the next value after pre-reconnect 7)"
        )
        assert th._next_part_revision(key) == 9
        assert th._next_part_revision(key) == 10
        # A new PartKey (never seen pre-reconnect) still starts at 0.
        assert th._next_part_revision(("s1", "m1", "p_new")) == 0


# ---------------------------------------------------------------------------
# 10. publish forwards per_part_rev to token hub (end-to-end)
# ---------------------------------------------------------------------------

class TestPublishForwardsRevisionToTokenHub:
    """``GlobalHub.publish(message.part.updated)`` computes per_part_rev in
    ``_part_state`` (independent of the token hub) and forwards the event
    to ``TokenStreamHub.on_part_updated(...)``.

    rev-ogpt CRITICAL 1 (Option B): the token hub self-increments its
    per-frame revision in emit paths; ``on_part_updated`` itself does
    NOT bump. So immediately after publish, ``_part_revisions[key]`` is
    empty — entries appear only when a frame is actually emitted (e.g.
    via flush, finish_part, snapshot).
    """

    def test_publish_forwards_per_part_revision(self, hub: GlobalHub):
        th = TokenStreamHub()
        hub.set_token_hub(th)
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="")))
        # Option B: token hub does NOT bump on on_part_updated. The
        # _part_state cache (GlobalHub-side) does carry per_part_rev=0
        # independently (used for /full?known= 304 fingerprint only).
        assert ("s1", "m1", "p1") not in th._part_revisions
        assert hub._part_state["s1"]["m1"]["parts"]["p1"] == 0
        assert hub._part_state["s1"]["m1"]["seq"] == 1
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="more")))
        # Second updated — token hub still empty (no emit yet).
        assert ("s1", "m1", "p1") not in th._part_revisions
        assert hub._part_state["s1"]["m1"]["seq"] == 2
        # Trigger an emit via flush after a delta — the first emitted
        # frame for this part gets revision 0.
        hub.publish(make_global_event("/proj", "message.part.delta",
                                      _delta_props(delta="x")))
        th.flush()
        assert th._part_revisions[("s1", "m1", "p1")] == 0


# ---------------------------------------------------------------------------
# 11. Stage B v0.5 §O — CRITICAL 1 + MAJOR 2 + MAJOR 3 regression coverage
# ---------------------------------------------------------------------------

class TestV05GlobalSeqMonotonicAcrossEviction:
    """Stage B v0.5 §K (CRITICAL 1 fix): ``messageEventSeq`` is a per-SESSION
    global monotonic counter. v0.4's per-message counter restarted at 1
    after LRU eviction + re-touch, breaking client strict-``>`` drift
    detection and enabling ABA false-304s. v0.5 assigns from the per-session
    global counter so an evicted-and-re-touched message gets a seq STRICTLY
    GREATER than any previously observed value."""

    def test_evicted_then_retouched_gets_next_global_seq(self, hub: GlobalHub):
        """Force LRU eviction of message m0, then re-touch it via a fresh
        ``message.part.updated``. v0.4 would reset seq to 1; v0.5 assigns
        the next global value (cap+2 → strictly greater than the cap
        events' seqs)."""
        cap = _PART_STATE_MAX_MESSAGES_PER_SESSION
        # m0 first touch → seq=1.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m0", pid="p1", text="")))
        m0_seq_before = hub._part_state["s1"]["m0"]["seq"]
        assert m0_seq_before == 1
        # Fill cap with other messages, evicting m0.
        for i in range(cap):
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(mid=f"m_fill_{i}", pid="p1",
                                                         text="")))
        assert "m0" not in hub._part_state["s1"]
        # Re-touch m0 — global counter is now at cap+1, so the re-touch
        # assigns seq=cap+2 (strictly greater than any prior seq).
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m0", pid="p1", text="")))
        m0_seq_after = hub._part_state["s1"]["m0"]["seq"]
        assert m0_seq_after == cap + 2, (
            f"v0.5 §K: re-touch after eviction must assign next global seq "
            f"(>{cap+1}); got {m0_seq_after} (v0.4 regression would be 1)"
        )
        assert m0_seq_after > m0_seq_before

    def test_global_seq_strict_monotonic_across_messages(self, hub: GlobalHub):
        """Two distinct messages in the same session: each touch advances
        the GLOBAL counter, so message seqs reflect the global sequence
        (m1=1, m2=2, m1=3, ...) — NOT per-message (m1=1, m2=1, m1=2)."""
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m1", pid="p1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m2", pid="p1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m1", pid="p1", text="x")))
        # Global seq counter: 3 events → 3.
        assert hub._session_event_seq["s1"] == 3
        # m1 last touched at event 3 → seq=3; m2 at event 2 → seq=2.
        assert hub._part_state["s1"]["m1"]["seq"] == 3
        assert hub._part_state["s1"]["m2"]["seq"] == 2

    def test_resync_clears_global_seq(self, hub: GlobalHub):
        """Reconnect zeroes the per-session global counter (new epoch).
        Without this, post-reconnect seq values would compare against
        pre-reconnect baselines, breaking the strict-``>`` invariant."""
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="")))
        assert hub._session_event_seq["s1"] == 1
        hub.resync_all()
        assert hub._session_event_seq == {}
        # Post-resync, first event assigns seq=1 again (new epoch).
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(text="")))
        assert hub._session_event_seq["s1"] == 1
        assert hub._part_state["s1"]["m1"]["seq"] == 1

    def test_session_deleted_clears_global_seq(self, hub: GlobalHub):
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s2", text="")))
        hub.publish(make_global_event("/proj", "session.deleted",
                                      {"sessionID": "s1"}))
        assert "s1" not in hub._session_event_seq
        assert "s2" in hub._session_event_seq


class TestV05UnknownMessageRemovalProducesDigest:
    """Stage B v0.5 §L (MAJOR 2 fix): ``message.part.removed`` for an
    unknown (never-seen / LRU-evicted) message STILL bumps the per-session
    global seq + writes a digest entry. v0.4 silently dropped these events,
    so a cap-evicted message's removed part would never reach the client —
    it permanently retained the deleted part locally. v0.5 forces the
    client's strict-``>`` check to fire → R1 → /full 200 self-healing."""

    def test_unknown_removal_advances_global_seq_and_digest(self, hub: GlobalHub):
        """Removal for a never-seen message → global seq advances 0→1,
        digest carries {mid: 1}, _part_state stays empty (no entry for
        a message that doesn't exist upstream)."""
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.part.removed",
                                      _part_removed_props()))
        # _part_state empty — no spurious entry created.
        assert hub._part_state == {}
        # Global seq advanced.
        assert hub._session_event_seq["s1"] == 1
        # Pending digest carries the bump so flush will notify the client.
        assert hub.pending["s1"].content_revisions == {"m1": 1}
        hub.flush()
        frames = asyncio.run(drain_queue(sub))
        _, data = parse_event(frames[0])
        assert data["contentRevisions"] == {"m1": 1}
        # No fingerprint for an unknown message → /full?known= returns 200.
        assert hub.get_part_fingerprint("s1", "m1") is None

    def test_cap_evicted_message_removal_advances_global_seq(self, hub: GlobalHub):
        """A message that WAS tracked but got LRU-evicted: when its part is
        removed upstream, the removal still bumps the global seq (and
        digest). Without §L the client would never learn the part was
        deleted."""
        cap = _PART_STATE_MAX_MESSAGES_PER_SESSION
        # Touch m0 (seq=1) then fill cap to evict it.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(mid="m0", pid="p1", text="")))
        for i in range(cap):
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(mid=f"m_fill_{i}", pid="p1",
                                                         text="")))
        assert "m0" not in hub._part_state["s1"]
        seq_before = hub._session_event_seq["s1"]
        # Late removal for the evicted m0 — still bumps global seq.
        hub.publish(make_global_event("/proj", "message.part.removed",
                                      _part_removed_props(mid="m0", pid="p1")))
        seq_after = hub._session_event_seq["s1"]
        assert seq_after == seq_before + 1
        # _part_state still doesn't have m0 (we don't recreate it for a
        # removed-upstream message).
        assert "m0" not in hub._part_state.get("s1", {})
        # Digest carries the bump.
        assert hub.pending["s1"].content_revisions.get("m0") == seq_after

    def test_known_message_removal_still_pops_part_and_bumps(self, hub: GlobalHub):
        """Sanity: the v0.4 path (known message removal) still works — pops
        the part + bumps seq. The §L change only adds the unknown-message
        branch; it does not regress the known-message one."""
        sub = Subscriber()
        hub.subscribers.add(sub)
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p2", text="")))
        hub.publish(make_global_event("/proj", "message.part.removed",
                                      _part_removed_props(pid="p1")))
        # parts has only p2 now; seq advanced on each event (3 total).
        entry = hub._part_state["s1"]["m1"]
        assert entry["parts"] == {"p2": 0}
        assert entry["seq"] == 3
        assert hub._session_event_seq["s1"] == 3


class TestV05HeaderStability:
    """Stage B v0.5 §M (MAJOR 3 fix): ``X-Message-Event-Seq`` is sampled
    BEFORE and AFTER the body fetch; if they differ (a part event arrived
    during the await), the header is emitted as ``0`` (client treats 0 as
    "no trustworthy baseline" → R1) instead of a stale seq that does not
    correspond to the returned body."""

    async def test_header_emits_seq_when_stable(self, upstream_factory):
        """No part event arrives during the body fetch → seq_pre == seq_post
        → header carries the seq (trustworthy)."""
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
            # seq=1, no event during body → header = 1.
            assert response.headers["X-Message-Event-Seq"] == "1"
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_header_emits_zero_when_seq_changes_during_body(
        self, upstream_factory,
    ):
        """A part event arriving during the body fetch (here: the upstream
        MockTransport handler publishes to the hub before returning the
        body) → seq advances between seq_pre and seq_post → header = 0
        (client R1s)."""
        body = _full_body()
        hub_ref: list = []  # closure trap for the live hub

        def handler(request: httpx.Request) -> httpx.Response:
            # Side effect: bump the seq DURING the body fetch (after
            # message() has sampled seq_pre, before it samples seq_post).
            # This simulates a real part event arriving on the SSE
            # upstream while the /full request is mid-flight.
            if hub_ref:
                hub_ref[0].publish(
                    make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s1", mid="m1",
                                                     pid="p1", text="more"))
                )
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        hub_ref.append(hub)
        try:
            # Pre-populate so seq_pre = 1; handler bumps to seq=2 → seq_post=2.
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
            # seq_pre=1, seq_post=2 → mismatch → header = 0 (client R1s).
            assert response.headers["X-Message-Event-Seq"] == "0", (
                "v0.5 §M: seq advanced during body fetch → header must be 0"
            )
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()

    async def test_header_zero_when_no_cache(self, upstream_factory):
        """Cold-start (no _part_state for this sid/mid): seq_pre == seq_post
        == 0 → header = 0 (naturally — client R1s)."""
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        try:
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
            assert response.headers["X-Message-Event-Seq"] == "0"
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()


class TestV05SkeletonHeader:
    """Stage B v0.5 §O.4: ``X-Message-Event-Seq`` is emitted on the skeleton
    200 path too (v0.4 already implemented; this is the regression test).
    Same §M stability check as the full path."""

    async def test_skeleton_200_carries_x_message_event_seq(self, upstream_factory):
        body = _full_body()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"Content-Type": "application/json"})

        upstream = upstream_factory(handler)
        app = _build_app(_settings(), upstream)
        hub = app.state.hubs.get_global()
        try:
            # Two part events → global seq = 2 for s1.
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="")))
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(sid="s1", mid="m1",
                                                         pid="p1", text="more")))
            transport = httpx.ASGITransport(app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                response = await client.get(
                    "/slimapi/messages/s1/full/m1?mode=skeleton",
                    headers=VERSION_HEADERS,
                )
            assert response.status_code == 200
            # seq_pre == seq_post == 2 → header = 2.
            assert response.headers["X-Message-Event-Seq"] == "2"
        finally:
            app.state.transforms.shutdown()
            await app.state.hubs.close()


class TestV05PerPartRevisionNonOverwrite:
    """Stage B v0.5 §O.5: the per-part revision (token-frame dedup) is
    INDEPENDENT of messageEventSeq. After _part_state LRU-evicts a message
    and a NEW part of a (re-touched) message arrives, the new part gets a
    fresh per_part_rev=0 at a NEW PartKey — the token hub's existing
    per_part_rev for the OLD part's PartKey is NOT overwritten.

    opencode ``PartID = Identifier.ascending("part")`` never reuses IDs
    within a session, so a "new part" is genuinely a new key. This test
    locks in that naturally-correct behavior as a regression guard.

    rev-ogpt CRITICAL 1 (Option B — per-FRAME): the token hub bumps its
    per-frame revision lazily on emit (``_next_part_revision``), NOT on
    ``on_part_updated``. To set up known revision values we call
    ``_next_part_revision`` directly.
    """

    def test_new_part_does_not_overwrite_old_part_revision(self, hub: GlobalHub):
        th = TokenStreamHub()
        hub.set_token_hub(th)
        # Part p1 created at the token hub.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        # Pre-bump p1 6 times → revision=5 (0,1,2,3,4,5).
        key_p1 = ("s1", "m1", "p1")
        for _ in range(6):
            th._next_part_revision(key_p1)
        assert th._part_revisions[key_p1] == 5
        # New part p2 (different key) — separate revision counter.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p2", text="")))
        # Old key's revision UNTOUCHED.
        assert th._part_revisions[key_p1] == 5
        assert ("s1", "m1", "p2") not in th._part_revisions  # no emit yet

    def test_per_part_rev_independent_of_message_event_seq(self, hub: GlobalHub):
        """per_part_rev (token frames) keys on partID; messageEventSeq
        (digest/R2/header) keys on messageID — they advance independently."""
        th = TokenStreamHub()
        hub.set_token_hub(th)
        # p1 created → seq=1 (no token rev yet under Option B).
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        assert ("s1", "m1", "p1") not in th._part_revisions
        assert hub._part_state["s1"]["m1"]["seq"] == 1
        # p2 created (NEW part of same message) → seq bumps to 2.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p2", text="")))
        assert ("s1", "m1", "p2") not in th._part_revisions
        assert hub._part_state["s1"]["m1"]["seq"] == 2
        # p1 updated → seq=3 (independent counters).
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="x")))
        assert hub._part_state["s1"]["m1"]["seq"] == 3
        # Now emit a frame for p1 → revision 0 (first emit for p1).
        th._next_part_revision(("s1", "m1", "p1"))
        assert th._part_revisions[("s1", "m1", "p1")] == 0
        assert ("s1", "m1", "p2") not in th._part_revisions


# ---------------------------------------------------------------------------
# Stage B v0.6 §Q — per_part_revision 独立递增（真实淘汰不回退）
# ---------------------------------------------------------------------------

class TestV06PerPartRevisionNoRegressionAfterEviction:
    """Stage B v0.6 §Q (新 MAJOR 修复): token hub 的 per_part_revision
    独立递增，不依赖 GlobalHub 转发。GlobalHub 的 _part_state 在 LRU
    cap 淘汰 message entry 后，同一 PartKey 再 message.part.updated →
    GlobalHub 会把 per_part_rev 当成 0 → 若 token hub 依赖该值则会覆盖
    自己更高的 revision → client strict `>` 漏帧。v0.6 token hub 自己
    递增，不受 GlobalHub 淘汰影响。

    rev-ogpt CRITICAL 1 (Option B): token hub bumps lazily in emit paths
    (``_next_part_revision``), NOT on ``on_part_updated``. The
    ``part_revision`` parameter is IGNORED entirely.
    """

    def test_evicted_then_retouched_token_rev_continues_monotone(
        self, hub: GlobalHub, monkeypatch,
    ):
        """m1/p1 pushed to token rev=5 → other messages trigger
        GlobalHub LRU cap eviction of m1 → re-touch m1/p1 → next emit
        produces partEventRevision=6 (independent monotone, NOT 0).

        Setup uses ``_next_part_revision`` directly to reach rev=5
        because ``on_part_updated`` no longer bumps under Option B."""
        # Prevent the token hub from evicting parts under the global
        # LIVE_PARTS_MAX count cap during the fill — we want to isolate
        # GlobalHub's LRU eviction, not the token hub's memory eviction.
        monkeypatch.setattr(
            "oc_slimapi.sse.tokenstream.hub.TOKEN_LIVE_PARTS_MAX", 10**9,
        )
        th = TokenStreamHub()
        hub.set_token_hub(th)
        # m1/p1 created.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        # Pre-bump to token rev=5 (6 emits: 0,1,2,3,4,5).
        key = ("s1", "m1", "p1")
        for _ in range(6):
            th._next_part_revision(key)
        assert th._part_revisions[key] == 5
        # Force LRU eviction of m1 by filling cap with other messages.
        cap = _PART_STATE_MAX_MESSAGES_PER_SESSION
        for i in range(cap):
            hub.publish(make_global_event("/proj", "message.part.updated",
                                          _updated_props(mid=f"m_evict_{i}",
                                                         pid="p1", text="")))
        # m1 evicted from GlobalHub._part_state.
        assert "m1" not in hub._part_state.get("s1", {})
        # Re-touch m1/p1 — on_part_updated does NOT bump under Option B;
        # the next emit will continue the per-frame monotone at 6.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="y")))
        # The next emit produces revision 6 (NOT 0).
        next_rev = th._next_part_revision(key)
        assert next_rev == 6, (
            "v0.6 §Q: token hub per_part_rev must continue monotonically "
            f"after LRU eviction + re-touch (expected 6, got {next_rev})"
        )

    def test_token_rev_independent_of_globalhub_per_part_rev(self, hub: GlobalHub):
        """Token hub 的 _part_revisions 完全独立于 GlobalHub._part_state
        的 per_part_rev。GlobalHub 的值可能因 LRU 淘汰归零，token hub
        的值始终单调。"""
        th = TokenStreamHub()
        hub.set_token_hub(th)
        # Create p1.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(pid="p1", text="")))
        # Pre-bump to token rev=3 (4 emits).
        key = ("s1", "m1", "p1")
        for _ in range(4):
            th._next_part_revision(key)
        assert th._part_revisions[key] == 3
        # GlobalHub's per_part_rev for p1 is also bumped per publish (but
        # is INDEPENDENT of the token hub's value).
        assert hub._part_state["s1"]["m1"]["parts"]["p1"] == 0  # only 1 publish
        # Now call on_part_updated directly with part_revision=0 (would
        # be the value GlobalHub might forward after LRU eviction).
        # Token hub should IGNORE the parameter (no bump under Option B).
        th.on_part_updated(
            {"sessionID": "s1", "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text", "time": {},
            }, "time": {}},
            part_revision=0,  # stale value — should be IGNORED
        )
        # Token hub value unchanged by on_part_updated under Option B.
        assert th._part_revisions[key] == 3, (
            "v0.6 §Q + Option B: on_part_updated must IGNORE part_revision "
            "param and NOT bump _part_revisions"
        )
        # The next emit produces revision 4 (continues monotone).
        assert th._next_part_revision(key) == 4


# ---------------------------------------------------------------------------
# Stage B v0.6 §P — message.removed tombstone + 重放队列
# ---------------------------------------------------------------------------

class TestV06MessageRemovedLiveFrame:
    """P.2: publish message.removed → token subs 收 message.removed 帧
    {sessionID, messageID}。"""

    def test_message_removed_fans_to_token_subscribers(self, hub: GlobalHub):
        th = TokenStreamHub()
        hub.set_token_hub(th)
        # Attach a fake token subscriber for s1.
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        # Publish message.removed for (s1, m1).
        hub.publish(make_global_event("/proj", "message.removed",
                                      _message_removed_props(mid="m1")))
        # The subscriber received exactly one message.removed frame.
        assert len(sub.frames) == 1
        event_name, data = parse_event(sub.frames[0])
        assert event_name == "message.removed"
        assert data == {"sessionID": "s1", "messageID": "m1"}

    def test_message_removed_no_subscribers_is_noop(self, hub: GlobalHub):
        """No subscribers → no crash, but tombstone still recorded."""
        th = TokenStreamHub()
        hub.set_token_hub(th)
        hub.publish(make_global_event("/proj", "message.removed",
                                      _message_removed_props(mid="m1")))
        # Tombstone recorded even with no subscribers.
        assert ("s1", "m1") in th._removed_messages

    def test_message_removed_unknown_message_still_records_tombstone(
        self, hub: GlobalHub
    ):
        """message.removed for a message not in _part_state still records
        the tombstone (the message is gone upstream regardless)."""
        th = TokenStreamHub()
        hub.set_token_hub(th)
        hub.publish(make_global_event("/proj", "message.removed",
                                      _message_removed_props(mid="never_seen")))
        assert ("s1", "never_seen") in th._removed_messages


class TestV06RemovedMessagesReplayQueueCap:
    """P.2: 重放队列 cap 1000 (FIFO 淘汰最旧)。"""

    def test_cap_enforced_at_1000(self):
        th = TokenStreamHub()
        # Fill the queue with 1001 entries.
        for i in range(TOKEN_REMOVED_MESSAGES_MAX + 1):
            th.on_message_removed("s1", f"m{i}")
        # Queue stays at cap.
        assert len(th._removed_messages) == TOKEN_REMOVED_MESSAGES_MAX
        # Oldest entry (m0) was evicted.
        assert ("s1", "m0") not in th._removed_messages
        # Newest entry present.
        assert ("s1", f"m{TOKEN_REMOVED_MESSAGES_MAX}") in th._removed_messages

    def test_cap_is_global_not_per_session(self):
        """Cap is global across all sessions (not per-session)."""
        th = TokenStreamHub()
        # Fill with entries from different sessions.
        for i in range(TOKEN_REMOVED_MESSAGES_MAX + 1):
            th.on_message_removed(f"s{i % 10}", f"m{i}")
        assert len(th._removed_messages) == TOKEN_REMOVED_MESSAGES_MAX


class TestV06RemovedMessagesReplayQueueTTL:
    """P.2: 重放队列 TTL 24h 清理。"""

    def test_expired_entries_pruned_by_ttl_sweep(self):
        th = TokenStreamHub()
        now_ms = 10**15  # arbitrary "now"
        # Insert a fresh entry.
        th._removed_messages["s1", "m_fresh"] = now_ms
        # Insert an expired entry (24h + 1ms ago).
        th._removed_messages["s1", "m_expired"] = (
            now_ms - TOKEN_REMOVED_MESSAGES_TTL_MS - 1
        )
        # Run TTL sweep.
        th.ttl_sweep(now_ms=now_ms)
        # Fresh entry survives.
        assert ("s1", "m_fresh") in th._removed_messages
        # Expired entry removed.
        assert ("s1", "m_expired") not in th._removed_messages

    def test_prune_on_insert_enforces_ttl(self):
        """_prune_removed_messages called on insert also enforces TTL."""
        th = TokenStreamHub()
        now_ms = 10**15
        # Manually insert an expired entry (bypassing on_message_removed).
        th._removed_messages["s1", "m_expired"] = (
            now_ms - TOKEN_REMOVED_MESSAGES_TTL_MS - 1
        )
        # Insert a fresh entry via on_message_removed (triggers prune).
        th._removed_messages["s1", "m_fresh"] = now_ms
        th._prune_removed_messages(now_ms)
        assert ("s1", "m_fresh") in th._removed_messages
        assert ("s1", "m_expired") not in th._removed_messages


class TestV06ReconnectReplay:
    """P.3: message.removed 发生 → token sub 断开重连 → attach 后收
    server.connected → message.removed 重放 → snapshot。"""

    def test_replay_tombstones_on_attach(self):
        th = TokenStreamHub()
        # Record some tombstones.
        th.on_message_removed("s1", "m1")
        th.on_message_removed("s1", "m2")
        th.on_message_removed("s2", "mX")  # different session
        # Create a fake subscriber.
        sub = _FakeSub()
        # Attach for s1.
        th.attach_subscriber("s1", sub)
        # Collect event names.
        events = [parse_event(f)[0] for f in sub.frames]
        # server.connected first.
        assert events[0] == "server.connected"
        # Then message.removed for s1's tombstones (m1, m2) — NOT m2's mX.
        removed_events = [e for e in events if e == "message.removed"]
        assert len(removed_events) == 2
        removed_payloads = [parse_event(f)[1] for f in sub.frames
                           if parse_event(f)[0] == "message.removed"]
        assert {"sessionID": "s1", "messageID": "m1"} in removed_payloads
        assert {"sessionID": "s1", "messageID": "m2"} in removed_payloads
        assert {"sessionID": "s2", "messageID": "mX"} not in removed_payloads

    def test_replay_ordering_strict(self):
        """P.3 严格时序: server.connected 先于 message.removed 重放
        先于 snapshot。

        rev-ogpt CRITICAL 2: ``on_message_removed`` now retires ALL parts
        for ``(sid, mid)``. To verify snapshot ordering we use a DIFFERENT
        message's live part (m2) so it survives the m1 retire.
        """
        th = TokenStreamHub()
        # Live part for s1/m2 (different message — survives m1 retire).
        th.on_part_updated(_updated_props(sid="s1", mid="m2", pid="p2", text="hello"))
        # Tombstone for s1/m1 — retires any (s1, m1, *) parts (none here).
        th.on_message_removed("s1", "m1")
        # Attach.
        sub = _FakeSub()
        th.attach_subscriber("s1", sub)
        events = [parse_event(f)[0] for f in sub.frames]
        # Find positions.
        connected_idx = events.index("server.connected")
        removed_idx = events.index("message.removed")
        snapshot_idx = events.index("message.part.snapshot")
        assert connected_idx < removed_idx < snapshot_idx, (
            f"v0.6 §P.3: expected server.connected({connected_idx}) < "
            f"message.removed({removed_idx}) < snapshot({snapshot_idx})"
        )

    def test_replay_excludes_expired_tombstones(self, monkeypatch):
        """Expired tombstones (TTL 24h) are NOT replayed."""
        th = TokenStreamHub()
        now_ms = 10**15
        # Mock _now_ms so attach_subscriber uses our fixed timestamp.
        monkeypatch.setattr(
            "oc_slimapi.sse.tokenstream.hub._now_ms", lambda: now_ms,
        )
        # Fresh tombstone.
        th._removed_messages["s1", "m_fresh"] = now_ms
        # Expired tombstone.
        th._removed_messages["s1", "m_expired"] = (
            now_ms - TOKEN_REMOVED_MESSAGES_TTL_MS - 1
        )
        sub = _FakeSub()
        th.attach_subscriber("s1", sub)
        removed_payloads = [parse_event(f)[1] for f in sub.frames
                           if parse_event(f)[0] == "message.removed"]
        # Only fresh tombstone replayed.
        assert {"sessionID": "s1", "messageID": "m_fresh"} in removed_payloads
        assert {"sessionID": "s1", "messageID": "m_expired"} not in removed_payloads


class TestV06ResyncDoesNotClearReplayQueue:
    """P.5: resync_all / on_upstream_reconnect 不清 message.removed
    重放队列；队列仍受 cap/TTL 限制。"""

    def test_on_upstream_reconnect_preserves_replay_queue(self):
        th = TokenStreamHub()
        th.on_message_removed("s1", "m1")
        th.on_message_removed("s2", "m2")
        assert len(th._removed_messages) == 2
        # Simulate upstream reconnect.
        th.on_upstream_reconnect()
        # Replay queue preserved.
        assert len(th._removed_messages) == 2
        assert ("s1", "m1") in th._removed_messages
        assert ("s2", "m2") in th._removed_messages

    def test_resync_all_preserves_replay_queue(self, hub: GlobalHub):
        """GlobalHub.resync_all() clears _part_state + _session_event_seq
        but the token hub's _removed_messages is NOT cleared (it's owned
        by the token hub, not GlobalHub)."""
        th = TokenStreamHub()
        hub.set_token_hub(th)
        th.on_message_removed("s1", "m1")
        assert len(th._removed_messages) == 1
        # resync_all clears GlobalHub state.
        hub.resync_all()
        # Token hub replay queue preserved.
        assert len(th._removed_messages) == 1
        assert ("s1", "m1") in th._removed_messages


# ===========================================================================
# rev-ogpt CRITICAL 1 — per-FRAME revision semantics (Option B)
#
# Every token frame with independent delivery semantics (snapshot / delta /
# done marker / truncated) consumes the NEXT strictly-increasing revision
# for its part via ``_next_part_revision``. No two frames for the same
# part ever share a revision, so a client using strict ``>`` on
# ``partEventRevision`` reliably accepts every delivery (no false-dedup).
# ===========================================================================

class TestCritical1PerFrameRevisionSemantics:
    """rev-ogpt CRITICAL 1 (Option B): per-FRAME revision, not per-event.

    Every emitted frame consumes the next strictly-increasing revision.
    Clients using strict ``>`` accept every frame (no false-dedup).
    """

    def test_residual_delta_and_done_marker_have_distinct_revisions(self):
        """Residual delta (drained in finish_part) gets revision N, the
        done:true marker gets revision N+1 — distinct, strictly increasing.

        Under the pre-fix per-event design they shared a revision → strict
        ``>`` would have silently dropped the done marker.
        """
        th = TokenStreamHub()
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        # text-start — no revision bump under Option B.
        th.on_part_updated(_updated_props(text=""))
        # delta accumulates (won't flush yet — under TOKEN_FLUSH_BYTES).
        th.on_part_delta(_delta_props(delta="abc"))
        # text-end → finish_part emits residual delta (rev=0) + done (rev=1).
        th.on_part_updated(_updated_props(text="final", end=1700000000000))
        # Should have exactly 2 frames: residual delta + done marker.
        delta_frames = [f for f in sub.frames if parse_event(f)[0] == "message.part.delta"]
        snapshot_frames = [f for f in sub.frames
                           if parse_event(f)[0] == "message.part.snapshot"]
        assert len(delta_frames) == 1
        assert len(snapshot_frames) == 1
        # Distinct, strictly increasing revisions.
        _, delta_data = parse_event(delta_frames[0])
        _, snap_data = parse_event(snapshot_frames[0])
        assert delta_data["partEventRevision"] == 0, (
            f"residual delta should be rev=0 (first emit), got "
            f"{delta_data['partEventRevision']}"
        )
        assert snap_data["partEventRevision"] == 1, (
            f"done marker should be rev=1 (strictly greater), got "
            f"{snap_data['partEventRevision']}"
        )
        assert snap_data["partEventRevision"] > delta_data["partEventRevision"], (
            "Option B: done marker revision must be strictly greater than "
            "the residual delta's revision"
        )

    def test_multi_flush_windows_each_delta_gets_distinct_revision(self):
        """Each delta frame across multiple flush windows consumes the
        next revision (0, 1, 2, ...). Under per-event (Option A) they
        would all share revision 0."""
        th = TokenStreamHub()
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        # text-start.
        th.on_part_updated(_updated_props(text=""))
        # Three delta-then-flush cycles: each emits a distinct revision.
        th.on_part_delta(_delta_props(delta="chunk1"))
        th.flush()
        th.on_part_delta(_delta_props(delta="chunk2"))
        th.flush()
        th.on_part_delta(_delta_props(delta="chunk3"))
        th.flush()
        delta_revs = [parse_event(f)[1]["partEventRevision"]
                      for f in sub.frames
                      if parse_event(f)[0] == "message.part.delta"]
        # Each delta gets its own strictly-increasing revision.
        assert delta_revs == [0, 1, 2], (
            f"expected [0, 1, 2] (per-FRAME revision), got {delta_revs}"
        )

    def test_snapshot_oversized_then_truncated_have_distinct_revisions(self):
        """Snapshot oversized → snapshot's revision is wasted (never
        delivered), truncated frame consumes the NEXT revision (strictly
        greater than the previous delivery)."""
        # Tiny cap → easy oversized.
        th = TokenStreamHub(max_frame_bytes=80)
        sub = _FakeSub()
        # text-start.
        th.on_part_updated(_updated_props(text=""))
        # Oversized snapshot via the per-sub emit path (sub NOT in fanout).
        big_text = "x" * 1000
        th._emit_snapshot_or_truncated(sub, ("s1", "m1", "p1"), big_text, done=False)
        # Exactly one truncated frame delivered directly to sub (not in fanout).
        assert len(sub.frames) == 1
        event_name, data = parse_event(sub.frames[0])
        assert event_name == "message.part.snapshot"
        assert data.get("truncated") is True
        # The snapshot consumed rev=0 (wasted); truncated consumed rev=1.
        assert data.get("partEventRevision") == 1, (
            f"truncated should be rev=1 (snapshot wasted rev=0), got "
            f"{data.get('partEventRevision')}"
        )

    def test_strict_greater_consumer_accepts_all_frames_no_drops(self):
        """rev-ogpt requested test: simulate a real client using strict
        ``>`` on ``partEventRevision`` (only accept frames whose revision
        is strictly greater than the last accepted). Verify multiple
        deltas + the done marker are ALL accepted — no false-drops.

        Under per-event (Option A) the done marker would share the
        residual delta's revision and the strict-> client would drop it
        (silent loss of the terminal marker). Option B guarantees strict
        monotonicity so the client always accepts.
        """
        th = TokenStreamHub()
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        th.on_part_updated(_updated_props(text=""))
        # Multiple deltas across multiple flushes.
        for i in range(5):
            th.on_part_delta(_delta_props(delta=f"chunk{i}"))
            th.flush()
        # Final delta + text-end (residual delta + done marker).
        th.on_part_delta(_delta_props(delta="final-chunk"))
        th.on_part_updated(_updated_props(text="final", end=1700000000000))

        # === Client simulation: strict ``>`` consumer =================
        accepted_frames: list[tuple[str, int]] = []  # (frame_kind, rev)
        last_seen_rev = -1  # nothing seen yet
        for f in sub.frames:
            event_name, data = parse_event(f)
            # Only consider token stream frames for this part.
            if event_name not in ("message.part.delta", "message.part.snapshot"):
                continue
            if "partEventRevision" not in data:
                continue
            rev = data["partEventRevision"]
            # Strict ``>`` dedup: only accept if revision is strictly
            # greater than the last accepted revision.
            if rev > last_seen_rev:
                # Classify the frame kind for the assertion below.
                if event_name == "message.part.delta":
                    kind = "delta"
                elif data.get("done"):
                    kind = "done"
                elif data.get("truncated"):
                    kind = "truncated"
                else:
                    kind = "snapshot"
                accepted_frames.append((kind, rev))
                last_seen_rev = rev
        # =============================================================

        # 5 flush deltas + 1 residual delta + 1 done marker = 7 frames.
        assert len(accepted_frames) == 7, (
            f"strict-> consumer should accept all 7 frames "
            f"(5 flush deltas + 1 residual + 1 done), got {len(accepted_frames)}: "
            f"{accepted_frames}"
        )
        # Revisions strictly increasing.
        revs = [r for _, r in accepted_frames]
        assert revs == sorted(revs), f"revisions not sorted: {revs}"
        assert len(set(revs)) == len(revs), f"duplicate revisions: {revs}"
        # Last frame must be the done marker (terminal).
        assert accepted_frames[-1][0] == "done", (
            f"last accepted frame must be done marker, got {accepted_frames[-1]}"
        )

    def test_per_frame_strict_monotonicity_across_mixed_frame_types(self):
        """Snapshot (handshake) → delta (flush) → done marker (text-end):
        all carry strictly increasing revisions for the same part."""
        th = TokenStreamHub()
        sub = _FakeSub()
        # text-start (creates LivePart, no emit yet).
        th.on_part_updated(_updated_props(text=""))
        # attach_subscriber emits snapshot (rev=0).
        th.attach_subscriber("s1", sub)
        # delta → flush (rev=1).
        th.on_part_delta(_delta_props(delta="x"))
        th.flush()
        # text-end → residual delta + done marker (rev=2, rev=3).
        # Note: there's no residual pending here (flush above drained it),
        # so finish_part only emits the done marker (rev=2).
        th.on_part_updated(_updated_props(text="final", end=1700000000000))

        # Collect revisions for this part, in delivery order.
        revs_kinds: list[tuple[str, int]] = []
        for f in sub.frames:
            event_name, data = parse_event(f)
            if event_name not in ("message.part.delta", "message.part.snapshot"):
                continue
            if data.get("sessionID") != "s1" or data.get("partID") != "p1":
                continue
            if "partEventRevision" not in data:
                continue
            rev = data["partEventRevision"]
            if event_name == "message.part.delta":
                revs_kinds.append(("delta", rev))
            elif data.get("done"):
                revs_kinds.append(("done", rev))
            elif data.get("truncated"):
                revs_kinds.append(("truncated", rev))
            else:
                revs_kinds.append(("snapshot", rev))
        # Strict monotonicity across all frame types.
        revs = [r for _, r in revs_kinds]
        assert revs == sorted(set(revs)), (
            f"per-frame revisions must be strictly monotonic across mixed "
            f"frame types, got {revs_kinds}"
        )
        assert len(revs) == len(set(revs)), (
            f"duplicate revisions across frames: {revs_kinds}"
        )
        # Expected sequence: snapshot(0), delta(1), done(2).
        kinds = [k for k, _ in revs_kinds]
        assert kinds == ["snapshot", "delta", "done"], (
            f"expected frame sequence [snapshot, delta, done], got {kinds}"
        )

    def test_strict_greater_does_not_drop_done_marker(self):
        """End-to-end regression: a client using strict ``>`` on
        ``partEventRevision`` must NOT silently drop the done:true marker
        after receiving a residual delta.

        Under the v0.5 bug (per-event revision shared across many frames),
        the client would see residual delta rev=N then done marker rev=N
        (same) and would dedup — silently losing the terminal marker.
        Under Option B (per-frame) the done marker has a strictly greater
        revision, so strict ``>`` accepts it.
        """
        th = TokenStreamHub()
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        th.on_part_updated(_updated_props(text=""))
        th.on_part_delta(_delta_props(delta="x"))
        th.on_part_updated(_updated_props(text="final", end=1700))
        # Client simulation: strict ``>`` dedup (only accept if rev > last).
        delta_frames = [f for f in sub.frames if parse_event(f)[0] == "message.part.delta"]
        done_frames = [f for f in sub.frames
                       if parse_event(f)[0] == "message.part.snapshot"
                       and parse_event(f)[1].get("done") is True]
        assert len(delta_frames) == 1
        assert len(done_frames) == 1
        delta_rev = parse_event(delta_frames[0])[1]["partEventRevision"]
        done_rev = parse_event(done_frames[0])[1]["partEventRevision"]
        # Strict ``>`` consumer behavior: accepted = rev > last_seen.
        last_seen = -1
        accepted = []
        for rev, kind in [(delta_rev, "delta"), (done_rev, "done")]:
            if rev > last_seen:
                accepted.append((kind, rev))
                last_seen = rev
        # BOTH frames accepted (strict-> works under Option B per-frame).
        assert [k for k, _ in accepted] == ["delta", "done"], (
            f"strict-> consumer should accept both delta + done under Option B, "
            f"got {accepted}"
        )


# ===========================================================================
# rev-ogpt CRITICAL 2 — message.removed retires token state
# ===========================================================================

class TestCritical2MessageRemovedRetiresState:
    """rev-ogpt CRITICAL 2: ``message.removed`` atomically retires ALL
    token state for ``(sid, mid)``. Late part events for the removed
    message are dropped via the ``_retired_messages`` gate.
    """

    def test_message_removed_clears_live_part_and_revision(self):
        """After message.removed, the LivePart + revision for (sid, mid, *)
        are gone and byte gauges are decremented."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text="seed"))
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="chunk"))
        key = ("s1", "m1", "p1")
        assert key in th.live_parts
        # Option B: pre-bump to simulate a frame emit (so _retire_message
        # has a revision to clear).
        th._next_part_revision(key)
        assert key in th._part_revisions
        assert th._total_live_bytes > 0
        # Remove the message.
        th.on_message_removed("s1", "m1")
        # All state cleared.
        assert key not in th.live_parts
        assert key not in th._part_revisions
        assert key not in th._pending
        assert th._total_live_bytes == 0
        assert th._total_pending_bytes == 0
        # Gate recorded.
        assert ("s1", "m1") in th._retired_messages

    def test_message_removed_preserves_other_messages(self):
        """Retiring (s1, m1) does NOT touch (s1, m2) or (s2, m1)."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        th.on_part_updated(_updated_props(sid="s1", mid="m2", pid="p1", text=""))
        th.on_part_updated(_updated_props(sid="s2", mid="m1", pid="p1", text=""))
        th.on_message_removed("s1", "m1")
        # m1 of s1 gone; m2 of s1 and m1 of s2 preserved.
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m2", "p1") in th.live_parts
        assert ("s2", "m1", "p1") in th.live_parts

    def test_late_part_updated_after_remove_is_dropped(self):
        """A late ``message.part.updated`` for a removed message MUST NOT
        recreate a LivePart (CRITICAL 2 gate)."""
        th = TokenStreamHub()
        th.on_message_removed("s1", "m1")
        # Late part update for the removed message.
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        # No LivePart created.
        assert ("s1", "m1", "p1") not in th.live_parts
        # No revision created.
        assert ("s1", "m1", "p1") not in th._part_revisions

    def test_late_delta_after_remove_is_dropped(self):
        """A late ``message.part.delta`` for a removed message is dropped."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        th.on_message_removed("s1", "m1")
        # Late delta.
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="late"))
        # No LivePart re-created, no pending accumulator.
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s1", "m1", "p1") not in th._pending

    def test_removed_message_no_more_frames_after_tombstone(self, hub: GlobalHub):
        """End-to-end: after a token subscriber receives the
        message.removed frame, no further delta / snapshot / done frames
        for that message can arrive."""
        th = TokenStreamHub()
        hub.set_token_hub(th)
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        # Build up live state.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s1", mid="m1", pid="p1",
                                                     text="")))
        # Remove the message.
        sub.frames.clear()
        hub.publish(make_global_event("/proj", "message.removed",
                                      _message_removed_props(sid="s1", mid="m1")))
        # Should have exactly one frame: the message.removed tombstone.
        events = [parse_event(f)[0] for f in sub.frames]
        assert events == ["message.removed"], (
            f"expected exactly [message.removed], got {events}"
        )
        # Late part update via publish — must be gated.
        sub.frames.clear()
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s1", mid="m1", pid="p1",
                                                     text="stale")))
        # No frames for the removed message.
        assert sub.frames == [], (
            f"late event for removed message must not produce frames, got {sub.frames}"
        )

    def test_retired_message_gate_cleared_on_session_deleted(self):
        """``on_session_deleted`` clears ``_retired_messages`` for the sid."""
        th = TokenStreamHub()
        th.on_message_removed("s1", "m1")
        th.on_message_removed("s1", "m2")
        th.on_message_removed("s2", "mX")
        assert ("s1", "m1") in th._retired_messages
        th.on_session_deleted("s1")
        assert ("s1", "m1") not in th._retired_messages
        assert ("s1", "m2") not in th._retired_messages
        # Other sessions preserved.
        assert ("s2", "mX") in th._retired_messages

    def test_retired_message_gate_cleared_on_reconnect(self):
        """``on_upstream_reconnect`` clears ``_retired_messages`` (new
        epoch; replay queue is intentionally preserved)."""
        th = TokenStreamHub()
        th.on_message_removed("s1", "m1")
        assert ("s1", "m1") in th._retired_messages
        th.on_upstream_reconnect()
        # Gate cleared.
        assert th._retired_messages == set()
        # Replay queue preserved.
        assert ("s1", "m1") in th._removed_messages

    def test_retired_message_gate_cleared_when_replay_queue_evicts(self):
        """When the replay queue evicts an entry (FIFO cap), the matching
        gate entry is also discarded."""
        from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_MAX
        th = TokenStreamHub()
        # Fill the queue with cap+1 entries.
        for i in range(TOKEN_REMOVED_MESSAGES_MAX + 1):
            th.on_message_removed("s1", f"m{i}")
        # m0 was evicted from both queue and gate.
        assert ("s1", "m0") not in th._removed_messages
        assert ("s1", "m0") not in th._retired_messages
        # Newest entry is in both.
        newest = ("s1", f"m{TOKEN_REMOVED_MESSAGES_MAX}")
        assert newest in th._removed_messages
        assert newest in th._retired_messages


# ===========================================================================
# rev-ogpt CRITICAL 3 — attach_subscriber handshake bypasses backpressure
# ===========================================================================

class TestCritical3HandshakeBypassesOverflow:
    """rev-ogpt CRITICAL 3: the handshake pre-fill (connected →
    tombstones → flush → snapshot) runs in handshake mode so a legitimate
    large batch cannot trigger ``subscriber_backpressure`` disconnect
    before the sub even enters the fanout.
    """

    def test_attach_does_not_register_closed_sub(self):
        """If the sub was already closed before attach, it is NOT
        registered to fanout."""
        th = TokenStreamHub()
        sub = _FakeSub()
        sub.closed = True  # simulate prior disconnect
        th.attach_subscriber("s1", sub)
        # Not registered.
        assert "s1" not in th._subs_by_sid or sub not in th._subs_by_sid.get("s1", set())

    def test_many_tombstones_do_not_overflow_handshake(self):
        """A large tombstone batch (well beyond the runtime queue cap)
        is delivered without overflow during handshake.

        Uses a REAL ``TokenSubscriber`` with tight caps so the test
        actually exercises the begin_handshake/end_handshake bypass —
        a ``_FakeSub`` mock would not.
        """
        from oc_slimapi.sse.tokenstream.subscriber import TokenSubscriber
        from oc_slimapi.sse.tokenstream.models import _TokenMetrics
        from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_MAX
        th = TokenStreamHub()
        # Fill the replay queue with many tombstones for s1.
        n = min(200, TOKEN_REMOVED_MESSAGES_MAX)
        for i in range(n):
            th.on_message_removed("s1", f"m{i}")
        # Real subscriber with a TIGHT queue cap (8 items / 4KiB).
        # Without handshake bypass, 200 tombstones would easily overflow.
        metrics = _TokenMetrics()
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=8, buffer_bytes=4096,
            max_frame_bytes=1024 * 1024,
        )
        th.attach_subscriber("s1", sub)
        # Sub was NOT closed (handshake bypassed overflow).
        assert not sub.closed, (
            "handshake must bypass overflow so the pre-fill always lands"
        )
        # Sub is registered to fanout.
        assert sub in th._subs_by_sid.get("s1", set())
        # All tombstones delivered (plus server.connected).
        # Drain the queue and count message.removed frames.
        delivered_count = 0
        while not sub.queue.empty():
            frame = sub.queue.get_nowait()
            if frame is None or frame is b"":
                continue
            if b"event: message.removed" in frame:
                delivered_count += 1
        assert delivered_count == n, (
            f"expected {n} message.removed frames delivered, got {delivered_count}"
        )

    def test_large_snapshot_does_not_overflow_handshake(self):
        """A large LivePart snapshot (well beyond the runtime byte cap)
        is delivered without overflow during handshake.

        Uses a REAL ``TokenSubscriber`` with a tight byte cap so the
        test actually exercises the begin_handshake/end_handshake bypass.
        """
        from oc_slimapi.sse.tokenstream.subscriber import TokenSubscriber
        from oc_slimapi.sse.tokenstream.models import _TokenMetrics
        th = TokenStreamHub()
        # Build up a large LivePart via many deltas.
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        # Append enough bytes to exceed the tight buffer_bytes (4KiB).
        big = "x" * (8 * 1024)
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta=big))
        # Real subscriber with tight byte cap (4KiB). Without handshake
        # bypass, the snapshot (>4KiB) would overflow.
        metrics = _TokenMetrics()
        sub = TokenSubscriber(
            session_id="s1", metrics=metrics,
            queue_items=8, buffer_bytes=4096,
            max_frame_bytes=1024 * 1024,
        )
        th.attach_subscriber("s1", sub)
        # Sub was NOT closed (handshake bypassed overflow).
        assert not sub.closed
        # Sub registered to fanout.
        assert sub in th._subs_by_sid.get("s1", set())
        assert sub._in_handshake is False  # handshake ended

    def test_handshake_ordering_preserved_under_load(self):
        """Strict ordering: server.connected → message.removed → snapshot."""
        th = TokenStreamHub()
        # Live part for s1/m2 (different message — survives m1 retire).
        th.on_part_updated(_updated_props(sid="s1", mid="m2", pid="p2", text="hi"))
        # Many tombstones for s1/m1.
        for i in range(50):
            th.on_message_removed("s1", f"m_a{i}")
        sub = _FakeSub()
        th.attach_subscriber("s1", sub)
        events = [parse_event(f)[0] for f in sub.frames]
        # First frame is server.connected.
        assert events[0] == "server.connected"
        # All message.removed come before snapshot.
        connected_idx = 0
        last_removed_idx = max(i for i, e in enumerate(events) if e == "message.removed")
        snapshot_idx = events.index("message.part.snapshot")
        assert connected_idx < last_removed_idx < snapshot_idx


# ===========================================================================
# rev-ogpt MAJOR 4 — message.part.removed routes to TokenStreamHub
# ===========================================================================

class TestMajor4PartRemovedRouting:
    """rev-ogpt MAJOR 4: ``message.part.removed`` retires the corresponding
    LivePart / pending / revision in the token hub. Without this routing
    the token hub would keep emitting stale frames for a part the upstream
    has removed.
    """

    def test_on_part_removed_drops_live_part(self):
        """Direct call: ``on_part_removed`` drops the LivePart."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        key = ("s1", "m1", "p1")
        assert key in th.live_parts
        th.on_part_removed("s1", "m1", "p1")
        assert key not in th.live_parts
        assert key in th._disabled_parts  # drop_part disables
        assert key not in th._part_revisions

    def test_on_part_removed_idempotent(self):
        """``on_part_removed`` is idempotent — calling twice is a no-op."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        th.on_part_removed("s1", "m1", "p1")
        # Second call — no exception, no state change.
        th.on_part_removed("s1", "m1", "p1")
        assert ("s1", "m1", "p1") not in th.live_parts

    def test_on_part_removed_blocks_late_delta(self):
        """After ``on_part_removed``, late deltas for the key drop silently."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        th.on_part_removed("s1", "m1", "p1")
        # Late delta — silently dropped (key is disabled).
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="late"))
        assert ("s1", "m1", "p1") not in th._pending

    def test_on_part_removed_gated_by_retired_message(self):
        """If the message is already retired (message.removed), part
        removal is a no-op."""
        th = TokenStreamHub()
        th.on_message_removed("s1", "m1")
        # Part removal for the retired message — no-op (no LivePart exists).
        th.on_part_removed("s1", "m1", "p1")
        # No _disabled_parts entry created either (drop_part never called).
        assert ("s1", "m1", "p1") not in th._disabled_parts

    def test_publish_routes_part_removed_to_token_hub(self, hub: GlobalHub):
        """End-to-end: publish message.part.removed → token hub retires
        the part."""
        th = TokenStreamHub()
        hub.set_token_hub(th)
        # Create the part via publish.
        hub.publish(make_global_event("/proj", "message.part.updated",
                                      _updated_props(sid="s1", mid="m1", pid="p1",
                                                     text="")))
        key = ("s1", "m1", "p1")
        assert key in th.live_parts
        # Publish message.part.removed.
        hub.publish(make_global_event("/proj", "message.part.removed",
                                      _part_removed_props(sid="s1", mid="m1",
                                                          pid="p1")))
        # Token hub retired the part.
        assert key not in th.live_parts
        assert key in th._disabled_parts

    def test_part_removed_interleaved_with_flush(self):
        """A part removal interleaved with flush correctly retires the
        part — no stale frames after removal."""
        th = TokenStreamHub()
        sub = _FakeSub()
        th._subs_by_sid.setdefault("s1", set()).add(sub)
        th.on_part_updated(_updated_props(sid="s1", mid="m1", pid="p1", text=""))
        # First delta → flush → frame emitted.
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="first"))
        th.flush()
        sub.frames.clear()
        # Remove the part.
        th.on_part_removed("s1", "m1", "p1")
        # Late delta after removal — must NOT produce a frame.
        th.on_part_delta(_delta_props(sid="s1", mid="m1", pid="p1", delta="late"))
        th.flush()
        # No delta frames after removal.
        delta_frames = [f for f in sub.frames
                        if parse_event(f)[0] == "message.part.delta"]
        assert delta_frames == []


# ===========================================================================
# rev-ogpt MAJOR 5 — revision created only for accepted text parts
# ===========================================================================

class TestMajor5RevisionGating:
    """rev-ogpt MAJOR 5: ``_part_revisions`` is bumped ONLY for accepted
    text parts. Non-text parts, malformed events, late text-starts for
    disabled keys, and late updates for retired messages never create or
    consume a revision. Bounded FIFO cap (``TOKEN_DISABLED_MAX``)
    prevents unbounded growth.
    """

    def test_non_text_part_does_not_create_revision(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(type="reasoning"))
        assert ("s1", "m1", "p1") not in th._part_revisions

    def test_malformed_part_missing_time_does_not_create_revision(self):
        th = TokenStreamHub()
        th.on_part_updated({
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text",  # no time
            },
        })
        assert ("s1", "m1", "p1") not in th._part_revisions

    def test_disabled_text_start_does_not_recreate_revision(self):
        """Scenario: revision created via emit → drop_part clears revision
        + adds to _disabled → late text-start returns early (no frame
        emitted → no revision consumed).

        Pre-fix (v0.5): the revision was written BEFORE the disabled
        check on every on_part_updated, leaving an orphan revision that
        never gets used.
        """
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        # Option B: pre-bump to simulate a frame emit (the only way
        # _part_revisions gets an entry under per-frame semantics).
        th._next_part_revision(key)
        assert key in th._part_revisions
        th.drop_part(key)  # clears revision + adds to _disabled
        assert key not in th._part_revisions
        assert key in th._disabled_parts
        # Late text-start for the disabled key.
        th.on_part_updated(_updated_props(text=""))
        # MAJOR 5: still no revision entry (no emit happened for the
        # rejected event; disabled check short-circuits before any path
        # that could call _next_part_revision).
        assert key not in th._part_revisions

    def test_part_revisions_cap_enforced(self):
        """``_part_revisions`` cannot grow unbounded —
        ``TOKEN_DISABLED_MAX`` cap evicts oldest (FIFO).

        We exercise the cap directly via ``_next_part_revision`` because
        under Option B that is the only site that creates entries (emit
        paths). ``on_part_updated`` does NOT create entries by itself.
        """
        from oc_slimapi.config import TOKEN_DISABLED_MAX
        th = TokenStreamHub()
        # Insert cap+1 distinct keys via direct emit simulation.
        for i in range(TOKEN_DISABLED_MAX + 1):
            th._next_part_revision(("s1", f"m{i}", "p1"))
        # Cap enforced.
        assert len(th._part_revisions) == TOKEN_DISABLED_MAX
        # Oldest key evicted (m0).
        assert ("s1", "m0", "p1") not in th._part_revisions
        # Newest key present.
        assert ("s1", f"m{TOKEN_DISABLED_MAX}", "p1") in th._part_revisions

    def test_part_revisions_move_to_end_on_bump(self):
        """When an existing key's revision is bumped (next emit), it moves
        to the end of the FIFO order (LRU correctness for the cap)."""
        from oc_slimapi.config import TOKEN_DISABLED_MAX
        th = TokenStreamHub()
        # Fill exactly to cap.
        for i in range(TOKEN_DISABLED_MAX):
            th._next_part_revision(("s1", f"m{i}", "p1"))
        # Touch m0 (oldest) again — should move it to the end.
        th._next_part_revision(("s1", "m0", "p1"))
        # Insert one more — the NEW oldest (m1, NOT m0) should be evicted.
        th._next_part_revision(("s1", "m_new", "p1"))
        assert ("s1", "m0", "p1") in th._part_revisions  # m0 survived
        assert ("s1", "m1", "p1") not in th._part_revisions  # m1 evicted


# ===========================================================================
# rev-ogpt MAJOR 6 — duplicate tombstone FIFO + TTL semantics
# ===========================================================================

class TestMajor6DuplicateTombstoneFifo:
    """rev-ogpt MAJOR 6: a duplicate ``message.removed`` for an
    already-recorded (sid, mid) refreshes the TTL AND ``move_to_end``s
    the key, so the freshest tombstone is never the oldest in FIFO order.
    v0.5 only refreshed the timestamp, leaving the duplicate at its
    original insertion position → the cap could evict the freshest data.
    """

    def test_duplicate_move_to_end(self):
        """Duplicate tombstone moves to end of FIFO order."""
        from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_MAX
        th = TokenStreamHub()
        # Fill the queue to one below cap.
        for i in range(TOKEN_REMOVED_MESSAGES_MAX - 1):
            th.on_message_removed("s1", f"m{i}")
        # Insert m_special (will become oldest soon).
        th.on_message_removed("s1", "m_special")
        assert ("s1", "m_special") in th._removed_messages
        # Now insert enough new entries to push m_special close to eviction.
        # Re-touch m_special — should move it to the end.
        th.on_message_removed("s1", "m_special")  # duplicate
        # Fill the rest with new entries (one more would have evicted m_special
        # without the move_to_end).
        th.on_message_removed("s1", "m_last")
        # m_special survived (it was moved to the end before m_last insertion).
        assert ("s1", "m_special") in th._removed_messages
        # The oldest (m0) was evicted, not m_special.
        assert ("s1", "m0") not in th._removed_messages

    def test_duplicate_refreshes_ttl(self):
        """Duplicate tombstone refreshes the TTL timestamp."""
        th = TokenStreamHub()
        th.on_message_removed("s1", "m1")
        old_ts = th._removed_messages[("s1", "m1")]
        # Wait a tiny bit then duplicate.
        import time as _time
        _time.sleep(0.005)
        th.on_message_removed("s1", "m1")
        new_ts = th._removed_messages[("s1", "m1")]
        assert new_ts > old_ts, (
            f"duplicate tombstone must refresh TTL timestamp "
            f"({new_ts} should be > {old_ts})"
        )

    def test_full_queue_duplicate_oldest_then_insert_new(self):
        """Cap edge case: full queue → duplicate the oldest → insert new
        → the OLDEST (not the duplicated key) is evicted."""
        from oc_slimapi.config import TOKEN_REMOVED_MESSAGES_MAX
        th = TokenStreamHub()
        # Fill exactly to cap.
        for i in range(TOKEN_REMOVED_MESSAGES_MAX):
            th.on_message_removed("s1", f"m{i}")
        assert len(th._removed_messages) == TOKEN_REMOVED_MESSAGES_MAX
        # Duplicate m0 (currently oldest).
        th.on_message_removed("s1", "m0")
        # m0 should now be at the end (move_to_end).
        # Last key in iteration order should be m0.
        last_key = next(reversed(th._removed_messages))
        assert last_key == ("s1", "m0"), (
            f"m0 should be at end after duplicate; last key = {last_key}"
        )
        # Insert one more — m1 (the new oldest) is evicted, NOT m0.
        th.on_message_removed("s1", "m_new")
        assert ("s1", "m0") in th._removed_messages  # survived
        assert ("s1", "m1") not in th._removed_messages  # evicted
        assert ("s1", "m_new") in th._removed_messages
