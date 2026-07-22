"""TDD DRAFT tests for Batch 3 — ``ChildrenCache`` (contract §2 / §16).

This module is the **authoritative specification** for the upcoming
``src/oc_slimapi/children_cache.py`` implementation. It is a TDD DRAFT: the
implementation does **not** exist yet, so importing this module triggers an
``ImportError`` at collection time. ``./scripts/check.sh`` is therefore
expected to be non-green (collection error) until the implementation (the
"fixer-bgpt" lane) lands. Existing tests outside this file are unaffected
by that collection error — they are still collected + executed when this
file is excluded (see verification commands in the change report).

The implementation must satisfy:

* ``ChildrenCache(upstream)`` — pure asyncio, no FastAPI import. Accepts any
  ``httpx.AsyncClient`` (production) or a fake/mock client (these tests).
* ``await cache.get_or_fetch(parent_sid, directory) -> (list[dict], int)`` —
  returns ``(child_skeletons, childrenVersion)`` where ``childrenVersion`` is
  the generation sampled at fetch start (NOT at response time).
* ``cache.invalidate(parent_sid) -> None`` — bump generation + evict every
  directory entry under that sid (Batch4 will wire this from the hub; Batch3
  only needs the signature + these tests).
* ``cache.generation_of(parent_sid) -> int`` — read the current generation.
* ``await cache.aclose() -> None`` — shutdown: cancel + await in-flight,
  broadcast 503 ``upstream_unavailable`` to waiters.
* Module constants ``TTL_SECONDS=30.0``, ``EMPTY_TTL_SECONDS=5.0``,
  ``MAX_ENTRIES=4096``.
* Module-level metrics fields ``self.hits`` / ``self.misses`` /
  ``self.coalesced`` on the instance.

House pattern (mirrors ``HubRegistry.subscribe``): all check→mutate sections
are **synchronous** (no ``await`` between them) → single worker / single
loop = naturally atomic. **No** ``asyncio.Lock``, **no** ``asyncio.shield``.

Tests cover the 12 oracle scenarios + version semantics. White-box: where
noted, tests inspect private fields (``_cache`` / ``_inflight`` /
``_generations``) to lock down invariants INV-1..INV-6 (contract §16).
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

import httpx
import orjson
import pytest

# NOTE: this import fails until the implementation lands — that is the
# expected TDD-draft collection error documented in the module docstring.
from oc_slimapi.children_cache import (  # noqa: E402
    ChildrenCache,
    EMPTY_TTL_SECONDS,
    MAX_ENTRIES,
    TTL_SECONDS,
)
from oc_slimapi.errors import CodedHTTPException  # noqa: E402
from oc_slimapi.upstream_errors import fetch_json_mapped  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FakeUpstream:
    """Controllable stand-in for ``httpx.AsyncClient``.

    Records every ``get()`` call and lets each test script the response
    (status, body, delay, exception). The transport itself is a real
    ``httpx.MockTransport`` so anything that uses ``raise_for_status`` /
    ``response.json()`` (including the real ``fetch_json_mapped``) keeps
    working unchanged.

    Two ways to drive it:

    * **Sync handler** (default): set ``upstream._handler`` to a sync
      callable ``(httpx.Request) -> httpx.Response``. Goes through the real
      MockTransport so ``response.json()`` / ``raise_for_status`` behave
      exactly as in production. ``call_count`` increments per call.

    * **Async handler with delay / event gating** (for concurrency tests):
      call ``upstream.set_async_get(async_fn)``. The override still routes
      through a synthetic request build + counter increment so
      ``call_count`` stays accurate. Use this whenever the test needs to
      ``await asyncio.Event`` inside the upstream GET.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[httpx.Request] = []
        self._transport = httpx.MockTransport(self._wrap)
        self._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:4096",
            transport=self._transport,
        )
        self._async_override: Any = None

    def _wrap(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._handler(request)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def set_async_get(self, async_fn) -> None:
        """Install an async ``get`` override that still records call count.

        The override synthesises an ``httpx.Request`` so the recorded
        ``self.calls`` list stays consistent with sync-handler mode.
        """
        self._async_override = async_fn

        async def wrapped(path, *, params=None, headers=None):
            request = self._client.build_request(
                "GET", path, params=params, headers=headers,
            )
            self.calls.append(request)
            return await async_fn(path, params=params, headers=headers)

        self.get = wrapped  # type: ignore[assignment]

    async def get(self, path, *, params=None, headers=None):
        return await self._client.get(path, params=params, headers=headers)

    async def aclose(self):
        await self._client.aclose()


def _ok_children(payload: list) -> httpx.Response:
    """Build a 200 response carrying a JSON list body (the upstream
    ``Session.Info[]`` shape)."""
    return httpx.Response(
        200, content=orjson.dumps(payload),
        headers={"Content-Type": "application/json"},
    )


def _make_session_info(
    sid: str, *, created: int | None = None, parent: str = "p1",
    directory: str = "/app",
) -> dict:
    """Build a minimal upstream ``Session.Info`` dict."""
    info: dict[str, Any] = {"id": sid, "parentID": parent, "directory": directory}
    if created is not None:
        info["time"] = {"created": created, "updated": created}
    return info


class FakeClock:
    """Deterministic monotonic clock for TTL tests.

    ChildrenCache MUST read time via ``time.monotonic`` (lazy-expiry). Tests
    monkeypatch ``time.monotonic`` in the ``children_cache`` module namespace
    to advance time deterministically.
    """

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = FakeClock()
    # Patch at the module-under-test namespace (the implementation MUST use
    # ``time.monotonic`` directly; if it imports the symbol we patch that).
    import oc_slimapi.children_cache as cc_mod
    monkeypatch.setattr(cc_mod, "time", type("_T", (), {"monotonic": clock})())
    return clock


@pytest.fixture
def cache_and_upstream():
    """Build a (cache, upstream) pair with a trivial 200 [] handler.

    Per-test overrides the handler by reassigning ``upstream._handler``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_children([])

    upstream = FakeUpstream(handler)
    cache = ChildrenCache(upstream)
    yield cache, upstream
    # Best-effort cleanup; swallow if a test already closed the cache.
    try:
        asyncio.get_running_loop().create_task(cache.aclose())
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Sanity: module constants + signatures (oracle spec contract)
# ---------------------------------------------------------------------------


def test_module_constants_match_contract():
    """Contract §16 fixes these names + values exactly."""
    assert TTL_SECONDS == 30.0
    assert EMPTY_TTL_SECONDS == 5.0
    assert MAX_ENTRIES == 4096


def test_class_signature_matches_oracle_spec():
    """The public surface described in the task spec / contract §16 must be
    present with the documented shapes. Locks down the API before any
    implementation work begins."""
    assert inspect.iscoroutinefunction(ChildrenCache.get_or_fetch)
    assert inspect.iscoroutinefunction(ChildrenCache.aclose)
    assert not inspect.iscoroutinefunction(ChildrenCache.invalidate)
    assert not inspect.iscoroutinefunction(ChildrenCache.generation_of)
    sig = inspect.signature(ChildrenCache.get_or_fetch)
    assert list(sig.parameters)[1:] == ["parent_sid", "directory"]
    sig_inv = inspect.signature(ChildrenCache.invalidate)
    assert list(sig_inv.parameters)[1:] == ["parent_sid"]
    sig_gen = inspect.signature(ChildrenCache.generation_of)
    assert list(sig_gen.parameters)[1:] == ["parent_sid"]
    sig_ctor = inspect.signature(ChildrenCache.__init__)
    assert list(sig_ctor.parameters)[1:] == ["upstream"]


def test_cache_exposes_metrics_fields(cache_and_upstream):
    """Instance MUST expose ``hits`` / ``misses`` / ``coalesced`` counters so
    ``/slimapi/metrics`` can read them (contract §16, additive)."""
    cache, _ = cache_and_upstream
    for field in ("hits", "misses", "coalesced"):
        assert hasattr(cache, field), f"missing metric field: {field}"


# ===========================================================================
# Scenario 1: miss → 1 upstream call; N concurrent same-key → still 1 call,
#             coalesced == N-1, all waiters get the same result.
# ===========================================================================


async def test_miss_issues_single_upstream_call(cache_and_upstream):
    """Oracle #1 (miss half): fresh miss on a key triggers exactly one upstream
    GET and caches the result."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    children, version = await cache.get_or_fetch("p1", "/app")
    assert len(children) == 1
    assert children[0]["id"] == "c1"
    assert version >= 0
    assert upstream.call_count == 1
    # hit/miss metrics
    assert cache.misses == 1
    assert cache.hits == 0
    assert cache.coalesced == 0


async def test_concurrent_same_key_coalesces_to_single_fetch(cache_and_upstream):
    """Oracle #1 (single-flight half): N concurrent waiters on the same key
    share one upstream GET; ``coalesced`` increments N-1; every waiter gets
    the same data + version (identity of children list, not just equality)."""
    cache, upstream = cache_and_upstream

    async def slow_get(path, *, params=None, headers=None):
        await asyncio.sleep(0.05)
        return _ok_children([_make_session_info("c1", created=10)])

    upstream.set_async_get(slow_get)

    n = 5
    results = await asyncio.gather(
        *(cache.get_or_fetch("p1", "/app") for _ in range(n))
    )
    # All waiters got the same number of children
    assert all(len(r[0]) == 1 for r in results)
    assert all(r[0][0]["id"] == "c1" for r in results)
    # All waiters got the SAME version (data + version 同源)
    versions = {r[1] for r in results}
    assert len(versions) == 1, f"expected single shared version, got {versions}"
    # Exactly one upstream call
    assert upstream.call_count == 1
    # coalesced == N-1 (leader counts as miss, the rest are coalesced)
    assert cache.coalesced == n - 1
    assert cache.misses == 1
    assert cache.hits == 0


# ===========================================================================
# Scenario 2: TTL hit (0 upstream); TTL expiry → new fetch.
# ===========================================================================


async def test_ttl_hit_issues_zero_upstream_calls(cache_and_upstream, fake_clock):
    """Oracle #2 (hit half): a second request inside TTL is served from cache
    with zero upstream calls; ``hits`` increments."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    await cache.get_or_fetch("p1", "/app")
    assert upstream.call_count == 1
    fake_clock.advance(TTL_SECONDS - 0.001)  # just under TTL
    children, _ = await cache.get_or_fetch("p1", "/app")
    assert len(children) == 1
    assert upstream.call_count == 1  # no new fetch
    assert cache.hits == 1
    assert cache.misses == 1


async def test_ttl_expiry_triggers_new_fetch(cache_and_upstream, fake_clock):
    """Oracle #2 (expiry half): once monotonic time has advanced past
    ``TTL_SECONDS``, the next request re-fetches from upstream."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    await cache.get_or_fetch("p1", "/app")
    assert upstream.call_count == 1
    fake_clock.advance(TTL_SECONDS + 0.001)  # past TTL
    children, _ = await cache.get_or_fetch("p1", "/app")
    assert len(children) == 1
    assert upstream.call_count == 2  # refetched
    assert cache.misses == 2


# ===========================================================================
# Scenario 3: empty array → 5s negative cache; non-empty → 30s.
# ===========================================================================


async def test_empty_array_uses_short_negative_ttl(cache_and_upstream, fake_clock):
    """Oracle #3 (empty half): a 200 + ``[]`` upstream response is negative-
    cached for ``EMPTY_TTL_SECONDS=5`` only — past 5s it must refetch, but
    before 5s it must NOT."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([])

    children, _ = await cache.get_or_fetch("p1", "/app")
    assert children == []
    assert upstream.call_count == 1

    # Within empty TTL → hit (no fetch)
    fake_clock.advance(EMPTY_TTL_SECONDS - 0.001)
    children2, _ = await cache.get_or_fetch("p1", "/app")
    assert children2 == []
    assert upstream.call_count == 1

    # Past empty TTL → refetch
    fake_clock.advance(0.002)  # total elapsed = EMPTY_TTL_SECONDS + 0.001
    await cache.get_or_fetch("p1", "/app")
    assert upstream.call_count == 2


async def test_non_empty_uses_long_ttl(cache_and_upstream, fake_clock):
    """Oracle #3 (non-empty half): non-empty payload uses the full 30s TTL —
    after 5s (which would expire an empty cache) a non-empty entry MUST still
    be fresh."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    await cache.get_or_fetch("p1", "/app")
    assert upstream.call_count == 1
    # 5s would evict an empty entry; non-empty MUST survive.
    fake_clock.advance(EMPTY_TTL_SECONDS + 1)
    await cache.get_or_fetch("p1", "/app")
    assert upstream.call_count == 1  # still cached
    # ...but 30s total expiry refetches
    fake_clock.advance(TTL_SECONDS - EMPTY_TTL_SECONDS)  # push past 30s total
    await cache.get_or_fetch("p1", "/app")
    assert upstream.call_count == 2


# ===========================================================================
# Scenario 4: generation guard — invalidate during in-flight fetch → fetch
#             completes, waiter still gets data, but cache is NOT written.
# ===========================================================================


async def test_invalidate_during_inflight_fetch_skips_cache_write(cache_and_upstream):
    """Oracle #4 (generation guard): with a slow in-flight fetch, calling
    ``invalidate(sid)`` while the fetch is suspended must (a) let the waiter
    still receive the data, and (b) prevent the result from being written to
    ``_cache`` (the bumped generation invalidates the in-flight write).

    This locks INV-3 (inflight.generation immutable post-creation) + the
    write-gate ``inflight.generation >= generation_of(sid)``."""
    cache, upstream = cache_and_upstream

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get(path, *, params=None, headers=None):
        started.set()
        await release.wait()
        return _ok_children([_make_session_info("c1", created=10)])

    upstream.set_async_get(slow_get)

    fetch_task = asyncio.create_task(cache.get_or_fetch("p1", "/app"))
    await started.wait()
    # Fetch is now in flight. Bump generation + evict.
    cache.invalidate("p1")
    assert cache.generation_of("p1") >= 1
    # Cache must NOT contain the key right now (we just invalidated).
    key = ("p1", "/app")  # implementation may differ; tolerate both shapes
    # Release the fetch.
    release.set()
    children, version = await fetch_task
    # Waiter still gets the data — generation guard only blocks _cache write,
    # not the in-flight waiter's payload.
    assert len(children) == 1
    assert children[0]["id"] == "c1"

    # The version returned equals the generation sampled at fetch start
    # (pre-invalidate). It must be strictly less than the current generation.
    assert version < cache.generation_of("p1")

    # INV-1 / generation guard: no entry written for this key (find both
    # possible key shapes — implementation detail).
    cache_keys = set(cache._cache.keys())  # noqa: SLF001 (white-box INV-1)
    matches = [k for k in cache_keys if (isinstance(k, tuple) and k[0] == "p1")
               or (isinstance(k, str) and "p1" in k)]
    assert matches == [], (
        f"generation guard failed: invalidated entry was written; keys={matches}"
    )


# ===========================================================================
# Scenario 5: exception broadcast — upstream raises CodedHTTPException → all
#             N waiters receive the SAME exception (code/status); no cache
#             write; next request retries.
# ===========================================================================


async def test_fetch_failure_broadcasts_same_exception_to_all_waiters(cache_and_upstream):
    """Oracle #5: upstream failure (via ``fetch_json_mapped`` raising
    CodedHTTPException) propagates to every waiter of the same flight. The
    cache MUST NOT store the result; ``_inflight`` MUST be cleaned up; the
    next request MUST issue a fresh upstream call."""
    cache, upstream = cache_and_upstream

    fail_count = {"n": 0}
    gate = asyncio.Event()

    async def failing_get(path, *, params=None, headers=None):
        fail_count["n"] += 1
        # First call: wait so other waiters coalesce on this flight before it
        # raises. Then return a 503-shaped response so the real
        # ``fetch_json_mapped`` raises CodedHTTPException(503).
        if fail_count["n"] == 1:
            await gate.wait()
        return httpx.Response(503, content=b"boom")

    upstream.set_async_get(failing_get)

    n = 3
    tasks = [
        asyncio.create_task(cache.get_or_fetch("p1", "/app"))
        for _ in range(n)
    ]
    # Let the leader fetch enter; then release it so all waiters receive.
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, CodedHTTPException) for r in results), (
        f"expected all waiters to get CodedHTTPException, got {results!r}"
    )
    # All same status + code
    assert {r.status_code for r in results} == {503}
    assert {r.code for r in results} == {"upstream_unavailable"}
    # Exactly one upstream call (coalesced)
    assert fail_count["n"] == 1
    # No cache entry written
    cache_keys = list(cache._cache.keys())  # noqa: SLF001 (white-box)
    assert not any(
        (isinstance(k, tuple) and k[0] == "p1") or
        (isinstance(k, str) and "p1" in k)
        for k in cache_keys
    )
    # No lingering in-flight entry
    inflight_keys = list(cache._inflight.keys())  # noqa: SLF001
    assert inflight_keys == []
    # Next request retries — new fetch.
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])
    # Restore the default recording get (clear the async override).
    upstream._async_override = None
    upstream.get = upstream._client.get  # type: ignore[assignment]
    children, _ = await cache.get_or_fetch("p1", "/app")
    assert len(children) == 1


