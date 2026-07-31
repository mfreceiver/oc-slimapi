"""Turn token fence — server-side causal identifiers for ocdroid.

This module implements the **turn token strong-fence contract**: the sidecar
stamps two flat top-level fields (``turnIncarnation`` + ``turn``) onto
forwarded ``session.digest`` SSE events so ocdroid can do causal fencing
(discard stale digests from a prior turn of a prior incarnation).

Design (frozen decisions O1–O4 / S2 / S5 — see the implementation brief):

* **O1 header-gated**: the sidecar only maintains turn state when the
  request carries ``X-Ocdroid-Server-Group-Fp``. Header missing → no scope
  → ``snapshot()`` returns ``None`` → fields are omitted (safe degrade).
* **O2 incarnation strategy A** (``persisted_last + 1``): single process,
  single event loop — no file lock needed.
* **O3 single-instance semantics**: no instanceFp; the scope key is just
  ``(serverGroupFp, sid)``.
* **O4 no turn persistence**: restart zeroes the turn registry; the
  incarnation bump covers correctness (a restarted process has a strictly
  greater incarnation, so stale turns from the old process compare low).
* **S2 commit point = send-before-bump**: turn is bumped in the catch-all
  proxy *before* ``await client.send()``. A connection-level failure (send
  raises) therefore produces a **hole** (turn number advances but no
  upstream work happened). This is the approved relaxation of contract
  §4.2 ("不 increment"); ocdroid's lex comparison tolerates holes and
  correctness is preserved.

Single process, asyncio, one event loop: every method below is a
synchronous pure-dict operation, so no locking is required (contract §7.2
single-loop monotonic visibility).
"""

from __future__ import annotations

from pathlib import Path

from .logging_config import get_logger

logger = get_logger(__name__)

# Best-effort incarnation fallback. Used when the persistence file is
# missing (first run — treated as persisted_last=0, so inc=1), corrupt, or
# unwritable: we never crash the process over a causal-identifier best
# effort (mirrors traffic_snapshot.py's fault tolerance style).
_FALLBACK_INCARNATION = 1

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
            # stale file and pick a possibly-colliding value; the header
            # gate + ocdroid's lex compare keep this correct in practice
            # (the contract only requires monotonic-within-process).
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
        """Best-effort write of ``inc`` to the persistence file.

        Creates the parent directory if needed. Returns ``True`` on success,
        ``False`` on any I/O failure (logged as a warning). Never raises.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write atomically-ish: write then flush. Single process, no
            # concurrent writer, so a plain overwrite is sufficient.
            self._path.write_text(f"{inc}\n", encoding="utf-8")
            return True
        except OSError:
            logger.warning(
                "turn-registry: could not write incarnation file %s",
                self._path,
                exc_info=True,
            )
            return False


class TurnRegistry:
    """In-process turn counter + scope mapping (S2 turn counting).

    Holds:

    * ``incarnation`` — frozen at startup from :class:`IncarnationStore`;
      constant for the process lifetime (O2/O4).
    * ``_turns`` — ``dict[(serverGroupFp, sid), int]``, monotonically
      non-decreasing (S2). Keyed on the scope tuple (O3 single-instance:
      no instanceFp in the key).
    * ``_sid_scope`` — ``dict[sid, serverGroupFp]`` populated by the
      catch-all proxy on every scoped request so the global hub can resolve
      a scope from just a ``sid`` when stamping.

    Header-gated (O1): if no scope is known for a ``sid`` (i.e. the proxy
    never saw a ``X-Ocdroid-Server-Group-Fp`` header for that session),
    :meth:`snapshot` returns ``None`` and the digest omits both fields.

    All methods are synchronous pure-dict operations — no locking needed
    under the single-event-loop model (contract §7.2).
    """

    def __init__(self, incarnation: int) -> None:
        self.incarnation: int = incarnation
        self._turns: dict[tuple[str, str], int] = {}
        self._sid_scope: dict[str, str] = {}

    def register_scope(self, sid: str, server_group_fp: str) -> None:
        """Record the ``sid → serverGroupFp`` mapping (idempotent overwrite).

        Called by the catch-all proxy on *every* scoped request (not just
        prompt/abort) to maximize the probability that a later digest
        stamp can resolve the scope from the ``sid`` alone.
        """
        self._sid_scope[sid] = server_group_fp

    def bump_turn(self, server_group_fp: str, sid: str) -> int:
        """Increment the turn for ``(server_group_fp, sid)`` and return it.

        S2 commit point: the proxy calls this *before* ``await client.send()``.
        A connection-level failure therefore leaves a hole (turn advanced,
        no upstream work) — the approved relaxation of contract §4.2.
        Monotonically non-decreasing per scope tuple.
        """
        key = (server_group_fp, sid)
        self._turns[key] = self._turns.get(key, 0) + 1
        return self._turns[key]

    def snapshot(self, server_group_fp: str | None, sid: str) -> tuple[int, int] | None:
        """Return ``(incarnation, turn)`` for the scope, or ``None`` if unknown.

        If ``server_group_fp`` is ``None``, the scope is reverse-resolved
        from ``sid`` via the registered mapping. If still unknown →
        ``None`` (header-gated safe degrade: both digest fields omitted).

        The returned turn is the *current* value at call time; the caller
        (GlobalHub.publish) freezes it onto the :class:`DigestFields` entry
        so a later bump does not retroactively change an already-stamped
        digest (contract §7.4, V10 acceptance).
        """
        if server_group_fp is None:
            server_group_fp = self._sid_scope.get(sid)
        if server_group_fp is None:
            return None
        return (self.incarnation, self._turns.get((server_group_fp, sid), 0))
