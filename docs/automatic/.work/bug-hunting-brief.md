# Deep Bug Hunting & Test Gap Exploration — Shared Brief

## Mission

Run an unattended, evidence-driven audit of the current `oc-slimapi` repository state after release `v4.13.0` (`6ad12ef` implementation, `e8873ad` release). Maximum elapsed budget: 3 hours. The shared workspace is the repository root. Git history is available.

This is not a normal code review. The only core objective is to find states or inputs that existing tests do not actually prove safe, then use targeted experiments to confirm or reject real bugs. A final result with zero confirmed bugs is valid.

Do not prioritize style, naming, formatting, generic refactoring, or subjective architecture preferences.

## Repository Rules

- Read `/home/mar/personal_projects/oc-slimapi/AGENTS.md` before work.
- Current wire authority: `/home/mar/personal_projects/oc-slimapi/docs/specs/v4-contract.md`.
- Never write to upstream opencode SQLite business data.
- Production code must remain unchanged by this audit.
- Temporary targeted tests/probes are allowed only to establish evidence. Put them under `/tmp/opencode/` or your assigned lane file area, and remove repository-local temporary probes before finishing.
- Do not commit, release, deploy, or modify tags.
- Do not edit `/home/mar/personal_projects/oc-slimapi/docs/specs/v3-contract.md`.
- Do not write the final report unless your lane explicitly owns it.
- Each exploration lane writes only its assigned output file under `/home/mar/personal_projects/oc-slimapi/docs/automatic/.work/`.

## Baseline

- Repository was clean at launch.
- HEAD: `e8873ad release: v4.13.0`.
- Prior notification reports full release gate: `3888 passed`, `56 routes / 7 semantic checks`, `compileall` passed.
- These baseline results are context, not a substitute for targeted experiments.

## Required Investigation Method

### Start from tests

First establish what the current suite truly proves. Inspect whether assertions reach the named behavior, mocks remove the critical behavior, fixtures are too idealized, target branches are unreachable, snapshots hide defects, integration boundaries are mocked away, and race/error/retry/cancellation states are absent.

### Use Git history

Search commits and diffs for bugfix, revert, hotfix, flaky, repeated corrections, edge cases, retry, duplicate, race, null, crash, rollback, cancellation, teardown, and determinism signals. Explore adjacent state space, but never re-report a historical issue that is already fixed.

### Targeted experiment standard

Experiments must be minimal, deterministic where feasible, repeatable, isolated, and centered on one expected/actual behavior. Large accidental integration failures are not `CONFIRMED`; minimize them first.

For every candidate use exactly:

- Hypothesis
- Input/State
- Execution Path
- Existing Coverage
- Experiment
- Expected
- Actual
- Repeatability
- Alternative Explanation
- Verdict: `CONFIRMED`, `REJECTED`, or `INCONCLUSIVE`

Only `CONFIRMED` candidates may enter Verified Bugs.

### Priority

Prioritize data corruption, user-visible wrong results, crashes, invalid state, duplicate side effects, retry/cancellation, races, recovery failures, and high-value edge cases.

### Passing investigations matter

Record high-risk scenarios that pass under `Interesting Tests That Passed`, including the invariant established and the exact targeted command/probe. This prevents duplicate future investigation.

## Lane Assignments

### Boundary lane

Explore null, empty, zero, one, maximum, negative, duplicate, very long, Unicode, invalid encoding, malformed input, timezone, DST, precision, ordering, and unusual-but-legal values. Own only:

`/home/mar/personal_projects/oc-slimapi/docs/automatic/.work/boundary.md`

### State/sequence lane

Explore multi-step sequences and invariants: A→A, A→B, failure→retry, cancel, timeout, back, create→delete→recreate, start→pause→resume, concurrent operations, teardown/restart, and duplicate delivery. Own only:

`/home/mar/personal_projects/oc-slimapi/docs/automatic/.work/state-sequence.md`

### Failure-injection lane

Safely explore network timeout, I/O error, DB error, partial response, stale cache, dependency unavailable, interrupted operation, cancellation, retries, and duplicate delivery. Own only:

`/home/mar/personal_projects/oc-slimapi/docs/automatic/.work/failure-injection.md`

### Reproduction / false-positive killer lane

This lane starts after initial candidates exist. It must independently rerun candidates, minimize reproductions, check determinism, expected behavior, contract/documentation, callers, framework guarantees, and test-harness artifacts. It owns only:

`/home/mar/personal_projects/oc-slimapi/docs/automatic/.work/reproduction.md`

## Final Deliverables (owned by orchestrator/final synthesis lane)

Create:

`/home/mar/personal_projects/oc-slimapi/docs/automatic/YYYYMMDD-HHmm_bug-hunting.md`

Required report structure:

1. Deep Bug Hunting Report
2. Executive Summary
3. Test Landscape
4. Git Bug-History Signals
5. State Space Explored
6. Verified Bugs (`BUG-001`, `BUG-002`, ...)
7. Interesting Tests That Passed
8. Rejected Hypotheses
9. Inconclusive Investigations
10. Remaining High-Risk State Space
11. Recommended Test Investments
12. Workspace Integrity

Each verified bug must include Severity, Confidence, Source Location, Missing Test Scenario, Trigger, Execution Path, Minimal Reproduction, Commands Executed, Expected, Actual, Repeatability, Root Cause, Impact, Relevant Git Context, Recommended Regression Test, Recommended Fix Direction, and Independent Reproduction.

Recommendations must be concrete: module + state transition + failure mode. Never merely say “increase coverage.”

## Additional Required Repair Plan

After all exploration and false-positive killing, produce a complete repair plan that a low-capability implementation model can execute and verify mechanically. The plan must:

- enumerate every confirmed bug and any prerequisite;
- state the recommended fix option and why it is preferred over alternatives;
- name exact files, symbols, and ordered edit steps;
- specify exact regression tests, fixtures, mocks/fault injection, expected assertions, and commands;
- include cleanup and rollback notes;
- include acceptance criteria and a final verification matrix;
- avoid applying production fixes during this audit.

The report must clearly label the orchestrator’s recommended repair option for user review.

## Completion Contract for Exploration Lanes

Return a concise summary and ensure your assigned lane file contains:

- test landscape observations;
- Git history signals;
- experiments with exact commands and counts;
- all candidates in verdict format;
- interesting passing scenarios;
- remaining high-risk areas;
- repository files changed by your lane (must be only the assigned lane file; temporary external files may be listed separately).
