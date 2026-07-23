# ocdroid 配合与核实清单：Token 批式 SSE 对接（handoff）

> 本文件给 **ocdroid 侧**做客户端集成对接与联合终审用。  
> 服务端权威：`docs/design-token-stream.md`（v4）+ `docs/v1-contract.md`（rev J，加性，`X-Slimapi-Version` 仍 1）+ `docs/CLIENT_CHANGES.md` §Token stream SSE。  
> **服务端 Stage A–E 已全部 PASS**（763 tests green，2026-07-23）。  
> 双边兼容核验（explorer 读 ocdroid 源码）已完成；本文件 §5 是 **ocdroid 须配合确认/更新** 的清单。

---

## 0. 状态一句话（终态）

oc-slimapi **v0.5.0 已发版 + 部署生效**：push main + tag `v0.5.0` + Gitea Release（2026-07-23）；服务端 reinstall + restart + `/slimapi/health` 自检通过（2026-07-23，`sidecar.version=0.5.0` / `server.api_version=1` / `features.tokenStream=true` / `schema.degraded=false`）。token 批式 SSE（opt-in 实时流，lever1 done:true 无 text + lever2 默认 gzip）上线；全加性 wire，**不 bump** `X-Slimapi-Version`（仍 `1`）。ocdroid 已 push origin（含 `token_memory_limit` 重连修复 commit `1986567`）。

**双边对接契约**：本文 §1 + [`release-v0.5.0-token-stream.md`](release-v0.5.0-token-stream.md) §4（post-fix 权威）。发版/部署/post-release backlog 见该文。

**ocdroid 回传裁定（已收，2026-07-23）**：C-1=**A**、C-2=**修**、C-3=做、C-4=延后；V-A=确认、V-B=客户端无可见性（服务端侧见 §8.3）、V-C=确认。

**联合终审轨迹（2026-07-23，rev-bgpt）**：初裁 **NO-GO 8.4**（`token_memory_limit` clear-only 不重连 → 后续 delta orphan，见 §9）；§9.4 方案 A 修复（`TOKEN_MEMORY_LIMIT.triggersReconnect=true`）后 re-gate **GO 9.7**（见 §11）。详细审查记录见 §8–§11。

---

## 1. 服务端最终 wire（权威，已落地）

| 项 | 值 |
|---|---|
| 端点 | `GET /slimapi/sessions/{sid}/stream`（`directory` query 可选；与 `X-Opencode-Directory` 冲突 → 400 `directory_not_allowed`） |
| 版本 | `X-Slimapi-Version: 1`（**不 bump**，加性） |
| 开关 | `/slimapi/health` **根级** `features.tokenStream === true` 才启用；缺/false → 客户端不得连 stream |
| 帧 | `server.connected{sessionID}` / `server.heartbeat{}`(15s) / `message.part.snapshot{done:false,text}` / `message.part.delta{text}` / `message.part.snapshot{done:true}`（**杠杆1：仅 marker，无 text**） / `message.part.snapshot{truncated:true}` / `resync{reason,sessionID}` |
| 无 replay | **不发 SSE `id:`**；`Last-Event-ID` 值忽略，仅触发首帧 `resync{reconnect_no_replay,sessionID}` |
| **resync reason 实际发出集** | `reconnect_no_replay` / `subscriber_backpressure` / `token_memory_limit` / `session_idle` / `session_deleted` |
| **不发** | `part_too_large`（单 part >1MiB 走 `snapshot{truncated:true}`，**非** resync） |
| gzip | **杠杆2**：token stream **默认 gzip**（流式 Z_SYNC_FLUSH，`Accept-Encoding` 协商）；控制面 `/slimapi/events` **仍不 gzip**（首个 SSE gzip 例外） |
| T3 | 独立账本（不占 `MAX_TOTAL_SUBSCRIBERS`）；admission 满 → 503 `sse_token_subscriber_limit` + `Retry-After:5` |
| 内存 | Option B 拆 4+4：4MiB live + 4MiB pending（不双计）；worst-case 12MiB |
| 终态顺序 | 同 part 所有 `delta` 必先于 `snapshot{done:true}`；`done:true` 后该 part 不再发 delta |
| 权威全文 | **`/since` 幂等凌驾**所有 token 帧（含 done:true marker） |

### 杠杆1（done:true 无 text）— 客户端必须对齐

```text
event: message.part.snapshot
data: {"sessionID":"…","messageID":"…","partID":"…","done":true}
# 无 text 字段
```

