# v3 退休专项：双口径终版交付（04-final/v3-retirement-plan.md）

> 独立交付物，由 03-reports/D02-v3-retirement.md 蒸馏；证据基线 BASELINE_HEAD=**0b836e7**（release v4.4.0，wire (3,4)，readiness 10/10 satisfied / ready:true）。全部 `file:line` 引用为审计快照实读，src 侧路径省略 `src/oc_slimapi/` 前缀。
>
> **§0.3 口径约束合规声明（最高优先级）**：wire **(3,4) 永久双版本**为 owner 已冻结终态裁决（2026-08-18，CHANGELOG [4.1.0]：「协议封顶 4 系，(3,4) 永久双版本，5.0.0 与 B6-2 取消」；v4-contract §0.3/§9.4「本契约不预设任何未来版本窗」）。本文全文不包含「建议淘汰 v3 / 建议重启裁决」类表述；**§3（口径 b）是成本模型，不是政策建议**；唯一政策输出 = **维持现状（(3,4) 双版本继续）+ §5 机械性迁移前置准备清单**。

---

## 1. 执行摘要

### 1.1 现状基线（一段）

`ACCEPTED_CLIENT_VERSIONS = (3, 4)`（versioning.py:44），54 条路由中 50 条 v3_face 可达（route-census：49 dual + 4 v4-only POST 等效族 + versions 豁免）。v3 在当前架构中是三合一角色：**ocdroid 全量生产流量的承载视图 + v4 契约的规范锚点**（v4-contract:5「凡未提及语义逐字沿用 v3」；67 行含 v3 字面、13 处显式沿用/继承表述；47 条路由 v4 = v3 语义原样）**+ v4 回归基线**（12 个双视图测试文件的「v4 ≡ v3 逐字节相同」断言）。

### 1.2 口径 a：ocdroid 迁移 v4 需要什么（评估结论）

- v4 修订面已全量就绪（readiness 10/10），迁移技术上可行；核心工作量集中四项：**sessions 列表参数轴换轨（A2）、directory 能力缺口的客户端补偿（A3，唯一无服务端等价物项，F-121）、降级/503 处理（A5/A6）、SSE 重连器重写（A11-A14）**。
- 16 项 checklist 见 §2；其余 30+ 路由 v4 = v3 语义原样，零必改点。
- 迁移风险有生产实证：oc-webui 迁 v4 即踩 providers 投影丢 `limit` 字段（上下文使用百分比失去分母，CHANGELOG [4.4.0] 修复）——教训：迁移前必须做消费字段差集全量 diff。

### 1.3 口径 b：假设性 v3 拆除需要动什么（**成本模型，非政策建议**）

- 若 someday owner 另行裁决退役，触碰面 = **12 条 v3-only 代码路径（约 10 个 src 文件、400-600 行 ≈ src 26,452 行的 2%）+ 51 个测试文件（106 个 v3 单态函数删除 + 12 双视图文件半区改写 + 294 处 `v=3` 字面清理）+ v3-contract 整份 288 行废止与 v4-contract 自包含化（67 处 v3 引用字面化）**。
- 拆除顺序要害：契约/测试字面化（B12）必须先行，否则 v4 面失去规范锚点与测试承载。
- 详见 §3。**本口径不构成任何行动建议；现状不存在已批准的退役路径。**

### 1.4 政策合规模块（唯一政策输出）

- **维持现状：(3,4) 永久双版本继续。** 该状态与全部已冻结裁决一致（v4-contract §0.3/§9.4；CHANGELOG [4.1.0]），无任何契约/实现漂移要求改变版本窗。
- 政策允许的可启动动作仅限 §5 的 5 项机械性前置准备（补测试/补文档/补观测类，不动 wire 行为、不预设退役决策）。
- v4-contract §0.3 已明文：未来若评估 v3 退役，判据为**纯观测性**（wireVersion 维度 v3 流量占比持续低于阈值 + SSE active 无 v3 连接），且「退役形式与版本号另行 owner 裁决，本契约不预设任何未来版本窗」——该判据当前未满足（ocdroid 仍在 v3）。

---

## 2. 口径 a：ocdroid（现锁 v3）迁移 v4 checklist（16 项）

