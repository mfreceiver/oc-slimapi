# V4 Semantic Expand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use ocmar-subagent-driven-development (recommended) or ocmar-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 v4 消息骨架升级为可渲染预览 + 精确按需展开模型，同时冻结 v3 wire、消除 merged 过取，并为终态内容提供安全的条件缓存。

**Architecture:** 用显式 `ProjectionPolicy` 隔离 v3/v4 投影。v4 wire 将完整、预览、折叠三种表示分开，并把 UI 展开资格、可渲染性、全文完整性拆成独立谓词；`/full` 与精确 merged 维持 owner 级权威，fragment 消费在客户端身份三件套完成前后置。所有 304 均在当次上游 fresh fetch 后重新计算，不建立 sidecar body cache。

**Tech Stack:** Python 3.12、FastAPI、httpx、orjson、pytest/pytest-asyncio、SQLite `mode=ro` dbaux、Kotlin ocdroid 协作契约。

## Global Constraints

- v3 wire 必须逐字节冻结；`GET /slimapi/messages/**?v=3`、v3 expandRefs 与 v3 ETag domain 均不得变化。
- v4 prefix 固定为 **512 UTF-8 bytes**，是 wire 常量，不读取环境变量。
- 用户↔LLM `text` 与 v4 `compaction` 全文内联；reasoning、tool output/error 采用预览态。
- 用户未点选的消息不得因 exact merged 带出全文。
- sidecar 禁止写 opencode SQLite；session 单查只能经 dbaux 只读路径。
- terminal ETag 只影响响应签发；每个条件请求仍须重新 fetch 上游，禁止以 terminal 为由跳过 fetch。
- fragment generic envelope、`data:null=consumed-empty` 和客户端 requestId/CAS/PartKey 三件套不进本批。
- 每个 Python/契约任务结束必须运行对应 targeted tests；最终必须运行 `./scripts/check.sh`。
- 不提交、不发版；除非用户后续显式要求，禁止运行 `scripts/release.sh`。

---

## Frozen V4 Contract Decisions

### Representation states

| Wire state | Required signals | Meaning |
|---|---|---|
| `inline-full` | 字段存在；对应 path 不在 `omitted`/`truncated`；无对应 expandRef | 当前字段完整 |
| `previewed` | 字段存在且为 ≤512B UTF-8 前缀；`truncated:[path]`；存在 actionable expandRef | 可渲染但非 full-fidelity |
| `folded` | 字段缺失或 null；`omitted:[path]`；仅客户端可承载类别签发 expandRef | 无正文或结构化值，需 owner/full fallback |

- `truncated` 与 `omitted` 共用同一 field-path 词表，例如 `text`、`state.output`、`state.error`。
- full-fidelity gap 是派生谓词，不增加 `fullFidelity` wire 布尔：

```text
gap = nonempty(expandRefs) OR nonempty(omitted) OR nonempty(truncated)
```

- `expandRefs` 是 v4 UI affordance 的唯一权威；`hasFull` 只表示 owner `/full` 可用。
- v4 不为 `part_state_attachments`、`part_snapshot`、object `part_source` 签发 actionable expandRefs；这些值保留 gap 标记并走 `/full` fallback。v3 行为不变。

### Exact merged envelope

```json
{
  "data": ["only explicitly requested message objects"],
  "resolvedIds": ["m1"],
  "unresolvedIds": ["m2"],
  "nextCursor": null,
  "complete": true
}
```

- v4 `mode=merged` 必须携带 `ids`；缺失、空值、id 超长、数量超限均返回 coded 422。
- 限制：最多 100 个 ids；单 id UTF-8 长度 1..128 bytes；去重后按 UTF-8 bytes 升序形成 representation key。
- `data` 仅包含请求集合中的消息，按现有 `time.created ASC` 排序；客户端必须按 `info.id` 索引，禁止按位置关联请求 ids。
- `resolvedIds` 按规范化 ids 顺序；预算/上游错误/跨 sid 不可解析项进入 `unresolvedIds`，并在 `data` 中保留可取得的 skeleton + gap 标记。
- exact merged 不分页：忽略列表窗口不得成为隐式行为；`nextCursor=null`、`complete=true`。

