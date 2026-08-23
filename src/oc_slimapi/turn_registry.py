"""Turn token fence — server-side causal identifiers for ocdroid.

This module implements the **turn token strong-fence contract**: the sidecar
stamps two flat top-level fields (``turnIncarnation`` + ``turn``) onto
forwarded ``session.digest`` SSE events so ocdroid can do causal fencing
(discard stale digests from a prior turn of a prior incarnation).

Design (frozen decisions O2 / O3 / O4 / S2 / S5 — see the implementation brief):

* **O2 incarnation strategy A** (``persisted_last + 1``): single process,
  single event loop — no file lock needed. Durability gate (4.12.1
  FIX-CORR-2r2): the startup write is retried a bounded number of times;
  if still unconfirmed the value is marked **non-durable** and the process
  **withholds the turn fence** (``snapshot`` → ``(None, None)`` → paired
  field omission → ocdroid Tier-2) instead of publishing a value the disk
  never learned. Only confirmed values are published; cross-process strict
  monotonicity is required of published values alone.
* **O3 single-instance semantics**: no instanceFp; the scope key is the
  ``sid`` alone (single sidecar + single opencode backend → ``sid`` is
  globally unique within the process, so no server-group fingerprint is
  required to bucket the per-turn counter).
* **O4 no turn persistence**: restart zeroes the turn registry; the
  incarnation bump covers correctness (a restarted process has a strictly
  greater incarnation, so stale turns from the old process compare low).
* **S2 commit point = bump-before-send**: turn is bumped in the annexed
  write pipeline (``routes/write_groups.py``; M3-2/C2 moved it there when
  the catch-all proxy closed) *before* ``await client.send()``. A
  connection-level failure (send raises) therefore produces a **hole**
  (turn number advances but no upstream work happened). This is the
  approved relaxation of contract §4.2 ("不 increment"); ocdroid's lex
  comparison tolerates holes and correctness is preserved. The turn
  counter is keyed by ``sid`` alone; bump only on the collected
  prompt_async/abort write paths (§8.2).

Single process, asyncio, one event loop: every method below is a
synchronous pure-dict operation, so no locking is required (contract §7.2
single-loop monotonic visibility).
"""

from __future__ import annotations

import os
import re
import time
from collections import OrderedDict
from pathlib import Path

from .logging_config import get_logger

logger = get_logger(__name__)

# Best-effort incarnation fallback. Used when the persistence file is
# missing (first run — treated as persisted_last=0, so inc=1), corrupt, or
# unwritable: we never crash the process over a causal-identifier best
# effort (mirrors traffic_snapshot.py's fault tolerance style).
_FALLBACK_INCARNATION = 1

# LRU cap on the per-sid turn map (mirrors _LAST_UPDATED_AT_BY_SID_MAX in
# sse/global_hub.py). KNOWN UPPER-BOUND TRADE-OFF (prospective disclosure,
# accepted): if a sid is evicted and later bumps again within the SAME
# incarnation, its turn restarts at 1 — a within-incarnation regression
# (lex-LOWER), which ocdroid's fence would treat as stale until the turn
# re-climbs past the pre-eviction value. This is NOT a restart hole (a restart
# bumps the incarnation, which this does not). Practically unreachable: needs
# >10_000 distinct bumped sids in a single process — far beyond the single-
# user sidecar's working set (same disclosure pattern as v2-contract.md's
# "known upper-bound race").
_TURNS_MAX = 10_000

# Single flat file holding the last-written incarnation integer, one line.
_INCARNATION_FILENAME = "incarnation"

# Startup persistence retries (FIX-CORR-2r2, direction X "don't publish
# unconfirmed"): transient I/O hiccups (short NFS blips, disk busy) get a
# bounded retry before the process gives up and degrades to Tier-2 (turn
# fence not published this process). Attempt 1 is immediate; two retries
# back off 0.1s / 0.25s — total worst-case added startup latency ~0.35s.
_WRITE_ATTEMPTS = 3
_WRITE_RETRY_DELAYS_S = (0.1, 0.25)


class IncarnationValue(int):
    """``int`` carrying the durability verdict of its persistence attempt.

    Subclassing ``int`` keeps the existing call sites (app.py wiring,
    annotations, tests asserting ``isinstance(inc, int)``) unchanged while
    letting :class:`TurnRegistry` learn whether the value was confirmed on
    disk. ``durable=True`` → the value is on disk (published); ``False`` →
    persistence is UNCONFIRMED and the process must NOT publish the turn
    fence (FIX-CORR-2r2: clients fence by lexicographic comparison of a
    value the disk never learned — reuse/regression across processes — so
    an unconfirmed value is withheld instead of published).
    """

    durable: bool

    def __new__(cls, value: int, *, durable: bool) -> "IncarnationValue":
        obj = super().__new__(cls, value)
        obj.durable = durable
        return obj


