"""B4-4 directory allowlist fail-closed coverage (S-B05)."""

from __future__ import annotations

import logging
import os

import httpx
import orjson
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.config import Settings, directory_allowed
from oc_slimapi.app import _log_directory_allowlist
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health, read_groups
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.sse.global_hub import GlobalHub
from oc_slimapi.sse.hub_types import Subscriber


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        directory_allowlist=None,
    )
    base.update(overrides)
    return Settings(**base)


def _build_file_app(settings: Settings, handler) -> tuple[FastAPI, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


def _file_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=b'{"sentinel":"byte-identical"}',
        headers={"Content-Type": "application/json"},
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _event(directory, event_type="session.status", properties=None):
    return {
        "directory": directory,
        "payload": {
            "type": event_type,
            "properties": properties or {"sessionID": "s1", "status": "busy"},
        },
    }


async def _close_hub(hub: GlobalHub) -> None:
    tasks = [
        task
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await __import__("asyncio").gather(*tasks, return_exceptions=True)


def test_directory_allowlist_env_has_three_states(monkeypatch):
    monkeypatch.delenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", raising=False)
    assert Settings().directory_allowlist is None

    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "")
    assert Settings().directory_allowlist == []

    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "/a/:/b/../b")
    assert Settings().directory_allowlist == ["/a", "/b"]


def test_directory_allowlist_env_rejects_blank_and_relative_entries(monkeypatch):
    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "/a::/b")
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_DIRECTORY_ALLOWLIST"):
        Settings().validate()

    monkeypatch.setenv("OC_SLIMAPI_DIRECTORY_ALLOWLIST", "relative")
    with pytest.raises(RuntimeError, match="OC_SLIMAPI_DIRECTORY_ALLOWLIST"):
        Settings().validate()


async def test_unconfigured_file_route_is_byte_identical():
    app, seen = _build_file_app(_settings(), _file_ok)
    async with _client(app) as client:
        response = await client.get(
            "/slimapi/file?v=4&path=foo&directory=/outside",
            headers={"Accept-Encoding": "identity"},
        )
    assert response.status_code == 200
    assert response.content == b'{"sentinel":"byte-identical"}'
    assert seen[0].url.path == "/file"


@pytest.mark.parametrize("path", ["/slimapi/file", "/slimapi/file/content", "/slimapi/file/status"])
@pytest.mark.parametrize("directory", ["/outside", None])
async def test_empty_allowlist_blocks_all_file_routes(path, directory):
    app, seen = _build_file_app(_settings(directory_allowlist=[]), _file_ok)
    async with _client(app) as client:
        query = "?v=4"
        if directory is not None:
            query += "&directory=" + directory
        if path.endswith("/file") or path.endswith("/file/content"):
            query += "&path=foo"
        response = await client.get(
            path + query, headers={"Accept-Encoding": "identity"})
    # B3b wire lock: the allowlist REJECTION is a 403 with raw identity
    # bytes + no Cache-Control (intentionally split from the token-stream
    # side's 400 directory_not_allowed — see test_token_stream_route.py).
    assert response.status_code == 403
    assert response.content == b'{"code":"directory_not_allowed"}'
    assert response.headers["content-type"] == "application/json"
    assert response.headers["vary"] == "Accept-Encoding"
    assert "cache-control" not in response.headers
    assert "outside" not in response.text
    assert not seen


async def test_allowlist_file_route_allows_directory_subtree_and_blocks_prefix_trap():
    app, seen = _build_file_app(_settings(directory_allowlist=["/a", "/ab"]), _file_ok)
    async with _client(app) as client:
        allowed = await client.get("/slimapi/file?v=4&path=foo&directory=/a/b")
        exact = await client.get("/slimapi/file/status?v=4&directory=/ab")
        blocked = await client.get("/slimapi/file/content?v=4&path=foo&directory=/abc")
    assert allowed.status_code == 200
    assert exact.status_code == 200
    assert blocked.status_code == 403
    assert len(seen) == 2


