"""L2-CD-2: ``GET /slimapi/messages/{sid}?mode=merged`` server-side merge.

Locks the five plan acceptance criteria (Task L2-CD-2, oracle §C-1/§C-2):

* **CD2-C1 inline** — a page containing a skeleton-collapsed message (its
  projection carries the ``thin_placeholder_{mid}`` part) is served with that
  message's parts replaced by the FULL projection (diagnostics stripped,
  nothing omitted); every other message stays byte-identical to the default
  skeleton mode; ``X-Next-Cursor`` is unchanged.
* **CD2-C2 page cap** — more placeholder messages than
  ``merged_max_fulls_per_page`` (16) → the first 16 are inlined, the rest
  keep their skeleton projection (progressive degrade, never 413 / error).
  Over-``merged_max_bytes`` items likewise stay skeleton (byte-budget
  degrade), and a per-item upstream failure degrades only that item.
* **CD2-C3 unknown mode ignored** — only the literal ``merged`` activates
  the merge; ``mode=full`` and unknown values behave EXACTLY like the
  default (byte-identical body, no fan-out at all). No 400 (oracle §C-1).
* **CD2-C4 shared flight** — a merged internal fetch and a concurrent
  direct ``/full`` for the same mid share ONE upstream GET via
  ``singleflight.fulls``.
* **CD2-C5 no starvation** — the merged fan-out does NOT hold per-full
  transform-pool slots (oracle §C-2): while a merged fan-out fetch is parked
  mid-flight, a concurrent direct ``/full`` for a different mid completes
  immediately instead of exhausting its admission budget.

Settings are pinned explicitly (wait=2.0, budget=2.5, max_transforms=1,
merged 16/8/8MiB) rather than read from env, so the assertions are isolated
from the developer's environment.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import orjson
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.proxy import install_proxy
from oc_slimapi.routes import messages
from oc_slimapi.transform import TransformConfig, TransformPool

HDR = {"X-Slimapi-Version": "2"}

# opencode advertises the next page via Link; the sidecar surfaces it as
# X-Next-Cursor (opaque passthrough) — merged must not change it (CD2-C1).
LIST_LINK = (
    '<http://127.0.0.1:4096/session/s1/message?before=CURSOR123&limit=40>; '
    'rel="next"'
)

# A skeleton-collapsed upstream message: the ONLY part is a text part with
# empty text → non-renderable → skeleton_message appends the
# thin_placeholder marker → merged must inline the full projection.
MSG_PLACEHOLDER = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {"id": "p_empty", "type": "text", "messageID": "msg_1", "text": ""},
    ],
}

# A renderable message — must stay byte-identical between merged and default.
MSG_PLAIN = {
    "info": {"id": "msg_2", "role": "user",
             "time": {"created": 2000, "updated": 2000}},
    "parts": [
        {"id": "p_plain", "type": "text", "messageID": "msg_2",
         "text": "plain"},
    ],
}

# Full body for msg_1: tool part carries the never-consumed LSP
# ``state.metadata.diagnostics`` map (stripped by the merge) plus sibling
# metadata keys that MUST survive, and a second text part.
FULL_MSG_1 = {
    "info": {"id": "msg_1", "role": "user",
             "time": {"created": 1000, "updated": 1000}},
    "parts": [
        {
            "id": "part_tool", "type": "tool", "messageID": "msg_1",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "ls"},
                "metadata": {
                    "diagnostics": {"lang": "ts", "items": [{"msg": "x"}]},
                    "cursor": 7,
                },
                "output": "file1\nfile2",
            },
        },
        {"id": "part_text", "type": "text", "messageID": "msg_1",
         "text": "hello full"},
    ],
}

# What merged must inline for msg_1: FULL parts with ONLY the diagnostics
# key removed (``cursor`` sibling survives, ``omitted`` never appears).
FULL_MSG_1_PARTS_STRIPPED = [
    {
        "id": "part_tool", "type": "tool", "messageID": "msg_1",
        "tool": "bash",
        "state": {
            "status": "completed",
            "input": {"command": "ls"},
            "metadata": {"cursor": 7},
            "output": "file1\nfile2",
        },
    },
    {"id": "part_text", "type": "text", "messageID": "msg_1",
     "text": "hello full"},
]


def _ph_message(mid: str, created: int) -> dict:
    """Upstream message whose projection collapses to a thin placeholder
    (single non-renderable empty-text part)."""
    return {
        "info": {"id": mid, "role": "user",
                 "time": {"created": created, "updated": created}},
        "parts": [{"id": f"p_{mid}", "type": "text", "messageID": mid,
                   "text": ""}],
    }


def _settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=2.0,
        transform_absorb_budget_seconds=2.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
        merged_fanout=8,
        merged_max_fulls_per_page=16,
        merged_max_bytes=8 * 1024 * 1024,
    )
    base.update(overrides)
    return Settings(**base)


def _build_app(upstream: httpx.AsyncClient, settings: Settings) -> FastAPI:
    """Fresh app: version middleware → messages router → catch-all proxy →
    coded-exception handlers. Mirrors the other route test modules (no
    module-level lifespan, no smoke probe)."""
    app = FastAPI(title="oc-slimapi-cd2-test")
    app.state.config = settings
    app.state.upstream = upstream
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(messages.router)
    install_proxy(app)
    register_error_handlers(app)
    return app


@asynccontextmanager
async def _test_client(upstream_factory, handler, **overrides):
    """Build (mock upstream → fresh app → ASGI client) with teardown.

    Each test gets its own app, hence its own TransformPool — which also
    namespaces the single-flight keys (the key embeds the pool identity),
    so no state leaks between tests through the process-level registry.
    """
    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings(**overrides))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0,
    ) as client:
        try:
            yield client
        finally:
            app.state.transforms.shutdown()


def _list_response(items: list[dict], *, link: str | None = LIST_LINK):
    return httpx.Response(
        200, content=orjson.dumps(items),
        headers={"Content-Type": "application/json",
                 **({"Link": link} if link else {})},
    )


# ---------------------------------------------------------------------------
# CD2-C1: placeholder message inlined as full; rest byte-identical.
# ---------------------------------------------------------------------------

async def test_merged_inlines_full_for_placeholder(upstream_factory):
    calls: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_PLACEHOLDER, MSG_PLAIN])
        if path == "/session/s1/message/msg_1":
            calls["full"] = calls.get("full", 0) + 1
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_1))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(upstream_factory, handler) as client:
        merged = await client.get(
            "/slimapi/messages/s1?mode=merged", headers=HDR,
        )
        plain = await client.get("/slimapi/messages/s1", headers=HDR)

    assert merged.status_code == plain.status_code == 200
    # nextCursor passthrough is unchanged by the merge (v3 envelope).
    assert merged.json()["nextCursor"] == "CURSOR123"
    assert merged.json()["nextCursor"] == plain.json()["nextCursor"]

    merged_items = merged.json()["items"]
    plain_items = plain.json()["items"]

    # Sanity: the default projection DID collapse msg_1 to a placeholder.
    assert any(
        str(p.get("id", "")).startswith("thin_placeholder_")
        for p in plain_items[0]["parts"]
    )

    # msg_1 → full projection: exactly the stripped full parts, nothing
    # omitted, diagnostics gone (sibling metadata key survives).
    assert merged_items[0]["parts"] == FULL_MSG_1_PARTS_STRIPPED
    tool_part = next(
        p for p in merged_items[0]["parts"] if p["id"] == "part_tool"
    )
    assert tool_part.get("omitted") is None  # full projection omits nothing
    assert b"diagnostics" not in orjson.dumps(merged_items[0])
    # The inlined message keeps the LIST info (ordering key unchanged).
    assert merged_items[0]["info"] == plain_items[0]["info"]

    # msg_2 (no placeholder) is byte-identical to the default projection.
    assert merged_items[1] == plain_items[1]


# ---------------------------------------------------------------------------
# CD2-C2: progressive degrade — page cap, byte budget, per-item failure.
# ---------------------------------------------------------------------------

async def test_merged_degrades_beyond_page_cap(upstream_factory):
    """20 placeholder messages, merged_max_fulls_per_page=16 → exactly 16
    upstream full GETs; items 0..15 inlined, 16..19 keep the skeleton
    placeholder (no 413, no error).

    ``max_message_bytes`` is pinned small (256 KiB ≤ 8 MiB / 16) so the
    per-item fetch reservations cannot exhaust ``merged_max_bytes`` before
    all 16 items start — isolating the PAGE-CAP criterion from the byte
    budget (rev-fix 2 made merged_max_bytes a true fetch budget).
    """
    items = [_ph_message(f"msg_{i}", created=i) for i in range(20)]
    full_calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_1))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler, max_message_bytes=256 * 1024,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    assert full_calls["n"] == 16  # page cap, not 20
    body = r.json()["items"]
    # First 16 (sorted order) inlined: full tool part present, no
    # placeholder marker part.
    for i in range(16):
        assert body[i]["parts"] == FULL_MSG_1_PARTS_STRIPPED, i
    # The rest keep their skeleton projection (placeholder part intact).
    for i in range(16, 20):
        assert any(
            str(p.get("id", "")).startswith("thin_placeholder_")
            for p in body[i]["parts"]
        ), i


async def test_merged_over_byte_budget_keeps_skeleton(upstream_factory):
    """A full body whose bytes would exceed merged_max_bytes stays skeleton
    (progressive byte-budget degrade; the fetch itself is not an error)."""
    full_calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_PLACEHOLDER], link=None)
        if path == "/session/s1/message/msg_1":
            full_calls["n"] += 1
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_1))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler, merged_max_bytes=10,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    assert full_calls["n"] == 1  # fetch happened; only the splice was skipped
    items = r.json()["items"]
    assert any(
        str(p.get("id", "")).startswith("thin_placeholder_")
        for p in items[0]["parts"]
    )


async def test_merged_item_fetch_failure_degrades_to_skeleton(upstream_factory):
    """A failing per-item full fetch (upstream 500) degrades ONLY that item
    to its skeleton projection — the rest of the page still merges, and the
    overall response stays 200."""
    bad = _ph_message("msg_bad", created=1)
    good = _ph_message("msg_good", created=2)

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([bad, good], link=None)
        if path == "/session/s1/message/msg_bad":
            return httpx.Response(500, content=b"boom")
        if path == "/session/s1/message/msg_good":
            full = {
                "info": good["info"],
                "parts": [{"id": "p_g", "type": "text",
                           "messageID": "msg_good", "text": "expanded"}],
            }
            return httpx.Response(200, content=orjson.dumps(full))
        raise AssertionError(f"unexpected upstream path {path}")

    # max_message_bytes pinned small so BOTH items' fetch reservations fit
    # the 8 MiB merged budget concurrently (the failure must come from the
    # upstream 500, not from budget starvation — rev-fix 2).
    async with _test_client(
        upstream_factory, handler, max_message_bytes=2 * 1024 * 1024,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    items = r.json()["items"]
    # msg_bad: placeholder kept (client can still /full on demand).
    assert any(
        str(p.get("id", "")).startswith("thin_placeholder_")
        for p in items[0]["parts"]
    )
    # msg_good: inlined.
    assert items[1]["parts"] == [
        {"id": "p_g", "type": "text", "messageID": "msg_good",
         "text": "expanded"},
    ]


# ---------------------------------------------------------------------------
# CD2-C3: only the literal "merged" activates; unknown values ignored.
# ---------------------------------------------------------------------------

async def test_merged_unknown_mode_ignored(upstream_factory):
    """mode=full (legacy) and unknown values behave EXACTLY like the default
    projection — byte-identical body, and NO fan-out full GETs at all even
    when the page carries a placeholder (oracle §C-1: no 400)."""
    full_calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_PLACEHOLDER, MSG_PLAIN])
        if path.startswith("/session/s1/message/"):
            full_calls["n"] += 1
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_1))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(upstream_factory, handler) as client:
        plain = await client.get("/slimapi/messages/s1", headers=HDR)
        legacy = await client.get(
            "/slimapi/messages/s1?mode=full", headers=HDR,
        )
        bogus = await client.get(
            "/slimapi/messages/s1?mode=banana", headers=HDR,
        )

    assert plain.status_code == legacy.status_code == bogus.status_code == 200
    # Rev-fix 4: byte-exact body comparison (not just .json() equality) plus
    # the key wire headers — an unknown mode must be indistinguishable from
    # the default mode down to the bytes on the wire.
    assert legacy.content == plain.content
    assert bogus.content == plain.content
    for header in ("content-type", "content-length"):
        assert legacy.headers[header] == plain.headers[header], header
        assert bogus.headers[header] == plain.headers[header], header
    # v3 envelope: the cursor lives in the body, headers are stable.
    assert legacy.json()["nextCursor"] == plain.json()["nextCursor"]
    assert bogus.json()["nextCursor"] == plain.json()["nextCursor"]
    # No mode other than the literal "merged" ever fans out.
    assert full_calls["n"] == 0


# ---------------------------------------------------------------------------
# CD2-C4: merged fetch + concurrent direct /full share one upstream GET.
# ---------------------------------------------------------------------------

async def test_merged_fetch_dedups_with_direct_full(upstream_factory):
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_PLACEHOLDER], link=None)
        if path == "/session/s1/message/msg_1":
            calls["n"] += 1
            await asyncio.sleep(0.3)  # widen the join window
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_1))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(upstream_factory, handler) as client:
        merged_task = asyncio.create_task(
            client.get("/slimapi/messages/s1?mode=merged", headers=HDR)
        )
        direct_task = asyncio.create_task(
            client.get("/slimapi/messages/s1/full/msg_1", headers=HDR)
        )
        merged, direct = await asyncio.gather(merged_task, direct_task)

    assert merged.status_code == direct.status_code == 200
    assert calls["n"] == 1  # shared flight: ONE upstream GET for both
    # The inlined parts equal the direct /full parts (same shared body).
    assert merged.json()["items"][0]["parts"] == direct.json()["parts"]


# ---------------------------------------------------------------------------
# CD2-C5: merged fan-out holds no per-full pool slot (oracle §C-2).
# ---------------------------------------------------------------------------

async def test_merged_fanout_does_not_starve_direct_full(upstream_factory):
    """While a merged fan-out fetch is parked mid-flight (gated upstream), a
    concurrent direct /full for a DIFFERENT mid must complete immediately —
    the fan-out must not hold per-full transform-pool slots. If it did (the
    max_transforms=1 default makes that fatal), the direct request would
    burn its whole 2.5s absorb budget waiting and 503."""
    gate = asyncio.Event()
    fanout_started = asyncio.Event()
    direct_full = {
        "info": {"id": "m_direct", "role": "user"},
        "parts": [{"id": "p_d", "type": "text", "messageID": "m_direct",
                   "text": "direct"}],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response([MSG_PLACEHOLDER], link=None)
        if path == "/session/s1/message/msg_1":
            fanout_started.set()
            await gate.wait()  # park the merged fan-out mid-flight
            return httpx.Response(200, content=orjson.dumps(FULL_MSG_1))
        if path == "/session/s1/message/m_direct":
            return httpx.Response(200, content=orjson.dumps(direct_full))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(upstream_factory, handler) as client:
        merged_task = asyncio.create_task(
            client.get("/slimapi/messages/s1?mode=merged", headers=HDR)
        )
        # Phase B proven started (the fan-out GET is in flight → the list
        # phase already released its admission slot).
        await asyncio.wait_for(fanout_started.wait(), timeout=5.0)

        loop = asyncio.get_running_loop()
        start = loop.time()
        direct = await client.get(
            "/slimapi/messages/s1/full/m_direct", headers=HDR,
        )
        direct_elapsed = loop.time() - start
        gate.set()  # release the parked fan-out
        merged = await merged_task

    assert direct.status_code == 200  # NOT starved into a 503
    assert direct_elapsed < 1.5  # nowhere near the 2.5s absorb budget
    assert merged.status_code == 200
    assert merged.json()["items"][0]["parts"] == FULL_MSG_1_PARTS_STRIPPED


# ---------------------------------------------------------------------------
# Rev-fix 2/4 + F-006 (batch 1): merged_max_bytes is a TRUE FETCH budget
# reserved in EQUAL SHARES — every candidate starts with its own share
# (anti-starvation, I1), the concurrent in-flight reservation peak stays
# ≤ merged_max_bytes in the strict segment (I2), and over-share bodies
# degrade to their skeleton (§4a.5, I4).
# ---------------------------------------------------------------------------

async def test_merged_budget_equal_share_all_start_and_peak_capped(
    upstream_factory,
):
    """F-006 equal-share reservation (max_message_bytes=8000,
    merged_max_bytes=10000, N=4 → share = 10000 // 4 = 2500):

    * **I1 all-start** — every candidate reserves
      ``min(8000, remaining, 2500)`` = 2500 and STARTS a fetch. The old
      first-come monopoly (A took 8000, B the last 2000, C/D zero-started
      with no request at all → 2 upstream GETs) is gone: ``full_calls ==
      4`` is the F-006 anti-starvation regression assertion.
    * **I2 peak** — all four reservations are held in flight concurrently
      and sum to 4 × 2500 = 10000 == merged_max_bytes exactly: the strict
      segment (M ≥ N) bound N × share ≤ M, with equality here.
    * **I4 degrade** — each ~6 KiB body exceeds its 2500 share → the read
      truncates at the allotted cap → the item keeps its skeleton
      placeholder (proving the read itself was capped, not a post-hoc
      filter) and the page is still 200 — §4a.5 budget degrade.

    The handler sleep holds all four budgeted reads IN FLIGHT concurrently
    (a synchronous mock would complete + refund each fetch before the next
    candidate reserves, serializing the page and hiding the peak).
    """
    items = [_ph_message(f"msg_{i}", created=i) for i in range(4)]
    full_calls = {"n": 0}
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_big", "type": "text", "messageID": "msg_x",
                   "text": "y" * 6000}],
    })
    stripped_parts = [
        {"id": "p_big", "type": "text", "messageID": "msg_x",
         "text": "y" * 6000},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            await asyncio.sleep(0.05)  # hold all four reads in flight
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=8000, merged_max_bytes=10000,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    # I1: ALL four candidates started (old code: 2 — C/D zero-started).
    assert full_calls["n"] == 4
    body = r.json()["items"]
    inlined = [
        m for m in body
        if m["parts"] == stripped_parts
    ]
    degraded = [
        m for m in body
        if any(str(p.get("id", "")).startswith("thin_placeholder_")
               for p in m["parts"])
    ]
    assert len(inlined) == 0  # every read truncated at its 2500 share (I4)
    assert len(degraded) == 4


async def test_merged_budget_equal_share_small_bodies_all_inline(
    upstream_factory,
):
    """Equal share with bodies that FIT (same 8000/10000 combo, N=4 →
    share 2500, body ≈ 1.9 KiB < 2500): every candidate starts AND
    inlines — equal shares remove the starvation without penalizing small
    bodies (old code: A inlined at an 8000 monopoly, B truncated at 2000,
    C/D never started). The sleep keeps all four reads in flight, so the
    all-inline outcome is not an artifact of serial completion + refunds."""
    items = [_ph_message(f"msg_{i}", created=i) for i in range(4)]
    full_calls = {"n": 0}
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_small", "type": "text", "messageID": "msg_x",
                   "text": "y" * 1800}],
    })
    stripped_parts = [
        {"id": "p_small", "type": "text", "messageID": "msg_x",
         "text": "y" * 1800},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            await asyncio.sleep(0.05)  # all four reads in flight at once
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=8000, merged_max_bytes=10000,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    assert full_calls["n"] == 4  # all started (I1)
    body = r.json()["items"]
    inlined = [m for m in body if m["parts"] == stripped_parts]
    degraded = [
        m for m in body
        if any(str(p.get("id", "")).startswith("thin_placeholder_")
               for p in m["parts"])
    ]
    assert len(inlined) == 4
    assert len(degraded) == 0


async def test_merged_serial_completion_no_starvation(upstream_factory):
    """I1 holds under SERIAL completion too (fanout=1, synchronous mock —
    item A completes and refunds BEFORE item B reserves): with N=2 the
    share alone (10000 // 2 = 5000 each) funds both starts, so both items
    fetch and inline regardless of completion ordering — the start
    guarantee is completion-timing-independent.

    I3 (refund bookkeeping retained): under equal shares the refund is no
    longer what enables later items WITHIN a page, but the reserve-cap /
    refund-(cap − len(body)) accounting stays correct for candidates that
    start after any completion interleaving and keeps ``remaining``
    accurate for the cumulative splice bound — so it is kept, and this
    test locks that serial completion still yields all-inline pages
    (old-code contrast: the monopoly made start rights depend on the
    completion order)."""
    items = [_ph_message(f"msg_{i}", created=i) for i in range(2)]
    full_calls = {"n": 0}
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_mid", "type": "text", "messageID": "msg_x",
                   "text": "y" * 3000}],
    })
    stripped_parts = [
        {"id": "p_mid", "type": "text", "messageID": "msg_x",
         "text": "y" * 3000},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        merged_fanout=1, max_message_bytes=8000, merged_max_bytes=10000,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    assert full_calls["n"] == 2  # both started — timing-independent (I1)
    body = r.json()["items"]
    assert len(body) == 2
    for message in body:
        assert message["parts"] == stripped_parts  # every item inlined


# ===========================================================================
# F-006 (batch 1, rev4): equal-share budget — default-param regressions,
# over-share degrade (I4), and the tiny-budget floor segment (I1).
# ===========================================================================

async def test_merged_default_params_two_candidates_both_inline(
    upstream_factory,
):
    """Default 32 MiB / 8 MiB combo (the ``_settings`` baseline), 2
    candidates with ~4 KiB bodies: BOTH inline. Under the old first-come
    reservation the first candidate took the WHOLE 8 MiB budget and the
    second degraded with NO upstream request at all — the exact F-006
    production defect (any 2-placeholder page deterministically merged to
    a single inline). share = 8 MiB // 2 = 4 MiB ≫ body: both start and
    fit. The sleep keeps both reads in flight so the second start is NOT
    an artifact of the first one's refund."""
    items = [_ph_message(f"msg_{i}", created=i) for i in range(2)]
    full_calls = {"n": 0}
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_def", "type": "text", "messageID": "msg_x",
                   "text": "y" * 4000}],
    })
    stripped_parts = [
        {"id": "p_def", "type": "text", "messageID": "msg_x",
         "text": "y" * 4000},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            await asyncio.sleep(0.05)  # both reads in flight at once
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    assert full_calls["n"] == 2  # old code: the 2nd candidate zero-started
    body = r.json()["items"]
    inlined = [m for m in body if m["parts"] == stripped_parts]
    assert len(inlined) == 2


async def test_merged_default_params_sixteen_candidates_page_cap(
    upstream_factory,
):
    """Default params × 16 candidates (~10 KiB bodies): all 16 inline. 16
    is both ``merged_max_fulls_per_page`` (the page-cap boundary — a 17th
    placeholder would stay skeleton) and the N of the strict segment
    (8 MiB ≥ 16 → share = 512 KiB each). fanout=8 serves the fetches in
    waves, but every candidate still starts and fits (old code: roughly
    one inline per completed monopoly, the rest degraded)."""
    items = [_ph_message(f"msg_{i}", created=i) for i in range(16)]
    full_calls = {"n": 0}
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_def", "type": "text", "messageID": "msg_x",
                   "text": "y" * 10000}],
    })
    stripped_parts = [
        {"id": "p_def", "type": "text", "messageID": "msg_x",
         "text": "y" * 10000},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            await asyncio.sleep(0.05)  # hold each wave's reads in flight
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(upstream_factory, handler) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    assert full_calls["n"] == 16  # every candidate started (I1 strict seg)
    body = r.json()["items"]
    assert len(body) == 16
    inlined = [m for m in body if m["parts"] == stripped_parts]
    assert len(inlined) == 16


async def test_merged_oversized_candidate_degrades_page_ok(upstream_factory):
    """I4 over-share trade-off: merged_max_bytes=6000, 2 candidates →
    share=3000. msg_big's ~3.9 KiB body exceeds its share → the read
    truncates at 3000 → msg_big keeps its skeleton; msg_small still gets
    its OWN share and inlines; the page is 200. Known cost of equal shares
    (orchestrator CHANGELOG): the old code here inlined msg_big at a 6000
    monopoly and STARVED msg_small instead — the degradation moved from
    "whoever is not first" to "whoever genuinely exceeds their share"."""
    items = [_ph_message("msg_big", created=1), _ph_message("msg_small", created=2)]
    full_calls = {"n": 0}
    big_body = orjson.dumps({
        "info": {"id": "msg_big", "role": "user"},
        "parts": [{"id": "p_big", "type": "text", "messageID": "msg_big",
                   "text": "y" * 3800}],
    })
    small_body = orjson.dumps({
        "info": {"id": "msg_small", "role": "user"},
        "parts": [{"id": "p_small", "type": "text",
                   "messageID": "msg_small", "text": "y" * 400}],
    })
    small_stripped = [
        {"id": "p_small", "type": "text", "messageID": "msg_small",
         "text": "y" * 400},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path == "/session/s1/message/msg_big":
            full_calls["n"] += 1
            await asyncio.sleep(0.05)  # both shares held in flight
            return httpx.Response(200, content=big_body)
        if path == "/session/s1/message/msg_small":
            full_calls["n"] += 1
            await asyncio.sleep(0.05)
            return httpx.Response(200, content=small_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler, merged_max_bytes=6000,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200  # degrade, never a page failure (I4)
    assert full_calls["n"] == 2  # both candidates started (I1)
    body = r.json()["items"]
    assert any(
        str(p.get("id", "")).startswith("thin_placeholder_")
        for p in body[0]["parts"]
    )  # msg_big: truncated at its 3000 share → skeleton
    assert body[1]["parts"] == small_stripped  # msg_small: inlined


async def test_merged_tiny_budget_floor_share_spread(upstream_factory):
    """I1 floor segment (M < N): merged_max_bytes=3, N=4 → share floors at
    ``max(1, 3 // 4)`` = 1. The serial-worst start count is exactly
    min(N, M) = 3: candidates msg_0..msg_2 each reserve 1 byte and start;
    msg_3 finds ``remaining == 0`` at its gate and never issues a request.
    All 4 items degrade (3 truncations at cap=1 + 1 never started), page
    200. Old code in this scenario gave the FIRST candidate the whole
    3-byte budget (full_calls == 1): the floor-segment promise is
    anti-monopoly, not all-start — M bytes cannot fund N > M positive
    shares. The handler sleep keeps the three started reads in flight so
    their (full) refunds cannot re-open the pool before msg_3's gate
    check."""
    items = [_ph_message(f"msg_{i}", created=i) for i in range(4)]
    called_mids: list[str] = []
    tiny_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_tiny", "type": "text", "messageID": "msg_x",
                   "text": "y" * 10}],
    })

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            called_mids.append(path.rsplit("/", 1)[-1])
            await asyncio.sleep(0.05)  # keep the 1-byte reads in flight
            return httpx.Response(200, content=tiny_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=8, merged_max_bytes=3,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200  # degrade, never a page failure
    # Exactly min(N, M) = 3 starts — and it is the FIRST three candidates
    # (msg_3 never requested: remaining == 0 at its gate).
    assert len(called_mids) == 3
    assert set(called_mids) == {"msg_0", "msg_1", "msg_2"}
    body = r.json()["items"]
    assert len(body) == 4
    for message in body:  # all four degraded: 3 truncations + 1 no-start
        assert any(
            str(p.get("id", "")).startswith("thin_placeholder_")
            for p in message["parts"]
        )


# ===========================================================================
# rev-fix (merged budget model round 2): explicit peak bound, direct /full
# immunity to merged-budget truncations, windfall splice.
# ===========================================================================

class _PieceStream(httpx.AsyncByteStream):
    """Serve a body as a PRE-SLICED piece list (built eagerly in __init__
    via a list comprehension — NOT lazily generated) so that
    ``aiter_bytes(chunk_size)`` re-slices it into EXACT ``chunk_size``
    reads.

    A buffered ``content=`` body arrives from the mock transport as ONE raw
    chunk regardless of ``chunk_size``, hiding the read loop's chunk
    granularity; the piece stream restores it (probe-verified: aiter_bytes
    re-buffers raw pieces and yields chunk_size slices, and an early break
    stops pulling from the transport).

    Proof scope: this fixture makes the TOTAL reported by ``read_with_cap``
    overshoot the cap by at most one chunk — i.e. it exercises the read
    loop's accounting boundary, not the request's real memory peak (which
    httpx's own re-buffering makes implementation-defined).
    """

    def __init__(self, body: bytes, piece: int = 256):
        self._pieces = [
            body[i:i + piece] for i in range(0, len(body), piece)
        ]

    async def __aiter__(self):
        for piece in self._pieces:
            yield piece


async def test_merged_read_chunk_overshoot_bounded(upstream_factory, monkeypatch):
    """rev-fix 1 (route b): the overshoot bound is EXPLICIT — and scoped.

    This test proves ONLY the merged-LED cap-read side:

    * ``read_with_cap`` checks the cap only after accumulating a whole
      chunk, so a truncated MERGED-LED flight overshoots its reservation
      by at most one chunk (injected here as 1024B via the ``read_with_cap``
      monkeypatch seam — the production default chunk is 64 KiB);
    * the incremental reservation bound for merged-LED reads is
      ``merged_max_bytes + in_flight × chunk_size``: reservations
      4 × 2500 = 10000 == ``merged_max_bytes`` (serial-point accounting,
      F-006 equal shares), each truncated flight's accumulated total
      stays within ``cap + 1024`` — observed at the read_with_cap
      boundary.

    It does NOT claim a whole-page peak: direct-led shared-flight windfalls
    (bodies read at ``max_message_bytes`` by a direct /full leader and held
    in the gather results until the splice) are outside this formula — see
    ``test_merged_windfall_from_direct_leader_excluded_at_splice`` for the
    response-level guarantee that covers them.

    F-006 equal shares (4 placeholder items, body ≈ 6049B each, budget
    10000 → share = 2500 each): ALL FOUR candidates start (I1) and every
    read truncates at its own 2500 reservation → all four items degrade.
    The handler sleeps so all four hold their reads concurrently (a
    synchronous mock would serialize them).
    """
    items = [_ph_message(f"msg_{i}", created=i) for i in range(4)]
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_big", "type": "text", "messageID": "msg_x",
                   "text": "y" * 6000}],
    })
    full_calls = {"n": 0}
    reads: list[tuple[int, int]] = []  # (max_bytes, total) per read_with_cap
    # W3-2 (F-302): this test exercises the FULL-fetch path — the chunked
    # read must reach the _full_merge submodule's namespace.
    orig_read_with_cap = messages._full_merge.read_with_cap

    async def _chunked_read(response, max_bytes, **kwargs):
        kwargs["chunk_size"] = 1024  # inject a small, observable chunk
        body, total = await orig_read_with_cap(response, max_bytes, **kwargs)
        reads.append((max_bytes, total))
        return body, total

    monkeypatch.setattr(messages._full_merge, "read_with_cap", _chunked_read)

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            await asyncio.sleep(0.05)  # all four hold their reads in flight
            return httpx.Response(200, content=_PieceStream(full_body))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=8000, merged_max_bytes=10000,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    body = r.json()["items"]
    def _has_placeholder(m: dict) -> bool:
        # skeleton_message APPENDS the thin_placeholder part after the
        # projected (non-renderable) originals — parts[0] is NOT it.
        return any(
            str(part.get("id", "")).startswith("thin_placeholder_")
            for part in m["parts"]
        )

    inlined = sum(1 for m in body if not _has_placeholder(m))
    assert full_calls["n"] == 4  # equal share: ALL candidates started (I1)
    assert inlined == 0 and len(body) == 4

    # Filter out the list read (cap = max_response_bytes 64 KiB): the full
    # fetch records are the four with cap ≤ max_message_bytes.
    full_reads = [(cap, total) for cap, total in reads if cap <= 8000]
    assert len(full_reads) == 4
    # Reservations of STARTED flights never exceed the budget:
    # 4 × share(2500) = 10000 == merged_max_bytes (I2 strict segment, peak).
    assert sum(cap for cap, _ in full_reads) == 10000
    overshoot = 0
    for cap, total in full_reads:
        assert cap == 2500  # every candidate reserved its equal share
        assert 2500 < total  # the read bailed past its reservation...
        # ...and the overshoot beyond the 2500 reservation is ≤ ONE 1024B
        # chunk (read_with_cap checks after accumulating a whole chunk).
        assert total <= 2500 + 1024
        overshoot += total - 2500
    # Composed explicit bound: peak ≤ budget + in_flight × chunk.
    assert 10000 + 4 * 1024 >= sum(cap for cap, _ in full_reads) + overshoot


