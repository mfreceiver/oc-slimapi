# 透传接口收敛 + 代码重构 终审报告

> **日期**：2026-07-22  
> **基线**：slimapi **v0.3.1**（已发，Opt-A partial-envelope 落地）/ wire `X-Slimapi-Version: 1` / 契约 rev F  
> **数据来源**：近 7 天 slimapi access log（93k 入站请求）+ source IP 归因 + opencode 源码（exp-1）  
> **Lane 产出**：explorer 端点梳理(exp-1) · rev-glm 初版汇总(rev-1) · oracle children 设计(ora-1) · oracle 代码审查(ora-2) · **rev-gpt 综合终审(rev-3)**  
> **目的**：识别 catch-all 透传接口的收敛机会 + 代码冗余/拆分改进，整合成统一批次化实施路线。

---

## §1 摘要（TL;DR）

- **透传巨头**：`GET /session/{sid}/children` 75,065 次/7d（占透传流量绝对多数），根因是客户端列表刷新时对每个 session 各拉一次 children（N× 放大）。
- **归因**：流量双源——ocdroid 经 Tailscale（`100.105.94.5`=android `mar-15sp`）明文直连 `0.0.0.0:4097`（children 5,803）+ 本机 `127.0.0.1`（children 69,262，进程身份未最终坐实，疑 omni-orch 编排）。qq-ocbot 不走 slimapi（配 4096 直连 opencode）。
- **真正值得动工**：① status/active 降频（slimapi **零改动**，立即可做）；② children 投影（主收益）。
- **代码审查**：ora-2 发现多项 P1（messages.py/hub.py过大、batch status 缺错误映射致 500 风险、max_json_bytes dead code、Registry 读 TransformPool 私有字段等），rev-gpt 已逐条源码核实。
- **统一顺序**：Batch 0（A 立即）→ Batch 1（错误边界修复）→ Batch 2（hub 最小拆分）→ Batch 3（children 端点）→ Batch 4（childrenVersion 失效）→ Batch 5（G6 shape/tombstone/清理）。
- **全程无 wire bump**；加性契约变更就地标注 `v1-contract.md`。

---

## §2 数据基线与归因

### 2.1 透传接口频次（近 7 天，已归一化动态 id）

| 端点 | 7d 总量 | ocdroid / 本机 拆分 | 分类 |
|---|---|---|---|
| `GET /session/{sid}/children` | **75,065** | 5,803 / 69,262 | PROJECT（巨头） |
| `GET /session/status` | 3,740 | 866 / 2,876 | THROTTLE |
| `GET /api/session/active` | 3,038 | 694 / 2,346 | THROTTLE |
| `GET /session/{sid}/todo` | 771 | 191 / 580 | PROJECT |
| `GET /session/{sid}/diff` | 601 | 145 / 456 | KEEP |
| `GET /question` | 131 | 12 / 119 | MIGRATE |
| `/config/providers`·`/command`·`/agent`（各） | 126 | — | KEEP（启动期元数据） |
| `/file`·`/file/content`·`/session`·`/global/health` | <60 | — | KEEP/MIGRATE |
| `PATCH /session/{sid}`（184）·`POST /session/{sid}/prompt_async`（61）·`POST /session`（7）·`summarize`（1） | — | — | KEEP（写路径，catch-all 透传） |
| `POST /question/{qid}/reply` | 1 | **0 / 1**（本机） | MIGRATE（未走 routeToken） |

### 2.2 归因结论（source IP 实测）

- `100.105.94.5`（13,146 次）= Tailscale android 设备 `mar-15sp` = **ocdroid** → 经 Tailscale 明文直连 `0.0.0.0:4097`（G-ACL 姿态，stunnel 14097 mTLS 入口**未部署**）。
- `127.0.0.1`（79,691 次）= 本机客户端；30 次 ss 密集采样未抓到 ESTAB（连接极短促）；qq-ocbot 配 `127.0.0.1:4096` 不走 slimapi；**本机 69k 进程身份未最终坐实**（疑 omni-orch 编排，待 SO_PEERCRED 坐实）。
- ocdroid 双路：Tailscale→4097（slimapi 省流读）+ LAN `192.168.3.251`→14096→4096（直连回退）。
- **收益口径**：children "75k→1.5k" 是含本机 69k 的乐观上界；ocdroid 保底 = 5.8k→~0.12k（仍 -98%）。沟通须区分两套数字。

