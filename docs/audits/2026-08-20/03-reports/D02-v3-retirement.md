# D02：A2 v3 可淘汰性分析（口径 a 迁移评估 + 口径 b 拆除成本模型）

> 产出专项：Phase 2 / A2。为 `04-final/v3-retirement-plan.md` 打底。
> 证据基线 BASELINE_HEAD=**0b836e7**（release: v4.4.0）。全部 `file:line` 为实读/rg 实取。
> **§0.3 口径约束合规声明**：本报告遵循审计方案 §0.3——协议版本封顶 4 系、wire (3,4) 永久双版本、5.0.0 已取消（v4-contract §0.3、§9.4；CHANGELOG [4.1.0]）。**§3（口径 b）是成本模型，不是政策建议**；本报告结论不包含任何「建议淘汰 v3」表述，政策输出仅限「维持现状」与「可启动的机械性迁移前置准备」（补测试/补文档/补观测类，见 §5）。

---

## 1. 现状评估

### 1.1 版本窗与消费方格局

- **版本窗**：`ACCEPTED_CLIENT_VERSIONS = (3, 4)`（`src/oc_slimapi/versioning.py:44`）；selector 支持集由其派生（`src/oc_slimapi/selector.py:135-136`）。同一 `/slimapi/**` 路径经 `?v=3`/`?v=4` 决定视图（v3-contract §0.6）。
- **消费方格局（快照时点）**：
  - **ocdroid**：锁定 wire v3（CHANGELOG [3.3.1]：「现行消费方（ocdroid/WebUI）均使用 wire v3」；[4.0.0] 消费者行动项将 v4 升级列为「可选」B5a 探测/B5b 适配，至今未执行）。
  - **oc-webui**：已于 4.2.0→4.4.0 窗口内迁至 v4（CHANGELOG [4.2.0]「v4 尚无消费方（ocdroid/WebUI 均在 ?v=3）」→ [4.4.0] 动因为 oc-webui 对 v4 providers 投影的现场反馈）。**迁移坑实证**：oc-webui 上 v4 即踩中 providers 投影丢 `limit` 字段（上下文使用百分比失去分母，[4.4.0] 修复）——证明 v3→v4 的投影字段差集类回归可达生产。
- **路由面**：54 路由中 **50 条 v3_face 可达**（01-explore/expected-keys.csv `v3_face` 行 = 50；route-census.csv 版本面列：49 dual + 4 v4-only + versions 豁免）。v4 差异面：4 条发布态差异（sessions/events/stream/versions，v4-contract §10）+ 修订面（§12-§16，4.2.0 起 10/10 feature satisfied、ready:true，v4.3.0 修订二激活 POST 等效族、v4.4.0 修订三恢复 limit）。
- **v3 面冻结状态**：v3 语义冻结不变是 (3,4) 窗口的构造性承诺（v3-contract 头部 2026-08-19 状态注记；CHANGELOG [4.0.0]/[4.2.0]/[4.3.0]/[4.4.0] 每版显式「?v=3 零改动/逐字节不变」），并由测试锁定（§4.3）。

### 1.2 v3 在当前架构中的三重角色（现状定性）

1. **生产消费面**：ocdroid 全量流量的承载视图（50 路由 v3_face）。
2. **规范锚点**：v4 契约以 v3 为继承基线（v4-contract:5「凡未提及语义逐字沿用 v3」；全文 67 行含 v3 字面、13 处显式「沿用/继承 v3」表述）；47 条路由 v4 = v3 语义原样（v4-contract §10）；expand 端点、SSE 帧形、错误族、资源上限全部以 v3 条款为权威出处。
3. **回归基线**：v4 的正确性大量以「与 v3 逐字节相同」断言（test-census §8：12 个双视图文件、v3 字节回归锚三处）。

→ v3 不是「遗留分支」而是**双稳态架构的一侧 + 另一侧的等价性基准**。

---

## 2. 口径 a：ocdroid（ocdroid@v3）迁移 v4 评估——checklist 16 项

