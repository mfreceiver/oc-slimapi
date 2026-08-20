# D01 — A1 v4 完备性矩阵（54 路由 × ?v=3/?v=4 能力对照）

> Phase 2 / A1。快照 BASELINE_HEAD=0b836e7（v4.4.0，wire (3,4)，readiness 10/10 全亮）。
> 输入：`01-explore/route-census.csv`（54 行真值）、`01-explore/expected-keys.csv`（580 期望键，唯一来源）、parts/e1-*.md 卡片、dataflows.md 场景 5-9、v3-contract.md、v4-contract.md（§10/§12-§16 重点）。
> 行号均为本快照工作树实读（src 侧路径省略 `src/oc_slimapi/` 前缀时指该包下文件）。
> 新发现：F-101、F-102（见 02-findings/；均 P3，无 P2 以上契约违约）。

---

## 0. 结论摘要

- **54/54 路由在 `?v=4` 下无「缺失」**：census `presence` 全 54 行 = `both`（`route-census.md` 双计数声明①②），无 contract_only 行 → 「契约要求但实现缺失」的期望键集合为空，差异性质 `缺失` 出现 0 次（判据本身不可触达，见 §4）。
- 路由级差异性质分布：**等价 44 / 增强 7 / 变形 3 / 退役 0（路由级）/ 缺失 0**。退役均为**子面退役**（sessions 的 directory 参数、/events 的 tokens=1、v4 SSE 的 server.connected/welcome 与非冻结 reason resync），不构成整路由退役。
- 已声明退役面 5 项全部与契约逐条对上（§5 核对）；v4 新增面（§3.3 readiness 十 ID、§12-§16 修订面）逐项实现完整且测试锁定（§6 核对）。
- 三清单（§7）：全覆盖 44；有差距 5（均已在契约中声明为设计差异，但事实性阻碍 v3 消费方迁移，逐项标阻塞度）；有意不覆盖 9（§17 non-goals + 差异列设计内）。
- 期望键机械关系（§8）：580 键全部归属 54 路由，键级聚合 happy_path×54 + v3_face×50 + v4_face×53 + feature_off×10 + boundary×14 + error_*×399 = 580，与矩阵行一一对应。

---

## 1. 判定口径（差异性质定义）

| 性质 | 定义 | 本矩阵判据 |
|---|---|---|
| 等价 | `?v=3` 与 `?v=4` 行为逐字节/语义相同（无版本分支或分支无 wire 差异） | v4-contract §10「零 v4 差异」行 + 源码无 wire 分支 |
| 增强 | v4 加性能力，v3 语义原样保留 | §10 差异列 4 条 + §10 修订块加性行 |
| 变形 | v4 wire 形状/管线与 v3 异构（非纯加性） | §10 差异列（sessions §4）+ 修订块（§12/§13） |
| 退役 | v3 能力在 v4 被声明移除 | §5.2/§7.3（本审计均为子面级，无整路由级） |
| 缺失 | 契约要求 v4 提供但实现缺失 | 恒等于 census `presence=contract_only` 行的期望键（本审计 0 行，§4 验证） |

行级取**主导性质**，子面差异在备注列载明并给双侧引文（变形/退役强制双侧引文，见各行）。

---

## 2. 能力矩阵（54 行全量）

缩写：v4C=v4-contract；「§10 差异列」指 v4C §10 已发布 4 条差异表或 §10 修订块。所有行 `presence=both`（census）。

### 2.1 读组（27 行 GET，含 SSE 2 行另列 §2.4）

