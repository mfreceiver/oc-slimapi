"""Unit tests for the transform pool, worker entrypoints, and the streaming cap reader.

These cover the three failure modes the fix targets:
* admission timeout (→ TransformBusy, surfaced as 503 transform_busy by routes)
* upstream body exceeding ``max_response_bytes`` (bail without buffering)
* event-loop freedom while a worker churns on a slow transform

Route-level integration tests live in ``test_messages_routes.py``.
"""
from __future__ import annotations

import asyncio
import gzip
import time

import httpx
import orjson
import pytest

from oc_slimapi.config import settings as _skel_config
from oc_slimapi.transform import (
    TransformBusy,
    TransformConfig,
    TransformPool,
    read_with_cap,
    strip_diagnostics_and_pack,
)


# ---------------------------------------------------------------------------
# strip_diagnostics_and_pack — /full LSP diagnostics strip
# ---------------------------------------------------------------------------


def _msg_with_diagnostics() -> dict:
    return {
        "info": {"id": "m1", "role": "assistant"},
        "parts": [
            {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
            {
                "id": "p2", "type": "tool", "messageID": "m1", "tool": "edit",
                "state": {
                    "status": "completed",
                    "metadata": {
                        "sessionId": "s1",
                        "description": "edit file",
                        "diagnostics": [
                            {"range": {"start": 0, "end": 1}, "severity": 1,
                             "message": "unused import"},
                        ],
                    },
                    "output": "the edited file contents",
                },
            },
        ],
    }


def test_strip_diagnostics_and_pack_removes_only_diagnostics():
    body = orjson.dumps(_msg_with_diagnostics())

    payload, headers = strip_diagnostics_and_pack(body, accept_encoding=None)

    decoded = orjson.loads(payload)
    tool_state = decoded["parts"][1]["state"]
    # diagnostics removed ...
    assert "diagnostics" not in tool_state["metadata"]
    # ... but every metadata sibling + the full output/text are preserved
    # (/full semantics — only diagnostics is touched).
    assert tool_state["metadata"] == {"sessionId": "s1", "description": "edit file"}
    assert tool_state["output"] == "the edited file contents"
    assert decoded["parts"][0]["text"] == "hello"
    assert headers["Vary"] == "Accept-Encoding"
    assert "Content-Encoding" not in headers


def test_strip_diagnostics_and_pack_applies_gzip_when_client_accepts_it():
    body = orjson.dumps(_msg_with_diagnostics())

    payload, headers = strip_diagnostics_and_pack(body, accept_encoding="gzip")

    assert payload[:2] == b"\x1f\x8b"
    assert headers["Content-Encoding"] == "gzip"
    decoded = orjson.loads(gzip.decompress(payload))
    assert "diagnostics" not in decoded["parts"][1]["state"]["metadata"]


def test_strip_diagnostics_and_pack_skips_gzip_for_tiny_body():
    """P1-31: a body below MIN_GZIP_BYTES is returned raw (gzip would make
    it larger due to header/footer overhead). The transform worker pack
    function uses compress_if_beneficial, not raw gzip."""
    from oc_slimapi.gzip_util import MIN_GZIP_BYTES
    tiny_msg = {"info": {"id": "m"}, "parts": []}
    body = orjson.dumps(tiny_msg)
    assert len(body) < MIN_GZIP_BYTES, "test body must be below threshold"

    payload, headers = strip_diagnostics_and_pack(body, accept_encoding="gzip")

    assert "Content-Encoding" not in headers
    assert payload == body  # raw, unchanged


def test_strip_diagnostics_message_is_in_place_and_keeps_empty_metadata():
    """Production path: orjson.loads trees have no shared aliases, so strip
    mutates in place (no deepcopy). Emptied ``metadata`` stays as ``{}``."""
    from oc_slimapi.skeleton import strip_diagnostics_message

    src = {
        "info": {"id": "m1"},
        "parts": [{
            "id": "p1", "type": "tool", "messageID": "m1",
            "state": {"metadata": {"diagnostics": [{"severity": 1}]}},
        }],
    }
    out = strip_diagnostics_message(src)
    # Same object returned (in-place).
    assert out is src
    # The emptied metadata container stays as {} (never dropped) — only the
    # diagnostics key is removed.
    assert out["parts"][0]["state"]["metadata"] == {}
    assert src["parts"][0]["state"]["metadata"] == {}


def test_strip_diagnostics_message_wrong_shape_passthrough():
    from oc_slimapi.skeleton import strip_diagnostics_message

    # A non-dict / non-list body (malformed upstream 200) has nothing to scrub —
    # returned as-is so the /full route still serves it, matching the prior
    # verbatim passthrough for non-conforming shapes (no 500).
    assert strip_diagnostics_message([]) == []
    assert strip_diagnostics_message("scalar") == "scalar"


def test_strip_diagnostics_and_pack_rejects_empty_and_invalid_json():
    """Empty / garbage upstream bodies raise orjson.JSONDecodeError so the
    route layer can map them to 503 upstream_unavailable (no bare 500)."""
    with pytest.raises(orjson.JSONDecodeError):
        strip_diagnostics_and_pack(b"", accept_encoding=None)
    with pytest.raises(orjson.JSONDecodeError):
        strip_diagnostics_and_pack(b"not-json", accept_encoding=None)


# ---------------------------------------------------------------------------
# read_with_cap — bail-on-cap without buffering the whole body
# ---------------------------------------------------------------------------


class _FakeStreamingResponse:
    """Minimal stand-in for an httpx streaming response: yields chunk_size bytes per call.

    Tracks how many bytes the consumer actually pulled from the iterator so we
    can assert the cap-bail does not drain the entire upstream stream.
    """

    def __init__(self, total_bytes: int, chunk_size: int = 1024) -> None:
        self._total = total_bytes
        self._chunk = chunk_size
        self.produced = 0

    async def aiter_bytes(self, chunk_size: int):
        # Honor the requested chunk_size (the cap reader always passes one).
        while self.produced < self._total:
            n = min(chunk_size, self._total - self.produced)
            self.produced += n
            yield b"x" * n


async def test_read_with_cap_returns_full_body_when_under_cap():
    fake = _FakeStreamingResponse(total_bytes=2048)
    body, total = await read_with_cap(fake, max_bytes=4096, chunk_size=512)
    assert body == b"x" * 2048
    assert total == 2048
    # Whole stream consumed.
    assert fake.produced == 2048


async def test_read_with_cap_bails_shortly_after_cap_without_buffering_full_body():
    cap = 4 * 1024
    total_streamed = 256 * 1024  # 64x the cap; classic OOM-upstream scenario.
    fake = _FakeStreamingResponse(total_bytes=total_streamed, chunk_size=1024)

    body, total = await read_with_cap(fake, max_bytes=cap, chunk_size=1024)

    # None means cap exceeded — caller emits 413.
    assert body is None
    # Total observed is bounded by cap + one chunk (we stop at the chunk that
    # crossed the line, not after draining the whole upstream).
    assert cap < total <= cap + 1024
    # Critical: we did NOT read the entire 256 KiB stream before bailing.
    assert fake.produced == total
    assert fake.produced < total_streamed / 8


async def test_read_with_cap_handles_zero_or_negative_budget_without_iteration():
    fake = _FakeStreamingResponse(total_bytes=512)
    # Cumulative-budget callers pass `cap - already_consumed`, which can hit 0.
    body, total = await read_with_cap(fake, max_bytes=0)
    assert body is None
    assert total == 0
    assert fake.produced == 0  # never touched the stream


# ---------------------------------------------------------------------------
# on_read callback (P0-9): unify byte attribution across all three exit paths
# (success / cap-bail / mid-stream exception) so already-read bytes are never
# lost from upIn.
# ---------------------------------------------------------------------------


class _FailingStreamingResponse:
    """Stand-in for an httpx streaming response that yields a few chunks then
    raises ``httpx.RequestError`` mid-stream (simulates a connection reset)."""

    def __init__(self, chunks: list[bytes], fail_after: int) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    async def aiter_bytes(self, chunk_size: int):
        for i, chunk in enumerate(self._chunks):
            if i >= self._fail_after:
                raise httpx.ReadError("simulated mid-stream disconnect")
            yield chunk


async def test_read_with_cap_on_read_success_path_sums_to_total():
    """on_read fires once per chunk on the success path; the sum of callback
    values equals ``total`` (additive equivalence to the old post-call stash)."""
    fake = _FakeStreamingResponse(total_bytes=4096, chunk_size=512)
    seen: list[int] = []
    body, total = await read_with_cap(
        fake, max_bytes=8192, chunk_size=512,
        on_read=lambda n: seen.append(n),
    )
    assert body == b"x" * 4096
    assert total == 4096
    assert sum(seen) == total
    assert len(seen) == 4096 // 512  # one callback per chunk


async def test_read_with_cap_on_read_cap_bail_attributes_oversize_read():
    """on_read fires for every chunk up to and INCLUDING the one that crosses
    the cap; the sum equals ``total`` (which is > cap). This preserves the B1
    unified cap-bail upIn convention."""
    cap = 4 * 1024
    fake = _FakeStreamingResponse(total_bytes=256 * 1024, chunk_size=1024)
    seen: list[int] = []
    body, total = await read_with_cap(
        fake, max_bytes=cap, chunk_size=1024,
        on_read=lambda n: seen.append(n),
    )
    assert body is None
    assert cap < total <= cap + 1024
    assert sum(seen) == total, (
        "on_read sum must equal total so cap-bail bytes are fully attributed"
    )


async def test_read_with_cap_on_read_mid_stream_exception_attributes_read_bytes():
    """P0-9 regression: when ``aiter_bytes`` raises ``httpx.RequestError``
    mid-stream, ``on_read`` has already fired for every chunk read before the
    failure. Without the callback the caller would see the exception and have
    no way to recover ``total``, silently undercounting upIn."""
    chunks = [b"aaaa", b"bbbb", b"cccc"]  # 12 bytes before failure
    fake = _FailingStreamingResponse(chunks=chunks, fail_after=2)
    seen: list[int] = []
    with pytest.raises(httpx.ReadError):
        await read_with_cap(
            fake, max_bytes=4096, chunk_size=64,
            on_read=lambda n: seen.append(n),
        )
    # The two chunks before the failure were attributed via the callback.
    assert seen == [4, 4]
    assert sum(seen) == 8


async def test_read_with_cap_on_read_none_is_backward_compatible():
    """on_read=None (default) preserves the original behaviour: no callback,
    same (body, total) return — all existing call sites that don't pass
    on_read keep working unchanged."""
    fake = _FakeStreamingResponse(total_bytes=2048, chunk_size=512)
    body, total = await read_with_cap(fake, max_bytes=4096, chunk_size=512)
    assert body == b"x" * 2048
    assert total == 2048


# ---------------------------------------------------------------------------
# TransformPool — admission + offload semantics
# ---------------------------------------------------------------------------


def _pool(**overrides) -> TransformPool:
    defaults = dict(
        max_transforms=1,
        transform_wait_seconds=1.0,
        max_response_bytes=64 * 1024 * 1024,
    )
    defaults.update(overrides)
    return TransformPool(TransformConfig(**defaults))


async def test_admission_times_out_when_pool_is_saturated():
    pool = _pool(max_transforms=1, transform_wait_seconds=0.1)
    try:
        async with pool:
            # The single admission slot is held; re-entering must time out
            # and raise TransformBusy (route turns this into 503).
            with pytest.raises(TransformBusy):
                async with pool:
                    pass
    finally:
        pool.shutdown()


async def test_admission_releases_on_block_exit_even_on_exception():
    pool = _pool(max_transforms=1, transform_wait_seconds=0.1)
    try:
        class _Boom(Exception):
            pass

        with pytest.raises(_Boom):
            async with pool:
                raise _Boom()
        # If release didn't happen, this second entry would time out.
        async with pool:
            pass
    finally:
        pool.shutdown()


async def test_offload_runs_callable_in_worker_with_kwargs():
    pool = _pool(max_transforms=2)

    def func(a, *, b):
        return a + b

    try:
        assert await pool.offload(func, 1, b=2) == 3
        # positional-only path also works
        assert await pool.offload(func, 5, b=10) == 15
    finally:
        pool.shutdown()


async def test_offload_does_not_block_event_loop_during_slow_worker():
    """The whole point of the offload: a long-running transform must not park
    the event loop. While the worker sleeps, a cooperative async task should
    complete well before the worker returns."""
    pool = _pool(max_transforms=1)
    try:
        async with pool:
            slow_task = asyncio.create_task(pool.offload(time.sleep, 0.5))

            # Let the worker actually start.
            await asyncio.sleep(0.05)

            # Now race a light cooperative task against the in-flight worker.
            # If the event loop were blocked, this would take ~0.5s too.
            light_start = time.monotonic()
            await asyncio.sleep(0.05)
            light_elapsed = time.monotonic() - light_start

            assert light_elapsed < 0.15, (
                f"event loop appears blocked: light task took {light_elapsed:.3f}s"
            )

            await slow_task
    finally:
        pool.shutdown()


def test_shutdown_is_idempotent():
    pool = _pool()
    pool.shutdown()
    pool.shutdown()  # second shutdown must not raise


def test_shutdown_with_slow_worker_returns_within_timeout():
    """P1-41: shutdown(wait_seconds=N) must return within ~N even if a
    worker is still running. Previously shutdown() blocked on
    executor.shutdown(wait=True) with no timeout — a stuck worker would
    stall the event loop past the uvicorn graceful-shutdown window."""
    pool = _pool(max_transforms=1)
    # Submit a slow task directly to the executor (bypass admission) so it's
    # in-flight when shutdown is called.
    pool._executor.submit(time.sleep, 3.0)
    # Let the worker actually start.
    time.sleep(0.05)
    start = time.monotonic()
    pool.shutdown(wait_seconds=0.2)
    elapsed = time.monotonic() - start
    # Must return well within the slow worker's 3s duration.
    assert elapsed < 1.0, (
        f"shutdown took {elapsed:.2f}s (expected < 1s with 0.2s timeout)"
    )


def test_shutdown_default_wait_seconds_is_10():
    """The default wait_seconds is 10 (production graceful window)."""
    pool = _pool(max_transforms=1)
    import inspect
    sig = inspect.signature(pool.shutdown)
    assert sig.parameters["wait_seconds"].default == 10.0
    pool.shutdown(wait_seconds=0.01)  # fast cleanup
