# E1-05 精读卡片：SSE hub 四文件（global_hub / hub_types / hub / token_hub）

> 生成：2026-08-20 审计探索（只读）。引用格式 `路径:行号`。四文件均已全文精读（非抽样）。

---

### src/oc_slimapi/sse/global_hub.py（1090 行）

#### 职责
进程唯一（process-wide）的上游 `/global/event` SSE 订阅者：单条连接消费上游 GlobalBus 事件流，将其分类（IMMEDIATE 直推 / digest 防抖合并 / token 流路由 / 丢弃），策展后扇出给控制面订阅者（`Subscriber`），并维护 sticky lastError、deleted tombstone、retired-message gate、replay 日志写入、token-hub 镜像路由、T3 观测计数。任务组（run/flush/heartbeat）由 supervisor done_callback 自愈。

#### 对外符号（完整）

模块级：
- `_LAST_UPDATED_AT_BY_SID_MAX = 10_000`（src/oc_slimapi/sse/global_hub.py:60）— 三个 sid 表（`_last_updated_at_by_sid` / `sticky_last_error` / `deleted_tombstones`）共用的 FIFO/LRU 上限。
- `logger`（src/oc_slimapi/sse/global_hub.py:57）— `logging_config.get_logger(__name__)`。

类 `GlobalHub`（src/oc_slimapi/sse/global_hub.py:63）：
- `__init__`（:66）— 构造：client/订阅参数/traffic_ledger/allowlist/replay_log/turn_registry 注入位 + 全部可变状态容器初始化（见「状态/可变性」）。
- `_bump_updated_at`（:168）— `entry.updated_at = max(now, max(entry_prev, session_prev)+1)`，按 sid 跨防抖窗保证严格单调（LRU `move_to_end`）。
- `ensure_upstream`（:194）— 幂等启动 run/flush/heartbeat 任务组；取消已武装的 grace-stop；`task.done()` 时经 `_spawn_group` 重建。
- `_spawn_group`（:219）— INV-1 原子任务组创建：先取消残存兄弟任务，再以局部变量建 run/flush/heartbeat 三个 task 并挂 done_callback，最后赋给 `self.*`。
- `_make_group_done_callback`（:260）— 组成员 supervisor：闭包持有本组 task 引用；cancelled→no-op；正常退出（仅 run）→取消兄弟；异常死亡→取消兄弟并（若有消费者）强制 `_spawn_group` 重建；staleness 守卫 `self.task is run_task`。
- `subscribe`（:329）— 准入一个 `Subscriber`；`welcome=True` 先投连接局部 `server.connected` 帧（v4 路由传 False 抑制）；随后 `ensure_upstream()`。
- `unsubscribe`（:349）— 移除订阅者；最后一个离开且无 stop_task 时武装 `stop_after_grace`（生产 detach 走 `HubRegistry.unsubscribe`）。
- `has_consumers`（:362）— `bool(self.subscribers) or (_token_hub.subscriber_count > 0)`：控制面 + token 两个账本合并判活（刻意为方法非属性）。
- `_notify_upstream_loss`（:383）— 上游失联规范化钩子：`resync_all()` + replay `write_barrier()`（best-effort）+ `token_hub.on_upstream_reconnect()`；per-epoch 只应触发一次（守卫在 run() 侧）。
- `stop_after_grace`（:419）— 30s 宽限后若仍无消费者则 cancel 三个任务。
- `flush_loop`（:427）— 每 `DEBOUNCE_SECONDS`(0.25s) 调 `flush()`。
- `flush_sid`（:432）— 只冲刷单个 sid 的 pending digest（G1-A `session.error` 与 busy-清-sticky 立即路径）；sticky 合并 + `changed=[sid]` + `_emit_directory_frame`。
- `flush`（:452）— 批量冲刷：先机会式 prune 四张表，再 `snapshot, self.pending = self.pending, {}` 逐 sid 合并 sticky、置 `changed`、发 `session.digest`。
- `heartbeat_loop`（:481）— 每 10s 向所有订阅者投 `server.heartbeat` 并累计 `emitted_frames_total`。
- `set_token_hub`（:490）— 注入 TokenStreamHub（publish 路由 part/delta 依赖）。
- `set_directory_observer`（:500）— 注入可选同步 directory 观察者（B1b shadow scheduler）。
- `_observe_directory`（:506）— 非空 str directory 时调观察者；异常仅 debug log，绝不影响 ingest。
- `set_turn_registry`（:516）— 注入 TurnRegistry（digest 的 `turnIncarnation`/`turn` 快照盖章）。
- `set_directory_allowlist`（:528）— 设置进程级 directory 过滤并 `clear_allowlist_roots_cache()`（config 变更信号，重解析根）。
- `set_replay_log`（:536）— 注入 ReplayLog（B3b-2 v4 replay；None = v3-only 栈）。
- `_replay_publish`（:547）— 向 GLOBAL domain append 一帧，返回 `id:` 行；append 失败降级为 None（id-less 扇出 + warning）。
- `_directory_allowed`（:572）— allowlist 为空→True；否则委托 `config.directory_allowed`（相对路径/非 str fail-closed）。
- `_emit_directory_frame`（:584）— allowlist 闸门（不过→`allowlist_dropped_events++` 丢弃）；过→replay 记录；wire_v4 订阅者收 `id_line+frame`，v3 收原字节；`emitted_frames_total += len(subscribers)`。
- `_prune_retired_messages`（:601）— retired-message gate 的 TTL(24h)+FIFO(1000) 修剪，与 token hub 语义对齐。
- `_prune_last_updated_at`（:629）— `_last_updated_at_by_sid` LRU 上限 10k。
- `_prune_sticky_last_error`（:644）— sticky lastError FIFO 上限 10k。
- `_prune_deleted_tombstones`（:651）— deleted tombstone FIFO 上限 10k。
- `publish`（:658）— 上游帧总分类器（详见下）。
- `resync_all`（:977）— 清 tombstone / retired gate / updated_at 表；向全部订阅者投 `resync{reconnect_no_replay}`。
- `run`（:993）— 上游连接主循环：指数退避(1→30s)重连；连接成功且 ever_connected→重连计数+（未通知过则）loss 通知；aiter_lines 组帧（`data:` 前缀 + 空行分隔）→ `orjson.loads` → `publish`；EOF 与异常路径等价处理（通知+退避）。