> 全集来源：route-census v3_face 键（50 路由）+ v3-contract §3 capabilities + v4-contract §10 差异面（含 D01 §7.2 G1-G5 差距清单）。wire 行为均按 4.4.0 现态（readiness 10/10 satisfied）。每项四元组：v3 现状 → v4 等价物 → 必改点 → 风险。

### 2.1 十六项主表

| # | v3 能力（现状） | v4 等价物 | ocdroid 必改点 | 风险 |
|---|---|---|---|---|
| A1 | `?v=3` selector + `capabilities["3"]` 探测 | `?v=4` + `capabilities["4"]`（静态四键 + `readiness` 十 ID + `expand` 扩键） | 探测按 §3.3：`optIn(F) = localFlag && (4 in available) && (F ∈ readiness.satisfied)`；实现 discovery-contradiction 七条件分类（含修订二蕴含⑦） | **中**：readiness 规范化（去重+UTF-8 字节序排序）与十 ID 全集识别是新逻辑；漏判⑦会误触 contradiction |
| A2 | sessions 列表 `?directory=`（per-workspace）、`roots`、`start`（time_updated 水位）、`limit≤1000` | 参数轴整体更换：`archived`(omit/only/all)、`parent`(all/none/only/`<sid>`)、`search`、`cursor`（keyset）、`limit≤500`；`roots`→`parent=none`（§4.1） | 移除 directory/roots/start 发送；`start` 时间水位无 v4 等价（改 cursor 翻页）；limit>500 → 422 `param_version_mismatch`（sessions.py:390-393） | **高**：见 A3；`start` 是时间水位非 offset，cursor 是 (t,i) keyset——增量列表逻辑需重写 |
| A3 | **sessions 列表 per-directory 服务端过滤**（`?directory=X` → 上游 `/session` + `X-Opencode-Directory`，sessions.py:706-717） | **无等价物**（详见 §2.2 专节） | 全局拉取 + 按 `item.directory` 客户端过滤；目录发现继续走 `/slimapi/directories` | **高（能力缺口，F-121）** |
| A4 | sessions 列表 ETag/304（`_finalize_sessions_response`，sessions.py:92-138，wire=v3 validator 域） | §15 修复态（4.2.0 起）：恒 `Vary: Accept-Encoding` + ETag（identity 强/gzip 弱）+ 304；REP `wire=v4` 域隔离（sessions.py:598-655） | 重新初始化 ETag 缓存（跨视图 validator 不互配，「不可能误 304」）；304 头集合 = ETag+Vary+no-store | **低**：必须按 §15 修复态写客户端（勿按 4.0.0 原始态开发——门控关态摘 Vary 为已发布行为，sessions.py:620-624） |
| A5 | sessions 数据源 = 上游 HTTP `/session`（实时权威） | 常态 = opencode SQLite 只读投影（dbaux，mode=ro 零写入）；上游 HTTP = schema 权威 + 降级路径（§4） | 无请求形态变化；处理 `degraded:true`（envelope required 布尔）与 503 `auxiliary_unavailable` + `Retry-After: 30`（降级矩阵：Class B/allowlist 非空/cursor/通配 search 全 503） | **中**：72 等价类降级矩阵是新错误面组合空间；503 不自动回退 v3（§0.4 显式错误裁决） |
| A6 | （无 cursor） | `cursor` keyset 翻页：base64url(JSON `{t,i,f}`)，f = 过滤上下文指纹 | 维持过滤参数组合一致性（变更 → 400 `invalid_cursor`，sessions.py:414-426）；不承诺零重复零遗漏 | **低-中**：翻页期间切换过滤轴需清 cursor 重拉 |
| A7 | session 单查 = v3 skeleton 投影 | §13 canonical `SessionSkeletonV4`（与列表同源唯一 projector）：+ `project` 对象、tokens 五列平铺、`partial`/`degraded` 标记；裸对象无 envelope | 消费新字段集；`project:null`+partial 与 `projectID:null`（absent）是两种形态（§13.5）；不可表示字段不可得 → 整响应 503 | **低**：字段均加性+nullable；注意 §13.2b 三态（业务 null vs 来源不可得 null+partial） |
| A8 | messages 面（列表/full/expand/envelope/nextCursor）零 v4 差异（§10） | 逐字节同 v3（含 skeleton 投影、expandRefs、merged、指纹） | 唯一差异：expandRefs `href` 按请求视图生成（v4 响应 → `?v=4`，messages.py:58-66 `_expand_wire_view`）——**不得缓存跨版本 href** | **低** |
| A9 | expand 能力探测读 `capabilities["3"].expand` | `capabilities["4"].expand`（4.2.0 起广告，与 `messages.expand.v4 ∈ satisfied` 双向 iff，versions.py:101-135） | 探测键随视图切换；两键形状同构（12 类目 + fragmentMaxBytes） | **低** |
| A10 | providers raw 透传（全字段含 `env`/`api`/`key`/`options`/`cost` 等） | §12 安全投影：白名单 schema、嵌套递归丢弃、四限额（256/1024/64/8MiB）、`limit` 嵌套恢复（修订三 §12.1） | 字段差集核对（§12.1 确定性丢弃清单）；`limit` 顶层 → 嵌套 `limit.{context,input,output}`（int-else-omit，绝无 `limit:null`/`limit:{}`）；canonical 键序（UTF-8 字节序）；新错误码 502 `provider_upstream_malformed` / 413 `provider_projection_limit` | **中（实证坑）**：oc-webui 已踩「投影丢 limit → 上下文百分比失分母」（[4.4.0]）；迁移前须全量 diff 自身消费的 provider/model 字段与 §12.1 白名单（见 §2.3） |
| A11 | SSE：无 `id:` 帧；任意 Last-Event-ID → 恒 `resync{reconnect_no_replay}`（events.py:207-210）；断连盲区靠双轨消费（digest 精拉 + 周期 304 对账） | §7 重放：`id: g:<epoch>:<seq>`（全局）/`id: t:<sid>:<epoch>:<seq>`（token）；Last-Event-ID 四类短路分类；resync reason 冻结四值；meta 首帧 +`capabilities`/`epoch`/`seqBase` | 重连器按 (epoch,seq) 实现；epoch=随机 16hex 不比较大小；**服务端永不发 snapshot 帧——resync 后必须 HTTP 全量对齐**（§7.2）；周期对账仍必选（§7.5：重放缩小盲区不消除） | **中**：重连状态机为新实现；跨端点/跨 sid 域 ID 混用被忽略+重置（不报错）——客户端 bug 静默 |
| A12 | `/events?tokens=1` 统一 token 流 | v4 退役：400 `tokens_stream_retired_in_v4`（events.py:20-25,91-99）；唯一通道 `/slimapi/sessions/{sid}/stream`（独立 id: 序列） | 移除 `tokens=1` 用法；token 订阅独立连接（ocdroid 本就分离两连接，S-B01① 裁决） | **低** |
| A13 | SSE 握手：`server.connected` 首帧 → tombstone 预发 → live part snapshot 预发（subscriber.py:625-705 v3 预填握手） | v4 抑制全部连接本地帧：无 `server.connected`、无 tombstone 预发、无 snapshot 帧（hub.py:169-171 `_V4_INELIGIBLE_FRAME_PREFIX`）；v4 首帧恒 `slimapi.meta` | 冷启动状态对齐改走 HTTP（messages 重拉）；不再依赖握手快照 | **中**：冷启动逻辑若依赖 snapshot/tombstone 预填需重写为 HTTP 对齐 |
| A14 | SSE legacy resync reason 路径（`subscriber_backpressure`/`token_memory_limit`/`session_idle`/`session_deleted`）发 resync 帧 | v4：域外 reason → **不发帧直接终结连接**（subscriber.py:444-450、hub.py:1502-1526；CHANGELOG [4.0.0] R3 裁决） | 把「连接被服务端断开」当对齐信号；`session_deleted` 语义由全局 digest 控制面表达 | **中**：静默断连 vs 显式 resync 帧的 UX 差异；需区分网络断连与服务端终结 |
| A15 | 写路由 17 端点零 v4 差异（§10；directory 消费语义原样 §5.2） | 同 + 加性三条 POST 等效动作族（`POST /session/{sid}`≡PATCH、`…/delete`≡DELETE、`…/archive` 合成，§16.2，4.3.0 已激活） | 无必改点；POST 等效族可选采用 | **低** |
| A16 | 观测/health：v3 视图（schema.version=3）；digest 帧形两视图一致（§7.5） | v4 视图 schema.version/api_version=4 + `auxiliary:{available,mode}` 瞬态字段（health.py:30-79） | 兼容自检读数更新；DB 不可用经 `health.auxiliary` 观察（§9.3 运维信号） | **低** |

