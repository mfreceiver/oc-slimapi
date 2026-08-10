"""``/slimapi/actions`` — configuration-driven generic admin action framework.

Two action kinds, both declared server-side in a TOML manifest (the caller
only names an action — argv is fully fixed by the manifest, never caller-
supplied):

* ``exec`` — trigger a server-side command, echo a status envelope
  ``{kind, ok, exit_code, duration_ms, message?}``.
* ``query`` — trigger a command, echo its stdout as renderable markdown
  ``{kind, ok, markdown, exit_code, duration_ms, truncated, message?}``.

Security posture (also pasted into the manifest header comment and
operations.md): this is a **risk-accepted** surface, co-equal to the
pre-existing plaintext catch-all → opencode control endpoints
(``/global/upgrade``, ``/global/config`` PATCH, ...).  Low-cost mitigations —
not authorization: default-empty manifest, spawn concurrency cap,
per-action single-flight + min-interval throttling, owner-only-write
manifest, non-disableable structured audit, ``shell=False``, and an argv
interpolation scan as a regression guard.

:func:`load_registry` is best-effort (mirrors the access-log pattern in
``app.py``): an unset / missing / unreadable / invalid manifest disables the
feature with a warning — it never crashes lifespan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import signal
import stat
import time
import tomllib
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping

from .errors import CodedHTTPException

# ---------------------------------------------------------------------------
# Code-level constants (wire-invariant; not env knobs)
# ---------------------------------------------------------------------------

# \Z (not $) so a trailing newline cannot sneak past the name gate: re.$ also
# matches just before a final "\n", which would let "name\n" through.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]*\Z")
_MAX_NAME_LEN = 64

_DEFAULT_TIMEOUT_S = 30.0
_TIMEOUT_S_MIN, _TIMEOUT_S_MAX = 1.0, 600.0
_DEFAULT_MIN_INTERVAL_EXEC = 30.0
_DEFAULT_MIN_INTERVAL_QUERY = 0.0
_DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024      # query default cap (spec §2)
_MAX_OUTPUT_BYTES_CAP = 1024 * 1024        # 1 MiB hard cap (spec §2 rule 7)
_DESCRIPTION_MAX_LEN = 256

_READ_CHUNK = 4096                         # 4 KiB stdout/stderr stream chunks
_STDERR_LOG_CAP = 64 * 1024                # stderr journald truncation cap (bytes)
_DRAIN_DEADLINE_S = 5.0                    # hard drain bound (Bug C, rev-13)
_CLEANUP_REAP_S = 5.0                      # reap bound inside the unified cleanup
_ADMISSION_TIMEOUT_S = 2.0                 # semaphore acquire budget → ActionBusy
_EXEC_KINDS = frozenset({"exec", "query"})
_ALLOWED_FIELDS = frozenset({
    "kind", "argv", "description", "timeout_s", "min_interval_s",
    "require_confirm", "max_output_bytes", "cwd",
})
# Regression guard markers (spec §2 rule 4): with shell=False these literal
# strings are never interpreted — the scan exists to fail loudly if a future
# change ever switches to shell=True.
_INTERPOLATION_MARKERS = ("${", "%(", "$(")

_AUDIT_LOGGER = logging.getLogger("oc_slimapi.actions_audit")
_APP_LOGGER = logging.getLogger("oc_slimapi.actions")

# P2-2 (Task 11): action subprocess environment allowlist.
# Fail-closed: only these vars are inherited by spawned action subprocesses.
# Rationale: the sidecar's own OC_SLIMAPI_* config vars (upstream URL, paths,
# version gate, salt, …) must NEVER leak into an action's environment — an
# action could otherwise exfiltrate sidecar internals or be influenced by them.
# No fuzzy "name contains secret" rule; this is a strict allowlist. Existing
# manifest actions (e.g. /usr/bin/systemctl --user) depend on DBUS_SESSION_BUS_ADDRESS
# and XDG_RUNTIME_DIR, both included below.
_ACTION_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
)


def _build_action_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a fail-closed env dict for action subprocesses.

    Only keys in :data:`_ACTION_ENV_ALLOWLIST` present in *source* (default
    ``os.environ``) are copied. ``OC_SLIMAPI_*`` and every other sidecar-specific
    variable is dropped because it is not in the allowlist. Returns a fresh dict.
    """
    env = dict(os.environ) if source is None else dict(source)
    return {k: v for k, v in env.items() if k in _ACTION_ENV_ALLOWLIST}


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """A validated, registry-held action declaration (from the manifest)."""

    name: str
    kind: str                       # "exec" | "query"
    argv: tuple[str, ...]
    description: str
    timeout_s: float
    min_interval_s: float
    require_confirm: bool           # exec only; query is always False
    max_output_bytes: int | None    # query only; exec is always None
    cwd: str | None                 # None → inherit the sidecar cwd