> 全集来源：route-census v3_face 键（50 路由）+ v3-contract §3 capabilities + v4-contract §10 差异面。每项：v3 现状 → v4 等价物 → 必改点 → 风险。引用 wire 行为均为 4.4.0 现态（readiness 10/10 satisfied）。

| # | 能力（v3 现状） | v4 等价物 | ocdroid 必改点 | 风险 |
|---|---|---|---|---|
| A1 | `?v=3` selector + `capabilities["3"]` 探测 | `?v=4` + `capabilities["4"]`（静态四键 + `readiness` 十 ID + `expand` 键） | 探测逻辑按 §3.3：`optIn(F) = localFlag && (4 in available) && (F ∈ readiness.satisfied)`；需实现 discovery-contradiction 七条件分类（§3.3，含修订二蕴含⑦） | 中：readiness 规范化（去重+UTF-8 字节序排序）与十 ID 全集识别为客户端新逻辑；漏判⑦会误触 contradiction |
| A2 | sessions 列表 `?directory=`（per-workspace 列表）、`roots`、`start`（time_updated 水位）、`limit≤1000` | 参数轴整体更换：`archived`(omit/only/all)、`parent`(all/none/only/`<sid>`)、`search`、`cursor`（keyset）、`limit≤500`；`roots`→`parent=none`（v4-contract §4.1） | 移除 directory/roots/start 发送；`start` 水位语义无 v4 等价（改 cursor 翻页）；limit>500 → 422 `param_version_mismatch`（sessions.py:390-393） | **高**：见 A3 能力缺口；`start` 是时间水位非 offset（INTERFACE_MAP:25），cursor 是 (t,i) keyset——语义换轨需重写列表增量逻辑 |
| A3 | **sessions 列表 per-directory 服务端过滤**（`?directory=X` → 上游 `/session` + `X-Opencode-Directory`，sessions.py:706-717） | **无等价物**：v4 全局列表 directory 整体退役（四形态一律 400 `directory_retired_in_v4`，selector.py:668-673；v4-contract §5.2）；`SessionSkeletonV4` 恒携带 `directory` 字段（v4-contract §13.1）→ 仅客户端侧过滤；`/slimapi/directories` 发现保留（v4-contract §4.6「不升 DB 投影」） | per-workdir 会话列表改为：全局拉取 + 按 `item.directory` 客户端过滤；目录发现继续走 `/slimapi/directories` | **高（能力缺口，F-121）**：①cursor 在全局序 (time_updated DESC, id DESC) 上推进——目标 workdir 会话稀疏时需遍历多页无关行，无服务端 directory 谓词；②per-directory「complete」（该 dir 列表完备性）不可判定（`complete` 仅表全局分页窗口）；③`search` 仅 title 字面子串、无 directory 轴，且 cross-session search 为**永久 non-goal**（§17 owner q3）——缺口无服务端补齐路径；④allowlist 非空时全局列表受子树谓词影响（§4.6）。v4-contract §0.4 自认此方向为「功能降级非等价回退」 |
| A4 | sessions 列表 ETag/304（`_finalize_sessions_response`，sessions.py:92-138，wire=v3 validator） | §15 修复态（4.2.0 起）：恒 `Vary: Accept-Encoding` + ETag（identity 强/gzip 弱 `W/`）+ 304；REP_VERSION `wire=v4` 域隔离（sessions.py:598-655） | 重新初始化 ETag 缓存（跨视图 validator 构造上不互配，v4-contract §15「不可能误 304」）；304 头集合 = ETag+Vary+no-store（envelope 自含 nextCursor/complete） | 低：若按 4.0.0 原始态（无 ETag/Vary）开发会踩已知坑——**必须按 §15 修复态写客户端**（`_v4_json_response` 门控关态摘 Vary 为 4.0.0 已发布行为，sessions.py:620-624） |
| A5 | sessions 数据源 = 上游 HTTP `/session`（实时权威） | 常态 = opencode SQLite 只读投影（dbaux，mode=ro 零写入）；上游 HTTP = schema 权威 + 降级路径（v4-contract §4） | 无请求形态变化；需处理 `degraded:true`（envelope required 布尔，§13.4）与 503 `auxiliary_unavailable` + `Retry-After: 30`（§4.2 降级矩阵：Class B/allowlist 非空/cursor/通配 search 全 503） | 中：降级矩阵 72 等价类是新的错误面组合空间；503 不自动回退 v3（§0.4：显式错误，维持当前 wire 版本） |
| A6 | （无 cursor） | `cursor` keyset 翻页：base64url(JSON `{t,i,f}`)，f = 过滤上下文指纹 | 维持过滤参数组合一致性（变更 → 400 `invalid_cursor` 提示重开首屏，sessions.py:414-426）；不承诺零重复零遗漏（跨边界重见为预期） | 低-中：翻页期间用户切换过滤轴需清 cursor 重拉 |
| A7 | session 单查 = v3 skeleton 投影 | §13 canonical `SessionSkeletonV4`（与列表同源唯一 projector）：+ `project` 对象、tokens 五列平铺（`tokens_input` 等）、`partial`/`degraded` 标记；裸对象无 envelope | 消费新字段集；`project:null`+partial 与 `projectID:null`（absent）是两种形态（§13.5）；不可表示字段不可得 → 整响应 503 | 低：字段均为加性+nullable；注意 §13.2b 三态（业务 null vs 来源不可得 null+partial） |
| A8 | messages 面（列表/full/expand/envelope/nextCursor）零 v4 差异（v4-contract §10） | 逐字节同 v3（含 skeleton 投影、expandRefs、merged、指纹） | 唯一差异：expandRefs `href` 按请求视图生成（v4 响应 → `?v=4`，messages.py:58-61 `_expand_wire_view`、:854/:1050）——客户端不得缓存跨版本 href | 低 |
| A9 | expand 能力探测读 `capabilities["3"].expand` | `capabilities["4"].expand`（4.2.0 起广告，与 `messages.expand.v4 ∈ satisfied` 双向 iff，versions.py:101-135） | 探测键随视图切换；两键形状同构（12 类目 + fragmentMaxBytes） | 低 |
| A10 | providers raw 透传（全字段含 `env`/`api`/`key`/`options`/`cost` 等） | §12 安全投影：白名单 schema、嵌套递归丢弃、四限额（256/1024/64/8MiB）、`limit` 嵌套恢复（修订三，§12.1） | 字段差集核对（确定性丢弃清单 §12.1）；`limit` 从顶层改嵌套 `limit.{context,input,output}`（int-else-omit，绝无 `limit:null`/`limit:{}`）；canonical 键序（UTF-8 字节序） | **中（实证坑）**：oc-webui 已踩「投影丢 limit → 上下文百分比失分母」（[4.4.0]）；ocdroid 迁移前须全量 diff 自身消费的 provider/model 字段与 §12.1 白名单；新错误码 502 `provider_upstream_malformed` / 413 `provider_projection_limit` |
| A11 | SSE：无 `id:` 帧；任意 Last-Event-ID → 恒 `resync{reconnect_no_replay}`（events.py:207-210）；断连盲区靠双轨消费（digest 精拉 + 周期 304 对账，v3-contract §7.8） | §7 重放：`id: g:<epoch>:<seq>`（全局）/`id: t:<sid>:<epoch>:<seq>`（token）；Last-Event-ID 四类短路分类；resync reason 冻结四值 `epoch_changed`/`replay_expired`/`replay_gap`/`reconnect_no_replay`；meta 首帧 +`capabilities`/`epoch`/`seqBase` | 重连器按 (epoch,seq) 实现；epoch=随机 16hex 不比较大小；**服务端永不发 snapshot 帧——resync 后必须 HTTP 全量对齐**（§7.2）；周期对账在 v4 仍必选（§7.5：重放缩小盲区不消除） | 中：重连状态机为新实现；跨端点/跨 sid 域 ID 混用被忽略+重置（不报错）——客户端 bug 静默 |
| A12 | `/events?tokens=1` 统一 token 流 | v4 退役：400 `tokens_stream_retired_in_v4`（events.py:20-25,91-99）；token 流唯一通道 `/slimapi/sessions/{sid}/stream`（v4 起独立 id: 序列） | 移除 `tokens=1` 用法；token 订阅独立连接（oc-webui/ocdroid 本就分离两连接，S-B01① 裁决） | 低 |
| A13 | SSE 握手：`server.connected` 首帧 → token 流 tombstone 预发 → live part snapshot 预发（subscriber.py:625-705 v3 预填握手） | v4 抑制全部连接本地帧：无 `server.connected`、无 tombstone 预发（重放日志带 id: 恰一次回放）、无 snapshot 帧（hub.py:169-171 `_V4_INELIGIBLE_FRAME_PREFIX`）；v4 首帧恒 `slimapi.meta` | 冷启动状态对齐改走 HTTP（messages 重拉）；不再依赖握手快照 | 中：若客户端冷启动逻辑依赖 snapshot 预填/tombstone 预发需重写为 HTTP 对齐 |
| A14 | SSE legacy resync reason 路径（`subscriber_backpressure`/`token_memory_limit`/`session_idle`/`session_deleted`）发 resync 帧 | v4：域外 reason → **不发帧直接终结连接**（subscriber.py:444-450、hub.py:1502-1526；CHANGELOG [4.0.0] R3 裁决） | 把「连接被服务端断开」当对齐信号（断连本身可观察）；`session_deleted` 语义由全局 digest 控制面表达 | 中：静默断连 vs 显式 resync 帧的 UX 差异；客户端需区分网络断连与服务端终结 |
| A15 | 写路由 17 端点零 v4 差异（v4-contract §10；directory 消费语义原样 §5.2） | 同 + 加性三条 POST 等效动作族（`POST /session/{sid}`≡PATCH、`…/delete`≡DELETE、`…/archive` 合成，§16.2，4.3.0 已激活） | 无必改点；POST 等效族可选采用 | 低 |
| A16 | 观测/health：v3 视图（schema.version=3）；digest 帧形 v3/v4 一致（§7.5） | v4 视图 schema.version/api_version=4 + `auxiliary:{available,mode}` 瞬态字段（health.py:30-79）；digest/q/p 载荷两视图一致（§7.4/§7.5） | 兼容自检读数更新；DB 不可用经 `health.auxiliary` 观察（§9.3 运维信号） | 低 |

