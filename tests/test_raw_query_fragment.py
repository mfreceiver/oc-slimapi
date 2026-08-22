"""BE-003: bare ``#`` bytes in query strings must be percent-encoded before
upstream forwarding.

Mechanism
    ``_raw_upstream_url`` in both ``_read_passthrough`` and ``file_raw``
    concatenates the raw query bytes into the upstream URL via f-string.
    httpx interprets the first bare ``#`` (0x23) as a fragment separator,
    silently dropping everything after it from the query.

Fix
    ``raw_qs.replace(b"#", b"%23")`` before the decode/concatenation —
    bytes before any percent-encoding, so existing ``%23`` sequences are
    untouched (no double-encoding path).

Coverage
    * Unit tests for both ``_raw_upstream_url`` functions (19 tests).
    * ASGI-level integration tests that inject a raw ``#`` byte via
      ``scope["query_string"]`` (bypassing httpx client-side fragment
      stripping) and verify the upstream mock receives the encoded URL,
      covering all 4 endpoint families that call ``_raw_upstream_url``:
        - read_groups (``read_passthrough_get`` pipeline)
        - providers (``_handle_providers_v4`` direct call at read_groups.py:279)
        - write (``_write_passthrough`` pipeline at write_groups.py:172)
        - file_raw (``file_raw._raw_upstream_url``)
"""

from __future__ import annotations

from starlette.requests import Request

from oc_slimapi.routes._read_passthrough import _raw_upstream_url as pt_raw_upstream_url
from oc_slimapi.routes.file_raw import _raw_upstream_url as fr_raw_upstream_url


# =============================================================================
# Helpers
# =============================================================================

def _scope(query_string: bytes) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "path": "/slimapi/file",
        "query_string": query_string,
        "headers": [(b"host", b"testserver")],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "http_version": "1.1",
    }


# =============================================================================
# _read_passthrough._raw_upstream_url — unit tests
# =============================================================================

