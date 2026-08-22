"""Bounded, generation-fenced state for messages ``?since=`` cursors.

The cache deliberately stores serialized projection bytes rather than the
projection object graph.  Route code performs projection and diff work in the
transform pool; this module only owns the synchronous state transition that
publishes a new snapshot.
"""

from __future__ import annotations

import base64
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Literal

import orjson


TokenKind = Literal["valid", "reset", "invalid"]


_PROCESS_EPOCH = secrets.token_urlsafe(16)
_GENERATION_LOCK = Lock()
_NEXT_PROCESS_GENERATION = 0


def _allocate_process_generation() -> int:
    global _NEXT_PROCESS_GENERATION
    with _GENERATION_LOCK:
        _NEXT_PROCESS_GENERATION += 1
        return _NEXT_PROCESS_GENERATION


@dataclass(frozen=True)
class CacheEntry:
    """The compact value retained for one ``(sid, cq_hash)`` key."""

    canonical_items: bytes
    fingerprints: dict[str, str]
    generation: int

    @property
    def retained_bytes(self) -> int:
        return len(self.canonical_items) + sum(
            len(mid.encode("utf-8")) + 32 + 64
            for mid in self.fingerprints
        )


@dataclass(frozen=True)
class ObservedSnapshot:
    """A no-await snapshot captured before an upstream request begins."""

    entry: CacheEntry | None

    @property
    def generation(self) -> int | None:
        return None if self.entry is None else self.entry.generation


@dataclass(frozen=True)
class TokenCheck:
    kind: TokenKind
    generation: int | None = None


@dataclass(frozen=True)
class CommitResult:
    entry: CacheEntry | None
    cas_loser: bool = False
    omitted: bool = False
    bypassed: bool = False


