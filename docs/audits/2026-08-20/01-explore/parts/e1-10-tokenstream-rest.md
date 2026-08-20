# E1-10 精读卡片 — tokenstream subscriber / sse registry / frames / models

> 审计探索产物（2026-08-20）。只读精读，引用格式 `src/oc_slimapi/...:行号`。四个文件均全文精读（未抽样）。

---

### src/oc_slimapi/sse/tokenstream/subscriber.py（874 行）

- **职责**：token stream 的消费者侧三件套：(1) `_SubscriberQueue` — 把 handshake 预填充（有界 deque）与 runtime T3 背压（有界 asyncio.Queue）物理分离（rev-ogpt CRITICAL 3）；(2) `TokenSubscriber` — 单 session 出站队列，T3 三段守卫（closed → oversized-drop → overflow-disconnect）（design §5.5/§5.6/§16-D）；(3) `TokenStreamRegistry` — token 订阅独立准入账本（不占 `MAX_TOTAL_SUBSCRIBERS`）+ flush loop 首挂/尾卸生命周期 + events-token tap 账本（L2-A）。

- **对外符号**（逐个）：
  - `_SubscriberQueue`（:58）
    - `__slots__`（:78-86）：`_runtime/_handshake/_handshake_max_items/_handshake_max_bytes/runtime_bytes/handshake_bytes/last_get_handshake`。
    - `__init__(*, runtime_max_items, handshake_max_items, handshake_max_bytes)`（:88）：runtime `asyncio.Queue(maxsize=…)`（:99，" defence-in-depth"，正常路径 caller 先检 `qsize()`，QueueFull 永不触发）；handshake `deque()`（:100）；双字节账本（:104-105）；`last_get_handshake` 单槽切换（:111，get→ack 路由用）。
    - `put_handshake(frame) -> bool`（:116）：**fail-on-overflow**（非 drop-oldest）——条目数或字节数超帽即返回 False（:133-137），落地则 append + `handshake_bytes += size`（:138-139）。
    - `put_runtime(frame)`（:145）：`put_nowait`，非 STOP 计入 `runtime_bytes`（:155-157）；不复查上限（caller 责任）。
    - `clear_runtime()`（:159）：清空 runtime 队列 + `runtime_bytes = 0`（:167-172）；**不动 handshake**。
    - `put_runtime_terminal(frame)`（:174）：终态帧（resync/STOP）入队但不计账（"backlog cleared, terminal pair sealed outside the budget"）。
    - `ack_runtime(frame)`（:186）/ `ack_handshake(frame)`（:192）：floor-0 字节减记，STOP no-op。
    - `qsize()`（:201）：**仅 runtime 深度**（handshake 有意不计入 T3 item 帽，:202-209）；`handshake_qsize()`（:212）诊断面；`empty()`（:216）双侧。
    - `get()` async（:219）/ `get_nowait()`（:227）：handshake 先排干（"handshake drains first"），并置 `last_get_handshake`。
  - `@dataclass(eq=False) TokenSubscriber`（:235）
    - 字段：`session_id`（:289）、`metrics: _TokenMetrics`（:290）、`queue_items=64`（:291）、`buffer_bytes=512KiB`（:292）、`max_frame_bytes=DEFAULT_TOKEN_MAX_FRAME_BYTES`（:293, 1MiB）、`handshake_items=TOKEN_HANDSHAKE_ITEMS`（:302, 2048）、`handshake_buffer_bytes=TOKEN_HANDSHAKE_BUFFER_BYTES`（:303, 8MiB）、`id="tok_"+hex4`（:305）、`closed`（:306）、`_handshake_overflow`（:310, MINOR 1 错误码区分）、`dropped_frames`（:311）、`forced_disconnects`（:312）、`wire_v4`（:323, B3b-2）、`queue`（:325）、`_in_handshake`（:331）。
    - `__post_init__`（:333）：惰性建 `_SubscriberQueue`。
    - `queued_bytes` property（:341）：`runtime_bytes + handshake_bytes`（与控制面 Subscriber 可变字段同名 duck-type 兼容；T3 字节检查直读 `queue.runtime_bytes`，handshake 字节不计入 runtime 预算）。
    - `begin_handshake()`（:354）/ `end_handshake()`（:358）：切 `_in_handshake`。
    - `put(frame) -> bool`（:362）：T3 路由 — `closed` → 静默丢弃（:386-390）；`STOP` → 恒走 runtime（:391-396）；oversized（`len > max_frame_bytes`）→ drop + `dropped_frames+1` + `metrics.dropped_frames_total+1`，**不闭连接**（:397-406）；handshake 模式 → `put_handshake` 失败则 `closed=True` + `_handshake_overflow=True` + 双计数（:407-423）；runtime 守卫 `qsize() < queue_items and runtime_bytes + size <= buffer_bytes`（:428-431）；溢出 → `closed=True` + `forced_disconnects+1` + `metrics.dropped_frames_total+1` + `clear_runtime()`（:439-442），v4 且 reason 不在 `V4_RESYNC_REASONS` → 仅 STOP（:450-452），v3 → `_resync_frame(sid,"subscriber_backpressure")` + STOP（:453-457）。
    - `terminate(reason)`（:460）：INV-4 服务端终止（session.deleted）——closed + clear_runtime +（reason 在 v4 域内或 v3 才发）resync + STOP（:487-493）；**不** bump forced_disconnects/dropped；不摘除 fanout（留给 generator finally → unsubscribe）。
    - `ack(frame)`（:495）：按 `last_get_handshake` 路由到对应账本；STOP no-op（:507-512）。
  - `TokenSubscriberCapacityError(Exception)`（:515）：`code`（`sse_token_subscriber_limit` / `sse_token_handshake_overflow`）、`limit/current/buffer_bytes`（:528-533）。
  - `TokenStreamRegistry`（:536）
    - `__init__(token_hub, hub_registry, *, max_subscribers, queue_items, buffer_bytes, max_frame_bytes)`（:554）：`total_subscribers/rejected_total`（:570-571）、`events_tokens: set[Any]`（:582, L2-A）。
    - `attach_events_subscriber(sub)`（:584）：幂等集合去重（:600-601）+ `token_hub.events_tap.append(sub.put)`（:603）+ `token_hub.start()`（:606）。
    - `detach_events_subscriber(sub)`（:608）：discard + `events_tap.remove(sub.put)`（suppres ValueError, :617-619）+ 双账本皆空才 `th.stop()`（:620-621）+ `maybe_arm_grace_if_idle()`（:622-623）。
    - `subscribe(sid, wire_v4=False) -> TokenSubscriber`（:625）：容量检查（:668-674）→ 早建 sub（无副作用, :679-685）→ 先盖 `wire_v4` 章（:689）→ try：`hub_registry.get_global()` + `cancel_pending_removal()` + `ensure_upstream()`（:696-699）+ `token_hub.start()`（:701）+ `attach_subscriber`（:705）；`except asyncio.CancelledError: raise`（:706-707）；`except Exception:` rollback + `rejected_total+1` + raise（:708-711）；post-attach `sub.closed` 复查（:725）→ `_rollback_failed_attach` + 抛 `TokenSubscriberCapacityError`（MINOR 1 按 `_handshake_overflow` 选码, :732-742）；成功才 `total_subscribers += 1`（:743）。
    - `_rollback_failed_attach(sid, sub)`（:746）：防御性 detach（:772-773）+ 双账本空才 `th.stop()`（:781-782）+ 对称重挂 grace（:786-787）。
    - `unsubscribe(sub)`（:789）：**成员守卫真幂等**（NB-D1, :821-822 `if not th.has_subscriber(...): return`）→ detach + 减记 + floor 0（:823-827）→ 双账本空才 stop（:831-832）→ `maybe_arm_grace_if_idle()`（:835-836）。
    - `snapshot_token_metrics()`（:838）：读 `th._metrics/th._pending/_subs_by_sid` 私有；`maxSubscriberQueueDepth` 只算 runtime 深度、只算 fanout 内 sub（:852-857）；输出 `sse.tokenStream.*` 14 键（:858-874）。

