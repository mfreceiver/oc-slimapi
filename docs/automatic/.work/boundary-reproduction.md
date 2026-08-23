# Boundary Reproduction Lane — BND-001 / BND-002

Date: 2026-08-24  
Scope: independent reproduction and false-positive elimination only.  
Validation owner: parent orchestrator.  
Production code and repository tests were not modified.

## Test landscape observations

- `/home/mar/personal_projects/oc-slimapi/tests/test_selector.py` exercises the real `SlimapiSelectorMiddleware` and ordinary malformed/unsupported selectors, but has no 4300/4301-digit selector boundary case.
- `/home/mar/personal_projects/oc-slimapi/tests/test_sse_replay_wire.py` covers normal global/token IDs, malformed forms, endpoint-domain mismatch, and token sid mismatch, but has no extreme decimal sequence case.
- Both production SSE routes classify reconnect state before subscriber admission:
  - `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/events.py:88-99`
  - `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/token_stream.py:102-113`
- The independent probe therefore used the real middleware, both real route handlers, and the real replay parser/log, with only the subscriber registries replaced by deterministic STOP-queue fakes.

## Contract and runtime evidence

- Selector grammar is frozen as unbounded `^[1-9][0-9]*$` by `/home/mar/personal_projects/oc-slimapi/docs/specs/v4-contract.md:24`; a lexically valid value outside supported set `{4}` belongs to the 400 unsupported-version class at `/home/mar/personal_projects/oc-slimapi/docs/specs/v4-contract.md:35`.
- SSE IDs are frozen as `g:<epoch>:<seq>` and `t:<sid>:<epoch>:<seq>` with decimal `seq` at `/home/mar/personal_projects/oc-slimapi/docs/specs/v4-contract.md:259-270`. Malformed/cross-domain/cross-sid values are ignored and reset; the contract gives no decimal-length rejection or 500 outcome.
- Installed runtime: Python `3.14.4`; `sys.get_int_max_str_digits() == 4300`; `sys.flags.int_max_str_digits == 4300`; `sys.int_info.default_max_str_digits == 4300`; threshold `640`; `PYTHONINTMAXSTRDIGITS` unset.
- Installed server: Uvicorn `0.51.0`; `httptools` absent; h11 `0.16.0` present. A real `http="auto"` server selected `uvicorn.protocols.http.h11_impl.H11Protocol`; `DEFAULT_MAX_INCOMPLETE_EVENT_SIZE == 16384` bytes.
- `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/app.py:841-857` uses Uvicorn defaults; `/home/mar/personal_projects/oc-slimapi/deploy/stunnel.conf:18-29` is TCP forwarding, not an HTTP size filter; `/home/mar/personal_projects/oc-slimapi/deploy/oc-slimapi.service:33-43` does not override the Python digit limit.

## Git-history signals

- Relevant history includes the v4-only normalization commit `6ad12ef`; no later guard for either conversion was found.
- Historical `/home/mar/personal_projects/oc-slimapi/docs/audits/2026-08-20/02-findings/F-013.md` recorded the same replay-sequence conversion class at older snapshot `0b836e7`. The independent current probe confirms persistence; BND-002 is not a novel class.
- Historical F-013 and the analogous single-request input robustness finding F-253 both settled at P3 because exposure is authenticated/loopback and impact is request-local without state corruption or amplification.

## Exact experiments

Temporary artifact (outside repository): `/tmp/opencode/boundary_reproduction_probe.py`.

The probe records 31 deterministic observations per run: runtime/server facts, direct CPython boundaries, both parser domains, short-circuit controls, ASGI selector/global/token wire results, escaped exceptions, selected network protocol, and six real-network requests.

```bash
.venv/bin/python "/tmp/opencode/boundary_reproduction_probe.py"
set -o pipefail; for i in 1 2 3; do .venv/bin/python "/tmp/opencode/boundary_reproduction_probe.py" | wc -l; done
```

Actual repetition counts: `31`, `31`, `31`; all tested verdict-bearing values were identical in 3/3 complete repetitions.

Targeted existing tests only (no full suite):

```bash
for i in 1 2 3; do PYTHONWARNINGS=ignore .venv/bin/pytest -q tests/test_selector.py tests/test_sse_replay_wire.py; done
```