async def test_direct_full_recovers_after_joined_merged_truncation(
    upstream_factory,
):
    """rev-fix 3 scenario 1: direct /full joins a merged-led flight truncated
    at the merged budget cap (2000 < body 6049 < max_message_bytes 64000) →
    the dropped-entry retry re-leads at the DIRECT cap and succeeds — 200,
    never a false 413. The merged item itself degrades to skeleton."""
    items = [_ph_message("msg_x", created=1)]
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p1", "type": "text", "messageID": "msg_x",
                   "text": "y" * 6000}],
    })
    stripped_parts = [
        {"id": "p1", "type": "text", "messageID": "msg_x", "text": "y" * 6000},
    ]
    full_calls = {"n": 0}
    served_first = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path == "/session/s1/message/msg_x":
            full_calls["n"] += 1
            if full_calls["n"] == 1:
                served_first.set()   # flight F1 (merged-led, cap 2000) parked
                await release.wait()
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=64000, merged_max_bytes=2000,
    ) as client:
        merged_task = asyncio.create_task(
            client.get("/slimapi/messages/s1?mode=merged", headers=HDR)
        )
        await asyncio.wait_for(served_first.wait(), timeout=5.0)
        direct_task = asyncio.create_task(
            client.get("/slimapi/messages/s1/full/msg_x", headers=HDR)
        )
        await asyncio.sleep(0.05)
        # Direct JOINED the in-flight small-cap flight (no extra upstream GET).
        assert full_calls["n"] == 1
        release.set()
        merged_r, direct_r = await asyncio.gather(merged_task, direct_task)

    assert direct_r.status_code == 200  # NOT a false 413
    assert direct_r.json()["parts"] == stripped_parts
    assert direct_r.json()["info"]["id"] == "msg_x"
    # The merged item degraded to its skeleton placeholder (truncation at
    # its own 2000 reservation is terminal for the merged caller).
    assert merged_r.status_code == 200
    assert any(
        str(part.get("id", "")).startswith("thin_placeholder_")
        for part in merged_r.json()["items"][0]["parts"]
    )
    # F1 (merged-led, truncated) + direct's re-lead at its full cap.
    assert full_calls["n"] == 2


