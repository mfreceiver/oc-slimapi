# Independent reproduction: FI-001 / FI-002

Date: 2026-08-24  
Baseline: `e8873ad` (`release: v4.13.0`)  
Scope: independent false-positive killing only; no production, test, config, contract, or changelog edits.

## Executive verdicts

| ID | Verdict | Severity calibration | Confidence | Operational calibration |
|---|---|---|---|---|
| FI-001 | **CONFIRMED** | **P3 on the currently installed Uvicorn stack; conditional P2 failure mode** | High on mechanism; medium on present-day incidence | A start-send exception leaks a slot and token flush task, and repetition exhausts the cap. Installed Uvicorn 0.51.0 normally returns silently for an already-disconnected client, so this is not equivalent to every routine disconnect. |
| FI-002 | **CONFIRMED** | **P3 latent correctness defect** | High on mechanism; low current-production reachability | A fallible supported `size_of` hook exposes a real non-atomic mutation and false `replay_expired`; current production callers publish `bytes` through the default non-throwing `len` path. |

## Environment and source controls

- Installed versions: Starlette `1.3.1`, Uvicorn `0.51.0`, Python venv under `.venv/lib/python3.14/`.
- Starlette source: `.venv/lib/python3.14/site-packages/starlette/responses.py:248-278` sends `http.response.start` before iterating `body_iterator`; ASGI `>=2.4` translates `OSError` to `ClientDisconnect`.
- Uvicorn source: `.venv/lib/python3.14/site-packages/uvicorn/protocols/http/h11_impl.py:207` advertises ASGI spec `2.3`; `RequestResponseCycle.send()` at lines `460-532` returns immediately when `disconnected` and otherwise calls `transport.write(output)`.
- Exact version/source inspection commands:

  ```bash
  .venv/bin/python -c 'import starlette, uvicorn; print(starlette.__version__, uvicorn.__version__)'
  .venv/bin/python -c 'import inspect, starlette; from starlette.responses import StreamingResponse; print("starlette", starlette.__version__); print(inspect.getsource(StreamingResponse.stream_response)); print(inspect.getsource(StreamingResponse.__call__))'
  rg -n 'async def stream_response|spec_version|ClientDisconnect' ".venv/lib/python3.14/site-packages/starlette/responses.py"
  rg -n 'spec_version|async def send|if self.disconnected|transport.write\(output\)' ".venv/lib/python3.14/site-packages/uvicorn/protocols/http/h11_impl.py"
  ```

---

## FI-001 — response-start failure leaks admitted subscribers

### Hypothesis

Both SSE routes admit a subscriber before returning `StreamingResponse`, but transfer cleanup ownership only to an async generator `finally`. If ASGI `send()` fails on `http.response.start`, Starlette has not entered the generator, so the admission cannot be released by that `finally`.

### Input/State

- Real event route with a real `HubRegistry`, cap `8`.
- Real token-stream route with real `TokenStreamRegistry` + `TokenStreamHub`, cap `8`.
- Fresh deterministic ASGI send functions raising either:
  - `RuntimeError("INDEPENDENT_START_RUNTIMEERROR")`, or
  - `OSError("INDEPENDENT_START_OSERROR")`.
- Both installed-server ASGI spec `2.3` and modern disconnect-signalling spec `2.4`.
- Passing control enters the body iterator, observes the first `slimapi.meta` frame, then closes it.

### Execution Path

- Event admission: `src/oc_slimapi/routes/events.py:98`; generator-only unsubscribe: `src/oc_slimapi/routes/events.py:139-200`; response creation: `src/oc_slimapi/routes/events.py:208-212`.
- Token admission: `src/oc_slimapi/routes/token_stream.py:112`; generator-only unsubscribe: `src/oc_slimapi/routes/token_stream.py:147-212`; response creation: `src/oc_slimapi/routes/token_stream.py:225-229`.
- Registry admission is synchronous and immediately increments the ledger (`src/oc_slimapi/sse/registry.py:200-234`, `src/oc_slimapi/sse/tokenstream/subscriber.py:268-358`). Token admission also attaches to the hub and starts flush work.
- Starlette sends the response start before `async for chunk in self.body_iterator`, so the route generators have not reached their `try/finally` at the injected boundary.

