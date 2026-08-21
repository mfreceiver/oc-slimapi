"""Shared cross-directory aggregation framework (F-304 extraction).

``GET /slimapi/questions`` and ``GET /slimapi/permissions`` run the SAME
pipeline — discover workdirs → per-dir fan-out → byte/item-budget
aggregation with per-dir error isolation — which used to live as two
copy-pasted ~500-line route files (normalized similarity 0.832; fixes
historically landed on one side only, e.g. CHANGELOG 1.1.1/1.1.3/1.1.4).
This module owns the shared skeleton; each route file keeps ONLY its
envelope packer, its field mapping (projection), and its config knobs:

* :func:`discover_directories` — Step 1: the coalesced/direct
  ``GET /experimental/session?roots=true`` discovery and the derivation of
  the distinct directory list (the coalescing-LEVEL-1 lease discipline is
  implemented here once);
* :func:`fetch_items_for_dir` — the per-dir upstream GET skeleton
  (semaphore injection, per-dir byte cap, coalescing LEVEL 2, §7 error
  mapping), parameterized by item path (``/question`` vs ``/permission``),
  flight-key prefix, and the route's entry projection;
* :func:`collect_with_byte_budget` — the sliding-window fan-out scheduler
  with the aggregate byte budget, the aggregate item cap, and per-dir
  error collection, parameterized by a per-directory worker coroutine.

Everything here is a pure lift from the two route files — zero wire
change; the two envelopes' field sets and error paths are frozen (N6
golden ``refactor-baseline-v1.json`` pins the response bytes).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import orjson
from fastapi import Request

from ..discovery import (
    fetch_global_root_sessions,
    fetch_global_root_sessions_raw,
)
from ..traffic import stash_up_in
from ..transform import read_with_cap
from ..upstream import forward_directory_headers
from ..upstream_errors import (
    UPSTREAM_UNAVAILABLE,
    raise_upstream_unavailable,
    upstream_error_code_for_status,
)


class _DirFetchFailure(Exception):
    """Per-dir upstream failure raised INSIDE a shared flight factory
    (traffic plan Batch 1 / A4) so the flight FAILS — immediate budget
    refund, never grace-retained (no negative caching) — while every joiner
    re-raises the same instance and isolates its ``code`` into its own
    envelope ``errors[]`` (per-dir errors never abort the request)."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# P1-28: aggregate item budget (second-layer cap, T5-C10). Each per-dir
# /question (or /permission) response is capped by per_dir_cap, but
# items.extend() across all dirs can accumulate far beyond a single dir's
# cap. Once the merged item count exceeds this safety limit (or the byte
# budget), the envelope is marked ``truncated: true`` and remaining dirs
# are cancelled. The sliding window scheduler receives this as ``item_cap``
# (imported by both route files so their module-level bindings stay the
# tests' monkeypatch anchors).
_MAX_AGGREGATE_ITEMS = 10_000


def _directories_from_sessions(sessions_payload: list) -> list[str]:
    """Step 2 helper: derive the DISTINCT set of workdir directories
    (first-seen order) from each session's REAL ``directory`` field.

    Unlike /project's ``worktree`` (which normalizes non-git workdirs to
    "/" and must be skipped), the session ``directory`` is always a real
    path — no synthetic-global skip is needed. Skip non-string/empty
    defensively.

    Returns a caller-owned list of strings, so the (transient) expanded
    session graph can be dropped as soon as this returns — in the coalesced
    path it is called INSIDE the discovery lease and the graph is ``del``'d
    before the lease releases (final review B1 fix, 2026-08-16).
    """
    return list(dict.fromkeys(
        s["directory"]
        for s in sessions_payload
        if isinstance(s, dict)
           and isinstance(s.get("directory"), str)
           and s["directory"]
    ))