**其余 30+ 路由**（file/vcs/find/agent/command/todo/children/diff/questions/permissions/actions/directories/context/status 等）：v4 = v3 语义原样（v4-contract §10「零 v4 差异」注载；directory 消费继承 v3 §5.2）——零必改点，仅需回归验证。

**工作量定性**：核心改动 = A2/A3/A5/A11 四项（sessions 换轨 + directory 缺口补偿 + 降级处理 + SSE 重连器）；A3 是唯一无服务端等价物的项。

### 2.2 专节：directory 能力缺口（A3 展开，F-121，P2 gap）

**机制**：v4 将 sessions 列表重构为全局 DB 投影面（dbaux 单 SQL 组装），directory 作为过滤维度被整体移出参数域——selector 层前置拦截（selector.py:668-673：`wire_version >= 4` 且命中 v4 退役 pattern → 四形态（query 单值/多值、header、混合）一律 400 `directory_retired_in_v4`，先于路由）。`SessionSkeletonV4` 恒携带 `directory` 字段（§13.1）→ 客户端过滤唯一数据来源；`/slimapi/directories` 发现保留（§4.6「不升 DB 投影、范围冻结」）。

**对多工作目录客户端（ocdroid）的四点影响**（F-121 三方一致取证：契约条款 + 实现实读 + 消费方形态）：