@dataclass
class ActionResult:
    """Outcome of an action invocation (both kinds; timeout/spawn-fail raise
    instead of returning, so ``exit_code`` there is None — but never here)."""

    kind: str
    ok: bool
    exit_code: int | None           # never None on the success path
    duration_ms: int                # wall time incl. any cleanup
    markdown: str | None            # query only (exec → None)
    truncated: bool                 # query only (exec → False)
    message: str | None


@dataclass
class _DrainState:
    """Shared accumulator for the stdout drain (rev-14).

    The drain task writes its accumulated bytes and the truncation flag onto
    this holder instead of task-local variables, so when the drain deadline
    force-cancels the task (:meth:`ActionRegistry._drain_with_deadline`) the
    partial output remains readable from the holder — task-local state would
    be destroyed with the task.  ``exec`` actions never fill ``kept``.
    """

    kept: bytearray = field(default_factory=bytearray)
    truncated: bool = False


# ---------------------------------------------------------------------------
# Errors (registry.invoke raises these; the routes layer maps them via
# ``to_coded()`` to :class:`~oc_slimapi.errors.CodedHTTPException`)
# ---------------------------------------------------------------------------


class ActionError(Exception):
    """Base class for action invocation failures with an HTTP mapping.

    Subclasses declare ``status_code`` / ``code``; instance attributes
    ``retry_after`` / ``timeout_s`` feed the ``Retry-After`` header and the
    ``timeout_s`` body field respectively.
    """

    status_code: ClassVar[int]
    code: ClassVar[str]
    headers: ClassVar[dict[str, str]] = {}

    def to_coded(self) -> CodedHTTPException:
        headers = dict(self.headers)
        retry_after = getattr(self, "retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        fields: dict[str, Any] = {}
        timeout_s = getattr(self, "timeout_s", None)
        if timeout_s is not None:
            fields["timeout_s"] = timeout_s
        return CodedHTTPException(
            self.status_code, code=self.code, headers=headers, **fields
        )


class ActionsDisabled(ActionError):
    """Manifest not configured (or a file-level failure disabled the feature)."""

    status_code = 503
    code = "actions_disabled"


class ActionNotFound(ActionError):
    """The requested name is not in the registry."""

    status_code = 404
    code = "action_not_found"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown action {name!r}")


class ActionConfirmRequired(ActionError):
    """``exec`` action with ``require_confirm=true`` invoked without confirm."""

    status_code = 409
    code = "action_confirm_required"


class ActionThrottled(ActionError):
    """Action-level throttle: single-flight conflict or min_interval window."""

    status_code = 429
    code = "action_throttled"

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"action throttled; retry after {retry_after}s")


class ActionBusy(ActionError):
    """Service-level admission: the global spawn semaphore is saturated."""

    status_code = 503
    code = "action_busy"
    retry_after = 2


class ActionTimeout(ActionError):
    """The action exceeded its ``timeout_s`` budget."""

    status_code = 504
    code = "action_timeout"

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(f"action timed out after {timeout_s}s")


class ActionUnavailable(ActionError):
    """Spawn failed (OSError family: ENOENT / EACCES / EMFILE / ENOMEM ...)."""

    status_code = 503
    code = "action_unavailable"


# ---------------------------------------------------------------------------
# Manifest loading + validation (spec §2; fail-closed)
# ---------------------------------------------------------------------------


class _ManifestError(Exception):
    """A single validation failure inside the manifest.  Caught by
    :func:`load_registry` → per-action drop (WARNING) or, for file-level
    failures, full disable (ERROR).  Never propagates out of
    :func:`load_registry`."""


