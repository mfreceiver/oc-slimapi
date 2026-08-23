# State / sequence lane

## Scope

- Audited lifecycle state machines, cancellation ordering, grace removal, zombie-hub revival, and shutdown sequencing.
- Repository baseline: `HEAD e8873ad` (`release: v4.13.0`), implementation commit `6ad12ef`.
- Production code was not modified. Targeted probes were written only under `/tmp/opencode/`.
- The parent orchestrator owns full validation; this lane did not rerun `./scripts/check.sh`.

## Test-landscape observations

- `/home/mar/personal_projects/oc-slimapi/tests/test_zombie_hub_revival.py` has deterministic event-gated coverage for full/partial group death, stale and single revival waiters, close barriers, consumer departure during revival, exception-driven revival, stop-task disarming, and real event/token revival.
- `/home/mar/personal_projects/oc-slimapi/tests/test_registry_grace_removal.py` covers teardown exceptions, stale removal-task identity, and normal cleanup.
- `/home/mar/personal_projects/oc-slimapi/tests/test_lifespan.py`, `/home/mar/personal_projects/oc-slimapi/tests/test_token_hub_lifecycle.py`, and `/home/mar/personal_projects/oc-slimapi/tests/test_token_stream_route.py` cover shutdown order, token-flush cancellation, attach rollback, grace symmetry, and unsubscribe idempotence.
- `/home/mar/personal_projects/oc-slimapi/tests/test_zombie_hub_revival.py:353-409` deliberately models a task paused in cancellation cleanup. Its comment at lines 396-399 says grace removal's second cancellation “kills the gated unwind”; the test checks final registry/revival state, but does not require the task's awaited cleanup to finish.
- No existing test was found that composes an in-progress `_remove_hub_after_grace()` gather with `HubRegistry.close()` and asserts completion of `GlobalHub.run()`'s stream `__aexit__`.

## Git-history signals

- `8654d5b fix: harden lifecycle teardown and test determinism` made application shutdown await token-flush task exit before hub close.
- `8fa99a4`, `e3b9443`, and `5591aef` iteratively hardened zombie `GlobalHub` revival and cancellation behavior.
- `f75e0e7` introduced the current serial grace-removal cancellation/gather logic; `f7e65dd7` introduced the base registry close; `8fa99a4` added close-barrier/revival-task handling.
- `git blame -L 334,350 -- src/oc_slimapi/sse/registry.py` attributes grace task cancellation/gather to `f75e0e71`.
- `git blame -L 479,504 -- src/oc_slimapi/sse/registry.py` attributes the base close cancellation/gather to `f7e65dd7`, with the revival close barrier from `8fa99a4a`.
- The adjacent helper `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/global_hub.py:371-393` explicitly avoids cancelling an already-cancelling task because a second `CancelledError` can truncate awaited cleanup (including httpx teardown). The registry teardown paths do not preserve that rule.

## Candidate SS-1 — grace/shutdown re-cancellation truncates stream cleanup

**Hypothesis:** `_remove_hub_after_grace()` and `HubRegistry.close()` can re-cancel `GlobalHub` tasks already unwinding from cancellation. Repeated cancellation interrupts `GlobalHub.run()`'s awaited stream-context cleanup, while registry state is nevertheless dropped as if INV-2 (“full task exit and `/global/event` connection release before dropping the reference”) had completed.

**Input/State:** Two deterministic states were exercised:

1. Runtime grace state: `GlobalHub.run()` is inside `client.stream(...)`; a group-supervisor-style first cancellation has entered a gated `__aexit__`; all consumers have left; grace removal fires.
2. Shutdown overlap: grace removal has already cancelled and is gathering the hub task while its `__aexit__` is gated; `HubRegistry.close()` begins.

**Execution Path:**

- `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/global_hub.py:1375-1472` — `GlobalHub.run()` owns the upstream stream via `async with self.client.stream(...)`.
- `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/registry.py:334-350` — grace removal selects all not-done tasks, calls `task.cancel()` without checking `task.cancelling()`, gathers, then drops the hub.
- `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/registry.py:479-506` — close collects the same children plus `_removal_task`, cancels all of them, gathers, then clears state. Cancelling the removal task also propagates through its `gather()` to the child, producing an additional cancellation.

**Existing Coverage:** Existing tests prove end-state coherence and no zombie revival. They do not assert cleanup completion under repeated cancellation. The explicit “kills the gated unwind” comment in `/home/mar/personal_projects/oc-slimapi/tests/test_zombie_hub_revival.py:396-399` confirms that this behavior is currently exercised but not treated as a cleanup failure.

**Experiment:** Created `/tmp/opencode/state_sequence_probe.py` with an event-gated async context manager and a fake client used by the real `GlobalHub.run()` implementation. Exact command:

```bash
.venv/bin/python "/tmp/opencode/state_sequence_probe.py"
```

The probe ran four scenarios 30 times each:

- synthetic grace/close overlap;
- sole-canceller control;
- real `GlobalHub.run()` grace/close overlap;
- real `GlobalHub.run()` already cancelling when grace removal fires.

**Expected:** A cancellation-safe teardown should retain one effective cancellation while awaited cleanup runs, should not drop the hub until the stream exit completes, and should make `close()` wait for that completion. The sole-canceller control should establish that the gate itself is valid.

**Actual:**