# ===========================================================================
# Scenario 6: single waiter cancel — does NOT cancel shared fetch; other
#             waiter still completes; upstream call count stays 1.
# ===========================================================================


async def test_single_waiter_cancel_does_not_cancel_shared_fetch(cache_and_upstream):
    """Oracle #6: cancelling one waiter removes ONLY that waiter's Future; the
    shared fetch task keeps running (no ``asyncio.shield``); the remaining
    waiter gets the data; upstream is hit exactly once.

    Implementation note for the implementer: structure fetch so it never
    awaits on any waiter's Future — only the waiters await their own Future.
    That makes single-waiter cancel structurally incapable of touching the
    fetch."""
    cache, upstream = cache_and_upstream
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get(path, *, params=None, headers=None):
        started.set()
        await release.wait()
        return _ok_children([_make_session_info("c1", created=10)])

    upstream.set_async_get(slow_get)

    w1 = asyncio.create_task(cache.get_or_fetch("p1", "/app"))
    w2 = asyncio.create_task(cache.get_or_fetch("p1", "/app"))
    await started.wait()
    # Cancel one waiter.
    w1.cancel()
    with pytest.raises((asyncio.CancelledError, BaseException)):
        try:
            await w1
        except CodedHTTPException:
            raise
    # Release the fetch; the OTHER waiter must still get the result.
    release.set()
    children, _ = await w2
    assert len(children) == 1
    assert children[0]["id"] == "c1"
    assert upstream.call_count == 1
    # Fetch task is not lingering (it completed normally).
    # Allow event loop to drain any finalisation.
    await asyncio.sleep(0)


