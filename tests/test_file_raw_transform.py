"""TDD coverage for the strict transform-pool primitive used by file/raw."""

from __future__ import annotations

import asyncio
import threading

import pytest

from oc_slimapi.transform import TransformBusy, TransformConfig, TransformPool


def _pool(*, workers: int = 1, wait: float = 0.05) -> TransformPool:
    return TransformPool(TransformConfig(
        max_transforms=workers,
        transform_wait_seconds=wait,
        max_response_bytes=1024,
    ))


async def test_offload_strict_releases_after_success() -> None:
    pool = _pool()
    try:
        await pool.acquire()
        assert await pool.offload_strict(lambda: 7) == 7
        assert pool.snapshot_metrics() == {"active": 0, "waiting": 0}
    finally:
        pool.shutdown()


async def test_offload_strict_cancellation_keeps_permit_until_worker_finishes() -> None:
    pool = _pool()
    started = threading.Event()
    finish = threading.Event()

    def blocked() -> str:
        started.set()
        finish.wait(timeout=2)
        return "done"

    try:
        await pool.acquire()
        task = asyncio.create_task(pool.offload_strict(blocked))
        await asyncio.to_thread(started.wait, 2)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert pool.snapshot_metrics()["active"] == 1
        with pytest.raises(TransformBusy):
            await pool.acquire(timeout=0.01)

        finish.set()
        await asyncio.sleep(0.05)
        await pool.acquire(timeout=0.2)
        pool.release()
        assert pool.snapshot_metrics()["active"] == 0
    finally:
        finish.set()
        pool.shutdown()


async def test_offload_strict_done_cancellation_releases_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool()
    releases = 0
    original_release = pool.release

    def counted_release() -> None:
        nonlocal releases
        releases += 1
        original_release()

    monkeypatch.setattr(pool, "release", counted_release)

    async def cancel_after_done(future):
        while not future.done():
            await asyncio.sleep(0)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "shield", cancel_after_done)
    try:
        await pool.acquire()
        with pytest.raises(asyncio.CancelledError):
            await pool.offload_strict(lambda: "done")
        assert releases == 1
        assert pool.snapshot_metrics()["active"] == 0
    finally:
        pool.shutdown()


async def test_offload_strict_callback_cancellation_releases_exactly_once() -> None:
    pool = _pool()
    started = threading.Event()
    finish = threading.Event()
    releases = 0
    original_release = pool.release

    def counted_release() -> None:
        nonlocal releases
        releases += 1
        original_release()

    pool.release = counted_release

    def blocked() -> None:
        started.set()
        finish.wait(timeout=2)

    try:
        await pool.acquire()
        task = asyncio.create_task(pool.offload_strict(blocked))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert releases == 0

        finish.set()
        for _ in range(20):
            if releases == 1:
                break
            await asyncio.sleep(0.01)
        assert releases == 1
        assert pool.snapshot_metrics()["active"] == 0
    finally:
        finish.set()
        pool.shutdown()


async def test_existing_offload_context_manager_path_is_unchanged() -> None:
    pool = _pool()
    try:
        async with pool:
            assert await pool.offload(lambda value, *, suffix: value + suffix,
                                      "raw", suffix="-existing") == "raw-existing"
        assert pool.snapshot_metrics() == {"active": 0, "waiting": 0}
    finally:
        pool.shutdown()