Actual: `125 passed` in `0.77s`, `125 passed` in `0.84s`, `125 passed` in `0.81s` — 375 targeted test executions, zero failures.

## BND-001 — extreme `?v=` decimal

**Hypothesis**  
A selector value containing 4301 decimal digits is lexically valid but the unguarded integer conversion raises Python's digit-limit `ValueError`, escaping selector error mapping and producing a plain 500. Exactly 4300 digits still follows normal coded unsupported-version handling.

**Input/State**  
Python 3.14.4 with active 4300-digit limit; requests to a real `SlimapiSelectorMiddleware` app and to a real loopback Uvicorn server. Inputs were `?v=` followed by 4299, 4300, or 4301 copies of `9`.

**Execution Path**  
`/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/selector.py:127-128` accepts every input via `^[1-9][0-9]*$`; `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/selector.py:547-567` reaches unguarded `int(values[0])` before normal unsupported-version rejection.

**Existing Coverage**  
The selector suite covers normal lexical and support-set classes, but no conversion-limit boundary. It remained green and therefore did not falsify the missing edge.

**Experiment**  
1. Call `int("9" * digits)` directly at 4299/4300/4301.
2. Send the same values through in-process ASGI with exception capture disabled and enabled.
3. Send 4300/4301 through a real Uvicorn/h11 TCP listener and record request-target byte length and response.

**Expected**  
If BND-001 is real: 4299/4300 convert normally and produce coded 400 unsupported-version responses; 4301 raises the exact CPython limit error and becomes a plain 500. If it is an ASGI-test artifact or unreachable oversized input, the real network server should reject it before middleware or behave differently.

**Actual**  
- Direct 4299 and 4300 conversions succeeded. Direct 4301 raised exactly: `ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit`.
- In-process 4299 and 4300 requests returned `400 application/json` with current normal body `{"code":"unsupported_version","supported":[4]}`.
- In-process 4301 returned `500 text/plain; charset=utf-8`, body `Internal Server Error`; `raise_app_exceptions=True` exposed the exact `ValueError` above.
- Real Uvicorn/h11: 4300-digit request target = 4318 bytes and returned 400; 4301-digit request target = 4319 bytes and reached application code, returning plain 500.

**Repeatability**  
3/3 complete probe repetitions matched. The initial development run also reproduced the same wire and exception boundary.

**Alternative Explanation**  
- Rejected: test-client-only artifact — real TCP Uvicorn reproduced it.
- Rejected: request target too large for deployed parser — 4319 bytes is below active 16384-byte h11 allowance and was accepted.
- Rejected: 4301 is lexically malformed — it matches the frozen unbounded selector grammar.
- Qualification: environments that explicitly disable/increase `int_max_str_digits` may not reproduce. The shipped service does not set such an override, and the installed runtime uses 4300.

**Verdict: CONFIRMED**

Severity/confidence: **P3 / high**. An authenticated client can deterministically cause one request-local 500 and log noise, but no state mutation, persistence, or amplification was observed.

## BND-002 — extreme SSE `Last-Event-ID` decimal

**Hypothesis**  
A syntactically valid global or token `Last-Event-ID` containing 4301 sequence digits passes replay syntax/domain validation, then the unguarded integer conversion raises the same `ValueError`. Both SSE routes return plain 500 before subscriber admission. Exactly 4300 digits remains accepted.

**Input/State**  
Replay epoch `0123456789abcdef`, token sid `probe-sid`, and real route inputs:

- Global: `g:0123456789abcdef:<seq>` on `/slimapi/events?v=4`.
- Token: `t:probe-sid:0123456789abcdef:<seq>` on `/slimapi/sessions/probe-sid/stream?v=4`.
- Sequence was 4299/4300/4301 copies of `9`; deterministic fake registries counted subscribe/unsubscribe calls.

**Execution Path**  
`/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/replay_wire.py:67-71` accepts unbounded decimal text; `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/sse/replay_wire.py:114-154` returns `epoch, int(seq_text)` without guarding conversion. Global and token handlers classify at `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/events.py:88-99` and `/home/mar/personal_projects/oc-slimapi/src/oc_slimapi/routes/token_stream.py:102-113`, respectively, before subscribing.

