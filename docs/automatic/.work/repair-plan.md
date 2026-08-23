# Repair Plan — BUG-001..005 → Batches R1–R4

> Derived from `docs/automatic/20260824-0714_bug-hunting.md` §Repair Plan. Owner-approved for execution 2026-08-24.
> Batching: all four write domains are disjoint → 4 parallel lanes (R1/R2 = fixer-glm, R3/R4 = fixer-clm).

## Shared rules (every lane MUST follow)

1. **Write scope**: ONLY the files listed in your batch. Do NOT touch `CHANGELOG.md`, `docs/`, other `src/` or `tests/` files (orchestrator owns CHANGELOG + final gate).
2. **No git mutations**: no commit/stash/checkout/branch. Read-only git (log/show/diff) allowed.
3. **No full gate**: do NOT run `./scripts/check.sh` (orchestrator runs it once after all lanes land). Run ONLY your targeted pytest command.
4. **TDD order**: (a) write the new regression test → (b) run it against CURRENT code, confirm it FAILS (proves it catches the bug) → (c) apply the src fix → (d) confirm new test PASSES + targeted existing tests PASS.
5. Runtime: Python 3.14 (CPython int-str limit 4300 digits), venv at `.venv/`.
6. Report back: files changed, test fail-first evidence (1-3 lines), final test output summary.
7. Keep diffs minimal — no drive-by refactors, no style churn.

## Batch R1 — BUG-001: registry cancel guards (lane: fixer-glm)

- **Evidence**: report §BUG-001; `docs/automatic/.work/state-sequence.md` (SS-1); orchestrator probe `/tmp/opencode/ss1_final.py` (3-scenario reference incl. stub-hub shape).
- **Write scope**: `src/oc_slimapi/sse/registry.py`, NEW `tests/test_registry_close_overlap.py`.
- **Invariant to enforce**: once a task has begun unwinding (`task.cancelling() > 0`), no second `CancelledError` may be delivered to it — neither by direct `task.cancel()` nor by cancelling a parent `gather()` that owns it. Cleanup (upstream stream `__aexit__`) must be able to complete. `close()` must not block ~30 s waiting out an armed grace timer.
- **Changes**:
  - `_remove_hub_after_grace` (~`:334-350`): guard every child-task cancel with `if task is not None and not task.done() and task.cancelling() == 0: task.cancel()` (mirror the deliberate pattern in `src/oc_slimapi/sse/global_hub.py:371-393`).
  - `close()` (~`:479-506`): same guard for hub child tasks. For `self._removal_task`: do not blind-cancel it while its gather owns unwinding children — if children are mid-unwind, await the removal task (its own guarded pass completes promptly); if it is merely sleeping in grace and children are NOT unwinding, cancelling is safe. Awaiting must not introduce a 30 s stall — the removal task after its guarded cancel pass reaches its gather promptly.
  - Gathers that can contain unwinding children: `return_exceptions=True`; do not re-raise into them.
- **New regression test spec** (`tests/test_registry_close_overlap.py`, pytest-asyncio, event-gated like `/tmp/opencode/ss1_final.py`):
  1. *overlap*: stub hub parked in stream-exit cleanup, `GRACE_SECONDS=0` armed, `registry.close()` → assert `cleanup_completed is True` and `cancel_count == 1`.
  2. *duplicate-cancel*: second `hub.task.cancel()` injected while unwinding → cleanup still completes.
  3. *control*: grace alone → cleanup completes, `_global` dropped.
- **Targeted tests**: `.venv/bin/python -m pytest tests/test_registry_close_overlap.py tests/test_zombie_hub_revival.py -q` (plus any `tests/test_hub*registry*`/`tests/test_registry*` found via glob).

## Batch R2 — BUG-002: SSE admission rollback on response-start failure (lane: fixer-glm)

- **Evidence**: report §BUG-002; `docs/automatic/.work/failure-injection.md` (FI-001) + `docs/automatic/.work/reproduction.md`; probe `/tmp/opencode/repro_fi001_independent.py` (24/24 one-shot leak, cap-exhaustion batches).
- **Write scope**: `src/oc_slimapi/routes/events.py`, `src/oc_slimapi/routes/token_stream.py`, NEW `tests/test_sse_send_start_rollback.py`.
- **Problem**: routes admit subscriber (events `:88-105`, token `:102-120`) then return `StreamingResponse`; cleanup lives only in the body generator `finally` (events `:193-200`, token `:209-213`). If ASGI `send({"type": "http.response.start"})` raises, the generator never starts → slot (and token `_flush_task`) leaks.
- **Two candidate fixes — pick whichever keeps ALL existing tests green**:
  - **Option A (preferred if compatible)**: move the subscribe call into the generator prologue (first lines before first yield), so admission cannot precede response-start. Verify `slimapi.meta` first-frame ordering and all existing route tests (some may assert admission immediately after route call without iterating — if so, Option A breaks them → use B).
  - **Option B**: minimal `StreamingResponse` subclass whose `__call__`/`stream_response` wraps the send calls in try/except; on exception pre-first-body-chunk, invoke the same detach routine the generator `finally` uses, then re-raise.
  - Either way: token route must also tear down `_flush_task` on this path.
