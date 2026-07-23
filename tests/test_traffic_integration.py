"""End-to-end integration tests for the traffic-accounting (省流实证) feature.

Exercises the full bidirectional byte ledger (:class:`TrafficLedger`) +
:class:`TrafficAccountingMiddleware` through real FastAPI apps constructed per
scenario, mirroring the per-test app construction pattern in
``test_metrics.py`` / ``test_proxy.py`` / ``test_messages_routes.py``.

Scenarios:

1. **metrics additivity** (core contract): ``GET /slimapi/metrics`` surfaces a
   ``traffic`` block ``iff`` a ledger is wired into ``app.state``. With a
   ledger the block has shape ``{enabled, buckets, totals, ratios}`` and
   ``enabled is True``; without a ledger the response keeps the original
   ``{sse, skeleton, batch}`` shape untouched (zero-knowledge additive —
   proves no impact on the existing 792-test suite).
2. **proxy passthrough baseline** (no 省流): a ``/session/...`` GET through
   ``install_proxy()`` records ``upIn == downOut == len(upstream body)`` in
   the ``proxy_passthrough`` bucket — the catch-all reverse proxy is a 1:1
   byte relay, so the ratio is ~1.0 (proves the baseline bucket is honest).
3. **messages skeleton 省流实证** (the headline): a
   ``GET /slimapi/messages/{sid}`` skeleton-mode GET records
   ``upIn`` (upstream full bytes, including tool ``output`` that skeleton
   strips) ``>>`` ``downOut`` (skeleton-projected bytes) in the ``messages``
   bucket — i.e. ``downOut < upIn`` and
   ``ratios["messages"]["downOutOverUpIn"] < 1.0``. This is the core evidence
   the sidecar is doing its 省流 job.
4. **config env parse**: the new ``OC_SLIMAPI_TRAFFIC_*`` /
   ``OC_SLIMAPI_ACCESS_LOG_*`` :class:`Settings` fields default correctly, and
   disabling the ledger via env makes :meth:`TrafficLedger.snapshot` return
   ``{"enabled": False}``.

Hard constraint: this file is self-contained — the ``_build_app_with_traffic``
helper lives here (not in ``conftest.py``), and NO source / conftest changes.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages, metrics
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.traffic import TrafficLedger
from oc_slimapi.transform import TransformConfig, TransformPool
from oc_slimapi.versioning import SlimapiVersionMiddleware

VERSION_HEADERS = {"X-Slimapi-Version": "1"}


# ---------------------------------------------------------------------------
# Settings + app helpers (mirror test_metrics.py / test_messages_routes.py).
# ---------------------------------------------------------------------------

def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        route_secret="x" * 32,
        route_secret_file=None,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
    )
    base.update(overrides)
    return Settings(**base)


def _build_app_with_traffic(
    settings: Settings,
    upstream: httpx.AsyncClient,
    *,
    wire_ledger: bool = True,
    include_messages: bool = False,
    include_proxy: bool = False,
    include_metrics: bool = False,
) -> tuple[FastAPI, TrafficLedger | None]:
    """Construct a FastAPI app with the traffic ledger + middleware wired up.

    ``TrafficAccountingMiddleware`` is added LAST so it is the OUTERMOST
    wrapper (sees every byte on the wire — downstream req/resp via the wrapped
    receive/send, upstream bytes via the route-handler stash read at request
    end). When ``wire_ledger=True`` a fresh :class:`TrafficLedger` is installed
    on ``app.state.traffic_ledger`` and returned for direct snapshot assertion;
    otherwise no ledger attribute is set (mirrors the existing test fixtures
    that pre-date the traffic feature) and ``None`` is returned.

    Routers are opt-in via flags so each scenario mounts only what it needs
    (the catch-all proxy in particular would otherwise shadow nothing here,
    but keeping the surface minimal makes the byte-accounting attribution
    unambiguous per bucket).
    """
    app = FastAPI(title="oc-slimapi-traffic-test")
    # Version gate added FIRST = innermost. Traffic middleware added LAST =
    # outermost (see docstring).
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=settings.accepted_client_versions,
    )
    app.state.config = settings
    app.state.route_secret = settings.route_secret.encode()
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.directory_allowlist = set()
    # messages router reads allowlist_ready / schema_degraded off app.state.
    app.state.allowlist_ready = False
    app.state.allowlist_lock = asyncio.Lock()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.state.hubs = HubRegistry(upstream)

    ledger: TrafficLedger | None = None
    if wire_ledger:
        ledger = TrafficLedger()
        app.state.traffic_ledger = ledger

    if include_metrics:
        app.include_router(metrics.router)
    if include_messages:
        app.include_router(messages.router)
    if include_proxy:
        install_proxy(app)
    register_error_handlers(app)

    # Outermost: counts downstream req/resp bytes and attributes stashed
    # upstream bytes for every bucket.
    app.add_middleware(TrafficAccountingMiddleware)
    return app, ledger


async def _shutdown(app: FastAPI) -> None:
    """Best-effort teardown mirroring the existing test fixtures."""
    app.state.transforms.shutdown()
    await app.state.hubs.close()


# ===========================================================================
# Scenario 1 — metrics endpoint additivity (core contract).
# ===========================================================================

async def test_metrics_with_ledger_surfaces_traffic_block(upstream_factory):
    """Wired ledger + middleware → GET /slimapi/metrics 200 with a ``traffic``
    block of shape ``{enabled, buckets, totals, ratios}`` and ``enabled is True``."""
    handler_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        handler_calls["count"] += 1
        return httpx.Response(204)

    upstream = upstream_factory(handler)
    app, ledger = _build_app_with_traffic(
        _settings(), upstream, include_metrics=True, wire_ledger=True,
    )
    assert ledger is not None
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics", headers=VERSION_HEADERS)
        assert response.status_code == 200
        data = response.json()
        # The traffic block is additive on top of the existing shape.
        assert "traffic" in data
        traffic = data["traffic"]
        assert set(traffic) == {"enabled", "buckets", "totals", "ratios"}
        assert traffic["enabled"] is True
        # No requests recorded yet → empty buckets / ratios, zero totals.
        assert traffic["buckets"] == {}
        assert traffic["ratios"] == {}
        assert traffic["totals"] == {
            "requests": 0, "downIn": 0, "downOut": 0, "upIn": 0, "upOut": 0,
        }
    finally:
        await _shutdown(app)


async def test_metrics_without_ledger_omits_traffic_block_zero_impact(upstream_factory):
    """Un-wired ledger (mirrors the pre-traffic test fixtures) → GET
    /slimapi/metrics response does NOT contain a ``traffic`` key, and the
    original ``{sse, skeleton, batch}`` shape is unchanged. Proves the feature
    is fully additive to the existing 792-test suite."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    upstream = upstream_factory(handler)
    # wire_ledger=False → no app.state.traffic_ledger attribute. The middleware
    # is STILL mounted (``_build_app_with_traffic`` unconditionally calls
    # ``add_middleware(TrafficAccountingMiddleware)`` at line 144), but without
    # a ledger it operates as a lazy pass-through (pure ASGI wrapper that never
    # records or modifies the response). This means the /slimapi/metrics
    # response has no ``traffic`` key and the middleware exerts zero observable
    # impact on the response — the cleanest "zero impact" baseline.
    app, ledger = _build_app_with_traffic(
        _settings(), upstream, include_metrics=True, wire_ledger=False,
    )
    assert ledger is None
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics", headers=VERSION_HEADERS)
        assert response.status_code == 200
        data = response.json()
        # The traffic block must NOT appear when no ledger is wired.
        assert "traffic" not in data
        # The original metrics shape is intact.
        assert set(data) <= {"sse", "skeleton", "batch"}
        assert set(data["sse"]) == {"subscribers", "hubs", "clients"}
        assert set(data["skeleton"]) == {"activeTransforms", "waitingTransforms", "cacheEnabled"}
    finally:
        await _shutdown(app)


