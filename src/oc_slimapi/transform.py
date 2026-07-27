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
"""

from __future__ import annotations

import asyncio
import functools
import gzip
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import orjson

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
    ``Vary: Accept-Encoding`` and adds ``Content-Encoding: gzip`` when the
    caller's ``Accept-Encoding`` header allows it. Pure-CPU; safe to call
    from a worker thread.
    """
    encoded = orjson.dumps(value)
    headers: dict[str, str] = {"Vary": "Accept-Encoding"}
    if "gzip" in (accept_encoding or "").lower():
        encoded = gzip.compress(encoded, compresslevel=6)
        headers["Content-Encoding"] = "gzip"
    return encoded, headers


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
    projected = strip_diagnostics_message(parsed)
    return _pack_json(projected, accept_encoding)


async def read_with_cap(
    response: Any,
    max_bytes: int,
    *,
    chunk_size: int = 64 * 1024,
) -> tuple[bytes | None, int]:
    """Stream-read an httpx streaming response, aborting as soon as ``max_bytes`` is crossed.

    Returns ``(body, total_bytes)`` on success or ``(None, total_bytes)`` when
    the cap was exceeded. The None case means the caller has **not** buffered
    the entire oversize body — only the chunks read up to and including the
    one that crossed the cap (so total is at most ``max_bytes + chunk_size``).

    A non-positive ``max_bytes`` short-circuits to ``(None, 0)`` without
    touching the stream, so cumulative-budget callers (``messages_since``)
    can safely pass ``remaining = cap - total_so_far``.
    """
    if max_bytes <= 0:
        return None, 0
    chunks: list[bytes] = []
    total = 0
    iterator: AsyncIterator[bytes] = response.aiter_bytes(chunk_size)
    async for chunk in iterator:
        total += len(chunk)
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

    @property
    def config(self) -> TransformConfig:
        return self._config

    async def __aenter__(self) -> "TransformPool":
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._config.transform_wait_seconds,
            )
        except TimeoutError as exc:
            raise TransformBusy() from exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
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

        Encapsulates :attr:`asyncio.Semaphore._value` and
        :attr:`asyncio.Semaphore._waiters` so callers
        (:class:`~oc_slimapi.sse.hub.HubRegistry`) do not reach into
        private semaphore fields.
        """
        waiters = self._semaphore._waiters
        if waiters is not None:
            waiting = len(waiters)
        else:
            waiting = 0
        return {
            "active": self._config.max_transforms - self._semaphore._value,
            "waiting": waiting,
        }

    def shutdown(self) -> None:
        """Drain in-flight workers (``wait=True``) without cancelling queued futures."""
        self._executor.shutdown(wait=True, cancel_futures=False)
