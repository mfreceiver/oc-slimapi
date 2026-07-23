# 体验优先联合方案 — 双方共识归档

> **类型**：共识归档（一页纸 · 冻结快照）  
> **日期**：2026-07-21  
> **状态**：**✅ 共识完全达成 · 双方开工待用户批准开发**  
> **冻结点**：ocdroid rev 6（GO 基线）+ slimapi collab-reply rev 3.1 + C1 CONFIRMED  
> **Wire**：`X-Slimapi-Version: 1`（本周期保持；Opt-A 用能力 opt-in 头，非 wire bump）  
> **基线**：slimapi `v0.3.0` / rev F 已部署；ocdroid `v0.11.10` 已发

---

## 1. 一句话

契约主线（rev F）闭环后，双方以「体验优先」为下一阶段主里程碑：**展开可靠 + 弱网保留 + 实测省流**。经 rev-bgpt 四轮复审收敛到 ocdroid rev 6 GO 基线，slimapi 联审**全部接受**，唯一实现澄清 C1 已确认。**架构全收敛，无需 rev 7 / 无需 rev-bgpt 复审。**

---

## 2. 7 项共识（slimapi 全同意 · 无反案）

| # | 共识 | slimapi 落地 |
|---|---|---|
| 1 | U3=Opt-A + 能力头 `X-Slimapi-Capabilities: mid-partial-envelope=1`（加性头，非 wire bump）；非 opt-in 逐场景零改变，legacy 分流在 RequestError→envelope 映射之前 | `message_batch` 入口解析能力头 → `opt_in` 传入 `fetch_one` 闭包 |
| 2 | B2 六行响应矩阵 + invariant（items/errors 按 messageID 互斥幂等）+ feature flag + 回滚阈值（基线零/样本≥100/1h） | 矩阵映射现有 `state`/`succeeded`/`errors`；invariant 当前代码已成立；flag 走 config env |
| 3 | B1 契约只写服务端保证（顶层 413 / 无 partial / 不泄露完成态）；恢复算法归客户端 | `v1-contract.md` 只写服务端保证；客户端预算进 `CLIENT_CHANGES` |
| 4 | G-F1 cursor-walk 降级复用 `GET /slimapi/messages/{sid}`；撤回序列-gap（无连续序列号）；digest 异常 + 周期 bounded re-sync（事件驱动 + 15min + single-flight） | 端点已存在；**slimapi 造 G-F1 fixture** |
| 5 | G-ACL：4097 bind loopback + 远端 14097 mTLS；无证据则回退 | `config.py:23` 默认已 loopback，**无需改码**，纯 ops 纪律 + 负向探针 |
| 6 | slimapi rev 2 撤回项（U2 越界 / U3 等指标 / S-B 编造 / S-E runtime git / U6 真实 session / F-1 顺带）全部接受 | 留档（§5） |
| 7 | Retry-After：顶层 503 用 HTTP `Retry-After`；envelope 可选 per-mid `retryAfterMs`（非负整数 ≤10000）；客户端 cap 10s | S-B 实现 |

---

## 3. C1 澄清（✅ CONFIRMED · 已烘焙进 rev 6 §6 B2）

**议题**：累计字节超限 `response_too_large`（顶层整请求 413）× opt-in 的交互——rev 6 §6 矩阵未显式覆盖。

**共识结论**：**Opt-A 变更面 = 仅 mid `httpx.RequestError` 映射为 envelope 可重试 code。**

- 累计 413 `response_too_large`：对 opt-in / 非 opt-in **一致保持顶层 413**，`succeeded` 不输出，B1 分区恢复统一适用（**不返 200 partial**）。
- per-mid `message_too_large` 与 mid HTTP≥400 envelope 映射：同理对 opt-in/非 opt-in 一致。

**决定性理由**：
1. rev-bgpt B1 已 CLOSED 的契约基础 = 「顶层 413 = 整请求中止、NO partial、B1 分区恢复」；opt-in 累计 413 改返 partial 会重开 B1 契约触发再审。
2. 失败模式不同：413 = batch 过大 → 确定性分区（B1 halve）；Opt-A = 网络瞬态 → 保 partial 防重下。
3. B1 分区恢复对 opt-in/非 opt-in 统一，客户端逻辑不变。
4. 省流损失边际（仅罕见累计 413 已读前缀在分区时重取一次）；Opt-A 主要省流收益完整保留。
5. 最小变更面 = 低风险快发版。

**不改变矩阵语义，无需 rev-bgpt 复审。**

---

## 4. 发版硬门禁（rev-bgpt 前置）

