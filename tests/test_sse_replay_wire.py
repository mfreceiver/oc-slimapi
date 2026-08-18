"""B3b-2 — v4 SSE ``id:``/replay wire layer (REPLAY-001..018) + v3 anchors.

Covers the wire half of design-v4-sse-replay.md (B3b-1 covered the pure
data layer in tests/test_replay_log.py):

* ``id:`` generation — global ``g:<epoch>:<seq>`` / token ``t:<sid>:<epoch>:<seq>``,
  stamped at hub fanout; v4 subscribers only (v3 frames carry NO id at all —
  byte-anchored below); meta/resync/heartbeat frames never carry an id
  (REPLAY-014 / §7.0②).
* Published-frame logging — GlobalHub digest/q/p/error frames into the
  global domain, TokenStreamHub delta / done-marker / truncated /
  ``message.removed`` tombstone frames into per-sid domains, logged even
  with zero subscribers ("published" semantics, REPLAY-007/018).
* Last-Event-ID reconnect — the frozen ① syntax / ② endpoint+sid /
  ③ epoch / ④ barrier→window→gap classification priority, replay frames
  yielded strictly seq-increasing BEFORE any new frame, NO snapshot frames.
* tokens=1 retired in v4 (§7.3) with the exact frozen error body; the v3
  tokens=1 behaviour is unchanged.
* meta v4 additive extension (§7.0②): capabilities + epoch + seqBase; the
  v3 meta shape is byte-identical (zero change).
* Upstream-loss barrier wiring (REPLAY-015/017/018): first confirmed loss
  fans ``resync{reconnect_no_replay}`` to existing subscribers and writes
  barriers across ALL domains (offline token domains included); reconnect
  cursors at/below the watermark are uniformly intercepted.
* sweep/recycle wiring: the periodic sweep TTL-GCs frames, recycles
  frame-less token domains while RETAINING seq continuity + barriers.

Harnesses:

* **A (fake)** — minimal apps with fake hubs (test_v3_sse_meta pattern) for
  the v3 byte-exact anchors and the v4-without-replay-log degradation.
* **B (real)** — real HubRegistry/GlobalHub/TokenStreamHub/TokenStreamRegistry
  sharing a real ReplayLog; scripted finite streams (the script coroutine
  publishes into the hub, then enqueues STOP) so httpx can read a complete
  deterministic body.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import orjson
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import events as events_routes
from oc_slimapi.routes import token_stream as stream_routes
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.sse import global_hub as global_hub_module
from oc_slimapi.sse.tokenstream import hub as tokenstream_hub_module
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub import STOP as HUB_STOP, HubRegistry
from oc_slimapi.sse.replay_log import (
    FRAME_KIND_TOMBSTONE,
    GLOBAL_DOMAIN,
    ReplayFrames,
    ReplayIgnoreReset,
    ReplayLog,
    ReplayResync,
    token_domain,
)
from oc_slimapi.sse.replay_wire import (
    classify_reconnect,
    frame_with_id,
    meta_v4_extension,
    parse_last_event_id,
    replay_sweep_loop,
    sse_id_line,
)
from oc_slimapi.sse.token_hub import (
    STOP as TOKEN_STOP,
    TokenStreamHub,
    sse_frame,
)
from oc_slimapi.sse.tokenstream.frames import _message_removed_frame
from oc_slimapi.sse.tokenstream.hub import DEFAULT_TOKEN_MAX_FRAME_BYTES
from oc_slimapi.sse.tokenstream.subscriber import TokenStreamRegistry, TokenSubscriber

EPOCH = "0123456789abcdef"
OTHER_EPOCH = "ffffffffffffffff"
SID = "s1"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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
    )
    base.update(overrides)
    return Settings(**base)


def _parse_block(block: bytes):
    """Parse one SSE block → (event, id, data-dict-or-None)."""
    event = None
    id_ = None
    data_lines: list[bytes] = []
    for line in block.split(b"\n"):
        if line.startswith(b"event:"):
            event = line[6:].strip().decode()
        elif line.startswith(b"id:"):
            id_ = line[3:].strip().decode()
        elif line.startswith(b"data:"):
            data_lines.append(line[5:].strip())
    data = None
    if data_lines:
        data = json.loads(b"\n".join(data_lines).decode())
    return event, id_, data


def _blocks(body: bytes):
    """All non-empty SSE blocks (raw bytes) in wire order."""
    for block in body.split(b"\n\n"):
        block = block.strip(b"\n")
        if block:
            yield block


def _frames(body: bytes):
    """(event, id, data) tuples in wire order (data frames only)."""
    for block in _blocks(body):
        event, id_, data = _parse_block(block)
        if data is not None:
            yield event, id_, data


def _ids(body: bytes) -> list[str | None]:
    return [id_ for _, id_, _ in _frames(body)]


def _seq_of(id_: str) -> int:
    return int(id_.rsplit(":", 1)[1])


async def _read(app: FastAPI, path: str, headers: dict | None = None):
    """Open the (finite, scripted) stream; return (response, body)."""
    transport = ASGITransport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", path, headers=headers or {}) as response:
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
            return response, body


def _q_event(n: int) -> dict:
    """An IMMEDIATE question.* global event (fans out synchronously)."""
    return {
        "directory": "/base",
        "payload": {
            "type": "question.asked",
            "properties": {"questionID": f"q{n}", "title": f"t{n}"},
        },
    }


def _q_frame(n: int) -> bytes:
    # IMMEDIATE q/p frames are DATA-ONLY (no ``event:`` line) — that is
    # the frozen v3 wire shape and v4 keeps it.
    return sse_frame(
        {
            "directory": "/base",
            "type": "question.asked",
            "properties": {"questionID": f"q{n}", "title": f"t{n}"},
        },
    )


def _text_start(sid: str, mid: str, pid: str) -> dict:
    return {
        "part": {
            "sessionID": sid,
            "messageID": mid,
            "id": pid,
            "type": "text",
            "time": {"end": None},
            "text": "",
        }
    }


def _delta(sid: str, mid: str, pid: str, text: str) -> dict:
    return {
        "sessionID": sid,
        "messageID": mid,
        "partID": pid,
        "field": "text",
        "delta": text,
    }


async def _kill_hub_tasks(hub: GlobalHub) -> None:
    """Cancel the 4 background tasks subscribe()/ensure_upstream() spawns
    (frame-level tests build bare GlobalHubs outside a registry)."""
    tasks = [
        task
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if task is not None and not task.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Harness A — fake hubs (test_v3_sse_meta pattern), no replay state
# ---------------------------------------------------------------------------

class _FakeSubscriber:
    def __init__(self, sub_id: str = "sub_test") -> None:
        self.id = sub_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.metrics = SimpleNamespace(
            gzip_raw_bytes_total=0, gzip_compressed_bytes_total=0,
        )

    def ack(self, item: bytes) -> None:  # pragma: no cover - no-op
        pass


class _FakeHubs:
    def __init__(self) -> None:
        self.sub = _FakeSubscriber()

    def subscribe(self) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass


class _FakeTokenRegistry:
    def __init__(self) -> None:
        self.sub = _FakeSubscriber("tok_test")

    def subscribe(self, sid: str) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass

    def attach_events_subscriber(self, subscriber) -> None:
        pass

    def detach_events_subscriber(self, subscriber) -> None:
        pass


def _fake_app(*, hubs=None, token_registry=None) -> FastAPI:
    app = FastAPI(title="sse-replay-wire-fake")
    app.add_middleware(SlimapiSelectorMiddleware)
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = hubs if hubs is not None else _FakeHubs()
    app.state.token_registry = (
        token_registry if token_registry is not None else _FakeTokenRegistry()
    )
    app.include_router(events_routes.router)
    app.include_router(stream_routes.router)
    register_error_handlers(app)
    return app


# ---------------------------------------------------------------------------
# Harness B — real hub stack + scripted finite streams
# ---------------------------------------------------------------------------

class _ScriptedHubRegistry(HubRegistry):
    """Real HubRegistry whose subscribe() schedules a per-test script.

    The script coroutine ``(hub, subscriber)`` publishes into the real hub
    (so fanout + replay logging run) and typically finishes by enqueuing
    STOP — the SSE generator then drains a deterministic finite sequence.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(None, **kwargs)
        self._scripts: list = []
        self.script_errors: list[BaseException] = []

    def push_script(self, script) -> None:
        self._scripts.append(script)

    def subscribe(self):
        sub = super().subscribe()
        if self._scripts:
            script = self._scripts.pop(0)
            hub = self.get_global()
            asyncio.get_running_loop().create_task(self._guard(script, hub, sub))
        return sub

    async def _guard(self, script, hub, sub) -> None:
        try:
            await script(hub, sub)
        except Exception as exc:  # noqa: BLE001 — recorded, surfaced by assertions
            self.script_errors.append(exc)