class IncarnationStore:
    """Single-process-lifetime epoch counter (S5 incarnation persistence).

    Strategy A: ``incarnation = persisted_last + 1``. On startup
    :meth:`load_or_bump` reads the persisted integer, adds one, writes the
    new value back, and returns it. The returned value is then frozen onto
    the :class:`TurnRegistry` for the lifetime of the process.

    Single process / single event loop: no file lock. Startup runs before
    any concurrent request can interleave, so the read→bump→write sequence
    is atomic in practice.

    Fault tolerance (best-effort, never crashes lifespan):

    * **Missing file** (first run): treated as ``persisted_last = 0`` →
      returns ``1`` and attempts to persist it.
    * **Corrupt content** (non-integer): warn, treat the file as absent,
      do NOT crash. We still attempt to overwrite with the computed value
      so the next restart can recover.
    * **Unwritable** (directory does not exist / permissions): warn and
      return a **wall-clock monotonic floor** instead of the bare
      ``base + 1`` (FIX-CORR-2b — see :meth:`load_or_bump`).
    * **High-watermark across files** (FIX-CORR-2c): the primary and the
      legacy persistence files are BOTH read; ``base`` is the max of all
      valid values. A corrupt primary must never fall back below a
      still-readable legacy value.
    """

    def __init__(self, state_dir: str, legacy_state_dir: str | None = None) -> None:
        # Save two paths: the new state_dir and the legacy (old access_log dir).
        # New path takes priority; legacy is only consulted when the new path
        # is missing or corrupt (monotonic migration without reset).
        self._path = Path(state_dir) / _INCARNATION_FILENAME
        self._legacy_path = (
            Path(legacy_state_dir) / _INCARNATION_FILENAME
            if legacy_state_dir else None
        )

    def _read_path(self, path: Path | None) -> tuple[bool, int]:
        """Return (exists_and_valid, value).

        missing → silent (False, 0); empty/corrupt/unreadable → (False, 0)
        with a warning (same logging semantics as the prior _read_persisted).
        A None path (no legacy configured) → (False, 0) silent.
        """
        if path is None or not path.exists():
            return False, 0
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning(
                "turn-registry: unreadable incarnation file %s; treating as fresh",
                path, exc_info=True,
            )
            return False, 0
        if not text:
            logger.warning("turn-registry: empty incarnation file %s; treating as fresh", path)
            return False, 0
        try:
            value = int(text)
        except ValueError:
            logger.warning("turn-registry: corrupt incarnation file %s; treating as fresh", path)
            return False, 0
        if value < 0:
            logger.warning("turn-registry: negative incarnation in %s; treating as fresh", path)
            return False, 0
        return True, value

    def load_or_bump(self) -> IncarnationValue:
        # FIX-CORR-2c: read BOTH files and take the high watermark — a
        # corrupt (truncated/half-written) primary must never fall back
        # below a still-readable legacy value, and vice versa. Under the
        # normal migration invariant (primary migrated FROM legacy, so
        # primary >= legacy) the max is simply the primary value; only
        # anomalous states change the outcome.
        # inc = base + 1; write ONLY the new path; legacy file is never
        # deleted.
        candidates = [
            self._read_path(path)
            for path in (self._path, self._legacy_path)
            if path is not None
        ]
        valid_values = [value for valid, value in candidates if valid]
        base = max(valid_values) if valid_values else 0
        inc = base + 1
        # FIX-CORR-2r2 (direction X — "don't publish unconfirmed"): retry
        # the write a bounded number of times; if it is STILL unconfirmed
        # the value is marked non-durable and the process withholds the
        # turn fence entirely (TurnRegistry.snapshot → (None, None) →
        # paired field omission → ocdroid Tier-2 per contract §7.5) rather
        # than publishing a value the disk never learned. An unconfirmed
        # but PUBLISHED value is unfixable cross-process: two same-second
        # failures would reuse it, and a later successful write would
        # regress below it (rev-2 gate counterexamples). An unconfirmed
        # and UNPUBLISHED value can collide with nothing — nothing fences
        # on it.
        durable = False
        for attempt in range(_WRITE_ATTEMPTS):
            if self._write_persisted(inc):
                durable = True
                break
            if attempt < _WRITE_ATTEMPTS - 1:
                time.sleep(_WRITE_RETRY_DELAYS_S[min(attempt, len(_WRITE_RETRY_DELAYS_S) - 1)])
        if not durable:
            # The floor is NOT published (snapshot returns (None, None)
            # for non-durable registries) — it only keeps the in-memory
            # value self-consistent and acts as a collision pad for any
            # hypothetical future observer of this process's internals.
            floor = max(inc, int(time.time()) + 1)
            inc = floor
            logger.warning(
                "turn-registry: persistence unconfirmed after %d attempts "
                "to %s — turn fence NOT published this process (paired "
                "field omission → ocdroid Tier-2 degrade); value %d kept "
                "in-memory only",
                _WRITE_ATTEMPTS, self._path, inc,
            )
        return IncarnationValue(inc, durable=durable)

    def _write_persisted(self, inc: int) -> bool:
        """Best-effort **atomic** write of ``inc`` to the persistence file.

        Creates the parent directory if needed. Writes the value to a sibling
        ``.tmp`` file, ``fsync``s it (durability against power loss), then
        ``os.replace``s it onto the final path — atomic on POSIX (and on the
        same filesystem rename is atomic on Linux). A crash at any point
        leaves the previous file intact (either the old value or the new
        value, never a truncated/half-written file). This guards against the
        restart-incarnation-reuse hazard: a half-written file would be parsed
        as corrupt → fallback to 0 → the next process reuses incarnation 1
        (reusing the old fence).

        Single process / single event loop → no concurrent writer; the
        atomicity is purely for **crash** protection (process killed, OOM,
        power loss). Returns ``True`` on success, ``False`` on any I/O
        failure (logged as a warning). Never raises.

        The ``.tmp`` sibling is cleaned up on any failure path so a stale
        temp never lingers to confuse readers (the reader only ever reads
        the final path, but cleanliness is its own reward).
        """
        data = f"{inc}\n".encode("utf-8")
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write + fsync the temp file first. fsync is what makes the
            # subsequent atomic replace actually durable on power loss —
            # without it the rename could land before the data.
            with open(str(tmp_path), "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # Atomic commit — the final path now points at the fully-written,
            # fsynced content. A reader never observes a partial write.
            os.replace(str(tmp_path), str(self._path))
            # FIX-CORR-2a: the rename is in the namespace, but the
            # directory entry is not durable until the parent dir is
            # fsynced — without it a power loss can revert the file to
            # its previous content (or nothing) and a restart would
            # re-read the pre-bump base, re-publishing the same fence.
            try:
                dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # Best-effort: the rename already landed; only the
                # power-loss window remains. Degrade with a warning —
                # this is NOT a write failure (the value is persisted in
                # the page cache and will land barring power loss).
                logger.warning(
                    "turn-registry: parent-dir fsync failed for %s",
                    self._path.parent,
                    exc_info=True,
                )
            return True
        except OSError:
            logger.warning(
                "turn-registry: could not write incarnation file %s",
                self._path,
                exc_info=True,
            )
            # Best-effort cleanup of the temp on failure so it doesn't
            # linger (it is harmless to the reader, but tidy).
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return False


class TurnRegistry:
    """In-process turn counter keyed by ``sid`` (S2 turn counting).

    Holds:

    * ``incarnation`` — frozen at startup from :class:`IncarnationStore`;
      constant for the process lifetime (O2/O4).
    * ``_turns`` — ``OrderedDict[str, int]``, monotonically non-decreasing per
      ``sid`` while that sid remains resident (S2). LRU-bounded by
      :data:`_TURNS_MAX` (10_000): if a sid is evicted and later bumps again
      within the same incarnation, its turn restarts at 1 — a within-incarnation
      regression (lex-LOWER) the client's fence would treat as stale until the
      turn re-climbs; this is NOT a restart hole (a restart bumps incarnation)
      and is accepted prospectively (practically unreachable at >10_000 bumped
      sids; same disclosure pattern as v2-contract.md's "known upper-bound
      race"). Scope is the ``sid`` alone (O3
      single-instance: no instanceFp, no server-group fingerprint in the key
      — single sidecar + single opencode backend makes ``sid`` globally
      unique).

    All methods are synchronous pure-dict operations — no locking needed
    under the single-event-loop model (contract §7.2). ``snapshot`` returns
    a ``(incarnation, turn)`` tuple for an unobserved ``sid`` as
    ``(incarnation, 0)``. Two paired-omission triggers (FIX-CORR-2r2):
    the registry itself is absent (lifespan-level deployment property), or
    the registry is wired but its incarnation persistence was unconfirmed
    at startup (non-durable — ``snapshot`` returns ``(None, None)`` and the
    digest/REST merge layers omit both fields; ocdroid degrades to Tier-2
    per contract §7.5 paired-optional semantics).
    """

    def __init__(self, incarnation: int) -> None:
        self.incarnation: int = incarnation
        # FIX-CORR-2r2: IncarnationValue carries the durability verdict.
        # A bare int (tests, legacy construction) is treated as durable —
        # the pre-r2 semantics — so every existing call site is unchanged.
        self._durable: bool = getattr(incarnation, "durable", True)
        self._turns: OrderedDict[str, int] = OrderedDict()

    @property
    def durable(self) -> bool:
        """Whether the frozen incarnation was confirmed on disk at startup.

        ``False`` → this process must not publish the turn fence; the
        merge layers (digest stamp + /slimapi/sessions/status) omit both
        paired fields and ocdroid degrades to Tier-2 (contract §7.5).
        """
        return self._durable

    def bump_turn(self, sid: str) -> int:
        """Increment the turn for ``sid`` and return it.

        S2 commit point: the proxy calls this *before* ``await client.send()``.
        A connection-level failure therefore leaves a hole (turn advanced,
        no upstream work) — the approved relaxation of contract §4.2.
        Monotonically non-decreasing per ``sid`` while it remains resident
        (eviction under :data:`_TURNS_MAX` is the lone exception; see there).
        """
        self._turns[sid] = self._turns.get(sid, 0) + 1
        # LRU cap: refresh insertion order so actively-bumping sids survive,
        # then evict least-recently-bumped over _TURNS_MAX.
        self._turns.move_to_end(sid)
        # B7 (P1-23): behaviour unchanged (LRU eviction maintained — oracle
        # ruled the eviction→new-incarnation cure expands the blast radius
        # since incarnation is process-level frozen). Added an observability
        # warning so the practically-unreachable edge (>10_000 bumped sids in
        # one process) is visible to ops rather than silent. Fires only when
        # an eviction actually occurs, not on every bump.
        while len(self._turns) > _TURNS_MAX:
            evicted_sid, _ = self._turns.popitem(last=False)
            logger.warning(
                "turn-registry: LRU evicted sid %r at incarnation %d; its "
                "turn will restart at 1 if bumped again this incarnation "
                "(within-incarnation regression — practically unreachable at "
                "_TURNS_MAX=%d)",
                evicted_sid, self.incarnation, _TURNS_MAX,
            )
        return self._turns[sid]

    def snapshot(self, sid: str) -> tuple[int | None, int | None]:
        """Return ``(incarnation, turn)`` for ``sid`` (always a tuple).

        An unobserved ``sid`` returns ``(incarnation, 0)``. Non-durable
        registries (FIX-CORR-2r2: persistence unconfirmed at startup)
        return ``(None, None)`` for EVERY sid — the paired fields are
        omitted wire-side (digest stamp and REST merge both check for
        None) and ocdroid degrades to Tier-2 rather than fencing on a
        value the disk never learned.

        The returned turn is the *current* value at call time; the caller
        (GlobalHub.publish) freezes it onto the :class:`DigestFields` entry
        so a later bump does not retroactively change an already-stamped
        digest (contract §7.4, V10 acceptance).
        """
        if not self._durable:
            return (None, None)
        return (self.incarnation, self._turns.get(sid, 0))


# --- Path classifiers (moved from the retired catch-all proxy; v3-only
# terminal keeps the S2 turn-fence bump on the annexed write routes) ---

_SESSION_SID_RE = re.compile(r"^/session/([^/]+)")
_TURN_BUMPING_SUFFIX_RE = re.compile(
    r"^/session/[^/]+/(prompt_async|abort)/?$")


def extract_sid_from_path(norm_path: str) -> str | None:
    """Extract the ``{sid}`` segment from a ``/session/{sid}/...`` path.

    Returns ``None`` for non-session paths. Does NOT hardcode the opencode
    ``01HQ...`` id format — any non-empty first segment under ``/session/``
    qualifies (the upstream will reject malformed ids).
    """
    m = _SESSION_SID_RE.match(norm_path)
    if m is None:
        return None
    sid = m.group(1)
    return sid or None


def is_turn_bumping_path(norm_path: str) -> bool:
    """True iff ``norm_path`` is a turn-bumping write (contract §3.y.3).

    Matches ``/session/{sid}/prompt_async`` or ``/session/{sid}/abort``
    (trailing slash tolerant) — the two collected turn writes (§8.2;
    M3-3: the sync ``/session/{sid}/prompt`` form was never collected and
    the closed catch-all makes it unreachable, so it is no longer
    recognised). These are the writes that start/stop a turn of work and
    therefore must advance the turn counter at the S2 commit point
    (bump-before-send). Path match alone is NOT sufficient — the caller
    must additionally require ``POST``.
    """
    return _TURN_BUMPING_SUFFIX_RE.match(norm_path) is not None