### Session-single and cache decisions

- `/slimapi/session/{sid}?v=4` 保留 ETag，必须使用 `wire_view=4` validator domain。
- dbaux 点查执行 directory allowlist 与 sid↔directory 归属校验；dbaux 不可用且 allowlist 非空时返回 `503 auxiliary_unavailable`。
- terminal 304 必须基于当次 fresh fetch 的新 body 重新 hash；条件请求可 join in-flight singleflight，但不得消费 completed grace。

---

### Task 1: Freeze Contract and V4 Projection Policy

**Files:**
- Modify: `docs/specs/v4-contract.md`
- Modify: `src/oc_slimapi/skeleton.py:28-179,298-546,798-850`
- Modify: `src/oc_slimapi/routes/messages.py:74-134,774-1054`
- Test: `tests/test_skeleton.py`
- Test: `tests/test_skeleton_expand.py`
- Test: `tests/test_v3_etag_domain.py`

**Interfaces:**
- Produces: `ProjectionPolicy`, `V3_POLICY`, `V4_POLICY`, `projection_policy(wire_view: int) -> ProjectionPolicy`
- Produces: `truncate_utf8_prefix(value: str, limit_bytes: int) -> tuple[str, bool]`
- Produces: wire-aware `_expand_ref(..., wire_view: int) -> dict`
- Consumes later: Tasks 2-4 use the same wire-aware projection and field-path vocabulary.

**Acceptance Criteria:**
- `T1-C1`: Existing v3 skeleton/expandRef golden assertions remain byte-identical, including `?v=3` hrefs.
- `T1-C2`: v4 513B reasoning/output/error becomes ≤512B valid UTF-8 prefix + `truncated` + `?v=4` expandRef.
- `T1-C3`: v4 text and compaction remain complete; compaction projection does not deepcopy the full text value.
- `T1-C4`: v4 tool input recognizes string `pattern/query/glob/regex`; non-string values remain folded.
- `T1-C5`: v4 unsupported fragment categories carry a gap but no actionable expandRef.

- [ ] **Step 1: Write failing projection tests**

Add named tests covering exact boundaries and policy separation:

```python
def test_v4_reasoning_513_bytes_is_previewed(): ...
def test_v4_output_utf8_boundary_never_splits_emoji(): ...
def test_v4_compaction_is_full_and_not_marked_gap(): ...
def test_v4_search_input_keys_are_bounded_strings(): ...
def test_v4_unsupported_fragment_has_no_actionable_ref(): ...
def test_v3_expand_ref_href_remains_v3(): ...
def test_v4_expand_ref_href_uses_v4(): ...
```

- [ ] **Step 2: Run tests and verify the v4 cases fail**

Run:

```bash
.venv/bin/pytest tests/test_skeleton.py tests/test_skeleton_expand.py tests/test_v3_etag_domain.py -q
```

Expected: new v4 assertions fail; existing v3 tests pass.

- [ ] **Step 3: Add policy types and UTF-8 prefix helper**

Implement these exact public module-level shapes in `src/oc_slimapi/skeleton.py`:

```python
@dataclass(frozen=True, slots=True)
class ProjectionPolicy:
    wire_view: int
    prefix_bytes: int | None
    reasoning_preview: bool
    tool_preview: bool
    compaction_limit_bytes: int | None
    message_inline_budget_bytes: int | None
    tool_input_keys: frozenset[str]
    actionable_categories: frozenset[str]

def projection_policy(wire_view: int) -> ProjectionPolicy: ...
def truncate_utf8_prefix(value: str, limit_bytes: int) -> tuple[str, bool]: ...
```

`V3_POLICY` must preserve all existing constants/limits. `V4_POLICY.prefix_bytes=512`, `compaction_limit_bytes=None`, `message_inline_budget_bytes=None`.

- [ ] **Step 4: Thread policy through skeleton projection and href generation**

Ensure list projection chooses policy using `wire_view_from_scope(request.scope)` and passes it through worker/offload boundaries. Keep v3 serialized output unchanged.

- [ ] **Step 5: Update the projection contract in the same diff**