- **New regression test spec**: ASGI harness calling the route app with a `send` that raises `RuntimeError("INJECTED_SEND_START_FAILURE")` on `http.response.start`; after exception propagates, assert event hub subscriber count == baseline 0 AND token hub subscriber count == 0 AND token flush task absent/not-live. Include one control case with normal send (200, cleanup intact after generator close).
- **Targeted tests**: `.venv/bin/python -m pytest tests/test_sse_send_start_rollback.py tests/test_token_stream_route.py -q` + events-route test file(s) found via glob `tests/test_*event*`.

## Batch R3 — BUG-003: ReplayLog seq burn on size_of failure (lane: fixer-clm)

- **Evidence**: report §BUG-003; `docs/automatic/.work/failure-injection.md` (FI-002) + `reproduction.md`; probe `/tmp/opencode/repro_fi002_independent.py`.
- **Write scope**: `src/oc_slimapi/sse/replay_log.py`, NEW `tests/test_replay_append_size_failure.py`.
- **Change** (in `append()`, ~`:404-483`): compute `size = self._size_of(payload)` BEFORE `self._order += 1` / `state.last_seq = seq` mutation; on `size_of` exception nothing has mutated → propagate (caller already handles). Keep existing rollback path for post-commit failures unchanged.
- **New regression test spec**: `FailOnceSizer` (custom `size_of` raising `OSError` on 2nd call, mirroring `/tmp/opencode/repro_fi002_independent.py`): append#1 ok → append#2 raises → assert `last_seq`/`_order` unchanged (rollback-equivalent, no burn) → append#3 succeeds REUSING seq 2 → replay from cursor 0 returns frames 1,2 with NO `ReplayResync`. Reference existing rollback test pattern `tests/test_token_seq.py:223-263`.
- **Targeted tests**: `.venv/bin/python -m pytest tests/test_replay_append_size_failure.py tests/test_token_seq.py -q` (+ glob `tests/test_replay*`).

## Batch R4 — BUG-004/005: guarded decimal parse (lane: fixer-clm)

- **Evidence**: report §BUG-004/005; `docs/automatic/.work/boundary.md` (BND-001/002) + `boundary-reproduction.md`; probe `/tmp/opencode/boundary_reproduction_probe.py`.
- **Write scope**: `src/oc_slimapi/selector.py`, `src/oc_slimapi/sse/replay_wire.py`, NEW `tests/test_digit_boundary_guard.py`.
- **Change**: at each unguarded `int(<decimal-string>)` site:
  - `selector.py:547-567` `int(values[0])`: pre-guard `len(digits) > 19` → route to existing invalid-version path (coded 400 `unsupported_version`); also wrap `int()` in `try/except ValueError` → same invalid path. (grep for any other `int(` on selector values.)
  - `replay_wire.py:114-154` `parse_last_event_id` `int(seq_text)`: same length guard (≤19) + try/except → treat as invalid cursor → existing reset/default-cursor behavior per contract (`docs/specs/v4-contract.md:261,269-270` — ignore/reset, never request failure). Also covers 4301-digit all-zero/leading-zero forms.
  - Local guards in each module (no new shared module, no cross-imports).
- **New regression test spec**: parametrized digit lengths `{4299, 4300, 4301, 5000}` × forms `{plain, all-zeros, leading-zero}`:
  - `?v=` → ALL coded 400 `unsupported_version` (no 500, no exception).
  - `Last-Event-ID` global `g:<epoch>:<seq>` + token `t:<sid>:<epoch>:<seq>` → default-cursor reset behavior (no 500).
- **Targeted tests**: `.venv/bin/python -m pytest tests/test_digit_boundary_guard.py tests/test_selector.py tests/test_sse_replay_wire.py -q`.

## Post-merge (orchestrator-owned, NOT lane work)

1. CHANGELOG.md `Fixed` ×4 entries.
2. Full `./scripts/check.sh`.
3. Commit; minor release via `scripts/release.sh` only after owner confirms.
