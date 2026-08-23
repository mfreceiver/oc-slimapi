"""Native-v4 overflow and admission tests for the token stream.

The v4-only subscriber has one bounded runtime queue. Replay/meta are emitted
by the route before the subscriber enters live fanout. Backpressure is
fail-closed via a terminal STOP sentinel only; reconnect alignment is owned by
Last-Event-ID.
"""

from __future__ import annotations

import asyncio

import pytest

from oc_slimapi.sse.tokenstream.frames import STOP, _delta_frame, _resync_frame
from oc_slimapi.sse.tokenstream.models import _TokenMetrics
from oc_slimapi.sse.tokenstream.subscriber import (
    TokenStreamRegistry,
    TokenSubscriber,
    TokenSubscriberCapacityError,
)


@pytest.fixture
def metrics() -> _TokenMetrics:
    return _TokenMetrics()


@pytest.fixture
def sample_frame() -> bytes:
    return _delta_frame(("s1", "m1", "p1"), "x")


def _sub(metrics: _TokenMetrics, **overrides) -> TokenSubscriber:
    params = {
        "session_id": "s1",
        "metrics": metrics,
        "queue_items": 2,
        "buffer_bytes": 4096,
        "max_frame_bytes": 1024,
    }
    params.update(overrides)
    return TokenSubscriber(**params)


