from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Request

from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..traffic import stash_up_in
from ..upstream import forward_directory_headers

router = APIRouter(prefix="/slimapi", tags=["questions"])

# High limit for the directory-discovery /session call. The upstream legacy
# /session listing is the only global source of "which directories have
# sessions"; we pull a generous page so the distinct-directory set is as
# complete as possible in one round-trip. Upstream legacy /session exposes NO
# forward pagination cursor (`start` is a `time_updated >=` watermark filter,
# not an offset/cursor), so raising the limit is the only lever — do NOT
# attempt fake pagination loops. 10_000 is far above any realistic single-user
# session count; if a response ever reaches this length it is treated as
# possibly-truncated (see discoveryComplete below).
_DISCOVERY_LIMIT = 10_000

# Bounds concurrent per-dir /question fan-out within a single
# /slimapi/questions request so a burst of many session-dirs cannot queue an
# unbounded number of in-flight requests against the shared upstream client
# (httpx max_connections=32). Acquired inside _fetch_questions_for_dir around
# the upstream GET. Module-level (single uvicorn worker / single event loop in
# production; modern asyncio.Semaphore does not bind a loop at construction).
_FANOUT_CONCURRENCY = 16
_fanout_sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)


@router.get("/questions")
async def questions(request: Request):
    """GET /slimapi/questions — cross-directory aggregation of pending questions.

    opencode's upstream ``GET /question`` is **per-Location** (per workdir
    instance): it only returns questions for the directory routed via
    ``X-Opencode-Directory`` and falls back to ``process.cwd()`` with no
    header, so questions pending in OTHER directories are invisible. This
    endpoint fans out across every directory that has at least one session
    and merges the results into a single envelope, fixing the slim-mode
    cold-start regression where pending questions in a workdir ≠
    ``process.cwd()`` could not be seen.

    Additive (re-add); **no** ``X-Slimapi-Version`` bump (still 2). Each
    question entry is the upstream entry verbatim plus a ``directory`` field
    stamped with the directory it was fetched from (field order: id,
    sessionID, questions, tool, then directory).

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
    - ``discoveryComplete`` (additive diagnostic): ``true`` iff the discovery
      ``/session`` response held fewer than ``_DISCOVERY_LIMIT`` rows (i.e.
      not truncated). Client may ignore if absent-aware.

    Total failure (cannot list sessions to discover directories): HTTP 503
    ``{"code": "upstream_unavailable"}`` (no envelope).
    """
    upstream_client = request.app.state.upstream

    # ------------------------------------------------------------------
    # Step 1: discover directories via the global /session list (no
    # directory header — sessions are global storage). Any failure here is a
    # TOTAL failure (503): without the directory set the sidecar cannot fan
    # out, so the whole endpoint is unavailable to the client.
    # ------------------------------------------------------------------
    try:
        response = await upstream_client.get(
            "/session", params={"limit": _DISCOVERY_LIMIT},
        )
    except httpx.RequestError as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    # Traffic accounting: discovery upstream body.
    stash_up_in(request, len(response.content))
    if response.status_code >= 400:
        # network/5xx/4xx on discovery = total failure (contract §7: 503
        # upstream_unavailable, NOT upstream_http_N). Discovery is an
        # internal derived call; leaking the upstream status would mislead
        # the client about which directory failed.
        raise CodedHTTPException(503, code="upstream_unavailable")
    try:
        sessions_payload = response.json()
    except Exception as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc
    if not isinstance(sessions_payload, list):
        raise CodedHTTPException(503, code="upstream_unavailable")

    # Discovery completeness: upstream legacy /session has no forward cursor,
    # so a full page (len == limit) means the directory set is *possibly*
    # truncated. Downgraded to "partial" authority below to avoid the client
    # discarding pending questions from undiscovered directories.
    discovery_complete = len(sessions_payload) < _DISCOVERY_LIMIT

    # ------------------------------------------------------------------
    # Step 2: derive the DISTINCT set of directory values (first-seen
    # order). Sessions without a string `directory` field are ignored.
    # ------------------------------------------------------------------
    directories: list[str] = list(dict.fromkeys(
        s["directory"]
        for s in sessions_payload
        if isinstance(s, dict) and isinstance(s.get("directory"), str)
    ))

    # ------------------------------------------------------------------
    # Step 3: fan out concurrently over each directory (bounded by
    # _fanout_sem). Per-dir errors are isolated into errors[] (one bad dir
    # never aborts the request). gather([]) == [] handles the zero-directory
    # case naturally.
    # ------------------------------------------------------------------
    results = await asyncio.gather(
        *(
            _fetch_questions_for_dir(upstream_client, request, d)
            for d in directories
        ),
        return_exceptions=True,
    )

    items: list[dict] = []
    errors: list[dict] = []
    succeeded: list[str] = []
    for directory, result in zip(directories, results):
        # _fetch_questions_for_dir catches httpx.RequestError internally, but
        # guard against any unexpected exception so one bad dir cannot abort
        # the whole response. CancelledError is re-raised (cancellation must
        # propagate, not be swallowed as a partial failure).
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            errors.append({"directory": directory, "code": "upstream_unavailable"})
            continue
        dir_items, error_code = result
        if error_code is not None:
            errors.append({"directory": directory, "code": error_code})
            continue
        items.extend(dir_items)
        succeeded.append(directory)

    # ------------------------------------------------------------------
    # Step 4: authoritativeDirectories — null ONLY on full success AND
    # complete discovery (replace-all for the client). On truncation or any
    # per-dir error, emit the succeeded-directory list (partial-replace) so
    # the client never discards pending questions from undiscovered/failed
    # directories.
    # ------------------------------------------------------------------
    authoritative = None if (not errors and discovery_complete) else succeeded
    envelope = {
        "items": items,
        "errors": errors,
        "authoritativeDirectories": authoritative,
        "discoveryComplete": discovery_complete,
    }
    return json_response(
        envelope,
        accept_encoding=request.headers.get("accept-encoding"),
    )


