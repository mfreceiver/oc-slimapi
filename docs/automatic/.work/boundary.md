# Boundary lane report

## Scope and disposition

- Audited boundary behavior only. No production source or test file was modified.
- Runtime used for probes: `Python 3.14.4` (`.venv/bin/python --version`).
- Result: **2 confirmed boundary bugs**, both caused by Python's 4300-digit
  decimal-to-`int` guard escaping request-facing parsers; **2 investigated
  lookalikes rejected**.
- Validation remains owned by the parent orchestrator, per brief.

## Test landscape

The repository already has strong boundary matrices, so experiments targeted
gaps rather than replaying the full suite:

- `tests/test_selector.py`: lexical selector forms, blank/missing/duplicate
  selectors, retired/unsupported versions, versions exemption, and path
  normalization. It stops at ordinary unsupported values such as `999999`.
- `tests/test_sse_replay_wire.py`: global/token `Last-Event-ID` grammar,
  endpoint and sid domain separation, normal seq values, future IDs, and
  reconnect classification. It has no seq near Python's integer digit limit.
- `tests/test_cursor_matrix.py`, `tests/test_sessions_v4_matrix.py`,
  `tests/test_directory.py`, `tests/test_providers_projection_v4.py`, and
  `tests/test_file_raw_integration.py` already cover unusually broad null,
  malformed, Unicode, size, paging, and degradation edges. No contradictory
  result was found in those surfaces.
- `tests/test_actions_routes.py:520-606` covers the 1024-byte action-body edge,
  advertised oversize, chunked oversize, and a single oversized chunk.

Targeted existing-suite evidence:

```text
.venv/bin/pytest -q tests/test_selector.py
41 passed

.venv/bin/pytest -q tests/test_sse_replay_wire.py -k 'parse_global or parse_token or classify_reconnect_future'
18 passed, 66 deselected, 1 warning in 0.17s
```

These passes establish that both confirmed failures are missing extreme-input
cells, not pre-existing failures in the ordinary matrices.

## Git signals

- Audit baseline: `HEAD e8873ad` (`release: v4.13.0`); the v4-only runtime
  normalization is commit `6ad12ef`.
- Relevant recent fixes show active attention to edge correctness, including
  `8654d5b` (lifecycle teardown/test determinism), `854215a` (correctness and
  architecture audit batch), `6f9a941` (bare `#` raw-query forwarding), and
  `dfd5824` (degenerate message boundary rows). None addresses arbitrary-length
  decimal conversion in selector or replay parsing.
- Initial `git status --short` showed only the shared untracked
  `docs/automatic/.work/` area. Sibling lane files were not touched.

## Candidate BND-001 — extreme valid `?v=` decimal escapes as 500

**Hypothesis**

A lexically valid selector with more than 4300 decimal digits passes the frozen
selector grammar but raises an uncaught `ValueError` during integer conversion,
returning an unstructured 500 instead of `unsupported_version` 400.

**Input/State**

Minimal FastAPI application wrapped in `SlimapiSelectorMiddleware`; GET
`/slimapi/health?v=<N digits of 9>`. Tested digit counts
`1, 4, 4299, 4300, 4301, 5000, 10000` with
`httpx.ASGITransport(raise_app_exceptions=False)`, then captured the exception
with `raise_app_exceptions=True`.

**Execution Path**

- `src/oc_slimapi/selector.py:127-128` freezes lexical acceptance as
  `_SELECTOR_LEXICAL_RE = re.compile(r"^[1-9][0-9]*$")` with no length cap.
- `src/oc_slimapi/selector.py:547-563` validates grammar and duplicate values,
  then executes `if int(values[0]) not in SUPPORTED_WIRE_VERSIONS:` without a
  conversion guard.
- The v4 selector contract distinguishes lexically invalid selectors from
  lexically valid unsupported versions; this input belongs to the latter.

**Existing Coverage**

`tests/test_selector.py` covers many malformed spellings and ordinary large
unsupported values but has no input at 4300/4301 digits. Targeted suite result:
`41 passed`.

**Experiment**

Probe file (outside repository): `/tmp/opencode/boundary_probe_selector.py`.

```text
.venv/bin/python "/tmp/opencode/boundary_probe_selector.py"
```

The command was run three consecutive times. Each run tested the complete
boundary set and captured the raised exception.

**Expected**

All ASCII-decimal selectors other than `4` that pass the lexical grammar return
HTTP 400 JSON `{"code":"unsupported_version","supported":[4]}`. At minimum,
request input must not escape as 500.

**Actual**

Every run produced:

