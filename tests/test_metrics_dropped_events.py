"""R-5 (owner ruling 2026-08-21): ``droppedEventsByType`` on /slimapi/metrics.

Additive exposure of the 4.5.0-internal catch-all drop counter table
(``GlobalHub.upstream_dropped_events_total``, L1-2/F-216) onto the
``/slimapi/metrics`` hubs[] entry — superseding the 4.5.0 internal-only
decision. Locks four properties:

1. Per-type counts are on the wire (route-level, real FastAPI app).
2. Additivity: every pre-existing hubs[] key is untouched.
3. Always published: an empty drop table surfaces as ``{}`` — the key is
   never optional.
4. Snapshot independence (NI-1): a returned snapshot's dict is decoupled
   from the hub's live table (shallow copy; nested-field identity check).

Plan: docs/ocmar/plans/2026-08-21-batch2-decision-rollout.md §Lane A Task A-2.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import metrics
from oc_slimapi.sse.hub import HubRegistry
from oc_slimapi.transform import TransformConfig, TransformPool


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
        max_subscribers_per_directory=8,
        max_total_subscribers=16,
        sse_queue_items=256,
        sse_buffer_bytes=2 * 1024 * 1024,
        sse_max_frame_bytes=256 * 1024,
    )


def _build_app(settings: Settings) -> tuple[FastAPI, HubRegistry, httpx.AsyncClient]:
    """Mirror the construction pattern of tests/test_metrics.py (real
    FastAPI app + registry wired into app.state, metrics router mounted)."""
    app = FastAPI(title="oc-slimapi-metrics-dropped-test")
    upstream = httpx.AsyncClient()
    app.state.config = settings
    app.state.upstream = upstream
    transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.transforms = transforms
    hubs = HubRegistry(
        upstream,
        max_subscribers_per_directory=settings.max_subscribers_per_directory,
        max_total_subscribers=settings.max_total_subscribers,
        queue_items=settings.sse_queue_items,
        buffer_bytes=settings.sse_buffer_bytes,
        max_frame_bytes=settings.sse_max_frame_bytes,
    )
    hubs.set_transforms(transforms)
    app.state.hubs = hubs
    app.include_router(metrics.router)
    register_error_handlers(app)
    return app, hubs, upstream


def ev(directory, event_type: str, properties: dict | None = None) -> dict:
    """Build one upstream /global/event frame."""
    return {"directory": directory, "payload": {"type": event_type, "properties": properties or {}}}


async def test_per_type_drop_counts_reach_the_wire():
    """A-2-C1: unknown upstream types dropped by the hub's catch-all are
    surfaced per-type on GET /slimapi/metrics hubs[0]."""
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        hub = hubs.get_global()
        hub.publish(ev(None, "todo.updated"))
        hub.publish(ev(None, "todo.updated"))
        hub.publish(ev(None, "file.edited"))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics")
        assert response.status_code == 200
        entry = response.json()["sse"]["hubs"][0]
        assert entry["droppedEventsByType"] == {
            "todo.updated": 2,
            "file.edited": 1,
        }
    finally:
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def test_hubs_entry_keys_are_additive():
    """A-2-C2: every pre-existing hubs[] key survives — R-5 is purely
    additive; the key set is the 4.5.0 five PLUS droppedEventsByType."""
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        subscriber = hubs.subscribe()  # materialize the hubs[] entry
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics")
        entry = response.json()["sse"]["hubs"][0]
        assert set(entry) == {
            "subscribers",
            "upstreamConnected",
            "upstreamEventsTotal",
            "emittedFramesTotal",
            "reconnectsTotal",
            "droppedEventsByType",
        }
        assert entry["subscribers"] == 1
    finally:
        hubs.unsubscribe(subscriber)
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def test_empty_drop_table_always_publishes_empty_dict():
    """A-2-C3: on a fresh hub with zero drops the key is present and ``{}``
    — always-published, never optional."""
    app, hubs, upstream = _build_app(_settings())
    transport = httpx.ASGITransport(app)
    try:
        hubs.subscribe()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/slimapi/metrics")
        entry = response.json()["sse"]["hubs"][0]
        assert "droppedEventsByType" in entry
        assert entry["droppedEventsByType"] == {}
    finally:
        await hubs.close()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def test_snapshot_independence_from_live_table():
    """NI-1 (rev4): a returned snapshot stays frozen against later hub
    mutations, and its nested field is a distinct object from the hub's
    live table (shallow-copy decoupling — guards against a future change
    back to a shared reference)."""
    registry = HubRegistry(None)
    hub = registry.get_global()
    hub.publish(ev(None, "todo.updated"))

    snap1 = registry.snapshot_metrics()
    frozen = snap1["sse"]["hubs"][0]["droppedEventsByType"]
    assert frozen == {"todo.updated": 1}

    # Mutate the live table AFTER the snapshot was taken …
    hub.publish(ev(None, "tool.updated"))
    hub.publish(ev(None, "todo.updated"))

    # … the already-returned snapshot dict is unchanged …
    assert snap1["sse"]["hubs"][0]["droppedEventsByType"] == {"todo.updated": 1}
    assert frozen == {"todo.updated": 1}
    # … while a fresh snapshot reflects the new state …
    snap2 = registry.snapshot_metrics()
    assert snap2["sse"]["hubs"][0]["droppedEventsByType"] == {
        "todo.updated": 2, "tool.updated": 1,
    }
    # … and each returned nested field is its own object, NOT the hub's
    # live table (nested-field identity, not the outer snap object).
    assert snap1["sse"]["hubs"][0]["droppedEventsByType"] is not hub.upstream_dropped_events_total
    assert snap2["sse"]["hubs"][0]["droppedEventsByType"] is not hub.upstream_dropped_events_total
