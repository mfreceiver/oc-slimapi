"""Single-flight dedup for shared upstream GETs (L2-CD-1).

The sidecar frequently has several in-flight requests that all need the
exact same upstream body — today two concurrent direct
``GET /slimapi/messages/{sid}/full/{mid}`` calls for one message, and (per
the merged fan-out design, CD-2) a raw per-mid fetch racing direct /full
requests. This module collapses them onto **one** upstream GET per key.

Scope boundaries (plan L2-CD-1, oracle §C-2):

* **Only the raw fetch is shared.** Transforms (strip/pack offloads) stay
  with each caller — direct /full does its own pool admission + offload
  around the shared body; a later merged consumer batches its own single
  offload. Nothing in this module knows about the transform pool.
* **In-flight join + short completion grace.** While a fetch is running,
  every same-key ``fetch()`` awaits the leader's future. When it
  completes, the result stays joinable for ``_RESULT_GRACE_SECONDS`` so
  admission-serialized peers (``max_transforms`` defaults to 1, so direct
  /full requests queue at the pool BEFORE they can join) still coalesce
  onto the leader's GET instead of each re-fetching the instant the
  leader releases its slot. This grace window is a dedup artefact, NOT a
  cache: entries are unvalidated, expire within a second, and are bounded
  in both count and retained bytes.
* **Exceptions propagate to every waiter.** A failed fetch (network error,
  mapped upstream status, decode error) fails all joined waiters with the
  same exception instance. Failures are never negatively cached — the entry
  is dropped immediately so the next request retries. Failure delivery uses
  a ``FetchFailed(exc)`` RESULT envelope (``set_result``, never
  ``set_exception``) so a leader that fails with zero waiters cannot leak
  ``Future exception was never retrieved`` warnings — a future that holds a
  result is silent even if nobody reads it.
* **Retention is actively bounded.** Completing a flight accounts its
  retained bytes and runs eviction IMMEDIATELY (not just on the next
  insertion), and every retained entry schedules a ``loop.call_later``
  expiry callback at its grace deadline — completed entries disappear
  without needing a same-key fetch to lazily expire them. Eviction and
  expiry run at event-loop serial points (no ``await`` inside), so the
  count/byte bounds hold under concurrent completions.
* **Leader cancellation is self-healing.** If the caller executing the
  factory is cancelled, waiters that are themselves still live retry as a
  new leader instead of dying with a spurious CancelledError.

Keys embed the caller-supplied scope (the app's transform-pool identity in
``routes/messages.py``) so two app instances with different upstreams —
e.g. tests, each with their own ``MockTransport`` — never share a flight.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Hashable

# How long a COMPLETED fetch result stays joinable. Must comfortably cover
# an admission-queue drain of same-key requests (each peer only needs the
# few milliseconds of its own offload once admitted), while keeping the
# freshness window for a repeated same-key /full small.
_RESULT_GRACE_SECONDS = 1.0

# Bounds on retained (completed) entries — defence in depth against grace
# retention growing without limit under a pathological key churn rate.
# In-flight entries are never evicted (they're inherently bounded by the
# callers' own admission).
_MAX_RETAINED_ENTRIES = 64
_MAX_RETAINED_BYTES = 32 * 1024 * 1024

FactoryT = Callable[[], Any]


class FetchFailed:
    """RESULT envelope for a failed flight (never an exception on the future).

    The leader writes ``FetchFailed(exc)`` via ``set_result`` so that a
    failure with zero waiters stays silent (results never trigger the
    "Future exception was never retrieved" warning that a ``set_exception``
    future would). Waiters unwrap it in :meth:`SingleFlight.fetch` and
    re-raise the wrapped exception — every waiter sees the SAME instance.
    """

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _Entry:
    """One shared flight: its future, plus retention bookkeeping."""

    __slots__ = ("future", "expires_at", "size", "timer")

    def __init__(self, future: "asyncio.Future[Any]") -> None:
        self.future = future
        # None while the flight is running; a monotonic deadline once the
        # result is retained for the grace window.
        self.expires_at: float | None = None
        # Retained-bytes contribution (counted only after completion).
        self.size = 0
        # Pending grace-expiry TimerHandle (set only once retained), so
        # shutdown() can cancel it instead of leaving loop callbacks
        # pointing at a dead app domain (rev-9 lifecycle fix).
        self.timer: asyncio.TimerHandle | None = None

    @property
    def in_flight(self) -> bool:
        return self.expires_at is None


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
    """Per-key in-flight (+ short completion grace) dedup of a factory call.

    ``fetch(key, factory)``: the first caller for a key runs ``factory()``
    (the leader); concurrent and grace-window callers await the same
    result. Not thread-safe — asyncio single-thread use only.
    """

    def __init__(
        self,
        *,
        result_grace_seconds: float = _RESULT_GRACE_SECONDS,
        max_retained_entries: int | None = None,
        max_retained_bytes: int | None = None,
    ) -> None:
        self._grace = result_grace_seconds
        # Injection points for the retention-bound tests (rev-fix 1); the
        # module constants are the production defaults.
        self._max_entries = (
            _MAX_RETAINED_ENTRIES if max_retained_entries is None
            else max_retained_entries
        )
        self._max_bytes = (
            _MAX_RETAINED_BYTES if max_retained_bytes is None
            else max_retained_bytes
        )
        self._entries: dict[Hashable, _Entry] = {}
        self._retained_bytes = 0

    def in_flight(self, key: Hashable) -> bool:
        """True while a fetch for ``key`` is running (not grace-retained)."""
        entry = self._entries.get(key)
        return entry is not None and entry.in_flight

    async def fetch(self, key: Hashable, factory: FactoryT) -> Any:
        """Return the shared result for ``key``, running ``factory`` at most
        once per flight. See module docstring for failure/cancellation
        semantics."""
        while True:
            self._expire(key, time.monotonic())
            entry = self._entries.get(key)
            if entry is None:
                return await self._lead(key, factory)
            try:
                result = await asyncio.shield(entry.future)
            except asyncio.CancelledError:
                if _current_task_cancelling():
                    # We were cancelled ourselves — propagate, don't retry.
                    raise
                # The shared flight died (its leader was cancelled); we are
                # still live, so loop and lead a fresh fetch.
                continue
            if isinstance(result, FetchFailed):
                raise result.exc  # same instance the leader saw
            return result

    # -- internals ----------------------------------------------------------

    async def _lead(self, key: Hashable, factory: FactoryT) -> Any:
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Any]" = loop.create_future()
        entry = _Entry(future)
        self._entries[key] = entry
        self._evict_over_budget()
        try:
            result = await factory()
        except BaseException as exc:
            # Failure (any kind, including leader cancellation): drop the
            # entry so the next caller retries — never negative-cache.
            self._drop(key)
            if isinstance(exc, asyncio.CancelledError):
                # Waiters' shield raises CancelledError; live ones loop and
                # re-lead (self-healing). Cancelling the future (rather
                # than delivering the leader's CancelledError as a result)
                # keeps the "was it MY cancellation?" distinction honest.
                future.cancel()
            elif not future.done():
                # FetchFailed RESULT, not set_exception: a zero-waiter
                # failure must not leak "exception was never retrieved".
                future.set_result(FetchFailed(exc))
            raise
        # Success: resolve waiters first, then retention (grace bookkeeping
        # + active expiry timer) — applied ONLY if this entry is still the
        # registered one. A shutdown() (or any concurrent dict replacement)
        # may have cleared the entry while the factory ran; in that case
        # skip retention entirely so shutdown STAYS converged: no new timer,
        # no byte accounting, nothing re-registered after the fact. Waiters
        # already awaiting the future still get their result either way.
        # There is no ``await`` between accounting and eviction, so this is
        # an event-loop serial point: the bounds hold even when many
        # flights complete back-to-back. The just-completed entry may be
        # evicted itself (it is the newest, so only when it alone busts the
        # byte bound) — hence the second identity check before scheduling
        # the timer (a self-evicted entry schedules no pointless callback).
        if not future.done():
            future.set_result(result)
        if self._entries.get(key) is entry:
            entry.expires_at = time.monotonic() + self._grace
            entry.size = len(result) if isinstance(result, (bytes, bytearray)) else 0
            self._retained_bytes += entry.size
            self._evict_over_budget()
            if self._entries.get(key) is entry:
                # Active cleanup at the grace deadline. _expire re-checks
                # the entry's identity via its deadline: if this entry was
                # already dropped/evicted and a NEW flight (even for the
                # same key) sits in the dict, the guard no-ops — stale
                # timers are harmless (and shutdown() cancels the handles).
                entry.timer = loop.call_later(
                    max(0.0, entry.expires_at - time.monotonic()),
                    self._expire, key,
                )
        return result

    def _expire(self, key: Hashable, now: float | None = None) -> None:
        """Drop ``key``'s entry if its grace has elapsed.

        Doubles as the ``call_later`` callback (``now`` defaults to the
        current monotonic clock) and as the lazy pre-check in ``fetch``.
        """
        if now is None:
            now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at is not None and entry.expires_at <= now:
            self._drop(key)

    def _drop(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            if entry.timer is not None:
                # No-op if the timer already fired (we may BE the callback);
                # otherwise stops a stale expiry from firing later.
                try:
                    entry.timer.cancel()
                except Exception:
                    pass
            if entry.expires_at is not None:
                self._retained_bytes -= entry.size

    def shutdown(self) -> None:
        """Converge the registry for shutdown (rev-9 lifecycle FAIL fix).

        Cancels every pending grace-expiry timer, clears ALL entries
        (retained AND in-flight registrations) and zeroes the
        retained-bytes ledger — a stopped app must not leave old app-domain
        bodies or ``loop.call_later`` callbacks behind.

        In-flight flights are NOT cancelled, and that is deliberate: their
        futures resolve naturally when their factories finish. The
        completion path re-checks registration identity (see ``_lead``)
        and, finding the entry gone, skips retention — so nothing
        re-registers, re-accounts, or re-arms a timer after shutdown.
        Waiters already awaiting a flight still receive its result; only
        NEW same-key callers lead a fresh (duplicate) fetch — the safe
        direction during the shutdown window, convergence first.

        Per-entry cleanup failures are ISOLATED: a hostile ``timer.cancel``
        or ``_drop`` for one key cannot abort the convergence of the rest
        (the fallback path force-removes the entry and refunds its bytes
        directly from the entry's own bookkeeping).

        The registry stays USABLE after shutdown — ``fetch`` simply
        re-creates entries (full retention semantics resume). Callers that
        want a permanently dead registry should discard the instance.
        """
        for key, entry in list(self._entries.items()):
            try:
                if entry.timer is not None:
                    entry.timer.cancel()
            except Exception:
                pass  # isolated: one hostile handle cannot stop convergence
            try:
                self._drop(key)
            except Exception:
                # Force-remove + refund from the entry's own fields so the
                # ledger stays exact even when the normal drop path raised.
                try:
                    stale = self._entries.pop(key, None)
                    if stale is not None and stale.expires_at is not None:
                        self._retained_bytes -= stale.size
                except Exception:
                    pass

    def _evict_over_budget(self) -> None:
        """Enforce the retention bounds by dropping oldest COMPLETED entries
        (never in-flight ones). Runs at two serial points — after inserting
        a new flight and after completing one — so the bounds are invariant
        under any interleaving the single-threaded event loop can produce."""
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


def _current_task_cancelling() -> bool:
    """True if the CURRENT task has a real cancellation pending (3.11+),
    i.e. the CancelledError we caught is ours and not the flight's."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


# Process-level registry shared by direct /full (L2-CD-1) and the merged
# fan-out raw fetches (L2-CD-2) — the whole point is cross-shape dedup.
fulls = SingleFlight()
