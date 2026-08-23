"""Unit tests for :mod:`oc_slimapi.actions` (the /slimapi/actions core).

Covers the §7 matrix of the actions-impl spec: manifest validation
(fail-closed per-action drops / file-level disable, never crashes
lifespan), the exec/query executor semantics (stdout cap + drain-discard,
stderr exclusion, binary replace, signal exit codes), throttling
(min-interval / single-flight / service-level busy), confirm gating,
timeout process-group kill, spawn failure, and structured audit.

The tests drive the registry directly (no FastAPI routing — that is the
Wave-2 ``tests/test_actions_routes.py``), spawning real short-lived child
processes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time

import pytest

from oc_slimapi.actions import (
    ActionBusy,
    ActionConfirmRequired,
    ActionError,
    ActionNotFound,
    ActionThrottled,
    ActionTimeout,
    ActionUnavailable,
    ActionsDisabled,
    ActionResult,
    ActionRegistry,
    load_registry,
)
from oc_slimapi.config import Settings


def _os_pid_running(pid: int) -> bool:
    """Check if a process with the given PID is running (not zombie).

    Reads ``/proc/<pid>/stat``; treats 'Z' (zombie) as not running since
    a zombie holds no file descriptors and cannot keep a pipe open.
    Returns ``True`` only if the process exists and is not a zombie.
    Never raises."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        # field 3 (0-indexed: 2) is the process state character
        if len(fields) >= 3:
            return fields[2] != "Z"
        return False  # truncated stat → not reliably running
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (PermissionError, OSError):
        return True  # defensive: can't determine → assume running


# ---------------------------------------------------------------------------
# Manifest / registry builders
# ---------------------------------------------------------------------------


def _base_exec(**overrides) -> dict:
    fields = {
        "kind": "exec",
        "argv": [sys.executable, "-c", "pass"],
        "description": "ok",
        "min_interval_s": 0,
    }
    fields.update(overrides)
    return fields


def _base_query(**overrides) -> dict:
    fields = {
        "kind": "query",
        "argv": [sys.executable, "-c", "import sys; sys.stdout.write('hi')"],
        "description": "ok",
        "min_interval_s": 0,
    }
    fields.update(overrides)
    return fields


def _toml_body(actions: dict[str, dict]) -> str:
    """Render an actions dict to a manifest TOML body (JSON scalar syntax is
    valid TOML for strings / arrays; names are quoted for strict keys)."""
    lines: list[str] = []
    for name, fields in actions.items():
        lines.append(f"[actions.{json.dumps(name)}]")
        for key, value in fields.items():
            if value is None:
                continue  # None = field absent (e.g. "missing kind")
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, str):
                lines.append(f"{key} = {json.dumps(value)}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key} = {value!r}")
            elif isinstance(value, list):
                lines.append(f"{key} = {json.dumps(value)}")
            else:
                raise AssertionError(f"unsupported field value {key}={value!r}")
    return "\n".join(lines)


def _write_manifest(tmp_path, body: str, *, name: str = "manifest.toml", mode: int = 0o600):
    p = tmp_path / name
    p.write_text(body)
    os.chmod(p, mode)
    return p


def _registry(tmp_path, actions: dict[str, dict], *, max_concurrent: int = 4) -> ActionRegistry:
    p = _write_manifest(tmp_path, _toml_body(actions))
    reg = load_registry(Settings(actions_file=str(p), actions_max_concurrent=max_concurrent))
    assert reg.enabled is True, "test fixture manifest must load enabled"
    return reg


# ---------------------------------------------------------------------------
# Validation matrix — per-action failures drop only that action
# ---------------------------------------------------------------------------

_BAD_CASES = [
    pytest.param(_base_exec(kind="kexec"), id="bad-kind"),
    pytest.param(_base_exec(kind=None), id="missing-kind"),
    pytest.param(_base_exec(argv=["bin/true"]), id="relative-argv0"),
    pytest.param(_base_exec(argv=[]), id="empty-argv"),
    pytest.param(_base_exec(argv=None), id="missing-argv"),
    pytest.param(_base_exec(argv=["/bin/true", 5]), id="non-string-argv-element"),
    pytest.param(_base_exec(argv=["/bin/echo", "${HOME}"]), id="interpolation-argv"),
    pytest.param(_base_exec(argv=["/bin/echo", "$(id)"]), id="interpolation-command-sub"),
    pytest.param({**_base_exec(), "shell": True}, id="unknown-field"),
    pytest.param(_base_exec(max_output_bytes=1024), id="exec-with-max-output-bytes"),
    pytest.param(_base_query(require_confirm=True), id="query-with-require-confirm"),
    pytest.param(_base_exec(timeout_s=0), id="timeout-too-small"),
    pytest.param(_base_exec(timeout_s=601), id="timeout-too-large"),
    pytest.param(_base_exec(min_interval_s=-1), id="min-interval-negative"),
    pytest.param(_base_query(max_output_bytes=2 * 1024 * 1024), id="max-output-too-large"),
    pytest.param(_base_exec(description="bad\x01desc"), id="description-control-char"),
    pytest.param(_base_exec(description="x" * 300), id="description-too-long"),
]


@pytest.mark.parametrize("bad_fields", _BAD_CASES)
def test_bad_action_dropped_others_kept(tmp_path, bad_fields):
    reg = _registry(tmp_path, {"good": _base_exec(), "bad": bad_fields})
    names = {entry["name"] for entry in reg.discover()}
    assert names == {"good"}


