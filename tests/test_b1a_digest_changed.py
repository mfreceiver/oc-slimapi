"""B1a: ``session.digest`` frames carry a minimal ``changed`` field.

Settled semantics (frozen — do not expand or re-interpret):
  * Digest frames are produced per-sid: ``flush_sid()`` emits one immediate
    frame for a single sid; ``flush()`` emits one frame per pending entry.
  * Minimal ``changed`` semantics: a frame appearing means that sid changed
    → every digest frame carries ``changed: [<this frame's sid>]``.
  * The array shape ``[sid…]`` is kept for future aggregation; the sidecar
    adds ZERO new state — no new dict/list cache or tracking structure.
    ``changed`` is constructed at flush time directly from the frame's own
    sid (a fresh list per frame).

Locked here:
  1. status event driving ``flush_sid`` (busy-clear path) → payload has
     ``changed: [sid]``.
  2. batch ``flush`` → every frame carries its OWN ``changed`` (per-sid).
  3. per-frame ``changed`` value equals the frame's ``sessionID`` (and is a
     fresh list — no shared/cached identity across frames).
  4. q/p IMMEDIATE direct-push frames (question.* / permission.*) carry NO
     ``changed`` (direct-push frames are zero-changed).
  5. resync frames and heartbeat frames carry NO ``changed``.
  6. sticky lastError merge and ``changed`` coexist correctly in one frame.

Self-contained: own helpers + fixtures; does NOT touch tests/conftest.py.
"""

from __future__ import annotations

from conftest import current_replay_log

import asyncio
import json

import pytest

from oc_slimapi.sse.hub import STOP, GlobalHub, Subscriber, sse_frame


def ev(
    directory: str | None,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties}}."""
    return {
        "directory": directory,
        "payload": {"type": event_type, "properties": properties or {}},
    }


def parse(raw: bytes) -> tuple[str | None, dict]:
    """Parse one SSE frame into (event_name, data). event_name is None when
    the frame has no ``event:`` header (raw passthrough like question.asked)."""
    event_name: str | None = None
    data_lines: list[str] = []
    for line in raw.decode().split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return event_name, data


async def drain(sub: Subscriber, timeout: float = 0.2) -> list[bytes]:
    """Drain every currently-queued frame without blocking on an empty queue."""
    out: list[bytes] = []
    while True:
        try:
            item = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if item is STOP:
            continue
        if isinstance(item, (bytes, bytearray)):
            out.append(bytes(item))
    return out


async def _teardown_hub(hub: GlobalHub) -> None:
    """Cancel + await every background task the hub started."""
    me = asyncio.current_task()
    tasks = [
        t for t in asyncio.all_tasks()
        if t is not me and not t.done()
    ]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def hub():
    """Bare GlobalHub(client=None); teardown cancels all background tasks."""
    h = GlobalHub(client=None, replay_log=current_replay_log())
    try:
        yield h
    finally:
        await _teardown_hub(h)


@pytest.fixture
async def pair(hub: GlobalHub):
    """GlobalHub with one manually-attached subscriber (no run() side effects)."""
    sub = Subscriber()
    hub.subscribers.add(sub)
    return hub, sub


def only_digests(frames: list[bytes]) -> list[dict]:
    return [d for e, d in (parse(f) for f in frames) if e == "session.digest"]


# ---------------------------------------------------------------------------
# Case 1: status event driving flush_sid (busy-clear) → changed: [sid]
# ---------------------------------------------------------------------------


async def test_flush_sid_busy_clear_digest_carries_changed(pair):
    """A status event that triggers the immediate per-sid ``flush_sid`` path
    (busy-clear of sticky lastError) must produce a digest with
    ``changed: [sid]``."""
    hub, sub = pair
    # Seed sticky lastError via G1-A session.error (immediate digest).
    hub.publish(ev("/p", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    await drain(sub)  # consume the immediate G1-A digest

    # session.status busy → busy-clear path → flush_sid("s1") immediate frame.
    hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "busy"}))
    frames = await drain(sub)
    digests = only_digests(frames)
    clear_digests = [
        d for d in digests
        if d.get("sessionID") == "s1" and "lastError" in d
    ]
    assert clear_digests, "expected the busy-clear digest carrying lastError"
    for d in clear_digests:
        assert d["lastError"] is None
        assert d["changed"] == ["s1"]


# ---------------------------------------------------------------------------
# Case 2: batch flush → every frame carries its OWN changed (per-sid)
# ---------------------------------------------------------------------------


async def test_batch_flush_each_digest_carries_own_changed(pair):
    """``flush()`` emits one digest per pending entry and each frame's
    ``changed`` list holds ONLY that frame's sid."""
    hub, sub = pair
    for sid, status in [("s1", "busy"), ("s2", "idle"), ("s3", "completed")]:
        hub.publish(ev("/p", "session.status", {"sessionID": sid, "status": status}))
    hub.flush()

    digests = only_digests(await drain(sub))
    assert len(digests) == 3
    by_sid = {d["sessionID"]: d for d in digests}
    assert by_sid["s1"]["changed"] == ["s1"]
    assert by_sid["s2"]["changed"] == ["s2"]
    assert by_sid["s3"]["changed"] == ["s3"]
    # The per-frame sid list must never bleed across frames (batch flush
    # would otherwise aggregate — that is future work, not this change).
    assert set(d["changed"][0] for d in digests) == {"s1", "s2", "s3"}


