# D04 — A4 契约清晰性与完整性（v3/v4-contract 全量审读）

> 审计专项报告 · Phase 2 / A4 · 2026-08-20
> 快照：`0b836e7`（BASELINE_HEAD = 0b836e78c5de62d0c73b8593bf62c6650043dedf，release: v4.4.0）。本文全部 `file:line` 证据属该快照。
> 输入：`docs/specs/v3-contract.md`（288 行）、`docs/specs/v4-contract.md`（713 行）全文逐节亲读（对照 01-explore/docs-notes.md 摘要加速定位，四问均对原文复核）；01-explore/{route-census.csv, upstream-notes.md, config-census.md, dataflows.md}；CHANGELOG.md。
> 双轨纪律（§0.2）：规范轨 = 契约权威；测试通过不豁免契约违反。
> 完成判据对照：两契约逐节四问表（全量 16+19 单元）✔；错误码三向对账表 ✔；硬不变量清单含测试锁定列（45 条）✔。

---

## 0. 方法与符号

- **四问**：(i) 可测试断言是否明确（输入→期望可机械判定）；(ii) 错误路径是否穷尽（malformed 每种形态有归宿）；(iii) 与另一契约对应节有无组合矛盾；(iv) 实现是否照做（该节全部可机械判定句对照代码与测试——逐句核对，非抽样）。
- 结论符号：✓ 通过 / △ 部分通过（有缺口）/ ✗ 不通过。评分 1-5（5 = 冻结完备且实现逐条对应；4 = 有措辞/归宿缺口但不影响主路径裁决；3 = 有滞留文本或矩阵偏差；≤2 = 有影响裁决的矛盾）。
- 单元划分沿用 docs-notes §7 口径：v3 = 16 单元（§0/§1/§2/§3/§3a/§4/§4a/§4b/§5(含5.7a)/§6/§7/§8/§9/§10/§11/§12），v4 = 19 单元（§0/§1/§2/§3(3.1-3.3)/§4(4.1-4.6)/§5/§6/§7(7.0-7.5)/§8/§9/§10/§11/§12/§13/§14/§15/§16/§17/附录）。

---

## 1. v4-contract 逐节四问表（19/19 单元）