| # | method+path | ?v=3 实际行为 | ?v=4 实际行为 | 性质 | 契据（v4C §10 差异列）+ 代码/契约引文 |
|---|---|---|---|---|---|
| 1 | GET /slimapi/sessions | 上游 GET /session 受控代理 + coalesce lease；skeleton 投影 envelope `{items,complete}` + ETag/Vary/304；directory query 消费剥离→header 通道；`archived/parent/cursor` 出现→422；limit≤1000（sessions.py:693 分叉 v3 侧 :700-791、:92 `_finalize_sessions_response`；dataflows 5b） | dbaux 只读 SQL 投影常态（§4 全参数矩阵 archived/parent/search/cursor/limit≤500）；72 格降级矩阵 + Class A HTTP 回退 degraded:true；`roots/start`→422；directory 任何形式→400 `directory_retired_in_v4`；§13 canonical item/envelope（degraded required bool + partial/degraded 标记）；§15 ETag（identity 强/gzip 弱）+Vary 恒发 | **变形**（含子面退役：directory） | 契约：v4C §4 全量 + §10 行 1 + §5.2 行 1（「整体退役…一律 400」）+ §4.1 参数矩阵。实现：sessions.py:371 `_sessions_v4`、:383-402 参数 422、:409-427 cursor、:430-487 dbaux、:488-568 降级矩阵、:598-645 `_v4_json_response`（§15 门控：关=无 ETag 摘 Vary / 开=ETag+Vary 恒发）；selector.py:197-199 退休表 + :205-212 统一错误体 + :668-673 拦截 |
| 2 | GET /slimapi/sessions/status | 上游 GET /session/status 全局 map + TurnRegistry merge；directory 仅 workspace 路由通道（sessions.py:806+，docstring 明示恒全局 map） | **零 v4 分叉**（无版本分支代码路径；directory 消费继承 v3） | 等价 | 契约：v4C §10 显式注载「零 v4 分叉（无版本分支代码路径），directory 消费继承 v3（§5.2 表行）」。实现：sessions.py:806-830 无 wire_view 分支（对照 :693 sessions 列表分叉） |
| 3 | GET /slimapi/session/{sid} | 受控代理透传 + `_project_session` skeleton 投影（read_groups.py:555-570 `read_passthrough_get(project=...)`） | §13 canonical 单查：dbaux 点查优先→裸 SessionSkeletonV4（与列表同一 canonical projector）；不可用→整响应 native fallback degraded；required 不可表示→503 `auxiliary_unavailable` | **变形** | 契约：v4C §13 全量 + §10 修订块行 2（「升级为与列表同源 canonical…?v=3 恒 v3 skeleton」）。实现：read_groups.py:555-570 分叉（`wire_view==4 ∧ _V4_SESSION_SINGLE_REVISION_ACTIVE()`）；sessions.py:571-585 门控；skeleton.py:1017 `canonical_session_skeleton_v4`（列表/单查共用，§13.3 同一 projector 不变量） |
| 4 | GET /slimapi/session/{sid}/context | 上游 GET /api/session/{sid}/context 受控代理 + ETag（read_groups.py:619） | 同 v3（无版本分支） | 等价 | 契约：v4C §10「读组零 v4 差异」。实现：read_groups.py:619 无 wire 分支（census contract_refs v3:§10.a） |
| 5 | GET /slimapi/messages/{sid} | skeleton+envelope+ETag（REP wire=v3 域）+coalesce 单飞；limit 1..200；expandRefs href `?v=3`（dataflows 场景 1） | 投影管线逐字节同 v3；仅 expandRefs href `?v=4`（§14 视图跟随）；REP 域 wire=v4 与 v3 validator 隔离 | 等价（§14 弱差异：href v 值跟随请求视图；expand 端点行为两视图逐字节相同） | 契约：v4C §10 修订块行 3（「href 按 wire 视图生成（?v=4 响应→?v=4）」）+ §3.1 注记（expand 可达且行为与 v3 逐字节相同）。实现：messages.py:58-66 `_expand_wire_view`（selector view==4 ∧ `messages.expand.v4∈SATISFIED`→4 else 3）；skeleton.py:167-187 `_expand_ref`（`?v={wire_view}`，v 第一键序）；messages.py:948 路由体 wire 分叉仅此一线 |
| 6 | GET /slimapi/messages/{sid}/full/{mid} | 单飞+transform absorb+strip diagnostics；无 ETag；Vary: X-Opencode-Directory（messages.py:1144+；dataflows 场景 3） | 同 v3（无版本分支） | 等价 | 契约：v4C §10「messages…零 v4 差异」（4 条+2 expand 显式列举外）。实现：messages.py:1144-1249 无 wire_view 分支（census contract_refs NONE，v3 §4a/§4b 散文覆盖） |
| 7 | GET /slimapi/messages/{sid}/expand/{category}/{mid} | §4b 12 类目 expand，求值序 400→准入→单飞→cap→decode→locate→extract→cap（messages.py:1616；dataflows 场景 4） | **与 v3 逐字节相同**（selector 放行，行为继承 v3，无 v4 分叉） | 等价 | 契约：v4C §3.1 注记（「2 条 expand 路由在 ?v=4 下可达且行为与 v3 逐字节相同」）+ §10 注载行 3。实现：messages.py:1616 `_expand_fragment` 无 wire_view 消费 |
| 8 | GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID} | 同上 part 级（messages.py:1631） | 同 v3 逐字节 | 等价 | 同 #7 |
| 9 | GET /slimapi/config/providers | 上游 GET /config/providers 受控透传 + 既有 ETag（skeleton REP 域 wire=v3）；上游 4xx verbatim（dataflows 6a） | §12 安全投影：白名单 schema（顶层恰两 key）+ 递归丢弃 + 修订三 `limit` 恢复（子键 {context,input,output} int-else-omit）+ 四限额（256/1024/64/8MiB）+ 三带错误面（502/413/503）+ canonical ETag（`providers-projection-v2` 域）+ Vary 恒发 | **变形** | 契约：v4C §12 全量 + 修订三（§12.1 ModelEntry limit + §12.6 REP v1→v2）+ §10 修订块行 1（「?v=3 恒透传不变」）。实现：read_groups.py:382 `_V4_PROVIDERS_REVISION_ACTIVE`、:403-409 分叉、:247-368 `_handle_providers_v4`（③-⑫ 12 步）；providers_projection.py:54-57 限额常量、:66 REP v2、:149 重复成员拒绝、:187-280 校验、:295-375 投影（:344-355 修订三 limit）、:409-412 canonical+cap |
| 10 | GET /slimapi/agent | catalog 面 skeleton + ETag/Vary（agent.py:17） | 同 v3 | 等价 | 契约：v4C §10「零 v4 差异」。实现：agent.py:17 无 wire 分支 |
| 11 | GET /slimapi/command | 同上（command.py:17） | 同 v3 | 等价 | 同 #10 |
| 12 | GET /slimapi/file | 读组受控代理 + allowlist fail-closed 403（read_groups.py:149） | 同 v3（§5.2 allowlist 作用域全覆盖两视图一致） | 等价 | 契约：v4C §5.2（「/slimapi/file/** fail-closed」未分版本）+ §10 零差异。实现：read_groups.py:149 无 wire 分支 |
| 13 | GET /slimapi/file/content | 同上（read_groups.py:166） | 同 v3 | 等价 | 同 #12 |
| 14 | GET /slimapi/file/status | 同上（read_groups.py:178） | 同 v3 | 等价 | 同 #12 |
| 15 | GET /slimapi/find/file | 同上（read_groups.py:227） | 同 v3 | 等价 | 同 #12 |
| 16 | GET /slimapi/vcs | 读组代理 + ETag（read_groups.py:192） | 同 v3 | 等价 | 同 #10 |
| 17 | GET /slimapi/vcs/status | 同上（read_groups.py:201） | 同 v3 | 等价 | 同 #10 |
| 18 | GET /slimapi/vcs/diff | 同上（read_groups.py:210） | 同 v3 | 等价 | 同 #10 |
| 19 | GET /slimapi/api/session/active | 代理（read_groups.py:582） | 同 v3 | 等价 | 同 #10 |
| 20 | GET /slimapi/global/health | 代理（read_groups.py:591） | 同 v3 | 等价 | 同 #10 |
| 21 | GET /slimapi/sessions/{sid}/children | 上游 child 列表 skeleton（children.py:55） | 同 v3 | 等价 | 契约：v4C §10「零 v4 差异」（children 散文覆盖 v3 §10.a 注）。实现：children.py:55 无 wire 分支 |
| 22 | GET /slimapi/sessions/{sid}/diff | 代理（diff.py:63） | 同 v3 | 等价 | 同 #21 |
| 23 | GET /slimapi/sessions/{sid}/todo | 代理（todo.py:47） | 同 v3 | 等价 | 同 #21 |
| 24 | GET /slimapi/directories | /experimental/session 发现 + allowlist 过滤 skeleton（directories.py:17） | 同 v3（§4.6 范围冻结：**不**升 DB 投影） | 等价 | 契约：v4C §4.6 末条（「保持现形态…不升 DB 投影（范围冻结）」）。实现：directories.py:17 无 wire 分支 |
| 25 | GET /slimapi/permissions | 发现 flight + permission fanout skeleton（permissions.py:84） | 同 v3 | 等价 | 契约：v4C §10 零差异。实现：permissions.py:84 无 wire 分支 |
| 26 | GET /slimapi/questions | 发现 flight + question fanout（questions.py:78） | 同 v3 | 等价 | 同 #25 |
| 27 | GET /slimapi/actions | 本地 manifest 清单（actions.py:130） | 同 v3 | 等价 | 契约：v3 §5/§8 收编；v4C §10 零差异。实现：actions.py:130 无 wire 分支 |

### 2.2 发现/运维（6 行）

| # | method+path | ?v=3 | ?v=4 | 性质 | 契据 + 引文 |
|---|---|---|---|---|---|
| 28 | GET /slimapi/versions | `{current:4(双窗期恒最新), available:[3,4], capabilities:{3:{…含 expand}}}`；selector 豁免（无 v 可达）；非 GET→405+Allow: GET | capabilities 增 `"4"` 面：静态四键 `globalSessions/auxiliaryFilters/sseReplay/qpImmediateFull` + 加性 `readiness`（§3.3 十 ID 载荷）+ `expand`（iff `messages.expand.v4∈satisfied`） | **增强** | 契约：v4C §3.1 + §10 差异列行 4 + §3.3/§14 扩键。实现：versions.py:61-64（current=S-B04）、:67-98 CAPABILITIES（3/4 两面）、:104-132 `_capabilities4`（readiness 恒发 :124、expand iff :125-131）；测试锁定 test_versions_route.py:110 `test_versions_caps4_static_face_no_runtime_keys`、test_versions_readiness.py:504 静态四键字节锁定 |
| 29 | GET /slimapi/health | v3 视图：schema.version=3/server.api_version=3/slimapi_contract=3 + features 块（含 features.allowlist，3.3.0 起） | v4 视图：三个版本字段同源=4（S-B04）+ 瞬态 `auxiliary:{available,mode}` + features.allowlist 照发 | **增强** | 契约：v4C §3.2（「按请求 wireVersion 返回对应视图…v4 视图新增瞬态字段 auxiliary」；allowlist 见 v3C §3a 与 F-102）。实现：health.py:30-50（view 单源驱动三字段）、:75-85（auxiliary 仅 v4 视图，v3 视图无该键）、:90-101（allowlist 两视图） |
| 30 | GET /slimapi/ready | 上游 5s ping 探活，形状 v3 冻结（health.py:105+，READY_VIEW=3） | **零 v4 分叉**（形状与值恒 v3） | 等价 | 契约：v4C §3.2「ready 端点形状不变」+ §10 零差异。实现：health.py:19 `READY_VIEW = 3`、:105-110 注释明示不随版本分叉 |
| 31 | GET /slimapi/metrics | hub/token/traffic/sweep/dbaux/sessionsDegraded/replay 快照（metrics.py:20） | 同 v3（内容含 v4 观测维度 degradedMatrix 等，与请求版本无关） | 等价 | 契约：v4C §9.1 维度扩展为观测内容非路由分叉；§10 零差异。实现：metrics.py:20-111 无 wire_view 分支 |
| 32 | POST /slimapi/actions/{name} | 本地子进程动作（v3 §5/§8 收编；详细 wire 规范留存 v2-contract §2 历史——F-019） | 同 v3 | 等价 | 契约：v4C §10 零差异。实现：actions.py:140 无 wire 分支 |
| 33 | （/slimapi/versions 非 GET） | 405+`Allow: GET` 优先于一切（selector.py:533-545） | 同 v3（两视图一致） | 等价 | 契约：v4C §2 表行 5（「非 GET → 405+Allow: GET 优先于一切（v3 §8.3 ①不变）」）。实现：selector.py:533-545 |

注：#33 计入 expected-keys 的 `error_method_not_allowed`（versions 行唯一 method 边界键），路由行本身即 census `GET /slimapi/versions` 行（method_not_allowed 键挂该行）。**矩阵路由计数保持 54**（census 主键口径），#33 为该行的 method 边界注记。

### 2.3 SSE（2 行）

| # | method+path | ?v=3 | ?v=4 | 性质 | 契据 + 引文 |
|---|---|---|---|---|---|
| 34 | GET /slimapi/events | 策展全局流：首帧 `slimapi.meta{subscriberId,tokens}` → server.connected welcome → 业务帧（digest/q/p/error，无 id）→ 任意 Last-Event-ID → `resync{reconnect_no_replay}`；`tokens=1` 合法（附 token 帧）；恒 identity 无 Vary；溢出断连 = `resync{subscriber_backpressure}`+STOP（dataflows 场景 8） | v4：welcome 抑制（线上首帧恒 slimapi.meta）；meta 加性扩展 `{subscriberId,tokens,capabilities:{sseReplay:true},epoch,seqBase}`；业务帧带 `id: g:<epoch>:<seq>`；Last-Event-ID 四级分类重放（①语法②域③epoch④barrier/窗口）+ resync 四值域；**`tokens=1` → 400 `tokens_stream_retired_in_v4`**；溢出断连 STOP-only | **增强**（含子面退役：tokens=1 统一流、server.connected、非冻结 reason resync） | 契约：v4C §7 全量 + §10 差异列行 2 + §7.3（tokens=1 400 + 冻结错误体）+ §7.5（welcome 抑制/meta 扩展/SSE 恒 identity）+ §7.2（resync 值域四值冻结）。实现：events.py:88-89（invalid_tokens）、:91-99（v4×tokens=1→400，错误体 :21-24 与契约 §7.3 逐字一致）；registry.py:226 `hub.subscribe(welcome=not wire_v4)`（welcome 抑制）；replay_wire.py:72-77 `V4_RESYNC_REASONS` 四值；hub_types.py:310-319（v4 STOP-only / v3 resync+STOP 冻结对）；replay 分类 replay_wire.py:126/169 + replay_log.py:399-468 |
| 35 | GET /slimapi/sessions/{sid}/stream | token 流：server.connected → tombstones → snapshot 握手预填；`tokens` 恒 true；v3 LEI→resync{reconnect_no_replay,sessionID}；溢出 resync{subscriber_backpressure,sessionID}+STOP；恒 identity（dataflows 场景 9） | v4：no-prefill 握手（无 server.connected/无历史 tombstone/无 snapshot）；meta v4 扩展（epoch=本 sid 域、seqBase）；帧带 `id: t:<sid>:<epoch>:<seq>` per-sid 独立序列；tombstone 重放以 `message.removed` 轻量撤销帧占 seq；溢出/服务端终止非冻结 reason → silent STOP | **增强**（含子面退役：握手预填帧、非冻结 reason resync） | 契约：v4C §7（§7.0③ per-sid 域、§7.1 t: 语法、§7.2 tombstone 撤销帧）+ §10 差异列行 3。实现：token_stream.py:125+；subscriber.py:625+ 准入、:694-711 attach（v4 no-prefill：tokenstream/hub.py:1289-1300）、:450-452 v4 STOP-only、:472-479 terminate 非冻结域 silent STOP；hub.py:1371-1390 `_replay_publish_token`（message.part.snapshot 家族不进 log）、:835 tombstone、:1396 状态失效 barrier |

### 2.4 写组（20 行：DELETE 1 + PATCH 1 + POST 18）

| # | method+path | ?v=3 | ?v=4 | 性质 | 契据 + 引文 |
|---|---|---|---|---|---|
| 36 | DELETE /slimapi/session/{session_id} | 受控写：实体读+cap 413、4xx verbatim、5xx→503、no-store（write_groups.py:274） | **DELETE 继承**（§16 表行 2「不退役」） | 等价 | 契约：v4C §16 表（「DELETE 继承（不退役）」）+ §16.1 末条（PATCH/DELETE 两视图逐字不变）。实现：write_groups.py:274 无 wire 分支 |
| 37 | PATCH /slimapi/session/{session_id} | 同上受控写（write_groups.py:262） | **PATCH 继承**（applicability 行显式声明，非 fallthrough） | 等价 | 同 #36 |
| 38 | POST /slimapi/session | 受控写（PromptPayload→上游 POST /session）（write_groups.py:256） | 同 v3 | 等价 | 契约：v4C §10 零差异。实现：write_groups.py:256 无 wire 分支 |
| 39 | POST /slimapi/session/{session_id} | **404 `thin_route_not_found`**（路由注册前落 catch-all proxy.py:44-51；注册后 handler `_pre_revision_404` 复现同字节答案） | **≡ PATCH 等效路由**（§16.2-a）：同一 PatchPayload 透传、逐字节等效受控写管线 | **增强**（v4-only 路由；v3 面维持 404 现状） | 契约：v4C §16.2-a + §10 修订块行（「POST /slimapi/session/{sid}（新）…?v=3 → 404 thin_route_not_found 现状不变」）。实现：write_groups.py:326-339（`_post_actions_admitted` :303-310 = `wire_view>=4 ∧ session.post-actions.v4∈SATISFIED`；非 admitted→`_pre_revision_404` :314-322 → `error_response("thin_route_not_found",404)` 与 proxy.py:47-51 逐字节一致） |
| 40 | POST /slimapi/session/{session_id}/archive | 404（同上） | **便捷 archive**（§16.2-c）：octet 级缺省判据（实体长度=0→合成 `{"time":{"archived":<ms>}}` 紧凑形+application/json；非空含 `{}`/纯空白→一律不解析逐字节透传）；错误映射零偏差 | **增强** | 契约：v4C §16.2-c 三项精确冻结 + §10 修订块。实现：write_groups.py:343-395（判空 :378-385、合成 :387-395 `int(time.time()*1000)` 判空后立即读、preset_body 单次读 socket :368-376） |
| 41 | POST /slimapi/session/{session_id}/delete | 404（同上） | **≡ DELETE 等效路由**（§16.2-b）：实体读取同 cap 同序 413、Content-Type 透传、body 逐字节转发，无 ignore-body 分支；上游递归删子+吞错语义如实继承 | **增强** | 契约：v4C §16.2-b + §10 修订块。实现：write_groups.py:398-414（docstring 载明 NO ignore-body branch；`_write_passthrough(method="DELETE", ...)`） |
| 42 | POST /slimapi/session/{session_id}/prompt_async | 受控写 + turn bump S2（write_groups.py:417） | 同 v3 | 等价 | 契约：v4C §10 零差异（write 17 条列举内）。实现：write_groups.py:417 无 wire 分支 |
| 43 | POST /slimapi/session/{session_id}/abort | 同上（write_groups.py:425） | 同 v3 | 等价 | 同 #42 |
| 44 | POST /slimapi/session/{session_id}/summarize | 同上（write_groups.py:432） | 同 v3 | 等价 | 同 #42 |
| 45 | POST /slimapi/session/{session_id}/fork | 同上（write_groups.py:440） | 同 v3 | 等价 | 同 #42 |
| 46 | POST /slimapi/session/{session_id}/permissions/{permission_id} | 同上（write_groups.py:455） | 同 v3 | 等价 | 同 #42 |
| 47 | POST /slimapi/question/{request_id}/reply | 同上（write_groups.py:465） | 同 v3 | 等价 | 同 #42 |
| 48 | POST /slimapi/question/{request_id}/reject | 同上（write_groups.py:473） | 同 v3 | 等价 | 同 #42 |
| 49 | POST /slimapi/session/{session_id}/revert | 同上（write_groups.py:448） | 同 v3 | 等价 | 同 #42 |
| 50 | POST /slimapi/session/{session_id}/revert/stage | tolerant 组受控写（write_groups.py:545） | 同 v3 | 等价 | 同 #42 |
| 51 | POST /slimapi/session/{session_id}/revert/commit | 同上（write_groups.py:573） | 同 v3 | 等价 | 同 #42 |
| 52 | POST /slimapi/session/{session_id}/revert/clear | 同上（write_groups.py:560） | 同 v3 | 等价 | 同 #42 |
| 53 | POST /slimapi/session/{session_id}/command | 受控写（write_groups.py:481） | 同 v3 | 等价 | 同 #42 |
| 54 | POST /slimapi/session/{session_id}/agent | tolerant 组（write_groups.py:517） | 同 v3 | 等价 | 同 #42 |
| — | POST /slimapi/session/{session_id}/model | tolerant 组（write_groups.py:531） | 同 v3 | 等价 | 同 #42 |

（修正计数：写组实际 21 行——DELETE 1 + PATCH 1 + POST 19（含 #38 POST /slimapi/session 与 17 条 session 子动作与 POST actions 归发现/运维）；矩阵总行数以 census 54 主键为准，上表编号至 54 与 census 对齐：#54 行 model，agent 并入同格注记。**census 54 = 读 27（含 SSE 2）+ 发现/运维 6（versions/health/ready/metrics/actions GET/actions POST）+ 写 21**；契约 §10「54 条（write 20）」的 write 口径不含 POST actions/{name}（计入发现/运维 6），两口径总数一致 54。）

**行级性质汇总（54 行）**：等价 44（§2.1 除 #1/#3/#9 外 24 行 + §2.2 #30/#31/#32 三行 + §2.4 除 #39-41 外 17 行含 model 行）；增强 7（#28 versions、#29 health、#34 events、#35 stream、#39 POST session/{sid}、#40 archive、#41 delete）；变形 3（#1 sessions、#3 session single、#9 providers）；退役 0；缺失 0。（#33 为 versions 非 GET 的 method 边界注记行，非 census 主键，不计数。）

---

## 3. §16 method 边界（三组合 × 两视图 × 门控）专用核对

| 组合 | ?v=3 | ?v=4（四位组合表现行：boundary∈sat ∧ post-actions∈sat，即激活态） |
|---|---|---|
| POST /slimapi/session/{sid} | 404 thin_route_not_found（`_pre_revision_404`） | 等效路由（放行至 handler→PATCH 管线）；过渡态 405 `method_not_applicable`（`Allow: GET, PATCH, DELETE`）已因 post-actions∈SATISFIED 熄灭 |
| POST /slimapi/session/{sid}/archive | 404 | 等效路由；过渡态 405（空 Allow）已熄灭 |
| POST /slimapi/session/{sid}/delete | 404 | 等效路由；同上 |

- 契约：v4C §16.0 操作表 + §16.3 四位组合表第三行（当前态）。实现：selector.py:592-609（405 插列位置=version 400 之后、directory 400 之前，符合 §8.4 优先级插列；两条件合取 `_v4_method_boundary_405_live` :257-262 当前返回 False）；Allow 字面量冻结 selector.py:247-254；handler 侧 `_post_actions_admitted` write_groups.py:303-310。
- `method_not_applicable` 错误码保留定义、当前无命中面（§8.4 修订二注记）——与实现一致（405 分支代码保留、动态熄灭）。
- expected-keys `error_method_not_applicable×3`（三条 POST 行）+ `error_thin_route_not_found×3`（同三行的 v3 面 404）均被本节覆盖。

---

## 4. 「缺失」判据验证（presence=contract_only ⇒ 差异性质恒缺失）

- 机制：expected-keys 生成函数（方案 §5 E2 冻结）对 `presence=contract_only` 行同样产出期望键（happy_path/版本面/错误码/feature/boundary），差异性质判据规定此类键恒为 `缺失`（实际行为格记 MISSING）。
- 事实：`route-census.csv` 54 行 `presence` 全部 = `both`（脚本复核：`Counter({'both': 54})`）；`route-census.md` 双计数声明 contract_only 行数 = **0**。
- 结论：**本审计「缺失」性质 0 次出现不是抽样结果而是全集事实**——契约（v3/v4 §10 路由表）声明的全部路由均已实现，不存在「契约要求但实现缺失」格；对应地 E2 未生成任何缺实现 draft（与 route-census.md「零 contract-violation draft」声明一致）。
- 反向核对（actual_only）：同样为 0——54 行全部双侧（实现 54 = 联合主键 54），无「实现存在而契约完全未收录」的路由（5 条无字面命中路由经散文覆盖，见 route-census.md「契约引用覆盖说明」；D14/F-019 处理文档侧滞后）。

---

## 5. 已声明退役面核对（5 项全过）

| # | 退役面 | 契约声明 | 实现证据 | 结论 |
|---|---|---|---|---|
| 1 | sessions directory 整体退役（`directory_retired_in_v4` 400） | v4C §5.2 行 1（query 单值/多值、header 任何形式、混合→一律 400；selector 层拦截先于路由；不泄露目录存在性）+ §8.1 | selector.py:197-199（`_DIRECTORY_V4_RETIRED_PATTERNS` 仅 `^/slimapi/sessions$`——SET DIFFERENCE 设计防漂移）+ :205-212（统一体 code+hint 无 directory 回显）+ :668-673（query 键存在或 header 任何形态→400，优先于 v3 消费阶梯） | ✅ 一致 |
| 2 | `/events?tokens=1` → 400 | v4C §7.3（错误体逐字：`{"code":"tokens_stream_retired_in_v4","hint":"token 流请使用 /slimapi/sessions/{sid}/stream"}`；流打开前拦截；v3 请求该参数语义不变） | events.py:21-24 常量与契约逐字一致；:91-99 v4∧tokens=="1"→CodedHTTPException 400（先于 subscribe/sse_open——无 SSE 字节、无订阅位）；v3 面 tokens=1 照常 attach（:160-162） | ✅ 一致（B3b-5 一致性注记成立） |
| 3 | SSE v4 握手抑制（welcome 帧） | v4C §7.5（「v4 连接不产出连接本地 server.connected 首帧（v3 照旧产出）；v4 线上首帧恒为 slimapi.meta」）+ §7.0② meta 恒首帧无 id | registry.py:226 `hub.subscribe(welcome=not wire_v4)` + :227 `subscriber.wire_v4=wire_v4`（单无 await 临界区内，防 fanout 竞态）；token 流侧 subscriber.py:694-711 v4 no-prefill（tokenstream/hub.py:1289-1300）；events.py:188-189 meta 先行 | ✅ 一致 |
| 4 | resync reason 值域冻结四值 | v4C §7.2（`epoch_changed \| replay_expired \| replay_gap \| reconnect_no_replay`，v4 冻结加性扩展）+ §7.0 裁决记录 | replay_wire.py:72-77 `V4_RESYNC_REASONS = frozenset({EPOCH_CHANGED, REPLAY_EXPIRED, REPLAY_GAP, RECONNECT_NO_REPLAY})`（注释明示 PRODUCTION allowlist 非 test-only oracle；subscriber/hub v4 分支与 wire 测试同源引用） | ✅ 一致 |
| 5 | 非冻结 reason 终结连接（STOP-only） | v4C §7.2 值域冻结的反面（legacy v3 reason 不入 v4 wire；断连本身为可观察信号） | hub_types.py:310-319（控制面订阅者：v4∧reason∉V4_RESYNC_REASONS→仅 STOP，v3 保留 resync+STOP 冻结对）；token 侧 subscriber.py:450-452（runtime 溢出 v4 STOP-only）+ :472-479（`terminate(reason)` v4 仅冻结域 reason 发 resync 否则 silent STOP） | ✅ 一致 |

---

## 6. v4 新增面实现完整度核对（§3.3 + §12-§16 逐面「契约声明 vs 实现」）

### 6.1 §3.3 readiness 十 ID 与 readiness.py/features.py 同源一致性

| 核对项 | 契约 | 实现 | 结论 |
|---|---|---|---|
| 全集 U 十 ID 及顺序 | §3.3 编号 1-10（第 10 项排序位于 method.boundary.v4 之后） | readiness.py:58-69 `REQUIRED` 元组逐 ID 同序 | ✅ |
| `required ≡ U` 恒发全集 | §3.3「服务端必须以全集发出（修订二后 U=十项）」 | readiness_payload :175-179（`required=list(normalize(REQUIRED))` 恒全集；normalize=去重+UTF-8 字节序 :96-103） | ✅ |
| `ready` 公式 | `ready ⇔ f(required) ⊆ f(satisfied)` 派生值不许独立翻转 | readiness.py:147-160（`set(normalize(required)) <= set(normalize(satisfied))`；payload 内派生 :176） | ✅ |
| 蕴含守卫（条件⑦） | `session.post-actions.v4 ∈ satisfied ⇒ method.boundary.v4 ∈ satisfied` | validate_dependencies :126-144（RuntimeError）+ 模块级守卫 :186-187 + payload 发射前复验 :174 | ✅（结构性不发出⑦违约载荷） |
| 未知 ID 拒绝 | §3.3「未知 ID（∉U）拒绝——不静默忽略」 | validate :106-123（RuntimeError 列名 offender） | ✅ |
| satisfied 当前态 | 修订二实施批次后十项全亮 ready:true | readiness.py:93 `SATISFIED = frozenset(REQUIRED)` | ✅（与 dataflows.md 头注「10/10 全亮」一致） |
| 静态性（不随 DB/运行时抖动） | §3.1「能力键为静态键…不随 DB 抖动」 | readiness 集合仅随代码版本（模块常量，零运行时输入）；versions.py:104-132 组装不含运行时态 | ✅ |

### 6.2 `capabilities["4"]` 静态性测试锁定

- 契约：§3.1 静态四键 + §3.3/§14 扩键（readiness 恒广告、expand iff）。
- 实现：versions.py:92-98（静态四键，sseReplay 与 meta 帧同源 `META_CAPABILITY_KEYS`——versions 与 meta 两 lane 结构性不漂移）、:104-132（扩键 iff 矩阵）。
- 测试锁定：`tests/test_versions_route.py:97`（静态键序）、`:110 test_versions_caps4_static_face_no_runtime_keys`（无运行时派生键）、`tests/test_versions_readiness.py:504 test_versions_caps4_static_four_keys_byte_unchanged`（四键字节冻结）+ 同文件 expand iff 双向不变量（§3.3 组合①-④）测试。✅
- 残留：versions.py:24 模块 docstring 仍写「nine-ID readiness gate」——修订二后为十 ID，注释漂移（wire 正确）→ **F-101**。

### 6.3 §12 providers 投影（含修订三 limit 恢复）

| 条款 | 契约 | 实现 | 结论 |
|---|---|---|---|
| 顶层恰两 key / 递归丢弃 / required 集 | §12.1 | providers_projection.py:199-208（exact-two）、:295-375（白名单投影=递归丢弃） | ✅ |
| optional source/status str-else-omit；variants absent 省略/非 map malformed | §12.1 | :321-323/:363-365（omit）；:236-243（唯一 optional 错误路径） | ✅ |
| 修订三 limit 恢复（{context,input,output} 逐子键 int-else-omit、bool 排除、orjson 64 位域、零子键→整键省略、零错误路径增量） | §12.1 修订三 + §12.5.3 零增量 | :344-355 + :80-81 `_ORJSON_INT_MIN/MAX` 实测边界常量 | ✅ |
| 排序（UTF-8 字节序三层 + default OPT_SORT_KEYS） | §12.2 | :304/:312/:330-331/:409 | ✅ |
| default 三重校验 | §12.3 | :265-280 | ✅ |
| 四限额 wire 常量（256/1024/64/8388608，无 env 覆写，first-triggered-wins 无截断） | §12.4 | :54-57 常量、:300-308/:326-328（计数绊线）、:410-412（body cap limit="projected_body_bytes"） | ✅ |
| 错误契约三带（502 malformed/upstream_http、413 两族、503 upstream_unavailable/transform_busy）+ 求值序 ①-⑫ + offload 边界（⑥-⑪ 全在 worker、permit 在 ⑤ 唯一 transform_busy 点） | §12.5 | read_groups.py:290-368（③网络→④cap→⑤permit→worker→⑫主上下文 INM）；路由映射 :346-356；`_loads_strict` :149（stdlib json 重复成员拒绝） | ✅ |
| ETag/canonical（canonical 字节即 wire body、强/弱 validator、REP `providers-projection-v2` 域隔离、etag off→无 ETag 但 Vary 仍发） | §12.6 + 修订三指纹 bump | :409（orjson OPT_SORT_KEYS）、:428-432（compute_etag）、:111 providers_rep_version（含四常量+wire=v4）、:66 REP v2 | ✅ |

### 6.4 §13 单查 parity

- 契约要点：单查=裸 SessionSkeletonV4、与列表同一 canonical projector、dbaux 点查优先、whole-response native fallback 禁跨源拼接、§13.2a required 不可 null 不可得→503 `auxiliary_unavailable`（复用）、§13.4 degraded required bool + partial⇒degraded、§13.5 project join 三不变量。
- 实现：read_groups.py:555-570（分叉 + dbaux 点查 → canonical 裸对象 json_response；不可表示→`_aux_unavailable()`）；sessions.py:576-585（门控）、:452-462（列表侧同款 fail-closed）、:479-487（envelope degraded=any + required 化）；skeleton.py:1017（canonical projector 列表/单查/HTTP fallback 三路共用——§13.3「不存在第二投影实现」成立）；:659-675 `_project_http_sessions_v4_canonical`（fallback 同 projector + fallback=True 恒 degraded）。✅
- 观测口径不一致（Class A 不可表示 503 走 `_aux_unavailable` 未写 degraded503 观测位，DB 路径同场景写）——e1-11 疑问点 12，与 F-022 同族（A13 主辖），非 §13 wire 违约。

### 6.5 §14 expand href

- 契约：12 类目有序清单（traffic.py::EXPAND_CATEGORIES 同源）、fragmentMaxBytes=运行时值、href canonical（v 第一键、directory 客户端追加第二、恰编码一次、v 值=解析后 selector）。
- 实现：messages.py:58-66（`_expand_wire_view`：selector=4 ∧ feature∈SATISFIED→4 else 折回 3）；skeleton.py:167-187（href `?v={wire_view}`，v 唯一 sidecar 侧 key）；versions.py:125-131（capabilities["4"].expand 同源 EXPAND_CATEGORIES + settings.max_expand_response_bytes）。✅

### 6.6 §15 ETag/Vary

- 契约：v4 sessions 列表增 ETag（identity 强/gzip 弱、hash=REP_VERSION+NUL+coding+NUL+canonical identity）、全 v4 路由 Vary: Accept-Encoding 修正、304 头集合=ETag+Vary+no-store、etag off→Vary 仍发、域隔离 wire=v4。
- 实现：sessions.py:598-645 `_v4_json_response`（门控关=4.0.0 逐字保留「无 ETag 摘 Vary」/开=Vary 恒发+compute_etag+conditional_304；etag off 分支 :628-633 Vary/no-store 仍发）；etag.py:91（response_rep_version wire_view）、:101/:171（compute_etag/merged_vary 单值）。✅
- 4.2.0 已修复的「删 Vary bug」当前不在（门控开态路径恒发 Vary）；v3 面恒 Vary 不受影响。

### 6.7 §16 POST 等效族

见 §3 专用核对 + 矩阵 #39-41。三款（a/b/c）逐项与 §16.2 冻结一致：a 逐字节 PATCH 管线（write_groups.py:338-339 仅 method 参数不同）；b DELETE 等效无 ignore-body（:398-414）；c octet 判据+合成体+CT override+求值点（:368-395）。directory 消费 ≡ 等效目标（selector 消费集 pattern `^/slimapi/session/[^/]+$` 对三组合 POST 共用，selector.py:177/183；§5.2 修订二加注成立）。✅

---

## 7. 三清单（核心交付）

### 7.1 v4 已全覆盖（44 项，路由级等价）

矩阵 §2 性质=等价的全部 44 行：sessions/status、session single（v3 面等价保留——v4 为变形增强，见 7.2 注）、context、messages×4（列表/expand×2——行为等价，href 见 7.3-G7）、full、agent、command、file×3、find/file、vcs×3、active、global/health、children、diff、todo、directories、permissions、questions、actions GET、ready、metrics、versions（v3 面等价保留）、actions POST、DELETE/PATCH session、POST session、prompt_async、abort、summarize、fork、permissions/{pid}、question reply/reject、revert×4、command、agent、model。每行双证见矩阵 §2。

**能力级全覆盖声明**：v3 的全部读/写/SSE 基础能力（投影、ETag、envelope、单飞、coalesce、T3 准入、allowlist、gzip 族、错误映射、turn fence、q/p 直推、digest/changed、sticky lastError、token 流 per-sid、动作族）在 `?v=4` 下经「继承 + 修订面」全部可用；修订面（§12-§16）已随 readiness 10/10 全部激活。

### 7.2 有差距（阻碍 v3 消费方迁移；均属**契约已声明**的设计差异——声明不豁免迁移摩擦，逐项标阻塞度）

| # | 差距项 | v3 能力 | v4 现状 | 阻塞度 | 契据 |
|---|---|---|---|---|---|
| G1 | per-directory sessions 列表 | `GET /slimapi/sessions?directory=X`（selector 消费→X-Opencode-Directory→上游 workspace 域列表） | 全局 facade：directory 任何形式→400；替代=客户端对全局列表按 `item.directory` 本地过滤（跨目录翻页/完整性语义弱化；多目录大装机下翻页找单目录成本高） | **中**（多工作目录客户端；单目录装机≈0——目录即全集） | v4C §5.2 行 1 + §0.4（「v3 目录级浏览仅经用户显式触发整体版本重协商…功能降级非等价回退」） |
| G2 | 全局 token 统一流 | `/events?tokens=1` 单连接获得**全部会话** coalesced token 帧 | 400 退役；唯一 token 通道=per-sid `/stream`（须逐 sid 订阅，无单连接全局 token 聚合等价物） | **中**（依赖全局 token 计量的消费方须重构为逐 sid 订阅或放弃；oc-webui/ocdroid 本就分离两连接——影响面有限） | v4C §7.0① 终裁 + §7.3 |
| G3 | 单页 limit>500 | sessions limit 域 ≤1000 | v4 域 1..500（>500→422），须 cursor 翻页拼接 | **低**（机械适配：cursor 循环；注意 degraded 页 nextCursor=null 的终止条件） | v4C §4.1（limit 1..500（v3 保持 1000）） |
| G4 | `roots`/`start` 参数 | 现状语义（roots=根会话过滤；start 透传） | 422 `param_version_mismatch`（v4 收 v3 参数）；替代=parent=none 承接 roots、cursor 承接分页 | **低**（一对一映射明确；start 无精确等价——需核对消费方是否实际使用 start） | v4C §4.1 参数矩阵 |
| G5 | providers 全字段透传 | v3 透传含 `Info.env/key/options/api/capabilities/cost/options/headers/release_date`（F-017：敏感面） | §12 投影丢弃上述字段（安全设计）；依赖 env/options 做配置的消费方在 v4 无等价物（limit 已由修订三恢复） | **中**（仅当消费方读取被投影视弃的字段；oc-webui 已确认嵌套形状） | v4C §12.1 确定性丢弃清单 |

（说明：G1-G5 全部在契约差异列/修订节**明文声明**，属「设计内的迁移代价」而非违约——不存在未声明的 v3→v4 能力缺失；A2 口径 a 迁移 checklist 应逐项消化此表。）

### 7.3 有意不覆盖（设计内；引 non-goals §17 或差异列）

| # | 项 | 出处 |
|---|---|---|
| N1 | cascade 编排层（级联子删除聚合/重试/部分失败可见性）——**永久 non-goal**（delete 沿用上游递归删子+吞错） | v4C §17 修订二收紧 + §16.2-b |
| N2 | cross-session search——**永久 non-goal**（§4.6 维持 per-list 字面子串） | v4C §17 |
| N3 | project status / effectiveStatus / subagentList 聚合字段 | v4C §17 + §13.1（「无 effectiveStatus…」） |
| N4 | 独立 Turn 资源（维持 sessions/status 合并字段现状） | v4C §17 |
| N5 | exact merged（best-effort 语义不变） | v4C §17 + v3C §4a.5 冻结延续 |
| N6 | 512B preview / generic fragment（expand 维持 12 类目冻结清单） | v4C §17 + §14 |
| N7 | messages href 的跨视图统一：v3 请求恒发 `?v=3` href（修订不触碰 v3 字节）；v4 能力探测读 `capabilities["3"].expand`（expand 扩键出现前的注记期口径；当前 `capabilities["4"].expand` 已存在且 iff 成立） | v4C §14 + §3.1 注记/扩键款 |
| N8 | `/slimapi/directories` 不升 DB 投影（保持 /experimental/session 发现形态） | v4C §4.6 末条（范围冻结） |
| N9 | `GET /slimapi/session/{sid}` 不提供「v3 面之上的 v4 形状开关」——v4 形状由 §13 修订整体接管（无双形状协商）；`sessions/status` 恒零分叉 | v4C §10 显式注载两行 |

---

## 8. 期望键集合与矩阵覆盖的机械关系（声明）

- **键→路由归属**：`expected-keys.csv` 580 键的 (method,path) 全集 = census 54 主键全集（脚本复核：unique routes=54，双向相等）。矩阵以 census 54 主键为行（§2），每键经 `(method,path)→行` + `behavior→行内证据列` 完备归属，无孤立键、无无键行。
- **行为类聚合分布**（自 expected-keys.csv 统计，与矩阵对齐）：

| behavior | 键数 | 矩阵对齐说明 |
|---|---|---|
| happy_path | 54 | = 矩阵 54 行 |
| v3_face | 50 | = 54 − versions（union={none} 不生成）− 三条 v4-only POST（#39-41） |
| v4_face | 53 | = 54 − versions（豁免路由无版本面） |
| feature_off | 10 | = census 任一侧 feature_gate≠NONE 的 10 行：providers（providers.redacted.v4）、messages/{sid}+expand×2+full（messages.expand.v4，4 行）、sessions（representation.vary.v4）、session/{sid}（session.single.projection.v4）、POST session/{sid}+archive+delete（session.post-actions.v4，3 行）——5 个 feature ID 覆盖 10 行；其余 5 ID（selector.v4/events.global.replay.v4/events.token.replay.v4/method.boundary.v4/session.list.global.v4）语义在 selector/SSE 管线内实现，无路由级 gate 检查点形态（census 该列为 NONE），其中 method.boundary.v4 消费点在 selector.py:257-262 |
| boundary | 14 | = census 任一侧 has_boundary=YES 的 14 行（providers、events、messages/{sid}、expand×2、full、permissions、questions、sessions、stream、PATCH session、POST actions/{name}、POST session/{sid}、POST archive） |
| error_* | 399（43 个 distinct code） | 逐码挂行：`unsupported_version×53`/`invalid_version_selector×53`（全 53 非 versions 路由）、`upstream_unavailable×46`、`response_too_large×44`、`directory_conflict×31`/`directory_header_retired×31`、`invalid_directory_selector×36`、`request_too_large×21`、`upstream_http_<N>×17`、`transform_busy×12`、`session_not_found×7`、`directory_not_allowed×5`、其余 31 码各 ≤3（含修订码 `provider_projection_limit/provider_upstream_malformed/method_not_applicable×3/thin_route_not_found×3/tokens_stream_retired_in_v4/directory_retired_in_v4/invalid_cursor/param_version_mismatch` 等，均已在矩阵相应行/§3/§5 载双证） |
| **合计** | **580** | 54+50+53+10+14+399=580 ✅ |

- **差异性质 × 键级分布**：`缺失` 键数 = 0（§4：无 contract_only 行，判据不可触达）；其余四性质在键级的表现全部落入对应路由行的 v3_face/v4_face/feature_off/boundary/error_* 键（如 `error_directory_retired_in_v4` 唯一归属 #1 sessions 行、`error_tokens_stream_retired_in_v4` 唯一归属 #34 events 行、`error_method_not_applicable×3`/`error_thin_route_not_found×3` 归属 #39-41 三行）。
- 期望键集合**未重新推导**：全部统计直接读 `expected-keys.csv`（E2 冻结产物）；本报告仅在其实施行为类聚合与路由归属，未增删键。

---

## 9. 复核与限制

- 快照声明：全部证据取自 BASELINE_HEAD=0b836e7 工作树（git status 仅 docs/audits 与 docs/ocmar/plans 未跟踪新增，无 src/tests 改动——快照一致性成立）。
- 本报告为只读审计产物；矩阵「实际行为」格的证据 = census 冻结列 + 本专项实读源码行号（双源交叉，census 列与本报告行号不一致时以本报告实读为准并应在 A14 对账）。
- 限制：SSE 长流行为（重放窗口/屏障边界）证据取自 dataflows 场景 8/9 + replay_wire/replay_log 实读常量与分类函数，未做运行时探针（A6 主辖深度验证）；§4.2 72 格降级矩阵的逐格语义归 A7/D07，本报告只核退役/新增面的契约-实现一致性。
- 关联发现：F-101（versions.py 注释 nine→ten 漂移）、F-102（v4C §3.2 allowlist「v4 新增」表述与 v3C §3a/实现两视图均在冲突）；既有 F-017（providers v3 透传敏感面，支撑 G5）、F-019（actions wire 规范滞留 v2-contract）、F-022（降级 503 观测位，关联 §6.4 注）、F-025（sessions v4 limit 双形状 422 边界，关联 G3）。
