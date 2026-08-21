"""Bug A regression: upstream ``session.status`` object envelope handling.

Upstream opencode ``/global/event`` (captured 2026-08-19) emits
``session.status`` with ``properties.status`` as an **object** —
``{"type": "busy"}`` — while the sidecar historically compared it as a
plain string. Consequences (root-caused on the live wire):

* ``global_hub.py`` G1 sticky-clear guard compared ``props.get("status")
  == "busy"`` → always False for the object envelope → sticky lastError
  never cleared (ocdroid banner stuck forever, revived on every digest
  flush after cold start — Bug B amplification).
* ``entry.status`` (digest ``status`` field) was only filled for
  ``isinstance(status, str)`` → never filled for the object envelope.
* The token-hub mirror branch had the same ``isinstance(str)`` guard →
  ``_session_status`` never updated from the object envelope.

These tests lock the normalized behavior: a single helper accepts BOTH
the legacy string (``"busy"``) and the object envelope
(``{"type": "busy"}``); an object without a string ``type`` carries no
valid status (ignored, never a crash).

Self-contained helpers (house style — do NOT depend on tests/conftest.py
or other test modules; cf. test_hub_behavior_lock.py header note).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from oc_slimapi.sse.hub import GlobalHub, STOP, Subscriber
from oc_slimapi.sse.tokenstream.hub import TokenStreamHub


# ---------------------------------------------------------------------------
# Self-contained helpers
# ---------------------------------------------------------------------------

def ev(
    directory: str | None,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties}}."""
    return {"directory": directory, "payload": {"type": event_type, "properties": properties or {}}}


def parse(raw: bytes) -> tuple[str | None, dict]:
    """Parse one SSE frame into (event_name, data)."""
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


def digests_for(frames: list[bytes], sid: str) -> list[dict]:
    return [
        d for e, d in (parse(f) for f in frames)
        if e == "session.digest" and d.get("sessionID") == sid
    ]


async def seed_sticky(hub: GlobalHub, sub: Subscriber, sid: str = "s1") -> None:
    """Publish one session.error so ``sticky_last_error[sid]`` is populated,
    then drain the immediate G1-A digest it flushes."""
    hub.publish(ev("/proj", "session.error", {
        "sessionID": sid,
        "error": {"name": "UsageLimitError", "data": {"message": "quota"}},
    }))
    await drain(sub)
    assert sid in hub.sticky_last_error


@pytest.fixture
async def pair():
    """GlobalHub(client=None) with one manually-attached subscriber; teardown
    cancels all background tasks."""
    hub = GlobalHub(client=None)
    try:
        sub = Subscriber()
        hub.subscribers.add(sub)
        yield hub, sub
    finally:
        me = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ===========================================================================
# Requirement 1 — object envelope {"type": "busy"} pops sticky + explicit
# null clear frame (G1 contract restored)
# ===========================================================================

class TestObjectBusyClearsSticky:
    async def test_object_busy_pops_sticky_and_emits_null_clear(self, pair):
        """REQ 1: {"type":"busy"} + sticky → sticky popped + digest
        lastError:null (explicit clear frame) via flush_sid."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "busy"},
        }))
        frames = await drain(sub)

        assert "s1" not in hub.sticky_last_error, "sticky must be popped on object busy"
        cleared = [d for d in digests_for(frames, "s1") if "lastError" in d]
        assert any(d["lastError"] is None for d in cleared), (
            "expected an explicit lastError:null clear digest"
        )
        # The clear digest also carries the normalized status fill (REQ 2 fill).
        assert any(d.get("status") == "busy" for d in cleared)

    async def test_object_busy_clear_preserves_other_pending(self, pair):
        """flush_sid scope: a busy-clear for s1 must not flush another sid's
        pending digest (existing G1 contract, object format)."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")
        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s2", "status": {"type": "idle"},
        }))

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "busy"},
        }))
        frames = await drain(sub)
        # s2's pending digest stays in the debounce window.
        assert not any(e == "session.digest" and d.get("sessionID") == "s2"
                       for e, d in (parse(f) for f in frames))
        assert "s2" in hub.pending


# ===========================================================================
# Requirement 2 — legacy string format regression: behavior unchanged
# ===========================================================================

class TestStringBusyRegression:
    async def test_string_busy_pops_sticky_and_emits_null_clear(self, pair):
        """REQ 2: "busy" (string) → identical behavior (regression lock)."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        frames = await drain(sub)

        assert "s1" not in hub.sticky_last_error
        cleared = [d for d in digests_for(frames, "s1") if "lastError" in d]
        assert any(d["lastError"] is None for d in cleared)
        assert any(d.get("status") == "busy" for d in cleared)

    async def test_string_idle_fills_status_no_pop(self, pair):
        """REQ 3 (string side): "idle" fills entry.status, does NOT pop sticky."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": "idle",
        }))
        hub.flush()
        frames = await drain(sub)

        assert "s1" in hub.sticky_last_error, "idle must not pop sticky"
        ds = digests_for(frames, "s1")
        assert ds and ds[0].get("status") == "idle"
        # No explicit clear frame on idle.
        assert all("lastError" not in d or d["lastError"] is not None for d in ds)


# ===========================================================================
# Requirement 3 — object envelope fills digest status for all state values
# ===========================================================================

