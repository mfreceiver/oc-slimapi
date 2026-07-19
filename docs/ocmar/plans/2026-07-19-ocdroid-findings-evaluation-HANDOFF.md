# HANDOFF — ocdroid 评审修复 + v1 遗留补齐（执行前 checkpoint）

> **用途**：context compaction 后的单一 resume 入口。读此文件 + 跳转 spec/plan 即可恢复全部决策与执行状态。
> **状态**：阶段 1–4 完成，**阶段 5 执行前暂停**（未派发、未 commit、未做 git 变更）。
> **日期**：2026-07-19｜**slug**：`ocdroid-findings-evaluation`｜**base**：`9373550`（origin/main，working tree 干净）

---

## 0. 一句话目标

评估并修复 ocdroid 接口评审的 F1–F5 + §5 文档重构 + 补齐 v1 遗留 G1（错误可见性）/ G6（批量展开）/ D1–D8（文档同步），全部**加性变更不 bump `X-Slimapi-Version`**。

## 1. 产物索引

| 产物 | 路径 | 状态 |
|---|---|---|
| **Spec（已确认）** | `docs/ocmar/specs/2026-07-19-ocdroid-findings-evaluation-design.md` | ✅ |
| **Plan（已确认 + grilling 修正）** | `docs/ocmar/plans/2026-07-19-ocdroid-findings-evaluation.md` | ✅ |
| **本 handoff** | `docs/ocmar/plans/2026-07-19-ocdroid-findings-evaluation-HANDOFF.md` | ✅ |
| 调研 exp-1（B0 验证 GO） | reusable: `ses_08529a7a8ffefuCqoSoZgopfMu` | reconciled |
| 调研 exp-2（G1 hub 地图） | 已 reconciled（6 插点） | terminal |
| 调研 exp-3（G6 messages 地图） | reusable: `ses_0852966eaffe113EcAlooFMQO2` | reconciled |

## 2. 范围与决策清单

### 修复项（全部已定）

| 项 | 方向 | 决策依据 |
|---|---|---|
| **F1** q/p `directory` 必填 | 两者都做：sidecar 允许 null=聚合 allowlist + 契约写 cold-start 顺序 | spec §4.1 加性；与 `/sessions` null=all 对齐 |
| **F2** listed-but-rejected | 放宽 per-session status allowlist（sid 自洽即能力） | spec §4.2：allowlist 非安全边界（stunnel mTLS 才是） |
| **F3** allowlist 冷启动 400 窗 | 三管齐下：`_token` 走 `require_directory` + 启动 warm-up + 文档 |  |
| **F4** CLIENT_CHANGES SSE 过期 | 同步 SSE 节与 INTERFACE_MAP §3 一致 |  |
| **F5** `accepted:[1,1]` 标注 | contract §1 加闭区间说明 |  |
| **§5** 文档结构 | directory 三态表 + allowlist 独立节 + 同步纪律 + 一致性 |  |
| **G1** 错误可见性 | **本批实现**（spec §6.4 + plan T5/T6） | exp-1 B0 **GO** |
| **G6** 批量展开 | **本批实现**（spec §6.5 + plan T7） | exp-3 全路径 |
| **D1–D8** 文档同步 | 全折叠（见 §6 表） |  |

### 关键决策（grilling + 调研得出）

