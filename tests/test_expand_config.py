"""Expand lane C tests: config envelope + version capabilities + metrics.

Covers (design-expand v5, §3.2 / §6 / §11 P4 / §12):

* ``max_expand_response_bytes`` default (8 MiB) and startup window
  [1 KiB, 32 MiB] — out-of-window values are rejected by ``Settings.validate()``.
* The R4-M3 aggregate memory envelope: the transform-pool product accounts
  ``max(max_response_bytes, max_expand_response_bytes) × max_transforms`` —
  an expand cap ABOVE the plain response cap combined with the product over
  512 MiB must fail startup.
* ``GET /slimapi/versions`` capabilities["4"]["expand"]: the 12 §2.2
  categories in verbatim table order + ``fragmentMaxBytes`` live-linked to
  the running Settings (nothing hardcoded).
* Expand metrics: ``TrafficLedger.record_expand`` (category × status +
  response bytes), the ``messages.expand`` bucket in ``bucketize``, and the
  middleware feeding ``record_expand`` from the request path.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport

from oc_slimapi import config as config_module
from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.routes import versions as versions_route
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.traffic import (
    TrafficLedger,
    bucketize,
    expand_category_from_path,
)


def _base(**overrides) -> Settings:
    """Minimal-but-valid Settings; override the field under test per case."""
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# 1. max_expand_response_bytes: default + [1 KiB, 32 MiB] startup window
# ---------------------------------------------------------------------------

def test_max_expand_response_bytes_default():
    """§3.2 default: 8 MiB (8388608) when OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES
    is unset."""
    s = _base()
    assert s.max_expand_response_bytes == 8 * 1024 * 1024


def test_validate_accepts_expand_boundaries():
    """1 KiB and 32 MiB are valid boundary values (inclusive window)."""
    _base(max_expand_response_bytes=1024).validate()
    _base(max_expand_response_bytes=32 * 1024 * 1024).validate()


@pytest.mark.parametrize("value", [0, 1023])
def test_validate_rejects_expand_below_1_kib(value):
    """Sub-1 KiB cap → startup rejection (no fragment could serialize)."""
    settings = _base(max_expand_response_bytes=value)
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES must be in"):
        settings.validate()


@pytest.mark.parametrize("value", [32 * 1024 * 1024 + 1, 64 * 1024 * 1024])
def test_validate_rejects_expand_above_32_mib(value):
    """Above 32 MiB → startup rejection (expand worker buffers risk OOM)."""
    settings = _base(max_expand_response_bytes=value)
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES must be in"):
        settings.validate()


# ---------------------------------------------------------------------------
# 2. R4-M3 aggregate memory envelope: max(两 cap) × max_transforms
# ---------------------------------------------------------------------------

def test_validate_rejects_expand_cap_above_response_cap_with_envelope_exceeded():
    """R4-M3 (§3.2) config test: expand cap HIGHER than max_response_bytes AND
    the aggregate envelope ((max(两 cap)) × max_transforms) over the 512 MiB
    cap → startup rejection. max(8 MiB, 32 MiB) × 32 = 1 GiB > 512 MiB, while
    the plain response product (8 MiB × 32 = 256 MiB) would wrongly fit if
    only max_response_bytes were consulted."""
    settings = _base(
        max_transforms=32,
        max_response_bytes=8 * 1024 * 1024,
        max_expand_response_bytes=32 * 1024 * 1024,
    )
    with pytest.raises(RuntimeError, match=r"OOM under MemoryMax"):
        settings.validate()


def test_validate_rejects_envelope_when_expand_cap_is_winning_term():
    """The max() accounting must consult the expand cap: 17 × max(16 MiB,
    32 MiB) = 544 MiB > 512 MiB even though the plain response product
    (17 × 16 MiB = 272 MiB) fits comfortably — proving the expand cap alone
    can push the envelope over."""
    settings = _base(
        max_transforms=17,
        max_response_bytes=16 * 1024 * 1024,
        max_expand_response_bytes=32 * 1024 * 1024,
    )
    with pytest.raises(RuntimeError, match=r"OOM under MemoryMax"):
        settings.validate()


def test_validate_accepts_expand_cap_at_envelope_boundary():
    """16 × max(16 MiB, 32 MiB) = 512 MiB == the exact cap (<= comparison) →
    OK; the expand cap is the winning term and still fits."""
    _base(
        max_transforms=16,
        max_response_bytes=16 * 1024 * 1024,
        max_expand_response_bytes=32 * 1024 * 1024,
    ).validate()


def test_validate_accepts_expand_cap_below_or_equal_response_cap():
    """expand cap ≤ response cap leaves the P1-30 envelope unchanged: default
    1 × max(64 MiB, 8 MiB) = 64 MiB (well within budget)."""
    _base().validate()
    _base(max_expand_response_bytes=2 * 1024 * 1024).validate()


def test_env_non_integer_expand_cap_is_named_runtime_error(monkeypatch):
    """m1 (rev-gpt R1): a malformed OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES must
    fail startup with a RuntimeError NAMING the variable — not a bare
    ValueError at import time (mirrors the _version_range pattern)."""
    monkeypatch.setenv("OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES", "abc")
    with pytest.raises(
        RuntimeError,
        match=r"OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES must be an integer",
    ):
        config_module._int_env("OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES", 8 * 1024 * 1024)


# ---------------------------------------------------------------------------
# 3. versions capabilities["4"]["expand"] (§6)
# ---------------------------------------------------------------------------

EXPECTED_CATEGORIES = [
    "info_summary_diffs",
    "part_text",
    "part_reasoning",
    "part_state_output",
    "part_state_error",
    "part_state_input_full",
    "part_state_metadata_full",
    "part_state_attachments",
    "part_url",
    "part_source",
    "part_snapshot",
    "compaction_full",
]


def test_expand_categories_constant_verbatim_order():
    """§2.2 frozen table order — exact list, 12 items, no extras."""
    assert versions_route.EXPAND_CATEGORIES == EXPECTED_CATEGORIES
    assert len(versions_route.EXPAND_CATEGORIES) == 12


def _build_app() -> FastAPI:
    app = FastAPI(title="expand-versions-test")
    app.add_middleware(SlimapiSelectorMiddleware)
    app.include_router(versions_route.router)
    register_error_handlers(app)
    return app


async def test_versions_capabilities_include_expand():
    async with httpx.AsyncClient(
        transport=ASGITransport(_build_app()), base_url="http://test"
    ) as client:
        body = (await client.get("/slimapi/versions")).json()
    caps = body["capabilities"]
    assert set(caps.keys()) == {"4"}  # (4,4) window: v3 face retired
    expand = caps["4"]["expand"]
    assert expand["categories"] == EXPECTED_CATEGORIES
    # fragmentMaxBytes reflects the running Settings (default 8 MiB unset).
    assert expand["fragmentMaxBytes"] == 8 * 1024 * 1024
    assert expand["fragmentMaxBytes"] == Settings().max_expand_response_bytes


async def test_versions_fragment_max_bytes_follows_config(monkeypatch):
    """fragmentMaxBytes must track the ACTIVE Settings — monkeypatch the
    module settings and the advertisement must follow (nothing hardcoded)."""
    monkeypatch.setattr(
        versions_route,
        "settings",
        _base(max_expand_response_bytes=2 * 1024 * 1024),
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(_build_app()), base_url="http://test"
    ) as client:
        body = (await client.get("/slimapi/versions")).json()
    expand = body["capabilities"]["4"]["expand"]
    assert expand["fragmentMaxBytes"] == 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# 4. Metrics: ledger counter + bucketize (category × status + bytes)
# ---------------------------------------------------------------------------

def test_expand_counter_category_status_bytes():
    ledger = TrafficLedger()
    ledger.record_expand(category="part_text", status=200, resp_bytes=250)
    ledger.record_expand(category="part_text", status=200, resp_bytes=300)
    ledger.record_expand(category="part_text", status=400, resp_bytes=64)
    snap = ledger.snapshot()
    assert snap["expand"] == {
        "part_text|200": {"requests": 2, "bytes": 550},
        "part_text|400": {"requests": 1, "bytes": 64},
    }


def test_expand_counter_disabled_noop():
    ledger = TrafficLedger(enabled=False)
    ledger.record_expand(category="part_text", status=200, resp_bytes=10)
    assert ledger.snapshot() == {"enabled": False}


def test_expand_counter_clamps_negative_bytes():
    """Negative resp_bytes → clamped to 0 (request still counted), matching
    the other ledger accumulators."""
    ledger = TrafficLedger()
    ledger.record_expand(category="part_url", status=500, resp_bytes=-7)
    snap = ledger.snapshot()
    assert snap["expand"]["part_url|500"] == {"requests": 1, "bytes": 0}


def test_bucketize_expand_paths():
    """Expand endpoints (design-expand §2.1 / §8 read group 8) get their own
    bucket; plain messages paths keep the generic bucket."""
    assert bucketize("GET", "/slimapi/messages/ses_x/expand/part_text/msg_x") == "messages.expand"
    assert bucketize("GET", "/slimapi/messages/ses_x/expand/part_text/msg_x/prt_y") == "messages.expand"
    assert bucketize("GET", "/slimapi/messages/ses_x") == "messages"
    assert bucketize("GET", "/slimapi/messages") == "messages"


def test_expand_category_from_path_extracts_segment():
    """Segment-strict category extraction; non-expand paths yield None."""
    assert expand_category_from_path("/slimapi/messages/ses_x/expand/part_text/msg_x") == "part_text"
    assert expand_category_from_path("/slimapi/messages/ses_x/expand/part_text/msg_x/prt_y") == "part_text"
    assert expand_category_from_path("/slimapi/messages/ses_x") is None
    assert expand_category_from_path("/slimapi/messages") is None
    assert expand_category_from_path("/slimapi/messages/ses_x/expand") is None  # no category segment
    assert expand_category_from_path("/slimapi/other") is None


# ---------------------------------------------------------------------------
# 5b. Whitelist cardinality bound (rev-gpt R1 M2) — forged / malformed
# categories collapse onto the fixed ``invalid`` key (bounded memory).
# ---------------------------------------------------------------------------

def test_expand_counter_forged_and_empty_categories_collapse_to_invalid():
    """Any category outside the 12-item §2.2 whitelist — forged segments AND
    empty segments — counts under the fixed ``invalid`` key instead of
    opening a per-value key."""
    ledger = TrafficLedger()
    ledger.record_expand(category="aaaaaa", status=200, resp_bytes=10)
    ledger.record_expand(category="", status=200, resp_bytes=20)
    ledger.record_expand(category="part_text", status=200, resp_bytes=30)
    snap = ledger.snapshot()
    assert snap["expand"] == {
        "invalid|200": {"requests": 2, "bytes": 30},
        "part_text|200": {"requests": 1, "bytes": 30},
    }


def test_expand_counter_forged_category_cardinality_bounded():
    """100 distinct attacker-chosen category segments → ONE invalid key, so
    the ``_expand`` dict cannot grow with the request path (DoS bound:
    ≤ 12 whitelisted + 1 invalid categories)."""
    ledger = TrafficLedger()
    for i in range(100):
        ledger.record_expand(category=f"forged{i:03d}", status=404, resp_bytes=0)
    snap = ledger.snapshot()
    assert set(snap["expand"].keys()) == {"invalid|404"}
    assert snap["expand"]["invalid|404"]["requests"] == 100
    assert len(snap["expand"]) <= 13


def test_empty_sid_segment_is_not_expand():
    """/slimapi/messages//expand/... has an EMPTY session segment — malformed,
    so it must NOT classify as an expand request (bucket stays plain
    messages, category extractor returns None)."""
    path = "/slimapi/messages//expand/part_text/mid"
    assert expand_category_from_path(path) is None
    assert bucketize("GET", path) == "messages"


async def test_middleware_forged_category_counts_as_invalid():
    """A 404 on an attacker-rotatable category segment must land in the fixed
    ``invalid`` counter (bucket messages.expand), never open a per-category
    key — the middleware records AFTER routing, including 404s."""
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        pass  # expand routes are a parallel lane — every expand path 404s

    app = _middleware_app(ledger=ledger, configure_routes=routes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        r = await client.get("/slimapi/messages/ses_x/expand/aaaaaa/msg_x")

    assert r.status_code == 404
    snap = ledger.snapshot()
    assert snap["buckets"]["messages.expand"]["requests"] == 1
    assert set(snap["expand"].keys()) == {"invalid|404"}


async def test_middleware_empty_category_segment_counts_as_invalid():
    """Empty category segment (/expand//msg_x) still classifies as an expand
    request (path shape) but counts under ``invalid`` — defined accounting
    for otherwise-undefined malformed forms (rev-gpt R1 m2)."""
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        pass

    app = _middleware_app(ledger=ledger, configure_routes=routes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        r = await client.get("/slimapi/messages/ses_x/expand//msg_x")

    assert r.status_code == 404
    snap = ledger.snapshot()
    assert snapshot_bucket_requests(snap, "messages.expand") == 1
    assert set(snap["expand"].keys()) == {"invalid|404"}


def snapshot_bucket_requests(snap: dict, bucket: str) -> int:
    """Local helper: requests count for a bucket (avoids KeyError churn)."""
    return snap["buckets"].get(bucket, {}).get("requests", 0)


# ---------------------------------------------------------------------------
# 5. Metrics: middleware wires expand paths into the counter (end-to-end)
# ---------------------------------------------------------------------------

def _middleware_app(*, ledger: TrafficLedger,
                    configure_routes) -> FastAPI:
    app = FastAPI()
    app.state.traffic_ledger = ledger
    configure_routes(app)
    app.add_middleware(TrafficAccountingMiddleware)
    return app


async def test_middleware_counts_expand_by_category_and_status():
    """The expand routes are a parallel lane — stub both path forms here so
    the middleware hook (the lane-C compute) is exercised end-to-end: bucket
    messages.expand + per-category|status counter with wire bytes."""
    ok_body = b'{"ok":true,"mid":"msg_x"}'
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/slimapi/messages/ses_x/expand/part_text/msg_x")
        async def part_text_fragment():
            return PlainTextResponse(ok_body, media_type="application/json")

        @app.get("/slimapi/messages/ses_x/expand/part_url/msg_y")
        async def part_url_fragment():
            return PlainTextResponse(b"", media_type="application/json", status_code=400)

    app = _middleware_app(ledger=ledger, configure_routes=routes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        r1 = await client.get("/slimapi/messages/ses_x/expand/part_text/msg_x")
        r2 = await client.get("/slimapi/messages/ses_x/expand/part_url/msg_y")

    assert r1.status_code == 200
    assert r2.status_code == 400
    snap = ledger.snapshot()
    # HTTP bucket dimension (shared with record_downstream).
    b = snap["buckets"]["messages.expand"]
    assert b["requests"] == 2
    assert b["downOut"] == len(ok_body)
    # category × status cross-cut + response bytes.
    assert snap["expand"]["part_text|200"] == {"requests": 1, "bytes": len(ok_body)}
    assert snap["expand"]["part_url|400"] == {"requests": 1, "bytes": 0}


async def test_middleware_no_expand_counter_for_plain_messages():
    """A non-expand messages path must NOT touch the expand counter."""
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/slimapi/messages/ses_x")
        async def msgs():
            return PlainTextResponse(b'{"ok":true}', media_type="application/json")

    app = _middleware_app(ledger=ledger, configure_routes=routes)
    async with httpx.AsyncClient(
        transport=ASGITransport(app), base_url="http://test"
    ) as client:
        await client.get("/slimapi/messages/ses_x")

    snap = ledger.snapshot()
    assert snap["expand"] == {}
    assert "messages.expand" not in snap["buckets"]
    assert snap["buckets"]["messages"]["requests"] == 1