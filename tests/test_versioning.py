import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from oc_slimapi.versioning import ACCEPTED_CLIENT_VERSIONS, SlimapiVersionMiddleware


def test_production_default_accepted_versions():
    """v3 Batch A (2.0.0): the production pin is (2, 3) — the header gate
    admits 2 and 3 while the selector routes ?v semantics. Still no v1."""
    assert ACCEPTED_CLIENT_VERSIONS == (2, 3)


def make_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=(2, 3),
    )

    @app.get("/slimapi/health")
    async def health():
        return {"ok": True}

    @app.get("/global/health")
    async def global_health():
        return {"healthy": True}

    return TestClient(app)


def test_missing_version_header_is_rejected():
    response = make_client().get("/slimapi/health")

    assert response.status_code == 400
    assert response.json() == {
        "code": "version_required",
        "accepted": [2, 3],
    }


def test_supported_version_is_allowed():
    response = make_client().get(
        "/slimapi/health",
        headers={"X-Slimapi-Version": "2"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_out_of_range_versions_are_rejected():
    client = make_client()

    for version in (1, 4):
        response = client.get(
            "/slimapi/health",
            headers={"X-Slimapi-Version": str(version)},
        )
        assert response.status_code == 400
        assert response.json() == {
            "code": "version_incompatible",
            "client": version,
            "accepted": [2, 3],
        }


def test_non_integer_version_is_reported_as_required():
    response = make_client().get(
        "/slimapi/health",
        headers={"X-Slimapi-Version": "v1"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "version_required",
        "accepted": [2, 3],
    }


def test_non_slimapi_path_is_not_gated():
    response = make_client().get("/global/health")

    assert response.status_code == 200
    assert response.json() == {"healthy": True}


# ---------------------------------------------------------------------------
# P1-14: double-slash and root-path normalisation — version gate must not be
# bypassable via ``//slimapi/foo`` or ``/slimapi`` (exact root).
#
# TestClient (httpx) folds ``//`` → ``/`` before the request reaches the ASGI
# app, so the double-slash bypass is tested at the raw ASGI scope level. In
# production, the ASGI server (uvicorn) may deliver ``//slimapi/foo`` verbatim.
# ---------------------------------------------------------------------------

from oc_slimapi.versioning import _is_slimapi_path


def test_is_slimapi_path_normalises_double_slash():
    """The path helper collapses ``//`` before checking the prefix."""
    assert _is_slimapi_path("//slimapi/foo") is True
    assert _is_slimapi_path("///slimapi/foo") is True


def test_is_slimapi_path_recognises_root():
    """The exact root ``/slimapi`` is recognised (not bypassed)."""
    assert _is_slimapi_path("/slimapi") is True
    assert _is_slimapi_path("/slimapi/") is True


def test_is_slimapi_path_rejects_non_slimapi():
    """Non-slimapi paths (including double-slash variants) are rejected."""
    assert _is_slimapi_path("/global/health") is False
    assert _is_slimapi_path("//global") is False
    assert _is_slimapi_path("/slimapifoo") is False


@pytest.mark.asyncio
async def test_double_slash_scope_is_gated():
    """A raw ASGI scope with ``//slimapi/health`` must be gated.

    Simulates an ASGI server that does NOT fold ``//``. The middleware
    normalises the path for its gate decision and returns 400 if the version
    header is missing."""
    from starlette.responses import JSONResponse

    app = FastAPI()

    @app.get("/slimapi/health")
    async def health():
        return JSONResponse({"ok": True})

    middleware = SlimapiVersionMiddleware(app, accepted_client_versions=(2, 2))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "//slimapi/health",  # raw double-slash — NOT folded
        "headers": [],
        "query_string": b"",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)

    # The gate fires (no version header) → 400, NOT passed through to the
    # handler (which would be 200 or a 404 from mis-routing).
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400


@pytest.mark.asyncio
async def test_slimapi_root_scope_is_gated():
    """A raw ASGI scope with exact ``/slimapi`` path must be gated."""
    from starlette.responses import JSONResponse

    app = FastAPI()

    middleware = SlimapiVersionMiddleware(app, accepted_client_versions=(2, 2))

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/slimapi",  # exact root
        "headers": [],
        "query_string": b"",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400
