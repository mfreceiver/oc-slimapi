"""v3-contract §10.b — the 12 annexed WRITE endpoints (Batch C2).

Every endpoint is a **controlled write proxy**: the sidecar never rewrites
success semantics, only adding protection (request/response caps), audit
(tracking headers) and ``?v=``/``?directory=`` consumption. Paths are the
legacy opencode paths under the ``/slimapi`` prefix.

Frozen unified behaviour (contract §10.b「统一行为」, anchored to upstream
opencode v1.18.16 — ``groups/session.ts:203-397``, ``groups/question.ts:32-48``):

* **request body + content-type forwarded verbatim** (both legal PATCH
  shapes, ForkPayload's optional ``messageID`` body field, etc. — the
  sidecar never parses or distinguishes payload shapes; the upstream
  validates). Empty bodies (abort/reject/DELETE/fork-without-payload)
  forward as empty;
* **request-size cap** → 413 ``request_too_large`` before the upstream call
  (contract: "既有 max_request_bytes 语义" — repo knob
  ``max_message_bytes``);
* **query verbatim** (§5.2): post-selector raw bytes embedded in the
  upstream URL — unknown params / repeats / percent-encodings survive; the
  sidecar-reserved ``v`` is stripped everywhere, ``directory`` additionally
  so on v3 (consumed by the selector);
* **directory consumed on all 12** (every upstream endpoint declares
  ``WorkspaceRoutingQuery``): v3 ``?directory=`` → selector stash →
  forwarded as the ``X-Opencode-Directory`` header; v2 → the header is the
  channel (bound + validated); v2 ``?directory=`` values are validated then
  forwarded verbatim (not consumed);
* **response status verbatim** — 2xx (incl. 201/202/204) AND 3xx (status +
  body + ``Location`` untouched; never followed — the upstream client has
  ``follow_redirects=False``) AND 4xx (client validation errors arrive
  raw);
* **response-header frozen set** (present ones only): ``Content-Type`` /
  ``Location`` / ``Retry-After`` / ``X-Request-ID`` / ``Last-Request-ID``.
  Upstream ``Content-Encoding`` never passes (httpx already decoded the
  entity; the sidecar re-encodes under its own gzip gate);
* **two-tier errors**: upstream 5xx / network → 503
  ``upstream_unavailable``; response over ``max_response_bytes`` → 413
  ``response_too_large``;
* **no ETag** (write routes — §6.3) and **no transform-pool admission**
  (no projection → no ``transform_busy``), gzip re-encode inline like the
  §10.a read groups;
* success responses carry ``Cache-Control: no-store`` and the merged
  ``Vary: Accept-Encoding, X-Opencode-Directory`` (§6.2 directory-sensitive
  set: every §10.b write route consumes directory).
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from starlette.responses import Response

from ..directory import validate_directory
from ..gzip_util import compress_if_beneficial, error_response
from ..selector import resolve_route_directory, wire_view_from_scope
from ..traffic import stash_up_in
from ..transform import read_with_cap
from ..upstream import forward_upstream_headers, request_id_from_scope
from ..upstream_errors import raise_upstream_unavailable
from ._read_passthrough import (
    _PASSTHROUGH_UPSTREAM_HEADERS,
    _raw_upstream_url,
    _read_error_body,
    _upstream_passthrough_headers,
)

router = APIRouter(prefix="/slimapi", tags=["write-groups"])


def _resolve(request: Request) -> str | None:
    """Workspace directory for a write route (§5.2).

    v3: the selector consumed ``?directory=`` into the scope stash
    (validated) — forward it as the ``X-Opencode-Directory`` header.
    v2: the header is the channel (bind + validate); the v2 query —
    ``directory`` included — forwards verbatim (validated per value).
    """
    resolved = resolve_route_directory(request.scope, None)
    if resolved is not None:
        return validate_directory(resolved)
    if wire_view_from_scope(request.scope) == 2:
        header_dir = request.headers.get("x-opencode-directory")
        if header_dir:  # treat empty header as absent
            return validate_directory(header_dir)
    return None


async def _write_passthrough(
    request: Request,
    *,
    method: str,
    upstream_path: str,
) -> Response:
    """The shared §10.b write pipeline (see module docstring)."""
    config = request.app.state.config
    accept_encoding = request.headers.get("accept-encoding")

    # v2 duty: validate (not consume) every ?directory= value before it
    # leaves the sidecar — identical to the §10.a read groups.
    if wire_view_from_scope(request.scope) == 2:
        for dir_val in request.query_params.getlist("directory"):
            validate_directory(dir_val)

    # Request body: read once (caps at max_message_bytes — the repo's
    # request-size knob; 413 before any upstream call).
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > config.max_message_bytes:
            return error_response(
                "request_too_large", 413,
                accept_encoding=accept_encoding,
                limit=config.max_message_bytes,
            )
        body += chunk

    directory = _resolve(request)
    headers = forward_upstream_headers(
        directory, request_id_from_scope(request.scope))
    content_type = request.headers.get("content-type")
    if content_type is not None:
        # Forward the client's content-type verbatim (write bodies are
        # payload contracts the upstream validates — never re-labelled).
        headers["content-type"] = content_type

    upstream_request = request.app.state.upstream.build_request(
        method,
        _raw_upstream_url(request, upstream_path),
        content=bytes(body) or None,
        headers=headers,
    )
    try:
        response = await request.app.state.upstream.send(
            upstream_request, stream=True)
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)

    try:
        status = response.status_code
        if status >= 500:
            # Two-tier rule: 5xx collapses to 503 upstream_unavailable.
            # The error body read is cap-protected (§10.a:141 frozen —
            # applies to the §10.b unified behaviour): over the limit the
            # 503 still fires (never an unbounded buffer), resource
            # protection wins over any body duty.
            await _read_error_body(request, response)
            raise_upstream_unavailable(RuntimeError(f"upstream {status}"))
        if status >= 400:
            # 4xx (and any non-5xx error): status + body verbatim + the
            # frozen header set. No sidecar additions (no Vary/no-store).
            # The body read itself is cap-protected (§10.a:141): an
            # oversized error body degrades to 503 upstream_unavailable —
            # verbatim is a duty only while safely readable.
            err = await _read_error_body(request, response)
            return Response(
                err,
                status_code=status,
                headers=_upstream_passthrough_headers(
                    response, default_content_type=None),
            )
        try:
            resp_body, _ = await read_with_cap(
                response, config.max_response_bytes,
                on_read=lambda n: stash_up_in(request, n))
        except httpx.RequestError as exc:
            raise_upstream_unavailable(exc)
        if resp_body is None:
            return error_response(
                "response_too_large", 413,
                accept_encoding=accept_encoding,
                limit=config.max_response_bytes,
            )
    finally:
        await response.aclose()

    # Success (2xx AND 3xx — both "success" for the verbatim rule): the
    # sidecar re-encodes under its own gzip benefit gate; the entity bytes
    # are what survives. Empty bodies (204/202 NoContent) skip the gate.
    encoded, coding = compress_if_beneficial(resp_body, accept_encoding)
    resp_headers: dict[str, str] = {
        "Cache-Control": "no-store",
        # Strictly present-only frozen set (§10.b: no Content-Type default
        # when the upstream never sent one).
        **_upstream_passthrough_headers(
            response, default_content_type=None),
    }
    resp_headers.update(coding)
    # §6.2: every §10.b write route is directory-sensitive (consumer set)
    # — merged double Vary, overwriting compress_if_beneficial's bare one.
    resp_headers["Vary"] = "Accept-Encoding, X-Opencode-Directory"
    return Response(encoded, status_code=status, headers=resp_headers)


# ---------------------------------------------------------------------------
# The 12 endpoints (contract §10.b table, 1:1 path mapping + /slimapi).
# ---------------------------------------------------------------------------


@router.post("/session")
async def create_session(request: Request) -> Response:
    """#1 createSession — POST /session (payload [NoContent, CreateInput])."""
    return await _write_passthrough(request, method="POST", upstream_path="/session")


