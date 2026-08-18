"""Bounded ring replay log for v4 SSE replay (design-v4-sse-replay.md §3.4).

B3b-1 scope — **pure data-structure layer only**:

* three-dimensional bounds + ring overwrite: ``count`` (frames per domain),
  ``bytes`` (process-wide total), ``ttl_s`` (per-frame age);
* per-domain monotonic ``seq`` assignment starting at 1 — token tombstones
  (``message.removed``) consume a seq exactly like a business frame, keeping
  the ID sequence hole-free (§3.5 rev-1裁决 / REPLAY-012);
* process-level ``epoch`` — random 16-hex boot nonce (NOT wall-clock,
  unordered, never compared by magnitude); restart always changes it, SSE
  reconnects within one process never do;
* persistent upstream-loss barriers (S-B01④ frozen semantics): per-domain
  low-watermark written at first confirmed upstream loss; any reconnect
  cursor ``seq <= watermark`` → ``reconnect_no_replay`` (禁跨 barrier 补帧);
  barriers are metadata, immune to count/bytes/TTL eviction.

Deliberately NOT here (B3b-2 wiring lane in events/tokenstream): SSE frame
serialization, ``id:`` header generation, Last-Event-ID syntax parsing
(classification steps ① syntax / ② endpoint+sid label), resync frame
fanout, meta/heartbeat frames. This module consumes already-parsed inputs
(``domain``, ``after_seq``, ``epoch``) and returns structured outcomes the
caller translates to wire behaviour. Classification steps ③ (epoch) and ④
(barrier → window → gap) live in :meth:`ReplayLog.replay` with the frozen
short-circuit order.

Domain model (S-B01③): the GLOBAL domain (key ``"g"`` — the single
whole-instance sequence of ``/events``) plus one lazily-created domain per
subscribed sid (key ``"t:<sid>"`` — the per-sid token-stream sequence).
Domain keys deliberately mirror the ``id:`` label grammar so B3b-2 can map
mechanically.

Concurrency: asyncio single-thread model — ``append``/``replay`` may be
called from different tasks on the loop; no locks (same style as
GlobalHub / TokenStreamHub).
"""

from __future__ import annotations

import math
import re
import secrets
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable, Union

import orjson

__all__ = [
    "DEFAULT_REPLAY_MAX_BYTES",
    "DEFAULT_REPLAY_MAX_COUNT",
    "DEFAULT_REPLAY_TTL_S",
    "FRAME_KIND_BUSINESS",
    "FRAME_KIND_TOMBSTONE",
    "GLOBAL_DOMAIN",
    "RESYNC_EPOCH_CHANGED",
    "RESYNC_REPLAY_EXPIRED",
    "RESYNC_REPLAY_GAP",
    "RESYNC_RECONNECT_NO_REPLAY",
    "ReplayEntry",
    "ReplayFrames",
    "ReplayIgnoreReset",
    "ReplayLog",
    "ReplayOutcome",
    "ReplayResync",
    "new_epoch",
    "token_domain",
]

# ---------------------------------------------------------------------------
# Constants (design-v4-sse-replay §3.4 proposal values — tunable via env,
# NOT wire-visible; production wiring goes through Settings → ReplayLog ctor).
# ---------------------------------------------------------------------------

#: Global-stream domain key. Per-sid token domains are ``"t:<sid>"`` — the
#: sid itself can never collide with this key.
GLOBAL_DOMAIN = "g"

#: Frame kinds. ``business`` = digest/q/p/error/token frame (gets ``id:``);
#: ``tombstone`` = token-domain ``message.removed`` lightweight revocation
#: frame — replayed WITH its ``id:`` and consuming its seq (no holes).
FRAME_KIND_BUSINESS = "business"
FRAME_KIND_TOMBSTONE = "tombstone"

# resync reason value domain (v4-contract §7.2 frozen — additive extension
# surface). These are the ONLY reasons the log layer can decide.
RESYNC_EPOCH_CHANGED = "epoch_changed"
RESYNC_REPLAY_EXPIRED = "replay_expired"
RESYNC_REPLAY_GAP = "replay_gap"
RESYNC_RECONNECT_NO_REPLAY = "reconnect_no_replay"

DEFAULT_REPLAY_MAX_COUNT = 2048                 # frames per domain
DEFAULT_REPLAY_MAX_BYTES = 64 * 1024 * 1024     # 64 MiB process-wide total
DEFAULT_REPLAY_TTL_S = 900.0                    # 15 min per-frame age

