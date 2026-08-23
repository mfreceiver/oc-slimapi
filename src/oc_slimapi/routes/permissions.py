from __future__ import annotations

import orjson
from fastapi import APIRouter, Request
from starlette.responses import Response

from ..discovery import _DISCOVERY_LIMIT
from ..gzip_util import compress_if_beneficial
from ._aggregate_fanout import (
    _MAX_AGGREGATE_ITEMS,
    collect_with_byte_budget,
    discover_directories,
    fetch_items_for_dir,
)

router = APIRouter(prefix="/slimapi", tags=["permissions"])

# Shared aggregation skeleton (F-304): discovery input assembly, the
# semaphore-bounded per-dir fetch, and the byte/item-budget sliding-window
# scheduler live in routes/_aggregate_fanout.py. This file keeps the route,
# the /permission whitelist field mapping, and the envelope packer.
# ``_DISCOVERY_LIMIT`` and ``_MAX_AGGREGATE_ITEMS`` are re-bound here (read
# by name inside the route body) so tests can monkeypatch THIS module's
# bindings (mirrors questions.py).

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


def _project_permission_entry(entry: dict) -> dict:
    """Field mapping for /permission items: whitelist-project the upstream
    ``PermissionV1.Request`` entry to the 7 known card fields (unknown
    fields dropped defensively). The shared fetch skeleton stamps
    ``directory`` after this projection (field order: the 7 Request fields,
    then directory)."""
    return {k: entry[k] for k in _PERMISSION_FIELDS if k in entry}


@router.get("/permissions")
async def permissions(request: Request):
    """GET /slimapi/permissions — cross-directory aggregation of pending permission cards.

    B1 (upstream research, opencode v1.18.16 — read before implementing):
    opencode's ``GET /permission`` is **per-Location** (per workdir instance):
    the HTTP handler ``packages/opencode/src/server/routes/instance/httpapi/
    handlers/permission.ts`` ``list`` effect calls
    ``Permission.Service.list()`` (``packages/opencode/src/permission/index.ts``),
    which reads the per-`InstanceState` pending map and returns a **bare
    array** ``PermissionV1.Request[]`` (NOT an ``{items:}`` wrapper — the
    upstream ``GET /question`` similarly returns a bare ``Question.Request[]``,
    see ``groups/question.ts`` success schema). It only returns pending cards for the
    directory routed via ``X-Opencode-Directory`` (workspace-routing
    middleware: ``directory`` query param || ``x-opencode-directory`` header
    || ``process.cwd()``), so pending cards in OTHER workdirs are invisible.
    This endpoint fans out across every discovered workdir and merges the
    results into a single envelope — the cold-start recovery path for
    ocdroid's slim mode.

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
    config = request.app.state.config
    registry = getattr(request.app.state, "raw_fetch_registry", None)
    coalesce = registry is not None and config.coalesce_enabled

    # ------------------------------------------------------------------
    # Step 1: discover directories via GET /experimental/session?roots=true
    # &archived=true — identical to /slimapi/questions, shared skeleton
    # (discover_directories in routes/_aggregate_fanout.py): coalescing
    # LEVEL 1 single-flight under the SAME fixed key as questions.py —
    # concurrent /permissions bursts (and concurrent /questions, which
    # shares the key) all join ONE discovery flight. The session
    # `directory` is the REAL workdir path (covers non-git dirs and
    # git-worktree subdirs that /project's normalized `worktree` drops), and
    # archived=true keeps discovery a superset so archived-only workdirs
    # holding pending cards are not missed.
    #
    # status>=400 / RequestError / bad JSON / non-list / cap-exceeded →
    # 503 upstream_unavailable (total failure, contract §7 discovery
    # exception — do NOT leak upstream status).
    # ------------------------------------------------------------------
    directories, discovery_complete = await discover_directories(
        upstream_client, request,
        limit=_DISCOVERY_LIMIT,
        registry=registry if coalesce else None,
        reserve_bytes=config.max_response_bytes,
    )

    # (Step 2 — directory derivation — happened inside the shared discovery
    # call: `directories` is a caller-owned list of strings; mirrors
    # questions.py's semantics exactly.)
    shadow = getattr(request.app.state, "qp_sweep", None)
    if shadow is not None:
        shadow.record_request_activity(directories)

    # ------------------------------------------------------------------
    # Step 3: sliding-window fan-out with per-dir byte cap, aggregate byte
    # budget, and aggregate item cap (shared scheduler:
    # collect_with_byte_budget in routes/_aggregate_fanout.py — mirrors
    # questions.py). The semaphore (app.state.permissions_semaphore) limits
    # cross-request /permission concurrency globally. Coalescing LEVEL 2
    # (plan A4): each per-dir GET may be shared with concurrent requests
    # through the registry; the aggregation itself stays per-caller. Budgets
    # are the T0 internal knobs (permissions_max_response_bytes /
    # permissions_fanout / permissions_max_aggregate_bytes) — ops-facing,
    # not wire.
    # ------------------------------------------------------------------
    async def _worker(directory: str):
        return await _fetch_permissions_for_dir(
            upstream_client, request, directory,
            cap=config.permissions_max_response_bytes,
            registry=registry if coalesce else None,
        )

    items, errors, succeeded, truncated = await collect_with_byte_budget(
        directories, _worker,
        concurrency=config.permissions_fanout,
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
    upstream_client,
    request: Request,
    directory: str,
    *,
    cap: int,
    registry=None,
) -> tuple[list[dict], str | None, int]:
    """Fetch pending permission cards for a single directory (thin route
    binding over the shared skeleton :func:`fetch_items_for_dir` in
    routes/_aggregate_fanout.py — F-304).

    Route parameters: item path ``/permission``, semaphore
    ``app.state.permissions_semaphore`` (cross-request global /permission
    concurrency cap), coalescing LEVEL 2 flight key
    ``("permission-dir", id(upstream), directory)`` — concurrent requests
    aggregating the same directory share ONE upstream GET, with only the
    RAW body shared (parse + whitelist projection + budget accounting
    per-caller, so the envelope is byte-identical to the direct path).
    Entries pass through :func:`_project_permission_entry` (the
    ``PermissionV1.Request`` whitelist) and get ``directory`` stamped by
    the shared skeleton.
    """
    return await fetch_items_for_dir(
        upstream_client, request, directory,
        cap=cap,
        item_path="/permission",
        semaphore=request.app.state.permissions_semaphore,
        flight_key_prefix="permission-dir",
        project_entry=_project_permission_entry,
        registry=registry,
    )
