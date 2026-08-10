# Design proposal — `GET /slimapi/sessions/{sid}/children` (read-only children skeleton)

- **Task:** T17 — product route selection/design (design doc 2 of 2)
- **Route under design:** `GET /slimapi/sessions/{sid}/children`
- **Baseline HEAD:** `6a4ca78fa9a8f2951f669d61170a32e216417896`
- **Branch:** `bundle-slimapi-actions`
- **Evidence source:** `docs/traffic-opportunity-report-2026-08-10.md` (T16 report, top table row #2)
- **Status:** **PROPOSAL — NOT IMPLEMENTED.** No code, no contract, no INTERFACE_MAP, no
  CHANGELOG change is produced by this document. See §Approval gate.

> This is one of two T17 design docs. The sibling is
> `docs/specs/traffic-route-todo-2026-08-10.md`. **This doc carries the single most
> important judgement call of T17**: the v2 removal history (§1.1) and its reconciliation
> (§6.1). Read those before any implementation plan.

---

## 0. Why this route (T16 evidence recap)

From `docs/traffic-opportunity-report-2026-08-10.md` §"Top table" row #2 and §"Slimming
candidates A" rank 2:

| method | normalized_path | requests (3d) | upIn (bytes) | downOut (bytes) | ratio |
|---|---|---:|---:|---:|---:|
| GET | `/session/{sid}/children` | 3,393 | 427,682 (0.41 MiB) | 429,620 (0.41 MiB) | 1.005 |

- `ratio ≈ 1.005` confirms this is **pure passthrough** today (byte-for-byte proxy, no
  projection, no compression). It is the **2nd-largest unslimmed read cost** in the 3-day
  window.
- Per-request average: `427,682 / 3,393 ≈ 126 B/req avg`.
- **Caveat (important):** the ~126 B/req average is *smaller than a single populated
  `Session.Info` object* (which is a 14-field struct — §1). This strongly implies **a large
  fraction of responses are empty arrays `[]`** (sessions with no children) or near-empty.
  The slimming value is concentrated in the **non-empty tail** of the response-size
  distribution, not the average request. See §3.

---

## 1. Upstream schema (evidence: file:line)

All paths relative to `/home/mar/personal_projects/ocdroid/opencode-src/current/`.

### Route registration
- **`packages/opencode/src/server/routes/instance/httpapi/groups/session.ts`**
  - `SessionPaths.children = "/session/:sessionID/children"` — **line 82**
  - Endpoint declaration — **lines 144-155**:
    `HttpApiEndpoint.get("children", SessionPaths.children, { params: { sessionID }, query: WorkspaceRoutingQuery, success: described(Schema.Array(Session.Info), "List of children"), error: [HttpApiError.BadRequest, ApiNotFoundError] })`

### Handler
- **`packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`**
  - `children = Effect.fn(...)(function*(ctx){ return yield* session.children(ctx.params.sessionID) })` — **lines 89-91**
  - registered `.handle("children", children)` — **line 417**

### Response schema `Session.Info` (HEAVY — 14-field struct)
- **`packages/schema/src/session.ts:19-44`** — the element shape of the returned array:
  ```
  Schema.Struct({
    id, parentID?, projectID, agent?, model?,
    cost (Finite),
    tokens: { input, output, reasoning, cache: { read, write } },
    time:   { created, updated, archived? },
    title (String),
    location (Location.Ref),
    subpath?,
    revert?
  })
  ```
- **Response body** = `Schema.Array(Session.Info)` — a JSON **array** of these heavy
  objects, one per child session.

### This is the SAME shape `/slimapi/sessions` already projects
`Session.Info` is **exactly** the element type returned by the upstream list route
`GET /session` (which the existing `/slimapi/sessions` thin route consumes). The existing
sessions-list skeleton projection — `skeleton_session()` in
`src/oc_slimapi/skeleton.py:476-486`, with whitelist `SESSION_KEYS` at
`skeleton.py:471-473` — already knows how to thin this struct. **A children thin route can
reuse `skeleton_session()` verbatim.** See §2 for the keep/drop breakdown.

### Error surface
- `HttpApiError.BadRequest` (400) and `ApiNotFoundError` (404) per the endpoint
  declaration. Unknown `sessionID` → 404; malformed sessionID → 400.

---

## 1.1. CRITICAL — v2 removal history (the single most important judgement call)

A `/slimapi/sessions/{sid}/children` thin route **existed in v1 and was REMOVED in the v2
contract revision**. Any re-introduction MUST reconcile this. The evidence:

### What was removed
- **`docs/specs/v2-contract.md:74`** (§"v2 删除的端点"):
  > "`GET /slimapi/sessions/{sid}/children` —— children 投影端点删除（含
  > `X-Children-Version` 头、`childrenIDs[]` / `childrenComplete` list hint 全部移除）。"
- **`docs/specs/v2-contract.md:34`** (v2 删减面):
  > "移除 routeToken、discovery allowlist 数据流、**children 投影缓存**、Stage B 单条
  > fingerprint … 以及 10+ 依赖性端点（… `/sessions/{sid}/children` …）。"
- **`docs/specs/v2-contract.md:260`** (§digest fields):
  > "v2 字段删除：`childrenVersion?`、`contentRevisions?` 已移除（children 投影缓存与
  > Stage B fingerprint 全部下线）。客户端**不应**再消费这两个字段。"
- **`docs/specs/v2-contract.md:488`** (§client migration):
  > "v2 删除面：… `/sessions/{sid}/children` + `childrenVersion` 比对 … 客户端**不应**
  > 再实现这些代码路径。"
- **`CHANGELOG.md:312,317,323-324`**: the v2 batch deleted
  `routes/sessions_children.py`, `children_cache.py`, and removed `childrenVersion` from
  the digest fields.

### WHY it was removed (root cause)
The v1 children route was **not a standalone read**. It was the read-side of a
**cache-coherence system**:
- A per-key **cache** + **single-flight** (`children_cache.py`, contract §16) stored
  projected children arrays keyed by parent sid.
- A **version tag** `X-Children-Version` (a monotonic generation number) was returned with
  each children fetch.
- A **digest field** `childrenVersion` (bumped on the *parent* session's digest when a
  child was created — driven by the `session.created` SSE event →
  `children_cache.invalidate(parentID)`, see `CHANGELOG.md:570`) let the client know to
  refetch.
- **List hints** `childrenIDs[]` + `childrenComplete` on `/slimapi/sessions` (per-row)
  back-filled the cache from the list response (`CHANGELOG.md:569`).

The v2 revision **deleted the entire fingerprint/cache-coherence machinery** as a
deliberate simplification: routeToken, discovery allowlist, **children projection cache**,
Stage B single-part fingerprint (`_part_state`/`contentRevisions`/`X-Message-Event-Seq`/
304/`?known.*`), Opt-A partial-envelope, BatchLedger — **all** the incremental/cache layers
went away together (`v2-contract.md:17, 34`). The children **route** was collateral
damage: its reason-to-exist was the cache invalidation layer, and without that layer the
v1 authors judged the bare projected-fetch not worth keeping as a first-class endpoint.
The client was told to stop consuming `childrenVersion` and the `/sessions/{sid}/children`
path (`v2-contract.md:488`).

### What this implies for re-introducing a children thin route NOW
The v2 removal removed the **cache-coherence machinery**, NOT a judgement that "clients
never need to list child sessions". The underlying upstream capability
(`GET /session/{sid}/children`) still exists and ocdroid is clearly still calling it
through the passthrough catch-all (3,393 reqs / 3d — T16 row #2). A **stateless** re-add
(no `childrenVersion`, no `X-Children-Version`, no `childrenIDs[]`/`childrenComplete`
hints, no cache, no single-flight) is a **different animal** from the v1 route: it is just
a skeleton projection + gzip + cap of the upstream array, exactly analogous to the
`/slimapi/sessions` list route but scoped to one parent's children.

### DIRECT re-add precedent (the reconciliation path)
Two other routes suffered the exact same lite-v2 deletion and were **additively re-added**
later, with the contract explicitly documenting the re-add via strikethrough:

- **`GET /slimapi/sessions/status`** — removed in lite-v2, **additively re-added
  2026-08-03** (`v2-contract.md:14`, `:73` shows `~~...~~` strikethrough; INTERFACE_MAP
  row at `docs/specs/INTERFACE_MAP.md:26`). Re-add was **stateless** (read-only turn-merge,
  no cache).
- **`GET /slimapi/questions`** — removed in lite-v2, **additively re-added 2026-08-05**
  (`v2-contract.md:12`, `:72` shows `~~...~~` strikethrough; INTERFACE_MAP row at
  `docs/specs/INTERFACE_MAP.md:29`). Re-add was a **new design** (cross-directory fan-out
  aggregator), not a v1 resurrection.

**Both re-adds are classified additive, `X-Slimapi-Version` stays 2.** A stateless children
skeleton re-add is directly analogous: it reverses a prior lite-v2 deletion, but relative
to the **current v2 surface** (where the route is simply absent) it is a **net-new
additive endpoint**. See §6.1 for the full classification.

---

## 2. Client consumption fields (ocdroid)

**Assumption (conservative inference — labelled):** ocdroid renders a session's children
as a sub-list under the parent (a "sub-sessions" / "forks" / "branches" view). The minimal
field set such a UI needs is the same set the `/slimapi/sessions` **list** view needs —
which is exactly what `skeleton_session()` already keeps.

### Existing projection — `skeleton_session()` (`src/oc_slimapi/skeleton.py:476-486`)
Whitelist `SESSION_KEYS` (`skeleton.py:471-473`) + nested picks:
- **Kept (top-level):** `id`, `directory`, `parentID`, `projectID`, `title`, `agent`, `model`
- **Kept (nested):**
  - `time.{created, updated, archived}` (sort key + display + archived flag)
  - `summary.{additions, deletions, files}` (diff summary, if present)
  - `revert.{messageID, partID}` (revert pointer, if present)
- **Dropped (the heavy / non-UI fields):**
  - `cost` (Finite) — per-child dollar cost; likely not shown in a children list (assumption).
  - `tokens: {input, output, reasoning, cache:{read, write}}` — the **heaviest** nested
    object (5 integers); only needed if the UI shows per-child token accounting (assumption:
    not in a list view).
  - `location` (`Location.Ref`) — upstream location ref; redundant with `directory`.
  - `subpath` — rarely populated; assumption is the children list does not render it.

**Reuse conclusion:** a children thin route should project each child via the **existing**
`skeleton_session()` — zero new projection code, identical keep/drop semantics to the
sessions list route the client already consumes. This is the strongest reuse signal in T17:
the heavy `Session.Info` element is already a solved projection.

> **Honest note:** whether ocdroid actually wants `cost` / `tokens` in the children view is
> an open assumption (§Open questions). If it does, the projection can be widened — but the
> default conservative choice is to mirror the list skeleton and let the client fetch full
  child detail via the upstream `GET /session/{sid}` passthrough when needed.

---

## 3. Estimated saving

**Current state (T16):** passthrough, ratio 1.005, `upIn = 427,682 B`, `downOut = 429,620 B`
over 3,393 reqs in 3 days (~126 B/req avg).

### Lever (a) — skeleton projection (the dominant lever here)
Dropping `cost`, `tokens` (5-int nested), `location`, `subpath` from each `Session.Info`:
- A fully-populated `Session.Info` is roughly ~400-700 B raw JSON (14 fields + nested
  tokens object). The dropped fields (`cost` ~10 B, `tokens` ~80-120 B, `location` ~50-100
  B, `subpath` ~0-30 B) account for roughly **~40-60% of a populated element's bytes**.
- **BUT** — caveat: the ~126 B/req average is *smaller than one populated `Session.Info`*,
  which means **most responses are `[]` or near-empty** (sessions with no children).
  Projection saves nothing on `[]`. The saving is concentrated on the non-empty tail
  (sessions that actually have children).
- **Estimate (assumption-labelled):** assume ~20% of responses are non-empty with avg ~3
  children each (~1.5 KiB raw → ~700 B projected after dropping heavy fields). Non-empty
  subset bytes ≈ 0.20 × 3,393 × 1.5 KiB ≈ 1.0 MiB raw over 3d... but that exceeds the
  measured 0.41 MiB total, so the non-empty fraction / per-child size is smaller. A more
  conservative model: ~10% non-empty, avg ~2 children, ~800 B raw each → ~0.27 MiB raw on
  the non-empty subset, ~50% projection saving → **~0.13 MiB saved over 3d** from
  projection.

### Lever (b) — gzip compression
- The non-empty responses (arrays of structs with repeated keys) gzip well (~60-70%). On
  the ~0.27 MiB non-empty subset (model above), gzip adds ~0.16-0.19 MiB further saving.
- Empty `[]` responses (the majority) gain nothing from gzip (2 B → ~22 B — net negative;
  the implementation should skip gzip on tiny bodies, matching the catalog routes'
  behaviour).

### Headline (estimate, assumption-labelled)
- **Realistic 3-day saving: ~0.25-0.35 MiB downOut (~60-80% of the 0.41 MiB passthrough
  cost)**, split roughly: projection ~0.13 MiB + gzip ~0.15-0.20 MiB, almost entirely from
  the non-empty tail.
- **Per-request average saving: ~75-100 B/req** (very uneven — empty responses save ~0,
  non-empty responses save ~1 KiB each).
- **The realistic saving is a meaningful fraction of the route's cost** because the
  non-empty responses carry the heavy `Session.Info` struct that projection+gzip attacks
  effectively. This is a stronger case than the todo route (sibling doc), where projection
  has nothing to drop.

---

## 4. T3 cap (read-cap mechanism)

The new thin route would reuse the **existing** T3 read-cap chain. Two viable structural
mirrors exist in the codebase:

- **Mirror A — the sessions-list route** (`src/oc_slimapi/routes/sessions.py:20-111`):
  admission → stream upstream → `read_upstream_response(...)` cap-read → offload
  `skeleton_session()` projection per element → gzip. This is the closest mirror because
  the children response is `Session.Info[]` (same element type as the list route).
- **Mirror B — the shared catalog handler** (`src/oc_slimapi/routes/_catalog_common.py`,
  `handle_catalog_request` at `:168-215`): the same chain factored into a reusable
  function, used by `/slimapi/command` and `/slimapi/agent`.

Either way the mechanism is identical:
- **Admission first:** `async with request.app.state.transforms as pool:` acquires a
  transform slot **before** the upstream GET. Pool full → `TransformBusy` →
  `busy_response(...)` (§below).
- **Stream + cap-read:** `read_with_cap(response, cap, on_read=stash_up_in)` (from
  `src/oc_slimapi/transform.py`), wrapped by `read_upstream_response(...)` in
  `_catalog_common.py:88-134`. Cap metric = decompressed logical bytes.
- **Cap field name:** the binding cap is `Settings.max_response_bytes`
  (`src/oc_slimapi/config.py:175`, env `OC_SLIMAPI_MAX_RESPONSE_BYTES`, **default
  `64 * 1024 * 1024` = 64 MiB**).

  > **Note (factual correction vs. the T17 brief):** there is **no**
  > `thin_route_max_response_bytes` field in `config.py`. The actual cap field reused by
  > every catalog/sessions/messages thin route is `max_response_bytes` (global). The only
  > per-route cap variant that exists is `questions_max_response_bytes` (`config.py:330`,
  > for the `/slimapi/questions` fan-out). This design reuses the global
  > `max_response_bytes` like the sibling routes — no new config field is proposed.

- **Cap exceeded behaviour:** `read_with_cap` returns `None` → route returns **413**
  `{"code":"response_too_large","limit":<cap>}` (`sessions.py:70-74`;
  `_catalog_common.py:196-201`).
- **Busy behaviour:** transform-pool admission timeout → **503**
  `{"code":"transform_busy"}` + `Retry-After: 2` (`_catalog_common.py:42-50`).
- Like the todo route, the 64 MiB cap is far above any realistic children body, so 413 is
  defense-in-depth, not a practical path.

---

## 5. Fallback

Per the v2 contract, an unknown/unsupported `/slimapi/**` route falls through to the
catch-all reverse proxy, which returns **404 `{"code":"thin_route_not_found"}`**
(`src/oc_slimapi/proxy.py:130`). Therefore:

- **If this thin route is NOT deployed** (older sidecar, or this proposal is rejected),
  the client's `GET /slimapi/sessions/{sid}/children` request receives 404
  `thin_route_not_found`, and the client **falls back** to the legacy passthrough path
  `GET /session/{sid}/children` (always exists on opencode upstream; reverse-proxied
  byte-for-byte by the catch-all).
- **Client routing decision (recommended):** capability detection (probe once, cache the
  404 `thin_route_not_found` as "unsupported"), then route to thin when supported else to
  legacy passthrough. This is the established pattern for every additive re-add
  (`CLIENT_CHANGES.md:64-75` catalog, `:92-94` directories, `:42-44` questions).
- **Special note for this route (v2 removal context):** ocdroid **already** stopped
  consuming the v1 children cache-coherence machinery per the v2 migration instruction
  (`v2-contract.md:488`). If ocdroid today fetches children at all, it is doing so via the
  plain passthrough `GET /session/{sid}/children` (which is exactly what T16 row #2
  measured). Re-adding a **stateless** thin route does not resurrect any v1 code path on
  the client — it is a fresh, additive target that ocdroid can opt into via capability
  detection, with no change to its current (post-v2) behaviour when unsupported.
- **Zero-regression guarantee:** older ocdroid keeps hitting the legacy path through the
  catch-all; behaviour is byte-identical to today.

---

## 6. Wire classification

### 6.1. Classification + the v2-removal reconciliation (the key call)

- **Additive new `/slimapi/**` route relative to the CURRENT v2 surface.** It does **not**
  modify any existing route, does **not** change `X-Slimapi-Version` (**stays 2**), does
  **not** bump the wire contract.
- **Contract authority — additive-change rule** (`docs/specs/v2-contract.md:43`):
  > "bump 规则：整数，仅破坏性变更 bump；加性变更同版本。"
  and (`v2-contract.md:19`):
  > "所有加性变更**不 bump `X-Slimapi-Version`** 除非另行说明。"
- **Reconciliation with the v2 removal:** the v1 `/slimapi/sessions/{sid}/children` route
  is **gone** from the v2 surface (`v2-contract.md:74`, `:488`). Re-adding a route at that
  path is therefore **net-new relative to v2**, not a modification of an existing v2
  endpoint. The classification is identical to the two documented re-add precedents:
  - `/slimapi/sessions/status` — removed lite-v2, re-added 2026-08-03, **additive, no bump**
    (`v2-contract.md:14`, `:73`).
  - `/slimapi/questions` — removed lite-v2, re-added 2026-08-05, **additive, no bump**
    (`v2-contract.md:12`, `:72`).
  Both were "reverse a prior lite-v2 deletion" and both were classified additive with
  `X-Slimapi-Version` unchanged at 2. A children re-add follows the same rule.

### 6.2. What this design explicitly does NOT resurrect (the guardrail)

To stay genuinely additive and not silently undo the v2 simplification, this design
proposes a **stateless** route and **explicitly excludes** every piece of the v1
cache-coherence machinery that v2 removed:
- **NO** `X-Children-Version` response header.
- **NO** `childrenVersion` digest field (on the parent's `session.digest`).
- **NO** `childrenIDs[]` / `childrenComplete` list hints on `/slimapi/sessions`.
- **NO** per-key cache, **no** single-flight, **no** `children_cache.py`.
- **NO** `session.created` SSE handler invalidating a children cache.

The route is a plain read: upstream `GET /session/{sid}/children` → cap-read → project via
`skeleton_session()` → gzip → 200. Nothing more. This is the same posture as the
re-added `/slimapi/sessions/status` (stateless read + merge, no cache).

### 6.3. Why this still especially needs user approval
Although the wire classification is "additive, no bump", this route **reverses a prior
deliberate v2 decision** (delete the children surface). The two prior re-adds
(status, questions) each had a **concrete forcing reason**: status re-added to carry the
turn-token fence merge (`v2-contract.md:14`); questions re-added to fix a real
cold-start bug (cross-directory pending-question visibility, `v2-contract.md:12`). For
children, the forcing reason here is **traffic cost** (T16 row #2: 2nd-largest unslimmed
read) — which is a weaker forcing function than a correctness bug. **This asymmetry is why
the approval gate (§below) is mandatory and why this doc does not assume approval.**

---

## 7. Test design (design level — NO test code here)

If implemented, the route would be tested by mirroring the existing sessions-list test
suite (`tests/test_sessions_routes.py`) — the element-projection + array-envelope shape is
identical. Established assertions to replicate (design-level, no code):

1. **Happy path + projection:** fake upstream returns a `Session.Info[]` with the heavy
   fields populated (`cost`, `tokens`, `location`, `subpath`); assert the thin route
   returns 200 with each element projected by `skeleton_session()` (heavy fields dropped,
   `id`/`title`/`agent`/`time`/etc. kept). Reuse the exact projection the list route tests
   already validate.
2. **gzip negotiation:** with `Accept-Encoding: gzip` assert `Content-Encoding: gzip` +
   `Vary: Accept-Encoding` and decodable body; without it (or `gzip;q=0`) assert no gzip.
   Mirror `make_project_and_pack`'s `accepts_gzip` use (`_catalog_common.py:162`).
3. **Cap behaviour:** oversized upstream body (> `max_response_bytes`) → 413
   `{"code":"response_too_large", ...}`. Mirror
   `test_sessions_list_oversize_body_returns_413` (`test_sessions_routes.py:208`).
4. **Busy / Retry-After:** transform pool saturated → 503 `{"code":"transform_busy"}` +
   `Retry-After: 2`.
5. **Error mapping (sid-aware):** upstream 404 → `session_not_found` (per-session route,
   mirrors the messages-route sid mapping); other upstream 4xx → 502 `upstream_http_N`;
   upstream 5xx / network / mid-stream read error / bad JSON / non-list body → 503
   `upstream_unavailable`. Mirror
   `test_sessions_list_upstream_4xx_returns_502` / `..._404_returns_502_upstream_http_404`
   (with the sid-aware variant), `..._5xx_returns_503`, `..._mid_stream_read_error_returns_503`,
   `..._bad_json_returns_503`, `..._non_array_json_returns_503`, `..._scalar_element_list_returns_503`
   (`test_sessions_routes.py:74-401`).
6. **`directory` param:** validated via `validate_directory`
   (`..`/control-char/overlong → 400 `invalid_directory`), forwarded as
   `X-Opencode-Directory` header (per-session routing, like the messages route).
7. **Empty array:** upstream returns `[]` → 200 `[]` (no projection crash, gzip skipped on
   tiny body — confirm against the implementation's gzip threshold).
8. **Route↔INTERFACE_MAP gate (REQUIRED companion change, NOT in this doc):**
   `scripts/check.sh` runs `scripts/check_routes_doc.py`, which enforces that **every**
   `/slimapi` route is registered in `docs/specs/INTERFACE_MAP.md`. A future
   implementation plan MUST add the new route to INTERFACE_MAP (one row, mirroring the
   `/slimapi/sessions` list row at `INTERFACE_MAP.md:23`) — and, per `v2-contract.md:72-73`
   precedent for re-adds, **update the `v2-contract.md:74` line** to strikethrough
   `~~GET /slimapi/sessions/{sid}/children~~` and note the additive re-add (exactly as was
   done for status `:73` and questions `:72`). These two doc edits are non-negotiable
   companions; they are explicitly out of scope for this design doc.

---

## Approval gate (T17-C3)

**This design requires explicit user approval before any separate implementation plan is
opened.** This document is a **proposal**, not an implementation:

- No implementation code is produced here (T17-C4).
- No modification to `docs/specs/v2-contract.md`, `docs/specs/INTERFACE_MAP.md`,
  `docs/specs/CLIENT_CHANGES.md`, `CHANGELOG.md`, or any `src/` / `tests/` / `scripts/` /
  `deploy/` file is made by this doc.
- **The approval decision must explicitly weigh §6.3**: re-adding this route reverses a
  prior v2 deletion. The forcing reason is traffic cost (T16 #2), not a correctness bug.
  The user may decide (a) approve the stateless re-add as proposed, (b) prefer the client
  keep using the plain passthrough and instead reduce cost via request-frequency reduction
  (caching/ETag — §Open questions), or (c) reject. All three are legitimate outcomes.
- If approved, a **separate** implementation task would: add the route handler (mirror
  sessions.py), reuse `skeleton_session()`, add the INTERFACE_MAP row + v2-contract
  strikethrough note (§7.8), add tests (§7), and record the additive re-add in CHANGELOG.
  None of that happens here.

---

## Open questions / risks

1. **The v2 removal reconciliation (primary risk).** This is the single biggest judgement
   call of T17. The design reconciles it by proposing a **stateless** route (§6.2) and
   classifying it additive-by-precedent (§6.1, status/questions re-adds). But the forcing
   reason here is traffic, not correctness — weaker than the two precedents. The user
   approval gate (§above) is where this is decided. **Risk:** approving the re-add without
   explicit acknowledgement that it reverses a v2 decision would be a silent contract
   drift; this doc makes the reversal explicit to avoid that.
2. **Is body slimming the right lever, or is it request frequency?** Like the todo route,
   the T16 numbers (3,393 reqs / 3d ≈ 1,131/day) suggest the client polls children
   frequently. Most responses are `[]` (empty — §0 caveat), so body slimming only helps
   the non-empty tail (~0.25-0.35 MiB / 3d). A caching/ETag/conditional-GET design could
   shrink the 3,393-request volume itself, which may dwarf the per-response saving. **The
   "body slimming vs. request-frequency" tradeoff should be decided before implementing.**
3. **Projection field-set assumption.** The design assumes ocdroid's children list view
   needs the same fields as `/slimapi/sessions` list (keeps `id`/`title`/`agent`/`model`/
   `time`/`summary`/`revert`; drops `cost`/`tokens`/`location`/`subpath`). If ocdroid
   actually shows per-child cost/token totals, the projection must widen. Confirm against
   ocdroid's actual children UI when the implementation plan is opened.
4. **`parentID` is redundant in a children response** (every element's `parentID` equals
   the path `sid`). The existing `skeleton_session()` keeps `parentID` (it is in
   `SESSION_KEYS`); for a children-specific projection one *could* drop it, but reusing
   `skeleton_session()` unchanged is simpler and the ~12 B/element saving is negligible.
   Recommend reuse over a bespoke projection.
5. **Children cache invalidation was the v1 value proposition.** A stateless route means
   the client must accept stale children (no invalidation signal). The client already
   accepted this in v2 (the cache is gone); the thin route does not make it worse. But the
   design should not implicitly promise freshness it cannot deliver.
