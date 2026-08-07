"""Stage-B tests for the token-stream accumulator (design §5.2 + §5.3 + §16-B).

Scope: LIFECYCLE ONLY — has_consumers(), reconnect both-paths wiring,
bounded tombstones (_disabled_parts / _nontext_parts cap + TTL),
session routing (on_session_status / on_session_deleted / _retire_session),
TTL busy-guard (ttl_sweep), and publish() session.status/deleted routing.

Out of scope (covered by their own stages):
* Stage C: flush_loop, finish_part fanout, _reserve eviction, safe_put,
  gzip, done:true marker.
* Stage D: HTTP endpoint, TokenSubscriber, registry admission, health,
  metrics.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from oc_slimapi.config import TOKEN_ACC_IDLE_MS
from oc_slimapi.sse.hub import GlobalHub, Subscriber
from oc_slimapi.sse.token_hub import TokenStreamHub, _now_ms


# ---------------------------------------------------------------------------
# Helpers (inlined per repo pattern — no sibling-test imports).
# ---------------------------------------------------------------------------

def make_global_event(
    directory: str,
    event_type: str,
    properties: dict | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties}}."""
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


async def drain_queue(subscriber: Subscriber, timeout: float = 0.1) -> list[bytes]:
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


def _delta_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    field: str = "text", delta: str = "x",
) -> dict:
    return {
        "sessionID": sid, "messageID": mid, "partID": pid,
        "field": field, "delta": delta,
    }


def _updated_props(
    sid: str = "s1", mid: str = "m1", pid: str = "p1",
    *, type: str = "text", text: str | None = None, end=None,
) -> dict:
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


class _FakeStreamCtx:
    """Async context manager mimicking httpx's stream() return value.

    ``enter_error`` (if set) is raised on __aenter__ to simulate a connect
    failure (which run()'s ``except Exception`` branch catches).

    WHY the unconditional ``await asyncio.sleep(0)`` in aiter_lines: with
    an empty ``lines`` list the async generator completes without ever
    awaiting, so the run() loop would spin back-to-back iterations without
    yielding to the event loop — starving the test task that's waiting to
    cancel it. Forcing one yield per call keeps the loop cooperative.
    """

    def __init__(self, *, lines: list[str] | None = None, enter_error: Exception | None = None):
        self._lines = lines or []
        self._enter_error = enter_error

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self):
        await asyncio.sleep(0)  # yield once so empty streams don't hog the loop
        for line in self._lines:
            yield line


class _BlockingStreamCtx:
    """Ctx whose __aenter__ parks forever.

    Used as the sentinel outcome after the scripted sequence is consumed:
    the run() task blocks INSIDE __aenter__ (before ``raise_for_status`` /
    the success-reconnect fire path), giving the test task full CPU to
    cancel and assert. Without this the loop would either spin on the last
    scripted outcome forever (success → fire → reconnect → fire → ...) or
    re-fire on a synthetic reconnect.
    """

    async def __aenter__(self):
        # Park forever — the test cancels run() explicitly via task.cancel(),
        # which raises CancelledError out of this await (handled by run()'s
        # ``except asyncio.CancelledError: raise``).
        await asyncio.Event().wait()
        return self  # pragma: no cover - unreachable

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self) -> None:
        pass  # pragma: no cover - never reached

    async def aiter_lines(self):
        if False:  # pragma: no cover - keep this an async generator
            yield ""


class _FakeClient:
    """httpx-like client whose stream() returns a scripted outcome sequence.

    Each scripted item is either an ``Exception`` (raised on __aenter__) or
    a ``_FakeStreamCtx``. After the scripted sequence is consumed, a
    :class:`_BlockingStreamCtx` is returned so the run() task parks cleanly
    (instead of spinning on the last scripted outcome forever).
    """

    def __init__(self, outcomes: list):
        self._outcomes = outcomes
        self.calls = 0

    def stream(self, *args, **kwargs):
        i = self.calls
        self.calls += 1
        if i < len(self._outcomes):
            outcome = self._outcomes[i]
            if isinstance(outcome, Exception):
                return _FakeStreamCtx(enter_error=outcome)
            return outcome
        return _BlockingStreamCtx()