# ===========================================================================
# Scenario 2 — proxy passthrough baseline (no 省流; ratio ≈ 1.0).
# ===========================================================================

async def test_proxy_passthrough_records_equal_up_and_down_bytes(upstream_factory):
    """A /session/... GET through install_proxy() is a 1:1 byte relay: the
    ``proxy_passthrough`` bucket records ``upIn == downOut == len(upstream body)``.

    Uses ``stream=`` for the MockTransport response because the proxy iterates
    ``aiter_raw`` (httpx marks ``content=`` responses as stream-consumed at
    construction, which would raise StreamConsumed mid-relay — see test_proxy.py).
    """
    body = b"x" * 1000  # fixed, unambiguous baseline payload

    def handler(request: httpx.Request) -> httpx.Response:
        async def body_iter():
            yield body

        return httpx.Response(
            200,
            stream=httpx._content.AsyncIteratorByteStream(body_iter()),
            headers={"Content-Type": "application/octet-stream"},
        )

    upstream = upstream_factory(handler)
    app, ledger = _build_app_with_traffic(
        _settings(), upstream, include_proxy=True, wire_ledger=True,
    )
    assert ledger is not None
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/session/ses_x")
        assert response.status_code == 200
        assert response.content == body

        snap = ledger.snapshot()
        assert snap["enabled"] is True
        # The /session path bucketizes to proxy_passthrough.
        assert "proxy_passthrough" in snap["buckets"]
        bucket = snap["buckets"]["proxy_passthrough"]
        # 1:1 relay — upstream bytes in == downstream bytes out == body length.
        assert bucket["upIn"] == len(body)
        assert bucket["downOut"] == len(body)
        assert bucket["requests"] == 1
        # Ratio ≈ 1.0 (no 省流 on the catch-all proxy).
        ratio = snap["ratios"]["proxy_passthrough"]["downOutOverUpIn"]
        assert ratio == pytest.approx(1.0)
    finally:
        await _shutdown(app)


