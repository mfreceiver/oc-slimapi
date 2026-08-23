# v4 原生化全局重构实施方案（门控修订版）

> 日期：2026-08-23
> 状态：**方案门控 PASS；尚未开始实现**
> 实施会话上限：**3 个 fixer-sgpt 会话，可暂停后复用，不得创建第 4 个**
> 验证 owner：**主编排者**
> 允许目标：仅做 v4 原生化删除与就地简化，保持等效能力；**不创建 v5，不借机增加优化特性**

---

## 0. 门控结论

原草案把“删除已不可达的旧协议”与三项独立架构项目混在同一批次：

1. 新建 `RuntimeServices` 并搬迁 lifespan 所有权；
2. 新建全路由 `EndpointPolicy` 注册表；
3. 新建 messages pipeline，并同时改变 raw singleflight key、给 directories discovery 增加合流。

这些项目会改变资源所有权、流量分类或并发/背压行为，不能作为 wire 等效重构夹带实施。本修订版将它们全部移出本批次，只保留可由现有 v4 契约和测试直接证明的删除式简化。

### 0.1 分项评分

| 门控项 | 权重 | 得分 | 结论 |
|---|---:|---:|---|
| v4 wire 等效性与契约边界 | 2.0 | 1.95 | 保留全部现行可观察形状；唯一契约编辑是纠正 token stream directory 错误码描述，使文档符合既有生产行为 |
| 旧协议删除边界 | 1.5 | 1.5 | 精确区分 Slimapi 旧 wire、上游固有版本名、历史档案、运维兼容 schema |
| 三会话写域与依赖顺序 | 1.0 | 1.0 | 采用 A→(B∥C)→A 的受控调度；并发阶段文件零重叠 |
| global/token SSE 不变量 | 1.5 | 1.45 | replay/meta/resync/epoch/turn/partEventRevision、barrier、失败路径均有显式锁定；执行期仍需主编排者核对大体量 lifecycle 测试迁移 |
| 架构贴合与不过度设计 | 1.5 | 1.5 | 本批次明确不引入 RuntimeServices、EndpointPolicy、messages pipeline |
| 配置迁移/SQLite/资源所有权 | 1.0 | 1.0 | 配置与状态迁移保留；SQLite 只读；AsyncExitStack 清理顺序不动 |
| 可观察验收与命令真实性 | 1.0 | 0.95 | 所列现有文件/测试已核对存在；新增测试路径和失败条件明确；静态扫描结果仍需人工分类 |
| 真正简化/YAGNI | 0.5 | 0.5 | 删除参数、分支、帧构造器和过时测试，不新增抽象层 |
| **总分** | **10.0** | **9.85** | **PASS，无 Blocker** |

开工条件：主编排者确认工作区基线后，按本方案顺序执行；任一会话不得自行扩大写域。

### 0.2 剩余非阻塞风险及既定闭环

- **构造签名传播面较广**：ReplayLog 改为必需依赖会触及多类测试 app/hub builder；本方案已把所有当前 `GlobalHub(`/`TokenStreamHub(`/`HubRegistry(` 直接构造文件分配到 A/B/C 写域，并在 Task 4 增设专门回归命令，禁止以恢复 optional replay 降低迁移量。
- **同文件混有上游与旧 wire 同名事件**：例如测试可构造上游 `server.connected` 输入以验证 dropped-event 行为；结构测试和人工扫描必须区分“上游输入/负面断言”与“sidecar 向客户端构造发送”，不得按字符串批量删除。
- **token lifecycle 测试体量大**：handshake/private cache 删除会波及 flush、overflow、reconnect 叙事；B 只改 token 域，A 在 B 停写后负责共享 lifecycle/replay 复核，任何 revision/seq/barrier 差异均按 Blocker 回退。

---

## 1. 目标、非目标与等效边界

### 1.1 本批次目标

将已经公开 v4-only 的 HTTP 服务收敛为 v4 原生实现：

- global SSE 删除 v3 subscriber/欢迎帧和“未配置 ReplayLog”导致的常态 id-less 业务帧分支；保留 replay append 失败时既有 id-less 降级；
- token SSE 删除 v3 handshake、snapshot/truncated wire、`wire_v4` 分支；
- selector、消息投影和 route helper 删除 v3 命名、默认值与 selector-less 旧目录请求头适配；
- 删除只为上述旧分支服务的测试夹具，改为生产装配和 v4 不变量测试；
- 清理现行文档中的 v2/v3/catch-all 叙事，保留明确标记的历史文档和运维兼容说明。

### 1.2 严格保持的现行行为

以下行为在本批次不得改变：

