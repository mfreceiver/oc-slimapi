"""TDD coverage for the Phase B messages ``since`` query contract."""

from __future__ import annotations

import base64
import asyncio
from contextlib import asynccontextmanager

import httpx
import orjson
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages
from oc_slimapi.routes.messages import _list as messages_list
from oc_slimapi.singleflight import LeasedSingleFlight, fulls
from oc_slimapi.since_cache import SinceCache
from oc_slimapi.transform import TransformConfig, TransformPool


def _settings(**overrides) -> Settings:
    values = {
        "host": "127.0.0.1",
        "port": 4097,
        "upstream": "http://127.0.0.1:4096",
        "max_message_bytes": 32 * 1024 * 1024,
        "max_transforms": 1,
        "transform_wait_seconds": 1.0,
        "max_response_bytes": 64 * 1024,
        "smoke_session_id": None,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def since_client():
    payload = orjson.dumps([])
    upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=payload)
        ),
    )
    app = FastAPI()
    settings = _settings()
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.state.since_cache = SinceCache(
        enabled=True,
        max_entries=256,
        max_bytes=64 * 1024 * 1024,
        max_entry_bytes=1024 * 1024,
        epoch="test-epoch",
    )
    app.include_router(messages.router)
    install_proxy(app)
    register_error_handlers(app)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.state.transforms.shutdown()
        await upstream.aclose()


def _message(mid: str, created: int, text: str) -> dict:
    return {
        "info": {
            "id": mid,
            "role": "user",
            "time": {"created": created},
        },
        "parts": [
            {"id": f"{mid}-part", "type": "text", "messageID": mid, "text": text}
        ],
    }


@asynccontextmanager
async def _message_client(
    handler, *, raw_registry=False, settings_options=None, **cache_options
):
    upstream = httpx.AsyncClient(
        base_url="http://127.0.0.1:4096",
        transport=httpx.MockTransport(handler),
    )
    app = FastAPI()
    settings = _settings(**(settings_options or {}))
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    cache_config = {
        "enabled": True,
        "max_entries": 256,
        "max_bytes": 64 * 1024 * 1024,
        "max_entry_bytes": 1024 * 1024,
    }
    cache_config.update(cache_options)
    app.state.since_cache = SinceCache(epoch="test-epoch", **cache_config)
    registry = None
    if raw_registry:
        registry = LeasedSingleFlight(
            max_bytes=4 * settings.max_response_bytes,
            network_concurrency=4,
            result_grace_seconds=0,
        )
        app.state.raw_fetch_registry = registry
    app.include_router(messages.router)
    install_proxy(app)
    register_error_handlers(app)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            yield client, app
    finally:
        if registry is not None:
            registry.shutdown()
        app.state.transforms.shutdown()
        await upstream.aclose()


async def test_since_and_before_are_rejected_as_invalid_params(since_client):
    response = await since_client.get(
        "/slimapi/messages/s1?since=not-a-token&before=cursor"
    )

    assert response.status_code == 400
    assert response.json() == {"code": "invalid_params"}