`publish()` 内部分类（全集）：
1. `IMMEDIATE`（question.asked/question.v2.asked/permission.asked/permission.resolved/permission.v2.asked/permission.v2.resolved，src/oc_slimapi/sse/hub_types.py:73）→ 原样直推帧（含 `qp_last_activity[directory]` 记录）（:671-685）。
2. `SESSION_EVENTS`（session.status/session.updated/session.deleted）（:688-782）：
   - `session.status`：`normalize_session_status` 归一（:702）→ 填 `entry.status`；TurnRegistry 快照 ingest 时盖章（:713）；busy 且 sticky 存在 → pop sticky + `entry.last_error=None` + `flush_sid` 立即清帧（:720-723）。
   - `session.deleted`：`entry.deleted=True`、写 tombstone、pop sticky、`last_error=_UNSET`、清该 sid 的 retired gate 与 updated_at 高水位（:724-743）。
   - `session.updated`：仅透传 `info.time.archived`（int 非 bool，含 0；缺失不清）（:744-762）。
   - token hub 镜像分支（session.status/deleted → `on_session_status`/`on_session_deleted`）（:768-781）。
3. `MESSAGE_EVENTS`（message.updated/message.appended）（:784-800）：提取 sid + messageID → `_bump_updated_at`（updatedAt=sidecar 墙钟，非上游时间戳）。
4. `session.error`（:806-858）：name 非 str 强转 None；`ABORT_NAME` 静默丢弃；`_sanitize_error_message` 脱敏；有 sid（仅取 `props.sessionID`）→ tombstone/同窗 deleted 守卫 → 写 entry+sticky → `flush_sid` 立即；无 sid → 直接 `session.error` 帧。
5. token 族（message.part.delta / message.part.updated / message.part.removed / message.removed）（:882-973）：part.updated 校验 `part.{sessionID,messageID,id}` → retired gate 拦截 → `token_hub.on_part_updated`；part.removed（flat `{sessionID,messageID,partID}`）→ `on_part_removed`；message.removed（flat）→ 写 retired gate + `on_message_removed`；delta / 畸形 part → `on_part_delta` / `on_part_updated` 兜底。
6. 其余一切（text delta、tool.*、未知类型）→ 静默丢弃（:975 注释，无计数）。

#### 依赖 / 被依赖
- 依赖（import）：`..config`（TOKEN_REMOVED_MESSAGES_MAX/TTL、clear_allowlist_roots_cache、directory_allowed，src/oc_slimapi/config.py:75-76,255,341）；`..logging_config.get_logger`；`.hub_types`（21 个符号）；`.replay_log.GLOBAL_DOMAIN`；`.replay_wire.sse_id_line`；TYPE_CHECKING：`..traffic.TrafficLedger`、`..turn_registry.TurnRegistry`、`.replay_log.ReplayLog`、`.token_hub.TokenStreamHub`（注意：**经 shim** `.token_hub` 而非直连 `.tokenstream`，src/oc_slimapi/sse/global_hub.py:55）；运行时三方：httpx、orjson、asyncio。
- 被依赖（生产）：`sse/registry.py:13`（HubRegistry 持有唯一 GlobalHub）；`sse/hub.py:17`（shim re-export）；`app.py:490-491,505,511,588`（set_replay_log / set_directory_allowlist / qp_last_activity / set_directory_observer / set_turn_registry）；`routes/health.py:100`（读 allowlist_dropped_events）；`sse/registry.py:347-349`（读三个计数器）。
- 被依赖（测试）：test_batch3_lifecycle / test_sse_replay_wire / test_b4_allowlist / test_turn_registry / test_b1b_sweep_shadow / test_message_fingerprint / test_hub（monkeypatch `global_hub._now_ms`、`GRACE_SECONDS`、`asyncio.sleep`）/ test_events_tokens / test_globalhub_retired_gate / test_session_status_object_format 等。