def _load_manifest(path: str, logger: logging.Logger) -> dict[str, ActionSpec]:
    """Load + validate the manifest; returns the valid action specs.

    File-level failures raise :class:`_ManifestError` (→ full disable);
    per-action failures are logged as WARNING and the action dropped.  The
    manifest file itself is the real privileged surface, so its checks use a
    single ``os.open`` + ``fstat`` (no check-then-open TOCTOU window beyond
    the opened descriptor itself).
    """
    if os.path.islink(path):
        raise _ManifestError("manifest path is a symlink (must be a regular file)")
    real = os.path.realpath(path)
    fd = os.open(real, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _ManifestError("manifest does not resolve to a regular file")
        # Owner-only-write: no group/other write bits (0o022 mask).
        if st.st_mode & 0o022:
            raise _ManifestError(
                "manifest is group/other-writable (must be owner-only-write)"
            )
        if st.st_uid != os.geteuid():
            raise _ManifestError("manifest is not owned by the runtime user")
        with os.fdopen(fd, "rb") as f:
            fd = -1  # ownership transferred to the file object
            root = tomllib.load(f)
    finally:
        if fd != -1:
            os.close(fd)

    if not isinstance(root, dict):
        raise _ManifestError("manifest root must be a TOML table")
    unknown_root = set(root) - {"actions"}
    if unknown_root:
        raise _ManifestError(
            f"manifest root must contain exactly the 'actions' table "
            f"(unknown top-level key(s): {sorted(unknown_root)})"
        )
    actions_table = root["actions"]
    if not isinstance(actions_table, dict):
        raise _ManifestError("'actions' must be a table of action definitions")

    specs: dict[str, ActionSpec] = {}
    for name, raw in actions_table.items():
        try:
            specs[name] = _validate_action(name, raw)
        except _ManifestError as exc:
            logger.warning("actions manifest: action %r rejected: %s", name, exc)
    return specs


def _validate_action(name: str, raw: Any) -> ActionSpec:
    if not isinstance(raw, dict):
        raise _ManifestError("action must be a TOML table")
    unknown = set(raw) - _ALLOWED_FIELDS
    if unknown:
        raise _ManifestError(f"unknown field(s): {sorted(unknown)}")

    # Rule 6: action name (TOML guarantees uniqueness of the table keys).
    if not _NAME_RE.match(name) or len(name) > _MAX_NAME_LEN:
        raise _ManifestError(
            f"invalid name {name!r} (must match {_NAME_RE.pattern}, <= {_MAX_NAME_LEN} chars)"
        )

    # Rule 1: kind.
    kind = raw.get("kind")
    if kind not in _EXEC_KINDS:
        raise _ManifestError(f"kind must be one of {sorted(_EXEC_KINDS)}, got {kind!r}")

    # Rules 2-4: argv.
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv:
        raise _ManifestError("argv must be a non-empty array of strings")
    if not all(isinstance(a, str) and a for a in argv):
        raise _ManifestError("argv elements must be non-empty strings")
    if not os.path.isabs(argv[0]):
        raise _ManifestError("argv[0] must be an absolute path")
    for marker in _INTERPOLATION_MARKERS:
        if any(marker in a for a in argv):
            raise _ManifestError(
                f"argv contains interpolation marker {marker!r} "
                "(regression guard; rejected)"
            )
    real0 = os.path.realpath(argv[0])
    if not os.path.isfile(real0):
        raise _ManifestError(f"argv[0] {argv[0]!r} does not resolve to a regular file")
    if not os.access(real0, os.X_OK):
        raise _ManifestError(f"argv[0] {argv[0]!r} is not executable")

    # Rule 9: description.
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise _ManifestError("description must be a string")
    if len(description) > _DESCRIPTION_MAX_LEN:
        raise _ManifestError(f"description longer than {_DESCRIPTION_MAX_LEN} chars")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in description):
        raise _ManifestError("description contains control characters")

    # Rule 7: numeric bounds.
    timeout_s = _as_number(raw, "timeout_s", _DEFAULT_TIMEOUT_S)
    if not _TIMEOUT_S_MIN <= timeout_s <= _TIMEOUT_S_MAX:
        raise _ManifestError(
            f"timeout_s must be in [{_TIMEOUT_S_MIN}, {_TIMEOUT_S_MAX}]"
        )
    default_interval = (
        _DEFAULT_MIN_INTERVAL_EXEC if kind == "exec" else _DEFAULT_MIN_INTERVAL_QUERY
    )
    min_interval_s = _as_number(raw, "min_interval_s", default_interval)
    if min_interval_s < 0:
        raise _ManifestError("min_interval_s must be >= 0")

    # Rule 8: kind mutual exclusion.
    if kind == "exec" and "max_output_bytes" in raw:
        raise _ManifestError("exec actions must not set max_output_bytes")
    if kind == "query" and "require_confirm" in raw:
        raise _ManifestError("query actions must not set require_confirm")

    max_output_bytes: int | None = None
    if kind == "query":
        value = raw.get("max_output_bytes", _DEFAULT_MAX_OUTPUT_BYTES)
        if not isinstance(value, int) or isinstance(value, bool):
            raise _ManifestError("max_output_bytes must be an integer")
        if not 0 < value <= _MAX_OUTPUT_BYTES_CAP:
            raise _ManifestError(f"max_output_bytes must be in (0, {_MAX_OUTPUT_BYTES_CAP}]")
        max_output_bytes = value

    require_confirm = False
    if kind == "exec" and "require_confirm" in raw:
        rc = raw["require_confirm"]
        if not isinstance(rc, bool):
            raise _ManifestError("require_confirm must be a boolean")
        require_confirm = rc

    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise _ManifestError("cwd must be a string or absent")

    return ActionSpec(
        name=name,
        kind=kind,
        argv=tuple(argv),
        description=description,
        timeout_s=timeout_s,
        min_interval_s=min_interval_s,
        require_confirm=require_confirm,
        max_output_bytes=max_output_bytes,
        cwd=cwd,
    )