- `/slimapi/**` 只接受一个词法合法的 `?v=4`；旧 `X-Slimapi-Version` 不参与协商；
- `/slimapi/versions`、health/ready 的当前字段和值；
- `/event`、`/global/event`、`/session/**` 等旧路径终止为本地 404/405，不产生 upstream I/O；
- global/token SSE 的 meta、replay、resync、heartbeat、业务帧字节与顺序；
- global replay append 失败时既有“告警并降级为 id-less fanout”语义；
- token replay append 失败时既有“回滚 seq、丢帧、计数；内存 resync 失败时 fail-closed”语义；
- `turnIncarnation`、`turn`、`partEventRevision`、payload `seq`、SSE id 的现有含义；
- `mode=full` 和未知 `mode` 仍按 baseline skeleton 处理；raw-flight key 仍包含 mode；
- health 中 `server.*`、`schema.*`、`features.tokenCoalesce=true`；
- read passthrough 的历史 `wire_view=3` ETag representation label；
- `metrics.traffic.v3`、traffic snapshot `v3`、v2/v3/v4 维度和旧日志解释能力；
- `OC_SLIMAPI_SERVER_API_VERSION`、`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS`、`OC_SLIMAPI_ACCESS_LOG_PATH` 的现有弃用/迁移行为；
- access log 旧文件迁移、turn registry 新旧 incarnation 文件 high-water 迁移；
- SQLite 投影仍为 `mode=ro`，不得增加 DDL/DML/写 PRAGMA；
- `AsyncExitStack` 是唯一生命周期清理 owner，现有后台任务停止顺序不变。

### 1.3 不得误删

- 上游固有名称：`question.v2.*`、`permission.v2.*`、`message-v2`、`PermissionV1`、`LegacyContent`；
- 历史档案：`docs/specs/v2-contract.md`、`docs/specs/v3-contract.md`；
- 内部格式版本：ETag/fingerprint/since-token/provider projection/golden fixture 的 `v1`/`v2` 名称；
- 当前 resilience：DB/native fallback、replay 降级、上游字符串/对象 status 归一化；
- 运维兼容 schema 和迁移代码，除非另开带迁移窗口的方案。
- `AGENTS.md` 作为契约、SQLite 写域、质量门禁和发版纪律权威只读引用，本批次不修改。

### 1.4 明确移出本批次

以下项目不创建文件、不改接口、不写占位代码：

- `src/oc_slimapi/runtime.py` / `RuntimeServices`；
- `src/oc_slimapi/endpoint_policy.py` / `EndpointPolicy`；
- `src/oc_slimapi/routes/messages/_pipeline.py`；
- messages raw-flight key 去除 `mode`；
- questions/permissions/directories discovery 接入 raw singleflight；
- health/metrics/ETag/config/state-file 的兼容 schema 迁移；
- `shell_deny_list_enabled`、Python re-export/alias 等与本批次 wire 删除无直接依赖的独立清理。
- 全仓“见 v2/v3 就改名”的注释清洗；上游 taxonomy、运维 schema、历史来源说明继续保留，仅清理本计划写域内把已关闭 catch-all、退休请求头或旧 wire 说成当前路径的文字。

移出原因：这些变更分别影响资源所有权、路径分类、并发合流、运维 schema 或内部包兼容；都需要独立 benchmark/迁移/契约门控，不能以“v4 原生化”名义夹带。

---

## 2. 三会话调度与零冲突写域

三个会话不是同时从头跑到尾。共享工作区中测试文件和被测源码会互相影响，故采用：

```text
Session A：Task 1 基线/锁测试 + shared ReplayLog 构造缝 → 暂停
                         ├─ Session B：Task 2 token SSE
                         └─ Session C：Task 3 selector/projection/routes
Session B/C 停止写入并交接 → Session A：Task 4 global/shared integration → Task 5 docs/final
```

### 2.1 Session A：锁、global、共享集成、文档

独占写域：

- `tests/test_v4_native_runtime.py`（新增）；
- `src/oc_slimapi/sse/registry.py`；
- `src/oc_slimapi/sse/hub_types.py`；
- `src/oc_slimapi/sse/global_hub.py`；
- `src/oc_slimapi/app.py`（仅把已先创建的 `app.state.replay_log` 作为 `HubRegistry` 必需构造参数接入，并清理已删除 snapshot/v3 pipeline 的陈旧注释；不得改 lifespan 顺序或所有权）；
- `src/oc_slimapi/routes/events.py`；
- 以下文件只允许清理误述当前 catch-all、旧请求头或安全边界的注释/docstring，禁止改变去除 docstring 后的 AST：`src/oc_slimapi/actions.py`、`src/oc_slimapi/upstream.py`、`src/oc_slimapi/middleware/request_id.py`、`src/oc_slimapi/traffic.py`、`src/oc_slimapi/routes/actions.py`、`src/oc_slimapi/routes/agent.py`、`src/oc_slimapi/routes/children.py`、`src/oc_slimapi/routes/command.py`、`src/oc_slimapi/routes/diff.py`、`src/oc_slimapi/routes/directories.py`、`src/oc_slimapi/routes/permissions.py`、`src/oc_slimapi/routes/questions.py`、`src/oc_slimapi/routes/todo.py`；
- `tests/test_sse_replay_wire.py`；
- `tests/test_hub.py`；
- `tests/test_hub_behavior_lock.py`；
- `tests/test_globalhub_retired_gate.py`；
- `tests/test_registry_grace_removal.py`；
- `tests/test_idle_recycle_replay_barrier.py`；
- `tests/test_zombie_hub_revival.py`；
- `tests/test_turn_registry.py`；
- `tests/test_digest_revision.py`；
- `tests/test_lifespan.py`；
- `tests/test_batch3_lifecycle.py`；
- `tests/test_session_status_object_format.py`；
- `tests/test_sse_logging.py`；
- `tests/test_sse_meta.py`（由 `tests/test_v3_sse_meta.py` 重命名）；
- `tests/test_b1a_digest_changed.py`；
- `tests/test_b1b_sweep_shadow.py`；
- `tests/test_qp_tables_bounded.py`；
- `tests/test_global_hub_dropped_events.py`；
- `tests/test_message_fingerprint.py`；
- `tests/test_catalog_epoch_invalidation.py`；
- `tests/test_b4_allowlist.py`；
- `tests/test_upstream_error_boundary.py`；
- `tests/test_command_routes.py`；
- `tests/test_dbaux_metrics.py`；
- `tests/test_offload_equivalence.py`；
- `tests/test_metrics_dropped_events.py`；
- `tests/test_traffic_sse.py`；
- `tests/test_metrics_replay_block.py`；
- `tests/test_traffic_upin_gaps.py`；
- `tests/test_traffic_integration.py`；
- `tests/test_sessions_coalesce.py`；
- `tests/test_etag.py`；
- `tests/test_metrics.py`；
- `tests/test_messages_coalesce.py`；
- `tests/test_agent_routes.py`；
- `tests/test_versions_rawbody_regression.py`（由 `tests/test_v3_rawbody_regression.py` 重命名）；
- `tests/test_v4_only_window.py`（由 `tests/test_v4_dual_window.py` 重命名并重写过期双窗口叙事）；
- `tests/test_health_v4_view.py`（由 `tests/test_health_dual_view.py` 重命名）；
- `tests/test_v3_directory.py`（最后改名为 `tests/test_directory_policy.py`）；
- `tests/test_directory_policy.py`；
- `tests/test_refactor_equivalence.py`；
- 本方案 Task 5 列出的文档、脚本提示文本与 deploy 示例注释。

