"""Shared catalog-skeleton route logic (agent & command).

P2-B2 dedup: agent.py and command.py were ~95% identical.  This module
factors the shared chain — admission (``async with pool``) → stream upstream
→ cap-read (``read_with_cap``) → error mapping (4xx→502 ``upstream_http_N``,
5xx/network→503 ``upstream_unavailable``) → offload project+pack → Response —
into a single :func:`handle_catalog_request` that takes the variable parts as
parameters (``upstream_path``, ``project_fn``, ``err_label``,
``read_timeout``).

``read_with_cap`` is passed **as a parameter** (not imported here) so the
monkey-patch in ``test_command_routes.py:test_mid_stream_read_error_returns_503``
keeps working with zero test changes — it patches
``oc_slimapi.routes.command.read_with_cap``, and the patched value flows
through to this handler.

P3-C1 gzip: ``make_project_and_pack`` uses :func:`~oc_slimapi.gzip_util.accepts_gzip`
for ``Accept-Encoding`` negotiation (not the old substring match).
"""

from __future__ import annotations

import gzip

import orjson
import httpx
from fastapi import Request
from starlette.responses import Response

from .. import etag as etag_mod
from ..gzip_util import accepts_gzip, error_response
from ..traffic import stash_cache, stash_up_in
from ..upstream import forward_upstream_headers, request_id_from_scope
from ..upstream_errors import (
    raise_upstream_status_code,
    raise_upstream_unavailable,
)

# Shared with tests so the route and the wire contract agree on Retry-After.
TRANSFORM_RETRY_AFTER_SECONDS = 2


def busy_response(accept_encoding: str | None = None) -> Response:
    """503 + Retry-After when the transform pool admission times out."""
    response = error_response(
        "transform_busy", 503,
        accept_encoding=accept_encoding,
        retry_after=TRANSFORM_RETRY_AFTER_SECONDS,
    )
    response.headers["Retry-After"] = str(TRANSFORM_RETRY_AFTER_SECONDS)
    return response


async def stream_upstream(
    request: Request,
    upstream_path: str,
    directory: str | None,
    read_timeout: float | None = None,
    upstream_params: dict[str, str] | None = None,
):
    """Build & send a streaming GET so we can cap-read the body (413 on oversize)
    instead of buffering the whole catalog into memory at once.

    Caller MUST ``await response.aclose()`` (typically in a ``finally`` block).

    P0-6: forwards ``X-Request-ID`` alongside the directory header so the
    sidecar access log line can be correlated with opencode's logs
    (contract §7). The two headers have distinct names → no collision.

    T18: ``upstream_params`` (optional) is forwarded verbatim as the
    upstream GET's query parameters (``None`` → no query, byte-identical
    to today for every existing caller). Used by the sid-scoped diff route
    for the optional ``messageID`` passthrough.
    """
    headers = forward_upstream_headers(
        directory, request_id_from_scope(request.scope),
    )
    upstream_request = request.app.state.upstream.build_request(
        "GET", upstream_path,
        params=upstream_params,
        headers=headers,
    )
    if read_timeout is not None:
        upstream_request.extensions["timeout"] = {
            "connect": 5.0,
            "read": read_timeout,
            "write": read_timeout,
            "pool": 5.0,
        }
    try:
        return await request.app.state.upstream.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)