客户端 **不得** 用该帧的 text（没有）替换缓冲；应：
1. 标该 part **完成**（停 append）；
2. **保留**已累积的 streamOwned 文本（或至少不清成 `""`）；
3. 等控制面 digest step-finish → `/since` 权威全文覆盖（插件 text-end 改写以 `/since` 为准）。

### 单 part >1MiB

走 `snapshot{truncated:true}`（part 级清 streamOwned + `/since`），**不是** `resync{part_too_large}`。

---

## 2. 服务端阶段完成证据

| Stage | 内容 | 门控 |
|---|---|---|
| A | 地基（token_hub + config + 注入） | 9.5 |
| B | 生命周期（has_consumers / 重连 / session 路由 / TTL） | 9.5 |
| C | flush 引擎 + wire + 内存 + lever1 | 9.5 |
| D | 端点 + admission + lever2 gzip + health/metrics + B-D1 修复 | 9.6 |
| E | 预算 4+4 + 契约加性 rev J + 跨文档 + fold `part_too_large` 虚假契约 | 9.3→fold 净 |

- `./scripts/check.sh`：**763 passed**（控制面 188 零回归）。
- 评委池（服务端后续）：**rev-bgpt**（用户指定；弃 grok，glm 仅 Stage E 用过）。

---

## 3. 双边兼容核验结果（explorer 读 ocdroid 源码，2026-07-23）

| # | 检查点 | 结果 | 严重度 |
|---|---|---|---|
| 1 | done:true 无 text → 不取 text、保留累积 / 走 /since | **风险** | 中 |
| 2 | truncated:true 处理 | **兼容** | — |
| 3a | resync 处理（识别到的 reason 泛化清态） | **兼容** | — |
| 3b | `session_deleted` / `session_idle` 未识别 → 帧静默丢弃 | **缺口** | 低 |
| 3c | `part_too_large` 死码 | **兼容（无害）** | — |
| 4 | gzip 解码（OkHttp 默认 Accept-Encoding:gzip） | **兼容且 lever2 生效** | — |
| 5 | 端点 + 版本 + 无 replay | **兼容** | — |
| 6 | health 根级 `features.tokenStream` 门控 | **兼容** | — |
| 7 | 生命周期（前台 opt-in / 后台断 / 独立于 /events） | **兼容** | — |

**总评**：可进联合终审；§5 两项须 ocdroid 决策/修复后再标「双边齐」。

---

## 4. 风险详解（#1 done:true 空白窗口）

ocdroid 当前路径（证据见核验）：

```text
服务端: snapshot{done:true}          // 无 text
    ↓
TokenStreamReducer.reduceSnapshot
    → text = frame.text ?: ""        // null → ""
    → state = DONE
    → effects = []                   // 无 TriggerSinceFetch
    ↓
bridge → streamingPartTexts[partId] = ""
    ↓
MessageCard: streaming.takeIf { it.isNotEmpty() } → null → 空白
    ↓
(0–2s 后) digest step-finish → /since 权威覆盖 → 文本出现
```

设计意图是「动画让位于 `/since` 真值」，但 **done:true → authoritative 覆盖之间无安全网**。若 digest 迟/丢，用户可见空文本框。

---

## 5. ocdroid 须配合确认 / 更新（请回传）

### 5.1 必须决策（联合终审前）

| ID | 项 | 建议 | 请确认 |
|---|---|---|---|
| **C-1** | **done:true 空白窗口（风险）** | 三选一（或等价）：**(A)** done:true 时 **保留** 最后累积 text（`frame.text ?: lastAccumulated`，不写 `""`）；**(B)** done:true 时发 `TriggerSinceFetch(authoritative=true)`；**(C)** 接受窗口空白，依赖 digest 可靠性（须书面接受） | 选 A/B/C + 若修则改哪些文件 |
| **C-2** | **`session_deleted` / `session_idle` 未识别（缺口）** | 在 `ResyncReason` 加 `SESSION_DELETED("session_deleted")`、`SESSION_IDLE("session_idle")`，`triggersReconnect=false`；未知 reason **勿静默丢弃**（建议 fallback：清态 + /since，与已知 reason 同路径） | 是否修 + 是否改「未知 reason 静默丢」策略 |

### 5.2 建议清理（不阻塞终审）

| ID | 项 | 建议 |
|---|---|---|
| **C-3** | `part_too_large` 死码 | 从 `ResyncReason` 移除（服务端永不发）；保留 `truncated:true` 路径即可 |
| **C-4** | 文档对齐 | ocdroid `docs/token-stream-dev-plan.md` / client design 与本 handoff §1 对齐（lever1 marker 无 text、reason 集 5 个、无 part_too_large） |

