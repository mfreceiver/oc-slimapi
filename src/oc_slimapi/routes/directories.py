from __future__ import annotations

import orjson
from fastapi import APIRouter, Request
from starlette.responses import Response

from ..directory import normalize_directory
from ..discovery import _DISCOVERY_LIMIT, fetch_global_root_sessions
from ..gzip_util import compress_if_beneficial
from ..transform import TransformBusy
from ..upstream_errors import raise_upstream_unavailable
from ._catalog_common import busy_response

router = APIRouter(prefix="/slimapi", tags=["directories"])


@router.get("/directories")
async def directories(request: Request):
    """GET /slimapi/directories — global directory catalog (project switcher).

    Lists opencode's known work directories for the client to render a
    "project switcher". Discovery source is ``GET /experimental/session?
    roots=true&archived=true`` (opencode's GLOBAL top-level session list,
    cross all workdir instances; ``roots=true`` ⇒ ``parentID==null`` only;
    ``archived=true`` ⇒ superset incl. archived sessions). Each session's
    REAL ``directory`` field is the workdir it was created in. Sessions are
    aggregated by normalized directory into one row per workdir.

    **No query parameters** — this is a GLOBAL discovery call (unlike the
    catalog endpoints' no-op ``directory``, this endpoint accepts none at
    all to avoid implying it is scoped). Admitted via the version selector
    (``?v=3`` terminal) like every ``/slimapi/**`` route.

    Additive (brand-new endpoint); **no** ``X-Slimapi-Version`` bump (still
    2). An older sidecar without this route returns 404
    ``thin_route_not_found`` from the catch-all and the client falls back.

    Resource bounds (review blocker): TransformPool admission is acquired
    **before** the upstream GET and held across fetch→guard→aggregate;
    discovery failure of any kind → 503 ``upstream_unavailable`` (no
    envelope); admission full → 503 ``transform_busy`` + ``Retry-After:2``.

    Envelope (always 200 on the happy path):

    .. code-block:: json

        {
          "items": [
            {
              "directory": "/home/mar/.../ocdroid",
              "title": "winner session title or null",
              "lastUpdated": 1723000000000,
              "rootSessionCount": 12,
              "activeRootSessionCount": 2,
              "archivedRootSessionCount": 10,
              "archivedOnly": false
            }
          ],
          "discoveryComplete": true
        }

    - ``items``: one row per distinct normalized directory, sorted
      ``lastUpdated`` DESC, tie-break ``directory`` ASC.
    - ``discoveryComplete``: ``true`` unless the discovery page filled
      exactly at ``_DISCOVERY_LIMIT`` (possible truncation).

    Total failure (cannot list top-level sessions): HTTP 503
    ``{"code": "upstream_unavailable"}`` (no envelope).

    **Passive-discovery limitation (honest)**: this endpoint only sees
    workdirs that have at least one top-level session. A workdir where no
    session was ever created, or a directory that has since been deleted
    from the filesystem, is invisible. It does NOT scan the filesystem.
    """
    upstream_client = request.app.state.upstream
    try:
        async with request.app.state.transforms as pool:
            # Admission covers fetch + guard + aggregate so a burst cannot
            # monopolise memory / event-loop CPU.
            sessions_payload, discovery_complete = await fetch_global_root_sessions(
                upstream_client, request, limit=_DISCOVERY_LIMIT,
            )
            # Strict schema guard (review blocker): this endpoint uses
            # `discoveryComplete` to express completeness, so silently
            # skipping a bad session would make a "seemingly complete" list
            # miss directories. ANY session that is not a dict, or whose
            # `directory` field is not a non-empty string → 503
            # upstream_unavailable. Mirrors sessions.py's
            # `not all(isinstance(s, dict))` → 503 guard.
            for s in sessions_payload:
                if not isinstance(s, dict):
                    raise_upstream_unavailable()
                d = s.get("directory")
                if not isinstance(d, str) or not d:
                    raise_upstream_unavailable()
            # Aggregate + serialize + gzip offloaded to the worker so a
            # large aggregation's orjson.dumps + gzip does not block the
            # event loop (SSE heartbeats, other light async work).
            encoded, extra = await pool.offload(
                _aggregate_and_pack, sessions_payload, discovery_complete,
                accept_encoding=request.headers.get("accept-encoding"),
            )
    except TransformBusy:
        return busy_response(request.headers.get("accept-encoding"))
    return Response(
        encoded, status_code=200, media_type="application/json",
        headers={"Cache-Control": "no-store", **extra},
    )


