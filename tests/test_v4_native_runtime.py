"""Structural locks for the staged v4-native runtime refactor.

Task 1 makes the process-wide replay ring a required ``HubRegistry``
dependency.  The remaining red assertions deliberately describe deletion
work owned by Sessions B/C and Session A Task 4; they must not be weakened to
make this file green early.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import httpx
from fastapi import FastAPI, Request

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import health as health_routes
from oc_slimapi.selector import SlimapiSelectorMiddleware, wire_view_from_scope
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub_types import IMMEDIATE
from oc_slimapi.sse.registry import HubRegistry
from oc_slimapi.sse.replay_log import ReplayLog
from oc_slimapi.sse.tokenstream.hub import TokenStreamHub
from oc_slimapi.traffic import TrafficLedger
from oc_slimapi.traffic_snapshot import aggregate_v3_observability
from oc_slimapi.turn_registry import IncarnationStore


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "oc_slimapi"


def _source(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def _required_parameter(owner: object, name: str) -> inspect.Parameter:
    parameter = inspect.signature(owner).parameters[name]
    assert parameter.default is inspect.Parameter.empty
    return parameter


def _exact_string_constants(relative: str) -> set[str]:
    tree = ast.parse(_source(relative), filename=relative)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_hub_registry_requires_one_replay_log_at_construction() -> None:
    parameter = _required_parameter(HubRegistry.__init__, "replay_log")
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert not hasattr(HubRegistry, "set_replay_log")

    log = ReplayLog()
    registry = HubRegistry(None, replay_log=log)
    try:
        assert registry.get_global()._replay is log
    finally:
        log.close()


def test_all_hubs_require_the_replay_log_dependency() -> None:
    """Expected red until Session B and Session A Task 4 finish."""
    _required_parameter(GlobalHub.__init__, "replay_log")
    _required_parameter(TokenStreamHub.__init__, "replay_log")
    assert not hasattr(GlobalHub, "set_replay_log")


def test_subscriber_apis_have_no_wire_version_dimension() -> None:
    """Expected red until the v3 subscriber branches are deleted."""
    surfaces = (
        HubRegistry.subscribe,
        GlobalHub.subscribe,
        _source("sse/tokenstream/subscriber.py"),
        _source("sse/tokenstream/fanout.py"),
    )
    for surface in surfaces:
        text = surface if isinstance(surface, str) else str(inspect.signature(surface))
        assert "wire_v4" not in text


def test_public_sse_routes_do_not_select_a_legacy_serializer() -> None:
    """Expected red until Sessions B and A Task 4 collapse delivery."""
    assert "wire_v4" not in _source("routes/events.py")
    assert "wire_v4" not in _source("routes/token_stream.py")


def test_global_event_route_requires_lifespan_owned_replay_log() -> None:
    """The v4-native route has no selector-less/no-log runtime branch."""
    source = _source("routes/events.py")
    assert 'getattr(request.app.state, "replay_log", None)' not in source
    assert "request.app.state.replay_log" in source
    assert "RESYNC_RECONNECT_NO_REPLAY" not in source


def test_retired_outbound_sse_frame_builders_are_absent() -> None:
    """Expected red until the global/token v3 frame families are removed."""
    global_source = _source("sse/global_hub.py")
    token_frames = _source("sse/tokenstream/frames.py")
    assert '"type": "server.connected"' not in global_source
    for symbol in ("_snapshot_frame", "_truncated_frame", "_connected_frame"):
        assert f"def {symbol}(" not in token_frames


def test_projection_defaults_and_directory_state_are_v4_native() -> None:
    """Expected red until Session C removes v3 defaults and naming."""
    skeleton_source = _source("skeleton.py")
    list_source = _source("routes/messages/_list.py")
    selector_source = _source("selector.py")
    for source in (skeleton_source, list_source):
        assert "wire_view: int = 3" not in source
    assert "?v=3" not in skeleton_source
    assert "V3_DIRECTORY_STATE_KEY" not in selector_source
    assert 'DIRECTORY_STATE_KEY = "slimapi_directory"' in selector_source


def test_selectorless_routes_do_not_read_retired_directory_header() -> None:
    """Expected red until Session C removes the direct-stack fallback."""
    for relative in (
        "routes/read_groups.py",
        "routes/messages/_router.py",
        "routes/token_stream.py",
    ):
        assert "x-opencode-directory" not in _source(relative).lower()


async def test_retired_public_paths_are_closed_without_upstream_io() -> None:
    calls = 0

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"unexpected": True})

    app = FastAPI()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream_handler))
    app.state.upstream = upstream
    install_proxy(app)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for path in ("/event", "/global/event", "/session/s1/message"):
                response = await client.get(path)
                assert response.status_code == 404
                assert response.json() == {"code": "thin_route_not_found"}
    finally:
        await upstream.aclose()

    # The handler is intentionally unused: the closed boundary owns every
    # request above and cannot forward to any upstream transport.
    assert calls == 0


async def test_selector_admits_only_v4_and_ignores_old_version_header() -> None:
    app = FastAPI()
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)

    @app.get("/slimapi/echo")
    async def echo(request: Request) -> dict:
        return {
            "view": wire_view_from_scope(request.scope),
            "query": request.scope["query_string"].decode("latin-1"),
        }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for target in ("/slimapi/echo", "/slimapi/echo?v=3"):
            rejected = await client.get(
                target,
                headers={"X-Slimapi-Version": "4"},
            )
            assert rejected.status_code == 400
            assert rejected.json() == {
                "code": "unsupported_version",
                "supported": [4],
            }

        admitted = await client.get(
            "/slimapi/echo?keep=1&v=4",
            headers={"X-Slimapi-Version": "1"},
        )
        assert admitted.status_code == 200
        assert admitted.json() == {"view": 4, "query": "keep=1"}


def test_retired_tracking_headers_and_fields_have_no_producer() -> None:
    retired = {
        "X-Next-Cursor",
        "X-Complete",
        "X-Slimapi-Subscriber-ID",
        "X-Children-Version",
        "childrenVersion",
        "childrenIDs",
        "childrenComplete",
    }
    runtime_strings: set[str] = set()
    for relative in (
        "routes/messages/_list.py",
        "routes/events.py",
        "routes/token_stream.py",
        "routes/children.py",
    ):
        runtime_strings.update(_exact_string_constants(relative))
    assert retired.isdisjoint(runtime_strings)


def test_upstream_native_versioned_event_names_remain_supported() -> None:
    assert {
        "question.v2.asked",
        "question.v2.replied",
        "question.v2.rejected",
        "permission.v2.asked",
        "permission.v2.replied",
    } <= IMMEDIATE


async def test_preserved_health_and_observability_shapes_remain_visible() -> None:
    ledger = TrafficLedger()
    assert ledger.snapshot()["v3"] == {
        "matrix": {},
        "sseLifecycle": {},
        "sseActive": {},
    }
    aggregated = aggregate_v3_observability([])
    assert {"counts", "countsByDate", "sseActive", "sseLive"} <= aggregated.keys()

    app = FastAPI()
    app.state.config = Settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(health_routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/slimapi/health")
    assert response.status_code == 200
    body = response.json()
    assert body["server"]["api_version"] == 4
    assert body["server"]["accepted_client_versions"] == [4, 4]
    assert body["schema"]["version"] == 4
    assert body["schema"]["clientMin"] == 4
    assert body["schema"]["clientMax"] == 4
    assert body["features"]["tokenCoalesce"] is True


def test_deliberately_retained_compatibility_surfaces_stay_present() -> None:
    passthrough_source = _source("routes/_read_passthrough.py")
    config_source = _source("config.py")

    assert "response_rep_version(config, wire_view=3)" in passthrough_source
    assert "OC_SLIMAPI_SERVER_API_VERSION" in config_source
    assert "OC_SLIMAPI_ACCESS_LOG_PATH" in config_source
    assert "migrate_legacy_access_log" in _source("access_log.py")
    assert "legacy_state_dir" in inspect.signature(IncarnationStore.__init__).parameters
