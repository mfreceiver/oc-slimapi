# D06 — A6 SSE 状态机与正确性深审（GlobalHub / TokenStreamHub / Replay 体系）

> Phase 2 专项 A6 产物。快照 `0b836e7`（v4.4.0）。只读审计；证据格式 `文件:行号`（相对仓库根）。
> 输入全文自读：`src/oc_slimapi/sse/{global_hub.py, hub_types.py, registry.py, replay_log.py, replay_wire.py}`、`sse/tokenstream/{hub.py, subscriber.py, frames.py, models.py}`、消费侧 `routes/{events.py, token_stream.py}`；对照 `01-explore/upstream-notes.md`（E8 上游真值）、`01-explore/state-machines.md` 卡 3-9、`parts/e1-{01,05,09,10}.md`、`docs/specs/v3-contract.md §5.7a/§7`、`docs/specs/v4-contract.md §7`。
> 发现：复核 F-001/F-002/F-003/F-011/F-013/F-014/F-015/F-030，新增 **F-216 ~ F-227**（12 条）。

---

## 0. 结论速览

| 维度 | 结论 |
|---|---|
| 三子系统转移表 | GlobalHub 8态×12事件（未定义/可疑转移 5，其中 1 条经契约对照**解除**）；TokenStreamHub PartKey 9态×14事件（未定义/可疑 6，1 条确认为设计+兜底成立）；ReplayLog 分类 7出口×7事件（**零未定义转移**——四级短路序与契约 §7.2 逐条吻合） |
| 泄漏审计 | **未发现可达的任务/状态泄漏**。sse/ 内 7 处 `create_task` 全部有属主与取消路径；断连清理四路（正常/溢出/terminate/关停）汇聚于 unsubscribe 成员守卫闭环。残余风险 = F-222（hub 侧 stop_task 残留 done 引用，测试面）、F-225（watchdog 重建风暴）、F-226（events_tap 清理依赖 generator finally 外部前提）、F-011 降级为防御性缺口（现行代码不可达，见 §5.5） |
| 「上游会发但 sidecar 不处理」 | /global/event 可观察 89 型（88 manifest + 合成 heartbeat），sidecar 有意义处理 13 型，**76 型进 catch-all 静默丢弃且零观测**（F-216）；其中 6 个 q/p 决议事件（permission.replied 等）是唯一有直接功能损失的子集（F-001 P1 维持） |
| F-013（超长 seq 500） | 可达性**证实**（venv Python 3.14.4 实测 `int("9"*5000)` 抛 ValueError；h11 头部上限 64KiB 容得下 >4300 位 seq；`_SEQ_RE` 不限长）→ 定级 P2→**P3**（触发面 = 恶意/损坏客户端，部署面 = loopback + stunnel mTLS 之后） |
| F-014（非 dict JSON 拆连接） | 机制证实（`global_event.get` 先于 `isinstance(payload, dict)` 守卫）但上游恒发对象帧（E8 §3.1）→ 定级 P2→**P3**（放大面记录：单毒帧 = 整条连接拆断 + 全局 resync + barrier） |
| F-015（qp_last_activity 无界） | 证实为 sid 表族中唯一无上界 map；A6 初拟 P3，经 A9 并行复核（F-273 放大器：qp_sweep `_ingest_directory_source` 刷新 seen_at 使 30 天逐出永假，三张镜像表随之有效无界）修正为 **维持 P2**（§3.3 无上界规则） |
| G1 busy 清 sticky「3.3.1 回归」 | **无回归**。`normalize_session_status` 生产调用点全集 = global_hub.py:702（唯一）+ tokenstream/hub.py:1048（幂等二次归一）；:702 归一结果同时喂 digest 填充(:703-704)、G1 busy 清除(:720)、token 镜像(:778-779)；全仓再无裸 `props.get("status") == "busy"` 比较（rg 实证仅存于注释 :718） |
| barrier 水位原子性 | **成立**。`_notify_upstream_loss`（resync_all + write_barrier + token 清态）为无 await 同步块；两路由 classify(T0)→subscribe(T1) 之间无 await（events.py:116→132 / token_stream.py:155→172 全同步）→ 丢失钩子不可能切进分类与入组之间。残余面仅 ReplayIgnoreReset 的客户端自愿倒退（契约义务） |
| epoch 唯一性 | 64-bit 随机 nonce：单对碰撞 2⁻⁶⁴≈5.4e-20；小时级重启连跑 10 年（N≈87,600）生日界期望碰撞 ≈2.1e-10。**量化结论：充分**，残余风险可忽略（无 pid/启动时间复合校验属可接受设计） |

---

## 1. GlobalHub（上游订阅 + digest 策展）

### 1.1 帧分类全集（16 类）vs 上游事件全集（89 型）

sidecar 分类器（global_hub.py:658-975）：

| # | 类别 | 成员 | 上游存在性（E8 §4） |
|---|---|---|---|
| 1-6 | IMMEDIATE（:671-685） | question.asked / question.v2.asked / permission.asked / permission.v2.asked / **permission.resolved / permission.v2.resolved** | 前四 ✓；后二 **幽灵**（上游实为 *.replied，F-001） |
| 7-9 | SESSION_EVENTS（:688-782） | session.status / session.updated / session.deleted | ✓ |
| 10-11 | MESSAGE_EVENTS（:784-800） | message.updated / **message.appended** | 前者 ✓；后者 **死码**（F-002） |
| 12 | G1 | session.error（:806-858） | ✓ |
| 13-16 | token 族（:882-973） | message.part.delta / part.updated / part.removed / message.removed | ✓ |
| — | catch-all（:975 仅注释） | 其余一切 | **76 型真实事件**（见下） |

