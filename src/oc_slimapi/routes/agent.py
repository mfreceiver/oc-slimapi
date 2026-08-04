from __future__ import annotations

import gzip

import orjson
import httpx
from fastapi import APIRouter, Request
from starlette.responses import Response

from ..directory import validate_directory
from ..errors import CodedHTTPException
from ..gzip_util import error_response
from ..skeleton import skeleton_agents
from ..traffic import stash_up_in
from ..transform import TransformBusy, read_with_cap
from ..upstream import forward_directory_headers

router = APIRouter(prefix="/slimapi", tags=["catalog"])

# Shared with tests so the route and the wire contract agree on Retry-After.
TRANSFORM_RETRY_AFTER_SECONDS = 2


def _project_agent_and_pack(
    body: bytes, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entry: parse + whitelist project + serialize (+ optional gzip).

    Catalog listings are NOT time-ordered; upstream order is preserved (no
    defensive sort, unlike the messages list endpoint). A non-list upstream
    body is a malformed catalog -> ``ValueError``, which the route maps to
    503 ``upstream_unavailable`` (mirrors the sessions-list non-array guard).
    Pure-CPU; runs in the bounded transform executor so the event loop stays
    free for SSE heartbeats.
    """
    parsed = orjson.loads(body)
    if not isinstance(parsed, list):
        raise ValueError("non-list agent catalog body")
    projected = skeleton_agents(parsed)
    encoded = orjson.dumps(projected)
    headers: dict[str, str] = {"Vary": "Accept-Encoding"}
    if "gzip" in (accept_encoding or "").lower():
        encoded = gzip.compress(encoded, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return encoded, headers


def _busy_response(accept_encoding: str | None = None) -> Response:
    """503 + Retry-After when the transform pool admission times out."""
    response = error_response(
        "transform_busy", 503,
        accept_encoding=accept_encoding,
        retry_after=TRANSFORM_RETRY_AFTER_SECONDS,
    )
    response.headers["Retry-After"] = str(TRANSFORM_RETRY_AFTER_SECONDS)
    return response


async def _stream_upstream(request: Request, directory: str | None):
    """Build & send a streaming GET so we can cap-read the body (413 on oversize)
    instead of buffering the whole catalog into memory at once.

    Caller MUST ``await response.aclose()`` (typically in a ``finally`` block).
    """
    upstream_request = request.app.state.upstream.build_request(
        "GET", "/agent",
        headers=forward_directory_headers(directory),
    )
    try:
        return await request.app.state.upstream.send(upstream_request, stream=True)
    except httpx.RequestError as exc:
        raise CodedHTTPException(503, code="upstream_unavailable") from exc


@router.get("/agent")
async def agent(request: Request, directory: str | None = None):
    """Skeleton projection of upstream opencode's agent catalog.

    Proxies upstream ``GET /agent`` and keeps only the ocdroid-consumed
    whitelist (``name`` / ``description`` / ``mode`` / ``hidden`` /
    ``native``), dropping the dominant ``prompt`` (the full system prompt,
    ~34.7%) and ``permission`` (the ``Permission.Ruleset`` list, ~61.2% —
    NOT the pending permission card; no UI consumer). Live-measured ~95.8%
    raw byte saving (250 KB -> 10.7 KB; gzip 3.57 KB — note gzip has some
   消解 because ``permission`` repeats rule strings that compress well).

    ``directory`` is accepted for slimapi API consistency and forwarded as
    ``X-Opencode-Directory``; the agent catalog is global so upstream
    ignores it (harmless). This is an ADDITIVE route: a client on an older
    sidecar without it gets 404 ``thin_route_not_found`` from the catch-all
    proxy and falls back to the passthrough ``GET /agent``.
    """
    if directory is not None:
        directory = validate_directory(directory)
    config = request.app.state.config
    pool = request.app.state.transforms
    try:
        # Admission BEFORE the upstream GET: bounds concurrent catalog
        # fetches (body buffer + parse + project) by max_transforms so a
        # burst cannot monopolise memory / event-loop CPU. Slot released on
        # exit even if the upstream errors out.
        async with pool:
            response = await _stream_upstream(request, directory)
            try:
                try:
                    if response.status_code >= 400:
                        # Drain upstream error body for connection reuse.
                        body = await response.aread()
                        stash_up_in(request, len(body))
                        # No session-scoped 404 mapping (catalog endpoint);
                        # 4xx -> 502 upstream_http_N, 5xx -> 503
                        # upstream_unavailable.
                        if response.status_code < 500:
                            raise CodedHTTPException(
                                502, code=f"upstream_http_{response.status_code}",
                            )
                        raise CodedHTTPException(503, code="upstream_unavailable")
                    body, n_read = await read_with_cap(
                        response, config.max_response_bytes,
                    )
                except httpx.RequestError as exc:
                    # Wrap mid-stream upstream I/O failures (httpx.RequestError
                    # raised by the error-body drain aread() or read_with_cap
                    # aiter_bytes()) into a structured 503 instead of bubbling
                    # up as an unhandled FastAPI 500. The finally below still
                    # runs to release the connection.
                    raise CodedHTTPException(503, code="upstream_unavailable") from exc
                # Traffic accounting: cap-read upstream bytes (counted even
                # on cap-bail, matching the messages /full convention — must
                # run BEFORE the body-None check so oversize reads are still
                # attributed).
                stash_up_in(request, n_read)
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        limit=config.max_response_bytes,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
                try:
                    encoded, extra = await pool.offload(
                        _project_agent_and_pack, body,
                        accept_encoding=request.headers.get("accept-encoding"),
                    )
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise CodedHTTPException(
                        503, code="upstream_unavailable",
                    ) from exc
            finally:
                await response.aclose()
        return Response(
            encoded, status_code=200, media_type="application/json",
            headers={"Cache-Control": "no-store", **extra},
        )
    except TransformBusy:
        return _busy_response(request.headers.get("accept-encoding"))
