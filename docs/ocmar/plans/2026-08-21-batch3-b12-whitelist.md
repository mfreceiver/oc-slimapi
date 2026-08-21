# B12 白名单：`rg -l 'v=3|"3"' tests/` 保留清单 + 106 函数三分处置全量清单

> 批次三 Wave 3 Phase C（B12）C2 批（最后一批）交付物，2026-08-21。
> 依据：`docs/ocmar/plans/2026-08-21-batch3-full-rollout.md` :87（B12 完成判据）、
> `docs/ocmar/reviews/2026-08-21-v3-retirement-reassessment.md` §3（三分处置配方）、
> `docs/audits/2026-08-20/04-final/v3-retirement-plan.md` §4（106 函数清单口径）。
> 本批写域：8 个 v3 单态测试文件 + 本文档。C1a/C1b 已改文件（8 个）未触碰；
> 禁改 CHANGELOG/契约文档；未跑 RECORD 模式金样；pytest 恒 `-p no:cacheprovider`。

**执行摘要**：106 函数 = ①改写 30 / ②保留 37 / ③保留 39。改写全部跑绿（无回退案例：
凡预判改写失败的（v4 行为差异点）直接判 ②/③ 未做改写尝试，逐函数理由见 §2）。
v=3 字面清理计数 **0**——8 文件内剩余 `v=3`/`"3"` 字面逐条核对均为语义性（②守护网 /
③观测维度 / 字节锚 golden），无「非 v3 语义双写」的冗余行可删（参数化表无与 v4 维度
重复的 `"3"` 值行）。

---

## 1. 完成判据核对：`rg -l 'v=3|"3"' tests/` 当前完整输出（43 文件）逐文件保留理由

> 命令：`rg -l 'v=3|"3"' tests/ | sort`（2026-08-21，B12 C2 批落盘后工作区实测）。
> 分类图例：**②守护网** = 纯 v3-only wire 行为，Phase 4 拆除前守护网；**③观测维度** =
> access log / snapshot 的 wireVersion/selectorResult 维度断言，Phase 3 收窄改写；
> **①已改写残留** = 本批 ① 改写后函数内仍遗留 v=3 字面（**应为零**，实测为零——本批
> 30 个 ① 改写函数已全部换至 `?v=4`/wire "4"，下列 8 文件中的残留命中全部位于 ②/③
> 函数体内）；**字节锚** = PYTHONHASHSEED=0 / 逐字节基线；**golden "3" 面 key** =
> capabilities["3"] 等冻结载荷中的面键；**金样输入** = W2/W3 golden 矩阵以 ?v=3 捕获
> 的基线输入（改输入即作废 hash）；**准入管线** = ?v=3 仅作 (3,4) 窗口内合法准入视图
> 使用的功能测试（被测语义与版本无关或 v4 等价；本批写域外，Phase 4 前随 v4 面统一
> 改写）；**噪声** = 与 wire 版本无关的 "3" 字面（§3 口径）。

### 1a. 本批（C2）处置的 8 文件

| 文件 | 保留理由（一句话） |
|---|---|
| tests/test_v3_directory.py | ②守护网（consuming-set 参数化含 sessions 行 v4=directory_retired_in_v4、v2 形态拒绝 ×3、sessions v3 消费、tolerant+health v3 视图锁）+ ③观测维度（directoryForm 两函数）；①18 函数已改写无残留 |
| tests/test_traffic_snapshot_v3.py | ③观测维度全文件（§9.2 聚合矩阵：wireVersion="3"/selectorResult 维度数据值、sseActive 四维、ledger "v3" 节） |
| tests/test_access_log_v3_fields.py | ③观测维度（§9.1 行字段 wireVersion=="3"/selectorResult=="v3"/directoryForm/生命周期行）+ ②守护网 1 函数（legacy 旧形态拒绝 + supported:[3,4] 窗口字面） |
| tests/test_v3_sse_meta.py | ②守护网（v3 meta 字段集冻结、blanket resync ×2、tokens=1、恒 identity 编码、v2 拒绝 ×4、无 scope 直连防御）+ ③观测维度（生命周期配对 ×2）；①2 函数（头退役跨视图）已改写无残留 |
| tests/test_v3_envelope.py | ②守护网（sessions v3 envelope ×3、sessions 上游 5xx 管线、v2 拒绝 ×2）；①5 函数（messages 零差异族 + status）已改写无残留 |
| tests/test_v3_etag_domain.py | ②守护网（b"wire=v3" 指纹域 ×2、默认视图=3、v2 validator 拒绝、sessions v3 304/Vary——v4 全局列表无 ETag）；①2 函数已改写无残留 |
| tests/test_health_dual_view.py | ②守护网（health v3 视图三元组、退役头不改视图、无 v 拒绝 + supported:[3,4]）+ 头值枚举 "3" 噪声（:84）；①3 函数已改写无残留 |
| tests/test_v3_rawbody_regression.py | 字节锚（PYTHONHASHSEED=0 子进程基线；BASELINE_VERSIONS_BODY 内含 capabilities "3" 面 key 的逐字节冻结）——4 函数全 ②，零改动 |