class TestObjectStatusFill:
    async def test_object_idle_fills_status_and_keeps_sticky(self, pair):
        """REQ 3: {"type":"idle"} → entry.status filled ("idle"), sticky kept,
        merged sticky lastError still present in the flush digest."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "idle"},
        }))
        hub.flush()
        frames = await drain(sub)

        assert "s1" in hub.sticky_last_error
        ds = digests_for(frames, "s1")
        assert ds, "expected a digest for the object idle status"
        assert ds[0].get("status") == "idle", "digest.status must be filled from the object envelope"
        # Sticky merge (Bug B amplifier, by design until cleared by busy):
        assert ds[0].get("lastError", {}).get("name") == "UsageLimitError"

    async def test_object_other_state_value_filled(self, pair):
        """REQ 3: any other state value (e.g. "shared") flows through with the
        same value domain as the string format."""
        hub, sub = pair
        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "shared"},
        }))
        hub.flush()
        frames = await drain(sub)
        ds = digests_for(frames, "s1")
        assert ds and ds[0].get("status") == "shared"


# ===========================================================================
# Requirement 4 — object without a string type: ignored, no crash, other
# digest fields unaffected
# ===========================================================================

class TestObjectMalformedType:
    @pytest.mark.parametrize("bad_status", [
        {},                      # missing type
        {"type": 123},           # non-string type
        {"type": None},          # explicit null type
        {"kind": "busy"},        # wrong key entirely
        42,                      # non-dict, non-string junk
    ])
    async def test_bad_status_shape_ignored_no_crash(self, pair, bad_status):
        """REQ 4: invalid status shapes are ignored (no valid status), no
        crash, and the digest still carries the event's other fields."""
        hub, sub = pair
        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": bad_status,
        }))
        hub.flush()  # must not raise
        frames = await drain(sub)
        ds = digests_for(frames, "s1")
        assert ds, "digest still emitted"
        assert "status" not in ds[0], "no valid status → status key omitted"
        assert ds[0].get("sessionID") == "s1"

    async def test_bad_status_does_not_pop_sticky_or_clear(self, pair):
        """REQ 4 (sticky side): an invalid status shape must not trigger the
        busy clear path nor disturb the sticky entry."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {},
        }))
        hub.flush()
        frames = await drain(sub)
        assert "s1" in hub.sticky_last_error
        ds = digests_for(frames, "s1")
        assert all("lastError" not in d or d["lastError"] is not None for d in ds)


# ===========================================================================
# Requirement 5 — Bug B path: after a busy clear, later flushes must NOT
# re-attach the old lastError
# ===========================================================================

class TestNoRebirthAfterClear:
    async def test_message_updated_after_busy_clear_omits_last_error(self, pair):
        """REQ 5: error → busy(object) clear → later message.updated digest
        carries NO lastError (sticky was popped; flush merge finds nothing)."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "busy"},
        }))
        await drain(sub)  # clear frame
        assert "s1" not in hub.sticky_last_error

        hub.publish(ev("/proj", "message.updated", {
            "sessionID": "s1",
            "info": {"id": "m1", "time": {"updated": 1700000000000}},
        }))
        hub.flush()
        frames = await drain(sub)
        ds = digests_for(frames, "s1")
        assert ds, "expected the message.updated digest"
        assert all("lastError" not in d for d in ds), (
            "old sticky lastError must not be re-attached after the busy clear"
        )

    async def test_string_busy_clear_also_prevents_rebirth(self, pair):
        """REQ 5 (string regression): same no-rebirth guarantee via "busy"."""
        hub, sub = pair
        await seed_sticky(hub, sub, "s1")
        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        await drain(sub)

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "idle"},
        }))
        hub.flush()
        frames = await drain(sub)
        ds = digests_for(frames, "s1")
        assert ds and all("lastError" not in d for d in ds)


# ===========================================================================
# Requirement 6 — token hub: both formats behave identically for
# _session_status routing (no regression)
# ===========================================================================

class TestTokenHubMirror:
    async def test_object_busy_idle_drive_session_status(self, pair):
        """REQ 6: the publish() mirror feeds the token hub with the normalized
        status — object envelope must drive _session_status exactly like the
        legacy string."""
        hub, sub = pair
        th = TokenStreamHub()
        hub.set_token_hub(th)

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "busy"},
        }))
        assert th._session_status["s1"] == "busy"

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": "idle"},
        }))
        assert th._session_status["s1"] == "idle"
        await drain(sub)

    async def test_string_busy_idle_drive_session_status_regression(self, pair):
        """REQ 6 (regression): legacy string format unchanged through the mirror."""
        hub, sub = pair
        th = TokenStreamHub()
        hub.set_token_hub(th)

        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        assert th._session_status["s1"] == "busy"
        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": "idle",
        }))
        assert th._session_status["s1"] == "idle"
        await drain(sub)

    async def test_invalid_object_status_not_forwarded_as_busy(self, pair):
        """REQ 6/4: an invalid status shape must not mark the sid busy in the
        token hub either."""
        hub, sub = pair
        th = TokenStreamHub()
        hub.set_token_hub(th)
        hub.publish(ev("/proj", "session.status", {
            "sessionID": "s1", "status": {"type": 7},
        }))
        assert th._session_status.get("s1") != "busy"
        assert "s1" not in th._session_status
        await drain(sub)

    def test_on_session_status_direct_object_envelope(self):
        """REQ 6: on_session_status itself accepts both formats (defensive
        parity — the shared normalizer is applied at the entry point)."""
        th = TokenStreamHub()
        th.on_session_status("s1", {"type": "busy"})
        assert th._session_status.get("s1") == "busy"
        th.on_session_status("s1", {"type": "idle"})
        assert th._session_status.get("s1") != "busy"

    def test_on_session_status_direct_invalid_shapes(self):
        th = TokenStreamHub()
        th.on_session_status("s1", {})
        th.on_session_status("s1", {"type": None})
        th.on_session_status("s1", 3.14)
        assert th._session_status.get("s1") != "busy"
        assert "s1" not in th._session_status
