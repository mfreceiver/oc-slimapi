# oc-slimapi 唯一入口路线（single-entry roadmap）

> 状态：**提案（proposal）**——方向已获用户确认（2026-08-16："收编到 slim"），各阶段实施前仍需按门控流程评审。
> 证据基线：2026-08-16 生产 access log（`~/.local/state/oc-slimapi/logs/access-2026-08-16.jsonl`，截至北京时间 17:51）。
> 关联：`docs/ocmar/plans/2026-08-16-traffic-optimization-plan.md`（v1.5.0 已发版收官）；`docs/specs/CLIENT_CHANGES.md`「直连退役」节。

---

## 0. 动机与数据锚定

用户提出的终局问题：**slimapi 成为唯一入口后，是否可以完全避免透传（catch-all proxy）和自定义头？**

2026-08-16 生产实测：passthrough 共 4,772 次，**100% 匿名**（ocdroid 已全程 slim 路由）。画像：

| 家族 | 次数/日 | 最近访问（北京） | slim 等价 |
|---|---|---|---|
| `GET /session/status` | 3,734 | 17:51（活跃轮询） | ✅ `/slimapi/sessions/status` |
| `GET /session/{sid}/todo` | 324 | 17:48 | ✅ v1.5.0 `/slimapi/sessions/{sid}/todo` |
| `GET /session/{sid}/diff` | 269 | 17:48 | ❌ **T18 补齐（进行中）** |
| `GET /session/{sid}/children` | 156 | 16:02 | ✅ v1.5.0 |
| `GET /question`（含 POST reply） | 57 | 17:51 | ✅ 读侧已有 |
| `GET /permission` | 103 | 17:42 | ✅ 已有 |
| `POST /session/{sid}/prompt_async` | 41 | 17:48 | ❌ 写路径，Phase 2 决策 |
| `PATCH /session/{sid}` | ~20 | 15:12 | ❌ 写路径（改名/归档） |
| `GET /config/providers` 等 config | 9 | 17:51 | ❌ 小流量 |
| `GET /slimapi/health` / `events` | 9 | 17:47 | —（匿名方已在探测 slim 路由） |

**结论**：读侧缺口只剩 `/diff` 一条；写路径两家族（prompt_async、PATCH session）量小但需显式语义决策；匿名方已在主动探测 slim 端点 → 收编可平滑推进。

## 1. 目标态定义

1. **无透传 catch-all**：代理引擎保留但改为**显式 allowlist**；未登记路径 → 404 `unknown_route`（结构化错误，与 `thin_route_not_found` 同族）。上游新端点不再自动隧道。
2. **无自定义头**（客户端→sidecar 方向）：`X-Opencode-Directory` / `X-Next-Cursor` / `X-Complete` / `X-Slimapi-Version` 全部退役，功能由 query 参数 / body envelope / **版本发现端点**承接（§3）。服务路径保持**无版本段**（`/slimapi/*` 不引入 `/v3/` 前缀）；同一无版本路径上 v2/v3 语义**短期并行**（v3 经 `?v=3` 选取，机制以 design-v3 评审定稿为准），ocdroid 组完成改造后再彻底移除 v2。
3. ocdroid 直连回退口（stunnel 14096 → opencode 4096）关闭——目标态收编完成后退役（AGENTS.md「直连退役」节同步更新）。

**安全收益**（allowlist 的核心动机）：现状 catch-all = mTLS 边界上的任意 opencode API 隧道（含写操作、无 admission/字节 cap/审计归属）；allowlist 后攻击面收敛为逐条登记的显式面。

## 2. 三阶段路线

### Phase 1 — v1.5.x（加性铺垫，零破坏）

| 项 | 内容 | 状态 |
|---|---|---|
| T18 | `GET /slimapi/sessions/{sid}/diff` thin 路由（todo/children 同款；query `messageID` 可选透传；上游 `groups/session.ts:84,:168-176`，`FileDiff.Info` 5 字段近恒等） | **进行中**（fix-1） |
| T19 | 全部 GET 路由统一支持 `?directory=` query（todo/children 已支持；messages/sessions/status/question/permission/agent/command 补齐；`X-Opencode-Directory` 头**继续接受**，双轨过渡） | 待立项 |
| 匿名迁移 | 本地消费方（opencode 插件生态）配置迁移到 slim 路由：status → `/slimapi/sessions/status` 等 | 与 ocdroid/插件侧协调，观察 access log 验证 |

### Phase 2 — v1.6（行为变更，加性可观测）

| 项 | 内容 |
|---|---|
| catch-all → allowlist | 未登记路径 404 `unknown_route`（响应体带 path + 「如何申请登记」指引）；allowlist 配置驱动（`OC_SLIMAPI_PROXY_ALLOWLIST`，默认含当前实测全部活跃家族） |
| 写路径决策 | `prompt_async` / `PATCH session` / `POST /question/{id}/reply`：**倾向 allowlist 登记 + 审计增强**（access log 已有全量记录），不做真路由（写语义投影无省流价值）；如需更强控制再评估 |
| 404 观察期 | allowlist 上线后观察 ≥1 周 access log 的 `unknown_route` 命中，识别漏网消费者后补登记或收编 |
| 回退开关 | `OC_SLIMAPI_PROXY_ALLOWLIST_ENABLED=false` 一键回到 catch-all（运维兜底） |

### Phase 3 — v3 契约窗口（破坏性打包，一次做齐）

前置：ocdroid 侧协调（联合发版或版本协商），`X-Slimapi-Version` bump 2 → 3。**用户已确认 v3 方向（2026-08-16）：无版本段路径 + 发现端点 + 并行期，webui 直接按 v3 实现。**

