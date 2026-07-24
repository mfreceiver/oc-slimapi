"""Discovery dataset refresh (load_products + warm_allowlist).

Moved out of routes/sessions.py to eliminate route→route coupling
(questions.py lazy-import hack). Still used by app.py lifespan warm-up.
"""

from __future__ import annotations

import asyncio
from typing import Any, NoReturn
from urllib.parse import quote

from fastapi import FastAPI

from .logging_config import get_logger

logger = get_logger(__name__)


async def load_products(app: FastAPI, traffic_request: Any = None) -> list[dict]:
    """Refresh the discovery dataset and, on success, write the allowlist.

    Concurrency: the whole fetch → validate → commit → notify sequence is
    serialised under ``app.state.allowlist_lock`` so a slow stale fetch can
    never overwrite a fast fresh one (v6 §3.3). All three callers —
    ``warm_allowlist``, ``/projects``, the q-p null-dir fan-out — call this
    function directly without their own lock.

    Failure modes (any one of these aborts the whole refresh and leaves the
    last-known-good allowlist + ``allowlist_ready`` untouched):

    * upstream ``/project`` returns a non-list (v5 §3.3 #4 / v6 §3.3 #4)
    * upstream ``/project/{id}/directories`` for any project returns a
      non-list (v6 §3.3 #4 — the previous ``isinstance(..., list) else []``
      silent downgrade would have produced an incomplete ``new_set`` and
      either missed a real discovery change or fabricated a fake one)
    * upstream ``raise_for_status`` / connect error (propagates)

    Notification fires only when the committed state is *observably* new
    (set changed **or** readiness transitioned False→True); the in-process
    ``asyncio.Lock`` plus the last-known-good model keep cold-start idempotent.
    """
    async with app.state.allowlist_lock:
        old_set = set(app.state.directory_allowlist)
        old_ready = bool(getattr(app.state, "allowlist_ready", False))
        client = app.state.upstream
        response = await client.get("/project")
        # Traffic accounting: stash project-list bytes when called from a
        # request-scoped caller (e.g. /projects route or null-dir fan-out).
        # Background callers (warm_allowlist) pass traffic_request=None.
        if traffic_request is not None:
            from .traffic import stash_up_in  # avoid top-level import cycle

            stash_up_in(traffic_request, len(response.content))
        response.raise_for_status()
        projects = response.json()
        if not isinstance(projects, list):
            raise ValueError("upstream /project body is not a list")
        semaphore = asyncio.Semaphore(8)

        async def decorate(project: dict) -> dict:
            async with semaphore:
                result = await client.get(f"/project/{quote(str(project['id']), safe='')}/directories")
                if traffic_request is not None:
                    stash_up_in(traffic_request, len(result.content))
                result.raise_for_status()
                raw_directories = result.json()
            if not isinstance(raw_directories, list):
                # v6: per-directory non-list is a refresh failure (was a
                # silent empty-decorate downgrade pre-v6 — would have masked
                # a broken upstream). Let ``gather`` propagate so the whole
                # refresh aborts; last-known-good stays intact.
                raise ValueError("upstream directories body is not a list")
            directories = []
            for item in raw_directories:
                if not isinstance(item, dict):
                    continue
                path = item.get("directory", item.get("path"))
                if isinstance(path, str):
                    directories.append({"path": path.rstrip("/") or "/", "strategy": item.get("strategy")})
            worktree = project.get("worktree")
            return {
                "id": project.get("id"),
                "name": project.get("name"),
                "worktree": worktree,
                "directories": directories,
            }

        output = await asyncio.gather(*(decorate(item) for item in projects if isinstance(item, dict)))
        new_set = {
            path.rstrip("/") or "/"
            for project in output
            for path in ([project.get("worktree")] + [item["path"] for item in project["directories"]])
            if isinstance(path, str) and path.startswith("/")
        }
        # Atomic commit (single-async — no other coroutine can observe the
        # half-written state because they are blocked on the lock above).
        app.state.directory_allowlist = new_set
        # First successful refresh flips readiness True; failures leave it
        # at its previous value (False pre-warm-up, True last-known-good
        # thereafter). This drives ``X-Discovery-Ready`` (v6 §1.1).
        app.state.allowlist_ready = True
        # Notify: set changed OR readiness transitioned False→True. Hub
        # discovery with no active subscribers is a no-op (no hub is
        # lazily created). v6 §3.3 #2.
        if (new_set != old_set) or (not old_ready):
            hubs = getattr(app.state, "hubs", None)
            if hubs is not None:
                hubs.notify_reconfigured_if_active("discovery_changed")
        return output


async def warm_allowlist(app: FastAPI) -> None:
    """Best-effort allowlist warm-up at startup. Swallows upstream errors so a
    not-yet-ready opencode does not block sidecar boot.

    The allowlist is no longer a gate (directories are now passed through to
    upstream opencode, which decides whether it can serve them). It survives
    as a discovery dataset for ``/slimapi/projects`` display and for the
    null-directory aggregation fan-out in ``questions._aggregate``."""
    try:
        await load_products(app)
    except Exception:
        logger.warning("allowlist warm-up failed", exc_info=True)
