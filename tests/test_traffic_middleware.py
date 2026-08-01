"""Integration tests for the pure-ASGI ``TrafficAccountingMiddleware``.

Drives a minimal FastAPI app (with the middleware registered via
``app.add_middleware``) through ``httpx.ASGITransport`` so the receive/send
wrapping exercised is exactly the production code path.

Covers: downstream req/resp byte counting (downIn/downOut), StreamingResponse
chunk accumulation, SSE-bucket ``resp_bytes=0`` carve-out, exception-path
recording (``status_code or 500``), no-ledger pass-through, the
``stash_up_in`` → middleware → ``record_upstream`` chain, and per-request
access-log emission.
"""

from __future__ import annotations

import json
import logging
import logging.handlers

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from oc_slimapi.access_log import get_access_logger, setup_access_log
from oc_slimapi.config import Settings
from oc_slimapi.middleware.traffic_accounting import TrafficAccountingMiddleware
from oc_slimapi.traffic import TrafficLedger, stash_up_in


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingLedger(TrafficLedger):
    """``TrafficLedger`` that records every ``record_*`` call's kwargs.

    The real ledger accepts ``status`` on ``record_downstream`` but does not
    store it in the bucket dict (only requests/bytes), so this subclass is
    used to assert the status the middleware attributed to a request.
    """

    def __init__(self) -> None:
        super().__init__()
        self.downstream_calls: list[dict] = []
        self.upstream_calls: list[dict] = []

    def record_downstream(self, **kwargs) -> None:  # type: ignore[override]
        self.downstream_calls.append(kwargs)
        super().record_downstream(**kwargs)

    def record_upstream(self, **kwargs) -> None:  # type: ignore[override]
        self.upstream_calls.append(kwargs)
        super().record_upstream(**kwargs)


@pytest.fixture(autouse=True)
def _reset_access_logger():
    """Reset the global ``oc_slimapi.access`` logger after each test."""
    yield
    logger = get_access_logger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.disabled = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(*, ledger: TrafficLedger | None = None,
              configure_routes) -> FastAPI:
    app = FastAPI()
    if ledger is not None:
        app.state.traffic_ledger = ledger
    configure_routes(app)
    app.add_middleware(TrafficAccountingMiddleware)
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _bucket(snap: dict, name: str) -> dict:
    return snap["buckets"][name]


# ---------------------------------------------------------------------------
# 1. Normal JSON-ish GET → passthrough bucket, downOut=len(body)
# ---------------------------------------------------------------------------


async def test_get_response_counts_downout_and_zero_downin():
    body = b'{"ok":true}'
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(body, media_type="application/json")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.get("/data")

    assert resp.status_code == 200
    assert resp.content == body
    snap = ledger.snapshot()
    # /data is not under /slimapi/ → catch-all passthrough bucket.
    entry = _bucket(snap, "passthrough")
    assert entry["requests"] == 1
    assert entry["downOut"] == len(body)
    assert entry["downIn"] == 0  # no request body on GET


# ---------------------------------------------------------------------------
# 2. POST request body → downIn=len(body) (route must read the body)
# ---------------------------------------------------------------------------


async def test_post_request_body_counts_downin():
    payload = b"request-body-payload-1234567890"
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.post("/submit")
        async def submit(request: Request):
            # The body must be consumed for receive() (→ counted_receive) to fire.
            read = await request.body()
            return PlainTextResponse(b"got:" + read[:0], media_type="text/plain")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.post("/submit", content=payload)

    assert resp.status_code == 200
    entry = _bucket(ledger.snapshot(), "passthrough")
    assert entry["requests"] == 1
    assert entry["downIn"] == len(payload)
    # response body is tiny but non-zero
    assert entry["downOut"] == len(b"got:")


# ---------------------------------------------------------------------------
# 3. StreamingResponse: body intact + downOut == sum of chunk lengths
# ---------------------------------------------------------------------------