async def _fetch_questions_for_dir(
    upstream_client: httpx.AsyncClient,
    request: Request,
    directory: str,
) -> tuple[list[dict], str | None]:
    """Fetch pending questions for a single directory.

    Returns ``(items, error_code)``. On success ``error_code`` is ``None`` and
    ``items`` is a list of upstream question entries each stamped with the
    ``directory`` they came from (only dict entries are stamped; non-dict
    entries are skipped defensively — lenient, do not over-engineer). On
    failure ``items`` is empty and ``error_code`` is the contract §7 code
    string (``upstream_unavailable`` for network/5xx/non-list,
    ``upstream_http_<N>`` for 4xx).

    The upstream GET is bounded by the module-level ``_fanout_sem`` so a
    single /slimapi/questions request cannot queue an unbounded number of
    in-flight calls against the shared upstream client.

    Never raises for upstream/network failures — the caller isolates per-dir
    errors into the envelope's ``errors[]``. ``asyncio.CancelledError``
    propagates (it is a ``BaseException`` subclass, so the ``except`` clauses
    below never swallow it).
    """
    async with _fanout_sem:
        try:
            response = await upstream_client.get(
                "/question", headers=forward_directory_headers(directory),
            )
        except httpx.RequestError:
            return [], "upstream_unavailable"
        # Traffic accounting: per-dir upstream body.
        stash_up_in(request, len(response.content))
        status = response.status_code
        if status >= 500:
            return [], "upstream_unavailable"
        if status >= 400:
            # 4xx (incl. unlikely 404) → upstream_http_N (per-dir, do NOT raise).
            return [], f"upstream_http_{status}"
        try:
            payload = response.json()
        except Exception:
            return [], "upstream_unavailable"
        if not isinstance(payload, list):
            # Non-list body for this single dir → treat the dir as failed.
            return [], "upstream_unavailable"
        items: list[dict] = []
        for entry in payload:
            if isinstance(entry, dict):
                # Stamp directory last (after id, sessionID, questions, tool)
                # so the upstream field order is preserved verbatim.
                entry["directory"] = directory
                items.append(entry)
        return items, None
