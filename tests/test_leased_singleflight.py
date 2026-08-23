"""Task 1.2 (A2) — LeasedSingleFlight protocol tests (traffic plan Batch 1 §3.x).

A2-C4⑥ nine per-path release classes, plus the ownership in-place conversion
special (plan §3.x rev-9 review product). Every test asserts the ledger
invariant along the way: ``leased_bytes == sum(reserve_bytes of entries in
ownership_state ∈ {in-flight (incl. detached), grace, retained})``.

Snapshot shape (plan §3.x): ``{key: [(layer, seq, caller_refs,
ownership_state), ...]}`` with layer ∈ {"active", "retired"} and
ownership_state ∈ {"in-flight", "grace", "retained", "failed"}.

Timing strategy (CI de-flake, zero src changes): the grace-expiry waits are
split by SUBJECT. Tests whose subject is the background grace timer itself
(``call_later`` dispatch, singleflight.py leased path) keep REAL loop time
via bounded polls (short interval + loose deadline — never one fixed
``sleep(grace + margin)``): ``test_normal_release…``,
``test_grace_expiry_moves_to_retired…``,
``test_shutdown_grace_entry_becomes_retained_and_timer_cancelled`` and
``test_release_cuts_body_and_entry_references`` (its unreachability assert
depends on the timer callback having actually DISPATCHED — a pending
TimerHandle keeps its args alive on some CPython versions even after
cancel, so real dispatch is the portable mechanism). Incidental past-grace
cleanup tails inject a ``_FakeClock`` (constructor ``clock=`` param) and
drive the LAZY expiry check — ``_expire_if_due``, the exact check
``fetch_or_bypass`` runs at its serial point — via ``_sweep_lazy``. The
budget-eviction core is synchronous and clock-free (``_try_reserve`` never
consults the clock). ``_FakeClock`` never patches the event loop,
``call_later`` or ``asyncio.sleep``.
"""
from __future__ import annotations

import asyncio
from typing import Callable

import pytest

from oc_slimapi.singleflight import LeasedSingleFlight

RESERVE = 60


def _registry(**overrides) -> LeasedSingleFlight:
    kwargs = dict(max_bytes=RESERVE, network_concurrency=8,
                  result_grace_seconds=0.05)
    kwargs.update(overrides)
    return LeasedSingleFlight(**kwargs)


async def _settle() -> None:
    """Let the event loop run pending timer callbacks + queued tasks."""
    await asyncio.sleep(0)


class _FakeClock:
    """Deterministic monotonic clock (tests/test_burst_watch.py style).

    Injected via ``LeasedSingleFlight(clock=...)``. NOTE: the leased grace
    timer is still armed with REAL loop delay (``call_later(grace)``), so a
    FakeClock alone never dispatches it — tests below advance the clock and
    then run the lazy serial-point check (``_sweep_lazy``). Both routes
    converge to the identical ledger state, and a real timer firing early
    on a slow CI box only ever races to the same reaped result.
    """

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _sweep_lazy(sf: LeasedSingleFlight) -> None:
    """Run the lazy grace-expiry check for every live key — exactly what
    ``fetch_or_bypass`` does at its serial point (``_expire_if_due``),
    poked directly so no fresh flight is led. Converts + reaps zero-caller
    grace entries (in-place retired/retained conversion, refund, delete)."""
    for key in list(sf.snapshot()):
        sf._expire_if_due(key)


# Long grace for all FakeClock-based tests: the real ``call_later`` timer
# is armed with this value too, so it cannot race before the lazy sweep
# even on the slowest CI.  Only tests whose SUBJECT is the real background
# timer (bounded-poll) keep a short ``result_grace_seconds``.
FAKE_GRACE_SECONDS = 60.0


def _fake_clock_registry(**overrides) -> tuple[_FakeClock, LeasedSingleFlight]:
    """Create a ``_FakeClock`` + ``LeasedSingleFlight`` with long grace.
    All overrides pass through to the registry constructor."""
    clock = _FakeClock()
    kwargs = dict(
        max_bytes=RESERVE, network_concurrency=8,
        result_grace_seconds=FAKE_GRACE_SECONDS, clock=clock,
    )
    kwargs.update(overrides)
    return clock, LeasedSingleFlight(**kwargs)