### Existing Coverage

- `tests/test_hub.py:374-389` enters the event generator with `anext()` before `aclose()`.
- `tests/test_token_stream_route.py:996-1009` drives the token body before asserting ledger zero and flush stopped.
- Those tests correctly cover cleanup **after generator entry**, not response-start failure before entry.

### Experiment

Fresh probe: `/tmp/opencode/repro_fi001_independent.py`.

```bash
.venv/bin/python "/tmp/opencode/repro_fi001_independent.py"
```

Additional installed-Uvicorn control: `/tmp/opencode/probe_uvicorn_start_send.py`.

```bash
.venv/bin/python "/tmp/opencode/probe_uvicorn_start_send.py"
```

The first probe ran:

- 24 one-shot failures: `3 repeats × 2 routes × 2 ASGI specs × 2 exception types`;
- 6 entered-generator passing controls: `3 repeats × 2 routes`;
- 12 cap-exhaustion batches: `3 repeats × 2 routes × 2 ASGI specs`, with 10 requests per batch (120 requests total).

The Uvicorn probe ran two controls: an already-disconnected cycle and a connected cycle with a deterministic raising transport.

### Expected

- Failed connection setup should release the admitted subscriber.
- Token setup failure should also stop last-detach flush work.
- Repeated failed starts should not consume all eight admission slots.
- Passing control should continue to clean up normally once the generator owns the admission.

### Actual

- **24/24** one-shot failures leaked exactly one subscriber on both routes (`after_failure=1`).
- Calling `await response.body_iterator.aclose()` on the never-entered generator still left `after_unstarted_aclose=1`; an unstarted async generator does not run this cleanup body.
- Token cases retained `_flush_task` (`flush_after_failure=true`) after both RuntimeError and OSError paths.
- ASGI 2.3 propagated the injected OSError as `OSError: INDEPENDENT_START_OSERROR`; ASGI 2.4 translated it to `ClientDisconnect`. Both retained admission.
- **6/6** entered-generator controls passed: first frame was `slimapi.meta`, ledger changed `1 → 0` on close, and token flush stopped. Unsubscribe itself is healthy once ownership transfers.
- **12/12** cap batches reproduced. Every batch returned statuses:

  ```text
  [200, 200, 200, 200, 200, 200, 200, 200, 503, 503]
  ```

  Each retained `current=8`, recorded `rejected=2`, and caught eight start failures. Across 120 requests, 96 failed starts consumed all slots and 24 subsequent attempts were rejected. Token batches retained active flush work.
- Installed-Uvicorn control:

  ```json
  {
    "disconnected_response_started": false,
    "disconnected_send": "returned_without_error",
    "raising_transport_error": "OSError: INDEPENDENT_UVICORN_TRANSPORT_WRITE",
    "raising_transport_response_started": true
  }
  ```

  Therefore installed Uvicorn suppresses the ordinary already-disconnected case, but a transport write exception still escapes at exactly the vulnerable boundary.

### Repeatability

- One-shot leak: 24/24.
- Passing generator cleanup: 6/6.
- Cap exhaustion: 12/12 batches, 120/120 expected status outcomes.
- No timing, sleeps, external network, or upstream server required.

### Alternative Explanation

1. **“Starlette catches the disconnect and closes the iterator.”** Rejected. ASGI 2.4 converts OSError to `ClientDisconnect`, but admission remains. ASGI 2.3 also leaks.
2. **“Explicitly closing the body iterator runs the `finally`.”** Rejected for the pre-entry state: `aclose()` left the ledger at one. It works only after first iteration, as all six controls show.
3. **“Any normal client disconnect triggers this in production.”** Not established and specifically narrowed. Installed Uvicorn 0.51.0 advertises ASGI 2.3 and returns silently if already disconnected. The confirmed trigger is an actual response-start `send`/transport exception, not an ordinary disconnect notification.
4. **“Shutdown eventually clears it.”** True but not exculpatory: registry shutdown recovers resources only at process teardown; repeated failures exhaust runtime admission before shutdown.