**「上游会发但 sidecar 不处理」清单**（/global/event 可观察 89 型 − 13 型有意义处理；全部落 :975 静默丢弃，无计数无日志）：

- **q/p 决议族 6**（唯一有直接功能损失的子集，F-001 主辖）：`permission.replied`、`permission.v2.replied`（IMMEDIATE 拼错为 *.resolved 而永失）；`question.replied`、`question.rejected`、`question.v2.replied`、`question.v2.rejected`（从未进入任何集合）
- **会话生命周期 5**：`session.created`（legacy create 恒发布，opencode session.ts:537；设计取舍 = HTTP 冷同步覆盖，E8 假设 #31）、`session.idle`（上游 deprecated）、`session.diff`、`session.compacted`、`server.connected`（sidecar 自产连接本地帧替代）
- **v2 引擎 32**：`session.next.*` 全族（step/text/reasoning/tool/compaction/revert 等）
- **环境/工具 33**：`todo.updated`、`file.edited`、`file.watcher.updated`、`pty.created/updated/exited/deleted`、`command.executed`、`installation.updated/update-available`、`integration.updated/connection.updated`、`models-dev.refreshed`、`catalog.updated`、`project.updated`、`project.directories.updated`、`lsp.updated`、`mcp.tools.changed/browser.open.failed`、`tui.*`×4、`vcs.branch.updated`、`reference.updated`、`plugin.added`、`workspace.*`×3、`worktree.*`×2、`global.disposed`
- **合成 1**：`server.heartbeat`（10s 数据帧；sidecar 丢弃后自产同频心跳，hub 侧 :481-488——设计内）

审计判定：**策展边界本身是设计冻结面**（省流 sidecar 的本意），逐型是否该透传不属缺陷；缺陷 = **丢弃零观测**（:975 无 per-type 计数/日志）+ 上游事件集漂移不可检测（幽灵名 F-001 即此盲区的产物——拼写错误三年不可见）→ F-216（P2）。

### 1.2 F-001 完整影响链（permission.replied 被丢弃）

1. 用户在 opencode 内回答 permission 请求 → 上游发 `permission.replied` / `permission.v2.replied`（v1.18.18 schema/src/v1/permission.ts:61-65、schema/src/permission.ts:43-45，常规高频操作）。
2. sidecar `publish`：不在 IMMEDIATE/SESSION/MESSAGE/token 任一集合 → :975 catch-all 静默丢弃。**digest 不受影响**（该事件不属 session/message 族，pending/sticky 零写入）→ digest 不丢更新、也不补任何更新。
3. **q/p IMMEDIATE 单向失效**：`permission.asked` 直推（:671-685）创建客户端决议 UI，`replied` 永不达 → UI 只能靠超时/轮询关闭。`qp_last_activity` 只被 asked 族刷新（:672-678），replied 不记。
4. 消费方可观察后果：ocdroid 权限弹窗经 SSE 永不收到「已答复」信号；上游实际状态已变（permission merged）而客户端流视图停在被问状态。恢复路径 = digest 触发重拉亦不可得（第 2 点）→ 只剩契约 §7.8 的低频周期 304 对账兜底。
5. 观测盲区：丢弃无计数 → `upstream_events_total` 含被丢帧（:661）但不可分辨，metrics/access log 均不可见。
同类（question.replied/rejected ×2 版本）同链路同后果——F-001 文件已聚焦 permission.*，question 族并入 F-216 清单。

### 1.3 digest 合并 / flush 时序

- 累积：publish 对 SESSION/MESSAGE 族 `pending.setdefault(sid, DigestFields())` 逐字段合并（:692-799）；`_bump_updated_at`（:168-192）per-sid 跨窗严格单调（`max(now, prev+1)`，LRU 10k）。
- 批量 flush：flush_loop 0.25s（:427-430）→ `flush()` 换出整表（:468）逐 sid 发 `session.digest`（`changed` 恒 `[sid]`，:476）。先机会式 prune 四表（:462-465）。
- 立即 flush：`flush_sid`（:432-450）仅 G1-A session.error（:846）与 busy 清 sticky（:723）两条路径。
- sticky 合并：entry 未自设/自清 lastError（`_UNSET`）时贴回 sticky（:443-444/:471-472）；busy → 显式 `lastError:null` 清除帧；deleted → 字段省略。与 v3 §7.5 / v4 §7.5 冻结语义逐条吻合。
- 结构性脆弱点（记录非缺陷）：flush_sid pop 掉 entry 后，同一次 publish 调用内后续分支不得再写 `entry`——当前 busy-clear（:723 后仅 token 镜像分支）与 session.error（:846 后 return）均安全；未来在 flush_sid 之后追加 entry 写即成孤儿写（写进已发出对象）。
- 「token mirror 分支读外层 `status`」（:778）：仅 `event_type=="session.status"` 分支定义（:702），控制流保证已定义；重排即 NameError（无静态保障）——维持 e1-05 疑问 14 记录。

### 1.4 sid 表逐出 × digest 正确性