### 1b. C1a/C1b 已处置文件（8，本批未触碰，各文件头部已带 B12 清点注记）

| 文件 | 保留理由 |
|---|---|
| tests/test_expand_href_v4.py | ②字节锚：href `?v=3` 逐字节回归（v3 selector → v3 href 是行为差异点，B12 三字节锚之一） |
| tests/test_readiness_gating_integration.py | ②守护网/golden：门控关态 v4 已发布行为 golden 中的 href `?v=3` 字面 + `params={"v":"3"}` v3 面守护请求 |
| tests/test_selector.py | ②守护网：selector 双视图矩阵的 `?v=3` 准入半区（selector 自身即被测分叉）+ 头值 "3" 噪声（:95/:139） |
| tests/test_sessions_v4_matrix.py | ②守护网：v3-only 参数轴（roots/start/limit≤1000）与 v3×v4 互斥 422 轴（:583/:594）+ `"limit": "3"` 数据噪声（:422/:426） |
| tests/test_sse_replay_wire.py | ②字节锚/守护网：v3 无 id 帧字节锚半区 + parametrize `["3","4"]` 的 v3 侧（:1106） |
| tests/test_v4_dual_window.py | golden "3" 面 key（capabilities "3" 终态面，:114）+ ②守护网 ?v=3 半区（C1a/C1b 注记明示三分②） |
| tests/test_v4_observability.py | ③观测维度（wireVersion=="3" 行断言 :149、聚合行数据 :241） |
| tests/test_versions_readiness.py | golden "3" 面 key：capabilities["3"] 终态形状冻结回归锁（§0.5 v3 freeze） |

### 1c. 其余 27 文件（本批写域外，只读清点）

| 文件 | 保留理由 |
|---|---|
| tests/test_b2_merged_text_compat.py | 准入管线（`V3 = "?v=3"` 常量驱动 merged text 兼容测试） |
| tests/test_b4_allowlist.py | 准入管线 + directory 消费语义（file 族 `?v=3&directory=` 组合；v4 非全局列表路由等价，Phase 4 前不动） |
| tests/test_b4_new_routes.py | 准入管线（file 新路由 `?v=3` 探针） |
| tests/test_dbaux_lifecycle.py | 准入管线（health `params={"v":"3"}` 生命周期探针） |
| tests/test_degraded_observability.py | ③观测维度（wireVersion=="3" 断言 :419 + 降级行探针） |
| tests/test_expand_config.py | golden "3" 面 key（capabilities["3"]["expand"] 断言 :185-:205） |
| tests/test_expand_routes.py | 准入管线 + directory 消费（expand 路由 `?v=3`/`V3` 常量） |
| tests/test_health_features.py | 准入管线（health features 单探针 :15） |
| tests/test_method_boundary_v4.py | ②守护网（行为分叉矩阵的 v3 列：v3 三组合 → 404 vs v4 405，分叉本身即被测对象；parametrize :215/:276/:305） |
| tests/test_offload_equivalence.py | 金样输入（W2 offload 字节基线以 ?v=3 请求捕获，hash 锚定不可换输入） |
| tests/test_post_actions_v4.py | 准入管线（POST 等效族 `?v=3` 写路由探针 :552） |
| tests/test_providers_projection_v4.py | ②守护网：`?v=3`/无 selector 逐字节透传回归（B7 providers v3 面） |
| tests/test_proxy.py | 准入管线（catch-all 404 探针 `?v=3` :99） |
| tests/test_read_groups.py | 准入管线 + directory 消费（file/vcs 读组 `?v=3&directory=` 族） |
| tests/test_refactor_equivalence.py | 金样输入（W3 N6 接口矩阵 golden 以 ?v=3 请求捕获） |
| tests/test_selector_query_strip.py | 准入管线（`v` 剥离机制学，版本无关；`?v=3` 仅合法准入视图） |
| tests/test_sessions_coalesce.py | 准入管线 + directory 消费（status `?v=3&directory=` :316） |
| tests/test_session_single_v4.py | 准入管线（`V3 = {"v":"3"}` 常量）+ 噪声（`"updated": "3"` 数据字面 :766） |
| tests/test_sessions_v4_representation.py | ②守护网：v3 表示层零改动锚（门控翻转前后 `?v=3` 响应逐字节相同） |
| tests/test_skeleton_expand.py | 准入管线（`V3 = "?v=3"` 常量驱动 skeleton/expand 投影测试） |
| tests/test_terminal_matrix.py | golden "3" 面 key + ②守护网（capabilities 键 "3" 终态、V3 常量、头值 "3" 噪声 :147） |
| tests/test_traffic_snapshot.py | ③观测维度（聚合器测试 `selector_result="v3", wire_version="3"` 数据 :1059） |
| tests/test_traffic_upin_gaps.py | 准入管线（command 路由 `?v=3` 探针 :237） |
| tests/test_turn_registry.py | 准入管线（prompt_async/abort 写路由 `?v=3` 探针） |
| tests/test_vary_directory_unconditional.py | ②守护网（v3 面 Vary 锁定：messages/sessions `?v=3` 表示层断言） |
| tests/test_versions_route.py | golden "3" 面 key（caps["3"] 面逐字冻结断言 :47-:70） |
| tests/test_write_groups.py | 准入管线 + directory 消费（写路由 `?v=3&directory=` 梯子复验族） |

