"""Data models for the token stream accumulator (design §5.3).

Moved from :mod:`oc_slimapi.sse.token_hub`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


from .frames import _now_ms


@dataclass
class LivePart:
    """One in-flight text part (design §5.3).

    ``chunks`` is a list (NOT ``text += delta``) so appending is O(1) and
    the full text is materialized once, on demand, via
    ``"".join(chunks)``. ``byte_count`` is the UTF-8 sum of ``chunks``; it
    is the budget unit for the per-part and global memory caps (Stage C
    ``_reserve``). ``last_delta_ms`` feeds the Stage-B TTL retiree (only
    retires an idle LivePart when the session is known idle, not just
    quiet — bgpt NB#4) AND the Stage-C LRU eviction key (oldest by
    ``last_delta_ms`` is evicted first under global memory pressure).
    """

    chunks: list[str] = field(default_factory=list)
    byte_count: int = 0
    ended: bool = False
    last_delta_ms: int = field(default_factory=lambda: _now_ms())


@dataclass
class DeltaAccumulator:
    """Per-key flush window (design §5.4 C1).

    Chunk-list + UTF-8 byte counter. :meth:`drain` joins the chunks, clears
    the list, and resets ``byte_count`` so the accumulator is reusable for
    the next window. flush_loop calls ``drain()`` when either
    ``TOKEN_FLUSH_SECONDS`` (100ms) or ``TOKEN_FLUSH_BYTES`` (4KiB) trips.
    """

    chunks: list[str] = field(default_factory=list)
    byte_count: int = 0

    def append(self, text: str) -> None:
        """Append ``text`` and bump the UTF-8 byte counter. No-op on empty."""
        if not text:
            return
        self.chunks.append(text)
        self.byte_count += len(text.encode("utf-8"))

    def drain(self) -> str:
        """Join chunks, clear state, return the joined text.

        Resetting ``byte_count`` to 0 keeps the accumulator reusable across
        flush windows without the caller having to reconstruct it.
        """
        if not self.chunks:
            self.byte_count = 0
            return ""
        text = "".join(self.chunks)
        self.chunks.clear()
        self.byte_count = 0
        return text


@dataclass
class _TokenMetrics:
    """Counters surfaced via ``/slimapi/metrics`` (Stage D wires the endpoint).

    Stage A exercised ``orphan_deltas``; Stage C exercises
    ``flushed_frames_total`` / ``truncated_snapshots_total`` /
    ``token_memory_limit_total`` / ``dropped_frames_total``. Stage D will
    expose all of them under ``sse.tokenStream.*``.

    S-3a additive observability counters:
    - ``gzip_raw_bytes_total`` / ``gzip_compressed_bytes_total``
    - ``flush_duration_ms_total`` / ``flush_ticks_total``

    ``maxSubscriberQueueDepth`` is NOT a stored counter — it is a live gauge
    computed at snapshot time in
    :meth:`TokenStreamRegistry.snapshot_token_metrics` (max ``queue.qsize()``
    over attached subs).
    """

    orphan_deltas: int = 0
    flushed_frames_total: int = 0       # Native business-frame deliveries
    dropped_frames_total: int = 0       # Oversized/backpressured frames dropped
    truncated_snapshots_total: int = 0  # Compatibility key; native-v4 stays zero
    token_memory_limit_total: int = 0   # Stage C: resync{token_memory_limit} fans
    # 4.12.0 修订六 B-1: general business-frame publish failures on the
    # reserve→encode→append path (frame dropped + seq rolled back — the
    # historical "deliver the raw frame un-logged" degradation is gone).
    seq_publish_failures_total: int = 0
    # 4.12.0 修订六 B-2 (rev-2 修正 1, fail-closed): replayable-resync
    # publish failures AFTER eviction already cleared the part state —
    # every subscriber of the sid was force-terminated instead of being
    # left on a dead baseline.
    seq_resync_failclosed_total: int = 0
    # S-3a additive
    gzip_raw_bytes_total: int = 0
    gzip_compressed_bytes_total: int = 0
    flush_duration_ms_total: float = 0.0
    flush_ticks_total: int = 0