**其余 30+ 路由**（file/vcs/find/agent/command/todo/children/diff/questions/permissions/actions/directories/context/status 等）：v4 = v3 语义原样（v4-contract §10「零 v4 差异」注载），directory 消费继承 v3（§5.2 表行）——零必改点，仅需回归验证。

**迁移工作量定性**：核心改动集中在 A2/A3/A5/A11 四项（sessions 列表换轨 + directory 缺口应对 + 降级处理 + SSE 重连器）；A3 是唯一无服务端等价物的项。

---

## 3. 口径 b：（假设性）v3 拆除成本模型

> **本节为成本模型，非政策建议**（§0.3 口径约束：(3,4) 永久双版本为 owner 终态裁决，5.0.0 已取消——v4-contract §0.3「本契约不预设任何未来版本窗」）。以下量化仅为回答「若 someday owner 另行裁决退役，需要动什么」。

### 3.1 v3-only / v3-serving 代码路径清单（12 项，逐条 file:line）

| # | 路径 | 位置 | 拆除涉及 |
|---|---|---|---|
| B1 | **selector 双版本表与 v3 directory 消费梯子** | selector.py:100-101（SELECTOR_V3/V4）、:135-136（支持集）、:194-197+294（v4 退役 pattern 表）、:567（支持集判定）、:579（wire stash）、:636-699（`_consume_directory`：v4 fork :668-673 + v3 梯子逐字） | 1 文件；unsupported_version 支持集、v3 梯子、双 stash 全部重写 |
| B2 | **sessions.py v3 面** | `_finalize_sessions_response` :92-138、lease coalesce 面（`_sessions_via_lease`）、direct path :693-798、v3×v4 参数互斥 422 :700-705、`start`/`roots`/`limit≤1000` 参数、上游 HTTP 管线（cap-read/413/503） | 1 文件 ≈200 行；v3 数据管线（上游 HTTP + envelope）整体退役 |
| B3 | **envelope.py（v3-only 模块）** | envelope.py:1-73（`messages_envelope_bytes` :24-31、`sessions_envelope_payload` :34+；X-Next-Cursor/X-Complete 语义载体） | 1 文件 73 行全删 |
| B4 | **messages v3 envelope + ETag v3 域** | messages.py:903/:1099（`wire_view=3`）、:854/:1050（`_expand_wire_view`）、envelope 调用链 | 1 文件；v4 面行为继承 v3 条款需先字面化（见 B12） |
| B5 | **SSE v3 握手/帧路由/legacy resync** | registry.py:226（`welcome=not wire_v4`——server.connected）；tokenstream/subscriber.py:625-705（v3 预填握手 tombstone+snapshot）、:444-489（V4_RESYNC_REASONS 门控 + v3 冻结 resync+STOP 对）；tokenstream/hub.py:169-171（`_V4_INELIGIBLE_FRAME_PREFIX` 帧路由）、:1502-1526（`_fanout_resync` v3 legacy reason 帧）；global_hub.py:594（id: 仅 v4 订阅者）；events.py:207-210（v3 blanket resync） | 4 文件；「v3 逐字节不变」的帧分叉逻辑全部收编为 v4 单态 |
| B6 | **ETag wire=v3 validator 域** | etag.py:50-98（`representation_version` 的 `f"wire=v{wire_view}"` 标记 :87）；消费方 = 全部 ETag 路由 v3 面（sessions/messages/read_groups/providers v3） | 1 文件 + 全部调用点参数；validator 轮换（存量客户端 ETag 全失效重拉——破坏面） |
| B7 | **versions 双 payload** | versions.py:137-157（capabilities["3"] 形状冻结 + ["4"]）、:104-135（`_capabilities4`） | 1 文件；capabilities["3"] 形状是冻结契约（v3-contract §3） |
| B8 | **观测双维度** | access_log.py:309（wireVersion "3"）、traffic_snapshot/sse_observability（selectorResult v3 维度、sseActive 四值） | 3 文件；维度收窄属观测 breaking（快照聚合键变化） |
| B9 | **write_groups v3 404 面与 directory stash** | write_groups.py:99-103（v3-only directory stash 消费）、:281-329（POST 等效族 admission `wire_view_from_scope>=4` :304-310 + v3 `thin_route_not_found` 复现 :316-320） | 1 文件；v3 404 分支删除 |
| B10 | **health 双视图** | health.py:30（view 分叉）、:39/:132（api_version=view）、:76-79（v4 auxiliary） | 1 文件；v3 视图字段（schema.version=3）退役 |
| B11 | **测试面** | 见 §4.3 量化 | 8 文件 106 函数 + 12 双视图文件 v3 半区 + 294 处 `v=3` 字面 |
| B12 | **契约/文档继承链** | v3-contract.md 全文 288 行 25 节（`?v=3` wire 权威）；v4-contract.md:5「凡未提及语义逐字沿用 v3」+ 67 行 v3 字面 + 13 处显式沿用/继承表述；INTERFACE_MAP.md 41 处 v3 引用；CHANGELOG 版本双轨段；AGENTS.md「版本双轨」硬规则 | v4-contract 需全量自包含重写（47 条零差异路由 + expand 端点 + SSE 帧形 + 错误族 + 资源上限的权威出处全部指向 v3 条款）；INTERFACE_MAP 54 行双面注记重写 |