```text
digits=4299 status=400 body_prefix='{"code":"unsupported_version","supported":[4]}'
digits=4300 status=400 body_prefix='{"code":"unsupported_version","supported":[4]}'
digits=4301 status=500 content_type='text/plain; charset=utf-8' body_prefix='Internal Server Error'
digits=5000 status=500 content_type='text/plain; charset=utf-8' body_prefix='Internal Server Error'
digits=10000 status=500 content_type='text/plain; charset=utf-8' body_prefix='Internal Server Error'
raised=ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit
```

**Repeatability**

3/3 independent probe runs reproduced the exact 4300-pass/4301-fail boundary.

**Alternative Explanation**

This is not an ASGI transport artifact: `raise_app_exceptions=True` exposes the
exact uncaught production-parser exception, while neighboring 4300-digit input
traverses the same transport and returns the contractual JSON 400. A 4301-digit
query is also small enough to fit normal HTTP request-line limits used by the
deployment stack.

**Verdict**

`CONFIRMED`

Materiality: any unauthenticated `/slimapi/**` request can create false 5xx
telemetry and bypass the frozen version-error envelope with a roughly 4.3 KiB
query string.

## Candidate BND-002 — extreme replay seq crashes both SSE reconnect paths

**Hypothesis**

A digit-only `Last-Event-ID` seq longer than 4300 digits passes replay syntax,
then raises the same uncaught integer-conversion `ValueError` before subscriber
admission, causing both global and token SSE reconnect requests to fail as 500.

**Input/State**

Direct parser inputs and a minimal global SSE endpoint request using
`Last-Event-ID: g:0123456789abcdef:<N digits of 9>`. Direct counts were
`4299, 4300, 4301, 5000`; endpoint requests used 4301 digits and a fake hub that
must never be reached before classification.

**Execution Path**

- `src/oc_slimapi/sse/replay_wire.py:67-71` accepts seq with
  `_SEQ_RE = re.compile(r"^[0-9]+$")` and no length cap.
- `parse_last_event_id(...)` at
  `src/oc_slimapi/sse/replay_wire.py:114-154` returns
  `epoch, int(seq_text)` after syntax/domain checks, without guarding the
  conversion.
- Global `/events` classifies before subscription at
  `src/oc_slimapi/routes/events.py:81-98`; token stream does the same at
  `src/oc_slimapi/routes/token_stream.py:96-112`.
- `docs/specs/v4-contract.md:261,269-270` freezes decimal seq grammar and says
  invalid/cross-domain IDs are ignored/reset rather than surfacing a request
  failure.

**Existing Coverage**

`tests/test_sse_replay_wire.py:469-511` covers normal seq (`0`, `12`) and
malformed/domain cases but no extreme decimal. Targeted result:
`18 passed, 66 deselected, 1 warning in 0.17s`.

**Experiment**

Probe file (outside repository): `/tmp/opencode/boundary_probe_replay_seq.py`.

```text
.venv/bin/python "/tmp/opencode/boundary_probe_replay_seq.py"
```

The complete probe was run three consecutive times; each probe itself made
three endpoint attempts.

**Expected**

Replay parsing should classify or safely ignore/reset all request header input.
It must not emit an HTTP 500 before subscriber admission.

**Actual**

Every run produced:

```text
direct digits=4299 parsed_epoch=0123456789abcdef seq_digits=4299
direct digits=4300 parsed_epoch=0123456789abcdef seq_digits=4300
direct digits=4301 raised=ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit
direct digits=5000 raised=ValueError: Exceeds the limit (4300 digits) for integer string conversion: value has 5000 digits; use sys.set_int_max_str_digits() to increase the limit
endpoint attempt=1 status=500 body='Internal Server Error'
endpoint attempt=2 status=500 body='Internal Server Error'
endpoint attempt=3 status=500 body='Internal Server Error'
```

**Repeatability**

9/9 endpoint attempts returned the same 500 across three complete probe runs;
the direct parser boundary was identical in all three runs.

**Alternative Explanation**

The fake hub was never reached, excluding replay-log contents, subscriber
capacity, streaming timing, or upstream state. The direct parser reproduces the
same exception. A roughly 4.35 KiB header is below common 8 KiB per-field/header
budgets, so this is not merely an impossible synthetic size. The token endpoint
shares the same parser and pre-admission call shape, so the defect is common to
both domains even though the endpoint probe used the global route.

**Verdict**

`CONFIRMED`

Materiality: an unauthenticated reconnect header can force deterministic 500s
and reconnect loops on both replay-enabled SSE surfaces before T3 subscriber
admission.

## Candidate BND-003 — huge legacy access-log suffix

**Hypothesis**

`int(suffix)` in legacy numbered-log migration can encounter the same 4301-digit
failure and disrupt startup maintenance.

**Input/State**