**①已改写残留核查：零。** 本批 ① 改写的 30 函数体内已无任何 `v=3`/`"3"` 字面
（§1a 各行注明的命中全部位于 ②/③ 函数或注释/docstring）。

---

## 2. 106 函数三分处置全量清单（文件 → 函数 → ①/②/③ + 一行理由）

> ① = selector `3`→`4` 改写后保留（本批已执行，全部跑绿）；② = 原样保留
> （Phase 4 拆除前守护网）；③ = 原样保留（Phase 3 收窄随观测/窗口面改写）。
> 凡 ②/③ 涉及「v4 行为差异点」的，理由中给出差异点出处（v4-contract 章节/源码实证）。

### 2.1 tests/test_v3_directory.py（26 函数：①18 / ②6 / ③2）

| 函数 | 类 | 理由 |
|---|---|---|
| test_agent_matrix_none | ① | agent 无 directory 直通；v4 非退役路由 §5.1 梯子逐字沿用（§5.2 set-difference 仅全局列表） |
| test_agent_matrix_query_only_consumed_and_forwarded | ① | query 单值消费+剥离+`X-Opencode-Directory` 转发；v4 同（selector.py `_consume_directory` v3 ladder verbatim） |
| test_agent_matrix_header_only_retired | ① | `directory_header_retired`；v4 同码（梯子 case 3） |
| test_agent_matrix_dual_same_normalized_retired | ① | 归一同值双现仍 `directory_header_retired`；v4 同 |
| test_agent_matrix_dual_different_conflict_fields_frozen | ① | `directory_conflict` 冻结字段 queryDirectory/headerDirectory；v4 同码 |
| test_agent_matrix_multi_same_folds | ① | 多值同值折叠单值；v4 同 |
| test_agent_matrix_multi_different_rejected | ① | `invalid_directory_selector`；v4 同码（梯子 case 1） |
| test_agent_matrix_invalid_value_rejected | ① | `invalid_directory`；v4 同（值校验继承） |
| test_messages_keeps_other_params_after_strip | ① | messages 消费后兄弟参数（limit/before）保留；messages 零 v4 差异（§10） |
| test_diff_forwards_messageid_without_directory | ① | diff 消费剥离 + messageID 保留；v4 同 |
| test_stream_v4_multi_different_invalid_directory_selector（原 test_stream_v3_…） | ① | §5.6 路由守卫 wire 无关 + v4 流端点 directory 消费保留（§5.2/§7 路由表） |
| test_stream_v4_multi_same_folds_to_single_value（原 v3） | ① | 同上（折叠后单值过继承守卫） |
| test_stream_v4_query_only_accepted_noop（原 v3） | ① | §5.6 query-only 受理 no-op；v4 流端点同 |
| test_stream_v4_dual_different_directory_not_allowed（原 v3） | ① | 继承守卫 `directory_not_allowed`；wire 无关 |
| test_stream_v4_header_only_noop（原 v3） | ① | header 由 dispatch 层先行退役；守卫 no-op；v4 同 |
| test_stream_selector_precheck_rejects_multi_different | ① | selector 预检 `invalid_directory_selector` 先于路由；v4 流端点非退役路由 |
| test_stream_selector_single_value_not_consumed | ① | 单值不入 stash 不剥离、query 原样到路由；v4 同（result 断言随改 v4） |
| test_selector_never_consumes_tolerant_paths | ① | 容忍集排除表（questions/permissions/events/health/versions/directories）；v4 容忍集不变 |
| test_consuming_routes_v3_consume_strip_forward（×7 参数化） | ② | 含 `/slimapi/sessions` 行——v4 全局列表任何 directory 形态 → 400 `directory_retired_in_v4`（§5.2），整函数级改写失败回退；其余 6 行 v4 等价性已由 ①18 函数覆盖 |
| test_v2_agent_directory_query_form_unsupported | ② | 退役 v2 形态拒绝守护（版本族 400；无 selector 字面可改写） |
| test_v2_agent_header_only_unsupported | ② | 同上 + 优先级链（②version 400 > ③directory）锁定 |
| test_v2_sessions_form_unsupported | ② | 同上（sessions v2 再加行为已随 v2 管线消亡） |
| test_v3_sessions_header_only_upstream_query_clean | ② | sessions v3 消费形态（query 消费→header 转发→上游 query 干净）；v4 该路由 directory 整体退役 |
| test_tolerant_routes_ignore_any_directory_form | ② | 容忍忽略语义 v4 等价，但函数锁定 health v3 视图（`slimapi_contract==3`；v4=4，health.py 双视图）——改写失败回退 |
| test_directory_form_observable_values_v3 | ③ | §9.1 directoryForm 四值观测（query/header/both/absent）——观测维度 |
| test_directory_form_null_on_tolerant_route | ③ | §9.1 非消费路由 directoryForm=null——观测维度 |