### 3.2 成本汇总（每路径：文件数/测试数/契约节/客户端破坏面）

| 路径组 | 涉及文件 | 直接关联测试 | 契约节修订 | 客户端破坏面 |
|---|---|---|---|---|
| B1-B4, B6, B9, B10（REST v3 面） | 8 | test_v3_envelope(11) + test_v3_etag_domain(8) + test_sessions_routes(29) + test_read_groups(59) + test_etag(38) + test_write_groups(27) 等的 v3 断言半区 | v3-contract §2/§3/§3a/§4/§4a/§4b/§6/§8/§10/§11（约 10 节）+ v4-contract §0/§2/§5.1/§10 | **ocdroid 全量 400 `unsupported_version`**（端点存在不 404）；存量 ETag 全失效 |
| B5（SSE v3 面） | 4 | test_v3_sse_meta(15) + test_sse_replay_wire(73) v3 字节锚半区 + test_events_tokens(12) + test_token_stream_route(49) 的 v3 分支 | v3-contract §7（8 款）+ v4-contract §7/§7.5 | ocdroid SSE 全断（开流前 400）；无重连 API（v3 无 Last-Event-ID 语义可降级） |
| B7/B8（发现+观测） | 4 | test_versions_route(9) + test_access_log_v3_fields(16) + test_traffic_snapshot_v3(20) + test_health_dual_view(6) | v3-contract §3/§3a/§9；v4-contract §3/§9 | 发现端点 `available` 变 `[4]`（可忽略字段但仍为变更）；快照聚合键维度收窄（运维 breaking） |
| B11/B12（测试+文档） | 51 测试文件 / 5 文档 | 见 §4.3 | v3-contract 整份 288 行；v4-contract 自包含化 | — |

