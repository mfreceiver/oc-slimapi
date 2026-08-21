# Design proposal — `GET /slimapi/sessions/{sid}/todo` (read-only todo skeleton)

- **Task:** T17 — product route selection/design (design doc 1 of 2)
- **Route under design:** `GET /slimapi/sessions/{sid}/todo`
- **Baseline HEAD:** `6a4ca78fa9a8f2951f669d61170a32e216417896`
- **Branch:** `bundle-slimapi-actions`
- **Evidence source:** `docs/traffic-opportunity-report-2026-08-10.md` (T16 report, top table row #1)
- **Status:** **IMPLEMENTED 2026-08-16**（T17/T18，[1.5.0]；wire 规范见 v3-contract §10 / INTERFACE_MAP §1）。原始 proposal 状态行（历史）：**PROPOSAL — NOT IMPLEMENTED.** No code, no contract, no INTERFACE_MAP, no CHANGELOG
  change is produced by this document. See §Approval gate.

> This is one of two T17 design docs. The sibling is
> `docs/specs/traffic-route-children-2026-08-10.md` (the other top-2 read-only candidate).

---

## 0. Why this route (T16 evidence recap)

From `docs/traffic-opportunity-report-2026-08-10.md` §"Top table" row #1 and §"Slimming
candidates A" rank 1:

| method | normalized_path | requests (3d) | upIn (bytes) | downOut (bytes) | ratio |
|---|---|---:|---:|---:|---:|
| GET | `/session/{sid}/todo` | 3,300 | 1,410,809 (1.35 MiB) | 1,410,809 (1.35 MiB) | 1.000 |

- `ratio ≈ 1.000` confirms this is **pure passthrough** today (byte-for-byte proxy, no
  projection, no compression). It is the **single largest unslimmed read cost** in the
  3-day window — the top `upIn` row of the whole passthrough table.
- Per-request average: `1,410,809 / 3,300 ≈ 427 B/req`.

This is the #1 ranked slimming candidate in the T16 report.

---

## 1. Upstream schema (evidence: file:line)

All paths relative to `/home/mar/personal_projects/ocdroid/opencode-src/current/`.

### Route registration
- **`packages/opencode/src/server/routes/instance/httpapi/groups/session.ts`**
  - `SessionPaths.todo = "/session/:sessionID/todo"` — **line 83**
  - Endpoint declaration — **lines 156-167**:
    `HttpApiEndpoint.get("todo", SessionPaths.todo, { params: { sessionID }, query: WorkspaceRoutingQuery, success: described(Schema.Array(Todo.Info), "Todo list"), error: [HttpApiError.BadRequest, ApiNotFoundError] })`

### Handler
- **`packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`**
  - `todo = Effect.fn(...)(function*(ctx){ return yield* todoSvc.get(ctx.params.sessionID) })` — **lines 94-96**
  - registered `.handle("todo", todo)` — **line 418**

### Response schema `Todo.Info`
- **`packages/schema/src/session-todo.ts:7-15`** — quoted verbatim:
  ```ts
  Schema.Struct({
    content:  Schema.String   // "Brief description of the task"
    status:   Schema.String   // "pending | in_progress | completed | cancelled"
    priority: Schema.String   // "high | medium | low"
  })
  ```
- **Response body** = `Schema.Array(Todo.Info)` — a JSON **array** of `{content, status,
  priority}` objects. **The schema is already minimal**: only 3 string fields per item.
  The bulk of the wire bytes is the free-text `content` strings, not field overhead.

### Error surface
- `HttpApiError.BadRequest` (400) and `ApiNotFoundError` (404) per the endpoint
  declaration. An unknown/garbage `sessionID` yields 404; a malformed sessionID yields 400.

---

## 2. Client consumption fields (ocdroid)

**Assumption (conservative inference — labelled):** ocdroid's todo-list UI renders every
todo item the user authored during a session, so it needs **all 3 fields** of every item:
- `content` — the task text shown in the todo row.
- `status` — drives the checkbox / strike-through state (`pending` / `in_progress` /
  `completed` / `cancelled`).
- `priority` — drives ordering / icon/color (`high` / `medium` / `low`).

**Implication for projection:** because every field of `Todo.Info` is UI-consumed, a
field-whitelist skeleton projection (like `skeleton_command` / `skeleton_agent` in
`src/oc_slimapi/skeleton.py:508-546`) yields **near-zero raw-byte saving** — there is
nothing to drop. Unlike the catalog routes (which drop `template`/`prompt`/`permission`
— the dominant bytes), todo has no "never-consumed heavy field".

Cross-reference — the existing `/slimapi/sessions` list skeleton (`skeleton_session` in
`skeleton.py:476-486`, `SESSION_KEYS` at `skeleton.py:471-473`) drops `cost`, `tokens`,
`location`, `subpath` from `Session.Info`. The todo schema has **no analogous heavy field
to drop** — it is already as thin as a whitelist can make it.

> **Honest conclusion:** the slimming lever for this route is **gzip compression** (§3),
> NOT skeleton projection. A field projection would not materially reduce the body.

---

## 3. Estimated saving

**Current state (T16):** passthrough, ratio 1.000, `upIn = downOut = 1,410,809 B` over 3,300
reqs in 3 days (~427 B/req avg).

### Lever (a) — gzip compression
- The body is a JSON array of small string objects. JSON text compresses well (~3-5× on
  structured text with repeated keys like `"content"`,`"status"`,`"priority"`).
- **BUT** — caveat: the per-request average is only ~427 B, and many responses are likely
  small (a handful of todos) or empty (`[]`). gzip has fixed framing overhead (~18-22 B
  for the gzip header/trailer + the deflate stream's own overhead). On a ~50-100 B body
  (e.g. `[]` or one short todo), gzip can be **net-negative** (compressed ≥ raw). On a
  ~1-4 KiB body (a session with many todos), gzip typically yields ~60-75% reduction.
- **Estimate (assumption-labelled):** assume the 1.35 MiB is distributed across a mix of
  empty/small (~60% of reqs, `[]` or 1-3 items, negligible gzip win) and larger
  (~40% of reqs, ≥10 items, ~70% gzip win on the larger subset ≈ 0.54 MiB × 0.7 ≈
  0.38 MiB saved on the larger subset).
  - **Rough per-3-day downOut saving estimate: ~0.3-0.4 MiB** (≈ 22-30% of the 1.35 MiB
    passthrough cost), almost entirely from gzipping the larger responses.
  - **Per-request average saving: ~90-120 B/req** (very unevenly distributed — most reqs
    save ~0, the large-tail reqs save hundreds of bytes).

### Lever (b) — skeleton projection
- **~0 raw-byte saving** (see §2). Not a lever for this route.

### Headline
- **Realistic 3-day saving: ~0.3-0.4 MiB downOut (~22-30%), gzip-only.** No projection win.
- This is an **estimate with assumptions** (response-size distribution is inferred, not
  measured per-response; the T16 report only gives aggregate `upIn`/`downOut`).

---

## 4. T3 cap (read-cap mechanism)

The new thin route would use the **existing** T3 read-cap chain, mirroring the catalog
routes (`src/oc_slimapi/routes/_catalog_common.py`) and the sessions list route
(`src/oc_slimapi/routes/sessions.py:44-101`):

- **Admission first:** `async with request.app.state.transforms as pool:` acquires a
  transform slot **before** the upstream GET (bounds concurrent body buffering + parse +
  projection CPU). Pool full → `TransformBusy` → `busy_response(...)` (§below).
- **Stream + cap-read:** the upstream response is consumed via
  `read_with_cap(response, cap, on_read=stash_up_in)` (from
  `src/oc_slimapi/transform.py`, imported in `sessions.py:12`), wrapped by
  `read_upstream_response(...)` in `_catalog_common.py:88-134`. The cap metric is
  **decompressed logical bytes**.
- **Cap field name:** the binding cap is `Settings.max_response_bytes`
  (`src/oc_slimapi/config.py:175`, env `OC_SLIMAPI_MAX_RESPONSE_BYTES`, **default
  `64 * 1024 * 1024` = 64 MiB**).

  > **Note (factual correction vs. the T17 brief):** there is **no**
  > `thin_route_max_response_bytes` field in `config.py`. The actual cap field reused by
  > every catalog/sessions/messages thin route is `max_response_bytes` (global). The only
  > per-route cap variant that exists is `questions_max_response_bytes`
  > (`config.py:330`, for the `/slimapi/questions` fan-out). This design reuses the
  > global `max_response_bytes` like the sibling catalog routes do — no new config field
  > is proposed.

- **Cap exceeded behaviour:** if `read_with_cap` returns `None` (body > cap), the route
  returns **413** with body `{"code":"response_too_large","limit":<cap>}` — exactly as the
  sessions list route does (`sessions.py:70-74`) and the catalog handler does
  (`_catalog_common.py:196-201` via `error_response("response_too_large", 413, ...)`).
- **Busy behaviour:** transform-pool admission timeout → **503**
  `{"code":"transform_busy"}` + response header `Retry-After: 2`
  (`_catalog_common.py:42-50`, `TRANSFORM_RETRY_AFTER_SECONDS = 2`).
- For this route specifically: the cap (64 MiB) is astronomically larger than any realistic
  todo body (per-req avg 427 B), so the 413 path is essentially unreachable in practice —
  it exists for defense-in-depth symmetry with the other read routes.

---

## 5. Fallback

Per the v2 contract, an unknown/unsupported `/slimapi/**` route falls through to the
catch-all reverse proxy, which returns **404 `{"code":"thin_route_not_found"}`** for any
unrecognised `/slimapi/` path (`src/oc_slimapi/proxy.py:130`). Therefore:

- **If this thin route is NOT deployed** (older sidecar, or this proposal is rejected),
  the client's `GET /slimapi/sessions/{sid}/todo` request receives 404
  `thin_route_not_found`, and the client **falls back** to the legacy passthrough path
  `GET /session/{sid}/todo` (which always exists on opencode upstream and is reverse-proxied
  byte-for-byte by the catch-all).
- **Client routing decision (recommended pattern, mirroring the catalog re-adds documented
  in `CLIENT_CHANGES.md:64-75`):** ocdroid should use **capability detection**, not version
  negotiation — probe once (a 404 `thin_route_not_found` cached as "unsupported") and
  thereafter route per-session todo fetches to the thin route when supported, else to the
  legacy passthrough. This is the established pattern for every additive `/slimapi/**`
  route (catalog skeleton, directories, questions re-add — all use try-thin-then-fallback).
- **Zero-regression guarantee:** an older ocdroid that never learned the thin route keeps
  hitting the legacy path through the catch-all; behaviour is byte-identical to today.

---

## 6. Wire classification

- **Additive new `/slimapi/**` route.** It does **not** modify any existing route, does
  **not** change `X-Slimapi-Version` (**stays 2**), does **not** bump the wire contract.
- **Contract authority — additive-change rule** (`docs/specs/v2-contract.md:43`):
  > "bump 规则：整数，仅破坏性变更 bump；加性变更同版本。"
  and (`v2-contract.md:19`):
  > "所有加性变更**不 bump `X-Slimapi-Version`** 除非另行说明。"
- This is the same classification used by every recent additive endpoint: the catalog
  skeletons (`v2-contract.md:13`, 2026-08-05), `/slimapi/directories` (`v2-contract.md:9`,
  2026-08-08), `/slimapi/actions` (`v2-contract.md` §2 actions API, 2026-08-09). All are
  explicitly documented as "加性新增，未 bump `X-Slimapi-Version`，仍 2".
- **No prior-removal to reconcile** for the todo route: there was never a
  `/slimapi/sessions/{sid}/todo` thin route in v1 that got deleted in v2. This is a
  genuinely brand-new endpoint (unlike the children route — see the sibling doc). So
  there is no "reverses a prior decision" concern here.

---

## 7. Test design (design level — NO test code here)

If implemented, the route would be tested by mirroring the existing sessions-list /
sessions-status test suite (`tests/test_sessions_routes.py`). The established assertions
to replicate (design-level, no code):

1. **Happy path:** fake upstream returns a `Todo.Info[]` array; assert the thin route
   returns 200 with the array body (projection is identity here — §2 — so the body equals
   the upstream body, optionally gzipped).
2. **gzip negotiation:** with `Accept-Encoding: gzip`, assert `Content-Encoding: gzip`
   + `Vary: Accept-Encoding` and the body decodes to the JSON array. Without it (or with
   `gzip;q=0`), assert no gzip. Mirror `make_project_and_pack`'s `accepts_gzip` use
   (`_catalog_common.py:162`).
3. **Cap behaviour:** oversized upstream body (> `max_response_bytes`) → 413
   `{"code":"response_too_large", ...}`. Mirror
   `test_sessions_list_oversize_body_returns_413` (`test_sessions_routes.py:208`).
4. **Busy / Retry-After:** transform pool saturated → 503
   `{"code":"transform_busy"}` + `Retry-After: 2`. Mirror the catalog busy tests.
5. **Error mapping:** upstream 4xx → 502 `upstream_http_N` (with `sid`-aware 404 →
   `session_not_found` mapping, since this is a per-session route — mirror the messages
   route's sid-aware mapping); upstream 5xx / network error / mid-stream read error /
   bad JSON / non-list body → 503 `upstream_unavailable`. Mirror
   `test_sessions_list_upstream_4xx_returns_502`, `..._5xx_returns_503`,
   `..._mid_stream_read_error_returns_503`, `..._bad_json_returns_503`,
   `..._non_array_json_returns_503` (`test_sessions_routes.py:74-205`).
6. **`directory` param (if accepted):** validated via `validate_directory`
   (`..`/control-char/overlong → 400 `invalid_directory`), forwarded as
   `X-Opencode-Directory` header (the route is per-session, so directory is routing-only —
   like the messages route).
7. **Route↔INTERFACE_MAP gate (REQUIRED companion change, NOT in this doc):**
   `scripts/check.sh` runs `scripts/check_routes_doc.py`, which enforces that **every**
   `/slimapi` route is registered in `docs/specs/INTERFACE_MAP.md`. A future
   implementation plan MUST add the new route to INTERFACE_MAP (one row in the §1 table,
   mirroring the `/slimapi/command` / `/slimapi/agent` rows at INTERFACE_MAP.md:27-28) or
   `check.sh` fails. This is the single non-negotiable companion edit; it is explicitly
   out of scope for this design doc.

---

## Approval gate (T17-C3)

**This design requires explicit user approval before any separate implementation plan is
opened.** This document is a **proposal**, not an implementation:

- No implementation code is produced here (T17-C4).
- No modification to `docs/specs/v2-contract.md`, `docs/specs/INTERFACE_MAP.md`,
  `docs/specs/CLIENT_CHANGES.md`, `CHANGELOG.md`, or any `src/` / `tests/` / `scripts/` /
  `deploy/` file is made by this doc.
- If approved, a **separate** implementation task would: add the route handler, add the
  projection (likely identity/passthrough — §2), add the INTERFACE_MAP row, add tests
  (§7), and record the additive wire behaviour in CHANGELOG. None of that happens here.

---

## Open questions / risks

1. **Is gzipping already-tiny bodies worth the CPU? (primary risk)** The per-request avg
   is only ~427 B, and a large fraction of responses are likely `[]` or 1-3 items where
   gzip is net-negative or negligible. The realistic saving (~0.3-0.4 MiB / 3d) is modest.
   The honest question: **is body slimming the right lever at all for this route, or is
   the real win reducing request frequency** (client-side caching / ETag / conditional
   GET)? The T16 report shows 3,300 reqs / 3d ≈ 1,100 reqs/day for a per-session todo list
   — that polling frequency, not per-response size, may be the actual cost driver. A
   caching/ETag design (not in scope here) could dwarf the gzip saving. **This is the
   single biggest open question for the todo route.**
2. **Projection has no win.** Unlike children (sibling doc) or the catalog routes, there
   is no heavy field to whitelist away. The route's value proposition is "gzip + cap +
   admission + structured errors", not "skeleton projection". If the user's mental model
   of "thin route" requires a field projection, this route does not deliver one.
3. **Empty-array handling.** The T16 avg (~427 B/req) divided across 3,300 reqs suggests
   many small/empty responses; the design should not gzip `[]` (the catalog routes' gzip
   threshold behaviour should be confirmed against the implementation when it happens).
4. **`directory` semantics.** Like the messages route, this is a per-session (`sid`)
   endpoint; `directory` is routing-only (selects the opencode workdir instance). The
   design assumes it is accepted as an optional query + `X-Opencode-Directory` header,
   mirroring `/slimapi/messages/{sid}`. Confirm against ocdroid's actual call shape when
   the implementation plan is opened.
