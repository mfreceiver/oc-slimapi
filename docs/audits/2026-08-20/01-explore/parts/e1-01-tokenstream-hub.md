# E1 精读卡片 — src/oc_slimapi/sse/tokenstream/hub.py

- 文件：`src/oc_slimapi/sse/tokenstream/hub.py`（2190 行，全文精读，无抽样）
- 职责（一句）：**part 生命周期门控的 token 累积器 + 100ms flush 引擎**——把上游 `message.part.updated/delta/removed` 与 `session.status/deleted` 事件累积进 `LivePart`/`DeltaAccumulator` 双账本，按 TICK/字节阈值 flush 成 delta/snapshot/resync/heartbeat 帧 fanout 给 per-session 订阅者与 `/slimapi/events?tokens=1` tap，并执行 LIVE/PENDING 双内存预算、LRU 逐出、tombstone 回放与 v4 ReplayLog 发布。
- 对外符号数：顶层 9 个（1 类 + 8 模块级）；`TokenStreamHub` 类内 59 个方法/属性。
- 疑问点数：24（见文末）。

---

## 1. 对外符号（逐类逐方法完整清单）

### 模块级（8 + logger）

| 符号 | 行号 | 职责 |
|---|---|---|
| `logger` | :111 | 模块 logger（`get_logger(__name__)`），全文件仅 3 个日志点（:469 critical、:1392/:1422 warning） |
| `_SESSION_STATUS_MAX` | :116 | P1-21：`_session_status`/`_busy_sids` 的 FIFO cap（10_000） |
| `_TTL_TICK_INTERVAL` | :121 | flush tick ↔ 60s TTL sweep 换算（import 时计算，floor 1） |
| `_HEARTBEAT_TICK_INTERVAL` | :123 | flush tick ↔ 15s heartbeat 换算（import 时计算，floor 1） |
| `apply_debug_budget_overrides(settings)` | :126-161 | **Debug/联调 break-glass**：用 `OC_SLIMAPI_TOKEN_STREAM_DEBUG_*` env 覆盖模块级全局 `TOKEN_LIVEPARTS_MAX_BYTES`/`TOKEN_PART_MAX_BYTES`/`TOKEN_LIVE_PARTS_MAX`；app lifespan 启动时调用一次（app.py:307） |
| `_V4_INELIGIBLE_FRAME_PREFIX` | :171 | `b"event: message.part.snapshot\n"`——snapshot 族帧前缀（v4 wire 永不发送） |
| `_v4_frame_eligible(frame)` | :174-190 | rev-gate R2 BLOCKER-1：帧是否可进 v4 wire/ReplayLog（snapshot 族 → False） |
| `_events_token_frame(key, text)` | :193-211 | L2-A curated-events 精简 token 帧：`{type:"token", sessionID, messageID, partID, delta}`，无 `event:` 名、无 revision、无 directory |

### class TokenStreamHub（:214）

类 docstring（:215-246）枚举 9 个核心容器。构造参数（:248-253）：`max_frame_bytes=DEFAULT_TOKEN_MAX_FRAME_BYTES`（1 MiB）、`replay_log: ReplayLog | None = None`。

#### 属性 / 只读（7）

| 方法/属性 | 行号 | 职责 |
|---|---|---|
| `subscriber_count` (property) | :360-375 | 所有 sid 的 token 订阅者总数（`sum(len(subs))`）；`GlobalHub.has_consumers` 的存活判据之一 |
| `has_consumers()` | :377-395 | 统一存活谓词：`subscriber_count > 0 or len(events_tap) > 0`（events-token-only 也保活 flush loop） |
| `orphan_deltas` (property) | :397-400 | 累计孤儿 delta 计数（C3） |
| `flushed_frames_total` (property) | :402-404 | 已 fanout 帧计数 |
| `dropped_frames_total` (property) | :406-408 | 丢帧计数（**本文件从不递增**，只有 subscriber.py:405/421/441 递增共享 `_metrics`） |
| `truncated_snapshots_total` (property) | :410-412 | truncated 帧计数 |
| `token_memory_limit_total` (property) | :414-416 | `resync{token_memory_limit}` 次数 |

#### 后台 flush 生命周期（5）