| 表 | 界 | 逐出后果 | 契约对齐 |
|---|---|---|---|
| `_last_updated_at_by_sid` | LRU 10k（:60,629-642） | 被逐 sid 单调性丢失 → updatedAt 可能回退 | 契约明示非 wire 保证（§7.8「重启即丢、跨进程不可比」同族）✓ |
| `sticky_last_error` | FIFO 10k（:644-649） | 被逐 sid 不再贴回 lastError（wire = 字段消失） | §7.5「逐出后的 sid 不再贴回」**已冻结载明** ✓ |
| `deleted_tombstones` | FIFO 10k（:651-656） | 极高 churn（>10k 删除）后迟到 session.error 可复活已删会话 sticky | 注释 :138-143 论证的已知权衡；resync_all 清空 |
| `_retired_messages` | TTL 24h + FIFO 1000（:601-627） | gate 逐出后迟到 part.updated 可为已删消息重建 token-hub 状态 | 与 token hub `_prune_removed_messages` 语义对齐（:605-610 自述） |
| `qp_last_activity` | **无界**（:109,:678） | 键 = q/p 事件 directory 串，永不删 | **F-015**（P2 维持，A9 主辖 + A6 同意）：sid 表族唯一例外；放大器 F-273（qp_sweep 逐出失效）使三张镜像表有效无界 |

### 1.5 q/p IMMEDIATE 条件与丢失窗口

- **hub 停机窗**（无消费者 → 任务组退场，:995）：期间上游事件完全不入账，q/p 永久丢失；客户端恢复靠重连后的 HTTP 冷同步（无「hub 重启」信号帧——resync_all 只对**在线**订阅者扇出 :988-989）。v4 GLOBAL 域 replay 也无法补（帧从未发布）。
- **上游断连窗**：v4 名义上 GLOBAL 域记录 IMMEDIATE 帧（:584-599），但首次确认丢失即 `write_barrier()`（:413）→ 断连期间离线客户端重连一律 `reconnect_no_replay`（§3.3 短路 ④a）——**跨丢失边界的 q/p 不可补**（by design：sidecar 自己也漏看了上游事件，§7.2 冻结）。仅「客户端自己短暂掉线、上游未断」窗内可补。
- **连接建立→订阅之间**：控制面 classify(T0)→subscribe(T1) 无 await（events.py:116→132），v4 由 replay 覆盖 T0 前窗口；v3 该窗口帧丢失（无重放）——v3 无重放是冻结语义。
- **SSE 组帧 EOF 残留**（:1027-1055）：流 EOF 时无尾空行的最后 data 块丢弃（`data_lines` 残留不 publish）→ **F-217**（P3；EOF 本身走丢失通知 + barrier，放大有限）。

### 1.6 allowlist 丢帧 × digest 完整性（§5.7a）

闸门在 replay 记账**之前**（:585-587 丢 → :592 `_replay_publish`）→ 被拒帧「从未发布」：不耗 seq、不进窗口。组合语义自洽：
- v4 客户端游标序列无空洞（被拒帧不占 seq，窗口连续性不受影响）；
- 被拒 sid 的 digest 对客户端不可见，但内部 sticky/pending/tombstone 照常演化——目录重新入白名单后状态连续；
- 计数仅 `allowlist_dropped_events`（:586），帧内容级不可观测。
- 边角：G1-B 无 sid session.error 帧 directory=None → allowlist 启用时 fail-closed 丢弃（`directory_allowed(allowlist, None)` 非 str → False，:572-582）——无目录全局错误在白名单部署下全灭；与 §5.7a fail-closed 语义一致，记录备查（未单列发现，低频路径）。

### 1.7 GlobalHub 转移表（精化）

| 态 | 事件 | 次态 | 判定 |
|---|---|---|---|
| idle | subscribe/ensure_upstream | running | :194-217（先 cancel 残留 stop_task） |
| running(connecting) | 连接成功 | streaming | :1003-1026（reconnects_total、epoch 重置 :1025） |
| streaming | SSE 行 ×N + 空行 | streaming | :1048-1055 → publish |
| streaming | EOF / 异常 | backoff | :1056-1090（INV-6 notify-once；退避 1→30s） |
| streaming | 成功重连（曾失联未通知） | streaming | :1020-1021 补位通知 |
| backoff | sleep 到 ∧ has_consumers | connecting | :995,1072,1090 |
| backoff | ¬has_consumers | dead | :995 循环退出 → done_callback 取消兄弟 :304-307 |
| running(任意) | 最后消费者离开 | grace-armed | registry maybe_arm :161-185（跨双账本谓词） |
| grace-armed | 30s 到 ∧ 仍无消费者 | 拆除 | registry :258-325（gather 后复查 + 同步清态段） |
| grace-armed | 新消费者 | running | ensure_upstream cancel stop_task :213-215 / registry cancel_pending_removal :146-159 |

**未定义/可疑转移 5**（卡 3 种子复核）：D3-1 双计时器并存（幂等 cancel，低危，维持）；D3-2 barrier 写失败降级（设计声明 best-effort + warning，:411-415，维持）；D3-3 flush/publish 无锁串行前提（成立——单 loop，全同步调用链，维持记录）；D3-4 cap 逐出单调性（契约已载，解除可疑）；**D3-5 第三种 status 形状静默忽略 → 经 v3 §7.6 对照 = 契约冻结行为**（「信封无效时该次状态更新被忽略」），**解除**。净可疑 4，未定义转移（会崩/未处理）**0**。

---

## 2. TokenStreamHub

### 2.1 预算与 flush 窗口

- LIVE 4MiB 全局 + 1MiB per-part + 32 count（`_reserve` :1703-1749 / `_start_part` :1867-1917）；PENDING 4MiB 独立（`_check_pending_budget` :1823-1862）。同 delta 双计两账本（设计，:46-54）。
- 100ms flush tick / 4KiB 早冲（:811-823）/ 60s TTL sweep / 15s heartbeat（:121-123,484-517）。
- **发现 F-219**：`_check_pending_budget` 的 `had_subs` 为**全局**口径（:1853 `subscriber_count > 0`）——sid B 无订阅者触发 pending 溢出、sid A 有订阅者时只 force-flush（B 的 delta 静默丢）不逐出；B 的 LivePart 继续增长仅靠 LIVE 预算兜底，且每条 delta 反复触发全量 flush。docstring（:1848-1849）说的是 "NO subscribers"，实现与文档语义偏差。