class _ScriptedTokenRegistry(TokenStreamRegistry):
    """Real TokenStreamRegistry whose subscribe() schedules a script.

    Script signature ``(subscriber, sid)``.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._scripts: list = []
        self.script_errors: list[BaseException] = []

    def push_script(self, script) -> None:
        self._scripts.append(script)

    def subscribe(self, sid: str):
        sub = super().subscribe(sid)
        if self._scripts:
            script = self._scripts.pop(0)
            asyncio.get_running_loop().create_task(self._guard(script, sub, sid))
        return sub

    async def _guard(self, script, sub, sid) -> None:
        try:
            await script(sub, sid)
        except Exception as exc:  # noqa: BLE001 — recorded, surfaced by assertions
            self.script_errors.append(exc)


def _publish_global(hub: GlobalHub, events) -> None:
    for event in events:
        hub.publish(event)


def _script_publish_global(*events, stop=True):
    """Script: publish global events into the real hub, then STOP."""

    async def script(hub, sub):
        _publish_global(hub, events)
        if stop:
            sub.put(HUB_STOP)

    return script


async def _stop_token_stream(sub, sid=None) -> None:
    sub.put(TOKEN_STOP)


class _RealStack:
    """Real hub/token/replay stack wired onto a FastAPI app."""

    def __init__(self, *, log: ReplayLog | None = None, **registry_kwargs) -> None:
        self.log = log if log is not None else ReplayLog(epoch=EPOCH)
        self.hubs = _ScriptedHubRegistry(**registry_kwargs)
        self.hubs.set_replay_log(self.log)
        self.token_hub = TokenStreamHub(replay_log=self.log)
        self.hubs.set_token_hub(self.token_hub)
        self.token_registry = _ScriptedTokenRegistry(
            self.token_hub,
            self.hubs,
            max_subscribers=64,
            queue_items=64,
            buffer_bytes=512 * 1024,
            max_frame_bytes=DEFAULT_TOKEN_MAX_FRAME_BYTES,
        )
        self.app = FastAPI(title="sse-replay-wire-real")
        self.app.add_middleware(SlimapiSelectorMiddleware)
        self.app.state.config = _settings()
        self.app.state.schema_degraded = False
        self.app.state.deployment_revision = None
        self.app.state.hubs = self.hubs
        self.app.state.token_registry = self.token_registry
        self.app.state.replay_log = self.log
        self.app.state.replay_epoch = self.log.epoch
        self.app.include_router(events_routes.router)
        self.app.include_router(stream_routes.router)
        register_error_handlers(self.app)

    @property
    def hub(self) -> GlobalHub:
        return self.hubs.get_global()

    def publish(self, *events) -> None:
        _publish_global(self.hub, events)

    async def close(self) -> None:
        try:
            await self.hubs.close()
        finally:
            self.token_hub.stop()
            assert not self.hubs.script_errors, self.hubs.script_errors
            assert not self.token_registry.script_errors, (
                self.token_registry.script_errors
            )


@pytest.fixture
async def stack():
    s = _RealStack()
    try:
        yield s
    finally:
        await s.close()


# ===========================================================================
# §1 — id line / parsing helpers (unit)
# ===========================================================================

def test_sse_id_line_global_and_token_format():
    assert sse_id_line("g", EPOCH, 7) == b"id: g:0123456789abcdef:7\n"
    assert (
        sse_id_line(token_domain(SID), EPOCH, 3)
        == b"id: t:s1:0123456789abcdef:3\n"
    )


def test_frame_with_id_prepends_id_line():
    frame = sse_frame({"a": 1}, event="session.digest")
    assert frame_with_id(frame, "g", EPOCH, 2) == sse_id_line("g", EPOCH, 2) + frame


def test_parse_global_valid_and_seq_zero():
    assert parse_last_event_id(f"g:{EPOCH}:12", token_sid=None) == (EPOCH, 12)
    assert parse_last_event_id(f"g:{EPOCH}:0", token_sid=None) == (EPOCH, 0)


@pytest.mark.parametrize(
    "header",
    [
        "g:0123456789abcdef:1:2",   # extra segment
        "g:0123456789abcdef",        # missing seq
        "g:XYZ:1",                   # epoch not hex
        "g:0123456789ABCDEF:1",      # uppercase hex rejected
        "g:0123456789abcde:1",       # 15-hex epoch
        "g:0123456789abcdef:-1",     # negative seq
        "g:0123456789abcdef:x",      # non-decimal seq
        "g:0123456789abcdef:1.5",    # float seq
        "t:s1:0123456789abcdef:1",   # token label on global endpoint
        "x:0123456789abcdef:1",      # unknown label
        "garbage",
        "g::1",                      # empty epoch
        "g:0123456789abcdef:",       # empty seq
    ],
)
def test_parse_global_rejects_invalid(header):
    assert parse_last_event_id(header, token_sid=None) is None


def test_parse_global_empty_header_is_none():
    assert parse_last_event_id("", token_sid=None) is None
    assert parse_last_event_id(None, token_sid=None) is None


def test_parse_token_valid_including_colon_sid():
    assert parse_last_event_id(f"t:s1:{EPOCH}:5", token_sid=SID) == (EPOCH, 5)
    # sid containing colons — rsplit-from-right grammar
    assert parse_last_event_id(f"t:a:b:{EPOCH}:5", token_sid="a:b") == (EPOCH, 5)


def test_parse_token_rejects_wrong_sid_and_label():
    assert parse_last_event_id(f"t:s2:{EPOCH}:5", token_sid=SID) is None
    assert parse_last_event_id(f"g:{EPOCH}:5", token_sid=SID) is None
    # ① still applies on the token endpoint
    assert parse_last_event_id(f"t:s1:{OTHER_EPOCH}:5x", token_sid=SID) is None


def test_classify_reconnect_no_header_returns_none():
    log = ReplayLog(epoch=EPOCH)
    assert classify_reconnect(None, log, domain=GLOBAL_DOMAIN) is None
    assert classify_reconnect("", log, domain=GLOBAL_DOMAIN) is None


def test_classify_reconnect_syntax_violation_returns_none_not_epoch_changed():
    """REPLAY-016: ① outranks ③ — a malformed header NEVER epoch_changed."""
    log = ReplayLog(epoch=EPOCH)
    assert classify_reconnect("not-an-id", log, domain=GLOBAL_DOMAIN) is None


def test_classify_reconnect_cross_endpoint_outranks_epoch():
    """REPLAY-016: ② outranks ③ — token ID on /events is a reset, even
    with a stale epoch."""
    log = ReplayLog(epoch=EPOCH)
    assert (
        classify_reconnect(
            f"t:{SID}:{OTHER_EPOCH}:5", log, domain=GLOBAL_DOMAIN
        )
        is None
    )


def test_classify_reconnect_epoch_mismatch_resync():
    log = ReplayLog(epoch=EPOCH)
    outcome = classify_reconnect(f"g:{OTHER_EPOCH}:3", log, domain=GLOBAL_DOMAIN)
    assert isinstance(outcome, ReplayResync)
    assert outcome.reason == "epoch_changed"


def test_classify_reconnect_window_replay():
    log = ReplayLog(epoch=EPOCH)
    log.append(GLOBAL_DOMAIN, b"a")
    log.append(GLOBAL_DOMAIN, b"b")
    log.append(GLOBAL_DOMAIN, b"c")
    outcome = classify_reconnect(f"g:{EPOCH}:1", log, domain=GLOBAL_DOMAIN)
    assert isinstance(outcome, ReplayFrames)
    assert [e.seq for e in outcome.entries] == [2, 3]


def test_classify_reconnect_future_cursor_reset():
    log = ReplayLog(epoch=EPOCH)
    log.append(GLOBAL_DOMAIN, b"a")
    outcome = classify_reconnect(f"g:{EPOCH}:99", log, domain=GLOBAL_DOMAIN)
    assert isinstance(outcome, ReplayIgnoreReset)


# ===========================================================================
# §2 — hub fanout logging (frame level)
# ===========================================================================

async def test_emit_directory_frame_logs_to_global_domain_without_subscribers():
    log = ReplayLog(epoch=EPOCH)
    hub = GlobalHub(None, replay_log=log)
    try:
        hub.publish(_q_event(1))
        assert log.last_seq(GLOBAL_DOMAIN) == 1
        assert log.domain_frame_count(GLOBAL_DOMAIN) == 1
    finally:
        await _kill_hub_tasks(hub)


async def test_emit_directory_frame_v3_subscriber_gets_raw_bytes():
    """v3 zero change: wired log, non-v4 subscriber → id-less frame."""
    log = ReplayLog(epoch=EPOCH)
    hub = GlobalHub(None, replay_log=log)
    sub = hub.subscribe()
    try:
        hub.publish(_q_event(1))
        item = sub.queue.get_nowait()
        # welcome frame first, then the business frame
        assert item.startswith(b"event: server.connected")
        item = sub.queue.get_nowait()
        assert item == _q_frame(1)
        assert not item.startswith(b"id:")
    finally:
        hub.subscribers.discard(sub)
        await _kill_hub_tasks(hub)


async def test_emit_directory_frame_v4_subscriber_gets_id_stamped():
    log = ReplayLog(epoch=EPOCH)
    hub = GlobalHub(None, replay_log=log)
    sub = hub.subscribe()
    sub.wire_v4 = True
    try:
        hub.publish(_q_event(1))
        sub.queue.get_nowait()  # welcome
        item = sub.queue.get_nowait()
        assert item == sse_id_line(GLOBAL_DOMAIN, EPOCH, 1) + _q_frame(1)
        assert log.last_seq(GLOBAL_DOMAIN) == 1
    finally:
        hub.subscribers.discard(sub)
        await _kill_hub_tasks(hub)


async def test_allowlist_dropped_frames_are_not_published():
    log = ReplayLog(epoch=EPOCH)
    hub = GlobalHub(None, replay_log=log)
    try:
        hub.set_directory_allowlist(["/allowed"])
        hub.publish(_q_event(1))  # directory /base → dropped
        assert log.last_seq(GLOBAL_DOMAIN) == 0
        assert log.domain_frame_count(GLOBAL_DOMAIN) == 0
    finally:
        await _kill_hub_tasks(hub)


async def test_notify_upstream_loss_writes_barriers_and_fans_resync():
    """REPLAY-015 (frame level): first confirmed loss → resync fanout to
    existing subscribers + barrier across global AND offline token domains."""
    log = ReplayLog(epoch=EPOCH)
    hub = GlobalHub(None, replay_log=log)
    hub.set_token_hub(TokenStreamHub(replay_log=log))
    sub = hub.subscribe()
    sub.wire_v4 = True
    try:
        hub.publish(_q_event(1))
        hub.publish(_q_event(2))
        # offline token domain: frames published with zero subscribers
        hub.publish({
            "directory": "/base",
            "payload": {
                "type": "message.part.updated",
                "properties": _text_start(SID, "m1", "p1"),
            },
        })
        hub.publish({
            "directory": "/base",
            "payload": {
                "type": "message.part.delta",
                "properties": _delta(SID, "m1", "p1", "hello"),
            },
        })
        # flush the pending delta (the real flush loop does this ~100ms) —
        # only PUBLISHED frames create the domain the barrier must span.
        hub._token_hub.flush()
        hub._notify_upstream_loss()
        assert log.barrier_watermark(GLOBAL_DOMAIN) == 2
        assert log.barrier_watermark(token_domain(SID)) == 1
        # existing subscriber got the resync frame (no id)
        drained = []
        while not sub.queue.empty():
            drained.append(sub.queue.get_nowait())
        resync_frames = [f for f in drained if f.startswith(b"event: resync")]
        assert len(resync_frames) == 1
        assert b'"reconnect_no_replay"' in resync_frames[0]
        assert not resync_frames[0].startswith(b"id:")
        # epoch unchanged, seq continues after recovery
        epoch_before = log.epoch
        hub.publish(_q_event(3))
        assert log.epoch == epoch_before
        assert log.last_seq(GLOBAL_DOMAIN) == 3
    finally:
        hub.subscribers.discard(sub)
        await _kill_hub_tasks(hub)


async def test_token_fanout_logs_and_stamps_v4():
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = TokenSubscriber(session_id=SID, metrics=th._metrics)
    sub.wire_v4 = True
    th.attach_subscriber(SID, sub)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "hello "))
        th.on_part_delta(_delta(SID, "m1", "p1", "world"))
        th.flush()
        assert log.last_seq(token_domain(SID)) == 1
        drained = []
        while True:
            try:
                drained.append(sub.queue.get_nowait())
            except Exception:
                break
        deltas = [
            f for f in drained if b"event: message.part.delta" in f
        ]
        assert len(deltas) == 1
        assert deltas[0].startswith(sse_id_line(token_domain(SID), EPOCH, 1))
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


async def test_token_message_removed_tombstone_logged_contiguous():
    """REPLAY-012: the tombstone consumes a seq exactly like a business
    frame — the domain sequence stays hole-free."""
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = TokenSubscriber(session_id=SID, metrics=th._metrics)
    th.attach_subscriber(SID, sub)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "x"))
        th.flush()  # seq 1 (delta)
        th.on_message_removed(SID, "m1")  # seq 2 (tombstone)
        assert log.last_seq(token_domain(SID)) == 2
        outcome = log.replay(token_domain(SID), after_seq=0, epoch=EPOCH)
        assert isinstance(outcome, ReplayFrames)
        kinds = [e.kind for e in outcome.entries]
        seqs = [e.seq for e in outcome.entries]
        assert kinds == ["business", FRAME_KIND_TOMBSTONE]
        assert seqs == [1, 2]
        assert outcome.entries[1].payload == _message_removed_frame(SID, "m1")
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


async def test_token_truncated_frame_logged_as_business(monkeypatch):
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    monkeypatch.setattr(tokenstream_hub_module, "TOKEN_PART_MAX_BYTES", 4)
    sub = TokenSubscriber(session_id=SID, metrics=th._metrics)
    th.attach_subscriber(SID, sub)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "hello world longer than cap"))
        th.flush()
        outcome = log.replay(token_domain(SID), after_seq=0, epoch=EPOCH)
        assert isinstance(outcome, ReplayFrames)
        assert any(b'"truncated":true' in e.payload for e in outcome.entries)
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


async def test_global_heartbeat_never_id_never_logged(monkeypatch):
    """REPLAY-014: heartbeat frames carry no id and never enter the log."""
    log = ReplayLog(epoch=EPOCH)
    hub = GlobalHub(None, replay_log=log)
    monkeypatch.setattr(global_hub_module, "HEARTBEAT_SECONDS", 0.02)
    sub = hub.subscribe()
    sub.wire_v4 = True
    task = asyncio.create_task(hub.heartbeat_loop())
    try:
        await asyncio.sleep(0.08)
        beats = []
        while not sub.queue.empty():
            item = sub.queue.get_nowait()
            if item.startswith(b"event: server.heartbeat"):
                beats.append(item)
        assert beats
        assert all(not b.startswith(b"id:") for b in beats)
        assert log.domain_frame_count(GLOBAL_DOMAIN) == 0
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        hub.subscribers.discard(sub)
        await _kill_hub_tasks(hub)


async def test_token_heartbeat_and_resync_not_logged():
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    sub = TokenSubscriber(session_id=SID, metrics=th._metrics)
    sub.wire_v4 = True
    th.attach_subscriber(SID, sub)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "x"))
        th.flush()  # seq 1
        th._fanout_heartbeat()
        th._fanout_resync(SID, "token_memory_limit")
        assert log.last_seq(token_domain(SID)) == 1
        assert log.domain_frame_count(token_domain(SID)) == 1
        # the fanned heartbeat/resync frames carry no id even for v4
        drained = []
        while True:
            try:
                drained.append(sub.queue.get_nowait())
            except Exception:
                break
        ctrl = [f for f in drained if b"server.heartbeat" in f or b"resync" in f]
        assert ctrl
        assert all(not f.startswith(b"id:") for f in ctrl)
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


# ===========================================================================
# §3 — /events wire (Harness B unless noted)
# ===========================================================================

async def test_v4_events_first_connect_meta_seqbase_and_order(stack: _RealStack):
    """REPLAY-001: meta (no id, seqBase=max seq) → new frames (id) → end."""
    for n in range(1, 6):
        stack.publish(_q_event(n))
    stack.hubs.push_script(_script_publish_global(_q_event(6)))

    response, body = await _read(stack.app, "/slimapi/events?v=4")
    assert response.status_code == 200
    frames = list(_frames(body))

    event0, id0, data0 = frames[0]
    assert event0 == "slimapi.meta"
    assert id0 is None  # meta never carries an id (§7.0②)
    assert set(data0.keys()) == {
        "subscriberId", "tokens", "capabilities", "epoch", "seqBase",
    }
    assert data0["tokens"] is False
    assert data0["capabilities"] == {"sseReplay": True}
    assert data0["epoch"] == EPOCH
    assert data0["seqBase"] == 5

    # welcome frame is connection-scoped — no id, before business frames
    assert frames[1][0] == "server.connected"
    assert frames[1][1] is None
    # the new business frame: id g:<epoch>:6 (data-only q/p frame)
    assert frames[2][0] is None
    assert frames[2][2]["type"] == "question.asked"
    assert frames[2][1] == f"g:{EPOCH}:6"
    # NO resync on a first connect
    assert all(e != "resync" for e, _, _ in frames)


async def test_v4_events_window_replay_strictly_before_new_frames(stack: _RealStack):
    """REPLAY-002: cursor 4 → replay 5,6 (with ids) then live 7 — strictly
    increasing, replay block precedes the welcome/new frames."""
    for n in range(1, 7):
        stack.publish(_q_event(n))
    stack.hubs.push_script(_script_publish_global(_q_event(7)))

    response, body = await _read(
        stack.app, "/slimapi/events?v=4", headers={"Last-Event-ID": f"g:{EPOCH}:4"},
    )
    assert response.status_code == 200
    frames = list(_frames(body))

    assert frames[0][0] == "slimapi.meta"
    assert frames[0][2]["seqBase"] == 6
    # replay block: 5 and 6, id-stamped, payload equal to the originals
    assert [f[1] for f in frames[1:3]] == [f"g:{EPOCH}:5", f"g:{EPOCH}:6"]
    assert frames[1][2] == {
        "directory": "/base",
        "type": "question.asked",
        "properties": {"questionID": "q5", "title": "t5"},
    }
    assert frames[2][2]["properties"]["questionID"] == "q6"
    # welcome AFTER the replay block, then the live frame 7
    assert frames[3][0] == "server.connected"
    assert frames[3][1] is None
    assert frames[4][1] == f"g:{EPOCH}:7"
    # strictly increasing id seq across the whole connection
    id_frames = [f for f in frames if f[1] is not None]
    seqs = [_seq_of(f[1]) for f in id_frames]
    assert seqs == sorted(seqs)


async def test_v4_events_replay_up_to_date_no_frames_no_resync(stack: _RealStack):
    stack.publish(_q_event(1), _q_event(2))
    stack.hubs.push_script(_script_publish_global())

    response, body = await _read(
        stack.app, "/slimapi/events?v=4", headers={"Last-Event-ID": f"g:{EPOCH}:2"},
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    assert frames[0][0] == "slimapi.meta"
    assert frames[1][0] == "server.connected"  # no replay frames, no resync
    assert all(e != "resync" for e, _, _ in frames)


async def test_v4_events_old_epoch_resync_epoch_changed(stack: _RealStack):
    """REPLAY-003: stale epoch → resync{epoch_changed}, no replay frames."""
    stack.publish(_q_event(1))
    stack.hubs.push_script(_script_publish_global())

    response, body = await _read(
        stack.app, "/slimapi/events?v=4",
        headers={"Last-Event-ID": f"g:{OTHER_EPOCH}:1"},
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    assert frames[0][0] == "slimapi.meta"
    assert frames[1] == ("resync", None, {"reason": "epoch_changed"})
    assert frames[1][1] is None  # resync never carries an id
    assert frames[2][0] == "server.connected"


async def test_v4_events_expired_window_resync_no_snapshot():
    """REPLAY-004 + REPLAY-006: cursor inside an expired window →
    replay_expired and NO snapshot frame ever follows."""
    fake_now = [1000.0]
    log = ReplayLog(epoch=EPOCH, ttl_s=0.05, clock=lambda: fake_now[0])
    s = _RealStack(log=log)
    try:
        s.publish(_q_event(1), _q_event(2), _q_event(3))
        fake_now[0] += 0.1  # all frames past TTL
        s.hubs.push_script(_script_publish_global())

        response, body = await _read(
            s.app, "/slimapi/events?v=4",
            headers={"Last-Event-ID": f"g:{EPOCH}:1"},
        )
        assert response.status_code == 200
        frames = list(_frames(body))
        assert frames[1] == ("resync", None, {"reason": "replay_expired"})
        # REPLAY-006: NO snapshot frame ever (client does HTTP full fetch)
        assert all(e != "snapshot" for e, _, _ in frames)
    finally:
        await s.close()


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage",
        "not:a:valid:id",
        f"g:{EPOCH}:999",        # future seq (④ window: ignore reset)
        f"t:s1:{EPOCH}:5",       # cross-endpoint (②)
        f"t:{SID}:{OTHER_EPOCH}:5",  # ② outranks ③ (REPLAY-016)
    ],
)
async def test_v4_events_ignore_reset_inputs(stack: _RealStack, cursor):
    """REPLAY-005 / REPLAY-016: ①② violations + future cursor → ignore
    reset — NO resync, NO replay, first-connect semantics."""
    stack.publish(_q_event(1))
    stack.hubs.push_script(_script_publish_global())

    response, body = await _read(
        stack.app, "/slimapi/events?v=4", headers={"Last-Event-ID": cursor},
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    assert frames[0][0] == "slimapi.meta"
    assert frames[1][0] == "server.connected"
    assert all(e != "resync" for e, _, _ in frames)
    assert [f for f in frames if f[1] is not None] == []


async def test_v4_events_g_label_old_epoch_still_epoch_changed(stack: _RealStack):
    """REPLAY-016: a GLOBAL label with a stale epoch DID pass ② → ③ fires."""
    stack.hubs.push_script(_script_publish_global())
    response, body = await _read(
        stack.app, "/slimapi/events?v=4",
        headers={"Last-Event-ID": f"g:{OTHER_EPOCH}:5"},
    )
    frames = list(_frames(body))
    assert frames[1] == ("resync", None, {"reason": "epoch_changed"})


async def test_v4_events_backpressure_overflow_recovery():
    """REPLAY-007: overflowed frames live in the log — the reconnecting
    client replays them with ids."""
    log = ReplayLog(epoch=EPOCH)
    # queue_items=2: the overflow branch enqueues resync+STOP after
    # clearing the queue — a maxsize of 1 would drop STOP (pre-existing
    # put_nowait behaviour, harmless at the production default of 256).
    s = _RealStack(log=log, queue_items=2, buffer_bytes=1024 * 1024)
    try:
        # first connection: 3 quick publishes overflow the 1-slot queue →
        # forced disconnect with the backpressure resync.
        async def script1(hub, sub):
            hub.publish(_q_event(1))
            hub.publish(_q_event(2))
            hub.publish(_q_event(3))

        s.hubs.push_script(script1)
        response, body = await _read(s.app, "/slimapi/events?v=4")
        assert response.status_code == 200
        frames = list(_frames(body))
        backpressure = [
            d for e, i, d in frames
            if e == "resync" and d.get("reason") == "subscriber_backpressure"
        ]
        assert backpressure, frames
        # the log retained every published frame regardless of delivery
        assert log.last_seq(GLOBAL_DOMAIN) == 3

        # reconnect from seq 1 → replay 2,3
        s.hubs.push_script(_script_publish_global())
        response, body = await _read(
            s.app, "/slimapi/events?v=4",
            headers={"Last-Event-ID": f"g:{EPOCH}:1"},
        )
        frames = list(_frames(body))
        assert [f[1] for f in frames[1:3]] == [f"g:{EPOCH}:2", f"g:{EPOCH}:3"]
        assert frames[1][2]["properties"]["questionID"] == "q2"
        assert frames[2][2]["properties"]["questionID"] == "q3"
    finally:
        await s.close()


async def test_v4_events_overflow_beyond_window_expired():
    """REPLAY-008: reconnect cursor fell outside the (count-bound) ring →
    replay_expired."""
    log = ReplayLog(epoch=EPOCH, max_count=2)
    s = _RealStack(log=log)
    try:
        for n in range(1, 5):
            s.publish(_q_event(n))  # window keeps only seq 3,4
        assert log.window_start(GLOBAL_DOMAIN) == 3
        s.hubs.push_script(_script_publish_global())
        response, body = await _read(
            s.app, "/slimapi/events?v=4",
            headers={"Last-Event-ID": f"g:{EPOCH}:1"},
        )
        frames = list(_frames(body))
        assert frames[1] == ("resync", None, {"reason": "replay_expired"})
    finally:
        await s.close()


async def test_tokens_1_v4_retired_exact_body():
    """REPLAY-009 / §7.3: v4 tokens=1 → 400 with the frozen body, before
    the stream opens."""
    s = _RealStack()
    try:
        transport = ASGITransport(s.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as client:
            response = await client.get("/slimapi/events?v=4&tokens=1")
        assert response.status_code == 400
        assert orjson.loads(response.content) == {
            "code": "tokens_stream_retired_in_v4",
            "hint": "token 流请使用 /slimapi/sessions/{sid}/stream",
        }
        assert "text/event-stream" not in response.headers.get("content-type", "")
        assert s.hubs.total_subscribers == 0  # no slot consumed
    finally:
        await s.close()


@pytest.mark.parametrize("version", ["3", "4"])
async def test_tokens_invalid_value_is_invalid_tokens_on_both_wires(version):
    s = _RealStack()
    try:
        transport = ASGITransport(s.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
        ) as client:
            response = await client.get(f"/slimapi/events?v={version}&tokens=2")
        assert response.status_code == 400
        assert orjson.loads(response.content) == {"code": "invalid_tokens"}
    finally:
        await s.close()


async def test_v4_events_id_no_regression_across_three_connections():
    """REPLAY-010: id seq strictly non-decreasing per (epoch, domain) across
    reconnects; epoch unchanged; meta seqBase tracks the domain max."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        # connection 1: consume seq 1..3 live
        s.hubs.push_script(_script_publish_global(*[_q_event(n) for n in (1, 2, 3)]))
        _, body = await _read(s.app, "/slimapi/events?v=4")
        ids1 = [i for i in _ids(body) if i]
        assert ids1 == [f"g:{EPOCH}:{n}" for n in (1, 2, 3)]

        # frames 4,5 published while disconnected
        s.publish(_q_event(4), _q_event(5))

        # connection 2: cursor 3 → replay 4,5, then live 6
        s.hubs.push_script(_script_publish_global(_q_event(6)))
        _, body = await _read(
            s.app, "/slimapi/events?v=4", headers={"Last-Event-ID": f"g:{EPOCH}:3"},
        )
        ids2 = [i for i in _ids(body) if i]
        assert ids2 == [f"g:{EPOCH}:{n}" for n in (4, 5, 6)]

        # connection 3: cursor 5 → replay 6 (published while that client
        # was disconnected), then live 7
        s.hubs.push_script(_script_publish_global(_q_event(7)))
        _, body = await _read(
            s.app, "/slimapi/events?v=4", headers={"Last-Event-ID": f"g:{EPOCH}:5"},
        )
        ids3 = [i for i in _ids(body) if i]
        assert ids3 == [f"g:{EPOCH}:6", f"g:{EPOCH}:7"]
        assert log.epoch == EPOCH
    finally:
        await s.close()