- **依赖 / 被依赖**（rg 反查）：
  - 依赖：`config`（TOKEN_HANDSHAKE_ITEMS=2048/BUFFER_BYTES=8MiB/DEFAULT_TOKEN_MAX_FRAME_BYTES=1MiB, config.py:81/100-101 + 静态断言 :153-163）；`.frames`（STOP, _resync_frame）；`..replay_wire.V4_RESYNC_REASONS`（replay_wire.py:72-77，四值冻结域）；`.models._TokenMetrics`；`..hub.HubRegistry`（TYPE_CHECKING, :23-24）。运行期通过 `token_hub`（TokenStreamHub）与 `hub_registry`（HubRegistry）协作：`attach_subscriber/has_subscriber/detach_subscriber/start/stop/events_tap`（tokenstream/hub.py:1211/1355/1340/421/478/298），`get_global/cancel_pending_removal/maybe_arm_grace_if_idle`（registry.py:143/146/161）。
  - 被依赖：`sse/tokenstream/__init__.py:8` 再导出；`sse/token_hub.py:11-13` 兼容 shim；`routes/token_stream.py:63,172-182`（subscribe + 503 映射）；`routes/events.py:162,237`（attach/detach_events_subscriber）；`routes/metrics.py:31`（snapshot_token_metrics）；`app.py:560-567`（构造，参数来自 settings.token_stream_*）；测试 `tests/test_token_subscriber_overflow.py`（874 行级专项）、`test_token_stream_route.py`、`test_events_tokens.py`、`test_batch3_lifecycle.py`、`test_sse_replay_wire.py`、`test_token_hub_flush.py`。

