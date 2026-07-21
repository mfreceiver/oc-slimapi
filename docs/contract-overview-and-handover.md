# oc-slimapi 契约总览与交接（ocdroid ↔ slimapi）

> **截至 2026-07-21（rev F 落地后）。** 本文是 ocdroid 客户端与 oc-slimapi 契约相关的**全部内容汇总**，供交接/转交用。详细论据见 §八参考文档。
>
> 一句话现状：契约主体已成熟；ocdroid v0.11.7 反馈 4 项中 **§1 / §3 / §4 已在 slimapi 实现并文档化**（wire 仍为 1）；**§2 partId 已 ratify**（placeholder 保留）；ocdroid 侧「展开失败」靠 message-level 替换 + 建议迁 G6，不阻塞本批 slimapi 部署。

---

## 一、执行摘要

| 议题 | 客户端诉求 | slimapi 侧状态 | 阻塞 ocdroid？ |
|---|---|---|---|
| **§1** `/sessions` 完整性 + readiness + roots | 加完整性头、`roots` 默认 `True` | **已实施**（三头 + readiness；`roots` 默认不动；`start` 语义勘误） | 否（已 `roots=true+limit=500` 兜底；可消费三头） |
| **§2** partId 稳定性 + G6 迁移 | thin skeleton 返真实 partId（去 placeholder） | **已 ratify**（schema-valid 下跨端点稳定；placeholder 保留为 message-level 兜底；去 placeholder 转入 backlog） | **是（客户端）**——「展开失败」待 ocdroid 迁 G6 + message-level 替换 |
| **§3** reconfigure 主动失效信号 | 新 SSE 帧 `server.reconfigured` | **已实施**（`reason:"discovery_changed"`；resync 重连不动） | 否（本地 token gate 兜底；消费新帧可降脆弱性） |
| **§4** `/health` schema 回显 | min/max 回显 | **已实施**（`schema.version/clientMin/clientMax`） | 否 |

**优先级**：§1、§3 已落地（高）；§2 主要等 ocdroid 侧迁移（中）；§4 已落地（低）。

**门控状态**：设计 v6 + 实现经 **rev-bgpt** Gate-1 9.5 / Gate-2 9.7 PASS；`./scripts/check.sh --full` → **247 passed**。全程**纯加性、不 bump `X-Slimapi-Version`（维持 1）**。移交报告：`docs/ocmar/reports/2026-07-21-v0.11.7-feedback-handoff.md`。

---

## 二、背景与契约基线

- **契约**：oc-slimapi v1（`docs/v1-contract.md` rev **F**），请求头 `X-Slimapi-Version:1`（wire 门禁，必带），fail-closed。
- **演进原则**：**加性变更不 bump version**；破坏性变更才 bump。本次 4 项均为加性。
- **客户端**：ocdroid（Android），v0.11.7 / v0.11.8 已发版。
- **fail-closed 双侧**：ocdroid 本地 `SlimCommitToken`；slimapi 侧 version 门禁。

---

## 三、已闭环（契约成熟，仅确认）

| 项 | 状态 | 说明 |
|---|---|---|
| G1 `session.error` / `digest.lastError` 三态 | ✅ 已落地 | ocdroid 已消费 |
| G6 批量展开 `/full?ids=` | ✅ 服务端已落地 | **ocdroid 尚未迁移**（仍走单条 `/full/{mid}` 404 fallback） |
| `/since` tie-break + cursor drain | ✅ 已 ratify | Gap1/3 闭环 |
| q/p `scope.directories` 三态 | ✅ 已落地 | ocdroid 已消费 |
| 错误体 `{code}` 统一 | ✅ 已落地 | circuit breaker 友好 |
| **rev F §1 sessions 三头** | ✅ **已实施** | 见契约 §2 / CLIENT_CHANGES |
| **rev F §3 `server.reconfigured`** | ✅ **已实施** | `discovery_changed`；resync 不动 |
| **rev F §4 health schema** | ✅ **已实施** | version/clientMin/clientMax |
| **rev F §2 partId ratify** | ✅ **文档 ratify** | placeholder 保留；去 placeholder backlog |

---

## 四、待推进 / 客户端跟进

### §1 sessions 三头 — slimapi 已实施

- 200：`X-Complete` / `X-Discovery-Directories` / `X-Discovery-Ready`
- `X-Complete` **禁止**当权威全集；`start` = 时间戳水位非 offset；`roots` 默认 false（应显式 `roots=true`）
- **客户端**：消费三头；`limit=500` 可保留但勿盲猜全集

### §2 partId — slimapi 已 ratify；ocdroid 修「展开失败」

- thin/`/full` partId 稳定（schema-valid）
- `thin_placeholder_*` → **message-level 整体替换**（`partId.startsWith("thin_placeholder_")`）
- 建议迁 G6 batch

### §3 reconfigured — slimapi 已实施

- `server.reconfigured{reason:"discovery_changed", at}`
- resync 路径**未改**（无双重 cold-start）
- 连接建立期 `connected`+Last-Event-ID `resync` **SHOULD** once-latch
- **客户端**：消费 reconfigured → 作废 token + cold-start

### §4 health schema — slimapi 已实施

- `schema:{degraded,version,clientMin,clientMax}`；诊断用，非 feature discovery

---

## 五、ocdroid 客户端侧待跟进表

| 跟进项 | 依赖 | 说明 |
|---|---|---|
| 消费 sessions 三完整性头 | §1 已上线 | 去掉「盲猜全集」 |
| **G6 batch + placeholder message-level 替换** | §2 ratify | **修「展开失败」** |
| 消费 `server.reconfigured` + single-flight coalescing | §3 已上线 | cold-start；同连接合并 reconcile |
| 可选读 health schema 三键 | §4 已上线 | 诊断 |

---

## 六、优先级与决策点（已裁定）

1. §1 + §3：**已实施**（不翻 roots；reason=`discovery_changed`；resync 不动）。
2. §2：去 placeholder **backlog**；ocdroid 先做 message-level 替换。
3. §4：**已实施**。
4. Wire 维持 **1**。

---

## 七、设计与门控状态

- **设计稿 v6**：`docs/ocmar/specs/2026-07-21-ocdroid-v0.11.7-feedback-design.md`
- **Gate-1**（方案）：rev-bgpt **9.5 PASS**
- **实现**：fixer-mm；**247 tests** green
- **Gate-2**（实现）：rev-bgpt **9.7 PASS**
- **移交**：`docs/ocmar/reports/2026-07-21-v0.11.7-feedback-handoff.md`

---

## 八、参考文档

| 文档 | 内容 |
|---|---|
| `docs/v1-contract.md` | 契约正文（rev F） |
| `docs/ocdroid-v0.11.7-contract-feedback.md` | 客户端原始反馈 |
| `docs/ocmar/specs/2026-07-21-ocdroid-v0.11.7-feedback-design.md` | 设计 v6 |
| `docs/ocmar/reports/2026-07-21-v0.11.7-feedback-handoff.md` | **本批移交报告** |
| `docs/CLIENT_CHANGES.md` | 客户端影响清单 |
| `docs/INTERFACE_MAP.md` | 端点实现映射 |
| `CHANGELOG.md` [Unreleased] | 行为变更条目 |