| 方法 | 行号 | 职责 |
|---|---|---|
| `start()` | :421-441 | 幂等启动 `flush_loop` task + 挂 `_on_flush_done` watchdog |
| `_on_flush_done(task)` | :443-476 | INV-1 watchdog：exception 死亡且有消费者 → CRITICAL 日志 + `start()` 重建；cancelled/normal/stale-task → no-op |
| `stop()` | :478-482 | 幂等 cancel flush task |
| `flush_loop()` | :484-517 | `while True: sleep(TOKEN_FLUSH_SECONDS) → flush()`；每 `_TTL_TICK_INTERVAL` tick 一次 `ttl_sweep`，每 `_HEARTBEAT_TICK_INTERVAL` tick 一次 `_fanout_heartbeat`；仅 CancelledError 重抛 |
| `flush()` | :519-579 | 排序 drain 全部 `_pending` → `_fanout_frame`（每帧消费独立 revision）+ events_tap 直推 + `_drain_pending_session_resyncs`；记 `flush_duration_ms_total`/`flush_ticks_total` |

#### flush / 握手辅助（1）

| 方法 | 行号 | 职责 |
|---|---|---|
| `flush_sid(sid)` | :581-614 | 仅 drain 一个 sid 的 pending（§5.5 握手第 3 步：老订阅者收残差、新订阅者不收，C2 防双发） |

#### Ingest（GlobalHub.publish 调用，5）

| 方法 | 行号 | 职责 |
|---|---|---|
| `on_part_updated(props, part_revision=None)` | :619-715 | `message.part.updated`：text-start 创建 LivePart（与订阅者解耦，B1）/ text-end → `finish_part`；非 text 记 `_nontext_parts`；`part_revision` 参数被忽略（签名兼容）；deleted-sid / retired-message / malformed / disabled 门全部先于 revision 消费 |
| `on_part_delta(props)` | :739-829 | `message.part.delta`：field==text 门 + 五重 gate → `_reserve` 预算 → 同时 append 到 LivePart 与 `_pending` → 超 `TOKEN_FLUSH_BYTES`(4KiB) 立即早 flush → `_check_pending_budget` |
| `on_message_removed(sid, mid)` | :835-877 | `_retire_message`（原子清态+gate）→ live fanout（tombstone 进 ReplayLog）→ 记 `_removed_messages`（move_to_end 防 FIFO 逐出新鲜项，MAJOR 6） |
| `on_part_removed(sid, mid, pid)` | :879-907 | 幂等 `drop_part`（退役单 part；message-level 已退役则 no-op） |
| `on_session_status(sid, status)` | :1019-1070 | 归一化 busy/idle（兼容 string 与 `{"type":...}` 信封）；idle → `_retire_session` + **无条件写 replay barrier**（R4）+ `_enqueue_session_resync("session_idle")` |

#### 退役 / 清理（3）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_retire_message(sid, mid)` | :909-945 | 清 `(sid, mid, *)` 的 5 类结构 + 字节表 floor-0 + 写 `_retired_messages` gate |
| `_retire_session(sid)` | :1134-1165 | 清一个 sid 的 5 类 part 态结构（不动 `_session_status`/`_busy_sids`） |
| `ttl_sweep(now_ms=None)` | :1167-1206 | 60s tick：仅对 `_session_status==idle` 且超 `TOKEN_ACC_IDLE_MS`(60s) 的 LivePart 静默退役（busy-guard NB#4）+ prune `_removed_messages` |

#### 终态（1）

| 方法 | 行号 | 职责 |
|---|---|---|
| `finish_part(key, final_text)` | :950-1014 | 同步 drain 残差 pending → delta 帧（先）→ `snapshot{done:true}` 无 text 终态标记（后，Lever 1）→ `drop_part`；LivePart 已不存在则抑制标记 |

#### 会话删除 / 上游重连（2）

| 方法 | 行号 | 职责 |
|---|---|---|
| `on_session_deleted(sid)` | :1072-1132 | `_retire_session` + 清 status/busy + 写 barrier("session_deleted") + 清 `_retired_messages` 本 sid 项 + `_remember_deleted_sid` gate + **逐个 `sub.terminate("session_deleted")`**（INV-4，resync→STOP） |
| `on_upstream_reconnect()` | :2124-2190 | 全量清态（8 类）+ **保留 `_part_revisions`（防 ocdroid 严格 `>` 水位回退）与 `_removed_messages`**；对每个有订阅者的 sid fan `resync{reconnect_no_replay}` |

#### 订阅者 fanout 记账（3）