async def discover_directories(
    upstream_client: httpx.AsyncClient,
    request: Request,
    *,
    limit: int,
    registry=None,
    reserve_bytes: int,
) -> tuple[list[str], bool]:
    """Step 1 shared: discover workdirs via ``GET /experimental/session?
    roots=true`` [&archived=true] and derive the distinct directory list.

    Returns ``(directories, discovery_complete)`` where ``directories`` is a
    caller-owned list of strings (the transient session graph does NOT
    outlive this call).

    Coalescing LEVEL 1 (plan A4): when ``registry`` is given, the discovery
    GET is single-flighted under a FIXED key — concurrent /questions bursts
    (and concurrent /permissions, which shares the key) all join ONE
    discovery flight. The shared flight value is the CAPPED RAW BODY
    (≤ ``reserve_bytes`` == the flight's reserve), never the expanded
    session graph: each joiner parses INSIDE its lease window, derives its
    OWN copy of the directory strings, and drops the expanded graph + raw
    body BEFORE releasing the lease — budget ownership covers the caller's
    entire consumption of the shared GET (plan §3.x GET→caller-consumption
    invariant; final review B1 fix, 2026-08-16; mirrors sessions.py's
    parse-inside-lease pattern). A ``registry`` of ``None`` (coalescing
    disabled) or a budget-full bypass (``None`` lease) takes the direct
    caller-private fetch.

    status>=400 / RequestError / bad JSON / non-list / cap-exceeded → 503
    upstream_unavailable (total failure, contract §7 discovery exception —
    do NOT leak upstream status). ``limit`` is passed in by the route
    module (read from its own ``_DISCOVERY_LIMIT`` binding) so tests can
    monkeypatch the caller's binding.
    """
    if registry is not None:
        async def _discovery_factory():
            return await fetch_global_root_sessions_raw(
                upstream_client, request, limit=limit,
            )

        lease = await registry.fetch_or_bypass(
            ("discovery", id(upstream_client), limit),
            _discovery_factory,
            reserve_bytes=reserve_bytes,
        )
        if lease is not None:
            async with lease:
                raw_body, discovery_complete = lease.body
                # leader validated list shape before the flight succeeded;
                # this defensive guard keeps the §7 mapping if it ever fails
                try:
                    sessions_payload = orjson.loads(raw_body)
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise_upstream_unavailable(exc)
                if not isinstance(sessions_payload, list):
                    raise_upstream_unavailable()
                directories = _directories_from_sessions(sessions_payload)
                # drop the expanded graph + shared raw bytes inside the
                # lease — only the caller-owned directory strings survive
                del sessions_payload, raw_body
            # Defense-in-depth (final review rev-1): the registry-level
            # release already severs Lease→body/_entry; dropping the local
            # handle keeps this call free of ANY post-release flight state
            # across the fan-out awaits that follow in the route.
            del lease
        else:  # budget full → direct discovery fetch (caller-private)
            sessions_payload, discovery_complete = (
                await fetch_global_root_sessions(
                    upstream_client, request, limit=limit,
                )
            )
            directories = _directories_from_sessions(sessions_payload)
            del sessions_payload
    else:
        sessions_payload, discovery_complete = await fetch_global_root_sessions(
            upstream_client, request, limit=limit,
        )
        directories = _directories_from_sessions(sessions_payload)
        del sessions_payload
    return directories, discovery_complete