### Verdict: CONFIRMED

The ownership gap is real on both routes. Impact after trigger is availability-significant and deterministic, but current Uvicorn behavior lowers trigger frequency. I therefore calibrate it **P3 on the installed deployment**, with a **conditional P2** impact if the ASGI server/transport begins surfacing response-start write failures (or deployment changes to one that does).

---

## FI-002 — partial `ReplayLog.append()` mutation burns sequence

### Hypothesis

`ReplayLog.append()` mutates `_order` and `state.last_seq` before calling the injectable, fallible `size_of(payload)`. If sizing raises, no entry is inserted, but the mutation prevents token reservation rollback and makes the next successful entry start at sequence 2. Replaying from cursor 0 then falsely reports `replay_expired` despite no eviction.

### Input/State

- Fresh `ReplayLog(epoch="0123456789abcdef", size_of=FailOnceSizer())` whose first call raises `OSError("INDEPENDENT_SIZE_FAILURE")`, then returns `len(payload)`.
- Real global caller: `GlobalHub._replay_publish()`.
- Real token caller: `TokenStreamHub._publish_seq_frame()`.
- Passing controls:
  - builder failure before `append()` (the currently tested rollback boundary), and
  - default production sizer with two byte payloads.

### Execution Path

- `src/oc_slimapi/sse/replay_log.py:357-430`: `_order += 1` and `state.last_seq = seq` precede `size = self._size_of(payload)`.
- `src/oc_slimapi/sse/replay_log.py:432-483`: token rollback requires `seq > state.last_seq`; after partial mutation that condition is false.
- `src/oc_slimapi/sse/replay_log.py:487-555`: cursor 0 sees first retained seq 2 and returns `ReplayResync("replay_expired")`.
- `src/oc_slimapi/sse/tokenstream/fanout.py:184-246`: token reserve/build/append path promises rollback on any step 1-3 failure, but rollback is refused after this internal mutation.
- `src/oc_slimapi/sse/global_hub.py:845-865`: global publishing catches append errors and continues, so its replay domain also retains the partial mutation.
- Production construction at `src/oc_slimapi/app.py:467-473` does not pass a custom sizer; current global/token callers publish bytes, for which the default sizer uses `len` (`src/oc_slimapi/sse/replay_log.py:116-129`).

### Existing Coverage

- `tests/test_token_seq.py:223-266` monkeypatches `log.append` to raise **before entering the real append**, so rollback succeeds and the test never exercises a post-mutation exception.
- Existing replay tests cover genuine retained-window expiry but not an internal hole with `frames=0` after a failed append.
- Contract relevance: `docs/specs/v4-contract.md:244-274` defines per-domain sequence/replay semantics; `replay_expired` denotes a cursor older than the retained window, not a bookkeeping-only burned sequence.

### Experiment

Fresh probe: `/tmp/opencode/repro_fi002_independent.py`.

```bash
.venv/bin/python "/tmp/opencode/repro_fi002_independent.py"
```

It ran 20 deterministic cases:

- 5 global partial-mutation cases;
- 5 token partial-mutation cases;
- 5 pre-append builder-failure controls;
- 5 default-sizer controls.

### Expected

- A failed append should be atomic from the replay ledger’s perspective: no entry, no advanced `last_seq`, no advanced order, and the next success should use sequence 1.
- Cursor 0 should replay that first successful entry, not report expiry.
- Pre-append and in-append failures should both satisfy the documented token rollback guarantee.

### Actual

- **Global 5/5:** first call returned `None`; immediate state was:

  ```text
  last_seq=1, next_seq=2, order=1, frames=0,
  total_bytes=0, window_start=None
  ```

  The next success emitted `id: g:0123456789abcdef:2`, retained only seq 2, and cursor 0 returned `ReplayResync(reason="replay_expired")`.