def _drain(sub: TokenSubscriber) -> list[object]:
    items: list[object] = []
    while True:
        try:
            items.append(sub.queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


class TestNativeV4RuntimeQueue:
    def test_fifo_preserves_data_and_control_order(
        self, metrics: _TokenMetrics, sample_frame: bytes,
    ) -> None:
        sub = _sub(metrics, queue_items=4)
        control = _resync_frame("s1", "replay_gap")

        assert sub.put(sample_frame) is True
        assert sub.put(control) is True

        first = sub.queue.get_nowait()
        sub.ack(first)
        second = sub.queue.get_nowait()
        sub.ack(second)
        assert [first, second] == [sample_frame, control]
        assert sub.queued_bytes == 0

    def test_terminal_control_replaces_queued_data(
        self, metrics: _TokenMetrics, sample_frame: bytes,
    ) -> None:
        sub = _sub(metrics, queue_items=4)
        assert sub.put(sample_frame) is True

        sub.terminate("replay_gap")

        assert _drain(sub) == [_resync_frame("s1", "replay_gap"), STOP]
        assert sub.queued_bytes == 0

    def test_item_overflow_clears_backlog_and_seals_stop_only(
        self, metrics: _TokenMetrics, sample_frame: bytes,
    ) -> None:
        sub = _sub(metrics)
        assert sub.put(sample_frame) is True
        assert sub.put(sample_frame) is True

        assert sub.put(sample_frame) is False

        assert sub.closed is True
        assert sub.forced_disconnects == 1
        assert sub.dropped_frames == 1
        assert metrics.dropped_frames_total == 1
        assert sub.queued_bytes == 0
        assert _drain(sub) == [STOP]

    def test_byte_overflow_clears_backlog_and_seals_stop_only(
        self, metrics: _TokenMetrics, sample_frame: bytes,
    ) -> None:
        sub = _sub(
            metrics,
            queue_items=64,
            buffer_bytes=len(sample_frame) * 2 - 1,
        )
        assert sub.put(sample_frame) is True
        assert sub.put(sample_frame) is False
        assert _drain(sub) == [STOP]
        assert sub.queued_bytes == 0

    def test_oversized_frame_drops_without_disconnect(
        self, metrics: _TokenMetrics,
    ) -> None:
        sub = _sub(metrics, max_frame_bytes=50)
        big = _delta_frame(("s1", "m1", "p1"), "x" * 1000)

        assert sub.put(big) is False
        assert sub.closed is False
        assert sub.dropped_frames == 1
        assert metrics.dropped_frames_total == 1
        assert _drain(sub) == []

    def test_closed_subscriber_silently_rejects_later_frames(
        self, metrics: _TokenMetrics, sample_frame: bytes,
    ) -> None:
        sub = _sub(metrics, queue_items=1)
        assert sub.put(sample_frame) is True
        assert sub.put(sample_frame) is False
        before = metrics.dropped_frames_total

        assert sub.put(sample_frame) is False
        assert metrics.dropped_frames_total == before
        assert _drain(sub) == [STOP]

    def test_ack_is_clamped_and_stop_is_not_accounted(
        self, metrics: _TokenMetrics, sample_frame: bytes,
    ) -> None:
        sub = _sub(metrics)
        assert sub.put(sample_frame) is True
        item = sub.queue.get_nowait()
        sub.ack(item)
        assert sub.queued_bytes == 0
        sub.ack(sample_frame)
        sub.ack(STOP)
        assert sub.queued_bytes == 0


class _ClosingTokenHub:
    def __init__(self, metrics: _TokenMetrics) -> None:
        self._metrics = metrics
        self._subs_by_sid: dict[str, set[TokenSubscriber]] = {}
        self._pending: dict = {}
        self.start_calls = 0
        self.stop_calls = 0
        self.detach_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def attach_subscriber(self, sid: str, sub: TokenSubscriber) -> None:
        sub.closed = True

    def has_subscriber(self, sid: str, sub: TokenSubscriber) -> bool:
        return False

    def detach_subscriber(self, sid: str, sub: TokenSubscriber) -> None:
        self.detach_calls += 1


class TestRegistryAttachFailure:
    def _registry(self, hub: _ClosingTokenHub) -> TokenStreamRegistry:
        return TokenStreamRegistry(
            hub,
            hub_registry=None,
            max_subscribers=5,
            queue_items=64,
            buffer_bytes=4096,
            max_frame_bytes=1024,
        )

    def test_attach_failure_does_not_increment_ledger(
        self, metrics: _TokenMetrics,
    ) -> None:
        hub = _ClosingTokenHub(metrics)
        registry = self._registry(hub)

        with pytest.raises(TokenSubscriberCapacityError):
            registry.subscribe("s1")

        assert registry.total_subscribers == 0
        assert registry.rejected_total == 1
        assert hub.start_calls == 1
        assert hub.stop_calls == 1

    def test_attach_failure_keeps_loop_for_existing_subscriber(
        self, metrics: _TokenMetrics,
    ) -> None:
        hub = _ClosingTokenHub(metrics)
        registry = self._registry(hub)
        registry.total_subscribers = 1

        with pytest.raises(TokenSubscriberCapacityError):
            registry.subscribe("s2")

        assert hub.start_calls == 1
        assert hub.stop_calls == 0

    def test_failed_attach_does_not_leak_capacity(
        self, metrics: _TokenMetrics,
    ) -> None:
        class FirstCloseThenAttach(_ClosingTokenHub):
            def __init__(self, metrics: _TokenMetrics) -> None:
                super().__init__(metrics)
                self.calls = 0

            def attach_subscriber(self, sid: str, sub: TokenSubscriber) -> None:
                self.calls += 1
                if self.calls == 1:
                    sub.closed = True
                    return
                self._subs_by_sid.setdefault(sid, set()).add(sub)

            def has_subscriber(self, sid: str, sub: TokenSubscriber) -> bool:
                return sub in self._subs_by_sid.get(sid, set())

        hub = FirstCloseThenAttach(metrics)
        registry = self._registry(hub)
        with pytest.raises(TokenSubscriberCapacityError):
            registry.subscribe("s1")

        sub = registry.subscribe("s2")
        assert sub.closed is False
        assert registry.total_subscribers == 1


def test_native_v4_subscriber_has_no_handshake_or_wire_mode_state(
    metrics: _TokenMetrics,
) -> None:
    sub = _sub(metrics)
    for name in (
        "wire_v4",
        "begin_handshake",
        "end_handshake",
        "_in_handshake",
        "handshake_items",
        "handshake_buffer_bytes",
        "_handshake_overflow",
    ):
        assert not hasattr(sub, name)