@router.patch("/session/{session_id}")
async def update_session(request: Request, session_id: str) -> Response:
    """#2 updateSession — PATCH /session/{id}.

    DUAL payload shape (UpdatePayload title/metadata/permission vs
    time.archived): forwarded verbatim, the sidecar does not distinguish —
    the upstream validates both.
    """
    return await _write_passthrough(
        request, method="PATCH", upstream_path=f"/session/{session_id}")


@router.delete("/session/{session_id}")
async def delete_session(request: Request, session_id: str) -> Response:
    """#3 deleteSession — DELETE /session/{id}."""
    return await _write_passthrough(
        request, method="DELETE", upstream_path=f"/session/{session_id}")


@router.post("/session/{session_id}/prompt_async")
async def prompt_async(request: Request, session_id: str) -> Response:
    """#4 promptAsync — POST (PromptPayload passthrough, 202/204 class)."""
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/session/{session_id}/prompt_async")


@router.post("/session/{session_id}/abort")
async def abort_session(request: Request, session_id: str) -> Response:
    """#5 abortSession — POST, no payload."""
    return await _write_passthrough(
        request, method="POST", upstream_path=f"/session/{session_id}/abort")


@router.post("/session/{session_id}/summarize")
async def summarize_session(request: Request, session_id: str) -> Response:
    """#6 summarize — POST (SummarizePayload passthrough)."""
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/session/{session_id}/summarize")


@router.post("/session/{session_id}/fork")
async def fork_session(request: Request, session_id: str) -> Response:
    """#7 fork — POST (payload [NoContent, ForkPayload]; ``messageID`` is
    an optional BODY JSON field, never a query param)."""
    return await _write_passthrough(
        request, method="POST", upstream_path=f"/session/{session_id}/fork")


@router.post("/session/{session_id}/revert")
async def revert_session(request: Request, session_id: str) -> Response:
    """#8 revert — POST (RevertPayload {messageID, partID?} body)."""
    return await _write_passthrough(
        request, method="POST", upstream_path=f"/session/{session_id}/revert")


@router.post("/session/{session_id}/permissions/{permission_id}")
async def respond_permission(
    request: Request, session_id: str, permission_id: str,
) -> Response:
    """#9 respondPermission — POST ({response} body)."""
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/session/{session_id}/permissions/{permission_id}")


@router.post("/question/{request_id}/reply")
async def reply_question(request: Request, request_id: str) -> Response:
    """#10 replyQuestion — POST (ReplyPayload passthrough)."""
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/question/{request_id}/reply")


@router.post("/question/{request_id}/reject")
async def reject_question(request: Request, request_id: str) -> Response:
    """#11 rejectQuestion — POST, no payload."""
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/question/{request_id}/reject")


@router.post("/session/{session_id}/command")
async def session_command(request: Request, session_id: str) -> Response:
    """#12 command — POST (CommandPayload passthrough)."""
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/session/{session_id}/command")
