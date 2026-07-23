"""Per-session children projection cache with asyncio single-flight."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from urllib.parse import quote

from .directory import normalize_directory
from .errors import CodedHTTPException
from .skeleton import skeleton_session
from .upstream import forward_directory_headers
from .upstream_errors import fetch_json_mapped

TTL_SECONDS = 30.0
EMPTY_TTL_SECONDS = 5.0
MAX_ENTRIES = 4096
CHILDREN_IDS_HINT_LIMIT = 32


@dataclass(slots=True)
class CacheEntry:
    value: list[dict]
    version: int
    generation: int
    fetched_at: float
    expires_at: float
    is_empty: bool

    def fresh(self, now: float) -> bool:
        return now < self.expires_at


@dataclass(slots=True)
class InFlight:
    task: asyncio.Task | None
    generation: int
    waiters: set[asyncio.Future]
    started_at: float


class ChildrenCache:
    def __init__(self, upstream):
        self._upstream = upstream
        self._cache: dict[tuple[str, str], CacheEntry] = {}
        self._inflight: dict[tuple[str, str], InFlight] = {}
        self._generations: dict[str, int] = {}
        self._closed = False
        self.hits = 0
        self.misses = 0
        self.coalesced = 0

    def generation_of(self, parent_sid: str) -> int:
        return self._generations.get(parent_sid, 0)

    def invalidate(self, parent_sid: str) -> None:
        self._generations[parent_sid] = self.generation_of(parent_sid) + 1
        for key in [key for key in self._cache if key[0] == parent_sid]:
            del self._cache[key]

    def peek(self, parent_sid: str, directory: str | None) -> tuple[list[str], bool] | None:
        """只读查缓存（不 fetch）。fresh 命中→(child_ids, complete)；complete=False 表示超 budget。
        未命中/过期→None。同步、无 await。"""
        norm_dir = normalize_directory(directory) if directory else ""
        key = (parent_sid, norm_dir)
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is None or not entry.fresh(now):
            return None
        ids = [c.get("id") for c in entry.value if isinstance(c, dict) and c.get("id")]
        if len(ids) <= CHILDREN_IDS_HINT_LIMIT:
            return ids, True
        return [], False

    async def get_or_fetch(self, parent_sid: str, directory: str | None):
        """Return cached children or coalesce onto one upstream fetch.

        ``None`` and ``""`` both mean no directory and use ``""`` in the
        cache key. An explicit ``"/"`` is the root directory and is a
        different key. Direct callers should pass ``None`` for no directory.
        """
        if self._closed:
            raise CodedHTTPException(503, code="upstream_unavailable")
        norm_dir = normalize_directory(directory) if directory else ""
        key = (parent_sid, norm_dir)
        now = time.monotonic()

        entry = self._cache.get(key)
        if entry is not None and entry.fresh(now):
            self.hits += 1
            return entry.value, entry.version
        if entry is not None:
            del self._cache[key]

        inflight = self._inflight.get(key)
        if inflight is None:
            self.misses += 1
            inflight = InFlight(
                task=None,
                generation=self.generation_of(parent_sid),
                waiters=set(),
                started_at=now,
            )
            self._inflight[key] = inflight
            inflight.task = asyncio.create_task(
                self._fetch_and_publish(key, inflight),
                name="children-cache-fetch",
            )
        else:
            self.coalesced += 1

        waiter = asyncio.get_running_loop().create_future()
        inflight.waiters.add(waiter)
        try:
            value = await waiter
        except asyncio.CancelledError:
            inflight.waiters.discard(waiter)
            raise
        return value, inflight.generation

    async def _fetch_and_publish(self, key, inflight: InFlight):
        parent_sid, directory = key
        try:
            # NOTE: fetch_json_mapped is called with traffic_request=None (no
            # per-request upIn accounting for the children fetch). This is an
            # intentional design choice: the single-flight cache coalesces
            # concurrent requests, and attributing the single upstream fetch
            # to any one of the coalesced waiters would be unfair (the others
            # would "ride free" on another request's counted bytes). Per the
            # approved review (rev-1), children fetch bytes are not charged to
            # any per-request bucket. They are, however, real upstream bytes
            # that the sidecar consumes — consider adding a process-level
            # counter if full visibility is needed.
            raw = await fetch_json_mapped(
                self._upstream,
                f"/session/{quote(parent_sid, safe='')}/children",
                params={"directory": directory} if directory else None,
                headers=forward_directory_headers(directory or None),
                sid=parent_sid,
                expect=list,
            )
            skeletons = sorted(
                (skeleton_session(item) for item in raw if isinstance(item, dict)),
                key=lambda item: (
                    -(item.get("time", {}).get("created") or 0),
                    item.get("id") or "",
                ),
            )
        except asyncio.CancelledError:
            error = CodedHTTPException(503, code="upstream_unavailable")
            self._publish(key, inflight, error)
            raise
        except BaseException as exc:
            self._publish(key, inflight, exc)
            return
        self._publish(key, inflight, skeletons)

    def _publish(self, key, inflight: InFlight, outcome) -> None:
        if self._inflight.get(key) is inflight:
            del self._inflight[key]
        if not isinstance(outcome, BaseException) and inflight.generation >= self.generation_of(key[0]):
            now = time.monotonic()
            is_empty = not outcome
            self._cache[key] = CacheEntry(
                value=outcome,
                version=inflight.generation,
                generation=inflight.generation,
                fetched_at=now,
                expires_at=now + (EMPTY_TTL_SECONDS if is_empty else TTL_SECONDS),
                is_empty=is_empty,
            )
            if len(self._cache) > MAX_ENTRIES:
                for cache_key, cache_entry in list(self._cache.items()):
                    if not cache_entry.fresh(now):
                        del self._cache[cache_key]
                while len(self._cache) > MAX_ENTRIES:
                    oldest = min(
                        self._cache,
                        key=lambda cache_key: self._cache[cache_key].fetched_at,
                    )
                    del self._cache[oldest]
        for waiter in inflight.waiters:
            if not waiter.done():
                if isinstance(outcome, BaseException):
                    waiter.set_exception(outcome)
                else:
                    waiter.set_result(outcome)
        inflight.waiters.clear()

    async def aclose(self) -> None:
        self._closed = True
        tasks = [flight.task for flight in self._inflight.values() if flight.task is not None and not flight.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
