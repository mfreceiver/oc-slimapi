"""Backward-compatible re-export hub.

Code was physically split (lite-v2 post-cleanup) into three modules:

- :mod:`.hub_types` — sentinels, config defaults, event sets, timing
  constants, frame helpers, :class:`DigestFields`, :class:`Subscriber`,
  :class:`SubscriberCapacityError`, ``_extract_session_id``.
- :mod:`.global_hub` — :class:`GlobalHub`.
- :mod:`.registry` — :class:`HubRegistry`.

This module re-exports every symbol that external code (app.py, routes,
tests) imports, so ``from oc_slimapi.sse.hub import X`` continues to work
unchanged.
"""
from __future__ import annotations

from .global_hub import GlobalHub
from .hub_types import (
    ABORT_NAME,
    DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY,
    DEFAULT_MAX_TOTAL_SUBSCRIBERS,
    DEFAULT_SSE_BUFFER_BYTES,
    DEFAULT_SSE_MAX_FRAME_BYTES,
    DEFAULT_SSE_QUEUE_ITEMS,
    DEBOUNCE_SECONDS,
    DigestFields,
    GRACE_SECONDS,
    HEARTBEAT_SECONDS,
    IMMEDIATE,
    MESSAGE_EVENTS,
    SESSION_EVENTS,
    STOP,
    Subscriber,
    SubscriberCapacityError,
    _UNSET,
    _extract_session_id,
    _now_ms,
    _sanitize_error_message,
    _upstream_line_bytes,
    sse_frame,
)
from .registry import HubRegistry