def _as_number(raw: dict, key: str, default: float) -> float:
    """Read a numeric manifest field (TOML int or float; bool is rejected)."""
    value = raw.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _ManifestError(f"{key} must be a number")
    return float(value)


def load_registry(settings) -> ActionRegistry:
    """Best-effort manifest load (spec §0.3).  Never raises.

    ``settings.actions_file`` unset → feature disabled (opt-in, default off).
    A file-level failure (missing / symlink / writable-by-others / wrong
    owner / unparseable TOML / bad top-level shape) → disabled with an ERROR
    log.  Per-action validation failures drop only that action (WARNING).
    Mirrors the access-log best-effort pattern in ``app.py`` so a broken
    manifest can never crash lifespan.
    """
    logger = logging.getLogger("oc_slimapi.actions")
    max_concurrent = settings.actions_max_concurrent
    path = settings.actions_file
    if not path:
        logger.info(
            "actions manifest not configured (OC_SLIMAPI_ACTIONS_FILE unset); "
            "actions disabled"
        )
        return ActionRegistry(enabled=False, actions={}, max_concurrent=max_concurrent)
    try:
        actions = _load_manifest(path, logger)
    except _ManifestError as exc:
        logger.error(
            "actions manifest %r rejected: %s; actions disabled", path, exc
        )
        return ActionRegistry(enabled=False, actions={}, max_concurrent=max_concurrent)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.error(
            "actions manifest %r unreadable or invalid: %s; actions disabled",
            path, exc,
        )
        return ActionRegistry(enabled=False, actions={}, max_concurrent=max_concurrent)
    if not actions:
        logger.warning(
            "actions manifest %r loaded but no valid actions; catalog is empty",
            path,
        )
    return ActionRegistry(enabled=True, actions=actions, max_concurrent=max_concurrent)


# ---------------------------------------------------------------------------
# Registry + executor (spec §3)
# ---------------------------------------------------------------------------


