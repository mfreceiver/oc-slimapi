# Failure-injection lane

Audit target: post-`v4.13.0` tree (`6ad12ef`, release `e8873ad`).  Scope was
limited to deterministic fault injection and existing failure-path tests.  No
production or test source was changed.  Temporary probes were created only
under `/tmp/opencode/`.

## Executive verdict

Two defects were confirmed:

1. **SSE response-start failure leaks admitted subscribers** on both
   `/slimapi/events` and `/slimapi/sessions/{sid}/stream`. Eight consecutive
   failed handshakes exhaust each default admission ledger; the token variant
   also leaves its flush task running.
2. **`ReplayLog.append()` can make sequence rollback impossible after an
   internal append failure.** A failure in the supported `size_of` hook occurs
   after `last_seq` is advanced, burns a phantom sequence, and changes reconnect
   classification to `replay_expired`.

The first defect is directly transport-reachable and operationally significant.
The second is a real transaction/invariant defect with low default production
reachability because ordinary production payloads are bytes and the default
sizer is `len(payload)`.

## Test landscape

- Broad failure-keyword inventory command:
  `rg -l 'ConnectError|ReadError|TimeoutException|OSError|disk full|injected|locked|cancel' tests | wc -l`
  returned **77 files**; the corresponding `rg -n ... | wc -l` returned
  **526 matches**. This is a lexical inventory, not a claim that all matches
  are distinct fault tests.
- A narrower SSE/replay/retry scan covered **13 files** matching disconnect,
  append-failure, rollback, reconnect, upstream-loss, or retry terms.
- Existing tests are strongest for upstream connect/mid-read failure mapping,
  DB auxiliary open/schema recovery, cache epoch fencing, snapshot/log I/O,
  cancellation after an SSE generator has started, retry epoch guards, and
  lifecycle rollback.
- The important uncovered boundaries found in this lane were (a) failure after
  route admission but before first body iteration, and (b) an exception from
  inside `ReplayLog.append()` after partial state mutation. Existing tests mock
  failure before either partial mutation occurs.

## Candidate 1 — response-start failure leaks SSE admission

- **Hypothesis:** If ASGI transport fails while sending `http.response.start`,
  the route has already admitted a subscriber but its body generator has not
  started; therefore the generator-owned `finally` cannot unsubscribe it.
- **Input/State:** Real FastAPI routes with real `ReplayLog`, `HubRegistry`,
  `TokenStreamHub`, and `TokenStreamRegistry`; default caps
  `max_subscribers_per_directory=8` and
  `token_stream_max_subscribers=8`. A custom ASGI `send()` raises exact
  `RuntimeError("INJECTED_SEND_START_FAILURE")` on `http.response.start`.
- **Execution Path:**
  - `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/events.py:88-105`
    subscribes before returning the response; cleanup is only in the generator
    `finally` at `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/events.py:193-200`.
  - `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/token_stream.py:102-120`
    subscribes before returning the response; cleanup is only at
    `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/token_stream.py:209-213`.
  - Starlette attempts `http.response.start` before iterating either body.
- **Existing Coverage:**
  `/home/mar/personal_projects/oc-slimapi/tests/test_token_stream_route.py:996-1009`
  and `/home/mar/personal_projects/oc-slimapi/tests/test_hub.py:374-409`
  start the iterator and then cancel/close it. They correctly exercise cleanup
  after generator entry, not failure before generator entry.
- **Experiment:** `/tmp/opencode/failure_probe_sse_start_send.py` drove each
  route directly through ASGI with the injected send failure. It was run twice
  as a single-failure probe, then once with ten consecutive failures per route
  against one app instance.
- **Expected:** After every failed handshake, control/token subscriber ledgers
  return to zero, subscriber sets are empty, and the token flush task stops.
- **Actual:** Both independent single-failure runs were identical:
  - events: `control_total=1`, `control_set=1`;
  - token stream: `token_total=1`, `token_subscriber_count=1`,
    `token_flush_live=True`.

  The ten-failure run produced ten exact
  `RuntimeError: INJECTED_SEND_START_FAILURE` outcomes per route, then:
  - events: `control_total=8`, `control_rejected=2`, `control_set=8`;
  - token stream: `token_total=8`, `token_rejected=2`,
    `token_subscriber_count=8`.
- **Repeatability:** Reproduced in every attempted failing handshake. Two
  independent one-shot runs were byte-for-byte consistent; accumulation was
  deterministic across ten attempts.
