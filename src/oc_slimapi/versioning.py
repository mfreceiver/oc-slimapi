"""Wire-version domain constants — terminal state (v3-only).

The v2-era ``X-Slimapi-Version`` header gate is RETIRED (v3-contract §1):
the header is dead input — never read, never an error. Version negotiation
lives entirely in the ``?v=`` selector (:mod:`oc_slimapi.selector`), which
admits ``?v=3`` only (§2 退役后).

Kept here:

* :data:`SERVER_API_VERSION` / :data:`ACCEPTED_CLIENT_VERSIONS` — the
  single-version domain ``(3, 3)``. ``Config.validate()`` pins
  ``accepted_client_versions`` to exactly this constant (fail-closed —
  widening/narrowing via env stays forbidden).
* :func:`_is_slimapi_path` — the slash-collapsing namespace test shared
  with the selector.
"""

from __future__ import annotations

import re

SERVER_API_VERSION = 3
# Terminal (3.0.0): the v2 pipeline is deleted; the only supported wire
# version is 3. The tuple stays a (min, max) pair so the existing
# fail-closed pin/validation shapes (config.validate) are unchanged.
ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (3, 3)

# P1-14: collapse duplicate slashes for the namespace decision only.
# ASGI servers may not fold ``//`` → ``/`` (unlike FastAPI's URL parsing), so
# ``//slimapi/foo`` could bypass the selector (raw path does not start with
# ``/slimapi/``) while routing normalises it later and hits a /slimapi/
# endpoint. This regex normalises the path for THE NAMESPACE DECISION ONLY —
# ``scope["path"]`` is left unchanged so downstream routing (FastAPI /
# Starlette) sees the original path exactly as the ASGI server delivered it.
_SLASH_RE = re.compile(r"/+")


def _is_slimapi_path(path: str) -> bool:
    """Return True if ``path`` targets the ``/slimapi`` namespace.

    Collapses duplicate slashes first (``//slimapi/foo`` → ``/slimapi/foo``)
    so a double-slash prefix cannot bypass the selector. Recognises BOTH the
    exact root ``/slimapi`` and any sub-path ``/slimapi/...``.
    """
    normalised = _SLASH_RE.sub("/", path)
    return normalised == "/slimapi" or normalised.startswith("/slimapi/")