- **Token 5/5:** first `_publish_seq_frame()` returned `None`, incremented `seq_publish_failures_total`, and left the same burned state. The next success emitted `id: t:s-fi002:0123456789abcdef:2` with payload `token-2`; cursor 0 returned false `replay_expired`.
- **Pre-append control 5/5:** builder failure left `last_seq=0,next_seq=1,order=0,frames=0`; next success reused seq 1; cursor 0 returned `ReplayFrames([1])`.
- **Default-sizer control 5/5:** byte publishes used seqs `[1,2]`; cursor 0 returned `ReplayFrames([1,2])`.

### Repeatability

- Failure path: 10/10 across both real callers.
- Controls: 10/10.
- No concurrency or timing involved.

### Alternative Explanation

1. **“The injected failure occurs before mutation, so rollback should work.”** Rejected by direct state snapshots: `_order` and `last_seq` advanced while frame count and bytes stayed zero.
2. **“The missing seq was evicted, so `replay_expired` is correct.”** Rejected: there was never an inserted seq-1 entry, total bytes remained zero, and no capacity/TTL operation ran.
3. **“The behavior affects only the token helper.”** Rejected: the real global caller reproduced the same state and wire classification.
4. **“This is currently reachable under normal production payloads.”** Not shown. The production app uses default sizing and both callers supply bytes; all five default-sizer controls passed. The defect is real at a supported injection/exotic-payload seam, but present production reachability is low.

### Verdict: CONFIRMED

The append operation is observably non-atomic under its supported fallible sizing hook, and the resulting classification is semantically false. Confidence in the mechanism is high. Because current production construction uses byte payloads and the default `len` path, severity is calibrated **P3 latent correctness**, not an asserted active outage.

---

## Targeted existing-test landscape

Exact corrected command (full suite intentionally not run):

```bash
.venv/bin/pytest -q \
  tests/test_hub.py::test_events_route_streams_first_native_frame \
  tests/test_token_stream_route.py::TestTokenStreamNativeJoin::test_disconnect_detaches_subscriber \
  tests/test_token_seq.py::test_b1_append_failure_drops_frame_keeps_seq_contiguous \
  tests/test_replay_log.py::test_ttl_not_expired_replays_normally
```

Result: `4 passed, 28 warnings in 0.27s`.

One initial selector typo referenced nonexistent `tests/test_hub.py::test_events_stream_disconnect_cleanup_no_double_unsubscribe`; pytest reported `ERROR: not found` and `no tests ran in 0.28s`. It was a collection mistake, not a product failure, and was corrected by reading the file.

## Counts and interesting passes

- Fresh independent probe cases: **64** total.
  - FI-001: **44** cases (24 single failures, 6 entered controls, 12 cap batches, 2 installed-Uvicorn controls); cap batches contain **120** route attempts.
  - FI-002: **20** cases (10 failure path, 10 controls).
- Existing targeted pytest nodes: **4 passed**.
- Interesting pass: cleanup and token flush shutdown are correct after generator entry.
- Interesting pass: the pre-append failure path currently tested does roll back and reuse seq 1.
- Interesting pass: current default byte sizing remains dense and replays normally.

## Git signals

- FI-001’s response construction/admission ordering traces to the original route implementations (`573fc2d` for event/replay work; `786d8fb` for token stream) and has no later response-start ownership fix.
- FI-002’s reserve/encode/append rollback promise was introduced by `09c0359` (`2026-08-23T09:26:07+08:00`, `feat: revision-6 lane B - atomic seq publication + replayable token_memory_limit resync`). Its same-commit test injects outside the real append and therefore does not falsify the partial-mutation case.

## Files changed

- Repository: `docs/automatic/.work/reproduction.md` only.
- Temporary evidence: `/tmp/opencode/repro_fi001_independent.py`, `/tmp/opencode/repro_fi002_independent.py`, `/tmp/opencode/probe_uvicorn_start_send.py`.
- Production code/tests/config/contracts/changelog: unchanged.