- **Alternative Explanation:** Normal client disconnect cleanup is not broken;
  the targeted existing tests pass after entering the generator. The injected
  exception deliberately isolates the earlier ASGI response-start boundary,
  so queue timing, upstream availability, and replay state do not explain the
  retained admissions.
- **Verdict:** **CONFIRMED**.

## Candidate 2 — internal replay append failure burns a sequence

- **Hypothesis:** `ReplayLog.append()` advances `last_seq` before all fallible
  append work is complete. An exception after that assignment prevents the
  caller's documented rollback and leaves a phantom sequence.
- **Input/State:** Real `ReplayLog(epoch="0123456789abcdef",
  size_of=FailOnceSizer())` and production
  `FanoutMixin._publish_seq_frame()`. `FailOnceSizer` raises exact
  `OSError("INJECTED_REPLAY_SIZE_FAILURE")` once and succeeds thereafter.
- **Execution Path:**
  `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/replay_log.py:404-406`
  increments `_order`, assigns `state.last_seq = seq`, then calls fallible
  `self._size_of(payload)`. Rollback at
  `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/replay_log.py:465-483`
  requires `seq > state.last_seq`, which is now false. Production caller
  `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/tokenstream/fanout.py:184-246`
  catches the failure, attempts rollback, counts it, and drops the frame.
- **Existing Coverage:**
  `/home/mar/personal_projects/oc-slimapi/tests/test_token_seq.py:223-263`
  monkeypatches `log.append` to fail at function entry and proves that this
  pre-mutation failure rolls back cleanly. It does not inject after
  `state.last_seq` changes. Direct rollback tests likewise do not exercise a
  partially-mutated append.
- **Experiment:** `/tmp/opencode/failure_probe_replay_append.py` published two
  frames through `FanoutMixin`: first with the injected sizer failure, second
  normally, then requested replay from cursor zero. Exact command was run twice:
  `.venv/bin/python /tmp/opencode/failure_probe_replay_append.py && .venv/bin/python /tmp/opencode/failure_probe_replay_append.py`.
- **Expected:** First publish is dropped and reservation rolls back; state
  remains `last_seq=0`, next successful publish uses seq 1, and replay from zero
  returns that frame.
- **Actual:** Both runs emitted exact log text
  `seq rollback refused for sid 's1' seq 1 — domain sequence carries a hole (structurally unreachable under the synchronous-scope contract)`
  and exact `OSError: INJECTED_REPLAY_SIZE_FAILURE` from
  `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/replay_log.py:406`.
  State was `after_first result=None last_seq=1 frames=0 failures=1`.
  The next publish used seq 2: `after_second result_none=False last_seq=2
  frames=1 window_start=2 failures=1`. Replay from zero returned
  `ReplayResync(reason='replay_expired')` rather than the first successful frame.
- **Repeatability:** Two of two independent process runs produced identical
  state and reconnect outcome.
- **Alternative Explanation:** This is not an asynchronous reserve/append race;
  all relevant operations are synchronous and the failure is raised on the
  exact line after `last_seq` mutation. Default production bytes use
  `_default_size_of -> len(payload)`, so ordinary reachability is low, but
  `size_of` is a supported constructor dependency and the rollback invariant is
  explicitly documented for anything between reserve and append.
- **Verdict:** **CONFIRMED** (low default reachability, real invariant break).

## Candidate 3 — cache epoch invalidation can repopulate stale data

- **Hypothesis:** A catalog refresh already in flight when upstream epoch is
  invalidated can store its stale response after the invalidation fence.
- **Input/State:** Existing deterministic leader/follower refresh test pauses
  the factory, invalidates the cache, then releases the old response.
- **Execution Path:**
  `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/catalog_cache.py:111-152`
  captures `_generation`; `invalidate()` clears entries and increments that
  generation before a store decision.
- **Existing Coverage:**
  `/home/mar/personal_projects/oc-slimapi/tests/test_catalog_epoch_invalidation.py:132-191`.
- **Experiment:** Ran
  `TestGenerationFence::test_inflight_refresh_returns_body_but_never_stores`
  plus callback-failure isolation in the six-test resilience command below.
- **Expected:** Current waiters may receive the completed body, but stale data
  is not reinserted; a later fresh refresh stores normally.
- **Actual:** Test passed. Generation mismatch suppressed the stale store;
  callback failure was warning-only and did not block subsequent callbacks.