# ===========================================================================
# Scenario 7: shutdown — aclose() with in-flight fetch + waiters → waiters
#             get 503; aclose() returns; no residual children-fetch tasks.
# ===========================================================================


async def test_aclose_with_inflight_broadcasts_503_and_cleans_tasks(cache_and_upstream):
    """Oracle #7: ``aclose()`` during in-flight fetch must:

    1. broadcast 503 ``upstream_unavailable`` to every waiter,
    2. cancel + await every fetch task,
    3. return promptly (not hang on the upstream GET),
    4. leave zero children-fetch tasks alive afterwards (verified via
       ``asyncio.all_tasks()`` name-shape filter)."""
    cache, upstream = cache_and_upstream

    started = asyncio.Event()
    release = asyncio.Event()

    async def stuck_get(path, *, params=None, headers=None):
        started.set()
        await release.wait()  # never set during this test
        return _ok_children([])

    upstream.set_async_get(stuck_get)

    w1 = asyncio.create_task(cache.get_or_fetch("p1", "/app"))
    w2 = asyncio.create_task(cache.get_or_fetch("p1", "/app"))
    await started.wait()
    # Sanity: there are at least 2 tasks besides the test's own (the two
    # waiter tasks; the fetch may be inside one of them or a separate task
    # — implementation detail).
    pending_before = {t for t in asyncio.all_tasks()
                      if t is not asyncio.current_task()}
    assert len(pending_before) >= 2

    # aclose MUST return within a reasonable bound (no hang).
    await asyncio.wait_for(cache.aclose(), timeout=2.0)

    # Both waiters must raise a structured 503, not receive an exception object
    # as a successful return value.
    for waiter in (w1, w2):
        with pytest.raises(CodedHTTPException) as ei:
            await waiter
        assert ei.value.status_code == 503
        assert ei.value.code == "upstream_unavailable"

    # No residual fetch task naming-wise referencing children_cache.
    leftover = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and ("children" in (t.get_name() or "").lower()
             or "fetch" in (t.get_name() or "").lower())
    ]
    # Allow the just-finished waiters to be GC'd; verify no NEW fetch task is
    # pending. We assert on name-substring OR coro-qualname-substring to be
    # robust against implementations that do not name tasks explicitly.
    def _task_kind(t: asyncio.Task) -> str:
        return (t.get_name() or "") + " " + (
            getattr(t.get_coro(), "__qualname__", "") or ""
        )
    leftovers_named = [t for t in asyncio.all_tasks()
                       if "get_or_fetch" in _task_kind(t)
                       or "_fetch" in _task_kind(t)
                       or "children" in _task_kind(t)]
    assert leftovers_named == [], (
        f"aclose leaked fetch tasks: {[_task_kind(t) for t in leftovers_named]}"
    )