#### 状态 / 可变性
- 任务组：`task`/`flush_task`/`heartbeat_task`（run/flush/heartbeat）+ `stop_task`（grace 定时器）；INV-1 原子组 + supervisor 自愈；**全程无锁**（单事件循环内联假设，`Subscriber.put` 注释 src/oc_slimapi/sse/hub_types.py:298 亦确认）。
- sid 表：`pending: dict[str, DigestFields]`（防抖窗累积，flush 时整体换出）；`_last_updated_at_by_sid`（LRU 10k）；`sticky_last_error`（FIFO 10k）；`deleted_tombstones`（FIFO 10k，resync_all 清空）；`_retired_messages`（(sid,mid)→ts，TTL 24h + FIFO 1000，session.deleted / resync_all 清）。
- `qp_last_activity: dict[str, float]`（:109,:678）— **唯一无上限的 map**（键=出现过 q/p 事件的 directory；QpSweepShadow 只读写从不删键，src/oc_slimapi/qp_sweep.py:55-183）。
- 计数器：`upstream_events_total` / `emitted_frames_total` / `reconnects_total` / `allowlist_dropped_events`（:146-149）；`ever_connected`、`_upstream_loss_notified`（per-epoch 失联守卫）。
- 注入位（可后置替换）：`_token_hub` / `_turn_registry` / `_replay` / `_directory_observer` / `directory_allowlist` / `_traffic_ledger`。

#### 错误路径
- run()：`raise_for_status` 失败 / 流异常 → except Exception → （首失联时）`_notify_upstream_loss` + warning + 退避重连（:1075-1090）；EOF（正常结束）等价处理（:1056-1072）；client=None 防御性 sleep 退避防热循环（:997-1001）。
- 帧解码：`orjson.JSONDecodeError` → debug log 丢帧（:1053-1054）。
- publish 内 session.error name 强转 str 防 TypeError 逃逸（:810-813）。
- replay：append 失败 → warning + id-less 降级（:567-569）；barrier 写失败 → warning 降级（:411-415）。
- 观察者 / 流量账本失败 → warning/debug，不影响主路径（:512-514,:1046-1047）。
- 下游背压：`Subscriber.put` 溢出 → 强制断连（v3: resync+STOP；v4: 仅 STOP）。