### 2.3 上游事件源码确认（exp-1）

| 问 | 结论 | 证据 |
|---|---|---|
| fork 子会话事件 | fork **只发 `session.created`**（子，含 parentID），**不发 `session.updated`**，父会话无修改 | `packages/opencode/src/session/session.ts:693-734` → `createNext:537` |
| `session.status` 覆盖率 | **完整**：idle/busy/retry/cancel/abort 全经 `SessionStatus.set` 发 `session.status` | `packages/opencode/src/session/status.ts:39-48` + processor/run-state/prompt |
| `/api/session/active` 归属 | v2 协议端点，`Record<sid,{type:"running"}>` = `/session/status` 的 `busy` **子集** | `packages/protocol/src/groups/session.ts:146-155` |

---

## §3 三项产出要点

### 3.1 Lane A — status/active 降频（已写入 `CLIENT_CHANGES.md` 末尾节）
- slimapi **零改动**；ocdroid 停 4s 轮询，改 cold-start 一次 `/slimapi/sessions/status` + SSE digest `status` 增量接力；断连降级轮询 10–30s。
- 收益：ocdroid 1,560 次/7d 轮询归零（省电+弱网稳定）；本机 5,222 次/7d upstream QPS 归零。

### 3.2 Lane B（ora-1）— children 投影架构设计
- **端点**：组合——(B) 权威 `GET /slimapi/sessions/{sid}/children`（缓存+single-flight）+ (A) `/slimapi/sessions` 列表加 `childrenIDs[]`/`childrenComplete` hint（仅 ID，超预算省略）。
- **缓存**：键 `(parentSid, normalizedDir)`；粒度=完整 child skeleton 数组；TTL 30s（空负缓存 5–10s）；**generation 防旧 fetch 覆盖新失效**。
- **失效**：X 主（hub 监听 `session.created`，子有 parentID → 失效父 cache + 推父 digest `childrenVersion` 单调计数）+ Y 兜底（客户端收 `session.created` 主动刷父）。
- **全加性不 bump wire**；§2/§3/§5/§7 契约变更；与 M-1 去 placeholder **分批发版**。

### 3.3 Lane C（ora-2）— 代码冗余/拆分审查（rev-gpt 已源码核实）

| C 项 | rev-gpt 判定 |
|---|---|
| ⑤ `max_json_bytes` dead code | **属实**（`config.py:26` 仅定义，生产无引用；生产用 `max_response_bytes`/`max_message_bytes`） |
| ③ 批量 status 缺 RequestError/raise_for_status/shape 校验（500 风险） | **属实**（`sessions.py:197-210` 裸 `get().json()`；单条 status 有完整守卫） |
| ④ messages 初始 send 缺 RequestError 捕获 | **部分属实**（list/since/full 初始 send 可冒泡；G6 per-mid 与 full 中途读已捕获） |
| ⑪ Registry 读 TransformPool `_semaphore._value/_waiters` 私有字段 | **属实**（`hub.py:800-805`） |
| ⑨ G6 合法 JSON 非 MessageWithParts 未映射 envelope error | **属实**（`messages.py:707-719` 仅捕获 JSONDecodeError/ValueError；§14.4 已承认） |
| ⑩ deleted 后无 tombstone，迟到 error 重建 sticky | **属实**（entry 被清理后 `setdefault` 重建 `deleted=False`） |
| ⑥/⑦/⑧ gzip 重复·normalize_directory 职责泄漏·discovery 重复 normalize | 属实（维护性） |
| P2：假 async·未用 request 参数·测试过大·双 teardown·get(directory) 忽略 | 属实/部分（维护性，非运行风险） |

---

## §4 rev-gpt 终审：风险深审（要点）

