# slimapi × ocdroid 体验优先方案 — slimapi 联审确认（rev 3 · 对齐 ocdroid rev 6 GO 基线）

> **日期**：2026-07-21  
> **版本**：**rev 3**（ocdroid rev 6 四轮复审收敛后，slimapi 转为 GO 确认）  
> **对齐**：ocdroid `docs/0.11-ux-first-joint-plan.md` **rev 6**（280 行，rev-bgpt「有条件 GO」全成稿）  
> **rev-bgpt 终审**：`docs/ocmar/reviews/2026-07-21-rev-bgpt-ux-first-review.md`（slimapi 侧档案）  
> **基线**：slimapi **v0.3.0** / wire **1** / rev F；ocdroid **v0.11.10**

---

## 0. 总立场：GO，接受 rev 6 全部共识

slimapi **同意** ocdroid rev 6 的 7 项共识（§2 逐条裁决）。架构已收敛，**slimapi 侧无需另起 rev-bgpt 架构复审**——本方变更是对 rev 6 成稿的**对齐与实现承诺**，非新架构。**C1（§3 累计 413 × opt-in）已由 ocdroid 于 2026-07-21 经 session_notify 正式确认接受 → 双方共识完全达成，各自开工。**

本 rev 3 **替换** rev 2 的「前置裁决」姿态：B1/B2/B3/G-F1/G-MODE/G-ACL 全部 CLOSED 或转实现。

---

## 1. 对 ocdroid 7 项共识的明确裁决

| # | ocdroid 共识 | **slimapi 裁决** | 实现确认 |
|---|---|---|---|
| **1** | U3=Opt-A + `X-Slimapi-Capabilities: mid-partial-envelope=1` 能力头；非 opt-in 零改变；legacy 分流在 RequestError→envelope 映射之前 | **同意** | `messages.py:457` handler 入口解析能力头 → `opt_in` bool 传入 `fetch_one` 闭包；grammar（逗号切分/单 `=`/name 大小写不敏感/未知忽略/重复冲突 fail-closed）按 rev 6 §6 grammar 实现。可行性已核实。 |
| **2** | B2 六行响应矩阵 + invariant + feature flag + 回滚阈值 | **同意** | 矩阵映射到现有 `state`/`succeeded`/`errors` 结构（`messages.py:501-621`）；invariant（items/errors 按 messageID 互斥幂等）当前已成立；feature flag 走 config env 模式（同 `shell_deny_list_enabled`，`config.py:60`）；回滚阈值进 S-A 指标。 |
| **3** | B1 契约只写服务端保证；恢复算法归客户端 | **同意** | slimapi `v1-contract.md` 只写：顶层 413 不返 partial / 不泄露完成态 / 503 优先于 413。客户端预算（≤2N-1 / per-node ≤3 / 并发 ≤2 / 30s / 耗尽态）写入 `CLIENT_CHANGES`，不进 wire 契约。 |
| **4** | G-F1 cursor-walk 降级复用 `GET /slimapi/messages/{sid}`；撤回序列-gap；digest 异常 + 周期 bounded re-sync | **同意** | 端点已存在（`messages.py` list 路由，before-cursor，无 ts 过滤）。slimapi 角色 = **造 G-F1 fixture**（等 ts 多 mid / 跨页 / limit / 重连 / 循环触发降级）。撤回序列-gap 正确（无连续序列号字段）。 |
| **5** | G-ACL：4097 bind loopback + 远端 14097 mTLS；无证据则回退 | **同意** | **无需改码**：`config.py:23` host 默认已是 `127.0.0.1`；`0.0.0.0` 是 ops override。G-ACL = ops 纪律（不设 `0.0.0.0` / 确保 stunnel 14097 mTLS / 负向探针）+ 文档。 |
| **6** | slimapi rev 2 撤回项全部接受 | **确认** | 无额外动作；撤回清单保留于 §5。 |
| **7** | Retry-After：顶层 HTTP header + envelope `retryAfterMs`（≤10000）；cap 10s | **同意** | 顶层 503 走 HTTP `Retry-After`（透传上游优先 / 保守最小建议 / 坏 JSON 不发）；envelope 可选 per-mid `retryAfterMs` 字段。 |

**结论：1–7 全部同意，无反案。** 唯一补充是 §3 C1（实现细节澄清，非反案）。

---

## 2. slimapi Opt-A 服务端实现承诺（S-新 · 落地规约）

> 这是 slimapi 本周期**唯一**的服务端协议面变更。加性 wire，**不 bump `X-Slimapi-Version`**，须入 `v1-contract.md` + `CLIENT_CHANGES` + `CHANGELOG`。

### 2.1 能力头解析（R3-B2-NEGOTIATION + grammar）