async def _poll_until(
    predicate: Callable[[], bool],
    *,
    deadline: float = 2.0,
    interval: float = 0.01,
    what: str = "condition",
) -> None:
    """Bounded poll for the REAL-loop-timer tests: short-interval state
    checks with a loose deadline (replaces fixed ``sleep(grace+margin)``)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline
    while not predicate():
        if loop.time() >= end:
            raise AssertionError(
                f"timed out after {deadline}s waiting for {what}")
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Class ② — __aexit__ normal release (also the happy-path baseline)
# ---------------------------------------------------------------------------

async def test_normal_release_path_and_idempotent_double_release():
    sf = _registry()
    calls = []

    async def factory():
        calls.append(1)
        return b"body"

    lease = await sf.fetch_or_bypass("k", factory, RESERVE)
    assert lease is not None
    assert lease.body == b"body"
    assert sf.leased_bytes == RESERVE  # grace ownership held
    snap = sf.snapshot()
    assert snap["k"][0][0] == "active"  # layer
    assert snap["k"][0][2] == 1  # caller_refs (leader still holds lease)
    assert snap["k"][0][3] == "grace"  # ownership_state after success

    async with lease:
        pass  # release via __aexit__
    lease._release()  # manual double release must be idempotent
    # grace window still retains the body → budget held until expiry
    assert sf.leased_bytes == RESERVE
    # REAL loop time — nothing accesses the entry afterwards, so only the
    # grace timer (call_later, leased completion path) can refund+delete.
    # Bounded poll past grace (0.05) instead of a fixed sleep.
    await _poll_until(
        lambda: sf.leased_bytes == 0 and sf.snapshot() == {},
        what="grace timer zero-caller refund+delete")
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


# ---------------------------------------------------------------------------
# Class ① — budget exhaustion → None bypass
# ---------------------------------------------------------------------------

async def test_budget_full_returns_none_and_bypass_fetches_directly():
    clock, sf = _fake_clock_registry()  # budget fits exactly ONE reserve
    gate = asyncio.Event()

    async def gated_factory():
        await gate.wait()
        return b"A"

    leader = asyncio.create_task(
        sf.fetch_or_bypass("a", gated_factory, RESERVE))
    await _settle()
    assert sf.leased_bytes == RESERVE

    async def plain_factory():
        return b"B"

    lease_b = await sf.fetch_or_bypass("b", plain_factory, RESERVE)
    assert lease_b is None  # budget exhausted → caller must bypass
    assert sf.leased_bytes == RESERVE  # no phantom accounting

    gate.set()
    lease_a = await leader
    async with lease_a:
        assert lease_a.body == b"A"
    # incidental past-grace tail → virtual clock + lazy serial-point sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0


async def test_reserve_larger_than_total_budget_always_bypasses():
    sf = _registry(max_bytes=100)

    async def factory():
        return b"X"

    assert await sf.fetch_or_bypass("k", factory, 101) is None
    assert await sf.fetch_or_bypass("k", factory, 100) is not None  # fits
    assert sf.leased_bytes == 100


# ---------------------------------------------------------------------------
# Class ③ — factory regular exception → FetchFailed envelope, immediate refund
# ---------------------------------------------------------------------------

async def test_factory_exception_same_instance_all_callers_immediate_refund():
    sf = _registry()
    gate = asyncio.Event()
    boom = ValueError("upstream boom")
    seen: list[BaseException] = []

    async def failing_factory():
        await gate.wait()
        raise boom

    leader = asyncio.create_task(
        sf.fetch_or_bypass("k", failing_factory, RESERVE))
    await _settle()
    waiters = [asyncio.create_task(
        sf.fetch_or_bypass("k", failing_factory, RESERVE))
        for _ in range(2)]
    await _settle()
    gate.set()

    for task in [leader, *waiters]:
        try:
            await task
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            seen.append(exc)

    assert all(exc is boom for exc in seen)  # SAME instance, FetchFailed envelope
    assert sf.leased_bytes == 0  # immediate refund — never enters grace
    assert sf.snapshot() == {}  # tombstone deleted with last residual ref


async def test_failure_refunds_budget_so_next_flight_can_reserve():
    clock, sf = _fake_clock_registry()  # single-flight budget

    async def failing():
        raise RuntimeError("first attempt dies")

    async def ok():
        return b"second"

    with pytest.raises(RuntimeError):
        await sf.fetch_or_bypass("k", failing, RESERVE)
    assert sf.leased_bytes == 0  # refunded → capacity available again
    lease = await sf.fetch_or_bypass("k", ok, RESERVE)
    assert lease is not None and lease.body == b"second"
    async with lease:
        pass
    # incidental past-grace tail → virtual clock + lazy sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0


# ---------------------------------------------------------------------------
# Class ④ — waiter self-cancellation in the registered-ref window
# ---------------------------------------------------------------------------

async def test_waiter_cancel_releases_own_ref_only_and_shared_flight_lives():
    clock, sf = _fake_clock_registry()
    gate = asyncio.Event()

    async def gated_factory():
        await gate.wait()
        return b"shared"

    leader = asyncio.create_task(
        sf.fetch_or_bypass("k", gated_factory, RESERVE))
    await _settle()
    assert sf.snapshot()["k"][0][2] == 1  # leader ref only

    cancelled_waiter = asyncio.create_task(
        sf.fetch_or_bypass("k", gated_factory, RESERVE))
    await _settle()
    assert sf.snapshot()["k"][0][2] == 2  # waiter ref registered

    survivor = asyncio.create_task(
        sf.fetch_or_bypass("k", gated_factory, RESERVE))
    await _settle()
    assert sf.snapshot()["k"][0][2] == 3

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert sf.snapshot()["k"][0][2] == 2  # exactly-once own ref release
    assert sf.leased_bytes == RESERVE  # shared future untouched

    gate.set()
    leader_lease = await leader
    survivor_lease = await survivor
    assert leader_lease.body == b"shared"
    assert survivor_lease.body == b"shared"
    async with leader_lease, survivor_lease:
        pass
    # incidental past-grace tail → virtual clock + lazy sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


# ---------------------------------------------------------------------------
# Class ⑤ — leader cancellation → future.cancel() → waiters re-lead
# ---------------------------------------------------------------------------

async def test_leader_cancel_waiters_shield_relead_and_succeed():
    clock, sf = _fake_clock_registry()
    first_gate = asyncio.Event()
    calls: list[str] = []

    async def first_factory():
        calls.append("first")
        await first_gate.wait()
        return b"never"

    async def second_factory():
        calls.append("second")
        return b"rescued"

    factory_ref = {"fn": first_factory}

    async def factory():
        return await factory_ref["fn"]()

    leader = asyncio.create_task(sf.fetch_or_bypass("k", factory, RESERVE))
    await _settle()
    waiter = asyncio.create_task(sf.fetch_or_bypass("k", factory, RESERVE))
    await _settle()
    assert sf.snapshot()["k"][0][2] == 2

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    # leader-cancel refunded the budget immediately; the waiter's old ref was
    # released BEFORE it re-registered — capacity is exactly one reserve here
    # so the re-lead can only succeed under the dual refund rule.
    factory_ref["fn"] = second_factory
    lease = await waiter
    assert lease is not None
    assert lease.body == b"rescued"
    assert calls == ["first", "second"]
    async with lease:
        pass
    # incidental past-grace tail → virtual clock + lazy sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


# ---------------------------------------------------------------------------
# Class ⑥ — grace expiry (ownership release)
# ---------------------------------------------------------------------------

async def test_grace_expiry_moves_to_retired_then_refunds_on_last_release():
    sf = _registry(result_grace_seconds=0.05)

    async def factory():
        return b"body"

    lease = await sf.fetch_or_bypass("k", factory, RESERVE)
    seq = sf.snapshot()["k"][0][1]
    lease._release()  # caller released; grace still holds the body
    assert sf.leased_bytes == RESERVE
    # REAL loop time — this test's SUBJECT is the grace timer itself
    # (call_later dispatch → in-place retired/retained conversion →
    # zero-caller refund+delete). Bounded poll: converge to the empty
    # snapshot within a loose deadline; anything still visible past the
    # original 0.08s margin must already show the converted tombstone shape.
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + 2.0
    while sf.snapshot() != {}:
        await asyncio.sleep(0.01)
        now = loop.time()
        if now >= deadline:
            raise AssertionError("grace timer did not converge within 2s")
        snap = sf.snapshot()
        if snap and now - started > 0.08:
            assert snap["k"][0][0] == "retired"
            assert snap["k"][0][1] == seq  # in-place conversion: same seq
            assert snap["k"][0][3] == "retained"
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}

    # a new caller leads a FRESH flight (old one is a retired tombstone)
    lease2 = await sf.fetch_or_bypass("k", factory, RESERVE)
    assert lease2 is not None
    assert sf.snapshot()["k"][0][1] == seq + 1
    async with lease2:
        pass


async def test_grace_entry_joinable_by_straggler_within_window():
    sf = _registry(result_grace_seconds=0.5)

    async def factory():
        return b"body"

    lease1 = await sf.fetch_or_bypass("k", factory, RESERVE)
    lease2 = await sf.fetch_or_bypass("k", factory, RESERVE)  # straggler
    assert lease2 is not None and lease2.body == b"body"
    assert sf.snapshot()["k"][0][2] == 2
    async with lease1, lease2:
        pass


# ---------------------------------------------------------------------------
# Class ⑦ — budget eviction of zero-caller grace entries
# ---------------------------------------------------------------------------

async def test_budget_eviction_drops_oldest_zero_caller_grace_entry():
    clock, sf = _fake_clock_registry(max_bytes=100)  # fits 60 + 60 only after eviction

    async def factory_a():
        return b"A"

    async def factory_b():
        return b"B"

    lease_a = await sf.fetch_or_bypass("a", factory_a, RESERVE)
    lease_a._release()  # zero callers, grace window still holds the body
    assert sf.leased_bytes == RESERVE

    # Budget-driven SYNCHRONOUS eviction (serial-point ``_try_reserve`` —
    # clock-free): "b" is admitted by evicting zero-caller grace entry "a".
    lease_b = await sf.fetch_or_bypass("b", factory_b, RESERVE)
    assert lease_b is not None  # admitted by evicting "a"
    assert sf.leased_bytes == RESERVE  # exactly one reserve in the ledger
    async with lease_b:
        assert lease_b.body == b"B"
    # incidental past-grace tail ("b") → virtual clock + lazy sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


async def test_budget_no_eviction_when_callers_still_hold_refs():
    sf = _registry(max_bytes=100)

    async def factory_a():
        return b"A"

    async def factory_b():
        return b"B"

    lease_a = await sf.fetch_or_bypass("a", factory_a, RESERVE)
    # leader still holds its lease → grace body has a caller → NOT evictable
    lease_b = await sf.fetch_or_bypass("b", factory_b, RESERVE)
    assert lease_b is None  # cannot evict a body someone may still read
    assert sf.leased_bytes == RESERVE
    async with lease_a:
        pass


# ---------------------------------------------------------------------------
# Class ⑧ — shutdown: atomic conversion, full-ledger assertions
# ---------------------------------------------------------------------------

async def test_shutdown_inflight_detaches_then_retains_until_last_caller():
    sf = _registry()
    gate = asyncio.Event()

    async def gated_factory():
        await gate.wait()
        return b"detached-result"

    leader = asyncio.create_task(
        sf.fetch_or_bypass("k", gated_factory, RESERVE))
    await _settle()
    waiter = asyncio.create_task(
        sf.fetch_or_bypass("k", gated_factory, RESERVE))
    await _settle()

    sf.shutdown()
    snap = sf.snapshot()
    assert snap["k"][0][0] == "retired"  # detached (in-flight) tombstone
    assert snap["k"][0][2] == 2
    assert snap["k"][0][3] == "in-flight"
    assert sf.leased_bytes == RESERVE  # detached still counts

    gate.set()
    leader_lease, waiter_lease = await asyncio.gather(leader, waiter)
    snap = sf.snapshot()
    assert snap["k"][0][3] == "retained"  # in-place conversion, no grace/timer
    assert snap["k"][0][0] == "retired"
    assert sf.leased_bytes == RESERVE  # held until last caller releases

    leader_lease._release()
    assert sf.leased_bytes == RESERVE
    waiter_lease._release()
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


async def test_shutdown_grace_entry_becomes_retained_and_timer_cancelled():
    sf = _registry(result_grace_seconds=0.05)

    async def factory():
        return b"body"

    lease = await sf.fetch_or_bypass("k", factory, RESERVE)
    # Save entry ref BEFORE release (release severs lease._entry)
    entry = lease._entry
    timer_handle = entry.timer
    assert timer_handle is not None
    assert not timer_handle.cancelled()

    lease._release()  # grace window, zero callers

    # Timer still live pre-shutdown
    assert not timer_handle.cancelled()

    sf.shutdown()
    # Shutdown MUST cancel the timer handle
    assert timer_handle.cancelled()

    snap = sf.snapshot()
    if snap:  # zero-caller retained tombstone may already be reaped
        assert snap["k"][0][0] == "retired"
        assert snap["k"][0][3] == "retained"
    # Ledger stays converged past the original grace deadline — the
    # cancelled timer will never fire.
    loop = asyncio.get_running_loop()
    end = loop.time() + 0.1
    while True:
        assert sf.leased_bytes == 0
        assert sf.snapshot() == {}
        if loop.time() >= end:
            break
        await asyncio.sleep(0.01)


async def test_shutdown_failed_entry_refunds_immediately():
    sf = _registry()
    gate = asyncio.Event()

    async def failing_factory():
        await gate.wait()
        raise RuntimeError("boom")

    leader = asyncio.create_task(
        sf.fetch_or_bypass("k", failing_factory, RESERVE))
    await _settle()
    sf.shutdown()
    assert sf.leased_bytes == RESERVE  # detached in-flight still counted
    gate.set()
    with pytest.raises(RuntimeError, match="boom"):
        await leader
    assert sf.leased_bytes == 0  # detached failure → immediate refund
    assert sf.snapshot() == {}


# ---------------------------------------------------------------------------
# Class ⑨ — detached-leader-cancel + ownership in-place conversion special
# ---------------------------------------------------------------------------

async def test_detached_leader_cancel_after_shutdown_fails_exactly_once():
    sf = _registry()
    gate = asyncio.Event()

    async def gated_factory():
        await gate.wait()
        return b"never"

    leader = asyncio.create_task(
        sf.fetch_or_bypass("k", gated_factory, RESERVE))
    await _settle()
    sf.shutdown()
    assert sf.snapshot()["k"][0][3] == "in-flight"
    assert sf.leased_bytes == RESERVE

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    # no residual refs → the failed tombstone is reaped instantly; with
    # residual refs it would read ("retired", seq, n, "failed") instead.
    snap = sf.snapshot()
    if snap:
        assert snap["k"][0][0] == "retired"
        assert snap["k"][0][3] == "failed"
    assert sf.leased_bytes == 0  # exactly-once refund
    gate.set()  # nothing left to observe; entry converges empty
    await _settle()
    assert sf.snapshot() == {}


async def test_detached_leader_cancel_residual_waiter_refs_are_pure_counting():
    """After shutdown, a cancelled detached leader fails its flight while a
    waiter still holds a ref: the budget is refunded anyway (pure counting),
    and the waiter re-leads a FRESH flight successfully."""
    clock, sf = _fake_clock_registry()  # single-flight budget
    gate = asyncio.Event()
    calls: list[str] = []

    async def first_factory():
        calls.append("first")
        await gate.wait()
        return b"never"

    factory_ref = {"fn": first_factory}

    async def factory():
        return await factory_ref["fn"]()

    leader = asyncio.create_task(sf.fetch_or_bypass("k", factory, RESERVE))
    await _settle()
    waiter = asyncio.create_task(sf.fetch_or_bypass("k", factory, RESERVE))
    await _settle()
    assert sf.snapshot()["k"][0][2] == 2

    sf.shutdown()
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    # waiter's residual ref on the failed entry is pure counting — the
    # budget is already refunded, so the re-lead can reserve again.
    assert sf.leased_bytes == 0

    async def second_factory():
        calls.append("second")
        return b"rescued"

    factory_ref["fn"] = second_factory
    lease = await waiter  # shield branch → release old ref → re-lead
    assert lease is not None and lease.body == b"rescued"
    assert calls == ["first", "second"]
    async with lease:
        pass
    # incidental past-grace tail → virtual clock + lazy sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


async def test_leader_cancel_double_waiter_generation_interleave():
    """A2-C6 (v1.6/v1.7 — rev-gpt B2): double-waiter generation interleave.

    The leader is cancelled with TWO waiters joined. The first waiter to
    wake re-leads (new ACTIVE entry, seq=N+1) while the second waiter still
    holds its residual ref on the OLD retired failed entry (seq=N). The
    release handle is bound to the old ``_Entry`` OBJECT (never a fresh
    active-lookup by key), so the second waiter's later release decrements
    ONLY the old entry (deleting it at zero) and never the new one."""
    clock, sf = _fake_clock_registry()  # single-flight budget: the failed flight's
                        # immediate refund is what makes the re-lead reservable.
    gate1 = asyncio.Event()
    gate2 = asyncio.Event()
    observed: list[tuple[dict, int]] = []
    calls: list[str] = []

    async def first_factory():
        calls.append("first")
        await gate1.wait()
        return b"never"

    async def re_lead_factory():
        # Runs INSIDE the re-lead, in the same scheduling slot as the
        # re-leading waiter — i.e. BEFORE the second waiter processes its
        # wake-up. This IS the coexistence window.
        calls.append("re-lead")
        observed.append((sf.snapshot(), sf.leased_bytes))
        await gate2.wait()
        return b"rescued"

    leader = asyncio.create_task(sf.fetch_or_bypass("k", first_factory, RESERVE))
    await _settle()
    waiter_a = asyncio.create_task(
        sf.fetch_or_bypass("k", re_lead_factory, RESERVE))
    await _settle()
    waiter_b = asyncio.create_task(
        sf.fetch_or_bypass("k", re_lead_factory, RESERVE))
    await _settle()
    snap = sf.snapshot()["k"]
    assert len(snap) == 1
    seq_n = snap[0][1]
    assert snap[0][2] == 3  # leader + BOTH waiters registered pre-failure

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    await _settle()
    await _settle()

    # Coexistence window (captured inside the re-lead factory): BOTH
    # generations visible in the unified view, correctly layered.
    assert calls == ["first", "re-lead"]
    assert len(observed) == 1
    window_snap, window_ledger = observed[0]
    entries = window_snap["k"]
    assert len(entries) == 2
    by_seq = {e[1]: e for e in entries}
    old = by_seq[seq_n]
    new = by_seq[seq_n + 1]
    # old: retired layer, failed, only the second waiter's residual ref
    assert old == ("retired", seq_n, 1, "failed")
    # new: active layer, in-flight, only the re-leading waiter's ref
    assert new == ("active", seq_n + 1, 1, "in-flight")
    # old failed reserves NOTHING; the new generation alone is counted
    assert window_ledger == RESERVE

    # Post-cascade: the second waiter's release was DIRECTED at the old
    # entry — it hit zero and vanished; the new entry gained the second
    # waiter's join (refs=2), untouched by that release. (A key-based
    # release would instead leave the old entry lingering at refs=1 and
    # decrement the NEW entry to refs=1 — both caught here.)
    snap = sf.snapshot()["k"]
    assert snap == [("active", seq_n + 1, 2, "in-flight")]
    assert sf.leased_bytes == RESERVE

    gate2.set()
    lease_a = await waiter_a
    lease_b = await waiter_b
    assert lease_a is not None and lease_a.body == b"rescued"
    assert lease_b is not None and lease_b.body == b"rescued"
    lease_a._release()
    lease_b._release()
    # incidental past-grace tail → virtual clock + lazy sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


async def test_ownership_inplace_conversion_keeps_seq_within_layers():
    """Success never re-registers: active in-flight → active grace (same seq,
    same layer); only grace expiry/shutdown moves it to retired.

    Representative lazy-path proof: saves the entry + timer handle before
    release, asserts they are live pre-sweep, then proves the lazy serial-
    point sweep converges them (the 60s grace means the real call_later
    timer cannot race before the sweep)."""
    clock, sf = _fake_clock_registry()

    async def factory():
        return b"body"

    lease = await sf.fetch_or_bypass("k", factory, RESERVE)
    layer, seq, refs, state = sf.snapshot()["k"][0]
    assert (layer, state) == ("active", "grace")

    # Save entry + timer ref BEFORE release (release severs lease._entry)
    entry = lease._entry
    timer_handle = entry.timer
    assert timer_handle is not None
    assert not timer_handle.cancelled()

    lease._release()

    # Before sweep: entry/timer still live (real timer can't race with 60s)
    assert sf.snapshot() != {}
    assert not timer_handle.cancelled()

    # Sweep past grace → lazy path converges
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.snapshot() == {}

    lease2 = await sf.fetch_or_bypass("k", factory, RESERVE)
    layer2, seq2, refs2, state2 = sf.snapshot()["k"][0]
    assert seq2 == seq + 1  # fresh generation after full removal
    async with lease2:
        pass


# ---------------------------------------------------------------------------
# Ledger invariant + concurrency knob + registry reusability
# ---------------------------------------------------------------------------

async def test_network_concurrency_bounds_inflight_factories():
    sf = _registry(max_bytes=10_000, network_concurrency=2)
    peak = 0
    active = 0
    gates = [asyncio.Event() for _ in range(4)]

    async def make_factory(gate: asyncio.Event):
        async def factory():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await gate.wait()
            active -= 1
            return b"x"
        return factory

    tasks = [asyncio.create_task(
        sf.fetch_or_bypass(f"k{i}", await make_factory(gates[i]), 100))
        for i in range(4)]
    await _settle()
    await _settle()
    for gate in gates:
        gate.set()
    leases = await asyncio.gather(*tasks)
    assert peak <= 2  # network slot cap held across DISTINCT in-flight GETs
    for lease in leases:
        lease._release()


async def test_registry_reusable_after_shutdown():
    clock, sf = _fake_clock_registry()

    async def factory():
        return b"after"

    sf.shutdown()
    lease = await sf.fetch_or_bypass("k", factory, RESERVE)
    assert lease is not None and lease.body == b"after"
    async with lease:
        pass
    # incidental past-grace tail → virtual clock + lazy sweep
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0


async def test_ledger_invariant_across_mixed_lifecycle():
    """Stress-mix: leaders, waiters, cancellations, failures, shutdown —
    leased_bytes must equal the exact set of counted entries at every settle
    point, and must converge to zero after everyone releases."""
    clock, sf = _fake_clock_registry(max_bytes=600, network_concurrency=8)
    counted_states = {"in-flight", "grace", "retained"}

    def _assert_invariant() -> None:
        # EXACT ledger equation (rev-gpt B2: replaces the weak modulo
        # check). This fixture reserves exactly RESERVE bytes per flight,
        # so the per-entry sum over the snapshot's counted entries is
        # counted * RESERVE; failed entries reserve nothing.
        counted = 0
        for entries in sf.snapshot().values():
            for _layer, _seq, _refs, state in entries:
                assert state in {"in-flight", "grace", "retained", "failed"}
                if state in counted_states:
                    counted += 1
        assert sf.leased_bytes == counted * RESERVE

    async def ok_factory():
        return b"ok"

    async def bad_factory():
        raise RuntimeError("bad")

    async def worker(i: int):
        if i % 5 == 4:
            # may LEAD a failing flight (raises) or JOIN an existing good
            # flight (lease) — join semantics make both correct.
            try:
                lease = await sf.fetch_or_bypass(
                    f"k{i % 3}", bad_factory, RESERVE)
            except RuntimeError:
                return
            if lease is not None:
                async with lease:
                    await asyncio.sleep(0.005)
            return
        lease = await sf.fetch_or_bypass(f"k{i % 3}", ok_factory, RESERVE)
        if lease is None:
            return
        async with lease:
            await asyncio.sleep(0.01)
        _assert_invariant()

    results = await asyncio.gather(*(
        asyncio.create_task(worker(i)) for i in range(30)),
        return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException):
            raise r
    # past grace (virtual) + lazy serial-point sweep over every live key
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    _assert_invariant()
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


# ---------------------------------------------------------------------------
# Final review rev-1 blocker — release must CUT the Lease→body/_entry
# references: a caller that keeps the (released) Lease object alive across
# later awaits (e.g. a slow fan-out) must not keep the shared raw body
# reachable after the entry is reaped (grace expiry + budget refund). Old
# generations must never coexist as zombie bodies under sustained load.
# ---------------------------------------------------------------------------

async def test_release_cuts_body_and_entry_references():
    """Unit probe (registry level): after `__aexit__` the Lease severs both
    `body` and `_entry`; the body becomes unreachable as soon as the grace
    entry is reaped — even with the Lease object itself still alive."""
    import gc
    import weakref

    class _Body:
        """weakref-able stand-in for the shared value (Lease.body is Any;
        real routes share bytes, which cannot be weakref'd — hence the
        route-level probes use refcount calibration instead)."""

        __slots__ = ("payload", "__weakref__")

    sf = _registry(result_grace_seconds=0.05)
    holder: dict = {}

    async def factory():
        body = _Body()
        holder["ref"] = weakref.ref(body)
        return body

    def _body_unreachable() -> bool:
        gc.collect()
        return holder["ref"]() is None

    async def caller():
        lease = await sf.fetch_or_bypass("k", factory, RESERVE)
        assert lease is not None
        ref = holder["ref"]
        assert ref() is not None  # reachable pre-release (lease + entry)
        async with lease:
            pass  # release at exit
        # grace window: the reaped-not-yet entry's future still holds the
        # body BY DESIGN (straggler join window) — deterministic here since
        # the grace timer (0.05s) cannot have fired inside this synchronous
        # section.
        gc.collect()
        assert ref() is not None
        # keep the RELEASED Lease object itself alive too, across later
        # awaits (route pattern: local handle survives a slow fan-out),
        # past the grace expiry. REAL loop time: unreachability needs the
        # grace timer to have actually DISPATCHED (a merely pending
        # TimerHandle keeps its callback args — and thus the entry/body —
        # reachable on some CPython versions even after cancel), so this
        # cannot be virtualized with a FakeClock. Bounded poll.
        holder["lease"] = lease
        await _poll_until(_body_unreachable,
                          what="grace timer dispatch releasing the body")

    await caller()
    lease = holder["lease"]
    gc.collect()
    # post-release access is None (docstring'd semantics), the entry handle
    # is severed, and the body is unreachable despite the live Lease.
    assert lease is not None
    assert lease.body is None
    assert lease._entry is None
    assert holder["ref"]() is None
    # ledger fully refunded once grace reaped the zero-ref entry
    await asyncio.sleep(0)
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}


async def test_release_cut_does_not_break_release_accounting():
    """The cut must not disturb the dual refund rule: releasing still
    decrements the caller ref exactly once and a second (manual) release is
    a no-op even after the body was severed."""
    clock, sf = _fake_clock_registry()

    async def factory():
        return b"payload"

    lease = await sf.fetch_or_bypass("k", factory, RESERVE)
    assert lease is not None
    lease._release()
    assert lease.body is None
    lease._release()  # idempotent double release — no double decrement
    assert sf.leased_bytes == RESERVE  # grace window: budget still held
    # past (virtual) grace + lazy serial-point sweep → refund + delete
    clock.advance(FAKE_GRACE_SECONDS + 1)
    _sweep_lazy(sf)
    assert sf.leased_bytes == 0
    assert sf.snapshot() == {}
