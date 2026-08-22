"""BE-004 regression tests: access-log maintenance TOCTOU races.

Covers the active-transition short-lock fix in ``oc_slimapi.access_log``:

* ``compress_old_access_logs`` snapshot→unlink window vs an emit date-switch
  that (re-)opens the "stale-dated" source mid-gzip (NTP rollback scenario);
* normal archiving of genuinely inactive files is unaffected;
* same-date emits never take ``_ACTIVE_TRANSITION_LOCK`` (zero hot-path
  cost);
* ``prune_old_access_logs`` defers the live handler's open file.

All race orchestration uses threading.Event barriers — no probabilistic
sleeps. The core-race test pins the compressor exactly between "gzip
finished" and "final commit entered" by wrapping ``gzip.open`` with a
proxy whose close() runs the barrier; this is purely behavioural (it does
not reference any fix-introduced symbol), so the test also runs — and
FAILS — against the unfixed implementation.
"""

from __future__ import annotations

import gzip
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import oc_slimapi.access_log as mod
from oc_slimapi.access_log import DailyAccessHandler

_TODAY = date(2026, 8, 23)
_OLD = _TODAY - timedelta(days=3)


def _midnight_ts(d: date) -> float:
    return datetime(d.year, d.month, d.day).timestamp()


def _make_record(msg: str, target_date: date | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        "oc_slimapi.access", logging.INFO, "", 0, msg, (), None
    )
    if target_date is not None:
        record.created = _midnight_ts(target_date)
    return record


@pytest.fixture(autouse=True)
def _reset_access_logger():
    """Clean logger state AND restore the module active-handler ref."""
    saved_ref = mod._active_handler_ref
    logger = logging.getLogger("oc_slimapi.access")
    yield
    mod._active_handler_ref = saved_ref
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.disabled = False


def _make_handler(tmp_path: Path) -> DailyAccessHandler:
    handler = DailyAccessHandler(directory=str(tmp_path))
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


class _PinnedGzipFile:
    """Proxy around the real GzipFile that pins close() on two events.

    The compressor's ``with gzip.open(tmp, "wb") as f_out:`` block calls
    close() at exit — i.e. exactly when the gzip payload is complete but
    BEFORE the final commit (os.replace / unlink, or ``_commit_archive``).
    close() finishes the real gzip stream, announces ``gzip_done``, then
    blocks until ``switch_done`` is set. This pins the compressor in the
    historic TOCTOU window deterministically.
    """

    def __init__(
        self,
        inner,
        gzip_done: threading.Event,
        switch_done: threading.Event,
    ) -> None:
        self._inner = inner
        self._gzip_done = gzip_done
        self._switch_done = switch_done

    def write(self, data):  # pragma: no cover - delegate
        return self._inner.write(data)

    def writelines(self, lines):
        return self._inner.writelines(lines)

    def flush(self):  # pragma: no cover - delegate
        return self._inner.flush()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        self._inner.close()
        self._gzip_done.set()
        if not self._switch_done.wait(10):
            raise AssertionError("test barrier: date-switch never happened")


def test_compress_commit_defers_when_emit_reopens_source_mid_gzip(tmp_path):
    """Core BE-004 race closure.

    Compressor pinned between "gzip complete" and "final commit"; an emit
    date-switch (NTP-rollback style: record.created maps to the OLD date)
    re-opens the exact source file being compressed. The fix must defer
    the commit (discard temp, keep .jsonl, no .gz) and the next maintenance
    round must archive the file cleanly — including the straggler line the
    racing emit wrote — proving no permanently split archive and no lost
    data. Against the unfixed code the source gets unlinked while the live
    handler still holds its fd, so ``src.exists()`` fails.
    """
    src = tmp_path / f"access-{_OLD.isoformat()}.jsonl"
    gz = src.with_name(src.name + ".gz")
    src.write_text("pre\n", encoding="utf-8")

    # Handler installed as the module's active ref, but with NO file open
    # yet: the compressor's entry-time snapshot sees current_path=None and
    # does not skip the source.
    handler = _make_handler(tmp_path)
    mod._active_handler_ref = handler

    gzip_done = threading.Event()
    switch_done = threading.Event()
    real_gzip_open = gzip.open

    def spy_open(*args, **kwargs):
        inner = real_gzip_open(*args, **kwargs)
        return _PinnedGzipFile(inner, gzip_done, switch_done)

    errors: list[BaseException] = []
    count: list[int] = []

    def run_compress():
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(mod.gzip, "open", spy_open)
                count.append(mod.compress_old_access_logs(str(tmp_path), _TODAY))
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    t = threading.Thread(target=run_compress)
    t.start()
    try:
        assert gzip_done.wait(10), "compressor never reached the pinned point"

        # While the compressor is pinned post-gzip, an emit whose
        # record.created maps to the OLD date (NTP rollback) re-opens the
        # source file — it is now the active handler's open file.
        handler.emit(_make_record("straggler", target_date=_OLD))
        assert handler.current_path == src

        switch_done.set()  # release the compressor into its final commit
    finally:
        t.join(15)
        switch_done.set()  # safety net on any early failure
    assert not t.is_alive()
    assert errors == []

    # Committed nothing: temp discarded, source kept, no .gz.
    assert count == [0]
    assert src.exists()
    assert not gz.exists()
    assert list(tmp_path.glob("*.gz.tmp.*")) == []

    # Next maintenance round: handler no longer holds the file; archive
    # proceeds normally and the .gz contains BOTH lines (pre-gzip content
    # plus the straggler the racing emit appended) — no permanent split.
    handler.close()
    assert handler.current_path is None
    assert mod.compress_old_access_logs(str(tmp_path), _TODAY) == 1
    assert gz.exists()
    assert not src.exists()
    with real_gzip_open(gz, "rt", encoding="utf-8") as f:
        assert f.read() == "pre\nstraggler\n"