@pytest.fixture
async def bare_hub():
    """Bare GlobalHub; tears down background tasks."""
    h = GlobalHub(client=None)
    try:
        yield h
    finally:
        await _close_hub(h)


# ===========================================================================
# has_consumers() — spans control-plane + token ledgers (§5.2 + §16-B)
# ===========================================================================

class TestHasConsumers:
    def test_empty_hub_no_consumers(self):
        hub = GlobalHub(client=None)
        assert hub.has_consumers() is False

    def test_control_subscriber_present(self):
        hub = GlobalHub(client=None)
        hub.subscribers.add(Subscriber())
        assert hub.has_consumers() is True

    def test_token_hub_stub_returns_zero(self):
        """Stage A/B stub subscriber_count=0 → has_consumers falls through."""
        hub = GlobalHub(client=None)
        hub.set_token_hub(TokenStreamHub())
        assert hub.has_consumers() is False

    def test_token_subscribers_keep_hub_alive(self):
        """When Stage D wires subscriber_count > 0, the hub stays alive even
        with zero control-plane subs (the §16-B 'has_consumers 贯穿所有 grace
        路径' contract)."""
        hub = GlobalHub(client=None)

        class _StageDStub(TokenStreamHub):
            @property
            def subscriber_count(self) -> int:
                return 1

        hub.set_token_hub(_StageDStub())
        assert hub.has_consumers() is True

    def test_control_subs_short_circuit_token_check(self):
        """If control subs exist, the token hub is not consulted (None-safe)."""
        hub = GlobalHub(client=None)
        hub.subscribers.add(Subscriber())
        # No token hub wired — must still return True (no None deref).
        assert hub.has_consumers() is True


# ===========================================================================
# stop_after_grace — uses has_consumers() (§16-B)
# ===========================================================================

class TestStopAfterGrace:
    async def test_cancels_tasks_when_no_consumers(self, monkeypatch):
        """stop_after_grace tears down tasks when has_consumers() is False."""

        # Save the real sleep so the test itself can yield after patching.
        real_sleep = asyncio.sleep

        async def _fast(_):
            return  # skip GRACE_SECONDS wait inside stop_after_grace

        monkeypatch.setattr("oc_slimapi.sse.global_hub.asyncio.sleep", _fast)

        hub = GlobalHub(client=None)

        async def _park_forever():
            # Real suspension (not asyncio.sleep, which is patched) so the
            # task can actually receive CancelledError at its await point.
            await asyncio.Event().wait()

        hub.task = asyncio.create_task(_park_forever())
        await hub.stop_after_grace()
        # Yield via the REAL sleep so the cancellation actually propagates
        # into _park_forever (the patched _fast returns without yielding).
        await real_sleep(0)
        assert hub.task.done() or hub.task.cancelled()

    async def test_preserves_tasks_when_token_subscribers_present(self, monkeypatch):
        """stop_after_grace is a no-op when token subscribers are attached
        (has_consumers True). §16-B: token subs keep the hub alive across
        the grace window."""

        real_sleep = asyncio.sleep

        async def _fast(_):
            return

        monkeypatch.setattr("oc_slimapi.sse.global_hub.asyncio.sleep", _fast)

        hub = GlobalHub(client=None)

        class _StageDStub(TokenStreamHub):
            @property
            def subscriber_count(self) -> int:
                return 1

        hub.set_token_hub(_StageDStub())

        async def _park_forever():
            await asyncio.Event().wait()

        hub.task = asyncio.create_task(_park_forever())
        try:
            await hub.stop_after_grace()
            await real_sleep(0)
            # has_consumers True → task NOT cancelled.
            assert not hub.task.done()
        finally:
            hub.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hub.task


# ===========================================================================
# _notify_upstream_loss — direct unit + reconnect integration (§16-B backstop)
# ===========================================================================