# epoch = random boot nonce, exactly 16 lowercase hex chars (§7.1 frozen).
_EPOCH_RE = re.compile(r"^[0-9a-f]{16}$")


def new_epoch() -> str:
    """Generate a fresh process-level epoch (16-hex random boot nonce).

    ``secrets.token_hex(8)`` → 16 lowercase hex chars. Unordered by design
    (never compared by magnitude — only equality); regenerated per process
    boot, never per SSE reconnect.
    """
    return secrets.token_hex(8)


def token_domain(sid: str) -> str:
    """Domain key for a per-sid token stream (``t:<sid>``)."""
    return f"t:{sid}"


def _default_size_of(payload: Any) -> int:
    """Byte size attributed to one frame payload for the bytes bound.

    ``bytes``/``str`` pay ``len()``; anything JSON-serializable (dict frame
    payloads) pays its serialized length; a payload that is neither is
    accounted as 0 (the bytes bound still holds via the other frames).
    Injectable via ``ReplayLog(size_of=...)`` for tests / exotic payloads.
    """
    if isinstance(payload, (bytes, bytearray, memoryview, str)):
        return len(payload)
    try:
        return len(orjson.dumps(payload))
    except TypeError:
        return 0


# ---------------------------------------------------------------------------
# Entries and outcomes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReplayEntry:
    """One retained frame in a domain ring window.

    ``payload`` is opaque to the log (the already-built wire frame object —
    B3b-2 hands it over verbatim on replay); ``kind`` distinguishes
    tombstones so the token-stream replay path can emit the lightweight
    ``message.removed`` shape; ``order`` is a process-wide append counter
    ordering cross-domain bytes-budget evictions (per-domain seqs are not
    comparable across domains).
    """

    domain: str
    seq: int
    payload: Any
    kind: str = FRAME_KIND_BUSINESS
    appended_at: float = 0.0
    size: int = 0
    order: int = 0

    @property
    def is_tombstone(self) -> bool:
        return self.kind == FRAME_KIND_TOMBSTONE


@dataclass(frozen=True, slots=True)
class ReplayFrames:
    """Success: contiguous retained entries with ``seq > after_seq``.

    ``entries`` are strictly seq-increasing and contiguous (no holes); an
    up-to-date cursor (``after_seq == last published seq``) yields an empty
    tuple — NOT a resync.
    """

    entries: tuple[ReplayEntry, ...]


