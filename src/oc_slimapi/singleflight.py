"""Single-flight dedup of shared upstream GETs — ONE implementation, two
profiles (B6-1 merge of the former plain ``sse/singleflight.py`` (L2-CD-1)
and leased ``leased_singleflight.py`` (traffic plan Batch 1 / A2)).

Several in-flight requests often need the exact same upstream body —
concurrent direct /full calls, merged-fan-out raw fetches racing them
(CD-2), list-route GETs during directory scans. This module collapses them
onto ONE upstream GET per key. Both profiles share the whole flight
machinery (FetchFailed envelope, three-branch cancellation, shield-join
loop, lead/failure path, grace timers, shutdown convergence) and differ
ONLY in admission/retention bookkeeping:

* **Plain** (``max_bytes=None``): join-or-lead ``fetch()`` — never
  bypasses. The completed result stays joinable for a short grace window
  so admission-serialized peers (``max_transforms`` defaults to 1 — direct
  /full requests queue at the pool BEFORE they can join) still coalesce
  onto the leader's GET. The grace window is a dedup artefact, NOT a
  cache (unvalidated, ~1s, bounded in count and bytes). Callers: the
  process-level ``fulls`` registry (direct /full + merged fan-out) and the
  catalog-cache refresh stampede guard.
* **Leased** (``max_bytes`` required): ``fetch_or_bypass()`` with
  byte-budget admission and lease discipline. The shared unit is the
  upstream GET + cap-read ONLY — per-caller projection/serialization stays
  in the routes (byte-identical to a direct fetch). Each flight reserves
  ``reserve_bytes`` (worst-case read cap) up-front; a reserve the budget
  cannot fit returns ``None`` → the caller bypasses (fetches directly —
  today's path). Reserves are NOT adjusted to actual body size
  (deliberate simplification). Caller: the per-app ``raw_fetch_registry``
  for list-route upstream GETs.

Scope boundary (L2-CD-1, oracle §C-2): ONLY the raw fetch is shared —
transforms (strip/pack offloads) stay with each caller; nothing here
knows about the transform pool.

Shared flight semantics (both profiles):

* **Failures propagate to every waiter** — all joined callers re-raise
  the SAME exception instance, never negatively cached (entry dropped,
  next request retries). Delivery uses a ``FetchFailed(exc)`` RESULT
  envelope (``set_result``, never ``set_exception``) so a leader that
  fails with zero waiters cannot leak ``Future exception was never
  retrieved`` warnings.
* **Cancellation state machine (three branches):**
  1. factory regular exception → waiters re-raise the same instance;
     leased: budget refunded IMMEDIATELY (never enters grace; residual
     refs are pure counting).
  2. leader cancelled → shared future ``.cancel()`` (NOT wrapped);
     surviving waiters release their old ref FIRST, then re-join /
     re-reserve / re-lead at the serial point — the immediate refund is
     what makes the re-lead possible under a single-flight budget.
  3. waiter itself cancelled (incl. the registered-ref → await window) →
     own ref released exactly once, CancelledError propagates, the shared
     future is never cancelled and other callers are unaffected.

Plain retention is actively bounded: completion accounts actual bytes
(``len(result)``; bytes/bytearray else 0) and runs eviction IMMEDIATELY;
every retained entry schedules an active ``loop.call_later`` expiry at
its grace deadline. The count/byte bounds are enforced at two serial
points (after insertion, after completion), so they hold under any
interleaving the event loop can produce; eviction drops the oldest
COMPLETED entries and NEVER in-flight ones (bounded by the callers' own
admission).

Leased registry is two-layer: ``_entries`` (joinable: in-flight or grace)
and ``_retired`` (tombstones: never joinable, no timers). Failed entries
hold no budget; retained bodies hold budget until the last caller
releases; detached in-flight entries (shutdown products) keep counting
until their factory resolves. Ledger invariant: ``leased_bytes == Σ
reserve of {in-flight (incl. detached), grace, retained}``.

``shutdown()`` converges the registry and leaves it USABLE afterwards
(CD-1 semantics): plain clears every entry + zeroes the retained-bytes
ledger (a stopped app must not leave old app-domain bodies or loop
callbacks behind); leased atomically converts active entries (in-flight
→ retired/detached, still counted, future untouched; grace → retained,
caller-less reaped immediately). In-flight futures are NEVER cancelled —
their factories resolve them naturally, and the late completion path
re-checks registration identity so nothing re-registers, re-accounts, or
re-arms a timer after the fact.

Keys embed the caller-supplied scope (the app's transform-pool identity)
so two app instances with different upstreams — e.g. tests with their own
``MockTransport`` — never share a flight.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Hashable

FactoryT = Callable[[], Awaitable[Any]]

# How long a COMPLETED fetch result stays joinable. Must comfortably cover
# an admission-queue drain of same-key requests (each peer only needs the
# few milliseconds of its own offload once admitted), while keeping the
# freshness window for a repeated same-key /full small.
_DEFAULT_RESULT_GRACE_SECONDS = 1.0

# Bounds on retained (completed) entries — defence in depth against grace
# retention growing without limit under a pathological key churn rate.
# In-flight entries are never evicted (they're inherently bounded by the
# callers' own admission).
_MAX_RETAINED_ENTRIES = 64
_MAX_RETAINED_BYTES = 32 * 1024 * 1024

# Ownership states (leased ledger vocabulary; plain entries stay
# IN_FLIGHT/ACTIVE for their whole life and discriminate via expires_at).
IN_FLIGHT = "in-flight"
GRACE = "grace"
RETAINED = "retained"
FAILED = "failed"

# Registry layers.
ACTIVE = "active"
RETIRED = "retired"

# Sentinel returned by _join when the awaited flight died (branch ②
# fall-out): the caller must re-enter its serial point. Unique object —
# a factory result can never collide with it.
_REJOIN = object()


class FetchFailed:
    """RESULT envelope for a failed flight (never an exception on the future).

    The leader writes ``FetchFailed(exc)`` via ``set_result`` so that a
    failure with zero waiters stays silent (results never trigger the
    "Future exception was never retrieved" warning that a ``set_exception``
    future would). Waiters unwrap it in the join path and re-raise the
    wrapped exception — every caller of the failed flight observes the SAME
    exception instance.
    """

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _Entry:
    """One flight generation (superset for both profiles).

    ``(key, seq)`` uniquely identifies a generation in the leased profile's
    retired layer. The plain profile uses ``key`` alone (flat active
    registry, no tombstones), keeps ``caller_refs`` at 0 / ``state`` at
    IN_FLIGHT forever (so the shared release path is a natural no-op), and
    discriminates in-flight vs grace via ``expires_at`` — None while the
    flight is running, a monotonic deadline once the result is retained
    for the grace window.
    """

    __slots__ = (
        "key", "seq", "future", "caller_refs", "state", "layer",
        "reserve_bytes", "accounted", "expires_at", "timer", "size",
    )

    def __init__(self, key: Hashable, seq: int, reserve_bytes: int = 0) -> None:
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
        # Pending grace-expiry TimerHandle (set only once retained), so
        # shutdown() can cancel it instead of leaving loop callbacks
        # pointing at a dead app domain (rev-9 lifecycle fix).
        self.timer: asyncio.TimerHandle | None = None
        # Retained-bytes contribution (plain profile; counted only after
        # completion, from the actual ``len(result)``).
        self.size = 0

    @property
    def in_flight(self) -> bool:
        return self.expires_at is None


def _current_task_cancelling() -> bool:
    """True if the CURRENT task has a real cancellation pending (3.11+),
    i.e. the CancelledError we caught is ours and not the flight's."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class Lease:
    """Caller's handle to a shared flight result (leased profile).

    ``body`` is whatever the leader's factory returned (conceptually the
    raw upstream bytes; routes may share a small tuple such as
    ``(body, next_cursor)`` when the upstream response carries
    route-relevant headers). Release binds the ENTRY directly — no
    active-layer lookup — so releasing after ``shutdown()`` keeps working;
    exactly-once (idempotent guard) so ``__aexit__`` finally semantics +
    manual double releases are all safe.

    **Post-release semantics (final review rev-1 blocker)**: ``_release()``
    SEVERS the handle's references — after release, ``body`` reads ``None``
    and the entry handle is gone. A released Lease must never keep the
    shared raw body reachable across later awaits (callers routinely keep
    the Lease object alive in local scope through a fan-out while grace
    expiry refunds the budget and new generations are admitted — old
    generations would otherwise survive as zombie bodies). Every
    ``lease.body`` access in src happens INSIDE the ``async with lease``
    window, or reads the bytes reference inside the window and finishes
    consuming it (orjson parse) SYNCHRONOUSLY after exit — no await
    between the read and the consume, so no zombie window exists.
    """

    __slots__ = ("_registry", "_entry", "body", "_released")

    def __init__(self, registry: "SingleFlight", entry: _Entry, body: Any) -> None:
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


