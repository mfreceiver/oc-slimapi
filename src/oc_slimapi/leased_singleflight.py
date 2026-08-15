"""Leased single-flight registry with byte-budget admission (plan §3.x).

Traffic-optimization plan Batch 1, Task 1.2 (A2). This module is the
review-frozen protocol (rev-sgpt 9.6 PASS) for join-first upstream-GET
deduplication with an explicit lease discipline:

* Callers first acquire the RAW upstream body through
  :meth:`LeasedSingleFlight.fetch_or_bypass` — the shared unit is the
  upstream GET + cap-read ONLY. Per-caller projection/serialization stays
  in the routes (byte-identical to today).
* Byte budget: each registered flight reserves ``reserve_bytes``
  (worst-case read cap) up-front. When the budget cannot fit a reserve the
  caller receives ``None`` and must bypass (fetch directly — today's path).
  Reserves are NOT adjusted to actual body size (deliberate simplification).
* Two-layer registry: ``active`` (joinable: in-flight or grace) and
  ``retired`` (tombstones: never joinable, no timers). Failed entries do
  not hold budget; retained bodies hold budget until the last caller
  releases; detached in-flight entries (shutdown products) keep counting
  until their factory resolves.
* Cancellation state machine (three branches):
  1. factory regular exception → every waiter re-raises the SAME exception
     instance (FetchFailed envelope); budget refunded immediately — the
     entry never enters grace; residual refs are pure counting.
  2. leader cancelled → shared future is cancelled (NOT wrapped); surviving
     waiters release their old ref FIRST, then re-join / re-reserve /
     re-lead at the serial point. The immediate refund on failure is what
     makes the re-lead possible under a single-flight budget.
  3. waiter itself cancelled (including the registered-ref → await window)
     → own caller ref released exactly once, CancelledError propagates, the
     shared future is never cancelled and other callers are unaffected.
* ``shutdown()`` atomically converts every active entry: in-flight →
  retired/detached (still counted, future untouched); grace → retained
  (grace ownership released, budget kept until the last caller releases).
  Registry stays usable afterwards (CD-1 semantics).
* Ledger invariant: ``leased_bytes == sum(reserve_bytes of entries in
  ownership_state ∈ {in-flight (incl. detached), grace, retained})``.

This module is INDEPENDENT of ``sse/singleflight.py`` (CD-1, in production
for /full): that class stays byte-for-byte untouched.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Hashable

FactoryT = Callable[[], Awaitable[Any]]

# Ownership states (plan §3.x ledger vocabulary).
IN_FLIGHT = "in-flight"
GRACE = "grace"
RETAINED = "retained"
FAILED = "failed"

# Registry layers.
ACTIVE = "active"
RETIRED = "retired"

_DEFAULT_RESULT_GRACE_SECONDS = 1.0


class FetchFailed:
    """Envelope carrying a factory failure to joined waiters.

    Waiters unwrap and re-raise ``exc`` — every caller of the failed flight
    observes the SAME exception instance. Failures are never negative-
    cached: a later caller leads a fresh flight immediately.
    """

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _Entry:
    """One flight generation. ``(key, seq)`` uniquely identifies it."""

    __slots__ = (
        "key", "seq", "future", "caller_refs", "state", "layer",
        "reserve_bytes", "accounted", "expires_at", "timer",
    )

    def __init__(self, key: Hashable, seq: int, reserve_bytes: int) -> None:
        self.key = key
        self.seq = seq
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.caller_refs = 0
        self.state = IN_FLIGHT
        self.layer = ACTIVE
        self.reserve_bytes = reserve_bytes
        # True while this entry's reserve_bytes are inside leased_bytes.
        # Exactly one refund per accounted lifetime (dual refund rule).
        self.accounted = False
        self.expires_at: float | None = None
        self.timer: asyncio.TimerHandle | None = None


def _current_task_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class Lease:
    """Caller's handle to a shared flight result.

    ``body`` is whatever the leader's factory returned (conceptually the raw
    upstream bytes; routes may share a small tuple such as
    ``(body, next_cursor)`` when the upstream response carries route-relevant
    headers). Release binds the ENTRY directly — no active-layer lookup — so
    releasing after ``shutdown()`` keeps working. Release is exactly-once
    (idempotent guard): ``__aexit__`` finally semantics + manual double
    releases are all safe.

    **Post-release semantics (final review rev-1 blocker)**: ``_release()``
    SEVERS the handle's references — after release, ``body`` reads ``None``
    and the entry handle is gone. A released Lease must never keep the
    shared raw body reachable across later awaits (callers routinely keep
    the Lease object alive in local scope through a fan-out while grace
    expiry refunds the budget and new generations are admitted — old
    generations would otherwise survive as zombie bodies). Every
    ``lease.body`` access in src (7 sites) happens INSIDE the
    ``async with lease`` window. Some routes (sessions status, per-dir
    questions/permissions) read the bytes reference inside the window and
    finish consuming it (orjson parse) SYNCHRONOUSLY after exit — no await
    between the read and the consume, so no zombie window exists.
    """

    __slots__ = ("_registry", "_entry", "body", "_released")

    def __init__(self, registry: "LeasedSingleFlight", entry: _Entry, body: Any) -> None:
        self._registry = registry
        self._entry = entry
        self.body = body
        self._released = False

    async def __aenter__(self) -> "Lease":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._release()
        return False

    def _release(self) -> None:
        if self._released:
            return
        self._released = True
        entry = self._entry
        # Cut the reference chain FIRST (final review rev-1): a released
        # Lease must not keep the shared body / entry reachable. The
        # idempotent guard above stays — the cut happens exactly once and
        # the caller-ref decrement below runs exactly once either way.
        self.body = None
        self._entry = None
        if entry is not None:
            self._registry._release_caller(entry)


class LeasedSingleFlight:
    """Join-first single-flight registry with leased byte accounting."""

    def __init__(
        self,
        *,
        max_bytes: int,
        network_concurrency: int | None = None,
        result_grace_seconds: float = _DEFAULT_RESULT_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_bytes = max_bytes
        self._grace = float(result_grace_seconds)
        self._clock = clock
        self._active: dict[Hashable, _Entry] = {}
        self._retired: dict[tuple[Hashable, int], _Entry] = {}
        self._leased_bytes = 0
        self._seq = 0
        # Optional cap on concurrently-running leader factories (the actual
        # upstream GETs). Bypass-path fetchers never pass through here.
        self._network_sem = (
            asyncio.Semaphore(network_concurrency)
            if network_concurrency is not None else None
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def leased_bytes(self) -> int:
        return self._leased_bytes

    def snapshot(self) -> dict[Hashable, list[tuple[str, int, int, str]]]:
        """Unified ledger view: ``{key: [(layer, seq, caller_refs,
        ownership_state), ...]}`` across both registry layers."""
        out: dict[Hashable, list[tuple[str, int, int, str]]] = {}
        for entry in self._active.values():
            out.setdefault(entry.key, []).append(
                (ACTIVE, entry.seq, entry.caller_refs, entry.state)
            )
        for entry in self._retired.values():
            out.setdefault(entry.key, []).append(
                (RETIRED, entry.seq, entry.caller_refs, entry.state)
            )
        return out

    # ------------------------------------------------------------------
    # Fetch path
    # ------------------------------------------------------------------

    async def fetch_or_bypass(
        self,
        key: Hashable,
        factory: FactoryT,
        reserve_bytes: int,
    ) -> Lease | None:
        """Join or lead a shared flight; ``None`` = budget full → bypass.

        The join/lead decision happens at a synchronous serial point (no
        ``await`` above it), so an existing flight is always atomically
        joined (waiter ref counted BEFORE awaiting) and a fresh flight is
        registered only after a successful try-reserve.
        """
        while True:
            # ---------------- serial point (no await above) ----------------
            self._expire_if_due(key)
            entry = self._active.get(key)
            if entry is None:
                if not self._try_reserve(reserve_bytes):
                    return None  # budget full → caller bypasses (today's path)
                entry = self._new_entry(key, reserve_bytes)
                self._active[key] = entry
                entry.caller_refs += 1  # leader ref, registered BEFORE factory
                return await self._lead(entry, factory)
            # Existing joinable flight (in-flight or grace): join it.
            entry.caller_refs += 1  # waiter ref BEFORE await (join window)
            try:
                result = await asyncio.shield(entry.future)
            except asyncio.CancelledError:
                if _current_task_cancelling():
                    # Branch ③: THIS caller was cancelled (incl. the
                    # registered-ref → resolve window). Release the own ref
                    # exactly once and propagate; the shared future and all
                    # other callers are unaffected.
                    self._release_caller(entry)
                    raise
                # Branch ② fall-out: the shared flight died (leader
                # cancelled). Release the OLD ref first, then re-enter the
                # serial point to re-join / re-reserve / re-lead.
                self._release_caller(entry)
                continue
            if isinstance(result, FetchFailed):
                # Branch ①: factory failure envelope → same instance to all.
                self._release_caller(entry)
                raise result.exc
            return Lease(self, entry, result)

    async def _lead(self, entry: _Entry, factory: FactoryT) -> Lease:
        try:
            if self._network_sem is not None:
                async with self._network_sem:
                    result = await factory()
            else:
                result = await factory()
        except asyncio.CancelledError:
            # Branch ②: leader cancelled → fail the flight (immediate
            # refund, future.cancel()) and propagate to the leader caller.
            self._fail(entry)
            raise
        except BaseException as exc:
            # Branch ①: factory failure → envelope for waiters, immediate
            # refund, no grace.
            self._fail(entry, exc)
            raise
        # Success: publish to waiters first, then convert ownership.
        if not entry.future.done():
            entry.future.set_result(result)
        self._convert_success(entry)
        return Lease(self, entry, result)

    # ------------------------------------------------------------------
    # Ownership transitions (all synchronous — serial points)
    # ------------------------------------------------------------------

    def _convert_success(self, entry: _Entry) -> None:
        if entry.layer == ACTIVE and entry.state == IN_FLIGHT:
            # In-place conversion: registry ownership becomes a grace window
            # (joinable by stragglers). No re-registration — same seq/layer.
            entry.state = GRACE
            entry.expires_at = self._clock() + self._grace
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover — always in a loop here
                loop = None
            if loop is not None:
                entry.timer = loop.call_later(
                    self._grace, self._expire_grace, entry
                )
        elif entry.layer == RETIRED and entry.state == IN_FLIGHT:
            # Shutdown-detached success: retained variant — no grace, no
            # timer, no re-accounting; budget stays until the last caller.
            entry.state = RETAINED

    def _fail(self, entry: _Entry, exc: BaseException | None = None) -> None:
        """Leader failure path — dual refund rule, branch ②/①.

        Order matters: leader ref released first, active registration
        dropped, budget refunded IMMEDIATELY (before waiters wake, so a
        waiter's re-lead can reserve the freed bytes), then the future is
        failed and the (now purely counting) tombstone reaped if childless.
        """
        self._release_caller(entry)
        self._detach_from_active(entry)
        entry.layer = RETIRED
        entry.state = FAILED
        self._retired[(entry.key, entry.seq)] = entry
        self._refund(entry)
        if not entry.future.done():
            if exc is None:
                entry.future.cancel()  # branch ②: cancellation, not envelope
            else:
                entry.future.set_result(FetchFailed(exc))  # branch ①
        if entry.caller_refs == 0:
            self._reap(entry)

    def _expire_grace(self, entry: _Entry) -> None:
        """Grace deadline: release registry ownership → retired/retained."""
        if entry.state != GRACE or entry.layer != ACTIVE:
            return  # already converted (shutdown/eviction) — defensive
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None
        self._drop_grace(entry)

    def _expire_if_due(self, key: Hashable) -> None:
        entry = self._active.get(key)
        if (
            entry is not None
            and entry.state == GRACE
            and entry.expires_at is not None
            and self._clock() >= entry.expires_at
        ):
            self._expire_grace(entry)

    def _drop_grace(self, entry: _Entry) -> None:
        """active/grace → retired/retained (+ reap when caller-less)."""
        if self._active.get(entry.key) is entry:
            del self._active[entry.key]
        entry.layer = RETIRED
        entry.state = RETAINED
        self._retired[(entry.key, entry.seq)] = entry
        if entry.caller_refs == 0:
            self._reap(entry)

    def _release_caller(self, entry: _Entry) -> None:
        if entry.caller_refs > 0:
            entry.caller_refs -= 1
        if entry.caller_refs != 0:
            return
        # Refcount hit zero — state decides the fate:
        if entry.state == RETAINED:
            self._reap(entry)  # success path end: refund + delete
        elif entry.state == FAILED:
            self._reap(entry)  # budget already refunded; pure count → delete
        # GRACE: body stays for stragglers until expiry.
        # IN_FLIGHT (incl. detached): factory unresolved; budget stays.

    def _reap(self, entry: _Entry) -> None:
        """Delete a caller-less tombstone; refund if still accounted."""
        self._refund(entry)
        self._retired.pop((entry.key, entry.seq), None)
        if self._active.get(entry.key) is entry:
            del self._active[entry.key]  # defensive: never expected here

    def _refund(self, entry: _Entry) -> None:
        if entry.accounted:
            entry.accounted = False
            self._leased_bytes -= entry.reserve_bytes

    def _detach_from_active(self, entry: _Entry) -> None:
        if self._active.get(entry.key) is entry:
            del self._active[entry.key]

    # ------------------------------------------------------------------
    # Budget admission
    # ------------------------------------------------------------------

    def _try_reserve(self, needed: int) -> bool:
        if needed > self._max_bytes:
            return False  # a single flight larger than the whole budget
        if self._leased_bytes + needed <= self._max_bytes:
            return True
        # Serial-point eviction (mirrors CD-1 discipline): drop zero-caller
        # grace entries, oldest (insertion-order) first, until it fits.
        for key in list(self._active.keys()):
            if self._leased_bytes + needed <= self._max_bytes:
                break
            entry = self._active[key]
            if entry.state == GRACE and entry.caller_refs == 0:
                self._expire_grace(entry)  # cancels timer, reaps (refs==0)
        return self._leased_bytes + needed <= self._max_bytes

    def _new_entry(self, key: Hashable, reserve_bytes: int) -> _Entry:
        self._seq += 1
        entry = _Entry(key, self._seq, reserve_bytes)
        entry.accounted = True
        self._leased_bytes += reserve_bytes
        return entry

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Atomically convert every active entry (no awaits inside).

        * in-flight → retired/detached: keeps counting, future untouched —
          the leader's factory still resolves it (success → retained,
          failure/cancel → failed + exactly-once refund).
        * grace → retained: grace ownership released (timer cancelled);
          budget held until the last caller releases; caller-less entries
          are reaped immediately.

        The registry stays usable afterwards (CD-1 semantics): new fetches
        simply create fresh entries.
        """
        for key, entry in list(self._active.items()):
            if entry.timer is not None:
                entry.timer.cancel()
                entry.timer = None
            del self._active[key]
            entry.layer = RETIRED
            self._retired[(entry.key, entry.seq)] = entry
            if entry.state == IN_FLIGHT:
                pass  # detached in-flight — resolution continues counting
            elif entry.state == GRACE:
                entry.state = RETAINED
                if entry.caller_refs == 0:
                    self._reap(entry)
