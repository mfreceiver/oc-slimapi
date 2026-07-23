# Token Stream P3–P4 Implementation Plan（本机 oc-slimapi）

> **For agentic workers:** REQUIRED SUB-SKILL: Use ocmar-subagent-driven-development (recommended) or ocmar-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 v0.5.0 已发版+本机部署生效的前提下，按依赖顺序消化本仓 post-release 工程债（P3）与可选产品扩展（P4），不破坏现有 wire / 763 测试绿。

**Architecture:** 先做纯结构拆包（S-1）降低 `token_hub.py` 认知负载；再补观测（S-3a）使调参可证；可选补强 memory-limit 恢复（S-2）；数据驱动调参（S-3b）；最后按产品 go 做加性扩展（F-2 → F-3 → F-1）。

**Tech Stack:** Python 3 / FastAPI / asyncio / zlib 流式 gzip / pytest / `./scripts/check.sh`

**Source of truth:** [`docs/release-v0.5.0-token-stream.md`](../../release-v0.5.0-token-stream.md) §6（细化版 backlog）

**Out of scope（本计划不实施）:**
- **S-4** — ocdroid 仓 flow 级测（跨仓）
- **C-4** — ocdroid 客户端文档（跨仓）
- **P2 V-B / V-A′ / V-M** — 运维/实网实证（非码）
- 生产机 reinstall（若与本机分离）

## Global Constraints

- Wire API：`X-Slimapi-Version` 仍为 `1`；P3 项**不** bump；P4 加性变更评估后仍优先不 bump，破坏性才走正式契约 bump（`docs/release.md`）。
- 改 Python 后必须 `./scripts/check.sh` 通过（当前 = `pytest tests/`）。
- 契约权威：`docs/v1-contract.md`；P4 改 wire 时同步 `CHANGELOG.md` + `CLIENT_CHANGES.md`。
- S-1 **纯结构、零行为**；行为改动另起 task/PR。
- ocmar default：**不 commit**，除非用户明确要求。
- 公开导入路径兼容：`from oc_slimapi.sse.token_hub import TokenStreamHub, TokenStreamRegistry, TokenSubscriber, TokenSubscriberCapacityError, STOP, _resync_frame` 必须继续可用（shim 或 re-export）。

---

## 0. 本机范围与优先级

> **第 1 轮（2026-07-23）已收口：S-1 / S-3a / S-2 ✅ 完成**（commit `7a1861a`；rev-grok lane 评审 + rev-opus 终审 APPROVED + fresh verifier 767 passed）。剩余后续任务见本文末 §6。

| 优先级 | ID | 项 | 估工 | 依赖 | 本机? | 状态 |
|---|---|---|---|---|---|---|
| 1 | **S-1** | 拆 `token_hub.py` → `sse/tokenstream/` | M | — | ✅ | **✅ 已完成 r1** |
| 2 | **S-3a** | 补 metrics（压缩比 / flush 时延 / queue depth） | S–M | 建议 S-1 后 | ✅ | **✅ 已完成 r1** |
| 3 | **S-2** | 方案 B：evict 后重发剩余 live snapshots | S–M | 建议 S-1 后 | ✅ 可选 | **✅ 已完成 r1** |
| 4 | **S-3b** | 数据驱动调参（flush / gzip level / early-flush） | S | **S-3a** + harness 数据 | ✅ 门控 | 待办（门控） |
| 5 | **F-2** | busy-open 占位帧 | S | 产品 go | ✅ 可选 | 待办（产品 go） |
| 6 | **F-3** | 自适应 flush 窗 | M | **S-3a** + 产品 go | ✅ 可选 | 待办（产品 go） |
| 7 | **F-1** | reasoning / tool-input 流式 | L | 产品 go + ocdroid 协调 | ✅ 延后 | 待办（产品 go + 双边） |

