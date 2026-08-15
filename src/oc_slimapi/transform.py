"""Bounded transform pool: admission control + off-thread parse/project/serialize/gzip.

Skeleton/messages routes need to run::

    orjson.loads -> skeleton projection -> orjson.dumps -> (optional) gzip

That entire chain is CPU-bound (deepcopy under the hood, plus gzip level 6).
Running it inline on the uvicorn event loop blocks SSE heartbeats and other
light async work; buffering the upstream body first and only *then* acquiring
admission allows many concurrent large bodies to exhaust the sidecar's
``MemoryMax`` before any of them gets a chance to be transformed.

This module fixes both by exposing a single :class:`TransformPool` that pairs:

* an ``asyncio.Semaphore`` sized to ``max_transforms`` for admission control,
  acquired **before** the upstream GET; and
* a ``ThreadPoolExecutor`` with the same worker count for the CPU work.

The pool's async-context-manager protocol means a route can write::

    async with request.app.state.transforms as pool:
        response = await upstream.send(..., stream=True)
        body, _ = await read_with_cap(response, pool.config.max_response_bytes)
        encoded, extra = await pool.offload(strip_diagnostics_and_pack, body, ...)

and the admission slot is released on exit even if the upstream errors out.

SSE routes (``sse/hub.py``, ``routes/events.py``) never touch this module, so
the event loop stays free for heartbeats regardless of transform load.

RSS / memory model (P1-30):
    The worst-case RSS attributable to the transform pool is approximately::

        max_transforms × (max_response_bytes + projection_overhead)

    where ``projection_overhead`` is the skeleton tree + serialised output
    (typically < 2× the upstream body for skeleton projections, which are
    smaller than the source). Admission is acquired BEFORE the upstream GET,
    so at most ``max_transforms`` bodies are buffered at any time. The
    default ``max_transforms=1`` is the strongest protection: at most one
    body is buffered regardless of ``max_response_bytes``. Operators who raise
    ``max_transforms`` should verify ``max_transforms × max_response_bytes``
    stays well under the systemd ``MemoryMax`` — config.validate() rejects a
    product exceeding 512 MiB (see ``_MAX_TRANSFORM_TOTAL_BYTES`` in config.py).
"""

from __future__ import annotations

import asyncio
import functools
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import orjson

from .gzip_util import compress_if_beneficial
from .skeleton import (
    strip_diagnostics_message,
)


@dataclass(frozen=True, slots=True)
class TransformConfig:
    """Snapshot of the transform-pool knobs (see ``config.Settings.validate``)."""

    max_transforms: int
    transform_wait_seconds: float
    max_response_bytes: int


class TransformBusy(Exception):
    """Raised when admission times out — pool saturated, caller emits ``503``."""


def _pack_json(value: Any, accept_encoding: str | None) -> tuple[bytes, dict[str, str]]:
    """Serialize ``value`` to JSON bytes and optionally gzip them.

    Returns ``(payload, extra_headers)``; ``extra_headers`` always carries
    ``Vary: Accept-Encoding`` and adds ``Content-Encoding: gzip`` when
    compression is both negotiated and beneficial (see
    :func:`oc_slimapi.gzip_util.compress_if_beneficial`). Pure-CPU; safe to
    call from a worker thread.
    """
    encoded = orjson.dumps(value)
    return compress_if_beneficial(encoded, accept_encoding)


