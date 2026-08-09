"""``/slimapi/actions`` — action discovery and invocation routes (spec §5).

Two sidecar-local endpoints (no upstream call; the reverse proxy catch-all is
never involved):

* ``GET /slimapi/actions`` — catalog discovery: ``{"enabled": bool,
  "actions": [{"name","kind","description","requireConfirm"}]}`` straight from
  :meth:`ActionRegistry.discover`.
* ``POST /slimapi/actions/{name}`` — invoke a manifest-declared action by name
  (whitelist dictionary-key lookup only; argv is fully fixed by the manifest).
  The request body is read RAW (``request.json()`` would raise on an empty
  body): an empty body or ``{}`` is treated as ``{}``, a non-object /
  unparseable body is a 422, and a present-but-non-boolean ``confirm`` is a
  422 (fail-closed).  The body is size-capped at 1 KiB — a ``Content-Length``
  over the cap or a chunked body that overruns it is rejected 413
  ``request_too_large`` before admission (plaintext memory-DoS guard).  The
  seven :mod:`oc_slimapi.actions` exceptions are mapped via
  ``ActionError.to_coded()`` to :class:`CodedHTTPException` with their
  ``Retry-After`` / ``timeout_s`` payloads.

Both endpoints negotiate gzip via :func:`json_response` and pin
``Cache-Control: no-store`` on every response (200 and coded error — contract
§5).  The version gate (``SlimapiVersionMiddleware``) already covers every
``/slimapi/**`` path.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

import orjson

from ..actions import ActionError, ActionResult
from ..errors import CodedHTTPException
from ..gzip_util import json_response

router = APIRouter(prefix="/slimapi", tags=["actions"])

# Pin no-store on both endpoints (contract §5: every /slimapi/actions
# response — success AND coded error — carries Cache-Control: no-store).
_NO_STORE = "no-store"

# POST body cap.  The body is always empty or ``{"confirm": true}`` (~17
# bytes), so 1 KiB rejects anything larger outright — reading an unbounded
# body fully here, before admission, would be a plaintext memory DoS.
_BODY_CAP_BYTES = 1024


def _request_too_large() -> CodedHTTPException:
    """413 for a request body over the 1 KiB cap (registered in contract §7)."""
    return CodedHTTPException(
        413, code="request_too_large",
        headers={"Cache-Control": _NO_STORE},
    )


async def _read_body(request: Request) -> dict:
    """Read the raw request body as a JSON object, capped at 1 KiB.

    An empty / whitespace-only body and ``{}`` both become ``{}`` (never call
    ``await request.json()`` here — it raises on an empty body).  A body that
    is not a JSON object, or a JSON object carrying a non-boolean ``confirm``
    value, is malformed → 422 ``invalid_request_body`` (also pinned
    ``Cache-Control: no-store`` — contract §5: every response, incl. coded
    errors, carries it).

    Size gate (rev-13): a ``Content-Length`` > ``_BODY_CAP_BYTES`` is rejected
    without reading a single byte; a chunked body (no/useless Content-Length)
    is streamed up to cap+1 bytes and rejected the instant it exceeds.  Either
    way a large body never gets fully buffered here → 413 ``request_too_large``.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = None  # non-numeric → rely on the stream cap below
        if declared is not None and declared > _BODY_CAP_BYTES:
            raise _request_too_large()
    raw = bytearray()
    async for chunk in request.stream():
        # Cap BEFORE appending (rev-14): a single oversized chunk is rejected
        # without ever being buffered into ``raw`` — the pre-fix code appended
        # first and checked after, so one huge chunk transiently buffered its
        # full size before the 413 fired (plaintext memory-DoS guard).
        if len(raw) + len(chunk) > _BODY_CAP_BYTES:
            raise _request_too_large()
        raw += chunk
    if not raw.strip():
        return {}
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise CodedHTTPException(
            422, code="invalid_request_body",
            headers={"Cache-Control": _NO_STORE},
        ) from exc
    if not isinstance(payload, dict):
        raise CodedHTTPException(
            422, code="invalid_request_body",
            headers={"Cache-Control": _NO_STORE},
        )
    if "confirm" in payload and not isinstance(payload["confirm"], bool):
        raise CodedHTTPException(
            422, code="invalid_request_body",
            headers={"Cache-Control": _NO_STORE},
        )
    return payload


def _envelope(result: ActionResult) -> dict:
    """Wire envelope for a 200 action result (contract §2 / §5).

    exec: ``{kind, ok, exit_code, duration_ms, message}`` (markdown/truncated
    are query-only and excluded).  query adds ``markdown`` / ``truncated``.
    """
    body = {
        "kind": result.kind,
        "ok": result.ok,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "message": result.message,
    }
    if result.kind == "query":
        body["markdown"] = result.markdown
        body["truncated"] = result.truncated
    return body


@router.get("/actions")
async def list_actions(request: Request):
    registry = request.app.state.actions_registry
    return json_response(
        {"enabled": registry.enabled, "actions": registry.discover()},
        accept_encoding=request.headers.get("accept-encoding"),
        headers={"Cache-Control": _NO_STORE},
    )


@router.post("/actions/{name}")
async def invoke_action(request: Request, name: str):
    registry = request.app.state.actions_registry
    payload = await _read_body(request)
    confirmed = bool(payload.get("confirm", False))
    try:
        result = await registry.invoke(name, confirmed=confirmed)
    except ActionError as exc:
        # All seven action errors map via to_coded(): 404 action_not_found /
        # 409 action_confirm_required / 429 action_throttled (+Retry-After) /
        # 503 actions_disabled|action_busy (+Retry-After:2)|action_unavailable
        # / 504 action_timeout (+timeout_s body field).  Stamp no-store onto the
        # coded error too (the generic handler does not add it).
        coded = exc.to_coded()
        headers = dict(coded.headers or {})
        headers["Cache-Control"] = _NO_STORE
        coded.headers = headers
        raise coded from exc
    return json_response(
        _envelope(result),
        accept_encoding=request.headers.get("accept-encoding"),
        headers={"Cache-Control": _NO_STORE},
    )