async def read_upstream_response(
    request: Request,
    response: httpx.Response,
    *,
    cap: int,
    read_with_cap,
    sid: str | None = None,
) -> bytes | None:
    """Drain an error response OR cap-read the success body of a streaming
    upstream ``response`` — the shared skeleton duplicated verbatim across
    sessions / messages / catalog routes.

    Does **not** close ``response`` — the caller owns ``await response.aclose()``
    (typically in a ``finally`` block) so it can keep the response open across
    any post-read offload, matching the existing control flow of every caller.

    Behaviour (identical to the inlined chains it replaces):

    * ``response.status_code >= 400`` → drain the error body (for connection
      reuse), stash its length, then map via
      :func:`raise_upstream_status_code` (``sid`` toggles the session-scoped
      404 → ``session_not_found`` mapping).
    * success → :func:`read_with_cap` with ``on_read=stash_up_in`` so a
      mid-stream ``httpx.RequestError`` cannot lose already-read bytes from
      ``upIn`` (P0-9).
    * any ``httpx.RequestError`` from the drain ``aread()`` or from
      ``read_with_cap``'s ``aiter_bytes()`` → 503 ``upstream_unavailable``
      (structured, never a bare 500).

    Returns the buffered body on success, or ``None`` when the cap was
    exceeded (caller decides its own 413 shape — ``response_too_large`` vs
    ``message_too_large``). ``read_with_cap`` is a parameter so test
    monkey-patches on the route module (e.g. ``command.read_with_cap``) flow
    through unchanged.
    """
    try:
        if response.status_code >= 400:
            err_body = await response.aread()
            stash_up_in(request, len(err_body))
            raise_upstream_status_code(response.status_code, sid=sid)
        body, _ = await read_with_cap(
            response, cap,
            on_read=lambda n: stash_up_in(request, n),
        )
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    return body