- 入口：`message_batch` 读 `request.headers.get("x-slimapi-capabilities")`。
- grammar（rev 6 §6 I-R4/I-R5）：
  1. 逗号切分 token，两侧 trim ASCII whitespace；
  2. 每 token 须恰含一个 `=`；name 大小写不敏感；value trim 后字面比较；
  3. 未知 name / 格式错误 token 忽略，不导致整请求失败；
  4. `mid-partial-envelope=1` = opt-in；absent / `=0` = 非 opt-in；
  5. **同 capability 重复且值冲突 → fail closed**（按非 opt-in 处理）+ 计数指标（不含请求正文/ID）。

### 2.2 分流点（legacy 在 RequestError→envelope 映射之前）

```
opt_in = parse_capabilities(request) AND feature_flag_on(config)
state["opt_in"] = opt_in   # fetch_one 闭包可见

fetch_one(mid):
    ...
    except httpx.RequestError:
        if state["opt_in"]:
            errors.append({"messageID": mid, "code": "upstream_unavailable",
                           # 可选 retryAfterMs
            })
            return
        state["network_failed"] = True   # legacy 不变
        return
```

**非 opt-in 路径逐场景零改变**（`network_failed` → 整 503、`budget_exceeded` → 整 413、成功 → envelope）。回归矩阵钉死（rev 6 §6 R4-B2-OLD-SEMANTICS + I-R5-LEGACY-EQUIVALENCE）。

### 2.3 响应矩阵（rev 6 §6 六行 · 直接采用）

post-gather 决策（opt-in 分支）：

| 成功 items | RequestError mids | 其它 errors | 响应 |
|---|---|---|---|
| ≥1 | 0 | 0 | 200 success envelope（items 非空、errors 空） |
| ≥1 | 任意 | 任意非空 | 200 partial |
| 0 | 0 | ≥1 | 200 errors-only（全 mid-terminal → completion；含可重试 → 仅重试这些） |
| 0 | ≥1 | ≥1 | 200 errors-only（RequestError mid 映射可重试 code） |
| 0 | ≥1（全覆盖） | 0 | **顶层 503 `upstream_unavailable`** |

**非 opt-in**：完整 legacy 分支（不应用 Opt-A 映射）。

### 2.4 invariant / feature flag / 回滚

- **invariant**：`items[]` 与 `errors[]` 按 messageID 互斥、幂等 merge、顺序无关（当前代码已成立）。
- **feature flag**：`OC_SLIMAPI_OPT_A_MID_PARTIAL_ENVELOPE` env（默认 ON 部署后；OFF 时即使客户端 opt-in 也走 legacy）。
- **回滚阈值**（rev 6 I-R4/I-R5）：基线 = Opt-A 部署前 24h 同 endpoint 同客户端版本分层；观察窗 1h、样本 ≥100：
  1. envelope 相关 5xx > 2× 基线（基线率=0 时 current >1%）→ 自动关；
  2. unknown-code 占比 >5% → 自动关；
  3. 客户端上报展开失败率 >5% → 自动关。
  样本不足只告警；责任人可人工关；关闭后进行中 operation 已 merge 的 partial **不撤回**。
- **wire**：保持 1；bump 不排除（仅当能力头方案部署证明不足）。

---

## 3. C1 — 累计 413 × opt-in（✅ CONFIRMED · ocdroid 2026-07-21 接受；已烘焙进 rev 6 §6 B2）

### C1 — 累计 413 × opt-in 交互

**背景**：rev 6 §6 矩阵列 = 「成功 items / RequestError network mids / 其它 envelope errors」。**累计字节超限（`response_too_large`，整请求 413）未显式作为一列**。当前代码（`messages.py:553-557,610-614`）：累计超限 → `budget_exceeded` → 整请求 413，`succeeded` 全丢弃，**无论 opt-in 与否**。

**slimapi 实现解释（拟采用，请确认）**：

> **Opt-A 的变更面 = 仅 mid `httpx.RequestError` 的映射**。累计 413 `response_too_large` 与 per-mid `message_too_large`、mid HTTP ≥400 一样，**对 opt-in 与非 opt-in 行为一致**：
> - 累计 413 保持**顶层整请求**（opt-in 也一样），因为 B1 的分区恢复契约依赖 413 为顶层信号（「batch 太大 → 拆分」是正确恢复，与「mid 网络瞬态失败 → 重试该 mid」语义不同）。
> - per-mid `message_too_large` 仍进 envelope（单 mid 过大 = mid 终态，非整请求）。

**理由**：
- 413 = 批次过大需分区（B1 已定义恢复）；Opt-A = 网络瞬态失败保 partial。两者失败模式不同，恢复策略不同。
- 保持 413 顶层使 B1 分区契约对 opt-in/非 opt-in **统一**，最小变更面。
- 累计 413 应罕见（64 MiB cap）；若频发，根因是 batch 过大，应调 batch size 而非保 partial。

**若 ocdroid 认为 opt-in 应在累计 413 时也返 200 partial**（即 mid1 成功 + 其余因预算截断 → 200 partial），请明示；那会扩大变更面并需重新定义「截断 mid」的 envelope code。slimapi **倾向当前解释（413 顶层不变）**。