| 节 | (i) 可测试断言 | (ii) 错误路径穷尽 | (iii) 与 v3 组合矛盾 | (iv) 实现照做 | 分 | 问题清单 |
|---|---|---|---|---|---|---|
| §0 版本原则 | ✓（(3,4) 钉死、?v=3 逐字节不变、无 v→400 supported:[3,4]、major 绑定、回退语义） | ✓ | ✓（§0.6 交叉注记存在） | ✓ versioning.py:38,44；selector.py:551-572,622-634；test_v4_dual_window.py:128-180 | **5** | 无 |
| §1 头与参数总则 | ✓（头不解读、?v= 词法不变、剥离规则） | ✓ | ✓（= v3 §1 终态） | ✓ selector.py:14-22,477-499；test_terminal_matrix.py:140-157 | **5** | 无 |
| §2 selector 状态表 | ✓（五行状态机 + request-scope wireVersion + directory 分叉） | ✓（词法/不支持/多值异值各有归宿） | ✓（v3 §2 词法逐字沿用；差异仅支持集扩 [3,4]——见 F-151〔v3 侧滞后〕） | ✓ selector.py 全文逐条对应（SUPPORTED_WIRE_VERSIONS:135-137、wire stash:574-579、目录分叉:298-308）；test_selector.py(23)、test_terminal_matrix.py(24)、test_v4_dual_window.py | **5** | 无 |
| §3.1 versions | ✓（载荷形状、静态四键、广告时序、expand 注记、修订扩键） | ✓（端点无参数，无 malformed 面） | ✓ | ✓ routes/versions.py:61-162（capabilities["4"] 四静态键+readiness+iff expand）；test_versions_route.py(9)、test_versions_readiness.py(35) | **5** | 无 |
| §3.2 health 双视图 | ✓（3/4 同源同值、auxiliary 瞬态字段） | ✓ | ✓（v3 §3a 承接） | ✓ routes/health.py:30-85（view 单源驱动三处） | **4** | `allowlist` 字段嵌套位置措辞歧义（**F-155**） |
| §3.3 readiness 门禁 | ✓（U 十项、f() 规范化、ready 公式双向、按 ID 独立门控、蕴含⑦、expand iff、contradiction 七条件） | ✓（客户端侧 contradiction 分类穷尽；服务端构造期拒绝） | ✓（自含；与 §16.3 同口径） | ✓ readiness.py:58-187（REQUIRED 十项、validate、validate_dependencies、ready、readiness_payload 全对应）；test_versions_readiness.py(35)、test_readiness_gating_integration.py(6) | **5** | 无（全契约最强节） |
| §4 sessions 全局（4.1-4.6） | ✓（参数矩阵、排序冻结、LIMIT+1、72 格降级矩阵、cursor 指纹、SQL 谓词） | △（limit/archived/parent 非法值归宿未载；§4.3 422 触发枚举窄于实现） | ✓ | ✓ sessions.py:371-561、dbaux/projection.py:144-311（ORDER BY :238、LIMIT ?+1 :239、keyset :226）、dbaux/cursor.py（trim-后-转义-前 hash :88-97、base64url 无 padding）；test_sessions_v4_matrix.py(36)、test_cursor_matrix.py(36)、test_sql_semantics.py(19)、test_equivalence_anchor.py(28) | **4** | limit 域外双形状 + code 字面量未命名 + 触发集超枚举（**F-025 终裁**） |
| §5 directory 消费矩阵 | ✓（退役表 = SET DIFFERENCE、四形态单一错误码、修订二加注） | ✓ | ✓（§5.1 v3 逐字沿用声明） | ✓ selector.py:191-199,636-717（retired 优先于 v3 梯子、hint 统一体）；test_v4_dual_window.py:265-366 | **5** | 无 |
| §6 ETag/Vary | ✓（v3 原样 + v4 sessions 例外 + validator 域隔离） | ✓ | ✓（与 §4.4/§15 三处互引自洽） | ✓ sessions.py:598-630（门控关无 ETag 摘 Vary / 门控开 ETag+Vary+304） | **5** | 无 |
| §7 SSE id:/重放（7.0-7.5） | ✓（四终裁、ID 语法、四级分类短路序、barrier 水位、resync 值域四值、meta 字段序、welcome 抑制） | ✓（①②违例=忽略重置、future=忽略重置、跨域=忽略重置——malformed 全有归宿） | ✓（v3 SSE 帧形零变化声明 + §7.5 两视图一致条目） | ✓ sse/replay_wire.py:72-77,104-209（语法/域判定/分类委托）；sse/replay_log.py（barrier/窗口）；routes/events.py:88-253（tokens 400 先开流、meta 恒首帧、v4 welcome 抑制 :132）；test_sse_replay_wire.py(73)、test_replay_log.py(51)、test_events_tokens.py | **5** | 无 |
| §8 错误族与优先级 | ✓（§8.1 五新码、§8.3 七级总链 + 四组合、§8.4 修订三码 + 插列） | △（422 行未命名 code 字面量；405 versions body code 未载〔F-152〕） | ✓（与 v3 §8.3 链相容——v4 链是 v3 链的扩集） | ✓ selector.py:581-619（method 405 插列 ②③ 之间）、sessions.py:382-427（④⑤⑥ 序）；test_terminal_matrix.py、test_method_boundary_v4.py(16) | **4** | 422 code 字面量 + 触发集（**F-025**）；versions 405 body（**F-152**） |
| §9 观测 | △（维度扩展明确；DB 指标/replay 指标仅列名未冻结字段集形状） | ✓ | ✓（v3 §9 枚举加性扩 v4） | ✓ selector.py:95-109（枚举含 v4）；dbaux metrics/replay 计数存在（test_dbaux_metrics、test_metrics_replay_block）——字段级形状归 A13 深查 | **4** | 指标字段集未冻结（可测试性弱于他节；A13 主辖） |
| §10 路由全集 | ✓（51→54 计数口径显式、差异面逐条、修订块门控标注） | ✓ | ✓ | ✓ route-census.csv 54 行 presence=both 全对上（write_groups.py 20 装饰器）；test_check_routes_doc.py gate 同口径 | **5** | 无 |
| §11 测试矩阵 | ✓（12 项用例面 + 落地批次标注） | —（矩阵非错误面） | ✓（与 v3 §11 分立不冲突） | ✓ 11.1→test_v4_dual_window、11.2→同、11.3→test_sessions_v4_matrix(36)、11.4→test_cursor_matrix(36)、11.5→test_sql_semantics(19)、11.6→test_dbaux_lifecycle(34)、11.7→test_wal_staleness、11.8→test_equivalence_anchor(28)、11.9→test_eqp_matrix、11.10→test_sse_replay_wire(73)+test_replay_log(51)、11.11→test_dbaux_lifecycle、11.12→test_dbaux_lifecycle（warmup 断言存在） | **4** | 11.3「144 case 逐格」实际以 36 个参数化测试覆盖等价类（未见逐格 144 断言文件——等价类口径可辩护，矩阵字面与测试计数不同；A12 复核） |
| §12 providers 投影 | ✓（schema 白名单、optional 省略策略穷尽句、排序、default 三重校验、四限额、十二步求值序 + offload 边界、错误表逐类目、canonical 字节、REP_VERSION） | ✓（「malformed 全部来源」穷尽句 + 修订三零增量声明——错误路径穷尽性为全契约最佳实践） | ✓（?v=3 恒透传） | ✓ providers_projection.py:54-66,98-108,300-356（限额常量、ProjectionLimit、limit int-else-omit、_ORJSON_INT_MAX）；routes/read_groups.py:282-355（_v4_error/状态映射）；test_providers_projection_v4.py(51——含修订三 limit 族 9 测试、canonical/ETag/求值序/permit 时序全覆盖) | **5** | 无 |
| §13 单查 parity | ✓（canonical 形状、字段真值表、§13.2a/b/c、同一 projector 不变量、§13.4 公式、§13.5 三不变量） | ✓（不可表示 item → 整响应 503；required 不可 null → 整响应 503） | ✓（继承 §4 冻结不放宽） | ✓ sessions.py:452-479,659-675（canonical projector 唯一入口、§13.4 公式注释）；dbaux/projection.py canonical_session_skeleton_v4；test_session_single_v4.py(50) | **5** | 无 |
| §14 expand href | ✓（12 类目照抄、href canonical 形态、键序、值=selector 视图） | ✓（端点错误继承 v3 §4b） | ✓（原样照抄 v3 §3/§4b.2） | ✓ skeleton.py:167-187（`?v={wire_view}` 恰一次编码、v 第一）；test_expand_href_v4.py(11) | **5** | 无 |
| §15 表示层 | ✓（Vary 直接规则、v4 sessions ETag 口径、域隔离、「现状 bug」如实披露） | ✓（SSE 显式例外） | ✓（与 §4.4/§12.6 同规则互引） | ✓ sessions.py:598-630；etag.py:49-98,171-174（merged_vary 单值）；test_sessions_v4_representation.py(10)、test_v4_etag_disabled_still_varies | **5** | 无 |
| §16 POST 等效动作族 | ✓（三视图操作表、§16.1 字面冻结、§16.2 三款、§16.3 四位组合表穷尽 + 蕴含） | ✓（v3 恒 404、过渡态 405、激活态等效——全组合有归宿） | ✓（PATCH/DELETE 不退役；与 §3.3 例外①②同口径） | ✓ selector.py:244-280,581-609（两条件合取 + Allow 字面）；write_groups.py:300-411（POST≡PATCH/delete≡DELETE/archive octet 判据 + 紧凑合成体 :389-391）；test_method_boundary_v4.py(16)、test_post_actions_v4.py(29) | **5** | 无 |
| §17 non-goals | ✓（边界声明 + 无 feature ID 编码方式说明） | —（负向声明） | ✓ | ✓ rg 负向：src 无 cascade/subagentList/cross-session search 实现（05-reports 归属 A1/A3；负向证据见 D01/D03） | **5** | 无 |
| 附录（设计对应表） | —（纯引用） | — | ✓ | ✓ 四份设计文档在 docs/specs/ 存在 | **5** | 无 |