class SinceCache:
    """Process-local LRU of single-snapshot message projections.

    ``commit`` is the CAS serial point.  It contains no await and must remain
    synchronous: the event loop serializes callers between transform awaits,
    so reading the current value, comparing the observed generation, and
    replacing/reusing the entry is one atomic state transition.
    """

    TOKEN_VERSION = 1
    TOKEN_MAX_BYTES = 512

    def __init__(
        self,
        *,
        enabled: bool,
        max_entries: int,
        max_bytes: int,
        max_entry_bytes: int,
        epoch: str | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self.max_entry_bytes = int(max_entry_bytes)
        self.epoch = _PROCESS_EPOCH if epoch is None else epoch
        self._entries: OrderedDict[tuple[str, str], CacheEntry] = OrderedDict()
        self._retained_bytes = 0

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def observe(self, key: tuple[str, str]) -> ObservedSnapshot:
        """Capture the current entry for a no-before request.

        A hit is touched because this is an LRU.  The returned entry is
        immutable by convention and is safe for the route to retain while it
        awaits upstream/projection work.
        """
        if not self.enabled:
            return ObservedSnapshot(None)
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return ObservedSnapshot(entry)

    def current(self, key: tuple[str, str]) -> CacheEntry | None:
        """Return and touch the current entry, if any."""
        if not self.enabled:
            return None
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def issue_token(self, sid: str, cq_hash: str, generation: int) -> str:
        """Encode a v1 token without padding."""
        payload = {
            "v": self.TOKEN_VERSION,
            "epoch": self.epoch,
            "sid": sid,
            "cq_hash": cq_hash,
            "gen": generation,
        }
        raw = orjson.dumps(payload)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def check_token(
        self,
        token: str,
        *,
        sid: str,
        cq_hash: str,
    ) -> TokenCheck:
        """Classify a token before the route chooses diff or reset.

        Syntax, shape, version, session-identity, and length errors are
        ``invalid``.  A well-formed token from another epoch, an evicted
        entry, a non-current generation, or — per the 2026-08-22 v6.1
        adjudication — a mismatched ``cq_hash`` (the limit/directory/mode
        query axis changed; the token is format-valid but semantically
        stale) is a safe reset.
        """
        if not isinstance(token, str) or len(token.encode("utf-8")) > self.TOKEN_MAX_BYTES:
            return TokenCheck("invalid")
        if not token or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in token):
            return TokenCheck("invalid")
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload = orjson.loads(raw)
        except (ValueError, TypeError, UnicodeError, orjson.JSONDecodeError):
            return TokenCheck("invalid")
        if not isinstance(payload, dict):
            return TokenCheck("invalid")
        if (
            not isinstance(payload.get("v"), int)
            or isinstance(payload.get("v"), bool)
            or payload["v"] != self.TOKEN_VERSION
        ):
            return TokenCheck("invalid")
        if not (
            isinstance(payload.get("epoch"), str)
            and isinstance(payload.get("sid"), str)
            and isinstance(payload.get("cq_hash"), str)
            and isinstance(payload.get("gen"), int)
            and not isinstance(payload.get("gen"), bool)
        ):
            return TokenCheck("invalid")
        # Session identity is a hard 400; the query axis (cq_hash) is not —
        # a format-valid token minted under a different limit/directory/mode
        # is merely stale, so the v6.1 adjudication (2026-08-22) routes it
        # to the reset group (full projection + freshly issued nextSince).
        if payload["sid"] != sid:
            return TokenCheck("invalid")
        if payload["cq_hash"] != cq_hash:
            return TokenCheck("reset", payload["gen"])
        if payload["epoch"] != self.epoch:
            return TokenCheck("reset", payload["gen"])
        current = self._entries.get((sid, cq_hash))
        if current is None or current.generation != payload["gen"]:
            return TokenCheck("reset", payload["gen"])
        return TokenCheck("valid", payload["gen"])

    def commit(
        self,
        key: tuple[str, str],
        observed: ObservedSnapshot,
        canonical_items: bytes,
        fingerprints: dict[str, str],
        *,
        cacheable: bool = True,
    ) -> CommitResult:
        """Publish a fresh snapshot using the Phase B CAS lineage rules.

        This method is intentionally synchronous.  A differing CAS loser
        never overwrites the winner and reports ``omitted=True``.  An
        oversized/non-cacheable winner drops the key so tokens that point at
        the old generation reset on the next request.
        """
        if not self.enabled:
            return CommitResult(None, omitted=True, bypassed=True)

        current = self._entries.get(key)
        observed_generation = observed.generation
        same_observation = (
            current is None if observed_generation is None
            else current is not None and current.generation == observed_generation
        )

        if not same_observation:
            if current is not None and current.canonical_items == canonical_items:
                self._entries.move_to_end(key)
                return CommitResult(current, cas_loser=True)
            return CommitResult(current, cas_loser=True, omitted=True)

        if current is not None and current.canonical_items == canonical_items:
            self._entries.move_to_end(key)
            return CommitResult(current)

        retained = len(canonical_items) + sum(
            len(mid.encode("utf-8")) + 32 + 64 for mid in fingerprints
        )
        if not cacheable or retained > self.max_entry_bytes:
            self._drop(key)
            return CommitResult(None, omitted=True, bypassed=True)

        generation = self._allocate_generation()
        entry = CacheEntry(canonical_items, dict(fingerprints), generation)
        self._drop(key)
        self._entries[key] = entry
        self._retained_bytes += entry.retained_bytes
        self._evict_over_budget()
        if self._entries.get(key) is None:
            return CommitResult(None, omitted=True, bypassed=True)
        return CommitResult(entry)

    def _allocate_generation(self) -> int:
        return _allocate_process_generation()

    def _drop(self, key: tuple[str, str]) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self._retained_bytes -= old.retained_bytes

    def _evict_over_budget(self) -> None:
        while (
            len(self._entries) > self.max_entries
            or self._retained_bytes > self.max_bytes
        ):
            oldest = next(iter(self._entries), None)
            if oldest is None:
                break
            self._drop(oldest)


__all__ = [
    "CacheEntry",
    "CommitResult",
    "ObservedSnapshot",
    "SinceCache",
    "TokenCheck",
]