Replace `docs/specs/v4-contract.md` §10's “messages 零 v4 差异” statement with the frozen state table and gap invariant above. Capability advertising is deliberately deferred to Task 5, after Tasks 1-4 make every advertised behavior true.

- [ ] **Step 6: Run targeted tests and record diff**

```bash
.venv/bin/pytest tests/test_skeleton.py tests/test_skeleton_expand.py tests/test_v3_etag_domain.py -q
git rev-parse HEAD
git diff --stat
```

Expected: PASS; no v3 golden change.

---

### Task 2: Implement Exact IDs-Only Merged Expansion

**Files:**
- Modify: `src/oc_slimapi/routes/messages.py:356-718,909-1054`
- Modify: `src/oc_slimapi/etag.py`
- Modify: `docs/specs/v4-contract.md`
- Test: `tests/test_messages_merged.py`
- Test: `tests/test_etag.py`
- Test: `tests/test_messages_routes.py`

**Interfaces:**
- Produces: `_parse_exact_merged_ids(raw_values: list[str]) -> tuple[str, ...]`
- Produces: `_canonical_exact_merged_key(sid: str, ids: tuple[str, ...], wire_view: int) -> str`
- Produces: `_pack_exact_merged_envelope(...) -> bytes`
- Consumes: `V4_POLICY` from Task 1.

**Acceptance Criteria:**
- `T2-C1`: v4 merged without valid ids returns coded `422 merged_requires_ids` or `422 invalid_merged_ids`.
- `T2-C2`: response contains only requested ids, never neighboring window messages.
- `T2-C3`: permutations/duplicates of one id set produce the same identity body and ETag.
- `T2-C4`: `data` is `created ASC`; `resolvedIds` is normalized byte-order; `nextCursor=null`; `complete=true`.
- `T2-C5`: v3 merged tests remain unchanged.

- [ ] **Step 1: Add failing exact-merged tests**

```python
async def test_v4_merged_requires_ids_and_returns_coded_422(): ...
async def test_v4_merged_never_expands_unrequested_neighbors(): ...
async def test_v4_merged_permuted_ids_share_body_and_etag(): ...
async def test_v4_merged_cross_session_id_is_unresolved(): ...
async def test_v3_merged_window_semantics_are_frozen(): ...
```

- [ ] **Step 2: Verify failure before implementation**

```bash
.venv/bin/pytest tests/test_messages_merged.py tests/test_messages_routes.py tests/test_etag.py -q
```

Expected: new v4 exact-merged cases fail; v3 cases pass.

- [ ] **Step 3: Implement coded validation and deterministic envelope**

Parse repeated/comma-separated `ids`, reject blank ids, enforce 100/128 limits, deduplicate and UTF-8-byte-sort. Do not reuse `_merged_candidate_pairs()` for v4; it is the v3 window algorithm.

- [ ] **Step 4: Add representation-key isolation**

The validator input must include `wire=4`, `mode=merged`, normalized ids and content coding. Different id sets must not match; permutations of the same set must match.

- [ ] **Step 5: Update v4 contract envelope and error precedence**

Document exact envelope, no-pagination semantics, owner authority, id limits, coded 422 errors and deterministic ordering.

- [ ] **Step 6: Run tests and record diff**

```bash
.venv/bin/pytest tests/test_messages_merged.py tests/test_messages_routes.py tests/test_etag.py -q
git diff --stat
```

Expected: PASS; v3 merged fixtures unchanged.

---

### Task 3: Add Terminal Conditional Caching for Full and Fragment Routes

**Files:**
- Modify: `src/oc_slimapi/routes/messages.py:1099-1598`
- Modify: `src/oc_slimapi/etag.py:87-105,255-295`
- Modify: `src/oc_slimapi/singleflight.py`
- Modify: `docs/specs/v4-contract.md`
- Test: `tests/test_etag.py`
- Test: `tests/test_messages_routes.py`
- Test: `tests/test_skeleton_expand.py`
- Test: `tests/test_messages_coalesce.py`

**Interfaces:**
- Produces: `Cacheability` enum with `TERMINAL`, `LIVE`, `MALFORMED`
- Produces: `message_cacheability(message: dict) -> Cacheability`
- Produces: `fragment_cacheability(message: dict, category: str, part_id: str | None) -> Cacheability`
- Produces: ETag response helper accepting an explicit cache policy instead of hardcoded `no-store`.

