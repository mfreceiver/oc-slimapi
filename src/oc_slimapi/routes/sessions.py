from __future__ import annotations

import httpx
import orjson
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from ..directory import validate_directory
from ..envelope import sessions_envelope_payload
from ..errors import CodedHTTPException
from .. import etag as etag_mod
from ..gzip_util import accepts_gzip, json_response
from ..selector import resolve_route_directory, wire_view_from_scope
from ..skeleton import skeleton_session
from ..traffic import stash_up_in
from ..transform import TransformBusy, read_with_cap
from ..upstream import forward_directory_headers
from ..upstream_errors import raise_upstream_status, raise_upstream_unavailable
from ._catalog_common import busy_response, read_upstream_response

router = APIRouter(prefix="/slimapi", tags=["sessions"])


# ---------------------------------------------------------------------------
# Upstream-fetch coalescing (traffic plan Batch 1 / A3, §3.x join-first).
#
# Both endpoints share ONLY the upstream GET (+ cap-read / status mapping):
# the skeleton projection, ``X-Complete`` computation and the TurnRegistry
# merge stay per-caller. A full registry budget (``fetch_or_bypass`` →
# ``None``) falls back to the unchanged admission-first direct path.
# ---------------------------------------------------------------------------

def _canonical_sessions_query(
    limit: int, roots: bool, start: int | None, search: str | None,
) -> str:
    """Deterministic sorted query for the list key (directory is a separate
    key component — it is both a query param and a routing header)."""
    parts: dict[str, str] = {"limit": str(limit), "roots": str(roots).lower()}
    if start is not None:
        parts["start"] = str(start)
    if search is not None:
        parts["search"] = search
    return "&".join(f"{name}={parts[name]}" for name in sorted(parts))


async def _fetch_sessions_raw(
    request: Request, params: dict, directory: str | None, *, cap: int,
) -> bytes | None:
    """Shared factory body: ONE upstream ``GET /session`` + cap-read."""
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
        return await read_upstream_response(
            request, response,
            cap=cap,
            read_with_cap=read_with_cap,
        )
    finally:
        await response.aclose()


def _finalize_sessions_response(
    request: Request, sessions: list[dict], limit: int,
    accept_encoding: str | None,
) -> Response:
    """Shared response tail for BOTH sessions-list paths.

    Batch 2 / B1: per-caller conditional-request evaluation AFTER the
    pipeline (shared or direct GET + projection) has fully run. The
    canonical ETag input is the identity serialization of the projected
    list — note this ``orjson.dumps`` runs on the event loop, but so does
    the one inside ``json_response`` (pre-existing shape); the duplicated
    pass is the cost of keeping ``json_response`` untouched.

    ``etag_enabled=false`` (rep ``None``) → the exact pre-ETag response,
    byte-identical.

    v3 (§4.2, Batch B): the payload is the envelope
    ``{"items":[...],"complete":<bool>}`` — the envelope bytes are the
    canonical ETag input (§6.3), the ``X-Complete`` header is NOT emitted
    on either 200 or 304 (the client reads ``complete`` from the cached
    envelope, §6.4), and the validator carries the wire-view marker so
    v2/v3 tags never cross-match (§6.1).
    """
    complete = len(sessions) < limit
    view = wire_view_from_scope(request.scope)
    rep = etag_mod.response_rep_version(
        request.app.state.config, wire_view=view)
    if view == 3:
        payload: list[dict] | dict = sessions_envelope_payload(sessions, complete)
        v3_headers: dict[str, str] = {}
        if rep is None:
            response = json_response(
                payload, headers=v3_headers, accept_encoding=accept_encoding,
            )
            # §6.2 (gate C3): directory dimension unconditional.
            response.headers["Vary"] = etag_mod.merged_vary("Accept-Encoding")
            return response
        identity = orjson.dumps(payload)
        coding = "gzip" if accepts_gzip(accept_encoding) else "identity"
        etag_value = etag_mod.compute_etag(identity, coding, rep)
        vary = etag_mod.merged_vary("Accept-Encoding")
        not_modified = etag_mod.conditional_304(
            {"ETag": etag_value, "Vary": vary},
            request.headers.get("if-none-match"),
        )
        if not_modified is not None:
            return not_modified
        response = json_response(
            payload, headers=v3_headers, accept_encoding=accept_encoding,
        )
        response.headers["ETag"] = etag_value
        response.headers["Vary"] = vary
        return response
    if rep is None:
        response = json_response(
            sessions,
            headers={"X-Complete": "true" if complete else "false"},
            accept_encoding=accept_encoding,
        )
        # §6.2 (gate C3): directory dimension unconditional — Vary is
        # cache-correctness semantics, independent of validator support.
        response.headers["Vary"] = etag_mod.merged_vary("Accept-Encoding")
        return response
    identity = orjson.dumps(sessions)
    coding = "gzip" if accepts_gzip(accept_encoding) else "identity"
    etag_value = etag_mod.compute_etag(identity, coding, rep)
    vary = etag_mod.merged_vary("Accept-Encoding")
    not_modified = etag_mod.conditional_304(
        {"ETag": etag_value, "Vary": vary},
        request.headers.get("if-none-match"),
        aux={"X-Complete": "true" if complete else "false"},
    )
    if not_modified is not None:
        return not_modified
    response = json_response(
        sessions,
        headers={"X-Complete": "true" if complete else "false"},
        accept_encoding=accept_encoding,
    )
    # json_response owns Vary (unconditionally sets Accept-Encoding);
    # decorate post-hoc so the merged directory dimension survives.
    response.headers["ETag"] = etag_value
    response.headers["Vary"] = vary
    return response


