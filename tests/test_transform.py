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

import orjson
import pytest

from oc_slimapi.config import settings as _skel_config
from oc_slimapi.transform import (
    TransformBusy,
    TransformConfig,
    TransformPool,
    project_and_pack,
    read_with_cap,
    strip_diagnostics_and_pack,
)


# ---------------------------------------------------------------------------
# Worker entrypoints
# ---------------------------------------------------------------------------


def _sample_messages() -> list[dict]:
    return [
        {
            "info": {"id": "m1", "role": "user"},
            "parts": [
                {"id": "p1", "type": "text", "messageID": "m1", "text": "hello"},
                {
                    "id": "p2", "type": "tool", "messageID": "m1", "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "ls", "debug": "drop me"},
                        "output": "x" * (_skel_config.skeleton_inline_output_max_bytes + 1000),
                    },
                },
            ],
        }
    ]


def test_project_and_pack_round_trips_skeleton_message():
    body = orjson.dumps(_sample_messages()[0])
    from oc_slimapi.skeleton import skeleton_message

    payload, _ = project_and_pack(body, accept_encoding=None)
    assert orjson.loads(payload) == skeleton_message(_sample_messages()[0])


def test_project_and_pack_applies_gzip_when_client_accepts_it():
    body = orjson.dumps(_sample_messages()[0])

    payload, headers = project_and_pack(body, accept_encoding="gzip, br")

    # gzip magic bytes.
    assert payload[:2] == b"\x1f\x8b"
    assert headers["Content-Encoding"] == "gzip"
    # Round-trip back to the skeleton contract.
    decoded = orjson.loads(gzip.decompress(payload))
    assert decoded["parts"][1]["state"]["input"] == {"command": "ls"}


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