- **状态 / 可变性**：
  - 每 sub：handshake deque（一次性，attach 同步段内填充）+ runtime bounded Queue（64 item / 512KiB 默认）+ 双字节账本 + `last_get_handshake` 单槽 + `closed/_in_handshake/_handshake_overflow/wire_v4` 布尔 + `dropped_frames/forced_disconnects` 计数。全部单事件循环内同步访问，无锁（admission 关键段无 await，:637-645 声明）。
  - Registry：`total_subscribers/rejected_total` int、`events_tokens` set（identity 去重，控制面 `Subscriber` 为 `@dataclass(eq=False)` hub_types.py:213 可哈希）。本模块**不持有 task**（flush task 在 TokenStreamHub `_flush_task`，tokenstream/hub.py:302/439）。
  - 消费者（routes/token_stream.py:293-302）`await queue.get()` → STOP 则 break → `finally: registry.unsubscribe(subscriber)`（:303-307）。慢客户端背压链路：ASGI send 阻塞 → generator 停在 yield → runtime 队列涨 → put 溢出 → 立即 clear_runtime + resync+STOP → generator 排干 handshake 后取 STOP 断开。**公平性**：put 全为 put_nowait（永不阻塞 flush loop），单慢 sub 只影响自己（每 sub 独立队列），无队头阻塞；heartbeat 每 15s（tokenstream/hub.py:1529-1536）保证 get() 不会永久饿死。

- **错误路径**：容量满 → `sse_token_subscriber_limit`（503+Retry-After:5, routes/token_stream.py:177-181）；handshake 溢出 → `sse_token_handshake_overflow`（含 bufferBytes）；attach 任意异常 → rollback 后原样 raise（INV-3）；oversized 帧 → 丢弃不闭连（C6 由 hub 出 truncated 替代帧）；runtime 溢出 → v3 `resync{subscriber_backpressure, sessionID}`+STOP / v4 STOP-only（rev-gate R3 BLOCKER-1）；`session.deleted` → `terminate("session_deleted")`（hub.py:1131-1132 调用）。

- **疑问点（13）**：
  1. **:397-406 vs :651-653/:729-736 文档失真**：`put()` 的 oversized 分支只 drop 不置 `closed`，但 `subscribe()` docstring（:651-653）与 MINOR 1 注释（:729-731 "handshake buffer / oversized-frame guard"）声称 oversized 守卫可导致 attach 带 `closed=True` 回来并映射 `sse_token_handshake_overflow`。按现行代码 oversized 永不闭 sub；若未来某路径在 handshake 期闭 sub 而未置 `_handshake_overflow`，错误码会回落到语义错误的 `sse_token_subscriber_limit`（容量码，实际失败是帧尺寸）。
  2. **:440-441 指标口径**：runtime 溢出 `metrics.dropped_frames_total += 1` 且 `dropped_frames`（per-sub）不加，但 `clear_runtime()` 实际丢弃可达 64 帧/512KiB —— 指标计的是"断连事件数"而非"丢帧数"；与 oversized 路径（:404 同时 bump per-sub）不一致；`snapshot_token_metrics` 未暴露 per-client 计数，仅总量。
  3. **:449-457 v4 溢出可观测性**：v4 断连只有 STOP（无 resync reason），客户端无法区分 backpressure / 服务端 close / `session_deleted`（:489-493 后者在 v4 同样静默）。设计上以"断连即信号 + Last-Event-ID 重连"恢复，但对排障仅剩 metrics。
  4. **:303 + config.py:90-94 握手字节帽可行性**：8MiB 帽 vs 32×近1MiB snapshot + JSON 转义放大（config 注释自认 "may be insufficient"）→ 合法大状态也可能 503 重试循环（客户端 Retry-After:5 无限重试同一 sid）。确认产品接受度 / 是否需要按 sid 降级策略。
  5. **:694-711 CancelledError 路径无 rollback**：`except asyncio.CancelledError: raise`（:706-707）跳过 `_rollback_failed_attach` —— 若 `ensure_upstream/start/attach` 段内出现 await 点（当前全同步，但 `ensure_upstream` 内部或未来改动引入 await）后被 cancel，已做的副作用（grace 取消、flush 启动）不回滚。当前无 await 时 CancelledError 只能来自更外层，仍会泄漏已 start 的 flush loop（由 `_rollback_failed_attach` 覆盖的路径不走）。
  6. **:19 vs hub_types.py:30 双 STOP 哨兵**：tokenstream `STOP` 与控制面 `STOP` 是不同 `object()`；若误把控制面 STOP 喂给 `TokenSubscriber.put`，`frame is STOP` 为 False → `len(frame)` 对 `object()` 抛 TypeError。当前无跨用（rg 验证），但无类型防呆。
  7. **:109-111 `last_get_handshake` 单槽假设**：仅当"单消费者且 get→ack 严格成对"成立才正确；route generator 满足，但任何 `get()` 后不 `ack()` 或乱序 ack 会使账本减记路由错侧（floor-0 防负数，不防漂高）。
  8. **:227-232 `get_nowait()` 空队抛 QueueEmpty**：runtime 侧直接透传 `get_nowait`；当前消费面只用 async `get()`（routes/token_stream.py:294），`empty()`（:216）有但无人用 —— 若未来用 get_nowait 轮询需自catch。
  9. **:710 `rejected_total` 语义混合**：容量拒绝（:669）、attach 异常（:710）、handshake 溢出（:727）共用一个计数器，metrics 无法区分原因；且 handshake 溢出时 `limit/max_subscribers` 字段报的是与失败无关的容量帽（:739-740）。
  10. **:582-606 events_tokens 泄漏面**：强引用控制面 Subscriber + `events_tap` 里的 bound method；清理完全依赖 events 路由 generator finally（routes/events.py:237）。若 generator 从未启动（Starlette 取消响应体前不再迭代），`events_tokens`/`events_tap` 永不清 → flush loop 永不停（100ms 空转）+ GlobalHub grace 永不挂。依赖框架 aclose 语义，本仓无兜底超时。
  11. **:852-857 深度规口径**：`maxSubscriberQueueDepth` 只看 `_subs_by_sid`（per-session sub）的 runtime 深度；events-token 消费者（走控制面自己的 queue）与其 flush 压力不在该 gauge 内，而 `has_consumers()`（tokenstream/hub.py:395）却覆盖 taps —— 观测谓词与存活谓词不对称。
  12. **:838-874 层级穿透**：registry 直读 `th._metrics/_pending` 私有属性（:849-853,862-867），hub 已有同名 public property（flushed_frames_total 等, tokenstream/hub.py:402-416）—— 混用公私接口，形状已冻结但实现耦合。
  13. **:325 `queue: _SubscriberQueue = field(default=None)`**：注解非 Optional 却默认 None；且 `queued_bytes` property（:341）与控制面 Subscriber 的可变字段（hub_types.py:241）同名 —— duck-type 读 OK，任何 `sub.queued_bytes += x` 写法（控制面测试风格）会 AttributeError。测试通过 monkeypatch `_SubscriberQueue.__init__` 缩帽（test_token_stream_route.py:999-1011），说明该构造缝是被依赖的测试面。