### 2.2 tests/test_traffic_snapshot_v3.py（20 函数：全 ③）

| 函数 | 类 | 理由 |
|---|---|---|
| test_matrix_counts_one_per_row | ③ | §9.2 计数矩阵（含 wireVersion="3" 维度数据行） |
| test_matrix_dimensions_distinct | ③ | 六维互异键（selector/wire/directoryForm/recordType/statusClass/bucket） |
| test_matrix_splits_by_date | ③ | countsByDate 按日切分 |
| test_sse_active_same_day_open_close | ③ | sseActive 当日开合 + sseLive 收尾 |
| test_sse_active_carry_in_sequence_1_open_crosses_day_unclosed | ③ | carry-in 公式（跨日未闭合） |
| test_sse_active_carry_in_sequence_2_closes_after_day_boundary | ③ | 跨日配对 close + D+2 归零 |
| test_sse_active_orphan_close_correction | ③ | 孤儿 close 钳零 |
| test_sse_close_mismatched_lifecycle_id_is_orphan_not_drain | ③ | lifecycleId 配对（防误排水位） |
| test_sse_close_after_restart_is_all_orphan | ③ | 重启窗口全孤儿 |
| test_sse_close_without_lifecycle_id_is_orphan | ③ | 无 id close 孤儿化 |
| test_sse_pairing_is_per_dim | ③ | 配对按 dim 独立（v3/absent 域不互通） |
| test_sse_matched_pairing_regression_normal_flow | ③ | 正常配对 + 双 close 第二次孤儿化 |
| test_sse_active_four_dims_independent | ③ | 四维独立（v3/v2/absent/not_applicable） |
| test_sse_not_applicable_dim_nonzero_carry | ③ | catch-all SSE not_applicable 维非零 carry |
| test_sse_open_without_v_maps_to_absent | ③ | 无 v → absent 维 |
| test_rejected_and_exempt_never_counted_as_sse_dims | ③ | rejected/exempt 不入 SSE 维 |
| test_ledger_sse_lifecycle_counters | ③ | TrafficLedger 内存 sseActive/生命周期计数（"v3" 节） |
| test_ledger_sse_lifecycle_orphan_close | ③ | ledger 孤儿钳零 |
| test_ledger_selector_request_matrix | ③ | ledger 请求矩阵（v2/rejected 维度键） |
| test_ledger_v3_section_absent_when_disabled | ③ | 关态 snapshot 无 "v3" 节 |