async def fetch_items_for_dir(
    upstream_client: httpx.AsyncClient,
    request: Request,
    directory: str,
    *,
    cap: int,
    item_path: str,
    semaphore,
    flight_key_prefix: str,
    project_entry: Callable[[dict], dict],
    registry=None,
) -> tuple[list[dict], str | None, int]:
    """Fetch one directory's pending items (``/question`` or ``/permission``
    — the shared per-dir skeleton, parameterized by the route).

    Returns ``(items, error_code, body_bytes)``. On success ``error_code``
    is ``None``, ``items`` is a list of upstream entries projected through
    ``project_entry`` and each stamped with the ``directory`` it came from,
    and ``body_bytes`` is the raw body byte count (from ``read_with_cap`` —
    used for aggregate budget accounting). On failure ``items`` is empty,
    ``error_code`` is the contract §7 code string, and ``body_bytes`` is 0
    (does not occupy the accepted aggregate).

    The upstream GET is bounded by the route-injected ``semaphore``
    (cross-request global per-item concurrency cap, e.g.
    ``app.state.questions_semaphore``) and by ``cap`` (per-dir byte ceiling
    via ``read_with_cap``).

    Coalescing LEVEL 2 (traffic plan Batch 1 / A4): when ``registry`` is
    given, the raw GET + cap-read runs through ``fetch_or_bypass`` keyed
    ``(flight_key_prefix, id(upstream), directory)`` — concurrent requests
    aggregating the same directory share ONE upstream GET. Only the RAW
    body is shared: the parse + projection + ``directory`` stamping +
    budget accounting stay per-caller, so the envelope is byte-identical to
    the direct path. Upstream failures raise ``_DirFetchFailure`` inside
    the factory (flight fails — immediate refund, never retained),
    re-raised to every joiner as the same instance and isolated per-caller
    into ``errors[]``. A budget bypass (``None`` lease) falls back to this
    function's direct path.

    Never raises for upstream/network failures — the caller isolates per-dir
    errors into the envelope's ``errors[]``. ``asyncio.CancelledError``
    propagates (it is a ``BaseException`` subclass, so the ``except``
    clauses below never swallow it).
    """
    config = request.app.state.config

    async def _raw() -> tuple[bytes, int]:
        async with semaphore:
            try:
                response = await upstream_client.send(
                    upstream_client.build_request(
                        "GET", item_path,
                        headers=forward_directory_headers(directory),
                    ),
                    stream=True,
                )
            except httpx.RequestError:
                raise _DirFetchFailure(UPSTREAM_UNAVAILABLE)
            try:
                status = response.status_code
                if status >= 400:
                    # 4xx (incl. unlikely 404) → upstream_http_N; 5xx →
                    # upstream_unavailable (per-dir, do NOT raise — isolated
                    # into the envelope errors[]). Bounded drain via
                    # read_with_cap (NOT unbounded aread).
                    await read_with_cap(
                        response, cap,
                        on_read=lambda n: stash_up_in(request, n),
                    )
                    raise _DirFetchFailure(
                        upstream_error_code_for_status(status))
                body, total = await read_with_cap(
                    response, cap,
                    on_read=lambda n: stash_up_in(request, n),
                )
                # per-dir cap exceeded / read failure → error + body_bytes=0.
                # Read bytes are still counted via stash_up_in (traffic), but
                # do NOT occupy the accepted aggregate budget.
                if body is None:
                    raise _DirFetchFailure(UPSTREAM_UNAVAILABLE)
                return body, total
            except httpx.RequestError:
                raise _DirFetchFailure(UPSTREAM_UNAVAILABLE)
            finally:
                await response.aclose()

    body: bytes
    total: int
    if registry is not None:
        try:
            lease = await registry.fetch_or_bypass(
                (flight_key_prefix, id(upstream_client), directory),
                _raw,
                reserve_bytes=config.max_response_bytes,
            )
        except _DirFetchFailure as exc:
            return [], exc.code, 0
        if lease is not None:
            async with lease:
                body, total = lease.body
        else:  # budget full → direct fetch (unchanged behaviour)
            try:
                body, total = await _raw()
            except _DirFetchFailure as exc:
                return [], exc.code, 0
    else:
        try:
            body, total = await _raw()
        except _DirFetchFailure as exc:
            return [], exc.code, 0

    try:
        payload = orjson.loads(body)
    except (orjson.JSONDecodeError, ValueError):
        return [], UPSTREAM_UNAVAILABLE, 0
    if not isinstance(payload, list):
        return [], UPSTREAM_UNAVAILABLE, 0
    items = [
        {**project_entry(entry), "directory": directory}
        for entry in payload if isinstance(entry, dict)
    ]
    return items, None, total