---

### src/oc_slimapi/sse/registry.py（408 行）

- **职责**：`HubRegistry` — 进程唯一 `GlobalHub` 的持有者 + 控制面（curated SSE）T3 准入（per-directory/total 双帽）+ 空闲 grace 拆除编排（`GRACE_SECONDS=30`, hub_types.py:94）+ 对 token hub / turn registry / replay log / TransformPool 的接线板（set_* 转发到惰性创建的 hub）。

- **对外符号**：
  - `HubRegistry.__init__(client, *, max_subscribers_per_directory, max_total_subscribers, queue_items, buffer_bytes, max_frame_bytes, traffic_ledger=None)`（:50-84）：`_global`、双帽、`total_subscribers/rejected_total`（:69-70）、`_transforms`（:71）、`_token_hub`（:75）、`_turn_registry`（:79）、`_replay_log`（:83）、`_removal_task`（:84）。
  - `set_transforms(pool)`（:86）：仅 metrics 引用。
  - `set_token_hub(token_hub|None)`（:94）：写入自身 + 活跃 hub 的 `_token_hub`（:100-102 私有直写）。
  - `set_turn_registry(registry|None)`（:104）：同型转发（:111-113）。
  - `set_replay_log(replay_log|None)`（:115）：经 hub 的 `set_replay_log` 转发（:126-127）。
  - `get(directory=None) -> GlobalHub`（:129）：惰性建 hub 并转发全部接线（:130-140）；**directory 被忽略**（:37-40 兼容声明）。
  - `get_global()`（:143）：`get(None)`。
  - `cancel_pending_removal()`（:146）：cancel `_removal_task` + 置 None，幂等（NB-B1 —— token subscribe 在 grace 窗口内到达时不被拆）。
  - `maybe_arm_grace_if_idle()`（:161）：hub 存在 && `hub.has_consumers()` 为 False && 未在挂 → `create_task(_remove_hub_after_grace)`（:185）。`has_consumers` 跨两账本（global_hub.py:362-381：控制面 subscribers ∪ `_token_hub.subscriber_count > 0`）。
  - `subscribe(wire_v4=False) -> Subscriber`（:187）：单同步关键段——per-directory 帽（:212-218, `sse_subscriber_limit_directory`）→ total 帽（:219-225, `sse_subscriber_limit_total`）→ `hub.subscribe(welcome=not wire_v4)`（:226, v4 抑制 server.connected）→ 盖 `wire_v4`（:227）→ 增记（:228）。**有意不 cancel `_removal_task`**（:203-207 注释：保持同步性，靠 grace task 醒后 `has_consumers()` 复查自 abort）。
  - `unsubscribe(subscriber)`（:232）：幂等（:240-241 成员守卫）→ discard + 减记 + floor 0（:242-247）→ `maybe_arm_grace_if_idle()`（:256, NB-D3 双账本谓词）。
  - `_remove_hub_after_grace(hub)` async（:258）：sleep 30s（:284）→ 复查 `hub is self._global and not hub.has_consumers()`（:287）→ cancel hub 4 task（:295-300）→ `gather(..., return_exceptions=True)`（:303-305, INV-2 严格串行 epoch）→ 复查（:314）→ 同步段：`token_hub.on_upstream_reconnect()`（:322-323, 清旧 epoch ingest 态；`_part_revisions/_removed_messages` 保留）→ `self._global = None; _removal_task = None`（:324-325）。
  - `snapshot_metrics()`（:327）：冻结形状 `sse.subscribers{current,limit,rejectedTotal}/hubs[...]/clients[...]` + `skeleton`（:344-370）。
  - `_snapshot_skeleton()`（:372）：TransformPool 公共 API；`cacheEnabled` 硬编码 False。
  - `close()` async（:389）：hub 4 task + `_removal_task` 一并 cancel + gather（:396-406）→ `_global = None`、`total_subscribers = 0`（:407-408）。