async def _sessions_via_lease(
    request: Request, registry, pool, config, params: dict,
    directory: str | None, limit: int,
    *, roots: bool, start: int | None, search: str | None,
):
    """Join-first lease path for the sessions list. Returns ``None`` when
    the registry budget is full (caller takes the direct path)."""
    accept_encoding = request.headers.get("accept-encoding")

    async def _factory() -> bytes | None:
        return await _fetch_sessions_raw(
            request, params, directory, cap=config.max_response_bytes,
        )

    lease = await registry.fetch_or_bypass(
        (
            "sessions-list", id(request.app.state.upstream), directory,
            _canonical_sessions_query(limit, roots, start, search),
        ),
        _factory,
        reserve_bytes=config.max_response_bytes,
    )
    if lease is None:
        return None
    async with lease:
        body = lease.body
        if body is None:
            raise CodedHTTPException(
                413, code="response_too_large",
                limit=config.max_response_bytes,
            )
        try:
            # rev-gpt B1: the caller's OWN admission + offload — identical
            # admission-before-projection discipline (and byte-identical
            # ``transform_busy`` 503 shape) as the direct path below; only
            # the raw GET moved out (join-first). The lease context still
            # releases the caller ref on the busy exit (no budget leak).
            # rev-gpt B1-residual: the JSON parse + payload guards live
            # INSIDE the admission section (mirroring the direct path's
            # fetch→parse→project-under-admission and messages.py:710-723),
            # so joiners queued on the transform slot hold only the shared
            # raw body — never a per-caller expanded object graph
            # (plan :110,179 per-caller memory bound).
            async with pool:
                try:
                    payload = orjson.loads(body)
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise_upstream_unavailable(exc)
                if not isinstance(payload, list):
                    raise_upstream_unavailable()
                if payload and not all(isinstance(s, dict) for s in payload):
                    raise_upstream_unavailable()
                sessions = await pool.offload(_project_sessions, payload)
        except TransformBusy:
            return busy_response(accept_encoding)
    # X-Complete is computed per-caller from the caller's own limit; the
    # ETag/304 evaluation is per-caller too (Batch 2 / B1).
    return _finalize_sessions_response(request, sessions, limit, accept_encoding)


async def _fetch_status_raw(
    request: Request, params: dict, directory: str | None,
) -> bytes:
    """Shared factory body: ONE upstream ``GET /session/status`` (including
    the status mapping — a 5xx fails the flight for every joiner)."""
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
    return response.content


async def _status_via_lease(
    request: Request, registry, directory: str | None,
):
    """Join-first lease path for sessions/status. The TurnRegistry merge is
    deliberately OUTSIDE the factory (plan A3): turn state changes with
    time, so every caller merges the CURRENT registry state into the shared
    body — never frozen at factory time."""
    async def _factory() -> bytes:
        return await _fetch_status_raw(request, {"directory": directory}
                                       if directory is not None else {},
                                       directory)

    lease = await registry.fetch_or_bypass(
        ("sessions-status", id(request.app.state.upstream), directory),
        _factory,
        reserve_bytes=request.app.state.config.max_response_bytes,
    )
    if lease is None:
        return None
    async with lease:
        body = lease.body  # bytes are immutable — safe to parse post-release
    try:
        payload = orjson.loads(body)
    except (orjson.JSONDecodeError, ValueError) as exc:
        raise_upstream_unavailable(exc)
    if not isinstance(payload, dict):
        raise_upstream_unavailable()
    # Per-caller turn merge — identical to the direct path (contract §3.y.1).
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


@router.get("/sessions")
async def sessions(
    request: Request,
    directory: str | None = None,
    roots: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    start: int | None = Query(None, ge=0),
    search: str | None = None,
):
    # v3 (§5, Batch B): a consumed ``?directory=`` was validated + stripped
    # at dispatch — the stash replaces the (absent) query param here.
    directory = resolve_route_directory(request.scope, directory)
    if directory is not None:
        # slimapi no longer gates directories — normalize and forward; the
        # upstream opencode decides whether it can serve the directory.
        directory = validate_directory(directory)
    params = {"limit": limit, "roots": str(roots).lower()}
    if directory is not None and wire_view_from_scope(request.scope) != 3:
        # v3 (§5.2, Batch B): a consumed directory travels upstream as the
        # canonical ``X-Opencode-Directory`` header ONLY — the dispatch
        # layer stripped the client's query pair, and the sidecar does not
        # re-add it as an upstream query param.
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
    registry = getattr(request.app.state, "raw_fetch_registry", None)
    if registry is not None and config.coalesce_enabled:
        leased = await _sessions_via_lease(
            request, registry, request.app.state.transforms, config,
            params, directory, limit,
            roots=roots, start=start, search=search,
        )
        if leased is not None:
            return leased
        # budget full → unchanged admission-first direct path below
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
    # do not emit it, by design). ETag/304 per-caller (Batch 2 / B1).
    return _finalize_sessions_response(
        request, sessions, limit, request.headers.get("accept-encoding"),
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
    # v3 (§5, Batch B): stash substitutes the stripped query param (see
    # the sessions-list handler above).
    directory = resolve_route_directory(request.scope, directory)
    if directory is not None:
        directory = validate_directory(directory)
    params: dict[str, str] = {}
    if directory is not None and wire_view_from_scope(request.scope) != 3:
        # v3 (§5.2, Batch B): canonical header only — see the sessions-list
        # handler above.
        params["directory"] = directory
    registry = getattr(request.app.state, "raw_fetch_registry", None)
    if registry is not None and request.app.state.config.coalesce_enabled:
        leased = await _status_via_lease(request, registry, directory)
        if leased is not None:
            return leased
        # budget full → unchanged direct path below
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



