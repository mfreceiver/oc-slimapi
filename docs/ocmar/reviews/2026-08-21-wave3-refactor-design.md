# Wave 3 实施设计：大拆分 + B12 — 评审包

> 状态：待评审（门禁 ≥9.0）。基线 `7c9192f`（v4.6.2）。
> 依据：批次三计划 §Wave 3（:82-89，F-301/F-302/F-304 + B12 + N6）；发现原文 `docs/audits/2026-08-20/02-findings/F-301/302/304.md`；e1-01 卡片 `docs/audits/2026-08-20/01-explore/parts/e1-01-tokenstream-hub.md`；B12 配方 = 计划 :87 + 评估文档 §3（三分处置）。

## 0. 泳道划分与写域（派发前机器校验交集 = ∅）

| 泳道 | src 写域 | tests 写域 | 排除（归后序阶段） |
|---|---|---|---|
| W3-1 hub 五拆 | `src/oc_slimapi/sse/tokenstream/**`（hub.py、subscriber.py 等）、`src/oc_slimapi/sse/registry.py`（仅 :305 docstring 行）、`src/oc_slimapi/sse/hub_types.py`（仅当需 re-export 兼容） | test_token_hub.py、test_token_hub_flush.py、test_token_hub_lifecycle.py、test_token_stream_route.py、test_token_subscriber_overflow.py、test_events_tokens.py、test_batch3_lifecycle.py、test_session_status_object_format.py、**test_sse_replay_wire.py（仅 :728/:759/:1441/:1649/:1890 五行 object-form patch 目标随迁——rev1 P-2 carve-in；其 B12 语义断言半区仍归 C 阶段）**、test_refactor_equivalence.py（仅 token_frames 场景若需 patch 路径迁移） | test_sse_replay_wire.py 其余内容（C 阶段 B12） |
| W3-2 messages 三拆 | `src/oc_slimapi/routes/messages.py`（整文件 → `routes/messages/` 包） | test_messages_routes.py、test_messages_merged.py、test_messages_coalesce.py、test_expand_routes.py、test_expand_config.py、test_expand_href_v4.py、test_full_absorb.py、test_cursor_matrix.py、test_b2_merged_text_compat.py、test_skeleton_expand.py、test_message_fingerprint.py、test_etag.py（仅 messages patch 目标行）、test_readiness_gating_integration.py（同前）、test_offload_equivalence.py（patch 目标行） | test_v3_etag_domain.py、test_v3_envelope.py（C 阶段 B12）。**import-only 免改文件**（实测零 patch，re-export 后不受影响）：test_errors.py、test_traffic_integration.py、test_upstream_error_boundary.py |
| W3-3 聚合共享 | `src/oc_slimapi/routes/questions.py`、`src/oc_slimapi/routes/permissions.py`、新增 `src/oc_slimapi/routes/_aggregate_fanout.py` | test_questions_routes.py、test_questions_coalesce.py、test_permissions.py、新增 test_aggregate_fanout.py（预算器直测） | — |
| B 残留（A 后串行） | `src/oc_slimapi/routes/events.py`（:211）、`src/oc_slimapi/routes/token_stream.py`（:290）、`src/oc_slimapi/skeleton.py`（:256 注释）、`docs/operations.md`（冗余句） | — | — |
| C B12（B 后串行三批） | **零 src 改动**（tests + 文档 only） | 12 双视图文件 + 8 个 v3 单态文件 + 294 字面涉及的其余 tests 文件、新增白名单文档 | — |

编排者派发前以脚本比对三泳道（src+tests）路径交集；A 阶段三泳道并行，B/C 串行。

## 1. N6 等价性门（已落地并按 rev1 P-1/P-6 修正，基线 7c9192f 录制）

- `tests/test_refactor_equivalence.py` + `tests/golden/refactor-baseline-v1.json`（**17 case**：**路由表** digest / sessions×2 / agent / **questions×4 + discovery_5xx(503) + fanout_window12（12 目录 > 并发窗 8，钉跨批严格序合并）+ truncated（`questions_max_aggregate_bytes=1024` 真触发，harness 内断言信封 `truncated:true`——rev2 R-1 修正：`_MAX_AGGREGATE_ITEMS=10_000` 项数截断不可达，字节预算才是可达路径）** / **permissions×4** / **token_frames**（嵌套 §4 信封 + text-start + 两窗 delta + `end` 终态；实测 **4 帧**——connected/两窗 delta/终态，非退化快照，断言 `>=3` 防再退化））；hashseed=0 子进程录制（同 W2 先例）。
- W2 金样（`offload-baseline-v1.json`，29 case）在每阶段后同样回放（messages/read-group 字节面持续钉死）。
- 每泳道收尾：N6 + W2 金样回放绿 + 泳道定向 pytest 绿；编排者每阶段末全量 `check.sh`。
- 路由表 digest 与 check.sh 路由↔文档 gate 双保险（W3-2 包化不得动路由注册表——F-302 冻结要求）。