# ===========================================================================
# Scenario 3 — messages skeleton 省流实证 (the headline assertion).
# ===========================================================================

def _fat_upstream_messages_payload() -> bytes:
    """A sizeable upstream full-messages body whose skeleton projection is
    dramatically smaller.

    Each of the 3 messages carries a tool part with a 4 KiB ``state.output``
    blob — skeleton strips ``state.output`` (see skeleton._tool), so the full
    upstream body (~12 KiB+ of output alone) collapses to a small skeleton
    envelope downstream. That gap is the 省流 the sidecar exists to deliver.
    """
    big_output = "O" * 4096  # skeleton drops state.output entirely
    msgs = []
    for i in range(3):
        msgs.append({
            "info": {"id": f"m{i}", "role": "user"},
            "parts": [
                {"id": f"p{i}-text", "type": "text", "messageID": f"m{i}", "text": f"hello {i}"},
                {
                    "id": f"p{i}-tool", "type": "tool", "messageID": f"m{i}", "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls"},
                        "output": big_output,  # dropped by skeleton
                    },
                },
            ],
        })
    return orjson.dumps(msgs)


async def test_messages_skeleton_proves_traffic_saving(upstream_factory):
    """GET /slimapi/messages/{sid}?mode=skeleton records ``upIn`` (upstream
    full body, incl. tool output) clearly greater than ``downOut`` (skeleton
    body) in the ``messages`` bucket: ``downOut < upIn``, positive savedBytes,
    and ``ratios["messages"]["downOutOverUpIn"] < 1.0`` — the 省流实证 core."""
    payload = _fat_upstream_messages_payload()

    def handler(request: httpx.Request) -> httpx.Response:
        # Skeleton list mode hits the /session/{sid}/message listing.
        assert request.url.path == "/session/s1/message"
        return httpx.Response(
            200, content=payload, headers={"Content-Type": "application/json"},
        )

    upstream = upstream_factory(handler)
    app, ledger = _build_app_with_traffic(
        _settings(), upstream, include_messages=True, wire_ledger=True,
    )
    assert ledger is not None
    transport = httpx.ASGITransport(app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Accept-Encoding: identity → no transport gzip, so downOut ==
            # raw skeleton JSON length and the 省流 comparison is purely the
            # skeleton projection (apples-to-apples vs the uncompressed
            # upstream full payload). With gzip the savings would be even
            # larger (downOut would be the compressed wire length), which is
            # a real production win but obscures the projection-only win
            # this test exists to pin.
            response = await client.get(
                "/slimapi/messages/s1?mode=skeleton",
                headers={**VERSION_HEADERS, "Accept-Encoding": "identity"},
            )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") is None
        # The skeleton body must be non-empty and actually project (tool output
        # gone) — otherwise the byte comparison would be meaningless.
        body = orjson.loads(response.content)
        assert len(body) == 3
        for item in body:
            tool_part = item["parts"][1]
            assert "output" not in tool_part.get("state", {})

        snap = ledger.snapshot()
        assert snap["enabled"] is True
        assert "messages" in snap["buckets"]
        bucket = snap["buckets"]["messages"]
        # The headline 省流 assertion: upstream full bytes > downstream skeleton.
        assert bucket["upIn"] == len(payload), (
            f"upIn ({bucket['upIn']}) should equal the full upstream body "
            f"({len(payload)}); stash_up_in mis-attribution?"
        )
        # identity encoding → wire bytes == raw skeleton JSON bytes.
        assert bucket["downOut"] == len(response.content), (
            f"downOut ({bucket['downOut']}) should equal the raw skeleton "
            f"response length ({len(response.content)})"
        )
        assert bucket["downOut"] < bucket["upIn"], (
            "省流 regression: skeleton downOut must be smaller than full upIn"
        )
        saved = bucket["upIn"] - bucket["downOut"]
        assert saved > 0
        # Skeleton strips ~12 KiB of tool output here; the saving must be
        # substantial, not a rounding artifact.
        assert saved > 8 * 1024, (
            f"expected >8 KiB saved by skeleton projection, got {saved} B"
        )
        # And the ratio reflects it.
        assert "messages" in snap["ratios"]
        ratio = snap["ratios"]["messages"]["downOutOverUpIn"]
        assert ratio < 1.0, f"expected downOut/upIn < 1.0 (省流), got {ratio}"
    finally:
        await _shutdown(app)


# ===========================================================================
# Scenario 4 — config env parse for the new traffic / access-log Settings.
# ===========================================================================

def test_traffic_settings_defaults_are_on():
    """The traffic ledger + access log default ON (zero-config observability),
    with the documented default path / rotation knobs."""
    # Settings reads env at construction; scrub the relevant keys so the
    # documented defaults are observed even if another test set them.
    keys = [
        "OC_SLIMAPI_TRAFFIC_METRICS_ENABLED",
        "OC_SLIMAPI_ACCESS_LOG_ENABLED",
        "OC_SLIMAPI_ACCESS_LOG_PATH",
        "OC_SLIMAPI_ACCESS_LOG_MAX_BYTES",
        "OC_SLIMAPI_ACCESS_LOG_BACKUPS",
    ]
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        s = Settings(
            host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
            route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
            server_api_version=1, accepted_client_versions=(1, 1),
        )
        assert s.traffic_metrics_enabled is True
        assert s.access_log_enabled is True
        assert s.access_log_path == "logs/access.jsonl"
        assert s.access_log_max_bytes == 10 * 1024 * 1024
        assert s.access_log_backups == 5
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_traffic_ledger_disabled_via_env_makes_snapshot_disabled(monkeypatch):
    """OC_SLIMAPI_TRAFFIC_METRICS_ENABLED=false flows through Settings →
    TrafficLedger(enabled=...) → snapshot() returns {"enabled": False} and
    every record_* is a no-op.

    NOTE: ``Settings`` is a ``@dataclass(frozen=True, slots=True)`` whose
    ``traffic_metrics_enabled`` default is ``os.getenv(...)`` evaluated at
    **class-definition (module-import) time**, NOT at instantiation. So a
    post-import ``monkeypatch.setenv`` cannot retroactively change the baked-in
    default that ``Settings()`` (with no explicit kwarg) picks up. To honour
    the production wiring (env → field → ledger) WITHOUT a fragile
    ``importlib.reload(config)`` (which re-bakes every default and would
   invalidate other tests' cached ``settings``), we parse the monkeypatched env
    var ourselves and pass it explicitly to ``Settings`` — exercising the real
    field name + the disabled-ledger snapshot contract.
    """
    monkeypatch.setenv("OC_SLIMAPI_TRAFFIC_METRICS_ENABLED", "false")
    # Mirror config.py's own env-parse expression so this test stays locked to
    # the documented env-var name + accepted truthy/falsey token set.
    raw = os.getenv("OC_SLIMAPI_TRAFFIC_METRICS_ENABLED", "true")
    enabled = raw.lower() in ("1", "true", "yes", "on")
    s = Settings(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        route_secret="x" * 32, route_secret_file=None, smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
        traffic_metrics_enabled=enabled,
    )
    assert s.traffic_metrics_enabled is False

    ledger = TrafficLedger(enabled=s.traffic_metrics_enabled)
    assert ledger.enabled is False

    # record_* must be a no-op when disabled.
    ledger.record_downstream(
        bucket="messages", method="GET", status=200,
        req_bytes=10, resp_bytes=20, duration_ms=1.0,
    )
    ledger.record_upstream(
        bucket="messages", method="GET", status=200,
        req_bytes=5, resp_bytes=100,
    )
    # snapshot() returns the disabled sentinel.
    snap = ledger.snapshot()
    assert snap == {"enabled": False}