def test_compress_archives_inactive_file_normally(tmp_path):
    """Regression guard: non-active old files still gzip+replace+unlink."""
    src = tmp_path / f"access-{_OLD.isoformat()}.jsonl"
    gz = src.with_name(src.name + ".gz")
    src.write_text("line1\nline2\n", encoding="utf-8")

    # No handler installed → nothing can be active.
    mod._active_handler_ref = None

    assert mod.compress_old_access_logs(str(tmp_path), _TODAY) == 1
    assert gz.exists()
    assert not src.exists()
    with gzip.open(gz, "rt", encoding="utf-8") as f:
        assert f.read() == "line1\nline2\n"
    assert list(tmp_path.glob("*.gz.tmp.*")) == []


def test_same_date_emit_does_not_take_transition_lock(tmp_path):
    """Hot-path guarantee: same-date emits never acquire the lock.

    With the transition lock held by the test thread (simulating a
    concurrent maintenance commit / setup re-init), a burst of same-date
    emits must complete — only date-switch transitions take the lock.
    """
    handler = _make_handler(tmp_path)

    # First emit establishes the open file (a date-switch — done BEFORE
    # the lock is taken).
    handler.emit(_make_record("first", target_date=_TODAY))
    assert handler.current_path == (
        tmp_path / f"access-{_TODAY.isoformat()}.jsonl"
    )

    done = threading.Event()
    errors: list[BaseException] = []

    def emit_burst():
        try:
            for i in range(20):
                handler.emit(_make_record(f"same-date-{i}", target_date=_TODAY))
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)
        finally:
            done.set()

    acquired = mod._ACTIVE_TRANSITION_LOCK.acquire()
    assert acquired
    try:
        t = threading.Thread(target=emit_burst)
        t.start()
        # Deterministic: if same-date emits needed the lock, the burst
        # would queue behind our held lock and done would never be set
        # within the (generous) timeout.
        assert done.wait(10), "same-date emit blocked on transition lock"
        t.join(10)
    finally:
        mod._ACTIVE_TRANSITION_LOCK.release()
    assert not t.is_alive()
    assert errors == []

    handler.close()
    out = tmp_path / f"access-{_TODAY.isoformat()}.jsonl"
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "first"
    assert lines[1:] == [f"same-date-{i}" for i in range(20)]


def test_prune_defers_active_handler_open_file(tmp_path):
    """Prune must never unlink the live handler's open .jsonl (BE-004).

    NTP-rollback shape: the handler's open file carries an old date already
    past the prune deadline. Prune defers while held; after the handler
    releases the file, the next prune removes it.
    """
    src = tmp_path / f"access-{_OLD.isoformat()}.jsonl"
    src.write_text("held\n", encoding="utf-8")

    handler = _make_handler(tmp_path)
    handler.emit(_make_record("held-line", target_date=_OLD))
    assert handler.current_path == src
    mod._active_handler_ref = handler

    # _OLD = today-3 < deadline = today-3? Use retain_days=2 → deadline
    # today-2, so _OLD (today-3) is expired.
    assert mod.prune_old_access_logs(str(tmp_path), 2, _TODAY) == 0
    assert src.exists()

    handler.close()
    assert handler.current_path is None
    assert mod.prune_old_access_logs(str(tmp_path), 2, _TODAY) == 1
    assert not src.exists()
