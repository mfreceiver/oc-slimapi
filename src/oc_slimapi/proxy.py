import re

import httpx
from fastapi import FastAPI, Request, WebSocket
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

from .directory import validate_directory
from .errors import CodedHTTPException
from .traffic import stash_up_in, stash_up_out
from .upstream import strip_hop_by_hop, DIRECTORY_HEADER

# B0 §1.3 shell/PTY route table (opencode v1.18.3). Hardcoded — do NOT infer.
# - POST /session/{sid}/shell: legacy direct command execution (spawn).
# - /pty, /api/pty: the two PTY trees (list/create/CRUD/connect-token/connect-WS).
# Method-agnostic: deny any method on these paths (defense in depth).
# WebSocket PTY (/pty/{id}/connect upgrade) is already blocked by the global
# WS catch-all (→ 501) below; this guard covers the HTTP-method variants.
_SHELL_PATH_RE = re.compile(r"^/session/[^/]+/shell/?$")


def _normalize_path(path: str) -> str:
    """Collapse duplicate slashes and reject path traversal segments.

    Mirrors opencode's ``ignoreDuplicateSlashes:true`` behaviour and adds
    security rejection of ``..`` / ``.`` segments. Ensures the result
    starts with ``/``.
    """
    # Ensure leading slash
    if not path.startswith("/"):
        path = "/" + path
    # Collapse consecutive slashes
    normalized = re.sub(r"/+", "/", path)
    # Check for path traversal segments
    for segment in normalized.split("/"):
        if segment in {".", ".."}:
            raise CodedHTTPException(400, code="invalid_path")
    return normalized


def _is_shell_path(path: str) -> bool:
    if path == "/pty" or path.startswith("/pty/"):
        return True
    if path == "/api/pty" or path.startswith("/api/pty/"):
        return True
    return _SHELL_PATH_RE.match(path) is not None


# Turn token fence (S2): scope/sid extraction from the catch-all path. The
# sid segment is captured generically (no opencode ``01HQ...`` format
# hardcoding) so any upstream id shape flows through.
_SESSION_SID_RE = re.compile(r"^/session/([^/]+)")
# The forward paths that bump the turn counter (contract §3.y.3): prompt /
# prompt_async (new turn of work — ocdroid's production send path is
# prompt_async) and abort (cancels the current turn). Trailing slash
# tolerant. The bump itself is additionally gated on POST method below.
_TURN_BUMPING_SUFFIX_RE = re.compile(r"^/session/[^/]+/(prompt(?:_async)?|abort)/?$")


def _extract_sid_from_path(norm_path: str) -> str | None:
    """Extract the ``{sid}`` segment from a ``/session/{sid}/...`` path.

    Returns ``None`` for non-session paths. Does NOT hardcode the opencode
    ``01HQ...`` id format — any non-empty first segment under
    ``/session/`` qualifies (the upstream will reject malformed ids).
    """
    m = _SESSION_SID_RE.match(norm_path)
    if m is None:
        return None
    sid = m.group(1)
    return sid or None


def _is_turn_bumping_path(norm_path: str) -> bool:
    """True iff ``norm_path`` is a turn-bumping forward (contract §3.y.3).

    Matches ``/session/{sid}/prompt``, ``/session/{sid}/prompt_async``, or
    ``/session/{sid}/abort`` (trailing slash tolerant). These are the forwards
    that start/stop a turn of work and therefore must advance the turn
    counter at the S2 commit point (bump-before-send). Path match alone is
    NOT sufficient — the caller must additionally require ``POST`` method.
    """
    return _TURN_BUMPING_SUFFIX_RE.match(norm_path) is not None


# Client-identity headers that must be stripped before forwarding upstream
# (device id could leak PII to opencode if forwarded).
_CLIENT_IDENT_HEADERS = {"x-client-name", "x-client-version", "x-client-id"}


def _strip_client_ident_headers(headers: dict[str, str]) -> dict[str, str]:
    """Remove X-Client-Name / X-Client-Version / X-Client-Id (case-insensitive).

    Operates on the already-validated proxy headers dict (returned by
    :func:`strip_hop_by_hop`). Returns a new dict with the client-ident headers
    removed.
    """
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _CLIENT_IDENT_HEADERS
    }


