from __future__ import annotations

from contextvars import ContextVar

import httpx
import pytest

from oc_slimapi.sse.replay_log import ReplayLog


_CURRENT_REPLAY_LOG: ContextVar[ReplayLog] = ContextVar("test_replay_log")


def current_replay_log() -> ReplayLog:
    """Return the current test's explicitly-owned v4 replay ring."""
    return _CURRENT_REPLAY_LOG.get()


@pytest.fixture(autouse=True)
def replay_log():
    """Provide one test-owned v4 replay ring and close it after the test."""
    log = ReplayLog()
    token = _CURRENT_REPLAY_LOG.set(log)
    try:
        yield log
    finally:
        _CURRENT_REPLAY_LOG.reset(token)
        log.close()


@pytest.fixture
async def upstream_factory():
    """Build a MockTransport-backed AsyncClient; handler is set per-test.

    Shared across test_errors / test_sessions_routes / test_proxy.
    Mirrors oc_slimapi.upstream.create_client."""
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