def test_argv0_directory_dropped(tmp_path):
    reg = _registry(tmp_path, {"good": _base_exec(), "bad": _base_exec(argv=[str(tmp_path)])})
    assert {e["name"] for e in reg.discover()} == {"good"}


def test_argv0_not_executable_dropped(tmp_path):
    script = tmp_path / "noexec.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o644)
    reg = _registry(tmp_path, {"good": _base_exec(), "bad": _base_exec(argv=[str(script)])})
    assert {e["name"] for e in reg.discover()} == {"good"}


def test_argv0_missing_dropped(tmp_path):
    reg = _registry(
        tmp_path, {"good": _base_exec(), "bad": _base_exec(argv=[str(tmp_path / "ghost.sh")])}
    )
    assert {e["name"] for e in reg.discover()} == {"good"}


def test_invalid_name_dropped(tmp_path):
    reg = _registry(tmp_path, {"good": _base_exec(), "-bad": _base_exec()})
    assert {e["name"] for e in reg.discover()} == {"good"}


def test_overlong_name_dropped(tmp_path):
    reg = _registry(tmp_path, {"good": _base_exec(), "a" * 65: _base_exec()})
    assert {e["name"] for e in reg.discover()} == {"good"}


def test_relative_args_accepted(tmp_path):
    """argv[1:] are arbitrary non-empty strings — absolute paths NOT required."""
    reg = _registry(tmp_path, {"run": _base_exec(argv=["/bin/sh", "-c", "exit 0"])})
    assert {e["name"] for e in reg.discover()} == {"run"}


# ---------------------------------------------------------------------------
# Validation matrix — file-level failures disable the whole registry
# ---------------------------------------------------------------------------


def test_manifest_unset_disabled():
    reg = load_registry(Settings(actions_file=None, actions_max_concurrent=4))
    assert reg.enabled is False
    assert reg.discover() == []


def test_manifest_missing_disabled(tmp_path):
    reg = load_registry(
        Settings(actions_file=str(tmp_path / "nope.toml"), actions_max_concurrent=4)
    )
    assert reg.enabled is False
    assert reg.discover() == []


def test_manifest_malformed_toml_disabled(tmp_path):
    p = _write_manifest(tmp_path, "this is { not toml")
    reg = load_registry(Settings(actions_file=str(p), actions_max_concurrent=4))
    assert reg.enabled is False


def test_manifest_symlink_disabled(tmp_path):
    target = _write_manifest(tmp_path, _toml_body({"run": _base_exec()}), name="real.toml")
    link = tmp_path / "link.toml"
    link.symlink_to(target)
    reg = load_registry(Settings(actions_file=str(link), actions_max_concurrent=4))
    assert reg.enabled is False


def test_manifest_group_writable_disabled(tmp_path):
    p = _write_manifest(tmp_path, _toml_body({"run": _base_exec()}), mode=0o660)
    reg = load_registry(Settings(actions_file=str(p), actions_max_concurrent=4))
    assert reg.enabled is False


def test_manifest_non_owner_disabled(tmp_path, monkeypatch):
    p = _write_manifest(tmp_path, _toml_body({"run": _base_exec()}))
    monkeypatch.setattr("oc_slimapi.actions.os.geteuid", lambda: 123456)
    reg = load_registry(Settings(actions_file=str(p), actions_max_concurrent=4))
    assert reg.enabled is False


def test_manifest_extra_top_level_key_disabled(tmp_path):
    body = _toml_body({"run": _base_exec()}) + "\n[meta]\nfoo = 1\n"
    p = _write_manifest(tmp_path, body)
    reg = load_registry(Settings(actions_file=str(p), actions_max_concurrent=4))
    assert reg.enabled is False


# ---------------------------------------------------------------------------
# Exec semantics
# ---------------------------------------------------------------------------


async def test_exec_success(tmp_path):
    reg = _registry(tmp_path, {"run": _base_exec()})
    res = await reg.invoke("run", confirmed=True)  # confirm ignored (not required)
    assert res.kind == "exec"
    assert res.ok is True
    assert res.exit_code == 0
    assert res.message is None
    assert res.markdown is None
    assert res.truncated is False
    assert isinstance(res.duration_ms, int) and res.duration_ms >= 0


async def test_exec_nonzero_exit(tmp_path):
    reg = _registry(tmp_path, {"run": _base_exec(argv=[sys.executable, "-c", "import sys; sys.exit(3)"])})
    res = await reg.invoke("run", confirmed=False)
    assert res.ok is False
    assert res.exit_code == 3
    assert res.message == "non-zero exit"
    assert res.markdown is None


async def test_exec_signal_exit_negative(tmp_path):
    code = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    reg = _registry(tmp_path, {"run": _base_exec(argv=[sys.executable, "-c", code])})
    res = await reg.invoke("run", confirmed=False)
    assert res.ok is False
    assert res.exit_code == -signal.SIGTERM
    assert res.message == "non-zero exit"


# ---------------------------------------------------------------------------
# Query semantics
# ---------------------------------------------------------------------------


async def test_query_success_markdown(tmp_path):
    reg = _registry(
        tmp_path, {"q": _base_query(argv=[sys.executable, "-c", "import sys; sys.stdout.write('hello md')"])}
    )
    res = await reg.invoke("q", confirmed=False)
    assert res.kind == "query"
    assert res.ok is True
    assert res.exit_code == 0
    assert res.markdown == "hello md"
    assert res.truncated is False
    assert res.message is None