def install_proxy(app: FastAPI) -> None:
    @app.websocket("/{path:path}")
    async def websocket_not_supported(websocket: WebSocket, path: str):
        await websocket.accept()
        await websocket.send_json({"code": "websocket_not_supported", "status": 501})
        await websocket.close(code=1011)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def proxy(request: Request, path: str):
        # Use the fully-normalized URL.path (FastAPI already collapses //)
        raw_path = request.url.path
        # Normalize the path (reject .. / .)
        try:
            norm_path = _normalize_path(raw_path)
        except CodedHTTPException:
            return JSONResponse({"code": "invalid_path"}, status_code=400)

        if norm_path.startswith("/slimapi/"):
            return JSONResponse({"code": "thin_route_not_found"}, status_code=404)

        # Validate X-Opencode-Directory header if present
        dir_header = request.headers.get(DIRECTORY_HEADER)
        if dir_header is not None:
            try:
                validate_directory(dir_header)
            except CodedHTTPException:
                return JSONResponse({"code": "invalid_directory"}, status_code=400)

        # Validate ?directory= query params (catch-all forwards them upstream)
        for dir_val in request.query_params.getlist("directory"):
            try:
                validate_directory(dir_val)
            except CodedHTTPException:
                return JSONResponse({"code": "invalid_directory"}, status_code=400)

        if request.app.state.config.shell_deny_list_enabled and _is_shell_path(norm_path):
            return JSONResponse({"code": "shell_not_allowed"}, status_code=403)
        client = request.app.state.upstream
        # Wrap the downstream request body stream so we count the bytes
        # actually forwarded upstream (``upOut``). Only ``len()`` per chunk —
        # body is not buffered.
        async def _counted_req_stream():
            n = 0
            try:
                async for chunk in request.stream():
                    n += len(chunk)
                    yield chunk
            finally:
                # finally ensures the stash runs even on client disconnect
                # (GeneratorExit/CancelledError mid-stream), preventing lost
                # upOut bytes on interrupted proxy requests.
                if n > 0:
                    stash_up_out(request, n)

        proxy_headers = strip_hop_by_hop(request.headers)
        proxy_headers = _strip_client_ident_headers(proxy_headers)
        rid = request.scope.get("state", {}).get("request_id")
        if rid is not None:
            proxy_headers["X-Request-ID"] = rid
        upstream_request = client.build_request(
            request.method,
            norm_path,  # norm_path already starts with /
            params=request.query_params.multi_items(),
            headers=proxy_headers,
            content=_counted_req_stream(),
        )
        is_sse = norm_path in {"/event", "/global/event"}
        is_command = norm_path.endswith("/command")
        upstream_request.extensions["timeout"] = {
            "connect": 5.0,
            "read": None if is_sse else (300.0 if is_command else 30.0),
            "write": 300.0,
            "pool": 5.0,
        }
        # S2: turn token fence — commit point is bump-before-send. The turn
        # counter is keyed by sid alone (single sidecar + single opencode
        # backend → sid is globally unique). Bump only on POST prompt/
        # prompt_async/abort forwards (method gate prevents GET/HEAD/etc.
        # on matching paths from advancing the causal high-water).
        #
        # If client.send() raises below (connection-level failure), the turn
        # has already advanced → a HOLE is produced (no rollback /
        # decrement). ocdroid's lex comparison tolerates holes; correctness
        # is preserved.
        turn_registry = getattr(request.app.state, "turn_registry", None)
        if (
            turn_registry is not None
            and request.method == "POST"
            and _is_turn_bumping_path(norm_path)
        ):
            sid = _extract_sid_from_path(norm_path)
            if sid is not None:
                turn_registry.bump_turn(sid)
        try:
            response = await client.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            # Align catch-all with thin routes (sessions/messages/agent/...):
            # upstream connect/read/timeout/pool failures → structured 503
            # upstream_unavailable, not a bare FastAPI 500. NOTE: turn-fence
            # bump above (line ~196) already advanced; the resulting hole on
            # send-failure is tolerated by ocdroid's lex comparison
            # (see comment block above) — no rollback here. Scope: only the
            # send() call itself; mid-stream breaks (send already returned)
            # surface via _counted_upstream_response's finally.
            raise CodedHTTPException(503, code="upstream_unavailable") from exc
        # Wrap the upstream response iterator so we count the bytes returned
        # to the client (``upIn`` — the upstream leg of THIS request). The
        # finally guarantees the count lands even on disconnect / error mid
        # stream. The ``response.aclose()`` in finally is the LAST line of
        # defense against connection-pool leaks: StreamingResponse's
        # BackgroundTask(response.aclose) only runs after the generator
        # completes normally, so a mid-stream exception (generator torn down
        # by a client disconnect or upstream error) would skip it. Closing
        # here is idempotent with the BackgroundTask (httpx aclose is
        # reentrant) — the normal path closes twice harmlessly, the exception
        # path closes exactly once via this finally (P1-10).
        async def _counted_upstream_response():
            n = 0
            try:
                async for chunk in response.aiter_raw():
                    n += len(chunk)
                    yield chunk
            finally:
                if n > 0:
                    stash_up_in(request, n)
                await response.aclose()

        return StreamingResponse(
            _counted_upstream_response(),
            status_code=response.status_code,
            headers=strip_hop_by_hop(response.headers),
            background=BackgroundTask(response.aclose),
        )