async def test_streaming_response_body_intact_and_counted():
    chunks = [b"aaa", b"bb", b"ccccc"]  # 3 + 2 + 5 = 10
    expected_total = sum(len(c) for c in chunks)
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/stream")
        async def stream():
            async def gen():
                for c in chunks:
                    yield c
            return StreamingResponse(gen(), media_type="text/plain")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.get("/stream")

    assert resp.status_code == 200
    # Body fully reassembled — streaming was NOT broken by the middleware.
    assert resp.content == b"".join(chunks)
    entry = _bucket(ledger.snapshot(), "passthrough")
    assert entry["requests"] == 1
    assert entry["downOut"] == expected_total


# ---------------------------------------------------------------------------
# 4. SSE bucket → resp_bytes=0 (downOut owned by record_sse_downstream)
# ---------------------------------------------------------------------------


async def test_sse_bucket_resp_bytes_is_zero_but_downin_counted():
    sse_body = b"event: digest\ndata: {}\n\n"
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.post("/slimapi/events")
        async def events(request: Request):
            # Read the body so downIn is non-zero and assertable.
            await request.body()
            return PlainTextResponse(sse_body, media_type="text/event-stream")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.post("/slimapi/events", content=b"abc")

    assert resp.status_code == 200
    assert resp.content == sse_body
    entry = _bucket(ledger.snapshot(), "events_sse")
    assert entry["requests"] == 1
    # Middleware deliberately passes resp_bytes=0 for SSE buckets.
    assert entry["downOut"] == 0
    # downIn is still counted by the middleware.
    assert entry["downIn"] == 3


async def test_token_stream_bucket_is_also_sse_zero_resp_bytes():
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/slimapi/sessions/ses_x/stream")
        async def stream():
            return PlainTextResponse(b"token-stream-bytes", media_type="text/event-stream")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.get("/slimapi/sessions/ses_x/stream")

    assert resp.status_code == 200
    entry = _bucket(ledger.snapshot(), "token_stream_sse")
    assert entry["requests"] == 1
    assert entry["downOut"] == 0


# ---------------------------------------------------------------------------
# 4b. SSE double-count invariant (pinned with a spy ledger)
# ---------------------------------------------------------------------------
# The middleware zeroes resp_bytes ONLY for genuine SSE streams
# (200 + content-type: text/event-stream). This is the load-bearing guard
# against double-counting: if a real SSE response forgot the text/event-stream
# header, the middleware would count wire downOut AND the generator's
# record_sse_downstream would ALSO add bytes → silent 2× downOut.
# These two tests pin both halves of the invariant.


async def test_sse_stream_response_records_downstream_with_zero_resp_bytes():
    """An SSE-path 200 response WITH ``content-type: text/event-stream`` is
    recorded exactly once with ``resp_bytes == 0`` (the per-frame
    ``record_sse_downstream`` owns downOut). Pinning the exact call kwargs
    via the spy ledger guards against a future regression that would silently
    double-count downOut for SSE buckets."""
    body = b"event: digest\ndata: {}\n\n"
    ledger = RecordingLedger()

    def routes(app: FastAPI) -> None:
        @app.post("/slimapi/events")
        async def events(request: Request):
            await request.body()
            return PlainTextResponse(body, media_type="text/event-stream")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.post("/slimapi/events", content=b"abc")

    assert resp.status_code == 200
    # Exactly one record_downstream call for the request.
    downstream = [c for c in ledger.downstream_calls if c["bucket"] == "events_sse"]
    assert len(downstream) == 1, (
        f"expected exactly 1 events_sse downstream call, got {len(downstream)}"
    )
    call = downstream[0]
    assert call["status"] == 200
    # The invariant: resp_bytes MUST be 0 so record_sse_downstream owns downOut.
    assert call["resp_bytes"] == 0, (
        f"SSE stream resp_bytes must be 0 (owned by record_sse_downstream), "
        f"got {call['resp_bytes']} — would double-count downOut"
    )
    # Sanity: downIn still counted.
    assert call["req_bytes"] == 3