#### 疑问点（宁多勿漏）
1. **死 import**：`import logging`（:11）全文件未使用（logger 来自 logging_config）。
2. **publish 异常逃逸面**：`orjson.loads` 产出非 dict JSON（如 `[1,2]`）时 `global_event.get` AttributeError 逃出 publish → 被 run() except 捕获 → **整条连接按上游失联处理**（重连 + resync 扇出）；JSONDecodeError 有守卫（:1053）但非 dict 形状无守卫（:1052 vs :662-663）。毒帧可造成反复重连噪音。
3. **未知类型零观测**：catch-all 丢弃（:975）无 per-type 计数 / 日志；上游帧分类全集是否与 opencode v1.18.18 GlobalBus 实际发出的类型集对齐（instance.idle / authorization.updated / integration.* / config.* 等是否需要透传或至少计数）无法从本文件验证 —— 需对照 `opencode-src/current` 源码（AGENTS.md 表列路径）。
4. **allowlist 语义**：空 list 与 None 等价=禁用（:574-575）；被拒帧静默丢（仅计数 :586），**不进 replay**（`_replay_publish` 在闸门之后，:592）—— 符合"从未发布"语义，但 v4 客户端对被拒 sid 的 Last-Event-ID 补帧会直接跳过该帧，客户端无从感知过滤发生。
5. **`emitted_frames_total` 语义偏差**：按 `len(self.subscribers)` 累加（:488,:598-599），忽略 `Subscriber.put` 的 bool 返回（v6 §3.5 已提供）——closed/溢出丢弃的帧也被计入"emitted"。
6. **IMMEDIATE 帧含 `"directory": None`**（:679-683）：directory 缺失时 q/p 帧仍带 null 键；与 digest 的省略式（`to_payload` 仅非 None 才带键，src/oc_slimapi/sse/hub_types.py:183-184）不一致 —— 是否契约冻结形状需对照 v3-contract §7。
7. **q/p 丢失窗口**：(a) hub 任务组退出期间（无消费者）上游事件完全不被 ingest，永久丢失，靠重连路径 `resync_all` 通知客户端冷同步；(b) 上游断连到重连之间的事件丢失，v4 靠 ReplayLog 补（GLOBAL domain 记录含 IMMEDIATE 帧，:90-93 注释），但 v3 无补；(c) SSE 组帧要求空行终结（:1050），**流 EOF 时无空行收尾的最后一个 data 块丢失**（`data_lines` 残留不 publish）。
8. **read=None 无读超时**（:1002）：上游半开连接（TCP 未死但无数据）不会被检测，恢复仅靠 EOF/异常；heartbeat 是下游方向，掩盖不了上游僵死。
9. **sticky 表 FIFO 逐出的隐性语义**（:648-649）：sticky 被逐出后 digest 不再合并 lastError（wire 效果=字段消失），与显式 `lastError: null` 清除（:722）在客户端可区分吗？契约是否区分"省略"与"null"？
10. **G1 busy 清 sticky 的精确条件**（:720-723）：仅 `normalized == "busy"` 精确匹配（大小写敏感）；`normalize_session_status` 不做枚举校验（任意 str 直通 digest.status，src/oc_slimapi/sse/hub_types.py:398-399），所以 `"Busy"` 会进 digest 但不触发清除——上游枚举域需对照 opencode 源码确认。
11. **busy-clear 后同窗孤儿对象**：`flush_sid` pop 掉 pending entry 后，同一次 publish 调用内后续分支不再写 `entry`（当前安全，:723 后直接进 mirror 分支）；但 `session.error` 路径 :846 同理——若未来在 flush_sid 之后追加对 `entry` 的写即成孤儿写（写进已发出的对象），结构性脆弱点。
12. **session.updated 不 bump updatedAt**（:744-762 只透传 archived）：纯 session.updated 窗口产出的 digest 无 `updatedAt` 字段；客户端 `(updatedAt, messageID)` 排序对无 updatedAt 帧的行为需对照契约 §5。
13. **session.error 的 sid 仅取 `props.sessionID`**（:804-805,:823）：若上游把 sid 放 info 内（其他 session.* 支持 info.id 兜底，src/oc_slimapi/sse/hub_types.py:349-373），会误走 G1-B 无 sid 全局帧路径——上游 session.error 实际形状需对照 schema。
14. **token mirror 分支读外层变量 `status`**（:778）：仅在 `event_type=="session.status"` 分支定义（:702）；当前控制流保证已定义，但重排即 NameError（无局部静态保障）。
15. **`_retired_messages` 逐出不对称**：gate FIFO 1000 逐出后，迟到的 part.updated 可为已删消息重建 token-hub 状态（与 token hub 自身 `_retired_messages` 语义对齐，:606-618 承认此权衡）；跨 24h TTL 的迟到帧同理。
16. **tombstone FIFO 10k 逐出后**（:655-656），极高 churn 下迟到 session.error 可复活已删会话的 sticky（P1-21 已知权衡，注释 :138-143 仅论证 resync 清空路径）。
17. **stop_after_grace 不清 task 引用**（:419-425）：cancel 后 `self.task` 仍指 done task；`ensure_upstream` 靠 `task.done()` 判断可重建（正确），但 `self.stop_task` 触发后残留 done 引用使 `unsubscribe` 的 `not self.stop_task` 守卫不再武装第二次 grace（需订阅→退订→再退订序列才复现，边缘）。
18. **heartbeat 也计入 emitted_frames_total**（:487-488）：控制帧与业务帧混在同一计数器，metrics 语义需对照契约 §6。
19. **`qp_last_activity` 无 prune**（见上）；键空间=上游可控的 directory 字符串（任意非空 str 即入键，:672-678），恶意/异常上游可撑大该 dict。
20. `_observe_directory` 双重观察（publish 入口 :663 + flush/flush_sid :449,:478）——幂等无害但重复调用。

---

### src/oc_slimapi/sse/hub_types.py（419 行）

#### 职责
SSE hub 基础层：哨兵（STOP/_UNSET）、错误脱敏、T3 默认值、事件分类集合（IMMEDIATE/SESSION_EVENTS/MESSAGE_EVENTS）、时序常量、帧构造助手、`DigestFields`（防抖窗聚合）、`Subscriber`（T3 带守卫出站队列）、sid 提取、status 归一化、`SubscriberCapacityError`。叶子模块（hub→registry 单向，无反向依赖）；被 global_hub / registry / tokenstream 共享以避免实现重复。

#### 对外符号（完整）