## 2. W3-1：tokenstream/hub.py 五拆（F-301 蓝本，Mixin 纯移动）

**机制**：Mixin 组合而非函数搬迁——`class TokenStreamHub(BudgetMixin, FlushEngineMixin, IngestMixin, FanoutMixin)`。理由：e1-01 测试锚点（9 文件）大量白盒调用实例方法/属性（`th._pending`、`th.flush()`、`th._reserve`…），Mixin 保持单类单实例全部符号可达 → **测试锚点零迁移**（仅模块级常量 patch 目标除外，见下）；纯移动零行为。

模块与内容（按 e1-01 十二组表映射）：
- `tokenstream/budgets.py`（~450 行）：截断/内存预算组（`_truncate_part_for_all`/`_reserve`/`_evict_part_for_memory`/`_check_pending_budget`/`_start_part`）+ Part 生命周期/tombstone 记账 13 符号（`drop_part`/`_remember_*`/`_is_*`/`_prune_*`/`_next_part_revision`）+ **`TOKEN_*` 预算常量族与 `apply_debug_budget_overrides`（rev1 P-4：随真实读取点迁入，hub re-export 保 app.py import 兼容）**。
- `tokenstream/flush_engine.py`（~250 行）：后台 flush 生命周期组（`start`/`_on_flush_done`/`stop`/`flush_loop`/`flush`/`flush_sid`）+ 模块级 `_TTL_TICK_INTERVAL`、`_HEARTBEAT_TICK_INTERVAL` + pending resync 队列组（`_enqueue_session_resync`/`_drain_pending_session_resyncs`）。
- `tokenstream/ingest.py`（~550 行）：Ingest 组（`on_part_updated`/`on_part_delta`/`on_message_removed`/`on_part_removed`/`on_session_status`）+ 退役清理组（`_retire_message`/`_retire_session`/`ttl_sweep`）+ 终态 `finish_part` + 会话删除/重连（`on_session_deleted`/`on_upstream_reconnect`）+ 模块级 `_SESSION_STATUS_MAX`。
- `tokenstream/fanout.py`（~450 行）：订阅者接线（`attach_subscriber`/`detach_subscriber`/`has_subscriber`）+ Fanout 辅助 10 符号 + 属性/只读 7 property + 模块级 `_V4_INELIGIBLE_FRAME_PREFIX`/`_v4_frame_eligible`/`_events_token_frame`。
- `hub.py` 壳（~300 行）：`class TokenStreamHub(四 Mixin)` + `__init__` 十容器 + `logger` + **全部迁出符号的兼容 re-export**（TOKEN_*、`apply_debug_budget_overrides`、`_TTL_TICK_INTERVAL` 等——保 `app.py:35/:311` 与既有 import 兼容；读取点/变异点在真模块）。
- `sse/token_hub.py` shim 不动。

**模块级常量与 debug 开关 patch 兼容（关键风险，rev1 P-2/P-4 修订）**：
- 勘测口径 = **string-form 前缀 patch ∪ object-form 别名 setattr**（`import …tokenstream.hub as <alias>` 后 `setattr(<alias>, "TOKEN_*", …)`）。实测全集：string-form 三文件（test_token_hub_flush / test_token_hub_lifecycle / test_token_stream_route）；object-form = test_sse_replay_wire.py:728/:759/:1441（`TOKEN_PART_MAX_BYTES`）、:1649/:1890（`TOKEN_LIVEPARTS_MAX_BYTES`）——该文件五行已 carve 入 W3-1 写域（仅 patch 目标行），其余内容归 C 阶段。
- `apply_debug_budget_overrides` 与 `TOKEN_*` 预算常量（`global` 就地重绑语义）**随读取点迁入 budgets.py**；hub.py re-export 保 `app.py:35/:311` 的 import 兼容；test_token_hub_flush 的 `TestDebugBudgetOverrides`（:1579-1640，含行为级逐出断言）与 `hubmod.TOKEN_*` 直赋点随之迁移到 budgets 命名空间——变异必须抵达真实消费点（P-4）。
- 其余模块级常量（`_TTL_TICK_INTERVAL`/`_HEARTBEAT_TICK_INTERVAL`/`asyncio.sleep` patch）：patch 目标随迁至真模块路径（flush_engine），hub.py re-export 保 import 兼容。清单入泳道报告。
- Wave 1 残留顺带：`sse/registry.py:305` docstring 去除 `_busy_sids`；`subscriber.py:449` `"subscriber_backpressure"` 字面量 → `hub_types.RESYNC_SUBSCRIBER_BACKPRESSURE`（常量已在冻结集，零集变更）；常量引用使 F-122 AST gate 继续绿。
- 符号口径勘误（rev1 N-1）：Part 生命周期/tombstone 组现树 13 符号（W1-B 已删 `_prune_busy_sids`）；类内 def=58 含 `__init__`，与映射总数吻合。计划 :84 括注「五职责」（连接/订阅者账本/flush loop/背压/replay 桥）与 F-301 五模块名（budgets/flush_engine/ingest/fanout/壳）不同轴，实现以 F-301/e1-01 为准（出处说明）。
- 验收：N6 token_frames digest 不变（4 帧基线）+ token 泳道测试全绿（含 carve 的 replay_wire 五行迁移）+ 全量 check.sh。