async def test_sse_path_non_stream_response_counts_downout_normally():
    """An SSE-path 200 response WITHOUT ``content-type: text/event-stream``
    (e.g. a synthetic JSON 200) is counted normally — ``resp_bytes == len(body)``,
    NOT zeroed. This is the complement of the zeroing guard: if a future SSE
    variant dropped the header, the middleware must NOT silently zero the
    bytes (it would lose them). Pinning that downOut is recorded proves the
    zeroing is keyed strictly on the content-type."""
    body = b'{"ok":true}'
    ledger = RecordingLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/slimapi/events")
        async def events():
            # Non-stream JSON 200 on an SSE path — no text/event-stream header.
            return PlainTextResponse(body, media_type="application/json")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.get("/slimapi/events")

    assert resp.status_code == 200
    downstream = [c for c in ledger.downstream_calls if c["bucket"] == "events_sse"]
    assert len(downstream) == 1
    call = downstream[0]
    assert call["status"] == 200
    # NOT zeroed — the body bytes are counted because this is not a real SSE
    # stream (no text/event-stream content-type).
    assert call["resp_bytes"] == len(body), (
        f"non-stream SSE-path resp_bytes should be {len(body)} (counted "
        f"normally), got {call['resp_bytes']}"
    )
    # And the bucket downOut reflects it.
    entry = _bucket(ledger.snapshot(), "events_sse")
    assert entry["downOut"] == len(body)


# ---------------------------------------------------------------------------
# 5. Exception path → status=500 recorded, requests=1
# ---------------------------------------------------------------------------


async def test_exception_path_records_status_500_and_one_request():
    ledger = RecordingLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/boom")
        async def boom():
            raise RuntimeError("kaboom")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        got_500 = False
        raised = False
        try:
            resp = await client.get("/boom")
            got_500 = resp.status_code == 500
        except Exception:
            # ASGI-layer error (exception re-raised after best-effort recording)
            # is acceptable per the contract.
            raised = True
        assert got_500 or raised

    # Exactly one downstream record, attributed status 500.
    assert len(ledger.downstream_calls) == 1
    assert ledger.downstream_calls[0]["status"] == 500
    assert ledger.downstream_calls[0]["bucket"] == "passthrough"
    # The bucket also reflects one completed request.
    entry = _bucket(ledger.snapshot(), "passthrough")
    assert entry["requests"] == 1


# ---------------------------------------------------------------------------
# 6. No ledger on app.state → pass-through, request still succeeds
# ---------------------------------------------------------------------------


async def test_no_ledger_is_safe_passthrough():
    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app(ledger=None, configure_routes=routes)
    # app.state has no traffic_ledger attribute → _ledger_from_scope returns None.
    assert not hasattr(app.state, "traffic_ledger")
    async with await _client(app) as client:
        resp = await client.get("/data")
    assert resp.status_code == 200
    assert resp.content == b"ok"


# ---------------------------------------------------------------------------
# 7. stash_up_in → record_upstream chain (upIn == N)
# ---------------------------------------------------------------------------


async def test_stash_up_in_flows_to_record_upstream():
    n = 4242
    ledger = RecordingLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/proxy/upstream")
        async def upstream(request: Request):
            # Simulate a route that consumed an upstream response body.
            stash_up_in(request, n)
            return PlainTextResponse(b"curated", media_type="text/plain")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.get("/proxy/upstream")

    assert resp.status_code == 200
    # /proxy/upstream → passthrough (non-SSE) → record_upstream called.
    assert len(ledger.upstream_calls) == 1
    up = ledger.upstream_calls[0]
    assert up["bucket"] == "passthrough"
    # stash_up_in(N) → record_upstream(resp_bytes=N) → bucket upIn += N.
    assert up["resp_bytes"] == n
    entry = _bucket(ledger.snapshot(), "passthrough")
    assert entry["upIn"] == n