- **不 bump `X-Slimapi-Version`**：F1/F2/F3 加性、G1（新 digest 字段 + 新 SSE event）加性、G6（新端点 + envelope code）加性、文档无 wire。ocdroid 现有代码不因升级而坏（G6 现走 404 fallback，升级后首调 200）。spec §4.1 逐项分析。
- **G6 mid 5xx 处理**：→ envelope `errors[] upstream_http_N`（整 200，部分失败容错）；仅 `httpx.RequestError`（网络不通）才整请求 503。与 envelope 哲学 + q/p 聚合语义一致。
- **G1 sid 提取**：**必须显式取 `props.get("sessionID")`**，**禁用 `_extract_session_id`**——后者会回落到 `payload.id`（GlobalBus 自动赋的 `evt_...` 事件 id），把事件 id 误当 sid。
- **G1 sticky 跨窗口**：用独立 `self.sticky_last_error: dict[sid, dict]` 持久层 + `flush()` 内 `if fields.last_error is _UNSET and sid in sticky: 回填`；clear=`status=busy`→置 None（显式 null 帧）；deleted→pop sticky + `entry.last_error=_UNSET`。
- **G1 三态 sentinel**：模块级 `_UNSET = object()`；`last_error` 字段默认 `_UNSET`（to_payload 省略）/ `None`（输出 `null`，显式 clear）/ `dict`（输出对象）。
- **G1 时间戳**：复用 hub.py 既有 `_now_ms()`（line 356 已引用），**勿用** `int(time.time()*1000)`（grilling 修正）。
- **G1 publish 内调 flush 安全**：hub.py `flush()` 用 snapshot 模式（line 278），publish 各分支末尾立即 return，无迭代-中-换-dict 风险（grilling 验证）。
- **abort 过滤**：`error.name == "MessageAbortedError"`（确切字面值，exp-1 确认；TUI `app.tsx:1021` 同规则）。abort 静默丢（不写 lastError、不发 B 帧）。

## 3. 调研结论（condensed）

### exp-1：B0 go/no-go → **GO**
- `session.error` 经 `/global/event` 实发：schema `schema/src/v1/session.ts:651-657` → `event-v2-bridge.ts:35-44` → `handlers/global.ts:36-52`。
- payload：`{directory, project?, workspace?, payload:{id:"evt_..", type:"session.error", properties:{sessionID?, error:{name, data:{message}}}}}`。
- `sessionID` optional：`plugin/index.ts:136` + `skill/index.ts:114` 无 sid 实发 → G1-B（session-less 帧）必需。
- abort：`processor.ts:648-655` + `message-v2.ts:608-614`，name=`MessageAbortedError`。
- 8 种 AssistantErrorSchema 变体（ProviderAuthError/UnknownError/MessageOutputLengthError/MessageAbortedError/StructuredOutputError/ContextOverflowError/ContentFilterError/APIError）全过同通道。
- **意外（→ D8）**：AGENTS.md 称 `current→v1.17.20` 但实链 `v1.18.3`（两版结论一致）；impl-spec.md:68 已引 v1.18.3 → AGENTS.md 对齐。

### exp-2：G1 hub.py 6 插点
- INSERT-A `publish()` 加 `session.error` 分派（MESSAGE_EVENTS return 之后、catch-all line 365 之前）。
- INSERT-B `DigestFields` 加 `last_error`（默认 `_UNSET`）+ `to_payload` 三态。
- INSERT-C `flush()` 合并 sticky（非 reseed）+ sticky 持久层。
- INSERT-D `session.status busy` 触发 clear。
- INSERT-E 测试：改 `test_message_part_delta_produces_no_frames`（line 205-220，删 session.error 丢弃断言，改 abort 仍丢）+ 7 类新 G1 测试。
- INSERT-F 文档。
- **必修测试**：`tests/test_hub.py:205-220` 现断言 session.error 被丢，G1 后会产帧，必改。

### exp-3：G6 messages.py 全路径
- 路由：`@router.get("/full")` 插 messages.py **L435–436 间**（先于 `/full/{mid}`，spec §8 MUST；段数不同本不冲突但顺序锁定）。
- 流程：`_resolve_messages_directory` → discover `/session/{sid}`（带 directory 头，`_raise_upstream_status` 映射）→ 局部 `asyncio.Semaphore(4)` 并发拉 N mid + 共享 `total_bytes`。
- full 模式**不进 pool**（对齐 `/full/{mid}` full 先例 L445-479）；skeleton 模式**单 pool admission 包整批**（仿 messages_since L260-355）。
- 失败语义：discover 404→404 `session_not_found`（0 mid 调用）；mid 404→envelope `message_not_found`；mid >`max_message_bytes`→`message_too_large`；累计 >`max_response_bytes`→413 `response_too_large` 中止；mid `httpx.RequestError`→整 503；mid 5xx→envelope `upstream_http_N`；**全 mid 404 仍 200+全 errors**。
- items 严格按 ids 去重保序；`Cache-Control:no-store`。
- 测试复用 `_settings/_build_app/upstream_factory/_msg` + 累计预算 `calls["count"]` 锁定模式（仿 `test_messages_since_enforces_cumulative_byte_budget`）。
- **NOT-to-do**：discover 前不拉 mid / mid 404 非整 404 / 不校验 mid 字符集 / full 不包 admission / `_resolve_messages_directory` 必先于 discover。

