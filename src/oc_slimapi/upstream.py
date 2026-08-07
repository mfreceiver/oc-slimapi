"""Shared httpx client and safe header forwarding."""

from __future__ import annotations

from collections.abc import Mapping
import httpx

from .config import Settings

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
DIRECTORY_HEADER = "X-Opencode-Directory"

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