### 5.3 请确认（事实，非改码）

| ID | 项 | 服务端假设 | 请确认 |
|---|---|---|---|
| **V-A** | 控制面 digest step-finish → `/since` 是否 **可靠** 覆盖 done:true 后的 part？ | 是（独立连接、独立退避） | 是否有已知丢帧/迟帧场景？ |
| **V-B** | stunnel mTLS 对 token stream 长连是否缓冲 / idle 断？ | 透明；服务端 15s heartbeat | 生产路径实测是否 OK？ |
| **V-C** | 客户端 OkHttp 默认 gzip 是否在 **所有** 构建变体保持开启？ | lever2 已生效 | 有无自定义 Client 禁用 transparent gzip？ |

### 5.4 回传格式（建议）

```text
C-1: A|B|C + 简述
C-2: 修/不修 + 未知 reason 策略
C-3/C-4: 是否做
V-A/V-B/V-C: 确认/补正
联合终审：ready | blocked(原因)
```

回传目标：本仓主会话（或用户指定 session）。服务端侧 **无阻塞码**；等 ocdroid 落地 C-1(A)+C-2 并标 ready 后开联合终审（评委 **rev-bgpt**）。

---

## 6. 联合终审范围（双方齐后）

1. wire 帧对照（§1 vs ocdroid reducer/parser）— 尤其 lever1 / truncated / resync 5 reason。  
2. 空白窗口（C-1=A 落地）与 session_deleted/session_idle（C-2 落地）+ 未知 reason 不静默丢。  
3. 零回归：服务端 763 + ocdroid 既有测试绿。  
4. 评委：**rev-bgpt**（用户指定）。  
5. 通过后：服务端可走 `scripts/release.sh`（wire 加性已记 CHANGELOG；不 bump `X-Slimapi-Version`）。

---

## 7. 历史（设计期，已归档）

早期 V1–V7 核实与 Stage-0 联合门控见 git 历史 / `docs/design-token-stream.md` §15。  
本文件以 **落地后 wire + 双边核验 residual** 为准；与旧 handoff 冲突时以 §1 + §5 + §8 为准。

---

## 8. ocdroid 回传裁定（2026-07-23，已收）

### 8.1 必须决策

| ID | 裁定 | 证据 / 范围（ocdroid 自报） | 服务端动作 |
|---|---|---|---|
| **C-1** | **A**（保留已累计 buffer，**禁写 `""`**） | blank window 成立：`TokenStreamReducer.kt:153-161`（done 分支 `text=frame.text?:""` 零 effect）→ `TokenStreamCoordinator.kt:550-560` → `StreamingBufferFieldsReducer.kt:81-90`（`streamingPartTexts[partId]=""`）→ `ChatMessageRow.kt:154,636`。~5–15 LOC + 1 测。**B 可作 A 的可选补强，不能替代 A** | **无**（客户端修） |
| **C-2** | **修** | `ResyncReason` 仅 4 值（`TokenStreamFrame.kt:205-209`），缺 `session_idle`/`session_deleted`；未知 reason `fromWire→null→parse` 整帧静默丢（:156-158）。加 2 枚举 + **ANY 未知 reason** → 清态 + authoritative `/since`（不静默丢、不 reconnect）。`session_deleted` 优先走既有 EvictSession/close。~30–60 LOC | **无**（客户端修） |

### 8.2 建议清理

| ID | 裁定 | 说明 |
|---|---|---|
| **C-3** | **做** | 删死码 `part_too_large`（枚举+测）；服务端永不发，超限走 `truncated:true`（reducer :145-151） |
| **C-4** | **做** | 漂移：`token-stream-client-design.md:106`（4-reason）、`dev-plan.md:180/187`、design V6/§5.1「SSE 不 gzip」vs lever2 |

### 8.3 事实确认