**拆除顺序约束（成本结构的要害）**：B12 先行——v4 的规范基准与回归对照系（「v4 ≡ v3」断言、67 处 v3 引用）必须先机械性字面化（v4 golden/契约自包含），否则 B1-B10 任一步都会使 v4 面失去规范锚点与测试承载（§4.3）。

---

## 4. 阻塞项识别（现状下 v3 退役不可启动的三项）

1. **ocdroid 锁定 v3（消费方硬阻塞）**：ocdroid 全量流量走 `?v=3`（CHANGELOG [3.3.1]/[4.0.0] 消费者行动项；[4.2.0]「ocdroid/WebUI 均在 ?v=3」）。v4 对接的 B5a 探测/B5b 适配未启动。任何 v3 收窄对 ocdroid 即全量 400（§2 状态表：无 `v` → 400 `unsupported_version`）。
2. **(3,4) 永久双版本 owner 终态裁决（政策阻塞）**：v4-contract §0.3 + §9.4：「协议封顶 4 系、(3,4) 永久双版本窗口、原预定 major 退役发版已取消（CHANGELOG [4.1.0]）；若未来评估 v3 退役，判据为纯观测性（wireVersion 维度 v3 流量占比持续低于阈值 + SSE active 无 v3 连接）；退役形式与版本号另行 owner 裁决，本契约不预设任何未来版本窗」。→ 现状不存在已批准的退役路径；评估触发条件本身（v3 流量占比观测）也未满足（ocdroid 仍在 v3）。
3. **v3 冻结回归基线作用（质量基础设施阻塞）**——拆除后回归网变薄的量化（rg 实取，快照 0b836e7）：
   - **v3 单态测试文件 8 个 / 106 个测试函数**：test_v3_directory(26)、test_traffic_snapshot_v3(20)、test_access_log_v3_fields(16)、test_v3_sse_meta(15)、test_v3_envelope(11)、test_v3_etag_domain(8)、test_health_dual_view(6)、test_v3_rawbody_regression(4)。
   - **294 处 `v=3` selector 字面断言**（32 个测试文件含 `v=3` 字面；51 文件含 v3 相关字面（`v3`/`"3"`/wireVersion 族），合计 568 行）。
   - **12 个双 wire 视图同文件锁定**（其中 10 文件 `?v=3`+`?v=4` 字面双 selector）——这些文件的 v3 半区在拆除后失去对照对象，其「v4 ≡ v3 逐字节相同」断言模式（test_sse_replay_wire 的 v3 无 id 字节锚、test_expand_href_v4 的 v3 href 字节回归、test_v3_rawbody_regression 的 PYTHONHASHSEED 围栏字节基线）必须先改写为 v4 字面 golden，否则 47 条零差异路由 + expand 端点 + SSE 帧形的等价性验证直接失载。
   - 测试总量对照：2642 个测试函数（test-census §0）中，v3 承载/对照相关 ≈ 106 直接 + 12 文件半区 + 294 字面断言点——粗估 **>15% 的断言面以 v3 为被测对象或对照基准**。