| 方法 | 行号 | 职责 |
|---|---|---|
| `attach_subscriber(sid, sub, wire_v4=False)` | :1211-1338 | §5.5 握手：v4 = 无 prefill 直入 fanout（`sub.wire_v4=True` + closed 检查）；v3 = `begin_handshake` 括号内 connected 帧 → tombstone 回放（TTL 过滤）→ `flush_sid` → 每 LivePart 快照/截断 → `end_handshake` 后 closed 再查 → 入 `_subs_by_sid` |
| `detach_subscriber(sid, sub)` | :1340-1353 | 幂等移出 fanout set；**不**退役 LivePart（B1 解耦） |
| `has_subscriber(sid, sub)` | :1355-1366 | 身份制成员检查（NB-D1，unsubscribe 防重复扣减） |

#### Fanout 辅助（10）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_replay_publish_token(sid, frame, kind)` | :1371-1394 | B3b-2 咽喉：帧 append 进 sid 的 token domain（published 语义，零订阅也记）→ 返回 `id:` 行；log 异常降级返回 None |
| `_write_replay_barrier(sid, why)` | :1396-1424 | R4：idle/evict/delete 三处**无条件**写 barrier，使 cursor≤watermark 的重连必走 `resync{reconnect_no_replay}`；异常吞掉 + warning |
| `_deliver_logged(sid, frame, id_line)` | :1426-1438 | v4 sub 收 `id_line+frame`，v3 收裸 frame；返回投递 sub 数 |
| `_deliver_v3_only(sid, frame)` | :1440-1459 | snapshot 族帧只投 v3 sub（v4 一律不收） |
| `_fanout_frame(key, frame)` | :1461-1481 | 帧分发总入口：eligible → 先 log 再 `_deliver_logged`；ineligible → `_deliver_v3_only`；计 `flushed_frames_total` |
| `_fanout_message_removed(sid, mid)` | :1483-1500 | tombstone live fanout（`FRAME_KIND_TOMBSTONE` 进 log，REPLAY-012） |
| `_fanout_resync(sid, reason)` | :1502-1527 | R3：v4 sub 遇非冻结 reason（`token_memory_limit`/`session_idle` 等）→ `sub.terminate(reason)`（只断不发帧）；v3 照发 `resync{reason}` |
| `_fanout_heartbeat()` | :1529-1536 | 15s 心跳到全部 token 订阅者（v3+v4 都收，不进 log） |
| `_emit_snapshot_or_truncated(sub, key, text, done)` | :1538-1595 | 单 sub 快照 + C6 帧上限检查；超限 → `_truncate_part_for_all`（fanout + drop）+ 非 fanout 内 sub 直投 truncated；v4 sub 只做探针（超限即 truncate，不投帧） |
| `_emit_snapshot_or_truncated_nodrop(sub, key, text, done)` | :1597-1648 | MB-P-S1：eviction 后对 **skip_key（当前 key）** 的重快照——超限只对**该 sub** 投 truncated、**绝不 drop_part**（保 O1：调用方持有的 `live` 引用不失效）；v4 直接 return |

#### 截断 / 内存预算（5）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_truncate_part_for_all(key, done)` | :1650-1698 | C6 backstop：幂等（`_is_disabled` 先查）→ 消费 revision → `drop_part` → `_deliver_v3_only(truncated)`；返回捕获的 revision 供直投 |
| `_reserve(live, n, key)` | :1703-1749 | LIVE 预算：per-part `TOKEN_PART_MAX_BYTES`(1MiB) 超限 → truncate+False；全局 `TOKEN_LIVEPARTS_MAX_BYTES`(4MiB) 超限 → while 循环 LRU 逐出**其他** key（绝不逐当前 key） |
| `_evict_part_for_memory(key, skip_key=None)` | :1751-1821 | LRU 逐出：`drop_part` → `flush_sid`（I1 防双发）→ 写 barrier("token_memory_limit") → `resync{token_memory_limit}` → 对该 sid 剩余 LivePart 重快照（skip_key 走 nodrop 路径） |
| `_check_pending_budget(current_key)` | :1823-1862 | Stage E：`_total_pending_bytes > TOKEN_PENDING_MAX_BYTES`(4MiB) → 强制 `flush()`；无订阅者（**全局**计数）/仍超 → LRU 逐出最老 LivePart + resync |
| `_start_part(key, seed="")` | :1867-1917 | 建 LivePart；count cap（`TOKEN_LIVE_PARTS_MAX`=32）先逐出；seed 超 per-part cap → 立即 truncate；NB-C1：seed 入账后 while 逐出其他 key 直至 ≤ 全局字节 cap |

#### Part 生命周期 / tombstone 记账（14）