**Acceptance Criteria:**
- `T3-C1`: user message full responses are terminal; assistant requires `time.completed`.
- `T3-C2`: tool `completed/error` fragment responses are terminal; `pending/running` and malformed states are no-store.
- `T3-C3`: identity uses strong ETag; gzip uses weak ETag; cross-coding comparisons conservatively return 200.
- `T3-C4`: conditional requests always fresh-fetch; upstream mutation during completed grace returns new 200/body, never stale 304.
- `T3-C5`: a concurrent conditional request may join an in-flight fetch and causes only one upstream GET.

- [ ] **Step 1: Add the terminal/live matrix tests**

```python
async def test_full_user_message_supports_304_after_fresh_fetch(): ...
async def test_full_running_assistant_ignores_if_none_match(): ...
async def test_expand_completed_and_error_are_terminal(): ...
async def test_expand_pending_running_and_malformed_are_no_store(): ...
async def test_conditional_request_bypasses_completed_grace_after_mutation(): ...
async def test_conditional_requests_join_inflight_fetch(): ...
```

- [ ] **Step 2: Run tests and confirm failures**

```bash
.venv/bin/pytest tests/test_etag.py tests/test_messages_routes.py tests/test_messages_coalesce.py -q
```

- [ ] **Step 3: Implement cacheability as pure classification**

Classification must consume the same decoded message that supplies the response bytes. It may decide headers only; it must never select a cached body or skip `_fetch_full_shared()`.

- [ ] **Step 4: Extend ETag helper and singleflight conditional semantics**

Preserve existing identity/gzip behavior. Add an explicit “join running only / bypass completed grace” path for conditional requests; leave unconditional behavior unchanged.

- [ ] **Step 5: Freeze the fresh-fetch invariant in the contract**

Add normative text: terminal is not immutable storage; completed/error upstream parts may later change, and changed bytes must turn a would-be 304 into 200.

- [ ] **Step 6: Run tests and record diff**

```bash
.venv/bin/pytest tests/test_etag.py tests/test_messages_routes.py tests/test_messages_coalesce.py tests/test_skeleton_expand.py -q
git diff --stat
```

Expected: PASS.

---

### Task 4: Unify V4 Session-Single Value Semantics

**Files:**
- Modify: `src/oc_slimapi/dbaux/projection.py:135-362`
- Modify: `src/oc_slimapi/dbaux/lifecycle.py`
- Modify: `src/oc_slimapi/routes/read_groups.py:225-236`
- Modify: `src/oc_slimapi/routes/_read_passthrough.py:157-186`
- Modify: `docs/specs/v4-contract.md`
- Test: `tests/test_sessions_v4_matrix.py`
- Test: `tests/test_sessions_routes.py`
- Test: `tests/test_v3_etag_domain.py`

**Interfaces:**
- Produces: `async fetch_session_by_id(..., sid: str, directory: str | None) -> dict | None`
- Produces: a v4 route branch returning the same `SessionSkeletonV4` value semantics as list projection.

**Acceptance Criteria:**
- `T4-C1`: v4 single and list return equal `project` and `tokens_*` values for the same sid.
- `T4-C2`: directory allowlist and sid↔directory ownership are enforced before returning DB data.
- `T4-C3`: db unavailable + nonempty allowlist returns coded `503 auxiliary_unavailable`.
- `T4-C4`: v4 single retains ETag in `wire_view=4`; v3/v4 validators never cross-match.
- `T4-C5`: v3 single behavior remains unchanged.

- [ ] **Step 1: Add failing matrix tests**

```python
async def test_v4_session_single_matches_v4_list_values(): ...
async def test_v4_session_single_rejects_directory_mismatch(): ...
async def test_v4_session_single_db_unavailable_with_allowlist_is_503(): ...
async def test_session_single_v3_v4_etags_are_isolated(): ...
```

- [ ] **Step 2: Verify failure**

```bash
.venv/bin/pytest tests/test_sessions_v4_matrix.py tests/test_sessions_routes.py tests/test_v3_etag_domain.py -q
```

- [ ] **Step 3: Add a read-only point query and route branch**

