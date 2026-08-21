"""Direct unit tests for ``routes/_aggregate_fanout.py`` (W3-3 / F-304).

The shared aggregation skeleton — directory discovery assembly
(:func:`discover_directories`), the semaphore-injected per-dir fetch
(:func:`fetch_items_for_dir`), and the byte/item-budget sliding-window
scheduler (:func:`collect_with_byte_budget`) — used to be duplicated
across ``routes/questions.py`` and ``routes/permissions.py`` and was
covered only INDIRECTLY through the route test files
(test_questions_routes.py / test_questions_coalesce.py /
test_permissions.py). These tests exercise the shared layer directly:

* ``collect_with_byte_budget`` — byte-budget truncation semantics (the
  budget-triggering dir is excluded from items/succeeded but never lands
  in ``errors[]``), exact-boundary acceptance (strict ``>``), item-cap
  truncation, per-dir ``error_code`` isolation, unexpected-exception
  isolation, strict index-order merging across concurrency windows, and
  cancellation of in-flight / un-started dirs on truncation;
* ``discover_directories`` — happy path (dedup + defensive skip +
  roots/archived/limit discovery call shape), discovery total failure
  (503 ``upstream_unavailable``), non-list body, the page-fills-at-limit
  truncation flag, and the coalescing LEVEL 1 lease path (concurrent
  callers join ONE discovery GET);
* ``fetch_items_for_dir`` — parameterized item path + entry projection +
  directory stamping, per-dir 5xx/4xx §7 code mapping, per-dir cap
  exceeded, non-list body, semaphore injection, and the coalescing
  LEVEL 2 flight key.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import orjson
import pytest
from fastapi import Request

from oc_slimapi.errors import CodedHTTPException
from oc_slimapi.routes import _aggregate_fanout as fanout
from oc_slimapi.singleflight import LeasedSingleFlight

CAP = 64 * 1024  # matches the route tests' max_response_bytes


def _request() -> Request:
    """Minimal Request: the shared layer only reads
    ``request.app.state.config.max_response_bytes`` (flight reserve) and
    the defensive traffic stash (scope state dict)."""
    app = SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(max_response_bytes=CAP),
    ))
    return Request({"type": "http", "app": app, "state": {}})


def _sessions_body(*directories: str) -> bytes:
    return orjson.dumps([
        {"id": f"ses_{i:04d}", "directory": d,
         "time": {"updated": 0, "created": 0}}
        for i, d in enumerate(directories)
    ])


def _discovery_upstream(*, body: bytes | None = None, status: int = 200,
                        gate: float = 0.0, calls: dict | None = None):
    """MockTransport upstream serving only /experimental/session."""
    sessions = _sessions_body("/a", "/b") if body is None else body

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/experimental/session":
            if calls is not None:
                calls["discovery"] = calls.get("discovery", 0) + 1
                calls["params"] = dict(request.url.params)
            if gate:
                await asyncio.sleep(gate)
            return httpx.Response(status, content=sessions,
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    return httpx.AsyncClient(base_url="http://127.0.0.1:4096",
                             transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# _directories_from_sessions — derivation is dedup'd, first-seen, defensive
# ---------------------------------------------------------------------------

def test_directories_from_sessions_dedup_first_seen_and_defensive_skip():
    payload = [
        {"directory": "/a"},
        {"directory": "/a"},          # duplicate → dedup
        {"directory": ""},            # empty → skip
        {"directory": None},          # non-string → skip
        {"no": "directory"},          # missing → skip
        "not-a-dict",                 # non-dict → skip
        {"directory": "/b"},
    ]
    assert fanout._directories_from_sessions(payload) == ["/a", "/b"]


# ---------------------------------------------------------------------------
# collect_with_byte_budget — the scheduler, with fake workers (no HTTP)
# ---------------------------------------------------------------------------

async def test_collect_byte_budget_truncation_semantics():
    """3 dirs × 100 body bytes, aggregate_cap=250 → d0/d1 accepted; d2
    triggers the budget: truncated=True, d2 NOT in items/succeeded AND NOT
    in errors[] (cancelled/unconsumed dirs are communicated via the
    truncated flag, not errors)."""
    async def worker(d: str):
        return [{"id": d}], None, 100

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        ["/d0", "/d1", "/d2"], worker,
        concurrency=2, aggregate_cap=250, item_cap=100,
    )
    assert truncated is True
    assert [i["id"] for i in items] == ["/d0", "/d1"]
    assert succeeded == ["/d0", "/d1"]
    assert errors == []


async def test_collect_byte_budget_exact_boundary_accepted():
    """used + body == cap exactly is NOT over the cap (strict ``>``): both
    dirs accepted, no truncation."""
    async def worker(d: str):
        return [{"id": d}], None, 100

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        ["/d0", "/d1"], worker,
        concurrency=2, aggregate_cap=200, item_cap=100,
    )
    assert truncated is False
    assert len(items) == 2
    assert succeeded == ["/d0", "/d1"]
    assert errors == []


async def test_collect_item_cap_truncation():
    """item_cap fires like the byte budget: 2 items/dir, item_cap=3 → d0
    accepted (2), d1 triggers 2+2 > 3."""
    async def worker(d: str):
        return [{"id": d, "n": 1}, {"id": d, "n": 2}], None, 10

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        ["/d0", "/d1"], worker,
        concurrency=2, aggregate_cap=10_000, item_cap=3,
    )
    assert truncated is True
    assert len(items) == 2
    assert succeeded == ["/d0"]
    assert errors == []


async def test_collect_per_dir_error_code_isolated():
    """A worker-returned error_code (the per-dir 5xx/4xx path) lands in
    errors[] with the directory + code; other dirs succeed; no truncation
    (failed dirs have body_bytes=0 and never occupy the budget)."""
    async def worker(d: str):
        if d == "/bad":
            return [], "upstream_unavailable", 0
        return [{"id": d}], None, 50

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        ["/a", "/bad", "/c"], worker,
        concurrency=3, aggregate_cap=10_000, item_cap=100,
    )
    assert truncated is False
    assert [i["id"] for i in items] == ["/a", "/c"]
    assert errors == [{"directory": "/bad", "code": "upstream_unavailable"}]
    assert succeeded == ["/a", "/c"]


async def test_collect_unexpected_worker_exception_isolated():
    """An unexpected exception from the worker is isolated as
    upstream_unavailable for that directory (never aborts the batch)."""
    async def worker(d: str):
        if d == "/boom":
            raise RuntimeError("unexpected")
        return [{"id": d}], None, 50

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        ["/a", "/boom"], worker,
        concurrency=2, aggregate_cap=10_000, item_cap=100,
    )
    assert truncated is False
    assert [i["id"] for i in items] == ["/a"]
    assert errors == [{"directory": "/boom", "code": "upstream_unavailable"}]


async def test_collect_strict_index_order_across_windows():
    """12 dirs > window 4: even though later dirs COMPLETE first (reverse
    delays), merged items/succeeded stay in original directory order —
    the cross-batch strict-order contract pinned by the N6
    questions_fanout_window12 golden."""
    dirs = [f"/d{i:02d}" for i in range(12)]

    async def worker(d: str):
        idx = dirs.index(d)
        await asyncio.sleep((len(dirs) - idx) * 0.005)  # later dirs finish first
        return [{"id": d}], None, 1

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        dirs, worker,
        concurrency=4, aggregate_cap=10_000, item_cap=10_000,
    )
    assert truncated is False
    assert errors == []
    assert [i["id"] for i in items] == dirs
    assert succeeded == dirs


async def test_collect_truncation_cancels_inflight_and_skips_unstarted():
    """On truncation at the FIRST consumed dir: the other in-window tasks
    are cancelled (CancelledError delivered), dirs beyond the window are
    never launched, and the cancelled dirs appear nowhere in the result."""
    dirs = [f"/d{i}" for i in range(6)]
    started: list[str] = []
    finished: list[str] = []
    cancelled: list[str] = []

    async def worker(d: str):
        started.append(d)
        try:
            # d0 finishes fast; the rest are still in flight when d0's
            # outcome blows the budget (0 + 100 > 50) → deterministic cancel
            await asyncio.sleep(0.01 if d == dirs[0] else 0.5)
            finished.append(d)
            return [{"id": d}], None, 100
        except asyncio.CancelledError:
            cancelled.append(d)
            raise

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        dirs, worker,
        concurrency=4, aggregate_cap=50, item_cap=1000,
    )
    assert truncated is True
    assert items == []
    assert succeeded == []
    assert errors == []  # cancelled dirs are not errors
    assert started == dirs[:4]      # only the initial window ever launched
    assert finished == [dirs[0]]
    assert sorted(cancelled) == dirs[1:4]


async def test_collect_empty_directories():
    async def worker(d: str):  # pragma: no cover - must never be called
        raise AssertionError("worker must not run for an empty dir list")

    items, errors, succeeded, truncated = await fanout.collect_with_byte_budget(
        [], worker, concurrency=4, aggregate_cap=100, item_cap=10,
    )
    assert (items, errors, succeeded, truncated) == ([], [], [], False)


# ---------------------------------------------------------------------------
# discover_directories — discovery input assembly (direct, no route)
# ---------------------------------------------------------------------------

async def test_discover_directories_happy_path_and_call_shape():
    """Direct (non-coalesced) discovery: distinct directories derived from
    the sessions' real ``directory`` field, and the upstream GET carries
    roots=true&archived=true&limit — the discovery call shape the route
    tests also pin."""
    calls: dict = {}
    upstream = _discovery_upstream(calls=calls)
    try:
        directories, complete = await fanout.discover_directories(
            upstream, _request(), limit=10_000, reserve_bytes=CAP,
        )
    finally:
        await upstream.aclose()
    assert directories == ["/a", "/b"]
    assert complete is True
    assert calls["discovery"] == 1
    assert calls["params"]["roots"] == "true"
    assert calls["params"]["archived"] == "true"
    assert calls["params"]["limit"] == "10000"


async def test_discover_directories_total_failure_5xx():
    """Discovery 5xx → 503 upstream_unavailable (total failure — the §7
    discovery exception; the upstream status is never leaked)."""
    upstream = _discovery_upstream(status=500)
    try:
        with pytest.raises(CodedHTTPException) as excinfo:
            await fanout.discover_directories(
                upstream, _request(), limit=100, reserve_bytes=CAP,
            )
    finally:
        await upstream.aclose()
    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "upstream_unavailable"


async def test_discover_directories_non_list_body_total_failure():
    upstream = _discovery_upstream(body=b'{"unexpected":"shape"}')
    try:
        with pytest.raises(CodedHTTPException) as excinfo:
            await fanout.discover_directories(
                upstream, _request(), limit=100, reserve_bytes=CAP,
            )
    finally:
        await upstream.aclose()
    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "upstream_unavailable"


async def test_discover_directories_page_fills_at_limit_incomplete():
    """Exactly ``limit`` sessions → possible truncation → complete=False
    (the flag that degrades the envelope's authoritativeDirectories)."""
    upstream = _discovery_upstream(body=_sessions_body("/a", "/b", "/c"))
    try:
        directories, complete = await fanout.discover_directories(
            upstream, _request(), limit=3, reserve_bytes=CAP,
        )
    finally:
        await upstream.aclose()
    assert directories == ["/a", "/b", "/c"]
    assert complete is False


async def test_discover_directories_coalesced_single_flight():
    """Coalescing LEVEL 1 (direct): two concurrent discover_directories
    calls through one registry join ONE discovery flight — one upstream
    GET, identical caller-owned results, ledger drained after shutdown."""
    calls: dict = {}
    upstream = _discovery_upstream(gate=0.05, calls=calls)
    registry = LeasedSingleFlight(max_bytes=4 * CAP, network_concurrency=8)
    try:
        first, second = await asyncio.gather(
            fanout.discover_directories(
                upstream, _request(), limit=10_000,
                registry=registry, reserve_bytes=CAP),
            fanout.discover_directories(
                upstream, _request(), limit=10_000,
                registry=registry, reserve_bytes=CAP),
        )
    finally:
        await upstream.aclose()
        registry.shutdown()
    assert calls["discovery"] == 1
    assert first == (["/a", "/b"], True)
    assert second == (["/a", "/b"], True)
    assert registry.leased_bytes == 0


# ---------------------------------------------------------------------------
# fetch_items_for_dir — the parameterized per-dir fetch skeleton
# ---------------------------------------------------------------------------

def _items_upstream(item_path: str, *, per_dir: dict | None = None,
                    status: int = 200, body: bytes | None = None,
                    gate: float = 0.0, calls: dict | None = None):
    """MockTransport upstream serving ``item_path`` per X-Opencode-Directory
    (``per_dir`` maps directory → list of entries; directories absent from
    the map get the default single-entry body)."""
    default = [{"id": "item_1", "sessionID": "s"}]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == item_path:
            directory = request.headers.get("x-opencode-directory", "")
            if calls is not None:
                calls[item_path] = calls.get(item_path, {})
                calls[item_path][directory] = (
                    calls[item_path].get(directory, 0) + 1)
            if gate:
                await asyncio.sleep(gate)
            if status != 200:
                return httpx.Response(status, content=b"boom")
            if body is not None:
                content = body  # pre-encoded exact bytes
            else:
                payload = (per_dir.get(directory, default)
                           if per_dir is not None else default)
                content = orjson.dumps(payload)
            return httpx.Response(200, content=content,
                                  headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    return httpx.AsyncClient(base_url="http://127.0.0.1:4096",
                             transport=httpx.MockTransport(handler))


def _keep_entry(entry: dict) -> dict:
    """Questions-style projection: verbatim passthrough."""
    return entry


async def _fetch(upstream, directory="/a", *, cap=CAP, registry=None,
                 project=_keep_entry, semaphore=None):
    return await fanout.fetch_items_for_dir(
        upstream, _request(), directory,
        cap=cap,
        item_path="/question",
        semaphore=semaphore or asyncio.Semaphore(4),
        flight_key_prefix="question-dir",
        project_entry=project,
        registry=registry,
    )


async def test_fetch_items_projects_entries_and_stamps_directory():
    """Success path: dict entries are projected via project_entry and
    stamped with the directory; non-dict entries are dropped; body_bytes
    is the RAW body byte count used for budget accounting."""
    body = orjson.dumps([
        {"id": "q1"}, {"id": "q2"}, "not-a-dict", 7,
    ])
    upstream = _items_upstream("/question", body=body)
    try:
        items, error_code, body_bytes = await _fetch(upstream)
    finally:
        await upstream.aclose()
    assert error_code is None
    assert items == [
        {"id": "q1", "directory": "/a"},
        {"id": "q2", "directory": "/a"},
    ]
    assert body_bytes == len(body)


async def test_fetch_items_projection_parameterized():
    """The projection callable is the route's field mapping: a whitelist
    projection drops unknown fields BEFORE the directory stamp (the
    permissions route binding, exercised directly)."""
    upstream = _items_upstream("/permission", body=orjson.dumps([
        {"id": "per_1", "sessionID": "s", "permission": "bash",
         "extra": "drop-me"},
    ]))
    try:
        items, error_code, _ = await fanout.fetch_items_for_dir(
            upstream, _request(), "/wd",
            cap=CAP,
            item_path="/permission",
            semaphore=asyncio.Semaphore(4),
            flight_key_prefix="permission-dir",
            project_entry=lambda e: {
                k: e[k] for k in ("id", "sessionID", "permission")
                if k in e
            },
        )
    finally:
        await upstream.aclose()
    assert error_code is None
    assert items == [{"id": "per_1", "sessionID": "s",
                      "permission": "bash", "directory": "/wd"}]


async def test_fetch_items_5xx_and_4xx_error_code_mapping():
    """Per-dir §7 mapping: 5xx → upstream_unavailable, 4xx →
    upstream_http_N — isolated as ([], code, 0) (never raises, body_bytes=0
    so a failed dir never occupies the aggregate budget)."""
    upstream = _items_upstream("/question", status=500)
    try:
        items, error_code, body_bytes = await _fetch(upstream)
    finally:
        await upstream.aclose()
    assert (items, error_code, body_bytes) == ([], "upstream_unavailable", 0)

    upstream = _items_upstream("/question", status=403)
    try:
        items, error_code, body_bytes = await _fetch(upstream)
    finally:
        await upstream.aclose()
    assert (items, error_code, body_bytes) == ([], "upstream_http_403", 0)


async def test_fetch_items_per_dir_cap_exceeded():
    """Body over the injected per-dir cap → ([], upstream_unavailable, 0)."""
    upstream = _items_upstream("/question",
                               body=orjson.dumps([{"id": f"q{i}"} for i in range(500)]))
    try:
        items, error_code, body_bytes = await _fetch(upstream, cap=16)
    finally:
        await upstream.aclose()
    assert (items, error_code, body_bytes) == ([], "upstream_unavailable", 0)


async def test_fetch_items_non_list_body_fails_dir():
    upstream = _items_upstream("/question", body=b'{"unexpected":"shape"}')
    try:
        items, error_code, body_bytes = await _fetch(upstream)
    finally:
        await upstream.aclose()
    assert (items, error_code, body_bytes) == ([], "upstream_unavailable", 0)


class _CountingSemaphore:
    """Records the peak number of concurrent holders (test double for the
    injected app.state semaphore)."""

    def __init__(self, n: int):
        self._sem = asyncio.Semaphore(n)
        self.active = 0
        self.peak = 0

    async def __aenter__(self):
        await self._sem.acquire()
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0)  # let siblings queue deterministically
        return self

    async def __aexit__(self, *exc):
        self.active -= 1
        self._sem.release()
        return False


async def test_fetch_items_semaphore_injection_bounds_concurrency():
    """The semaphore is INJECTED (questions_semaphore / permissions_
    semaphore at the route): a capacity-1 injection serializes concurrent
    per-dir GETs (peak == 1) and both callers still get their own items."""
    upstream = _items_upstream("/question", gate=0.05)
    semaphore = _CountingSemaphore(1)
    try:
        results = await asyncio.gather(
            _fetch(upstream, "/a", semaphore=semaphore),
            _fetch(upstream, "/b", semaphore=semaphore),
        )
    finally:
        await upstream.aclose()
    assert semaphore.peak == 1
    assert semaphore.active == 0
    for (items, error_code, _), directory in zip(results, ("/a", "/b")):
        assert error_code is None
        assert items == [{"id": "item_1", "sessionID": "s",
                          "directory": directory}]


async def test_fetch_items_registry_single_flights_per_dir():
    """Coalescing LEVEL 2 (direct): concurrent fetches for the SAME
    directory through one registry share ONE upstream GET and both get
    byte-identical items (the flight key is
    (prefix, id(upstream), directory))."""
    calls: dict = {}
    upstream = _items_upstream("/question", gate=0.05, calls=calls)
    registry = LeasedSingleFlight(max_bytes=4 * CAP, network_concurrency=8)
    try:
        first, second = await asyncio.gather(
            _fetch(upstream, "/a", registry=registry),
            _fetch(upstream, "/a", registry=registry),
        )
    finally:
        await upstream.aclose()
        registry.shutdown()
    assert calls["/question"] == {"/a": 1}
    assert first == second
    assert first[0] == [{"id": "item_1", "sessionID": "s",
                         "directory": "/a"}]
    assert registry.leased_bytes == 0