模块级常量 / 哨兵：
- `STOP = object()`（src/oc_slimapi/sse/hub_types.py:30）— 控制面 SSE 生成器终结哨兵（注意与 tokenstream 自有 STOP 是**两个不同对象**，src/oc_slimapi/sse/tokenstream/frames.py:19）。
- `_UNSET = object()`（:32）— `DigestFields.last_error` 三态哨兵（_UNSET=省略 / None=显式清 / dict=对象）。
- `ABORT_NAME = "MessageAbortedError"`（:33）— 静默丢弃的 abort 错误名。
- `_UNIX_PATH_RE`（:35）/`_WIN_PATH_RE`（:36）— 绝对路径脱敏正则。
- `_STACK_FRAME_RE`（:37）— Python 风格栈帧剥离正则。
- `_SECRET_RE`（:41-44）— 键值式 secret 脱敏正则（access_token/token/key/bearer/password/authorization 等）。
- `DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY = 8`（:66）/ `DEFAULT_MAX_TOTAL_SUBSCRIBERS = 16`（:67）— T3 准入默认上限（生产由 Settings 覆盖）。
- `DEFAULT_SSE_QUEUE_ITEMS = 256`（:68）/ `DEFAULT_SSE_BUFFER_BYTES = 2MiB`（:69）/ `DEFAULT_SSE_MAX_FRAME_BYTES = 256KiB`（:70）— Subscriber 队列三默认。
- `IMMEDIATE`（:73-77）— 免防抖直推的 q/p 六类型 frozenset。
- `SESSION_EVENTS`（:80-82）— session.status/updated/deleted。
- `MESSAGE_EVENTS`（:88-90）— message.updated/appended（appended 仅为 wire 兼容保留）。
- `DEBOUNCE_SECONDS = 0.25`（:92）/ `HEARTBEAT_SECONDS = 10.0`（:93）/ `GRACE_SECONDS = 30.0`（:94）。
- `TOKEN_FRAME_TYPE = "token"`（:102）— L2-A 策划流 token 帧类型（仅 tokenstream 消费，放在本模块是共享叶子化选择）。

函数：
- `_sanitize_error_message(message, fallback_name)`（:47-61）— G1 脱敏：首行→Win 路径→Unix 路径→栈帧→secret→截 512；空/非 str 回退 name 或 "(no detail)"。
- `sse_frame(payload, event=None)`（:105-107）— 组 `event:`+`data: <json>\n\n` 帧字节。
- `_now_ms()`（:110-111）— epoch 毫秒。
- `_upstream_line_bytes(line)`（:114-133）— 流量计量：行字节数+1（补被 strip 的 LF）；空行=1；CRLF 会每行少计 1 字节（文档明示保守偏差）。
- `_extract_session_id(payload, props)`（:349-373）— sid 解析序：`props.sessionID`→`props.info.sessionID`→（仅 session.*）`props.info.id`；**刻意不回退 `payload.id`**（那是事件 id，误用会挂错 digest/sticky）。
- `normalize_session_status(value)`（:376-404）— str→原样；dict 且 `type` 为 str→取 type；其余（dict 无 str type / 非 dict 非 str）→None（该事件的 status 被忽略）。global_hub（digest 填充/G1 清除/token 镜像）与 tokenstream/hub.py:1048 共用同一实现。

数据类：
- `DigestFields`（:136-210）— 防抖窗内每 sid 聚合态。字段：`directory`(:154)、`status`(:155)、`message_id`(:156)、`updated_at: Any`(:157)（实际恒 int，见疑问 6）、`archived: int|None`(:158)、`deleted`(:159)、`last_error: Any=_UNSET`(:160)、`turn_incarnation`(:170)/`turn`(:171)（成对出现或成对省略）、`changed: list[str]|None`(:179)。方法：`to_payload(session_id)`（:181-210）— 非None条件拼装 digest payload；turn 两字段平铺在根级（ocdroid 平根解析约束，:199-205）；`changed` 同其他可选字段条件包含。
- `Subscriber`（:213-346，`eq=False`）— 单客户端出站队列 + T3 三守卫。字段：`queue_items/buffer_bytes/max_frame_bytes`(:235-237)、`id`(:240,"sub_"+hex4)、`queued_bytes`(:241)、`closed`(:242)、`dropped_frames`(:243)、`forced_disconnects`(:244)、`wire_v4`(:255,由 /events 路由在 subscribe 返回后立即置位)、`queue`(:258,post_init 建 maxsize 队列)。方法：
  - `__post_init__`（:260）— 建 asyncio.Queue。
  - `put(frame)`（:264-325）— 三守卫序：closed 静默丢；STOP 哨兵尽力入队（满→False）；超 max_frame_bytes→dropped++ 丢；队满或字节超限→**立即断连**（closed=True、forced_disconnects++、清队、v3 投 resync{subscriber_backpressure}+STOP / v4 仅 STOP，:316-320）；成功入队记账返回 True。
  - `ack(frame)`（:327-338）— 消费侧对称减 `queued_bytes`，STOP 不记账，floor 0。
  - `_clear_queue`（:340-346）— 清空队列并归零字节账。