| 门禁 | 内容 | 负责 |
|---|---|---|
| **G-F1** | `/since` watermark 运行时确认 + cursor-walk 降级（复用 ocdroid façade）+ digest 异常 + 周期 bounded re-sync | 联合（slimapi fixture） |
| **G-MODE** | 用户展开 MUST `mode=full`；skeleton 仅诊断不得标 Loaded | ocdroid |
| **G-ACL** | 4097 loopback + 14097 mTLS + 负向探针证据；无证据则回退 | slimapi 运维 + ocdroid 迁移 |

---

## 5. slimapi rev 2 撤回项（留档）

| 撤回项 | 问题 | 处理 |
|---|---|---|
| U2「契约 MUST 二分」 | 越界：客户端算法当服务端 wire 保证 | 契约只写服务端保证 |
| U3「纯等指标」 | 偏轻：与 P0 省流目标结构性冲突 | 升级为 Opt-A 前置（§2） |
| S-B「瞬态 503 统一固定 Retry-After」 | 误导退避 | 改透传优先 + 保守最小建议 |
| S-E「runtime `git describe`」 | 信息泄露 + editable 不可靠 | 改 build 时注入 deployment revision |
| U6「固定真实 session id 清单」 | 隐私/可移植 | 拆分合成可提交 vs 临时脱敏 |
| F-1「顺带」 | 生产正确性无期限 | 升级为发版门禁 G-F1 |

---

## 6. 双方开工清单（待用户批准开发）

### slimapi（目标 v0.3.1 patch · wire 保持 1）

| Pri | 项 |
|---|---|
| P0 | **S-新 Opt-A**：能力头分流 + §2 六行矩阵 + invariant + feature flag + 回滚阈值（入 contract/CLIENT_CHANGES/CHANGELOG） |
| P0 | S-A G6 可观测（ledger + 量化 B2 代价 + Opt-A 回滚指标 + mode=full vs skeleton） |
| P0 | S-B Retry-After（顶层透传优先 / 保守建议 / 坏 JSON 不发；envelope `retryAfterMs`） |
| P0 | S-D G-F1 fixture（等 ts 多 mid / 跨页 / limit / 重连 / 循环触发降级） |
| P0 | G-ACL ops 收敛 + 负向探针证据 |
| P1 | S-C gzip 实测 + 字节比（匿名聚合 median/P90） |
| P1 | S-E build 时注入 deployment revision |

### ocdroid（目标 v0.11.11）

- O-A：预算模型（节点≤2N-1 / per-node≤3 / 并发≤2 / wall-clock 30s / 耗尽态）/ 能力头 / envelope 保留 `(messageID, code)` / mode=full / m8 节流 bypass / mid 单飞
- G-F1 cursor-walk 降级（复用 `fetchSlimInitialWindowBounded`）+ 循环检测
- G-ACL 客户端 profile 迁移 + TOFU/pinning + smoke
- 弱网缓存优先 + stale 指示 + 流量归因

---

## 7. 评审与修订轨迹

| 阶段 | 产出 |
|---|---|
| slimapi rev 1 回复 | rev-bgpt 终审判 **3 Blocker + 10 Major + 4 Minor**（`docs/ocmar/reviews/2026-07-21-rev-bgpt-ux-first-review.md`） |
| slimapi rev 2 回复 | 按评审修订：撤回越界 / 收紧断言 / 升级前置门禁 |
| ocdroid rev 4–6 | rev-bgpt 四轮复审 → 「有条件 GO」全成稿（矩阵 / grammar / 预算 / cursor-walk / ACL / 回滚基线） |
| slimapi rev 3 / 3.1 | 对齐 rev 6 GO 基线；C1 提议 → ocdroid 确认 → 共识达成 |

---

## 8. 权威文件索引

| 文件 | 角色 |
|---|---|
| ocdroid `docs/0.11-ux-first-joint-plan.md` **rev 6** | **体验优先权威方案**（含 C1 澄清 line 181） |
| slimapi `docs/ocmar/plans/2026-07-21-ux-first-collab-reply.md` **rev 3.1** | slimapi 联审确认（Opt-A 服务端规约 §2） |
| slimapi `docs/ocmar/reviews/2026-07-21-rev-bgpt-ux-first-review.md` | rev-bgpt 终审纪要 |
| slimapi `docs/ocmar/plans/2026-07-21-v0.3.0-joint-workplan.md` | 契约期背景（已标注体验优先覆盖） |
| slimapi `docs/specs/v1-contract.md` | Wire 契约权威（rev F；Opt-A 待写入） |
| **本文件** | **共识归档冻结点** |

---

## 9. 当前动作

- **归档完成**（本文件）。  
- **开发暂未启动**（用户指示：归档共识，暂不开发）。  
- 下一步触发：用户批准后，slimapi 按 §6 P0 清单开工 → v0.3.1 patch。
