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

router = APIRouter(prefix="/slimapi", tags=["questions"])

# P1-28: aggregate item budget (second-layer cap, T5-C10). Each per-dir
# /question response is capped by per_dir_cap, but items.extend() across all
# dirs can accumulate far beyond a single dir's cap. Once the merged item
# count exceeds this safety limit (or the byte budget), the envelope is marked
# ``truncated: true`` and remaining dirs are cancelled. The sliding window
# scheduler passes this as ``item_cap``.
_MAX_AGGREGATE_ITEMS = 10_000

# Discovery page size for GET /experimental/session?roots=true — imported
# from ..discovery (single source of truth; see fetch_global_root_sessions).
# Referenced by name (not inlined) so tests can monkeypatch this binding to
# exercise the discovery-truncation path without building 10k sessions.


@router.get("/questions")
async def questions(request: Request):
    """GET /slimapi/questions — cross-directory aggregation of pending questions.

    opencode's upstream ``GET /question`` is **per-Location** (per workdir
    instance): it only returns questions for the directory routed via
    ``X-Opencode-Directory`` and falls back to ``process.cwd()`` with no
    header, so questions pending in OTHER directories are invisible. This
    endpoint fans out across every discovered workdir and merges the results
    into a single envelope, fixing the slim-mode cold-start regression where
    pending questions in a workdir ≠ ``process.cwd()`` could not be seen.

    Additive (re-add); **no** ``X-Slimapi-Version`` bump (still 2). Each
    question entry is the upstream entry verbatim plus a ``directory`` field
    stamped with the directory it was fetched from (field order: id,
    sessionID, questions, tool, then directory).

    Discovery source: ``GET /experimental/session?roots=true`` — opencode's
    GLOBAL top-level session list (cross all workdir instances; ``roots=true``
    ⇒ ``parentID==null`` only). Each session carries its REAL ``directory``
    field (the workdir it was created in). This is preferred over
    ``GET /project`` because ``/project``'s ``worktree`` normalizes non-git
    workdirs to ``"/"`` (synthetic global project, ``project.ts`` resolve()
    non-git branch) — silently dropping pending questions from non-git
    workdirs (custom working dirs, ``/tmp`` scratch dirs) AND git-worktree
    subdirs. The session ``directory`` covers git repos, non-git dirs, and
    git-worktree subdirs alike. (Mirrors qq-ocbot's proven
    ``fetch_questions`` discovery approach.)

    Envelope (always 200 on the happy/partial path):

    .. code-block:: json

        {
          "items": [ {<upstream entry verbatim>, "directory": "/some/dir"} ],
          "errors": [ {"directory": "/failed/dir", "code": "upstream_http_500"} ],
          "authoritativeDirectories": null | ["/succeeded/dir", ...],
          "discoveryComplete": true | false
        }

    - ``items``: merged question entries from ALL discovered directories.
    - ``errors``: per-directory failures (isolated — one bad dir never aborts
      the whole request). ``code`` follows contract §7: network/5xx →
      ``upstream_unavailable``; 4xx → ``upstream_http_N``.
    - ``authoritativeDirectories``: ``null`` **only** when ``errors`` is empty
      AND discovery was complete (``discoveryComplete == true``) → full global
      authority, client uses **replace-all** semantics. Otherwise an array of
      the succeeded directory strings → the client must treat
      non-listed/undiscovered dirs as "keep local" (partial-replace, no data
      loss). On discovery truncation this array protects the client from
      discarding pending questions in undiscovered directories.
    - ``discoveryComplete`` (additive diagnostic): ``true`` unless the
      ``GET /experimental/session?roots=true&archived=true`` discovery page
      filled exactly at ``_DISCOVERY_LIMIT`` (possible truncation). ``roots=true``
      returns only top-level sessions (count ≈ distinct workdirs), so in
      practice it is effectively always ``true``. Client may ignore if
      absent-aware. Discovery includes archived sessions (``archived=true``)
      so a workdir whose top-level sessions are all archived but whose
      instance still holds pending questions is NOT dropped (``/question`` is
      an in-memory store independent of archive state).

    Total failure (cannot list top-level sessions to discover workdirs):
    HTTP 503 ``{"code": "upstream_unavailable"}`` (no envelope).
    """
    upstream_client = request.app.state.upstream

    # ------------------------------------------------------------------
    # Step 1: discover directories via GET /experimental/session?roots=true
    # &archived=true — the GLOBAL top-level session list (cross all workdir
    # instances, roots=true ⇒ parentID==null only). Each session carries its
    # REAL `directory` field (the workdir it was created in). This replaces
    # the former GET /project discovery: /project's `worktree` normalizes
    # non-git workdirs to "/" (synthetic global project), silently dropping
    # their pending questions — non-git working dirs (e.g. custom dirs,
    # /tmp scratch) and git-worktree subdirs were invisible. The session
    # `directory` covers all of them. (Same approach as qq-ocbot's
    # fetch_questions.) archived=true makes discovery a SUPERSET (includes
    # archived sessions) so a workdir whose top-level sessions are all
    # archived but whose instance still holds pending questions is not
    # dropped — /question is an in-memory store independent of archive
    # state, so at worst we fan out to a dead instance (isolated errors[]).
    #
    # status>=400 / RequestError / bad JSON / non-list / cap-exceeded →
    # 503 upstream_unavailable (total failure, contract §7 discovery
    # exception — do NOT leak upstream status). ``limit`` is read by name
    # from this module so tests can monkeypatch the binding.
    # ------------------------------------------------------------------
    sessions_payload, discovery_complete = await fetch_global_root_sessions(
        upstream_client, request, limit=_DISCOVERY_LIMIT,
    )

    # ------------------------------------------------------------------
    # Step 2: derive the DISTINCT set of workdir directories (first-seen
    # order) from each session's REAL `directory` field. Unlike /project's
    # `worktree` (which normalizes non-git workdirs to "/" and must be
    # skipped), the session `directory` is always a real path — no
    # synthetic-global skip is needed. Skip non-string/empty defensively.
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
    # budget, and aggregate item cap. Replaces the former asyncio.gather
    # approach. The semaphore (app.state.questions_semaphore) limits cross-
    # request /question concurrency globally.
    # ------------------------------------------------------------------
    config = request.app.state.config
    items, errors, succeeded, truncated = await _collect_with_byte_budget(
        upstream_client, request, directories,
        concurrency=config.questions_fanout_concurrency,
        per_dir_cap=config.questions_max_response_bytes,
        aggregate_cap=config.questions_max_aggregate_bytes,
        item_cap=_MAX_AGGREGATE_ITEMS,
    )

    # ------------------------------------------------------------------
    # Step 4: authoritativeDirectories — null ONLY on full success AND
    # complete discovery (replace-all for the client). On truncation or any
    # per-dir error, emit the succeeded-directory list (partial-replace) so
    # the client never discards pending questions from undiscovered/failed
    # directories. Aggregate truncation (P1-28) also forces partial-replace.
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
    # P1-28: offload orjson.dumps + gzip to the transform pool's executor so
    # serialising a large aggregation does not block the event loop (SSE
    # heartbeats, other light async work). No admission acquisition — the
    # aggregation is already memory-bounded by _MAX_AGGREGATE_ITEMS and the
    # per-dir cap; the offload is purely about CPU-bound serialisation.
    pool = request.app.state.transforms
    encoded, extra = await pool.offload(
        _pack_questions_envelope, envelope,
        accept_encoding=request.headers.get("accept-encoding"),
    )
    return Response(
        encoded, status_code=200, media_type="application/json", headers=extra,
    )