async def test_aclose_marks_closed_and_rejects_new_requests(cache_and_upstream):
    """Oracle #7 (closed state half, INV-6): after ``aclose()`` returns, any
    new ``get_or_fetch`` MUST fail fast (no new task, no new entry). The
    spec mandates this be a structured coded error (503
    ``upstream_unavailable``) consistent with the broadcast shape."""
    cache, _ = cache_and_upstream
    await cache.aclose()
    with pytest.raises(CodedHTTPException) as ei:
        await cache.get_or_fetch("p1", "/app")
    assert ei.value.status_code == 503
    assert ei.value.code == "upstream_unavailable"


# ===========================================================================
# Scenario 8: INV-1 — _cache[key] and _inflight[key] never coexist.
# ===========================================================================


async def test_inv1_cache_and_inflight_mutually_exclusive(cache_and_upstream):
    """Oracle #8 (invariant INV-1, white-box): after a successful fetch, the
    key is in ``_cache`` and NOT in ``_inflight``. While in flight, the key
    is in ``_inflight`` and NOT in ``_cache``. The transition is atomic
    (no-await check→mutate)."""
    cache, upstream = cache_and_upstream
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get(path, *, params=None, headers=None):
        started.set()
        await release.wait()
        return _ok_children([_make_session_info("c1", created=10)])

    upstream.set_async_get(slow_get)

    waiter = asyncio.create_task(cache.get_or_fetch("p1", "/app"))
    await started.wait()
    # In flight: in _inflight, NOT in _cache
    inflight_keys = set(cache._inflight.keys())  # noqa: SLF001
    cache_keys = set(cache._cache.keys())  # noqa: SLF001
    assert inflight_keys, "expected inflight entry during fetch"
    assert not (set(inflight_keys) & set(cache_keys)), (
        f"INV-1 violation: key in both _inflight={inflight_keys} and _cache={cache_keys}"
    )
    release.set()
    await waiter
    # Settled: in _cache, NOT in _inflight
    inflight_keys = set(cache._inflight.keys())  # noqa: SLF001
    cache_keys = set(cache._cache.keys())  # noqa: SLF001
    assert cache_keys, "expected cached entry after fetch"
    assert not (set(inflight_keys) & set(cache_keys)), (
        f"INV-1 violation post-settle: keys={inflight_keys & cache_keys}"
    )