def strip_diagnostics_and_pack(
    body: bytes, *, accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entrypoint for the ``/full`` route: ``orjson.loads`` → strip the
    never-consumed LSP ``diagnostics`` map from the single message →
    ``dumps`` → (gzip).

    Applies the light in-place :func:`strip_diagnostics_message` scrub instead
    of the skeleton thinning, so a client expanding a thin skeleton fetches
    the WHOLE part (output / text / files / metadata siblings / ...) minus
    only the ``state.metadata.diagnostics`` map it never reads. The parse
    tree is owned solely by this worker (fresh ``orjson.loads``), so strip
    mutates it in place and skips a full ``deepcopy``.

    Raises :exc:`orjson.JSONDecodeError` on empty / non-JSON bodies — callers
    (the ``/full`` route) map that to 503 ``upstream_unavailable`` so a bad
    upstream 200 never escapes as a bare 500. Like the other worker
    entrypoints this is pure-CPU and runs in the bounded transform executor so
    the event loop stays free for SSE heartbeats.
    """
    # Empty body and garbage both raise JSONDecodeError (orjson treats
    # zero-length input as an empty document). Do not swallow here — the
    # route layer turns it into a structured 503.
    parsed = orjson.loads(body)
    if not isinstance(parsed, dict):
        # /full/{mid} expects a single message dict. A non-dict body (list/
        # null/scalar) would otherwise be passed through verbatim by
        # strip_diagnostics_message's shape-robustness guard (skeleton.py),
        # surfacing a malformed upstream 200 as a confusing 200 to the
        # client. Treat as malformed upstream → route maps to 503.
        raise ValueError("upstream single-message body is not a dict")
    projected = strip_diagnostics_message(parsed)
    return _pack_json(projected, accept_encoding)


async def read_with_cap(
    response: Any,
    max_bytes: int,
    *,
    chunk_size: int = 64 * 1024,
    on_read: Callable[[int], None] | None = None,
) -> tuple[bytes | None, int]:
    """Stream-read an httpx streaming response, aborting as soon as ``max_bytes`` is crossed.

    Returns ``(body, total_bytes)`` on success or ``(None, total_bytes)`` when
    the cap was exceeded. The None case means the caller has **not** buffered
    the entire oversize body — only the chunks read up to and including the
    one that crossed the cap (so total is at most ``max_bytes + chunk_size``).

    A non-positive ``max_bytes`` short-circuits to ``(None, 0)`` without
    touching the stream, so cumulative-budget callers (``messages_since``)
    can safely pass ``remaining = cap - total_so_far``.

    ``on_read`` (optional) is invoked with ``len(chunk)`` for every chunk
    pulled from the stream, immediately after accumulating into ``total`` and
    BEFORE the cap check. This unifies byte attribution across all three exit
    paths so callers need not stash separately:

    * **success** — one callback per chunk, summing to ``total``;
    * **cap-bail** — the chunk that crosses the cap is attributed, THEN
      ``(None, total)`` is returned (so the oversize read is accounted);
    * **mid-stream exception** (``aiter_bytes`` raises ``httpx.RequestError``)
      — every chunk read before the failure is attributed; the exception then
      propagates untouched. Without the callback the caller would see the
      exception and have no way to recover ``total``, silently undercounting
      ``upIn`` (P0-9).

    Typical route usage passes ``on_read=lambda n: stash_up_in(request, n)``
    and drops the post-call ``stash_up_in(request, n_read)`` (the callback is
    additive and equivalent on the success/cap paths, and is the ONLY way to
    attribute bytes on the exception path).
    """
    if max_bytes <= 0:
        return None, 0
    chunks: list[bytes] = []
    total = 0
    iterator: AsyncIterator[bytes] = response.aiter_bytes(chunk_size)
    async for chunk in iterator:
        total += len(chunk)
        if on_read is not None:
            on_read(len(chunk))
        if total > max_bytes:
            return None, total
        chunks.append(chunk)
    return b"".join(chunks), total


class TransformPool:
    """Admission semaphore paired with a bounded worker pool.

    Acquire via ``async with pool:`` **before** issuing the upstream GET so
    the admission slot covers the entire read+convert chain (admission
    bounds memory pressure; the executor bounds CPU contention). Inside the
    context, ``pool.offload(...)`` submits CPU work to the bounded
    ``ThreadPoolExecutor`` and awaits it via the event loop, keeping the
    loop free for SSE heartbeats while the worker churns.
    """

    def __init__(self, config: TransformConfig) -> None:
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_transforms)
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_transforms,
            thread_name_prefix="oc-slimapi-transform",
        )
        self._active = 0
        self._waiting = 0

    @property
    def config(self) -> TransformConfig:
        return self._config

    async def acquire(self, timeout: float | None = None) -> None:
        """Acquire admission, optionally bounded by an explicit per-attempt
        ``timeout`` (L2-CD-1 budget narrowing: callers that retry admission
        pass the REMAINING wall-clock budget so the worst-case cumulative
        wait stays within their total budget; a naive retry at the full
        ``transform_wait_seconds`` could wait N× that long).

        ``TransformBusy`` semantics are unchanged from the async-with
        protocol: raised when the wait (``timeout`` here, otherwise
        ``transform_wait_seconds``) elapses without admission. Callers must
        pair a successful acquire with exactly one :meth:`release`.
        """
        wait_seconds = (
            self._config.transform_wait_seconds if timeout is None else timeout
        )
        self._waiting += 1
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=wait_seconds,
                )
            except TimeoutError as exc:
                raise TransformBusy() from exc
        finally:
            self._waiting -= 1
        self._active += 1

    def release(self) -> None:
        """Release admission granted by :meth:`acquire` (or ``__aenter__``)."""
        self._active -= 1
        self._semaphore.release()

    async def __aenter__(self) -> "TransformPool":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._active -= 1
        self._semaphore.release()

    async def offload(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run a sync callable in the worker pool; kwargs supported via ``functools.partial``.

        The executor is the same one bounded by ``max_transforms``, so offload
        queueing is naturally bounded by admission (callers hold the slot
        while awaiting offload). ``run_in_executor`` only supports positional
        args natively, so we wrap kwargs cases in a partial.
        """
        loop = asyncio.get_running_loop()
        if kwargs:
            return await loop.run_in_executor(
                self._executor, functools.partial(func, *args, **kwargs),
            )
        return await loop.run_in_executor(self._executor, func, *args)

    def snapshot_metrics(self) -> dict[str, int]:
        """Return current transform pool admission state.

        ``active``: permits currently held (i.e., transforms in-flight),
        ``waiting``: acquirers blocked on the semaphore.

        Counters are maintained internally (P2-3) so callers
        (:class:`~oc_slimapi.sse.hub.HubRegistry`) do not reach into
        private ``asyncio.Semaphore`` fields. ``__aenter__`` increments
        ``_waiting`` before acquire and decrements it in a ``finally``
        (covers timeout/cancel/exception); ``_active`` is bumped only on a
        successful acquire and reversed in ``__aexit__`` before release.
        """
        return {"active": self._active, "waiting": self._waiting}

    def shutdown(self, wait_seconds: float = 10.0) -> None:
        """Drain in-flight workers bounded by ``wait_seconds`` (P1-41).

        ``ThreadPoolExecutor.shutdown(wait=True)`` blocks without a native
        timeout; a stuck or slow worker (large gzip, pathological input)
        would stall the event loop past the uvicorn graceful-shutdown window
        during hot reload / systemd stop. This bounds the drain:

        1. Cancel pending (not-yet-started) futures immediately.
        2. Wait for in-flight workers in a daemon thread, bounded by
           ``wait_seconds``.
        3. If the drain hasn't finished when the timeout fires, return
           anyway — the daemon thread keeps waiting in the background but
           does not block process exit, and the calling event loop is freed.

        Idempotent: subsequent calls are no-ops (the executor is already
        shut down; ``shutdown`` on a terminated executor returns immediately).
        """
        # Cancel pending futures; let running workers finish naturally.
        self._executor.shutdown(wait=False, cancel_futures=True)
        # Bounded wait for in-flight workers via a daemon thread so the
        # calling (event-loop) thread is never blocked past ``wait_seconds``.
        done = threading.Event()

        def _drain() -> None:
            try:
                self._executor.shutdown(wait=True)
            finally:
                done.set()

        watcher = threading.Thread(
            target=_drain, daemon=True,
            name="oc-slimapi-transform-drain",
        )
        watcher.start()
        done.wait(timeout=wait_seconds)