**Existing Coverage**  
Existing replay tests cover ordinary valid IDs and malformed/domain/sid short-circuits, but not a syntactically valid integer over the Python conversion limit. Both targeted files passed in all three runs.

**Experiment**  
1. Invoke production `parse_last_event_id()` directly for both domains at 4299/4300/4301.
2. Exercise both real SSE route paths in-process, recording response content type/body and registry admission counts.
3. Exercise both paths over real Uvicorn/h11 TCP.
4. Run controls for 4301 zeros, one leading zero plus 4300 nines, a non-decimal suffix, and a token ID sent to the global parser.

**Expected**  
If BND-002 is real: both domains succeed at 4300, fail with the exact `ValueError` at 4301 digits, and fail before subscribe. Malformed/cross-domain controls should short-circuit to `None`; all-digit 4301 controls should establish whether leading zeros change the installed Python 3.14 behavior.

**Actual**  
- Direct global and token parsing succeeded at 4299 and 4300; both raised the exact 4301-digit `ValueError` quoted under BND-001.
- In-process global 4300 and token 4300 returned `200 text/event-stream`; the matching registry subscribed and unsubscribed exactly once.
- In-process global 4301 and token 4301 returned `500 text/plain; charset=utf-8`, body `Internal Server Error`; both subscribe counters stayed `0`, proving failure before admission.
- `raise_app_exceptions=True` exposed the same exact `ValueError` on both route paths.
- Real Uvicorn/h11 accepted and returned 200 for global 4319-byte/token 4329-byte headers at 4300 sequence digits; it accepted and returned plain 500 for global 4320-byte/token 4330-byte headers at 4301 sequence digits.
- Boundary controls: 4301 zeros and one leading zero plus 4300 nines both raised the same digit-limit `ValueError`; 4300 nines plus `x` returned `None`; token-form ID on the global parser returned `None` without conversion.

**Repeatability**  
3/3 complete probe repetitions matched for direct parser, global route, token route, and real-network behavior.

**Alternative Explanation**  
- Rejected: only the global route is affected — token route reproduced identically.
- Rejected: subscribers or SSE generators cause the 500 — admission count remained zero; exception occurs during pre-admission classification.
- Rejected: header cannot reach the app — real h11 accepted 4320/4330-byte headers, well below its active 16384-byte allowance; stunnel adds no HTTP filtering.
- Confirmed boundary characteristic: on the installed Python 3.14.4 runtime, the tested 4301-digit all-zero and leading-zero forms also raise. The trigger is therefore not limited to non-zero/significant-digit payloads in this deployment.
- Rejected: the contract permits 500 for oversized decimal syntax — the frozen grammar says decimal and specifies ignore/reset for invalid IDs; it states no length limit or exception outcome.

**Verdict: CONFIRMED**

Severity/confidence: **P3 / high**. This is a persistent, authenticated request-local robustness defect on two reconnect endpoints, with no observed subscription/state mutation or amplification. It independently reconfirms historical F-013 rather than establishing novelty.

## Interesting passing scenarios

- The all-`9` CPython boundary is exact: 4300 digits succeeds; 4301 digits fails.
- Both 4300-digit SSE route controls complete as SSE and balance subscribe/unsubscribe 1:1.
- Replay syntax/domain short-circuiting works: non-decimal suffix and cross-endpoint token ID return `None` before conversion.
- The 4301-zero and 4301-leading-zero controls fail consistently, pinning the installed runtime's behavior rather than assuming leading zeros are exempt.
- Targeted selector/replay suites passed 375/375 executions across three independent runs.

## Remaining risks / limits

- No full suite was run, by instruction; parent orchestrator owns validation.
- Severity assumes the documented deployment boundary (loopback sidecar behind stunnel mTLS). A different HTTP front end with a sub-4.3-KiB target/header cap could make exploitation unreachable, while direct loopback access or a larger parser cap preserves it.
- This lane did not assess unrelated selector response-shape or replay-policy issues.

## Changed files

- Added `/home/mar/personal_projects/oc-slimapi/docs/automatic/.work/boundary-reproduction.md`.
- Temporary only: `/tmp/opencode/boundary_reproduction_probe.py`.
- Production code, repository tests, and all other documentation remained unchanged.
