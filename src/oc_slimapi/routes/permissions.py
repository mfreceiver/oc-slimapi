from __future__ import annotations

import asyncio

import httpx
import orjson
from fastapi import APIRouter, Request
from starlette.responses import Response

from ..discovery import _DISCOVERY_LIMIT, fetch_global_root_sessions
from ..gzip_util import compress_if_beneficial
from ..traffic import stash_up_in
from ..transform import read_with_cap
from ..upstream import forward_directory_headers
from ..upstream_errors import (
    UPSTREAM_UNAVAILABLE,
    upstream_error_code_for_status,
)

router = APIRouter(prefix="/slimapi", tags=["permissions"])

# P1-28: aggregate item budget (second-layer cap, mirrors questions.py). Each
# per-dir /permission response is capped by per_dir_cap, but items.extend()
# across all dirs can accumulate far beyond a single dir's cap. Once the
# merged item count exceeds this safety limit (or the byte budget), the
# envelope is marked ``truncated: true`` and remaining dirs are cancelled.
_MAX_AGGREGATE_ITEMS = 10_000

# B1 (upstream `GET /permission` field-level shape, opencode v1.18.16):
# `PermissionV1.Request` (packages/schema/src/v1/permission.ts) =
# Struct{ id: ID (string, startsWith "per"), sessionID, permission: String,
# patterns: Array(String), metadata: Record(String, Unknown),
# always: Array(String), tool?: optional Struct{ messageID, callID } }.
# This is the **pending permission card** shape — do NOT confuse it with
# `Permission.Ruleset` (the agent-catalog `permission` field, which the
# agent route strips wholesale; different shape, no UI consumer).
#
# Projection whitelist = the 7 known Request fields (kept as-is when
# present, incl. optional `tool`). Unknown/extra fields in an upstream entry
# are dropped defensively — the client renders exactly these card fields.
_PERMISSION_FIELDS = (
    "id", "sessionID", "permission", "patterns", "metadata", "always", "tool",
)