---

## 4. S-A–S-F + S-新 承诺（对齐 rev 6 §7.2）

| ID | 任务 | slimapi 承诺 |
|---|---|---|
| **S-新** | Opt-A 服务端 | §2 全规约；feature flag + 回滚；入 contract/CLIENT_CHANGES/CHANGELOG。**v0.3.1**（与 S-A/S-B 同 patch） |
| **S-A** | G6 可观测 | ledger（fetched/delivered/retry-duplicated/discarded）+ 量化 B2 代价 + Opt-A 回滚指标（unknown-code% / envelope-5xx / 展开失败率）+ mode=full vs skeleton；**撤回序列-gap**；异常走 digest 不一致 + 周期 re-sync 集合差 |
| **S-B** | Retry-After | 顶层 HTTP `Retry-After`（透传优先 / 保守建议 / 坏 JSON 不发）+ envelope 可选 `retryAfterMs`（≤10000）；cap 10s |
| **S-C** | gzip/字节比 | slimapi 序列化路径已 gzip；full passthrough 端到端实测；匿名聚合 median/P90；`mode=full` 流量单独统计 |
| **S-D** | Fixtures | **G-F1 fixture**（等 ts 多 mid / 跨页 / limit / 重连 / 循环触发降级）；合成可提交 + 临时脱敏分离 |
| **S-E** | 部署身份 | build 时注入 deployment revision（非 runtime git describe）；G-ACL 成立前不扩大 4097 暴露面 |
| **S-F** | reconfigured/resync | 同因不双发；resync+connected 仍可能，ocdroid once-latch 仍是客户端责任 |

**发版**：S-新 + S-A + S-B（+ S-E）→ `./scripts/release.sh patch` → **v0.3.1**；wire 保持 1。

---

## 5. rev 2 撤回项（ocdroid 已全部接受 · 留档）

U2 越界「契约 MUST 二分」/ U3「纯等指标」/ S-B「统一固定 Retry-After」/ S-E「runtime git describe」/ U6「真实 session id 清单」/ F-1「顺带」——全部已在 rev 2 §6 透明化，ocdroid rev 6 §9 接受。

---

## 6. G-ACL ops 清单（slimapi 侧 · 无码改）

| 项 | 动作 |
|---|---|
| bind | systemd unit 不设 `OC_SLIMAPI_HOST=0.0.0.0`（用默认 `127.0.0.1`）；或显式设 loopback |
| mTLS | 确保 stunnel 14097 监听 + 客户端证书校验；代理到 loopback:4097 |
| 证据 | 负向探针：公网/LAN 不可达 14097；Tailscale/指定源可达 |
| 回退 | 无证据则保持 loopback + 14097（即默认态） |
| 文档 | `docs/operations.md` 写清拓扑 + 安全模型 |

**注**：`config.py:71` 已拒绝任意 routable IP（仅允许 loopback / `0.0.0.0`）；G-ACL 后 `0.0.0.0` 不再用于生产，仅留作本地调试。

---

## 7. slimapi 近期执行看板

| Pri | 项 | 状态 |
|---|---|---|
| P0 | ~~C1 确认（§3）~~ | ✅ **已确认（2026-07-21）** |
| P0 | S-新 Opt-A 实现（§2）+ 测试（含非 opt-in 回归矩阵） | **可开工**（待用户发版批准） |
| P0 | S-A 指标 + S-B Retry-After | 并行 |
| P0 | S-D G-F1 fixture | 并行 |
| P0 | G-ACL ops 收敛 + 证据 | 并行 |
| P1 | S-C 字节比报告 / S-E build revision | 跟随 |
| P1 | contract/CLIENT_CHANGES/CHANGELOG 写入（Opt-A 加性） | 随 S-新 |
| P2 | v0.3.1 发版（patch） | 各项就绪后 |

---

## 8. 修订历史

| 版本 | 日期 | 说明 |
|---|---|---|
| rev 1 | 2026-07-21 | 初版回复（被 rev-bgpt 终审判 3 Blocker） |
| rev 2 | 2026-07-21 | 按 rev-bgpt 修订（撤回越界/收紧断言/升级前置门禁） |
| **rev 3** | 2026-07-21 | **对齐 ocdroid rev 6 GO 基线**：1–7 全部同意；Opt-A 服务端实现规约（§2）；唯一澄清 C1（累计 413 × opt-in）；S 表对齐 rev 6；G-ACL ops 清单 |
| **rev 3.1** | 2026-07-21 | **C1 CONFIRMED**：ocdroid 经 session_notify 正式接受 slimapi C1 提议（累计 413 顶层一致，Opt-A 仅 mid `httpx.RequestError` 映射）；ocdroid 已烘焙进 rev 6 §6 B2；**共识完全达成，双方开工**。 |
