"""BUG-004/005: guarded decimal parse — digit boundary guards.

Parametrized digit lengths {4299, 4300, 4301, 5000} × forms {plain, all-zeros,
leading-zero}:

- ``?v=`` → ALL coded 400 ``unsupported_version`` (no 500, no exception).
- ``Last-Event-ID`` global ``g:<epoch>:<seq>`` + token
  ``t:<sid>:<epoch>:<seq>`` → default-cursor reset behavior (no 500).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import health, versions
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.sse.replay_wire import parse_last_event_id

EPOCH = "0123456789abcdef"
SID = "s1"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_selector_app() -> FastAPI:
    app = FastAPI(title="selector-boundary-test")
    app.add_middleware(SlimapiSelectorMiddleware)
    app.state.config = _settings()
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    app.include_router(health.router)
    app.include_router(versions.router)
    register_error_handlers(app)
    return app


def _settings(**overrides) -> dict:
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1,
        transform_wait_seconds=0.5,
        max_response_bytes=64 * 1024,
        smoke_session_id=None,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Selector: ?v= with extreme digit lengths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "digits,form,expected_code",
    [
        # plain forms (all nines, lexically valid) → length > 19 → unsupported
        (4299, "plain", "unsupported_version"),
        (4300, "plain", "unsupported_version"),
        (4301, "plain", "unsupported_version"),
        (5000, "plain", "unsupported_version"),
        # all-zeros: fails lexical check ^[1-9][0-9]*$ → invalid_version_selector
        (4300, "all_zeros", "invalid_version_selector"),
        (4301, "all_zeros", "invalid_version_selector"),
        # leading-zero: fails lexical check → invalid_version_selector
        (4300, "leading_zero", "invalid_version_selector"),
        (4301, "leading_zero", "invalid_version_selector"),
    ],
)
async def test_selector_digit_boundary_returns_400(digits, form, expected_code):
    """Extreme digit lengths in ?v= must produce 400, never 500 or exception.
    Plain forms (>19 digits) → unsupported_version; all-zeros/leading-zero
    fail the existing lexical check → invalid_version_selector."""
    if form == "all_zeros":
        v = "0" * digits
    elif form == "leading_zero":
        v = "0" + "9" * (digits - 1)
    else:
        v = "9" * digits

    app = _build_selector_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get(f"/slimapi/health?v={v}")

    assert resp.status_code == 400, (
        f"Expected 400 for {form} len={digits}, got {resp.status_code}: {resp.text[:100]}"
    )
    body = resp.json()
    assert body.get("code") == expected_code, (
        f"Expected {expected_code} for {form} len={digits}, got {body.get('code')}: {body}"
    )


# ---------------------------------------------------------------------------
# parse_last_event_id: global and token with extreme digit lengths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "digits,form",
    [
        (4299, "plain"),
        (4300, "plain"),
        (4301, "plain"),
        (5000, "plain"),
        (4300, "all_zeros"),
        (4301, "all_zeros"),
        (4300, "leading_zero"),
        (4301, "leading_zero"),
    ],
)
def test_parse_last_event_id_global_digit_boundary_returns_none(digits, form):
    """Extreme digit seq in global Last-Event-ID must return None
    (reset behavior), never raise."""
    if form == "all_zeros":
        seq = "0" * digits
    elif form == "leading_zero":
        seq = "0" + "9" * (digits - 1)
    else:
        seq = "9" * digits

    result = parse_last_event_id(
        f"g:{EPOCH}:{seq}", token_sid=None,
    )
    assert result is None, (
        f"Expected None for global {form} len={digits}, got {result}"
    )


@pytest.mark.parametrize(
    "digits,form",
    [
        (4299, "plain"),
        (4300, "plain"),
        (4301, "plain"),
        (5000, "plain"),
        (4300, "all_zeros"),
        (4301, "all_zeros"),
        (4300, "leading_zero"),
        (4301, "leading_zero"),
    ],
)
def test_parse_last_event_id_token_digit_boundary_returns_none(digits, form):
    """Extreme digit seq in token Last-Event-ID must return None
    (reset behavior), never raise."""
    if form == "all_zeros":
        seq = "0" * digits
    elif form == "leading_zero":
        seq = "0" + "9" * (digits - 1)
    else:
        seq = "9" * digits

    result = parse_last_event_id(
        f"t:{SID}:{EPOCH}:{seq}", token_sid=SID,
    )
    assert result is None, (
        f"Expected None for token {form} len={digits}, got {result}"
    )


# ---------------------------------------------------------------------------
# Existing short-digit cases still work (regression guard)
# ---------------------------------------------------------------------------

async def test_selector_short_digit_still_works():
    """A normal-length ?v=999999 still returns unsupported_version."""
    app = _build_selector_app()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/slimapi/health?v=999999")
    assert resp.status_code == 400
    assert resp.json()["code"] == "unsupported_version"


def test_parse_global_normal_seq_still_works():
    """A normal-length seq still parses correctly."""
    result = parse_last_event_id(f"g:{EPOCH}:42", token_sid=None)
    assert result == (EPOCH, 42)


def test_parse_token_normal_seq_still_works():
    result = parse_last_event_id(f"t:{SID}:{EPOCH}:42", token_sid=SID)
    assert result == (EPOCH, 42)