`src/oc_slimapi/sse/registry.py` 同时持有 global/token ledger、grace task、barrier/clear 顺序，必须由 A 单点修改，B 不得写。

### 2.2 Session B：token SSE

独占写域：

- `src/oc_slimapi/sse/tokenstream/**`；
- `src/oc_slimapi/sse/token_hub.py`；
- `src/oc_slimapi/config.py`（只删除 token v3 handshake 队列常量/静态断言；不得触碰 deprecated env/access-log/turn-state 迁移）；
- `src/oc_slimapi/routes/token_stream.py`；
- `tests/test_token_hub.py`；
- `tests/test_token_hub_flush.py`；
- `tests/test_token_hub_lifecycle.py`；
- `tests/test_token_seq.py`；
- `tests/test_token_stream_route.py`；
- `tests/test_events_tokens.py`；
- `tests/test_token_subscriber_overflow.py`；
- `tests/test_resync_reason_gate.py`。

B 不得修改 `sse/registry.py`、`tests/test_sse_replay_wire.py`、`tests/test_v3_directory.py` 或任何文档。需要共享签名配合时，先在交接记录中说明，由 A 串行完成。

### 2.3 Session C：selector、投影、route helper

独占写域：

- `src/oc_slimapi/selector.py`；
- `src/oc_slimapi/skeleton.py`；
- `src/oc_slimapi/routes/messages/_list.py`；
- `src/oc_slimapi/routes/messages/_router.py`；
- `src/oc_slimapi/routes/read_groups.py`；
- `tests/test_selector.py`；
- `tests/test_selector_query_strip.py`；
- `tests/test_expand_href_v4.py`；
- `tests/test_messages_routes.py`；
- `tests/test_children_routes.py`；
- `tests/test_messages_merged.py`；
- `tests/test_envelope.py`（由 `tests/test_v3_envelope.py` 重命名）；
- `tests/test_etag_domain.py`（由 `tests/test_v3_etag_domain.py` 重命名；只改测试名/叙事，不改 ETag 兼容 label）。

C 不得修改 token/global SSE、共享 directory 测试或文档。

### 2.4 并发纪律

- 只有 B 与 C 可并发写入；A 在此阶段必须暂停写入。
- 并发阶段只跑 lane-local pytest；不得并发运行 `./scripts/check.sh`、全量 pytest 或 `compileall`。
- B/C 完成后各自提供：修改文件清单、目标测试结果、未解决共享签名清单。两者停止写入后 A 才恢复。
- 任一发现需要越界修改即停止并交给 A；不得临时扩大写域。

---

## 3. Task 1 — 建立基线与 v4 原生锁（Session A，先执行）

### 3.1 基线检查

主编排者记录：

```bash
git status --short
./scripts/check.sh
```

除本方案文件和主编排者明确接受的既有改动外，存在未知 dirty 文件即停止开工。

### 3.2 新增结构与边界测试

新增 `tests/test_v4_native_runtime.py`，必须包含以下可观察断言：

1. public route 只调用无版本维度的 v4-native global/token subscribe API；
2. global `Subscriber`、token `TokenSubscriber`、两个 subscribe/attach API 不再有 `wire_v4` 参数或字段；
3. 运行时代码不再构造/发送 `server.connected`；
4. token 运行时代码不再构造/发送 `message.part.snapshot` 或 `truncated` 帧；
5. skeleton expand href 只能生成 `?v=4`；
6. current request state key 不含 `v3` 命名；
7. selector-less route helper 不读取 inbound `X-Opencode-Directory`；
8. `/session/abc`、`/event`、`/global/event` 仍为本地终止且 upstream call count 为 0；
9. `?v=3`、缺失 `?v=`、旧 version header 替代 selector 均失败；
10. 上游固有 `question.v2.*` / `permission.v2.*` 字符串仍在允许处理路径中；
11. `metrics.traffic` 和 snapshot 的 `v3` 键、health 兼容字段、旧 ETag label 未被删；
12. config/access-log/turn-state 迁移测试仍可发现对应兼容入口；
13. `HubRegistry.__init__(..., replay_log: ReplayLog)`、`GlobalHub(..., replay_log: ReplayLog)`、`TokenStreamHub(..., replay_log: ReplayLog)` 都把 ReplayLog 设为必需参数；删除 `HubRegistry.set_replay_log()`、`GlobalHub.set_replay_log()` 与 staged/optional 注入状态；production 的 global/token 实例持有同一个 lifespan 创建的 ring，direct tests 显式创建并关闭自己的 ring。