### 2.2 tombstone 回放

- v3 握手回放：`attach_subscriber` :1309-1322 对全局 `_removed_messages`（≤1000）按 timestamp 全排序后按 sid 过滤——O(N log N) 且过滤在排序后（成本点，e1-01 疑问 4，维持记录）；TTL 过滤在遍历内（:1320-1321）。
- v4 改由 ReplayLog 承担（`_fanout_message_removed` :1483-1500，FRAME_KIND_TOMBSTONE 占 seq，REPLAY-012）。
- gate/队列耦合：`_prune_removed_messages` 逐出同步 discard `_retired_messages`（:2078-2087）✓；reconnect 时 gate 清空（:2180）而队列保留（:2184-2185）——依赖「新 epoch 无迟到事件」上游前提（GlobalBus 无 replay，前提成立），维持 D6-5 记录。

### 2.3 空闲 / 删除 / 逐出

- idle：`on_session_status` :1019-1070 → `_retire_session` + **无条件写 barrier**（R4）+ 入队 `resync{session_idle}`（v4 走 terminate）。
- deleted：:1072-1132 → retire + barrier + 清 retired + deleted-sid gate + 逐个 `terminate("session_deleted")`（INV-4，不摘 fanout，留给 generator finally）。
- 内存逐出：`_evict_part_for_memory` :1751-1821 → drop → flush_sid（I1 防双发）→ barrier（写于 flush_sid 后，:1801）→ resync → 剩余 LivePart 重快照（skip_key 走 nodrop 保 O1）。
- **发现 F-220**：`ttl_sweep` :1192-1202 退役（idle 会话超 60s 无 delta 的 LivePart）**不写 barrier、不发任何帧**——与上述三处失效源不一致。可达路径：idle 后迟到 text-start 重建 LivePart → 60s 静默退役 → v4 cursor==last 判 up-to-date 挂在已退役 part 上（该 part 后续 delta 全成 orphan 静默丢）。极边缘但与 R4 rationale 相悖。
- **D6-1 复核（busy 会话 LivePart 永不清）**：证实 `ttl_sweep` 只认 `_session_status == "idle"`（:1190-1191）；busy / 未知 / retry（`on_session_status` :1049 只记 busy/idle，retry 不入表）会话的 LivePart 无时间性清理。**但**：LIVE 4MiB + count 32 双帽兜底，逐出路径写 barrier + resync（正确性保持），内存有界 → 判定 = **设计取舍 + 兜底成立，非泄漏**；残余 = busy 长会话占用预算挤占其他会话（最坏 32 part 全属同一 busy 会话）。

### 2.4 events-token 保活双账本

`events_tap`（hub :298 公有 list）+ `events_tokens`（registry set，subscriber.py:582）两容器由 attach/detach 对称维护（:584-623）；`has_consumers`（hub :377-395）统一判活修复 events-only 死循环。**发现 F-226**：清理完全依赖 SSE generator finally（events.py:232-241）——Starlette aclose 语义为外部前提，generator 永不启动则 tap 永挂、flush loop 100ms 空转、grace 永不武装；本仓无兜底超时。另 detach 的 `suppress(ValueError)`（subscriber.py:618-619）掩盖两容器失配。

### 2.5 两处 TODO（:663/:760）——F-030 复核维持 verified

- :663 TODO 后代码取 `part.get("sessionID")/("messageID")/("id")`（:667-669）；:760 TODO 后 `props.get("field")` 等（:761-766）。
- 上游真值（E8 §7 假设 #1/#2，schema/src/v1/session.ts:81-85,:612-620,:632-641）：恒 camelCase → **现码按键正确，TODO 为滞留注释**，无行为风险。附带确认：`field != "text"` 门与现存全部发布者一致（processor.ts 仅 "text"）——未来上游新增字段名会被静默丢（schema 开放 string），契约级观察维持。

### 2.6 TokenStreamHub PartKey 转移表（精化）

| 态 | 事件 | 次态 | 判定 |
|---|---|---|---|
| absent | text-start（非 deleted-sid/retired/disabled/nontext） | live | :663-710（门序 = MAJOR 5：结构检查先于 revision） |
| live | delta（预算过） | live | :795-804（双写 chunks+acc） |
| live | delta 超 per-part 1MiB | disabled | `_truncate_part_for_all` :1731-1733 |
| live | 全局 LIVE 超限（非本 key） | （他 key）disabled+barrier+resync | LRU 逐出永不逐 current key :1738-1748 |
| live | text-end | disabled | finish_part :950-1014（残余 delta 先于 done 标记，同步无 await） |
| live | sid idle / message.removed / session.deleted | retired（三档） | :1019-1132 |
| live | ttl_sweep ∧ sid 已 idle ∧ 60s 无 delta | absent | :1186-1202（**无 barrier——F-220**） |
| 任意 | upstream reconnect | 全清（保留 `_part_revisions`+`_removed_messages`） | :2124-2190 |