@dataclass(frozen=True, slots=True)
class ReplayResync:
    """Server decision: the client must resync (HTTP full realignment).

    ``reason`` is one of the frozen §7.2 values the log layer can decide:
    ``epoch_changed`` / ``replay_expired`` / ``replay_gap`` /
    ``reconnect_no_replay``.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class ReplayIgnoreReset:
    """Ignore the cursor + treat as first connect (no resync frame).

    Same-epoch ``seq`` beyond the domain's published max (future cursor —
    client protocol violation or a cursor into a domain this process never
    created). Emitting a resync would be noise; the client re-aligns from
    the meta baseline instead.
    """

    seq: int


ReplayOutcome = Union[ReplayFrames, ReplayResync, ReplayIgnoreReset]


# ---------------------------------------------------------------------------
# Per-domain state
# ---------------------------------------------------------------------------

class _DomainState:
    """Ring window + counters + barrier for ONE id domain.

    ``entries`` is always contiguous in ``seq`` (evictions only ever drop
    the head — count from the domain side, bytes from the globally-oldest
    side, TTL from the expired head), which is what makes the window-start
    vs cursor comparison a complete gap detector.
    """

    __slots__ = (
        "entries", "next_seq", "last_seq", "bytes",
        "barrier_watermark", "last_touch",
    )

    def __init__(self, now: float) -> None:
        self.entries: deque[ReplayEntry] = deque()
        self.next_seq = 1          # next seq to assign (seq starts at 1)
        self.last_seq = 0          # max published seq (never resets)
        self.bytes = 0             # summed entry sizes (this domain)
        self.barrier_watermark: int | None = None
        self.last_touch = now

    @property
    def window_start(self) -> int | None:
        """Oldest still-retained seq (``None`` = empty window)."""
        return self.entries[0].seq if self.entries else None


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

class ReplayLog:
    """Bounded ring replay log — global domain + lazily-created per-sid domains.

    Bounds (each independently enforceable; whichever trips first evicts):

    * ``max_count`` — per-domain frame cap (ring overwrite of the oldest);
    * ``max_bytes`` — process-wide summed payload bytes (evicts the
      globally-oldest retained frame across ALL domains; a single frame
      larger than the whole budget is still retained — the log never drops
      the frame it just accepted);
    * ``ttl_s`` — per-frame age, lazily enforced on append/replay for the
      touched domain and wholesale via :meth:`sweep` (B3b-2 periodic hook).

    Domain recycling (design §3.4): :meth:`recycle_domain` drops frames +
    bytes but RETAINS the seq counter and any barrier watermark — a
    same-epoch old cursor into a recycled domain never degenerates into
    first-connect semantics (REPLAY-018 fail-safe). Domain shells are never
    deleted within the process lifetime (epoch): a shell is a few ints, the
    per-epoch sid cardinality is small, and deleting it would reset
    ``next_seq`` → ID regression. The real GC is process restart (new
    epoch).
    """

    def __init__(
        self,
        *,
        epoch: str | None = None,
        max_count: int = DEFAULT_REPLAY_MAX_COUNT,
        max_bytes: int = DEFAULT_REPLAY_MAX_BYTES,
        ttl_s: float = DEFAULT_REPLAY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        size_of: Callable[[Any], int] | None = None,
    ) -> None:
        if epoch is None:
            epoch = new_epoch()
        elif not isinstance(epoch, str) or not _EPOCH_RE.match(epoch):
            raise ValueError(
                "epoch must be a 16-hex lowercase nonce string "
                f"(got {epoch!r})"
            )
        if max_count < 1:
            raise ValueError("max_count must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if ttl_s <= 0 or not math.isfinite(ttl_s):
            # rev-gate MAJOR-1: ``nan``/``inf`` must NOT slip through —
            # ``nan <= 0`` is False (bypasses the plain check) and makes
            # ``age > ttl_s`` constantly False, silently disabling TTL
            # eviction forever. Fail closed on every non-finite value.
            raise ValueError(
                "ttl_s must be a finite number > 0 "
                f"(got {ttl_s!r})"
            )
        self.epoch = epoch
        self.max_count = max_count
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self.total_bytes = 0
        # replay outcome counters (design §9.1 B3b metrics: hit/miss/gap/
        # resync counts) — plain Counter, read by B3b-5 metrics wiring.
        self.replay_outcomes_total: Counter[str] = Counter()
        self._clock = clock
        self._size_of = size_of or _default_size_of
        self._domains: dict[str, _DomainState] = {}
        self._order = 0
        self._closed = False

    # -- introspection -----------------------------------------------------

    def has_domain(self, domain: str) -> bool:
        return domain in self._domains

    def domain_keys(self) -> tuple[str, ...]:
        return tuple(self._domains)

    def domain_count(self) -> int:
        return len(self._domains)

    def frame_count(self) -> int:
        return sum(len(s.entries) for s in self._domains.values())

    def domain_frame_count(self, domain: str) -> int:
        state = self._domains.get(domain)
        return len(state.entries) if state is not None else 0

    def last_seq(self, domain: str) -> int:
        """Max published seq of the domain (0 = never created/empty)."""
        state = self._domains.get(domain)
        return state.last_seq if state is not None else 0

    def window_start(self, domain: str) -> int | None:
        """Oldest still-replayable seq (the domain's effective low-watermark
        / ring lower bound). ``None`` = empty or unknown domain."""
        state = self._domains.get(domain)
        return state.window_start if state is not None else None

    def barrier_watermark(self, domain: str) -> int | None:
        """Current barrier watermark, or ``None`` if no live barrier."""
        state = self._domains.get(domain)
        return state.barrier_watermark if state is not None else None

    def metrics_snapshot(self) -> dict[str, int]:
        """Flat counters for /slimapi/metrics (B3b-5 wiring lane)."""
        snap: dict[str, int] = dict(self.replay_outcomes_total)
        snap["domains"] = len(self._domains)
        snap["frames"] = self.frame_count()
        snap["bytes"] = self.total_bytes
        snap["barriers"] = sum(
            1 for s in self._domains.values() if s.barrier_watermark is not None
        )
        return snap

    # -- write path ---------------------------------------------------------

    def append(
        self,
        domain: str,
        payload: Any,
        *,
        kind: str = FRAME_KIND_BUSINESS,
    ) -> ReplayEntry:
        """Record one published frame; returns the entry carrying its seq.

        seq assignment is per-domain, strictly monotonic from 1, one seq per
        append regardless of kind (tombstones included — REPLAY-012). The
        log records *published* frames, not *delivered* frames (§3.2:
        backpressure overflow frames still land here).
        """
        if self._closed:
            raise RuntimeError("replay log is closed")
        if not isinstance(domain, str) or not domain:
            raise ValueError("domain must be a non-empty string")
        state = self._domains.get(domain)
        if state is None:  # lazy creation (per-sid domains on first frame)
            state = _DomainState(self._clock())
            self._domains[domain] = state
        now = self._clock()
        self._ttl_evict_head(state, now)
        self._order += 1
        seq = state.next_seq
        state.next_seq = seq + 1
        state.last_seq = seq
        size = self._size_of(payload)
        entry = ReplayEntry(
            domain=domain,
            seq=seq,
            payload=payload,
            kind=kind,
            appended_at=now,
            size=size,
            order=self._order,
        )
        state.entries.append(entry)
        state.bytes += size
        self.total_bytes += size
        state.last_touch = now
        self._evict_for_count(state)
        self._evict_for_bytes()
        return entry

    # -- read path ----------------------------------------------------------

    def replay(self, domain: str, after_seq: int, epoch: str | None) -> ReplayOutcome:
        """Classify a reconnect cursor and return frames or a decision.

        Implements the log-layer tail of the frozen §7.2 short-circuit
        priority (① syntax / ② endpoint-domain checks are the wire layer's
        job in B3b-2 — inputs here arrive pre-parsed):

        ③ ``epoch != self.epoch`` → ``ReplayResync("epoch_changed")``
        ④ otherwise, in order:
           - cursor ``seq <= barrier watermark`` →
             ``ReplayResync("reconnect_no_replay")`` (禁跨 barrier 补帧;
             the watermark frame itself was published pre-gap);
           - cursor beyond published max (future) →
             ``ReplayIgnoreReset``;
           - frame right after the cursor evicted (count/bytes/TTL) →
             ``ReplayResync("replay_expired")``;
           - non-contiguous retained window (defensive: should be
             unreachable — eviction is head-only) →
             ``ReplayResync("replay_gap")``;
           - else the contiguous ``ReplayFrames`` (empty when the cursor is
             exactly at the published max — up to date, NOT a resync).
        """
        if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative int")
        if epoch != self.epoch:  # ③ — dominates everything below
            self.replay_outcomes_total["epoch_changed"] += 1
            return ReplayResync(RESYNC_EPOCH_CHANGED)
        state = self._domains.get(domain)
        now = self._clock()
        if state is not None:
            self._ttl_evict_head(state, now)
            state.last_touch = now
        # ④a barrier (upstream-loss low-watermark; watermark itself counts
        # as intercepted — rev-5 off-by-one勘误: <=, not <).
        watermark = state.barrier_watermark if state is not None else None
        if watermark is not None and after_seq <= watermark:
            self.replay_outcomes_total["reconnect_no_replay"] += 1
            return ReplayResync(RESYNC_RECONNECT_NO_REPLAY)
        # ④b future cursor (same epoch, beyond published max; also the
        # path for a domain this process never created).
        last = state.last_seq if state is not None else 0
        if after_seq > last:
            self.replay_outcomes_total["ignore_reset"] += 1
            return ReplayIgnoreReset(after_seq)
        entries = (
            tuple(e for e in state.entries if e.seq > after_seq)  # type: ignore[union-attr]
            if state is not None
            else ()
        )
        if not entries:
            if after_seq == last:  # up to date — nothing new, NOT a resync
                self.replay_outcomes_total["up_to_date"] += 1
                return ReplayFrames(())
            self.replay_outcomes_total["replay_expired"] += 1
            return ReplayResync(RESYNC_REPLAY_EXPIRED)
        if entries[0].seq != after_seq + 1:
            # the immediate next frame was evicted → cursor older than the
            # window (design: "ID 过期（早于窗口）")
            self.replay_outcomes_total["replay_expired"] += 1
            return ReplayResync(RESYNC_REPLAY_EXPIRED)
        for previous, current in zip(entries, entries[1:]):
            if current.seq != previous.seq + 1:
                # Defensive branch (design §5 open item 5): unreachable via
                # the public API (evictions are head-only → contiguous
                # windows); kept so a corruption bug fails as replay_gap
                # rather than serving a silently-holed "replay".
                self.replay_outcomes_total["replay_gap"] += 1
                return ReplayResync(RESYNC_REPLAY_GAP)
        self.replay_outcomes_total["replayed"] += 1
        return ReplayFrames(entries)

    # -- barrier / recycle / sweep -------------------------------------------

    def write_barrier(self, domain: str | None = None) -> None:
        """Write the upstream-loss barrier (low-watermark) per domain.

        ``domain=None`` → global domain + EVERY domain created within the
        current epoch (NOT limited to domains with live subscribers —
        offline token clients reconnecting later are intercepted too, §7.2
        frozen write scope). Watermark = the domain's max published seq at
        write time, monotonically non-decreasing across multiple loss
        rounds (only the latest matters for judgement). Domains created
        after the write are post-barrier domains — no watermark for them.
        Barriers are metadata: never evicted by count/bytes/TTL.
        """
        now = self._clock()
        if domain is None:
            targets = list(self._domains.values())
        else:
            state = self._domains.get(domain)
            targets = [state] if state is not None else []
        for state in targets:
            if state.barrier_watermark is None or state.barrier_watermark < state.last_seq:
                state.barrier_watermark = state.last_seq
            state.last_touch = now

    def recycle_domain(self, domain: str) -> bool:
        """Recycle a per-sid domain (TTL expiry / long-no-subscribers hook).

        Drops frames + bytes; RETAINS the seq counter (next append
        continues monotonically — ID no-regression) and any barrier
        watermark (same-epoch old cursors into a recycled domain keep
        hitting ``reconnect_no_replay`` / ``replay_expired`` instead of
        first-connect semantics — REPLAY-018 fail-safe). Returns whether
        the domain existed.
        """
        state = self._domains.get(domain)
        if state is None:
            return False
        while state.entries:
            self._drop_head(state)
        state.last_touch = self._clock()
        return True

    def sweep(self, now: float | None = None) -> int:
        """Wholesale TTL maintenance across all domains.

        Evicts expired heads, then garbage-collects barriers whose window
        lower bound has STRICTLY passed the watermark (entries[0].seq >
        watermark → every cursor ≤ watermark now lands in replay_expired
        anyway → the barrier is redundant). An EMPTY window keeps its
        barrier (its lower bound is undefined, and cursor == watermark ==
        last must stay intercepted). Returns the number of frames evicted.
        """
        if now is None:
            now = self._clock()
        evicted = 0
        for state in self._domains.values():
            evicted += self._ttl_evict_head(state, now)
            if state.entries and state.barrier_watermark is not None:
                if state.entries[0].seq > state.barrier_watermark:
                    state.barrier_watermark = None
        return evicted

    @property
    def closed(self) -> bool:
        """True once :meth:`close` ran (the sweep loop polls this to exit)."""
        return self._closed

    def close(self) -> None:
        """Release all retained frames/domains (app shutdown; app.py hook).

        Idempotent. After close, ``append`` fails loud (RuntimeError) — a
        post-shutdown append would be a B3b-2 wiring bug, not something to
        paper over.
        """
        self._closed = True
        self._domains.clear()
        self.total_bytes = 0

    # -- internals ------------------------------------------------------------

    def _ttl_evict_head(self, state: _DomainState, now: float) -> int:
        """Drop expired frames from the head (a frame is replayable at
        exactly ``ttl_s`` age; evicted once strictly older)."""
        evicted = 0
        while state.entries and now - state.entries[0].appended_at > self.ttl_s:
            self._drop_head(state)
            evicted += 1
        return evicted

    def _evict_for_count(self, state: _DomainState) -> None:
        while len(state.entries) > self.max_count:
            self._drop_head(state)

    def _evict_for_bytes(self) -> None:
        """Process-wide bytes bound: evict the globally-oldest retained
        frame (min append ``order`` across domain heads) until under budget
        or only one frame remains (a single oversize frame is retained —
        the log never drops the frame it just accepted)."""
        if self.total_bytes <= self.max_bytes:
            return
        frames = self.frame_count()
        while self.total_bytes > self.max_bytes and frames > 1:
            victim: _DomainState | None = None
            victim_order = -1
            for state in self._domains.values():
                if state.entries:
                    head_order = state.entries[0].order
                    if victim is None or head_order < victim_order:
                        victim = state
                        victim_order = head_order
            if victim is None:  # unreachable (frames > 1 implies a head)
                break
            self._drop_head(victim)
            frames -= 1

    def _drop_head(self, state: _DomainState) -> ReplayEntry:
        entry = state.entries.popleft()
        state.bytes -= entry.size
        self.total_bytes -= entry.size
        return entry
