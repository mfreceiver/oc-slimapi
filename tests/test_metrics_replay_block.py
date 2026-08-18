"""B3b-5 — ``GET /slimapi/metrics`` replay observability block.

Shape + counter assertions for the additive ``replay`` block (v4-contract
§9.1 "replay 指标: hit/miss/gap/resync 计数", realized as the
ReplayLog outcome counters):

* the block appears ONLY when a replay log is wired into ``app.state``
  (zero-knowledge additive — the dbaux/traffic/sweep convention);
* ``epoch`` = the log's boot nonce (operator correlation handle for
  client-reported Last-Event-ID epochs);
* state keys (``domains``/``frames``/``bytes``/``barriers``) are split
  from the outcome ``counters`` dict;
* counters reflect the frozen outcome vocabulary of
  ``ReplayLog.replay``: ``replayed`` / ``up_to_date`` / ``ignore_reset``
  / ``epoch_changed`` / ``replay_expired`` / ``replay_gap`` /
  ``reconnect_no_replay`` — and nothing else (no payload/directory leak).
"""

from __future__ import annotations

import re

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import metrics
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.sse.replay_log import (
    GLOBAL_DOMAIN,
    ReplayFrames,
    ReplayIgnoreReset,
    ReplayResync,
    ReplayLog,
    token_domain,
)

_EPOCH_RE = re.compile(r"^[0-9a-f]{16}$")
# Frozen outcome vocabulary (design §9.1 / replay_log counters).
_OUTCOMES = {
    "replayed", "up_to_date", "ignore_reset",
    "epoch_changed", "replay_expired", "replay_gap", "reconnect_no_replay",
}


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=4, accepted_client_versions=(3, 4),
        max_subscribers_per_directory=8, max_total_subscribers=16,
        sse_queue_items=256, sse_buffer_bytes=2 * 1024 * 1024,
        sse_max_frame_bytes=256 * 1024,
    )


def _build_app(replay_log: ReplayLog | None) -> tuple[FastAPI, httpx.AsyncClient]:
    app = FastAPI(title="oc-slimapi-metrics-replay-test")
    upstream = httpx.AsyncClient()
    app.state.config = _settings()
    app.state.upstream = upstream
    hubs = HubRegistry(upstream)
    app.state.hubs = hubs
    if replay_log is not None:
        app.state.replay_log = replay_log
    app.include_router(metrics.router)
    register_error_handlers(app)
    return app, httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test")


async def test_metrics_replay_block_absent_without_log():
    """Zero-knowledge additive: no wired replay log → no ``replay`` key
    (test/minimal apps keep the original metrics shape)."""
    app, client = _build_app(None)
    try:
        body = (await client.get("/slimapi/metrics")).json()
        assert "replay" not in body
    finally:
        await client.aclose()
        await app.state.upstream.aclose()


async def test_metrics_replay_block_shape_and_epoch():
    log = ReplayLog(epoch="0123456789abcdef")
    log.append(GLOBAL_DOMAIN, {"type": "session.digest"})
    log.append(token_domain("s1"), {"type": "token"})
    app, client = _build_app(log)
    try:
        body = (await client.get("/slimapi/metrics")).json()
        replay = body["replay"]
        # Exact key set — producer-owned shape, nothing else leaks in.
        assert set(replay.keys()) == {
            "epoch", "domains", "frames", "bytes", "barriers", "counters",
        }
        assert replay["epoch"] == "0123456789abcdef"
        assert _EPOCH_RE.match(replay["epoch"]) is not None
        assert replay["domains"] == 2          # "g" + "t:s1"
        assert replay["frames"] == 2
        assert replay["bytes"] > 0
        assert replay["barriers"] == 0
        assert replay["counters"] == {}        # no classification ran yet
    finally:
        await client.aclose()
        await app.state.upstream.aclose()