## 3. W3-2：messages.py 三拆（F-302 蓝本，包化 + re-export）

`routes/messages.py` → `routes/messages/` 包：
- `_router.py`：`router = APIRouter(prefix=…, tags=…)` **单一共享 router 对象**（rev1 P-5：三子模块都从它 import 并装饰其上——避免「仅 re-export _list.router 丢三张路由」；注册次序 = 子模块 import 次序 list → full_merge → expand，与现文件内定义次序一致，route_table digest 钉死）；
- `_list.py`（~600 行）：lite-v2 §8 排序/投影/cursor/Link 解析/lease 取数族 + `_judge_pack_tail`（W2 产物）+ `messages` 路由（两尾部）；
- `_full_merge.py`（~400 行）：`_CapExceeded`/placeholder/ref 对/`_dedicated_full_get`/`_fetch_full_shared`/`_merge_fulls`/`_merge_fulls_and_pack` + `message` 路由；
- `_expand.py`（~380 行）：12 个 `_extract_*` + `_EXPAND_EXTRACTORS` 表 + `_expand_fragment_worker`/`_expand_fragment` + expand 两路由（rev1 N-1：现树 12 个提取器非 16；messages.py 现为 1696 行）；
- `__init__.py`：按原定义序 import 三子模块 + re-export `router` 与历史公开名**及被测试直接 import 的私有名**（`_parse_link_next_cursor`、`_judge_pack_tail` 等——rev1 P-3：它们是 import-only 零 patch，需的是 re-export 而非 patch 迁移）。

**monkeypatch 目标迁移（rev1 P-3 修订，实测 5 文件 8 目标）**：
| 文件:行 | 目标 | 处置 |
|---|---|---|
| test_messages_coalesce.py:477/:628 | `_project_list_sorted_and_pack` | → `messages._list.X` |
| test_messages_merged.py:939 | `read_with_cap` | **跨模块共享名**（full 族 + list 族消费）——按被测路径逐消费者改（:939 测 full 路径 → `_full_merge.read_with_cap`） |
| test_messages_merged.py:1096 | `fulls`（singleflight registry stub） | → `_full_merge.fulls`（stub 类，假绿高危——定向测试必须红转绿验证） |
| test_offload_equivalence.py:362 | `_messages_via_lease` | → `messages._list._messages_via_lease` |
| test_etag.py:917 | `compress_if_beneficial` | **跨模块共享名**（list 尾经 `_judge_pack_tail` + expand worker 消费）——spy 语义按被测路由归 `_list` 命名空间 |
| test_message_fingerprint.py:378/:447 | `recompute_fingerprint` | → `messages._full_merge.recompute_fingerprint`（rev2 R-2：唯一消费点在 `_merge_fulls_and_pack` :819，归 full-merge 族） |
- **多消费者共享名规则**：patch 目标 = 被测执行路径真正解析的命名空间；两族都覆盖的用例拆两处 patch 或收敛共享 helper 归属（泳道内裁量，报告记录）。
- 排除项守恒：test_v3_etag_domain / test_v3_envelope 不动（C 阶段）。
- 验收：W2 金样 29 case + N6 全部 case 回放不变（messages 路径字节面）+ 上列测试全绿 + 路由表 digest 不变 + check.sh。

## 4. W3-3：questions/permissions 共享聚合框架（F-304 蓝本）