async def test_empty_allowlist_does_not_filter_sse():
    hub = GlobalHub(client=None, directory_allowlist=[])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/outside"))
        hub.flush()
        assert subscriber.queue.qsize() == 1
        assert hub.allowlist_dropped_events == 0
    finally:
        await _close_hub(hub)


async def test_nonempty_allowlist_drops_outside_digest_and_counts_once():
    hub = GlobalHub(client=None, directory_allowlist=["/allowed"])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/outside"))
        hub.flush()
        assert subscriber.queue.qsize() == 0
        assert hub.allowlist_dropped_events == 1
    finally:
        await _close_hub(hub)


async def test_nonempty_allowlist_drops_outside_immediate_and_unknown_frames():
    hub = GlobalHub(client=None, directory_allowlist=["/allowed"])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/outside", "question.asked", {"id": "q1"}))
        hub.publish(_event(None, "permission.asked", {"id": "p1"}))
        assert subscriber.queue.qsize() == 0
        assert hub.allowlist_dropped_events == 2
    finally:
        await _close_hub(hub)


async def test_nonempty_allowlist_allows_subtree_and_preserves_changed_field():
    hub = GlobalHub(client=None, directory_allowlist=["/allowed"])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("/allowed/nested"))
        hub.flush()
        frame = await subscriber.queue.get()
        assert b'"directory":"/allowed/nested"' in frame
        assert b'"changed":["s1"]' in frame
        assert hub.allowlist_dropped_events == 0
    finally:
        await _close_hub(hub)


async def test_health_reports_allowlist_and_dropped_event_count():
    app = FastAPI()
    settings = _settings(directory_allowlist=["/allowed"])
    app.state.config = settings
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    hub = GlobalHub(client=None, directory_allowlist=settings.directory_allowlist)
    hub.allowlist_dropped_events = 3

    class Registry:
        def get_global(self):
            return hub

    app.state.hubs = Registry()
    app.include_router(health.router)
    register_error_handlers(app)
    try:
        async with _client(app) as client:
            response = await client.get("/slimapi/health")
        assert response.status_code == 200
        assert response.json()["features"]["allowlist"] == {
            "enabled": True,
            "droppedEvents": 3,
        }
    finally:
        await _close_hub(hub)


def test_empty_allowlist_startup_warning(caplog):
    settings = _settings(directory_allowlist=[])
    with caplog.at_level(logging.WARNING, logger="oc_slimapi.app"):
        _log_directory_allowlist(settings)
    assert "directory allowlist enabled but empty" in caplog.text
    assert "/slimapi/file/** will 403" in caplog.text


# --- rev-sgpt MAJOR-1: symlink bypass (canonical realpath matching) ---------


async def test_symlink_escape_is_rejected_by_file_routes(tmp_path):
    """Lexically-inside symlink that resolves outside → 403 (realpath)."""
    allowed_root = tmp_path / "allowed_root"
    outside = tmp_path / "outside"
    allowed_root.mkdir()
    outside.mkdir()
    os.symlink(outside, allowed_root / "link")
    app, seen = _build_file_app(
        _settings(directory_allowlist=[str(allowed_root)]), _file_ok
    )
    async with _client(app) as client:
        response = await client.get(
            "/slimapi/file?v=4&path=foo&directory=" + str(allowed_root / "link"),
            headers={"Accept-Encoding": "identity"},
        )
    assert response.status_code == 403
    assert response.content == b'{"code":"directory_not_allowed"}'
    assert str(outside) not in response.text
    assert not seen


async def test_symlink_chain_within_subtree_is_allowed(tmp_path):
    """link → link2 → real, all resolving inside the allowed root → 200."""
    allowed_root = tmp_path / "allowed_root"
    real_dir = allowed_root / "real"
    allowed_root.mkdir()
    real_dir.mkdir()
    os.symlink(real_dir, allowed_root / "link2")
    os.symlink(allowed_root / "link2", allowed_root / "link")
    app, seen = _build_file_app(
        _settings(directory_allowlist=[str(allowed_root)]), _file_ok
    )
    async with _client(app) as client:
        response = await client.get(
            "/slimapi/file?v=4&path=foo&directory=" + str(allowed_root / "link"),
            headers={"Accept-Encoding": "identity"},
        )
    assert response.status_code == 200
    assert response.content == b'{"sentinel":"byte-identical"}'
    assert len(seen) == 1