```text
S-1 ──┬──► S-3a ──┬──► S-3b   [S-1/S-3a/S-2 ✅ r1 完成]
      │           └──► F-3
      └──► S-2 ✅
F-2（独立，产品 go）
F-1（独立，产品 go + 双边，最后）
```

**推荐默认路径（本机最小有价值序列）：** S-1 → S-3a →（可选 S-2）→（有数据再 S-3b）。F-* 等产品明确 go 再开。

---

## 1. 文件边界（S-1 锁定）

| 路径 | 职责 |
|---|---|
| `src/oc_slimapi/sse/tokenstream/__init__.py` | 包公开 re-export |
| `src/oc_slimapi/sse/tokenstream/frames.py` | SSE 帧序列化 + `STOP` + `_now_ms` |
| `src/oc_slimapi/sse/tokenstream/models.py` | `PartKey` / `LivePart` / `DeltaAccumulator` / `_TokenMetrics` |
| `src/oc_slimapi/sse/tokenstream/budget.py` | 内存预算 + 驱逐 + tombstone（mixin 或 free functions 操作 hub 状态） |
| `src/oc_slimapi/sse/tokenstream/flush.py` | `flush_loop` / `flush` / `flush_sid` / `finish_part` / `ttl_sweep` / resync 队列 |
| `src/oc_slimapi/sse/tokenstream/session.py` | session status/deleted/retire/upstream reconnect |
| `src/oc_slimapi/sse/tokenstream/fanout.py` | attach/detach/fanout/snapshot emit |
| `src/oc_slimapi/sse/tokenstream/ingest.py` | `on_part_updated` / `on_part_delta` |
| `src/oc_slimapi/sse/tokenstream/subscriber.py` | `TokenSubscriber` / `TokenSubscriberCapacityError` / `TokenStreamRegistry` |
| `src/oc_slimapi/sse/tokenstream/hub.py` | `TokenStreamHub` 聚合壳 |
| `src/oc_slimapi/sse/token_hub.py` | **兼容 shim**：`from .tokenstream import *` 公开符号（调用方零改） |
| `src/oc_slimapi/config.py` | 常量仍在此（不搬） |
| `src/oc_slimapi/routes/token_stream.py` | gzip encode 边界（S-3a 压缩比计数点） |
| `src/oc_slimapi/routes/metrics.py` | 已挂 `sse.tokenStream`（S-3a 扩展字段） |

**生产调用方（shim 必须覆盖）：**
- `app.py` → `TokenStreamHub`, `TokenStreamRegistry`
- `routes/token_stream.py` → `STOP`, `TokenSubscriberCapacityError`, `_resync_frame`
- `sse/hub.py` → TYPE_CHECKING `TokenStreamHub`
- tests：`test_token_hub.py` / `test_token_hub_flush.py` / `test_token_hub_lifecycle.py` / `test_token_stream_route.py`

---

### Task 1: S-1 拆包（纯结构，零行为）

**Files:**
- Create: `src/oc_slimapi/sse/tokenstream/{__init__,frames,models,budget,flush,session,fanout,ingest,subscriber,hub}.py`
- Modify: `src/oc_slimapi/sse/token_hub.py` → 薄 shim（re-export only）
- Test: 既有 `tests/test_token_hub*.py` + `tests/test_token_stream_route.py`（不改断言语义）

**Interfaces:**
- Consumes: 现有 `token_hub.py` 全部实现
- Produces: 同名公开 API 经 `oc_slimapi.sse.token_hub` 与 `oc_slimapi.sse.tokenstream` 均可 import

**Acceptance Criteria:**
- `T1-C1`: `./scripts/check.sh` 全绿（测试数 ≥ 当前 763，无 fail）
- `T1-C2`: `python -c "from oc_slimapi.sse.token_hub import TokenStreamHub, TokenStreamRegistry, TokenSubscriber, TokenSubscriberCapacityError, STOP, _resync_frame"` 成功
- `T1-C3`: `token_hub.py` 行数 ≤ 40（仅 re-export + 模块 docstring 指向新包）
- `T1-C4`: 无行为 diff — 不改 `config.py` 常量值、不改 wire 帧形、不改 metrics 既有 key 集合