| 方法 | 行号 | 职责 |
|---|---|---|
| `drop_part(key)` | :1919-1946 | 幂等退役：pop pending/live（字节 floor-0）→ 清 revision → 首次调用记 `_disabled_parts` 返回 True，后续 False；**从未见过的 key 也合法并标记 disabled** |
| `_remember_disabled(key)` | :1951-1966 | 有界 tombstone（cap `TOKEN_DISABLED_MAX`=4096 + TTL；重记不刷新 TTL） |
| `_remember_nontext(key)` | :1968-1979 | 同上有界非 text part 记录 |
| `_discard_nontext(key)` | :1981-1984 | 删单条 nontext tombstone |
| `_is_disabled(key)` | :1986-1987 | disabled gate 查询 |
| `_is_nontext(key)` | :1989-1990 | nontext gate 查询 |
| `_prune_bounded(store, now_ms)` | :1992-2016 | TTL 前向扫描 + FIFO cap（O(cap)） |
| `_remember_deleted_sid(sid)` | :2018-2030 | P1-22 有界 deleted-sid gate（cap/TTL 对齐 removed-messages 常量） |
| `_is_deleted_sid(sid)` | :2032-2040 | gate 查询 + 惰性 TTL 过期 |
| `_prune_deleted_sids(now_ms)` | :2042-2049 | gate 的 FIFO cap + TTL |
| `_prune_session_status()` | :2051-2054 | `_session_status` FIFO cap（10k） |
| `_prune_busy_sids()` | :2056-2059 | `_busy_sids` FIFO cap（10k） |
| `_prune_removed_messages(now_ms)` | :2061-2087 | removed 队列 TTL+cap；**逐出项同步 discard `_retired_messages` gate**（生命周期耦合） |
| `_next_part_revision(key)` | :717-737 | revision 唯一递增点（-1 起步首帧 0）；move_to_end + LRU cap（`TOKEN_DISABLED_MAX`） |

#### Pending resync 队列（2）

| 方法 | 行号 | 职责 |
|---|---|---|
| `_enqueue_session_resync(sid, reason)` | :2092-2103 | 有界 `(sid, reason)` 队列（cap 64，溢出丢最老，NB-B2） |
| `_drain_pending_session_resyncs()` | :2105-2118 | flush 内快照+清空后逐条 `_fanout_resync` |

（注：`_next_part_revision` 位于 ingest 区，为凑表格归入 tombstone/revision 组；行号为准。）

---

## 2. 依赖（内部 imports）

- `...config`（:67-82）：13 个 `TOKEN_*` 预算/节奏常量 + `DEFAULT_TOKEN_MAX_FRAME_BYTES`；其中 3 个（`TOKEN_LIVEPARTS_MAX_BYTES`/`TOKEN_PART_MAX_BYTES`/`TOKEN_LIVE_PARTS_MAX`）会被 `apply_debug_budget_overrides` 运行时改写（模块全局，测试 monkeypatch 同名）。
- `...logging_config.get_logger`（:83）。
- `..hub_types`（:84）：`TOKEN_FRAME_TYPE`、`normalize_session_status`。
- `..replay_log`（:85-90）：`ReplayLog`、`token_domain`、`FRAME_KIND_BUSINESS/TOMBSTONE`。
- `..replay_wire`（:91）：`V4_RESYNC_REASONS`（frozen 4 元素：epoch_changed/replay_expired/replay_gap/reconnect_no_replay，replay_wire.py:72-77）、`sse_id_line`。
- `.frames`（:92-104）：`STOP`、`sse_frame`、`_connected/_delta/_heartbeat/_message_removed/_now_ms/_resync/_snapshot/_truncated_frame`、`PartKey`。
- `.models`（:105）：`DeltaAccumulator`、`LivePart`、`_TokenMetrics`（models.py 定义，含 `dropped_frames_total` 等 9 计数器）。
- `TYPE_CHECKING`：`..hub.HubRegistry`（:107-108，仅类型）。
- 标准库：`asyncio`、`time`、`collections.OrderedDict`、`typing`。**无锁、无 executor、无线程**——全部状态假设单事件循环线程内同步访问（所有 ingest/flush 路径无 await 窗口）。

## 3. 被依赖（rg 反查 `tokenstream.hub` / `TokenStreamHub`，结论）

生产代码（7 处）：