async def test_v4_events_epoch_switch_resync_and_new_segment():
    """REPLAY-010 cross-epoch: a new process epoch resyncs the old cursor;
    the new segment's first business frame is seqBase+1 (= 1)."""
    log1 = ReplayLog(epoch=EPOCH)
    s1 = _RealStack(log=log1)
    await s1.close()
    log1.close()

    log2 = ReplayLog(epoch=OTHER_EPOCH)
    s2 = _RealStack(log=log2)
    try:
        s2.hubs.push_script(_script_publish_global(_q_event(1)))
        response, body = await _read(
            s2.app, "/slimapi/events?v=4",
            headers={"Last-Event-ID": f"g:{EPOCH}:5"},
        )
        frames = list(_frames(body))
        assert frames[1] == ("resync", None, {"reason": "epoch_changed"})
        assert frames[0][2]["epoch"] == OTHER_EPOCH
        assert frames[0][2]["seqBase"] == 0
        # first business frame of the new segment = seqBase + 1
        id_frames = [f for f in frames if f[1] is not None]
        assert id_frames[0][1] == f"g:{OTHER_EPOCH}:1"
    finally:
        await s2.close()


async def test_v4_dual_stream_domain_isolation():
    """REPLAY-011: the global domain and per-sid token domains sequence
    independently — no shared counter, no cross-contamination."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        # global frames + token frames interleave
        s.publish(_q_event(1))
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "a"))
        s.token_hub.flush()  # t:s1:1 (published, zero subscribers)
        s.publish(_q_event(2))  # g:2

        # /events v4 first connect: only the LIVE frame 3 (1-2 predate the
        # connection — a first connect never replays).
        s.hubs.push_script(_script_publish_global(_q_event(3)))
        _, body_events = await _read(s.app, "/slimapi/events?v=4")
        ids_events = [i for i in _ids(body_events) if i]
        assert ids_events == [f"g:{EPOCH}:3"]

        # /stream v4 reconnect from 0: replay t:s1:1, then the live delta 2.
        async def token_script(sub, sid):
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "b"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(token_script)
        _, body_stream = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        ids_stream = [i for i in _ids(body_stream) if i]
        assert ids_stream == [f"t:{SID}:{EPOCH}:1", f"t:{SID}:{EPOCH}:2"]
        # independent per-domain counters — no shared sequence
        assert log.last_seq(GLOBAL_DOMAIN) == 3
        assert log.last_seq(token_domain(SID)) == 2
    finally:
        await s.close()


# ===========================================================================
# §4 — /stream wire (Harness B)
# ===========================================================================

def _token_delta_script(sid: str, text: str, *, pre_text_start=True):
    """Script: text-start (optional) + delta + flush + STOP."""

    async def script(sub, _sid, _stack=None):
        th = _token_delta_script.token_hub
        if pre_text_start:
            th.on_part_updated(_text_start(sid, "m1", "p1"))
        th.on_part_delta(_delta(sid, "m1", "p1", text))
        th.flush()
        sub.put(TOKEN_STOP)

    return script


_token_delta_script.token_hub = None  # set by callers (see tests below)


async def test_v4_stream_first_connect_meta_and_live_delta():
    s = _RealStack()
    try:
        async def script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "hello"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        response, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        assert response.status_code == 200
        frames = list(_frames(body))

        event0, id0, data0 = frames[0]
        assert event0 == "slimapi.meta"
        assert id0 is None
        assert set(data0.keys()) == {
            "subscriberId", "tokens", "capabilities", "epoch", "seqBase",
        }
        assert data0["tokens"] is True
        assert data0["capabilities"] == {"sseReplay": True}
        assert data0["epoch"] == EPOCH

        assert frames[1][0] == "server.connected"  # handshake, no id
        assert frames[1][1] is None
        assert frames[2][0] == "message.part.delta"
        assert frames[2][1] == f"t:{SID}:{EPOCH}:1"
    finally:
        await s.close()


async def test_v4_stream_tombstone_replay_with_id():
    """REPLAY-012 (wire): the reconnect replays the tombstone frame with
    its id, hole-free after the delta."""
    s = _RealStack()
    try:
        # pre-flow: delta seq 1, tombstone seq 2 (no subscribers needed)
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()
        s.token_hub.on_message_removed(SID, "m1")

        s.token_registry.push_script(_stop_token_stream)
        response, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        assert response.status_code == 200
        frames = list(_frames(body))
        assert frames[0][0] == "slimapi.meta"
        # replay block: delta(1) + tombstone(2), both id-stamped
        assert frames[1][0] == "message.part.delta"
        assert frames[1][1] == f"t:{SID}:{EPOCH}:1"
        assert frames[2][0] == "message.removed"
        assert frames[2][1] == f"t:{SID}:{EPOCH}:2"
        assert frames[2][2] == {"sessionID": SID, "messageID": "m1"}
        # replay precedes the handshake welcome
        assert frames[3][0] == "server.connected"
    finally:
        await s.close()


@pytest.mark.parametrize(
    "cursor,expect",
    [
        (f"t:s9:{EPOCH}:1", "reset"),
        (f"g:{EPOCH}:1", "reset"),
        (f"t:{SID}:{OTHER_EPOCH}:1", "epoch_changed"),
    ],
)
async def test_v4_stream_cross_sid_cross_endpoint_and_epoch(cursor, expect):
    """②/③ on the token endpoint: wrong sid / global label → reset; stale
    epoch (correct sid) → resync{epoch_changed, sessionID}."""
    s = _RealStack()
    try:
        s.token_registry.push_script(_stop_token_stream)
        _, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": cursor},
        )
        frames = list(_frames(body))
        assert frames[0][0] == "slimapi.meta"
        if expect == "reset":
            assert frames[1][0] == "server.connected"
            assert all(e != "resync" for e, _, _ in frames)
        else:
            assert frames[1] == (
                "resync", None,
                {"reason": "epoch_changed", "sessionID": SID},
            )
    finally:
        await s.close()


async def test_v3_stream_same_stack_unstamped():
    """REPLAY-011 wire side: a v3 /stream connection on the same stack
    carries no id lines even while the log records the domain sequence."""
    s = _RealStack()
    try:
        async def script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "plain"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        _, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=3")
        for block in _blocks(body):
            assert not block.startswith(b"id:")
        # but the log still advanced (published frames are logged once)
        assert s.log.last_seq(token_domain(SID)) == 1
    finally:
        await s.close()


async def test_v4_stream_expired_window_resync():
    log = ReplayLog(epoch=EPOCH, ttl_s=0.05, clock=lambda: 1000.0)
    s = _RealStack(log=log)
    try:
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()  # seq 1, appended_at=1000.0 (frozen clock)
        # NOTE: with a frozen clock the frames never age, so exercise the
        # expiry via a controllable clock bump instead.
        s.log._clock = lambda: 2000.0  # far past TTL
        s.token_registry.push_script(_stop_token_stream)
        _, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        frames = list(_frames(body))
        assert frames[1] == (
            "resync", None,
            {"reason": "replay_expired", "sessionID": SID},
        )
    finally:
        await s.close()


# ===========================================================================
# §5 — barrier wire (REPLAY-015/017/018) + sweep/recycle
# ===========================================================================

async def test_barrier_boundary_triple_wire():
    """REPLAY-017: watermark-1 and watermark are uniformly intercepted;
    watermark+1 passes to the window judgment and replays."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        s.publish(_q_event(1), _q_event(2), _q_event(3))
        s.hub._notify_upstream_loss()  # watermark 3
        assert log.barrier_watermark(GLOBAL_DOMAIN) == 3
        # post-recovery frames continue the SAME epoch/sequence
        s.publish(_q_event(4), _q_event(5))

        for cursor_seq, expect_resync in ((2, True), (3, True), (4, False)):
            s.hubs.push_script(_script_publish_global())
            _, body = await _read(
                s.app, "/slimapi/events?v=4",
                headers={"Last-Event-ID": f"g:{EPOCH}:{cursor_seq}"},
            )
            frames = list(_frames(body))
            resyncs = [d for e, i, d in frames if e == "resync"]
            if expect_resync:
                assert resyncs == [{"reason": "reconnect_no_replay"}], (
                    cursor_seq, frames,
                )
                replayed = [f for f in frames if f[1] is not None]
                assert replayed == []  # no frames across the barrier
            else:
                assert resyncs == []
                # watermark+1 → past the barrier into the window judgment
                # → replay frame 5 (client had 4)
                id_frames = [f for f in frames if f[1] is not None]
                assert [f[1] for f in id_frames] == [f"g:{EPOCH}:5"]
    finally:
        await s.close()