async def test_full_response_signs_next_since_and_since_returns_changed_items():
    payloads = [
        orjson.dumps([_message("m1", 1, "one"), _message("m2", 2, "two")]),
        orjson.dumps([_message("m1", 1, "one"), _message("m2", 2, "updated"), _message("m3", 3, "three")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        body = payloads[calls]
        calls += 1
        return httpx.Response(200, content=body)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        second = await client.get(f"/slimapi/messages/s1?since={token}")

    assert first.status_code == 200
    assert set(first.json()) == {"items", "nextCursor", "nextSince"}
    assert second.status_code == 200
    assert [item["info"]["id"] for item in second.json()["items"]] == ["m2", "m3"]
    assert second.json()["removed"] == []
    assert "nextSince" in second.json()
    assert calls == 2


async def test_exhausted_snapshot_reports_removed_ids_but_truncated_snapshot_does_not():
    payloads = [
        orjson.dumps([_message("m1", 1, "one"), _message("m2", 2, "two")]),
        orjson.dumps([_message("m2", 2, "two")]),
    ]
    links = [None, '</session/s1/message?before=opaque>; rel="next"']
    calls = 0

    def handler(request):
        nonlocal calls
        index = min(calls, len(payloads) - 1)
        calls += 1
        headers = {} if links[index] is None else {"Link": links[index]}
        return httpx.Response(200, content=payloads[index], headers=headers)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        truncated = await client.get(f"/slimapi/messages/s1?since={token}")

    assert truncated.json()["removed"] == []
    assert "nextSince" in truncated.json()

    # Repeat with an exhausted second page: the same cached m1 is now known
    # to have fallen out of a complete current snapshot.
    calls = 0
    links[1] = None
    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        exhausted = await client.get(f"/slimapi/messages/s1?since={token}")

    assert exhausted.json()["removed"] == ["m1"]


async def test_empty_non_exhausted_projection_does_not_infer_removals():
    payloads = [orjson.dumps([_message("m1", 1, "one")]), orjson.dumps([])]
    calls = 0

    def handler(request):
        nonlocal calls
        body = payloads[min(calls, 1)]
        calls += 1
        return httpx.Response(
            200,
            content=body,
            headers={"Link": '</session/s1/message?before=opaque>; rel="next"'}
            if calls == 2 else {},
        )

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )

    assert second.json()["items"] == []
    assert second.json()["removed"] == []


async def test_before_response_never_signs_next_since():
    def handler(request):
        return httpx.Response(200, content=orjson.dumps([_message("m1", 1, "one")]))

    async with _message_client(handler) as (client, _app):
        response = await client.get("/slimapi/messages/s1?before=cursor")

    assert response.status_code == 200
    assert "nextSince" not in response.json()


async def test_duplicate_since_and_before_values_are_invalid():
    def handler(request):
        return httpx.Response(200, content=b"[]")

    async with _message_client(handler) as (client, _app):
        for query in (
            "since=a&since=b",
            "before=a&before=b",
        ):
            response = await client.get(f"/slimapi/messages/s1?{query}")
            assert response.status_code == 400
            assert response.json() == {"code": "invalid_params"}


async def test_token_shape_errors_are_400_but_epoch_stale_and_axis_mismatch_reset():
    payloads = [
        orjson.dumps([_message("m1", 1, "one")]),
        orjson.dumps([_message("m1", 1, "two")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        body = payloads[min(calls, len(payloads) - 1)]
        calls += 1
        return httpx.Response(200, content=body)

    async with _message_client(handler) as (client, app):
        for token in ("not-base64", "A" * 513):
            response = await client.get(f"/slimapi/messages/s1?since={token}")
            assert response.status_code == 400
            assert response.json() == {"code": "invalid_params"}

        unsupported = base64.urlsafe_b64encode(
            orjson.dumps({"v": 2, "epoch": "test-epoch", "sid": "s1", "cq_hash": "v1:40::baseline", "gen": 1})
        ).rstrip(b"=").decode()
        response = await client.get(f"/slimapi/messages/s1?since={unsupported}")
        assert response.status_code == 400

        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        decoded = orjson.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        decoded["epoch"] = "other-process"
        epoch_token = base64.urlsafe_b64encode(orjson.dumps(decoded)).rstrip(b"=").decode()
        reset = await client.get(f"/slimapi/messages/s1?since={epoch_token}")
        assert reset.status_code == 200
        assert len(reset.json()["items"]) == 1
        assert "nextSince" in reset.json()
        # Gate-MAJOR-1 (§10.3): an epoch-mismatch reset is a full
        # projection — the ``removed`` key must be ABSENT (not ``[]``).
        assert "removed" not in reset.json()

        # v6.1 adjudication (2026-08-22): a cq_hash mismatch — the
        # limit/directory/mode query axis changed — is format-valid but
        # semantically stale, so it resets to the full projection with a
        # freshly issued nextSince instead of 400ing.
        decoded["epoch"] = "test-epoch"
        decoded["cq_hash"] = "v1:41::baseline"
        mismatched = base64.urlsafe_b64encode(orjson.dumps(decoded)).rstrip(b"=").decode()
        cq_reset = await client.get(f"/slimapi/messages/s1?since={mismatched}")
        assert cq_reset.status_code == 200
        assert len(cq_reset.json()["items"]) == 1
        assert "nextSince" in cq_reset.json()
        # Gate-MAJOR-1 (§10.3): reset responses never carry ``removed``.
        assert "removed" not in cq_reset.json()

        # The §3.6 checklist's "limit→reset" row, same v6.1 rule: the token
        # was minted under a different query axis (limit=40) than the
        # request (limit=41).
        limit_changed = await client.get(
            f"/slimapi/messages/s1?limit=41&since={token}"
        )
        assert limit_changed.status_code == 200
        assert len(limit_changed.json()["items"]) == 1
        assert "nextSince" in limit_changed.json()
        assert "removed" not in limit_changed.json()

        # A sid mismatch stays a hard 400 under v6.1: the token names a
        # different session, which is not a stale-query-axis case.
        decoded["cq_hash"] = "v1:40::baseline"
        decoded["sid"] = "s2"
        other_sid = base64.urlsafe_b64encode(orjson.dumps(decoded)).rstrip(b"=").decode()
        invalid_sid = await client.get(f"/slimapi/messages/s1?since={other_sid}")
        assert invalid_sid.status_code == 400
        assert invalid_sid.json() == {"code": "invalid_params"}

        # The original generation is no longer current after the reset.
        stale = await client.get(f"/slimapi/messages/s1?since={token}")
        assert stale.status_code == 200
        assert len(stale.json()["items"]) == 1


async def test_no_since_body_is_legacy_envelope_plus_next_since_only():
    payload = orjson.dumps([_message("m1", 1, "one")])

    def handler(request):
        return httpx.Response(200, content=payload)

    async with _message_client(handler, enabled=False) as (client, _app):
        uncached = await client.get("/slimapi/messages/s1")

    async with _message_client(handler) as (client, _app):
        cached = await client.get("/slimapi/messages/s1")

    assert uncached.status_code == cached.status_code == 200
    assert cached.json().keys() >= {"nextSince"}
    assert {
        key: value for key, value in cached.json().items() if key != "nextSince"
    } == uncached.json()
    assert cached.content == (
        uncached.content[:-1]
        + b',"nextSince":'
        + orjson.dumps(cached.json()["nextSince"])
        + b"}"
    )


async def test_same_token_concurrent_differing_loser_omits_next_since_and_does_not_rollback(
    monkeypatch,
):
    initial = orjson.dumps([_message("m1", 1, "one")])
    variants = [
        orjson.dumps([_message("m1", 1, "one"), _message("m2", 2, "winner-a")]),
        orjson.dumps([_message("m1", 1, "one"), _message("m2", 2, "winner-b")]),
    ]
    calls = 0
    waiting = 0
    release = asyncio.Event()
    winner_body = None

    def handler(request):
        return httpx.Response(200, content=initial)

    async def fake_stream(request, path, params, directory):
        nonlocal calls, waiting
        calls += 1
        if calls == 1:
            return httpx.Response(200, content=initial)
        if calls in (2, 3):
            index = calls - 2
            waiting += 1
            if waiting == 2:
                release.set()
            await release.wait()
            return httpx.Response(200, content=variants[index])
        return httpx.Response(200, content=winner_body)

    monkeypatch.setattr(messages_list, "_stream_upstream", fake_stream)

    async with _message_client(
        handler, settings_options={"max_transforms": 2}
    ) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        responses = await asyncio.gather(
            client.get(f"/slimapi/messages/s1?since={token}"),
            client.get(f"/slimapi/messages/s1?since={token}"),
        )

        winners = [response for response in responses if "nextSince" in response.json()]
        losers = [response for response in responses if "nextSince" not in response.json()]
        assert len(winners) == len(losers) == 1
        winner = winners[0]
        loser = losers[0]
        winner_text = winner.json()["items"][0]["parts"][0]["text"]
        stable = orjson.dumps(
            [_message("m1", 1, "one"), _message("m2", 2, winner_text)]
        )

        # The winning lineage remains current; a retry with its token must
        # diff against that snapshot rather than reset to the loser's bytes.
        winner_body = stable
        retry = await client.get(f"/slimapi/messages/s1?since={winner.json()['nextSince']}")

    assert all(response.status_code == 200 for response in responses)
    assert loser.json()["items"]
    assert retry.status_code == 200
    assert retry.json()["items"] == []
    assert retry.json()["removed"] == []
    assert "nextSince" in retry.json()


async def test_full_and_since_concurrent_requests_share_one_raw_fetch():
    old = orjson.dumps([_message("m1", 1, "one")])
    new = orjson.dumps([_message("m1", 1, "one"), _message("m2", 2, "two")])
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, content=old)
        started.set()
        await release.wait()
        return httpx.Response(200, content=new)

    async with _message_client(handler, raw_registry=True) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        full_task = asyncio.create_task(client.get("/slimapi/messages/s1"))
        since_task = asyncio.create_task(
            client.get(f"/slimapi/messages/s1?since={token}")
        )
        await started.wait()
        await asyncio.sleep(0)
        release.set()
        full, since = await asyncio.gather(full_task, since_task)

    assert calls == 2
    assert full.status_code == since.status_code == 200
    assert "nextSince" in full.json()
    assert [item["info"]["id"] for item in since.json()["items"]] == ["m2"]
    assert "nextSince" in since.json()


async def test_merged_mode_diff_uses_post_merge_projection(monkeypatch):
    # The production full-fetch registry intentionally retains successful
    # results briefly.  This scenario needs the second merged request to
    # fetch a fresh full projection so the since diff observes its change.
    monkeypatch.setattr(fulls, "_grace", 0)
    list_calls = 0

    def placeholder(text=""):
        return {
            "info": {"id": "m1", "role": "user", "time": {"created": 1}},
            "parts": [
                {"id": "p1", "type": "text", "messageID": "m1", "text": text}
            ],
        }

    async def handler(request):
        nonlocal list_calls
        if request.url.path == "/session/s1/message":
            list_calls += 1
            return httpx.Response(200, content=orjson.dumps([placeholder()]))
        if request.url.path == "/session/s1/message/m1":
            text = "old" if list_calls == 1 else "new"
            return httpx.Response(200, content=orjson.dumps(placeholder(text)))
        raise AssertionError(f"unexpected upstream path {request.url.path}")

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1?mode=merged")
        await asyncio.sleep(0)
        second = await client.get(
            f"/slimapi/messages/s1?mode=merged&since={first.json()['nextSince']}"
        )

    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["parts"][0]["text"] == "old"
    assert second.json()["items"][0]["parts"][0]["text"] == "new"
    assert second.json()["removed"] == []


async def test_same_timestamp_boundary_does_not_infer_sibling_removal():
    payloads = [
        orjson.dumps([_message("m1", 10, "one"), _message("m2", 10, "two")]),
        orjson.dumps([_message("m2", 10, "two")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        body = payloads[min(calls, 1)]
        calls += 1
        headers = (
            {"Link": '</session/s1/message?before=opaque>; rel="next"'}
            if calls == 2 else {}
        )
        return httpx.Response(200, content=body, headers=headers)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )

    assert second.json()["removed"] == []


async def test_evicted_since_token_resets_to_full_snapshot():
    payload = orjson.dumps([_message("m1", 1, "one")])

    def handler(request):
        return httpx.Response(200, content=payload)

    async with _message_client(handler, max_entries=1) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        await client.get("/slimapi/messages/s2")
        reset = await client.get(f"/slimapi/messages/s1?since={token}")

    assert reset.status_code == 200
    assert [item["info"]["id"] for item in reset.json()["items"]] == ["m1"]
    assert "nextSince" in reset.json()
    # Gate-MAJOR-1 (§10.3): LRU-eviction reset is a full projection — no
    # ``removed`` key.
    assert "removed" not in reset.json()


async def test_bypass_removes_old_lineage_and_retry_resets():
    payloads = [
        orjson.dumps([_message("m1", 1, "one")]),
        orjson.dumps([_message("m1", 1, "one"), _message("m2", 2, "two")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        body = payloads[min(calls, len(payloads) - 1)]
        calls += 1
        return httpx.Response(200, content=body)

    async with _message_client(handler) as (client, app):
        first = await client.get("/slimapi/messages/s1")
        token = first.json()["nextSince"]
        app.state.since_cache.max_entry_bytes = 1
        bypass = await client.get(f"/slimapi/messages/s1?since={token}")
        retry = await client.get(f"/slimapi/messages/s1?since={token}")

    assert bypass.status_code == retry.status_code == 200
    assert [item["info"]["id"] for item in bypass.json()["items"]] == ["m2"]
    assert "nextSince" not in bypass.json()
    # The bypass response itself IS a diff response (valid baseline): the
    # ``removed`` key stays present-as-empty; only ``nextSince`` is
    # withheld (byte-cap omission).
    assert bypass.json()["removed"] == []
    assert [item["info"]["id"] for item in retry.json()["items"]] == ["m1", "m2"]
    assert "nextSince" not in retry.json()
    # Gate-MAJOR-1 (§10.3): the retry is reset-shaped — its lineage was
    # dropped by the byte-cap bypass, so it re-fetches the full snapshot
    # with NO ``removed`` key.
    assert "removed" not in retry.json()


# ---------------------------------------------------------------------------
# BE-001: degenerate ``info.time.created`` rows must never false-positive the
# removed inference. ``_created_sort_key``'s ``0`` sentinel (which floats
# malformed rows to the page head — intentional §8 surface) overloaded
# "malformed" with a legal sort position, so the historical
# ``_boundary_key`` minted ``(0, deg_mid)`` and judged every absent baseline
# mid strictly newer — reporting the whole window as removed. The fix makes
# boundary validity explicit (``_created_value`` → None) while a legitimate
# ``created == 0`` row still compares normally.
# ---------------------------------------------------------------------------

_MISSING = object()  # sentinel: the ``time`` object carries no "created" key


def _degenerate_message(mid: str, created) -> dict:
    time_obj = {} if created is _MISSING else {"created": created}
    return {
        "info": {"id": mid, "role": "user", "time": time_obj},
        "parts": [
            {"id": f"{mid}-part", "type": "text", "messageID": mid, "text": "bad"}
        ],
    }


def test_created_value_boundary_and_sort_key_classification():
    # Well-formed values — including the legitimate epoch 0, which must
    # NEVER be conflated with the malformed sentinel.
    valid = {"info": {"id": "m", "time": {"created": 0}}}
    assert messages_list._created_value(valid) == 0
    assert messages_list._created_sort_key(valid) == 0
    assert messages_list._boundary_key(valid, "m") == (0, "m")
    fractional = {"info": {"id": "m", "time": {"created": 5.5}}}
    assert messages_list._created_value(fractional) == 5.5
    assert messages_list._boundary_key(fractional, "m") == (5.5, "m")

    # Degenerate variants (Q7-P3-19 malformed set): missing / non-dict
    # traversal legs, string, bool, null, NaN, +Inf, -Inf.
    degenerate_createds = [
        _MISSING,
        "not-a-number",
        True,
        None,
        float("nan"),
        float("inf"),
        float("-inf"),
    ]
    for created in degenerate_createds:
        item = _degenerate_message("m", created)
        assert messages_list._created_value(item) is None
        # Sort keeps the float-to-head §8 invariant (sentinel 0)…
        assert messages_list._created_sort_key(item) == 0
        # …but the row refuses to serve as a diff boundary.
        assert messages_list._boundary_key(item, "m") is None

    # Missing mid alone also mints no boundary.
    assert messages_list._boundary_key(valid, None) is None
    # Non-dict traversal legs stay swallowed exactly as before.
    for broken in ({}, {"info": "not-a-dict"}, {"info": {"time": "x"}}):
        assert messages_list._created_value(broken) is None
        assert messages_list._boundary_key(broken, "m") is None


async def test_created_zero_row_serves_as_diff_boundary():
    # A legitimate ``created == 0`` row is a VALID boundary: an absent
    # baseline mid strictly newer than it in a non-exhausted window is a
    # true removal (not suppressed by the BE-001 fix).
    payloads = [
        orjson.dumps([
            _message("m0", 0, "zero"),
            _message("m1", 5, "five"),
            _message("m2", 10, "ten"),
        ]),
        orjson.dumps([_message("m0", 0, "zero"), _message("m2", 10, "ten")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        index = min(calls, len(payloads) - 1)
        calls += 1
        headers = (
            {"Link": '</session/s1/message?before=opaque>; rel="next"'}
            if index == 1 else {}
        )
        return httpx.Response(200, content=payloads[index], headers=headers)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )

    assert second.status_code == 200
    assert second.json()["items"] == []
    assert second.json()["removed"] == ["m1"]


@pytest.mark.parametrize(
    "label,created",
    [
        ("missing", _MISSING),
        ("string", "not-a-number"),
        ("bool", True),
        ("null", None),
    ],
    ids=["missing-created", "string-created", "bool-created", "null-created"],
)
async def test_degenerate_fresh_boundary_suppresses_removal_inference(
    label, created
):
    # Non-exhausted window whose oldest (page-head) row is degenerate: the
    # pre-fix code minted the (0, "mdeg") sentinel boundary and reported the
    # absent m1 as removed — a §10.3 contract violation. The window must
    # instead infer NOTHING (conservative false-negative path), and the
    # degenerate row must keep sorting to the response items head (§8).
    payloads = [
        orjson.dumps([_message("m1", 5, "five"), _message("m2", 10, "ten")]),
        orjson.dumps([_degenerate_message("mdeg", created),
                      _message("m2", 10, "ten")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        index = min(calls, len(payloads) - 1)
        calls += 1
        headers = (
            {"Link": '</session/s1/message?before=opaque>; rel="next"'}
            if index == 1 else {}
        )
        return httpx.Response(200, content=payloads[index], headers=headers)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )

    assert second.status_code == 200
    assert second.json()["nextCursor"] is not None
    # Degenerate row floats to the items head — §8 sort invariant untouched.
    assert [item["info"]["id"] for item in second.json()["items"]] == ["mdeg"]
    # BE-001 core assertion: no removed false positives from this window.
    assert second.json()["removed"] == []
    assert "nextSince" in second.json()


async def test_degenerate_baseline_row_is_never_reported_removed():
    # A degenerate row in the BASELINE has no comparable boundary key, so
    # its absence from the fresh window must be conservatively skipped —
    # never inferred as a removal.
    payloads = [
        orjson.dumps([
            _degenerate_message("m1", "not-a-number"),
            _message("m2", 10, "ten"),
        ]),
        orjson.dumps([_message("m2", 10, "ten"), _message("m3", 20, "twenty")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        index = min(calls, len(payloads) - 1)
        calls += 1
        headers = (
            {"Link": '</session/s1/message?before=opaque>; rel="next"'}
            if index == 1 else {}
        )
        return httpx.Response(200, content=payloads[index], headers=headers)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )

    assert second.status_code == 200
    assert second.json()["removed"] == []


async def test_exhausted_window_still_reports_removals_despite_degenerate_row():
    # ``window_exhausted`` is authoritative (nextCursor is None): even with
    # a degenerate row present, the exhausted branch keeps reporting every
    # absent baseline mid — the BE-001 fix only guards the
    # boundary-inference branch, never the exhausted path.
    payloads = [
        orjson.dumps([_message("m1", 5, "five"), _message("m2", 10, "ten")]),
        orjson.dumps([_degenerate_message("mdeg", "not-a-number"),
                      _message("m2", 10, "ten")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        index = min(calls, len(payloads) - 1)
        calls += 1
        return httpx.Response(200, content=payloads[index])

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )

    assert second.status_code == 200
    assert second.json()["nextCursor"] is None
    assert [item["info"]["id"] for item in second.json()["items"]] == ["mdeg"]
    assert second.json()["removed"] == ["m1"]


async def test_same_timestamp_boundary_reports_strictly_newer_id():
    # Companion to ``test_same_timestamp_boundary_does_not_infer_sibling_removal``:
    # ties compare strictly on ``(created, id)``. An absent baseline mid
    # sharing the boundary's created but with a strictly GREATER id must
    # still be reported — the BE-001 fix must not weaken tuple comparison.
    payloads = [
        orjson.dumps([
            _message("m9", 2, "seed"),
            _message("mB", 10, "same-ts-b"),
            _message("mC", 10, "same-ts-c"),
        ]),
        orjson.dumps([_message("m9", 2, "seed"), _message("mB", 10, "same-ts-b")]),
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        index = min(calls, len(payloads) - 1)
        calls += 1
        headers = (
            {"Link": '</session/s1/message?before=opaque>; rel="next"'}
            if index == 1 else {}
        )
        return httpx.Response(200, content=payloads[index], headers=headers)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )

    assert second.status_code == 200
    assert second.json()["removed"] == ["mC"]


async def test_degenerate_window_still_signs_and_reuses_next_since():
    # nextSince/CAS behaviour is untouched by degenerate rows: the diff
    # response still signs a token, and a byte-identical retry takes the
    # CAS success (gen reuse) branch and signs again.
    fresh_payload = orjson.dumps([
        _degenerate_message("mdeg", "not-a-number"),
        _message("m2", 10, "ten"),
    ])
    payloads = [
        orjson.dumps([_message("m1", 5, "five"), _message("m2", 10, "ten")]),
        fresh_payload,
        fresh_payload,
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        index = min(calls, len(payloads) - 1)
        calls += 1
        headers = (
            {"Link": '</session/s1/message?before=opaque>; rel="next"'}
            if index >= 1 else {}
        )
        return httpx.Response(200, content=payloads[index], headers=headers)

    async with _message_client(handler) as (client, _app):
        first = await client.get("/slimapi/messages/s1")
        second = await client.get(
            f"/slimapi/messages/s1?since={first.json()['nextSince']}"
        )
        assert "nextSince" in second.json()
        third = await client.get(
            f"/slimapi/messages/s1?since={second.json()['nextSince']}"
        )

    assert third.status_code == 200
    # Valid baseline (no reset): the diff envelope carries ``removed``.
    assert "removed" in third.json()
    assert third.json()["removed"] == []
    assert third.json()["items"] == []
    assert "nextSince" in third.json()
