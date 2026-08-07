"""Turn token fence — server-side causal identifiers for ocdroid.

This module implements the **turn token strong-fence contract**: the sidecar
stamps two flat top-level fields (``turnIncarnation`` + ``turn``) onto
forwarded ``session.digest`` SSE events so ocdroid can do causal fencing
(discard stale digests from a prior turn of a prior incarnation).

Design (frozen decisions O2 / O3 / O4 / S2 / S5 — see the implementation brief):

* **O2 incarnation strategy A** (``persisted_last + 1``): single process,
  single event loop — no file lock needed.
* **O3 single-instance semantics**: no instanceFp; the scope key is the
  ``sid`` alone (single sidecar + single opencode backend → ``sid`` is
  globally unique within the process, so no server-group fingerprint is
  required to bucket the per-turn counter).
* **O4 no turn persistence**: restart zeroes the turn registry; the
  incarnation bump covers correctness (a restarted process has a strictly
  greater incarnation, so stale turns from the old process compare low).
* **S2 commit point = bump-before-send**: turn is bumped in the catch-all
  proxy *before* ``await client.send()``. A connection-level failure (send
  raises) therefore produces a **hole** (turn number advances but no
  upstream work happened). This is the approved relaxation of contract
  §4.2 ("不 increment"); ocdroid's lex comparison tolerates holes and
  correctness is preserved. The turn counter is keyed by ``sid`` alone;
  bump only on prompt/abort forward paths.

Single process, asyncio, one event loop: every method below is a
synchronous pure-dict operation, so no locking is required (contract §7.2
single-loop monotonic visibility).
"""

from __future__ import annotations

import os
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
    * **Corrupt content** (non-integer): warn, return the fallback, do NOT
      crash. We still attempt to overwrite with the fallback so the next
      restart can recover.
    * **Unwritable** (directory does not exist / permissions): warn, return
      the fallback, do NOT crash.
    """

    def __init__(self, state_dir: str) -> None:
        self._path = Path(state_dir) / _INCARNATION_FILENAME

    def load_or_bump(self) -> int:
        """Read persisted → +1 → write back → return new incarnation.

        Returns :data:`_FALLBACK_INCARNATION` (1) on any best-effort path
        (missing file, corrupt content, unwritable). Never raises.
        """
        persisted_last = self._read_persisted()
        inc = persisted_last + 1
        if not self._write_persisted(inc):
            # Persistence failed — but the in-memory incarnation is still
            # valid for this process lifetime. A restart will re-read the
            # stale file and pick a possibly-colliding value; ocdroid's
            # lex compare keeps this correct in practice (the contract
            # only requires monotonic-within-process).
            logger.warning(
                "turn-registry: failed to persist incarnation %d to %s; "
                "using value in-memory only (restart may re-use a stale "
                "incarnation from disk)",
                inc,
                self._path,
            )
        return inc

    def _read_persisted(self) -> int:
        """Return the persisted integer, or ``_FALLBACK_INCARNATION - 1`` (0).

        Missing file → 0 (so first run yields inc=1). Corrupt content → 0
        (fallback) after a warning. Never raises.
        """
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            # First run — no persisted value. Treated as 0 (inc → 1).
            return _FALLBACK_INCARNATION - 1
        except OSError:
            logger.warning(
                "turn-registry: unreadable incarnation file %s; "
                "treating as fresh start",
                self._path,
                exc_info=True,
            )
            return _FALLBACK_INCARNATION - 1
        if not text:
            return _FALLBACK_INCARNATION - 1
        try:
            value = int(text)
        except ValueError:
            logger.warning(
                "turn-registry: corrupt incarnation file %s (content=%r); "
                "treating as fresh start",
                self._path,
                text[:64],
            )
            return _FALLBACK_INCARNATION - 1
        # Negative / absurd values are normalized to the fresh-start base;
        # we do not trust junk on disk.
        if value < 0:
            return _FALLBACK_INCARNATION - 1
        return value

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
    under the single-event-loop model (contract §7.2). ``snapshot`` always
    returns a ``(incarnation, turn)`` tuple: an unobserved ``sid`` returns
    ``(incarnation, 0)``. The digest therefore always carries both fields
    once a registry is wired (omitted only when the registry itself is
    absent — a lifespan-level deployment property).
    """

    def __init__(self, incarnation: int) -> None:
        self.incarnation: int = incarnation
        self._turns: OrderedDict[str, int] = OrderedDict()

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

    def snapshot(self, sid: str) -> tuple[int, int]:
        """Return ``(incarnation, turn)`` for ``sid`` (always a tuple).

        An unobserved ``sid`` returns ``(incarnation, 0)`` — there is no
        None / header-gated degrade path: once a registry is wired the
        digest always carries both fields.

        The returned turn is the *current* value at call time; the caller
        (GlobalHub.publish) freezes it onto the :class:`DigestFields` entry
        so a later bump does not retroactively change an already-stamped
        digest (contract §7.4, V10 acceptance).
        """
        return (self.incarnation, self._turns.get(sid, 0))