@router.get("/permissions")
async def permissions(request: Request):
    """GET /slimapi/permissions — cross-directory aggregation of pending permission cards.

    B1 (upstream research, opencode v1.18.16 — read before implementing):
    opencode's ``GET /permission`` is **per-Location** (per workdir instance):
    the HTTP handler ``packages/opencode/src/server/routes/instance/httpapi/
    handlers/permission.ts`` ``list`` effect calls
    ``Permission.Service.list()`` (``packages/opencode/src/permission/index.ts``),
    which reads the per-`InstanceState` pending map and returns a **bare
    array** ``PermissionV1.Request[]`` (NOT an ``{items:}`` wrapper — unlike
    questions' ``{pending:[]}``). It only returns pending cards for the
    directory routed via ``X-Opencode-Directory`` (workspace-routing
    middleware: ``directory`` query param || ``x-opencode-directory`` header
    || ``process.cwd()``), so pending cards in OTHER workdirs are invisible.
    This endpoint fans out across every discovered workdir and merges the
    results into a single envelope — the cold-start recovery path for
    ocdroid's slim mode (v2 removed the permission aggregation endpoint;
    before this, cold start/reconnect could only poll the catch-all
    ``GET /permission``, which only sees ``process.cwd()``'s instance).

    Additive (new endpoint); **no** ``X-Slimapi-Version`` bump (still 2).
    Each permission entry is the upstream ``PermissionV1.Request`` whitelist
    projection plus a ``directory`` field stamped with the directory it was
    fetched from (field order: the 7 Request fields, then directory).

    Discovery source: ``GET /experimental/session?roots=true`` — identical to
    ``/slimapi/questions`` (``fetch_global_root_sessions``): the GLOBAL
    top-level session list (cross all workdir instances; ``roots=true`` ⇒
    ``parentID==null`` only), each session carrying its REAL ``directory``
    field. Covers git repos, non-git workdirs, and git-worktree subdirs
    alike; ``archived=true`` keeps the set a superset (protects archived-only
    workdirs whose instance still holds pending cards).

    Envelope (always 200 on the happy/partial path — mirrors questions,
    oracle §B-1):

    .. code-block:: json

        {
          "items": [ {<PermissionV1.Request whitelist>, "directory": "/some/dir"} ],
          "errors": [ {"directory": "/failed/dir", "code": "upstream_http_500"} ],
          "authoritativeDirectories": null | ["/succeeded/dir", ...],
          "discoveryComplete": true | false
        }

    - ``items``: merged pending-card entries from ALL discovered directories.
    - ``errors``: per-directory failures (isolated — one bad dir never aborts
      the whole request). ``code`` follows contract §7: network/5xx →
      ``upstream_unavailable``; 4xx → ``upstream_http_N``.
    - ``authoritativeDirectories``: ``null`` **only** when ``errors`` is empty
      AND discovery was complete (``discoveryComplete == true``) → full global
      authority, client uses **replace-all** semantics. Otherwise an array of
      the succeeded directory strings → the client must treat
      non-listed/undiscovered dirs as "keep local" (partial-replace, no data
      loss). This is the load-bearing field for the cold-start bug it fixes:
      a failed/undiscovered directory must NOT have its locally-known pending
      cards discarded (else the user can't approve → session deadlock).
    - ``discoveryComplete`` (additive diagnostic): ``true`` unless the
      discovery page filled exactly at ``_DISCOVERY_LIMIT`` (possible
      truncation); effectively always ``true`` in practice.

    Total failure (cannot list top-level sessions to discover workdirs):
    HTTP 503 ``{"code": "upstream_unavailable"}`` (no envelope).
    """
    upstream_client = request.app.state.upstream

    # ------------------------------------------------------------------
    # Step 1: discover directories via GET /experimental/session?roots=true
    # &archived=true — identical to /slimapi/questions (shared helper in
    # ..discovery). See questions.py for the full rationale; summary: the
    # session `directory` is the REAL workdir path (covers non-git dirs and
    # git-worktree subdirs that /project's normalized `worktree` drops), and
    # archived=true keeps discovery a superset so archived-only workdirs
    # holding pending cards are not missed.
    #
    # status>=400 / RequestError / bad JSON / non-list / cap-exceeded →
    # 503 upstream_unavailable (total failure, contract §7 discovery
    # exception — do NOT leak upstream status).
    # ------------------------------------------------------------------
    sessions_payload, discovery_complete = await fetch_global_root_sessions(
        upstream_client, request, limit=_DISCOVERY_LIMIT,
    )

    # ------------------------------------------------------------------
    # Step 2: derive the DISTINCT set of workdir directories (first-seen
    # order) from each session's REAL `directory` field. Skip
    # non-string/empty defensively (mirrors questions.py).
    # ------------------------------------------------------------------
    directories: list[str] = list(dict.fromkeys(
        s["directory"]
        for s in sessions_payload
        if isinstance(s, dict)
           and isinstance(s.get("directory"), str)
           and s["directory"]
    ))

    # ------------------------------------------------------------------
    # Step 3: sliding-window fan-out with per-dir byte cap, aggregate byte
    # budget, and aggregate item cap (mirrors questions.py's
    # _collect_with_byte_budget). The semaphore
    # (app.state.permissions_semaphore) limits cross-request /permission
    # concurrency globally. Budgets are the T0 internal knobs
    # (permissions_max_response_bytes / permissions_fanout /
    # permissions_max_aggregate_bytes) — ops-facing, not wire.
    # ------------------------------------------------------------------
    config = request.app.state.config
    items, errors, succeeded, truncated = await _collect_with_byte_budget(
        upstream_client, request, directories,
        concurrency=config.permissions_fanout,
        per_dir_cap=config.permissions_max_response_bytes,
        aggregate_cap=config.permissions_max_aggregate_bytes,
        item_cap=_MAX_AGGREGATE_ITEMS,
    )

    # ------------------------------------------------------------------
    # Step 4: authoritativeDirectories — null ONLY on full success AND
    # complete discovery (replace-all for the client). On truncation or any
    # per-dir error, emit the succeeded-directory list (partial-replace) so
    # the client never discards pending cards from undiscovered/failed
    # directories (the cold-start protection). Mirrors questions.py exactly.
    # ------------------------------------------------------------------
    authoritative = (
        None
        if (not errors and discovery_complete and not truncated)
        else succeeded
    )
    envelope = {
        "items": items,
        "errors": errors,
        "authoritativeDirectories": authoritative,
        "discoveryComplete": discovery_complete,
    }
    if truncated:
        envelope["truncated"] = True
    # Offload orjson.dumps + gzip to the transform pool's executor so
    # serialising a large aggregation does not block the event loop.
    # No admission acquisition — the aggregation is already memory-bounded
    # by _MAX_AGGREGATE_ITEMS and the per-dir cap (mirrors questions.py).
    pool = request.app.state.transforms
    encoded, extra = await pool.offload(
        _pack_permissions_envelope, envelope,
        accept_encoding=request.headers.get("accept-encoding"),
    )
    return Response(
        encoded, status_code=200, media_type="application/json", headers=extra,
    )


