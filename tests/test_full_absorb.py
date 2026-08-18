"""L2-CD-1: /full single-flight + transform-busy budget absorb.

Locks three behaviours of ``GET /slimapi/messages/{sid}/full/{mid}``
(plan Task L2-CD-1, oracle §C-2 / §D-1):

* **CD1-C1 single-flight** — N concurrent same-mid requests coalesce onto
  ONE upstream GET. The direct /full handler joins the process-level
  ``singleflight.fulls`` entry for ``(sid, mid, directory)``; with
  ``max_transforms=1`` the admission queue serializes requests, so the
  completed fetch result stays joinable for a short grace window — the
  burst must still produce exactly one upstream call, all 200, identical
  bodies.
* **CD1-C2 budget absorb** — a transform slot held 2.2s
  (``> transform_wait_seconds`` 2s but ``< transform_absorb_budget_seconds``
  2.5s) is ABSORBED: the waiting request re-attempts admission narrowed to
  the remaining budget, acquires on release, and returns 200 (a single
  fixed 2s wait would 503 here).
* **CD1-C3 budget exhaustion + key isolation** — a slot held past the
  budget (3.5s) yields the unchanged 503 ``transform_busy`` shape
  (``{"code", "retry_after"}`` body + ``Retry-After: 2`` header), and
  different mids never share a single-flight entry.

Settings are pinned explicitly (wait=2.0, budget=2.5, max_transforms=1)
rather than read from env, so the timing assertions below are isolated
from the developer's environment.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages
from oc_slimapi import singleflight as sf_mod
from oc_slimapi.transform import TransformConfig, TransformPool

HDR = {"X-Slimapi-Version": "2"}

# Small well-formed single message (dict shape passes the non-dict guard in
# strip_diagnostics_and_pack).
FULL_MESSAGE = {
    "info": {"id": "m1", "role": "user", "time": {"created": 1, "updated": 1}},
    "parts": [
        {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
    ],
}


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=2.0,
        transform_absorb_budget_seconds=2.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient, settings: Settings) -> FastAPI:
    """Fresh app: version middleware → messages router → catch-all proxy →
    coded-exception handlers. Mirrors the other route test modules (no
    module-level lifespan, no smoke probe)."""
    app = FastAPI(title="oc-slimapi-cd1-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(messages.router)
    install_proxy(app)
    register_error_handlers(app)
    return app


@asynccontextmanager
async def _test_client(upstream_factory, handler, **overrides):
    """Build (mock upstream → fresh app → ASGI client) with teardown.

    Each test gets its own app, hence its own TransformPool — which also
    namespaces the single-flight keys (the key embeds the pool identity),
    so no state leaks between tests through the process-level registry.
    """
    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings(**overrides))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0,
    ) as client:
        try:
            yield client
        finally:
            app.state.transforms.shutdown()


def _full_body(message: dict) -> bytes:
    return orjson.dumps(message)


# ---------------------------------------------------------------------------
# CD1-C1: single-flight merges concurrent same-mid /full requests.
# ---------------------------------------------------------------------------

async def test_single_flight_merges_concurrent_same_mid(upstream_factory):
    """20 concurrent /full for the same (sid, mid) → the upstream handler is
    called EXACTLY once, every response is 200, all bodies identical."""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        await asyncio.sleep(0.2)  # widen the race window for the join
        return httpx.Response(200, content=_full_body(FULL_MESSAGE))

    async with _test_client(upstream_factory, handler) as client:
        results = await asyncio.gather(*[
            client.get("/slimapi/messages/s1/full/m1", headers=HDR)
            for _ in range(20)
        ])

    assert all(r.status_code == 200 for r in results)
    assert calls["n"] == 1
    # Identical bytes across the whole burst (shared fetch → shared pack).
    assert len({r.content for r in results}) == 1


# ---------------------------------------------------------------------------
# CD1-C2: transient slot occupancy within the absorb budget → absorbed.
# ---------------------------------------------------------------------------

async def test_busy_absorbed_within_budget(upstream_factory):
    """Slot held 2.2s (> wait 2.0s, < budget 2.5s): the second /full for a
    DIFFERENT mid must be absorbed → 200, not 503 transform_busy.

    Rev-fix 3 (sync): the holder signals via ``asyncio.Event`` once its
    upstream handler is ENTERED — the /full route acquires admission BEFORE
    the GET, so handler-entry proves the slot is held. The waiter only
    starts after that signal (no sleep-race), and an acquire spy on the
    pool proves the absorb path: ≥2 admission attempts for the waiter (the
    first times out at the 2.0s ``transform_wait_seconds``; a retrier at the
    full wait would be a naive loop), total wall time inside the 2.5s
    budget, and the holder's release at 2.2s is what the narrowed second
    attempt (≤0.5s) waits out.
    """
    calls = {"m1": 0, "m_hold": 0}
    holder_in_handler = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/m_hold"):
            calls["m_hold"] += 1
            holder_in_handler.set()  # slot acquired (GET started) — sync point
            await asyncio.sleep(2.2)  # occupancy: wait 2.0s < t < budget 2.5s
            return httpx.Response(200, content=_full_body({
                **FULL_MESSAGE, "info": {"id": "m_hold", "role": "user"},
            }))
        calls["m1"] += 1
        return httpx.Response(200, content=_full_body(FULL_MESSAGE))

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings())
    pool = app.state.transforms
    # Spy on admission attempts (instance attribute shadows the method; the
    # pool is per-test so no restore is needed).
    acquire_times: list[float] = []
    original_acquire = pool.acquire

    async def _spy_acquire(timeout: float | None = None) -> None:
        acquire_times.append(time.monotonic())
        await original_acquire(timeout)

    pool.acquire = _spy_acquire  # type: ignore[method-assign]
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30.0,
        ) as client:
            holder = asyncio.create_task(
                client.get("/slimapi/messages/s1/full/m_hold", headers=HDR)
            )
            await asyncio.wait_for(holder_in_handler.wait(), timeout=5.0)
            waiter_start = time.monotonic()
            absorbed = await client.get(
                "/slimapi/messages/s1/full/m1", headers=HDR,
            )
            total = time.monotonic() - waiter_start
            held = await holder
    finally:
        app.state.transforms.shutdown()

    assert absorbed.status_code == 200
    assert absorbed.json()["info"]["id"] == "m1"
    assert held.status_code == 200
    assert calls["m1"] == 1
    assert calls["m_hold"] == 1
    # The absorb loop RETRIED admission: the waiter's first attempt timed
    # out at the 2.0s wait and a narrowed (≤ remaining budget) second
    # attempt acquired the slot when the holder released it at 2.2s.
    waiter_attempts = [t for t in acquire_times if t >= waiter_start]
    assert len(waiter_attempts) >= 2, acquire_times
    # Inside the 2.5s budget (a naive full-wait retry would need ~4s), and
    # the waiter really outlasted the 2.0s single-attempt wait.
    assert 2.0 < total < 2.5, total


# ---------------------------------------------------------------------------
# CD1-C3: budget exhaustion → unchanged 503 transform_busy shape.
# ---------------------------------------------------------------------------

async def test_busy_over_budget_503_shape_unchanged(upstream_factory):
    """Slot held 3.5s (> budget 2.5s): the waiter exhausts its budget and
    gets the byte-stable 503 transform_busy response, identical to the
    pre-CD-1 single-attempt shape.

    Rev-fix 4: the shape is locked against a HARDCODED baseline (captured
    from the ASGI app once, ``Accept-Encoding: identity`` to keep the gzip
    path out of the byte comparison) — body bytes AND the complete header
    set compared exactly, so any accidental drift in the 503 builder fails
    here, not at a client.
    """
    calls = {"m1": 0}
    holder_in_handler = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/m_hold"):
            # Sync point (rev-fix 3 pattern): the upstream handler entry
            # proves the holder's route already acquired the single
            # admission slot (admission precedes the GET).
            holder_in_handler.set()
            await asyncio.sleep(3.5)  # beyond the 2.5s absorb budget
            return httpx.Response(200, content=_full_body({
                **FULL_MESSAGE, "info": {"id": "m_hold", "role": "user"},
            }))
        calls["m1"] += 1
        return httpx.Response(200, content=_full_body(FULL_MESSAGE))

    async with _test_client(upstream_factory, handler) as client:
        holder = asyncio.create_task(
            client.get("/slimapi/messages/s1/full/m_hold", headers=HDR)
        )
        await asyncio.wait_for(holder_in_handler.wait(), timeout=5.0)
        rejected = await client.get(
            "/slimapi/messages/s1/full/m1",
            headers={**HDR, "Accept-Encoding": "identity"},
        )
        held = await holder

    # -- Hardcoded baseline (byte-exact body + complete header set) --------
    assert rejected.status_code == 503
    assert rejected.content == b'{"code":"transform_busy","retry_after":2}'
    assert dict(rejected.headers) == {
        "vary": "Accept-Encoding",
        "content-length": "41",
        "content-type": "application/json",
        "retry-after": "2",
    }
    # The 503'd request never reached upstream (admission precedes the GET).
    assert calls["m1"] == 0
    assert held.status_code == 200


# ---------------------------------------------------------------------------
# CD1-C3 (key isolation): different mids never share a flight.
# ---------------------------------------------------------------------------

async def test_different_mids_do_not_share_single_flight(upstream_factory):
    """Concurrent /full for two different mids → one upstream call EACH
    (no cross-mid dedup), both 200 with their own bodies."""
    calls = {"m1": 0, "m2": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)  # overlap the two fetches
        if request.url.path.endswith("/m2"):
            calls["m2"] += 1
            return httpx.Response(200, content=_full_body({
                **FULL_MESSAGE, "info": {"id": "m2", "role": "user"},
            }))
        calls["m1"] += 1
        return httpx.Response(200, content=_full_body(FULL_MESSAGE))

    async with _test_client(upstream_factory, handler) as client:
        r1, r2 = await asyncio.gather(
            client.get("/slimapi/messages/s1/full/m1", headers=HDR),
            client.get("/slimapi/messages/s1/full/m2", headers=HDR),
        )

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["info"]["id"] == "m1"
    assert r2.json()["info"]["id"] == "m2"
    assert calls["m1"] == 1 and calls["m2"] == 1


# ---------------------------------------------------------------------------
# Rev-fix 4: SingleFlight unit behaviour (exception propagation, leader
# cancellation self-healing, active grace expiry, retention guards).
# Direct unit tests against the class — no app, no upstream.
# ---------------------------------------------------------------------------

async def test_singleflight_waiter_receives_leader_exception():
    """A factory exception propagates to every waiter as the SAME exception
    instance — delivered via the FetchFailed result envelope (never
    set_exception), so a zero-waiter failure cannot leak 'Future exception
    was never retrieved' warnings either."""
    sf = sf_mod.SingleFlight(result_grace_seconds=0.05)
    boom = RuntimeError("boom")
    factory_started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        factory_started.set()
        await release.wait()  # hold the flight open until the waiter joined
        raise boom

    leader = asyncio.create_task(sf.fetch("k", factory))
    await asyncio.wait_for(factory_started.wait(), timeout=5.0)
    waiter = asyncio.create_task(sf.fetch("k", factory))
    await asyncio.sleep(0.05)  # let the waiter park on the shared future
    release.set()

    with pytest.raises(RuntimeError) as leader_exc:
        await leader
    with pytest.raises(RuntimeError) as waiter_exc:
        await waiter
    assert leader_exc.value is boom
    assert waiter_exc.value is boom  # same instance, not a copy
    assert calls["n"] == 1  # the failure was shared, not re-led
    assert not sf._entries  # failure dropped the entry (no negative cache)