async def test_nonexistent_candidate_falls_back_to_lexical_subtree(tmp_path):
    """No symlinks to resolve: non-strict realpath keeps the missing tail
    verbatim, so the decision degrades to the lexical subtree match."""
    allowed_root = tmp_path / "allowed_root"
    allowed_root.mkdir()
    app, seen = _build_file_app(
        _settings(directory_allowlist=[str(allowed_root)]), _file_ok
    )
    async with _client(app) as client:
        inside = await client.get(
            "/slimapi/file?v=4&path=foo&directory="
            + str(allowed_root / "no" / "such" / "dir"),
            headers={"Accept-Encoding": "identity"},
        )
        outside = await client.get(
            "/slimapi/file?v=4&path=foo&directory="
            + str(tmp_path / "elsewhere" / "missing"),
            headers={"Accept-Encoding": "identity"},
        )
    assert inside.status_code == 200
    assert outside.status_code == 403
    assert outside.json() == {"code": "directory_not_allowed"}
    assert len(seen) == 1


async def test_symlink_escape_digest_frame_dropped_and_counted(tmp_path):
    """SSE filter: digest whose directory symlink-escapes the root is
    dropped + counted (same canonical decision as the /file routes)."""
    allowed_root = tmp_path / "allowed_root"
    outside = tmp_path / "outside"
    allowed_root.mkdir()
    outside.mkdir()
    os.symlink(outside, allowed_root / "link")
    hub = GlobalHub(client=None, directory_allowlist=[str(allowed_root)])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event(str(allowed_root / "link")))
        hub.flush()
        assert subscriber.queue.qsize() == 0
        assert hub.allowlist_dropped_events == 1
    finally:
        await _close_hub(hub)


def test_directory_allowed_repeated_decisions_are_stable(tmp_path):
    """Cache correctness: identical inputs keep yielding identical
    verdicts across repeated resolutions (deny AND allow paths)."""
    allowed_root = tmp_path / "allowed_root"
    outside = tmp_path / "outside"
    allowed_root.mkdir()
    outside.mkdir()
    os.symlink(outside, allowed_root / "link")
    allowlist = [str(allowed_root)]
    assert [directory_allowed(allowlist, str(allowed_root / "link"))
            for _ in range(3)] == [False, False, False]
    assert [directory_allowed(allowlist, str(allowed_root / "sub"))
            for _ in range(3)] == [True, True, True]


# --- rev-2 closure: realtime candidate resolution / relative / canonical
# forward (sub-1 / sub-2 / sub-3) -------------------------------------------


async def test_candidate_created_as_escape_symlink_after_first_check(tmp_path):
    """rev-2 scenario 1 (sub-1): the candidate is judged nonexistent first
    (lexical subtree pass), then created as an escaping symlink — the
    second judgement must re-resolve in realtime and 403. No candidate
    realpath caching exists, so no stale verdict can be ridden."""
    allowed_root = tmp_path / "allowed_root"
    outside = tmp_path / "outside"
    allowed_root.mkdir()
    outside.mkdir()
    future = allowed_root / "future"  # does not exist yet
    app, seen = _build_file_app(
        _settings(directory_allowlist=[str(allowed_root)]), _file_ok
    )
    url = "/slimapi/file?v=4&path=foo&directory=" + str(future)
    async with _client(app) as client:
        first = await client.get(url, headers={"Accept-Encoding": "identity"})
        os.symlink(outside, future)  # becomes a symlink escaping the root
        second = await client.get(url, headers={"Accept-Encoding": "identity"})
    assert first.status_code == 200
    assert second.status_code == 403
    assert second.json() == {"code": "directory_not_allowed"}
    assert len(seen) == 1  # only the first (legitimately allowed) call


