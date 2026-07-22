"""TDD DRAFT tests for Batch4 — session.created → parent cache invalidation
+ parent digest ``childrenVersion`` (contract §3 rev I + §16, X-main path).

This module is the **authoritative specification** for the upcoming Batch4
implementation in ``src/oc_slimapi/sse/hub.py``. ALL tests are wrapped in a
module-level ``pytest.mark.xfail(strict=False)`` because the implementation
does NOT exist yet:

  * ``DigestFields`` has NO ``children_version`` field / ``to_payload`` branch.
  * ``GlobalHub.publish`` has NO ``session.created`` branch and no
    ``_children_cache`` attribute.
  * ``HubRegistry`` has NO ``set_children_cache`` method (analogous to
    ``set_transforms`` @ hub.py:631).

Once fixer-bgpt lands the implementation, these tests turn from XFAIL into
XPASS; the implementer then removes the module-level ``pytestmark`` so they
become strict PASSED. rev-bgpt uses this module as the deep-review baseline.

Covers (per task spec items 1–9):

  1. 核心: ``publish(session.created, info.parentID=P)`` →
     ``children_cache.invalidate(P)`` called (generation bumped) AND
     ``hub.pending[P].children_version == generation_of(P)`` (post-bump).
  2. digest 发出: flush → subscriber receives parent P ``session.digest`` with
     ``childrenVersion=<N>``; a non-parent digest in the same window OMITS
     ``childrenVersion``.
  3. version 单调: two successive ``session.created`` (same parentID=P,
     different child sids) → ``generation_of(P)`` increments twice; the two
     emitted ``childrenVersion`` values are strictly increasing.
  4. 无 parentID (根会话): ``publish(session.created, info without parentID)``
     → no invalidate (generation unchanged), no digest with childrenVersion.
  5. children_cache 未接线: hub never had ``set_children_cache`` called →
     ``publish(session.created, parentID=P)`` must NOT crash (graceful skip).
  6. cache 驱逐集成: prime ``get_or_fetch(P, dir)`` to fill P's cache entry,
     then ``publish(session.created, parentID=P)`` → P's cache entry evicted
     AND generation bumped (next get_or_fetch is a fresh fetch + new version).
  7. ``DigestFields.to_payload``: ``children_version=None`` → payload omits
     the key; ``=N`` → payload carries ``"childrenVersion": N``.
  8. 不转发 session.created: ``publish(session.created)`` pushes NO raw
     ``session.created`` frame to the subscriber queue (curated stream
     unchanged; X-main childrenVersion is the sole signal).
  9. 既有事件不回归: ``session.updated`` archived digest and IMMEDIATE
     ``question.asked`` forwarding behave exactly as before (no
     ``childrenVersion`` leakage into non-parent digests).

Reference: ``docs/v1-contract.md`` §3 (digest ``childrenVersion?`` rev I) +
§16 (invalidate semantics / generation). Mirrors the fixture / publish
paradigm of ``tests/test_hub.py`` and ``tests/test_hub_behavior_lock.py``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import orjson
import pytest

from oc_slimapi.children_cache import ChildrenCache
from oc_slimapi.sse.hub import (
    DigestFields,
    GlobalHub,
    HubRegistry,
    STOP,
    Subscriber,
)

# ---------------------------------------------------------------------------
# Module-level xfail: the Batch4 implementation does NOT exist yet. Every test
# below exercises an API / behaviour that has not landed (children_version
# field on DigestFields, session.created branch in publish,
# HubRegistry.set_children_cache). Under strict=False an unexpectedly-passing
# test (e.g. the "no crash" / "not forwarded" guards that are vacuously true
# today because publish(session.created) is currently a no-op) is reported as
# XPASS without failing the suite. fixer-bgpt 落地后转 pass 去 mark.
# ---------------------------------------------------------------------------
# ===========================================================================
# Self-contained helpers (do NOT depend on tests/conftest.py or sibling tests)
# ===========================================================================


def ev(
    directory: str | None,
    event_type: str,
    properties: dict | None = None,
    *,
    payload_id: str | None = None,
) -> dict:
    """Build an upstream /global/event frame: {directory, payload:{type, properties[, id]}}."""
    payload: dict = {"type": event_type, "properties": properties or {}}
    if payload_id is not None:
        payload["id"] = payload_id
    return {"directory": directory, "payload": payload}


def parse(raw: bytes) -> tuple[str | None, dict]:
    """Parse one SSE frame into (event_name, data). event_name is None when
    the frame has no ``event:`` header (raw passthrough like question.asked)."""
    event_name: str | None = None
    data_lines: list[str] = []
    for line in raw.decode().split("\n"):
        if line.startswith("event: "):
            event_name = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    data = json.loads("\n".join(data_lines)) if data_lines else {}
    return event_name, data


async def drain(sub: Subscriber, timeout: float = 0.2) -> list[bytes]:
    """Drain every currently-queued frame without blocking on an empty queue."""
    out: list[bytes] = []
    while True:
        try:
            item = await asyncio.wait_for(sub.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if item is STOP:
            continue
        if isinstance(item, (bytes, bytearray)):
            out.append(bytes(item))
    return out


def only_digests(frames: list[bytes]) -> list[dict]:
    return [d for e, d in (parse(f) for f in frames) if e == "session.digest"]


class FakeUpstream:
    """Minimal ``httpx.AsyncClient`` stand-in for ``ChildrenCache`` fetches.

    ``fetch_json_mapped`` calls ``upstream.get(path, params=, headers=)`` and
    reads ``response.json()`` / ``raise_for_status()``. Routing through a real
    ``httpx.MockTransport`` keeps those behaviours identical to production.
    """

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[httpx.Request] = []
        self._transport = httpx.MockTransport(self._wrap)
        self._client = httpx.AsyncClient(
            base_url="http://127.0.0.1:4096",
            transport=self._transport,
        )

    def _wrap(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._handler(request)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def get(self, path, *, params=None, headers=None):
        return await self._client.get(path, params=params, headers=headers)

    async def aclose(self):
        await self._client.aclose()


def _ok_children(payload: list) -> httpx.Response:
    """Build a 200 response carrying a JSON list body (Session.Info[] shape)."""
    return httpx.Response(
        200,
        content=orjson.dumps(payload),
        headers={"Content-Type": "application/json"},
    )


def _make_session_info(
    sid: str, *, created: int | None = None, parent: str = "p1",
    directory: str = "/app",
) -> dict:
    info: dict[str, Any] = {"id": sid, "parentID": parent, "directory": directory}
    if created is not None:
        info["time"] = {"created": created, "updated": created}
    return info


async def _teardown_hub(hub: GlobalHub) -> None:
    """Cancel + await every GlobalHub background task (incl. stop_after_grace).

    Mirrors tests/test_hub.py:_close_hub so the 30s grace task scheduled by
    ``unsubscribe()`` does not produce 'Task was destroyed but it is pending!'
    on loop teardown.
    """
    tasks = [
        task
        for task in (hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    hub.task = None
    hub.flush_task = None
    hub.heartbeat_task = None
    hub.stop_task = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def hub():
    """Bare GlobalHub(client=None); teardown cancels background tasks."""
    h = GlobalHub(client=None)
    try:
        yield h
    finally:
        await _teardown_hub(h)


@pytest.fixture
async def fresh_hub(hub: GlobalHub):
    """GlobalHub with one manually-attached subscriber (no run() side effects).

    The cache is NOT wired (no set_children_cache) — use this for the
    "children_cache 未接线" graceful-skip test.
    """
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    return hub, subscriber


@pytest.fixture
async def hub_with_cache():
    """GlobalHub wired to a real ChildrenCache via HubRegistry.set_children_cache.

    Yields ``(hub, cache, subscriber)``. Exercises the Batch4 wiring path:
    ``registry.set_children_cache(cache)`` stores the cache reference; the hub
    obtained via ``registry.get_global()`` then has ``self._children_cache``
    available for the session.created branch.

    ``set_children_cache`` does NOT exist yet → fixture setup raises
    AttributeError → every dependent test XFAILs under the module-level mark.
    """
    upstream = FakeUpstream(lambda req: _ok_children([]))
    cache = ChildrenCache(upstream)
    registry = HubRegistry(client=None)
    registry.set_children_cache(cache)  # Batch4 API — not yet implemented
    hub = registry.get_global()
    subscriber = Subscriber()
    hub.subscribers.add(subscriber)
    try:
        yield hub, cache, subscriber
    finally:
        # Best-effort teardown; swallow exceptions from the not-yet-implemented
        # wiring path so one failing cleanup does not mask the test result.
        try:
            await registry.close()
        except Exception:
            pass
        try:
            await cache.aclose()
        except Exception:
            pass
        await _teardown_hub(hub)


# ===========================================================================
# 1. 核心: session.created invalidates parent + sets children_version
# ===========================================================================


async def test_session_created_invalidates_parent_and_sets_children_version(hub_with_cache):
    """Spec item 1: ``publish(session.created, info.parentID=P)`` must

      * call ``children_cache.invalidate(P)`` → ``generation_of(P)`` bumps, and
      * create ``hub.pending[P]`` with ``children_version == generation_of(P)``
        (the post-bump value).
    """
    hub, cache, _sub = hub_with_cache
    P = "parent-sid"

    g_before = cache.generation_of(P)
    assert g_before == 0  # unseen parent starts at generation 0

    hub.publish(ev("/app", "session.created", {
        "info": {"id": "child-1", "parentID": P, "directory": "/app"},
    }))

    g_after = cache.generation_of(P)
    assert g_after == g_before + 1, (
        f"invalidate(P) must bump generation: before={g_before} after={g_after}"
    )
    entry = hub.pending.get(P)
    assert entry is not None, "parent P must have a pending digest entry"
    assert entry.children_version == g_after, (
        f"children_version must equal post-bump generation: "
        f"got {entry.children_version}, gen={g_after}"
    )


# ===========================================================================
# 2. digest 发出: parent digest carries childrenVersion; others omit it
# ===========================================================================


async def test_digest_carries_children_version_for_parent_only(hub_with_cache):
    """Spec item 2: after flush, the parent P digest carries ``childrenVersion``
    equal to ``generation_of(P)``; a non-parent session digest emitted in the
    SAME window must OMIT the field entirely (contract §3 rev I: only parents)."""
    hub, cache, sub = hub_with_cache
    P = "parent-sid"

    hub.publish(ev("/app", "session.created", {
        "info": {"id": "child-1", "parentID": P},
    }))
    # An unrelated session event in the SAME debounce window for a non-parent.
    hub.publish(ev("/app", "session.status", {"sessionID": "other", "status": "idle"}))
    hub.flush()

    digests = {d["sessionID"]: d for d in only_digests(await drain(sub))}
    assert P in digests, "parent P must have an emitted digest"
    assert "other" in digests
    assert "childrenVersion" in digests[P], "parent digest must carry childrenVersion"
    assert digests[P]["childrenVersion"] == cache.generation_of(P)
    assert "childrenVersion" not in digests["other"], (
        "non-parent digest must NOT carry childrenVersion"
    )


# ===========================================================================
# 3. version 单调: successive session.created → strictly increasing versions
# ===========================================================================


async def test_children_version_strictly_monotonic_across_creates(hub_with_cache):
    """Spec item 3: two ``session.created`` events for the same parent P (with
    different child sids) must each bump ``generation_of(P)``; the two emitted
    ``childrenVersion`` values must be strictly increasing (monotonic generation
    per contract §16 INV-4)."""
    hub, cache, sub = hub_with_cache
    P = "parent-sid"

    hub.publish(ev("/app", "session.created", {
        "info": {"id": "child-1", "parentID": P},
    }))
    hub.flush()
    first = only_digests(await drain(sub))
    assert first, "expected a parent digest after the first session.created"
    assert "childrenVersion" in first[0]
    v1 = first[0]["childrenVersion"]

    hub.publish(ev("/app", "session.created", {
        "info": {"id": "child-2", "parentID": P},
    }))
    hub.flush()
    second = only_digests(await drain(sub))
    assert second, "expected a parent digest after the second session.created"
    assert "childrenVersion" in second[0]
    v2 = second[0]["childrenVersion"]

    assert v2 > v1, (
        f"childrenVersion must strictly increase across session.created: v1={v1} v2={v2}"
    )
    assert v2 == cache.generation_of(P)


# ===========================================================================
# 4. 无 parentID (根会话): no invalidation, no childrenVersion digest
# ===========================================================================


async def test_root_session_created_without_parent_id_is_noop(hub_with_cache):
    """Spec item 4: a ``session.created`` whose ``info`` has NO ``parentID``
    (root session) must NOT invalidate any parent, must NOT bump any
    generation, and must NOT produce any digest carrying ``childrenVersion``."""
    hub, cache, sub = hub_with_cache
    P = "parent-sid"
    g_before = cache.generation_of(P)

    hub.publish(ev("/app", "session.created", {
        # Root session: no parentID field on info.
        "info": {"id": "root-session", "directory": "/app"},
    }))
    hub.flush()

    assert cache.generation_of(P) == g_before, (
        "root session.created must not invalidate any parent generation"
    )
    digests = only_digests(await drain(sub, timeout=0.05))
    assert all("childrenVersion" not in d for d in digests), (
        f"root session.created must not produce a childrenVersion digest: {digests}"
    )


# ===========================================================================
# 4b. 空字符串 parentID (malformed): treated like root session — no-op
# ===========================================================================


async def test_empty_string_parent_id_is_noop(hub_with_cache):
    """Spec item 4b (malformed-info robustness): a ``session.created`` whose
    ``info.parentID`` is an empty string must be treated like a root session —
    NO invalidate, NO generation bump, NO digest carrying ``childrenVersion``.
    Defensive against malformed upstream events (rev-bgpt Batch4 LOW nit)."""
    hub, cache, sub = hub_with_cache
    g_before = cache.generation_of("")

    hub.publish(ev("/app", "session.created", {
        "info": {"id": "c1", "parentID": ""},
    }))
    hub.flush()

    assert cache.generation_of("") == g_before, (
        "empty-string parentID must not bump any generation"
    )
    digests = only_digests(await drain(sub, timeout=0.05))
    assert all("childrenVersion" not in d for d in digests), (
        f"empty-string parentID must not produce a childrenVersion digest: {digests}"
    )


# ===========================================================================
# 5. children_cache 未接线: publish(session.created) 不 crash
# ===========================================================================


async def test_session_created_no_crash_when_children_cache_not_wired(fresh_hub):
    """Spec item 5: a hub whose registry never called ``set_children_cache``
    (or was passed None) must gracefully skip the invalidate path on
    ``session.created`` — NO AttributeError, NO crash. No childrenVersion
    digest is produced because the cache reference is absent."""
    hub, sub = fresh_hub  # plain hub — set_children_cache never called

    # Must NOT raise even though children_cache is not wired.
    hub.publish(ev("/app", "session.created", {
        "info": {"id": "child-1", "parentID": "parent-sid"},
    }))
    hub.flush()

    digests = only_digests(await drain(sub, timeout=0.05))
    assert all("childrenVersion" not in d for d in digests), (
        f"unwired cache must not produce childrenVersion digests: {digests}"
    )


# ===========================================================================
# 6. cache 驱逐集成: publish(session.created) evicts parent cache entry
# ===========================================================================


async def test_session_created_evicts_parent_cache_entry(hub_with_cache):
    """Spec item 6: prime ``get_or_fetch(P, dir)`` to fill P's cache entry,
    then ``publish(session.created, parentID=P)``. The parent's cache entry
    MUST be evicted (``invalidate`` drops every directory under that sid per
    Batch3 semantics) AND the generation MUST be bumped — so the next
    ``get_or_fetch`` is guaranteed a fresh fetch + new version."""
    hub, cache, _sub = hub_with_cache
    P = "parent-sid"

    # Prime the cache with a children fetch for P.
    cache._upstream._handler = lambda req: _ok_children([
        _make_session_info("c-old", created=10, parent=P),
    ])
    await cache.get_or_fetch(P, "/app")
    # Sanity precondition: P now has a cached children entry.
    assert any(
        isinstance(k, tuple) and k[0] == P for k in cache._cache
    ), "precondition: P must have a cached children entry after get_or_fetch"

    hub.publish(ev("/app", "session.created", {
        "info": {"id": "child-new", "parentID": P},
    }))

    # Parent cache entry MUST have been evicted by invalidate(P).
    p_keys = [k for k in cache._cache if isinstance(k, tuple) and k[0] == P]
    assert p_keys == [], (
        f"parent cache entry not evicted by session.created: {p_keys}"
    )
    # Generation bumped → next get_or_fetch is a fresh fetch + new version.
    assert cache.generation_of(P) >= 1


# ===========================================================================
# 7. DigestFields.to_payload: children_version omit / include
# ===========================================================================


def test_digest_fields_to_payload_omits_children_version_when_none():
    """Spec item 7 (omit half): ``DigestFields`` must declare
    ``children_version: int | None = None`` (per the 被测设计 field list) and
    ``to_payload`` must NOT emit a ``childrenVersion`` key when it is None —
    mirroring the archived/lastError optional-field pattern (contract §3)."""
    # Locks the spec-mandated field declaration (default None). Today the field
    # is not declared → AttributeError → XFAIL; once Batch4 lands the default
    # makes this pass.
    assert DigestFields().children_version is None
    fields = DigestFields()
    fields.children_version = None
    payload = fields.to_payload("s1")
    assert "childrenVersion" not in payload


def test_digest_fields_to_payload_includes_children_version_when_set():
    """Spec item 7 (include half): ``DigestFields`` with ``children_version=N``
    must emit ``"childrenVersion": N`` in the payload (epoch-int, matching the
    client's ``Long?`` typing and X-Children-Version header source)."""
    fields = DigestFields()
    fields.children_version = 7
    payload = fields.to_payload("s1")
    assert payload["childrenVersion"] == 7
    assert isinstance(payload["childrenVersion"], int)


# ===========================================================================
# 8. 不转发 session.created: no raw frame pushed to subscribers
# ===========================================================================


async def test_session_created_not_forwarded_raw(hub_with_cache):
    """Spec item 8: ``session.created`` is NOT in IMMEDIATE and MUST NOT be
    forwarded raw to the curated subscriber stream. The ONLY side-effect of a
    ``session.created`` on the wire is the parent's ``childrenVersion`` digest
    (spec: 'session.created 仍不原样转发到 /slimapi/events'). Verified here
    without an explicit flush: no raw frame may appear on the queue."""
    hub, _cache, sub = hub_with_cache

    hub.publish(ev("/app", "session.created", {
        "info": {"id": "child-1", "parentID": "parent-sid"},
    }))
    # NO hub.flush() — session.created is NOT in IMMEDIATE so must not be
    # pushed raw to the queue.
    frames = await drain(sub, timeout=0.05)
    for f in frames:
        _ev_name, data = parse(f)
        assert data.get("type") != "session.created", (
            f"session.created must NOT be forwarded raw to subscribers: {f!r}"
        )


# ===========================================================================
# 9. 既有事件不回归: archived digest / question IMMEDIATE unchanged
# ===========================================================================


async def test_session_updated_archived_unchanged_no_children_version(hub_with_cache):
    """Spec item 9a (regression sample): ``session.updated`` → archived digest
    behaviour (contract §3, locked in tests/test_hub.py) must be unchanged by
    the Batch4 children_version addition. In particular a non-parent
    ``session.updated`` digest must NOT spontaneously carry ``childrenVersion``."""
    hub, _cache, sub = hub_with_cache

    hub.publish(ev("/proj", "session.updated", {
        "sessionID": "s1",
        "info": {"time": {"archived": 1700000000000}},
    }))
    hub.flush()

    digests = only_digests(await drain(sub))
    assert len(digests) == 1
    assert digests[0]["sessionID"] == "s1"
    assert digests[0]["archived"] == 1700000000000
    assert isinstance(digests[0]["archived"], int)
    # No childrenVersion leakage into a non-parent session.updated digest.
    assert "childrenVersion" not in digests[0]


async def test_question_asked_still_forwarded_immediately(hub_with_cache):
    """Spec item 9b (regression sample): IMMEDIATE forwarding of
    ``question.asked`` (no debounce, no ``event:`` header, raw passthrough)
    must be unchanged by the Batch4 session.created branch addition."""
    hub, _cache, sub = hub_with_cache

    hub.publish(ev("/proj", "question.asked", {"id": "q1", "sessionID": "s1"}))
    # NO flush — IMMEDIATE events forward without waiting for the debounce tick.
    frames = await drain(sub, timeout=0.1)
    assert len(frames) == 1
    ev_name, data = parse(frames[0])
    assert ev_name is None  # raw passthrough — no event: header
    assert data == {
        "directory": "/proj",
        "type": "question.asked",
        "properties": {"id": "q1", "sessionID": "s1"},
    }