def make_project_and_pack(
    project_fn,
    body: bytes,
    *,
    err_label: str,
    accept_encoding: str | None,
    rep_version: bytes | None = None,
    if_none_match: str | None = None,
    merge_directory_vary: bool = False,
    min_gzip_bytes: int | None = None,
) -> tuple[bytes | None, dict[str, str]]:
    """Worker entry: parse + whitelist project + serialize (+ optional gzip).

    Catalog listings are NOT time-ordered; upstream order is preserved (no
    defensive sort, unlike the messages list endpoint). A non-list upstream
    body is a malformed catalog -> ``ValueError``, which the route maps to
    503 ``upstream_unavailable`` (mirrors the sessions-list non-array guard).
    Pure-CPU; runs in the bounded transform executor so the event loop stays
    free for SSE heartbeats.

    ``accept_encoding`` is evaluated via :func:`~oc_slimapi.gzip_util.accepts_gzip`
    so an explicit ``gzip;q=0`` is honoured (RFC 7231).

    Traffic plan Batch 2 / B1: when ``rep_version`` (a
    :func:`~oc_slimapi.etag.response_rep_version` value) is set, the worker
    derives the canonical validator from the identity (pre-gzip) bytes and
    judges ``If-None-Match`` BEFORE compressing (plan §4: a gzip hit is
    canonical-hash-only — zero compression, zero transport). A hit returns
    ``encoded=None`` with the ``ETag``/merged-``Vary`` headers; the route
    emits the 304. A miss compresses as before and the headers carry the
    same validator the 304 would have. ``rep_version=None`` keeps the
    return shape's headers byte-identical to the pre-ETag path.

    rev-6 B1/C2 (traffic plan Batch 3): ``rep_version=None`` routes may
    still pass ``merge_directory_vary=True`` (todo/children: ETag opted
    out per plan §5, but the directory variance is real — keep the merged
    ``Vary`` so caches key on ``X-Opencode-Directory``) and
    ``min_gzip_bytes`` (benefit gate: identity bodies below the threshold
    skip gzip). The gate is ONLY for routes without a pre-compression
    validator judgment — agent/command compress unconditionally so the
    judged coding == the served coding (Batch 2 final design); combining
    ``rep_version`` with ``min_gzip_bytes`` would break that exactness and
    is not done.
    """
    parsed = orjson.loads(body)
    if not isinstance(parsed, list):
        raise ValueError(f"non-list {err_label} catalog body")
    projected = project_fn(parsed)
    identity = orjson.dumps(projected)
    encoded = identity
    headers: dict[str, str] = {"Vary": "Accept-Encoding"}
    gzip_wanted = accepts_gzip(accept_encoding)
    if min_gzip_bytes is not None and len(identity) < min_gzip_bytes:
        gzip_wanted = False
    if rep_version is not None:
        # Exact prediction: this route compresses unconditionally on
        # accepts_gzip, so the judgment coding == the served coding.
        coding = "gzip" if gzip_wanted else "identity"
        headers["ETag"] = etag_mod.compute_etag(identity, coding, rep_version)
        headers["Vary"] = etag_mod.merged_vary(headers["Vary"])
        if etag_mod.if_none_match_matches(if_none_match, headers["ETag"]):
            return None, headers  # 304: canonical hash only — no compress
    elif merge_directory_vary:
        headers["Vary"] = etag_mod.merged_vary(headers["Vary"])
    if gzip_wanted:
        encoded = gzip.compress(encoded, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return encoded, headers


async def handle_catalog_request(
    request: Request,
    *,
    upstream_path: str,
    directory: str | None,
    project_fn,
    read_with_cap,
    err_label: str,
    read_timeout: float | None = None,
    cache=None,
    sid: str | None = None,
    enable_etag: bool = True,
    merge_directory_vary: bool = False,
    min_gzip_bytes: int | None = None,
    upstream_params: dict[str, str] | None = None,
) -> Response:
    """Skeleton catalog GET handler — admission → stream upstream → cap-read
    → error mapping → offload project+pack → Response.

    The route-level ``@router.get(...)`` handler validates the ``directory``
    parameter via :func:`~oc_slimapi.directory.validate_directory`, then
    delegates the remainder to this shared function. ``read_with_cap`` is a
    parameter so test monkey-patches on the route module pass through.

    Traffic plan Batch 1 / A1: ``cache`` (a
    :class:`~oc_slimapi.catalog_cache.CatalogCache` or ``None``) enables the
    TTL body cache. Absent (legacy test apps / knob off) → the uncached path
    below, byte-identical to today. Present → admission-first order is
    preserved: the upstream GET still happens inside transform admission,
    deduplicated through the cache's refresh single-flight.

    Traffic plan Batch 3 / C2a: ``sid`` (session-scoped routes — todo /
    children) toggles the 404 → ``session_not_found`` mapping in
    :func:`read_upstream_response`. Catalog routes omit it (unchanged
    404 → 502 ``upstream_http_404`` behaviour).

    rev-6 B1: ``enable_etag=False`` (todo/children — plan §5 keeps Batch 3
    off the Batch 2 ETag wiring) forces ``rep_version=None``: no ``ETag``
    header, no 304 judgment, while ``merge_directory_vary=True`` keeps the
    directory-merged ``Vary`` on 200s (the variance is real regardless of
    validator support) and ``min_gzip_bytes`` adds the tiny-body benefit
    gate (rev-6 C2). Defaults leave agent/command byte-identical.

    T18: ``upstream_params`` (optional) forwards verbatim query parameters
    on the upstream GET (``None`` → no query, byte-identical to today).
    """
    if cache is not None:
        return await _handle_catalog_cached(
            request,
            cache=cache,
            upstream_path=upstream_path,
            directory=directory,
            project_fn=project_fn,
            read_with_cap=read_with_cap,
            err_label=err_label,
            read_timeout=read_timeout,
        )
    config = request.app.state.config
    pool = request.app.state.transforms
    rep_version = (
        etag_mod.response_rep_version(config) if enable_etag else None
    )
    async with pool:
        response = await stream_upstream(
            request, upstream_path, directory, read_timeout,
            upstream_params=upstream_params,
        )
        try:
            body = await read_upstream_response(
                request, response,
                cap=config.max_response_bytes,
                read_with_cap=read_with_cap,
                sid=sid,
            )
            if body is None:
                return error_response(
                    "response_too_large", 413,
                    limit=config.max_response_bytes,
                    accept_encoding=request.headers.get("accept-encoding"),
                )
            try:
                encoded, extra = await pool.offload(
                    make_project_and_pack, project_fn, body,
                    err_label=err_label,
                    accept_encoding=request.headers.get("accept-encoding"),
                    rep_version=rep_version,
                    if_none_match=request.headers.get("if-none-match"),
                    merge_directory_vary=merge_directory_vary,
                    min_gzip_bytes=min_gzip_bytes,
                )
            except (orjson.JSONDecodeError, ValueError) as exc:
                raise_upstream_unavailable(exc)
        finally:
            await response.aclose()
    if encoded is None:
        # Pre-compression validator hit (judged in the worker on the
        # identity bytes — plan §4): 304 with zero compression.
        return etag_mod.conditional_304(
            extra, request.headers.get("if-none-match"),
        )
    return Response(
        encoded, status_code=200, media_type="application/json",
        headers={"Cache-Control": "no-store", **extra},
    )


async def _offload_catalog_body(
    request: Request, pool, project_fn, body: bytes, err_label: str,
    rep_version: bytes | None = None,
) -> Response:
    """Project+pack a (cached or freshly read) body under admission.

    Shared by the cached paths only — the uncached path above keeps its
    inline form byte-for-byte. Bad JSON / non-list bodies map to 503
    ``upstream_unavailable`` exactly like the uncached path (a cached body
    was validated at store time, but the check stays for parity).

    Batch 2 / B1: ``rep_version`` flows into the pack worker (validator on
    the FINAL projected body — a cached raw body re-projected per config is
    hashed after projection) and a pre-compression ``If-None-Match`` hit
    short-circuits with 304 BEFORE any gzip work (the upstream GET / cache
    refresh has already run — the pipeline is never skipped).
    """
    try:
        encoded, extra = await pool.offload(
            make_project_and_pack, project_fn, body,
            err_label=err_label,
            accept_encoding=request.headers.get("accept-encoding"),
            rep_version=rep_version,
            if_none_match=request.headers.get("if-none-match"),
        )
    except (orjson.JSONDecodeError, ValueError) as exc:
        raise_upstream_unavailable(exc)
    if encoded is None:
        # Pre-compression validator hit (judged in the worker on the
        # identity bytes — plan §4): 304 with zero compression.
        return etag_mod.conditional_304(
            extra, request.headers.get("if-none-match"),
        )
    return Response(
        encoded, status_code=200, media_type="application/json",
        headers={"Cache-Control": "no-store", **extra},
    )


async def _handle_catalog_cached(
    request: Request,
    *,
    cache,
    upstream_path: str,
    directory: str | None,
    project_fn,
    read_with_cap,
    err_label: str,
    read_timeout: float | None = None,
) -> Response:
    """Cached catalog chain (traffic plan Batch 1 / A1).

    * Fresh hit → skip the upstream GET entirely; admission + offload only.
    * Miss → admission-first refresh (today's order): acquire transform
      admission, then GET (deduplicated across concurrent callers by the
      cache's refresh single-flight) + cap-read + store + offload.
    * Only successful 200 bodies are ever cached (factory errors, cap
      overflow, bad/non-list JSON bypass the store) — see CatalogCache.
    * ttl=0 disables the cache entirely: refresh returns state ``None`` and
      no ``cache`` field is reported (the access-log key stays omitted).
    """
    config = request.app.state.config
    pool = request.app.state.transforms
    rep_version = etag_mod.response_rep_version(config)
    key = (upstream_path, directory)
    body = cache.lookup(key)
    if body is not None:
        stash_cache(request, "hit")
        async with pool:
            return await _offload_catalog_body(
                request, pool, project_fn, body, err_label,
                rep_version,
            )

    async def _fetch_body() -> bytes | None:
        response = await stream_upstream(
            request, upstream_path, directory, read_timeout
        )
        try:
            return await read_upstream_response(
                request, response,
                cap=config.max_response_bytes,
                read_with_cap=read_with_cap,
            )
        finally:
            await response.aclose()

    async with pool:
        body, cache_state = await cache.refresh(key, _fetch_body)
        # rev-gpt addendum: ttl=0 disables the cache — refresh returns
        # state None and NO ``cache`` semantics are reported (the field is
        # omitted, consistent with the None-omits-key rule). A live miss
        # reports "miss" only when the cache actually made a decision.
        stash_cache(request, cache_state)
        if body is None:
            return error_response(
                "response_too_large", 413,
                limit=config.max_response_bytes,
                accept_encoding=request.headers.get("accept-encoding"),
            )
        return await _offload_catalog_body(
            request, pool, project_fn, body, err_label,
            rep_version,
        )