**未定义/可疑 6**（卡 6 种子复核）：D6-1 证实但兜底成立（§2.3）；D6-2 → 升格为 **F-223**（revision LRU 逐出回 0：防线 = config.py:142-144 跨模块断言 + LRU 热度，debug override 只校验 live_parts_max 一项 config.py:1033-1038）；D6-3 truncated 保留 LivePart（设计已接受，维持）；D6-4 had_subs 采样（同步前提成立——但全局口径本身是 F-219）；D6-5 gate/队列解耦（前提成立，维持）；D6-6 events_tap 不入 replay（L2-A 设计，契约不要求，维持）。

### 2.7 其他新发现（token 域）

- **F-223**：`_next_part_revision` :730-737 FIFO cap=4096 逐出后同 key 从 0 重计 → 严格 `>` 客户端丢后续帧。
- **F-225**：`_on_flush_done` :443-476 异常重建无退避/预算——flush() 确定性异常 + 有消费者 = 10Hz CRITICAL 刷屏 + 无限重建（当前 put() 不抛使不可达，不变量靠约定）。
- **F-227**：`_busy_sids` :283 生产零读者（ttl_sweep 实读 `_session_status` :1190）——死状态 + 每次 status 事件多付一次 prune。

---

## 3. Replay 体系（replay_log + replay_wire + 两路由消费面）

### 3.1 id 语法生成/解析对称性

- 生成：`sse_id_line` :104-113 产 `id: <domain>:<epoch>:<seq>\n`（domain = "g" / "t:<sid>"）。
- 解析：`parse_last_event_id` :126-166——global 恰 3 段 + `g` 首段（:151）；token ≥4 段 + `t` 首段 + `sid=":".join(parts[1:-2])`（:157-159）**rsplit 固定尾部两段 → sid 含任意冒号 round-trip 无歧义**（`t:a:b:<epoch>:5` ↔ sid="a:b"）。
- 单向宽容（方向安全）：解析容忍 seq 前导零（:83,:166）与 cursor=0，生成端永不产（seq≥1 无前导零）——不构成 round-trip 冲突。
- 拒绝面完备：大写 hex、非数字 seq、段数错、错 label、跨端点（`t:` 到 /events :149-152）、跨 sid（:161-162）全 → None（ignore+reset）。
- **对称性结论：成立**。唯 F-013 破口（§3.6）。

### 3.2 四级重连分类短路序 vs 契约 §7.2

实现（replay() :399-468 + parse :126-166）：①语法 → ②端点/sid → ③epoch（:423-425，dominates）→ ④a barrier（:434-436，`<=` 含水位帧本身，rev-5 勘误）→ ④b future（:440-442）→ ④c 窗口空/过期（:448-458；`after_seq==last` = up_to_date 空帧）→ ④d 连续性防御（:459-466，head-only 不变式下不可达，fail as replay_gap）→ ④e Frames。

两处顺序自洽验证：barrier 先于 future——watermark ≤ last 恒成立（write_barrier 取 last_seq :491 且 last 只增）→ `after_seq ≤ watermark ≤ last` 必非 future ✓；expired 先于 gap，gap 靠 head-only 不变式兜底 ✓。**与 v4 §7.2「严格短路序」冻结文本逐条吻合；route 层直 yield 的 resync reason 恒来自 log 层封闭四值 + v3 fallback 亦为 reconnect_no_replay（events.py:196-126/token_stream.py:164-167），无未门控第五 reason 上线面**（值域封闭性靠字面量纪律，维持 e1-09 疑问 8 记录）。

### 3.3 barrier 水位原子性（上游断连瞬间并发重连竞态——代码路径推演）

单 loop 无锁模型下原子块边界 = await 点。逐路径推演：

1. `_notify_upstream_loss`（global_hub.py:383-417）= resync_all + write_barrier(None) + token 清态，**全程无 await**（同步函数，run() 任务内一次执行完毕）。
2. 路由侧 classify(T0) 与 subscribe(T1) 之间**无 await**（events.py:116→132、token_stream.py:155→172 均为同步调用序列）→ 丢失钩子不可能切进「分类已读日志、订阅未入组」之间。
3. 因此枚举完全序：(a) 钩子整体先于 classify → barrier 已写，④a 正确拦截；(b) 钩子整体后于 subscribe → 客户端已在 `hub.subscribers`/`_subs_by_sid`，resync_all/terminate 帧入队送达；(c) 中间态不存在。
4. meta seqBase 与 replay plan 同在 handler 冻结（events.py:142-155 注释自证）→ meta→replay(≤last@T0)→queue(>T0) 严格递增无重无漏。
5. 残余面：ReplayIgnoreReset（future cursor）依赖客户端主动倒退对齐（契约义务，服务端无强制）；token 域 barrier 写点（idle/evict/delete）与重连分类同为同步块，同序成立。

**结论：barrier 水位原子性成立，无可利用竞态窗口。**

### 3.4 环形覆盖正确性 × 在途重连

- 三界独立：count 2048/域（:568-570）、bytes 64MiB 全局（:572-592，跨域最旧逐出、单帧超预算仍保留）、TTL 900s（:559-566，严格大于才逐）。
- **在途重连安全**：`replay()` :444 `tuple(...)` 即时快照 + frozen dataclass + payload 引用保活 → 已返回 entries 不受此后 popleft 影响，客户端可安全逐帧 yield 已被覆盖帧 ✓。
- barrier 为元数据免逐出（:472-493）；sweep GC 条件 `entries[0].seq > watermark+1`（:536-538，R5 off-by-one 修正）——窗口头恰为 W+1 时 barrier 必须在位（cursor=W 会得到「正常窗口回放」假象），修正正确。
- 观察维持（不立发现）：count/bytes 驱逐推进窗口后、下次 sweep 前，cursor≤W 的 reason 可能从 reconnect_no_replay 漂移为 replay_expired——两码客户端动作等价（HTTP 全量对齐），barrier GC 后规则自洽；`_evict_for_bytes` O(域数×驱逐数) 复杂度、`write_barrier(指定域不存在)` 静默 no-op（:488-489）、close() 后 replay() 空域语义（append fail-loud 不对称）——均为记录级。