- [ ] **Step 1: 记录基线**

```bash
git rev-parse HEAD
./scripts/check.sh   # 期望：763 passed（或当前全绿数）
wc -l src/oc_slimapi/sse/token_hub.py   # 期望：1509
```

- [ ] **Step 2: 建包骨架 + 按 seam 剪切**

按 §1 表剪切；推荐实现策略（二选一，优先 A）：

- **A（推荐，低风险）**：先整文件复制到 `hub.py`，再逐步抽出 frames/models/subscriber 等，每抽一层跑一次相关测试。
- **B**：一次按 seam 切开；`TokenStreamHub` 用 mixin 组合 `BudgetMixin`/`FlushMixin`/…（注意 MRO 与私有属性访问）。

**硬规则：** 剪切时保持方法体字节级等价（除 import 路径）；`STOP = object()` 只定义一次（放 `frames.py`），shim 与 subscriber 共用。

- [ ] **Step 3: `token_hub.py` 改为 shim**

```python
"""Compatibility shim — implementation lives in ``oc_slimapi.sse.tokenstream``."""
from .tokenstream import (  # noqa: F401
    STOP,
    LivePart,
    DeltaAccumulator,
    TokenStreamHub,
    TokenSubscriber,
    TokenSubscriberCapacityError,
    TokenStreamRegistry,
    _resync_frame,
    _now_ms,
    # …其余测试/内部需要的符号
)
```

- [ ] **Step 4: 验证 import + 全量测试**

```bash
.venv/bin/python -c "from oc_slimapi.sse.token_hub import TokenStreamHub, STOP, _resync_frame; print('ok')"
./scripts/check.sh
```

Expected: PASS，测试数不降。

- [ ] **Step 5: Record diff（不 commit）**

```bash
git rev-parse HEAD
git diff --stat
```

---

### Task 2: S-3a 观测 metrics

**Files:**
- Modify: `src/oc_slimapi/sse/tokenstream/models.py`（或 hub metrics dataclass）— 增计数器
- Modify: `src/oc_slimapi/sse/tokenstream/flush.py` — flush 时延
- Modify: `src/oc_slimapi/sse/tokenstream/subscriber.py` — queue depth 暴露
- Modify: `src/oc_slimapi/routes/token_stream.py` — gzip raw/compressed 累计
- Modify: `src/oc_slimapi/sse/tokenstream/subscriber.py` `snapshot_token_metrics()` — 新字段
- Modify: `tests/test_token_stream_route.py` `TestMetricsTokenStream` — 扩展 expected keys
- Test: 新增/扩展 metrics 单测

**Interfaces:**
- Consumes: 现有 `snapshot_token_metrics()` dict shape
- Produces: `sse.tokenStream` 加性字段（旧字段保留）：

```text
# 既有（不可删）
current, limit, rejectedTotal, pendingAccumulators,
flushedFramesTotal, droppedFramesTotal, truncatedSnapshotsTotal,
orphanDeltasTotal, tokenMemoryLimitTotal

# 新增（S-3a）
gzipRawBytesTotal          # int, 仅 gzip 连接累计
gzipCompressedBytesTotal   # int
flushDurationMsTotal       # float 累计 ms（或 int 微秒）
flushTicksTotal            # int
maxSubscriberQueueDepth    # int, 当前所有 sub 的 max(qsize)
```

**Acceptance Criteria:**
- `T2-C1`: `GET /slimapi/metrics` 的 `sse.tokenStream` 含全部新 key；旧 key 仍在
- `T2-C2`: 无 gzip 连接时 `gzipRawBytesTotal`/`gzipCompressedBytesTotal` 为 0 或不增
- `T2-C3`: 跑一轮 flush 后 `flushTicksTotal >= 1` 且 `flushDurationMsTotal >= 0`
- `T2-C4`: `./scripts/check.sh` 全绿