async def test_symlink_retarget_after_first_check_is_rejected(tmp_path):
    """rev-2 scenario 2 (sub-1): link first points INSIDE the allowed root
    (pass), then is retargeted outside at runtime — realtime candidate
    resolution must reject the second request."""
    allowed_root = tmp_path / "allowed_root"
    real_dir = allowed_root / "real"
    outside = tmp_path / "outside"
    allowed_root.mkdir()
    real_dir.mkdir()
    outside.mkdir()
    link = allowed_root / "link"
    os.symlink(real_dir, link)
    app, seen = _build_file_app(
        _settings(directory_allowlist=[str(allowed_root)]), _file_ok
    )
    url = "/slimapi/file?v=4&path=foo&directory=" + str(link)
    async with _client(app) as client:
        first = await client.get(url, headers={"Accept-Encoding": "identity"})
        link.unlink()
        os.symlink(outside, link)
        second = await client.get(url, headers={"Accept-Encoding": "identity"})
    assert first.status_code == 200
    assert second.status_code == 403
    assert second.json() == {"code": "directory_not_allowed"}
    assert len(seen) == 1


async def test_relative_directory_rejected_even_when_cwd_inside_allowed_root(
    tmp_path, monkeypatch
):
    """rev-2 scenario 3 (sub-2): with the sidecar CWD inside the allowed
    root, a relative directory would realpath-resolve into the subtree —
    it must still fail closed with the uniform 403 (the upstream executes
    against its own CWD, so a relative authorisation object is never
    valid)."""
    allowed_root = tmp_path / "allowed_root"
    allowed_root.mkdir()
    monkeypatch.chdir(allowed_root)
    app, seen = _build_file_app(
        _settings(directory_allowlist=[str(allowed_root)]), _file_ok
    )
    async with _client(app) as client:
        response = await client.get(
            "/slimapi/file?v=4&path=foo&directory=sessions/sub",
            headers={"Accept-Encoding": "identity"},
        )
    assert response.status_code == 403
    assert response.json() == {"code": "directory_not_allowed"}
    assert not seen


async def test_forwarded_directory_header_is_canonical_after_symlink_pass(
    tmp_path,
):
    """rev-2 scenario 4 (sub-3): after a passing check the upstream request
    must carry the CANONICAL directory (realpath result) in
    ``X-Opencode-Directory`` — not the original symlink path — binding the
    access to the object the authorization decision was made on."""
    allowed_root = tmp_path / "allowed_root"
    real_dir = allowed_root / "real"
    allowed_root.mkdir()
    real_dir.mkdir()
    os.symlink(real_dir, allowed_root / "link")
    app, seen = _build_file_app(
        _settings(directory_allowlist=[str(allowed_root)]), _file_ok
    )
    async with _client(app) as client:
        response = await client.get(
            "/slimapi/file?v=4&path=foo&directory=" + str(allowed_root / "link"),
            headers={"Accept-Encoding": "identity"},
        )
    assert response.status_code == 200
    assert len(seen) == 1
    forwarded = seen[0].headers["x-opencode-directory"]
    assert forwarded == str(real_dir)
    assert forwarded != str(allowed_root / "link")


async def test_relative_directory_frame_dropped_when_allowlist_set(
    tmp_path, monkeypatch
):
    """sub-2 at the SSE layer: a relative-directory frame is dropped +
    counted under a set allowlist (same fail-closed semantics as the
    routes), even when the sidecar CWD sits inside the allowed root."""
    allowed_root = tmp_path / "allowed_root"
    allowed_root.mkdir()
    monkeypatch.chdir(allowed_root)
    hub = GlobalHub(client=None, directory_allowlist=[str(allowed_root)])
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event("sessions/sub"))
        hub.flush()
        assert subscriber.queue.qsize() == 0
        assert hub.allowlist_dropped_events == 1
    finally:
        await _close_hub(hub)


