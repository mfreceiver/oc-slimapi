# Slim 会话消息可靠性联合计划（本仓协作索引）

> 联合计划主文档位于 ocdroid 仓库：
> `docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md`（1097 行）。
> 本文件记录 oc-slimapi 侧交付物、冻结接口、门控状态和暂停点，避免跨仓协作时丢失上下文。

**状态：** `D-GATE PAUSED — 等待新会话执行`  
**门控：** `rev-gpt 9.5/10 PASS`  
**日期：** 2026-07-26

## 1. 双方阶段顺序

### 阶段 A：双方自身独立发布

双方先各自完成不依赖对方新版本的安全性和状态保护，并分别完成自己的测试、版本和发布证据。

oc-slimapi 已完成的独立交付物：

- `/since/{ts}` 按 opencode v1.18.4 页内 oldest-first 整页过滤；
- `X-Since-Complete: true|false` 加性响应头；
- SSE per-session immediate flush；
- `archived` 整数类型防护（保留 0、拒绝 bool）；
- session ID 提取不再使用 GlobalBus `payload.id`；
- `/full` 非法/空 JSON 及 body 中断统一为 503 `upstream_unavailable`；
- full strip in-place，移除不必要的整树 deepcopy；
- 契约、设计、接口追踪、客户端说明、CHANGELOG 和上游版本锚点同步。

已验证：

```text
./scripts/check.sh → 1022 passed
路由↔INTERFACE_MAP → 19 条 /slimapi 路由一致
git diff --check → 通过
```

当前这些改动仍是未提交工作树；commit/tag/artifact SHA-256/check log 需用户明确授权后按项目发布流程生成。

### 阶段 B：双方版本发布后联调

阶段 B 只有在以下能力冻结后才开始：

- `since-complete` capability 检测与旧 sidecar 兼容行为；
- `SlimDrainOutcome` 成功/截断/超时/部分结果语义；
- bookmark/localApplied/dirty/authoritative cache 的提交规则；
- watermark 字段及 `updated > 0L && id.isNotBlank()` 统一规则；
- full/cursor snapshot merge 的 union 或 replacement 语义；
- token done/idle/resync 与 provisional 内容保留语义。

## 2. 冻结的安全语义

- 客户端 `/since` 初期只 staging，不推进 `localApplied`，不清 dirty，不写 authoritative cache。
- `SlimDrainOutcome.Success` 只表示 cursor-null terminal；cap、partial、timeout 不推进 bookmark。
- authoritative commit 只承诺进程内 token-guarded commit，不承诺跨重启恢复。
- 集合 merge 复用现有 `MessageWithParts`，不新增第二套业务模型。
- 普通 resync、backpressure、reconnect、idle 不等于删除，不得清除唯一可见内容；删除须有明确确认。
- owner 必须由用户明确指定；orchestrator 不自行授权实施联合任务。

## 3. 联调验收矩阵

- slim + SSE 开启、slim + SSE 关闭最终消息集合一致；
- 同一 assistant message 多次追加后最终全文完整；
- SSE reconnect/backpressure、token done/idle 后不清空唯一可见内容；
- 空结果、截断、失败、超时、限流与 cursor fallback 语义可区分；
- 重复/乱序 digest 不导致 watermark 倒退；
- dirty 最终收敛且无无界重试；
- full/cursor snapshot merge 语义与删除/替换边界一致。

## 4. 门控后暂停点

联合计划已达到 `rev-gpt 9.5/10 PASS`，现在暂停，不实施阶段 B。

门控指出但不阻塞的两个实施前修订：

1. 静态审计命令必须区分 Retrofit 原始调用与旧 facade；
2. full/cursor snapshot merge 必须在实施前明确 union vs replacement。

下一步只有在用户明确指定 owner、目标版本和实施范围后才能开始。发布证据要求：commit、tag、artifact SHA-256、不可变 check log。

## 5. 新会话待办清单

### 阶段 A：自身任务（双方可独立执行）

- [ ] **A-SLIM-01（用户授权后）**：由用户指定 oc-slimapi owner；复核当前未提交第1类工作树，按项目发布流程生成 commit/tag/artifact SHA-256/不可变 check log。
- [ ] **A-SLIM-02**：确认新版本保留 `/since`、`X-Since-Complete`、full 错误归一和 SSE per-sid 修复；不新增第2类 watermark wire。
- [ ] **A-OCDROID-01**：由用户指定 ocdroid owner；实施阶段 A 客户端安全语义：`/since` 只 staging，失败/partial/cap/timeout 不推进 bookmark/localApplied，不清 dirty，不清唯一可见内容。
- [ ] **A-OCDROID-02**：客户端支持 `X-Since-Complete` capability；缺失时兼容旧 sidecar；完成客户端独立测试、commit/tag/artifact SHA-256/不可变 check log。
- [ ] **A-CROSS-01**：双方分别发布后交换版本、commit、artifact SHA-256 和 check log；未交换完成前不得进入阶段 B。

### 阶段 B：联调任务（阶段 A 双方发布证据齐全后）

- [ ] **B-DESIGN-01**：冻结 `SlimDrainOutcome`、bookmark commit、watermark 和重复/乱序 digest 规则。
- [ ] **B-DESIGN-02**：冻结 full/cursor snapshot merge 是 union 还是 replacement，并明确删除边界。
- [ ] **B-DESIGN-03**：冻结 token done/idle/resync 与 provisional 内容保留语义。
- [ ] **B-IMPL-01**：按双方冻结协议实施 sidecar/client 配套 wire 与状态机改造。
- [ ] **B-TEST-01**：覆盖 slim + SSE 开/关、断线、backpressure、token done/idle、同 message 多次追加、空/截断/失败、重复/乱序、最终集合一致和 dirty 收敛。
- [ ] **B-GATE-01**：若协议、wire 或 owner 范围发生变化，重新请求 rev-gpt 评审；当前 9.5/10 门控只覆盖现有联合计划。

## 6. 新会话提示词

- oc-slimapi 阶段 A：`docs/ocmar/reports/2026-07-26-slimapi-stage-a-execution-prompt.md`
- ocdroid 阶段 A：`docs/ocmar/reports/2026-07-26-ocdroid-stage-a-execution-prompt.md`
- 双方阶段 B 联调：`docs/ocmar/reports/2026-07-26-slim-message-stage-b-integration-prompt.md`

新会话必须先读取本索引和对应提示词；没有用户明确 owner、版本和范围时，只能勘察/提出方案，不得写代码、commit、tag 或发布。