## 4. 文件改动清单（spec §5 细化）

### 代码（5 文件）
| 文件 | task | 改动 |
|---|---|---|
| `src/oc_slimapi/routes/sessions.py` | T1, T4 | `load_products(app)` 签名 + `warm_allowlist` + caller；`normalize_directory` 提取 + per-session status 用其替代 `require_directory` |
| `src/oc_slimapi/app.py` | T1 | lifespan `smoke` 后加 `await sessions.warm_allowlist(app)` |
| `src/oc_slimapi/routes/questions.py` | T2, T3 | `_token` async + `require_directory`；`_aggregate` null 路径 + 两路由 `Query(None)` |
| `src/oc_slimapi/sse/hub.py` | T5, T6 | `_UNSET`/`ABORT_NAME`/`_sanitize_error_message`（纯函数+regex）；`DigestFields.last_error` 三态；`sticky_last_error` 持久层；`publish()` session.error 分派；`flush()` 合并 sticky；clear-on-busy；deleted 清除 |
| `src/oc_slimapi/routes/messages.py` | T7 | L435-436 间插 `@router.get("/full")` `message_batch` handler |

### 文档（8 文件）
| 文件 | task | 改动 |
|---|---|---|
| `docs/v1-contract.md` | T8 | 头部 changelog + §1 闭区间 + §2 端点表（q/p 可选/status 自洽/G6 新行）+ §3 digest `lastError?` + 新 `session.error` 帧定义 + §4 cold-start 暖机 + §7 错误码（invalid_ids/message_not_found + directory_not_allowed 适用范围）+ 新 §12 directory 三态表 + 新 §13 allowlist 机制 + §11 标 closed + 同步纪律 |
| `docs/design-v2.md` | T9 | §1.4 limit 422(D3) + §1.7 q/p 可选(D1) + §1.9 status(D2) + §1.10 删 session.error(G1) + §3 SSEClient/删 thin.session.dirty(D4/D5) + 新 §1.13 G6 |
| `docs/v1-impl-spec.md` | T9 | §1 B0 决策 GO(D7) + §7 G1 标已实现 + §8 G6 标已实现 |
| `AGENTS.md` | T9 | 对齐版本 v1.18.3(D8) |
| `docs/CLIENT_CHANGES.md` | T10 | SSE 节重写(F4) + G1/G6 客户端说明 |
| `docs/INTERFACE_MAP.md` | T10 | §0 normalize_directory + §1 表（q/p/status/G6）+ §3 session.error + §7 G2 放宽 |
| `docs/v1-contract-implementation-status.md` | T10 | §2 表 + §3 digest lastError + 诚实声明 routeToken 已修复 + G1/G6 落地 |
| `CHANGELOG.md` | T10 | `[Unreleased]` Added/Changed/Fixed |

### 测试（4 文件）
| 文件 | task | 新增/改 |
|---|---|---|
| `tests/test_sessions_routes.py` | T1, T4 | +`load_products(app)`/`warm_allowlist` 吞错；改写 `test_status_allowlist_miss_returns_400` → relaxed 200 |
| `tests/test_questions_routes.py` | T2, T3 | +cold allowlist routeToken 刷新；+null 聚合 + 空 envelope |
| `tests/test_hub.py` | T5, T6 | +8 sanitize golden；**改** `test_message_part_delta_produces_no_frames`（line 205-220）；+7 类 G1（A 立即 flush / B session-less / abort 过滤 / sticky / clear-on-busy / deleted 清除） |
| `tests/test_messages_routes.py` | T7 | +8 g6（ids 缺失/invalid/session_not_found 无 mid 调用/部分失败/全 mid 404 仍 200/累计 413/定序/路由不被吞） |