class ActionRegistry:
    """Owns the action catalog, admission, single-flight, rate limiting and
    the subprocess executor.  ``invoke`` raises one of the :mod:`actions`
    exceptions; the routes layer converts them via ``to_coded()``."""

    def __init__(
        self, enabled: bool, actions: dict[str, ActionSpec], max_concurrent: int
    ) -> None:
        self._enabled = enabled
        self._actions: dict[str, ActionSpec] = dict(actions)
        self._max_concurrent = max_concurrent
        # Global spawn concurrency cap (service-level admission).  Created
        # lazily-safe: asyncio primitives bind to a loop on first use, and
        # the registry is always constructed inside a running loop (lifespan
        # or a test).
        self._semaphore = asyncio.Semaphore(max_concurrent) if enabled else None
        # Per-action single-flight marks.  asyncio is single-threaded: the
        # check-and-set below is atomic between awaits.
        self._in_flight: set[str] = set()
        # Per-action min_interval, in-memory only (resets on restart — by
        # design, documented in operations.md).
        self._last_run: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def discover(self) -> list[dict]:
        """``[{"name","kind","description","requireConfirm"}]`` for GET."""
        return [
            {
                "name": spec.name,
                "kind": spec.kind,
                "description": spec.description,
                "requireConfirm": spec.require_confirm,
            }
            for spec in self._actions.values()
        ]

    async def invoke(self, name: str, confirmed: bool) -> ActionResult:
        """Run the named action.  Raises an :class:`ActionError` subclass on
        any failure (never returns a partial ``ActionResult``)."""
        start = time.monotonic()
        if not self._enabled:
            raise ActionsDisabled()
        spec = self._actions.get(name)
        if spec is None:
            raise ActionNotFound(name=name)

        # Single-flight: mark BEFORE the first await so a second coroutine
        # scheduled in the same loop iteration observes the conflict.
        if name in self._in_flight:
            self._audit(spec, start, exit_code=None, ok=False,
                        throttled=True, timeout=False, confirm=confirmed)
            raise ActionThrottled(retry_after=2)
        self._in_flight.add(name)
        try:
            # Confirm gate (exec-only; query is always confirm-free).
            if spec.kind == "exec" and spec.require_confirm and not confirmed:
                self._audit(spec, start, exit_code=None, ok=False,
                            throttled=False, timeout=False, confirm=confirmed)
                raise ActionConfirmRequired()
            # min_interval gate (action-level throttle).
            last = self._last_run.get(name)
            if last is not None:
                remaining = spec.min_interval_s - (time.monotonic() - last)
                if remaining > 0:
                    self._audit(spec, start, exit_code=None, ok=False,
                                throttled=True, timeout=False, confirm=confirmed)
                    raise ActionThrottled(retry_after=math.ceil(remaining))
            # Service-level admission: bounded semaphore wait (transform.py
            # precedent) — a saturated pool yields ActionBusy instead of an
            # unbounded queue.
            if self._semaphore is not None:
                try:
                    await asyncio.wait_for(
                        self._semaphore.acquire(), timeout=_ADMISSION_TIMEOUT_S
                    )
                except TimeoutError:
                    self._audit(spec, start, exit_code=None, ok=False,
                                throttled=True, timeout=False, confirm=confirmed)
                    raise ActionBusy() from None
                except asyncio.CancelledError:
                    # Bug E (rev-13): a client disconnect while parked on the
                    # semaphore used to propagate WITHOUT an audit record (the
                    # audit fired only after admission).  Now a disconnect audit
                    # is recorded too; the abort is still surfaced as
                    # CancelledError upstream.
                    self._audit(spec, start, exit_code=None, ok=False,
                                throttled=False, timeout=False, confirm=confirmed)
                    raise
                try:
                    self._last_run[name] = time.monotonic()
                    result = await self._execute(spec, start, confirmed=confirmed)
                finally:
                    self._semaphore.release()
            else:  # pragma: no cover — unreachable while disabled
                self._last_run[name] = time.monotonic()
                result = await self._execute(spec, start, confirmed=confirmed)
            self._audit(spec, start, exit_code=result.exit_code, ok=result.ok,
                        throttled=False, timeout=False, confirm=confirmed,
                        duration=result.duration_ms)
            return result
        finally:
            self._in_flight.discard(name)

    # -- audit -------------------------------------------------------------

    def _audit(
        self,
        spec: ActionSpec,
        start: float,
        *,
        exit_code: int | None,
        ok: bool,
        throttled: bool,
        timeout: bool,
        confirm: bool,
        duration: int | None = None,
    ) -> None:
        """Structured audit record on the well-known ``oc_slimapi.actions_audit``
        logger, always at WARNING level (independent of ``OC_SLIMAPI_LOG_LEVEL``;
        the handler is configured once in logging_config).  Covers every call
        path including timeout / spawn-fail / disconnect / throttle."""
        _AUDIT_LOGGER.warning(
            json.dumps(
                {
                    "action": spec.name,
                    "kind": spec.kind,
                    "exit_code": exit_code,
                    "ok": ok,
                    "duration_ms": duration if duration is not None else _ms(start),
                    "throttled": throttled,
                    "timeout": timeout,
                    "confirm": confirm,
                },
                sort_keys=True,
            )
        )

    # -- executor ----------------------------------------------------------

    async def _execute(
        self, spec: ActionSpec, start: float, confirmed: bool
    ) -> ActionResult:
        """Spawn the subprocess and run it to completion with unified cleanup.
        ``confirmed`` is the caller's confirm flag — propagated into the
        spawn-fail / timeout / disconnect audit records (Bug B), matching the
        throttle / busy / confirm audits already emitted by ``invoke``.  Raises
        :class:`ActionTimeout` on timeout and re-raises ``CancelledError``
        (client disconnect) — no orphaned process either way.

        **Unified lifecycle (rev-13 / rev-14)**: the whole execution — spawn
        included — is wrapped in a single ``try/finally``; the ``finally``
        unconditionally runs the process-group teardown (:meth:`_cleanup`:
        ``killpg(SIGKILL)`` the process group — the child is a group leader via
        ``start_new_session=True`` — reap, and the failure-path audit).  Every
        exit path is covered: normal exit / timeout / cancellation during the
        semaphore-adjacent execution or drain / cancellation DURING THE SPAWN
        ITSELF (Bug F, rev-14).  The spawn runs as a dedicated task whose body
        await is shielded (:func:`asyncio.shield`), so an outer cancellation
        cannot propagate INTO the spawn task; the ``finally`` then shields it
        to completion, obtains the ``Process`` handle, and cleans the child up
        instead of orphaning it (killpg covers the child's process group).
        The exit wait races against a ``returncode`` poll (:meth:`_wait_exit`)
        — asyncio's ``Process.wait`` resolves only when the pipes also
        disconnect (Bug C root), so a grandchild-held pipe would otherwise
        stall it — and the drain is bounded by a hard deadline
        (:meth:`_drain_with_deadline`).
        """
        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task | None = None
        stderr_task: asyncio.Task | None = None
        outcome: str | None = None  # None=success; "timeout"|"cancelled"
        # Bug F (rev-14): wrap the spawn so a cancellation that lands while
        # `create_subprocess_exec` is still in flight stays recoverable.  The
        # body awaits the spawn through asyncio.shield — a direct
        # `await spawn_task` would let the outer cancellation PROPAGATE into
        # the spawn task (outer Task.cancel() cancels its _fut_waiter, which
        # is the awaited inner task), destroying the recovery path below.
        # Shield breaks that propagation: the outer still receives
        # CancelledError, but spawn_task survives to completion.
        spawn_task = asyncio.ensure_future(self._spawn(spec))
        try:
            try:
                proc = await asyncio.shield(spawn_task)
            except (OSError, ValueError) as exc:
                # Spawn failure: OSError family (ENOENT/EACCES/EMFILE/ENOMEM)
                # or a ValueError from `create_subprocess_exec` (embedded NUL
                # byte in an argv element / invalid cwd) — both map to
                # action_unavailable.
                self._audit(spec, start, exit_code=None, ok=False,
                            throttled=False, timeout=False, confirm=confirmed)
                raise ActionUnavailable() from exc

            # stdout/stderr are drained CONCURRENTLY (asyncio.gather) so a
            # chatty child cannot deadlock on a full second pipe while we read
            # the first.  The stdout drain writes into the shared holder so a
            # deadline force-cancel keeps the partial output (rev-14).
            drain_state = _DrainState()
            stdout_task = asyncio.create_task(
                self._drain_stdout(proc, spec, drain_state)
            )
            stderr_task = asyncio.create_task(
                self._drain_stderr(proc, spec.name)
            )
            try:
                exit_code = await self._wait_exit(proc, spec.timeout_s)
            except TimeoutError:
                outcome = "timeout"
                raise ActionTimeout(timeout_s=spec.timeout_s) from None
            # Process exited: kill the process group NOW.  A grandchild still
            # holding a pipe write end would otherwise keep the drain from ever
            # reaching EOF (Bug C — pre-rev-13 the success-path drain was
            # unbounded).  killpg releases the pipes so the drain hits EOF
            # promptly; _drain_with_deadline is the belt-and-suspenders bound
            # for a grandchild that escaped the group (e.g. via setsid).
            await self._killpg_quiet(proc)
            stdout_data, truncated = await self._drain_with_deadline(
                spec, stdout_task, stderr_task, drain_state
            )
            return self._build_result(
                spec, exit_code, start, stdout_data, truncated
            )
        except asyncio.CancelledError:
            # Client disconnect (during the spawn await, proc.wait(), or the
            # drain): the killpg + reap + audit all happen in the finally
            # below, then the cancellation is re-raised so the caller observes
            # it.
            outcome = "cancelled"
            raise
        finally:
            # Unified teardown on EVERY path (normal / timeout / cancelled /
            # spawn-phase cancelled).  killpg is idempotent: on the normal
            # path the group is already gone (killed just before the drain) →
            # ProcessLookupError.
            cleaned = False
            if proc is not None:
                await self._cleanup(proc, spec, start, confirmed, outcome)
                cleaned = True
            else:
                # No handle was delivered by the spawn await.  Two sub-cases:
                # 1) the cancellation landed mid-spawn and the spawn task is
                #    STILL in flight → shield it to completion and recover the
                #    handle; 2) the spawn task finished concurrently with the
                #    cancellation → retrieve the stranded result directly.
                # Either way the spawned child (and its process group) must be
                # killpg'd + reaped, never orphaned (Bug F).
                if not spawn_task.done():
                    try:
                        recovered = await asyncio.shield(spawn_task)
                    except (Exception, asyncio.CancelledError):
                        recovered = None
                else:
                    try:
                        recovered = (
                            spawn_task.result()
                            if not spawn_task.cancelled() else None
                        )
                    except Exception:
                        recovered = None
                if recovered is not None:
                    await self._cleanup(
                        recovered, spec, start, confirmed, outcome
                    )
                    cleaned = True
            if not cleaned and outcome == "cancelled":
                # Disconnect during spawn with no recoverable handle (the
                # double-cancel narrow race — Bug D, accepted — or a spawn
                # failure racing the cancellation): the disconnect audit must
                # still fire so every spawn attempt is accounted for (Bug F).
                self._audit(spec, start, exit_code=None, ok=False,
                            throttled=False, timeout=False, confirm=confirmed)
            if stdout_task is not None or stderr_task is not None:
                await self._cancel_quietly(stdout_task, stderr_task)

    def _build_result(
        self,
        spec: ActionSpec,
        exit_code: int,
        start: float,
        stdout_data: bytes,
        truncated: bool,
    ) -> ActionResult:
        duration_ms = _ms(start)
        if spec.kind == "exec":
            return ActionResult(
                kind="exec",
                ok=exit_code == 0,
                exit_code=exit_code,
                duration_ms=duration_ms,
                markdown=None,
                truncated=False,
                # Fixed short string, never stdout (already discarded).
                message=None if exit_code == 0 else "non-zero exit",
            )
        # query: non-zero exit → empty markdown; ok purely from exit_code.
        return ActionResult(
            kind="query",
            ok=exit_code == 0,
            exit_code=exit_code,
            duration_ms=duration_ms,
            markdown=(
                "" if exit_code != 0
                else stdout_data.decode("utf-8", errors="replace")
            ),
            truncated=truncated and exit_code == 0,
            message=None,
        )

    @staticmethod
    async def _spawn(spec: ActionSpec) -> asyncio.subprocess.Process:
        """Spawn the action subprocess.

        Wrapped as a coroutine so :meth:`_execute` can run it in a dedicated
        task and recover the ``Process`` handle after a spawn-phase
        cancellation (Bug F, rev-14) instead of orphaning the child.
        """
        return await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=spec.cwd,                 # None → inherit sidecar cwd
            start_new_session=True,       # own process group → killpg works
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_action_env(),      # P2-2: fail-closed allowlist; drops OC_SLIMAPI_*
        )

    @staticmethod
    async def _cancel_quietly(*tasks: asyncio.Task) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _drain_stdout(
        proc: asyncio.subprocess.Process,
        spec: ActionSpec,
        state: _DrainState,
    ) -> None:
        """Read stdout in 4 KiB chunks into the shared ``state`` holder.

        query: accumulate up to the cap then keep draining-and-discarding to
        EOF (stopping would fill the pipe buffer and block the child → fake
        timeout); the final decode uses ``errors="replace"`` so binary output
        cannot crash the response.  exec: drain and discard everything.
        Returns nothing meaningful — the caller reads ``state``, which survives
        a deadline force-cancel (rev-14: task-local ``kept`` was destroyed
        with the task, so the partial output was lost)."""
        try:
            if spec.kind == "exec":
                while True:
                    chunk = await proc.stdout.read(_READ_CHUNK)
                    if not chunk:
                        break
                return
            cap = (
                spec.max_output_bytes
                if spec.max_output_bytes is not None
                else _DEFAULT_MAX_OUTPUT_BYTES
            )
            while True:
                chunk = await proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                if state.truncated:
                    continue
                room = cap - len(state.kept)
                if room >= len(chunk):
                    state.kept += chunk
                else:
                    state.kept += chunk[:room]
                    state.truncated = True
        except Exception:
            # A pipe error must never break the action result path.
            return

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process, name: str) -> None:
        """Drain stderr (concurrently with stdout), keep up to 64 KiB **bytes**
        and log them to journald; never raises.

        The cap is byte-based (``_STDERR_LOG_CAP``) — a chunk-count cap would
        let 65536 × 4 KiB chunks accumulate ~256 MiB in memory before the
        claimed 64 KiB journald cap ever engaged (rev-13).  Everything past the
        cap is drained-and-discarded to EOF: stopping early would fill the pipe
        buffer and block the child → fake timeout.
        """
        kept = bytearray()
        total = 0
        try:
            while True:
                chunk = await proc.stderr.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total <= _STDERR_LOG_CAP:
                    kept += chunk
        except Exception:
            return
        if total:
            _APP_LOGGER.warning(
                "action %s stderr (%d bytes%s): %s",
                name,
                total,
                " truncated" if total > _STDERR_LOG_CAP else "",
                kept.decode("utf-8", errors="replace"),
            )

    @staticmethod
    async def _drain_with_deadline(
        spec: ActionSpec,
        stdout_task: asyncio.Task,
        stderr_task: asyncio.Task,
        state: _DrainState,
    ) -> tuple[bytes, bool]:
        """Drain stdout/stderr to EOF under a hard deadline (Bug C, rev-13).

        The process group was already killpg'd by the caller, so the pipes
        normally reach EOF immediately.  The ``_DRAIN_DEADLINE_S`` bound is the
        belt-and-suspenders for a grandchild that escaped the process group
        (e.g. via ``setsid``) while still holding a pipe write end: on expiry
        both drain tasks are force-cancelled, a warning is logged, and the
        partial stdout received so far is returned — read from the shared
        :class:`_DrainState` holder, which survives the task cancellation
        (rev-14; previously the task-local ``kept`` bytearray was destroyed
        with the task and query returned an empty markdown instead of the
        partial output).  Query results are marked ``truncated`` — never
        faking a timeout on the success path."""
        try:
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                timeout=_DRAIN_DEADLINE_S,
            )
        except asyncio.TimeoutError:
            _APP_LOGGER.warning(
                "action %s: drain exceeded %ss deadline; force-cancelling "
                "drain tasks and using partial output",
                spec.name, _DRAIN_DEADLINE_S,
            )
            await ActionRegistry._cancel_quietly(stdout_task, stderr_task)
            return bytes(state.kept), True
        return bytes(state.kept), state.truncated

    @staticmethod
    async def _wait_exit(proc: asyncio.subprocess.Process, timeout_s: float) -> int:
        """Wait until the child process has exited; return its exit code.

        Polls ``proc.returncode`` instead of awaiting :meth:`Process.wait` —
        Bug C root cause (rev-13): asyncio's ``wait()`` resolves only when the
        stdout/stderr pipes ALSO disconnect (``_try_finish`` in
        ``base_subprocess.py``), so a grandchild holding a pipe write end open
        would stall it forever even though the child itself exited.  The
        transport caches ``returncode`` at SIGCHLD — independent of the pipes —
        so the poll is a reliable exit signal in both the normal and the
        grandchild-held-pipe cases.  Raises :class:`TimeoutError` if the child
        is still running after ``timeout_s``.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            rc = proc.returncode
            if rc is not None:
                return rc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.sleep(min(0.05, remaining))

    @staticmethod
    async def _killpg_quiet(proc: asyncio.subprocess.Process) -> None:
        """killpg(SIGKILL) the child's process group; never raises (a
        :class:`ProcessLookupError` means the group is already gone)."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    async def _cleanup(
        self,
        proc: asyncio.subprocess.Process,
        spec: ActionSpec,
        start: float,
        confirmed: bool,
        outcome: str | None,
    ) -> None:
        """Unified lifecycle teardown — called from the ``finally`` of every
        ``_execute`` path (normal exit / timeout / cancellation).

        Unconditionally ``killpg(SIGKILL)`` the process group — the child was
        spawned with ``start_new_session=True`` so pgid == child pid, covering
        the child itself AND any grandchildren (Bug A: never skipped on early
        child exit).  Then reap the child (``Process.wait`` returns immediately
        once the transport has cached the return code).

        The failure-path audit (timeout / disconnect) also lives here so no
        exit path can skip it; the success audit is emitted by ``invoke`` where
        the full result (incl. ``duration_ms``) is available.  Never raises —
        a second cancellation during the final reap leaves at most a brief
        zombie that the loop's transport cleanup reaps (Bug D, accepted)."""
        await self._killpg_quiet(proc)
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_CLEANUP_REAP_S)
            except (ProcessLookupError, asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if outcome is not None:
            self._audit(
                spec, start, exit_code=None, ok=False,
                throttled=False, timeout=(outcome == "timeout"),
                confirm=confirmed,
            )