async def test_inv1_expired_entry_is_removed_before_inflight(cache_and_upstream, fake_clock):
    """An expired entry is removed before the replacement flight is created."""
    cache, upstream = cache_and_upstream
    async def initial_get(path, *, params=None, headers=None):
        return _ok_children([])

    upstream.set_async_get(initial_get)
    await cache.get_or_fetch("p1", "/app")
    fake_clock.advance(EMPTY_TTL_SECONDS + 0.001)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get(path, *, params=None, headers=None):
        started.set()
        await release.wait()
        return _ok_children([])

    upstream.set_async_get(slow_get)
    waiter = asyncio.create_task(cache.get_or_fetch("p1", "/app"))
    await started.wait()
    key = ("p1", "/app")
    assert key not in cache._cache
    assert key in cache._inflight
    assert not (key in cache._cache and key in cache._inflight)
    release.set()
    await waiter


# ===========================================================================
# Scenario 9: key normalize — directory "/x/" and "/x" coalesce; None vs
#             "/x" do NOT coalesce.
# ===========================================================================


async def test_directory_trailing_slash_coalesces(cache_and_upstream):
    """Oracle #9 (trailing-slash half): ``"/x/"`` and ``"/x"`` MUST normalize
    to the same cache key → single upstream call. Uses
    ``routes.sessions.normalize_directory`` semantics (rstrip '/' keep '/')."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    await cache.get_or_fetch("p1", "/x/")
    await cache.get_or_fetch("p1", "/x")
    assert upstream.call_count == 1


async def test_none_directory_does_not_coalesce_with_explicit(cache_and_upstream):
    """Oracle #9 (None half): a None directory and an explicit directory are
    different cache keys → two upstream calls."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    await cache.get_or_fetch("p1", None)
    await cache.get_or_fetch("p1", "/x")
    assert upstream.call_count == 2