def _pack_questions_envelope(
    envelope: dict, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entrypoint (P1-28): serialise the questions envelope + optional gzip.

    Offloaded to the transform executor so a large aggregation's
    ``orjson.dumps`` + ``gzip`` does not block the event loop while SSE
    heartbeats are pending. Uses :func:`compress_if_beneficial` (P1-31) so
    small/incompressible envelopes skip gzip.
    """
    encoded = orjson.dumps(envelope)
    return compress_if_beneficial(encoded, accept_encoding)


async def _fetch_questions_for_dir(
    upstream_client: httpx.AsyncClient,
    request: Request,
    directory: str,
    *,
    cap: int,
) -> tuple[list[dict], str | None, int]:
    """Fetch pending questions for a single directory.

    Returns ``(items, error_code, body_bytes)``. On success ``error_code`` is
    ``None``, ``items`` is a list of upstream question entries each stamped
    with the ``directory`` they came from, and ``body_bytes`` is the raw body
    byte count (from ``read_with_cap`` — used for aggregate budget accounting).
    On failure ``items`` is empty, ``error_code`` is the contract §7 code
    string, and ``body_bytes`` is 0 (does not occupy the accepted aggregate).

    The upstream GET is bounded by ``request.app.state.questions_semaphore``
    (cross-request global /question concurrency cap) and by ``cap`` (per-dir
    byte ceiling via ``read_with_cap``).

    Never raises for upstream/network failures — the caller isolates per-dir
    errors into the envelope's ``errors[]``. ``asyncio.CancelledError``
    propagates (it is a ``BaseException`` subclass, so the ``except`` clauses
    below never swallow it).
    """
    async with request.app.state.questions_semaphore:
        try:
            response = await upstream_client.send(
                upstream_client.build_request(
                    "GET", "/question",
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
                {**entry, "directory": directory}
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
    """Sliding-window fan-out scheduler for cross-directory question aggregation.

    Replaces the former ``asyncio.gather`` approach with a sliding-window that:
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
            _fetch_questions_for_dir(
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