### 2.3 tests/test_access_log_v3_fields.py（16 函数：③15 / ②1）

| 函数 | 类 | 理由 |
|---|---|---|
| test_row_no_v_rejected | ③ | 无 selector → rejected/null 行（§9.1 行字段） |
| test_row_v2_explicit_rejected | ③ | 显式 v2 → rejected 行 |
| test_row_v3 | ③ | 核心 ③ 断言：selectorResult=="v3" + wireVersion=="3" |
| test_row_rejected | ③ | v9 词法合法超集 → rejected |
| test_row_exempt | ③ | versions → exempt |
| test_row_not_applicable_for_catch_all | ③ | 非 /slimapi 路径 → not_applicable（`/plain?v=3` 探针） |
| test_directory_form_query | ③ | directoryForm=query（gate 400 仍留行） |
| test_directory_form_header | ③ | directoryForm=header |
| test_directory_form_both | ③ | directoryForm=both |
| test_directory_form_absent_on_consuming_route | ③ | directoryForm=absent |
| test_directory_form_null_on_non_consuming_route | ③ | 非消费路由 directoryForm=None（v3 准入 + tolerant） |
| test_legacy_row_key_prefix_preserved | ③ | legacy 行首 14 键序 + 加性尾字段 |
| test_legacy_old_ocdroid_form_rejected | ② | 旧 ocdroid 形态 400 + `supported:[3,4]` 版本窗字面（Phase 3 收窄时随窗口改写；非观测维度故归 ②守护） |
| test_events_sse_open_close_rows | ③ | events SSE 生命周期行 + selectorResult/wireVersion 向行传播 |
| test_sse_helpers_no_selector_scope_defaults_absent | ③ | 无 selector 作用域 → absent 维（legacy 栈） |
| test_lifecycle_ids_monotonic | ③ | lifecycleId 进程单调（版本无关观测机制） |

### 2.4 tests/test_v3_sse_meta.py（15 函数：①2 / ②11 / ③2）

| 函数 | 类 | 理由 |
|---|---|---|
| test_v4_events_response_has_no_subscriber_id_header（原 v3） | ① | X-Slimapi-Subscriber-ID 3.0.0 头退役跨视图生效（§1；v4 面同不发） |
| test_v4_stream_response_has_no_subscriber_id_header（原 v3） | ① | 同上（token 流端点） |
| test_v3_events_meta_first_frame_default_tokens_false | ② | v3 meta 字段集冻结 `{subscriberId,tokens}`；v4 meta 加性扩展 capabilities/epoch/seqBase（events.py §7.0②）→ 改写失败 |
| test_v3_events_meta_tokens_true_with_tokens_param | ② | tokens=1 于 v4 → 400 `tokens_stream_retired_in_v4`（events.py:91-99） |
| test_v3_events_meta_before_resync_replay | ② | v3 blanket resync（任意 Last-Event-ID → reconnect_no_replay）；v4 为真重放/四类短路 |
| test_v2_events_form_rejected_before_stream | ② | v2 形态 400 先于开流（守护） |
| test_v2_events_explicit_selector_rejected | ② | 同上（显式 ?v=2） |
| test_v3_stream_meta_first_tokens_true | ② | v3 流端点 meta tokens:true 冻结（v4 meta 形状不同） |
| test_v3_stream_meta_before_resync_replay | ② | v3 blanket resync（流端点，meta→resync→握手帧序） |
| test_v3_stream_identity_despite_gzip_accept | ② | v3 SSE 恒 identity 冻结；v4 流随协商 gzip（§7 差异面） |
| test_v2_stream_form_rejected_before_stream | ② | v2 形态 400 先于 token 流 |
| test_v2_explicit_selector_stream_rejected | ② | 同上（显式 ?v=2） |
| test_stream_route_no_scope_request_ok | ② | 无 scope 直连调用防御（默认 v3 视图；无 selector 可改写） |
| test_v3_events_close_after_meta_pairs_lifecycle | ③ | meta 后即终流的 sse_open/close 配对（行断言 selectorResult=="v3"） |
| test_v3_stream_close_after_meta_pairs_lifecycle | ③ | 同上（token 流，bucket=token_stream_sse） |