# ===========================================================================
# Scenario 10: sort determinism — created DESC, id ASC tie-break; missing
#              created sorts to the tail (as 0).
# ===========================================================================


async def test_sort_created_desc_then_id_asc(cache_and_upstream):
    """Oracle #10: upstream returns children out of order; the cache must
    return them stably sorted by ``time.created DESC`` then ``id ASC``. This
    guarantee holds equally on cache hit and miss."""
    cache, upstream = cache_and_upstream
    upstream_payload = [
        _make_session_info("c1", created=10),
        _make_session_info("c2", created=30),
        _make_session_info("c3", created=20),
        _make_session_info("c4", created=30),  # tie with c2 → id ASC: c2 before c4
    ]
    upstream._handler = lambda req: _ok_children(upstream_payload)

    children, _ = await cache.get_or_fetch("p1", "/app")
    ids = [c["id"] for c in children]
    # created 30 → 30 → 20 → 10; tie 30 broken by id asc (c2 < c4).
    assert ids == ["c2", "c4", "c3", "c1"], f"unexpected order: {ids}"


async def test_sort_missing_created_sorts_to_tail(cache_and_upstream):
    """Oracle #10 (missing created half): a child without ``time.created``
    is treated as created=0 and sorts to the tail (after all positive
    timestamps)."""
    cache, upstream = cache_and_upstream
    upstream_payload = [
        _make_session_info("no_time"),  # no time.created
        _make_session_info("c1", created=5),
        _make_session_info("c2", created=10),
    ]
    upstream._handler = lambda req: _ok_children(upstream_payload)

    children, _ = await cache.get_or_fetch("p1", "/app")
    ids = [c["id"] for c in children]
    assert ids == ["c2", "c1", "no_time"], (
        f"missing-created not at tail: {ids}"
    )