async def test_singleflight_leader_cancellation_self_heals():
    """Cancelling the leader mid-factory cancels the shared future; a LIVE
    waiter treats that as 'flight died', re-leads, and succeeds."""
    sf = sf_mod.SingleFlight(result_grace_seconds=0.05)
    calls = {"n": 0}
    park = asyncio.Event()

    async def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            await park.wait()  # first (doomed) flight never completes
        return "ok"

    leader = asyncio.create_task(sf.fetch("k", factory))
    await asyncio.sleep(0.05)  # leader parked inside the factory
    waiter = asyncio.create_task(sf.fetch("k", factory))
    await asyncio.sleep(0.05)  # waiter joined the doomed flight

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    park.set()  # irrelevant for the doomed flight; keeps a re-lead honest

    assert await waiter == "ok"  # waiter re-led and succeeded
    assert calls["n"] == 2  # exactly one re-lead, no storm


async def test_singleflight_grace_expiry_refetches_and_actively_cleans():
    """Past the grace window a same-key fetch re-runs the factory (no
    reuse), and the completed entry is removed ACTIVELY by the call_later
    expiry callback — without any same-key fetch to lazily expire it."""
    sf = sf_mod.SingleFlight(result_grace_seconds=0.05)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return b"x"

    assert await sf.fetch("k", factory) == b"x"
    assert calls["n"] == 1
    assert "k" in sf._entries  # retained for the grace window

    await asyncio.sleep(0.15)  # past grace + timer-callback margin
    assert "k" not in sf._entries  # ACTIVE cleanup fired (timer), not lazy

    assert await sf.fetch("k", factory) == b"x"
    assert calls["n"] == 2  # fresh flight after grace