async def test_direct_full_falls_back_after_consecutive_truncation_joins(
    upstream_factory, monkeypatch,
):
    """rev-fix 3 scenario 2 (unit seam): ≥3 consecutive join-truncations on
    smaller-cap flights exhaust the retry budget — direct /full then does
    ONE dedicated GET outside the flight map and succeeds (413 only when the
    body genuinely exceeds max_message_bytes). Merged semantics keep ``None``
    (budget degrade) with NO fallback fetch.

    The pathological interleaving is deterministic only at this seam: a real
    direct retry re-leads instantly (zero awaits between attempts) and beats
    any merged re-lead, so end-to-end ≥3 consecutive joins cannot be
    scheduled reliably — the stub pins the branch under test.
    """
    full_body = orjson.dumps({
        "info": {"id": "m1", "role": "user"},
        "parts": [{"id": "p1", "type": "text", "messageID": "m1",
                   "text": "y" * 6000}],
    })
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session/s1/message/m1":
            calls["n"] += 1
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {request.url.path}")

    upstream = upstream_factory(handler)
    app = _build_app(upstream, _settings(max_message_bytes=64000))

    class _AlwaysJoinedTruncated:
        """Stub flight map: every fetch 'joins' a small-cap flight that
        truncated (entry dropped) — simulating consecutive join-truncations."""

        def __init__(self, cap: int):
            self.cap = cap
            self.fetches = 0

        async def fetch(self, key, factory):
            self.fetches += 1
            raise messages._CapExceeded(self.cap)

    stub = _AlwaysJoinedTruncated(2000)
    # W3-2 (F-302): ``fulls`` is consumed by _fetch_full_shared inside the
    # _full_merge submodule — the stub must replace THAT namespace binding
    # (patching the package re-export never reaches the consumer).
    monkeypatch.setattr(messages._full_merge, "fulls", stub)
    try:
        direct = Request({"type": "http", "headers": [], "app": app})
        result = await messages._fetch_full_shared(
            direct, app.state.transforms, "s1", "m1", None, cap=None,
        )
        assert result == full_body       # dedicated fallback GET succeeded
        assert stub.fetches == 3         # retry budget exhausted first
        assert calls["n"] == 1           # fallback issued the only real GET

        merged = Request({"type": "http", "headers": [], "app": app})
        assert await messages._fetch_full_shared(
            merged, app.state.transforms, "s1", "m1", None, cap=2000,
        ) is None                          # merged: degrade, no fallback
        assert calls["n"] == 1             # still exactly one upstream GET
    finally:
        app.state.transforms.shutdown()