class TestPassthroughRawUpstreamUrl:
    """Direct unit tests for ``_read_passthrough._raw_upstream_url``."""

    def test_single_bare_hash(self):
        """A single bare ``#`` is encoded to ``%23``."""
        req = Request(_scope(b"path=readme.md&q=a#b"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?path=readme.md&q=a%23b"

    def test_multiple_bare_hashes(self):
        """Multiple bare ``#`` bytes are all encoded."""
        req = Request(_scope(b"a=1#b&c=2#d"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?a=1%23b&c=2%23d"

    def test_existing_pct23_unchanged(self):
        """Already-encoded ``%23`` is NOT double-encoded."""
        req = Request(_scope(b"q=a%23b"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?q=a%23b"

    def test_bare_percent_unchanged(self):
        """A bare ``%`` byte (not followed by hex) passes through unchanged."""
        req = Request(_scope(b"q=a%bb"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?q=a%bb"

    def test_no_hash(self):
        """Query without ``#`` passes through verbatim (regression)."""
        req = Request(_scope(b"path=readme.md&q=ab"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?path=readme.md&q=ab"

    def test_empty_query(self):
        """Empty query string returns the upstream path with no ``?`` suffix."""
        req = Request(_scope(b""))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file"

    def test_v_is_stripped(self):
        """``v`` parameter is still stripped (regression)."""
        req = Request(_scope(b"v=4&path=readme.md"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?path=readme.md"

    def test_v_stripped_before_hash_encode(self):
        """Order independence: ``v`` strip then ``#`` encode, both work."""
        req = Request(_scope(b"v=4&path=readme.md&q=a#b"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?path=readme.md&q=a%23b"

    def test_repeated_keys_preserved(self):
        """Repeated query keys pass through verbatim (regression)."""
        req = Request(_scope(b"key=a&key=b#c"))
        result = pt_raw_upstream_url(req, "/file")
        assert result == "/file?key=a&key=b%23c"


# =============================================================================
# file_raw._raw_upstream_url — unit tests
# =============================================================================

class TestFileRawUpstreamUrl:
    """Direct unit tests for ``file_raw._raw_upstream_url``."""

    def test_single_bare_hash(self):
        req = Request(_scope(b"path=image.png&q=a#b"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?path=image.png&q=a%23b"

    def test_multiple_bare_hashes(self):
        req = Request(_scope(b"a=1#b&c=2#d"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?a=1%23b&c=2%23d"

    def test_existing_pct23_unchanged(self):
        req = Request(_scope(b"q=a%23b"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?q=a%23b"

    def test_bare_percent_unchanged(self):
        req = Request(_scope(b"q=a%bb"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?q=a%bb"

    def test_no_hash(self):
        req = Request(_scope(b"path=image.png"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?path=image.png"

    def test_empty_query(self):
        req = Request(_scope(b""))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content"

    def test_directory_stripped(self):
        """``directory`` parameter is still stripped (regression)."""
        req = Request(_scope(b"directory=/w&path=image.png"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?path=image.png"

    def test_v_stripped(self):
        """``v`` parameter is still stripped (regression)."""
        req = Request(_scope(b"v=4&path=image.png"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?path=image.png"

    def test_directory_and_v_stripped_with_hash(self):
        """``directory`` and ``v`` stripped, then ``#`` encoded."""
        req = Request(_scope(b"v=4&directory=/w&path=image.png&q=a#b"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?path=image.png&q=a%23b"

    def test_repeated_keys_preserved(self):
        req = Request(_scope(b"key=a&key=b#c"))
        result = fr_raw_upstream_url(req)
        assert result == "/file/content?key=a&key=b%23c"


# =============================================================================
# ASGI-level integration tests — upstream mock verification
# =============================================================================
# These construct the ASGI scope directly with a bare ``#`` byte so we bypass
# httpx client-side fragment stripping. The test verifies that the upstream
# mock receives the correctly percent-encoded URL (``%23`` instead of ``#``).
#
# Four endpoint families are tested:
#   1. read_groups — ``read_passthrough_get`` pipeline (e.g. /slimapi/file)
#   2. providers  — direct ``_raw_upstream_url`` call at read_groups.py:279
#   3. write      — ``_write_passthrough`` pipeline at write_groups.py:172
#   4. file_raw   — ``file_raw._raw_upstream_url``

import httpx
import pytest
from fastapi import FastAPI

from oc_slimapi.config import Settings
from oc_slimapi.errors import register_error_handlers
from oc_slimapi.routes import read_groups, write_groups, file_raw as file_raw_router
from oc_slimapi.selector import SlimapiSelectorMiddleware
from oc_slimapi.transform import TransformConfig, TransformPool


def _settings(**overrides) -> Settings:
    values = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=2,
        transform_wait_seconds=0.05,
        max_response_bytes=64 * 1024,
        file_raw_max_envelope_bytes=32 * 1024 * 1024,
        smoke_session_id=None,
        directory_allowlist=None,
    )
    values.update(overrides)
    return Settings(**values)


async def _asgi_send(
    app, *,
    method: str = "GET",
    path: str,
    query_string: bytes,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Drive an ASGI app with a raw ``query_string`` and return the response.

    This bypasses httpx client-side URL parsing, allowing bare ``#`` bytes
    in the query string to reach the route handler.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query_string,
        "root_path": "",
        "headers": headers or [(b"accept-encoding", b"identity")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    status = 0
    resp_headers: dict[str, str] = {}
    body = bytearray()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin-1")] = v.decode("latin-1")
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return status, resp_headers, bytes(body)


# ---------------------------------------------------------------------------
# Endpoint family builders
# ---------------------------------------------------------------------------
# Each returns (app, seen) where ``seen`` accumulates upstream requests.

def _build_read_groups_app(handler):
    """read_groups router (``read_passthrough_get`` pipeline)."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


def _build_providers_app(handler):
    """read_groups router, providers route (direct ``_raw_upstream_url`` call)."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(read_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


def _build_write_app(handler):
    """write_groups router (``_write_passthrough`` pipeline)."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(write_groups.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


def _build_file_raw_app(handler):
    """file_raw router (``file_raw._raw_upstream_url``)."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    app = FastAPI()
    settings = _settings()
    app.state.config = settings
    app.state.upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(recording),
        base_url=settings.upstream,
    )
    app.state.schema_degraded = False
    app.state.transforms = TransformPool(TransformConfig(
        max_transforms=settings.max_transforms,
        transform_wait_seconds=settings.transform_wait_seconds,
        max_response_bytes=settings.max_response_bytes,
    ))
    app.include_router(file_raw_router.router)
    register_error_handlers(app)
    app.add_middleware(SlimapiSelectorMiddleware)
    return app, seen


# ---------------------------------------------------------------------------
# Parametrized fixture: one stack per endpoint family
# ---------------------------------------------------------------------------

@pytest.fixture(
    params=[
        ("read_groups", "/slimapi/file", "GET", b"v=4&path=readme.md",
         lambda r: httpx.Response(200, content=b'[{"name":"readme.md","path":"readme.md"}]',
                                   headers={"Content-Type": "application/json"}),
         _build_read_groups_app),
        ("providers", "/slimapi/config/providers", "GET", b"v=4",
         lambda r: httpx.Response(200, content=b'{"providers":[{"id":"p1","name":"Provider 1","models":{"m1":{"id":"m1","name":"Model 1","providerID":"p1"}}}],"default":{"p1":"m1"}}',
                                   headers={"Content-Type": "application/json"}),
         _build_providers_app),
        ("write", "/slimapi/session/test-ses-id", "DELETE", b"v=4",
         lambda r: httpx.Response(204),
         _build_write_app),
        ("file_raw", "/slimapi/file/raw", "GET", b"v=4&path=image.png",
         lambda r: httpx.Response(200, content=b'{"type":"binary","content":"","mimeType":"image/png"}',
                                   headers={"Content-Type": "application/json"}),
         _build_file_raw_app),
    ],
    ids=["read_groups", "providers", "write", "file_raw"],
)
def family_stack(request):
    """Parametrized fixture: each parameter is one endpoint family."""
    _name, path, method, base_qs, handler_fn, builder = request.param
    app, seen = builder(handler_fn)
    return path, method, base_qs, app, seen


# ---------------------------------------------------------------------------
# Integration tests — 3 scenarios × 4 families = 12 tests
# ---------------------------------------------------------------------------

class TestIntegration:
    """ASGI integration tests: bare ``#`` in query → upstream ``%23``."""

    @staticmethod
    async def _check(family_stack, qs_suffix: bytes, *, expect_contains: str | None = None,
                     expect_count: int | None = None, expect_absent: str | None = None):
        path, method, base_qs, app, seen = family_stack
        qs = base_qs + qs_suffix
        status, _headers, _body = await _asgi_send(
            app, method=method, path=path, query_string=qs)
        assert status in (200, 204)
        assert len(seen) == 1
        upstream_url = str(seen[0].url)
        if expect_contains is not None:
            assert expect_contains in upstream_url, (
                f"expected {expect_contains!r} in upstream URL, got: {upstream_url}"
            )
        if expect_count is not None:
            assert upstream_url.count(expect_contains) == expect_count, (
                f"expected {expect_count}×{expect_contains!r}, got: {upstream_url}"
            )
        if expect_absent is not None:
            assert expect_absent not in upstream_url

    async def test_bare_hash_in_query_upstream_receives_encoded(self, family_stack):
        """Bare ``#`` in query → upstream receives ``%23``, not truncated."""
        await self._check(family_stack, b"&q=a#b", expect_contains="q=a%23b",
                          expect_absent="#")

    async def test_existing_pct23_not_double_encoded(self, family_stack):
        """Existing ``%23`` is NOT double-encoded to ``%2523``."""
        await self._check(family_stack, b"&q=a%23b", expect_contains="%23",
                          expect_count=1)

    async def test_no_hash_passes_verbatim(self, family_stack):
        """Query without ``#`` passes through unchanged (regression)."""
        await self._check(family_stack, b"&q=ab", expect_contains="q=ab",
                          expect_absent="#")
