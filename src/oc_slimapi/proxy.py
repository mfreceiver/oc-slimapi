"""Terminal catch-all boundary (v3-only terminal state, contract §8.2 3.0.0).

The v2-era transparent reverse-proxy catch-all is RETIRED: every HTTP
surface is either a collected ``/slimapi`` route or nothing. Un-collected
paths — ``/slimapi/**`` route misses AND all non-slimapi paths (the
retired legacy passthrough surface, including ``/event`` and
``/global/event``) — return 404 ``thin_route_not_found``.

Error priority (§8.3 terminal chain): the selector's 405 ① and 400s
②③ (``unsupported_version`` / ``invalid_version_selector`` /
``directory_conflict`` / ``directory_header_retired`` / …) fire before a
request can reach this boundary, so this handler only ever expresses the
final ④ route-miss class. WebSocket upgrades keep the 501
``websocket_not_supported`` stub.

Retired with the forwarder (moved or deleted):
* Turn-fence S2 bump (prompt_async/abort) → now lives in
  ``routes/write_groups.py`` ``_write_passthrough`` (bump-before-send,
  same semantics) via ``turn_registry.extract_sid_from_path`` /
  ``is_turn_bumping_path``.
* Shell/PTY deny list, directory header/query validation, raw-query
  verbatim forward, upstream byte counting, catch-all SSE observability —
  all unreachable by construction (no forwarding happens here anymore).
"""

from fastapi import FastAPI, Request, WebSocket

from .gzip_util import error_response


def install_proxy(app: FastAPI) -> None:
    """Install the terminal boundary: WS 501 stub + closed HTTP catch-all."""

    @app.websocket("/{path:path}")
    async def websocket_not_supported(websocket: WebSocket, path: str):
        await websocket.accept()
        await websocket.send_json({"code": "websocket_not_supported", "status": 501})
        await websocket.close(code=1011)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def catch_all_closed(request: Request, path: str):
        # §8.2 3.0.0: the reverse proxy is closed. gzip-aware coded error
        # (same response path as thin routes).
        return error_response(
            "thin_route_not_found",
            404,
            accept_encoding=request.headers.get("accept-encoding"),
        )