- **依赖 / 被依赖**（rg 反查）：
  - 依赖：`global_hub.GlobalHub`、`hub_types`（常量/Subscriber/SubscriberCapacityError/STOP/GRACE_SECONDS）；TYPE_CHECKING httpx/TrafficLedger/TurnRegistry/TokenStreamHub。
  - 被依赖：`app.py`（构造 `app.state.hubs`、lifespan 内 `set_token_hub/set_turn_registry/set_replay_log/set_transforms`、shutdown `await app.state.hubs.close()` app.py:497）；`routes/events.py:245`（unsubscribe）与 subscribe 入口；`routes/metrics.py:22`（snapshot_metrics）；`tokenstream/subscriber.py`（反向：token registry 持 hub_registry 引用，调 get_global/cancel_pending_removal/maybe_arm_grace_if_idle）；`sse/hub.py` 再导出；测试十余个文件（test_hub*.py、test_batch3_lifecycle.py、test_sse_replay_wire.py 等）。

- **状态 / 可变性**：
  - `_global`：单例强引用；grace 拆除与 close 置 None（GC 释放 hub + task 句柄，:260-264 注释）。
  - `_removal_task`：本文件唯一 `asyncio.create_task`（:185）；被 `cancel_pending_removal`（:157-159）与 `close`（:400-402）cancel+置 None。
  - 准入关键段（:209-229）无 await —— 协作调度下无 over-admit；`unsubscribe` 幂等防负数。
  - 全库 `create_task/ensure_future` 清点（本卡范围内逐点）：registry.py:185（如上，异常路径见疑问 1）；global_hub.py:238-240（run/flush/heartbeat，hub 自持）、:360（stop_after_grace → `hub.stop_task`）；tokenstream/hub.py:439（flush loop + done_callback 看门狗 :443-476，异常死亡自动重建）。范围外：traffic_snapshot.py:372、actions.py:648/666/669、qp_sweep.py:225、routes/permissions.py:461、routes/questions.py:439、app.py:448/675、dbaux/lifecycle.py:378（各文件自管）。

- **错误路径**：`subscribe` 双帽 raise `SubscriberCapacityError`（events 路由转 503+Retry-After）；`unsubscribe` hub 缺失/非成员 no-op；`_remove_hub_after_grace` sleep 期被 cancel → 直接 return（:285-286）；gather 期被 cancel → return 不置空（:306-311，依赖 canceller 已置 None）；`close` gather 吞异常（teardown 可接受）。

