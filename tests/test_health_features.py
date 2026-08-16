# tests/test_health_features.py
import httpx
from oc_slimapi.app import app
from oc_slimapi.config import settings

async def test_health_features_advertise_four_new_capabilities():
    # ASGITransport does not run lifespan, so pre-populate the same
    # app.state keys the lifespan body sets (app.py:166,306,316) — the
    # health route reads config / schema_degraded / deployment_revision.
    app.state.config = settings
    app.state.schema_degraded = False
    app.state.deployment_revision = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/slimapi/health?v=3", headers={"Accept-Encoding": "identity"})
        f = r.json()["features"]
        assert all(f[k] is True for k in
                   ("tokenCoalesce", "permissionEvents", "serverMerge", "transformAbsorb"))
        assert f["tokenStream"] is True  # 既有能力零回归
