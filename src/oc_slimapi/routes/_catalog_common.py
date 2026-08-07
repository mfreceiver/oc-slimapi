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

from ..gzip_util import accepts_gzip, error_response
from ..traffic import stash_up_in
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
):
    """Build & send a streaming GET so we can cap-read the body (413 on oversize)
    instead of buffering the whole catalog into memory at once.

    Caller MUST ``await response.aclose()`` (typically in a ``finally`` block).

    P0-6: forwards ``X-Request-ID`` alongside the directory header so the
    sidecar access log line can be correlated with opencode's logs
    (contract §7). The two headers have distinct names → no collision.
    """
    headers = forward_upstream_headers(
        directory, request_id_from_scope(request.scope),
    )
    upstream_request = request.app.state.upstream.build_request(
        "GET", upstream_path,
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


def make_project_and_pack(
    project_fn,
    body: bytes,
    *,
    err_label: str,
    accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entry: parse + whitelist project + serialize (+ optional gzip).

    Catalog listings are NOT time-ordered; upstream order is preserved (no
    defensive sort, unlike the messages list endpoint). A non-list upstream
    body is a malformed catalog -> ``ValueError``, which the route maps to
    503 ``upstream_unavailable`` (mirrors the sessions-list non-array guard).
    Pure-CPU; runs in the bounded transform executor so the event loop stays
    free for SSE heartbeats.

    ``accept_encoding`` is evaluated via :func:`~oc_slimapi.gzip_util.accepts_gzip`
    so an explicit ``gzip;q=0`` is honoured (RFC 7231).
    """
    parsed = orjson.loads(body)
    if not isinstance(parsed, list):
        raise ValueError(f"non-list {err_label} catalog body")
    projected = project_fn(parsed)
    encoded = orjson.dumps(projected)
    headers: dict[str, str] = {"Vary": "Accept-Encoding"}
    if accepts_gzip(accept_encoding):
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
) -> Response:
    """Skeleton catalog GET handler — admission → stream upstream → cap-read
    → error mapping → offload project+pack → Response.

    The route-level ``@router.get(...)`` handler validates the ``directory``
    parameter via :func:`~oc_slimapi.directory.validate_directory`, then
    delegates the remainder to this shared function. ``read_with_cap`` is a
    parameter so test monkey-patches on the route module pass through.
    """
    config = request.app.state.config
    pool = request.app.state.transforms
    async with pool:
        response = await stream_upstream(request, upstream_path, directory, read_timeout)
        try:
            try:
                if response.status_code >= 400:
                    # Drain upstream error body for connection reuse.
                    body = await response.aread()
                    stash_up_in(request, len(body))
                    # No session-scoped 404 mapping (catalog endpoint);
                    # 4xx -> 502 upstream_http_N, 5xx -> 503 upstream_unavailable.
                    raise_upstream_status_code(response.status_code)
                # on_read stashes each chunk so a mid-stream
                # httpx.RequestError cannot lose already-read bytes from
                # upIn (P0-9); success/cap paths are additive
                # equivalents of the old post-call stash (B1).
                body, _ = await read_with_cap(
                    response, config.max_response_bytes,
                    on_read=lambda n: stash_up_in(request, n),
                )
            except httpx.RequestError as exc:
                # Wrap mid-stream upstream I/O failures (httpx.RequestError
                # raised by the error-body drain aread() or read_with_cap
                # aiter_bytes()) into a structured 503 instead of bubbling
                # up as an unhandled FastAPI 500. The finally below still
                # runs to release the connection.
                raise_upstream_unavailable(exc)
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
                )
            except (orjson.JSONDecodeError, ValueError) as exc:
                raise_upstream_unavailable(exc)
        finally:
            await response.aclose()
    return Response(
        encoded, status_code=200, media_type="application/json",
        headers={"Cache-Control": "no-store", **extra},
    )
