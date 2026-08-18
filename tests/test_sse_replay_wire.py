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
import re
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
    V4_RESYNC_REASONS,
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


def _text_end(sid: str, mid: str, pid: str, text: str) -> dict:
    return {
        "part": {
            "sessionID": sid,
            "messageID": mid,
            "id": pid,
            "type": "text",
            "time": {"start": 1, "end": 2},
            "text": text,
        }
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

    def subscribe(self, wire_v4: bool = False) -> _FakeSubscriber:
        return self.sub

    def unsubscribe(self, subscriber) -> None:
        pass


class _FakeTokenRegistry:
    def __init__(self) -> None:
        self.sub = _FakeSubscriber("tok_test")

    def subscribe(self, sid: str, wire_v4: bool = False) -> _FakeSubscriber:
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

    def subscribe(self, wire_v4: bool = False):
        sub = super().subscribe(wire_v4=wire_v4)
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

    def subscribe(self, sid: str, wire_v4: bool = False):
        sub = super().subscribe(sid, wire_v4=wire_v4)
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

    def __init__(
        self,
        *,
        log: ReplayLog | None = None,
        token_queue_items: int | None = None,
        **registry_kwargs,
    ) -> None:
        self.log = log if log is not None else ReplayLog(epoch=EPOCH)
        self.hubs = _ScriptedHubRegistry(**registry_kwargs)
        self.hubs.set_replay_log(self.log)
        self.token_hub = TokenStreamHub(replay_log=self.log)
        self.hubs.set_token_hub(self.token_hub)
        token_kwargs: dict = {
            "max_subscribers": 64,
            "queue_items": (
                64 if token_queue_items is None else token_queue_items
            ),
            "buffer_bytes": 512 * 1024,
            "max_frame_bytes": DEFAULT_TOKEN_MAX_FRAME_BYTES,
        }
        self.token_registry = _ScriptedTokenRegistry(
            self.token_hub,
            self.hubs,
            **token_kwargs,
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
    # v4 admission path (rev-gate BLOCKER-1): welcome suppressed at the
    # source — the first queued item is already the stamped business frame.
    sub = hub.subscribe(welcome=False)
    sub.wire_v4 = True
    try:
        hub.publish(_q_event(1))
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


async def test_token_truncated_frame_v3_only_never_logged(monkeypatch):
    """R2 gate: the truncated marker (message.part.snapshot{truncated:true})
    is v4-INELIGIBLE — it must reach v3 subscribers (byte-identical
    semantics) but never enter the ReplayLog nor consume a v4 seq."""
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    monkeypatch.setattr(tokenstream_hub_module, "TOKEN_PART_MAX_BYTES", 4)
    sub = TokenSubscriber(session_id=SID, metrics=th._metrics)
    th.attach_subscriber(SID, sub)  # v3 subscriber (wire_v4=False)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "hello world longer than cap"))
        th.flush()
        # v3 subscriber still receives the truncated marker frame.
        drained = []
        while not sub.queue.empty():
            try:
                drained.append(sub.queue.get_nowait())
            except Exception:
                break
        assert any(
            b"event: message.part.snapshot" in f and b'"truncated":true' in f
            for f in drained
        )
        # ... but it never entered the ReplayLog (no seq allocated).
        assert log.domain_frame_count(token_domain(SID)) == 0
        assert log.last_seq(token_domain(SID)) == 0
    finally:
        th.detach_subscriber(SID, sub)
        th.stop()


async def test_token_truncated_frame_never_reaches_v4_sub(monkeypatch):
    """R2 gate: a v4 subscriber never receives the truncated marker on the
    wire, live or otherwise."""
    log = ReplayLog(epoch=EPOCH)
    th = TokenStreamHub(replay_log=log)
    monkeypatch.setattr(tokenstream_hub_module, "TOKEN_PART_MAX_BYTES", 4)
    sub = TokenSubscriber(session_id=SID, metrics=th._metrics)
    th.attach_subscriber(SID, sub, wire_v4=True)
    try:
        th.on_part_updated(_text_start(SID, "m1", "p1"))
        th.on_part_delta(_delta(SID, "m1", "p1", "hello world longer than cap"))
        th.flush()
        drained = []
        while not sub.queue.empty():
            try:
                drained.append(sub.queue.get_nowait())
            except Exception:
                break
        assert all(b"message.part.snapshot" not in f for f in drained)
        assert log.domain_frame_count(token_domain(SID)) == 0
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
        # R3: a FROZEN reason is still delivered (un-id'd) on v4 — e.g.
        # the reconnect_no_replay fanout from on_upstream_reconnect().
        th._fanout_resync(SID, "reconnect_no_replay")
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
        assert len(ctrl) == 2
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
    _assert_v4_no_forbidden_frames(frames)

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

    # rev-gate BLOCKER-1 / cond 5: server.connected is SUPPRESSED on v4 —
    # the first frame after meta is the stamped business frame directly
    # (data-only q/p frame: no event name, id + data).
    assert frames[1][0] is None
    assert frames[1][2]["type"] == "question.asked"
    assert frames[1][1] == f"g:{EPOCH}:6"
    # NO resync on a first connect, NO server.connected anywhere
    assert all(e != "resync" for e, _, _ in frames)
    assert all(e != "server.connected" for e, _, _ in frames)


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
    _assert_v4_no_forbidden_frames(frames)

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
    # live frame 7 DIRECTLY after the replay block (no welcome on v4)
    assert frames[3][1] == f"g:{EPOCH}:7"
    assert all(f[0] != "server.connected" for f in frames)
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
    _assert_v4_no_forbidden_frames(frames)
    assert frames[0][0] == "slimapi.meta"
    # up-to-date cursor: no replay frames, no resync — and no welcome on
    # v4, so meta is the ONLY frame on the wire.
    assert len(frames) == 1
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
    _assert_v4_no_forbidden_frames(frames)
    assert frames[0][0] == "slimapi.meta"
    assert frames[1] == ("resync", None, {"reason": "epoch_changed"})
    assert frames[1][1] is None  # resync never carries an id
    # no welcome on v4 — resync is the last frame
    assert len(frames) == 2


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
        _assert_v4_no_forbidden_frames(frames)
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
    _assert_v4_no_forbidden_frames(frames)
    assert frames[0][0] == "slimapi.meta"
    # ignore+reset = first-connect semantics; no welcome on v4 → meta only
    assert len(frames) == 1
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
    _assert_v4_no_forbidden_frames(frames)
    assert frames[1] == ("resync", None, {"reason": "epoch_changed"})


async def test_v4_events_backpressure_overflow_recovery():
    """REPLAY-007 + rev-gate R3: overflow terminates the v4 connection with
    NO resync frame (subscriber_backpressure is outside the frozen v4
    reason domain — the disconnect itself is the observable signal); the
    reconnecting client replays the overflowed frames from the log."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log, queue_items=2, buffer_bytes=1024 * 1024)
    try:
        # first connection: 3 quick publishes overflow the 2-slot queue →
        # forced disconnect. v4: STOP only, no backpressure resync.
        async def script1(hub, sub):
            hub.publish(_q_event(1))
            hub.publish(_q_event(2))
            hub.publish(_q_event(3))

        s.hubs.push_script(script1)
        response, body = await _read(s.app, "/slimapi/events?v=4")
        assert response.status_code == 200
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # R3: NO resync frame on the v4 wire at all — and no delivered
        # business frames either (the overflow clears the queue).
        assert all(e != "resync" for e, _, _ in frames), frames
        assert frames == [("slimapi.meta", None, frames[0][2])]
        # ... but the log retained every published frame regardless of
        # delivery (published-not-delivered semantics).
        assert log.last_seq(GLOBAL_DOMAIN) == 3
        # rev-gate R4 condition 6 (global side): backpressure is NOT state
        # invalidation — no barrier, watermark untouched.
        assert log.barrier_watermark(GLOBAL_DOMAIN) is None

        # reconnect from seq 1 → replay 2,3 (the overflowed frames)
        s.hubs.push_script(_script_publish_global())
        response, body = await _read(
            s.app, "/slimapi/events?v=4",
            headers={"Last-Event-ID": f"g:{EPOCH}:1"},
        )
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        assert [f[1] for f in frames[1:3]] == [f"g:{EPOCH}:2", f"g:{EPOCH}:3"]
        assert frames[1][2]["properties"]["questionID"] == "q2"
        assert frames[2][2]["properties"]["questionID"] == "q3"
        # still no barrier after the recovery replay.
        assert log.barrier_watermark(GLOBAL_DOMAIN) is None
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
        _assert_v4_no_forbidden_frames(frames)
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
        _assert_v4_no_forbidden_frames(list(_frames(body)))
        assert ids1 == [f"g:{EPOCH}:{n}" for n in (1, 2, 3)]

        # frames 4,5 published while disconnected
        s.publish(_q_event(4), _q_event(5))

        # connection 2: cursor 3 → replay 4,5, then live 6
        s.hubs.push_script(_script_publish_global(_q_event(6)))
        _, body = await _read(
            s.app, "/slimapi/events?v=4", headers={"Last-Event-ID": f"g:{EPOCH}:3"},
        )
        ids2 = [i for i in _ids(body) if i]
        _assert_v4_no_forbidden_frames(list(_frames(body)))
        assert ids2 == [f"g:{EPOCH}:{n}" for n in (4, 5, 6)]

        # connection 3: cursor 5 → replay 6 (published while that client
        # was disconnected), then live 7
        s.hubs.push_script(_script_publish_global(_q_event(7)))
        _, body = await _read(
            s.app, "/slimapi/events?v=4", headers={"Last-Event-ID": f"g:{EPOCH}:5"},
        )
        ids3 = [i for i in _ids(body) if i]
        _assert_v4_no_forbidden_frames(list(_frames(body)))
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
        _assert_v4_no_forbidden_frames(frames)
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
        _assert_v4_no_forbidden_frames(list(_frames(body_events)))
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
        _assert_v4_no_forbidden_frames(list(_frames(body_stream)))
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
        _assert_v4_no_forbidden_frames(frames)

        event0, id0, data0 = frames[0]
        assert event0 == "slimapi.meta"
        assert id0 is None
        assert set(data0.keys()) == {
            "subscriberId", "tokens", "capabilities", "epoch", "seqBase",
        }
        assert data0["tokens"] is True
        assert data0["capabilities"] == {"sseReplay": True}
        assert data0["epoch"] == EPOCH

        assert frames[1][0] == "message.part.delta"
        assert frames[1][1] == f"t:{SID}:{EPOCH}:1"
        # rev-gate BLOCKER-1: NO server.connected handshake frame on v4 —
        # the stamped delta is the first frame after meta; no snapshot.
        assert all(f[0] != "server.connected" for f in frames)
        assert all(f[0] != "message.part.snapshot" for f in frames)
    finally:
        await s.close()


async def test_v4_stream_tombstone_replay_with_id():
    """REPLAY-012 (rev-gate rewrite): FULL frame sequence — meta → replay
    delta(1) + tombstone(2, WITH id) → live delta(3). The tombstone
    appears EXACTLY ONCE on the whole stream (the v3 handshake pre-fill
    would have double-sent it un-id'd) and no server.connected / snapshot
    frame is ever sent on v4."""
    s = _RealStack()
    try:
        # pre-flow: delta seq 1, tombstone seq 2 (published, zero subs)
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()
        s.token_hub.on_message_removed(SID, "m1")

        # reconnect script: publish one MORE live delta (seq 3) then STOP
        async def script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m2", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m2", "p1", "y"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        response, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        assert response.status_code == 200
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)

        # complete sequence, nothing skipped
        assert [f[0] for f in frames] == [
            "slimapi.meta",
            "message.part.delta",   # replay 1 (m1/p1 "x")
            "message.removed",      # replay 2 (tombstone, id'd)
            "message.part.delta",   # live 3 (m2/p1 "y")
        ]
        assert [f[1] for f in frames[1:]] == [
            f"t:{SID}:{EPOCH}:1", f"t:{SID}:{EPOCH}:2", f"t:{SID}:{EPOCH}:3",
        ]
        # the removed message appears EXACTLY once, WITH its id
        removed = [f for f in frames if f[0] == "message.removed"]
        assert len(removed) == 1
        assert removed[0][2] == {"sessionID": SID, "messageID": "m1"}
        assert removed[0][1] == f"t:{SID}:{EPOCH}:2"
        # no un-id'd control frames beyond the frozen set; no snapshots
        assert all(f[0] != "server.connected" for f in frames)
        assert all(f[0] != "message.part.snapshot" for f in frames)
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
        _assert_v4_no_forbidden_frames(frames)
        assert frames[0][0] == "slimapi.meta"
        if expect == "reset":
            # ignore+reset = first-connect semantics; no welcome on v4 →
            # meta is the only frame (no resync, no replay).
            assert len(frames) == 1
            assert all(e != "resync" for e, _, _ in frames)
        else:
            assert frames[1] == (
                "resync", None,
                {"reason": "epoch_changed", "sessionID": SID},
            )
            assert len(frames) == 2  # no welcome after the resync on v4
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


async def test_v4_stream_finish_part_no_done_marker_on_wire():
    """R2 gate bypass ①: a v4 subscriber that is attached when
    ``finish_part()`` fans ``message.part.snapshot{done:true}`` never sees
    it — the marker is v4-INELIGIBLE (v3-only delivery, never logged, no
    seq). The v4 wire goes delta → subsequent business frames with NO
    terminal snapshot in between."""
    s = _RealStack()
    try:
        async def script(sub, sid):
            # part 1: text-start + delta → seq 1
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "hello"))
            s.token_hub.flush()
            # finish_part: done:true marker must NOT reach this v4 sub
            s.token_hub.on_part_updated(_text_end(sid, "m1", "p1", "hello"))
            # a subsequent business frame proves the wire continues
            # cleanly after the suppressed marker (no hole, no snapshot)
            s.token_hub.on_part_updated(_text_start(sid, "m2", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m2", "p1", "after"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        _, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)

        # exact v4 sequence: meta → delta(1) → delta(2). The done:true
        # marker never appears between them.
        assert [f[0] for f in frames] == [
            "slimapi.meta", "message.part.delta", "message.part.delta",
        ]
        assert [f[1] for f in frames[1:]] == [
            f"t:{SID}:{EPOCH}:1", f"t:{SID}:{EPOCH}:2",
        ]
        # the snapshot family never entered the ReplayLog nor consumed a
        # seq — the domain carries ONLY the two delta frames.
        assert s.log.domain_frame_count(token_domain(SID)) == 2
        assert s.log.last_seq(token_domain(SID)) == 2
        assert all(b"done" not in json.dumps(f[2]).encode() for f in frames)
    finally:
        await s.close()


async def test_cross_version_pollution_replay_clean(monkeypatch):
    """R2 gate judge-test ④: done:true and truncated:true markers produced
    while ONLY a v3 subscriber is active must never leak into a LATER v4
    cursor reconnect — the replay window contains business frames only.

    Also anchors the v3 side on the same real stack: the v3 subscriber
    still receives both snapshot variants byte-semantically (handshake
    prefill untouched)."""
    monkeypatch.setattr(tokenstream_hub_module, "TOKEN_PART_MAX_BYTES", 4)
    s = _RealStack()
    try:
        # leg 1 — v3 subscriber sees the classic frames (incl. both
        # snapshot variants; v3 semantics byte-identical).
        async def v3_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()  # t:s1:1
            # done:true marker → v3-only fanout
            s.token_hub.on_part_updated(_text_end(sid, "m1", "p1", "a"))
            # truncated:true marker (oversized p2) → v3-only fanout
            s.token_hub.on_part_updated(_text_start(sid, "m2", "p1"))
            s.token_hub.on_part_delta(
                _delta(sid, "m2", "p1", "way over the four byte cap")
            )
            # a business frame AFTER both markers (seq 2)
            s.token_hub.on_part_updated(_text_start(sid, "m3", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m3", "p1", "c"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v3_script)
        _, body_v3 = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=3")
        v3_frames = list(_frames(body_v3))
        v3_events = [f[0] for f in v3_frames]
        # v3 still gets: handshake welcome + deltas + done + truncated.
        assert "server.connected" in v3_events
        assert v3_events.count("message.part.snapshot") == 2
        done = [
            f for f in v3_frames
            if f[0] == "message.part.snapshot" and f[2].get("done") is True
        ]
        trunc = [
            f for f in v3_frames
            if f[0] == "message.part.snapshot"
            and f[2].get("truncated") is True
        ]
        assert len(done) == 1 and "text" not in done[0][2]
        assert len(trunc) == 1
        for block in _blocks(body_v3):
            assert not block.startswith(b"id:")

        # leg 2 — v4 cursor reconnect: the replay window must contain ONLY
        # the two logged deltas; neither snapshot variant is replayed.
        s.token_registry.push_script(_stop_token_stream)
        _, body_v4 = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        v4_frames = list(_frames(body_v4))
        _assert_v4_no_forbidden_frames(v4_frames)
        assert [f[0] for f in v4_frames] == [
            "slimapi.meta", "message.part.delta", "message.part.delta",
        ]
        assert [f[1] for f in v4_frames[1:]] == [
            f"t:{SID}:{EPOCH}:1", f"t:{SID}:{EPOCH}:2",
        ]
        assert v4_frames[1][2]["messageID"] == "m1"
        assert v4_frames[2][2]["messageID"] == "m3"
        # log integrity: only the two deltas were ever logged.
        assert s.log.domain_frame_count(token_domain(SID)) == 2
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
        _assert_v4_no_forbidden_frames(frames)
        assert frames[1] == (
            "resync", None,
            {"reason": "replay_expired", "sessionID": SID},
        )
    finally:
        await s.close()


# ===========================================================================
# §4b — rev-gate R3: the five non-frozen-reason paths. Each test triggers
# the path FOR REAL on the wire and asserts: (a) the v4 wire carries NO
# out-of-domain resync frame (allowlist = production V4_RESYNC_REASONS),
# (b) the v4 connection is TERMINATED (finite body — the disconnect is
# the observable signal), and (c) a v3 connection facing the same trigger
# still receives the legacy resync frame, byte-shape unchanged.
# ===========================================================================


async def test_r3_global_backpressure_v4_terminates_v3_resyncs():
    """Path ① — global stream overflow (hub_types.Subscriber.put).

    v4: STOP only (REPLAY-007 rev-gate semantics — no
    subscriber_backpressure frame outside the frozen domain).
    v3: the frozen ``resync{subscriber_backpressure}`` + STOP pair."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log, queue_items=2, buffer_bytes=1024 * 1024)
    try:
        # v4 leg: 3 publishes overflow the 2-slot queue.
        async def v4_script(hub, sub):
            hub.publish(_q_event(1))
            hub.publish(_q_event(2))
            hub.publish(_q_event(3))

        s.hubs.push_script(v4_script)
        _, body = await _read(s.app, "/slimapi/events?v=4")
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # terminated with NOTHING delivered past meta (queue was cleared)
        assert frames == [("slimapi.meta", None, frames[0][2])]

        # v3 leg: same trigger on the same stack → legacy resync emitted.
        async def v3_script(hub, sub):
            hub.publish(_q_event(4))
            hub.publish(_q_event(5))
            hub.publish(_q_event(6))

        s.hubs.push_script(v3_script)
        _, body = await _read(s.app, "/slimapi/events?v=3")
        v3_frames = list(_frames(body))
        resyncs = [f for f in v3_frames if f[0] == "resync"]
        assert len(resyncs) == 1
        assert resyncs[0][2] == {"reason": "subscriber_backpressure"}
        for block in _blocks(body):
            assert not block.startswith(b"id:")
    finally:
        await s.close()


async def test_r3_token_backpressure_v4_terminates_v3_resyncs():
    """Path ② — token stream overflow (TokenSubscriber.put).

    queue_items=2: the first delta lands in the queue, the second
    overflows. (maxsize=1 would drop the v3 STOP — the overflow branch
    seals resync+STOP as TWO terminal puts, a latent pre-existing
    behaviour at degenerate maxsize; production default is 64.) The
    overflow branch clears the runtime queue (the frozen disconnect
    idiom, REPLAY-007 precedent) — so even the first delta is dropped
    from the wire and lives ONLY in the ReplayLog.
    v4: STOP only. v3: ``resync{subscriber_backpressure, sessionID}``."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log, token_queue_items=2)
    sid2 = "s2"  # v3 leg runs on a fresh domain (v4 leg left m1 pending)
    try:
        async def v4_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()  # seq 1 logged; fills a queue slot
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "b"))
            s.token_hub.flush()  # overflow → v4 termination (queue wiped)
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "c"))
            s.token_hub.flush()  # seq 2 logged (sub closed — not delivered)
            sub.put(TOKEN_STOP)  # safety STOP (dropped — sub is closed)

        s.token_registry.push_script(v4_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # termination wiped the undelivered deltas: meta only, finite
        # body, NO resync frame anywhere on the wire — but the log
        # retained ALL published deltas (published-not-delivered).
        assert frames == [("slimapi.meta", None, frames[0][2])]
        assert log.last_seq(token_domain(SID)) == 3

        async def v3_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "b"))
            s.token_hub.flush()
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "c"))
            s.token_hub.flush()  # overflow → legacy resync + STOP
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v3_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{sid2}/stream?v=3")
        v3_frames = list(_frames(body))
        resyncs = [f for f in v3_frames if f[0] == "resync"]
        assert len(resyncs) == 1
        assert resyncs[0][2] == {
            "reason": "subscriber_backpressure", "sessionID": sid2,
        }
        for block in _blocks(body):
            assert not block.startswith(b"id:")
    finally:
        await s.close()


async def test_r3_token_memory_eviction_v4_terminates_v3_resyncs(monkeypatch):
    """Path ③ — LivePart LRU eviction (_reserve → _evict_part_for_memory →
    _fanout_resync{token_memory_limit}).

    TOKEN_LIVEPARTS_MAX_BYTES=1: part A (1 byte) is resident; part B's
    delta cannot fit → A is evicted → the sid's subscribers get the
    resync. v4: terminated (STOP only). v3: legacy
    ``resync{token_memory_limit, sessionID}``."""
    monkeypatch.setattr(tokenstream_hub_module, "TOKEN_LIVEPARTS_MAX_BYTES", 1)
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    sid2 = "s2"
    try:
        # v4 leg.
        async def v4_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "mA", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "mA", "p1", "a"))
            s.token_hub.flush()  # seq 1 delivered; live gauge = 1 byte
            # part B cannot fit under the 1-byte cap → A evicted →
            # _fanout_resync(sid, token_memory_limit) → v4 terminate.
            s.token_hub.on_part_updated(_text_start(sid, "mB", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "mB", "p1", "b"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v4_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # terminate() clears the runtime queue (existing disconnect
        # idiom): the undelivered delta is dropped from the wire; meta
        # only, finite body, no resync.
        assert frames == [("slimapi.meta", None, frames[0][2])]
        # both deltas survive in the log for replay (the evicted part's
        # and the post-eviction one — published-not-delivered).
        assert log.last_seq(token_domain(SID)) == 2

        # v3 leg on a fresh sid (same monkeypatched cap).
        async def v3_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "mA", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "mA", "p1", "a"))
            s.token_hub.flush()
            s.token_hub.on_part_updated(_text_start(sid, "mB", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "mB", "p1", "b"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v3_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{sid2}/stream?v=3")
        v3_frames = list(_frames(body))
        resyncs = [
            f for f in v3_frames
            if f[0] == "resync" and f[2].get("reason") == "token_memory_limit"
        ]
        assert len(resyncs) == 1
        assert resyncs[0][2]["sessionID"] == sid2
        for block in _blocks(body):
            assert not block.startswith(b"id:")
    finally:
        await s.close()


async def test_r3_session_idle_v4_terminates_v3_resyncs():
    """Path ④ — session.status idle (on_session_status → pending resync
    batch → _fanout_resync{session_idle}).

    v4: terminated (STOP only). v3: ``resync{session_idle, sessionID}``."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    sid2 = "s2"
    try:
        async def v4_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()  # seq 1 delivered
            # upstream says the session went idle → pending resync →
            # flush() drains it → _fanout_resync(session_idle) → v4 stop.
            s.token_hub.on_session_status(sid, "idle")
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v4_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # terminate() wipes the undelivered delta (existing disconnect
        # idiom): meta only, finite body, no resync on the v4 wire.
        assert frames == [("slimapi.meta", None, frames[0][2])]

        # recovery leg (rev-gate R4 rewrite): idle RETIRED the server-side
        # part state, so the token domain carries a barrier — a cursor-0
        # reconnect must get resync{reconnect_no_replay} (frozen reason) →
        # HTTP alignment, NOT a replay of the invalidated part's deltas
        # (the R3-era replay assertion was exactly the R4 BLOCKER-1 trap).
        s.token_registry.push_script(_stop_token_stream)
        _, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        assert [f[0] for f in frames] == ["slimapi.meta", "resync"]
        assert frames[1] == (
            "resync", None, {"reason": "reconnect_no_replay", "sessionID": SID},
        )
        # the barrier is durable in log state (source-level write).
        assert log.barrier_watermark(token_domain(SID)) == 1

        # v3 leg: same trigger on a fresh sid → legacy resync delivered.
        async def v3_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()
            s.token_hub.on_session_status(sid, "idle")
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v3_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{sid2}/stream?v=3")
        v3_frames = list(_frames(body))
        resyncs = [
            f for f in v3_frames
            if f[0] == "resync" and f[2].get("reason") == "session_idle"
        ]
        assert len(resyncs) == 1
        assert resyncs[0][2]["sessionID"] == sid2
        for block in _blocks(body):
            assert not block.startswith(b"id:")
    finally:
        await s.close()


async def test_r3_session_deleted_v4_terminates_v3_resyncs():
    """Path ⑤ — session.deleted (on_session_deleted → terminate).

    v4: STOP only — the deletion semantics reach the client via the
    GLOBAL session.digest control plane, never via a token-stream resync.
    v3: ``resync{session_deleted, sessionID}`` → STOP (INV-4 pair)."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    sid2 = "s2"  # v3 leg MUST use another sid: on_session_deleted arms
    # the deleted-sid gate (late part events for the sid are dropped).
    try:
        async def v4_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()  # seq 1 delivered
            s.token_hub.on_session_deleted(sid)  # terminate({session_deleted})
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v4_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # terminate() wipes the undelivered delta (existing disconnect
        # idiom): meta only, finite body, no resync on the v4 wire.
        assert frames == [("slimapi.meta", None, frames[0][2])]
        # the deletion's token-stream state is gone, but the frame lives
        # on in the log (deletion semantics reach clients via the global
        # session.digest control plane, not this stream).
        assert log.last_seq(token_domain(SID)) == 1

        async def v3_script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()
            s.token_hub.on_session_deleted(sid)
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(v3_script)
        _, body = await _read(s.app, f"/slimapi/sessions/{sid2}/stream?v=3")
        v3_frames = list(_frames(body))
        resyncs = [
            f for f in v3_frames
            if f[0] == "resync" and f[2].get("reason") == "session_deleted"
        ]
        assert len(resyncs) == 1
        assert resyncs[0][2]["sessionID"] == sid2
        for block in _blocks(body):
            assert not block.startswith(b"id:")
    finally:
        await s.close()


# ===========================================================================
# §4c — rev-gate R4: state-invalidation sources write replay barriers.
# The danger scenario: the client ACTUALLY consumed seq N (the frame hit
# the wire), the server then invalidated the accumulator state (idle
# retire / memory eviction / deletion), and the client reconnects with
# cursor N — which equals last_seq and would be judged up-to-date without
# the barrier, parking the client in live mode on a残缺 part.
# ===========================================================================


async def test_r4_idle_after_real_consumption_barrier_resync():
    """Judge condition 4 (idle variant) + condition 5 (offline write).

    帧序实录：conn1 送达 delta(id t:s1:E:1) → 离线（零在线订阅者）idle
    退役 → conn2 cursor 1 → [meta, resync{reconnect_no_replay}]，绝不判
    up-to-date、绝不重放已失效 part 的 delta。"""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        # leg 1 — the frame is REALLY delivered to the wire (asserted).
        async def leg1(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(leg1)
        _, body1 = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames1 = list(_frames(body1))
        _assert_v4_no_forbidden_frames(frames1)
        assert [f[0] for f in frames1] == ["slimapi.meta", "message.part.delta"]
        assert frames1[1][1] == f"t:{SID}:{EPOCH}:1"  # consumed cursor N=1

        # offline invalidation — condition 5: the barrier is written at the
        # SOURCE with ZERO online subscribers.
        assert not s.token_hub._subs_by_sid.get(SID)
        s.token_hub.on_session_status(SID, "idle")
        assert log.barrier_watermark(token_domain(SID)) == 1

        # leg 2 — reconnect with cursor == last_seq == watermark: the
        # up-to-date trap. Must resync via the frozen reason.
        s.token_registry.push_script(_stop_token_stream)
        _, body2 = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:1"},
        )
        frames2 = list(_frames(body2))
        _assert_v4_no_forbidden_frames(frames2)
        assert [f[0] for f in frames2] == ["slimapi.meta", "resync"]
        assert frames2[1] == (
            "resync", None, {"reason": "reconnect_no_replay", "sessionID": SID},
        )
        # no replay frames crept in (the invalidated part must not resume).
        assert all(f[1] is None for f in frames2)
    finally:
        await s.close()


async def test_r4_memory_eviction_after_real_consumption_barrier(monkeypatch):
    """Judge condition 4 (eviction variant) + the precise boundary.

    conn1 送达 A 的 delta(id 1) → 离线 LRU 逐出 A（B 进来，cap=1）→
    barrier watermark=1（A 的 delta 在 flush_sid 后落下）→ conn2 cursor 1
    → resync；conn3 cursor 2（B 的 delta，B 状态完好）→ 正常 up-to-date
    ——barrier 不越过失效边界。"""
    monkeypatch.setattr(tokenstream_hub_module, "TOKEN_LIVEPARTS_MAX_BYTES", 1)
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        async def leg1(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "mA", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "mA", "p1", "a"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(leg1)
        _, body1 = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames1 = list(_frames(body1))
        _assert_v4_no_forbidden_frames(frames1)
        assert frames1[1][1] == f"t:{SID}:{EPOCH}:1"  # consumed A's delta

        # offline eviction: B cannot fit under the 1-byte cap → A evicted
        # (barrier, source-level, zero subscribers) → B's delta seq 2.
        assert not s.token_hub._subs_by_sid.get(SID)
        s.token_hub.on_part_updated(_text_start(SID, "mB", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "mB", "p1", "b"))
        s.token_hub.flush()
        assert log.barrier_watermark(token_domain(SID)) == 1
        assert log.last_seq(token_domain(SID)) == 2

        # cursor 1 (≤ watermark, A's state gone) → frozen-reason resync.
        s.token_registry.push_script(_stop_token_stream)
        _, body2 = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:1"},
        )
        frames2 = list(_frames(body2))
        _assert_v4_no_forbidden_frames(frames2)
        assert [f[0] for f in frames2] == ["slimapi.meta", "resync"]
        assert frames2[1][2] == {
            "reason": "reconnect_no_replay", "sessionID": SID,
        }

        # cursor 2 (> watermark — B's state is INTACT server-side): no
        # resync, no frames (up-to-date). The barrier did not over-block
        # post-invalidation frames.
        s.token_registry.push_script(_stop_token_stream)
        _, body3 = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:2"},
        )
        frames3 = list(_frames(body3))
        _assert_v4_no_forbidden_frames(frames3)
        assert [f[0] for f in frames3] == ["slimapi.meta"]
    finally:
        await s.close()


async def test_r4_deleted_session_barrier_prevents_resurrection():
    """Deleted sessions: cursor < last_seq must NOT replay the dead
    session's deltas (resurrection); the barrier routes it to the frozen
    resync → HTTP alignment (global session.digest already carries the
    deletion semantics for clients subscribed to both streams)."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        async def leg1(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "a"))
            s.token_hub.flush()
            s.token_hub.on_session_deleted(sid)
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(leg1)
        _, body1 = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames1 = list(_frames(body1))
        _assert_v4_no_forbidden_frames(frames1)
        assert frames1 == [("slimapi.meta", None, frames1[0][2])]
        assert log.last_seq(token_domain(SID)) == 1
        assert log.barrier_watermark(token_domain(SID)) == 1

        # reconnect with cursor 0 (< last_seq): without the barrier this
        # REPLAYS the deleted session's delta — resurrection. With it:
        # frozen resync only.
        s.token_registry.push_script(_stop_token_stream)
        _, body2 = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        frames2 = list(_frames(body2))
        _assert_v4_no_forbidden_frames(frames2)
        assert [f[0] for f in frames2] == ["slimapi.meta", "resync"]
        assert frames2[1][2] == {
            "reason": "reconnect_no_replay", "sessionID": SID,
        }
    finally:
        await s.close()


async def test_r4_token_backpressure_no_barrier_replay_recovers():
    """Judge condition 6: backpressure termination is NOT state
    invalidation — no barrier is written, and the reconnect replays the
    overflowed frames from the log (REPLAY-007 published-not-delivered)."""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log, token_queue_items=2)
    try:
        async def overflow(sub, sid):
            for text in ("aa", "bb", "cc", "dd"):
                s.token_hub.on_part_updated(_text_start(sid, "m1", "p1"))
                s.token_hub.on_part_delta(_delta(sid, "m1", "p1", text))
                s.token_hub.flush()
            # no explicit STOP: the overflow's own STOP terminates.

        s.token_registry.push_script(overflow)
        _, body1 = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        frames1 = list(_frames(body1))
        _assert_v4_no_forbidden_frames(frames1)
        # v4 overflow = silent STOP (R3): whatever fit stays delivered;
        # crucially NO resync on the wire.
        assert all(f[0] != "resync" for f in frames1)

        # EXPLICIT no-barrier assertion (judge condition 6).
        assert log.barrier_watermark(token_domain(SID)) is None
        assert log.last_seq(token_domain(SID)) == 4  # all four logged

        # reconnect replays the missed frames — NOT a resync.
        s.token_registry.push_script(_stop_token_stream)
        _, body2 = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        frames2 = list(_frames(body2))
        _assert_v4_no_forbidden_frames(frames2)
        assert [f[0] for f in frames2] == [
            "slimapi.meta",
            "message.part.delta", "message.part.delta",
            "message.part.delta", "message.part.delta",
        ]
        assert [f[1] for f in frames2[1:]] == [
            f"t:{SID}:{EPOCH}:{n}" for n in (1, 2, 3, 4)
        ]
    finally:
        await s.close()


def test_v4_resync_reasons_frozen_literal_set():
    """MINOR-1: independent literal-set anchor. Every other oracle imports
    the production constant (same-source), so a future fifth value would
    silently pass them — THIS assertion fails instead. The four-value
    domain is frozen by v4-contract:191/208 + design:19/216."""
    assert V4_RESYNC_REASONS == {
        "epoch_changed", "replay_expired", "replay_gap", "reconnect_no_replay",
    }


# ===========================================================================
# §4b — rev-gate BLOCKER-1: v4 握手旁路修复的 wire 证据
# ===========================================================================

_CONTROL_EVENTS = frozenset({"slimapi.meta", "resync", "server.heartbeat"})
# rev-gate R2 BLOCKER-1 oracle：v4 流上禁止出现的事件——server.connected
# （R1 裁决抑制）与 message.part.snapshot 族（done:false 预填 / done:true
# 终态 marker / truncated:true 截断 marker，冻结契约：v4 服务端永不发
# snapshot 帧）。
_FORBIDDEN_V4_EVENTS = frozenset({"server.connected", "message.part.snapshot"})
_GLOBAL_ID_RE = re.compile(rf"^g:{EPOCH}:(\d+)$")
_TOKEN_ID_RE = re.compile(rf"^t:{re.escape(SID)}:{EPOCH}:(\d+)$")


def _assert_v4_no_forbidden_frames(frames):
    """禁止事件集 + 冻结 reason 值域断言（rev-gate R2+R3 BLOCKER-1）：
    v4 全流遍历永不出现 server.connected 或 message.part.snapshot
    （任何变体：done/truncated/handshake）；且每个出现的 resync 帧的
    data.reason ∈ 生产侧 V4_RESYNC_REASONS（import 自 replay_wire——
    与五条路径的 v4 分支共用同一常量，非测试私有副本）。"""
    for event, frame_id, _data in frames:
        assert event not in _FORBIDDEN_V4_EVENTS, (event, frame_id, _data)
        if event == "resync":
            reason = _data.get("reason")
            assert reason in V4_RESYNC_REASONS, (reason, frame_id, _data)


def _assert_v4_id_invariant(frames, domain, *, sid=SID):
    """通用 invariant（评委通过条件 4）：除裁决控制帧（meta/resync/
    heartbeat）外，v4 流上全部业务/token 帧必须携带正确域的 id 且 seq
    严格递增；控制帧必须无 id；且全流永不出现禁止事件集
    （server.connected / message.part.snapshot——rev-gate R2 BLOCKER-1）。"""
    _assert_v4_no_forbidden_frames(frames)
    pattern = _GLOBAL_ID_RE if domain == GLOBAL_DOMAIN else _TOKEN_ID_RE
    prev = 0
    for event, frame_id, _data in frames:
        if event in _CONTROL_EVENTS:
            assert frame_id is None, (event, frame_id)
            continue
        assert frame_id is not None, (event, frame_id)
        match = pattern.match(frame_id)
        assert match, (frame_id, domain)
        seq = int(match.group(1))
        assert seq > prev, (frame_id, prev)
        prev = seq


async def test_v4_stream_live_part_first_connect_no_snapshot():
    """live part 三态之一（首连）：v4 不预发 message.part.snapshot，也
    不预发首连前的历史帧——meta 之后直接是 live delta（带 id）。"""
    s = _RealStack()
    try:
        # part 处于 live 状态且已发布过一帧（seq 1，客户端未连）
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()

        async def script(sub, sid):
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "y"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        response, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        assert response.status_code == 200
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # 首连 = meta + live：无 cursor 不重放（seq 1 不出现），无 snapshot
        assert [f[0] for f in frames] == ["slimapi.meta", "message.part.delta"]
        assert frames[1][1] == f"t:{SID}:{EPOCH}:2"
        assert all(f[0] != "message.part.snapshot" for f in frames)
        assert all(f[0] != "server.connected" for f in frames)
        _assert_v4_id_invariant(frames, token_domain(SID))
    finally:
        await s.close()


async def test_v4_stream_live_part_window_replay_no_snapshot():
    """live part 三态之二（窗口 replay）：带 cursor 重连只从日志带 id
    回放，绝不以服务端 snapshot 帧对齐 live part 状态。"""
    s = _RealStack()
    try:
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()  # seq 1

        async def script(sub, sid):
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "y"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        _, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        # replay(seq 1) + live(seq 2)，全部带 id，无任何 snapshot/welcome
        assert [f[0] for f in frames] == [
            "slimapi.meta", "message.part.delta", "message.part.delta",
        ]
        assert [f[1] for f in frames[1:]] == [
            f"t:{SID}:{EPOCH}:1", f"t:{SID}:{EPOCH}:2",
        ]
        assert all(f[0] != "message.part.snapshot" for f in frames)
        assert all(f[0] != "server.connected" for f in frames)
        _assert_v4_id_invariant(frames, token_domain(SID))
    finally:
        await s.close()


async def test_v4_stream_live_part_resync_no_snapshot():
    """live part 三态之三（resync）：窗口过期 → resync 帧后 live 帧继续，
    仍无服务端 snapshot（协议：resync 后客户端走 HTTP 全量对齐）。"""
    log = ReplayLog(epoch=EPOCH, ttl_s=0.05, clock=lambda: 1000.0)
    s = _RealStack(log=log)
    try:
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()  # seq 1 @1000.0
        s.log._clock = lambda: 2000.0  # 越过 TTL

        async def script(sub, sid):
            s.token_hub.on_part_delta(_delta(sid, "m1", "p1", "y"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        _, body = await _read(
            s.app, f"/slimapi/sessions/{SID}/stream?v=4",
            headers={"Last-Event-ID": f"t:{SID}:{EPOCH}:0"},
        )
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        assert frames[0][0] == "slimapi.meta"
        assert frames[1] == (
            "resync", None,
            {"reason": "replay_expired", "sessionID": SID},
        )
        # resync 后 live 帧继续带 id 下发
        assert frames[2][0] == "message.part.delta"
        assert frames[2][1] == f"t:{SID}:{EPOCH}:2"
        assert all(f[0] != "message.part.snapshot" for f in frames)
        _assert_v4_id_invariant(frames, token_domain(SID))
    finally:
        await s.close()


async def test_v4_id_invariant_global_events_stream():
    """通用 invariant（条件 4）在 /events v4 全帧遍历上成立：含
    replay + live 混合、无事件名 IMMEDIATE 帧。"""
    log = ReplayLog(epoch=EPOCH)
    s = _RealStack(log=log)
    try:
        s.publish(_q_event(1), _q_event(2))  # 离线发布
        s.hubs.push_script(_script_publish_global(_q_event(3)))
        _, body = await _read(
            s.app, "/slimapi/events?v=4",
            headers={"Last-Event-ID": f"g:{EPOCH}:0"},
        )
        frames = list(_frames(body))
        _assert_v4_id_invariant(frames, GLOBAL_DOMAIN)
        # replay 1,2 + live 3 全部带 id（IMMEDIATE 帧无 event 名 → 业务帧）
        assert [f[1] for f in frames[1:]] == [
            f"g:{EPOCH}:{n}" for n in (1, 2, 3)
        ]
    finally:
        await s.close()


async def test_v4_no_old_tombstone_prefill_on_first_connect():
    """评委通过条件 1（v4 不预发旧 handshake tombstone）：历史
    message.removed 只进 ReplayLog；v4 首连（无 cursor）绝不重放它。"""
    s = _RealStack()
    try:
        # 历史流量：delta + tombstone（无订阅者时发布）
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()
        s.token_hub.on_message_removed(SID, "m1")
        assert s.log.domain_frame_count(token_domain(SID)) == 2

        # v4 首连：只有 meta + live delta（新 part）——旧 tombstone 不发
        async def script(sub, sid):
            s.token_hub.on_part_updated(_text_start(sid, "m2", "p1"))
            s.token_hub.on_part_delta(_delta(sid, "m2", "p1", "y"))
            s.token_hub.flush()
            sub.put(TOKEN_STOP)

        s.token_registry.push_script(script)
        response, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=4")
        assert response.status_code == 200
        frames = list(_frames(body))
        _assert_v4_no_forbidden_frames(frames)
        assert [f[0] for f in frames] == ["slimapi.meta", "message.part.delta"]
        assert all(f[0] != "message.removed" for f in frames)
        assert all(f[0] != "message.part.snapshot" for f in frames)
        assert all(f[0] != "server.connected" for f in frames)
    finally:
        await s.close()


async def test_v3_stream_handshake_prefill_unchanged():
    """v3 握手路径逐字节不变（条件 8 的 real-stack 面）：server.connected
    预填 + live part snapshot 预填照旧存在（v4 抑制不外溢）。"""
    s = _RealStack()
    try:
        # part live 状态 + 历史 tombstone 都在
        s.token_hub.on_part_updated(_text_start(SID, "m1", "p1"))
        s.token_hub.on_part_delta(_delta(SID, "m1", "p1", "x"))
        s.token_hub.flush()
        s.token_hub.on_message_removed(SID, "m2")

        s.token_registry.push_script(_stop_token_stream)
        response, body = await _read(s.app, f"/slimapi/sessions/{SID}/stream?v=3")
        assert response.status_code == 200
        frames = list(_frames(body))
        assert frames[0][0] == "slimapi.meta"
        assert set(frames[0][2].keys()) == {"subscriberId", "tokens"}
        names = [f[0] for f in frames]
        # v3 handshake 预填序列完整保留：connected → tombstone(m2) →
        # snapshot(m1 live part, done=false)
        assert names[1] == "server.connected"
        assert names[2] == "message.removed"
        assert "message.part.snapshot" in names
        removed = [f for f in frames if f[0] == "message.removed"]
        assert len(removed) == 1
        assert removed[0][2] == {"sessionID": SID, "messageID": "m2"}
        snapshot = [f for f in frames if f[0] == "message.part.snapshot"]
        assert snapshot and snapshot[0][2]["done"] is False
        for block in _blocks(body):
            assert not block.startswith(b"id:")
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
            _assert_v4_no_forbidden_frames(frames)
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
        _assert_v4_no_forbidden_frames(frames)
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
    _assert_v4_no_forbidden_frames(frames)
    assert frames[0][2].keys() == {"subscriberId", "tokens"}
    assert frames[1] == ("resync", None, {"reason": "reconnect_no_replay"})
    for block in _blocks(body):
        assert not block.startswith(b"id:")
