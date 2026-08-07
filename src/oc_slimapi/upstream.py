"""Shared httpx client and safe header forwarding."""

from __future__ import annotations

from collections.abc import Mapping
import httpx

from .config import Settings
from .middleware.request_id import REQUEST_ID_KEY

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
DIRECTORY_HEADER = "X-Opencode-Directory"
REQUEST_ID_HEADER = "X-Request-ID"

# Additional headers that must not be forwarded from client to upstream.
# These are security-sensitive or may be spoofed by untrusted clients.
FORBIDDEN_PREFIXES = {
    "x-forwarded-",
    "x-real-",
}
FORBIDDEN_EXACT = {
    "x-real-ip",
}


def create_client(config: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.upstream,
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=300.0, pool=5.0),
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        follow_redirects=False,
    )


def strip_hop_by_hop(headers: Mapping[str, str]) -> dict[str, str]:
    connection_tokens = {
        item.strip().lower()
        for item in headers.get("connection", "").split(",")
        if item.strip()
    }
    blocked = HOP_BY_HOP | connection_tokens

    # Also strip forbidden forwarded headers
    result: dict[str, str] = {}
    for key, value in headers.items():
        key_lower = key.lower()
        if key_lower in blocked:
            continue
        if key_lower in FORBIDDEN_EXACT:
            continue
        if any(key_lower.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            continue
        # Strip cookie header — opencode does not rely on cookies and it is
        # security-sensitive to forward potentially-session-fixing cookies.
        if key_lower == "cookie":
            continue
        result[key] = value
    return result

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
