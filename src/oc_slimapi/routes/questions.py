from __future__ import annotations

import asyncio

import httpx
import orjson
from fastapi import APIRouter, Request
from starlette.responses import Response

from ..gzip_util import compress_if_beneficial
from ..traffic import stash_up_in
from ..transform import read_with_cap
from ..upstream import forward_directory_headers
from ..upstream_errors import (
    UPSTREAM_UNAVAILABLE,
    raise_upstream_unavailable,
    upstream_error_code_for_status,
)

router = APIRouter(prefix="/slimapi", tags=["questions"])

# Bounds concurrent per-dir /question fan-out within a single
# /slimapi/questions request so a burst of many session-dirs cannot queue an
# unbounded number of in-flight requests against the shared upstream client
# (httpx max_connections=32). Acquired inside _fetch_questions_for_dir around
# the upstream GET. Module-level (single uvicorn worker / single event loop in
# production; modern asyncio.Semaphore does not bind a loop at construction).
_FANOUT_CONCURRENCY = 16
_fanout_sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)

# P1-28: aggregate item budget. Each per-dir /question response is capped by
# max_response_bytes, but items.extend() across all dirs can accumulate far
# beyond a single dir's cap (10k dirs × cap). Once the merged item count
# exceeds this safety limit, further dirs are skipped and the envelope is
# marked ``truncated: true`` (additive diagnostic — client degrades to
# partial-replace, same as discovery truncation).
_MAX_AGGREGATE_ITEMS = 10_000

# Page size for the GET /experimental/session?roots=true discovery call.
# `roots=true` returns only top-level sessions (parentID==null), so the count
# ≈ number of distinct workdirs — orders of magnitude smaller than the full
# session table. 10000 is therefore effectively never hit in practice; it is
# a safety cap so a pathological upstream cannot exhaust memory. If the page
# fills exactly, discovery is marked incomplete (see discovery_complete) so
# the client degrades authoritativeDirectories instead of replace-all.
_DISCOVERY_LIMIT = 10_000


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
    # ------------------------------------------------------------------
    config = request.app.state.config
    try:
        response = await upstream_client.send(
            upstream_client.build_request(
                "GET", "/experimental/session",
                params={
                    "roots": "true",
                    "archived": "true",
                    "limit": _DISCOVERY_LIMIT,
                },
            ),
            stream=True,
        )
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    try:
        try:
            if response.status_code >= 400:
                # network/5xx/4xx on discovery = total failure (contract §7: 503
                # upstream_unavailable, NOT upstream_http_N). Discovery is an
                # internal derived call; leaking the upstream status would mislead
                # the client about which directory failed.
                err_body = await response.aread()
                stash_up_in(request, len(err_body))
                raise_upstream_unavailable()
            body, _ = await read_with_cap(
                response, config.max_response_bytes,
                on_read=lambda n: stash_up_in(request, n),
            )
            if body is None:
                raise_upstream_unavailable()
            try:
                sessions_payload = orjson.loads(body)
            except (orjson.JSONDecodeError, ValueError) as exc:
                raise_upstream_unavailable(exc)
            if not isinstance(sessions_payload, list):
                raise_upstream_unavailable()
        except httpx.RequestError as exc:
            # Mid-stream read failure (aread or read_with_cap).
            raise_upstream_unavailable(exc)
    finally:
        await response.aclose()

    # /experimental/session honors `limit`; detect truncation so the client
    # can degrade authoritativeDirectories (avoid replace-all dropping
    # pending questions in undiscovered dirs). roots=true returns only
    # top-level sessions (count ≈ distinct workdirs), so _DISCOVERY_LIMIT is
    # effectively never hit in practice — but guard it for correctness.
    discovery_complete = len(sessions_payload) < _DISCOVERY_LIMIT

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
    truncated = False
    for directory, result in zip(directories, results):
        # _fetch_questions_for_dir catches httpx.RequestError internally, but
        # guard against any unexpected exception so one bad dir cannot abort
        # the whole response. CancelledError is re-raised (cancellation must
        # propagate, not be swallowed as a partial failure).
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            errors.append({"directory": directory, "code": UPSTREAM_UNAVAILABLE})
            continue
        dir_items, error_code = result
        if error_code is not None:
            errors.append({"directory": directory, "code": error_code})
            continue
        # P1-28: aggregate item budget — stop extending once the merged list
        # exceeds the safety cap. Remaining dirs are not collected into
        # succeeded[] so authoritativeDirectories degrades to partial-replace.
        if len(items) + len(dir_items) > _MAX_AGGREGATE_ITEMS:
            truncated = True
            break
        items.extend(dir_items)
        succeeded.append(directory)

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
        config = request.app.state.config
        try:
            response = await upstream_client.send(
                upstream_client.build_request(
                    "GET", "/question",
                    headers=forward_directory_headers(directory),
                ),
                stream=True,
            )
        except httpx.RequestError:
            return [], UPSTREAM_UNAVAILABLE
        try:
            try:
                status = response.status_code
                if status >= 400:
                    # 4xx (incl. unlikely 404) → upstream_http_N; 5xx →
                    # upstream_unavailable (per-dir, do NOT raise — isolated
                    # into the envelope errors[]). Drain the body for reuse.
                    err_body = await response.aread()
                    stash_up_in(request, len(err_body))
                    return [], upstream_error_code_for_status(status)
                body, _ = await read_with_cap(
                    response, config.max_response_bytes,
                    on_read=lambda n: stash_up_in(request, n),
                )
                if body is None:
                    return [], UPSTREAM_UNAVAILABLE
                try:
                    payload = orjson.loads(body)
                except (orjson.JSONDecodeError, ValueError):
                    return [], UPSTREAM_UNAVAILABLE
                if not isinstance(payload, list):
                    # Non-list body for this single dir → treat the dir as failed.
                    return [], UPSTREAM_UNAVAILABLE
                items: list[dict] = []
                for entry in payload:
                    if isinstance(entry, dict):
                        # Stamp directory last (after id, sessionID, questions, tool)
                        # so the upstream field order is preserved verbatim.
                        entry["directory"] = directory
                        items.append(entry)
                return items, None
            except httpx.RequestError:
                return [], UPSTREAM_UNAVAILABLE
        finally:
            await response.aclose()