- **疑问点（9）**：
  1. **:283-325 `_remove_hub_after_grace` 无 `except Exception` 兜底**：sleep 后的拆除体（尤其 :322-323 `on_upstream_reconnect()`）若抛异常，task 带 exception 死亡 → "Task exception was never retrieved" 警告 + `_removal_task` 残留非 None → `maybe_arm_grace_if_idle` 的 `if self._removal_task is not None: return`（:183-184）**永久失效**，且 `_global` 未置空（连接泄漏恰是它要修的 B-D1）。CancelledError 有处理，普通异常没有。
  2. **:203-207 靠复查而非取消的窗口**：`subscribe` 不 cancel grace task；若 grace task 恰在 :303 `await gather` 期间被新订阅"复活"——复查 :314 会 abandon，正确；但若订阅发生在 :287 复查之后、cancel 循环（:295-300, 同步不可插入）之前——不可能。唯一漏洞是 hub task 被 cancel 后 `hub.subscribe→ensure_upstream` 是否能复活已 cancel 的 task（属 global_hub 卡范围，此处挂链接待 E-05 核对）。
  3. **:212-218 "directory" 帽名存实亡**：单一全局 hub（:37-40 自认 directory ignored），`max_subscribers_per_directory` 实为第二个全局帽，错误码 `sse_subscriber_limit_directory` 对客户端传达错误语义；两帽作用于同一集合，前者恒 ≤ 后者时后者永不触发（默认值需核对 hub_types）。
  4. **:226 `hub.subscribe` 半途异常的账本漂移**：若 GlobalHub.subscribe 内部先 `subscribers.add` 后抛（未读其实现，E-05 核对），`total_subscribers` 不增而 hub 集合已有成员 → admission 永久少记一个；本文件假设 hub.subscribe 原子。
  5. **:296 hub 4 task 清单硬编码**：`(hub.task, hub.flush_task, hub.heartbeat_task, hub.stop_task)` 与 `close()`（:397）重复列举 —— GlobalHub 新增第 5 个 task 时两处都要改，易漏（无单一 source of truth）。
  6. **:389-408 `close()` 不清 token 侧账本**：`total_subscribers=0` 只救控制面；`TokenStreamRegistry.total_subscribers`、`events_tokens`、token hub 状态不归它管（app.py:543-551 用 ExitStack LIFO 先 `token_hub.stop()` 再 `hubs.close()`），但 token registry 的 `total_subscribers` 若此刻 >0（生成器 finally 未跑完）则残留 —— 进程关闭场景无害，测试复用 app 时可能。
  7. **:322-323 命名误导**：grace 拆除调用 `on_upstream_reconnect()`（实则"epoch 重置/清态"语义）；虽注释明确，函数名暗示的"重连 fanout resync"行为在 `has_consumers()==False` 下是 no-op —— 未来读者易误解其在有消费者时的副作用。
  8. **:185 task 未命名**：`asyncio.create_task(self._remove_hub_after_grace(hub))` 无 `name=`，与 qp_sweep（name="qp-sweep-shadow"）风格不一，排障时难从 task 列表辨认。
  9. **:341-358 `snapshot_metrics` 与 token 块拼装层级**：本文件产出控制面形状，metrics 路由再补 `sse.tokenStream`（metrics.py:31）——`clients[]` 只含控制面 sub，token sub 的 per-client 计数（dropped/forced_disconnects）无处暴露（对照 tokenstream snapshot 只有聚合值）。

---

### src/oc_slimapi/sse/tokenstream/frames.py（152 行）

- **职责**：token stream 的 wire 帧构造器（design §5.6）——snapshot / delta / truncated / resync / server.connected / server.heartbeat / message.removed 七种帧 + `STOP` 哨兵 + `PartKey` 类型别名 + SSE 序列化底座 `sse_frame`。

- **对外符号**：
  - `PartKey = tuple[str, str, str]`（:13）：(sessionID, messageID, partID)。
  - `STOP = object()`（:19）：runtime 终态哨兵（"kept local to avoid a runtime import cycle"）。
  - `_now_ms()`（:22）：epoch 毫秒（防 import 环有意复制自 hub）。
  - `sse_frame(payload, event=None) -> bytes`（:33）：`event: <name>\n`（可选）+ `data: <orjson>\n\n`（:42-43）。
  - `_snapshot_frame(key, text, done, part_revision=None)`（:53）：payload 固定序 `sessionID/messageID/partID/done`（:57-62）+ 可选 `text`（:63-64）+ 可选 `partEventRevision`（:65-81, rev-ogpt CRITICAL 1 Option B **per-frame** 严格递增）；event `message.part.snapshot`。
  - `_delta_frame(key, text, part_revision=None)`（:85）：`sessionID/messageID/partID/text` + 可选 revision（:88-99）；event `message.part.delta`。
  - `_truncated_frame(key, done, part_revision=None)`（:103）：`…/truncated:true/done` + 可选 revision（:107-121）；event 复用 `message.part.snapshot`（:122）。
  - `_resync_frame(sid, reason)`（:125）：`{"reason": reason, "sessionID": sid}`（**reason 在前**）；event `resync`。
  - `_connected_frame(sid)`（:129）：`{"sessionID"}`；event `server.connected`。
  - `_heartbeat_frame()`（:133）：`{}`；event `server.heartbeat`。
  - `_message_removed_frame(sid, mid)`（:137）：`{"sessionID","messageID"}`（message 级、无 partID）；event `message.removed`（:150-152）。

- **依赖 / 被依赖**：
  - 依赖：仅 `time` + `orjson`（:7,10）—— 零内部依赖（除注释引用 hub）。
  - 被依赖：`tokenstream/subscriber.py:19`（STOP, _resync_frame）；`tokenstream/hub.py:92-104`（全部 builder + PartKey）；`tokenstream/models.py:10`（_now_ms）；`tokenstream/__init__.py:2-5` 再导出；`sse/token_hub.py` shim；`routes/token_stream.py:61-66`（经 token_hub 取 STOP/_resync_frame/sse_frame）。