Reuse the existing v4 SELECT columns, row-to-record conversion, schema gate and degradation error mapping. Do not create a second SessionSkeletonV4 projector.

- [ ] **Step 4: Make read-passthrough ETag domain wire-aware**

Pass `wire_view_from_scope(request.scope)` instead of `wire_view=3`; keep v3 bytes and validators stable.

- [ ] **Step 5: Update contract directory/degradation matrix**

Add an explicit session-single row covering allowlist, directory ownership, DB unavailable behavior and retained ETag.

- [ ] **Step 6: Run tests and record diff**

```bash
.venv/bin/pytest tests/test_sessions_v4_matrix.py tests/test_sessions_routes.py tests/test_v3_etag_domain.py -q
git diff --stat
```

Expected: PASS.

---

### Task 5: Add Capabilities, Observability, and Client Handoff

**Files:**
- Modify: `src/oc_slimapi/routes/versions.py`
- Modify: `src/oc_slimapi/traffic.py`
- Modify: `src/oc_slimapi/metrics.py`
- Modify: `docs/specs/CLIENT_CHANGES.md`
- Modify: `docs/specs/INTERFACE_MAP.md`
- Modify: `docs/manual/traffic-accounting.md`
- Test: `tests/test_versions_route.py`
- Test: `tests/test_expand_config.py`
- Test: `tests/test_traffic_integration.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Produces: static v4 capabilities matching Task 1/2/3 implementation.
- Produces: `sessionsDegraded` observations with `wireVersion=4` without renaming existing dimensions.

**Acceptance Criteria:**
- `T5-C1`: all v4 capability keys are static and match deployed behavior; `fragmentExpand=false` until the deferred gate ships.
- `T5-C2`: degraded session-single/list metrics identify `wireVersion=4`.
- `T5-C3`: CLIENT_CHANGES instructs ocdroid to pass exact ids, consume by `info.id`, use expandRefs as affordance, and keep fragment consumption disabled pending requestId/CAS/PartKey.
- `T5-C4`: every changed route remains present in INTERFACE_MAP; route-doc consistency check passes.

- [ ] **Step 1: Add failing capability and metric tests**

```python
def test_v4_capabilities_advertise_exact_semantics(): ...
def test_sessions_degraded_metric_has_wire_version(): ...
```

- [ ] **Step 2: Run targeted tests and verify failures**

```bash
.venv/bin/pytest tests/test_versions_route.py tests/test_expand_config.py tests/test_traffic_integration.py tests/test_metrics.py -q
```

- [ ] **Step 3: Implement static capabilities and additive wireVersion dimension**

Do not derive capabilities from runtime DB health or environment variables.

- [ ] **Step 4: Write ocdroid migration instructions**

Document exact `mode=merged&ids=...` usage, the ids-only response envelope, ETag rules and the deferred fragment gate.

- [ ] **Step 5: Run route-doc and targeted checks**

```bash
.venv/bin/pytest tests/test_versions_route.py tests/test_expand_config.py tests/test_traffic_integration.py tests/test_metrics.py -q
.venv/bin/python scripts/check_routes_doc.py
git diff --stat
```

Expected: PASS.

---

### Task 6: Final Contract, Changelog, and Whole-Repository Gate

**Files:**
- Modify: `docs/specs/v4-contract.md`
- Modify: `CHANGELOG.md`
- Modify: `src/oc_slimapi/routes/write_groups.py:317`
- Verify: all files changed by Tasks 1-5

**Interfaces:**
- Consumes: all prior task deliverables.
- Produces: one coherent v4 contract revision and release-ready uncommitted working tree.

**Acceptance Criteria:**
- `T6-C1`: v4 contract contains no remaining “messages zero-difference” claim and no contradictory inline/folded invariant.
- `T6-C2`: CHANGELOG headline states “v4 wire 正式修订（v4 尚无消费方）”; package release target is 4.2.0, wire range remains `(3,4)`.
- `T6-C3`: deferred fragment work is explicitly recorded, not silently implemented.
- `T6-C4`: `./scripts/check.sh` exits 0.

- [ ] **Step 1: Perform spec-coverage and placeholder scan**

Verify every criterion in the matrix below has exactly one owner and the contract contains no unresolved placeholder markers, ambiguous cache behavior or unstated fallback.

- [ ] **Step 2: Update changelog and stale comment**

Record projection, exact merged, terminal cache, session-single and observability as one v4 revision. Remove the obsolete `write_groups.py:317` B0-era comment without changing behavior.

- [ ] **Step 3: Run the full required quality gate**

```bash
./scripts/check.sh
```

Expected: exit 0; pytest and route-doc consistency both pass.

- [ ] **Step 4: Record final evidence (do not commit)**

```bash
git rev-parse HEAD
git status --short
git diff --stat
git diff --check
```

Expected: only intended source/test/docs changes; `git diff --check` exits 0.

---

## Criterion Ownership Matrix

| Criterion ID | Spec requirement | Owner task | Cross-task deps | Verification | Final-only? |
|---|---|---|---|---|---|
| T1-C1 | v3 byte freeze | Task 1 | — | skeleton + v3 ETag tests PASS | N |
| T1-C2 | 512B previewed state | Task 1 | — | UTF-8 boundary tests PASS | N |
| T1-C3 | full text/compaction | Task 1 | — | projection tests PASS | N |
| T1-C4 | search input keys | Task 1 | — | tool input tests PASS | N |
| T1-C5 | unsupported categories no v4 affordance | Task 1 | — | expandRef tests PASS | N |
| T2-C1 | coded ids validation | Task 2 | Task 1 | merged route tests PASS | N |
| T2-C2 | no over-fetch | Task 2 | Task 1 | requested-id test PASS | N |
| T2-C3 | deterministic ids/ETag | Task 2 | Task 1 | permutation test PASS | N |
| T2-C4 | exact envelope ordering | Task 2 | Task 1 | envelope test PASS | N |
| T2-C5 | v3 merged freeze | Task 2 | Task 1 | existing merged suite PASS | N |
| T3-C1 | message terminal classification | Task 3 | Task 1 | full route cache tests PASS | N |
| T3-C2 | fragment terminal classification | Task 3 | Task 1 | expand matrix tests PASS | N |
| T3-C3 | identity/gzip validator model | Task 3 | — | ETag tests PASS | N |
| T3-C4 | fresh-fetch safety | Task 3 | — | grace mutation test PASS | N |
| T3-C5 | in-flight join | Task 3 | — | coalescing test PASS | N |
| T4-C1 | session list/single value equality | Task 4 | — | v4 matrix test PASS | N |
| T4-C2 | directory ownership | Task 4 | — | mismatch test PASS | N |
| T4-C3 | dbaux degradation | Task 4 | — | unavailable test PASS | N |
| T4-C4 | validator domain isolation | Task 4 | — | v3/v4 ETag test PASS | N |
| T4-C5 | v3 single freeze | Task 4 | — | sessions route suite PASS | N |
| T5-C1 | capability truthfulness | Task 5 | Tasks 1-3 | versions test PASS | N |
| T5-C2 | degraded wireVersion | Task 5 | Task 4 | metrics tests PASS | N |
| T5-C3 | ocdroid handoff | Task 5 | Tasks 1-4 | CLIENT_CHANGES manual review | Y |
| T5-C4 | route-doc consistency | Task 5 | Tasks 1-4 | check_routes_doc exits 0 | N |
| T6-C1 | coherent contract | Task 6 | Tasks 1-5 | spec review + full gate | Y |
| T6-C2 | explicit 4.2.0 revision record | Task 6 | Tasks 1-5 | CHANGELOG review | Y |
| T6-C3 | deferred fragment gate explicit | Task 6 | Task 5 | contract/docs review | Y |
| T6-C4 | full repository gate | Task 6 | Tasks 1-5 | `./scripts/check.sh` exits 0 | Y |

## Deferred Follow-up Gate (Not Part of 4.2.0)

Only start after ocdroid implements all three prerequisites:

1. monotonic requestId per `(ownerInfoId, partId)` and stale-response rejection;
2. one CAS reducer validating session/route/fingerprint/connection/active-writing/prior-state/requestId atomically;
3. canonical `PartKey=(ownerInfoId, partId)` throughout model, gateway and reconcile.

Then separately specify generic `FragmentResult`, `data:{field:null}` versus `data:null=consumed-empty`, sparse/field-scoped authority and the full 12-category transaction matrix. Do not infer these semantics during Task 1-6 implementation.

## V4 Session Status Tree Addendum

This addendum extends the V4 plan with the primary/subagent status capability. It is deliberately V4-only and must be confirmed with the WebUI consumer before implementation details are frozen.

### Wire shape

`GET /slimapi/sessions/status?v=4` remains a flat map keyed by session ID, but each value uses the V4 session-status shape:

```json
{
  "ses_primary": {
    "type": "session_status",
    "status": "idle",
    "effectiveStatus": "busy",
    "parentID": null,
    "subagentList": [
      {
        "sessionID": "ses_sub_1",
        "status": "busy",
        "agent": "explorer",
        "title": "分析会话状态能力"
      },
      {
        "sessionID": "ses_sub_2",
        "status": "idle",
        "agent": "fixer",
        "title": "实现 API 修改"
      }
    ]
  },
  "ses_sub_1": {
    "type": "session_status",
    "status": "busy",
    "parentID": "ses_primary"
  }
}
```

### Frozen semantics for WebUI discussion

- `type` is the V4 discriminator and is always `"session_status"`; V3 keeps the upstream-compatible `type: "idle"|"busy"|"retry"` shape.
- `status` is the session's own status. Allowed values remain `idle`, `busy`, and `retry`.
- `effectiveStatus` is present for primary sessions and is the user-facing aggregate: primary plus all known subagents are `idle` only when every one is idle; any `busy` or `retry` makes it `busy`.
- A subagent has `parentID`; a primary has `parentID: null`.
- `subagentList` is present only on primary entries and contains every known direct subagent, including idle subagents. A primary without subagents returns `subagentList: []`.
- Current topology is exactly two levels (`primary -> subagent`), so direct-child aggregation is equivalent to recursive descendant aggregation. No recursive tree field is introduced in this revision.
- Active subagents continue to appear as independent top-level status entries. Idle sessions may be absent from the upstream status map, but known idle subagents must still be materialized inside their primary's `subagentList`.
- If a primary is itself idle but any subagent is active, the primary must still be materialized in the V4 response so `effectiveStatus: "busy"` is observable.
- `agent` is optional and must be `null`/omitted when the upstream/session metadata does not provide a stable agent category. It must never be inferred from `title`.
- `sessionID` is preferred over `ses_id` to match the existing upstream/session wire naming. `parentID` follows the existing skeleton/session field convention.
- `childrenStatus` is not added separately; counts can be derived from `subagentList` and would create a second source of truth.

### Data and degradation contract

- The upstream status map remains the source of each session's own live status; absent entries mean `idle`.
- Session metadata (`parentID`, title, and, where available, agent category) supplies the primary/subagent relationship and list identity. The implementation must not treat the SQLite projection as a source of live status.
- The aggregation must use one coherent observation window. If status and metadata cannot be aligned, the response must mark the tree data degraded or omit only the optional V4 tree fields according to the final WebUI decision; it must not claim `effectiveStatus: "idle"` from incomplete metadata.
- Existing turn fields (`turnIncarnation`, `turn`) remain per-session and are not duplicated into `subagentList` unless WebUI demonstrates a need.

### Capability and implementation boundary

- Advertise a static V4 capability only after the final field names and degraded shape are accepted: `sessionStatusTree`, with sub-capabilities for `parentID`, `subagentList`, `effectiveStatus`, `agent`, and `title`.
- Expected implementation owner: `src/oc_slimapi/routes/sessions.py` status response finalization, plus the existing session metadata/children lookup path; no change to the upstream status service or SQLite write domain.
- Required tests: V3 response golden freeze; V4 discriminator and fields; idle subagent materialization; idle primary with busy subagent; retry treated as busy; independent subagent top-level entry; missing optional agent metadata; metadata/status degradation; stable subagent ordering.
- WebUI consultation must decide before implementation: whether `effectiveStatus` should be named `aggregateStatus`; whether empty `subagentList` is always emitted; whether `agent`/`title` are required or optional; and the exact degraded/error envelope.