- [ ] **Step 1: 写失败测试（扩展 metrics key 集合）**

在 `tests/test_token_stream_route.py` 的 `test_metrics_exposes_sse_token_stream_block` 中把 expected set 扩为含新 key；先跑确认 FAIL。

```bash
.venv/bin/pytest tests/test_token_stream_route.py::TestMetricsTokenStream -v
```

- [ ] **Step 2: 实现计数**

1. `token_stream.py` `encode()`：

```python
raw_n = len(frame)
out = compressor.compress(frame) + compressor.flush(zlib.Z_SYNC_FLUSH)
# bump registry/hub counters: raw_n, len(out)
return out
```

2. `flush_loop` / `flush`：`t0 = time.perf_counter()` … 累加 duration + ticks。
3. `snapshot_token_metrics`：暴露 `max(sub.queue.qsize() for …)`（无 sub → 0）。

- [ ] **Step 3: 测试 PASS + check.sh**

```bash
.venv/bin/pytest tests/test_token_stream_route.py::TestMetricsTokenStream -v
./scripts/check.sh
```

- [ ] **Step 4: Record diff**

```bash
git diff --stat
```

**文档（同 task 折叠）：** 若 `docs/design-token-stream.md` metrics 表存在，加性补新 key 一行；**不**改 `v1-contract.md`（metrics 为运维面，非 wire 客户端契约，除非契约已列死集合——以契约为准）。

---

### Task 3: S-2 方案 B（可选）— evict 后重发剩余 live snapshots

**前置：** Task 1 完成更佳（改 `budget.py` / `fanout.py`）。

**Files:**
- Modify: `…/budget.py` 或 hub 内 `_evict_part_for_memory`
- Modify: `…/fanout.py` — 抽出「对现有 sub 集发 handshake snapshots」辅助（复用 `_emit_snapshot_or_truncated`）
- Test: `tests/test_token_hub_flush.py` 或 `tests/test_token_hub_lifecycle.py` 新增用例

**Interfaces:**
- Consumes: `drop_part`, `_fanout_resync`, `live_parts`, `_subs_by_sid`, `_emit_snapshot_or_truncated`
- Produces: `_evict_part_for_memory` 语义扩展：resync 后对**同 sid 仍 live** 的 part 向**已 attach** 的 sub 发 `snapshot{done:false}`

**Acceptance Criteria:**
- `T3-C1`: 单测：两 live part A/B，驱逐 A → 现有 sub 队列顺序含 `resync{token_memory_limit}` **且** 含 B 的 `snapshot`（`done` 非 true / 无 truncated 除非超限）
- `T3-C2`: 驱逐后对 B 的 delta 仍可被该 sub 消费（非 orphan）
- `T3-C3`: 客户端方案 A（reconnect）仍兼容（B 为加性；不要求改 ocdroid）
- `T3-C4`: `./scripts/check.sh` 全绿

- [ ] **Step 1: 写失败测试**

```python
def test_evict_refans_remaining_live_snapshots_to_existing_subs():
    th = TokenStreamHub()
    # start part A + B on same sid, attach sub, clear handshake frames
    # force _evict_part_for_memory(A)
    # assert resync token_memory_limit present
    # assert snapshot for B present after resync
```

Run: 期望 FAIL（当前只 fan resync）。

- [ ] **Step 2: 最小实现**

在 `_evict_part_for_memory` 中 `drop_part` + `_fanout_resync` 之后：

```python
sid = key[0]
subs = list(self._subs_by_sid.get(sid, ()))
for live_key, part in sorted(
    ((k, p) for k, p in self.live_parts.items() if k[0] == sid),
    key=lambda kv: kv[0],
):
    for sub in subs:
        self._emit_snapshot_or_truncated(sub, live_key, part, done=False)
```