async def test_offline_token_domain_barrier_and_recycle_retention():
    """REPLAY-018: a token domain with NO online subscribers is still
    barriered on loss; recycle drops frames but retains seq + watermark —
    an old cursor gets the barrier resync, NEVER first-connect release."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        # token frames for a session nobody subscribes to
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()
        s.hub._notify_upstream_loss()
        assert log.barrier_watermark(token_domain(SID)) == 1

        # domain recycle (the sweep policy): frames gone, state kept
        log.recycle_domain(token_domain(SID))
        assert log.domain_frame_count(token_domain(SID)) == 0
        assert log.barrier_watermark(token_domain(SID)) == 1
        assert log.last_seq(token_domain(SID)) == 1

        # wire: reconnect with the pre-loss cursor → barrier resync
        s.token_registry.push_script(_stop_token_stream)
        _, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        frames = list(_frames(body))
        resyncs = [d for e, i, d in frames if e == "resync"]
        assert resyncs == [{"reason": "reconnect_no_replay", "sessionID": SID}]

        # post-recovery append continues the sequence (no ID reuse)
        s.token_hub.on_part_updated(_text_start(SID, "m2", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m2", "p1", "y"))
        s.token_hub.flush()
        assert log.last_seq(token_domain(SID)) == 2
    finally:
        await s.close()


async def test_replay_sweep_loop_ttl_gc_and_domain_recycle():
    """Sweep wiring: the periodic loop TTL-GCs frames and recycles
    frame-less token domains while retaining barriers (fail-safe)."""
    fake_now = [1000.0]
    log = ReplayLog(epoch=EPOCH, ttl_s=0.05, clock=lambda: fake_now[0])
    log.append(GLOBAL_DOMAIN, b"g1")
    log.append(token_domain(SID), b"t1")
    log.write_barrier(token_domain(SID))  # watermark 1
    assert log.barrier_watermark(token_domain(SID)) == 1

    stop = asyncio.Event()
    task = asyncio.create_task(
        replay_sweep_loop(log, interval_s=0.02, stop_event=stop)
    )
    try:
        fake_now[0] += 0.1  # everything past TTL
        await asyncio.sleep(0.09)
        assert log.frame_count() == 0
        # expired token domain recycled by the loop's policy; global kept
        assert token_domain(SID) in log.domain_keys()
        assert log.domain_frame_count(token_domain(SID)) == 0
        # barrier retained (fail-safe) even with an empty window
        assert log.barrier_watermark(token_domain(SID)) == 1
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_replay_sweep_loop_exits_on_closed_log():
    log = ReplayLog(epoch=EPOCH)
    stop = asyncio.Event()
    task = asyncio.create_task(
        replay_sweep_loop(log, interval_s=0.01, stop_event=stop)
    )
    try:
        await asyncio.sleep(0.03)
        log.close()
        await asyncio.sleep(0.05)
        assert task.done()
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_meta_v4_extension_shape():
    ext = meta_v4_extension(EPOCH, 9)
    assert ext == {
        "capabilities": {"sseReplay": True},
        "epoch": EPOCH,
        "seqBase": 9,
    }


# ===========================================================================
# §6 — v3 zero-change anchors (byte-exact)
# ===========================================================================

async def test_v3_events_frame_sequence_byte_exact():
    """v3 /events frames are byte-identical with the v4 code in place."""
    hubs = _FakeHubs()
    hubs.sub.queue.put_nowait(
        sse_frame({"sessionID": SID}, event="server.heartbeat")
    )
    hubs.sub.queue.put_nowait(HUB_STOP)
    app = _fake_app(hubs=hubs)
    response, body = await _read(app, "/slimapi/events?v=3")
    assert response.status_code == 200
    assert body == (
        b'event: slimapi.meta\n'
        b'data: {"subscriberId":"sub_test","tokens":false}\n\n'
        b'event: server.heartbeat\n'
        b'data: {"sessionID":"s1"}\n\n'
    )


async def test_v3_events_last_event_id_resync_byte_exact():
    hubs = _FakeHubs()
    hubs.sub.queue.put_nowait(HUB_STOP)
    app = _fake_app(hubs=hubs)
    response, body = await _read(
        app, "/slimapi/events?v=3", headers={"Last-Event-ID": "anything"},
    )
    assert response.status_code == 200
    assert body == (
        b'event: slimapi.meta\n'
        b'data: {"subscriberId":"sub_test","tokens":false}\n\n'
        b'event: resync\n'
        b'data: {"reason":"reconnect_no_replay"}\n\n'
    )


async def test_v3_stream_byte_exact_with_last_event_id():
    registry = _FakeTokenRegistry()
    registry.sub.queue.put_nowait(
        sse_frame({"sessionID": SID}, event="server.connected")
    )
    registry.sub.queue.put_nowait(TOKEN_STOP)
    app = _fake_app(token_registry=registry)
    response, body = await _read(
        app, f"/slimapi/sessions/{SID}/stream?v=3",
        headers={"Last-Event-ID": "anything"},
    )
    assert response.status_code == 200
    assert body == (
        b'event: slimapi.meta\n'
        b'data: {"subscriberId":"tok_test","tokens":true}\n\n'
        b'event: resync\n'
        b'data: {"reason":"reconnect_no_replay","sessionID":"s1"}\n\n'
        b'event: server.connected\n'
        b'data: {"sessionID":"s1"}\n\n'
    )


async def test_v3_tokens_1_still_meta_tokens_true():
    """REPLAY-009 v3 half: tokens=1 behaviour unchanged (200 + meta)."""
    hubs = _FakeHubs()
    hubs.sub.queue.put_nowait(HUB_STOP)
    app = _fake_app(hubs=hubs)
    response, body = await _read(app, "/slimapi/events?v=3&tokens=1")
    assert response.status_code == 200
    frames = list(_frames(body))
    assert frames[0][0] == "slimapi.meta"
    assert frames[0][2]["tokens"] is True
    assert set(frames[0][2].keys()) == {"subscriberId", "tokens"}


async def test_v3_events_real_stack_no_id_lines():
    """v3 zero change on the REAL stack: frames carry no id lines while
    the replay log advances underneath."""
    s = _RealStack()
    try:
        s.hubs.push_script(_script_publish_global(_q_event(1)))
        response, body = await _read(s.app, "/slimapi/events?v=3")
        assert response.status_code == 200
        for block in _blocks(body):
            assert not block.startswith(b"id:")
        frames = list(_frames(body))
        assert frames[0][2].keys() == {"subscriberId", "tokens"}
        # the published frame was logged — invisible on the v3 wire
        assert s.log.last_seq(GLOBAL_DOMAIN) == 1
    finally:
        await s.close()


async def test_v4_without_replay_log_degrades_to_v3_shape():
    """Minimal apps without app.state.replay_log: v4 keeps working with
    id-less frames and the v3-style reconnect resync."""
    hubs = _FakeHubs()
    hubs.sub.queue.put_nowait(
        sse_frame({"sessionID": SID}, event="server.heartbeat")
    )
    hubs.sub.queue.put_nowait(HUB_STOP)
    app = _fake_app(hubs=hubs)
    response, body = await _read(
        app, "/slimapi/events?v=4",
        headers={"Last-Event-ID": f"g:{EPOCH}:3"},
    )
    assert response.status_code == 200
    frames = list(_frames(body))
    assert frames[0][2].keys() == {"subscriberId", "tokens"}
    assert frames[1] == ("resync", None, {"reason": "reconnect_no_replay"})
    for block in _blocks(body):
        assert not block.startswith(b"id:")