| ID | 裁定 | 说明 |
|---|---|---|
| **V-A** | **确认（带边界）** | done:true 不自身触发 `/since`；靠 digest reconcile。边界：part DONE 但 session busy 时 skeleton merge 不保 DONE overlay；终态依赖 idle/resync authoritative。无已知永久丢完成态路径；未做 live 掉帧实证 |
| **V-B** | **客户端无可见性** + **服务端侧结论** | ocdroid 无 stunnel 专用逻辑；mTLS 仅握手；`readTimeout(0)` + watchdog。缓冲/idle 断属 server/stunnel。**服务端侧**：token stream 发 `server.heartbeat` 每 **15s**（`TOKEN_HEARTBEAT_SECONDS`，design §5.6 / 契约 §3.x）+ 响应头 `X-Accel-Buffering:no` / `Cache-Control:no-cache,no-transform`，专防代理 idle-timeout 断静默流。stunnel 为 TLS 终结（`requireCert=yes`），**不**做 HTTP 体缓冲（应用层 SSE 直透 sidecar）。生产路径 `:14097` mTLS 已自测 thin 路由；**长连 idle 断的最终实证**仍建议运维在生产路径抓一次 ≥30s 静默流（应见 15s heartbeat 保活）。客户端 45s watchdog（3×15s）与服务端心跳对齐 |
| **V-C** | **确认** | 全仓库无 `Accept-Encoding`/gzip 覆盖；所有 variant 走 OkHttp 默认透明 gzip。lever2 客户端 match；`/events` 不压是服务端策略 |

### 8.4 Wire 要点核对（ocdroid 自报）

| 项 | 状态 |
|---|---|
| lever1 done 语义 | **partial** — C-1=A 落地后对齐 |
| resync 5 reason | **mismatch** — 缺 2 + 静默丢（C-2 硬阻塞） |
| `part_too_large` 死码 | C-3 清理 |
| lever2 gzip | **match**（客户端） |
| health `features.tokenStream` | **match**（dual-read fail-closed） |

### 8.5 联合终审门闩

**GO**（2026-07-23 re-gate rev-bgpt **9.7/10**）— 见 §9 / §10 / §11。

| 门闩 | 状态 |
|---|---|
| C-1(A) blank window | **PASS** `d4b22da` |
| C-2 session_idle/deleted + 未知 reason 不静默丢 | **PASS** `d4b22da` |
| C-3 删 part_too_large 死码 | **PASS** |
| C-4 docs | 延后，不阻塞 |
| 服务端 763 | **PASS** |
| **token_memory_limit 恢复协议** | **PASS**（方案 A + re-gate 9.7） |
| CLIENT_CHANGES 两档 resync | **PASS** |

评委：**rev-bgpt**。

---

## 9. 联合终审结果（rev-bgpt，2026-07-23）

### 9.1 裁决

| 项 | 值 |
|---|---|
| 裁决 | **NO-GO** |
| 分数 | **8.4 / 10** |
| 服务端 check | 763 passed（终审前 re-check） |
| ocdroid commit | `d4b22da` |

**一句话**：C-1/C-2/C-3 与 wire 帧对照均 PASS；`token_memory_limit` 的 clear-only 策略与服务端「不重发 handshake snapshot」状态机冲突，实时流在该 resync 后永久失效至下次重连。

### 9.2 检查摘要

| 区 | 结果 |
|---|---|
| A wire（端点/lever1/5 reason/truncated/gzip/health） | **PASS** |
| B C-1 保留 buffer / 零 effect | **PASS** |
| C C-2 idle/deleted/UNKNOWN 清态+/since 不 reconnect | **PASS**（语义本身） |
| D C-3 删 part_too_large | **PASS** |
| E V-B / C-4 / 未 live 实证 | 不单独阻塞 |

### 9.3 阻塞项（唯一硬阻塞）

**`token_memory_limit` resync 后状态恢复协议不一致**

| 步 | 行为 | 证据 |
|---|---|---|
| 1 | 服务端 LRU 驱逐**一个** LivePart，向该 sid **所有** subscriber 扇 `resync{token_memory_limit}`；**不断开**连接、**不**对现有 sub 重发 handshake snapshot | `token_hub.py:970-982`；snapshot 仅 `attach_subscriber` `778-814` |
| 2 | 客户端清该 sid **全部** stream overlay + authoritative `/since` | `TokenStreamReducer.kt:217-244` |
| 3 | `TOKEN_MEMORY_LIMIT.triggersReconnect == false` → **不重连** | `TokenStreamFrame.kt:254-255` |
| 4 | 后续仍在生成的 part 只发 **delta**；无 STREAMING 锚点 → reducer **orphan 静默丢** | `TokenStreamReducer.kt:197-208` |

`/since` 只能恢复当时已持久化内容，**不能**重建 token stream 的 snapshot 锚点。

**session_idle / session_deleted 的 clear-only 可接受**（服务端已 retire live parts / 会话终态；digest 独立 eviction）。  
**token_memory_limit 不可 clear-only**（流仍在继续，且只驱逐一个 part）。

### 9.4 推荐修复（ocdroid 主修，~5 LOC + 测）

