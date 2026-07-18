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
    return {key: value for key, value in headers.items() if key.lower() not in blocked}


def decoded_body_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Headers for httpx-buffered bodies, which have already been decoded."""
    return {
        key: value for key, value in strip_hop_by_hop(headers).items()
        if key.lower() not in {"content-encoding", "content-length"}
    }


def forward_directory_headers(directory: str | None) -> dict[str, str]:
    return {DIRECTORY_HEADER: directory} if directory else {}