- `SubscriberCapacityError(Exception)`（:407-419）— T3 准入超限异常；`__init__(code, *, limit, current)`（:415）；code ∈ {sse_subscriber_limit_directory, sse_subscriber_limit_total}（由 registry 抛出，不在本四文件内）。

#### 依赖 / 被依赖
- 依赖：`oc_slimapi.logging_config.get_logger`（:23，`logger` 定义 :27 但**本文件无直接使用**）；`.replay_wire.V4_RESYNC_REASONS`（:25，用于 put 的 v4 分支 :317）；三方 asyncio/dataclasses/contextlib/re/secrets/time/orjson。
- 被依赖（生产）：`sse/global_hub.py:26`（21 符号）、`sse/hub.py:18`（24 符号 shim）、`sse/registry.py:14`（10 符号）、`sse/tokenstream/hub.py:84`（TOKEN_FRAME_TYPE、normalize_session_status）。
- 被依赖（测试）：test_batch3_lifecycle:19（Subscriber）、test_b4_allowlist:20、test_turn_registry:36（DigestFields）、test_sse_replay_wire:1543（注释引用）。

#### 状态 / 可变性
- 模块级全部为不可变常量/哨兵/正则（编译期）+ 两个 object 哨兵；无模块级可变状态。
- `DigestFields` 可变 dataclass（全局 hub 在 publish/flush 中反复改写）；`Subscriber` 可变（队列 + 计数 + closed/wire_v4 标志）；`wire_v4` 设计为 subscribe() 返回后、无 await 间隙由路由置位（:250-255）以杜绝竞态。
- 无锁（单 loop 内联假设）。

#### 错误路径
- `Subscriber.put` 的全部失败出口：closed 丢 / STOP 入队满 / 超大帧丢 / 溢出强制断连（v4 STOP-only 降级基于 `reason not in V4_RESYNC_REASONS`，:317）。
- `_sanitize_error_message` 对 None/非 str 输入全兜底；脱敏链每步防御性（无正则异常面，模式预编译）。
- `_extract_session_id` / `normalize_session_status` 全 isinstance 守卫，无抛出面。

#### 疑问点
1. **两个 STOP 哨兵**：`hub_types.STOP`（:30）与 `tokenstream/frames.py:19 STOP` 是不同 object；`sse/token_hub.py` shim re-export 的是 tokenstream 的 STOP。events.py（:7 从 hub shim 导入）与 token_stream 路由各自比较自己的哨兵——跨流误比较会永不命中，需在 E2 路由层复核。
2. **`_sanitize_error_message` 覆盖面**：(a) JS 风格栈帧 `at foo (file:1:2)` 不被 `_STACK_FRAME_RE`（仅 `at \S+?:\d+`，:37）剥离（`\S+` 无法跨空格括号）——上游若是 JS 错误消息则路径/file:line 残留（路径部分会被 <path> 替换，但函数名+行号形状残留）；(b) `_SECRET_RE` 值字符类 `[A-Za-z0-9._\-/=+]+`（:43）不含 `~ : ; , !` 等，含这些字符的 secret 值只部分脱敏；(c) 截断 `[:512]`（:60）可能把 `<redacted>` 字面量切成 `<reda` 尾巴。
3. **过度脱敏**：`_UNIX_PATH_RE`（:35）会把消息里任何 `a/b.c` 形状的相对路径片段（如 "src/foo.py"）也替换为 `<path>`——审计消息可读性受损是否为接受的权衡（impl-spec §7 硬约束 4 只说 strip abs paths）。
4. **`normalize_session_status` 无枚举校验**（:398-399 任意 str 直通）：digest.status 的值域=上游值域未冻结；`"busy"` 语义清 sticky 依赖精确匹配（global_hub:720）。对象信封只读 `type` 键，信封内其他字段（若有 reason 等）被丢弃——上游 2026-08-19 实测形状之外的第三种形状（如 `{"status":"busy"}`）会归一为 None 被整体忽略。
5. **`IMMEDIATE` 六类型是否完整**（:73-77）：question.v2 / permission.v2 双轨并存说明上游在迁移期；若上游再加 v3 后缀类型，本表不感知即静默丢弃（与 global_hub 疑问 3 同源，无漂移检测）。
6. **`DigestFields.updated_at: Any`（:157）**：实际只被赋 int（`_bump_updated_at`）或保持 None；类型标 Any 过宽，`to_payload` 的 `updatedAt` 无类型保障。
7. **`queue: asyncio.Queue = field(default=None)`（:258）**：注解非 Optional 却默认 None（post-init 惰性建），类型不严谨。
8. **`put` 的 QueueFull 竞争注释（:297-300）**：先查 `qsize()<queue_items` 再 put_nowait，满时落入溢出路径=立即断连——若真有并发生产者（注释称实际没有），一次偶发满员即断连过激。
9. **v4 STOP-only 路径 STOP 丢失**（:318-319）：断连分支里 queue 已满时 `put_nowait(STOP)` 被 suppress → 订阅者 closed 但 SSE 生成器收不到 STOP，连接是否靠生成器侧 closed/断线检测收尾需在 routes/events.py 复核（E2 范围）。
10. **`ack` 不配对风险**（:327-338）：只减不增、floor 0；若生成器取帧后不 ack（如异常路径），`queued_bytes` 虚高导致提前触发背压断连——生成器是否全路径 ack 需 E2 复核。
11. **`_extract_session_id` 的 `info.id` 兜底仅限 `session.*`**（:366-372）：message.* 事件若上游只带 `info.id`（消息 id）而无 sessionID，会返回 None → 整事件丢弃（global_hub:786-787）——上游 message.updated 是否恒带 sessionID 需对照 message-v2.ts。
12. **模块内 `logger` 死变量**（:27）：定义后本文件无任何调用。
13. `TOKEN_FRAME_TYPE`（:102）与 hub 控制面无关，放在 hub_types 仅因叶子共享；归属略错位（风格问题）。
14. `Subscriber` 无显式 `close()`/终结协议：closed 置位后队列残留（溢出路径已清，但 STOP 正常终结路径不清）——`queued_bytes` 残值无回收方。

