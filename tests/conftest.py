from __future__ import annotations

import httpx
import pytest


@pytest.fixture
async def upstream_factory():
    """Build a MockTransport-backed AsyncClient; handler is set per-test.

    Shared across test_errors / test_sessions_routes / test_proxy /
    test_questions_routes. Mirrors oc_slimapi.upstream.create_client."""
    clients: list[httpx.AsyncClient] = []

    def _make(handler, *, base_url: str = "http://127.0.0.1:4096"):
        client = httpx.AsyncClient(
            base_url=base_url,
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.aclose()
