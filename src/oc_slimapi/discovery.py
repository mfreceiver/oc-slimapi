"""Global root-session discovery helper.

Shared between ``GET /slimapi/questions`` (cross-directory pending-question
aggregation) and ``GET /slimapi/directories`` (global directory catalog):
both start by listing opencode's GLOBAL top-level sessions via
``GET /experimental/session?roots=true&archived=true``.

The helper owns the fetch + cap-read + JSON parse + non-list guard and
returns ``(sessions, discovery_complete)``. It does **NOT** validate the
shape of individual sessions — callers own that:

* ``questions.py`` is **lenient** (skips non-dict / non-string-directory
  entries), because its ``authoritativeDirectories`` envelope protects the
  client from partial coverage.
* ``directories.py`` is **strict** (raises 503 on any malformed session),
  because its ``discoveryComplete`` envelope implies completeness and
  silently skipping a bad session would make a "seemingly complete" list
  miss directories.

The parse runs in the event loop (mirrors ``sessions.py`` L76's in-loop
``orjson.loads`` — the C parser is fast enough; offloading the parse would
add round-trip latency for no event-loop benefit).
"""

from __future__ import annotations

import httpx
import orjson
from fastapi import Request

from .traffic import stash_up_in
from .transform import read_with_cap
from .upstream_errors import raise_upstream_unavailable

# Page size for the GET /experimental/session?roots=true discovery call.
# `roots=true` returns only top-level sessions (parentID==null), so the count
# ≈ number of distinct workdirs — orders of magnitude smaller than the full
# session table. 10000 is therefore effectively never hit in practice; it is
# a safety cap so a pathological upstream cannot exhaust memory. If the page
# fills exactly, discovery is marked incomplete so the client degrades
# (questions: authoritativeDirectories → partial-replace; directories:
# discoveryComplete=false). Exported so callers can pass it as ``limit``
# (and tests can monkeypatch the caller's binding).
_DISCOVERY_LIMIT = 10_000


async def fetch_global_root_sessions(
    upstream_client: httpx.AsyncClient,
    request: Request,
    *,
    limit: int = _DISCOVERY_LIMIT,
) -> tuple[list[dict], bool]:
    """GET ``/experimental/session?roots=true&archived=true&limit=limit``.

    opencode's GLOBAL top-level session list (cross all workdir instances;
    ``roots=true`` ⇒ ``parentID==null`` only; ``archived=true`` ⇒ includes
    archived sessions so the set is a superset — protects archived-only
    workdirs from being dropped). Each session carries its REAL ``directory``
    field (the workdir it was created in), covering git repos, non-git dirs,
    and git-worktree subdirs alike.

    Streams the response + cap-reads (``read_with_cap(max_response_bytes)``)
    so an oversized upstream body cannot spike sidecar RSS, then
    ``orjson.loads`` in the event loop.

    Error mapping (contract §7 — discovery is an internal derived call;
    leaking the upstream status would mislead the client — an
    experimental-endpoint 4xx means opencode does not support it):

    * initial-send ``httpx.RequestError`` → 503 ``upstream_unavailable``
    * status >= 400 (4xx **or** 5xx) → 503 ``upstream_unavailable``
      (NOT ``upstream_http_N``)
    * bad JSON / non-list body → 503 ``upstream_unavailable``
    * cap exceeded (``read_with_cap`` returns ``None``) → 503
      ``upstream_unavailable``

    Returns ``(sessions_list, discovery_complete)`` where
    ``discovery_complete = len(sessions) < limit``.

    Does NOT validate the shape of individual sessions (callers own that;
    see module docstring). Does NOT offload the parse (matches
    ``sessions.py``'s in-loop parse pattern).
    """
    config = request.app.state.config
    try:
        response = await upstream_client.send(
            upstream_client.build_request(
                "GET", "/experimental/session",
                params={
                    "roots": "true",
                    "archived": "true",
                    "limit": limit,
                },
            ),
            stream=True,
        )
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    try:
        try:
            if response.status_code >= 400:
                # network/5xx/4xx on discovery = total failure (contract §7:
                # 503 upstream_unavailable, NOT upstream_http_N). Discovery
                # is an internal derived call; leaking the upstream status
                # would mislead the client about which directory failed.
                err_body = await response.aread()
                stash_up_in(request, len(err_body))
                raise_upstream_unavailable()
            body, _ = await read_with_cap(
                response, config.max_response_bytes,
                on_read=lambda n: stash_up_in(request, n),
            )
            if body is None:
                raise_upstream_unavailable()
            try:
                sessions_payload = orjson.loads(body)
            except (orjson.JSONDecodeError, ValueError) as exc:
                raise_upstream_unavailable(exc)
            if not isinstance(sessions_payload, list):
                raise_upstream_unavailable()
        except httpx.RequestError as exc:
            # Mid-stream read failure (aread or read_with_cap).
            raise_upstream_unavailable(exc)
    finally:
        await response.aclose()

    return sessions_payload, len(sessions_payload) < limit