- 新增 `routes/_aggregate_fanout.py`（~250 行）：`_DirFetchFailure`、目录发现输入装配（`/experimental/session?roots=true` 行集 → directory 列表）、`collect_with_byte_budget` 字节预算器、semaphore 注入的并发 fan-out、per-dir 错误收集；参数化 item 投影/路径（`/question` vs `/permission`）。
- `questions.py`/`permissions.py` 薄壳化（各 ~120 行）：各自 envelope 打包器（`_pack_questions_envelope`/`_pack_permissions_envelope`）、字段映射、路由。**两 envelope 的字段集与错误路径零变化**。
- 新增 `tests/test_aggregate_fanout.py`：预算器/错误收集直接单测（当前仅经两路由间接覆盖）。
- 验收：N6 questions/permissions ×8 case digest 不变 + 三测试文件全绿 + check.sh。

## 5. B 阶段：Wave 1 残留 mini-lane（A 后）

- `routes/events.py:211`、`routes/token_stream.py:290`：`"reconnect_no_replay"` 字面量 → `hub_types.RESYNC_RECONNECT_NO_REPLAY`（值不变，F-122 AST gate 绿）。
- `skeleton.py:256` 历史搁置注释行核对：若指涉已决事项则改写为现状陈述（零行为）。
- `docs/operations.md:329` 冗余对照句删除（行号已漂移，按语义定位 §5.5 env 表附近）。

## 6. C 阶段：B12（tests-only，三批串行）

配方 = 计划 :87 + 评估文档 §3：
1. **12 双视图文件 v4 自包含 golden 化**（两批，各 6 文件）：等价断言（v4 响应 == v3 响应动态对照）→ v4 期望**字面化/golden 化**（内联字面或 golden 文件），v3 半区保留为显式 v3 锁定断言（其存在目的变为守护 v3 不变直到 Phase 4 拆除）。名单（test-census §8）：test_degraded_observability、test_expand_href_v4、test_method_boundary_v4、test_post_actions_v4、test_providers_projection_v4、test_readiness_gating_integration、test_selector、test_sessions_v4_matrix、test_sse_replay_wire、test_v4_dual_window、test_v4_observability、test_versions_readiness。
2. **106 v3 单态函数三分处置**（8 文件）：①消费梯子类（错误码/目录语义对 v4 同样生效）→ selector `3`→`4` 改写保留；②纯 v3-only 行为类（envelope X-Next-Cursor、SSE 握手预填、blanket resync、ETag wire=v3 域）→ **原样保留**（v3 守护网，进白名单）；③观测维度类（wireVersion "3"、selectorResult v3 断言）→ 原样保留至 Phase 3（进白名单）。
3. **294 处 `v=3` 字面清理**：仅清非语义双写（如参数化表里冗余 `"3"` 值维度）；**行为差异点不动**（`_expand_wire_view` 生成的 href `?v=3` vs `?v=4`、v3 域 ETag 期望等）。
4. **完成判据**：全量 check.sh 绿 + `rg -l 'v=3|"3"' tests/` 输出 == 白名单文档 `docs/ocmar/plans/2026-08-21-batch3-b12-whitelist.md`（逐文件列保留理由：②守护网/③观测/显式 v3 契约锁定）。
5. **字节锚文件最后动**（test_v3_rawbody_regression 等 PYTHONHASHSEED=0 基线；评估风险 6）；每批后全量 check.sh。
6. **v4-contract 自包含化（F-126）不在本 Wave**：划归 v4 分支 **v3 面拆除之前的第一个动作**（满足「B12 全部子项中唯一必须在任何 v3 面拆除之前完成」的顺序纪律；本 Wave 完成测试侧 B12 = v4 分支开工门要件）。**结构性落点（rev1 P-7）**：Wave 3 收尾时将「F-126 自包含 = v4 分支第一动作」写入批次三计划 v4 分支节（追加注记行）与 handoff 文档 §4.3，最终报告向 owner 明示。

## 7. 执行序与收尾（编排者）

A（W3-1/W3-2/W3-3 并行，写域互斥机器校验）→ 全量 check.sh → B（残留）→ check.sh → C（B12 三批，每批 check.sh）→ CHANGELOG `[4.7.0]`（Refactor，纯重构零 wire）→ check.sh → `./scripts/release.sh minor` → push → 本机部署 → 四件套终验。
**零 wire 变更声明**：全部泳道 = 纯移动/提取/测试改造/字面→常量（值不变）；N6+W2 双金样 + 3347 全量测试兜底。

## 8. 风险