1. **children single-flight 竞态**：generation 检查能防"旧 fetch 覆盖新失效"，但**不足以证明完整生命周期正确**——还需：in-flight 任务与 cache entry 分离、异常广播所有 waiter、单 waiter 取消不取消共享 task（shield）、shutdown 取消并 await 所有 fetch、key normalize 一致性。
2. **`childrenVersion` 重启归零**：客户端必须按 SSE connection/server generation 处理；`server.connected` 是 generation reset；重连 cold-start，**不得跨进程比较 version 大小**（否则新实例版本永远 <旧值，永不刷新）。
3. **batch status 500 风险**（C③）：违反契约 §7 统一 coded error 纪律；位于 A 冷启动关键路径；严重度 🟠，应修。
4. **deleted 无 tombstone**（C⑩）：entry 清理后迟到 error 经 `setdefault` 重建 sticky；已删除 session 错误复活；严重度 🟠。
5. **G6 shape 500**（C⑨）：上游合法 JSON 非 MessageWithParts → `KeyError`/`TypeError` 穿透 500；违反 batch "mid 级失败仍 200" 承诺；🟠。
6. **A 收窄**：CLIENT_CHANGES A 节"retry/cancel/abort 均经 session.status"是**上游事件事实**，但**契约 §3 digest `status` wire 只承诺 `idle|busy`**——客户端不得把 digest status 当完整状态机枚举，未知值应保守处理（重拉或 fallback）。

---

## §5 统一批次化路线图（核心）

| Batch | 范围 | slimapi 改动 | 前置 | 联合窗口/wire | 风险 |
|---|---|---|---|---|---|
| **0** | **A: status/active 降频**（客户端停轮询+SSE接力+断连fallback） | **零** | exp-1 已绿灯 | 无 / 无 bump | 🟡 SSE 重连/watermark |
| **1** | **错误边界修复**：batch status 补 RequestError/raise_for_status/shape guard；messages 初始 send 补 503；统一 upstream JSON/shape 错误 helper（为 B 复用） | 修复 | Batch 0 | 一致性修复，无 bump | 🟠 可能暴露客户端原始 body 依赖 |
| **2** | **hub 行为锁定测试 + 最小拆分**（subscriber/digest/classifier/registry 边界；TransformPool 私有字段改公开 `snapshot_metrics()`） | 重构（行为保持） | Batch 1 | 纯内部，无契约变更 | 🟠 hub 回归面大 |
| **3** | **children 权威端点 + 缓存**（B 端点 + per-key cache + single-flight + generation + shutdown 清理） | 新增 | Batch 1/2 | **需联合窗口**（契约§2/§5/§7），无 bump | 🔴 并发竞态/stale |
| **4** | **session.created 失效 + digest `childrenVersion`**（hub 监听→失效父cache→推 version；Y 兜底） | 新增 | Batch 3 | 加性 wire，无 bump；同窗口发布 | 🔴 跨重启 version 误判 |
| **5** | **G6 shape + deleted tombstone + 低风险清理**（max_json_bytes dead code、normalize_directory 抽公共模块、假 async、未用参数、测试拆分） | 修复+清理 | children 稳定 | G6 加性错误收敛，无 bump | 🟡 维护性为主 |

**顺序铁律**：Batch 1（错误边界）**必须先于** Batch 3（children）——否则 children 会复制错误映射缺口；Batch 2（hub 拆分）行为保持，先于 Batch 4（改 hub 接 session.created）。

---

## §6 Top 5 最值得先做 + 延后项

**Top 5**：
1. **修复 batch status 错误映射**（C③）— A 冷启动关键依赖，违反 §7，🟠
2. **落地 A status/active 降频**（Batch 0）— 零 slimapi 改动，省电省流立竿见影，🟡
3. **children single-flight/generation/shutdown 测试模型**（B 前置）— B 核心正确性，🔴 但先写测试
4. **hub 最小拆分 + 消除 TransformPool 私有耦合**（C⑪）— B 必改 hub，先稳边界，🟠
5. **修复 G6 shape 500 + deleted tombstone**（C⑨⑩）— 兑现 batch partial-failure 语义，🟠