> **勘误（2026-08-16，design-v3 rev6 定稿后）**：本节「`X-Slimapi-Version` bump 2 → 3」为 design-v3 评审定稿前的历史表述，**已被 v3-contract.md §1/§2 取代**——终态（sidecar 3.0.0）**删除该头而非 bump**：出现不报错、不解读；版本协商唯一机制 = `?v=3` selector + `GET /slimapi/versions` 发现端点。全部自定义协议头（X-Slimapi-Version / X-Opencode-Directory / X-Next-Cursor / X-Complete / X-Slimapi-Subscriber-ID）在 3.0.0 终态退役，wire 语义见 v3-contract.md §1 退役表。

| 项 | 内容 |
|---|---|
| **发现端点**（先行） | `GET /slimapi/versions`（**无版本段、无 `X-Slimapi-Version` 门禁、匿名可访问**）：返回 `{"current":3, "available":[2,3], "capabilities":{...}}`——告知本机可用 API 版本与能力清单；客户端据此按自身规则选取用法。旧客户端零影响（不认识该端点即不用）。 |
| Envelope 化 | `X-Next-Cursor` / `X-Complete` → body envelope `{"items":[…], "nextCursor":…, "complete":…}`（messages/sessions(+status)） |
| directory 转正 | `?directory=` 唯一 accepted 形式；头形式 3.x 窗口内保留兼容（`X-Opencode-Directory` 头 v3 并行期继续接受，v2 彻底移除时一并退役） |
| 头退役 | `X-Slimapi-Version` / `X-Opencode-Directory` / `X-Next-Cursor` / `X-Complete` 在 v3 语义下不再产出（304 的 `ETag`/`Vary`/`Cache-Control` 为标准头，保留）。**并行机制**：同一 `/slimapi/*` 无版本路径上，v2（现行头语义）与 v3（query/envelope 语义）共存，客户端经发现端点获知后自选；ocdroid 完成改造、v2 使用率归零后移除 v2（观察 access log 判定）。具体并行机制（`?v=3` query 选取 vs 其他）以 design-v3 评审定稿为准。 |
| 直连退役 | stunnel 14096 配置移除（CLIENT_CHANGES.md「直连退役」节落实） |

## 3. 头替代机制矩阵（功能不减，只减形式）

| 头 | 替代 | 迁移性质 |
|---|---|---|
| `X-Opencode-Directory` | `?directory=` query（Phase 1 双轨，Phase 3 转正；v3 并行期头继续接受） | 加性 → 破坏 |
| `X-Next-Cursor` / `X-Complete` | body envelope（Phase 3） | 破坏 |
| `X-Slimapi-Version` | **发现端点 `GET /slimapi/versions`**（无版本段路径 + `{"current","available","capabilities"}`；health `server.api_version` 自检保留） | 破坏（v3 窗口） |
| 客户端身份（`X-Client-*`，若匿名方未来引入） | mTLS 证书 CN / loopback socket peer 凭据 | 随收编消亡 |

## 4. 风险与开放问题

1. **匿名消费方盘点不完全**：access log 只见行为不见身份；allowlist 观察期是主要发现机制。`GET /config/providers`（9 次/日）等长尾待逐一决策（登记 vs 劝迁）。
2. **写路径安全语义**：allowlist 登记写方法 = 显式接受隧道风险；可选增强（Phase 2 评审定）：写方法额外审计字段 / 限速。
3. **v3 时机**：用户已确认提前启动（2026-08-16）；与 ocdroid 改造节奏解耦——发现端点 + 并行期使 v3 上线不等 ocdroid。envelope 化与头退役同一窗口打包（两次破坏两次适配不值得）。
4. **上游版本对齐**：上游 v1.18.16 快照行为为准；上游 major 变更时 allowlist 需复核。

## 5. 决策记录

- 2026-08-16 用户确认：匿名本地流量**收编到 slim**（非直连、非维持现状）；路线 = 读侧补齐 + allowlist + v3 窗口头退役三步走。
- 2026-08-16 用户确认：规划文档（本文件）+ T18 /diff 路由并行推进。
- 2026-08-16 **匿名归属确认**（ocdroid 会话回执）：匿名流量 = 本机 ocdroid 0.26.0，**设计使然**——ClientIdentityInterceptor（`ClientIdentityInterceptor.kt:62-77`）为 path gate，仅 `/slimapi/` 前缀请求注入 X-Client-*，catch-all 路径按设计不带身份。流量画像与 StandardApi.kt 存量调用逐项吻合（StatusPollOrchestrator 2s 轮询 / todo / diff / permission / config / prompt_async）。收编分工：读路径迁移 = ocdroid 0.26.x（其「sidecar v1.5.0 新能力接入」待办，L3-波2 发版后启动）；写路径 = 客户端不动，本仓 allowlist 登记；identity 头随读路径迁移自然解决。
- 2026-08-16 **v3 方向确认**（用户）：①去自定义头 + v3 **可以开始**（Phase 3 提前启动，不与 Phase 2 串行）；②短期并行可行——同一无版本路径 v2/v3 共存（机制以 design-v3 评审定稿为准），ocdroid 完成改造后再彻底移除 v2；③服务路径**不引入 `/v3/` 版本段**，改为 `GET /slimapi/versions` **发现端点**（无门禁、匿名可访问，返回本机可用版本与能力清单），客户端按自身规则选取用法；④webui 项目直接按 v3 实现（演进通知已发）。
- 待决策：T19 立项时机；Phase 2 写路径 allowlist 细则；design-v3 并行机制定稿（`?v=3` query 选取 vs 其他）。
