from __future__ import annotations

import httpx
import orjson
from fastapi import APIRouter, Query, Request

from ..directory import validate_directory
from ..errors import CodedHTTPException
from ..gzip_util import json_response
from ..skeleton import skeleton_session
from ..traffic import stash_up_in
from ..transform import TransformBusy, read_with_cap
from ..upstream import forward_directory_headers
from ..upstream_errors import raise_upstream_status, raise_upstream_unavailable
from ._catalog_common import busy_response, read_upstream_response

router = APIRouter(prefix="/slimapi", tags=["sessions"])


@router.get("/sessions")
async def sessions(
    request: Request,
    directory: str | None = None,
    roots: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    start: int | None = Query(None, ge=0),
    search: str | None = None,
):
    if directory is not None:
        # slimapi no longer gates directories — normalize and forward; the
        # upstream opencode decides whether it can serve the directory.
        directory = validate_directory(directory)
    params = {"limit": limit, "roots": str(roots).lower()}
    if directory is not None:
        params["directory"] = directory
    if start is not None:
        params["start"] = start
    if search is not None:
        params["search"] = search
    # Admission BEFORE the upstream GET (mirrors messages.py): bound
    # concurrent sessions-list requests (upstream body buffering + parse +
    # projection) by max_transforms so a burst cannot monopolise memory /
    # event-loop CPU. The slot is held across fetch→parse→project.
    config = request.app.state.config
    try:
        async with request.app.state.transforms as pool:
            # Stream + cap-read so an oversized upstream /session body cannot
            # spike sidecar RSS (mirrors messages.py:275-303). Cap metric =
            # decompressed logical bytes.
            try:
                response = await request.app.state.upstream.send(
                    request.app.state.upstream.build_request(
                        "GET", "/session",
                        params=params, headers=forward_directory_headers(directory),
                    ),
                    stream=True,
                )
            except httpx.RequestError as exc:
                raise_upstream_unavailable(exc)
            try:
                # Shared drain-or-cap-read skeleton (status mapping +
                # read_with_cap + mid-stream RequestError → 503); no sid
                # here (list endpoint), so a 404 reports as
                # upstream_http_404 like any other 4xx.
                body = await read_upstream_response(
                    request, response,
                    cap=config.max_response_bytes,
                    read_with_cap=read_with_cap,
                )
                if body is None:
                    raise CodedHTTPException(
                        413, code="response_too_large",
                        limit=config.max_response_bytes,
                    )
                try:
                    payload = orjson.loads(body)
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise_upstream_unavailable(exc)
                if not isinstance(payload, list):
                    # v6 §1.1: dict / string / null etc. would have been silently
                    # iterated by ``for item in payload`` and yielded a 200 with
                    # ``X-Complete: true`` (the empty skeleton list). Treat non-list
                    # bodies as a malformed upstream — same 503 as the sibling
                    # ``response.json()`` failure path. No completeness headers on
                    # this branch (the contract is: 200 only).
                    raise_upstream_unavailable()
                if payload and not all(isinstance(s, dict) for s in payload):
                    # Scalar-element list (e.g. [1, null, "x"]) would make
                    # skeleton_session() call .get() on non-dict → AttributeError.
                    # Mirrors messages list element-level guard (Task 1).
                    raise_upstream_unavailable()
                # Offload skeleton projection to the worker so the event loop is
                # not blocked by deep copy of potentially many sessions.
                sessions = await pool.offload(
                    _project_sessions,  # helper below
                    payload,
                )
            finally:
                await response.aclose()
    except TransformBusy as exc:
        return busy_response(request.headers.get("accept-encoding"))
    # v6 §1.1: completeness signal header (200-only — 503 / 502 paths above
    # do not emit it, by design).
    complete = len(sessions) < limit
    return json_response(
        sessions,
        headers={
            "X-Complete": "true" if complete else "false",
        },
        accept_encoding=request.headers.get("accept-encoding"),
    )


def _project_sessions(payload: list[dict]) -> list[dict]:
    """Worker-thread entry: project each session dict (no side effects)."""
    return [skeleton_session(item) for item in payload]


@router.get("/sessions/status")
async def sessions_status(request: Request, directory: str | None = None):
    """GET /slimapi/sessions/status?directory=<optional>.

    Additive re-add (lite-v2 originally deleted this; brought back as a
    read-only projection). Passthrough of upstream opencode
    ``GET /session/status`` (returns ``Record<SessionID, {type:"busy"|
    "idle"|"retry"}>``) with a sidecar merge of the turn-token fence
    fields (``turnIncarnation``/``turn``) per sid from
    :class:`TurnRegistry`. No caching, no new state — same in-memory
    sources the digest SSE already stamps from (contract §3.y).

    ``directory`` is OPTIONAL (additive). Upstream ``GET /session/status``
    ignores ``directory`` entirely — its handler takes no args and
    ``statusSvc.list()`` returns the full in-memory ``Map<SessionID, Info>``
    regardless (the param exists only for ``WorkspaceRoutingMiddleware``
    routing). So this endpoint always returns the GLOBAL status map no
    matter what directory is (or isn't) supplied; callers SHOULD omit
    ``directory`` and call once for the whole map (see
    ``docs/ocmar/specs/2026-08-05-s4-batch-status-research.md``). When
    supplied, it is validated + forwarded (as ``?directory=`` query and
    ``X-Opencode-Directory`` header) for compatibility — upstream treats
    it as a no-op either way.
    """
    if directory is not None:
        directory = validate_directory(directory)
    params: dict[str, str] = {}
    if directory is not None:
        params["directory"] = directory
    try:
        response = await request.app.state.upstream.get(
            "/session/status",
            params=params,
            headers=forward_directory_headers(directory),
        )
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    stash_up_in(request, len(response.content))
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise_upstream_status(exc)
    try:
        payload = response.json()
    except Exception as exc:
        raise_upstream_unavailable(exc)
    if not isinstance(payload, dict):
        # Upstream contract is Record<SessionID, Info> — a non-dict body is
        # malformed. Mirrors the sessions-list non-array guard (503).
        raise_upstream_unavailable()
    # Read-only turn merge (contract §3.y.1: paired turnIncarnation/turn at
    # the flat top level of each entry). Unobserved sid → (inc, 0). The
    # registry is lifespan-wired in production; when absent both fields are
    # omitted (paired missing → ocdroid Tier-2 degrade). Entries whose value
    # is not a dict (upstream schema violation) are passed through unchanged.
    turn_registry = getattr(request.app.state, "turn_registry", None)
    if turn_registry is not None:
        for sid, info in payload.items():
            if isinstance(info, dict):
                inc, turn = turn_registry.snapshot(sid)
                info["turnIncarnation"] = inc
                info["turn"] = turn
    return json_response(
        payload,
        accept_encoding=request.headers.get("accept-encoding"),
    )