def full_fetch_key(
    scope: object, sid: str, mid: str, directory: str | None,
) -> tuple[str, int, str, str, str | None]:
    """Stable single-flight key for a full-message upstream GET.

    ``scope`` namespaces the key per app instance (messages.py passes the
    app's TransformPool). ``directory`` must be part of the key: the same
    (sid, mid) under two directories is two different upstream resources.
    The CD-2 merged fan-out MUST reuse this builder so its raw per-mid
    fetches dedup against direct /full requests.
    """
    return ("full", id(scope), sid, mid, directory)


class SingleFlight:
    """Per-key single-flight dedup of a factory call — one class, two
    profiles selected at construction.

    ``max_bytes=None`` (plain): ``fetch(key, factory)`` — the first caller
    for a key runs ``factory()`` (the leader); concurrent and grace-window
    callers await the same result; retention is bounded in count and bytes.

    ``max_bytes`` set (leased): ``fetch_or_bypass(key, factory,
    reserve_bytes)`` — join-or-lead under byte-budget admission, returning
    a :class:`Lease` the caller must release, or ``None`` when the budget
    is full (caller bypasses).

    Not thread-safe — asyncio single-thread use only.
    """

    def __init__(
        self,
        *,
        max_bytes: int | None = None,
        network_concurrency: int | None = None,
        result_grace_seconds: float = _DEFAULT_RESULT_GRACE_SECONDS,
        max_retained_entries: int | None = None,
        max_retained_bytes: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``max_bytes`` present → leased profile (budget + leases);
        ``max_bytes=None`` → plain. Plain kwargs: ``result_grace_seconds``,
        ``max_retained_entries``/``max_retained_bytes`` (``None`` →
        defaults 64 / 32 MiB). Leased kwargs: ``max_bytes``,
        ``network_concurrency`` (leader factories only — bypass never
        passes through), ``result_grace_seconds``, ``clock`` (injectable
        monotonic clock). Profile-only kwargs are rejected up-front with
        ``TypeError`` (a leased registry bounds budget, not retention;
        a plain registry admits every flight — no concurrency cap).
        ``result_grace_seconds``/``clock`` are shared by both profiles."""
        self._leased = max_bytes is not None
        if self._leased:
            if (
                max_retained_entries is not None
                or max_retained_bytes is not None
            ):
                raise TypeError(
                    "max_retained_entries/max_retained_bytes are "
                    "plain-profile-only kwargs (max_bytes=None): a leased "
                    "registry (max_bytes set) bounds budget, not retention"
                )
        elif network_concurrency is not None:
            raise TypeError(
                "network_concurrency is a leased-profile-only kwarg: a "
                "plain registry (max_bytes=None) admits every flight"
            )
        self._leased = max_bytes is not None
        self._grace = float(result_grace_seconds)
        self._clock = clock
        # Joinable registry (in-flight or grace), by key — this is the
        # plain profile's flat `_entries` view AND the leased profile's
        # ACTIVE layer.
        self._entries: dict[Hashable, _Entry] = {}
        # Leased tombstones (failed/retired): never joinable, no timers.
        self._retired: dict[tuple[Hashable, int], _Entry] = {}
        self._leased_bytes = 0
        self._retained_bytes = 0
        self._seq = 0
        self._network_sem = (
            asyncio.Semaphore(network_concurrency)
            if network_concurrency is not None else None
        )
        if self._leased:
            self._max_bytes = max_bytes
        else:
            # Injection points for the retention-bound tests (rev-fix 1);
            # the module constants are the production defaults.
            self._max_entries = (
                _MAX_RETAINED_ENTRIES if max_retained_entries is None
                else max_retained_entries
            )
            self._max_bytes = (
                _MAX_RETAINED_BYTES if max_retained_bytes is None
                else max_retained_bytes
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def leased_bytes(self) -> int:
        """Leased profile: current budget usage (Σ reserves of counted
        entries — see the module docstring's ledger invariant)."""
        return self._leased_bytes

    def in_flight(self, key: Hashable) -> bool:
        """True while a fetch for ``key`` is running (not grace-retained)."""
        entry = self._entries.get(key)
        return entry is not None and entry.expires_at is None

    def snapshot(self) -> dict[Hashable, list[tuple[str, int, int, str]]]:
        """Leased profile: unified ledger view ``{key: [(layer, seq,
        caller_refs, ownership_state), ...]}`` across both layers."""
        out: dict[Hashable, list[tuple[str, int, int, str]]] = {}
        for entry in self._entries.values():
            out.setdefault(entry.key, []).append(
                (ACTIVE, entry.seq, entry.caller_refs, entry.state)
            )
        for entry in self._retired.values():
            out.setdefault(entry.key, []).append(
                (RETIRED, entry.seq, entry.caller_refs, entry.state)
            )
        return out

    # ------------------------------------------------------------------
    # Fetch paths (one shared join/lead machinery, two entry points)
    # ------------------------------------------------------------------

    async def fetch(self, key: Hashable, factory: FactoryT) -> Any:
        """Plain profile: return the shared result for ``key``, running
        ``factory`` at most once per flight. See the module docstring for
        failure/cancellation semantics."""
        if self._leased:
            raise RuntimeError(
                "fetch() is the plain-profile API; a leased-profile "
                "registry (max_bytes set) must use fetch_or_bypass()"
            )
        while True:
            self._expire_if_due(key)
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(key, 0, 0)
                self._entries[key] = entry
                self._evict_over_budget()
                return await self._lead(entry, factory)
            result = await self._join(entry)
            if result is not _REJOIN:
                return result

    async def fetch_or_bypass(
        self,
        key: Hashable,
        factory: FactoryT,
        reserve_bytes: int,
    ) -> Lease | None:
        """Leased profile: join or lead a shared flight; ``None`` = budget
        full → bypass.

        The join/lead decision happens at a synchronous serial point (no
        ``await`` above it), so an existing flight is always atomically
        joined (waiter ref counted BEFORE awaiting) and a fresh flight is
        registered only after a successful try-reserve.
        """
        if not self._leased:
            raise RuntimeError(
                "fetch_or_bypass() is the leased-profile API; a "
                "plain-profile registry (max_bytes=None) must use fetch()"
            )
        while True:
            # ---------------- serial point (no await above) ----------------
            self._expire_if_due(key)
            entry = self._entries.get(key)
            if entry is None:
                if not self._try_reserve(reserve_bytes):
                    return None  # budget full → caller bypasses (today's path)
                entry = self._new_entry(key, reserve_bytes)
                self._entries[key] = entry
                entry.caller_refs += 1  # leader ref, registered BEFORE factory
                return Lease(self, entry, await self._lead(entry, factory))
            # Existing joinable flight (in-flight or grace): join it.
            entry.caller_refs += 1  # waiter ref BEFORE await (join window)
            result = await self._join(entry)
            if result is _REJOIN:
                continue
            return Lease(self, entry, result)

    async def _join(self, entry: _Entry) -> Any:
        """Await an existing (in-flight or grace) flight — the three-branch
        cancellation machine (module docstring). Returns the result, or
        ``_REJOIN`` when the flight died and the caller must re-enter its
        serial point. ``_release_caller`` is a no-op on the plain profile
        (entries keep ``caller_refs`` at 0), so the shared branch structure
        is behaviour-identical for both profiles."""
        try:
            result = await asyncio.shield(entry.future)
        except asyncio.CancelledError:
            self._release_caller(entry)
            if _current_task_cancelling():
                # Branch ③: THIS caller was cancelled (incl. the
                # registered-ref → resolve window). Own ref released
                # exactly once (leased) and CancelledError propagates; the
                # shared future and all other callers are unaffected.
                raise
            # Branch ② fall-out: the shared flight died (leader
            # cancelled). The OLD ref is released FIRST; the caller
            # re-enters the serial point to re-join / re-reserve / re-lead.
            return _REJOIN
        if isinstance(result, FetchFailed):
            # Branch ①: factory failure envelope → same instance to all.
            self._release_caller(entry)
            raise result.exc
        return result

    async def _lead(self, entry: _Entry, factory: FactoryT) -> Any:
        """Run ``factory`` as the flight's leader (shared by both profiles;
        the entry is already registered by the caller). Returns the result —
        the leased entry point wraps it in a Lease."""
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
        self._convert_success(entry, result)
        return result

    # ------------------------------------------------------------------
    # Ownership transitions (all synchronous — serial points)
    # ------------------------------------------------------------------

    def _convert_success(self, entry: _Entry, result: Any) -> None:
        """Success ownership conversion. Both profiles publish to waiters
        FIRST (in ``_lead``) and re-validate that the entry is still the
        registered one before ANY retention bookkeeping — a ``shutdown()``
        (or concurrent dict replacement) may have moved/dropped the entry
        while the factory ran; waiters already awaiting the future still
        get their result either way."""
        if not self._leased:
            # Plain retention: grace window + actual-size byte accounting +
            # active expiry timer. There is no ``await`` between accounting
            # and eviction, so this is an event-loop serial point: the
            # bounds hold even when many flights complete back-to-back. The
            # just-completed entry may be evicted itself (it is the newest,
            # so only when it alone busts the byte bound) — hence the second
            # identity check before scheduling the timer (a self-evicted
            # entry schedules no pointless callback).
            if self._entries.get(entry.key) is entry:
                entry.expires_at = self._clock() + self._grace
                entry.size = (
                    len(result) if isinstance(result, (bytes, bytearray)) else 0
                )
                self._retained_bytes += entry.size
                self._evict_over_budget()
                if self._entries.get(entry.key) is entry:
                    # Active cleanup at the grace deadline. The callback
                    # re-checks the entry's identity: if this entry was
                    # already dropped/evicted and a NEW flight (even for
                    # the same key) sits in the dict, the guard no-ops —
                    # stale timers are harmless (and shutdown() cancels
                    # the handles).
                    entry.timer = asyncio.get_running_loop().call_later(
                        max(0.0, entry.expires_at - self._clock()),
                        self._expire_grace_entry, entry.key, entry,
                    )
            return
        if entry.layer == ACTIVE and entry.state == IN_FLIGHT:
            # In-place conversion: registry ownership becomes a grace window
            # (joinable by stragglers). No re-registration — same seq/layer.
            entry.state = GRACE
            entry.expires_at = self._clock() + self._grace
            entry.timer = asyncio.get_running_loop().call_later(
                self._grace, self._expire_grace_entry, entry.key, entry,
            )
        elif entry.layer == RETIRED and entry.state == IN_FLIGHT:
            # Shutdown-detached success: retained variant — no grace, no
            # timer, no re-accounting; budget stays until the last caller.
            entry.state = RETAINED

    def _fail(self, entry: _Entry, exc: BaseException | None = None) -> None:
        """Leader failure path (branch ② when ``exc`` is None, else ①).
        Plain: drop the entry (never negative-cache), then deliver. Leased
        — order matters: leader ref released first, active registration
        dropped, budget refunded IMMEDIATELY (before waiters wake, so a
        waiter's re-lead can reserve the freed bytes), future failed,
        childless tombstone reaped."""
        if not self._leased:
            self._drop(entry.key)
            self._fail_future(entry, exc)
            return
        self._release_caller(entry)
        self._detach_from_active(entry)
        entry.layer = RETIRED
        entry.state = FAILED
        self._retired[(entry.key, entry.seq)] = entry
        self._refund(entry)
        self._fail_future(entry, exc)
        if entry.caller_refs == 0:
            self._reap(entry)

    def _fail_future(self, entry: _Entry, exc: BaseException | None) -> None:
        """Deliver a leader failure to the shared future — FetchFailed
        RESULT envelope for a regular exception (never ``set_exception``:
        a zero-waiter failure must not leak "Future exception was never
        retrieved"), plain ``cancel()`` for a leader cancellation."""
        if entry.future.done():
            return
        if exc is None:
            entry.future.cancel()  # branch ②: cancellation, not envelope
        else:
            entry.future.set_result(FetchFailed(exc))  # branch ①

    def _expire_if_due(self, key: Hashable) -> None:
        """Lazy pre-check at the fetch serial point: convert the active
        entry for ``key`` when its grace deadline has already passed."""
        entry = self._entries.get(key)
        if entry is None or entry.expires_at is None:
            return
        if self._clock() >= entry.expires_at:
            self._expire_grace_entry(key, entry)

    def _expire_grace_entry(self, key: Hashable, entry: _Entry) -> None:
        """Grace-deadline conversion — the ``call_later`` callback AND the
        lazy pre-check tail, shared by both profiles. Identity-revalidates
        first (stale timers are harmless: a replaced/evicted/
        shutdown-converted entry no-ops)."""
        if self._entries.get(key) is not entry:
            return
        if not self._leased:
            # Plain: drop the completed entry outright (refund + timer
            # cancel inside _drop).
            if entry.expires_at is not None and entry.expires_at <= self._clock():
                self._drop(key)
            return
        if entry.state == GRACE and entry.layer == ACTIVE:
            self._expire_grace(entry)

    def _expire_grace(self, entry: _Entry) -> None:
        """Leased: grace deadline → release registry ownership →
        retired/retained."""
        if entry.state != GRACE or entry.layer != ACTIVE:
            return  # already converted (shutdown/eviction) — defensive
        if entry.timer is not None:
            entry.timer.cancel()
            entry.timer = None
        self._drop_grace(entry)

    def _drop_grace(self, entry: _Entry) -> None:
        """Leased: active/grace → retired/retained (+ reap when caller-less)."""
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]
        entry.layer = RETIRED
        entry.state = RETAINED
        self._retired[(entry.key, entry.seq)] = entry
        if entry.caller_refs == 0:
            self._reap(entry)

    def _release_caller(self, entry: _Entry) -> None:
        if not self._leased:
            return  # plain profile: no caller-ref discipline (refs stay 0)
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
        """Leased: delete a caller-less tombstone; refund if still counted."""
        self._refund(entry)
        self._retired.pop((entry.key, entry.seq), None)
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]  # defensive: never expected here

    def _refund(self, entry: _Entry) -> None:
        if entry.accounted:
            entry.accounted = False
            self._leased_bytes -= entry.reserve_bytes

    def _detach_from_active(self, entry: _Entry) -> None:
        if self._entries.get(entry.key) is entry:
            del self._entries[entry.key]

    # ------------------------------------------------------------------
    # Plain-profile retention bookkeeping
    # ------------------------------------------------------------------

    def _drop(self, key: Hashable) -> None:
        """Plain: remove the entry for ``key`` — cancel its pending grace
        timer (no-op if the timer already fired — we may BE the callback)
        and refund its retained bytes when it had completed."""
        entry = self._entries.pop(key, None)
        if entry is not None:
            if entry.timer is not None:
                try:
                    entry.timer.cancel()
                except Exception:
                    pass
            if entry.expires_at is not None:
                self._retained_bytes -= entry.size

    def _evict_over_budget(self) -> None:
        """Plain: enforce the retention bounds by dropping oldest COMPLETED
        entries (never in-flight ones). Runs at two serial points — after
        inserting a new flight and after completing one — so the bounds are
        invariant under any interleaving the single-threaded event loop can
        produce."""
        while (
            len(self._entries) > self._max_entries
            or self._retained_bytes > self._max_bytes
        ):
            for key, entry in self._entries.items():
                if entry.expires_at is not None:
                    self._drop(key)
                    break
            else:
                break  # only in-flight entries remain — nothing to evict

    # ------------------------------------------------------------------
    # Leased-profile budget admission
    # ------------------------------------------------------------------

    def _try_reserve(self, needed: int) -> bool:
        if needed > self._max_bytes:
            return False  # a single flight larger than the whole budget
        if self._leased_bytes + needed <= self._max_bytes:
            return True
        # Serial-point eviction (mirrors CD-1 discipline): drop zero-caller
        # grace entries, oldest (insertion-order) first, until it fits.
        for key in list(self._entries.keys()):
            if self._leased_bytes + needed <= self._max_bytes:
                break
            entry = self._entries[key]
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
        """Converge the registry for shutdown; it stays USABLE afterwards
        (CD-1 semantics — new fetches simply create fresh entries). Every
        pending grace timer is cancelled; in-flight futures are NEVER
        cancelled (their factories resolve them naturally; the late
        completion path re-checks registration identity).

        Plain: ALL entries dropped + retained-bytes ledger zeroed — a
        stopped app must not leave old app-domain bodies or ``call_later``
        callbacks behind. Per-entry cleanup failures are ISOLATED: a
        hostile ``timer.cancel``/``_drop`` for one key cannot abort the
        convergence of the rest (fallback force-removes and refunds from
        the entry's own bookkeeping).

        Leased: every active entry atomically converts — in-flight →
        retired/detached (still counted, future untouched; success →
        retained, failure/cancel → failed + exactly-once refund); grace →
        retained (budget held until the last caller releases; caller-less
        entries reaped immediately).
        """
        if not self._leased:
            for key, entry in list(self._entries.items()):
                try:
                    if entry.timer is not None:
                        entry.timer.cancel()
                except Exception:
                    pass  # isolated: one hostile handle can't stop convergence
                try:
                    self._drop(key)
                except Exception:
                    # Force-remove + refund from the entry's own fields so
                    # the ledger stays exact even when the normal drop path
                    # raised.
                    try:
                        stale = self._entries.pop(key, None)
                        if stale is not None and stale.expires_at is not None:
                            self._retained_bytes -= stale.size
                    except Exception:
                        pass
            return
        for key, entry in list(self._entries.items()):
            if entry.timer is not None:
                entry.timer.cancel()
                entry.timer = None
            del self._entries[key]
            entry.layer = RETIRED
            self._retired[(entry.key, entry.seq)] = entry
            if entry.state == IN_FLIGHT:
                pass  # detached in-flight — resolution continues counting
            elif entry.state == GRACE:
                entry.state = RETAINED
                if entry.caller_refs == 0:
                    self._reap(entry)


# Backwards-compatible alias (B6-1): the leased profile IS SingleFlight —
# the former LeasedSingleFlight class merged into this one.
LeasedSingleFlight = SingleFlight

# Process-level registry shared by direct /full (L2-CD-1) and the merged
# fan-out raw fetches (L2-CD-2) — the whole point is cross-shape dedup.
fulls = SingleFlight()