async def test_sort_stable_across_hit_and_miss(cache_and_upstream):
    """Oracle #10 (stability across hit/miss): the second call (cache hit)
    returns the EXACT same order as the first (no reshuffle). This is the
    actual contract motivation for slimapi-side sorting."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([
        _make_session_info("z", created=5),
        _make_session_info("a", created=5),
        _make_session_info("m", created=10),
    ])
    first, _ = await cache.get_or_fetch("p1", "/app")
    second, _ = await cache.get_or_fetch("p1", "/app")
    assert [c["id"] for c in first] == [c["id"] for c in second] == ["m", "a", "z"]


# ===========================================================================
# Scenario 11: invalidate(sid) evicts ALL directories for that sid; leaves
#              other sids untouched.
# ===========================================================================


async def test_invalidate_evicts_all_dirs_for_sid(cache_and_upstream):
    """Oracle #11: invalidate(p1) evicts entries for p1 across every cached
    directory; p2 entries survive."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    await cache.get_or_fetch("p1", "/app")
    await cache.get_or_fetch("p1", "/other")
    await cache.get_or_fetch("p2", "/app")
    assert upstream.call_count == 3

    cache.invalidate("p1")
    # p1 entries were evicted → refetch.
    await cache.get_or_fetch("p1", "/app")
    await cache.get_or_fetch("p1", "/other")
    assert upstream.call_count == 5
    # p2 entry survived → no refetch.
    await cache.get_or_fetch("p2", "/app")
    assert upstream.call_count == 5


async def test_invalidate_bumps_generation_monotonically(cache_and_upstream):
    """Oracle #11 (generation half, INV-4): each ``invalidate(sid)`` strictly
    increments ``generation_of(sid)``; sids are independent counters."""
    cache, _ = cache_and_upstream
    g0_p1 = cache.generation_of("p1")
    g0_p2 = cache.generation_of("p2")
    cache.invalidate("p1")
    assert cache.generation_of("p1") > g0_p1
    assert cache.generation_of("p2") == g0_p2
    cache.invalidate("p1")
    cache.invalidate("p1")
    g3_p1 = cache.generation_of("p1")
    assert g3_p1 > g0_p1 + 1  # strictly monotonic
    # p2 still untouched
    assert cache.generation_of("p2") == g0_p2


# ===========================================================================
# Scenario 12: MAX_ENTRIES eviction — once over 4096, oldest (smallest
#              fetched_at) gets evicted lazily on write.
# ===========================================================================


async def test_max_entries_evicts_oldest_on_write(cache_and_upstream, fake_clock, monkeypatch):
    """Oracle #12: with MAX_ENTRIES exceeded, the next write evicts the
    entries with the smallest ``fetched_at`` (oldest). Implementation uses
    module-level ``MAX_ENTRIES``; monkeypatch it to a small value (3) so
    the test is fast and deterministic."""
    import oc_slimapi.children_cache as cc_mod
    monkeypatch.setattr(cc_mod, "MAX_ENTRIES", 3)
    # Re-create the cache so the small bound is observed by the instance.
    def handler(req):
        return _ok_children([_make_session_info("c1", created=10)])
    upstream = FakeUpstream(handler)
    cache = ChildrenCache(upstream)

    # Fill 3 entries, advancing the clock between writes so fetched_at differs.
    await cache.get_or_fetch("p1", "/a")
    fake_clock.advance(1.0)
    await cache.get_or_fetch("p2", "/a")
    fake_clock.advance(1.0)
    await cache.get_or_fetch("p3", "/a")
    assert len(cache._cache) == 3  # noqa: SLF001

    # 4th write triggers eviction of the oldest (p1).
    fake_clock.advance(1.0)
    await cache.get_or_fetch("p4", "/a")
    keys = cache._cache.keys()  # noqa: SLF001
    # p1 must NOT be present; p2/p3/p4 must be.
    def _sid_of(k):
        return k[0] if isinstance(k, tuple) else k
    sids = {_sid_of(k) for k in keys}
    assert "p1" not in sids, f"oldest entry was not evicted; sids={sids}"
    assert {"p2", "p3", "p4"} <= sids

    # After eviction a re-fetch of p1 hits upstream again (cache miss).
    before = upstream.call_count
    await cache.get_or_fetch("p1", "/a")
    assert upstream.call_count == before + 1


async def test_max_entries_clears_expired_before_oldest_eviction(
    cache_and_upstream, fake_clock, monkeypatch,
):
    """Writes remove all stale entries before evicting fresh oldest entries."""
    from oc_slimapi.children_cache import CacheEntry
    import oc_slimapi.children_cache as cc_mod

    cache, upstream = cache_and_upstream
    monkeypatch.setattr(cc_mod, "MAX_ENTRIES", 3)
    now = fake_clock.now
    cache._cache.update({
        ("stale-1", "/a"): CacheEntry([], 0, 0, now - 4, now - 1, True),
        ("stale-2", "/a"): CacheEntry([], 0, 0, now - 3, now - 1, True),
        ("fresh-1", "/a"): CacheEntry([], 0, 0, now - 2, now + 100, True),
        ("fresh-2", "/a"): CacheEntry([], 0, 0, now - 1, now + 100, True),
    })
    upstream._handler = lambda req: _ok_children([])

    await cache.get_or_fetch("new", "/a")

    sids = {key[0] for key in cache._cache}
    assert {"fresh-1", "fresh-2", "new"} <= sids
    assert not {"stale-1", "stale-2"} & sids
    assert len(cache._cache) <= 3