async def test_stash_up_in_ignored_for_sse_bucket():
    """For SSE buckets the stash is intentionally not attributed upstream
    (the shared /global/event connection is accounted via record_sse_upstream
    in the hub, not per-request)."""
    n = 999
    ledger = RecordingLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/slimapi/events")
        async def events(request: Request):
            stash_up_in(request, n)
            return PlainTextResponse(b"x", media_type="text/event-stream")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        await client.get("/slimapi/events")

    # SSE bucket → record_upstream must NOT be called by the middleware.
    assert ledger.upstream_calls == []
    entry = _bucket(ledger.snapshot(), "events_sse")
    assert entry["upIn"] == 0


# ---------------------------------------------------------------------------
# 8. Access log: one JSON line per request
# ---------------------------------------------------------------------------


async def test_access_log_written_one_line_per_request(tmp_path):
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    ledger = TrafficLedger()

    def routes(app: FastAPI) -> None:
        @app.get("/slimapi/messages/ses_x")
        async def msgs():
            return PlainTextResponse(b'{"ok":true}', media_type="application/json")

    app = _make_app(ledger=ledger, configure_routes=routes)
    async with await _client(app) as client:
        resp = await client.get("/slimapi/messages/ses_x")

    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()

    from datetime import date
    today = date.today().isoformat()
    log_path = tmp_path / f"access-{today}.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["method"] == "GET"
    assert record["path"] == "/slimapi/messages/ses_x"
    assert record["bucket"] == "messages"
    assert record["status"] == 200
    assert record["downOut"] == len(b'{"ok":true}')


# ---------------------------------------------------------------------------
# Extra: non-http scope types pass through untouched (lifespan / ws)
# ---------------------------------------------------------------------------


async def test_non_http_scope_passes_through_untouched():
    """A non-http (e.g. lifespan) scope reaches the inner app unchanged and
    triggers no byte accounting — the middleware early-returns for non-http."""
    seen: dict = {}

    async def inner_app(scope, receive, send):
        seen["type"] = scope["type"]
        await send({"type": "lifespan.startup.complete"})

    ledger = TrafficLedger()
    outer = TrafficAccountingMiddleware(inner_app)
    # Sneak a ledger onto a fake app state so we can prove it stayed untouched.
    class _FakeApp:
        class state:
            traffic_ledger = ledger

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        seen.setdefault("sent", []).append(message["type"])

    await outer({"type": "lifespan", "app": _FakeApp}, receive, send)
    assert seen["type"] == "lifespan"
    assert "lifespan.startup.complete" in seen["sent"]
    # No http byte accounting happened for a non-http scope.
    assert ledger.snapshot()["buckets"] == {}


# ---------------------------------------------------------------------------
# Helpers for client-identity access log tests
# ---------------------------------------------------------------------------


def _make_app_with_config_and_ledger(
    *, config: Settings, ledger: TrafficLedger | None = None,
    configure_routes,
) -> FastAPI:
    """Like _make_app but also sets app.state.config for client-id hash tests."""
    app = FastAPI()
    app.state.config = config
    if ledger is not None:
        app.state.traffic_ledger = ledger
    configure_routes(app)
    app.add_middleware(TrafficAccountingMiddleware)
    return app


def _default_test_config(**overrides) -> Settings:
    """Minimal Settings for client-identity tests."""
    base = dict(
        host="127.0.0.1", port=4097, upstream="http://127.0.0.1:4096",
        max_message_bytes=32 * 1024 * 1024,
        max_transforms=1, transform_wait_seconds=0.5, max_response_bytes=64 * 1024,
        smoke_session_id=None,
        server_api_version=1, accepted_client_versions=(1, 1),
    )
    base.update(overrides)
    return Settings(**base)


def _read_access_log(dir_path) -> list[dict]:
    from datetime import date
    today = date.today().isoformat()
    log_path = dir_path / f"access-{today}.jsonl"
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]