---

## 5. 结论（固定四节）

### 5.1 现状评估

- (3,4) 永久双版本是 owner 终态裁决（v4-contract §0.3/§9.4），**现状无可启动的退役路径**；v3 是 ocdroid 的生产承载面、v4 的规范锚点与回归基准三合一。
- v4 修订面已全量就绪（4.4.0 readiness 10/10、ready:true），oc-webui 已在 v4 并反哺过一个投影字段丢失类修复（[4.4.0]）。
- **维持现状**（(3,4) 双版本继续）与所有已冻结裁决一致，无任何契约/实现漂移要求改变版本窗。

### 5.2 若（ocdroid）迁移 v4 需要什么

- 16 项 checklist（§2）：核心工作量 = sessions 列表换轨（A2）+ **directory 退役能力缺口的客户端补偿（A3，唯一无服务端等价物项，F-121）** + 降级/503 处理（A5/A6）+ SSE 重连器重写（A11-A14）。
- 迁移风险实证可引用：oc-webui 的 providers limit 丢失坑（[4.4.0]）——教训 = 迁移前必须做**消费字段差集全量 diff**（providers §12.1 丢弃清单、sessions §13.1 形状）。
- 回退语义已冻结：503 不自动回退；目录级浏览仅经用户显式触发整体版本重协商且是功能降级（v4-contract §0.4）。