async def test_merged_windfall_from_direct_leader_excluded_at_splice(
    upstream_factory,
):
    """rev-fix 3 scenario 3: DIRECT leads at its full cap; the merged waiter
    JOINS and receives a windfall body (6049B) larger than the whole merged
    budget (3000B). The cumulative splice check excludes it — the merged
    response degrades that item to skeleton, so the response-level splice
    bound (≤ merged_max_bytes of inlined fulls) holds regardless of read
    chunk overshoot; the direct response is unaffected (200, full body).

    What this PROVES: pre-splice exclusion + the response-level budget.
    What it does NOT prove: any bound on the page-held peak buffer — the
    windfall body IS fully held in the gather results until the splice
    excludes it (that transient hold is precisely the layer the
    merged-LED incremental bound does not cover; see the three-layer
    scope note in ``_merge_fulls``'s docstring)."""
    items = [_ph_message("msg_x", created=1)]
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p1", "type": "text", "messageID": "msg_x",
                   "text": "y" * 6000}],
    })
    stripped_parts = [
        {"id": "p1", "type": "text", "messageID": "msg_x", "text": "y" * 6000},
    ]
    full_calls = {"n": 0}
    served_first = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path == "/session/s1/message/msg_x":
            full_calls["n"] += 1
            served_first.set()   # direct-led flight (cap 64000) parked
            await release.wait()
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=64000, merged_max_bytes=3000,
    ) as client:
        direct_task = asyncio.create_task(
            client.get("/slimapi/messages/s1/full/msg_x", headers=HDR)
        )
        await asyncio.wait_for(served_first.wait(), timeout=5.0)
        merged_task = asyncio.create_task(
            client.get("/slimapi/messages/s1?mode=merged", headers=HDR)
        )
        await asyncio.sleep(0.05)
        # Merged JOINED the direct-led flight (no independent upstream GET).
        assert full_calls["n"] == 1
        release.set()
        direct_r, merged_r = await asyncio.gather(direct_task, merged_task)

    assert direct_r.status_code == 200
    assert direct_r.json()["parts"] == stripped_parts
    assert merged_r.status_code == 200
    # Windfall body (> the whole merged budget) excluded at the splice →
    # the item keeps its skeleton placeholder; spliced bytes == 0 ≤ budget.
    assert any(
        str(part.get("id", "")).startswith("thin_placeholder_")
        for part in merged_r.json()["items"][0]["parts"]
    )
    assert full_calls["n"] == 1