- **Repeatability:** One deterministic targeted run; existing test controls the
  ordering with events rather than timing sleeps.
- **Alternative Explanation:** None observed. The test directly inspects cache
  storage after the fence, so a merely hidden stale entry is unlikely.
- **Verdict:** **REJECTED**.

## Candidate 4 — dependency loss/retry causes duplicate epoch notification

- **Hypothesis:** Repeated connector exceptions during one token upstream-loss
  epoch either stop retries or fire duplicate loss notifications.
- **Input/State:** Existing connector script succeeds once, then raises repeated
  `RuntimeError("boom")`; stop is bounded by deterministic retry outcomes.
- **Execution Path:** Token hub reconnect loop and epoch loss callback.
- **Existing Coverage:**
  `/home/mar/personal_projects/oc-slimapi/tests/test_token_hub_lifecycle.py:384-430`.
- **Experiment:** Ran
  `TestRunReconnectWiring::test_exception_path_fires_once_per_epoch` in the
  six-test resilience command.
- **Expected:** Retries continue and loss callback fires exactly once for the
  epoch.
- **Actual:** Test passed; every scripted retry outcome was consumed and the
  callback count remained one.
- **Repeatability:** One deterministic targeted run; no wall-clock race is used
  for the assertion.
- **Alternative Explanation:** The test is narrower than a real socket half-open
  condition, but directly covers repeated dependency exceptions and duplicate
  notification state.
- **Verdict:** **REJECTED** for the tested exception path.

## Candidate 5 — DB auxiliary unavailability crashes or cannot recover

- **Hypothesis:** Initial read-only DB open failure, or a later bad-schema swap,
  leaves the auxiliary projection crashed or permanently disabled.
- **Input/State:** Existing injected open failure and bad-schema-then-valid
  reprobe fixtures.
- **Execution Path:** DB auxiliary lifecycle open/disable/reprobe state machine.
- **Existing Coverage:**
  `/home/mar/personal_projects/oc-slimapi/tests/test_dbaux_lifecycle.py` tests
  `test_open_failure_disables_not_crashes` and
  `test_swap_to_bad_schema_disables_then_reprobe_recovers`.
- **Experiment:** Ran both tests in the six-test resilience command.
- **Expected:** Failure degrades without crashing; a later valid reprobe restores
  service.
- **Actual:** Both tests passed.
- **Repeatability:** One deterministic targeted run each.
- **Alternative Explanation:** Does not model every SQLite OS error, but it
  directly exercises dependency unavailable and invalid-schema recovery.
- **Verdict:** **REJECTED** for the tested open/schema paths.

## Exact commands and counts

1. SSE probe, independent one-shot runs:
   `.venv/bin/python /tmp/opencode/failure_probe_sse_start_send.py`
   — run twice, each covering two routes; **4/4 injected handshakes leaked**.
2. Extended same probe: ten attempts per route in one process;
   **20/20 injected handshakes failed at response-start**, **16 admissions
   remained**, and **4 attempts were rejected after the two default caps filled**.
3. Replay probe:
   `.venv/bin/python /tmp/opencode/failure_probe_replay_append.py && .venv/bin/python /tmp/opencode/failure_probe_replay_append.py`
   — **2/2 runs burned seq 1** and returned `replay_expired` from cursor zero.
4. Resilience tests:
   `.venv/bin/pytest -q tests/test_catalog_epoch_invalidation.py::TestGenerationFence::test_inflight_refresh_returns_body_but_never_stores tests/test_catalog_epoch_invalidation.py::TestGlobalHubEpochCallback::test_callback_failure_degrades_to_warning tests/test_dbaux_lifecycle.py::test_open_failure_disables_not_crashes tests/test_dbaux_lifecycle.py::test_swap_to_bad_schema_disables_then_reprobe_recovers tests/test_token_hub_lifecycle.py::TestRunReconnectWiring::test_exception_path_fires_once_per_epoch tests/test_traffic_snapshot.py::TestFirstFrameFailure::test_open_failure_stays_inactive`
   — **6 passed, 55 warnings in 0.25s**.
5. Adjacent positive-control tests:
   `.venv/bin/pytest -q tests/test_token_seq.py::test_b1_append_failure_drops_frame_keeps_seq_contiguous tests/test_token_stream_route.py::TestTokenStreamNativeJoin::test_disconnect_detaches_subscriber tests/test_hub.py::test_events_route_streams_first_native_frame`
   — **3 passed, 28 warnings in 0.20s**. These prove pre-mutation append rollback and post-generator-entry disconnect cleanup work.