Attempt to create a real file named `access.jsonl.` plus 4301 digits in a fresh
temporary directory on the audit host.

**Execution Path**

`src/oc_slimapi/access_log.py:836-875` checks a numeric suffix with `int(suffix)`.
Application startup wraps access-log maintenance at
`src/oc_slimapi/app.py:296-307` and degrades on maintenance failure.

**Existing Coverage**

Legacy migration tests exercise ordinary numeric suffixes. No huge suffix cell
exists, but reachability must precede parser speculation.

**Experiment**

```text
.venv/bin/python "/tmp/opencode/boundary_probe_guarded_edges.py"
```

**Expected**

The filesystem should reject a filename component far beyond Linux `NAME_MAX`,
preventing the suspected parser state from arising through real directory IO.

**Actual**

```text
filename_4301: OSError errno=36 text='File name too long'
```

**Repeatability**

Deterministic on the audit filesystem; the component exceeds the platform
filename limit by an order of magnitude.

**Alternative Explanation**

A mocked `Path.iterdir()` could synthesize such a name, but that would not
represent a production-reachable filesystem state. Even a synthetic maintenance
exception is caught by lifespan startup and converted to warning/degraded mode.

**Verdict**

`REJECTED`

## Candidate BND-004 — huge action `Content-Length`

**Hypothesis**

An over-4300-digit `Content-Length` might escape the action body-size guard via
the same Python conversion limit.

**Input/State**

Direct Starlette requests with a 4301-digit `Content-Length`, once with an empty
body and once with a 1025-byte body.

**Execution Path**

`src/oc_slimapi/routes/actions.py:83-99` wraps `int(content_length)` in
`except ValueError`, treats unusable values as undeclared, then enforces the
1024-byte streaming cap before append.

**Existing Coverage**

`tests/test_actions_routes.py:520-606` covers the semantic cap boundaries but
not the Python decimal digit limit.

**Experiment**

```text
.venv/bin/python "/tmp/opencode/boundary_probe_guarded_edges.py"
```

**Expected**

Conversion failure should be contained; an empty body should parse as `{}` and
an actual oversized stream should still return coded 413.

**Actual**

```text
content_length_4301 empty: result={}
content_length_4301 oversized_stream: coded=413/request_too_large
```

**Repeatability**

Both guarded branches were deterministic.

**Alternative Explanation**

An upstream HTTP server may reject the malformed/oversized numeric header even
earlier, which only narrows exposure further; the application-level path itself
is already safe.

**Verdict**

`REJECTED`

## Interesting passing scenarios

- Selector inputs of 4299 and exactly 4300 digits return the contractual
  `unsupported_version` 400; 4301 is the sharp failure edge.
- Replay seq inputs of 4299 and exactly 4300 digits parse successfully; 4301 is
  the sharp failure edge.
- Extreme `Content-Length` conversion failure remains bounded by body streaming
  and preserves coded 413 for an actual 1025-byte body.
- Existing action body tests prove exact 1024 bytes is not misclassified as 413,
  while chunked and single-chunk oversize are rejected.
- Existing cursor, sessions-v4, directory, providers, and file/raw matrices
  already exercise large identifiers, max-size boundaries, malformed rows,
  Unicode/control input, and degradation paths without a new reproducible defect.

## Exact experiment commands

```text
.venv/bin/python --version
.venv/bin/pytest -q tests/test_selector.py
.venv/bin/pytest -q tests/test_sse_replay_wire.py -k 'parse_global or parse_token or classify_reconnect_future'
.venv/bin/python "/tmp/opencode/boundary_probe_selector.py"
.venv/bin/python "/tmp/opencode/boundary_probe_replay_seq.py"
.venv/bin/python "/tmp/opencode/boundary_probe_guarded_edges.py"
```

For repeatability, the selector and replay probe commands were each executed
three consecutive times.

## Remaining risks / follow-up for parent

- Both confirmed defects share a root class but require fixes at two separate
  request parsers. Regression cells should pin 4300 and 4301 digits and avoid
  depending on `sys.set_int_max_str_digits()` global configuration.
- A production-server smoke test could additionally prove the deployed HTTP
  parser accepts the approximately 4.3 KiB query/header. The in-process evidence
  already establishes application behavior; common request/header budgets make
  the inputs realistically reachable.
- The token SSE endpoint was proven by shared exact parser/call path rather than
  a second streaming endpoint harness. Parent may add a token-route regression
  alongside the global-route regression when fixing.

## Repository changed-files statement

This lane intentionally writes only
`docs/automatic/.work/boundary.md`. Probe programs live under
`/tmp/opencode/` and are outside the repository. Production code and repository
tests remain unchanged.