# ---------------------------------------------------------------------------
# Case 3: changed value == sessionID value; fresh list per frame
# ---------------------------------------------------------------------------


async def test_changed_value_equals_session_id(pair):
    """B1a minimal semantics: ``changed: [<this frame's sid>]`` — the array
    element is exactly the same string as the frame's ``sessionID``."""
    hub, sub = pair
    for sid, status in [("s1", "busy"), ("s2", "idle")]:
        hub.publish(ev("/p", "session.status", {"sessionID": sid, "status": status}))
    hub.flush()

    digests = only_digests(await drain(sub))
    assert len(digests) == 2
    for d in digests:
        assert d["changed"] == [d["sessionID"]]
    # Zero new state: each frame's changed list is constructed fresh at flush
    # time — no shared list object is cached/reused across frames.
    d1, d2 = digests
    assert d1["changed"] is not d2["changed"]


# ---------------------------------------------------------------------------
# Case 4: q/p IMMEDIATE direct-push frames carry NO changed
# ---------------------------------------------------------------------------


async def test_immediate_push_frames_have_no_changed(pair):
    """question.* / permission.* are raw passthrough frames — they must be
    byte-identical in shape to before (no ``changed``, no extra keys)."""
    hub, sub = pair
    hub.publish(ev("/p", "question.asked", {"id": "q1", "sessionID": "s1"}))
    hub.publish(ev("/p", "permission.asked", {"id": "p1", "sessionID": "s2"}))
    frames = await drain(sub, timeout=0.1)
    assert len(frames) == 2
    for raw in frames:
        ev_name, data = parse(raw)
        assert ev_name is None  # raw passthrough — no event header
        assert "changed" not in data
    # Shape is untouched.
    _, q = parse(frames[0])
    assert q == {
        "directory": "/p",
        "type": "question.asked",
        "properties": {"id": "q1", "sessionID": "s1"},
    }


# ---------------------------------------------------------------------------
# Case 5: resync + heartbeat frames carry NO changed
# ---------------------------------------------------------------------------


async def test_resync_frame_has_no_changed(pair):
    """``resync_all()`` emits the unchanged reconnect_no_replay resync frame —
    no ``changed`` key."""
    hub, sub = pair
    hub.resync_all()
    frames = await drain(sub, timeout=0.1)
    resyncs = [(e, d) for e, d in (parse(f) for f in frames) if e == "resync"]
    assert len(resyncs) == 1
    _, data = resyncs[0]
    assert data == {"reason": "reconnect_no_replay"}
    assert "changed" not in data


async def test_heartbeat_frame_has_no_changed(pair):
    """Heartbeat frames are an empty-payload ``server.heartbeat`` —
    structurally cannot carry ``changed``. Lock the exact frame shape the
    heartbeat loop emits."""
    hub, sub = pair
    # Same construction heartbeat_loop() uses (empty payload event).
    sub.put(sse_frame({}, event="server.heartbeat"))
    frames = await drain(sub, timeout=0.1)
    assert len(frames) == 1
    ev_name, data = parse(frames[0])
    assert ev_name == "server.heartbeat"
    assert data == {}
    assert "changed" not in data


# ---------------------------------------------------------------------------
# Case 6: sticky lastError merge + changed coexist correctly
# ---------------------------------------------------------------------------


async def test_sticky_last_error_merge_coexists_with_changed(pair):
    """G1-A immediate digest (session.error) AND the sticky merge in a later
    debounce window must both carry ``changed`` alongside ``lastError``."""
    hub, sub = pair

    # G1-A: session.error → immediate flush_sid digest: lastError + changed.
    hub.publish(ev("/p", "session.error", {
        "sessionID": "s1",
        "error": {"name": "UnknownError", "data": {"message": "boom"}},
    }))
    immediate = only_digests(await drain(sub))
    assert len(immediate) == 1
    assert immediate[0]["sessionID"] == "s1"
    assert immediate[0]["lastError"]["name"] == "UnknownError"
    assert immediate[0]["changed"] == ["s1"]

    # New debounce window: a non-error status event merges the sticky
    # lastError into the digest — changed must coexist in the same frame.
    hub.publish(ev("/p", "session.status", {"sessionID": "s1", "status": "idle"}))
    hub.flush()
    merged = only_digests(await drain(sub))
    assert len(merged) == 1
    assert merged[0]["lastError"]["name"] == "UnknownError"
    assert merged[0]["lastError"]["message"] == "boom"
    assert merged[0]["changed"] == ["s1"]
    # The sessionID of the frame is the same sid that changed.
    assert merged[0]["changed"] == [merged[0]["sessionID"]]