注意：勿对已 drop 的 key 再发；`drop_part` 已从 `live_parts` 移除 A。

- [ ] **Step 3: 测试 + check.sh**

```bash
.venv/bin/pytest tests/test_token_hub_flush.py tests/test_token_hub_lifecycle.py -v
./scripts/check.sh
```

- [ ] **Step 4: Record diff**

**不做（本 task）：** 把 ocdroid `token_memory_limit.triggersReconnect` 改为 false（双边 wire 决策，另立项）。

---

### Task 4: S-3b 调参（门控 — 有数据再开）

**前置：** Task 2 完成；本机或 staging 有 `sse.tokenStream` 样本 **或** 跑通 `scripts/measure_token_overhead.py`。

**Files:**
- Modify（仅在数据支持时）: `src/oc_slimapi/config.py` — `TOKEN_FLUSH_SECONDS` / `TOKEN_FLUSH_BYTES`
- Modify（仅在数据支持时）: `src/oc_slimapi/routes/token_stream.py` — `zlib.compressobj` level
- Test: 既有 + harness

**Gate（全部满足才改常量）：**
1. `measure_token_overhead.py` 报告 median `overhead_x_gzip` 与目标 `≤1.2x` 的差距已知
2. S-3a 的 `gzipCompressedBytesTotal/gzipRawBytesTotal` 与 flush 时延有至少一次真实负载样本
3. 变更有明确假设（例：CPU 高 → level 6→4；小包过多 → flush 100→200ms）

**Acceptance Criteria:**
- `T4-C1`: harness median `overhead_x_gzip` 仍 `≤1.2x`（或书面接受新阈值并更新 harness 注释）
- `T4-C2`: `./scripts/check.sh` 全绿
- `T4-C3`: CHANGELOG 记「运维默认调参」若影响可观察延迟（加性/运维，不 bump wire）

- [ ] **Step 1: 采基线**

```bash
.venv/bin/python scripts/measure_token_overhead.py
# 记录 median overhead_x_gzip、p95 等
```

- [ ] **Step 2: 一次只改一个旋钮**（flush **或** level **或** early-flush），复测对比。
- [ ] **Step 3: check.sh + record diff**

**若门控不满足：** 本 task 标 `skipped — no data`，不改常量。

---

### Task 5: F-2 busy-open 占位（产品 go 后）

**前置：** 产品确认需要；评估 `server.connected` 加字段是否可接受（加性，未知键客户端应 ignore）。

**Files:**
- Modify: `…/frames.py` `_connected_frame` / `attach_subscriber`
- Modify: `docs/v1-contract.md`（加性）+ `CLIENT_CHANGES.md` + `CHANGELOG.md`
- Test: attach 时 sid busy 且无 LivePart → connected 帧含 `busy: true`（或等价）

**Acceptance Criteria:**
- `T5-C1`: busy + 无 live part → 首帧 `server.connected` 带 `busy: true`（字段名以契约为准）
- `T5-C2`: idle 或不 busy → 行为与现网一致（无 busy 或 `busy: false`，二选一写死契约）
- `T5-C3`: 契约 + CLIENT_CHANGES + CHANGELOG 已更新；**不** bump `X-Slimapi-Version`
- `T5-C4`: `./scripts/check.sh` 全绿

- [ ] **Step 1: 契约小段起草（先文档后码）** — 冻结字段名/缺省。
- [ ] **Step 2: 失败测试 → 实现 → check.sh**
- [ ] **Step 3: Record diff**

**默认建议字段：**

```json
{"sessionID":"…","busy":true}
```

仅当 `sid in _busy_sids` 且该 sid 无 `live_parts` 时 `busy=true`；否则省略 `busy`（forward-compat 更干净）或显式 `false`（契约二选一，实现前锁定）。

---

