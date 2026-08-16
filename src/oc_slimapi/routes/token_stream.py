"""``GET /slimapi/sessions/{sid}/stream`` — token-stream SSE (design §5.1).

Generates a per-session ``text/event-stream`` of in-flight text-part deltas
plus handshake / snapshot / terminal / resync frames, sourced from the
process-wide :class:`TokenStreamHub` accumulator.

Stage-D scope (design-token-stream.md §5.1 / §5.5 / §5.6 / §7):

* Path is exact ``/slimapi/sessions/{sid}/stream`` — it lives under
  ``/slimapi/**`` so the version selector (``?v=3`` terminal) applies for
  free (no route-level ``Depends``).
* Admission runs in :meth:`TokenStreamRegistry.subscribe` under the token
  cap (independent ledger, NOT ``MAX_TOTAL_SUBSCRIBERS``); overflow → 503
  ``sse_token_subscriber_limit`` + ``Retry-After``.
* **No SSE ``id:`` field, no replay buffer** — clients MUST NOT rely on
  ``id:`` for resumption. A ``Last-Event-ID`` header (value ignored) only
  triggers a leading ``resync{reconnect_no_replay, sessionID}`` frame.
* **Lever 2 — streaming gzip (§7, the first SSE gzip exception)**: when the
  client advertises ``Accept-Encoding: gzip`` the body is compressed with a
  per-connection ``zlib`` deflater and **``Z_SYNC_FLUSH`` after every
  complete SSE event block** (each yielded frame is a whole
  ``event:…\\ndata:…\\n\\n``). Flush alignment to event boundaries is
  critical: a half-event gzip flush would make the client's SSE parser see a
  truncated ``data:`` line. The control-plane ``/slimapi/events`` is NOT
  gzipped — this is the sole SSE exception (CHANGELOG note is Stage E).
  **v3 exception-to-the-exception (v3-contract §7.2, Batch D)**: a ``?v=3``
  stream is ALWAYS identity — no ``Content-Encoding``, no gzip deflater,
  frames raw — regardless of ``Accept-Encoding``; gzip negotiation remains
  a v2-only lever.
* Lifecycle: ``subscribe`` starts the flush loop on first-attach; the
  generator's ``finally`` detaches on disconnect / client-close, and the
  last-detach stops the loop (NB-C4). Idempotent and leak-free.
"""

from __future__ import annotations

import zlib

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..directory import validate_directory
from ..errors import CodedHTTPException
from ..gzip_util import accepts_gzip, json_response
from ..sse.token_hub import (
    STOP,
    TokenSubscriberCapacityError,
    _resync_frame,
    sse_frame,
)
from ..sse_observability import sse_close, sse_open

router = APIRouter(prefix="/slimapi", tags=["token-stream"])


def _accepts_gzip(request: Request) -> bool:
    return accepts_gzip(request.headers.get("accept-encoding"))


def _resolve_directory_conflict(request: Request, directory: str | None) -> None:
    """NB-D7 (design §5.1): guard ``directory`` query vs ``X-Opencode-Directory``
    header — same structural 400 ``directory_not_allowed`` as the messages
    route. The directory is otherwise a NO-OP for token-stream fanout (the
    accumulator keys on ``sid``, which is globally unique in single-user T3;
    directory does not change which frames a subscriber receives). The
    conflict check is kept because a query+header mismatch is structurally
    ambiguous regardless of whether the value is consumed downstream —
    slimapi refuses to guess which one to honour. Normalisation is applied
    for parity with the messages route even though the result is unused.

    §5.6 terminal: the multi-value pre-check — a ``?directory=`` with
    differing (normalised) values → 400 ``invalid_directory_selector``
    (the consuming-set-wide rule, evaluated unconditionally under the
    v3-only terminal state). After single-valuing, the inherited guard
    runs: query-only accepted no-op; query+header normalised-different →
    ``directory_not_allowed``. A header alone is retired at the dispatch
    layer (§5.7). This route is NOT dispatch-consumed — the stream query
    keeps its ``directory`` bytes.
    """
    getlist = getattr(request.query_params, "getlist", None)
    if getlist is None:  # mock/direct-invocation requests (httpx QueryParams)
        getlist = lambda key: list(request.query_params.get_list(key))  # noqa: E731
    values = [value for value in getlist("directory")]
    if len({(value.rstrip("/") or "/") for value in values}) > 1:
        raise CodedHTTPException(400, code="invalid_directory_selector")
    if directory is None:
        return
    header_dir = request.headers.get("x-opencode-directory")
    if header_dir:  # treat empty header as absent
        if (header_dir.rstrip("/") or "/") != (directory.rstrip("/") or "/"):
            raise CodedHTTPException(400, code="directory_not_allowed")
    # Normalise for parity (unused — directory does not filter fanout).
    validate_directory(directory)


