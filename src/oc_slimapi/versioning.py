"""Wire-contract version pins (v3-contract §0 / v4-contract §0).

Dual-version period (4.0.0): ``ACCEPTED_CLIENT_VERSIONS == (3, 4)`` — the
``?v=3`` pipeline is byte-identical to the 3.x terminal state and ``?v=4``
selects the v4 differential face (v4-contract). The per-request view is
decided by the selector (``selector.py``) and read back via
``wire_view_from_scope`` — a single-value config knob cannot express a
dual view, which is why ``Settings.server_api_version`` is now pinned to
the :data:`SERVER_API_VERSION` constant (S-B04 config migration: the
legacy ``OC_SLIMAPI_SERVER_API_VERSION`` env no longer influences the
view; setting it only produces a startup warning).

:data:`SERVER_API_VERSION` is the CURRENT (latest major) wire version —
during the (3, 4) window it stays the newest major (=4); collapsing to
(4, 4) at 5.0.0 retires v3. The accepted range is fail-closed in
``config.validate`` — an operator cannot widen or narrow it via env.
"""

from __future__ import annotations

import re

# Slash-collapse helper shared with the selector (P1-14 parity: routing
# still sees the raw path; only selector decisions normalise).
_SLASH_RE = re.compile(r"/+")
_SLIMAPI_PATH = "/slimapi"


def _is_slimapi_path(path: str) -> bool:
    collapsed = _SLASH_RE.sub("/", path)
    return collapsed == _SLIMAPI_PATH or collapsed.startswith(_SLIMAPI_PATH + "/")


# Current (latest major) wire version served by this sidecar. During the
# (3, 4) dual-version window this is always the newest major (S-B04) —
# it feeds /slimapi/versions ``current`` and the (now constant-pinned)
# Settings.server_api_version.
SERVER_API_VERSION = 4

# Fail-closed production pin: the inclusive (min, max) wire-version window
# accepted from clients. 4.0.0: (3, 3) → (3, 4) (v4-contract §0.1; major
# release — v3 semantics unchanged, ?v=4 admitted). 5.0.0 will collapse
# this to (4, 4) once v3 traffic retires (§0.3).
ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (3, 4)