### 2.5 tests/test_v3_envelope.py（11 函数：①5 / ②6）

| 函数 | 类 | 理由 |
|---|---|---|
| test_messages_v4_envelope_null_cursor_byte_verbatim（原 v3） | ① | messages envelope 字节形（items/nextCursor:null 键序）；v4 messages ≡ v3 逐字节（§10 零差异） |
| test_messages_v4_envelope_non_null_cursor（原 v3） | ① | 上游 Link → nextCursor 逐字透传；零差异 |
| test_messages_v4_error_response_not_enveloped（原 v3） | ① | 错误体非 envelope（code 形）；投影失败 502 路径零差异 |
| test_messages_v4_304_empty_body_no_aux_headers（原 v3） | ① | 304 头集冻结 ETag+Vary+no-store；validator 值随 wire=v4 域但头集相同（§10/§15） |
| test_sessions_status_v4_not_enveloped（原 v3） | ① | status map 透传；§12 明示零 v4 分叉（无版本分支代码路径） |
| test_messages_retired_v2_forms_rejected | ② | v2 隐/显两形态 400 + `supported:[3,4]` 窗口字面（Phase 3 随窗口改写） |
| test_messages_retired_v2_304_form_rejected | ② | v2 形态上呈 validator → 先拒（v3 validator 取自 v3 面，函数保持一致） |
| test_sessions_v3_envelope_complete_true | ② | sessions v3 envelope（上游 HTTP 管线）；v4 全局列表 = DB 投影管线（§4），非等价 |
| test_sessions_v3_envelope_complete_false | ② | 同上（limit 截断 complete=false 语义） |
| test_sessions_v3_304_no_x_complete_header | ② | v3 sessions 304；v4 全局列表无 ETag/304（§4 表行「无 ETag」） |
| test_sessions_error_response_not_enveloped | ② | v3 上游 5xx → 503 `upstream_unavailable` 管线；v4 全局列表 503 路径为 `auxiliary_unavailable`（§4 降级矩阵）——非 v4 等价 |

### 2.6 tests/test_v3_etag_domain.py（8 函数：①2 / ②6）

| 函数 | 类 | 理由 |
|---|---|---|
| test_v4_etag_same_request_stable_and_own_view_304（原 v3） | ① | messages 同请求同 validator + 本视图 304；§10 零差异 + §15 REP wire=v4 域内同语义 |
| test_v4_etag_changes_with_envelope_content（原 v3） | ① | envelope 内容（nextCursor）变化旋转 validator；零差异 |
| test_representation_version_wire_marker_unit | ② | `b"wire=v3"` 指纹域锁定（B6 ETag wire=v3 validator 域——配方②点名例） |
| test_representation_version_terminal_default_is_v3 | ② | 默认视图=3 终态锁定（`wire_view_from_scope` 缺省 v3） |
| test_retired_v2_request_never_issues_validator | ② | v2 请求 400 不发 validator + v3 面 304 守护（v2/v3 域互配锁定半区） |
| test_v3_validator_on_retired_v2_form_rejected | ② | v3 validator 于 v2 形态先拒（守护） |
| test_v3_304_header_set_exact_sessions | ② | v3 sessions 304 精确头集；v4 全局列表无 ETag（§4）→ 改写失败 |
| test_vary_never_mentions_v_or_directory_params | ② | v3 sessions Vary=Accept-Encoding 锁定；v4 该请求形态（?v=4&directory=）即 400 退役（§5.2） |

### 2.7 tests/test_health_dual_view.py（6 函数：①3 / ②3）

| 函数 | 类 | 理由 |
|---|---|---|
| test_ready_v4_request_keeps_frozen_v3_view_no_contract_field（原 test_ready_single_v3_view_no_contract_field） | ① | ready 零 v4 分叉（§12/health.py READY_VIEW=3）：?v=4 请求仍回冻结 v3 视图值、无 contract 字段 |
| test_ready_upstream_down_503 | ① | ready 上游宕 → 503（版本无关管线） |
| test_health_deployment_revision_omitted | ① | deploymentRevision 省略（None 时两视图同；v4 视图仅加 auxiliary） |
| test_health_single_v3_view | ② | health v3 视图三元组锁定（slimapi_contract/api_version/schema.version==3；v4=4，B10 双视图） |
| test_health_retired_header_cannot_change_view | ② | 退役头任意值不改 v3 视图（v3 视图值断言） |
| test_health_no_v_rejected | ② | 无 v → 400 unsupported_version + `supported:[3,4]` 窗口字面（Phase 3 随窗口改写） |

