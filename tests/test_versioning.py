from fastapi import FastAPI
from fastapi.testclient import TestClient

from oc_slimapi.versioning import ACCEPTED_CLIENT_VERSIONS, SlimapiVersionMiddleware


def test_production_default_accepted_versions():
    """lite-v2 §9.3: the production default constant MUST be (2, 2) —
    v2-only, no back-compat with v1."""
    assert ACCEPTED_CLIENT_VERSIONS == (2, 2)


def make_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(
        SlimapiVersionMiddleware,
        accepted_client_versions=(2, 2),
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
        "accepted": [2, 2],
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

    for version in (1, 3):
        response = client.get(
            "/slimapi/health",
            headers={"X-Slimapi-Version": str(version)},
        )
        assert response.status_code == 400
        assert response.json() == {
            "code": "version_incompatible",
            "client": version,
            "accepted": [2, 2],
        }


def test_non_integer_version_is_reported_as_required():
    response = make_client().get(
        "/slimapi/health",
        headers={"X-Slimapi-Version": "v1"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "code": "version_required",
        "accepted": [2, 2],
    }


def test_non_slimapi_path_is_not_gated():
    response = make_client().get("/global/health")

    assert response.status_code == 200
    assert response.json() == {"healthy": True}