结构测试只检查明确符号/AST/公开结果，不用脆弱的全仓“字符串必须为零”；允许列表必须写在测试中并逐项解释类别。

### 3.3 先锁定 SSE 可观察不变量

在不改生产代码前，补足或确认以下测试：

- global meta 首帧、无 SSE id、字段集合、epoch/seqBase 同一快照；
- global replay/resync 在 live 前，heartbeat/resync 无 id 且不入 replay；
- global business frame 零订阅也入 replay；append 失败保持当前 id-less 降级；
- token meta 首帧、无 SSE id；`subscriberId` 是 JSON 字段，不是 SSE `id:`；
- token replay/resync 在 live 前；payload `seq` 等于 SSE id 尾段；
- token append 失败回滚 seq、无洞、无 id-less 泄漏；
- `token_memory_limit` resync 可 replay；其 publish 失败 fail-closed；
- route-private resync 无 id、不入 log、不耗 seq；
- global/token epoch 变化、expired/gap/no-replay 和 same-epoch continuation；
- idle/deleted barrier 在零订阅时仍写，sticky first-connect invalidation 和 token clear 顺序不变；
- `turnIncarnation`/`turn` 在 ingest 时取快照；同 sid digest 在 `question.asked` 前 flush；
- `partEventRevision` 对实际发布的 v4 delta 严格递增；v4 `message.removed`/resync 的 replay seq 语义不变；不得保留 v3 snapshot/truncated/done marker 的额外 revision 消耗；
- subscribe/attach 失败回滚、unsubscribe 幂等、grace re-arm、zombie revival、关闭顺序不变。

目标命令：

```bash
.venv/bin/pytest -q \
  tests/test_v4_native_runtime.py \
  tests/test_sse_replay_wire.py \
  tests/test_token_seq.py \
  tests/test_idle_recycle_replay_barrier.py \
  tests/test_turn_registry.py \
  tests/test_digest_revision.py
```

预期：既有 wire 锁通过；新增“旧运行时符号已删除”断言在实现前失败。A 继续完成下面唯一的共享前置缝，再暂停。

### 3.4 并发 lane 的唯一共享前置缝

B/C 开始前，A 先完成且只完成以下共享签名迁移：

- `HubRegistry.__init__(..., replay_log: ReplayLog)` 改为必需构造参数，删除 registry 的 staged `set_replay_log()`/`_replay_log=None`；
- `app.py` 用已经创建并登记关闭责任的 `app.state.replay_log` 构造 registry；不得移动 ReplayLog/registry 的创建顺序、sweep task 或 `AsyncExitStack` callback；
- registry 创建 `GlobalHub` 时显式传入该 ring；此时 `GlobalHub` 仍可暂时接受其现有 optional 签名，真正删除 global optional 分支由 Task 4 完成；
- 只适配本 Task 1 命令直接触达的 registry builder。B/C 各自在本 lane 测试中使用新构造参数；其余 A-owned builder 在 Task 4 集中适配。

前置缝完成后运行：

```bash
.venv/bin/pytest -q \
  tests/test_sse_replay_wire.py \
  tests/test_idle_recycle_replay_barrier.py \
  tests/test_lifespan.py
```

允许 `test_v4_native_runtime.py` 的“旧运行时符号仍存在”断言继续保持红色；除此之外，上述前置测试必须通过。A 随后停止写入，B/C 才可开始。该受控中间态不得运行全量 pytest；最终 Task 4/5 必须清零全部红灯。

---

## 4. Task 2 — token SSE 收敛为原生 v4（Session B）

### 4.1 API 与 subscriber 简化

- `TokenStreamRegistry.subscribe(sid)` 删除 `wire_v4` 参数；
- `TokenSubscriber` 删除 `wire_v4`、`_in_handshake`、`_handshake_overflow`、handshake item/byte ledger 与 `begin_handshake()/end_handshake()`；`_SubscriberQueue` 收敛为单一 runtime T3 队列，继续保留 data/control 优先级、STOP-only backpressure、item/byte cap 和 gauge；
- `TokenStreamHub` 的 `replay_log` 改为必需依赖，删除 `None` 分支；生产仍复用 lifespan 创建的 process-wide `ReplayLog`，不得在 hub 内私建 replay ring；
- `TokenStreamHub.attach_subscriber(sid, sub)` 只保留当前 v4 路径：closed check 后直接 attach，无 prefill；
- `routes/token_stream.py` 改用新签名；保留 classify-before-subscribe、meta snapshot、replay/resync/live 顺序；
- 保留 cap check、ensure upstream、cancel grace、start flush、attach、失败 rollback、ledger increment 的现有顺序。