| 使用方 | 位置 | 用法 |
|---|---|---|
| `sse/tokenstream/__init__.py` | :7 | re-export `TokenStreamHub` |
| `sse/token_hub.py` | :10-26 | 兼容 shim re-export（旧 import 路径） |
| `app.py` | :36-37, :307, :538-541, stack.callback | lifespan 构造唯一实例（`max_frame_bytes=settings.token_stream_max_frame_bytes`、`replay_log=app.state.replay_log`）、调 `apply_debug_budget_overrides(settings)`、shutdown 时 `stop()`（NB-C4 LIFO 在 hubs.close 前） |
| `sse/global_hub.py` | :55, :117, :490-491, :779/:781, :911/:935/:963/:970/:972, :417 | `set_token_hub` 注入；`publish()` 路由 session.status/deleted、part.updated/removed、message.removed、part.delta；`has_consumers` 读 `subscriber_count`；`_notify_upstream_loss` 调 `on_upstream_reconnect` |
| `sse/registry.py` | :31, :75, :94-95, :323 | `HubRegistry` 持 `_token_hub`；grace 移除路径调 `on_upstream_reconnect`（清理后再判断） |
| `sse/tokenstream/subscriber.py` | :603/:619（events_tap append/remove）、:606/:621/:701/:782/:832（start/stop）、:705（attach_subscriber）、:772/:821（has_subscriber）、:773/:823（detach_subscriber）、:681（`self.token_hub._metrics` 私有穿透共享计数器） | `TokenStreamRegistry` 是 hub 的 HTTP 层编排者；`TokenSubscriber` 被 hub 鸭子类型消费（`put`/`terminate`/`begin/end_handshake`/`closed`/`wire_v4`） |
| `routes/events.py` | :158-162 | `?tokens=1` → `token_registry.attach_events_subscriber(subscriber)`（间接进 `events_tap`）；`routes/token_stream.py:138` 经 `token_registry` 间接 |

测试（9 个文件，重度依赖）：`tests/test_token_hub.py`、`test_token_hub_flush.py`（大量 `monkeypatch.setattr("oc_slimapi.sse.tokenstream.hub.TOKEN_*")` 改模块全局）、`test_token_hub_lifecycle.py`、`test_token_stream_route.py`、`test_sse_replay_wire.py`（含 `tokenstream_hub_module` 直接 import + `_SESSION_STATUS_MAX`）、`test_events_tokens.py`、`test_batch3_lifecycle.py`、`test_session_status_object_format.py`、`test_token_subscriber_overflow.py`。

**结论**：hub 是 token 流域的唯一权威累积器，进程级单例（app.state.token_hub），上游入口 = `GlobalHub.publish`，下游出口 = `TokenStreamRegistry`/`TokenSubscriber` + events_tap；测试与模块全局 cap 的耦合是刻意的（`apply_debug_budget_overrides` docstring :139-141 说明兼容性）。

## 4. 状态 / 可变性（长生命周期对象逐项）

| 状态 | 行号 | 结构 / 预算 |
|---|---|---|
| `live_parts` | :254 | `dict[PartKey, LivePart]`——LIVE 权威文本；受 count cap 32 + 全局 4MiB + per-part 1MiB |
| `_replay` | :271 | 进程级 `ReplayLog`（只 append/barrier，异常全吞） |
| `_nontext_parts` | :273 | `OrderedDict[PartKey, int]`，cap 4096 + TTL |
| `_disabled_parts` | :274 | 同上 |
| `_pending` | :275 | `dict[PartKey, DeltaAccumulator]`——flush 前影子；全局 4MiB |
| `_total_live_bytes` | :276 | LIVE 字节表（floor-0 递减） |
| `_total_pending_bytes` | :277 | PENDING 字节表（与 LIVE 独立，同字节双计是设计 :50-54） |
| `_metrics` | :278 | `_TokenMetrics`（**与 subscriber.py 共享可变对象**，subscriber.py:681 私有穿透） |
| `_session_status` | :282 | `OrderedDict[str, str]`，cap 10k（P1-21） |
| `_busy_sids` | :283 | `OrderedDict[str, None]`，cap 10k；**生产无读者**（见疑问点 2） |
| `_pending_session_resinks` | :285 | `list[(sid, reason)]`，cap 64 丢最老 |
| `_subs_by_sid` | :288 | `dict[str, set[Any]]` 订阅者 fanout 账本 |
| `events_tap` | :298 | **公有可变 `list`**（registry append/remove bound `sub.put`） |
| `_max_frame_bytes` | :300 | 帧上限（来自 settings，INV-5） |
| `_flush_task` | :302 | 唯一后台 task（watchdog 重建） |
| `_part_revisions` | :325 | `OrderedDict[PartKey, int]` LRU cap 4096；reconnect 保留（:2170-2173） |
| `_removed_messages` | :335 | `OrderedDict[(sid, mid), int]` cap 1000 + 24h TTL；reconnect 保留 |
| `_retired_messages` | :345 | `set[(sid, mid)]`——与 removed 队列生命周期耦合（:2071-2087），但 reconnect wholesale 清空（:2180） |
| `_deleted_sids` | :355 | `OrderedDict[str, int]` cap 1000 + 24h TTL（对齐 removed 常量） |