async def test_query_truncated_at_cap(tmp_path):
    reg = _registry(
        tmp_path,
        {"q": _base_query(argv=[sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000)"], max_output_bytes=1024)},
    )
    res = await reg.invoke("q", confirmed=False)
    assert res.ok is True
    assert res.truncated is True
    assert len(res.markdown) <= 1024


async def test_query_large_output_no_fake_timeout(tmp_path):
    """Output (200 KiB) exceeds the 64 KiB pipe buffer AND the 1 KiB cap.
    The executor must keep draining to EOF — stopping at the cap would fill
    the pipe and make the child block → a fake ActionTimeout."""
    reg = _registry(
        tmp_path,
        {
            "q": _base_query(
                argv=[sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
                max_output_bytes=1024,
                timeout_s=10,
            )
        },
    )
    res = await reg.invoke("q", confirmed=False)
    assert res.ok is True
    assert res.truncated is True
    assert len(res.markdown) <= 1024


async def test_query_stderr_excluded(tmp_path):
    reg = _registry(
        tmp_path,
        {"q": _base_query(argv=[sys.executable, "-c", "import sys; sys.stderr.write('STDERR-TOKEN-777'); sys.stdout.write('visible')"])},
    )
    res = await reg.invoke("q", confirmed=False)
    assert res.ok is True
    assert res.markdown == "visible"
    assert "STDERR-TOKEN-777" not in res.markdown


async def test_query_binary_output_replace(tmp_path):
    reg = _registry(
        tmp_path,
        {"q": _base_query(argv=[sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfeok')"])},
    )
    res = await reg.invoke("q", confirmed=False)
    assert res.ok is True
    assert res.markdown == "\ufffd\ufffdok"


async def test_query_nonzero_markdown_empty(tmp_path):
    reg = _registry(
        tmp_path,
        {"q": _base_query(argv=[sys.executable, "-c", "import sys; sys.stderr.write('oops'); sys.exit(2)"])},
    )
    res = await reg.invoke("q", confirmed=False)
    assert res.ok is False
    assert res.exit_code == 2
    assert res.markdown == ""


# ---------------------------------------------------------------------------
# Confirm gating
# ---------------------------------------------------------------------------


async def test_confirm_required_gate(tmp_path):
    reg = _registry(tmp_path, {"run": _base_exec(require_confirm=True)})
    with pytest.raises(ActionConfirmRequired) as ei:
        await reg.invoke("run", confirmed=False)
    assert ei.value.status_code == 409
    assert ei.value.code == "action_confirm_required"
    res = await reg.invoke("run", confirmed=True)
    assert res.ok is True


async def test_confirm_ignored_when_not_required(tmp_path):
    reg = _registry(tmp_path, {"run": _base_exec()})
    res = await reg.invoke("run", confirmed=True)
    assert res.ok is True


# ---------------------------------------------------------------------------
# Throttling / concurrency
# ---------------------------------------------------------------------------


async def test_min_interval_throttles(tmp_path):
    reg = _registry(tmp_path, {"run": _base_exec(min_interval_s=60)})
    first = await reg.invoke("run", confirmed=False)
    assert first.ok is True
    with pytest.raises(ActionThrottled) as ei:
        await reg.invoke("run", confirmed=False)
    assert 1 <= ei.value.retry_after <= 60
    assert ei.value.status_code == 429


async def test_single_flight_serializes(tmp_path):
    reg = _registry(
        tmp_path, {"run": _base_exec(argv=[sys.executable, "-c", "import time; time.sleep(2)"], timeout_s=15)}
    )

    async def call():
        return await reg.invoke("run", confirmed=False)

    results = await asyncio.gather(call(), call(), return_exceptions=True)
    ok = [r for r in results if isinstance(r, ActionResult)]
    throttled = [r for r in results if isinstance(r, ActionThrottled)]
    assert len(ok) == 1
    assert len(throttled) == 1
    assert throttled[0].retry_after == 2
    assert ok[0].ok is True


async def test_semaphore_busy_service_level(tmp_path, monkeypatch):
    manifest = {
        "a": _base_exec(argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=15),
        "b": _base_exec(argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=15),
    }
    reg = _registry(tmp_path, manifest, max_concurrent=1)
    # Happens-before: fire event when "a" has acquired the semaphore and starts spawning.
    # _spawn is @staticmethod — the original takes only spec (no self).
    spawn_started = asyncio.Event()
    original_spawn = ActionRegistry._spawn
    async def _spawn_signal(self, spec):
        spawn_started.set()
        return await original_spawn(spec)
    monkeypatch.setattr(ActionRegistry, "_spawn", _spawn_signal)
    holder = asyncio.create_task(reg.invoke("a", confirmed=False))
    await spawn_started.wait()  # "a" admitted + spawn started
    with pytest.raises(ActionBusy) as ei:
        await reg.invoke("b", confirmed=False)
    assert ei.value.retry_after == 2
    assert ei.value.status_code == 503
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    # The cancelled holder's child must have been killed (no orphan).
    out = subprocess.run(["pgrep", "-f", "time.sleep(5)"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Timeout — process-group kill (including grandchildren)
# ---------------------------------------------------------------------------


async def test_timeout_kills_process_group(tmp_path):
    reg = _registry(
        tmp_path, {"run": _base_exec(argv=["/bin/sh", "-c", "sleep 300.5 & sleep 300.5"], timeout_s=1)}
    )
    with pytest.raises(ActionTimeout) as ei:
        await reg.invoke("run", confirmed=False)
    assert ei.value.timeout_s == 1
    assert ei.value.status_code == 504
    # killpg must have taken down the shell AND both `sleep 300.5` children.
    out = subprocess.run(["pgrep", "-f", "sleep 300.5"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


async def test_timeout_killpg_survives_child_early_exit(tmp_path):
    """Bug-A regression: the child (shell) answers SIGTERM within grace (its
    ``trap 'exit 0' TERM`` exits immediately), but a backgrounded ``sleep 300.5``
    grandchild keeps running in the same process group — i.e. the grandchild
    outlives the child.  killpg must run UNCONDITIONALLY after terminate: the
    pre-fix code returned early from the grace wait and leaked the grandchild."""
    reg = _registry(
        tmp_path,
        {
            "run": _base_exec(
                argv=["/bin/sh", "-c", "trap 'exit 0' TERM; sleep 300.5 & wait"],
                timeout_s=1,
            )
        },
    )
    with pytest.raises(ActionTimeout) as ei:
        await reg.invoke("run", confirmed=False)
    assert ei.value.timeout_s == 1
    assert ei.value.status_code == 504
    # The shell died on SIGTERM well inside grace; the backgrounded `sleep 300.5`
    # outlived it.  killpg(pgid=SIGKILL) must have taken the sleep down too.
    out = subprocess.run(["pgrep", "-f", "sleep 300.5"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Spawn failure
# ---------------------------------------------------------------------------


async def test_spawn_failure_action_unavailable(tmp_path):
    script = tmp_path / "boom.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)
    reg = _registry(tmp_path, {"run": _base_exec(argv=[str(script)])})
    script.unlink()  # passes load validation, fails at spawn
    with pytest.raises(ActionUnavailable) as ei:
        await reg.invoke("run", confirmed=False)
    assert ei.value.status_code == 503
    assert ei.value.code == "action_unavailable"


# ---------------------------------------------------------------------------
# Unknown name / disabled registry
# ---------------------------------------------------------------------------


async def test_invoke_unknown_action(tmp_path):
    reg = _registry(tmp_path, {"run": _base_exec()})
    with pytest.raises(ActionNotFound) as ei:
        await reg.invoke("nope", confirmed=False)
    assert ei.value.status_code == 404
    assert ei.value.code == "action_not_found"


async def test_invoke_when_disabled():
    reg = load_registry(Settings(actions_file=None, actions_max_concurrent=4))
    with pytest.raises(ActionsDisabled):
        await reg.invoke("run", confirmed=False)


# ---------------------------------------------------------------------------
# Discover / error mapping (route-layer integration contract)
# ---------------------------------------------------------------------------


def test_discover_shape(tmp_path):
    reg = _registry(
        tmp_path,
        {
            "run": _base_exec(description="runs things", require_confirm=True),
            "q": _base_query(description="queries"),
        },
    )
    listing = {e["name"]: e for e in reg.discover()}
    assert set(listing) == {"run", "q"}
    assert listing["run"] == {
        "name": "run", "kind": "exec", "description": "runs things", "requireConfirm": True,
    }
    assert listing["q"]["requireConfirm"] is False


def test_error_mapping_to_coded():
    from oc_slimapi.errors import CodedHTTPException

    throttled = ActionThrottled(retry_after=2)
    coded = throttled.to_coded()
    assert isinstance(coded, CodedHTTPException)
    assert coded.status_code == 429
    assert coded.code == "action_throttled"
    assert coded.headers == {"Retry-After": "2"}

    timed_out = ActionTimeout(timeout_s=3.0)
    coded = timed_out.to_coded()
    assert coded.status_code == 504
    assert coded.code == "action_timeout"
    assert coded.fields == {"timeout_s": 3.0}

    busy = ActionBusy()
    coded = busy.to_coded()
    assert coded.status_code == 503
    assert coded.headers == {"Retry-After": "2"}

    assert ActionError.__module__ == "oc_slimapi.actions"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class _CaptureHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]):
        super().__init__(level=logging.WARNING)
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def audit_capture(monkeypatch):
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("oc_slimapi.actions_audit")
    handler = _CaptureHandler(records)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


@pytest.fixture
def app_log_capture(monkeypatch):
    """Capture the ``oc_slimapi.actions`` application-logger warnings (the
    journald-side stderr drain records)."""
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("oc_slimapi.actions")
    handler = _CaptureHandler(records)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def _audit_records(records: list[logging.LogRecord]) -> list[dict]:
    return [json.loads(record.getMessage()) for record in records]


async def test_audit_success(tmp_path, audit_capture):
    reg = _registry(tmp_path, {"run": _base_exec()})
    await reg.invoke("run", confirmed=False)
    recs = _audit_records(audit_capture)
    assert len(recs) == 1
    r = recs[0]
    assert r["action"] == "run"
    assert r["kind"] == "exec"
    assert r["exit_code"] == 0
    assert r["ok"] is True
    assert r["throttled"] is False
    assert r["timeout"] is False
    assert r["confirm"] is False
    assert r["duration_ms"] >= 0


async def test_audit_timeout(tmp_path, audit_capture):
    reg = _registry(tmp_path, {"run": _base_exec(argv=["/bin/sh", "-c", "sleep 300.5 & sleep 300.5"], timeout_s=1)})
    with pytest.raises(ActionTimeout):
        await reg.invoke("run", confirmed=False)
    recs = _audit_records(audit_capture)
    assert any(
        r["action"] == "run" and r["timeout"] is True and r["ok"] is False
        and r["exit_code"] is None
        for r in recs
    )


async def test_audit_timeout_confirms_flag(tmp_path, audit_capture):
    """Bug-B regression: a require_confirm action invoked with confirm=true
    must carry confirm=true in the timeout audit (previously hardcoded False
    inside ``_execute``)."""
    reg = _registry(
        tmp_path,
        {
            "run": _base_exec(
                argv=["/bin/sh", "-c", "sleep 300.5 & sleep 300.5"],
                timeout_s=1,
                require_confirm=True,
            )
        },
    )
    with pytest.raises(ActionTimeout):
        await reg.invoke("run", confirmed=True)
    recs = _audit_records(audit_capture)
    timeout_recs = [r for r in recs if r["timeout"] is True]
    assert len(timeout_recs) == 1
    assert timeout_recs[0]["confirm"] is True


async def test_audit_spawn_failure(tmp_path, audit_capture):
    script = tmp_path / "boom.sh"
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o755)
    reg = _registry(tmp_path, {"run": _base_exec(argv=[str(script)])})
    script.unlink()
    with pytest.raises(ActionUnavailable):
        await reg.invoke("run", confirmed=False)
    recs = _audit_records(audit_capture)
    assert any(
        r["action"] == "run" and r["ok"] is False and r["exit_code"] is None
        and r["timeout"] is False and r["throttled"] is False
        for r in recs
    )


async def test_audit_throttle(tmp_path, audit_capture):
    reg = _registry(tmp_path, {"run": _base_exec(min_interval_s=60)})
    await reg.invoke("run", confirmed=False)
    with pytest.raises(ActionThrottled):
        await reg.invoke("run", confirmed=False)
    recs = _audit_records(audit_capture)
    assert len(recs) == 2
    assert recs[1]["throttled"] is True


async def test_audit_disconnect(tmp_path, audit_capture, monkeypatch):
    # Shell: background two long-lived sleeps, write each child PID to a
    # pidfile via ``$!`` (POSIX: PID of the most recent background command).
    # ``wait`` keeps the shell alive until the children terminate (so the
    # process group leader does NOT exit before cancellation).
    pidfile1 = tmp_path / "child1.pid"
    pidfile2 = tmp_path / "child2.pid"
    shell_cmd = (
        f"sleep 300.5 & echo $! > {shlex.quote(str(pidfile1))}; "
        f"sleep 300.5 & echo $! > {shlex.quote(str(pidfile2))}; "
        f"wait"
    )
    reg = _registry(
        tmp_path,
        {"run": _base_exec(argv=["/bin/sh", "-c", shell_cmd], timeout_s=60)},
    )
    # Capture the group-leader PID (shell) via the spawn seam.
    proc_pid = None
    child_pids: list[int] = []
    original_spawn = ActionRegistry._spawn
    async def _spawn_capture(self, spec):
        nonlocal proc_pid
        proc = await original_spawn(spec)
        proc_pid = proc.pid
        return proc
    monkeypatch.setattr(ActionRegistry, "_spawn", _spawn_capture)

    # Post-spawn happens-before seam: _drain_stdout is called only after
    # ``proc = await asyncio.shield(spawn_task)`` succeeds at line 651
    # (``_execute``).  At that point the caller has a valid ``Process`` handle
    # and the child is running — a cancellation lands in the *post-spawn*
    # running path, not the spawn-phase (Bug F) path.
    post_spawn = asyncio.Event()
    original_drain = ActionRegistry._drain_stdout
    async def _drain_signal(self, proc, spec, state):
        post_spawn.set()
        return await original_drain(proc, spec, state)
    monkeypatch.setattr(ActionRegistry, "_drain_stdout", _drain_signal)

    task = asyncio.create_task(reg.invoke("run", confirmed=False))
    await post_spawn.wait()  # proc is assigned, child is running

    # Wait for both background child pidfiles to appear, then read PIDs.
    deadline = time.monotonic() + 3.0
    for pf in (pidfile1, pidfile2):
        while time.monotonic() < deadline:
            if pf.exists():
                break
            await asyncio.sleep(0.05)
        assert pf.exists(), f"background child pidfile {pf} not created within deadline"
        child_pids.append(int(pf.read_text().strip()))

    all_pids = [proc_pid] + child_pids
    assert all(p is not None for p in all_pids), "failed to capture all descendant PIDs"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recs = _audit_records(audit_capture)
    assert any(
        r["action"] == "run" and r["ok"] is False and r["exit_code"] is None
        and r["timeout"] is False and r["throttled"] is False
        for r in recs
    )
    # Verify the entire process group (leader + both background children) was
    # killed by ActionRegistry's killpg.  Bounded poll on exact PIDs — no
    # global ``pgrep``.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not any(_os_pid_running(p) for p in all_pids):
            break
        await asyncio.sleep(0.05)
    running = [p for p in all_pids if _os_pid_running(p)]
    assert not running, f"descendant(s) still alive after cleanup: {running}"


async def test_audit_under_error_log_level(tmp_path, audit_capture, monkeypatch):
    monkeypatch.setattr(logging.getLogger("oc_slimapi"), "level", logging.ERROR)
    reg = _registry(tmp_path, {"run": _base_exec()})
    await reg.invoke("run", confirmed=False)
    assert len(audit_capture) == 1


# ---------------------------------------------------------------------------
# rev-13: stderr cap is BYTE-based (not chunk-count) + full drain to EOF
# ---------------------------------------------------------------------------


async def test_stderr_cap_by_bytes_not_chunks(tmp_path, app_log_capture):
    """A child writing >64 KiB to stderr must not accumulate it in memory and
    the pipe must be drained to EOF (no pipe-block → no fake timeout).

    The pre-fix chunk-count cap (``len(chunks) < 65536``) kept every 4 KiB
    chunk — ≈256 MiB worst case — before the claimed 64 KiB journald cap ever
    engaged.  The byte-based cap now truncates the retained buffer at 64 KiB
    while still draining the full pipe."""
    code = "import sys; sys.stderr.buffer.write(b'x' * (200 * 1024)); sys.exit(0)"
    reg = _registry(
        tmp_path,
        {"run": _base_exec(argv=[sys.executable, "-c", code], timeout_s=15)},
    )
    res = await reg.invoke("run", confirmed=False)
    assert res.ok is True
    assert res.exit_code == 0
    msgs = [r.getMessage() for r in app_log_capture]
    stderr = [m for m in msgs if m.startswith("action run stderr (")]
    assert len(stderr) == 1
    assert "truncated" in stderr[0]
    payload = stderr[0].split("): ", 1)[1]
    assert len(payload) <= 64 * 1024


async def test_stderr_small_logged_without_truncation(tmp_path, app_log_capture):
    reg = _registry(
        tmp_path,
        {"run": _base_exec(argv=[sys.executable, "-c",
                                 "import sys; sys.stderr.write('small-err-msg')"])},
    )
    res = await reg.invoke("run", confirmed=False)
    assert res.ok is True
    msgs = [r.getMessage() for r in app_log_capture]
    stderr = [m for m in msgs if m.startswith("action run stderr (")]
    assert len(stderr) == 1
    assert "truncated" not in stderr[0]
    assert stderr[0].endswith(": small-err-msg")


async def test_stderr_cap_query_body_unaffected(tmp_path, app_log_capture):
    """A >64 KiB stderr flood must not leak into the query markdown body."""
    code = ("import sys; sys.stderr.buffer.write(b'e' * (200 * 1024)); "
            "sys.stdout.write('visible')")
    reg = _registry(
        tmp_path,
        {"q": _base_query(argv=[sys.executable, "-c", code], timeout_s=15)},
    )
    res = await reg.invoke("q", confirmed=False)
    assert res.ok is True
    assert res.markdown == "visible"
    msgs = [r.getMessage() for r in app_log_capture]
    stderr = [m for m in msgs if m.startswith("action q stderr (")]
    assert len(stderr) == 1 and "truncated" in stderr[0]


# ---------------------------------------------------------------------------
# rev-13: unified lifecycle — killpg-after-exit + drain deadline + drain-phase
# cancellation (Bug C)
# ---------------------------------------------------------------------------


async def test_drain_hang_grandchild_holding_pipe(tmp_path):
    """Bug C regression: the child (sh) exits immediately but a backgrounded
    ``sleep 300.5`` grandchild keeps the stdout pipe write end open in the same
    process group.  Pre-rev-13 the success path hung indefinitely (asyncio's
    ``Process.wait`` only resolves once the pipes ALSO disconnect, and the
    post-exit drain was unbounded).  Now: exit detection polls ``returncode``
    (independent of the pipes), killpg-after-exit releases the pipe, and the
    invoke completes well inside a generous bound."""
    reg = _registry(
        tmp_path,
        {"run": _base_query(argv=["/bin/sh", "-c", "sleep 300.5 &"], timeout_s=30)},
    )
    res = await asyncio.wait_for(reg.invoke("run", confirmed=False), timeout=15)
    assert res.ok is True
    assert res.exit_code == 0
    assert res.markdown == ""
    # killpg must have taken the grandchild down (no orphan).
    out = subprocess.run(["pgrep", "-f", "sleep 300.5"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


async def test_drain_deadline_truncates_escaped_grandchild(tmp_path, monkeypatch):
    """The belt-and-suspenders bound: a grandchild that ESCAPED the process
    group (``setsid``) while holding the stdout pipe is unreachable by killpg,
    so the drain stalls past the hard deadline → the invoke still returns
    (partial output, marked truncated) instead of hanging."""
    monkeypatch.setattr("oc_slimapi.actions._DRAIN_DEADLINE_S", 0.5)
    reg = _registry(
        tmp_path,
        {"q": _base_query(argv=["/bin/sh", "-c", "setsid sleep 2 &"], timeout_s=60)},
    )
    res = await asyncio.wait_for(reg.invoke("q", confirmed=False), timeout=15)
    assert res.ok is True  # child exited 0; drain deadline ≠ action timeout
    assert res.truncated is True
    assert res.markdown == ""
    # The escaped grandchild (setsid) is out of killpg reach by design; it
    # self-terminates after its bounded sleep. A one-shot 2s sleep lands
    # exactly on the `sleep 2` lifetime boundary and races pgrep — wait it
    # out with a bounded poll instead.  The pattern is anchored so it only
    # matches the literal `sleep 2` process (a bare ``-f "sleep 2"`` also
    # substring-matches unrelated long-lived shells, e.g. a ``sleep 20`` in
    # their command line).
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        out = subprocess.run(["pgrep", "-f", "^sleep 2$"], capture_output=True, text=True)
        if out.stdout.strip() == "":
            break
        await asyncio.sleep(0.1)
    else:
        out = subprocess.run(["pgrep", "-f", "^sleep 2$"], capture_output=True, text=True)
    assert out.stdout.strip() == "", f"escaped grandchild still alive: {out.stdout!r}"


async def test_drain_phase_cancel_audits_and_cleans(tmp_path, audit_capture):
    """A client disconnect DURING the post-exit drain must still run the
    unified finally cleanup: killpg + reap + audit + drain-task cancel.

    The child (sh) exits immediately; a grandchild that ESCAPED the process
    group via ``setsid`` holds the stdout pipe open, so the drain genuinely
    stalls and the cancellation lands in the drain phase.  The escaped
    grandchild is (by design) out of killpg's reach — it self-terminates on its
    bounded sleep, and the test waits it out so nothing outlives the test."""
    reg = _registry(
        tmp_path,
        {"run": _base_exec(argv=["/bin/sh", "-c", "setsid sleep 2 &"], timeout_s=60)},
    )
    task = asyncio.create_task(reg.invoke("run", confirmed=False))
    await asyncio.sleep(0.5)  # sh exited; drain stalled on the escaped pipe-holder
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    recs = _audit_records(audit_capture)
    assert any(
        r["action"] == "run" and r["ok"] is False and r["exit_code"] is None
        and r["timeout"] is False and r["throttled"] is False
        for r in recs
    )
    # The escaped grandchild (setsid) is out of killpg reach by design; it
    # self-terminates after its bounded sleep — wait it out (bounded) so
    # nothing outlives the test.  Anchored pattern: only the literal
    # `sleep 2` process matches (a bare ``-f "sleep 2"`` would also catch
    # unrelated shells whose cmdline contains ``sleep 20``).
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        out = subprocess.run(["pgrep", "-f", "^sleep 2$"], capture_output=True, text=True)
        if out.stdout.strip() == "":
            break
        await asyncio.sleep(0.1)
    else:
        out = subprocess.run(["pgrep", "-f", "^sleep 2$"], capture_output=True, text=True)
    assert out.stdout.strip() == "", f"escaped grandchild still alive: {out.stdout!r}"


async def test_audit_disconnect_during_semaphore_wait(tmp_path, audit_capture, monkeypatch):
    """Bug E regression: cancelling an invoke parked on the admission
    semaphore must emit a disconnect audit record (previously the cancellation
    propagated WITHOUT any audit)."""
    manifest = {
        "a": _base_exec(argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=15),
        "b": _base_exec(argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=15),
    }
    reg = _registry(tmp_path, manifest, max_concurrent=1)
    # Happens-before: fire event when "a" has acquired the semaphore + spawns.
    # _spawn is @staticmethod — the original takes only spec (no self).
    spawn_started = asyncio.Event()
    original_spawn = ActionRegistry._spawn
    async def _spawn_signal(self, spec):
        spawn_started.set()
        return await original_spawn(spec)
    monkeypatch.setattr(ActionRegistry, "_spawn", _spawn_signal)
    holder = asyncio.create_task(reg.invoke("a", confirmed=False))
    await spawn_started.wait()  # "a" admitted + spawning
    # Happens-before: fire event when "b" enters the semaphore acquire (will block).
    # "a"'s acquire already happened before the tracker was installed, so "b"'s
    # call is the first call the tracker sees (count == 1).
    parked_on_semaphore = asyncio.Event()
    original_acquire = reg._semaphore.acquire
    async def _tracked_acquire():
        parked_on_semaphore.set()  # first tracked call = "b" attempting acquire
        return await original_acquire()
    monkeypatch.setattr(reg._semaphore, "acquire", _tracked_acquire)
    parked = asyncio.create_task(reg.invoke("b", confirmed=False))
    await parked_on_semaphore.wait()  # "b" parked on the (held) semaphore
    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    recs = _audit_records(audit_capture)
    b_recs = [r for r in recs if r["action"] == "b"]
    assert len(b_recs) == 1
    assert b_recs[0]["ok"] is False and b_recs[0]["exit_code"] is None
    assert b_recs[0]["timeout"] is False and b_recs[0]["throttled"] is False


# ---------------------------------------------------------------------------
# rev-14: spawn-phase cancellation must not orphan the child (Bug F)
# ---------------------------------------------------------------------------


async def test_spawn_cancelled_during_spawn_cleans_child(tmp_path, audit_capture, monkeypatch):
    """Bug F regression: a client disconnect while ``create_subprocess_exec``
    is still in flight used to orphan the spawned child — ``proc`` was never
    assigned, so no killpg ran (the child AND its process group leaked) and no
    disconnect audit fired.

    The spawn now runs as a shielded task (:meth:`ActionRegistry._spawn`
    wrapped in ``asyncio.shield``): the outer cancellation cannot propagate
    into the spawn task, and the unified ``finally`` shields it to completion,
    recovers the ``Process`` handle, and runs the full killpg + reap +
    failure-path-audit cleanup."""
    real_spawn = ActionRegistry._spawn
    spawn_entered = asyncio.Event()

    async def slow_spawn(self, spec):
        spawn_entered.set()  # signal: invoke entered the spawn function
        # Widen the spawn window so the cancellation deterministically lands
        # mid-spawn (before any Process handle exists).
        await asyncio.sleep(0.3)
        return await real_spawn(spec)  # _spawn is @staticmethod → no self

    monkeypatch.setattr(ActionRegistry, "_spawn", slow_spawn)
    reg = _registry(
        tmp_path,
        {"run": _base_exec(argv=["/bin/sh", "-c", "sleep 31 & sleep 31"], timeout_s=60)},
    )
    task = asyncio.create_task(reg.invoke("run", confirmed=False))
    await spawn_entered.wait()  # invoke is parked inside the (sleeping) spawn
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The spawn completed behind the shield and the finally cleaned it up: a
    # disconnect audit record MUST exist...
    recs = _audit_records(audit_capture)
    assert any(
        r["action"] == "run" and r["ok"] is False and r["exit_code"] is None
        and r["timeout"] is False and r["throttled"] is False
        for r in recs
    )
    # ...and the spawned process group must be gone (no orphaned children).
    out = subprocess.run(["pgrep", "-f", "^sleep 31$"], capture_output=True, text=True)
    assert out.stdout.strip() == ""


# ---------------------------------------------------------------------------
# rev-14: drain deadline preserves the partial stdout (shared holder)
# ---------------------------------------------------------------------------


async def test_drain_deadline_preserves_partial_output(tmp_path, monkeypatch):
    """The drain deadline force-cancels the stdout drain task; the partial
    output accumulated before the cancel must survive (it now lives in the
    shared :class:`_DrainState` holder, not task-local state) — the query
    returns the received bytes as markdown + ``truncated`` instead of an empty
    markdown.

    Setup: the sh child writes a prefix then exits; a ``setsid`` grandchild
    escapes the process group and keeps the stdout pipe write end open, so the
    drain genuinely stalls and the hard deadline fires.

    The escaped grandchild's exact PID is captured via a pidfile so cleanup
    targets only that process — never a global ``pgrep`` scan that could
    collide with unrelated ``sleep`` processes on the system."""
    pidfile = tmp_path / "escaped-grandchild.pid"
    monkeypatch.setattr("oc_slimapi.actions._DRAIN_DEADLINE_S", 0.5)
    reg = _registry(
        tmp_path,
        {
            "q": _base_query(
                argv=[
                    "/bin/sh", "-c",
                    f"printf 'partial-data-here'; setsid sleep 30 & echo $! > {shlex.quote(str(pidfile))}",
                ],
                timeout_s=60,
            )
        },
    )
    res = await asyncio.wait_for(reg.invoke("q", confirmed=False), timeout=15)
    assert res.ok is True  # child exited 0; drain deadline ≠ action timeout
    assert res.truncated is True
    assert res.markdown == "partial-data-here"

    # Read the exact PID of the escaped setsid grandchild.
    escaped_pid = int(pidfile.read_text().strip())

    # Verify the grandchild is still running (confirming it kept the pipe open
    # and triggered the drain deadline).
    assert _os_pid_running(escaped_pid), (
        f"escaped grandchild {escaped_pid} exited before drain deadline could trigger"
    )

    # Clean up the exact grandchild.  Never orphan — the ``finally`` block is
    # the last-resort safety net even if an assertion or signal fails.
    try:
        os.kill(escaped_pid, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not _os_pid_running(escaped_pid):
                break
            await asyncio.sleep(0.05)
        else:
            # SIGTERM didn't work within deadline → escalate to SIGKILL.
            os.kill(escaped_pid, signal.SIGKILL)
            deadline2 = time.monotonic() + 2.0
            while time.monotonic() < deadline2:
                if not _os_pid_running(escaped_pid):
                    break
                await asyncio.sleep(0.05)
    finally:
        if _os_pid_running(escaped_pid):
            try:
                os.kill(escaped_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert not _os_pid_running(escaped_pid), (
        f"escaped grandchild {escaped_pid} still alive after cleanup"
    )


# ---------------------------------------------------------------------------
# rev-13: spawn ValueError → action_unavailable; name regex \Z
# ---------------------------------------------------------------------------


async def test_spawn_valueerror_action_unavailable(tmp_path):
    """``create_subprocess_exec`` raises ValueError (embedded NUL byte in an
    argv element) → mapped to action_unavailable, not a raw 500."""
    reg = _registry(tmp_path, {"run": _base_exec(argv=[sys.executable, "bad\0arg"])})
    with pytest.raises(ActionUnavailable) as ei:
        await reg.invoke("run", confirmed=False)
    assert ei.value.status_code == 503
    assert ei.value.code == "action_unavailable"


def test_name_trailing_newline_dropped(tmp_path):
    """Name regex uses ``\\Z`` (not ``$``): a trailing newline must not sneak
    past the name gate (``$`` also matches just before a final ``\\n``)."""
    reg = _registry(tmp_path, {"good": _base_exec(), "bad\n": _base_exec()})
    assert {e["name"] for e in reg.discover()} == {"good"}


# ---------------------------------------------------------------------------
# P2-2 (Task 11): action subprocess environment allowlist
# ---------------------------------------------------------------------------


def test_build_action_env_copies_only_allowlist_keys():
    """``_build_action_env`` must copy only allowlist keys present in *source*.

    A non-allowlisted-but-sane var like USER, plus sidecar vars
    (OC_SLIMAPI_UPSTREAM) and arbitrary secrets (SECRET_TOKEN), are dropped.
    """
    from oc_slimapi.actions import _build_action_env

    source = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/root",
        "USER": "root",                # benign but NOT in allowlist → dropped
        "OC_SLIMAPI_UPSTREAM": "http://127.0.0.1:4096",  # sidecar var → dropped
        "SECRET_TOKEN": "hunter2",     # arbitrary secret → dropped
    }
    result = _build_action_env(source)
    assert set(result.keys()) == {"PATH", "HOME"}
    assert result["PATH"] == "/usr/bin:/bin"
    assert result["HOME"] == "/root"


def test_build_action_env_drops_oc_slimapi_vars():
    """All ``OC_SLIMAPI_*`` vars are dropped even if PATH is present.

    No fuzzy "name contains secret" rule — this is a strict allowlist, so the
    entire OC_SLIMAPI_* family is excluded by construction.
    """
    from oc_slimapi.actions import _build_action_env

    source = {
        "OC_SLIMAPI_UPSTREAM": "http://127.0.0.1:4096",
        "OC_SLIMAPI_STATE_DIR": "/var/lib/oc-slimapi",
        "OC_SLIMAPI_ACCESS_LOG_DIR": "/var/log/oc-slimapi",
        "PATH": "/usr/bin:/bin",
    }
    result = _build_action_env(source)
    assert set(result.keys()) == {"PATH"}
    assert all(not k.startswith("OC_SLIMAPI_") for k in result)