**延后/不做**：messages.py·hub.py 大规模拆分（先稳定边界，不以行数为目标）；gzip 完全合并；测试拆分；Opt-A 配置重分组；双 teardown；`get(directory)` 参数清理；directory_allowlist 结构调整；skeleton/transform 边界。

---

## §7 残留不确定项（实施前需坐实）

1. **本机 69k 进程身份**（决定 children 收益是 ocdroid-only 还是含本机）— 用 `SO_PEERCRED`（loopback socket option）临时 access log 坐实。
2. 上游 `session.created` payload 是否稳定含 `parentID`。
3. 上游 children 端点排序/非 list/坏 shape 行为。
4. `session.status` 实际状态集合 vs digest wire 是否需过滤为 `idle|busy`。
5. batch status value shape 是否应严格校验。
6. deleted entry 清理时机（决定 tombstone 必要性）。
7. children shutdown 是否有统一 app lifespan task registry。
8. 客户端是否已有 "server generation" 概念（处理 childrenVersion 重启归零）。

---

## §8 产出索引

| Lane | 产出 | 位置 |
|---|---|---|
| explorer | 透传端点行为+gap 梳理 | exp-1（会话） |
| rev-glm | 初版汇总报告（含 source IP 归因、双轨、优先级） | rev-1（会话） |
| oracle | children 投影架构设计 | ora-1（会话） |
| oracle | 代码冗余/拆分审查 | ora-2（会话） |
| **rev-gpt** | **综合终审（事实校验+风险+批次路线图）** | **rev-3（本报告 §3–§6 整合源）** |
| Lane A | status/active 降频客户端清单 | `docs/CLIENT_CHANGES.md` 末尾节 |

## §9 实施计划（已批准全做 · 测试/开发独立 · 第三方验证）

> **分工纪律**（用户指定）：**测试=fixer**（基于契约/规格独立先写，TDD）；**开发=fixer-zlm**（独立实现）；**特别困难=fixer-bgpt** 兜底；每 Batch 验证=**`_priv-verifier` fresh rerun**；全部完成=**rev-bgpt/rev-gpt** 终审。测试与开发并行启动，互不依赖实现细节。

### 9.1 ocdroid 配合改造清单

| # | ocdroid 改造点 | 关联 | slimapi 前置 | 时机 |
|---|---|---|---|---|
| **OC-1** | 停 4s 轮询 `/session/status`+`/api/session/active`；cold-start 改 `/slimapi/sessions/status` 全量；消费 SSE digest `status`（按 `busy` 推 `active`，未知值保守重拉）；断连降级轮询 10–30s + 重连 cold-start | Batch 0 | 无（slimapi 零改动） | **立即可做** |
| **OC-2** | 列表刷新消费 `/slimapi/sessions` 的 `childrenIDs`/`childrenComplete` hint；缺失/`false` 时调 `/slimapi/sessions/{sid}/children`；**停透传** `/session/{sid}/children` | Batch 3 | children 端点上线 | Batch 3 联合窗口 |
| **OC-3** | 消费 digest `childrenVersion`：仅同 server generation 内比较；`server.connected`/`resync` 后 cold-start 清版本基线；**禁止跨进程比大小**；Y 兜底（收 `session.created` 子→主动刷父，reconciler 去重） | Batch 4 | childrenVersion 上线 | Batch 4 联合窗口 |
| **OC-4** | 双轨迁移：`/question`→`/slimapi/questions`；`POST /question/{qid}/reply`→`/slimapi/questions/{qid}/reply`（routeToken）；`/global/health`→`/slimapi/health`；`/session`→`/slimapi/sessions` | 独立 | 无（端点已存在） | 随时（reply 1 次经查来自本机） |
| **OC-5** | 适配：G6 envelope shape error code（若新增）；digest `status` 未知值保守处理 | Batch 5/收窄 | slimapi 修复后 | 跟随 |

### 9.2 任务级拆分 + 分工