async def test_singleflight_retention_bounds_hold_under_concurrent_completions():
    """With injected small bounds, many DIFFERENT keys completing back to
    back stay within both guards: ≤ max_retained_entries entries and
    ≤ max_retained_bytes retained — enforced at completion time (eviction
    runs on the same event-loop serial point as the accounting)."""
    sf = sf_mod.SingleFlight(
        result_grace_seconds=60.0,  # nothing expires naturally in-test
        max_retained_entries=4,
        max_retained_bytes=3 * 1024,
    )
    body = b"x" * 1024

    async def factory():
        await asyncio.sleep(0.02)  # stagger the completions
        return body

    results = await asyncio.gather(*[
        sf.fetch(("k", i), factory) for i in range(8)
    ])
    assert all(r == body for r in results)
    # 8 completions × 1 KiB = 8 KiB > 3 KiB bound and 8 > 4 entries —
    # eviction-on-completion MUST have fired.
    assert len(sf._entries) <= sf._max_entries
    assert sf._retained_bytes <= sf._max_bytes


# ===========================================================================
# rev-9: lifecycle convergence of the single-flight registry.
# ===========================================================================

async def test_singleflight_shutdown_clears_retained_entries_and_timers():
    """shutdown() cancels pending grace-expiry timers, drops retained
    entries and zeroes the byte ledger — a stopped app leaves no stale
    app-domain bodies or loop.call_later callbacks behind."""
    sf = sf_mod.SingleFlight(result_grace_seconds=60.0)
    body = b"x" * 100

    async def factory():
        return body

    assert await sf.fetch("k", factory) == body
    entry = sf._entries["k"]
    assert sf._retained_bytes == 100
    assert entry.timer is not None and not entry.timer.cancelled()

    sf.shutdown()

    assert not sf._entries
    assert sf._retained_bytes == 0
    assert entry.timer.cancelled()