# ---------------------------------------------------------------------------
# 9. Access log — client-identity headers
# ---------------------------------------------------------------------------


async def test_access_log_with_client_headers(tmp_path):
    """X-Client-Name / X-Client-Version / X-Client-Id → access log JSON
    contains client/clientVer/clientId. clientId is a hash (not plaintext)."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    ledger = TrafficLedger()
    # Default config: client_id_hash=True (hash ON), no salt.
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, ledger=ledger, configure_routes=routes,
    )
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={
                "X-Client-Name": "ocdroid",
                "X-Client-Version": "2.1.0",
                "X-Client-Id": "dev123",
            },
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["client"] == "ocdroid"
    assert rec["clientVer"] == "2.1.0"
    # clientId must be a 16-char hex string (not plaintext).
    cid: str = rec["clientId"]
    assert len(cid) == 16
    assert all(c in "0123456789abcdef" for c in cid)
    assert cid != "dev123"  # not plaintext


async def test_access_log_no_client_headers(tmp_path):
    """No client-identity headers → identity fields null."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    async with await _client(app) as client:
        resp = await client.get("/data")
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["client"] is None
    assert rec["clientVer"] is None
    assert rec["clientId"] is None


async def test_access_log_client_headers_case_insensitive(tmp_path):
    """Lowercase x-client-id / x-client-name should still be read."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={
                "x-client-name": "ocdroid",
                "x-client-version": "2.1.0",
                "x-client-id": "dev123",
            },
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    rec = records[0]
    assert rec["client"] == "ocdroid"
    assert rec["clientVer"] == "2.1.0"
    assert rec["clientId"] is not None


async def test_access_log_overlong_client_id_is_null(tmp_path):
    """Client-Id > 128 UTF-8 bytes → clientId null (not truncated)."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()
    # 129 bytes of 'a' (ASCII, each 1 byte in UTF-8)
    overlong = "a" * 129

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={"X-Client-Id": overlong},
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    rec = records[0]
    assert rec["clientId"] is None, "overlong client-id must be null"


async def test_access_log_control_char_client_id_is_null(tmp_path):
    """X-Client-Id containing control chars → clientId null."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    # Inject a newline character — header injection attempt.
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={"X-Client-Id": "valid\ninjected"},
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    rec = records[0]
    assert rec["clientId"] is None, "control-char client-id must be null"


async def test_access_log_whitespace_client_header_is_null(tmp_path):
    """Whitespace-only client headers → null."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={"X-Client-Id": "   ", "X-Client-Name": "  "},
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    rec = records[0]
    assert rec["clientId"] is None, "whitespace client-id must be null"
    assert rec["client"] is None, "whitespace client-name must be null"


async def test_access_log_duplicate_client_header_first_wins(tmp_path):
    """Duplicate X-Client-Id: first valid value is used (lenient)."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    # httpx's ASGITransport doesn't support duplicate headers easily,
    # so we drive the middleware directly with a raw scope.
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/data",
        "headers": [
            (b"x-client-id", b"first"),
            (b"x-client-id", b"second"),
            (b"host", b"test"),
        ],
        "app": app,
    }
    recorded: dict = {}

    async def inner_app(scope, receive, send):
        nonlocal recorded
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"ok"})

    outer = TrafficAccountingMiddleware(inner_app, logger=logger)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        recorded.setdefault("messages", []).append(message)

    await outer(scope, receive, send)
    for handler in logger.handlers:
        handler.flush()

    records = _read_access_log(tmp_path)
    rec = records[0]
    # First header "first" should win.
    assert rec["clientId"] is not None
    # Verify the hash corresponds to "first", not "second".
    from oc_slimapi.access_log import hash_client_id
    assert rec["clientId"] == hash_client_id("first")


async def test_access_log_client_id_hash_off_plaintext(tmp_path):
    """client_id_hash=False → clientId logged as plaintext."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config(client_id_hash=False)

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={"X-Client-Id": "my-plain-id"},
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    rec = records[0]
    assert rec["clientId"] == "my-plain-id", "hash off → plaintext"