1. **per-workdir 会话列表退化为客户端过滤**：全局拉取（或按 cursor 翻页）后按 `item.directory` 过滤；目标 workdir 会话稀疏时，cursor 在全局序 `(time_updated DESC, id DESC)` 上推进需遍历多页无关行——无服务端效率等价物（cursor 指纹 `f` 仅含 archived/parent/search-hash/allowlist-rev，无 directory 谓词，§4.5）。
2. **per-directory `complete` 不可判定**：v4 `complete` 仅表全局分页窗口完备（LIMIT+1 判定）；「该目录列表已完备」的 v3 语义无对应。
3. **无 directory 检索轴且无补齐路径**：`search` 仅 title 字面子串、无 directory 轴；cross-session search 为**永久 non-goal**（§17 owner 裁决 q3，再启用须推翻正式修订）。
4. **缓解面**：`/slimapi/directories` 发现保留；allowlist 非空时全局列表受子树谓词影响（仅收紧不扩展）。

v4-contract §0.4 自认此方向为「功能降级非等价回退」。**注意**：F-121 是迁移评估期发现，非当前缺陷（ocdroid 锁 v3、v3 面不受影响）；本缺口的存在不改变 §1.4 政策输出。

### 2.3 专节：oc-webui 实证坑（A10 展开，迁移风险的生产证据）

- **事件**：oc-webui 于 4.2.0→4.4.0 窗口迁至 v4 后，即踩中 §12 providers 投影剥掉 `limit` 字段——上下文使用百分比失去分母（CHANGELOG [4.4.0] 动因原文：「oc-webui 反馈 v4 投影剥掉 limit……上下文使用百分比失去分母」；修订三恢复为嵌套 `limit.{context,input,output}`）。
- **证明的命题**：v3→v4 的投影字段差集类回归**可达生产**——即使消费方已确认嵌套形状，顶层字段级的差集仍需逐字段核对。
- **对 ocdroid 的直接教训**：迁移前必须做**消费字段差集全量 diff**——providers 对照 §12.1 确定性丢弃清单（v3 透传的 `env`/`key`/`options`/`api`/`cost`/`capabilities`/`headers`/`release_date` 在 v4 均无等价物，D01 G5）、sessions 对照 §13.1 形状差异。对应 §5-2 前置准备项（差集对照表）。

---

## 3. 口径 b：（假设性）v3 拆除成本模型