class TestNotifyUpstreamLoss:
    async def test_clears_token_state(self, bare_hub):
        """Direct call: _notify_upstream_loss clears token hub + resyncs subs."""
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        th.on_part_updated(_updated_props(text="seed"))
        th.on_part_delta(_delta_props(delta="chunk"))
        assert th.live_parts and th._pending
        bare_hub._notify_upstream_loss()
        assert not th.live_parts
        assert not th._pending
        assert not th._nontext_parts

    async def test_resyncs_control_plane_subscribers(self, bare_hub):
        """Control-plane subscribers still get reconnect_no_replay resync
        (regression: _notify_upstream_loss must call resync_all)."""
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        bare_hub._notify_upstream_loss()
        frames = await drain_queue(subscriber, timeout=0.1)
        assert len(frames) == 1
        event_name, data = parse_event(frames[0])
        assert event_name == "resync"
        assert data == {"reason": "reconnect_no_replay"}

    async def test_no_token_hub_no_crash(self, bare_hub):
        """Without a token hub, _notify_upstream_loss still resyncs control
        plane subscribers (no None deref)."""
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        bare_hub._notify_upstream_loss()
        frames = await drain_queue(subscriber, timeout=0.1)
        assert len(frames) == 1


class TestRunReconnectWiring:
    """run() integration: _notify_upstream_loss fires on BOTH paths and
    exactly once per upstream epoch transition (not per retry-loop iter)."""

    async def test_exception_path_fires_once_per_epoch(self, monkeypatch):
        """The FIRST exception after a successful connect fires the loss
        notify; subsequent retry-loop exceptions do NOT (per-epoch guard)."""
        # Fast-sleep so the retry loop iterates quickly without real waits.
        real_sleep = asyncio.sleep

        async def _fast(_):
            await real_sleep(0)

        monkeypatch.setattr("oc_slimapi.sse.global_hub.asyncio.sleep", _fast)

        th = TokenStreamHub()
        calls: list[int] = []
        orig = th.on_upstream_reconnect

        def spy():
            calls.append(1)
            orig()

        th.on_upstream_reconnect = spy  # type: ignore[method-assign]

        outcomes = [
            _FakeStreamCtx(lines=[]),  # 1: initial connect (epoch established)
            RuntimeError("boom"),       # 2: first exception → fires
            RuntimeError("boom"),       # 3: retry → SKIPPED (guard)
            RuntimeError("boom"),       # 4: retry → SKIPPED
            RuntimeError("boom"),       # 5: retry → SKIPPED
        ]
        client = _FakeClient(outcomes)
        hub = GlobalHub(client=client)
        hub.set_token_hub(th)
        hub.subscribers.add(Subscriber())  # has_consumers=True → loop keeps going

        hub.task = asyncio.create_task(hub.run())
        await real_sleep(0.05)
        hub.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hub.task

        # All 5 outcomes consumed (calls counter increments per stream() call).
        assert client.calls >= 5
        # Exactly ONE on_upstream_reconnect call — the first exception only.
        assert len(calls) == 1, f"expected exactly 1 fire, got {len(calls)}"

    async def test_success_reconnect_path_fires_notify(self, monkeypatch):
        """A successful reconnect (ever_connected already True) also fires
        _notify_upstream_loss — the previous epoch's accumulated state is
        stale and must be cleared even if no exception was observed
        (e.g. upstream just closed the stream cleanly)."""
        real_sleep = asyncio.sleep

        async def _fast(_):
            await real_sleep(0)

        monkeypatch.setattr("oc_slimapi.sse.global_hub.asyncio.sleep", _fast)

        th = TokenStreamHub()
        calls: list[int] = []
        orig = th.on_upstream_reconnect

        def spy():
            calls.append(1)
            orig()

        th.on_upstream_reconnect = spy  # type: ignore[method-assign]

        outcomes = [
            _FakeStreamCtx(lines=[]),  # 1: initial connect (no fire)
            _FakeStreamCtx(lines=[]),  # 2: reconnect → FIRES
        ]
        client = _FakeClient(outcomes)
        hub = GlobalHub(client=client)
        hub.set_token_hub(th)
        hub.subscribers.add(Subscriber())

        hub.task = asyncio.create_task(hub.run())
        await real_sleep(0.05)
        hub.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hub.task

        # At least one fire from the success-reconnect path (iter 2+).
        assert len(calls) >= 1
        assert hub.reconnects_total >= 1