### Task 6: F-3 自适应 flush（产品 go + S-3a 后）

**前置：** Task 2；明确 min/max cadence 护栏。

**Files:**
- Modify: `…/flush.py` `flush_loop` — 动态 `sleep` 间隔
- Modify: `config.py` — `TOKEN_FLUSH_SECONDS_MIN/MAX` 或复用现常量作 default
- Test: 多 sub / 高 queue depth 时 cadence 放宽；空闲收紧（具体策略实现时写死并测）

**建议策略（YAGNI 默认）：**
- `base = TOKEN_FLUSH_SECONDS`（0.1）
- `depth = maxSubscriberQueueDepth`
- `interval = clamp(base * (1 + depth/queue_items), min=base, max=0.5)`
- 不引入 ML / 外部依赖

**Acceptance Criteria:**
- `T6-C1`: 单测可注入假 clock/depth，断言 sleep 间隔在 `[min,max]`
- `T6-C2`: 默认无负载时行为 ≈ 固定 100ms（回归）
- `T6-C3`: `./scripts/check.sh` 全绿

- [ ] **Step 1: 失败测试（interval clamp）**
- [ ] **Step 2: 实现 + check.sh**
- [ ] **Step 3: Record diff**

---

### Task 7: F-1 reasoning/tool-input 流式（延后 — 仅门控清单）

**不在默认执行序列。** 产品 go + ocdroid 协调前 **禁止** 开工。

**现状 grounding：**
- `on_part_updated`：`type != "text"` → `_remember_nontext` + return
- `on_part_delta`：`field != "text"` 或 `_is_nontext` → silent drop

**开工前必须闭合：**
1. 产品：要流哪些 part type（reasoning / tool-input / 其它）？
2. Wire：同事件扩 `partType` vs 新 event name？是否 bump `X-Slimapi-Version`？
3. ocdroid：reducer/UI 是否消费？发版窗口？
4. 内存：非 text 是否计入同一 4+4 budget？

**Acceptance Criteria（仅当开工）：**
- `T7-C1`: 契约 + CLIENT_CHANGES + CHANGELOG 先于实现合并意图
- `T7-C2`: 非 text 不再误入 `_nontext` 静默 drop（按白名单 type）
- `T7-C3`: text 路径零回归（763+ 相关测绿）
- `T7-C4`: ocdroid 联调清单有 owner

---

## Criterion Ownership Matrix

| Criterion ID | Spec requirement | Owner task | Cross-task deps | Verification | Final-only? |
|---|---|---|---|---|---|
| T1-C1 | 拆包后全测绿 | Task 1 | — | `./scripts/check.sh` → PASS | N |
| T1-C2 | 旧 import 路径可用 | Task 1 | — | python -c import → ok | N |
| T1-C3 | shim 薄文件 | Task 1 | — | `wc -l token_hub.py` ≤ 40 | N |
| T1-C4 | 零行为 | Task 1 | — | 无 config/wire 常量改动 + 测绿 | Y |
| T2-C1 | metrics 新 key | Task 2 | Task 1 建议 | metrics 单测 PASS | N |
| T2-C2 | 无 gzip 不灌压缩计数 | Task 2 | — | 单测 | N |
| T2-C3 | flush 时延计数 | Task 2 | — | 单测 | N |
| T2-C4 | 全测绿 | Task 2 | — | `./scripts/check.sh` | N |
| T3-C1 | evict 后重发 B snapshot | Task 3 | Task 1 建议 | 新单测 PASS | N |
| T3-C2 | B delta 不 orphan | Task 3 | — | 新单测 PASS | N |
| T3-C3 | 方案 A 仍兼容 | Task 3 | — | 不改客户端；文档说明 | Y |
| T3-C4 | 全测绿 | Task 3 | — | `./scripts/check.sh` | N |
| T4-C1 | harness ≤1.2x | Task 4 | Task 2 | measure script | N |
| T4-C2 | 全测绿 | Task 4 | — | `./scripts/check.sh` | N |
| T4-C3 | 调参可观察记录 | Task 4 | — | CHANGELOG 行 | N |
| T5-C1–C4 | busy 占位 + 契约 | Task 5 | 产品 go | 测 + 文档 | N |
| T6-C1–C3 | 自适应 flush | Task 6 | Task 2 | 测 + check | N |
| T7-C1–C4 | 非 text 流式 | Task 7 | 产品+ocdroid | 契约+测 | Y |

