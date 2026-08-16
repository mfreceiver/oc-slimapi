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
    # X-Next-Cursor passthrough is unchanged by the merge.
    assert merged.headers.get("X-Next-Cursor") == "CURSOR123"
    assert merged.headers.get("X-Next-Cursor") == \
        plain.headers.get("X-Next-Cursor")

    merged_items = merged.json()
    plain_items = plain.json()

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
    body = r.json()
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
    items = r.json()
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
    items = r.json()
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
    for header in ("content-type", "content-length", "x-next-cursor"):
        assert legacy.headers[header] == plain.headers[header], header
        assert bogus.headers[header] == plain.headers[header], header
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
    assert merged.json()[0]["parts"] == direct.json()["parts"]


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
    assert merged.json()[0]["parts"] == FULL_MSG_1_PARTS_STRIPPED


# ---------------------------------------------------------------------------
# Rev-fix 2/4: merged_max_bytes is a TRUE FETCH budget — the request-level
# peak buffered by the fan-out never exceeds it.
# ---------------------------------------------------------------------------

async def test_merged_byte_budget_caps_fetch_buffers(upstream_factory):
    """Reservation model (max_message_bytes=8000, merged_max_bytes=10000):

    * item A reserves ``min(8000, 10000)`` = 8000 → its ~6 KiB body fits →
      inlined;
    * item B reserves the remaining 2000 → its 6 KiB body TRUNCATES at the
      allotted cap → degrades (proving the read itself was capped, not a
      post-hoc filter — without the cap it would have succeeded);
    * items C/D find ``remaining == 0`` at start → degraded with NO upstream
      request at all.

    Peak arithmetic: buffered ≤ 8000 (full body) + 2000 (truncated read) =
    10000 = merged_max_bytes. Exactly 2 upstream full GETs, exactly 1 item
    inlined, 3 keep their skeleton placeholder. The handler sleeps briefly
    so the two fetches are IN FLIGHT concurrently (a real network suspends
    the reader; a synchronous mock would complete item A + refund before
    item B reserves, serializing the page instead).
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
            await asyncio.sleep(0.05)  # hold both budgeted reads in flight
            return httpx.Response(200, content=full_body)
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=8000, merged_max_bytes=10000,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    # Only the two budget-allotted items hit upstream — C/D never started.
    assert full_calls["n"] == 2
    body = r.json()
    inlined = [
        m for m in body
        if m["parts"] == stripped_parts
    ]
    degraded = [
        m for m in body
        if any(str(p.get("id", "")).startswith("thin_placeholder_")
               for p in m["parts"])
    ]
    assert len(inlined) == 1  # the item that got the 8000-byte allotment
    assert len(degraded) == 3  # truncated at 2000 + never-started C/D


async def test_merged_budget_refund_lets_serial_items_proceed(upstream_factory):
    """Completed fetches REFUND their un-read reservation, so under
    fanout=1 (serial) three ~3 KiB bodies fit a 10000-byte budget even
    though the first item reserves ``min(8000, 10000)`` = 8000.

    Without the refund, pure pessimistic reservation would leave 2000 bytes
    after item 1 and truncate items 2-3 (1 fetch). With it: 8000 → refund
    5000 → 7000 → refund 4000 → 4000 — all 3 fetch (3 calls, all inlined).
    Peak stays bounded: at most ONE outstanding reservation at a time (≤
    8000 ≤ 10000)."""
    items = [_ph_message(f"msg_{i}", created=i) for i in range(3)]
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
    assert full_calls["n"] == 3  # refund kept the budget alive for all 3
    body = r.json()
    assert len(body) == 3
    for message in body:
        assert message["parts"] == stripped_parts  # every item inlined


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
      8000 + 2000 = 10000 == ``merged_max_bytes`` (serial-point
      accounting), the truncated flight's accumulated total stays within
      ``cap + 1024`` — observed at the read_with_cap boundary.

    It does NOT claim a whole-page peak: direct-led shared-flight windfalls
    (bodies read at ``max_message_bytes`` by a direct /full leader and held
    in the gather results until the splice) are outside this formula — see
    ``test_merged_windfall_from_direct_leader_excluded_at_splice`` for the
    response-level guarantee that covers them.

    4 placeholder items, body ≈ 6049B each, budget 10000: item A inlines
    (cap 8000), item B truncates at its 2000 reservation, items C/D find the
    pool empty and never fetch. The handler sleeps so A and B hold their
    reads concurrently (a synchronous mock would serialize them).
    """
    items = [_ph_message(f"msg_{i}", created=i) for i in range(4)]
    full_body = orjson.dumps({
        "info": {"id": "msg_x", "role": "user"},
        "parts": [{"id": "p_big", "type": "text", "messageID": "msg_x",
                   "text": "y" * 6000}],
    })
    full_calls = {"n": 0}
    reads: list[tuple[int, int]] = []  # (max_bytes, total) per read_with_cap
    orig_read_with_cap = messages.read_with_cap

    async def _chunked_read(response, max_bytes, **kwargs):
        kwargs["chunk_size"] = 1024  # inject a small, observable chunk
        body, total = await orig_read_with_cap(response, max_bytes, **kwargs)
        reads.append((max_bytes, total))
        return body, total

    monkeypatch.setattr(messages, "read_with_cap", _chunked_read)

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/session/s1/message":
            return _list_response(items, link=None)
        if path.startswith("/session/s1/message/msg_"):
            full_calls["n"] += 1
            await asyncio.sleep(0.05)  # A and B hold their reads concurrently
            return httpx.Response(200, content=_PieceStream(full_body))
        raise AssertionError(f"unexpected upstream path {path}")

    async with _test_client(
        upstream_factory, handler,
        max_message_bytes=8000, merged_max_bytes=10000,
    ) as client:
        r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)

    assert r.status_code == 200
    body = r.json()
    def _has_placeholder(m: dict) -> bool:
        # skeleton_message APPENDS the thin_placeholder part after the
        # projected (non-renderable) originals — parts[0] is NOT it.
        return any(
            str(part.get("id", "")).startswith("thin_placeholder_")
            for part in m["parts"]
        )

    inlined = sum(1 for m in body if not _has_placeholder(m))
    assert full_calls["n"] == 2  # A + B only; C/D never fetched
    assert inlined == 1 and len(body) == 4

    # Filter out the list read (cap = max_response_bytes 64 KiB): the full
    # fetch records are the two with cap ≤ max_message_bytes.
    full_reads = [(cap, total) for cap, total in reads if cap <= 8000]
    assert len(full_reads) == 2
    # Reservations of STARTED flights never exceed the budget.
    assert sum(cap for cap, _ in full_reads) == 10000
    by_cap = dict(full_reads)
    assert by_cap[8000] <= 8000          # untruncated flight: total ≤ its cap
    truncated_total = by_cap[2000]
    assert 2000 < truncated_total        # bail happened...
    # ...and the overshoot beyond the 2000 reservation is ≤ ONE 1024B chunk
    # (read_with_cap checks after accumulating a whole chunk).
    assert truncated_total <= 2000 + 1024
    # Composed explicit bound: peak ≤ budget + in_flight × chunk.
    assert 10000 + 2 * 1024 >= sum(cap for cap, _ in full_reads) + (
        truncated_total - 2000
    )


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
        for part in merged_r.json()[0]["parts"]
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
    monkeypatch.setattr(messages, "fulls", stub)
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
        for part in merged_r.json()[0]["parts"]
    )
    assert full_calls["n"] == 1