### 5.3 若（假设性）退役 v3 需要什么（成本模型，非建议）

- 前置（按依赖序）：① owner 另行裁决（推翻 §0.3 现裁决 + 确定退役形式/版本号——契约明文「不预设」）；② 观测判据满足（wireVersion v3 占比 + SSE active 双指标，现有 access log 维度已具备，无需新建观测）；③ B12 契约/测试字面化（v4-contract 自包含重写 67 处 v3 引用 + 12 双视图文件 v3 半区 golden 化）；④ B1-B10 代码拆除（12 路径、≈10 文件、伴随 v3-contract 整份 288 行废止与 INTERFACE_MAP 重写）；⑤ ocdroid 先完成 §2 迁移并稳定运行（消除阻塞项 1）。
- 量级参考：代码拆除直接触碰 ≈10 个 src 文件（v3/v4 分叉服务代码粗估 400-600 行，占 src 26,452 行 ≈2%）；测试面触碰 51 文件（106 函数删除 + 12 文件半区改写 + 294 字面清理）；契约触碰 v3-contract 全文 + v4-contract §0/§2/§3/§5.1/§7.5/§9/§10 + INTERFACE_MAP 54 行。
- **政策结论（§0.3 合规）：不建议、不启动**；上列仅回答「需要什么」。

### 5.4 永久双版本维持成本量化（现状接受的成本，rg 实取）

| 维度 | 数字 | 取证 |
|---|---|---|
| 双语义分支代码（含注释/docstring 提及 wire_v4/wire_view/wire_version） | **122 行 / 22 文件** | `rg -c "wire_v4\|wire_view\|wire_version" src/`；密度最高：skeleton.py(22)、routes/messages.py(18)、tokenstream/hub.py(11)、subscriber.py(10)、selector.py(8) |
| 其中可执行分支行（if/return/调用 keyed on wire） | **33 行 / 14 文件** | `rg "if .*(wire_view\|wire_v4\|wire_version)\|wire_view_from_scope\("`（剔除定义行） |
| v3-only 模块 | envelope.py **73 行**（整模块） | `wc -l` |
| selector 双版本 dispatch + v3 directory 梯子 | selector.py `_consume_directory` :636-699 + dispatch :561-620（≈100 可执行行） | 实读 |
| 双面测试 | **12 文件双 wire 视图**（10 文件字面双 selector）+ **8 文件 v3 单态 106 函数** + **294 处 `v=3` 字面**（32 文件；51 文件含 v3 相关字面）+ readiness 门控双测 10 文件 | test-census §0/§8 + rg 实取；总测试基数 2642 函数 |
| 双 payload/双视图 | versions capabilities["3"]+["4"]（versions.py:137-157）、health 双视图（health.py:30-79）、观测 wireVersion 三值 + selectorResult v3/v4（access_log.py:309） | 实读 |
| 契约/文档双轨 | v3-contract 288 行（25 节）+ v4-contract 713 行（其中 67 行含 v3 字面、13 处显式沿用/继承）+ INTERFACE_MAP 41 处 v3 引用 + CHANGELOG 双轨说明段 | rg 实取 |

