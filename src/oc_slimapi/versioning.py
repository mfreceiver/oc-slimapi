"""Wire-contract version pins (v3-contract §0 / v4-contract §0).

v4-only window (2026-08-21 version-window narrowing, target release
5.0.0): ``ACCEPTED_CLIENT_VERSIONS == (4, 4)`` — the inclusive window
collapsed to the single newest major, so ``?v=3`` (and every other
version form) is a 400 ``unsupported_version`` with
``supported:[4]``. The per-request view is decided by the selector
(``selector.py``) and read back via ``wire_view_from_scope``;
:data:`SERVER_API_VERSION` is the CURRENT (latest major) wire version
(=4) and feeds the constant-pinned ``Settings.server_api_version``
(S-B04 config migration: the legacy ``OC_SLIMAPI_SERVER_API_VERSION``
env no longer influences the view; setting it only produces a startup
warning). The accepted range stays fail-closed in ``config.validate`` —
an operator cannot widen or narrow it via env.

History: 4.0.0 opened the (3, 4) dual-version window (v4-contract §0.1,
major); the 2026-08-19 "permanent dual-version window" ruling was
superseded by the 2026-08-21 owner direction
(docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md §1) and the
window collapses here (major, v4-contract §0.3 revision).
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


# Current (latest major) wire version served by this sidecar. With the
# window collapsed to v4-only this is the only admitted major (S-B04) —
# it feeds /slimapi/versions ``current`` and the (constant-pinned)
# Settings.server_api_version.
SERVER_API_VERSION = 4

# Fail-closed production pin: the inclusive (min, max) wire-version window
# accepted from clients. 4.0.0: (3, 3) → (3, 4) (v4-contract §0.1; major
# release — v3 semantics unchanged, ?v=4 admitted). 2026-08-21 narrowing
# (target 5.0.0): (3, 4) → (4, 4) — the window collapsed to v4-only,
# ?v=3 answers 400 unsupported_version supported:[4] (§0.3 revision).
ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (4, 4)
