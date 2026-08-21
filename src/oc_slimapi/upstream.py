"""Shared httpx client and safe header forwarding."""

from __future__ import annotations

import httpx

from .config import Settings
from .middleware.request_id import REQUEST_ID_KEY

DIRECTORY_HEADER = "X-Opencode-Directory"
REQUEST_ID_HEADER = "X-Request-ID"


def create_client(config: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.upstream,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=300.0, pool=5.0),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        follow_redirects=False,
    )


def forward_directory_headers(directory: str | None) -> dict[str, str]:
    return {DIRECTORY_HEADER: directory} if directory else {}


def forward_upstream_headers(
    directory: str | None,
    request_id: str | None,
) -> dict[str, str]:
    """Build the sidecar-injected header set for an upstream opencode request.

    Combines the directory header (``X-Opencode-Directory``) with the
    correlation header (``X-Request-ID``) so thin routes can forward both
    in one call. The two headers have distinct names → no collision risk;
    either may be ``None``/empty and is simply omitted.

    Contract §7 observability: every sidecar→opencode request must carry the
    ``X-Request-ID`` so the sidecar's access log line can be correlated with
    opencode's own logs. The catch-all proxy already does this; thin routes
    (catalog, /ready) use this helper for the same effect.
    """
    headers = forward_directory_headers(directory)
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    return headers


def request_id_from_scope(scope: dict | None) -> str | None:
    """Read the request_id stored by :class:`RequestIdMiddleware`.

    Returns ``None`` when the middleware did not run (no scope / no state)
    so callers can harmlessly omit the header instead of raising.
    """
    if not scope:
        return None
    state = scope.get("state") or {}
    rid = state.get(REQUEST_ID_KEY)
    return rid or None