### 4.2 删除 v3 wire，而不删除 v4 状态机

删除：

- `server.connected` builder 和 handshake；
- `config.py` 中只服务 v3 prefill 的 `TOKEN_HANDSHAKE_ITEMS`、`TOKEN_HANDSHAKE_BUFFER_BYTES` 及对应静态断言；
- tombstone 的 connection-private prefill；
- `_removed_messages` connection-private cache、TTL prune 和相关锁内状态；保留 replay-logged `message.removed` business event、retired-message/deleted-sid gate 及其独立 bounds；
- live snapshot prefill；
- `_snapshot_frame`、`_truncated_frame` 及其 re-export；
- `_V4_INELIGIBLE_FRAME_PREFIX`、`_v4_frame_eligible`、`_deliver_v3_only`；
- finish/eviction 时仅供 v3 的 snapshot/done/truncated fanout；
- `wire_v4` 条件下的 id stamping，改为所有 v4 business frame 统一 `id_line + frame`。

保留并直接化：

- per-part/global cap 时的 `drop_part`、disabled 集合和 gauge 变化；
- `truncatedSnapshotsTotal`/`truncated_snapshots_total` 兼容字段保留，但删除所有 v3 truncated 发送增量；新进程内该值保持 0，不为维持旧计数执行旧序列化或消费 revision；
- eviction 顺序：drop → flush 其他 pending → replayable `token_memory_limit` resync → metric；删除其后的 v3 re-snapshot 循环；
- residual delta flush、part retire、replay-logged message removal、retired/deleted TTL 和 revision map 清理；删除 done-marker v3 snapshot 及其额外 revision bump；
- reserve→encode→append→fanout、seq rollback、barrier、sticky invalidation；
- v4 冻结 resync reason 集及 STOP-only backpressure 行为。

删除 snapshot/truncated 后，`partEventRevision` 只由实际发送的 v4 帧消费；不得为了维持历史 v3 计数而制造空洞。

### 4.3 token 测试迁移

- 删除 v3 handshake/snapshot/truncated 的正向断言；
- 将内存预算测试改为断言 state/gauge/resync；`truncatedSnapshotsTotal` 只断言兼容键存在且新进程保持 0，不再断言 v3 per-subscriber 增量；
- `tests/test_token_subscriber_overflow.py` 删除 handshake 双队列/cap 用例，改锁单一 runtime 队列的 item/byte cap、control 优先级、STOP-only backpressure 和 gauge；
- B 在 `tests/test_token_hub.py` 删除 `_removed_messages` 私有 cache 正向断言并改锁 replay-logged removal 与 retired/deleted gate；A 在 B 停写后对 A-owned `tests/test_batch3_lifecycle.py` 做同样迁移；
- 删除 `tests/test_token_seq.py` 中 v3 id-less business-frame 用例；
- 保留并加强 seq/id/replay/fail-closed/barrier/part revision/生命周期用例；
- route 测试不得再构造 `wire_v4=False` fake；默认即 v4；
- 所有直接构造 `TokenStreamHub` 的测试显式注入 `ReplayLog`，不得以测试便利为由恢复 optional replay。
- B 写域内所有直接构造 `GlobalHub`/`HubRegistry` 的测试也显式注入同一临时 `ReplayLog` 并由测试关闭。

目标命令：

```bash
.venv/bin/pytest -q \
  tests/test_token_hub.py \
  tests/test_token_hub_flush.py \
  tests/test_token_hub_lifecycle.py \
  tests/test_token_subscriber_overflow.py \
  tests/test_token_seq.py \
  tests/test_token_stream_route.py \
  tests/test_events_tokens.py \
  tests/test_resync_reason_gate.py \
  tests/test_config.py
```

验收：目标测试全过；`src/oc_slimapi/sse/tokenstream/**`、`src/oc_slimapi/sse/token_hub.py`、`config.py` 的 token 配额段与 `routes/token_stream.py` 中不存在 `wire_v4`、handshake queue、connection-private `_removed_messages`、`server.connected`、`message.part.snapshot` 的运行时构造路径；B 停止写入并交接。

---

## 5. Task 3 — selector、投影与 route helper 原生 v4（Session C，可与 Task 2 并行）

### 5.1 selector 内部命名

- `V3_DIRECTORY_STATE_KEY` 重命名为 `DIRECTORY_STATE_KEY`；state 字符串改为中性内部名；
- `V3_DIRECTORY_INPUT_*` 重命名为 `DIRECTORY_INPUT_*`；
- 保持 production selector 的词法解析、query strip、directory error precedence 和状态写入完全不变。

### 5.2 删除 selector-less 旧请求头适配

- `routes/messages/_router.py` 和 `routes/read_groups.py` 只读取 selector 已归一化的 request state；不再 fallback 到 inbound `X-Opencode-Directory`；
- custom/minimal route 测试改为安装真实 `SlimapiSelectorMiddleware` 或显式注入归一化 state；
- 不改变生产 error body、错误码或 query-only token stream 目录行为；token route 的 helper 由 B 删除，最终矩阵由 A 的共享测试锁定。

### 5.3 投影默认值