@router.get("/sessions/{sid}/stream")
async def token_stream(request: Request, sid: str, directory: str | None = None):
    """Per-session token-stream SSE.

    ``directory`` query (design §5.1): a conflict with the
    ``X-Opencode-Directory`` header (trailing-slash-normalised values
    differ) → 400 ``directory_not_allowed`` (NB-D7, structural guard mirroring
    the messages route). When not conflicting, ``directory`` is a NO-OP:
    the accumulator keys on ``sid`` which is globally unique (single-user
    T3), so directory filtering does not change which frames this connection
    receives and does not open a second upstream connection.
    """
    # NB-D7: structural directory conflict guard (before admission).
    _resolve_directory_conflict(request, directory)
    registry = request.app.state.token_registry
    last_event_id = request.headers.get("last-event-id")

    try:
        subscriber = registry.subscribe(sid)
    except TokenSubscriberCapacityError as exc:
        body = {"code": exc.code, "limit": exc.limit, "current": exc.current}
        if exc.buffer_bytes is not None:
            body["bufferBytes"] = exc.buffer_bytes
        return json_response(
            body,
            status_code=503,
            headers={"Retry-After": "5"},
            accept_encoding=request.headers.get("accept-encoding"),
        )

    subscriber_id = subscriber.id
    # §7.2 terminal (v3-only): the meta first frame is the id channel (the
    # retired X-Slimapi-Subscriber-ID header is never produced), AND the
    # frozen "SSE 流不做 content-encoding（帧字节原样）" clause — the
    # stream is ALWAYS identity regardless of Accept-Encoding
    # (meta/handshake/business/resync frames raw).
    use_gzip = False
    # Pull the traffic ledger here so a missing / disabled ledger does not
    # crash the SSE path on the first yield.
    traffic_ledger = getattr(request.app.state, "traffic_ledger", None)

    async def generate():
        # One deflater per connection. wbits = MAX_WBITS | 16 (31) emits a
        # gzip stream (header on the first compress call). Z_SYNC_FLUSH after
        # every complete SSE event block guarantees the client can decompress
        # each flushed chunk into whole ``event:…\ndata:…\n\n`` frames.
        compressor = (
            zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
            if use_gzip
            else None
        )

        def encode(frame: bytes) -> bytes:
            if compressor is None:
                return frame
            raw_n = len(frame)
            out = compressor.compress(frame) + compressor.flush(zlib.Z_SYNC_FLUSH)
            subscriber.metrics.gzip_raw_bytes_total += raw_n
            subscriber.metrics.gzip_compressed_bytes_total += len(out)
            return out

        def _account(out_bytes: bytes) -> None:
            # Traffic accounting: the on-the-wire (post-gzip) frame bytes —
            # the token_stream_sse ``downOut`` owner.
            # Semantics: counts bytes handed to the ASGI ``send`` path
            # (the yielded frame after gzip encoding). This is the same
            #口径 as the middleware's ``downOut`` for non-SSE buckets: both
            # count bytes submitted to ASGI send, before any possible
            # send-failure from a client disconnect. If ASGI send fails
            # the frame has already been counted — this is intentional and
            # consistent (no send-failure rollback exists elsewhere).
            if traffic_ledger is None:
                return
            try:
                traffic_ledger.record_sse_downstream(
                    bucket="token_stream_sse", bytes_out=len(out_bytes),
                )
            except Exception:
                pass

        try:
            # v3 §9.1 (Batch A): sse_open row when the stream actually
            # starts; sse_close + same lifecycleId in the finally below.
            # ``getattr`` — direct route-invocation tests may pass mock
            # requests without ``.scope`` (helper then no-ops).
            lifecycle_id = sse_open(getattr(request, "scope", None), bucket="token_stream_sse")
            # §7.2 terminal: meta FIRST — before the handshake frames
            # subscribe() already enqueued AND the Last-Event-ID resync
            # replay below. ``tokens`` is frozen ``true`` on /stream (a
            # token stream always carries tokens). Identity stream —
            # encode() is the passthrough selected above.
            out = encode(sse_frame(
                {"subscriberId": subscriber_id, "tokens": True},
                event="slimapi.meta",
            ))
            _account(out)
            yield out
            # §5.5 step 1: Last-Event-ID (value ignored) → leading
            # reconnect_no_replay resync BEFORE server.connected. The
            # handshake (server.connected → snapshot …) was already enqueued
            # synchronously by subscribe()→attach_subscriber and sits behind
            # this leading frame on the wire.
            if last_event_id:
                out = encode(_resync_frame(sid, "reconnect_no_replay"))
                _account(out)
                yield out
            while True:
                item = await subscriber.queue.get()
                if item is STOP:
                    break
                # Mirror put(): only sized frames bump queued_bytes; STOP is a
                # control sentinel never entered into the byte ledger.
                subscriber.ack(item)
                out = encode(item)
                _account(out)
                yield out
        finally:
            # Detach via the registry so total_subscribers is decremented and
            # the flush loop is stopped on last-detach (NB-C4). Idempotent.
            registry.unsubscribe(subscriber)
            sse_close(getattr(request, "scope", None), bucket="token_stream_sse", lifecycle_id=lifecycle_id)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    # §7.2 terminal: frames are always identity → the representation does
    # not depend on Accept-Encoding, so no Vary is emitted (an
    # AE-independent representation needs no AE variance marker; the stream
    # is no-cache anyway). The retired X-Slimapi-Subscriber-ID header is
    # never produced (§1) — the slimapi.meta first frame's
    # ``subscriberId`` is the id channel.

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=headers,
    )