锁：无（单事件循环假设）。executor：无。queue：订阅者队列在 subscriber.py 侧（T3 背压），hub 侧仅 `_pending_session_resinks` 简单 list。

## 5. 错误路径

- **异常抛出**：本文件**不主动抛**任何异常；ingest 全部静默早退（malformed props → return，:665-671、:766-776 等）。
- **异常捕获/降级**（仅 3 处，全在 ReplayLog 周边）：
  - `_replay_publish_token` :1389-1394 `except Exception` → `logger.warning("replay log append failed for sid %r")` → 返回 None（帧照发、无 id 行）。
  - `_write_replay_barrier` :1419-1424 `except Exception` → `logger.warning("replay barrier write failed ...")` → 吞掉。
  - `flush_loop` :516-517 仅重抛 `CancelledError`；**其他异常杀掉 task** → `_on_flush_done` :469-476 `logger.critical("token flush_loop died unexpectedly; ...")` + `has_consumers()` 时重建。
- **错误码/resync reason 产出**：`token_memory_limit`（:1802，v4 → terminate）、`session_idle`（:1070，v4 → terminate）、`reconnect_no_replay`（:2190，v4 冻结 reason，正常帧）、`session_deleted`（:1132 经 terminate）；`subscriber_backpressure` 在 subscriber.py 侧触发，hub 的 events_tap 直推路径复用同一 `put` 守卫（A-C4）。
- **warning 日志点**：:1392、:1422-1423（仅这两处 warning）；critical：:469-473。
- **静默丢弃（设计内，C3/C4）**：orphan delta 只计数（:787）；nontext/disabled/retired/deleted-sid/ended-late delta 全静默。

## 6. 疑问点（draft 种子，24 条，宁多勿漏）