- `skeleton.py` 中所有 `wire_view: int = 3` 改为无版本参数的 v4 原生 helper，expand href 始终为 `?v=4`；
- `routes/messages/_list.py` 的纯 helper 删除 `wire_view=3` 默认和 v3 href 分支；
- 保留 `skeleton_message_v2` 等现有内部函数名，避免把上游/内部投影命名清理扩大为跨仓 API 重命名；只修正文档字符串为“current skeleton projection”；
- 不修改 `_read_passthrough.py` 的 ETag `wire_view=3` 兼容 label。

### 5.4 测试修正

- `tests/test_expand_href_v4.py` 删除“默认 v3 href”断言，改为所有入口只产出 `?v=4`；
- selector 测试保留 `?v=3`、旧 header、目录冲突负面边界；
- `tests/test_messages_routes.py` 删除由不完整自建 app 造成的假“已删除”结论：`children`、questions、permissions 和写动作必须通过 production-like router 装配验证为存在；只保留真正退休的 `/full?ids=`、`/since/{cursor}`、`/status`、`/projects`；
- `tests/test_children_routes.py` 删除惯性 `X-Slimapi-Version: 2` fixture，保留旧 children headers/fields 不出现的断言；
- `tests/test_messages_merged.py` 明确保留 `mode=full`/unknown → baseline 等效测试；
- `tests/test_v3_envelope.py` 重命名为 `tests/test_envelope.py`，删除过期 v3 叙事但保留 current envelope shape；
- `tests/test_v3_etag_domain.py` 重命名为 `tests/test_etag_domain.py`，只去除测试命名债务，继续锁定 `_read_passthrough.py` 的旧 ETag 兼容 label。
- C-owned `tests/test_messages_routes.py` 中的 `HubRegistry(...)` builder 使用 Task 1 已落地的必需 ReplayLog 构造参数并由测试关闭；不得恢复 staged setter 或 optional replay。

目标命令：

```bash
.venv/bin/pytest -q \
  tests/test_selector.py \
  tests/test_selector_query_strip.py \
  tests/test_expand_href_v4.py \
  tests/test_messages_routes.py \
  tests/test_children_routes.py \
  tests/test_messages_merged.py \
  tests/test_envelope.py \
  tests/test_etag_domain.py
```

验收：目标测试全过；不存在可执行 v3 href 或 selector-less inbound directory header fallback；生产 selector 的错误矩阵字节不变；C 停止写入并交接。

---

## 6. Task 4 — global SSE 与共享生命周期收敛（Session A，B/C 停止后）

### 6.1 global subscriber 原生 v4

- `HubRegistry.subscribe()` 删除 `wire_v4` 参数；
- `Subscriber` 删除 `wire_v4` 字段；
- 复核 Task 1 已落地的 `HubRegistry` 必需 ReplayLog 构造缝；本任务把 `GlobalHub` 的 `replay_log` 改为必需构造参数，删除 `GlobalHub.set_replay_log()` 与 global `_replay_log=None` 分支；不得改变 lifespan 顺序或私建 ring；
- 删除 global `server.connected` 和 v3 welcome/prefill；
- replay append 成功时，business frame 对所有 subscriber 使用 replay 返回的 id line；append 失败仍保持现有 warning + id-less live 降级，不得伪造 seq 或新建私有 replay ring；
- 删除 v3 truncated marker 分支；保留当前 v4 STOP-only backpressure 结果；
- `routes/events.py` 改用无版本 subscribe API，其他顺序不动。

### 6.2 共享 lifecycle 不变量

修改 `sse/registry.py` 时必须保持：

- unified global/token idle predicate；
- grace task identity guard，旧任务不得删除复活后的 hub；
- token hub stop-and-wait 先于 global hub close；
- idle/deleted 的 replay barrier 在零 subscriber 时仍写；
- barrier 与 token state clear 的现有顺序；
- unsubscribe 幂等、ledger 不重复扣减；
- startup/shutdown 仍由 `app.py` 的 `AsyncExitStack` 驱动，本任务不改 lifespan 所有权。
- `ReplayLog` 继续只在 lifespan 构造一次并由 global/token 共享；测试中的临时 `ReplayLog` 归测试自身，不引入 close task 或新的背景任务 owner。

### 6.3 turn、digest、upstream 名称

- 保持 `turnIncarnation`/`turn` 在 event ingest 时读取并随 pending digest 冻结；
- 保持同 sid pending digest 在 `question.asked` 和 `question.v2.asked` 前 flush；
- `question.v2.*`、`permission.v2.*` 是上游名称，必须继续处理；
- status 字符串/对象归一化继续保留。

### 6.4 共享测试与 directory 契约修正

- `tests/test_sse_replay_wire.py` 删除 v3 subscriber/truncated 正向用例，保留 v4 no-snapshot/no-connected/meta/replay/live 断言；
- 所有 A 写域内直接构造 `GlobalHub`、`TokenStreamHub` 或 `HubRegistry` 的测试显式注入同一临时 `ReplayLog` 并由测试关闭；`tests/test_lifespan.py`/`tests/test_batch3_lifecycle.py` 证明 production wiring 在首次 `get()/subscribe()` 前已由构造参数完成，且 shutdown 顺序不变；
- `tests/test_v3_directory.py` 重命名为 `tests/test_directory_policy.py`，删除 selector-less 旧 helper 正向测试；
- `tests/test_v3_sse_meta.py` 重命名为 `tests/test_sse_meta.py`，保留 v4 meta first/no-id/epoch/seqBase 断言；
- `tests/test_v3_rawbody_regression.py` 重命名为 `tests/test_versions_rawbody_regression.py`；`tests/test_v4_dual_window.py` 重命名为 `tests/test_v4_only_window.py` 并删除双窗口叙事；`tests/test_health_dual_view.py` 重命名为 `tests/test_health_v4_view.py`；这些只清命名/叙事，不减少 v3 rejection 或 health 兼容字段锁；
- 锁定实际 public token-stream directory 矩阵：
  - query-only：允许；
  - header-only：`directory_header_retired`；
  - query+same header：`directory_header_retired`；
  - query+different header：production selector 先返回 `directory_conflict`；
  - repeated distinct query：`invalid_directory_selector`。
