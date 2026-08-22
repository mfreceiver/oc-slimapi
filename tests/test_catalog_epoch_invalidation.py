"""Tests for 4.10.1 B: catalog cache invalidation on upstream SSE epoch loss.

Covers: :meth:`CatalogCache.invalidate` lifecycle semantics, the
``GlobalHub._notify_upstream_loss`` callback fanout (best-effort), and the
``HubRegistry`` stash/forward wiring app.py relies on.
"""

from __future__ import annotations

import asyncio

import pytest

from oc_slimapi.catalog_cache import CatalogCache
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.registry import HubRegistry

_BODY = b'[{"id":"agent-a"}]'


def _cache(ttl: float = 300.0) -> CatalogCache:
    return CatalogCache(
        ttl_seconds=ttl,
        max_entries=4,
        max_bytes=64 * 1024,
        max_entry_bytes=32 * 1024,
    )


def _factory(body: bytes = _BODY, calls: list[int] | None = None):
    async def factory() -> bytes | None:
        if calls is not None:
            calls.append(1)
            await asyncio.sleep(0.01)  # let concurrent refreshes overlap
        return body

    return factory


class TestCatalogCacheInvalidate:
    async def test_clears_entries_and_lookup_misses(self):
        cache = _cache()
        body, state = await cache.refresh(("agent",), _factory())
        assert body == _BODY and state == "miss"
        assert cache.lookup(("agent",)) == _BODY
        cache.invalidate()
        assert cache.entry_count == 0
        assert cache.retained_bytes == 0
        assert cache.lookup(("agent",)) is None

    async def test_cache_stays_operational_after_invalidate(self):
        cache = _cache()
        await cache.refresh(("agent",), _factory())
        cache.invalidate()
        # The grace window (1s) makes a SAME-key re-refresh join the
        # completed flight without re-storing, so "operational" is proven
        # on fresh keys: refresh → store → lookup all keep working.
        body, state = await cache.refresh(("command",), _factory(b'[{"id":"b"}]'))
        assert body == b'[{"id":"b"}]' and state == "miss"
        assert cache.lookup(("command",)) == b'[{"id":"b"}]'
        assert cache.entry_count == 1

    async def test_invalidate_keeps_singleflight_coalescing(self):
        cache = _cache()
        await cache.refresh(("agent",), _factory())
        cache.invalidate()
        # Fresh key → fresh flight: concurrent refreshes must still coalesce
        # through the same (not-shut-down) single-flight.
        calls: list[int] = []
        results = await asyncio.gather(
            cache.refresh(("command",), _factory(calls=calls)),
            cache.refresh(("command",), _factory(calls=calls)),
        )
        assert len(calls) == 1
        assert all(body == _BODY and state == "miss" for body, state in results)

    async def test_shutdown_semantics_differ_no_entries_after_both(self):
        # invalidate() ≠ shutdown(): both clear entries, but only shutdown
        # tears the single-flight down; invalidate leaves it running.
        cache = _cache()
        await cache.refresh(("agent",), _factory())
        cache.shutdown()
        cache.invalidate()
        assert cache.entry_count == 0
        assert cache.retained_bytes == 0


class TestGlobalHubEpochCallback:
    def _seeded_cache(self) -> CatalogCache:
        return _cache()

    async def test_notify_upstream_loss_fires_registered_callback(self):
        hub = GlobalHub(client=None)
        cache = self._seeded_cache()
        await cache.refresh(("agent",), _factory())
        assert cache.lookup(("agent",)) is not None
        hub.add_upstream_loss_callback(cache.invalidate)
        hub._notify_upstream_loss()
        assert cache.lookup(("agent",)) is None
        assert cache.entry_count == 0

    async def test_callback_failure_degrades_to_warning(
        self, caplog
    ):
        hub = GlobalHub(client=None)
        ran: list[int] = []

        def _boom() -> None:
            raise RuntimeError("observer exploded")

        def _ok() -> None:
            ran.append(1)

        hub.add_upstream_loss_callback(_boom)
        hub.add_upstream_loss_callback(_ok)
        with caplog.at_level("WARNING"):
            hub._notify_upstream_loss()  # must not raise
        assert ran == [1]  # later callbacks still run after a failure
        assert any(
            r.levelname == "WARNING"
            and "upstream loss callback failed" in r.message
            for r in caplog.records
        )

    async def test_no_callbacks_registered_is_noop(self):
        hub = GlobalHub(client=None)
        hub._notify_upstream_loss()  # bare hub, no observers → no raise