1. **TODO :663（on_part_updated）**：`# TODO(§13.2): confirm live wire key casing for properties.part.` —— `props.get("part")` → `part.get("sessionID"/"messageID"/"id")` 的大小写是**未与 live wire 核对的假设**。若实际 wire 携带 `sessionId` 等不同 casing，:665-671 直接 return——**无声无计数无日志**（连 `orphan_deltas` 都不加），text-start 整个丢失后所有 delta 变 orphan。影响：整段流静默消失，仅能靠 orphan_deltas 涨数间接发现 text-start 没建出来。
2. **TODO :760（on_part_delta）**：同族 casing 假设（`sessionID`/`messageID`/`partID`/`field`/`delta`）。:761 `field != "text"` 与 :763-766 任一不匹配都是静默 return。与 :663 同为「上游 schema 漂移 → 全静默」风险点。
3. **`_busy_sids` 生产端只写不读**（:283、:1056-1058、:1061、:1113、:2176；docstring :240 称 "O(1) busy lookup mirror"）。`ttl_sweep` 实际读的是 `_session_status.get(sid)`（:1190）。rg 反查：生产代码零读者，仅测试断言读（test_token_hub_lifecycle.py:533 等）。死状态 + 每次状态事件多付一次 prune。
4. **tombstone 回放排序成本**（:1315-1317）：v3 握手对**全局** `_removed_messages`（最多 1000 项）按 timestamp 全排序再按 sid 过滤——每次 attach O(N log N)，且过滤在排序后。可先按 sid 过滤再排序。
5. **`_check_pending_budget` 的 had_subs 是全局计数**（:1853）：sid B 无订阅者触发 pending 溢出、但 sid A 有订阅者时 → `had_subs=True` → 只 force-flush（B 的 delta 静默丢）**不逐出**；B 的 LivePart 继续增长，仅靠 LIVE 预算兜底，且每条 delta 反复触发全量 flush（:1855）。docstring :1848-1849 说的是 "NO subscribers"，实现是全局口径——语义偏差。
6. **v4 订阅者 oversized part 零信号**（:1562-1576 + :1691-1696）：v4 探针超限 → `_truncate_part_for_all` → truncated 帧只投 v3（`_deliver_v3_only`）。v4 客户端对该 part **什么都收不到**（无 snapshot、无 truncated、无 resync、done 也被抑制），只能靠 HTTP 全量拉取自愈——契约上成立（v4 状态对齐=HTTP），但流侧无任何可观测信号。
7. **v4 非冻结 reason 一律 terminate**（:1520-1524）：`session_idle` 也走 `sub.terminate`——v4 sub 挂在一个 idle→busy→idle 波动的会话上会被**反复断连**而非收 resync。R3 语义如此，但对会话生命周期短的 v4 客户端体验是断连风暴隐患。
8. **`_on_flush_done` 重建无退避**（:469-476）：若 `flush()` 确定性抛异常（如未来某 tap/序列化路径引入异常），watchdog 以 ≤10Hz 频率 CRITICAL 刷屏 + 无限重建，无 retry budget/backoff。当前 `Subscriber.put` 不抛（返回 bool），但该不变量靠约定不靠类型。
9. **`_next_part_revision` LRU cap 逐出可致 revision 回退**（:730-737）：条目被 4096-cap 逐出后同 key 重新从 0 计数 → 严格 `>` 客户端丢后续帧。防线是 config.py:142-144 的 `assert TOKEN_LIVE_PARTS_MAX(32) <= TOKEN_DISABLED_MAX(4096)` + 活跃 part 的 LRU 热度——**隐式跨模块不变量**，`apply_debug_budget_overrides` 只校验 live_parts_max 一项（config.py:1033-1038）。
10. **进程重启 revision 归零**（:2149-2155 KNOWN LIMITATION）：文档已承认，跨仓协议问题。审计时确认 ocdroid 侧无「sidecar 冷启动重置水位」信号即可。
11. **`on_upstream_reconnect` 的 gate/queue 解耦**（:2180 清 `_retired_messages` vs :2184-2185 保留 `_removed_messages`）：违反 :341-344 声明的「gate 生命周期与 replay 队列耦合」不变式。理由是新 epoch 无迟到事件（GlobalBus 无 replay）——前提成立则无害，但这是**依赖上游行为假设的非对称**。
12. **`ttl_sweep` 退役不写 barrier、不发任何帧**（:1192-1202）：与 `_evict_part_for_memory`（:1801 写 barrier + resync）不一致。可达路径：idle 后又有迟到 text-start（idle 转换时 `_retire_session` 已清过一轮，:1062）→ 新 LivePart 累积 → 60s 静默 → TTL 退役。此时 v4 客户端 cursor==last_seq 仍判 up-to-date 挂在死 part 上（idle barrier 的 watermark 早于该 part 的帧）。极边缘但与 R4 rationale 相悖。
13. **`finish_part` 对孤儿 text-end 也会 disable key**（:983-1014 → `drop_part` :1919-1946 docstring「从未见过的 key 也标记 disabled」）：sidecar 重启错过 text-start 的 part，其 text-end 会把 key 永久（6h TTL 内）拉黑；若上游对同 pid 重发 text-start（文档 :1939-1941 称 session 内 ID 不复用）会被 `_is_disabled` 静默吞。
14. **`_emit_snapshot_or_truncated` v4 探针缺 revision 字段**（:1573，注释自认 ~10 字节偏差）：v3/v4 截断边界不一致——理论上一帧「v3 判不超、v4 探针判超」的窗口存在，导致 v4 路径多 truncate 一个本可不截的 part。
15. **revision 在尺寸检查前消费**（:1577-1581）：oversized snapshot 的 revision 被浪费（docstring :1553-1560 自认）。客户端严格 `>` 仍接受后续帧，只是序列有洞——可观测性小噪声。
16. **`flushed_frames_total` 计的是投递尝试数**（:1481、:1437-1438 返回 `len(subs)`）：`sub.put` 返回 False（closed/溢出）也计入。指标名与语义不符（delivered vs attempted）。
17. **`dropped_frames_total` 在 hub.py 零递增**（属性 :406-408；递增全在 subscriber.py:405/421/441）：靠 `subscriber.py:681` 的 `self.token_hub._metrics` **私有属性穿透**共享同一 `_TokenMetrics` 实例——封装脆弱点，两文件必须同实例否则指标分裂。
18. **`apply_debug_budget_overrides` 改模块全局**（:155-161）：生产误设 `OC_SLIMAPI_TOKEN_STREAM_DEBUG_*` 即改变所有 hub 实例预算；validate 只挡 live_parts_max 一项（config.py:1033-1038），`live_budget_bytes < TOKEN_PART_MAX_BYTES` 的误配会触发 :1741-1745 自认「不可达」的防御分支（当前 key 也被 truncate）。
19. **`_start_part` 先入账后逐出**（:1894-1917）：seed 字节先加进 `_total_live_bytes`（:1907）再 while 逐出（:1912）——与 `_reserve` 的「先查后加」模式相反，瞬时可超 cap；单线程内无害，但若未来并发化是隐患。且 count-cap 逐出（:1891-1893）在加 seed 之前，两轮逐出条件不对称（`>=` vs `>`）。
20. **import 时换算的 tick 间隔**（:121、:123）：`_TTL_TICK_INTERVAL`/`_HEARTBEAT_TICK_INTERVAL` 在 import 时由常量算出，事后 monkeypatch `TOKEN_FLUSH_SECONDS` 不生效（测试只能直接 patch 间隔，test_token_hub_flush.py:403/:438 即如此）——行为耦合点，非 bug。
21. **`events_tap` 是公有可变 list**（:298）：类型 `list[Any]`（装 bound `sub.put`），registry 双账本（`registry.events_tokens` set + `hub.events_tap` list）靠 `attach/detach_events_subscriber` 对称维护（subscriber.py:601-621）——docstring :387-390 自称「no parallel counter to drift」，但两容器本身**就是**并行账本，detach 的 `suppress(ValueError)`（subscriber.py:619）掩盖不对称。
22. **`on_part_updated` 重复 text-start 忽略 seed**（:706-710）：`key in live_parts` 时不 append seed。若上游在重复 text-start 里携带了更长累积文本（非重复而是补偿），多出的部分静默丢失，依赖后续 delta 补齐——依赖「text-start 幂等且文本单调」的上游假设。
23. **handshake tombstone 回放无 id/无 log**（:1322 `sub.put` 直投）：v3 客户端重连会再次收到同一批 tombstone（幂等性交给客户端）；同时该路径在 `begin_handshake` 括号内绕过 T3 溢出守卫（CRITICAL 3 设计），超大批量 tombstone（理论上限 = 1000 + 32 snapshot，config.py:87-88 注释自算）可无上限压入握手缓冲——溢出守卫被显式绕过后仅靠 `sub.closed` 事后检查兜底（:1335-1336）。
24. **命名/杂项**：`_pending_session_resinks` 拼写（resinks vs resyncs，:285 等）；`flush()` 每 tick 两次全量列表推导（:542 sorted + :572 全量扫描空 acc，O(N)·10Hz，N 受 4MiB 预算约束尚可）；`attach_subscriber` v4 分支首帧最多延迟 100ms（:1289-1304 无 flush_sid，留给下个 tick，docstring 自述）。