async def test_singleflight_shutdown_inflight_completes_without_retention():
    """An in-flight flight is NOT cancelled by shutdown — its registration
    is dropped (new same-key callers re-lead; convergence first) but the
    pending future resolves naturally, and the late completion must NOT
    re-register, re-account bytes, or arm a new timer."""
    sf = sf_mod.SingleFlight(result_grace_seconds=60.0)
    started, release = asyncio.Event(), asyncio.Event()

    async def factory():
        started.set()
        await release.wait()
        return b"late"

    leader = asyncio.create_task(sf.fetch("k", factory))
    await started.wait()
    assert sf.in_flight("k")

    sf.shutdown()
    assert "k" not in sf._entries  # registration gone, flight itself alive

    release.set()
    assert await leader == b"late"  # waiter/leader resolve naturally
    await asyncio.sleep(0)  # let the completion path run to the end
    assert not sf._entries
    assert sf._retained_bytes == 0  # no post-shutdown re-accounting


async def test_singleflight_reusable_after_shutdown_and_entry_isolation(
    monkeypatch,
):
    """Two rev-9 properties: (1) the registry stays USABLE after shutdown —
    fetch() leads fresh flights with full retention semantics (no
    deadlock, no dead registry); (2) per-entry cleanup failures are
    ISOLATED — a hostile ``_drop`` for one key cannot abort the
    convergence of the others, and the fallback refund keeps the byte
    ledger exact."""
    sf = sf_mod.SingleFlight(result_grace_seconds=60.0)
    calls = {"n": 0}

    async def _mk(value: bytes):
        async def factory():
            calls["n"] += 1
            return value
        return factory

    await sf.fetch("k1", await _mk(b"a" * 10))
    await sf.fetch("k2", await _mk(b"b" * 20))
    assert sf._retained_bytes == 30

    original_drop = sf._drop

    def _boom_for_k1(key):
        if key == "k1":
            raise RuntimeError("hostile drop")
        return original_drop(key)

    monkeypatch.setattr(sf, "_drop", _boom_for_k1)
    sf.shutdown()
    monkeypatch.undo()

    # Both entries gone; k1 via the isolation fallback, which also refunds
    # from the entry's own bookkeeping → the ledger stays exact.
    assert not sf._entries
    assert sf._retained_bytes == 0

    # Reusable: a post-shutdown fetch leads a fresh flight and retains it
    # normally (timer armed again).
    out = await sf.fetch("k3", await _mk(b"c" * 40))
    assert out == b"c" * 40
    assert calls["n"] == 3
    assert sf._entries["k3"].timer is not None

    sf.shutdown()  # final converge — clean drops this time
    assert sf._retained_bytes == 0
    assert not sf._entries