- Sole-canceller control: cancellation count stayed `1`; `close()` remained pending before gate release in `30/30`; cleanup completed after release in `30/30`.
- Synthetic grace/close overlap: cancellation count rose from `1` to `3`; cleanup completed in `0/30`; child and removal tasks nevertheless reported done in `30/30`.
- Real `GlobalHub.run()` grace/close overlap: cancellation count rose from `1` to `3`; stream `__aexit__` completion was `false` in `30/30`; run/removal tasks nevertheless reported done.
- Real runtime grace re-cancellation without close: cancellation count rose from `1` to `2`; stream `__aexit__` completion was `false` in `30/30`; `_global` was dropped in `30/30`.

Representative exact probe values:

```json
{
  "control_cancel_counts_before_release": [1],
  "control_cleanup_done_after_release": 30,
  "control_close_waited_for_cleanup": true,
  "overlap_cancel_counts": [3],
  "overlap_cleanup_done": 0,
  "real_run_cancel_counts": [3],
  "real_run_stream_exit_done": 0,
  "removal_recancel_cancel_counts": [2],
  "removal_recancel_stream_exit_done": 0,
  "removal_recancel_dropped_hub": 30
}
```

**Repeatability:** All four scenarios produced identical outcomes in `30/30` runs. No timing sleeps were used to decide the ordering; events gated every transition.

**Alternative Explanation:** During process shutdown, the later whole-client `aclose()` may reclaim pooled resources, reducing shutdown-only impact. It cannot restore the stated per-stream release-before-reference-drop ordering. More importantly, the 30/30 grace-only experiment demonstrates the same defect without `close()`: if a group task is already cancellation-unwinding when the last consumer's grace expires, removal interrupts cleanup and drops the hub. The default 30-second grace makes this uncommon, but slow or stuck network teardown is exactly the state in which forced re-cancellation is consequential.

**Verdict: CONFIRMED**

Suggested severity: **P2** (runtime lifecycle invariant violation, deterministic under the required state; likely low frequency). A fix must address both direct child re-cancellation and cancellation propagated by cancelling a parent `gather()`; merely filtering direct `task.cancel()` calls by `task.cancelling()` is insufficient for the close/removal overlap.

## Candidate SS-2 — close permits a pending revival to resurrect the hub

**Hypothesis:** A revival waiter pending during registry shutdown can pass its final checks and spawn a fresh upstream group after `close()`.

**Input/State:** A full hub group is held in event-gated cancellation cleanup; `ensure_upstream()` has armed one revival waiter; registry close begins before cleanup gates release.

**Execution Path:** `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/global_hub.py:395-426` checks `_closing` after gathering the old group; `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/registry.py:479-506` sets `_closing` before cancelling/gathering tasks.

**Existing Coverage:** `/home/mar/personal_projects/oc-slimapi/tests/test_zombie_hub_revival.py:287-347` contains deterministic `TestCloseBarrier.test_close_with_pending_revival_never_revives` coverage and asserts that no new run group is created.

**Experiment:** Exact repetition command:

```bash
passes=0; for i in $(seq 1 10); do if .venv/bin/pytest -q 'tests/test_zombie_hub_revival.py::TestCloseBarrier::test_close_with_pending_revival_never_revives' > "/tmp/opencode/close-barrier-$i.log"; then passes=$((passes+1)); else break; fi; done; printf 'passes=%s/10\n' "$passes"
```

**Expected:** If the hypothesis is true, at least one run should show a fresh spawn or fail the close-barrier assertions.

**Actual:** `passes=10/10`; no post-close revival was observed.

**Repeatability:** Passed `10/10` isolated runs, in addition to passing in the combined targeted suite below.

**Alternative Explanation:** A narrower stale-identity interleaving might evade this single test, but the implementation has two independent barriers: registry close sets `_closing` before cancellation, and the revival waiter checks `_closing` after its gather.

**Verdict: REJECTED**

## Interesting passing scenarios

Exact targeted suite command:

```bash
.venv/bin/pytest -q tests/test_registry_grace_removal.py tests/test_zombie_hub_revival.py tests/test_lifespan.py
```

Result: `28 passed, 244 warnings in 1.64s`. The warnings were Python 3.14 `pytest_asyncio` event-loop-policy deprecations, not lifecycle failures.

- Single-canceller close correctly waits for async child cleanup (`30/30` probe controls).
- Pending revival is blocked by registry close (`10/10` isolated repetitions).
- Existing tests passed for stale revival waiters, stale grace-task identity, teardown exceptions, full/partial group death, consumer departure, and app cleanup order.

## Remaining risks / follow-up

- The probe used the production `GlobalHub.run()` logic with a deterministic fake stream context rather than a live httpx socket. A regression test should preserve this event-gated determinism; a live socket test would be supplementary, not a substitute.
- The same unguarded registry loop handles `task`, `flush_task`, `heartbeat_task`, `stop_task`, and `_revive_task`; only `GlobalHub.run()`'s stream cleanup was directly instrumented.
- Remediation needs an explicit cancellation-ownership design for three states: removal sleeping, removal gathering, and close taking ownership. Cancelling an in-progress removal `gather()` can re-cancel children even when direct child cancellation is filtered.
- Full repository validation was intentionally not run; the parent orchestrator owns it.

## Changed files

- Added `/home/mar/personal_projects/oc-slimapi/docs/automatic/.work/state-sequence.md`.
- No production or test files changed.
- Temporary evidence only: `/tmp/opencode/state_sequence_probe.py` and `/tmp/opencode/close-barrier-*.log`.