> ### ⚠️ 本节为成本模型，非政策建议 ⚠️
> (3,4) 永久双版本为 owner 终态裁决（v4-contract §0.3/§9.4；CHANGELOG [4.1.0]——5.0.0 已取消）。以下量化**仅为回答「若 someday owner 另行裁决退役，需要动什么」**，不构成任何行动建议；本文不建议、不启动任何退役动作。

### 3.1 v3-only / v3-serving 代码路径清单（12 项，逐条 file:line）

| # | 路径 | 位置（file:line） | 拆除涉及 |
|---|---|---|---|
| B1 | selector 双版本表与 v3 directory 消费梯子 | selector.py:100-101（SELECTOR_V3/V4）、:135-136（支持集）、:194-197+294（v4 退役 pattern 表）、:567（支持集判定）、:579（wire stash）、:636-699（`_consume_directory`：v4 fork :668-673 + v3 梯子逐字） | 1 文件；unsupported_version 支持集、v3 梯子、双 stash 全部重写 |
| B2 | sessions.py v3 面 | `_finalize_sessions_response` :92-138、lease coalesce 面（`_sessions_via_lease`）、direct path :693-798、v3×v4 参数互斥 422 :700-705、`start`/`roots`/`limit≤1000` 参数、上游 HTTP 管线（cap-read/413/503） | 1 文件 ≈200 行；v3 数据管线（上游 HTTP + envelope）整体退役 |
| B3 | envelope.py（v3-only 模块） | envelope.py:1-73（`messages_envelope_bytes` :24-31、`sessions_envelope_payload` :34+；X-Next-Cursor/X-Complete 语义载体） | 1 文件 73 行全删 |
| B4 | messages v3 envelope + ETag v3 域 | messages.py:903/:1099（`wire_view=3`）、:854/:1050（`_expand_wire_view`）、envelope 调用链 | 1 文件；v4 面行为继承 v3 条款需先字面化（见 B12） |
| B5 | SSE v3 握手/帧路由/legacy resync | registry.py:226（`welcome=not wire_v4`）；tokenstream/subscriber.py:625-705（v3 预填握手 tombstone+snapshot）、:444-489（V4_RESYNC_REASONS 门控 + v3 冻结 resync+STOP 对）；tokenstream/hub.py:169-171（`_V4_INELIGIBLE_FRAME_PREFIX` 帧路由）、:1502-1526（`_fanout_resync` v3 legacy reason 帧）；global_hub.py:594（id: 仅 v4 订阅者）；events.py:207-210（v3 blanket resync） | 4 文件；「v3 逐字节不变」的帧分叉逻辑全部收编为 v4 单态 |
| B6 | ETag wire=v3 validator 域 | etag.py:50-98（`representation_version` 的 `f"wire=v{wire_view}"` 标记 :87）；消费方 = 全部 ETag 路由 v3 面（sessions/messages/read_groups/providers v3） | 1 文件 + 全部调用点参数；validator 轮换（存量客户端 ETag 全失效重拉——破坏面） |
| B7 | versions 双 payload | versions.py:137-157（capabilities["3"] 形状冻结 + ["4"]）、:104-135（`_capabilities4`） | 1 文件；capabilities["3"] 形状是冻结契约（v3-contract §3） |
| B8 | 观测双维度 | access_log.py:309（wireVersion "3"）、traffic_snapshot/sse_observability（selectorResult v3 维度、sseActive 四值） | 3 文件；维度收窄属观测 breaking（快照聚合键变化） |
| B9 | write_groups v3 404 面与 directory stash | write_groups.py:99-103（v3-only directory stash 消费）、:281-329（POST 等效族 admission `wire_view_from_scope>=4` :304-310 + v3 `thin_route_not_found` 复现 :316-320） | 1 文件；v3 404 分支删除 |
| B10 | health 双视图 | health.py:30（view 分叉）、:39/:132（api_version=view）、:76-79（v4 auxiliary） | 1 文件；v3 视图字段（schema.version=3）退役 |
| B11 | 测试面 | 见 §4 量化 | 8 文件 106 函数 + 12 双视图文件 v3 半区 + 294 处 `v=3` 字面 |
| B12 | 契约/文档继承链 | v3-contract.md 全文 288 行 25 节（`?v=3` wire 权威）；v4-contract.md:5「凡未提及语义逐字沿用 v3」+ 67 行 v3 字面 + 13 处显式沿用/继承表述；INTERFACE_MAP.md 41 处 v3 引用；CHANGELOG 版本双轨段；AGENTS.md「版本双轨」硬规则 | v4-contract 需全量自包含重写（47 条零差异路由 + expand 端点 + SSE 帧形 + 错误族 + 资源上限的权威出处全部指向 v3 条款）；INTERFACE_MAP 54 行双面注记重写 |

