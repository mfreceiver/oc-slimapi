"""Shared httpx client and safe header forwarding."""

from __future__ import annotations

from collections.abc import Mapping
import httpx

from .config import Settings
from .middleware.request_id import REQUEST_ID_KEY

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
    # P1-11: ``content-length`` was historically stripped here, but it is NOT
    # a hop-by-hop header per RFC 7230 §6.1 — it is a representation/framing
    # header. Removing it from upstream responses broke downstream framing
    # transparency (contract §4): the client couldn't see the byte count the
    # upstream reported. ``transfer-encoding`` is still stripped (real hop-by-
    # hop), and StreamingResponse framing is handled by uvicorn/httpx based
    # on whichever of (content-length | chunked) survives.
    # ``proxy-connection`` is a non-standard but commonly deployed
    # connection-level header (some proxies/upstreams emit it) — strip it
    # alongside the RFC connection headers.
    "proxy-connection",
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
    # P1-11: read via ``multi_items()`` when available so DUPLICATE headers
    # (multiple Set-Cookie, multiple Cache-Control, etc.) survive the
    # forward. ``httpx.Headers`` and ``starlette.datastructures.Headers``
    # both expose ``multi_items()``; plain dicts (used in unit tests) fall
    # back to ``.items()`` — they have already collapsed duplicates by
    # construction so no information is lost in that case.
    if hasattr(headers, "multi_items"):
        items = list(headers.multi_items())
    else:
        items = list(headers.items())

    # Connection-token list (RFC 7230 §6.1) adds to the per-request blocked
    # set. ``headers.get("connection", ...)`` works on both Mapping types we
    # accept; for multi-valued Connection (rare) we only read one value,
    # which matches the prior behaviour.
    connection_header = ""
    for k, v in items:
        if k.lower() == "connection":
            connection_header = v
            break
    connection_tokens = {
        item.strip().lower()
        for item in connection_header.split(",")
        if item.strip()
    }
    blocked = HOP_BY_HOP | connection_tokens

    # Also strip forbidden forwarded headers
    result: dict[str, str] = {}
    seen_lower: set[str] = set()
    for key, value in items:
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
        # P1-11: preserve duplicate headers by comma-merging per RFC 7230
        # §3.2.2 ('A recipient MAY combine multiple header fields with the
        # same field name into one field-value pair, without changing the
        # semantics, by appending each subsequent field value to the
        # combined field value in order, separated by a comma'). CAVEAT:
        # ``Set-Cookie`` is the one header whose grammar does NOT permit
        # comma-merge (a cookie value may itself contain a comma); clients
        # that need exact Set-Cookie fidelity should re-parse. This is a
        # Starlette Response headers limitation (its Headers type maps
        # field-name → single value) and is preferable to silently dropping
        # the duplicate entirely.
        if key_lower in seen_lower:
            # Case-insensitive lookup of the existing slot's key (preserves
            # the original case of the first occurrence).
            existing_key = next(k for k in result if k.lower() == key_lower)
            result[existing_key] = f"{result[existing_key]}, {value}"
        else:
            result[key] = value
            seen_lower.add(key_lower)
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