---

### src/oc_slimapi/sse/hub.py（42 行）

#### 职责
**纯 re-export 兼容 shim**（非实现）：原单体 hub 拆分为 hub_types / global_hub / registry 三模块后，本模块保持 `from oc_slimapi.sse.hub import X` 旧导入路径不变。自身零逻辑（仅 import 三方 26 个符号）。

#### 对外符号（完整）
- 来自 `.global_hub`（:17）：`GlobalHub`、`_LAST_UPDATED_AT_BY_SID_MAX`（下划线私有符号被 re-export）。
- 来自 `.hub_types`（:18-41）：`ABORT_NAME`、`DEFAULT_MAX_SUBSCRIBERS_PER_DIRECTORY`、`DEFAULT_MAX_TOTAL_SUBSCRIBERS`、`DEFAULT_SSE_BUFFER_BYTES`、`DEFAULT_SSE_MAX_FRAME_BYTES`、`DEFAULT_SSE_QUEUE_ITEMS`、`DEBOUNCE_SECONDS`、`DigestFields`、`GRACE_SECONDS`、`HEARTBEAT_SECONDS`、`IMMEDIATE`、`MESSAGE_EVENTS`、`SESSION_EVENTS`、`STOP`、`Subscriber`、`SubscriberCapacityError`、`_UNSET`、`_extract_session_id`、`_now_ms`、`_sanitize_error_message`、`_upstream_line_bytes`、`sse_frame`（22 个）。
- 来自 `.registry`（:42）：`HubRegistry`。

#### 依赖 / 被依赖
- 依赖：global_hub / hub_types / registry 三实现模块。
- 被依赖（生产，rg 实证）：`routes/events.py:7`（STOP、SubscriberCapacityError、sse_frame）；`app.py:33`（HubRegistry）。即 **shim 有真实生产使用者，非死代码**。
- 被依赖（测试，大量）：test_hub.py:28（及 954-1007 的 `_sanitize_error_message`、1428/1520 的 `DigestFields`）、test_token_hub_lifecycle:24、test_dbaux_metrics:18、test_token_stream_route:40、test_metrics_replay_block:30、test_globalhub_retired_gate:20、test_token_hub:31/669/683/694、test_traffic_integration:50、test_turn_registry:35、test_b1a_digest_changed:34、test_messages_routes:34、test_sse_replay_wire:58、test_upstream_error_boundary:38、test_command_routes:21、test_access_log_v3_fields:291、test_traffic_sse:29、test_agent_routes:28、test_hub_behavior_lock:55、test_events_tokens:43、test_v3_sse_meta:43、test_sse_logging:16、test_traffic_upin_gaps:35、test_metrics:23、test_sessions_coalesce:32、test_etag:41、test_messages_coalesce:41、test_session_status_object_format:33 等（≈25 个测试文件仍走 shim）。

#### 状态 / 可变性
- 无状态（纯转发；`from __future__ import annotations` + 三组 import）。

#### 错误路径
- 无自身错误路径；符号缺失会在 import 期 AttributeError（re-export 名单与实现模块 drift 时测试即崩，属可接受的显性失败）。