### 3.2 成本汇总（每路径组：文件数 / 测试数 / 契约节修订 / 客户端破坏面）

| 路径组 | 涉及文件 | 直接关联测试 | 契约节修订 | 客户端破坏面 |
|---|---|---|---|---|
| B1-B4, B6, B9, B10（REST v3 面） | 8 | test_v3_envelope(11) + test_v3_etag_domain(8) + test_sessions_routes(29) + test_read_groups(59) + test_etag(38) + test_write_groups(27) 等的 v3 断言半区 | v3-contract §2/§3/§3a/§4/§4a/§4b/§6/§8/§10/§11（约 10 节）+ v4-contract §0/§2/§5.1/§10 | ocdroid 全量 400 `unsupported_version`（端点存在不 404）；存量 ETag 全失效 |
| B5（SSE v3 面） | 4 | test_v3_sse_meta(15) + test_sse_replay_wire(73) v3 字节锚半区 + test_events_tokens(12) + test_token_stream_route(49) 的 v3 分支 | v3-contract §7（8 款）+ v4-contract §7/§7.5 | ocdroid SSE 全断（开流前 400）；无重连 API（v3 无 Last-Event-ID 语义可降级） |
| B7/B8（发现+观测） | 4 | test_versions_route(9) + test_access_log_v3_fields(16) + test_traffic_snapshot_v3(20) + test_health_dual_view(6) | v3-contract §3/§3a/§9；v4-contract §3/§9 | 发现端点 `available` 变 `[4]`（可忽略字段但仍为变更）；快照聚合键维度收窄（运维 breaking） |
| B11/B12（测试+文档） | 51 测试文件 / 5 文档 | 见 §4 | v3-contract 整份 288 行；v4-contract 自包含化 | — |

**拆除顺序约束（成本结构的要害）**：B12 必须先行——v4 的规范基准与回归对照系（「v4 ≡ v3」断言、67 处 v3 引用）须先机械性字面化（v4 golden / 契约自包含），否则 B1-B10 任一步都会使 v4 面失去规范锚点与测试承载。

**再次声明：以上为成本模型刻画，非政策建议；本报告不建议、不启动任何退役动作。**

---

## 4. 永久双版本维持成本量化（现状接受的成本）

> (3,4) 双版本继续运行所承担的结构性成本（rg 实取，快照 0b836e7）。此为维持现状的事实刻画，与 §1.4 政策输出一致。

| 维度 | 数字 | 取证 |
|---|---|---|
| **代码分叉** | 双语义分支代码 122 行 / 22 文件（含注释/docstring 提及 wire_v4/wire_view/wire_version）；其中可执行分支行（if/return/调用 keyed on wire）**33 行 / 14 文件**；v3 分叉服务代码粗估 400-600 行，占 src 26,452 行 **≈2%** | rg 实取；密度最高：skeleton.py(22)、routes/messages.py(18)、tokenstream/hub.py(11)、subscriber.py(10)、selector.py(8)；v3-only 模块 envelope.py 73 行（整模块）；selector `_consume_directory` :636-699 + dispatch :561-620（≈100 可执行行） |
| **测试双态** | **>15% 断言面以 v3 为被测对象或对照基准**：8 个 v3 单态测试文件 / 106 测试函数（test_v3_directory 26、test_traffic_snapshot_v3 20、test_access_log_v3_fields 16、test_v3_sse_meta 15、test_v3_envelope 11、test_v3_etag_domain 8、test_health_dual_view 6、test_v3_rawbody_regression 4）+ 12 个双 wire 视图同文件锁定（10 文件 `?v=3`+`?v=4` 字面双 selector）+ **294 处 `v=3` 字面断言**（32 文件；51 文件含 v3 相关字面，合计 568 行）+ readiness 门控双测 10 文件 | test-census §0/§8 + rg 实取；总测试基数 2642 函数 |
| **契约双轨** | v3-contract **288 行 / 25 节** + v4-contract **713 行**（其中 67 行含 v3 字面、13 处显式沿用/继承表述）+ INTERFACE_MAP **41 处 v3 引用** + CHANGELOG 双轨说明段 + AGENTS.md「版本双轨」硬规则 | wc -l + rg 实取（本次复核：288/713 行数与 D02 一致） |
| **观测双维度** | access log `wireVersion` 三值（"3"/"4"/豁免）、`selectorResult` v3/v4 维度、sseActive 四值、health 双视图（schema.version=3/4 + api_version） | access_log.py:309、health.py:30-79、versions.py:137-157（capabilities 双 payload） |