- **状态 / 可变性**：全模块无状态（纯函数 + 模块级常量 STOP）；orjson 序列化确定性依赖调用点 payload dict 构造序（本文件全部字面构造，快照测试可字节稳定）。

- **错误路径**：无（纯构造）；异常面只在调用方（序列化对象不可 JSON 时 orjson 抛 TypeError —— 本文件入参均为 str/bool/int，不会）。

- **序列化形状 vs 上游（opencode）差异**：
  - `message.part.snapshot` / `message.part.delta` 是 **sidecar 自造**事件名 —— 上游 `/global/event` 的 part 流事件是 `message.part.updated`（载荷含完整 part 结构，见 ocdroid 仓 `opencode-src/current` session/message 协议）；sidecar 把它重投影为省流 delta/snapshot 帧，非上游原样转发。
  - `message.removed` 事件名与上游一致，但载荷裁到最小 flat `{sessionID, messageID}`（:144-145 "mirrors the upstream flat-props shape" —— 上游还带更多字段）。
  - `server.connected` / `server.heartbeat` / `resync` 与控制面 curated SSE 同名同构；token 的 resync 多一个 `sessionID` 键（§16-D；控制面是 `{"reason"}` 单键，hub_types.py:321）。
  - 本模块**从不**产 `id:` 行 —— v4 的 `id: t:<sid>:<epoch>:<seq>` 前缀由 hub 层（`_deliver_logged`, tokenstream/hub.py:1437 / replay_wire.sse_id_line）在投递时包裹；故 frames.py 输出是版本无关字节。

- **疑问点（7）**：
  1. **:33-43 vs hub_types.py:105-107 `sse_frame` 双实现**：注释称 "Both copies share orjson so … byte-identical"，但无共享断言测试钉死两份实现（一改一漏即漂移，如未来 multi-line data / CRLF / retry 字段）。rg 未见专门的双实现字节一致性测试。
  2. **:19 双 STOP 哨兵**（同 subscriber 卡疑问 6）：与 hub_types.py:30 的 STOP 互不识别；`TokenSubscriber.put(控制面STOP)` 会 `len(object())` TypeError。
  3. **:65-81 `partEventRevision` 省略语义**：revision 未知（冷启/重连后）整键省略 → 同一 part 的投递历史可能"有无 revision 混排"，客户端必须把缺省当"未知"而非 0；契约侧是否明确该三态（有/无/回退）待与 v3/v4 契约 §7 对表。
  4. **:103-122 truncated 复用 `message.part.snapshot` 事件名**：按事件名分派的客户端若不检查 payload `truncated:true` 会把截断帧当全量快照（text 缺失）解析 —— 客户端规约是否强制按 (event,payload) 联合判别？
  5. **:125-126 resync 键序 `reason` 前 `sessionID` 后**：与 v3-contract §16-D 冻结形状是否逐字节对表过（快照测试有，但契约文档为准）——审计层面需交叉核对（E 卡契约对照阶段）。
  6. **:22-30 `_now_ms` 墙钟**：`time.time()` 非单调 —— 下游 `LivePart.last_delta_ms` 的 LRU 逐出序与 60s TTL 在 NTP 回拨时会失真（详见 models 卡）。
  7. **:137-152 `_message_removed_frame` 载荷最小化**：上游 message.removed 载荷更富（如 reason/timestamp）；sidecar 裁剪后 ocdroid 只能靠 `sessionID+messageID` 盲删本地态 —— CLIENT_CHANGES 是否已冻结该最小形状（是则无碍，记录在案）。

---

### src/oc_slimapi/sse/tokenstream/models.py（96 行）

- **职责**：token 累加器的数据模型（design §5.3/§5.4）：`LivePart`（在飞 text part 权威副本）、`DeltaAccumulator`（每 key flush 窗口影子缓冲）、`_TokenMetrics`（`sse.tokenStream.*` 计数器）。

- **对外符号**：
  - `@dataclass LivePart`（:13-30）：`chunks: list[str]`（:27, O(1) append, join-on-demand）、`byte_count: int`（:28, UTF-8 字节, Stage C `_reserve` 预算单位）、`ended: bool`（:29）、`last_delta_ms: int`（:30, 默认 `_now_ms()`；Stage-B TTL retiree + Stage-C LRU 逐出键）。
  - `@dataclass DeltaAccumulator`（:33-44）：`chunks`（:43）、`byte_count`（:44）。
    - `append(text)`（:46）：空串 no-op；append + `len(text.encode("utf-8"))` 计数（:48-51）。
    - `drain() -> str`（:53）：join + clear + `byte_count=0`（:59-65），跨窗口复用。
  - `@dataclass _TokenMetrics`（:68-96）：`orphan_deltas`（:87）、`flushed_frames_total`（:88）、`dropped_frames_total`（:89）、`truncated_snapshots_total`（:90）、`token_memory_limit_total`（:91）、S-3a 增补 `gzip_raw_bytes_total/gzip_compressed_bytes_total/flush_duration_ms_total(float)/flush_ticks_total`（:93-96）。`maxSubscriberQueueDepth` 特意不存（snapshot 时活算，:81-84）。

