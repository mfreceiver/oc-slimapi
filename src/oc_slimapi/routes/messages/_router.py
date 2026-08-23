"""Shared router + cross-family helpers for the ``/slimapi/messages/{sid}``
endpoint package (F-302 three-family split of the historical single
``routes/messages.py``; pure move, zero behaviour change).

The three family modules — :mod:`._list`, :mod:`._full_merge` and
:mod:`._expand` — all decorate THIS single shared ``router`` object, so
``messages.router`` exposes the same route set as the pre-split module.
Helpers consumed by more than one family (``_busy_response``,
``_resolve_messages_directory``) live here, the shared ancestor with no
sibling-module imports. ``_busy_response`` is a re-export alias of
:func:`oc_slimapi.routes._catalog_common.busy_response` (ARCH-3 dedup:
one authoritative definition + one ``TRANSFORM_RETRY_AFTER_SECONDS``;
the dependency direction stays routes/messages → routes, never back).
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...selector import resolve_route_directory
from ...directory import validate_directory
from .._catalog_common import TRANSFORM_RETRY_AFTER_SECONDS  # noqa: F401  (compat re-export)
from .._catalog_common import busy_response as _busy_response

router = APIRouter(prefix="/slimapi/messages/{sid}", tags=["messages"])


async def _resolve_messages_directory(request: Request, directory: str | None) -> str | None:
    """Return the selector-normalised workspace directory for upstream.

    The selector owns every query/header precedence and error decision.  It
    validates and stashes a consumed ``?directory=`` before stripping it from
    the downstream query; this route helper only reads that state (falling back
    to the bound query value for direct test invocation) and validates it
    idempotently.
    """
    directory = resolve_route_directory(request.scope, directory)
    if directory is None:
        return None
    return validate_directory(directory)