**定性**：维持成本主要是**测试双态（~15% 断言面）与文档双轨**，代码分叉本体很小（≈2% src），且分叉模式高度统一（单点 selector stash + `wire_view_from_scope` 读取），无散落 `if version==3` 式脏分叉。该成本结构与「v3 = v4 回归基准」的收益（47 路由等价性免单测化）相称。

---

## 5. 可启动的机械性迁移前置准备清单（政策允许输出，5 项）

> 以下均为**补测试 / 补文档 / 补观测**类：不动 wire 行为、不改版本窗、不预设退役决策。这是本文除「维持现状」外唯一可执行输出。

| # | 事项 | 类型 | 内容 | 关联 |
|---|---|---|---|---|
| P1 | **CLIENT_CHANGES.md 增补 v4 迁移章节** | 文档 | 现文档止于 3.x；把 CHANGELOG [4.0.0]-[4.4.0] 消费者行动项 + 本文 §2 checklist 16 项整理为 ocdroid 开发者单一入口 | F-124（P3 docs：对接权威清单滞后 v4 发布面 4 个版本） |
| P2 | **消费字段差集对照表** | 文档 | providers §12.1 丢弃清单 / sessions §13.1 形状差异的「v3 字段 → v4 去向」逐字段表（防 oc-webui limit 类坑，§2.3）；含 per-directory 列表的客户端补偿模式说明（全局拉取+过滤的翻页预算/终止条件建议，F-121 建议方向），并入 P1 章节 | §2.3 / F-121 |
| P3 | **文档漂移修正** | 文档 | INTERFACE_MAP 全局头「v3-only 终态/supported:[3]/v=4 不支持」、v3-contract §2/§3 的 `available:[3]` 行——均与 (3,4) 现状矛盾，修正为双版本表述 | F-123（P2 docs）/ F-125（P3 docs） |
| P4 | **观测增强（评估判据的数据面文档化）** | 文档/观测 | `wireVersion` 占比视图的查询样例补入 docs/manual/traffic-accounting.md（维度已在 access log，仅缺手册样例）——为 §1.4 所引 v4-contract §0.3 纯观测性判据提供手册化数据面；**仅文档化，不新建观测维度、不设定阈值** | v4-contract §0.3/§9.4 |
| P5 | **resync reason 值域运行时防线** | 测试 | log 层/replay 层 reason 常量集断言（route 层直发不经 V4_RESYNC_REASONS 门控的封闭性缺口）——v3/v4 双语义维持的结构性保险 | F-122（P3 risk） |

以上 5 项与 refactor-backlog.md 排名 10（F-123）、19（F-121 指引成文，依赖 F-124 先行）等条目兼容，可按 backlog 依赖序执行。

---

## 6. 阻塞项与边界

### 6.1 现状下 v3 退役不可启动的阻塞项（三项）