- **依赖 / 被依赖**：
  - 依赖：`.frames._now_ms`（:10）。
  - 被依赖：`tokenstream/hub.py:105`（LivePart/DeltaAccumulator/_TokenMetrics 全量导入 —— 状态容器与计数器宿主）；`tokenstream/subscriber.py:21`（`_TokenMetrics` 注解）；`tokenstream/__init__.py:6`、`sse/token_hub.py` shim；`routes/token_stream.py:224-225`（gzip 计数直写 `subscriber.metrics.gzip_*`）；测试 test_token_hub.py / test_token_subscriber_overflow.py（直接构造 _TokenMetrics/tight_sub）/ test_token_hub_flush.py / test_batch3_lifecycle.py。

- **状态 / 可变性**：纯可变 dataclass，无锁（单事件循环所有者 = TokenStreamHub）；不变式（byte_count == sum(UTF-8(chunks))、`_total_live_bytes/_total_pending_bytes` 聚合）全靠 hub 维护，模型自身不校验；无 `__slots__`（对象数受 TOKEN_LIVE_PARTS_MAX=32 / pending 聚合帽约束，开销可忽略）。

- **错误路径**：无（`drain` 空态返回 ""，`append` 空串 no-op）；所有预算/溢出决策在 hub（`_reserve`/`_evict_part_for_memory`/`_check_pending_budget`），模型层无失败面。

- **疑问点（6）**：
  1. **:30 `last_delta_ms` 墙钟做 LRU 键**：`_now_ms()` 基于 `time.time()`（frames.py:30）—— NTP 回拨会使 LRU 逐出（hub `_evict_part_for_memory`, hub.py:1754 "oldest by last_delta_ms"）与 60s TTL 判定失真；`time.monotonic()` 更稳（换 API 属实现自由，wire 不冻结）。
  2. **:87-91 docstring 口径偏窄**：`dropped_frames_total` 注释写 "oversized non-snapshot frames dropped"，但实际写入点含 oversized（subscriber.py:405）、handshake 溢出（:421）、runtime 溢出断连（:441）三类（subscriber.py:249-254 自称 single authoritative write site）—— models 注释与真实语义漂移。
  3. **:29 `ended` 的读者未在本卡范围核实**：置位点在 hub text-end 路径；读取点（若仅诊断/断言）不影响 wire —— 留给 E-01（hub 精读卡）交叉核对，此处挂账。
  4. **:95 `flush_duration_ms_total: float`**：名称带 Ms 但类型是 float 累加毫秒；snapshot 原样透出（subscriber.py:871）—— 消费方（metrics JSON）数值单位仅靠命名约定，文档未在 metrics 手册标注单位（待对 docs/manual）。
  5. **:59-61 `drain` 空分支冗余置零**：`if not self.chunks: self.byte_count = 0` —— byte_count 与 chunks 同生同灭，正常态不可能 chunks 空而 byte_count>0；防御无害但说明不变式无断言（一处 `assert` 可锁死模型契约，现为口头约定）。
  6. **:68 命名下划线却跨模块公开**：`_TokenMetrics` 私有名却被 registry/路由/测试广泛导入（metrics 直写点在 routes/token_stream.py:224-225 —— 绕过 hub 直接改计数器），"私有"约定名存实亡；S-3a 增补字段直写路由层若异常无防护（在 SSE 生成器内 +1，永不抛，可接受）。

---

## 附：跨卡交叉点（供后续卡片汇拢）

- 断连清理全链：客户端断 → ASGI send 抛/Cancelled → route generator finally（token_stream.py:303-307 / events.py:235-245）→ `unsubscribe`/`detach_events_subscriber` → 双账本空 → `token_hub.stop()` + `maybe_arm_grace_if_idle()` → 30s 后 `_remove_hub_after_grace` 拆 hub。溢出断连额外路径：`put` 溢出 → resync+STOP → generator break → 同一 finally。`session.deleted` → `terminate` → STOP → 同一 finally。三路汇聚于 unsubscribe 成员守卫，闭环成立（依赖 generator finally 必达 —— Starlette aclose 语义为外部前提）。
- 背压公平结论：per-sub 有界队列 + put_nowait + 溢出即断，flush loop 永不因单慢客户端阻塞；无跨 sub 优先级/加权 —— 帧序公平仅由 sorted-key 遍历（hub flush, hub.py:542）保证。
- 双实现/双哨兵（sse_frame ×2、STOP ×2、_now_ms ×2）是本组的结构性腐化风险点，建议审计结论单列。
