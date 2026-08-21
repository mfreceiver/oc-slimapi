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

router = APIRouter(prefix="/slimapi", tags=["questions"])

# Shared aggregation skeleton (F-304): discovery input assembly, the
# semaphore-bounded per-dir fetch, and the byte/item-budget sliding-window
# scheduler live in routes/_aggregate_fanout.py. This file keeps the route,
# the /question field mapping, and the envelope packer. ``_DISCOVERY_LIMIT``
# and ``_MAX_AGGREGATE_ITEMS`` are re-bound here (read by name inside the
# route body) so tests can keep monkeypatching THIS module's bindings.


def _project_question_entry(entry: dict) -> dict:
    """Field mapping for /question items: the upstream entry is passed
    through verbatim — the shared fetch skeleton stamps ``directory`` after
    this projection (field order: id, sessionID, questions, tool, then
    directory)."""
    return entry


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
    config = request.app.state.config
    registry = getattr(request.app.state, "raw_fetch_registry", None)
    coalesce = registry is not None and config.coalesce_enabled

    # ------------------------------------------------------------------
    # Step 1: discover directories via GET /experimental/session?roots=true
    # [&archived=true] — shared skeleton (discover_directories in
    # routes/_aggregate_fanout.py): coalescing LEVEL 1 single-flight under
    # the FIXED ("discovery", id(upstream), limit) key (concurrent
    # /questions AND /permissions bursts join ONE flight), raw-bytes lease
    # value, parse-inside-lease, graph dropped before lease release.
    # ``limit`` is read by name from this module so tests can monkeypatch
    # the binding.
    # ------------------------------------------------------------------
    directories, discovery_complete = await discover_directories(
        upstream_client, request,
        limit=_DISCOVERY_LIMIT,
        registry=registry if coalesce else None,
        reserve_bytes=config.max_response_bytes,
    )

    # (Step 2 — directory derivation — happened inside the shared discovery
    # call: `directories` is a caller-owned list of strings.)
    shadow = getattr(request.app.state, "qp_sweep", None)
    if shadow is not None:
        shadow.record_request_activity(directories)

    # ------------------------------------------------------------------
    # Step 3: sliding-window fan-out with per-dir byte cap, aggregate byte
    # budget, and aggregate item cap (shared scheduler: collect_with_byte_
    # budget in routes/_aggregate_fanout.py). The semaphore
    # (app.state.questions_semaphore) limits cross-request /question
    # concurrency globally. Coalescing LEVEL 2 (plan A4): each per-dir GET
    # may be shared with concurrent requests through the registry (see
    # _fetch_questions_for_dir); the aggregation itself stays per-caller.
    # ------------------------------------------------------------------
    async def _worker(directory: str):
        return await _fetch_questions_for_dir(
            upstream_client, request, directory,
            cap=config.questions_max_response_bytes,
            registry=registry if coalesce else None,
        )

    items, errors, succeeded, truncated = await collect_with_byte_budget(
        directories, _worker,
        concurrency=config.questions_fanout_concurrency,
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
    upstream_client,
    request: Request,
    directory: str,
    *,
    cap: int,
    registry=None,
) -> tuple[list[dict], str | None, int]:
    """Fetch pending questions for a single directory (thin route binding
    over the shared skeleton :func:`fetch_items_for_dir` in
    routes/_aggregate_fanout.py — F-304).

    Route parameters: item path ``/question``, semaphore
    ``app.state.questions_semaphore`` (cross-request global /question
    concurrency cap), coalescing LEVEL 2 flight key
    ``("question-dir", id(upstream), directory)`` — concurrent requests
    aggregating the same directory share ONE upstream GET, with only the
    RAW body shared (parse + projection + budget accounting per-caller, so
    the envelope is byte-identical to the direct path). Entries pass
    through :func:`_project_question_entry` (verbatim) and get
    ``directory`` stamped by the shared skeleton.
    """
    return await fetch_items_for_dir(
        upstream_client, request, directory,
        cap=cap,
        item_path="/question",
        semaphore=request.app.state.questions_semaphore,
        flight_key_prefix="question-dir",
        project_entry=_project_question_entry,
        registry=registry,
    )
