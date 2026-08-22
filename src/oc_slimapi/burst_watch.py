"""Sliding-window 5xx burst observability (4.10.1 C).

Motivation: two self-healed 503 clusters (2026-08-21 01:24–01:37, 16 in a
row; 21:19–21:27 sporadic) left no alertable trace. This module turns a
burst of sidecar-emitted 5xx responses into exactly ONE structured WARNING
per window, catchable via:

    journalctl --user -u oc-slimapi -p warning

Semantics (4.10.1 C spec):

* Counts ONLY 5xx responses (``500 <= status < 600``) — fail-closed 503s
  and upstream-5xx mappings — recorded at the traffic-accounting response
  hook; 4xx never counts (the guard lives here so the invariant has a
  single home even though the caller also gates on the wire status).
* ``_BURST_WINDOW_S``-second sliding window; ``_BURST_THRESHOLD`` events
  inside one window → ONE WARNING carrying the count, the per-code
  distribution (e.g. ``{"503":5}``) and the ≤ ``_TOP_PATHS`` most-seen
  request paths.
* Debounce: a trigger RESETS the window (events cleared), so one burst
  logs one line; the next line needs a fresh full window.
* Per-process in-memory state (this service is single-process). The
  window/threshold are module constants on purpose — pure observability
  side channel, no config surface.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from typing import Callable

import orjson

from .logging_config import get_logger

logger = get_logger(__name__)

_BURST_WINDOW_S = 60.0
_BURST_THRESHOLD = 5
_TOP_PATHS = 3

# (monotonic ts, status, path) events inside the current window.
# Module-level single-process state; cleared on trigger (debounce) and via
# _reset() between tests. All mutations are synchronous serial points.
_events: "deque[tuple[float, int, str]]" = deque()


def _reset() -> None:
    """Test hook: drop all window state."""
    _events.clear()


def record_5xx(
    status: int, path: str, *, clock: Callable[[], float] = time.monotonic
) -> None:
    """Account one 5xx response; emit the burst WARNING at threshold.

    Called from the traffic-accounting middleware's response hook with the
    final wire status + request path. Purely observational — this never
    raises into the response path (the caller also wraps it defensively).
    """
    if not 500 <= int(status) < 600:
        return  # 4xx (or non-5xx) never counts
    now = clock()
    cutoff = now - _BURST_WINDOW_S
    while _events and _events[0][0] <= cutoff:
        _events.popleft()  # events older than the window stop counting
    _events.append((now, int(status), path))
    if len(_events) < _BURST_THRESHOLD:
        return
    codes = Counter(f"{code}" for _, code, _ in _events)
    paths = Counter(p for _, _, p in _events)
    logger.warning(
        "upstream_5xx_burst count=%d window_s=%d codes=%s paths=%s",
        len(_events),
        int(_BURST_WINDOW_S),
        orjson.dumps(dict(codes)).decode(),
        orjson.dumps(dict(paths.most_common(_TOP_PATHS))).decode(),
    )
    # Debounce: reset the window so the same burst cannot re-trigger.
    _events.clear()