### 任务点名专题小结

- **两处 TODO（:663/:760）**：见疑问点 1/2——同一根因（live wire key casing 未核实），影响是「静默丢流」，建议 draft 阶段对照 opencode `message-v2.ts`/event payload 实测核对。
- **tombstone 回放**：v3 握手 :1313-1322（全局排序成本 #4）；v4 改由 ReplayLog 承担（:1498）；gate 与队列耦合破裂于 reconnect（#11）。
- **预算/flush 窗口**：LIVE/PENDING 双 4MiB 独立账本（:276-277）；4KiB 早 flush（:811-823）在 `_check_pending_budget`（:829）之前；had_subs 全局口径偏差（#5）；`_start_part` 先加后逐（#19）；debug override（#18）。
- **空闲逐出**：`ttl_sweep` busy-guard（:1190 只认已知 idle）+ 静默退役无 barrier（#12）；`_reserve`/`_start_part`/`_check_pending_budget` 三处 LRU 逐出共用 `_evict_part_for_memory`，skip_key/nodrop 保 O1（:1789-1821）。
- **events-token 保活双账本**：`events_tap`（hub :298）+ `events_tokens`（registry subscriber.py:581）并行容器（#21）；`has_consumers`（:395）统一判活修复了 events-only 死循环问题（docstring :390-393）。
- **背压溢出断连路径**：hub 侧全部经 `sub.put`（返回 bool 被忽略，#16）；溢出 → subscriber 内部 terminate(resync+STOP)+closed（subscriber.py:386-441）；握手期被 `begin/end_handshake` 绕过（#23）；v4 非冻结 reason 走 terminate 不走帧（#7）。