- `docs/specs/v4-contract.md` 对 stream dual-different 的 `directory_not_allowed` 描述改为现有生产 `directory_conflict`；这是文档勘误，不改响应。

目标命令：

```bash
.venv/bin/pytest -q \
  tests/test_v4_native_runtime.py \
  tests/test_sse_replay_wire.py \
  tests/test_hub.py \
  tests/test_hub_behavior_lock.py \
  tests/test_globalhub_retired_gate.py \
  tests/test_registry_grace_removal.py \
  tests/test_idle_recycle_replay_barrier.py \
  tests/test_zombie_hub_revival.py \
  tests/test_turn_registry.py \
  tests/test_digest_revision.py \
  tests/test_lifespan.py \
  tests/test_batch3_lifecycle.py \
  tests/test_session_status_object_format.py \
  tests/test_sse_logging.py \
  tests/test_b1a_digest_changed.py \
  tests/test_b1b_sweep_shadow.py \
  tests/test_qp_tables_bounded.py \
  tests/test_global_hub_dropped_events.py \
  tests/test_message_fingerprint.py \
  tests/test_catalog_epoch_invalidation.py \
  tests/test_b4_allowlist.py \
  tests/test_directory_policy.py \
  tests/test_sse_meta.py \
  tests/test_versions_rawbody_regression.py \
  tests/test_v4_only_window.py \
  tests/test_health_v4_view.py \
  tests/test_token_seq.py \
  tests/test_refactor_equivalence.py

# ReplayLog 必需构造参数的跨路由/metrics/traffic 测试适配；这些文件只做
# builder 注入与资源关闭，不借机改各自业务断言。
.venv/bin/pytest -q \
  tests/test_upstream_error_boundary.py \
  tests/test_command_routes.py \
  tests/test_dbaux_metrics.py \
  tests/test_offload_equivalence.py \
  tests/test_metrics_dropped_events.py \
  tests/test_traffic_sse.py \
  tests/test_metrics_replay_block.py \
  tests/test_traffic_upin_gaps.py \
  tests/test_traffic_integration.py \
  tests/test_sessions_coalesce.py \
  tests/test_etag.py \
  tests/test_metrics.py \
  tests/test_messages_coalesce.py \
  tests/test_agent_routes.py
```

验收：所有目标测试通过；global/token API 中无 `wire_v4`；无运行时 `server.connected`/snapshot wire；跨域 barrier、epoch、turn、revision 和关闭路径均通过。

---

## 7. Task 5 — 当前文档、完整门禁与交付（Session A）

### 7.1 文档写域

仅更新以下现行文档；历史 contract 文件除明确链接外不改正文：

- `docs/specs/v4-contract.md`：directory 错误码勘误与修订说明；
- `docs/specs/INTERFACE_MAP.md`：删除 bare array、旧 cursor/complete header、旧 version header、catch-all/v3 authority 混写；
- `docs/specs/CLIENT_CHANGES.md`：当前 v4 checklist 与历史迁移附录分区；
- `docs/specs/design-v2.md`、`docs/specs/design-v4-selector.md`、`docs/specs/design-v4-sse-replay.md`、`docs/specs/design-token-stream.md`：加醒目的 historical/superseded banner，不重写历史设计；
- `docs/release.md`：修正仍要求对照 v3 contract 的旧 checklist；
- `docs/specs/HANDOVER-4.11.0.md`：修正 file/raw 接受 inbound directory header 的错误陈述；
- `docs/manual/traffic-accounting.md`：明确顶层 `v3` 是保留的运维兼容 schema label，不代表仍接受 v3 wire；
- `scripts/check_routes_doc.py`：只修正“可依赖 catch-all”与 v2 contract authority 的失败提示，不改路由 AST 收集/校验逻辑；
- `deploy/actions.manifest.example.toml`：只修正已关闭 catch-all 的误导注释，不改示例数据；
- `CHANGELOG.md`：在 Unreleased 记录“内部旧分支删除、public v4 wire 不变、directory contract 文档勘误”；
- B/C 各自在自己的源码写域内清理被本批次删掉分支的注释；A 只清理 §2.1 点名的 comment/docstring-only 文件，不跨域改 B/C 源码。

`docs/specs/v2-contract.md`、`docs/specs/v3-contract.md` 保持历史档案，不删除、不现代化。

### 7.2 兼容保留回归

完整门禁前显式运行：

