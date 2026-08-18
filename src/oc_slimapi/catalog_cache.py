"""TTL body cache for catalog routes (/slimapi/agent, /slimapi/command).

Traffic-optimization plan Batch 1, Task 1.1 (A1). Caches successful upstream
catalog bodies (200 + parseable JSON list, within per-entry and total byte
budgets) for a TTL window so repeat catalog GETs stop hitting upstream.
gzip/identity is **not** part of the cache — the cached value is the raw
upstream body; compression stays per-request negotiation.

Design contract (plan §Task 1.1):

* Only successful bodies are cached: factory exceptions (mapped 4xx/5xx /
  network errors), cap-exceeded reads (``None``), unparseable JSON, and
  non-list JSON payloads are never stored (no negative caching).
* Dual caps: ``max_entries`` + ``max_bytes`` total, evicting oldest-fetched
  entries first. A body larger than ``max_entry_bytes`` bypasses the cache
  (served as-is, never accounted).
* Refresh stampede protection: concurrent refreshes of the same key
  coalesce through a plain :class:`~oc_slimapi.singleflight.SingleFlight`
  held by this cache (leader fetch + 1s grace window for stragglers).
  Eviction runs at the serial point (no ``await`` between insert and evict).
* ``shutdown()`` mirrors CD-1 semantics: clears entries and shuts the
  refresh single-flight down; the cache stays usable afterwards.
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable, Hashable

import orjson

from .singleflight import SingleFlight

FactoryT = Callable[[], Awaitable[bytes | None]]


class CatalogCache:
    """TTL + byte-budget cache of successful upstream catalog bodies.

    Route usage contract (admission-first order preserved):

    * ``lookup(key)`` — synchronous freshness check (``None`` when disabled
      by ``ttl_seconds <= 0`` or when no fresh entry exists).
    * ``refresh(key, factory)`` — called **inside** transform admission; a
      concurrent refresh coalesces through the internal single-flight. The
      factory returns the raw upstream body or ``None`` (cap exceeded).
      Returns ``(body, cache_state)`` where ``cache_state`` is ``"miss"`` or
      ``None`` (``None`` when the cache is disabled — no cache semantics,
      byte-identical to today's uncached path).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        max_bytes: int,
        max_entry_bytes: int,
        clock: Callable[[], float] = time.monotonic,
        refresh_singleflight: SingleFlight | None = None,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._max_bytes = int(max_bytes)
        self._max_entry_bytes = int(max_entry_bytes)
        self._clock = clock
        # key -> (body, fetched_at monotonic); dict insertion order ==
        # fetch order, so oldest-first eviction is plain iteration order.
        self._entries: dict[Hashable, tuple[bytes, float]] = {}
        self._retained_bytes = 0
        self._sf = refresh_singleflight if refresh_singleflight is not None else SingleFlight()

    # ------------------------------------------------------------------
    # Introspection (tests / ops)
    # ------------------------------------------------------------------

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------
    # Lookup / refresh
    # ------------------------------------------------------------------

    def lookup(self, key: Hashable) -> bytes | None:
        """Return the fresh cached body for ``key`` or ``None``.

        Expired entries are dropped lazily at this point (serial point, no
        ``await``). Returns ``None`` when the cache is disabled.
        """
        if self._ttl <= 0:
            return None
        item = self._entries.get(key)
        if item is None:
            return None
        body, fetched_at = item
        if self._clock() - fetched_at >= self._ttl:
            self._drop(key)
            return None
        return body

    async def refresh(
        self, key: Hashable, factory: FactoryT
    ) -> tuple[bytes | None, str | None]:
        """Fetch (coalesced) and store a body for ``key``.

        Must be called inside transform admission (plan: catalog paths keep
        the admission-first order). Concurrent refreshes of the same key
        share one flight; stragglers inside the single-flight grace window
        join the just-completed refresh.
        """
        if self._ttl <= 0:
            return await factory(), None  # disabled → today's path, no label

        async def _fetch_and_store() -> bytes | None:
            body = await factory()
            if body is None:
                return body  # cap exceeded → never cached
            if len(body) > self._max_entry_bytes:
                return body  # oversize → bypass cache, not accounted
            try:
                parsed = orjson.loads(body)
            except (orjson.JSONDecodeError, ValueError):
                return body  # bad JSON → route maps 503; never cached
            if not isinstance(parsed, list):
                return body  # malformed catalog payload → never cached
            self._store(key, body)  # serial point: store + evict, no await
            return body

        body = await self._sf.fetch(("catalog-refresh", key), _fetch_and_store)
        return body, "miss"

    # ------------------------------------------------------------------
    # Internals (all synchronous — serial points)
    # ------------------------------------------------------------------

    def _store(self, key: Hashable, body: bytes) -> None:
        # Replace-in-place drops the old accounting before appending the
        # fresh entry at the newest position (no double counting).
        self._drop(key)
        self._entries[key] = (body, self._clock())
        self._retained_bytes += len(body)
        self._evict_over_budget()

    def _evict_over_budget(self) -> None:
        # Serial-point eviction (mirrors SingleFlight discipline): run right
        # after insertion, oldest (insertion-order) first, until both caps
        # hold. The just-inserted entry is newest, so it is never evicted
        # here (validate guarantees a single entry fits both caps).
        while (
            len(self._entries) > self._max_entries
            or self._retained_bytes > self._max_bytes
        ):
            for oldest_key in self._entries:
                self._drop(oldest_key)
                break
            else:
                break  # empty (cannot happen while over cap, defensive)

    def _drop(self, key: Hashable) -> None:
        item = self._entries.pop(key, None)
        if item is not None:
            self._retained_bytes -= len(item[0])

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Clear entries + shut the refresh single-flight down.

        CD-1 semantics: the cache stays usable after shutdown (next access
        simply repopulates). Called from app lifespan teardown.
        """
        self._sf.shutdown()
        self._entries.clear()
        self._retained_bytes = 0