#### 疑问点
1. **结论：纯 shim，且有生产使用者**（routes/events.py、app.py）——不可删；但生产侧仅用 4 个符号（STOP/SubscriberCapacityError/sse_frame/HubRegistry），其余 22 个 re-export 主要服务测试兼容。
2. `_LAST_UPDATED_AT_BY_SID_MAX` 被 re-export（:17）但 rg 显示**无任何外部代码经 shim 使用它**（tests 直接从 global_hub 导入，test_batch3_lifecycle:1138）——疑似多余行。
3. 私有下划线符号（`_UNSET`/`_now_ms`/`_sanitize_error_message`/`_upstream_line_bytes`/`_extract_session_id`/`_LAST_UPDATED_AT_BY_SID_MAX`）经 shim 固化为事实公共 API（测试大量依赖），未来收窄面困难。
4. `sse/tokenstream/frames.py:25-36` 为避 import 环路**复制**了 `sse_frame`/`_now_ms` 而非复用 hub_types——本可 import hub_types（hub_types 是叶子、无环），复制理由（"hub.py re-export 会成环"）针对的是 hub.py 而非 hub_types.py，注释与实际可选方案有出入（需要 E 组其他卡片确认 frames.py 为何不复用 hub_types）。

---

### src/oc_slimapi/sse/token_hub.py（23 行）

#### 职责
**纯 re-export 兼容 shim**：token 流实现已物理迁移至 `oc_slimapi/sse/tokenstream/` 包（hub.py/subscriber.py/frames.py），本模块保持 `from oc_slimapi.sse.token_hub import ...` 旧路径可用。自身零逻辑。

#### 对外符号（完整）
来自 `.tokenstream`（src/oc_slimapi/sse/token_hub.py:5-23，共 18 个）：`STOP`（tokenstream 自有哨兵，≠hub_types.STOP）、`DeltaAccumulator`、`LivePart`、`PartKey`、`TokenStreamHub`、`TokenStreamRegistry`、`TokenSubscriber`、`TokenSubscriberCapacityError`、`_TokenMetrics`、`_connected_frame`、`_delta_frame`、`_heartbeat_frame`、`_now_ms`、`_resync_frame`、`_snapshot_frame`、`_truncated_frame`、`sse_frame`（均为 `# noqa: F401` 转发）。

#### 依赖 / 被依赖
- 依赖：`.tokenstream` 包（真实实现，`tokenstream/__init__.py:7` 导出 TokenStreamHub）。
- 被依赖（生产，rg 实证）：`app.py:36`（TokenStreamHub、TokenStreamRegistry——lifespan 构造）；`routes/token_stream.py:61`（大量符号）；`sse/global_hub.py:55`（TYPE_CHECKING 下 `from .token_hub import TokenStreamHub`——**类型引用也走 shim**）。即 **shim 有真实生产使用者，非死代码**。
- 被依赖（测试）：test_token_hub_lifecycle:25（含 `_now_ms`）、test_token_hub_flush:49、test_v3_sse_meta:44（STOP as TOKEN_STOP、sse_frame——与 hub 侧 STOP 并排导入证实双哨兵）、test_token_stream_route:41、test_events_tokens:44、test_sse_replay_wire:77、test_token_hub:32。
- 对照：`sse/registry.py` 的 TYPE_CHECKING **直连** `from .tokenstream import TokenStreamHub`（不走 shim）——同一语义两条导入路径并存。

#### 状态 / 可变性
- 无状态（纯转发）。

#### 错误路径
- 无自身错误路径；实现符号 drift 在 import 期显性失败。

#### 疑问点
1. **结论：纯 shim，且有生产使用者**（app.py、routes/token_stream.py、global_hub.py 的类型引用）——不可删。
2. **双哨兵风险再确认**：本 shim 的 `STOP`/`sse_frame`/`_now_ms` 来自 tokenstream/frames.py 的**复制实现**（frames.py:19,:25-36），与 hub_types 同名符号是不同对象/不同函数对象——任何跨两套体系的比较或单例假设都是隐患（test_v3_sse_meta:43-44 同时导入两者佐证已知此分叉）。
3. **导入路径不一致**：global_hub 类型引用走 shim（:55），registry 类型引用直连 tokenstream——建议统一（风格/漂移面）。
4. 私有符号（`_TokenMetrics`、`_*_frame`、`_now_ms` 等 9 个下划线符号）经 shim 固化为测试可达 API。
5. shim 存在使 `token_hub`（旧名）与 `tokenstream`（新名）双名并存；若无退役计划，长期漂移风险=两套入口任一改名即断（可接受但应有 retire 时间表——未见文档）。

---

## 汇总备注（跨文件）
- 四文件零锁设计的前提=「publish/flush/heartbeat 全部内联同一事件循环」（hub_types.py:297-299 注释自证）；任何未来把 publish 移到线程/其他 loop 的改动都会引入数据竞争。
- 帧分类全集（IMMEDIATE 6 + SESSION 3 + MESSAGE 2 + session.error + token 4 = 16 类型 + catch-all 丢弃）是本 sidecar 策展边界的单一事实源；与 `docs/specs/v3-contract.md` §7 的一致性、以及与 opencode v1.18.18 GlobalBus 实际发射集的对齐，是本次审计应在校验阶段完成的外部对照项（本卡片仅记录代码事实）。