## 5. 执行结构（10 task / 3 wave / worktree 隔离）

**模式**：ocmar-subagent-driven-development。每 task = fresh fixer（implementer）+ oracle（task-reviewer）+ wave 边界全量 `check.sh` + 终门控独立 verifier + final code-reviewer。
**并行隔离**：Wave A/B 代码用 git worktree（每 lane 一个 `.slim/worktrees/<slug>`，base `9373550`，分支 `omos/<slug>`）；Wave C 文档共享 tree 并行（文件互不重叠）。
**为何 worktree**：T1 改 `load_products(request)→(app)` 签名不向后兼容，共享 tree 并行会让 T2/T7 的全量 `_build_app` 测试看到 `request.state.upstream` AttributeError（upstream 挂 `app.state`）。worktree 是唯一安全兑现 4-fixer 并行的方式。

### Wave A — 4 fixer 并行（无依赖，写域不相交）
| Lane | Task | 写域 | worktree slug | 本模块测试（fixer 自跑） |
|---|---|---|---|---|
| sessions | **T1** F3 基础 | sessions.py + app.py + test_sessions_routes.py | `t1-sessions` | `pytest tests/test_sessions_routes.py` |
| questions | **T2** F3 routeToken | questions.py + test_questions_routes.py | `t2-questions` | `pytest tests/test_questions_routes.py` |
| hub | **T5** G1 脱敏 | sse/hub.py + test_hub.py | `t5-hub` | `pytest tests/test_hub.py -k sanitize` |
| messages | **T7** G6 | routes/messages.py + test_messages_routes.py | `t7-messages` | `pytest tests/test_messages_routes.py -k g6` |

**Wave A 门控**：4 fixer 完成（hook 驱动）→ reconcile 4 worktree diff → merge 4 分支到 main（按 worktrees skill **请用户确认 merge**）→ 全量 `./scripts/check.sh` → 4 oracle task-reviewer 并行 → fix 循环 → `check.sh` green → Wave B。

### Wave B — 3 fixer 并行（依赖 Wave A 已合入）
| Lane | Task | 写域 | worktree slug | 依赖 |
|---|---|---|---|---|
| questions | **T3** F1 null | questions.py + test_questions_routes.py | `t3-questions` | T1（`load_products(app)`）+ T2（文件锁） |
| sessions | **T4** F2 relax | sessions.py + test_sessions_routes.py | `t4-sessions` | T1（文件锁） |
| hub | **T6** G1 核心 | sse/hub.py + test_hub.py | `t6-hub` | T5（`_sanitize`/`_UNSET`/`ABORT_NAME`） |

**Wave B 门控**：同 A — 3 worktree merge → `check.sh` → 3 oracle 并行 → fix → `check.sh` → Wave C。

### Wave C — 3 fixer 并行（文档，依赖全部代码 verified）
| Lane | Task | 写域 |
|---|---|---|
| contract | **T8** | docs/v1-contract.md |
| design/spec | **T9** | docs/design-v2.md + docs/v1-impl-spec.md + AGENTS.md |
| client/map/status | **T10** | docs/CLIENT_CHANGES.md + docs/INTERFACE_MAP.md + docs/v1-contract-implementation-status.md + CHANGELOG.md |

**Wave C 门控**：3 fixer 完成 → `check.sh`（确认无副作用）→ 3 oracle（人工核交叉引用）→ 终门控。

### 终门控（阶段 6）
- **独立 verifier**（fresh `_priv-verifier`，read-only，live rerun 禁缓存，跑 `./scripts/check.sh`，日志首行 `OCMAR_VERIFY_START=`，只回 `EXIT=<n> FAILURES=<n> LOG=<path>`）。
- **final code-reviewer**（`oracle`，整支评审）。
- 双门控：`EXIT=0 AND FAILURES=0` **且** final review pass → 生成总结报告 → `docs/ocmar/reports/2026-07-19-ocdroid-findings-evaluation.md`。