# --- rev-3: root-symlink retarget + identical-value re-apply must
# re-resolve canonical roots (clear_allowlist_roots_cache is real) ------


async def test_root_retarget_reapply_same_settings_value_revalidates(tmp_path):
    """rev-3 path ①: the allowlist entry is a root symlink resolved to
    old_root on first validate() (roots cached); the symlink is then
    retargeted to new_root and the SAME allowlist value is re-applied via
    a fresh Settings().validate() — the cached roots must be invalidated
    (same value ⇒ same cache key, so only the explicit clear helps): the
    old canonical root's subtree now 403s, the new root's passes."""
    old_root = tmp_path / "old_root"
    new_root = tmp_path / "new_root"
    (old_root / "sub").mkdir(parents=True)
    (new_root / "sub2").mkdir(parents=True)
    link = tmp_path / "allowed_link"
    os.symlink(old_root, link)
    allowlist = [str(link)]  # the SAME value is reused after retarget

    settings = _settings(directory_allowlist=allowlist)
    settings.validate()  # config-determination point #1 → roots := old_root
    app, _ = _build_file_app(settings, _file_ok)
    async with _client(app) as client:
        before = await client.get(
            "/slimapi/file?v=4&path=foo&directory=" + str(old_root / "sub"),
            headers={"Accept-Encoding": "identity"},
        )
    assert before.status_code == 200  # decision #1 populated the cache

    link.unlink()
    os.symlink(new_root, link)  # retarget the root symlink on disk

    revalidated = _settings(directory_allowlist=allowlist)  # identical value
    revalidated.validate()  # must clear cached roots
    app2, seen2 = _build_file_app(revalidated, _file_ok)
    async with _client(app2) as client:
        after_old = await client.get(
            "/slimapi/file?v=4&path=foo&directory=" + str(old_root / "sub"),
            headers={"Accept-Encoding": "identity"},
        )
        after_new = await client.get(
            "/slimapi/file?v=4&path=foo&directory=" + str(new_root / "sub2"),
            headers={"Accept-Encoding": "identity"},
        )
    assert after_old.status_code == 403
    assert after_old.json() == {"code": "directory_not_allowed"}
    assert after_new.status_code == 200
    assert len(seen2) == 1  # only the new-root request reached upstream


async def test_root_retarget_runtime_hub_reapply_same_allowlist_value(tmp_path):
    """rev-3 path ②: same retarget scenario, but the re-apply happens at
    runtime via ``GlobalHub.set_directory_allowlist(same_value)`` — the
    hub's frame filter must drop the old canonical root's digest (+count)
    and pass the new root's."""
    old_root = tmp_path / "old_root"
    new_root = tmp_path / "new_root"
    (old_root / "sub").mkdir(parents=True)
    (new_root / "sub2").mkdir(parents=True)
    link = tmp_path / "allowed_link"
    os.symlink(old_root, link)
    allowlist = [str(link)]

    hub = GlobalHub(client=None, directory_allowlist=allowlist)
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        hub.publish(_event(str(old_root / "sub")))
        hub.flush()
        assert subscriber.queue.qsize() == 1  # decision #1 cached old_root

        link.unlink()
        os.symlink(new_root, link)  # retarget
        hub.set_directory_allowlist(allowlist)  # SAME value re-applied

        # Step-wise assertions so the stale-cache outcome (old root still
        # emitted) fails HERE, not via coincidentally-equal final counts.
        hub.publish(_event(str(old_root / "sub")))
        hub.flush()  # old canonical root → must be dropped + counted
        assert subscriber.queue.qsize() == 1  # still only the pre-retarget frame
        assert hub.allowlist_dropped_events == 1
        hub.publish(_event(str(new_root / "sub2")))
        hub.flush()  # new canonical root → passes
        assert subscriber.queue.qsize() == 2
        assert hub.allowlist_dropped_events == 1
    finally:
        await _close_hub(hub)
