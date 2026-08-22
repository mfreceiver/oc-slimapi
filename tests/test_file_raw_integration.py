"""P5 Phase D integration: ``/slimapi/file/raw`` on the PRODUCTION app.

Unlike ``tests/test_file_raw.py`` (which self-registers the router on a
private FastAPI app), these tests import the real ``oc_slimapi.app`` module
singleton — the full production assembly: selector middleware (version gate
+ §5.3 directory consuming set), traffic accounting, request-id, the file_raw
router registration, and the catch-all proxy — so a green run here proves
the router is actually wired into ``app.py`` and that ``?v=4`` + directory
semantics flow through the selector for this path.

``app.state`` is populated the way ``oc_slimapi.app.lifespan`` would
(ASGITransport fires no startup event) and restored on teardown, keeping
the singleton clean for other test modules.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import base64
import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.app import app as production_app
from oc_slimapi.config import Settings
from oc_slimapi.transform import TransformConfig, TransformPool


def _settings(**overrides) -> Settings:
    values = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.05,
        max_response_bytes=64 * 1024,
        file_raw_max_envelope_bytes=32 * 1024 * 1024,
        smoke_session_id=None,
        directory_allowlist=None,
    )
    values.update(overrides)
    return Settings(**values)


def _legacy_binary(body: bytes, *, mime: str | None = "image/png") -> bytes:
    value: dict[str, object] = {
        "type": "binary",
        "content": base64.b64encode(body).decode("ascii"),
    }
    if mime is not None:
        value["mimeType"] = mime
    return orjson.dumps(value)


def _legacy_text(body: str) -> bytes:
    return orjson.dumps({"type": "text", "content": body})


def _response_handler(body: bytes, *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=body,
            headers={"Content-Type": "application/json"},
        )

    return handler


_STATE_ATTRS = ("config", "upstream", "transforms", "schema_degraded")
_SENTINEL = object()


@asynccontextmanager
async def _production_stack(
    handler,
    *,
    settings: Settings | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, list[httpx.Request], FastAPI]]:
    """Serve requests through the real production app singleton.

    ``app.state`` attributes the route depends on are installed for the
    duration of the context and restored (or removed) afterwards.
    """
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    settings = settings or _settings()
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    pool = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))

    saved = {
        name: getattr(production_app.state, name, _SENTINEL)
        for name in _STATE_ATTRS
    }
    production_app.state.config = settings
    production_app.state.upstream = upstream
    production_app.state.transforms = pool
    production_app.state.schema_degraded = False

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=production_app),
            base_url="http://test",
        ) as client:
            yield client, seen, production_app
    finally:
        pool.shutdown()
        await upstream.aclose()
        for name, value in saved.items():
            if value is _SENTINEL:
                try:
                    delattr(production_app.state, name)
                except AttributeError:
                    pass
            else:
                setattr(production_app.state, name, value)


async def test_file_raw_reachable_via_production_app_assembly():
    """The router registered in app.py serves /slimapi/file/raw end to end:
    200, envelope decoded, upstream sees /file/content with sidecar-owned
    query keys (v) stripped."""
    payload = b"\x00\x02PNG\xff\x20"
    async with _production_stack(
        _response_handler(_legacy_binary(payload)),
    ) as (client, seen, _app):
        response = await client.get(
            "/slimapi/file/raw?v=4&path=image.png",
            headers={"Accept-Encoding": "gzip"},
        )

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"].startswith("image/png")
    assert len(seen) == 1
    assert seen[0].url.path == "/file/content"
    assert seen[0].url.params["path"] == "image.png"
    assert "v" not in seen[0].url.params
    assert "directory" not in seen[0].url.params


async def test_file_raw_directory_consumed_by_selector_on_production_app(tmp_path):
    """?directory= on /file/raw takes the §5.3 consuming-set path: the
    selector validates + stashes + strips it, the route resolves the stash,
    and the directory reaches upstream ONLY as the canonical
    X-Opencode-Directory header (allowlist canonicalises to realpath)."""
    allowed = tmp_path / "workspace"
    allowed.mkdir()

    async with _production_stack(
        _response_handler(_legacy_text("ok")),
        settings=_settings(directory_allowlist=[str(allowed)]),
    ) as (client, seen, _app):
        response = await client.get(
            "/slimapi/file/raw?v=4&path=x&directory=" + str(allowed),
        )

    assert response.status_code == 200
    assert response.content == b"ok"
    assert len(seen) == 1
    assert seen[0].headers["x-opencode-directory"] == str(allowed.resolve())
    assert "directory" not in seen[0].url.params
    assert "v" not in seen[0].url.params


@pytest.mark.parametrize("url", [
    "/slimapi/file/raw?path=x",
    "/slimapi/file/raw?v=3&path=x",
    "/slimapi/file/raw?v=3&v=4&path=x",
])
async def test_file_raw_version_gate_on_production_app(url):
    """The v4-only selector gate wraps the production app — the newly
    registered route cannot be reached without exactly ?v=4, and rejects
    before any upstream contact."""
    async with _production_stack(
        _response_handler(_legacy_text("x")),
    ) as (client, seen, _app):
        response = await client.get(url)

    assert response.status_code == 400
    assert seen == []


async def test_file_raw_consuming_semantics_on_production_app(tmp_path):
    """Consume-set ladder outcomes for ?directory= on /file/raw, matching
    the file read-group siblings: multi-value, dual-present, retired
    header, and invalid values all 400 before the route/upstream."""
    allowed = tmp_path / "workspace"
    allowed.mkdir()

    async with _production_stack(
        _response_handler(_legacy_text("ok")),
        settings=_settings(directory_allowlist=[str(allowed)]),
    ) as (client, seen, _app):
        multi = await client.get(
            "/slimapi/file/raw?v=4&path=x"
            f"&directory={allowed}&directory={allowed}/other"
        )
        assert multi.status_code == 400
        assert multi.json()["code"] == "invalid_directory_selector"

        dual = await client.get(
            f"/slimapi/file/raw?v=4&path=x&directory={allowed}",
            headers={"X-Opencode-Directory": str(allowed / "other")},
        )
        assert dual.status_code == 400
        assert dual.json()["code"] == "directory_conflict"

        header_only = await client.get(
            "/slimapi/file/raw?v=4&path=x",
            headers={"X-Opencode-Directory": str(allowed)},
        )
        assert header_only.status_code == 400
        assert header_only.json()["code"] == "directory_header_retired"

        invalid = await client.get(
            "/slimapi/file/raw?v=4&path=x&directory=" + str(allowed / ".." / "escape"),
        )
        assert invalid.status_code == 400
        assert invalid.json()["code"] == "invalid_directory"

    assert seen == []