# ===========================================================================
# Bounded tombstones — cap + TTL + sid-clear (§16-B)
# ===========================================================================

class TestBoundedDisabled:
    def test_cap_evicts_oldest(self, monkeypatch):
        """Over-cap insert evicts the oldest tombstone (insertion order)."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_MAX", 3)
        # Effectively disable TTL so cap is the binding constraint.
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_TTL_MS", 10**9)
        th = TokenStreamHub()
        for i in range(4):
            th._remember_disabled(("s", "m", f"p{i}"))
        assert len(th._disabled_parts) == 3
        # p0 (oldest) evicted; p1..p3 retained.
        assert ("s", "m", "p0") not in th._disabled_parts
        for i in (1, 2, 3):
            assert ("s", "m", f"p{i}") in th._disabled_parts

    def test_ttl_expires_old_entries_on_next_insert(self, monkeypatch):
        """Entries older than TTL are pruned when the next entry is inserted."""
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_MAX", 100)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_TTL_MS", 100)
        th = TokenStreamHub()
        th._remember_disabled(("s", "m", "old"))
        # Backdate the entry past TTL.
        th._disabled_parts[("s", "m", "old")] = _now_ms() - 1000
        # New insert triggers prune.
        th._remember_disabled(("s", "m", "new"))
        assert ("s", "m", "old") not in th._disabled_parts
        assert ("s", "m", "new") in th._disabled_parts

    def test_remember_disabled_is_idempotent(self):
        """Re-recording an existing key is a no-op (no TTL refresh)."""
        th = TokenStreamHub()
        th._remember_disabled(("s", "m", "p1"))
        original_ts = th._disabled_parts[("s", "m", "p1")]
        th._remember_disabled(("s", "m", "p1"))
        assert th._disabled_parts[("s", "m", "p1")] == original_ts
        assert len(th._disabled_parts) == 1

    def test_drop_part_uses_bounded_disabled(self):
        """End-to-end: drop_part records into the bounded map."""
        th = TokenStreamHub()
        key = ("s1", "m1", "p1")
        assert th.drop_part(key) is True
        assert key in th._disabled_parts
        # Idempotent.
        assert th.drop_part(key) is False


class TestBoundedNontext:
    def test_cap_evicts_oldest(self, monkeypatch):
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_MAX", 3)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_TTL_MS", 10**9)
        th = TokenStreamHub()
        for i in range(4):
            th._remember_nontext(("s", "m", f"p{i}"))
        assert len(th._nontext_parts) == 3
        assert ("s", "m", "p0") not in th._nontext_parts

    def test_ttl_expires_old_entries(self, monkeypatch):
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_MAX", 100)
        monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_DISABLED_TTL_MS", 100)
        th = TokenStreamHub()
        th._remember_nontext(("s", "m", "old"))
        th._nontext_parts[("s", "m", "old")] = _now_ms() - 1000
        th._remember_nontext(("s", "m", "new"))
        assert ("s", "m", "old") not in th._nontext_parts
        assert ("s", "m", "new") in th._nontext_parts

    def test_remember_nontext_is_idempotent(self):
        th = TokenStreamHub()
        th._remember_nontext(("s", "m", "p1"))
        original_ts = th._nontext_parts[("s", "m", "p1")]
        th._remember_nontext(("s", "m", "p1"))
        assert th._nontext_parts[("s", "m", "p1")] == original_ts
        assert len(th._nontext_parts) == 1


# ===========================================================================
# on_session_status / on_session_deleted / _retire_session (§16-B)
# ===========================================================================

class TestOnSessionStatus:
    def test_busy_records_status_and_busy_sid(self):
        th = TokenStreamHub()
        th.on_session_status("s1", "busy")
        assert th._session_status["s1"] == "busy"
        assert "s1" in th._busy_sids
        assert th._pending_session_resinks == []

    def test_idle_discards_busy_sid(self):
        th = TokenStreamHub()
        th.on_session_status("s1", "busy")
        th.on_session_status("s1", "idle")
        assert th._session_status["s1"] == "idle"
        assert "s1" not in th._busy_sids

    def test_idle_retires_session_live_parts(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="x"))
        assert ("s1", "m1", "p1") in th.live_parts
        th.on_session_status("s1", "idle")
        assert ("s1", "m1", "p1") not in th.live_parts
        assert th._total_live_bytes == 0

    def test_idle_records_pending_session_idle_resync(self):
        """§16-B / SF-3: idle enqueues a session_idle resync for Stage C/D."""
        th = TokenStreamHub()
        th.on_session_status("s1", "idle")
        assert th._pending_session_resinks == [("s1", "session_idle")]

    def test_unknown_status_ignored(self):
        """Statuses other than busy/idle carry no lifecycle signal."""
        th = TokenStreamHub()
        th.on_session_status("s1", "shared")
        assert "s1" not in th._session_status
        assert th._pending_session_resinks == []

    def test_busy_then_idle_only_one_resync(self):
        """busy does not enqueue; idle enqueues exactly one."""
        th = TokenStreamHub()
        th.on_session_status("s1", "busy")
        assert th._pending_session_resinks == []
        th.on_session_status("s1", "idle")
        assert th._pending_session_resinks == [("s1", "session_idle")]


class TestOnSessionDeleted:
    def test_retires_session_state(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="data"))
        th.on_session_deleted("s1")
        assert ("s1", "m1", "p1") not in th.live_parts
        assert th._total_live_bytes == 0

    def test_clears_session_status(self):
        """on_session_deleted is terminal for the session — clear status too
        (so a late status event for a deleted sid doesn't keep a stale
        entry forever)."""
        th = TokenStreamHub()
        th.on_session_status("s1", "busy")
        assert "s1" in th._session_status
        th.on_session_deleted("s1")
        assert "s1" not in th._session_status
        assert "s1" not in th._busy_sids

    def test_terminates_subscribers_on_session_deleted(self):
        """INV-4 (P0-3): on_session_deleted directly terminates subscribers
        (resync{session_deleted} → STOP), not via the deferred flush-loop
        resync queue. Test updated from test_records_pending_session_deleted_resync
        because the behavior changed: _enqueue_session_resync is replaced by
        direct sub.terminate."""
        from oc_slimapi.sse.tokenstream.frames import STOP, _resync_frame
        th = TokenStreamHub()
        # Attach a fake subscriber.
        class _FakeSub:
            def __init__(self):
                self.session_id = "s1"
                self.closed = False
                self.frames = []
                self._in_handshake = False
            def begin_handshake(self): self._in_handshake = True
            def end_handshake(self): self._in_handshake = False
            def put(self, frame): self.frames.append(frame); return True
            def terminate(self, reason):
                self.closed = True
                self.frames.append(_resync_frame(self.session_id, reason))
                self.frames.append(STOP)
        sub = _FakeSub()
        th.attach_subscriber("s1", sub)
        sub.frames.clear()
        th.on_session_deleted("s1")
        # No pending resync in the flush queue (old behavior removed).
        assert th._pending_session_resinks == []
        # Subscriber terminated directly.
        assert sub.closed is True
        resyncs = [f for f in sub.frames if isinstance(f, bytes) and b"resync" in f]
        assert len(resyncs) == 1

    def test_does_not_touch_other_sessions(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s2", "m2", "p2", text=""))
        th.on_session_deleted("s1")
        assert ("s1", "m1", "p1") not in th.live_parts
        assert ("s2", "m2", "p2") in th.live_parts


class TestRetireSession:
    def test_clears_all_state_for_sid(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))   # live
        th.on_part_updated(_updated_props("s1", "m1", "p2", type="reasoning"))  # nontext
        th.drop_part(("s1", "m1", "p3"))  # disabled
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="abc"))  # pending
        th.on_session_status("s1", "busy")  # status (NOT cleared by retire)
        assert th._total_live_bytes > 0
        th._retire_session("s1")
        # All s1 keys cleared across the 4 part-state structures.
        assert all(k[0] != "s1" for k in th.live_parts)
        assert all(k[0] != "s1" for k in th._pending)
        assert all(k[0] != "s1" for k in th._nontext_parts)
        assert all(k[0] != "s1" for k in th._disabled_parts)
        assert th._total_live_bytes == 0
        # Per spec: _session_status / _busy_sids outlive a part-level retire
        # (only on_session_deleted / on_upstream_reconnect clear them).
        assert th._session_status.get("s1") == "busy"
        assert "s1" in th._busy_sids

    def test_preserves_other_sids(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th.on_part_updated(_updated_props("s2", "m2", "p2", text=""))
        th.on_part_delta(_delta_props("s2", "m2", "p2", delta="keep"))
        s2_bytes = th._total_live_bytes
        th._retire_session("s1")
        assert ("s2", "m2", "p2") in th.live_parts
        assert th._total_live_bytes == s2_bytes  # s2 unchanged

    def test_byte_budget_floored_at_zero(self):
        """Floor-at-0 protects against accounting drift: if the budget gauge
        is somehow LOWER than the sum of live.byte_count (manual tamper,
        future bug), retire must not drive it negative."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="seed"))  # 4 bytes accounted
        th._total_live_bytes = 1  # tamper — lower than the real 4 bytes
        th._retire_session("s1")
        # 1 - 4 = -3 → floored at 0.
        assert th._total_live_bytes == 0

    def test_idempotent(self):
        """Retiring the same sid twice is a safe no-op."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th._retire_session("s1")
        th._retire_session("s1")  # second call must not raise / corrupt
        assert th._total_live_bytes == 0


# ===========================================================================
# ttl_sweep — busy-guard (§16-B NB#4)
# ===========================================================================

class TestTtlSweep:
    def test_retires_known_idle_expired_part(self):
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        key = ("s1", "m1", "p1")
        th._session_status["s1"] = "idle"
        now = _now_ms()
        th.live_parts[key].last_delta_ms = now - TOKEN_ACC_IDLE_MS - 1
        retired = th.ttl_sweep(now)
        assert retired == [key]
        assert key not in th.live_parts
        assert th._total_live_bytes == 0

    def test_does_not_retire_busy_session(self):
        """bgpt NB#4: a busy session that has gone quiet is most likely in a
        long generation pause — do NOT retire even if expired."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        key = ("s1", "m1", "p1")
        th._session_status["s1"] = "busy"
        th._busy_sids.add("s1")
        now = _now_ms()
        th.live_parts[key].last_delta_ms = now - TOKEN_ACC_IDLE_MS - 1
        retired = th.ttl_sweep(now)
        assert retired == []
        assert key in th.live_parts

    def test_does_not_retire_unknown_status(self):
        """Unknown status (sidecar missed the session.status event) → do NOT
        retire (backstop NB#4 — unknown is treated as 'could still be busy')."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        key = ("s1", "m1", "p1")
        now = _now_ms()
        th.live_parts[key].last_delta_ms = now - TOKEN_ACC_IDLE_MS - 1
        # No _session_status entry for s1.
        retired = th.ttl_sweep(now)
        assert retired == []
        assert key in th.live_parts

    def test_does_not_retire_active_part(self):
        """Recent last_delta_ms → part still active, do not retire."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        key = ("s1", "m1", "p1")
        th._session_status["s1"] = "idle"
        # last_delta_ms is fresh (default).
        retired = th.ttl_sweep()
        assert retired == []
        assert key in th.live_parts

    def test_retires_only_expired_idle_parts(self):
        """Mixed: one expired-idle (retire) + one active-idle (keep) +
        one expired-busy (keep)."""
        th = TokenStreamHub()
        # Expired idle.
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        th._session_status["s1"] = "idle"
        # Active idle.
        th.on_part_updated(_updated_props("s2", "m2", "p2", text=""))
        th._session_status["s2"] = "idle"
        # Expired busy.
        th.on_part_updated(_updated_props("s3", "m3", "p3", text=""))
        th._session_status["s3"] = "busy"
        th._busy_sids.add("s3")

        now = _now_ms()
        th.live_parts[("s1", "m1", "p1")].last_delta_ms = now - TOKEN_ACC_IDLE_MS - 1
        th.live_parts[("s3", "m3", "p3")].last_delta_ms = now - TOKEN_ACC_IDLE_MS - 1
        # p2 keeps default fresh last_delta_ms.

        retired = th.ttl_sweep(now)
        assert set(retired) == {("s1", "m1", "p1")}
        assert ("s2", "m2", "p2") in th.live_parts
        assert ("s3", "m3", "p3") in th.live_parts

    def test_clears_pending_and_nontext_for_retired_key(self):
        """TTL retire cleans up the key's pending + nontext bookkeeping too."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props("s1", "m1", "p1", text=""))
        key = ("s1", "m1", "p1")
        th.on_part_delta(_delta_props("s1", "m1", "p1", delta="x"))
        th._remember_nontext(("s1", "m1", "p1"))  # defensive: same key in nontext
        th._session_status["s1"] = "idle"
        now = _now_ms()
        th.live_parts[key].last_delta_ms = now - TOKEN_ACC_IDLE_MS - 1
        th.ttl_sweep(now)
        assert key not in th._pending
        assert key not in th._nontext_parts


# ===========================================================================
# on_upstream_reconnect — Stage-B full clear (§5.2 + §16-B)
# ===========================================================================

class TestOnUpstreamReconnectStageB:
    def test_clears_session_routing_state(self):
        """on_upstream_reconnect clears _session_status / _busy_sids /
        _pending_session_resinks (Stage B extension to Stage A's clear).

        Test updated: previously used on_session_deleted to populate
        _pending_session_resinks, but INV-4 (P0-3) changed
        on_session_deleted to directly terminate subscribers instead of
        enqueueing a resync. Now uses on_session_status("idle") to
        populate the resync queue."""
        th = TokenStreamHub()
        th.on_session_status("s1", "busy")
        th.on_session_status("s2", "idle")  # enqueues ("s2", "session_idle")
        th.on_part_updated(_updated_props("s3", "m3", "p3", text=""))
        assert th._session_status
        assert th._busy_sids
        assert th._pending_session_resinks
        assert th.live_parts
        th.on_upstream_reconnect()
        assert th._session_status == {}
        assert not th._busy_sids
        assert th._pending_session_resinks == []
        assert th.live_parts == {}


# ===========================================================================
# publish() session routing — parallel to control-plane digest (§16-B)
# ===========================================================================

class TestPublishSessionRouting:
    async def test_session_status_busy_routes_to_token_hub(self, bare_hub):
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/p", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        assert th._session_status.get("s1") == "busy"
        assert "s1" in th._busy_sids

    async def test_session_status_idle_routes_and_retires(self, bare_hub):
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        # Seed a live part first.
        bare_hub.publish(make_global_event("/p", "message.part.updated", {
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text", "text": "", "time": {},
            },
            "time": {},
        }))
        assert ("s1", "m1", "p1") in th.live_parts
        bare_hub.publish(make_global_event("/p", "session.status", {
            "sessionID": "s1", "status": "idle",
        }))
        assert ("s1", "m1", "p1") not in th.live_parts
        assert th._pending_session_resinks[-1] == ("s1", "session_idle")

    async def test_session_deleted_routes_to_token_hub(self, bare_hub):
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/p", "message.part.updated", {
            "sessionID": "s1",
            "part": {
                "id": "p1", "messageID": "m1", "sessionID": "s1",
                "type": "text", "text": "", "time": {},
            },
            "time": {},
        }))
        bare_hub.publish(make_global_event("/p", "session.deleted", {
            "sessionID": "s1",
        }))
        assert ("s1", "m1", "p1") not in th.live_parts
        # INV-4: session.deleted no longer enqueues a flush-loop resync;
        # it directly terminates subscribers. With no subscribers attached
        # here, _pending_session_resinks stays empty.
        assert th._pending_session_resinks == []

    async def test_no_token_hub_no_crash_on_session_status(self, bare_hub):
        """Without a token hub, session.status/deleted routing is a no-op."""
        bare_hub.publish(make_global_event("/p", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        bare_hub.publish(make_global_event("/p", "session.deleted", {
            "sessionID": "s2",
        }))
        # No exception, no state to check.

    async def test_control_plane_digest_unchanged_with_token_hub(self, bare_hub):
        """Regression guard: the control-plane session.status digest is still
        emitted when a token hub is wired (parallel route must not break
        the existing digest handling)."""
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/p", "session.status", {
            "sessionID": "s1", "status": "busy",
        }))
        bare_hub.flush()
        frames = await drain_queue(subscriber, timeout=0.1)
        assert len(frames) == 1
        event_name, data = parse_event(frames[0])
        assert event_name == "session.digest"
        assert data["status"] == "busy"

    async def test_control_plane_deleted_digest_unchanged(self, bare_hub):
        """Regression guard: the control-plane session.deleted digest is still
        emitted (with deleted:true) when a token hub is wired."""
        subscriber = Subscriber()
        bare_hub.subscribers.add(subscriber)
        th = TokenStreamHub()
        bare_hub.set_token_hub(th)
        bare_hub.publish(make_global_event("/p", "session.deleted", {
            "sessionID": "s1",
        }))
        bare_hub.flush()
        frames = await drain_queue(subscriber, timeout=0.1)
        assert len(frames) == 1
        event_name, data = parse_event(frames[0])
        assert event_name == "session.digest"
        assert data.get("deleted") is True


# ===========================================================================
# NB1 / NB3 rev-2 non-blocking fixes
# ===========================================================================

class TestNBGuards:
    def test_nb1_non_string_seed_treated_as_empty(self):
        """NB1: a malformed non-string seed (int / dict / list) is treated
        as empty, not appended to chunks (would corrupt byte accounting)."""
        th = TokenStreamHub()
        th.on_part_updated({"part": {
            "id": "p1", "messageID": "m1", "sessionID": "s1",
            "type": "text", "text": 12345, "time": {},  # int seed
        }})
        live = th.live_parts[("s1", "m1", "p1")]
        assert live.chunks == []
        assert live.byte_count == 0
        assert th._total_live_bytes == 0

    def test_nb1_dict_seed_treated_as_empty(self):
        th = TokenStreamHub()
        th.on_part_updated({"part": {
            "id": "p1", "messageID": "m1", "sessionID": "s1",
            "type": "text", "text": {"oops": True}, "time": {},
        }})
        live = th.live_parts[("s1", "m1", "p1")]
        assert live.chunks == []
        assert live.byte_count == 0

    def test_nb1_none_seed_treated_as_empty(self):
        th = TokenStreamHub()
        th.on_part_updated({"part": {
            "id": "p1", "messageID": "m1", "sessionID": "s1",
            "type": "text", "text": None, "time": {},
        }})
        live = th.live_parts[("s1", "m1", "p1")]
        assert live.chunks == []

    def test_nb1_string_seed_still_works(self):
        """Regression guard: a normal string seed is still appended."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text="hello"))
        live = th.live_parts[("s1", "m1", "p1")]
        assert live.chunks == ["hello"]
        assert live.byte_count == 5

    def test_nb3_post_drop_text_start_does_not_create_live_part(self):
        """NB3: after drop_part, a re-issued text-start for the same key
        MUST NOT create a dead LivePart (late deltas still drop on
        _disabled; a dangling LivePart would confuse Stage-C snapshot)."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.drop_part(key)
        assert key not in th.live_parts
        # Re-issue text-start with a seed.
        th.on_part_updated(_updated_props(text="reseed"))
        assert key not in th.live_parts  # NOT created.
        assert key in th._disabled_parts  # still disabled.

    def test_nb3_disabled_key_late_delta_still_drops(self):
        """Even after a re-issued text-start, late deltas for a disabled key
        drop on the _disabled check (not as orphans)."""
        th = TokenStreamHub()
        th.on_part_updated(_updated_props(text=""))
        key = ("s1", "m1", "p1")
        th.drop_part(key)
        th.on_part_updated(_updated_props(text="reseed"))
        th.on_part_delta(_delta_props(delta="late"))
        assert key not in th.live_parts
        assert key not in th._pending
        # Disabled short-circuits before orphan check → no orphan counter.
        assert th.orphan_deltas == 0