### 3.5 epoch 唯一性量化

64-bit `secrets.token_hex(8)`（:101-108）。单对碰撞概率 2⁻⁶⁴ ≈ 5.4e-20；N 次重启生日界期望碰撞 ≈ N²/2¹·²⁻⁶⁴：
- 每天重启跑 10 年（N=3,650）≈ 6.7e-14；
- 每小时重启跑 10 年（N=87,600）≈ 2.1e-10。
**结论：充分**。碰撞后果（旧 cursor 落新进程：future→静默首连 / 窗内→跨代际补发）为残余风险，无 pid/启动时间复合校验——设计接受（§7.1 frozen），维持记录。

### 3.6 F-013 复核（超长 seq int() 500）

- 可达性**实证**：`_SEQ_RE = ^[0-9]+$`（replay_wire.py:83）不限长；venv Python 3.14.4 实测 `int("9"*5000)` → `ValueError: Exceeds the limit (4300 digits)`（`sys.get_int_max_str_digits()=4300`）；:166 `int(seq_text)` 无 try；classify_reconnect（:209）与路由（events.py:116 / token_stream.py:155）均无 ValueError 分支 → FastAPI 500。
- 契约偏离：§7.2 ①「格式非法 → 忽略 + 重置」——语法合法但资源超限的头不应产生 5xx。
- 定级：P2 → **P3**。触发面 = 恶意/损坏客户端手工构造 >4300 位 seq（h11 头部 64KiB 上限容得下）；部署面 = loopback + stunnel mTLS 之后（已认证消费方）；影响 = 单请求 500 + 错误日志噪音，无状态腐蚀。

### 3.7 「窗口内无空洞补发」×「背压溢出帧仍入日志」组合语义

- 溢出帧入日志：`_replay_publish`（global_hub.py:547-570）与 `_replay_publish_token`（tokenstream/hub.py:1371-1394）均在 no-subscriber 早退**之前** append（published 语义，REPLAY-007）→ 背压断连/离线期发布的帧占 seq、可补发。
- 组合闭环：v4 溢出 = silent STOP（hub_types.py:316-320 / subscriber.py:449-452）→ 唯一恢复 = Last-Event-ID 重连 → 窗口判定。若溢出期间该 sid 恰逢 idle/evict/deleted（barrier 落位）→ ④a 先于窗口判定 → `reconnect_no_replay`——**屏障优先于补发，语义一致闭环** ✓（与 v4 §7.2「背压」条冻结文本吻合）。
- 允许列表外的 v3：溢出走 resync{subscriber_backpressure}+STOP（冻结），无重放（v3 语义）。

### 3.8 ReplayLog 转移表（分类决策表）

| 序 | 条件 | 出口 | 契约 §7.2 对照 |
|---|---|---|---|
| — | 无头 / ①②违规 | None（首连语义） | ✓「忽略 + 重置」 |
| ③ | epoch ≠ 当前 | Resync{epoch_changed} | ✓ dominates |
| ④a | seq ≤ watermark | Resync{reconnect_no_replay} | ✓（含水位帧） |
| ④b | seq > last（含未建域） | IgnoreReset | ✓ future |
| ④c | 窗口空 ∧ seq==last | Frames(()) | ✓ up-to-date 非 resync |
| ④d | 窗口空 ∧ seq<last / entries[0].seq≠seq+1 | Resync{replay_expired} | ✓ |
| ④e | 窗口内空洞（防御不可达） | Resync{replay_gap} | ✓ fail-loud |
| — | 否则 | Frames(entries) | ✓ 严格递增连续 |

**未定义转移 0**；种子 D9-1（reason 漂移，自洽）、D9-2（跨域 bytes 侵蚀，设计允许）、D9-3（barrier 后建域，安全——新域无 pre-loss 帧）、D9-4（outcome 键名无冻结，metrics 观察级）全部维持记录级。

---

## 4. 背压与公平

### 4.1 Subscriber 队列上限与溢出恢复

| 维度 | 控制面 Subscriber（hub_types.py:264-325） | TokenSubscriber（subscriber.py:362-458） |
|---|---|---|
| 上限 | 256 项 / 2MiB / 帧上限 256KiB | runtime 64 项 / 512KiB / 帧上限 1MiB；handshake 独立 2048 项 / 8MiB（fail-on-overflow → 503） |
| 超大帧 | drop + 计数，不断连（:287-289） | 同（:397-406） |
| 溢出 | 立即断连：清队 + v3 `resync{subscriber_backpressure}`+STOP / v4 仅 STOP（:304-325） | 清 **runtime** 队（handshake 存活，CRITICAL 3）+ 同 v3/v4 分叉（:439-458） |
| 恢复 | v3 = 重连收 resync 后 HTTP 对齐；v4 = Last-Event-ID 重连 → replay/屏障 | 同左；`session.deleted` → terminate（resync→STOP，v4 域外 reason 静默） |

两 hub 一致性：v4 STOP-only 门控（`reason not in V4_RESYNC_REASONS`）在两处对称（hub_types.py:317 / subscriber.py:450,489）；溢出帧均先入 replay 日志（§3.7）。**v4 客户端断连不可分辨原因**（backpressure/deleted/idle 均 STOP-only）——设计以「断连即信号 + 重连分类」闭环，排障仅剩 metrics（e1-10 疑问 3 维持）。