async def test_access_log_client_id_hash_with_salt(tmp_path):
    """client_id_hash=True + salt set → HMAC result (different from no-salt)."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config(client_id_hash=True, client_id_salt="pepper")

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={"X-Client-Id": "my-device"},
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()

    records = _read_access_log(tmp_path)
    rec = records[0]
    cid: str = rec["clientId"]
    assert len(cid) == 16
    assert all(c in "0123456789abcdef" for c in cid)
    # With salt, shorter HMAC round; just confirm it is a hash, not plaintext.
    assert cid != "my-device"


async def test_access_log_fail_closed_no_config(tmp_path):
    """No app.state.config → hash applied (fail-closed)."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    # App WITHOUT config set — _config_from_scope returns None.
    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app(ledger=None, configure_routes=routes)
    # Ensure NO config on state.
    assert not hasattr(app.state, "config")
    async with await _client(app) as client:
        resp = await client.get(
            "/data",
            headers={"X-Client-Id": "my-device"},
        )
    assert resp.status_code == 200
    for handler in logger.handlers:
        handler.flush()
    records = _read_access_log(tmp_path)
    rec = records[0]
    # clientId must still be hashed (not plaintext).
    cid: str = rec["clientId"]
    assert len(cid) == 16
    assert all(c in "0123456789abcdef" for c in cid)
    assert cid != "my-device"


# ---------------------------------------------------------------------------
# 10. UTF-8 edge cases — invalid bytes rejected, valid multi-byte accepted
# ---------------------------------------------------------------------------


async def test_access_log_invalid_utf8_client_id_is_null(tmp_path):
    """X-Client-Id with invalid UTF-8 bytes → clientId null (strict decode)."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    # Inject \xff\xfe which is invalid UTF-8 via raw scope.
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/data",
        "headers": [
            (b"x-client-id", b"\xff\xfe"),
            (b"host", b"test"),
        ],
        "app": app,
    }

    async def inner_app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"ok"})

    outer = TrafficAccountingMiddleware(inner_app, logger=logger)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await outer(scope, receive, send)
    for handler in logger.handlers:
        handler.flush()

    records = _read_access_log(tmp_path)
    rec = records[0]
    assert rec["clientId"] is None, "invalid UTF-8 client-id must be null"


async def test_access_log_multi_byte_utf8_client_id_ok(tmp_path):
    """X-Client-Id with valid multi-byte UTF-8 (e.g. Chinese) → read + hash."""
    logger = setup_access_log(enabled=True, dir=str(tmp_path))
    config = _default_test_config()

    def routes(app: FastAPI) -> None:
        @app.get("/data")
        async def data():
            return PlainTextResponse(b"ok", media_type="text/plain")

    app = _make_app_with_config_and_ledger(
        config=config, configure_routes=routes,
    )
    # Chinese device id as UTF-8 encoded bytes (httpx rejects non-ASCII headers,
    # so drive the middleware directly via raw scope).
    raw_id = "设备ID-中文"
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/data",
        "headers": [
            (b"x-client-id", raw_id.encode("utf-8")),
            (b"host", b"test"),
        ],
        "app": app,
    }

    async def inner_app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        })
        await send({"type": "http.response.body", "body": b"ok"})

    outer = TrafficAccountingMiddleware(inner_app, logger=logger)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await outer(scope, receive, send)
    for handler in logger.handlers:
        handler.flush()

    records = _read_access_log(tmp_path)
    rec = records[0]
    cid: str = rec["clientId"]
    assert len(cid) == 16
    assert all(c in "0123456789abcdef" for c in cid)
    # Hash of "设备ID-中文" — not plaintext.
    from oc_slimapi.access_log import hash_client_id
    assert cid == hash_client_id(raw_id)