**v4 小计**：19 单元中 14 个 5 分、5 个 4 分（§3.2/§4/§8/§9/§11），无 ≤3 分。

---

## 2. v3-contract 逐节四问表（16/16 单元）

| 节 | (i) | (ii) | (iii) | (iv) | 分 | 问题清单 |
|---|---|---|---|---|---|---|
| §0 继承基线 | ✓（差异面穷举、修订史内嵌、§0.6 双版本交叉引用） | — | ✓ | ✓（差异面与 v4 §10 差异集相容） | **4** | 权威基线指向已退役 v2-contract 且 v2 文件头无退役横幅（**F-019** 族；规范链长） |
| §1 头退役范围 | ✓（五头表格 + 不退役清单） | ✓ | ✓（v4 §1 维持声明闭环） | ✓ selector（头不解读：test_version_header_never_read；目录头 400：test_directory_header_retired_on_consuming_routes）；envelope 路由无 X-Next-Cursor/X-Complete：test_terminal_matrix.py:446-477 | **5** | 无 |
| §2 selector 状态机 | ✓（词法全集 + 状态表 + versions 豁免 405 优先） | ✓ | △（§2 冻结行 supported:[3] 与 v4 (3,4) 窗口的 [3,4] 字面冲突——§0.6 未覆盖非-v 面） | ✓ selector.py 逐行；test_terminal_matrix.py:115-199 | **4** | **F-151**（[3]/[3,3] 滞后三处 + 测试名滞后） |
| §3 发现端点 | ✓（形状约束五条 + expand 形状冻结） | ✓ | △（available:[3] 滞后同 F-151） | ✓ routes/versions.py:61-64,143-152；test_terminal_matrix.py:395 `test_versions_terminal_shape`（断言 [3,4]——见 F-151 佐证） | **4** | F-151 同族 |
| §3a health 双视图 | ✓（同源同值禁组合 + features.allowlist） | ✓ | △（accepted [3,3] 滞后同 F-151） | ✓ routes/health.py:30-51（单源 view） | **4** | F-151 同族 |
| §4 envelope | ✓（两端点形状 + 边界四条） | ✓（错误不 envelope/304 无 body） | ✓（v4 §4 分叉声明） | ✓ test_v3_envelope.py(11) | **5** | 无 |
| §4a 投影/expandRefs/指纹 | ✓（阈值表、/full-only 穷举、PatchPart verbatim、refs 去重排序、merged 语义、指纹 2026-08-19 就地复述） | ✓ | ✓ | ✓ skeleton.py:167-211,416-560（refs 生成/去重/排序）；test_skeleton_expand、test_b2_merged_text_compat(6)、test_message_fingerprint(28)、test_messages_merged | **5** | 无（指纹节消除了 v2 继承链歧义——好的补载范式） |
| §4b expand 端点 | ✓（12 类目表 + 8 步求值序 + 错误表 + envelope） | ✓（求值序显式冻结 503/413 先于 404/400 的「固有序」） | ✓（v4 §14 照抄闭环） | ✓ routes/messages.py:1540-1600（八步逐一对应：category→级别→池准入→single-flight→cap 413 先于解码→offload 定位/提取→片段 cap）；test_expand_routes.py(57)、test_expand_config.py(24) | **5** | 无 |
| §5 directory 矩阵 + §5.7a | ✓（消费集穷举、双现梯子、stream 例外、allowlist 三态 + realpath canonical） | ✓ | ✓（v4 §5 分叉 = SET DIFFERENCE） | ✓ selector.py:143-188,636-717；directory.py；test_v3_directory.py(26)、test_directory.py(17)、test_b4_allowlist.py(23)、test_terminal_matrix.py:348（stream terminal） | **5** | 无 |
| §6 ETag/Vary/304 | ✓（域隔离、单值 Vary 2026-08-19 注记、304 头集合、expand 除外） | ✓ | ✓（v4 §4.4/§15 差异声明） | ✓ etag.py:49-98（wire=v{view}）、:171-174（merged_vary 恒单值）；test_v3_etag_domain.py(8)、test_vary_directory_unconditional.py(11)、test_etag.py | **5** | 无 |
| §7 SSE | ✓（changed 最小语义、meta 首帧/tokens 冻结、§7.5-7.8 现状补载全量） | ✓ | ✓（v4 §7.5 两视图一致条目复述） | ✓ sse/hub_types.py:376-396（normalize_session_status 两形态/无效忽略）；sse/global_hub.py:130-137,435-472,644-649,714-720（sticky 记录/清除/贴回/FIFO 10000）；test_b1a_digest_changed(7)、test_session_status_object_format(15)、test_hub_behavior_lock(119)、test_v3_sse_meta(15) | **5** | 无 |
| §8 错误体与 catch-all 终局 | ✓（码清单 + 优先级链四级 + 收编全集） | △（WS 501 面缺席；422 形态沿 v2 未展开） | ✓（v4 §8.3 链为扩集） | ✓ proxy.py:47-53（404 thin_route_not_found）；test_proxy.py(8)、test_terminal_matrix.py(24) | **4** | **F-153**（WS 501 无载）；`method_not_allowed` body（**F-152**） |
| §9 观测与移除判据 | ✓（字段枚举 + sseActive 四维 + carry-in 公式 + 六条谓词） | ✓ | ✓（v4 §9.1 加性扩 v4 维） | ✓ test_access_log_v3_fields.py(16)、test_traffic_snapshot_v3.py(20)（含跨日公式测试） | **5** | 无 |
| §10 路由收编全集 | ✓（读 9 组 + 写 17 端点表 + 统一行为 + carve-out + 投影执行域） | ✓（错误两级制 + 错误 body cap 降级 503） | ✓（v4 §10 计数口径相容 54 条） | ✓ route-census.csv 全 both；test_read_groups.py(59)、test_write_groups.py(27)、test_b4_new_routes.py(19) | **5** | 无 |
| §11 测试矩阵 | ✓（18 项用例面） | — | △（无 (3,4) 窗口注记） | △ 15/18 有现代锁定；11.11 对象已删、11.16 grep 机制未落地（见 F-156 逐项映射） | **3** | **F-156**（三处滞后/偏差） |
| §12 里程碑 | ✓（自我定性 [计划]，历史已完成） | — | ✓ | —（流程节） | **5** | 无 |