### 失败回流（阶段 7）
- 代码层失败（reviewer/verifier 报具体问题）→ fixer 修该 task → re-review/re-verify（attempt++）。
- 方案层失败（spec 错/plan 方向错/反复修不过）→ STOP，回阶段 1/2 修正，旧 ledger release（abandoned）。

## 6. D1–D8 文档同步清单

| # | 位置 | 问题 | 修法 | task |
|---|---|---|---|---|
| D1 | design-v2 §1.7 | q/p directory 隐含必填 | 随 F1 改可选 | T9 |
| D2 | design-v2 §1.9 | status「fan-out 失败→503」 | 同步 B1 三态 + F2 放宽 | T9 |
| D3 | design-v2 §1.4 | `limit 0→400` | 实际 FastAPI `ge=1`→422 | T9 |
| D4 | design-v2 §3 line 160 | SSEClient `/global/event`→`/event` | 改 `/slimapi/events` | T9 |
| D5 | design-v2 §3 line 162 | `thin.session.dirty` 事件 | 实际是 `session.digest`，删/换 | T9 |
| D6 | v1-contract §11 | 待补缺口带 🆕 / 「驱动 lane 派发」 | impl-status 全 ✅，标 closed | T8 |
| D7 | v1-impl-spec §1 | B0 决策 pending | 记录 B0 GO | T9 |
| D8 | AGENTS.md | `current→v1.17.20` 实链 v1.18.3 | 改 v1.18.3（exp-1 发现） | T9 |

## 7. 全局纪律（执行时严守）

- **不 commit**（除非用户显式要求）；每 task 仅 `git diff` 记录。
- **不 bump** `X-Slimapi-Version`（仍 `1`）。
- **校验**：`./scripts/check.sh` = `pytest tests/`；wave 边界 + 终门控各跑一次。
- **并行 fixer 严禁跑全量 check.sh**（其它 lane 在 flux）；全量门控只在 wave 边界由编排者跑。
- **worktrees skill**：`git worktree add`/branch/merge **必须先请用户确认**（强制）。
- **写域不变量**：同 wave 任两 task 写域交集 = ∅（§5 已保证）；跨 wave 同 lane 串行。
- **writer ≠ reviewer**：fixer 不评审自己；reviewer 用 oracle。
- **中断可恢复**：每 task `transition→implementing` 后立即 checkpoint；resume 走四态（COMPLETED/READY_VERIFY/RESUME/INCOMPLETE）。
- **诚实**：测试/评审没过绝不宣称完成；反复失败回方案层而非硬冲。

## 8. resume 触发词与下一步

用户说「**继续**」/「**resume**」即启动阶段 5，按序：

1. `.slim/worktrees/` 忽略块（`.gitignore` + `.ignore` managed block）+ `.slim/worktrees.json` 初始化。
2. **请用户确认** → 建 4 个 Wave A worktree（`.slim/worktrees/{t1-sessions, t2-questions, t5-hub, t7-messages}`，分支 `omos/{slug}`，base `9373550`）。
3. 派 4 个 fresh fixer（background=true，各自 worktree 绝对路径，只跑本模块测试，按 plan 对应 task 的 TDD 步骤）。
4. hook 完成通知 → reconcile 4 worktree → **请用户确认** merge → 全量 `check.sh` → 4 oracle 并行 → fix 循环 → Wave B（3 worktree）→ Wave C（3 文档 fixer 并行）→ 终门控 → 总结报告。

---

> **compaction note**：本文件 + spec + plan 三份足以往后恢复全部执行上下文。调研详情（exp-1/2/3）已 condensed 进 §3；代码骨架在 spec §6 + plan 各 task Step 3；测试代码在 plan 各 task Step 1。无需重读 opencode 源码或 re-dispatch explorer。