def _num(v: object) -> int | float:
    """Numeric extraction for winner tie-break: non-number (missing / bool /
    str / None) → 0 (sorts last). Booleans are excluded even though
    ``isinstance(True, int)`` is True in Python."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _session_rank(s: dict) -> tuple[int | float, int | float, str]:
    """Sort key ``(time.updated, time.created, id)`` for the winner pick.

    ``id`` is coerced to ``""`` when absent / non-string so a heterogeneous
    group never raises ``TypeError`` comparing ``str`` vs ``None`` (the
    strict guard only checks ``directory``, not ``id``).
    """
    t = s.get("time")
    if not isinstance(t, dict):
        t = {}
    sid = s.get("id")
    sid = sid if isinstance(sid, str) else ""
    return (_num(t.get("updated")), _num(t.get("created")), sid)


def _aggregate_and_pack(
    sessions: list[dict],
    discovery_complete: bool,
    *,
    accept_encoding: str | None,
) -> tuple[bytes, dict[str, str]]:
    """Worker entry: aggregate sessions by normalized directory + serialize
    + optional gzip.

    Aggregation rules (review blocker — ``title`` + ``lastUpdated`` MUST come
    from the SAME winner session):

    * group key = ``normalize_directory(session.directory)`` (trailing slash
      stripped, root ``/`` preserved) — so ``/a`` and ``/a/`` merge.
    * ``rootSessionCount`` = top-level sessions in the dir.
    * ``activeRootSessionCount`` = sessions whose ``time.archived`` is NOT a
      number (missing / non-numeric).
    * ``archivedRootSessionCount`` = sessions whose ``time.archived`` IS a
      number (int/float, excluding bool).
    * ``archivedOnly`` = ``activeRootSessionCount == 0``.
    * winner = ``max`` by ``(time.updated, time.created, id)``; numeric
      fields via :func:`_num` (missing/non-number → 0, sorts last);
      ``lastUpdated`` = winner ``time.updated``; ``title`` = winner
      ``title`` (null when not a non-empty string).

    Items sorted ``lastUpdated`` DESC, tie-break ``directory`` ASC.
    Empty input → ``{"items": [], "discoveryComplete": …}`` (authoritative
    empty when discovery was complete).
    """
    groups: dict[str, list[dict]] = {}
    for s in sessions:
        # `directory` is guaranteed a non-empty string by the route's strict
        # guard (runs before offload); normalize as the group key.
        d = normalize_directory(s["directory"])
        groups.setdefault(d, []).append(s)

    items: list[dict] = []
    for directory, group_sessions in groups.items():
        active_count = 0
        archived_count = 0
        for s in group_sessions:
            t = s.get("time")
            archived = t.get("archived") if isinstance(t, dict) else None
            if isinstance(archived, (int, float)) and not isinstance(archived, bool):
                archived_count += 1
            else:
                active_count += 1
        winner = max(group_sessions, key=_session_rank)
        wt = winner.get("time")
        wt = wt if isinstance(wt, dict) else {}
        last_updated = _num(wt.get("updated"))
        title = winner.get("title")
        if not isinstance(title, str) or not title:
            title = None
        items.append({
            "directory": directory,
            "title": title,
            "lastUpdated": last_updated,
            "rootSessionCount": len(group_sessions),
            "activeRootSessionCount": active_count,
            "archivedRootSessionCount": archived_count,
            "archivedOnly": active_count == 0,
        })

    # lastUpdated DESC (negate), tie-break directory ASC.
    items.sort(key=lambda it: (-it["lastUpdated"], it["directory"]))

    envelope = {"items": items, "discoveryComplete": discovery_complete}
    encoded = orjson.dumps(envelope)
    return compress_if_beneficial(encoded, accept_encoding)