def _pack_permissions_envelope(
    envelope: dict, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entrypoint: serialise the permissions envelope + optional gzip.

    Offloaded to the transform executor so a large aggregation's
    ``orjson.dumps`` + ``gzip`` does not block the event loop. Uses
    :func:`compress_if_beneficial` so small/incompressible envelopes skip
    gzip.
    """
    encoded = orjson.dumps(envelope)
    return compress_if_beneficial(encoded, accept_encoding)


async def _fetch_permissions_for_dir(
    upstream_client: httpx.AsyncClient,
    request: Request,
    directory: str,
    *,
    cap: int,
) -> tuple[list[dict], str | None, int]:
    """Fetch pending permission cards for a single directory.

    Returns ``(items, error_code, body_bytes)``. On success ``error_code`` is
    ``None``, ``items`` is a list of whitelist-projected ``PermissionV1.Request``
    entries each stamped with the ``directory`` they came from, and
    ``body_bytes`` is the raw body byte count (from ``read_with_cap`` — used
    for aggregate budget accounting). On failure ``items`` is empty,
    ``error_code`` is the contract §7 code string, and ``body_bytes`` is 0.

    The upstream GET is bounded by ``request.app.state.permissions_semaphore``
    (cross-request global /permission concurrency cap) and by ``cap`` (per-dir
    byte ceiling via ``read_with_cap``).

    Never raises for upstream/network failures — the caller isolates per-dir
    errors into the envelope's ``errors[]``. ``asyncio.CancelledError``
    propagates (it is a ``BaseException`` subclass, so the ``except`` clauses
    below never swallow it).
    """
    async with request.app.state.permissions_semaphore:
        try:
            response = await upstream_client.send(
                upstream_client.build_request(
                    "GET", "/permission",
                    headers=forward_directory_headers(directory),
                ),
                stream=True,
            )
        except httpx.RequestError:
            return [], UPSTREAM_UNAVAILABLE, 0
        try:
            status = response.status_code
            if status >= 400:
                # 4xx (incl. unlikely 404) → upstream_http_N; 5xx →
                # upstream_unavailable (per-dir, do NOT raise — isolated
                # into the envelope errors[]). Bounded drain via read_with_cap
                # (NOT unbounded aread).
                await read_with_cap(
                    response, cap, on_read=lambda n: stash_up_in(request, n),
                )
                return [], upstream_error_code_for_status(status), 0
            body, total = await read_with_cap(
                response, cap, on_read=lambda n: stash_up_in(request, n),
            )
            # per-dir cap exceeded / read failure → error + body_bytes=0.
            # Read bytes are still counted via stash_up_in (traffic), but
            # do NOT occupy the accepted aggregate budget.
            if body is None:
                return [], UPSTREAM_UNAVAILABLE, 0
            try:
                payload = orjson.loads(body)
            except (orjson.JSONDecodeError, ValueError):
                return [], UPSTREAM_UNAVAILABLE, 0
            if not isinstance(payload, list):
                return [], UPSTREAM_UNAVAILABLE, 0
            items = [
                {**{
                    k: entry[k] for k in _PERMISSION_FIELDS if k in entry
                }, "directory": directory}
                for entry in payload if isinstance(entry, dict)
            ]
            return items, None, total
        except httpx.RequestError:
            return [], UPSTREAM_UNAVAILABLE, 0
        finally:
            await response.aclose()


async def _collect_with_byte_budget(
    upstream_client, request, directories, *,
    concurrency: int, per_dir_cap: int, aggregate_cap: int, item_cap: int,
) -> tuple[list[dict], list[dict], list[str], bool]:
    """Sliding-window fan-out scheduler for cross-directory permission aggregation.

    Mirrors questions.py's scheduler byte-for-byte (shared shape, per-dir
    isolation, strict index-order consumption, budget-triggered truncation):

    * Launches at most ``concurrency`` tasks at any time.
    * Consumes results in **strict index order** (original directory order).
    * Tracks aggregate **raw body bytes** (``body_bytes`` from the worker) and
      **item count** (``_MAX_AGGREGATE_ITEMS``).
    * On either cap being exceeded: marks ``truncated=True``, cancels all
      unconsumed tasks (index > current), awaits their cancellation (so the
      worker's ``finally: response.aclose()`` runs), and breaks.
    * Errors (per-dir failure / cap overflow) record only consumed-and-failed
      directories in ``errors[]``. Cancelled / un-started dirs are NOT in
      ``errors[]`` — the presence of ``truncated=True`` + partial
      ``authoritativeDirectories`` communicates the gap.

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
        tasks[index] = asyncio.create_task(
            _fetch_permissions_for_dir(
                upstream_client, request, directories[index], cap=per_dir_cap,
            )
        )

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