| Batch | 写域 | fixer 测试 | fixer-zlm 开发 | 困难→fixer-bgpt | 验证 |
|---|---|---|---|---|---|
| **0a 本机坐实** | catch-all 临时 log | — | SO_PEERCRED 临时 access log + 观测归因 + **还原** | — | orchestrator reconcile |
| **0b status 降频** | （本仓无码） | — | — | — | ocdroid OC-1 联调 |
| **1 错误边界** | `sessions.py`/`messages.py`/新 `upstream_errors.py` | 错误矩阵（**xfail 待开发**）：网络→503、4xx→502、5xx→503、坏JSON/非dict→503、shape guard | 补 RequestError+raise_for_status+shape guard+抽 `fetch_json_mapped()` | — | _priv-verifier |
| **2 hub 拆分** | `sse/hub.py`/`transform.py` | **行为锁定测试**（当前 pass，拆分后保持）：digest/deleted/error/subscriber/registry/metrics/backpressure | 拆 subscriber/digest/classifier/registry（行为保持）+ TransformPool→`snapshot_metrics()` | — | _priv-verifier + SSE 回归 |
| **3 children 端点** | 新 `routes/sessions_children.py`+`children_cache.py`/`sessions.py` hint/契约 | 端点契约 + **并发测试**（single-flight 合并/generation 防覆盖/waiter 取消不取消共享/shutdown 清理/key normalize） | 端点+per-key cache+single-flight+generation+shutdown | **fixer-bgpt**（并发最难） | _priv-verifier + rev-gpt 并发深审 |
| **4 childrenVersion** | `sse/hub.py`/契约§3 | 失效测试：fork→父 cache 失效+version 递增；旧 fetch 不覆盖；重启 generation reset；Y 兜底去重 | hub 监听 `session.created`→失效父 cache→推 `childrenVersion` | fixer-bgpt（hub 事件+重启语义） | _priv-verifier + rev-gpt |
| **5 G6/tombstone/清理** | `messages.py`/`hub.py`/`config.py`/新 `directory.py` | G6 shape 矩阵；deleted 迟到 error 测试 | G6 shape→envelope；deleted tombstone；`max_json_bytes` 清理；`normalize_directory` 抽公共；假async/未用参数 | — | _priv-verifier |

### 9.3 并行关系

```
T0 ┌─ 0a 本机坐实(fixer-zlm, 临时+还原)           ┐
   ├─ 0b status降频(ocdroid OC-1, 本仓无码)        ├─ 三者完全并行
   └─ Batch1测试(fixer) ‖ Batch2测试(fixer)        ┘   （写域不冲突）
T1  Batch1开发(zlm) ‖ Batch2开发(zlm)   ← 并行(sessions/messages vs hub/transform)
T2  Batch3契约先定 → Batch3测试(fixer) + Batch3开发(fixer-bgpt 缓存并发)
T3  Batch4测试(fixer) + Batch4开发(fixer-bgpt/zlm)   依赖 Batch3
T4  Batch5(G6 shape 待Batch1; tombstone 待Batch2; 清理独立)
T5  全部完成 → rev-bgpt/rev-gpt 终审 + _priv-verifier 全量 fresh rerun + ocdroid OC-1~5 联调
```

**铁律依赖**（不可并行）：Batch 1（错误 helper）**先于** Batch 3（复用 helper）；Batch 2（hub 稳定）**先于** Batch 4（改 hub）；Batch 3 **先于** Batch 4。

### 9.4 验证策略

- **每 Batch**：fixer 测试绿（xfail 去 mark 或行为锁定保持 pass）+ `_priv-verifier` fresh rerun（清 `__pycache__`、`./scripts/check.sh` EXIT=0/FAILURES=0）。
- **并发敏感（3/4）**：额外 rev-gpt/rev-bgpt 并发深审（generation/shutdown/waiter 语义）。
- **全部完成**：rev-bgpt 或 rev-gpt 终审（对照 §4 风险逐项闭环）+ ocdroid OC-1~5 联调。

> **T0 已启动**：本机坐实（fixer-zlm）+ Batch 1 测试（fixer）+ Batch 2 测试（fixer）并行。