### 4.2 慢客户端头部阻塞

- 生产侧全 `put_nowait` / 同步 put：flush loop 永不因单慢客户端阻塞（返回 bool 被忽略，F-218 记录计量偏差）。
- 每 sub 独立队列：单慢 sub 只影响自己；帧序公平由 flush 的 sorted-key 遍历保证（hub.py:542）。
- 消费侧：heartbeat 10s（控制面）/15s（token）保证 `queue.get()` 不永久饿死；握手帧先排干（`_SubscriberQueue.get` :219-225）。
- ack 配对：两路由均 get→(STOP break)→ack→yield 串行（events.py:215-231 / token_stream.py:294-302），yield 抛出（客户端断）时 ack 已完成 → 字节账不漂高 ✓。`last_get_handshake` 单槽前提成立（e1-10 疑问 7 维持「脆弱但当前正确」）。

---

## 5. 泄漏审计

### 5.1 create_task/ensure_future 全清点（sse/ + 消费路由）

| # | 位置 | task | 属主/引用 | 取消路径 | 异常吞噬判定 |
|---|---|---|---|---|---|
| 1-3 | global_hub.py:238-240 | run/flush/heartbeat 组 | self.task/flush_task/heartbeat_task + done_callback 闭包 | stop_after_grace / registry grace / close() | run() 内部全捕获（:1075-1090）不死；flush/heartbeat 死 → supervisor（:287-327）cancel 兄弟 + 有消费者则重建（warning 日志含 exc）→ 无吞 |
| 4 | global_hub.py:360 | hub 侧 stop_after_grace | self.stop_task | ensure_upstream:213-215 / 触发即尽 | sleep-only，无异常面；**F-222**：触发后残留 done 引用 |
| 5 | registry.py:185 | _remove_hub_after_grace | self._removal_task | cancel_pending_removal:157-159 / close:400-402（两者均先置 None） | 见 §5.5（F-011 复核） |
| 6 | tokenstream/hub.py:439 | flush_loop | self._flush_task + watchdog | stop():478-482 | 异常死 → watchdog CRITICAL + 重建（:443-476）；**F-225** 无退避 |

范围外（各自文件自管，A6 不辖）：traffic_snapshot/actions/qp_sweep/app/dbaux。

### 5.2 断连清理全路径

1. **正常断**（客户端断 → ASGI send 抛/Cancelled）：generator finally →（token 路由）registry.unsubscribe（成员守卫真幂等 :821-822）/（events 路由）先 detach_events_subscriber 再 hubs.unsubscribe（events.py:232-241）→ 双账本空 → flush stop + grace arm → 30s 后拆 hub。闭环。
2. **溢出断**：put 溢出 → resync+STOP 入队 → generator 取 STOP break → 同上 finally。v4 STOP-only 丢帧风险由 replay 承担（§3.7）。闭环。
3. **terminate**（session.deleted）：resync→STOP → generator break → finally → unsubscribe。闭环（不摘 fanout 的设计依赖 finally 必达——Starlette aclose 外部前提）。
4. **关停**：app LIFO token_hub.stop → hubs.close（registry.py:389-408 cancel 全部 + gather + 置空）。闭环。
5. **上游断连**：不拆连接，退避重连 + INV-6 一次通知；订阅者收 resync 续挂。无清理需求。
6. **失败 attach 回滚**：`_rollback_failed_attach`（subscriber.py:746-787）对称回收 grace/flush/fanout。闭环（CancelledError 路径依赖「无 await」前提，e1-10 疑问 5 维持）。

### 5.3 引用持有判定

- done_callback 闭包持有组内 task 引用 → 组死前不被 GC（supervisor 必达）✓；陈旧组靠 `self.task is run_task` 判废（:297）✓。
- 订阅者对象：hub.subscribers / _subs_by_sid / events_tokens / events_tap（bound method）强引用，全部由 finally 路径回收；无弱引用/定时器残留。
- ReplayLog 域壳永不删除（replay_log.py:253-257）——每壳几个 int，per-epoch sid 基数小，GC = 进程重启。非泄漏。

### 5.4 状态容器有界性

全部有界除 `qp_last_activity`（F-015，P3）。token hub 15 个容器全有界（cap/TTL 枚举见卡 6）；GlobalHub 5 表有界 + 1 无界。

### 5.5 F-011 复核（grace task 残留 → arming 永久失效）

逐路径核验 `_remove_hub_after_grace`（registry.py:258-325）：
- 全部 4 个完成出口（正常拆解 :324-325、两次复查放弃 :292/:315、gather 取消返回 :306-311）中，前三个**均置 `_removal_task=None`**；gather-取消出口依赖 canceller 置 None——而仓内仅有的两个 canceller（cancel_pending_removal :157-159、close :400-402）都先置 None 再 cancel ✓。
- 拆解体同步段（:322-325 `on_upstream_reconnect` + 置空）：`has_consumers()` 为 False（:314 刚查，同步段无 await 不可插队）→ `_subs_by_sid` 全空 → `_fanout_resync` 空转 no-op；其余为 dict clear / 属性写——**现行代码无可达异常**。
- 结论：F-011 所述「task 失败 → `_removal_task` 残留 → maybe_arm 永久失效」为**防御性缺口**（无 `except Exception` 兜底，未来同步段引入抛错路径即成真），现行快照不可达。**定级 P2 → P3**，建议补 try/except 包裹拆解体。

### 5.6 泄漏结论