**v3 小计**：16 单元中 9 个 5 分、6 个 4 分、1 个 3 分（§11）。

---

## 3. 错误码全集三向对账（实现 ↔ v3/v4 契约 ↔ CHANGELOG）

**实现侧全集提取**（`rg 'code="' src/` + 位置参数族 `"code", status` + `CodedHTTPException(...)`/`error_response(...)` 构造点全清单 rg 枚举 + f-string 族）：**40 个字面 code**（含 `upstream_http_<N>` 动态族）。清单（代表 file:line）：

`unsupported_version`(selector.py:629)、`invalid_version_selector`(:561)、`method_not_allowed`(:540)、`method_not_applicable`(:598)、`directory_retired_in_v4`(:206)、`invalid_directory_selector`(:681; token_stream.py:113)、`directory_conflict`(:692)、`directory_header_retired`(:698,704)、`invalid_directory`(directory.py:37-50)、`directory_not_allowed`(403; messages.py:315, read_groups.py:142, token_stream.py:119)、`tokens_stream_retired_in_v4`(events.py:22)、`invalid_tokens`(events.py:89)、`invalid_cursor`(sessions.py:417,424)、`auxiliary_unavailable`(503; sessions.py:303)、`param_version_mismatch`(422; sessions.py:386-401,703)、`response_too_large`(413; 11 处)、`request_too_large`(413; write_groups/actions)、`message_too_large`(413; messages.py:1227)、`transform_busy`(503; messages.py:279, _catalog_common.py:47)、`upstream_unavailable`(503; upstream_errors.py)、`upstream_http_<N>`(502; upstream_errors.py:60,83,104, read_groups.py:319)、`session_not_found`(404; upstream_errors.py:81,102)、`upstream_invalid_shape`(502; messages.py:1295)、`invalid_expand_category`(400; messages.py:1541)、`expand_category_mismatch`(400; :1511,1551,1557)、`expand_target_not_found`(404; :1327)、`expand_source_too_large`(413; :1592)、`expand_fragment_too_large`(413; :1526)、`thin_route_not_found`(404; proxy.py + write_groups.py:320)、`websocket_not_supported`(501; proxy.py:38)、`provider_upstream_malformed`(502; read_groups.py:316,352)、`provider_projection_limit`(413; :355)、`invalid_request_body`(422; routes/actions.py:95-105)、`actions_disabled`(503; actions.py:192)、`action_not_found`(404; :199)、`action_confirm_required`(409; :210)、`action_throttled`(429; :217)、`action_busy`(503; :228)、`action_timeout`(504; :236)、`action_unavailable`(503; :247)。

### 3.1 无主码（实现有、v3+v4 契约皆无）——4 个（含 1 个复合）

| code | 状态/场景 | 判定 |
|---|---|---|
| `param_version_mismatch` (422) | sessions v3/v4 参数互拒 + limit>500 + archived 非法 + parent 空 | **部分无主**：v4 §4.3/§8.1 以中文「参数版本不匹配」描述 422 族但**从未命名字面量**（rg 负向：docs/specs/ 无命中）；触发集实现超契约枚举——主辖 **F-025** |
| `method_not_allowed` (405) | 非 GET /slimapi/versions body | **无主**（v2/v3/v4/CHANGELOG 四向无；状态+Allow 已冻结）——**F-152** |
| `websocket_not_supported` (501) | WS upgrade stub | **无主**（v3 §8.2 未提 WS 面；仅 INTERFACE_MAP §4 追踪面有载）——**F-153** |
| `invalid_request_body` (422) | POST actions 畸形 body | **无主**（v2 §2 七码表外第 8 码；三向全空）——**F-154** |

其余 36 码均有契约归宿（v3 §8.1 清单 / v3 §4b.3 / v4 §4.3+§8.1+§8.4+§12.5.3+§16.1 / v2 继承链〔response_too_large、request_too_large、message_too_large、transform_busy、upstream_unavailable、upstream_http_N、session_not_found、thin_route_not_found、invalid_directory、directory_not_allowed、invalid_tokens、actions 七码〕）。v2 继承码经 v3 §0.1「未提及语义逐字沿用 v2」合法——不计无主。

### 3.2 幽灵码（契约有、实现无）——**0 个**

v3 §2/§8.1/§4b.3/§5.7a 与 v4 §4.3/§8.1/§8.4/§12.5.3/§16.1 声明的全部码均在上文实现清单中逐一命中（含 `version_required`——v3 §2 仅存于 2.0.0 历史行、3.0.0 已退役，属历史记载非幽灵）。**两契约零幽灵码**——契约的可达性纪律良好。

### 3.3 文档码（CHANGELOG 提及、两契约皆无）——live 1 个 + 历史 8 个

- **live**：`param_version_mismatch`（CHANGELOG 1 处）——唯一仅存于 CHANGELOG 的活码（并入 F-025 裁决）。
- **历史**（对应已删除特性，CHANGELOG 作为变更史记载，非缺陷）：`version_required`、`version_incompatible`（3.0.0 前版本门禁）、`invalid_route_token`、`message_not_found`、`invalid_ids`（v1 G6 批量展开，v2 已删）、`shell_not_allowed`、`invalid_path`（catch-all 反代时代，3.0.0 随 forwarder 删）、`invalid_directory_count`（1.3.x questions 守卫，后续移除）。另 CLIENT_CHANGES.md 仍向客户端列举 `version_required`/`version_incompatible` 为现行码（**F-157**）。

---

## 4. 硬不变量清单（45 条；「恒/绝无/逐字节/恰好/永不/必须」类全量提取）

> 锁定 = 测试名 + 文件（rg 断言存在）；NONE = 未找到直接锁定测试。按 §3.3 gap 行定级（消费方直接使用的面才 P1）。