1. **ocdroid 锁定 v3（消费方硬阻塞）**：ocdroid 全量流量走 `?v=3`（CHANGELOG [3.3.1]「现行消费方（ocdroid/WebUI）均使用 wire v3」；[4.0.0] 将 v4 升级列为可选 B5a 探测/B5b 适配，至今未启动；[4.2.0]「ocdroid/WebUI 均在 ?v=3」——同窗口 oc-webui 已迁 v4，ocdroid 未迁）。任何 v3 收窄对 ocdroid 即全量 400 `unsupported_version`（无 `v` 或 v∉{3,4} → 400，端点存在不 404）。
2. **(3,4) 永久双版本 owner 终态裁决（政策阻塞）**：v4-contract §0.3 + §9.4 明文冻结（CHANGELOG [4.1.0]：5.0.0 与 B6-2 取消；「退役形式与版本号另行 owner 裁决，本契约不预设任何未来版本窗」）。现状不存在已批准的退役路径；评估触发条件本身（wireVersion v3 流量占比 + SSE active 无 v3 连接双观测指标）也未满足（ocdroid 仍在 v3）。**本文尊重并遵循该裁决，不寻求改变。**
3. **v3 冻结回归基线作用（质量基础设施阻塞）**：v4 的正确性大量以「与 v3 逐字节相同」断言（§4 量化：106 个 v3 单态函数 + 12 双视图文件 + 294 处 `v=3` 字面 ≈ >15% 断言面）。拆除 v3 将使 47 条零差异路由 + expand 端点 + SSE 帧形的等价性验证失去对照系——任何拆除动作前必须先完成 B12 字面化，而该前置本身即大型机械工程（§3.2）。

### 6.2 §17 non-goal 对 directory 服务端补齐的封堵（边界声明）

- **cross-session search（owner 裁决 q3，永久 non-goal）**：v4-contract §17（修订二收紧）——「§4.6 search 维持 per-list 字面子串语义，不做跨会话检索」；无对应 feature ID、无 deferred 候选资格，**再启用须推翻正式修订**。
- **cascade 编排层（owner 裁决 q1，永久 non-goal）**：同节——sidecar 不自建级联编排/子删除聚合/重试/部分失败可见性。
- **对 §2.2 缺口的含义**：per-directory 服务端过滤的补齐路径被上述 non-goal 边界结构性封堵（§4.6 search 无 directory 轴 + §4.5 cursor 指纹无 directory 谓词 + §17 永久 non-goal 三重叠加）；多工作目录客户端在 v4 的补偿路径**只有客户端侧过滤**（全局拉取 + `item.directory` 本地过滤），该模式成文化即 §5-P2。
- **§4.6 范围冻结**：`/slimapi/directories` 不升 DB 投影（保持 /experimental/session 发现形态）——目录发现面同样冻结，不提供 directory 维度的服务端列表能力扩展点。

### 6.3 本文边界（自我声明）

- 本文为审计交付物，非实现计划：所有「若……需要什么」均为条件性成本/工作量刻画，不附带启动建议。
- §2 checklist 的消费者是 ocdroid 仓库侧开发者（是否迁移、何时迁移为 ocdroid 侧自主决策）；本仓库侧政策输出仅 §1.4 + §5。
- 引用行号均为快照 0b836e7；仓库演进后以 CHANGELOG + 契约现文为准。

---

## 附：证据源索引（审计链）

| 证据 | 位置 |
|---|---|
| 本蒸馏源报告 | 03-reports/D02-v3-retirement.md |
| v4 完备性矩阵（G1-G5 差距清单、44 等价/9 non-goals） | 03-reports/D01-v4-completeness.md §7 |
| 路由普查真值（54 路由、v3_face=50、错误码族） | 01-explore/route-census.md / route-census.csv / expected-keys.csv |
| directory 能力缺口 | 02-findings/F-121.md（P2 gap，confirmed，Phase 3 复核通过） |
| 文档漂移发现 | 02-findings/F-123.md / F-124.md / F-125.md |
| resync 值域风险 | 02-findings/F-122.md |
| 规范耦合量化 | 02-findings/F-126.md |
| 契约冻结条款 | docs/specs/v4-contract.md §0.3/§0.4/§2/§4.5/§4.6/§5.2/§17/§9.4；docs/specs/v3-contract.md（288 行） |
| 版本窗裁决记录 | CHANGELOG.md [3.3.1]/[4.0.0]/[4.1.0]/[4.2.0]/[4.4.0]；src/oc_slimapi/versioning.py:44 |

*审计快照 0b836e7；rg/实读取证；仓库只读合规（未运行 pytest/pip/git 写操作）。*