- Mixin `__init__` 序：四 Mixin 均不含 `__init__`（纯方法/属性移动），容器初始化留在壳——无 MRO 初始化冲突。
- patch 目标迁移遗漏 → 假绿/红：泳道收尾定向 pytest + 全量 check.sh 兜底；迁移清单入报告备查。
- B12 误触行为差异点：href `?v=3`、ETag v3 域期望、字节锚——设计点名 + 逐处确认纪律 + 白名单终检。
- 双金样 hashseed 敏感：子进程模式已固化；C 阶段改动 tests 时金样 harness 本身不在改写面（test_offload_equivalence/test_refactor_equivalence 不属 B12 12+8 名单）。

## 9. 验收清单

- [ ] 派发前写域交集机器校验 = ∅；
- [ ] N6 token_frames harness 修正 + 金样重录（4 帧基线，断言 ≥3）——**已完成（rev1 P-1）**；
- [ ] W3-1：patch 勘测 = string-form ∪ object-form 别名 setattr；test_sse_replay_wire 五行 patch 目标随迁（仅该五行）；`apply_debug_budget_overrides`/`TOKEN_*` 迁 budgets.py 且 TestDebugBudgetOverrides 绿；N6 token_frames digest 不变；迁移清单入报告；
- [ ] W3-2：迁移清单按实测 5 文件 8 目标（含 read_with_cap/fulls/compress_if_beneficial/recompute_fingerprint 四个跨模块名）+ 多消费者共享名规则 + `__init__` re-export 含被直接 import 的私有名；单一共享 router（`_router.py`）机制落地，路由次序与 route_table digest 不变；
- [ ] W3-3：N6 questions/permissions ×11 case（×8 基础 + discovery_5xx + fanout_window12 滑窗多批序 + truncated——config 压 `questions_max_aggregate_bytes` 真触发，harness 断言信封 `truncated:true`）digest 不变 + 共享预算器直测新增 + 三测试文件全绿；
- [ ] B 残留四点落地（resync 字面量 → 常量；F-122 gate 绿）+ config.py:26 docstring 的 `apply_debug_budget_overrides` `:func:` 引用随 W3-1 迁移更新（rev2 R-4，泳道顺手或 B 阶段）；
- [ ] B12：12 双视图 golden 化 + 106 三分处置 + 字面清理 + 白名单文档落盘；`rg -l 'v=3|"3"' tests/` == 白名单（白名单文档记录该 pattern 对无关 `"3"` 字面的已知噪声与处置口径）；字节锚文件最后动；
- [ ] F-126 = v4 分支第一动作的注记写入批次三计划 v4 分支节 + handoff §4.3（Wave 3 收尾时）；
- [ ] 每阶段全量 check.sh 绿（最终一次为准入）；
- [ ] CHANGELOG `[4.7.0]`（Refactor；编排者）→ `release.sh minor` → push → 部署 → 四件套终验（编排者）。

## 10. 评审记录

- **rev1（2026-08-21，独立评审 agent，对抗式只读）**：FAIL **8.1**——MAJOR×3：P-1 token_frames 金样退化快照（扁平信封 → LivePart 永不创建 → 仅 1 帧）；P-2 test_sse_replay_wire 5 处 object-form setattr patch 被 W3-1 排除项遗漏 + 勘测口径漏 object-form；P-3 W3-2 patch 清单失真（实测 5 文件 8 目标，点名 2 个零 patch 幻影、漏 4 个跨模块高危名）。MINOR×4：P-4 apply_debug_budget_overrides 变异面、P-5 单 router 机制、P-6 聚合缺 discovery/truncated case、P-7 F-126 归属需结构落点（裁定本身成立）。全部修订已吸收（§0/§1/§2/§3/§6/§9）。
- **rev2（同评审 agent 复审）**：FAIL **8.6**——rev1 修订 13/14 实测确认；剩 MAJOR R-1（`questions_truncated` case 实未触发截断：`_MAX_AGGREGATE_ITEMS=10_000` 项数截断不可达，金样钉的是普通 12-item 信封，字节级复算驳斥）+ MINOR R-2（迁移表 `recompute_fingerprint` 行命名空间应为 `_full_merge`）。修法：改名 `questions_fanout_window12` + 新增 config 驱动真截断 case + 双态守卫断言 `truncated:true`。
- **rev3（确认性复核）**：**PASS 9.5**——R-1/R-2 修复经字节级复算与双态回放验证（truncated 信封真触发：`truncated:True, items:7, authoritativeDirectories=['/d00'..'/d06']`，digest 吻合；回放两绿），放行派发。残留 NOTE×3（permissions 侧分支由共享面论证背书、expand 端点靠定向测试、评审记录补录——本条即补录）。