# ===========================================================================
# Version semantics — get_or_fetch's second return value == generation_of at
# fetch start; invalidate → next fetch returns a strictly greater version.
# ===========================================================================


async def test_version_matches_generation_at_fetch_start(cache_and_upstream):
    """Spec add-on (§16 generation guard): ``get_or_fetch`` returns
    ``(children, version)`` where ``version`` equals ``generation_of(sid)``
    sampled AT FETCH START (not at response time). After ``invalidate(sid)``,
    the next fetch returns a strictly greater version.

    Concretely: the very first fetch on a sid samples generation 0 (the
    default); the returned version MUST be 0 (the current generation, since
    nothing invalidated in between)."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: _ok_children([_make_session_info("c1", created=10)])

    # Pre-fetch baseline: generation starts at 0 for an unseen sid.
    assert cache.generation_of("p1") == 0

    _, v1 = await cache.get_or_fetch("p1", "/app")
    # Version returned == generation sampled at fetch start. Since no
    # invalidate happened, current generation == fetch-start generation.
    assert v1 == 0, (
        f"v1 should equal fetch-start generation (0); got v1={v1}, "
        f"current gen={cache.generation_of('p1')}"
    )
    # Generation counter itself is unchanged by a successful fetch.
    assert cache.generation_of("p1") == 0

    # Now invalidate → generation bumps; next fetch's version MUST be the new
    # generation sampled at its start, and strictly greater than v1.
    cache.invalidate("p1")
    new_gen = cache.generation_of("p1")
    assert new_gen > 0
    _, v2 = await cache.get_or_fetch("p1", "/app")
    assert v2 == new_gen, (
        f"v2 should equal post-invalidate generation; got v2={v2}, gen={new_gen}"
    )
    assert v2 > v1


# ===========================================================================
# Sorting: skeleton is applied (each child goes through skeleton_session).
# ===========================================================================


async def test_children_pass_through_skeleton_session(cache_and_upstream):
    """Spec (§2): each child Session.Info is projected through
    ``skeleton_session`` — non-whitelisted top-level keys are stripped. The
    cache must NOT bypass the skeleton (otherwise the slimapi size win is
    undone for children)."""
    cache, upstream = cache_and_upstream
    # Insert a noisy field that SESSION_KEYS does not whitelist; the cache
    # output must NOT contain it.
    info = _make_session_info("c1", created=10)
    info["hugeBlob"] = "x" * 1000
    upstream._handler = lambda req: _ok_children([info])
    children, _ = await cache.get_or_fetch("p1", "/app")
    assert len(children) == 1
    assert "hugeBlob" not in children[0]
    # Whitelisted keys survive
    for key in ("id", "parentID", "directory"):
        assert key in children[0]


# ===========================================================================
# fetch_json_mapped(expect=list) wiring — children path uses expect=list so a
# non-list upstream 200 → 503 upstream_unavailable (mapped via the cache).
# ===========================================================================


async def test_non_list_upstream_payload_maps_to_503_through_cache(cache_and_upstream):
    """Spec (§7 via §2): if upstream 200 returns a JSON dict (or any non-list)
    the cache MUST surface 503 ``upstream_unavailable`` rather than crashing
    or treating the dict as a one-element child list. Wires
    ``fetch_json_mapped(expect=list)`` into the cache fetch path."""
    cache, upstream = cache_and_upstream
    upstream._handler = lambda req: httpx.Response(
        200, content=orjson.dumps({"unexpected": "shape"}),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(CodedHTTPException) as ei:
        await cache.get_or_fetch("p1", "/app")
    assert ei.value.status_code == 503
    assert ei.value.code == "upstream_unavailable"


# ===========================================================================
# Test that fetch_json_mapped itself gains the expect= parameter (used by
# the children path). Cross-referenced from test_upstream_error_boundary.
# ===========================================================================


def test_fetch_json_mapped_accepts_expect_parameter():
    """Spec (task §"fetch_json_mapped extension"): ``fetch_json_mapped`` MUST
    accept ``expect: type = dict`` so the children path can call it with
    ``expect=list``. Default behaviour (dict required) is unchanged."""
    sig = inspect.signature(fetch_json_mapped)
    assert "expect" in sig.parameters, (
        "fetch_json_mapped must gain an `expect` parameter for Batch 3"
    )
    assert sig.parameters["expect"].default is dict