```bash
.venv/bin/pytest -q \
  tests/test_config.py \
  tests/test_config_phase_a.py \
  tests/test_access_log.py \
  tests/test_access_log_v3_fields.py \
  tests/test_access_log_toctou.py \
  tests/test_health.py \
  tests/test_health_features.py \
  tests/test_traffic_snapshot.py \
  tests/test_traffic_middleware.py \
  tests/test_turn_registry.py \
  tests/test_dbaux_lifecycle.py \
  tests/test_dbaux_metrics.py \
  tests/test_lifespan.py \
  tests/test_app_assembly.py
```

这些测试证明配置/日志/health/traffic/turn/DB 只读/lifespan 未被“顺手清理”。

### 7.3 最终静态审计

主编排者执行并人工分类结果：

```bash
rg -n 'wire_v4|server\.connected|message\.part\.snapshot|V3_DIRECTORY_STATE_KEY|wire_view\s*[:=].*3' src tests
rg -n 'replay_log\s*:\s*ReplayLog\s*\|\s*None|replay_log\s*=\s*None|set_replay_log' src tests
rg -n 'TOKEN_HANDSHAKE|handshake_(items|buffer)|_in_handshake|begin_handshake|end_handshake|_removed_messages' src tests
test ! -e tests/test_v3_directory.py && test ! -e tests/test_v3_envelope.py && test ! -e tests/test_v3_etag_domain.py && test ! -e tests/test_v3_sse_meta.py && test ! -e tests/test_v3_rawbody_regression.py && test ! -e tests/test_v4_dual_window.py && test ! -e tests/test_health_dual_view.py
rg -n 'X-Slimapi-Version|X-Next-Cursor|X-Complete|X-Children-Version|childrenVersion|childrenIDs|childrenComplete' src tests docs scripts
rg -n 'question\.v2|permission\.v2|message-v2|PermissionV1|LegacyContent|metrics\.traffic.*v3|aggregate_v3_observability' src tests docs
```

验收分类：

- 第一组在运行时代码必须为零；唯一允许的 `wire_view=3` 是 `_read_passthrough.py` 的 ETag 兼容 label，并由 `test_v4_native_runtime.py` 点名锁定；
- replay optional/setter 扫描在运行时代码必须为零；测试也不得通过 `None` 恢复 v3-only no-log pipeline；
- token handshake/private removed-cache 扫描在运行时代码必须为零；测试若提及只能是“符号不存在”的负面结构断言；旧测试文件名扫描必须为零；
- 旧 header/field 扫描只允许负面测试、历史档案或明确“已退休”说明；
- upstream/ops 扫描必须保留上游固有名称与运维兼容 schema，不得因字符串命中删除。

同时执行只读写域检查：

```bash
rg -n '\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE)\b|PRAGMA\s+[^;]*(journal_mode|synchronous|writable_schema)' src/oc_slimapi
```

任何新 SQLite 写路径均为 Blocker。

### 7.4 项目总门禁

```bash
./scripts/check.sh
git diff --check
git status --short
```

`./scripts/check.sh` 必须完成 pytest、route↔INTERFACE_MAP 一致性和 `compileall src`。不得用“lane tests 已通过”替代总门禁。

### 7.5 完成交付条件

全部满足才可报告完成：

- public v4 wire、ETag、health、traffic/config/state migration 与基线等效；
- executable v3 global/token/projection/directory fallback、optional replay、token handshake 双队列和 private removed cache 已删除；
- upstream v2/v1 名称、历史档案、运维兼容 schema 未误删；
- global/token replay/meta/resync/epoch/turn/partEventRevision 全部测试通过；
- `AsyncExitStack` 清理所有权和 SQLite 只读边界未改变；
- 没有新抽象层、没有 raw coalescing/route policy/lifespan 搬迁；
- 所有修改均在本方案列出的写域内；
- 主编排者保留完整命令输出并负责最终验证结论。

---

## 8. 失败与回退规则

- 发现现行 v4 客户端可观察差异：停止该 lane，回退差异，不通过增加兼容分支掩盖；
- 发现旧分支同时服务当前 v4 状态机：只删除旧 wire serialization/delivery，保留当前状态转换和对外指标 schema；不得为维持 v3-only 计数执行旧序列化、发送旧帧或额外消费 revision；
- 发现需要改 ops schema、health 字段、ETag、config/state migration：移出本批次，当前实现不动；
- 发现需要修改另一会话写域：停止并交接给 owner，不跨域写；
- 发现 replay/seq/barrier/cleanup 失败：视为 Blocker，不以文档说明替代修复；
- 总门禁失败：不得 commit/tag/release，不得宣称完成。

---

## 9. 预期净效果

本方案完成后的设计变化应是“删除”，而不是“搬家”：

- 删除两个 `wire_v4` API 维度及其 subscriber 状态；
- 删除 global/token 的旧欢迎帧、token handshake 双队列/private removed cache、snapshot/truncated wire 和 v3 delivery 分支；
- 删除 replay optional/staged 注入，以构造期必需依赖替代；
- 删除 v3 href/default 和 selector-less 请求头适配；
- 删除维护旧正向行为的测试，替换为 v4 不变量和生产装配测试；
- 不新增 runtime 容器、endpoint 注册表或 messages pipeline；
- 不改变并发、背压、资源关闭、SQLite、运维 schema 或公共 wire。

因此本批次可以诚实描述为：**将已经 v4-only 的公共服务实现收敛为 v4-native，同时保留上游兼容、历史档案和运维迁移边界。**