class TestGenerationFence:
    """rev-sgpt 4.10.1 MAJOR: a refresh already in flight when invalidate()
    fires must NOT write its dead-epoch body back into the cleared cache."""

    async def test_inflight_refresh_returns_body_but_never_stores(self):
        cache = _cache()
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def controlled_factory() -> bytes | None:
            calls.append(1)
            started.set()
            await release.wait()  # hold the fetch open across the epoch loss
            return _BODY

        leader = asyncio.create_task(cache.refresh(("agent",), controlled_factory))
        await started.wait()
        # Follower joins the leader's in-flight single-flight entry while
        # the factory is still pending (mid-flight epoch loss window).
        follower = asyncio.create_task(cache.refresh(("agent",), controlled_factory))
        await asyncio.sleep(0)

        cache.invalidate()  # epoch loss mid-flight → generation bumps
        release.set()

        leader_body, leader_state = await leader
        follower_body, follower_state = await follower
        # Both callers (leader AND single-flight follower) still receive
        # the body — the fence gates the STORE, never the return value.
        assert leader_body == _BODY and leader_state == "miss"
        assert follower_body == _BODY and follower_state == "miss"
        # Factory ran exactly once (single-flight coalescing held).
        assert calls == [1]
        # ... but the dead-epoch body was NOT written back: the cache
        # stays empty instead of serving a stale entry for a fresh 300s.
        assert cache.entry_count == 0
        assert cache.retained_bytes == 0
        assert cache.lookup(("agent",)) is None

    async def test_post_fence_refresh_stores_normally(self):
        # The fence must not over-fire: a refresh started AFTER the
        # invalidate (new generation) stores and serves normally.
        cache = _cache()
        cache.invalidate()
        body, state = await cache.refresh(("agent",), _factory())
        assert body == _BODY and state == "miss"
        assert cache.lookup(("agent",)) == _BODY
        assert cache.entry_count == 1

    async def test_completed_store_unaffected_then_invalidate_clears(self):
        # Regression: a store completed BEFORE the invalidate is a plain
        # normal-path store (fence never retroactively un-stores); the
        # subsequent invalidate clears it as before.
        cache = _cache()
        body, state = await cache.refresh(("agent",), _factory())
        assert body == _BODY and state == "miss"
        assert cache.lookup(("agent",)) == _BODY
        cache.invalidate()
        assert cache.lookup(("agent",)) is None


class TestHubRegistryWiring:
    def test_registry_forwards_stashed_callbacks_to_lazy_hub(self):
        registry = HubRegistry(None)
        flag: list[int] = []

        def _cb() -> None:
            flag.append(1)

        registry.add_upstream_loss_callback(_cb)
        hub = registry.get_global()  # lazy creation forwards the stash
        assert hub._upstream_loss_callbacks == [_cb]
        hub._notify_upstream_loss()
        assert flag == [1]

    def test_registry_adds_callback_to_already_live_hub(self):
        registry = HubRegistry(None)
        hub = registry.get_global()  # hub exists first
        flag: list[int] = []

        def _cb() -> None:
            flag.append(1)

        registry.add_upstream_loss_callback(_cb)
        assert _cb in hub._upstream_loss_callbacks
        hub._notify_upstream_loss()
        assert flag == [1]

    async def test_end_to_end_registry_cache_invalidation(self):
        registry = HubRegistry(None)
        cache = _cache()
        await cache.refresh(("agent",), _factory())
        registry.add_upstream_loss_callback(cache.invalidate)
        registry.get_global()._notify_upstream_loss()
        assert cache.lookup(("agent",)) is None
        # ... and the cache repairs on a fresh-key refresh (not killed;
        # same-key re-repair is grace-window-joined, see unit tests).
        await cache.refresh(("command",), _factory())
        assert cache.lookup(("command",)) == _BODY