6. Upstream initial-send and partial-read mapping:
   `.venv/bin/pytest -q tests/test_upstream_error_boundary.py::test_messages_list_initial_send_network_error_returns_503 tests/test_upstream_error_boundary.py::test_message_full_single_initial_send_network_error_returns_503 tests/test_upstream_error_boundary.py::test_message_full_single_mid_read_network_error_returns_503`
   — **3 passed, 28 warnings in 0.18s**.

All pytest warnings above were Python 3.14 `pytest_asyncio` event-loop-policy
deprecations, not product failures. Two earlier combined commands used guessed,
nonexistent node IDs and aborted collection with `no tests ran`; these were
command-selection errors and are not counted as product evidence.

## Interesting passing scenarios

- Initial upstream connect failure and a partial JSON body followed by
  `httpx.ReadError` both map to structured 503 behavior.
- SSE disconnect after first iterator entry detaches control/token subscribers;
  the token flush loop stops on last detach.
- An append failure injected before `ReplayLog.append()` mutates state rolls the
  reservation back and keeps sequence numbers contiguous.
- Cache invalidation generation fence prevents stale in-flight refresh storage.
- A failing epoch callback is isolated; later callbacks still run.
- DB read-side startup failure degrades rather than crashes, and bad schema can
  recover on reprobe.
- Repeated token connector failure continues retries without duplicate
  upstream-loss notification for the same epoch.
- Traffic snapshot first-frame write failure follows its tested best-effort
  contract: inactive state and safe no-op stop.

## Git-history signals

- `6ad12ef refactor: normalize runtime to v4-only` is the implementation target;
  `e8873ad release: v4.13.0` is the release target.
- Recent adjacent fixes show that lifecycle edge states have been recurrent:
  `8fa99a4` fixed zombie `GlobalHub` revival after grace-cancel unwind;
  `0430118` added catalog-cache epoch generation fencing;
  `1c08be4` closed access-log archive TOCTOU; `f36bcee` added transactional
  startup rollback; `09c0359` introduced atomic sequence publication and the
  rollback contract exercised by Candidate 2.
- The response-start gap differs from those fixes: it is ownership transfer
  between route-time admission and generator-time cleanup, not grace-task
  revival after generator execution.

## Verified Bugs

### FI-001 — SSE admission leak before body-generator entry

- **Affected:** `/slimapi/events`, `/slimapi/sessions/{sid}/stream`.
- **Trigger:** ASGI send failure/cancellation on `http.response.start` after
  handler admission and before first body iteration.
- **Impact:** Permanent admission consumption until process shutdown; default
  caps can be exhausted after eight failures. Token route additionally retains
  a subscriber and live flush task.
- **Confidence:** High; deterministic 20/20 admitted failing handshakes across
  isolated and accumulation runs leaked, after which 4/4 additional attempts
  were rejected by the exhausted ledgers.

### FI-002 — partial `ReplayLog.append()` mutation defeats rollback

- **Affected:** Replay domains publishing through reserve/build/append/rollback,
  demonstrated through token fanout.
- **Trigger:** Exception from `size_of` after `last_seq` assignment and before
  entry insertion/accounting.
- **Impact:** Phantom sequence, refused rollback, subsequent sequence gap, and
  false `replay_expired` reconnect classification.
- **Confidence:** High for invariant/cause, low-to-moderate operational severity
  under the default byte payload/default sizer path.

## Remaining risks / untested surfaces

- Real server implementations may expose additional cancellation points around
  response-start versus the direct ASGI probe; the confirmed ownership gap does
  not depend on a specific server, but server-specific cleanup behavior was not
  integration-tested.
- Socket half-open, slow-reader backpressure, and cancellation during gzip
  framing were not injected in this lane.
- Replay append failures after `_size_of` (for example object construction or
  deque/accounting allocation failure) were not separately injected; source
  ordering indicates the same or later partial-state risk, but no separate
  verdict is claimed.
- Access-log rotation/compression and traffic snapshot periodic retry paths were
  inspected but not independently re-probed because dedicated existing failure
  tests and recent TOCTOU fixes already cover them.
- No full suite or `scripts/check.sh` was run; validation is explicitly owned by
  the parent orchestrator.

## Files changed

- `docs/automatic/.work/failure-injection.md` only.