async def test_singleflight_cross_profile_api_misuse_raises():
    """Profile API separation (B6-1 review nits): a leased registry must
    use fetch_or_bypass(), a plain registry must use fetch() — the wrong
    entry point fails fast with an identifiably-worded RuntimeError
    instead of silently mis-accounting."""
    leased = sf_mod.SingleFlight(max_bytes=64)
    plain = sf_mod.SingleFlight()

    async def factory():
        return b"x"

    with pytest.raises(RuntimeError, match="plain-profile"):
        await leased.fetch("k", factory)
    with pytest.raises(RuntimeError, match="leased-profile"):
        await plain.fetch_or_bypass("k", factory, 8)


def test_singleflight_ctor_rejects_plain_only_kwargs_on_leased():
    """max_retained_entries/max_retained_bytes bound PLAIN retention; on a
    leased registry (max_bytes set) they are a caller bug → TypeError at
    construction, before any state exists. Production shapes (bare plain
    ``fulls``-style, leased with budget) must keep constructing fine."""
    with pytest.raises(TypeError, match="plain-profile-only"):
        sf_mod.SingleFlight(max_bytes=10, max_retained_entries=4)
    with pytest.raises(TypeError, match="plain-profile-only"):
        sf_mod.SingleFlight(max_bytes=10, max_retained_bytes=1024)

    # The two production shapes stay valid.
    assert sf_mod.SingleFlight() is not None
    assert sf_mod.SingleFlight(max_bytes=1024) is not None


def test_singleflight_ctor_rejects_leased_only_kwargs_on_plain():
    """network_concurrency caps leased LEADER factories only; on a plain
    registry (max_bytes=None, admission-free) it is a caller bug →
    TypeError. The raw-fetch production shape (budget + concurrency)
    stays valid."""
    with pytest.raises(TypeError, match="leased-profile-only"):
        sf_mod.SingleFlight(network_concurrency=2)

    assert sf_mod.SingleFlight(
        max_bytes=1024, network_concurrency=2
    ) is not None