async def collect_with_byte_budget(
    directories: list[str],
    worker: Callable[[str], Awaitable[tuple[list[dict], str | None, int]]],
    *,
    concurrency: int,
    aggregate_cap: int,
    item_cap: int,
) -> tuple[list[dict], list[dict], list[str], bool]:
    """Sliding-window fan-out scheduler for cross-directory aggregation.

    Replaces the former ``asyncio.gather`` approach with a sliding-window
    that:

    * Launches at most ``concurrency`` tasks at any time.
    * Consumes results in **strict index order** (original directory order).
    * Tracks aggregate **raw body bytes** (``body_bytes`` from the worker)
      and **item count** (``item_cap``, fed from ``_MAX_AGGREGATE_ITEMS``).
    * On either cap being exceeded: marks ``truncated=True``, cancels all
      unconsumed tasks (index > current), awaits their cancellation (so the
      worker's ``finally: response.aclose()`` runs), and breaks.
    * Errors (per-dir failure / cap overflow) record only consumed-and-failed
      directories in ``errors[]``. Cancelled / un-started dirs are NOT in
      ``errors[]`` — the presence of ``truncated=True`` + partial
      ``authoritativeDirectories`` communicates the gap.

    ``worker(directory)`` returns ``(items, error_code, body_bytes)`` (see
    :func:`fetch_items_for_dir`); an unexpected exception from the worker is
    isolated as ``upstream_unavailable`` for that directory.

    Returns ``(items, errors, succeeded, truncated)``.
    """
    tasks: dict[int, asyncio.Task] = {}
    next_to_launch = 0             # next directory index to launch
    consume_index = 0              # next index to consume (strict order)
    used_bytes = 0
    used_items = 0
    truncated = False
    items: list[dict] = []
    errors: list[dict] = []
    succeeded: list[str] = []

    def launch(index: int) -> None:
        tasks[index] = asyncio.create_task(worker(directories[index]))

    # Initial window: at most concurrency tasks
    while next_to_launch < len(directories) and next_to_launch < concurrency:
        launch(next_to_launch)
        next_to_launch += 1

    try:
        while consume_index < len(directories):
            task = tasks.get(consume_index)
            if task is None:        # not launched (should not happen after truncation)
                break
            try:
                outcome = await task   # each index consumed/charged exactly once
            except asyncio.CancelledError:
                raise                 # self-cancelled → finally handles cleanup
            except Exception as exc:
                outcome = exc         # regular exceptions → treat as dir failure

            if isinstance(outcome, Exception):
                errors.append({
                    "directory": directories[consume_index],
                    "code": UPSTREAM_UNAVAILABLE,
                })
            else:
                dir_items, error_code, body_bytes = outcome
                if error_code is not None:
                    # per-dir cap exceeded / upstream error: body_bytes=0
                    errors.append({
                        "directory": directories[consume_index],
                        "code": error_code,
                    })
                elif (used_bytes + body_bytes > aggregate_cap
                      or used_items + len(dir_items) > item_cap):
                    # Budget triggered: current dir NOT added to items/succeeded
                    truncated = True
                    for idx, t in tasks.items():
                        if idx > consume_index:
                            t.cancel()
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                    break
                else:
                    items.extend(dir_items)
                    succeeded.append(directories[consume_index])
                    used_bytes += body_bytes
                    used_items += len(dir_items)

            # After consuming index i, launch next (window ≤ concurrency)
            if next_to_launch < len(directories):
                launch(next_to_launch)
                next_to_launch += 1
            consume_index += 1
    finally:
        # Cleanup: cancel + await any unconsumed tasks (ensures aclose runs)
        pending = [t for idx, t in tasks.items() if idx > consume_index]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return items, errors, succeeded, truncated