---

## 2. 执行节奏建议

| Wave | 内容 | 何时 |
|---|---|---|
| **W0** | 本计划评审 / 确认是否做 S-2、是否开 F-* | 现在 |
| **W1** | Task 1（S-1） | 下一编码会话 |
| **W2** | Task 2（S-3a） | S-1 后 |
| **W3** | Task 3（S-2）可选 | 需要降低 reconnect 抖动时 |
| **W4** | Task 4（S-3b） | 有 metrics/harness 数据后 |
| **W5+** | F-2 / F-3 / F-1 | 产品 go 后单独开 plan 细化 F-1 |

**本机「做完即停」定义（工程债最小闭环）：** W1+W2 完成（可维护 + 可观测）。S-2/S-3b/F-* 非必须。

---

## 3. 风险与回滚

| 风险 | 缓解 |
|---|---|
| S-1 切包漏 re-export | shim 保留全符号；全量 pytest |
| S-2 与客户端 reconnect 双恢复 | B 加性；保持 A；不改 triggersReconnect |
| S-3b 盲调 | 无数据不改常量 |
| F-1 范围膨胀 | 强制产品+契约门控 |

回滚：各 task 独立 diff；S-1 可整包回退 shim；行为 task 用 `git diff` 还原单文件。

---

## 4. Self-Review（计划自检）

1. **Spec coverage：** release §6 本机项 S-1/S-2/S-3a/S-3b/F-1/F-2/F-3 均有 task；S-4/C-4/P2 显式 out of scope。
2. **Placeholder scan：** 无 TBD；F-1 为门控清单而非空实现。
3. **Type consistency：** 公开符号名与现网一致；metrics 新 key 驼峰对齐既有 `flushedFramesTotal` 风格。
4. **Acceptance observability：** 每 task 有 `T*-C*` + 命令/测。

---

## 5. 与 release 文档的关系

- Backlog 权威条目仍在 `docs/release-v0.5.0-token-stream.md` §6。
- **本文件** = 本机可执行实施计划（任务拆解 / 依赖 / 验收）。
- 实施完成后回写 release §6 对应 ID 状态为 ✅（与 D-1 同格式）。

---

## 6. 后续任务（第 1 轮收口后的 backlog）

> 第 1 轮（2026-07-23）：S-1 / S-3a / S-2 已完成（commit `7a1861a`）。下表为下一轮可选项，按「先闭合硬前置 → 再门控项 → 最后清理」排序。

### 6.1 硬前置（Method B 产品化前必修）— ✅ O1 已完成（c21ca3b）

| ID | 项 | 来源 | 严重度 | 说明 |
|---|---|---|---|---|
| **O1** ✅ | `_reserve → _evict_part_for_memory` re-entrancy 截断 | rev-opus 终审（新发现，per-task 评审均漏） | Minor → **已修 c21ca3b** | **✅ 已修（main `c21ca3b`）**：`_evict_part_for_memory` 增 `skip_key` 参数，4 个调用点（`_reserve`/`_check_pending_budget`/`_start_part`×2）各传本路径 current key，re-snapshot 循环跳过之；reconnect handshake 在 `triggersReconnect=true` 下恢复 K 锚点（严格优于修前）。回归测 `test_o1_evict_skips_current_key_being_reserved`（768→769 passed）。**残留**：method B 产品化时（flip `triggersReconnect=false`）current-key 锚点需 MB-P-S1 闭合 → 见 R2 计划 [`2026-07-23-ocdroid-cooperation-r2.md`](2026-07-23-ocdroid-cooperation-r2.md)。 |