**方案 A（推荐，对齐 CLIENT_CHANGES「resync → 重订阅」）**：

```text
ResyncReason.triggersReconnect:
  true  ← reconnect_no_replay | subscriber_backpressure | token_memory_limit
  false ← session_idle | session_deleted | UNKNOWN
```

- 重连 → `attach_subscriber` 重发剩余 LivePart 的 `snapshot{done:false}` 锚点。
- 补测：`token_memory_limit` → ClearPartState + TriggerSinceFetch + **Reconnect**。
- 同步 ocdroid 注释 / 可选 C-4 文档。

**方案 B（服务端补强，可选）**：`_evict_part_for_memory` 扇 resync 后，对**现有** subscriber 重发该 sid 剩余 live parts 的 handshake snapshot（使 clear-only 也可恢复）。更复杂；若 A 落地则 B 非必须。

**文档**：修复后更新 `docs/CLIENT_CHANGES.md` §resync — 明确两档恢复：

| reason | 清态 + /since | 重订阅 stream |
|---|---|---|
| `reconnect_no_replay` / `subscriber_backpressure` / `token_memory_limit` | 是 | **是** |
| `session_idle` / `session_deleted` | 是 | 否（socket 仍可用；无更多 token 或会话已删） |
| 未知 reason（客户端 UNKNOWN） | 是 | 建议否（与 idle 同保守路径；或与 A 一并 true 更安全） |

### 9.5 发版门闩（修复后重开终审）

1. ocdroid 落地 §9.4 方案 A（或等价 B）+ 测绿  
2. 本仓同步 CLIENT_CHANGES resync 两档表（加性文档，不 bump wire）  
3. 可选：rev-bgpt 快速 re-gate 仅 §9.3 路径  
4. PASS 后：服务端 `scripts/release.sh`（不 bump `X-Slimapi-Version`）；客户端随修复 commit 出货  

C-4 全文档对齐 / V-B 生产长连实证 / `token_hub` 拆包 → **post-release**。

---

## 10. §9.4 方案 A 落地（2026-07-23，re-gate 对象）

### 10.1 ocdroid（工作区，基于 `d4b22da`）

| 文件 | 变更 |
|---|---|
| `TokenStreamFrame.kt` | `triggersReconnect` 含 `TOKEN_MEMORY_LIMIT`；注释说明 orphan 风险 |
| `TokenStreamFrameTest.kt` | 断言 memory limit → reconnect；idle/deleted/UNKNOWN 仍 false |
| `TokenStreamReducerTest.kt` | `token_memory_limit` → Clear + Since + **Reconnect** |
| `TokenStreamCoordinatorTest.kt` | flow 泵送 resync → 实际 `openCount` 增加；null-sid 改用 SESSION_IDLE |

```text
triggersReconnect:
  true  ← reconnect_no_replay | subscriber_backpressure | token_memory_limit
  false ← session_idle | session_deleted | UNKNOWN
```

### 10.2 oc-slimapi 文档

- `docs/CLIENT_CHANGES.md` §resync：改为**两档恢复表**（与 §9.4 一致）。
- 本 handoff §8.5 / §10。

### 10.3 re-gate 范围（rev-bgpt）

仅复审 §9.3 阻塞路径是否闭合：

1. `TOKEN_MEMORY_LIMIT.triggersReconnect == true`  
2. reducer 发 `Reconnect`；coordinator 实际重连  
3. idle/deleted/UNKNOWN 仍 clear-only  
4. CLIENT_CHANGES 两档表与代码一致  
5. 无回归 C-1/C-2/C-3  

**不**重开全量 wire 审（A 项已 PASS）。

---

## 11. re-gate 结果（rev-bgpt，2026-07-23）

| 项 | 值 |
|---|---|
| 裁决 | **GO** |
| 分数 | **9.7 / 10** |
| 阻塞闭合 | `token_memory_limit` → Clear + `/since` + **实际 SSE 重连** → `attach_subscriber` 重建 snapshot 锚点；后续 delta 不再 orphan |
| 服务端 | 763 passed；wire 未改（方案 A = 客户端恢复策略 + 文档） |
| 客户端 | 工作区基于 `d4b22da`；Frame/Reducer/Coordinator 测绿（含 openCount 实连） |

### 发版

- **服务端**：可 `scripts/release.sh`（**不** bump `X-Slimapi-Version`）
- **客户端**：可随本修复 commit 出货
- **post-release**：C-4 文档 / V-B 生产长连实证 / `token_hub` 拆包