### 2.8 tests/test_v3_rawbody_regression.py（4 函数：全 ② 字节锚，零改动）

| 函数 | 类 | 理由 |
|---|---|---|
| test_sessions_v3_raw_body_bytes | ② | 字节锚：PYTHONHASHSEED=0 子进程 v3 sessions body+ETag 逐字节基线 |
| test_health_v3_raw_body_bytes | ② | 字节锚：v3 health body 逐字节基线 |
| test_versions_raw_body_bytes | ② | 字节锚：versions body（capabilities "3" 面 key 字节冻结） |
| test_v3_sessions_key_order_nondeterminism_recorded | ② | 字节锚配套：skeleton set 迭代键序非确定性发现锚 |

**合计**：①30（directory 18 / sse_meta 2 / envelope 5 / etag 2 / health 3）；
②37（directory 6 / access_log 1 / sse_meta 11 / envelope 6 / etag 6 / health 3 / rawbody 4）；
③39（directory 2 / traffic_snapshot 20 / access_log 15 / sse_meta 2）。合计 106。

---

## 3. 噪声口径声明：`rg 'v=3|"3"'` 对无关 `"3"` 字面的已知噪声及判定方法

已知噪声类型（本批实测命中）：

1. **数据值 "3"（与 wire 版本无关）**：`tests/test_session_single_v4.py:766`
   `{"time": {"created": 1, "updated": "3"}}`（updated 字段字符串）；`tests/test_sessions_v4_matrix.py:422/:426`
   `"limit": "3"`（limit 参数值）。
2. **退役头矩阵值 "3"**：`X-Slimapi-Version` 值枚举（`("2","3","9")`）中的 "3"——
   被测语义是「头不被读取」，非 selector：test_selector.py:95/:139、
   test_terminal_matrix.py:147、test_health_dual_view.py:84。
3. **注释/docstring 提及**：各文件头部 B12 清点注记与契约引用行中的 `?v=3` 文字
   （非断言、非请求）。
4. **parametrize 版本值 "3"**：双视图对称矩阵的 v3 侧值（如 test_sse_replay_wire.py:1106
   `["3","4"]`）——语义性（②守护半区），列入对应文件保留理由，不作噪声。

**判定方法**：对每处命中执行 `rg -n 'v=3|"3"' <file>` 逐条核对语境——
(a) 位于请求 URL/params 的 selector 位（`?v=3`、`params={"v":"3"}`）→ 语义性（②/③/准入管线/金样输入）；
(b) 位于断言的维度值或冻结载荷键（`wireVersion=="3"`、`selectorResult=="v3"`、`caps["3"]`、golden href）→ 语义性；
(c) 头值枚举成员、业务数据值、注释文字 → 噪声。
本批 8 文件内的噪声命中仅 1 处（test_health_dual_view.py:84 头值枚举，②函数体内，保留）。

---

## 4. B12 完成声明（三项判据）

1. **check.sh 绿**：`./scripts/check.sh` 全量通过——pytest 3369 passed（0:01:54）+
   路由↔文档一致性（54 条 /slimapi 路由全记录）+ compileall src，尾行 `✅ check.sh 通过`。
   （泳道内定向验收另含：8 文件定向 112 项全绿 + 双金样回放
   `test_refactor_equivalence.py::test_refactor_golden_matrix` /
   `test_offload_equivalence.py::test_golden_matrix` 绿，REPLAY 模式，未跑 RECORD。）
2. **白名单落盘**：本文件（§1 43 文件逐条 + §2 106 函数全量 + §3 噪声口径）。
3. **rg 输出 ⊆ 白名单**：`rg -l 'v=3|"3"' tests/` 当前输出 43 文件与 §1 清单逐一对应
   （1a 8 + 1b 8 + 1c 27 = 43），无未收录文件；①已改写残留为零。

> v4 分支开工门（B3 前移条款）在此判据全过的基础上对 B12 视为**已完成**；
> ②/③ 函数的 Phase 3/4 改写归属见批次三总计划 v4 分支节，不在本批写域内。