### 6.2 门控项（满足 gate 才开）

| ID | 项 | gate | 估工 |
|---|---|---|---|
| **S-3b** | 数据驱动调参（flush `TOKEN_FLUSH_SECONDS` 0.1→0.2 / gzip level 6↘ / early-flush `TOKEN_FLUSH_BYTES`） | 先跑 `scripts/measure_token_overhead.py` 采基线 + S-3a metrics 有真实负载样本；harness 目标 `≤1.2x median overhead_x_gzip` 不破 | S |
| **S-4** | ocdroid `TokenStreamCoordinator` 对 `session_idle`/`session_deleted`/`UNKNOWN` 的 flow 级测 + token stream × `/events` digest eviction 顺序测 | 跨仓（ocdroid），非本仓 | M |
| **F-2** | busy-open 占位帧（`server.connected{...,busy:true}`） | 产品 go（加性 wire，评估是否 bump） | S（服务端）+ ocdroid UX |
| **F-3** | 自适应 flush 窗（按 sub 数 / 观测时延动态调 cadence，min/max 护栏） | 产品 go + S-3a metrics | M |
| **F-1** | reasoning / tool-input part 流式 | 产品 go + ocdroid 协调发版 + wire 决策（新 event 或 partType + 是否 bump） | L |

### 6.3 清理项（cosmetic / optional，不阻塞任何事）

| ID | 项 | 来源 | 状态 |
|---|---|---|---|
| **2-M1** | 删 dead field `_TokenMetrics.max_subscriber_queue_depth`（定义未写，snapshot 用 local） | rev-grok (S-3a) | **✅ 已清理 r1**（删字段 + docstring 改述为 live gauge） |
| **2-M2** | `maxSubscriberQueueDepth` value-level 测（put 5 帧 → depth≥+5；unsubscribe → 0） | rev-grok (S-3a) | **✅ 已补测 r1**（`test_max_subscriber_queue_depth_value_level`） |
| **2-M3** | `flushDurationMsTotal >= 0` 断言过弱（恒真）→ 改 strict 增长断言 | rev-grok (S-3a) | **✅ 已改 r1**（direct flush + `after > before` strict） |
| **O2** | `flushTicksTotal` 实际计「所有 flush() 调用」（含 `_check_pending_budget` force-flush）而非「纯 loop tick」 | rev-opus | **✅ 已注释 r1**（hub.py bump 点一行说明） |
| **M1** | `PartKey` 在 `frames.py` 而非 `models.py`（brief map drift） | rev-grok (S-1) | 已接受，不改（reviewer：either fine） |
| **Y1** | `frames.py:16-18` STOP 相对 hub 的注释精度 | rev-grok (S-1) | 已接受，不改 |

> r1 清理后 767 → 768 passed。

### 6.4 下一轮建议序列

1. ~~**O1**~~ ✅ **已完成**（c21ca3b，skip_key）；method B 产品化（MB-P-S1 + flip `triggersReconnect`）转 R2 计划 [`2026-07-23-ocdroid-cooperation-r2.md`](2026-07-23-ocdroid-cooperation-r2.md)。
2. **2-M1 / 2-M2 / 2-M3 / O2**（一波清理 PR，低成本，可顺手）。
3. **S-3b**（采 harness 基线后再决策是否调参；无数据不改常量）。
4. **F-2 → F-3**（产品 go 后）。
5. **F-1**（最后；双边窗口，独立 plan 细化）。

### 6.5 不在本计划范围（提醒）

- **C-4**（ocdroid 客户端文档对齐）、**S-4**（ocdroid flow 测）— 跨仓，本仓不做。
- **P2 V-B / V-A′ / V-M**（运维/实网实证）— 非码。