**定性**：维持成本主要是**测试双态（~15% 断言面）与文档双轨**，代码分叉本体很小（≈2% src），且分叉模式高度统一（单点 selector stash + `wire_view_from_scope` 读取），无散落 `if version==3` 式脏分叉。该成本结构与「v3 = v4 回归基准」的收益（47 路由等价性免单测化）相称。

### 5.5 可启动的机械性迁移前置准备（政策允许输出）

> 以下均为补测试/补文档/补观测类，不动 wire 行为，不预设退役决策：

1. **CLIENT_CHANGES.md 增补 v4 迁移章节**（F-124）：现文档止于 3.x；把 CHANGELOG [4.0.0]-[4.4.0] 消费者行动项 + §2 checklist 16 项整理为 ocdroid 开发者单一入口。
2. **消费字段差集对照表**（防 oc-webui limit 类坑）：providers §12.1 丢弃清单 / sessions §13.1 形状差异的「v3 字段 → v4 去向」逐字段表（文档类）。
3. **文档漂移修正**（F-123/F-125）：INTERFACE_MAP 全局头「v3-only 终态/supported:[3]/v=4 不支持」、v3-contract §2/§3 的 `available:[3]` 行——均与 (3,4) 现状矛盾。
4. **观测增强（评估判据的数据面）**：`/slimapi/metrics.traffic` 或 snapshot 增加 `wireVersion` 占比视图的文档化查询样例（维度已在 access log，仅缺手册样例——docs/manual/traffic-accounting.md 补一节）。
5. **resync reason 值域运行时防线**（F-122）：log 层/replay 层 reason 常量集断言（测试类）——v3/v4 双语义维持的结构性保险。

---

## 6. 新发现清单（本次产出，详见 02-findings/）

| 编号 | 严重度 | 类别 | 标题 |
|---|---|---|---|
| F-121 | P2 | gap | v4 sessions 全局面对多工作目录客户端的能力缺口（per-directory 服务端过滤无等价、cursor 全局序、per-dir complete 不可判定、search 无 directory 轴且为永久 non-goal） |
| F-122 | P3 | risk | SSE resync reason 值域封闭性无运行时强制（route 层直发不经 V4_RESYNC_REASONS 门控） |
| F-123 | P2 | docs | INTERFACE_MAP.md 全局头仍声明「v3-only 终态」「v=4 不支持 supported:[3]」——与 4.0.0 起 (3,4) 双版本矛盾 |
| F-124 | P3 | docs | CLIENT_CHANGES.md 无 v4 迁移章节（ocdroid 对接权威清单滞后于 v4 发布面 4 个版本） |
| F-125 | P3 | docs | v3-contract §2 表/§3「3.0.0 起 available:[3]」行未随 (3,4) 窗口更新（头部注记与正文不一致） |
| F-126 | P3 | risk | v3→v4 规范耦合量化：v4-contract 67 行 v3 引用/13 处继承表述 + 12 双视图测试文件的等价性对照依赖——任何未来收窄的前置字面化工作量锚点 |

（INDEX.md 追加由主审计协调，本专项写域仅限 F-121..F-135 新建。）

---

*D02 完。审计快照 0b836e7；rg/实读取证；未运行 pytest/pip/git 写操作（仓库只读合规）。*