async def test_metrics_replay_counters_track_outcomes():
    """One representative outcome per branch of the ③④ classification →
    the counters dict keys stay within the frozen vocabulary and the
    counts are per-outcome (not a single lumped total)."""
    log = ReplayLog(epoch="0123456789abcdef")
    log.append(GLOBAL_DOMAIN, {"n": 1})
    log.append(GLOBAL_DOMAIN, {"n": 2})
    # replayed: cursor 0 → both frames
    out1 = log.replay(GLOBAL_DOMAIN, 0, "0123456789abcdef")
    assert isinstance(out1, ReplayFrames) and len(out1.entries) == 2
    # up_to_date: cursor == published max → empty replay, NOT a resync
    out2 = log.replay(GLOBAL_DOMAIN, 2, "0123456789abcdef")
    assert isinstance(out2, ReplayFrames) and out2.entries == ()
    # ignore_reset: future cursor beyond published max
    out3 = log.replay(GLOBAL_DOMAIN, 99, "0123456789abcdef")
    assert isinstance(out3, ReplayIgnoreReset)
    # epoch_changed: foreign epoch dominates everything
    out4 = log.replay(GLOBAL_DOMAIN, 1, "ffffffffffffffff")
    assert isinstance(out4, ReplayResync) and out4.reason == "epoch_changed"
    app, client = _build_app(log)
    try:
        replay = (await client.get("/slimapi/metrics")).json()["replay"]
        assert replay["counters"] == {
            "replayed": 1, "up_to_date": 1, "ignore_reset": 1,
            "epoch_changed": 1,
        }
        # vocabulary freeze: no unknown counter keys ever appear
        assert set(replay["counters"]) <= _OUTCOMES
    finally:
        await client.aclose()
        await app.state.upstream.aclose()


async def test_metrics_replay_snapshot_is_pure():
    """Repeated GETs without new replay outcomes observe identical
    counters — the block is a read-only snapshot, never a
    request-scoped mutation."""
    log = ReplayLog(epoch="0123456789abcdef")
    log.append(GLOBAL_DOMAIN, {"n": 1})
    log.replay(GLOBAL_DOMAIN, 0, "0123456789abcdef")
    app, client = _build_app(log)
    try:
        first = (await client.get("/slimapi/metrics")).json()["replay"]
        second = (await client.get("/slimapi/metrics")).json()["replay"]
        assert first == second
        assert first["counters"] == {"replayed": 1}
    finally:
        await client.aclose()
        await app.state.upstream.aclose()


async def test_metrics_replay_barrier_counter_and_expired():
    """Barrier write → reconnect at/below the watermark counts
    ``reconnect_no_replay`` and the ``barriers`` state key flips; TTL
    eviction of the cursor's successor counts ``replay_expired``."""
    class _Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = _Clock()
    log = ReplayLog(epoch="0123456789abcdef", ttl_s=10.0, clock=clock)
    log.append(GLOBAL_DOMAIN, {"n": 1})
    log.write_barrier()                        # watermark = 1 (global domain)
    assert log.barrier_watermark(GLOBAL_DOMAIN) == 1
    out = log.replay(GLOBAL_DOMAIN, 1, "0123456789abcdef")
    assert isinstance(out, ReplayResync)
    assert out.reason == "reconnect_no_replay"
    # fresh domain, one frame, TTL-evicted → cursor older than window
    expired = token_domain("expired")
    log.append(expired, {"n": 1})
    clock.now = 1_000.0                        # age >> ttl_s=10
    out2 = log.replay(expired, 0, "0123456789abcdef")
    assert isinstance(out2, ReplayResync)
    assert out2.reason == "replay_expired"
    app, client = _build_app(log)
    try:
        replay = (await client.get("/slimapi/metrics")).json()["replay"]
        assert replay["barriers"] == 1
        assert replay["counters"]["reconnect_no_replay"] == 1
        assert replay["counters"]["replay_expired"] == 1
    finally:
        await client.aclose()
        await app.state.upstream.aclose()
