"""Compatibility shim — implementation moved to :mod:`oc_slimapi.sse.tokenstream`.

Existing imports ``from oc_slimapi.sse.token_hub import ...`` keep working.
"""
from .tokenstream import (  # noqa: F401
    STOP,
    DeltaAccumulator,
    LivePart,
    PartKey,
    TokenStreamHub,
    TokenStreamRegistry,
    TokenSubscriber,
    TokenSubscriberCapacityError,
    _TokenMetrics,
    _delta_frame,
    _heartbeat_frame,
    _now_ms,
    _resync_frame,
    sse_frame,
)