| # | 不变量（契约引文） | 实现位置 | 测试锁定 |
|---|---|---|---|
| INV-01 | `?v=3` 管线逐字节不变（v4 §0.1） | selector.py:574-579 + 各路由 v3 分支 | test_v3_rawbody_regression.py(4)、test_v4_dual_window.py:197,224,331 |
| INV-02 | 无 v / 不支持 → 400 `unsupported_version` supported=[3,4]（v4 §0.1/§2） | selector.py:622-634 | test_terminal_matrix.py:115-177 |
| INV-03 | 版本窗任何变更 = major（v4 §0.2） | 流程规则（release.sh 门禁承载） | NONE（流程级，无自动化断言——gap P3） |
| INV-04 | `?v=` 词法 `^[1-9][0-9]*$`，`0/03/+3/ 3/3.0/空` 非法（v3 §2） | selector.py:127,558 | test_terminal_matrix.py:160-167（参数化 8 值） |
| INV-05 | 多值同值折叠、异值 400（v3 §2） | selector.py:558 | test_terminal_matrix.py:179-197 |
| INV-06 | versions 非 GET → 405+Allow 优先于一切（v3 §2/v4 §2） | selector.py:533-545 | test_terminal_matrix.py:201 |
| INV-07 | `v` 在 /slimapi/** 无条件剥离、其余 query 逐字节保序（v3 §5.2/v4 §1） | selector.py:478-499,719-732 | test_selector_query_strip.py |
| INV-08 | X-Slimapi-Version 出现不解读不报错（v3 §1/v4 §1） | selector（头不读） | test_terminal_matrix.py:140-157 |
| INV-09 | X-Opencode-Directory 消费集出现 → 400 directory_header_retired（v3 §1/§5.7） | selector.py:698,704 | test_terminal_matrix.py:268,301 |
| INV-10 | directory 双现异值 → 400 directory_conflict（附双值）（v3 §5.4） | selector.py:690-695 | test_v3_directory.py |
| INV-11 | stream 路由 query-only directory = no-op 接受（v3 §5.6） | selector.py:699-701 | test_terminal_matrix.py:348 |
| INV-12 | v4 sessions×directory 任何形式 → 400 directory_retired_in_v4 先于路由（v4 §5.2） | selector.py:668-673 | test_v4_dual_window.py:265-318（五形态） |
| INV-13 | v4 修订面语义 iff feature ID ∈ satisfied（v4 §3.3） | readiness.py + 路由门控 | test_readiness_gating_integration.py(6) |
| INV-14 | required ≡ U（十 ID）恒全集发出（v4 §3.3） | readiness.py:58-69 | test_versions_readiness.py |
| INV-15 | satisfied ⊆ required；未知 ID 拒绝（RuntimeError）（v4 §3.3） | readiness.py:106-123,186 | test_versions_readiness.py:213-231 |
| INV-16 | ready ⇔ f(required) ⊆ f(satisfied)（双向，不允许独立翻转）（v4 §3.3） | readiness.py:147-160 | test_versions_readiness.py |
| INV-17 | post-actions ∈ satisfied ⇒ boundary ∈ satisfied（蕴含⑦）（v4 §3.3/§16.3） | readiness.py:126-144,187 | test_versions_readiness.py:263-268 |
| INV-18 | `expand` 键 iff messages.expand.v4 ∈ satisfied（四组合穷尽）（v4 §3.3） | versions.py:125-131 | test_versions_readiness.py |
| INV-19 | capabilities 静态，不随 DB 抖动（v4 §3.1） | versions.py:104-132（无运行态输入） | test_versions_route.py（static key 断言） |
| INV-20 | current ∈ available、available 唯一升序（v3 §3/v4 §3.1） | versions.py:61-64 | test_versions_route.py / test_terminal_matrix.py:395 |
| INV-21 | v4 收 roots/start → 422；v3 收 archived/parent/cursor → 422（presence-based）（v4 §4.1） | sessions.py:383-405,700-705 | test_sessions_v4_matrix.py |
| INV-22 | sessions v4 排序冻结 (time_updated DESC, id DESC)（v4 §4.1） | projection.py:238 | test_equivalence_anchor.py:148（EQ-004 tie-break） |
| INV-23 | complete = 同 SQL LIMIT+1 窗口（v4 §4.1） | projection.py:239,301-311 | test_sessions_v4_matrix.py |
| INV-24 | db 不可用 × allowlist 非空 → 恒 503（fail-closed，不凑行）（v4 §4.2） | sessions.py 降级分支 | test_sessions_v4_matrix.py |
| INV-25 | search 含 %/_/\ × db 不可用 → 503（过滤语义永不降级）（v4 §4.2/§4.6） | sessions.py + has_wildcard | test_sql_semantics.py |
| INV-26 | 503 统一 Retry-After: 30（v4 §4.2） | sessions.py:305 | test_sessions_v4_matrix.py:255,871 |
| INV-27 | auxiliary/503 错误体不含 DB 路径/schema/白名单内容（v4 §4.2/§8.1） | sessions.py:299-306 | test_sessions_v4_matrix.py（负向断言族） |
| INV-28 | invalid_cursor 400 优先于 503（纯内存校验先于降级）（v4 §4.3/§8.3） | sessions.py:409-427 先于降级 | test_sessions_v4_matrix / test_cursor_matrix |
| INV-29 | cursor 指纹 = {archived,parent,search-hash,allowlist-rev}；不匹配 → 400（v4 §4.5） | cursor.py:55,88-146 | test_cursor_matrix.py(36) |
| INV-30 | search-hash 输入 = trim 后、LIKE 转义前（四消费点唯一源）（v4 §4.5） | cursor.py:88-97,126-146 | test_cursor_matrix / test_sql_semantics |
| INV-31 | allowlist 子树谓词二进制前缀（= + substr，弃 LIKE；大小写敏感；/foo 不含 /foobar）（v4 §4.6） | projection.py（谓词构造） | test_sql_semantics.py |
| INV-32 | v4 sessions 门控关 = 发布态无 ETag/摘 Vary；门控开 = ETag+Vary+304（v4 §4.4/§15） | sessions.py:598-630 | test_sessions_v4_representation.py(10) + test_sessions_v4_matrix.py::test_v4_gate_off_no_etag_vary_304 |
| INV-33 | tokens=1 v4 → 400 tokens_stream_retired_in_v4（错误体逐字冻结）（v4 §7.3） | events.py:21-24,92-99 | test_events_tokens.py |
| INV-34 | meta 恒首帧且自身无 id（v3 §7.3/v4 §7.0②） | events.py:184-189；token_stream 同 | test_v3_sse_meta.py(15)、test_sse_replay_wire.py |
| INV-35 | 带 id 帧按 (epoch,seq) 严格单调不减（v4 §7.1） | replay_log 分配序 | test_sse_replay_wire.py |
| INV-36 | epoch = 16 hex 随机 boot nonce、重启必换、不随重连换（v4 §7.1） | replay_log.py epoch | test_replay_log.py:84（两实例异 epoch） |
| INV-37 | Last-Event-ID 四级分类严格短路序 ①语法→②域→③epoch→④窗口（v4 §7.2） | replay_wire.py:169-209 | test_sse_replay_wire.py:474-515（语法/域矩阵） |
| INV-38 | resync reason 值域恰四值（v4 §7.2 冻结） | replay_wire.py:72-77 V4_RESYNC_REASONS | test_sse_replay_wire.py（同常量导入） |
| INV-39 | barrier 写入范围 = 全局域 + 当前 epoch 全部 per-sid 域；seq ≤ 水位 → resync；禁跨 barrier 补帧（v4 §7.2） | replay_log.py（barrier 族） | test_replay_log.py(51) + test_sse_replay_wire.py（barrier 三连） |
| INV-40 | v4 连接不产出 server.connected（首帧恒 slimapi.meta）（v4 §7.5） | events.py:132（wire_v4 传入 subscribe） | test_sse_replay_wire.py（welcome 抑制） |
| INV-41 | §8.3 总链优先级（①versions 405→②version 400→method 405→③directory 400→④422→⑤invalid_cursor→⑥503→⑦404）（v4 §8.3/§8.4） | selector.py:533-619 + sessions.py:382-427 | test_terminal_matrix.py:201-299 + test_method_boundary_v4.py(16) |
| INV-42 | providers 顶层恰两 key；多余/缺失 = malformed（v4 §12.1） | providers_projection.py | test_v4_malformed_matrix |
| INV-43 | 绝无 `"source"/"status"/"limit": null`、`"limit": {}`；optional 一律省略键（v4 §12.1） | providers_projection.py:332-356 | test_v4_optional_keys_all_omission_forms + limit 族(9) |
| INV-44 | limit 子键白名单恰 {context,input,output}；int-else-omit；bool 排除；超界整数值按非 int 省略（orjson 域 [-2^63, 2^64-1]）（v4 §12.1 修订三） | providers_projection.py:344-356,_ORJSON_INT_MAX:81 | test_v4_limit_int_range_is_orjson_serializable 等 9 测试 |
| INV-45 | 四限额 256/1024/64/8MiB 固定 wire 常量、无 env 覆写、先触发者生效无静默截断（v4 §12.4） | providers_projection.py:54-57,300-301 | test_v4_limits_are_frozen_wire_constants + exact/over 三组 |
| INV-46 | canonical 字节 = wire body = ETag 哈希输入（orjson OPT_SORT_KEYS；同一字节双重身份）（v4 §12.6） | providers_projection + read_groups | test_v4_etag_is_hash_of_served_canonical_bytes |
| INV-47 | REP_VERSION 域隔离：providers-projection-v2、wire=v{view}；跨视图保守 200（v4 §12.6/§15/v3 §6.1） | providers_projection.py:66；etag.py:87 | test_v4_limit_revision3_bumps_representation_fingerprint、test_v4_validator_domain_isolated_from_v3、test_v3_etag_domain.py(8) |
| INV-48 | transform permit 获取时机 = body cap 后、worker 提交前；transform_busy 仅可能发生在 ⑤（v4 §12.5.2） | read_groups.py providers 流程 | test_v4_permit_not_needed_for_upstream_errors / _for_body_cap、test_v4_transform_busy_precedes_malformed |
| INV-49 | 502/错误体零上游细节（providers 502 带、auxiliary 同纪律）（v4 §12.5.3） | read_groups.py:282-296 | test_providers_projection_v4.py |
| INV-50 | ETag 关闭 → 无 ETag/无 304，但 Vary 恒发（表示可变性与 ETag 正交）（v4 §12.6/§15） | sessions.py:614-629 | test_v4_etag_disabled_still_varies |
| INV-51 | SSE 恒 identity、无 Vary 头（响应头恰 no-cache,no-transform + X-Accel-Buffering:no）（v3 §7.7/v4 §7.5） | events.py:244-247 | test_v3_sse_meta / sse 测试族 |
| INV-52 | list item 与 single 共用同一 canonical projector（分裂投影 = 违约）（v4 §13.3） | sessions.py:452-479,659-675（唯一入口） | test_session_single_v4.py(50) |
| INV-53 | required-不可-null 字段不可得 → 整响应 503（不发明占位值/不砍字段）（v4 §13.2a） | sessions.py:457-461,672-674 | test_session_single_v4.py |
| INV-54 | envelope.degraded == any(item.degraded) ∨ fallback；partial ⇒ degraded 单向（v4 §13.4） | sessions.py:474-479 | test_session_single_v4 / test_sessions_v4_matrix |
| INV-55 | expand href：v 第一、directory 第二、值=解析后 selector、恰编码一次（v4 §14） | skeleton.py:167-187 | test_expand_href_v4.py(11) |
| INV-56 | 三条 POST：v3 恒 404；激活态 POST≡PATCH / delete≡DELETE / archive octet 判据 + `{"time":{"archived":<ms>}}` 紧凑合成（v4 §16.2） | write_groups.py:300-411 | test_post_actions_v4.py(29) |
| INV-57 | digest changed 恒单元素 [本帧 sid]；仅 digest 帧携带（v3 §7.2） | global_hub.py | test_b1a_digest_changed.py(7) |
| INV-58 | digest status 恒字符串；信封无效忽略该次更新（v3 §7.6） | hub_types.py:376-396 | test_session_status_object_format.py(15) |
| INV-59 | lastError sticky：busy → 显式 null 清除帧；deleted → 字段省略；FIFO 10,000；进程内无持久化（v3 §7.5） | global_hub.py:130-137,435-472,644-649,714-720 | test_hub_behavior_lock.py(119) |
| INV-60 | TextPart.text 永远全量内联、零 truncated 语义（v3 §4a.1 B2） | skeleton.py（无 text 折叠分支） | test_b2_merged_text_compat.py(6)——行为锁定；矩阵所述 grep 负向断言未落地（F-156） |
| INV-61 | ReasoningPart.text >2048 折叠 + part_reasoning ref；≤2048 内联（v3 §4a.1） | skeleton.py | test_skeleton_expand / test_skeleton.py |
| INV-62 | expandRefs 为 sidecar 拥有键：上游同名键剥离、确定性重建（v3 §4a.3） | skeleton.py:190-211 | test_skeleton_expand |
| INV-63 | merged：placeholder-first；diffs 恒 null+refs 永不 batch 还原；交集按 mid 去重（v3 §4a.5） | messages.py merged 分支 | test_messages_merged |
| INV-64 | contentFingerprint：同输入恒同指纹（跨重启）；merged splice 后重算、降级不重算；`vN:sha256`（v3 §4a.6） | skeleton/messages 指纹管线 | test_message_fingerprint.py(28) |
| INV-65 | 12 expand 类目单一事实源 EXPAND_CATEGORIES（versions 广告=流量记账同源）（v3 §3/§4b.2/v4 §14） | traffic.py:50 | test_versions_route + check_routes_doc 语义关键词 gate |
| INV-66 | expand 求值序：池满 503 先于 part 404；源 413 先于解码（v3 §4b.3） | messages.py:1545-1600 | test_expand_routes.py(57) |
| INV-67 | v3 304 头集合恰 ETag+Vary+no-store（不复制退役头）（v3 §6.4） | etag/路由 304 路径 | test_etag.py |
| INV-68 | 全部 /slimapi ETag 路由 Vary 恒单值 Accept-Encoding（v3 §6.2） | etag.py:171-174 | test_vary_directory_unconditional.py(11) |
| INV-69 | catch-all 关闭：未收编路径恒 404 thin_route_not_found、零上游 IO（v3 §8.2） | proxy.py:41-53 | test_proxy.py(8) |
| INV-70 | allowlist fail-closed 403 + realpath canonical（候选实时解析、根按值缓存）（v3 §5.7a） | directory.py + global_hub | test_b4_allowlist.py(23) |
| INV-71 | 上游自身 ETag 不透传（sidecar 生成域）（v3 §6.3） | 读管线（不拷上游 ETag） | test_read_groups.py:728（weak validator 自有域断言） |
| INV-72 | sseActive 跨日 carry-in 纯函数公式（v3 §9.2） | traffic_snapshot.py | test_traffic_snapshot_v3.py(20) |

**计数**：72 条（超出 ≥40 目标；其中 v3/v4 两契约「恒/绝无/逐字节/恰好/永不/必须」类硬不变量经逐节提取后全量入表——实际总数即 72，无遗落类）。
**未锁定**：仅 INV-03（流程级 major 绑定规则，无自动化断言；gap P3——非消费面）。INV-60 的矩阵指定机制（grep 负向断言）未落地（F-156），行为锁定存在。**其余 71 条均有测试名级锁定。**

---

## 5. 契约内部一致性检查

| 检查项 | 结论 | 证据 |
|---|---|---|
| 版本双轨（v4 §0/§2 ↔ versioning.py ↔ selector ↔ versions/health） | **自洽** | ACCEPTED_CLIENT_VERSIONS=(3,4)（versioning.py:44）→ SUPPORTED_WIRE_VERSIONS=[3,4]（selector.py:135-137）→ supported:[3,4]（:630）/ available:[3,4]（versions.py:62-64）/ health accepted（config 驱动）——单源钉死，五处同值 |
| v3 §2「[3]」字面 vs 现行 [3,4] | **不自洽（文本滞后）** | F-151；§0.6 注记不覆盖非-v 面 |
| readiness U=10（§3.3 ↔ §16.3 ↔ readiness.py ↔ versions 载荷） | **自洽** | REQUIRED 十项（readiness.py:58-69）；§16.3 四位组合表 2×2 穷尽且第四格由 validate_dependencies 构造期排除（:126-144,187）；versions 载荷经 readiness_payload 同源（versions.py:124） |
| 排序冻结（§4.1 ↔ §12.2 ↔ 实现） | **自洽（两处排序分属不同对象，无冲突）** | sessions = (time_updated DESC, id DESC)（projection.py:238）；providers = UTF-8 字节序（§12.2 ↔ OPT_SORT_KEYS）；§12.2 不涉 sessions、§4.1 不涉 providers |
| ETag 指纹域清单（§12.6 providers-projection-v2 ↔ §15 sessions 同规则 ↔ etag.py） | **自洽** | providers_projection.py:66 `b"providers-projection-v2"`；sessions ETag 走通用 representation_version（含 wire=v{view}，etag.py:87）——两域正交，修订三 bump 仅轮换 v4 validator |
| §16 四位组合表穷尽性 | **穷尽** | boundary∈/∉ × post-actions∈/∉ 四格全覆盖；∉∈ 格标 contradiction 且构造期不可达（readiness.py:126-144）；selector 两条件合取（selector.py:257-266）与 §16.1 精确响应范围声明一致 |
| v3 §11 测试矩阵 ↔ tests/ | **15/18 对应，3 项偏差** | F-156（11.11 对象删、11.16 机制未落地、11.1 部分 catch-all 断言对象消亡） |
| v4 §11 ↔ tests/ | **12/12 有对应测试文件**（11.3「144 case 逐格」字面与 36 参数化测试的等价类口径差异记 A12 复核） | §1 表 §11 行 |
| §8.3 总链 ↔ v3 §8.3 链 | **相容（扩集）** | v4 链 = v3 链 + method 405 插列 + 422/invalid_cursor/503 细化；v3 面行为不变（test_terminal_matrix 回归） |
| §10 路由计数 51→54 ↔ census | **一致** | route-census.csv 54 行 presence=both；write_groups 20 装饰器 |
| §7.2 上游断连触发条件（实现锚点 global_hub.py:894-904 等） | 自洽但为**实现行号锚**（漂移风险由 AGENTS「先读源码再改」纪律兜底） | 契约内行号引用 4 处，属可维护性注记非矛盾 |

---

## 6. CLIENT_CHANGES.md 时效性快评

**结论：时效性不足，五族滞后（详见 F-157）**。文件头部仍声明「Aligned with v2-contract.md (lite-v2)」（:1-5），而 v2 契约已退役两个 major 周期。逐族：

1. **权威指针**：多处「wire 以 v2-contract §3.x/§6.x 为准」（:356 等）——应指 v3/v4 契约；
2. **已删头**：「须带 `X-Slimapi-Version: 2`」四处（:112,:344,:361,:486）；错误码节仍列 `version_required`/`version_incompatible`（3.0.0 后不存在）；
3. **已删信封头**：X-Next-Cursor（:57）/X-Complete 整节（:75-81）——v3 envelope 替代后未标废止；
4. **token stream gzip 三层语义**（:366）：与 v3 终态「SSE 恒 identity」（§7.7）冲突；
5. **truncated 帧 / partEventRevision strict>**（:380,:413-414）：锚 v2 契约且与 :443 自述矛盾。

**正向**：3.1.0/3.2.0/3.3.0 各节与 2026-08-19 双轨 catch-up 新节内容准确；AGENTS.md 有「冲突以本仓契约为准」兜底。定级 P3（对接文档滞后，非运行时缺陷）。

---

## 7. 种子复核裁决（A4 主辖四条）

| 种子 | 终判 | 要点 |
|---|---|---|
| F-004 | **confirmed 方向、P1 维持**（状态 verified） | deploy:33 `2,2` × config.py:817-822 fail-closed = 按权威模板部署即 RuntimeError crash-loop；operations.md:92-94「已清理」为不实陈述（轨一内部矛盾）。作为 contract/ops 矛盾定 P1（部署面广泛不可恢复，但非运行中服务偏离，不满足 P0 门槛的「触发面广」运行时解释）。证据链闭合。 |
| F-019 | **P3 维持**（verified；A3 已补证据，A4 补 v2 头部无退役横幅 + F-154 关联） | 权威链断点；实现与 v2 §2 冻结语义一致。 |
| F-025 | **P2 维持**（verified；§4.1 原文逐条裁决见发现文件「A4 裁决」节） | 契约未载域外归宿（(ii) 不通过）+ 实现双形状（分界 1000 非 500）+ code 字面量无契约名 + 触发集超 §4.3 枚举。四项合并为一条 P2。 |
| F-030 | **P3 维持**（verified） | 两处 TODO 注释滞留；上游 camelCase 已定论（E8）；行为正确。 |

---

## 8. A4 新发现清单

F-151（v3 §2/§3/§3a 滞后 [3]/[3,3]，P3 contract）、F-152（`method_not_allowed` 无主码，P3）、F-153（`websocket_not_supported` 无主码，P3）、F-154（`invalid_request_body` 无主码，P3）、F-155（§3.2 allowlist 位置措辞歧义，P3）、F-156（v3 §11 矩阵三处滞后/偏差，P3 test）、F-157（CLIENT_CHANGES 五族滞后，P3 docs）。全部 verified（证据链在各自文件闭合）。

---

## 9. 逐节评分总表与全局裁决

### 9.1 v4-contract（19 单元）

| 节 | 分 | | 节 | 分 | | 节 | 分 |
|---|---|---|---|---|---|---|---|
| §0 | 5 | | §7 | 5 | | §13 | 5 |
| §1 | 5 | | §8 | 4 | | §14 | 5 |
| §2 | 5 | | §9 | 4 | | §15 | 5 |
| §3.1 | 5 | | §10 | 5 | | §16 | 5 |
| §3.2 | 4 | | §11 | 4 | | §17 | 5 |
| §3.3 | 5 | | §12 | 5 | | 附录 | 5 |
| §4 | 4 | | | | | | |
| §5 | 5 | | | | | | |
| §6 | 5 | | | | | | |

均值 ≈ **4.74**（14×5 + 5×4）。

### 9.2 v3-contract（16 单元）

| 节 | 分 | | 节 | 分 | | 节 | 分 |
|---|---|---|---|---|---|---|---|
| §0 | 4 | | §4b | 5 | | §9 | 5 |
| §1 | 5 | | §5 | 5 | | §10 | 5 |
| §2 | 4 | | §6 | 5 | | §11 | 3 |
| §3 | 4 | | §7 | 5 | | §12 | 5 |
| §3a | 4 | | §8 | 4 | | | |
| §4 | 5 | | | | | | |
| §4a | 5 | | | | | | |

均值 ≈ **4.56**（9×5 + 6×4 + 1×3）。

### 9.3 全局裁决

**总体：清晰且完整（高分），附小缺口清单；无影响主路径裁决的矛盾。**

- **清晰性**：两契约的可测试断言密度极高（状态机表、求值序、字面冻结、穷尽句、公式化不变量），v4 §3.3/§12/§16 为最佳实践范本；实现照做率高——本轮逐句核对未发现任何「契约冻结且实现偏离」的主路径违约（规范轨零 contract-violation 于成功路径）。
- **缺口清单**（7 条，全 P3 + 1 条 P2）：F-025（§4.1/§4.3 参数错误归宿 + code 字面量，P2——唯一非 P3 缺口）；F-152/153/154（三个无主错误码）；F-155（§3.2 措辞）；F-156（v3 §11 矩阵偏差）；§9 指标字段集未冻结（转 A13）；INV-03 无自动化锁定（流程级）。
- **矛盾清单**（轨内）：F-151（v3 §2/§3/§3a `[3]`/`[3,3]` 滞后字面 vs v4 [3,4]——v4 差异层权威覆盖，裁决无歧义，但 v3 文本单独读会误导）；F-004（operations.md「已清理」陈述 vs deploy 模板事实——工程规约层矛盾，P1）。
- **幽灵码为零、硬不变量 72 条中 71 条测试锁定**——契约的可达性与锁定纪律是本仓最强项；主要弱点集中在**错误路径边角的归宿记载**与**历史文本同步**（v3 §2/§11、CLIENT_CHANGES、无主码族），全部可经一次文档收编型修订关闭，无需实现变更（除 F-025 若选择统一 422 形状）。

---

*（完）A4 · D04 · 快照 0b836e7 · 2026-08-20*
