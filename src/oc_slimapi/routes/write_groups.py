"""v3-contract §10.b — the 12 annexed WRITE endpoints (Batch C2), plus the
5..6 B4 additions (#13-#17: agent / model / revert three-step — non-consuming
directory, see the B4 section note below).

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
* the B4 additions (#13-#17) are **exceptions to directory consumption**:
  their upstream v2 session group resolves location per-sid via
  sessionLocationMiddleware and does NOT participate in directory routing —
  client ``?directory=`` is tolerated and dropped (never forwarded upstream,
  never an error), matching the questions/permissions non-consuming set
  semantics;
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
  ``Vary: Accept-Encoding`` (§6.2 terminal single value
  set: every §10.b write route consumes directory).

§16 修订二 (v4-contract, 2026-08-19 freeze — the POST equivalent action
family): three more POST routes live in this module (#18-#20 —
``POST /session/{sid}`` ≡ PATCH, ``POST …/delete`` ≡ DELETE,
``POST …/archive`` convenience w/ octet-level default synthesis),
gated per request on the v4 wire view + ``session.post-actions.v4 ∈
readiness.SATISFIED`` (§3.3 dynamic module read). Admitted requests run
the very ``_write_passthrough`` pipeline below — zero new wire semantics
except the archive synthesis; every non-admitted arrival (v3 baseline,
or gate-closed leakage) answers the pre-revision coded 404
``thin_route_not_found`` (see the §16.2 section note below).
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Request
from starlette.responses import Response

from .. import readiness as readiness_mod
from ..directory import validate_directory
from ..gzip_util import compress_if_beneficial, error_response
from ..selector import (
    DIRECTORY_QUERY_PARAM,
    _strip_query_keys,
    resolve_route_directory,
)
from ..traffic import stash_up_in, stash_up_out
from ..transform import read_with_cap
from ..turn_registry import extract_sid_from_path, is_turn_bumping_path
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
    """Workspace directory for a write route (§5.2 terminal, v3-only).

    The selector consumed ``?directory=`` into the scope stash (validated)
    — forward it as the ``X-Opencode-Directory`` header. The header is
    never read as a client channel (§5.7: retired; presence on the wire
    is rejected by the dispatch layer).
    """
    resolved = resolve_route_directory(request.scope, None)
    if resolved is not None:
        return validate_directory(resolved)
    return None


async def _write_passthrough(
    request: Request,
    *,
    method: str,
    upstream_path: str,
    preset_body: bytes | bytearray | None = None,
    content_type_override: str | None = None,
) -> Response:
    """The shared §10.b write pipeline (see module docstring).

    ``preset_body`` / ``content_type_override`` exist solely for the §16.2-c
    archive route: the octet-level default judgement must buffer (and
    cap-check, same loop) the request entity itself before deciding
    synthesis vs passthrough, then hands the surviving bytes in here — or
    the sidecar-synthesized entity with its frozen ``application/json``
    label. With both ``None`` (every other caller) the socket is read
    exactly as before, byte-for-byte unchanged.
    """
    config = request.app.state.config
    accept_encoding = request.headers.get("accept-encoding")

    # Request body: read once (caps at max_message_bytes — the repo's
    # request-size knob; 413 before any upstream call).
    if preset_body is None:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > config.max_message_bytes:
                return error_response(
                    "request_too_large", 413,
                    accept_encoding=accept_encoding,
                    limit=config.max_message_bytes,
                )
            body += chunk
    else:
        # §16.2-c: already buffered (and cap-checked by the identical
        # loop) by the archive route, or synthesized by the sidecar —
        # never re-read from the socket.
        body = bytearray(preset_body)

    # upOut accounting parity: the retired catch-all forwarder counted the
    # request bytes it relayed upstream; the annexed write pipeline counts
    # the buffered body it is about to send.
    if body:
        stash_up_out(request, len(body))

    directory = _resolve(request)
    headers = forward_upstream_headers(
        directory, request_id_from_scope(request.scope))
    content_type = request.headers.get("content-type")
    if content_type_override is not None:
        # §16.2-c synthesized entity: always application/json — the
        # client's content-type described the replaced (empty) entity
        # and is dropped with it. The octet judgement never read it.
        content_type = content_type_override
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

    # S2 turn-fence commit point (bump-before-send): prompt_async/abort
    # writes advance the per-sid turn counter before the upstream send —
    # this is where the retired catch-all forwarder used to bump (the
    # strong-fence contract tolerates holes on connection-level failure).
    if method == "POST" and is_turn_bumping_path(upstream_path):
        turn_registry = getattr(request.app.state, "turn_registry", None)
        sid = extract_sid_from_path(upstream_path)
        if turn_registry is not None and sid:
            turn_registry.bump_turn(sid)

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
    # — §6.2 terminal: single-value Vary, overwriting whatever
    # compress_if_beneficial set.
    resp_headers["Vary"] = "Accept-Encoding"
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


# --- §16 修订二：POST 等效动作族（v4-contract 2026-08-19 冻结；feature
# ``session.post-actions.v4``，§3.3 第 10 ID）。三条 POST 组合的命中面分两层：
#
# * **selector 层（过渡态 405，不在本模块）**：``?v=4`` 且门控未激活时，
#   selector 在 directory 消费前拦下 coded 405 ``method_not_applicable``
#   （§16.1/§8.3 链——§16.3 四位组合表第二行），请求永远到不了下面三条
#   路由；
# * **handler 层（本模块）**：请求一旦到达（selector 已放行的「v4 且
#   ``session.post-actions.v4 ∈ satisfied``」激活态，或 v3——selector 从不
#   拦 v3），按下述分叉：
#     - v4 + 门控激活 → §16.2 等效管线（就是上面 ``_write_passthrough``
#       的逐字节复用——sidecar 零新增语义，仅 archive 合成一例外）；
#     - 其余一切（v3 冻结基线；防御性覆盖门控关穿透）→ coded 404
#       ``thin_route_not_found``，与 4.0.0 catch-all 基线逐字节一致
#       （proxy.py 同一 error_response 路径：gzip 协商 + Vary，无 Allow）。
#
# v3 面：三条组合此前经 catch-all 答 404 ``thin_route_not_found``；路由注册
# 后由 handler 原样复现该答案（v3-contract §8.2 冻结不变）。

_POST_ACTIONS_FEATURE = "session.post-actions.v4"


def _post_actions_admitted(scope: dict) -> bool:
    """§16.2 handler-side admission: gate open (the v4 leg is structural).

    The gate reads ``readiness.SATISFIED`` dynamically at request time
    (same convention as the selector's boundary gate) — a flip batch
    reassigns the set and this predicate changes with zero edits here.
    (V2b: the historical ``wire_view_from_scope(scope) >= 4`` conjunct
    died with the 2026-08-21 narrowing — under the (4, 4) v4-only window
    ``wire_view_from_scope`` is constant 4 for every scope, so the
    comparison was vacuously true and admission reduces to the gate
    check; see selector.wire_view_from_scope.)
    """
    return _POST_ACTIONS_FEATURE in readiness_mod.SATISFIED


def _pre_revision_404(request: Request) -> Response:
    """The pre-revision answer for the three POST combos: coded 404
    ``thin_route_not_found`` (gzip-negotiated, ``Vary: Accept-Encoding``,
    no Allow / no-store) — byte-identical to the catch-all boundary these
    paths answered from before the routes existed (proxy.py)."""
    return error_response(
        "thin_route_not_found", 404,
        accept_encoding=request.headers.get("accept-encoding"),
    )


@router.post("/session/{session_id}")
async def post_update_session(request: Request, session_id: str) -> Response:
    """#18（§16.2-a）— POST /session/{id} ≡ PATCH /session/{id}.

    Admitted (v4 + ``session.post-actions.v4``): the request runs the very
    PATCH pipeline ``update_session`` uses — body/content-type verbatim,
    request/response caps, directory consumption, evaluation order,
    4xx-verbatim / 5xx→503 error mapping, no-store — byte-for-byte the
    controlled-write semantics; only the wire method the client chose
    differs. Non-admitted: pre-revision 404.
    """
    if not _post_actions_admitted(request.scope):
        return _pre_revision_404(request)
    return await _write_passthrough(
        request, method="PATCH", upstream_path=f"/session/{session_id}")


@router.post("/session/{session_id}/archive")
async def post_archive_session(request: Request, session_id: str) -> Response:
    """#20（§16.2-c）— POST /session/{id}/archive 便捷动作（加性）。

    缺省判据（octet 层，冻结）：请求实体长度 = 0 → 合成；非空实体（含空
    JSON ``{}``、仅空白字节）→ **一律不解析**、逐字节透传上游验证
    （sidecar 不预解析，与 PATCH 同则）。Content-Type 是否存在/取值不影
    响判据——随实体透传（合成时客户端 CT 随被替换的空实体一并丢弃）。

    合成体（冻结）：``Content-Type: application/json`` + JSON 字节恰
    ``{"time":{"archived":<ms>}}``（无空格紧凑形，``<ms>`` 为十进制毫秒
    整数）；``<ms>`` = 判空后立即读的 sidecar wall-clock epoch-ms
    （``time.time()*1000`` 取整——与 digest ``updatedAt`` 同源口径，不读
    上游）。合成或透传后均走 §16.2-a PATCH 等效管线（错误映射零偏差：
    上游 4xx 原样、5xx/网络 → 既有受控 503，无新错误码）。
    """
    if not _post_actions_admitted(request.scope):
        return _pre_revision_404(request)
    config = request.app.state.config
    accept_encoding = request.headers.get("accept-encoding")

    # Entity read + cap: the SAME loop/order the PATCH pipeline runs (413
    # request_too_large before any upstream call) — the judgement below
    # needs the buffered octets, so this route performs the read itself
    # and hands the bytes to the pipeline via preset_body (the socket is
    # read exactly once, never re-read).
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > config.max_message_bytes:
            return error_response(
                "request_too_large", 413,
                accept_encoding=accept_encoding,
                limit=config.max_message_bytes,
            )
        body += chunk

    if body:
        # Non-empty: no parse, no shape judgement — straight into the
        # PATCH pipeline with the buffered bytes (client content-type
        # forwarded verbatim, whatever it says).
        return await _write_passthrough(
            request, method="PATCH", upstream_path=f"/session/{session_id}",
            preset_body=bytes(body),
        )

    # Default synthesis — <ms> read immediately after the empty-check
    # (§16.2-c frozen evaluation point).
    archived_ms = int(time.time() * 1000)
    synthesized = (b'{"time":{"archived":'
                   + str(archived_ms).encode("ascii") + b'}}')
    return await _write_passthrough(
        request, method="PATCH", upstream_path=f"/session/{session_id}",
        preset_body=synthesized, content_type_override="application/json",
    )


@router.post("/session/{session_id}/delete")
async def post_delete_session(request: Request, session_id: str) -> Response:
    """#19（§16.2-b）— POST /session/{id}/delete ≡ DELETE /session/{id}.

    Admitted: request-entity handling is EXACTLY the DELETE route's
    (``delete_session``) — entity read, same ``max_message_bytes`` cap
    (413 same code, same order), content-type passthrough, body forwarded
    byte-for-byte, NO ignore-body branch. The upstream recursive
    child-delete + error-swallowing semantics are inherited as-is
    (owner ruling q1: non-idempotency acceptable — a repeated delete
    answers whatever the upstream answers, e.g. 404). Non-admitted:
    pre-revision 404.
    """
    if not _post_actions_admitted(request.scope):
        return _pre_revision_404(request)
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


# ---------------------------------------------------------------------------
# B4 六条加性端点（wire contract v3 → v4 B0 起草段；上游锚点
# opencode v1.18.18 ``packages/protocol/src/groups/session.ts:173-305``，
# v2 ``/api/session/...`` 前缀，非 legacy ``/session/...``）。
#
# 与既有 #1-#12 的关键差异：**directory 列 = 不消费**。上游 v2 session 组
# 经 sessionLocationMiddleware 按 sid 从 DB 解析 location，不消费 directory；
# 客户端带 ``?directory=`` 时宽容忽略（不转发上游、不报错），同
# questions/permissions 非消费集语义（_DIRECTORY_CONSUMING_PATTERNS 未收录
# 这些路径 → 无校验/无 stash → _resolve 返回 None，X-Opencode-Directory 头
# 自然不加）。selector 只 strip ``v``（宽容路由不碰 directory），故需在端点
# 内显式剥掉 query 里的 directory 键，上游 URL 不携带任何 directory 痕迹。
# ---------------------------------------------------------------------------


def _strip_directory_query(request: Request) -> None:
    """B4 非消费集目录宽容：剥掉 ``scope['query_string']`` 中的 ``directory``。

    与 selector 自身消费集的剥离同源（:meth:`_strip_query_keys` 字节保真，
    & 分段扫描，不解析引号/编码）——B4 路由不在消费集，selector 不会代剥，
    这里在进入上游管线前就地处理，使上游请求 URL 无 directory 痕迹。
    """
    request.scope["query_string"] = _strip_query_keys(
        request.scope.get("query_string", b"") or b"",
        frozenset({DIRECTORY_QUERY_PARAM}),
    )


@router.post("/session/{session_id}/agent")
async def session_agent(request: Request, session_id: str) -> Response:
    """#13 agent（B4）— POST，body ``{"agent":"<id>"}`` 透传 → 上游 204。

    Upstream: opencode v1.18.18 ``protocol/groups/session.ts:173``-305
    (v2 session group, ``/api/session/{sid}/agent``)。directory 不消费 —
    ``?directory=`` 宽容忽略、不转发。
    """
    _strip_directory_query(request)
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/api/session/{session_id}/agent")


@router.post("/session/{session_id}/model")
async def session_model(request: Request, session_id: str) -> Response:
    """#14 model（B4）— POST，body ``{"model":"<provider/model>"}`` 透传 →
    上游 204。

    Upstream: opencode v1.18.18 ``protocol/groups/session.ts`` (v2 session
    group, ``/api/session/{sid}/model``)。directory 不消费（同上）。
    """
    _strip_directory_query(request)
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/api/session/{session_id}/model")


@router.post("/session/{session_id}/revert/stage")
async def revert_stage(request: Request, session_id: str) -> Response:
    """#15 revert/stage（B4，三段式第 1 段）— POST，body
    ``{"messageID":"…","files"?:bool}`` 透传 → 上游 200 ``{data:…}``。

    Upstream: opencode v1.18.18 ``protocol/groups/session.ts`` (v2 session
    group, ``/api/session/{sid}/revert/stage``)。目录不消费（同上）；
    与既有单步 ``POST /session/{sid}/revert``（#8）路径不同不互截。
    """
    _strip_directory_query(request)
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/api/session/{session_id}/revert/stage")


@router.post("/session/{session_id}/revert/clear")
async def revert_clear(request: Request, session_id: str) -> Response:
    """#16 revert/clear（B4，三段式第 2 段）— POST，无 payload → 上游 204。

    Upstream: opencode v1.18.18 ``protocol/groups/session.ts`` (v2 session
    group, ``/api/session/{sid}/revert/clear``)。目录不消费（同上）。
    """
    _strip_directory_query(request)
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/api/session/{session_id}/revert/clear")


@router.post("/session/{session_id}/revert/commit")
async def revert_commit(request: Request, session_id: str) -> Response:
    """#17 revert/commit（B4，三段式第 3 段）— POST，无 payload → 上游 204。

    Upstream: opencode v1.18.18 ``protocol/groups/session.ts`` (v2 session
    group, ``/api/session/{sid}/revert/commit``)。目录不消费（同上）。
    """
    _strip_directory_query(request)
    return await _write_passthrough(
        request, method="POST",
        upstream_path=f"/api/session/{session_id}/revert/commit")