**现行代码无可达泄漏**。四条残余风险按影响排序：F-226（events_tap 依赖外部前提，最值得加兜底）> F-225（重建风暴）> F-222（hub 侧测试面 grace 失效）> F-011（防御性）。

---

## 6. 发现汇总

### 6.1 主辖复核（02-findings/ 已更新）

| 编号 | 复核结论 | 定级变化 |
|---|---|---|
| F-001 | 维持 verified；影响链补全（§1.2：digest 不丢更新但不补、q/p 单向失效、消费方 UI 停留被问态、零观测） | P1 维持 |
| F-002 | 维持 verified（:88-90 死码；上游全集负向搜索已闭合） | P3 维持 |
| F-003 | 升 verified：str 分支生产不可达（上游恒对象信封，E8 §3.4）；无害防御；附带 digest.status 值域可冻结为 idle/retry/busy 三值 | P3 维持 |
| F-013 | 可达性实证（§3.6）；契约 ①「忽略+重置」偏离确认 | P2 → **P3** |
| F-014 | 机制证实（:662 `.get` 先于 :665 守卫）；上游恒对象帧 → 低可达 | P2 → **P3** |
| F-015 | 证实唯一无界 map；A6 初拟 P3，经 A9 并行复核（F-273 放大器）修正 | **维持 P2**（A9 主辖终判，A6 同意） |
| F-030 | 维持 verified（§2.5；TODO 滞留、按键正确） | P3 维持 |
| F-011（A5 主辖，A6 复核） | 现行不可达，防御性缺口（§5.5） | 建议 P2 → P3 |

### 6.2 新增（F-216 ~ F-227）

| 编号 | 严重度 | 类别 | 一句话 | 锚点 |
|---|---|---|---|---|
| F-216 | P2 | defect/observability | catch-all 丢弃零观测：76 型上游真实事件静默丢弃无 per-type 计数/日志，事件集漂移不可检测（含 question 决议族 4 型、session.created） | global_hub.py:975 |
| F-217 | P3 | defect | 上游 SSE 流 EOF 时无尾空行的最后 data 块静默丢失（data_lines 残留不 publish） | global_hub.py:1027-1055 |
| F-218 | P3 | observability | emitted_frames_total 计投递尝试非成功投递（closed/溢出/心跳计入；put() bool 被忽略） | global_hub.py:486-488,593-599,988-991 |
| F-219 | P3 | defect | _check_pending_budget had_subs 全局口径 vs docstring「NO subscribers」——无订阅 sid 溢出仅 force-flush 静默丢帧不逐出 | tokenstream/hub.py:1848-1862 |
| F-220 | P3 | defect | ttl_sweep 退役不写 replay barrier 不发帧——与 idle/evict/delete 失效源不一致；迟到 text-start 场景 v4 cursor 挂死 part | tokenstream/hub.py:1192-1202 |
| F-221 | P3 | risk | 上游读超时 read=None：半开连接不可检测，恢复仅靠 EOF/异常（心跳是下游方向） | global_hub.py:1002 |
| F-222 | P3 | defect | hub 侧 stop_task 触发后残留 done 引用 → unsubscribe 的 not stop_task 守卫使二次 grace 永不武装（测试/直连面） | global_hub.py:359-360,419-425 |
| F-223 | P3 | risk | _part_revisions LRU cap 逐出后 revision 回 0（严格 > 客户端丢帧）；防线为跨模块隐式断言，debug override 只校验一项 | tokenstream/hub.py:730-737; config.py:142-144,1033-1038 |
| F-224 | P3 | quality | 双哨兵/双实现族（STOP×2、sse_frame×2、_now_ms×2）无一致性防护——跨体系误用即 TypeError / 字节漂移 | hub_types.py:30,105-107; tokenstream/frames.py:19,25-36 |
| F-225 | P3 | risk | token flush watchdog 重建无退避/预算——确定性异常 + 有消费者 = 10Hz CRITICAL 无限重建 | tokenstream/hub.py:443-476 |
| F-226 | P3 | risk | events_tap/events_tokens 双账本清理全靠 generator finally（Starlette aclose 外部前提），无兜底——失守即 flush 永转 + grace 永不挂 | subscriber.py:584-623; routes/events.py:232-241 |
| F-227 | P3 | quality | _busy_sids 生产零读者死状态（ttl_sweep 实读 _session_status） | tokenstream/hub.py:283,1056-1058,1190 |

### 6.3 记录级观察（不立发现）

- barrier GC reason 漂移（reconnect_no_replay ↔ replay_expired，动作等价）；close() 后 replay() 空域语义与 append fail-loud 不对称；`_evict_for_bytes` O(N²)；`write_barrier(不存在域)` 静默 no-op；recycle_domain 生产 wiring 下近 no-op + last_touch 死状态；G1-B 无 sid 错误帧在 allowlist 下 fail-closed 全丢；IMMEDIATE 帧 directory=None 键保留（畸形上游才有）；flush_sid 后 entry 孤儿写脆弱性；registry 两处 4-task 清单硬编码；`retry` status 不入 `_session_status`（D6-1 附带）；meta/seqBase 冻结时序依赖 handler 无 await（已验证成立）。

---

## 7. 方法与限制

- 全文精读 9 个 sse 源文件 + 2 个消费路由；上游对照全部引 E8 已冻结的直读结论（opencode-src v1.18.18）。
- 并发正确性分析基于 asyncio 单 loop 串行化前提（各模块 docstring 自证 + await 点逐一核验）；未做运行时验证（只读纪律）。
- F-013 的 int 限制行为在审计 venv（Python 3.14.4）实测确认；h11/uvicorn 头部上限为已知默认值推演，未起服务实测。
