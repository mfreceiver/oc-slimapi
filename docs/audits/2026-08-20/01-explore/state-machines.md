# E4 状态机清单（Phase 1 · 只读探索）

> 审计对象：`src/oc_slimapi/**` @ 2026-08-20。证据格式 `路径:行号`（路径相对仓库根，`src/oc_slimapi/` 缩写省略为模块名）。
> 每张卡片：状态集 / 事件集 / 转移函数（含守卫）/ 终态与不变量（初稿）/ 超时·持久化·恢复 / 未定义转移与可疑点（draft 种子）。
> 本文档为草稿基线：转移表按实读代码归纳，"可疑点"未经复核，仅供 Phase 2 深查排序。

卡片索引：共 **20** 张。

---

## 1. selector 版本/目录选择器（selector.py:502-739）

- **状态集**（`selectorResult` 观测枚举，selector.py:98-104；请求级一次性判定，无跨请求状态）：
  - `not_applicable`（非 /slimapi 路径，selector.py:518）
  - `rejected`（versions 405 selector.py:537 / 版本 400 selector.py:559,626 / 目录 400 selector.py:613）
  - `exempt`（GET /slimapi/versions，selector.py:547）
  - `v3` / `v4`（admitted，selector.py:579）
  - 隐藏子状态：`wire="3"|"4"|None` + `directoryForm=query|header|both|absent|None`（selector.py:352-356,527-530）+ `V3_DIRECTORY_STATE_KEY` 消费成功 stash（selector.py:707-709）
- **事件集**：
  - E1 scope.type != "http" → 直通（selector.py:511-513）
  - E2 非 /slimapi 路径（`_is_slimapi_path`，versioning.py:29-31，斜杠折叠）→ E-not-applicable（selector.py:515-523）
  - E3 normalized=="/slimapi/versions" ∧ method≠GET → 405 `method_not_allowed`+Allow:GET（selector.py:533-545，§8.3① 最高优先）
  - E4 normalized=="/slimapi/versions" ∧ GET → exempt 直通（selector.py:546-549）
  - E5 无任何 `v` 对 → 400 `unsupported_version`（selector.py:551-556 → 622-634）
  - E6 `v` 词法非法（`^[1-9][0-9]*$`，selector.py:127）或多值不同 → 400 `invalid_version_selector`（selector.py:558-565）
  - E7 词法合法 ∉ {3,4}（SUPPORTED_WIRE_VERSIONS=ACCEPTED_CLIENT_VERSIONS(3,4)，selector.py:135-137; versioning.py:44）→ 400 `unsupported_version`（selector.py:567-572）
  - E8 admitted（v∈{3,4}，同值重复折叠）→ stash v3/v4（selector.py:578-579）
  - E9 v4 ∧ §16 合取（`method.boundary.v4∈SATISFIED ∧ session.post-actions.v4∉SATISFIED`，selector.py:257-266，请求时动态读 readiness）∧ (method,path)∈三组延迟 POST（selector.py:247-254,269-280）→ 405 `method_not_applicable`（selector.py:592-609，插列在②③之间）
  - E10 目录消费（selector.py:611,636-717）：
    - v4 ∧ 退役路由 `^/slimapi/sessions$`（selector.py:197-199）∧（`directory` 键存在 selector.py:391-403 或 header 存在 selector.py:325-333）→ 400 `directory_retired_in_v4`（selector.py:668-672，统一体 selector.py:205-212）
    - 非消费集（tolerant）→ 直通不消费（selector.py:674-677；消费集 selector.py:143-188）
    - 多值 distinct（normalize 后）→ 400 `invalid_directory_selector`（selector.py:678-681）
    - query 单值 + header 存在：normalize 不同 → 400 `directory_conflict`（selector.py:689-695）；相同 → 400 `directory_header_retired`（selector.py:696-698）
    - query 单值校验失败（validate_directory 抛 CodedHTTPException）→ 400 该 code（selector.py:686-688）
    - stream 路径 query-only 单值 → 接受 no-op（不 stash 不剥离，selector.py:699-701）
    - 仅 header → 400 `directory_header_retired`（selector.py:702-704）
    - 无任何输入 → 直通（selector.py:705-706）
    - 消费成功 → stash + 字节保留式剥离 `directory` 对（selector.py:707-716）
  - E11 forward：剥离所有 `v` 对（含 `%76` 形态，selector.py:465-499,729-731）
- **转移函数**（按 §8.3 冻结链 ①→②→method405→③）：

  | 入口态 | 事件 | 出口态 | 守卫 |
  |---|---|---|---|
  | start | E1/E2 | not_applicable（直通下游） | — |
  | start | E3 | rejected + 405 | normalized==versions ∧ method≠GET |
  | start | E4 | exempt（剥离 v 后 forward） | GET |
  | start | E5/E7 | rejected + 400 unsupported_version | `supported:[3,4]` |
  | start | E6 | rejected + 400 invalid_version_selector | 词法/多值 |
  | start | E8 | v3 或 v4 | 同值折叠 |
  | v4 | E9 | （stash 保持 v4）+ 405 method_not_applicable | §16 合取 ∧ 三组 POST |
  | v3/v4 | E10 错误分支 | rejected + 400 各码 | 见上 |
  | v3/v4 | E10 成功/E11 | forward（下游路由） | — |

- **终态/不变量（初稿）**：
  - 405① 优先于一切；版本 400② 优先于 method 405（method 判定不读 query，selector.py:583-591）；目录 400③ 最后。
  - `absent`/`v2` selectorResult 按构造不再出现（selector.py:95-98）。
  - S-B04：路由读 wire 视图唯一入口 `wire_view_from_scope`（默认 3，selector.py:368-388）。
  - rejected/exempt 路径永不 forward → 其 query 字节不影响下游。
- **超时/持久化/恢复**：无（纯 ASGI 每请求重算；readiness 集合随代码版本冻结，readiness.py:93）。
- **未定义转移/可疑点（draft 种子）**：
  - D1-1：`?directory=`（空值）在消费集路由上：`_collect_directory_values` keep_blank_values=True（selector.py:435-445）→ `validate_directory("")` 抛什么 code 需查 directory.py（可能是 `invalid_directory` 族），与 `_has_query_key` 的"空值不算 directory 形态"（selector.py:311-322，§9.1 观测）存在**观测/判定口径差**：directoryForm=absent 但消费判定走 400。Phase 2 核对 wire 一致性。
  - D1-2：E9 合取是请求时动态读模块常量（selector.py:265-266），而路由注册是启动时静态——若两者版本错位（热替换代码）理论上 405 面与路由面可短暂不一致；当前单进程静态加载下不可达。
  - D1-3：`_strip_query_keys` 纯文本扫描 `&` 分段（selector.py:478-494）：`v` 剥离后 query 为空串而非删除 `?`——下游 FastAPI 观测形态需与契约核对（低风险）。
  - D1-4：v4 退役路由判定用 `_has_directory_query_pair`（键存在即退役，selector.py:391-403）而 directoryForm 用 `_has_query_key`（非空才算，selector.py:311-322）——同一请求两处口径不同，属有意（注释已述），但审计需在 §9.1 对账时记住。

## 2. catch-all 404 终端边界（proxy.py:31-51 + selector 到达矩阵）

- **状态集**：无服务端状态；每请求二选一终态：`404 thin_route_not_found`（proxy.py:44-51）或 `WS 501 websocket_not_supported`（proxy.py:34-38，accept→JSON→close(1011)）。
- **事件集**：任意 HTTP 方法（GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS，proxy.py:42）；任意 WebSocket upgrade（proxy.py:34）。
- **转移函数（到达条件矩阵，初稿）**：

  | 请求形态 | 先行拦截 | 到达 catch-all 的条件 | 结果 |
  |---|---|---|---|
  | 非 /slimapi HTTP | selector 零触碰（selector.py:516-523） | 无路由匹配（所有收集路由均 /slimapi 前缀，app.py:760） | 404 thin_route_not_found |
  | /slimapi HTTP，v/directory/method 400/405 | selector ①②③ | **不可达**（selector 直接应答，proxy.py:9-14 注释） | — |
  | /slimapi HTTP，admitted 但路由未收集 | selector forward（selector.py:620） | 路由表 miss | 404 thin_route_not_found |
  | WS 任意路径 | selector 只处理 http scope（selector.py:511-513） | 恒可达 | 501 + close 1011 |
  | 非 7 方法（TRACE/CONNECT 等） | Starlette 路由层 | api_route methods 不含 → Starlette 泛化 405 | （非 coded 体，见 D2-1） |

- **终态/不变量（初稿）**：catch-all 是 HTTP 唯一出口兜底；3.0.0 起反代关闭，无转发语义（proxy.py:1-24）。错误优先链 = selector ①405 → ②版本400 → method405 → ③目录400 → 路由 miss ④404（proxy.py:9-14; selector.py:581-611）。
- **超时/持久化/恢复**：无。gzip 感知错误体经 `error_response`（proxy.py:47-51）。
- **可疑点（draft 种子）**：
  - D2-1：方法域外请求（TRACE 等）得到 Starlette 原生 405 而非 coded 体——与全站 coded 错误体约定是否一致需对契约 §8.3 复核。
  - D2-2：WS 501 先 `accept()` 再发 JSON 再 close(1011)（proxy.py:36-38）——客户端视角是"已建立再异常关闭"，与"501 during handshake"语义差异属已知设计，但值得在 v4 契约复核时确认。
  - D2-3：`/slimapi/versions` GET exempt 后若 versions 路由本身抛错，兜底仍是 catch-all 404——正常但审计矩阵需列入（versions 路由在 app.py:760 注册，实际可达性高）。

## 3. GlobalHub 上游订阅生命周期（sse/global_hub.py:63-1090）

- **状态集**（组合式）：
  - 任务组态：`idle`（无 task，global_hub.py:101-104 全 None）→ `running`（run+flush+heartbeat 三任务组，global_hub.py:238-258）→ `dead`（run 正常退出即 has_consumers()==False，global_hub.py:995）
  - run() 内部态：`backoff-sleep(delay∈[1,30]s)` → `connecting` → `streaming`（aiter_lines 中）→（`eof`|`exception`）→ backoff（global_hub.py:994-1090）
  - epoch 期标志：`ever_connected`（global_hub.py:105）；`_upstream_loss_notified`（per-epoch 一次性守卫，global_hub.py:129,1025,1066,1084）
  - 业务累加态：`pending[sid]→DigestFields`（debounce 窗，global_hub.py:106,452-479）、`sticky_last_error`（global_hub.py:133）、`deleted_tombstones`（global_hub.py:144）、`_retired_messages`（global_hub.py:166）、`_last_updated_at_by_sid`（global_hub.py:121，cap 10k，global_hub.py:60）
- **事件集**：
  - 订阅侧：`subscribe(welcome)`（global_hub.py:329-347，ensure_upstream 强制）；`unsubscribe`（global_hub.py:349-360，最后消费者离开→arm `stop_after_grace`）；`ensure_upstream`（global_hub.py:194-217，取消已 arm 的 stop_task）
  - 任务侧：INV-1 组监管 done_callback（global_hub.py:260-327：cancelled→no-op；run 正常退出→cancel 兄弟；异常死→cancel 兄弟 + `has_consumers()` 为真则 `_spawn_group` 强制重建，global_hub.py:319-326）
  - 上游侧：连接成功/首连/重连（global_hub.py:1003-1026：`reconnects_total++` 1009-1010；`_notify_upstream_loss()` 若本 epoch 未通知过 1020-1021；随后 `_upstream_loss_notified=False` 开新 epoch 1025；`delay=1.0` 1026）；SSE 行→publish（global_hub.py:1028-1055）；EOF（global_hub.py:1056-1072，INV-6：notify-once + backoff）；异常（global_hub.py:1075-1090，同前）；CancelledError re-raise（global_hub.py:1073-1074）
  - publish 事件族（global_hub.py:658-973）：IMMEDIATE q/p 直推（671-685）；session.status（695-723，G1 busy 清 sticky+立即 flush_sid 716-723）；session.deleted（724-743，tombstone+清 sticky/retired/水位）；session.updated→archived 粘滞（744-762）；message.updated/appended→bump updatedAt（784-800）；session.error（806-858，带 sid 立即 flush_sid / 无 sid 直推）；message.part.*/message.removed→token hub 路由（882-973）
  - 定时器：flush_loop 每 DEBOUNCE_SECONDS=0.25s（global_hub.py:427-430; hub_types.py:92）；heartbeat_loop 每 10s（global_hub.py:481-488; hub_types.py:93）；`stop_after_grace` GRACE_SECONDS=30s（global_hub.py:419-425; hub_types.py:94）
- **转移函数（核心，初稿）**：

  | 态 | 事件 | 次态 | 守卫/动作 |
  |---|---|---|---|
  | idle | subscribe/ensure_upstream | running | `_spawn_group`；先 cancel 残留 stop_task（global_hub.py:213-217）|
  | running | 最后消费者离开（两个账本皆空，`has_consumers` global_hub.py:362-381 跨 subscribers+_token_hub.subscriber_count） | running（armed） | arm stop_after_grace / registry 侧 arm `_remove_hub_after_grace` |
  | armed | 30s 到 ∧ 仍无消费者 | idle/被回收 | hub 侧仅 cancel 任务（global_hub.py:419-425）；registry 侧再 `_global=None`（registry.py:258-325）|
  | armed | 新消费者 | running | ensure_upstream cancel stop_task（global_hub.py:213-215）|
  | streaming | EOF/异常 | backoff | notify-once 守卫 `ever_connected ∧ ¬notified`（global_hub.py:1064-1066,1082-1084）；`_notify_upstream_loss`=resync_all+write_barrier(None)+token_hub.on_upstream_reconnect（global_hub.py:383-417）|
  | backoff | sleep 到 ∧ has_consumers | connecting | delay×2 封顶 30s（global_hub.py:1000,1072,1090）|
  | backoff | ¬has_consumers | dead | 循环条件退出（global_hub.py:995）→ done_callback cancel 兄弟（global_hub.py:304-307）|
  | connecting(重连成功) | — | streaming | notify-once 补位（global_hub.py:1020-1021）→ 开新 epoch（1025）|

- **终态/不变量（初稿）**：
  - INV-1：run/flush/heartbeat 三任务原子组，异常死由 done_callback 闭包内自建组重建，陈旧组以 `self.task is run_task` 判废（global_hub.py:287-327）。
  - INV-6：每次上游丢失（EOF/异常/成功重连三路径）`_notify_upstream_loss` 恰好一次（global_hub.py:1014-1025,1064-1066,1082-1084）。
  - 已发布帧语义：`_emit_directory_frame` 在 allowlist 通过后先 `_replay_publish`（GLOBAL 域，零订阅也记账）再投递（global_hub.py:584-599,547-570）；v4 订阅者收 `id:` 前缀行，v3 字节不变（global_hub.py:592-597）。
  - allowlist 丢帧计数不耗 seq（global_hub.py:585-587）。
- **超时/持久化/恢复**：重连退避 1→30s 指数；无跨进程持久化（epoch=进程随机 nonce，replay_log.py:101-108）；`resync_all` 冷启动语义清 tombstones/retired/水位（global_hub.py:977-991）。
- **可疑点（draft 种子）**：
  - D3-1：`stop_after_grace`（hub 侧，global_hub.py:419-425）与 `HubRegistry._remove_hub_after_grace`（registry.py:258）双计时器并存：生产走 registry；hub 侧仅测试直接调 `unsubscribe` 时 arm——若两计时器同时 armed（理论路径：直接 unsubscribe + registry unsubscribe），存在双 cancel 竞争（幂等 cancel，低危，但矩阵需记）。
  - D3-2：`_notify_upstream_loss` 的 barrier 写失败仅告警降级（global_hub.py:411-415）——重连客户端在 barrier 缺失时按普通窗口判定，可能补到跨丢失边界的旧帧（设计已声明 best-effort；Phase 2 评估是否违反 §7.2 冻结语义）。
  - D3-3：flush() 一次快照换 `self.pending`（global_hub.py:468）与 flush_sid 的 pop（global_hub.py:439）在同一 loop tick 串行——无锁正确；但 publish 在 streaming 协程、flush 在 flush_loop 协程，二者对 `pending` 的 setdefault/pop 交错依赖 asyncio 串行化，代码无显式断言（现状正确，审计记录依赖前提）。
  - D3-4：`_last_updated_at_by_sid` cap=10k（global_hub.py:60,641-642）驱逐后跨窗单调性静默丢失——契约已声明非 wire 保证（global_hub.py:176-183），列为止血确认项。
  - D3-5：G1 busy-clear 依赖 `normalize_session_status` 兼容对象信封（hub_types.py:376-404; global_hub.py:702-723）——上游第三种形状（如 `{"type":null}`）静默忽略，需在 live-wire 对账中确认覆盖。

## 4. HubRegistry 准入/宽限拆除（sse/registry.py:34-408）

- **状态集**：`_global=None`（无 hub）↔ `_global=GlobalHub`；`_removal_task∈{None, armed}`；计数器 `total_subscribers`、`rejected_total`（registry.py:69-70,84）。
- **事件集**：`subscribe(wire_v4)`（registry.py:187-230，检查→add 无 await 临界区）；`unsubscribe`（registry.py:232-256，幂等 membership 守卫 240）；`cancel_pending_removal`（registry.py:146-159，NB-B1）；`maybe_arm_grace_if_idle`（registry.py:161-185，跨双账本谓词）；`_remove_hub_after_grace` 到期（registry.py:258-325）；`close`（registry.py:389-408）。
- **转移函数（初稿）**：

  | 态 | 事件 | 次态 | 守卫 |
  |---|---|---|---|
  | 无 hub | subscribe | 有 hub | 懒创建并转发 token_hub/turn/replay（registry.py:129-141）；容量检查先于 add（212-228，目录帽/总数帽→503 SubscriberCapacityError 214-225）|
  | 有 hub | unsubscribe（最后一个） | armed | `has_consumers()` False 才 arm（registry.py:256→161-185）|
  | armed | 到期 ∧ hub is self._global ∧ ¬has_consumers | 无 hub | cancel 4 任务→gather（INV-2，registry.py:294-311）→二次复查（313-316）→`token_hub.on_upstream_reconnect()` 清旧 epoch→`_global=None`（317-325）|
  | armed | subscribe / token subscribe | 有 hub | `cancel_pending_removal`（registry.py:146-159）+ `ensure_upstream`（subscriber.py:695-699）|
  | 任意 | close | 无 hub | cancel 全部任务 + gather + total=0（registry.py:389-408）|

- **终态/不变量（初稿）**：准入临界区无 await（registry.py:211-229）；unsubscribe 幂等（membership 守卫 240-241）+ 负数防御（244-247）；grace=30s（hub_types.py:94）。
- **超时/持久化/恢复**：`_remove_hub_after_grace` sleep 30s 可被 cancel；gather 可被新订阅 cancel（registry.py:305-311 返回不置空）。
- **可疑点（draft 种子）**：
  - D4-1：`subscribe` 注释明确**不**取消 `_removal_task`（registry.py:203-207）——依赖 grace 任务醒来后自查退出；但 token 订阅路径**会**主动 cancel（subscriber.py:698）——两条路径行为不对称（皆正确但易漂移，审计跟踪点）。
  - D4-2：`_remove_hub_after_grace` 在 gather 被 cancel 时不清理 `_removal_task=None`（registry.py:305-311 提前 return）——槽位残留已 done 任务；下次 `maybe_arm_grace_if_idle` 的 `if self._removal_task is not None: return`（registry.py:183-184）将误判"已 armed"导致**不再 arm**——需 Phase 2 复核该路径是否可达（cancel 源=subscribe→ensure_upstream，而 ensure_upstream 只 cancel hub.stop_task 不 cancel registry 任务；真正 cancel registry 任务的只有 close/cancel_pending_removal，后者会置 None——初判不可达，留档）。

## 5. Subscriber 控制面出站队列（sse/hub_types.py:213-346）

- **状态集**：`open`（closed=False, hub_types.py:242）→ `closed`（溢出断连后终态，hub_types.py:305）；辅助：队列内容态（正常积压 / 终端 resync+STOP 对）；`wire_v4` 标志（hub_types.py:255）。
- **事件集**：`put(frame)`（hub_types.py:264-325，三段守卫）；`ack(frame)`（hub_types.py:327-338）；生成器消费 STOP（routes/events.py:214-221）；`unsubscribe`（routes/events.py:232-241 finally）。
- **转移函数（put 分支，初稿）**：

  | 态 | 事件 | 次态/结果 | 守卫 |
  |---|---|---|---|
  | closed | put | 静默丢（return False） | hub_types.py:274-277 |
  | open | put(STOP) | 入队哨兵 | 满则 False（hub_types.py:279-285）|
  | open | put(frame) 超大 | 丢帧计数，不闭 | `len>max_frame_bytes`（hub_types.py:286-289，默认 256KiB hub_types.py:70）|
  | open | put(frame) 预算内 | open（入队+计字节） | `qsize<queue_items ∧ queued_bytes+size≤buffer_bytes`（hub_types.py:290-303，默认 256项/2MiB hub_types.py:68-69）|
  | open | put(frame) 溢出 | **closed** | 清队列（hub_types.py:340-346）、forced_disconnects++（306）；v4：仅 STOP（hub_types.py:316-320）；v3：`resync{subscriber_backpressure}`+STOP（321-325）|

- **终态/不变量（初稿）**：溢出后先前已入队帧**不**投递（契约 §6，hub_types.py:227-231）；v4 的 `subscriber_backpressure` ∉ V4_RESYNC_REASONS（replay_wire.py:72-77）故 v4 线上只 STOP；ack 与 put 字节账镜像、STOP 不入账（hub_types.py:327-338）。
- **超时/持久化/恢复**：无自身定时器；恢复=客户端重连（v4 走 Last-Event-ID replay）。溢出时已 replay 记账的帧仍可经 ReplayLog 补发（已发布帧语义）。
- **可疑点（draft 种子）**：
  - D5-1：put 的预算检查与 put_nowait 之间理论竞态被注释声明为"实践无并发生产者"（hub_types.py:296-300）——依赖单 loop 串行前提，审计确认所有 fanout 调用点（flush/flush_sid/heartbeat/_emit_directory_frame）均同步调用。
  - D5-2：v4 溢出仅 STOP：若客户端未实现 Last-Event-ID 重连则静默丢尾（设计冻结，非 bug；矩阵记录）。
  - D5-3：`dropped_frames`（超大丢）与 `forced_disconnects`（溢出断）在 metrics 端聚合口径需与 contract §2 复核（registry.py:350-358）。

## 6. TokenStreamHub 累加器 + flush loop 双账本（sse/tokenstream/hub.py:214-2191）

- **状态集**（多重正交子机）：
  - PartKey 生命周期：`absent` → `live`（LivePart，hub.py:254,1867-1917）→ `disabled`（`_disabled_parts` 墓碑，hub.py:274,1919-1946）／`nontext`（`_nontext_parts`，hub.py:273,1968-1979）
  - flush 任务：`_flush_task∈{None, running}`（hub.py:302,421-441,478-482）+ INV-1 看门狗重建（hub.py:443-476）
  - 消息级：`(sid,mid)∈_retired_messages` 墓碑（hub.py:345,909-945）；重放队列 `_removed_messages`（hub.py:335,2061-2087）
  - 会话级：`_session_status∈{busy,idle,未知}`（hub.py:282,1019-1058）、`_deleted_sids`（hub.py:355,2018-2049）、`_busy_sids`（hub.py:283）
  - 双账本仪表：`_total_live_bytes ≤ TOKEN_LIVEPARTS_MAX_BYTES=4MiB`（config.py:48; hub.py:276）、`_total_pending_bytes ≤ TOKEN_PENDING_MAX_BYTES=4MiB`（config.py:49; hub.py:277）——独立预算（hub.py:46-54）
  - 待发 resync 队列 `_pending_session_resinks`（cap 64，config.py:67; hub.py:285,2092-2103）
- **事件集**（ingest 全部来自 GlobalHub.publish 串行路由，global_hub.py:882-973）：
  - `on_part_updated`（hub.py:619-715：text-start 建 LivePart 幂等 699-710；text-end→finish_part 711-715；非文本→nontext 686-690；deleted-sid/retired/disabled 守卫 673-695）
  - `on_part_delta`（hub.py:739-829：field=="text" 761、三重墓碑门 768-781、orphan 计数 782-788、`live.ended` 丢弃 789-790、`_reserve` 791-797、4KiB 早冲 806-823（config.py:51）、`_check_pending_budget` 829）
  - `finish_part`（hub.py:950-1014：同步排空残余→delta→`snapshot{done:true}`（无 text）→drop_part；wire-strong 顺序不变量）
  - `on_message_removed`（hub.py:835-877：先 `_retire_message` 再 fanout 再记重放队列）；`on_part_removed`（hub.py:879-907 幂等 drop）
  - `on_session_status`（hub.py:1019-1070：busy 记账；idle→`_retire_session`+barrier+入队 resync）；`on_session_deleted`（hub.py:1072-1132：retire+barrier+清 retired+deleted-sid 门+terminate 全部订阅者）
  - `on_upstream_reconnect`（hub.py:2124-2191：清 live/nontext/disabled/pending/session 态/两墓碑；**保留** `_part_revisions`（CRITICAL 1）与 `_removed_messages`；对每 sid fan `reconnect_no_replay`）
  - flush_loop 定时（100ms flush / 60s ttl_sweep / 15s heartbeat，hub.py:121-123,484-517；config.py:50,52,53）；`ttl_sweep`（hub.py:1167-1206：仅 idle 会话过期 LivePart）
  - 预算事件：`_reserve`（hub.py:1703-1749：per-part 1MiB truncate / 全局 LRU 逐出）；`_evict_part_for_memory`（hub.py:1751-1821：flush_sid→barrier→resync{token_memory_limit}→重快照，skip_key nodrop）；`_check_pending_budget`（hub.py:1823-1862：force-flush→无订阅/仍超→LRU 逐出）；`_start_part`（hub.py:1867-1917：count cap 32 LRU + 大种子截断）
  - 握手：`attach_subscriber`（hub.py:1211-1338：v4 无预填 1289-1304；v3 五步 begin/end_handshake 包夹 1305-1338）；`detach_subscriber`（1340-1353）
- **转移函数（PartKey 主线，初稿）**：

  | 态 | 事件 | 次态 | 守卫/动作 |
  |---|---|---|---|
  | absent | text-start | live | 非删除 sid/非 retired/非 disabled（hub.py:673-695）；建 LivePart+种子（1867-1917）|
  | live | delta | live（双写 chunks+acc） | `_reserve` 过（1703-1749）；4KiB 早冲（806-823）|
  | live | delta 超 per-part | disabled | `_truncate_part_for_all`（1730-1733）|
  | live | 全局 LIVE 超限（非本 key） | （他 key）disabled+resync | LRU 逐出最旧、永不逐 current key（1738-1748）|
  | live | text-end | disabled | finish_part：残余 delta→done 标记→drop_part（950-1014）|
  | live | ttl_sweep ∧ sid 已 idle ∧ 60s 无 delta | absent（清） | hub.py:1186-1202（busy/未知不 retire）|
  | live/disabled | message.removed | retired(消息) | `_retired_message` 原子清五种结构（909-945）|
  | live | session idle | absent | `_retire_session`+barrier+resync{session_idle}（1019-1070）|
  | 任意 | session.deleted | deleted-sid 门 | terminate 订阅者（1072-1132）|
  | 任意 | upstream reconnect | 全清（除 revisions/removed 队列） | 2124-2191 |

- **终态/不变量（初稿）**：
  - 终端顺序：delta 先于 `snapshot{done:true}`，标记后不再发 token 帧（hub.py:24-29,960-1014）。
  - v4 帧资格：`message.part.snapshot` 族永不入 ReplayLog/不耗 seq/只投 v3（hub.py:171-190,1440-1459）。
  - 每帧 revision 严格递增（Option B）：唯一递增点 `_next_part_revision`（hub.py:717-737）；reject 路径不耗 revision（MAJOR 5，hub.py:679-695）。
  - 已发布帧先记账后投递（`_fanout_frame`→`_replay_publish_token`，hub.py:1461-1481,1371-1394）。
  - 双账本独立：同一 delta 字节同时计 live 与 pending（hub.py:46-54）。
  - barrier 写点全枚举：session idle（1069）、session deleted（1121）、内存逐出（1801）、上游丢失（global_hub.py:411-417）。
- **超时/持久化/恢复**：100ms/60s/15s 三节拍（hub.py:484-517）；墓碑 TTL/cap：disabled/nontext 4096 项（config.py:59-61），removed 1000/24h（config.py:75-76），session 态 10k（hub.py:116）；无跨进程持久化。
- **可疑点（draft 种子）**：
  - D6-1：`ttl_sweep` 只清 `status=="idle"` 的会话（hub.py:1190-1191）——**busy 永不清**：上游若停在 busy（崩溃/漏发 idle），LivePart 只能靠全局 LRU 逐出兜底，且 busy 会话无 resync 提示（内存语义 vs 线上语义不一致窗口）。
  - D6-2：`_part_revisions` FIFO cap=4096（TOKEN_DISABLED_MAX 复用，hub.py:735-736）——若某活 part 的 revision 条目被驱逐（理论上需 4096 并发键），其下一帧 revision 回 0 → 客户端 strict `>` 丢帧；实践受 TOKEN_LIVE_PARTS_MAX=32 约束，但 `_pending`/迟到的键可短暂推高条目数（低概率，记录）。
  - D6-3：`_emit_snapshot_or_truncated_nodrop` 超大时投 truncated 但保留 LivePart（hub.py:1597-1648）——后续 delta 对客户端成孤儿（设计已接受，hub.py:1610-1617）；审计确认 ocdroid 侧行为一致。
  - D6-4：`_check_pending_budget` 的 `had_subs` 在 force-flush 前采样（hub.py:1853）——若 flush 期间新订阅者 attach（同一 tick 内不可能，attach 是同步），无竞态；依赖串行前提。
  - D6-5：`on_upstream_reconnect` 保留 `_removed_messages`（hub.py:2159-2162）但 `_retired_messages` 清空（2180）——TTL 驱逐时 gate 随队列耦合清理（2083-2087），重连后 gate 空+队列在：迟到的 part 事件（新 epoch 不会有）无门可挡——正确（新 epoch 无迟到），矩阵记录此依赖。
  - D6-6：`flush()` 内 events_tap 投递不在 replay 记账内（lean 投影，hub.py:567-570）——/events 通道的 token 帧与 GLOBAL 域 id 序列完全解耦，属设计（hub.py:193-211）；确认契约 §7 不要求该通道可重放。

## 7. TokenSubscriber 握手/运行物理分离队列（sse/tokenstream/subscriber.py:58-512）

- **状态集**：`open`（subscriber.py:307 closed=False）→ `closed`；运行模式子态：`handshake`（`_in_handshake=True`，subscriber.py:331,354-356）↔ `runtime`（358-360）；closed 诱因子标记 `_handshake_overflow`（subscriber.py:310）。
- **事件集**：`put(frame)`（subscriber.py:362-458，路由：closed→丢 / STOP→runtime / 超大→丢计数 / handshake→fail-on-overflow 缓冲 / runtime→T3 守卫→溢出断连）；`begin/end_handshake`（354-360）；`terminate(reason)`（460-493，INV-4 服务端终止：v4 非冻结 reason 时静默 STOP）；生成器 `queue.get()+ack`（routes/token_stream.py:293-306；先 handshake 后 runtime，subscriber.py:219-232）。
- **转移函数（初稿）**：

  | 态 | 事件 | 次态 | 守卫 |
  |---|---|---|---|
  | open/handshake | put | open/handshake | 缓冲 cap：2048 项/8MiB（config TOKEN_HANDSHAKE_*，subscriber.py:302-303,116-140）；**溢出→closed**（fail-loud，subscriber.py:417-422）|
  | open/handshake | end_handshake | open/runtime | 新预算（字节账分离，subscriber.py:99-111）|
  | open/runtime | put 预算内 | open/runtime | 只看 runtime 深度/字节（subscriber.py:428-433；CRITICAL 3）|
  | open/runtime | put 溢出 | closed | `clear_runtime()`（握手帧保留，subscriber.py:159-172）+v3: resync{subscriber_backpressure,sessionID}+STOP（449-458）/v4: STOP only（450-452）|
  | open（任意） | terminate(reason) | closed | v3: resync{reason}→STOP；v4: reason∈V4_RESYNC_REASONS 才发 resync（489-493）；不 bump 断连计数（460-470）|

- **终态/不变量（初稿）**：握手帧先于一切 runtime 帧被消费（subscriber.py:219-225）；溢出永不触碰握手缓冲（159-172）；STOP 永不入字节账（145-157,174-184）；queued_bytes=双账之和（341-352）。
- **超时/持久化/恢复**：无定时器；恢复=客户端重连（v4 Last-Event-ID）。
- **可疑点（draft 种子）**：
  - D7-1：`terminate` 在 `closed==True` 已断连的 sub 上再次调用会重复 `clear_runtime`+put STOP（460-493 无 closed 守卫）——STOP 双投递对生成器只是多 break 一次（幂等），低危记录。
  - D7-2：握手缓冲 byte cap 8MiB 对"32×近 1MiB 快照+JSON 转义放大"可能不足（subscriber.py:264-277 自述）——溢出映射 503 `sse_token_handshake_overflow`（fail-loud 正确）；Phase 2 统计真实放大系数。
  - D7-3：`ack` 依赖 `last_get_handshake` 单槽（subscriber.py:107-111,509-512）——若调用方 get 与 ack 之间又 get（乱序 ack），字节账路由错槽；当前生成器严格 get→yield→ack 串行（routes/token_stream.py:293-306），前提成立但脆弱。

## 8. TokenStreamRegistry 准入账本 + events_tap（sse/tokenstream/subscriber.py:536-874）

- **状态集**：每 sid fanout 集 `_subs_by_sid`（hub.py:288）；账本 `total_subscribers∈[0,max]`、`events_tokens` 集（subscriber.py:570-582）；flush loop 由"双账本任一非空"保持存活（hub.py:377-395 `has_consumers`）。
- **事件集**：`subscribe(sid,wire_v4)`（subscriber.py:625-744 七步无 await 临界区）；`_rollback_failed_attach`（746-787，MAJOR 5 对称回滚）；`unsubscribe`（789-836，NB-D1 membership 守卫）；`attach/detach_events_subscriber`（584-623，L2-A）。
- **转移函数（subscribe 主线，初稿）**：cap 检查（668-674→503 `sse_token_subscriber_limit`）→ 构造 sub → `cancel_pending_removal`+`ensure_upstream`（695-699）→ `token_hub.start()`（701）→ `attach_subscriber`（705）→ 异常则回滚+503（707-711）→ `sub.closed` 则回滚+503（handshake_overflow 区分码，725-742）→ 计数（743）。detach：membership 守卫→减数→双账本空则 `token_hub.stop()`→`maybe_arm_grace_if_idle`（821-836）。
- **终态/不变量（初稿）**：最后 detach 必停 flush loop + re-arm hub grace（B-D1 对称，subscriber.py:789-836）；失败 attach 不占账本槽（743 仅成功路径）。
- **超时/持久化/恢复**：无；grace 30s 由 HubRegistry 承担。
- **可疑点（draft 种子）**：
  - D8-1：`events_tap.append(sub.put)` / `remove(sub.put)`（subscriber.py:603,619）依赖 bound method 相等语义——正确但若未来换成包装函数会静默失配（tap 泄漏→flush 永活）。
  - D8-2：`subscribe` 的 cap 检查与 `attach_subscriber` 之间无 await（全同步）成立；但 `ensure_upstream` 内 `_spawn_group` 的 done_callback 在后续 tick 才可能触发重建——与订阅无直接竞态，矩阵记录依赖。

## 9. ReplayLog + replay_wire（sse/replay_log.py:237-598 + replay_wire.py:104-282）

- **状态集**：
  - Log 级：`open` ↔ `closed`（replay_log.py:302,541-555；closed 后 append 抛 RuntimeError 366）
  - Domain 级（`_DomainState`，replay_log.py:205-231）：`shell 无帧` / `窗口态`（entries 连续 seq）；附属：`next_seq/last_seq`（只增不 reset）、`barrier_watermark∈None|int`（元数据、免逐出，replay_log.py:472-493）
  - replay 出口（frozen，replay_log.py:161-198）：`ReplayFrames(可空)` / `ReplayResync{epoch_changed|replay_expired|replay_gap|reconnect_no_replay}` / `ReplayIgnoreReset`
- **事件集**：`append(domain,payload,kind)`（replay_log.py:351-395：lazy 域创建 369-372、TTL 头逐出 373,559-566、seq 分配 376-378、三界逐出 393-394,568-592）；`replay(domain,after_seq,epoch)`（399-468）；`write_barrier(domain|None)`（472-493）；`recycle_domain`（495-511，保 seq/barrier）；`sweep`（513-539，TTL+barrier GC `entries[0].seq > watermark+1`）；`close`（546-555）；wire 层 `classify_reconnect`（replay_wire.py:169-209：①② 此层，③④ 委托）+ `parse_last_event_id`（replay_wire.py:126-166：g:3 段 / t:≥4 段 rsplit sid，hex16/decimal）；维护 loop 60s（replay_wire.py:101,229-282）。
- **转移函数（replay 短路序，frozen §7.2，replay_log.py:420-468）**：

  | 序 | 条件 | 出口 |
  |---|---|---|
  | ③ | epoch≠self.epoch | Resync{epoch_changed}（423-425）|
  | ④a | watermark≠None ∧ after_seq≤watermark | Resync{reconnect_no_replay}（433-436，`<=` 含水位本身）|
  | ④b | after_seq>last_seq（含未建域） | IgnoreReset（439-442）|
  | ④c | 无 entries ∧ after_seq==last_seq | Frames(())（up-to-date，449-451）|
  | ④d | 无 entries ∧ after_seq<last_seq | Resync{replay_expired}（452-453）|
  | ④e | entries[0].seq≠after_seq+1 | Resync{replay_expired}（454-458）|
  | ④f | 窗口内部空洞（防御，理论不可达） | Resync{replay_gap}（459-466）|
  | — | 否则 | Frames(entries)（467-468）|

- **终态/不变量（初稿）**：域内 seq 严格单调从 1、tombstone 同耗 seq（replay_log.py:7-9,84-85）；三界独立（count 2048/域、bytes 64MiB 全局、TTL 900s，replay_log.py:93-95）；bytes 逐出保留最后单帧（580-592）；barrier 单调不降（491-492）；recycle 不回退 seq（REPLAY-018）；epoch=进程随机 nonce 永不比较大小（101-108）。
- **超时/持久化/恢复**：纯内存，进程重启=新 epoch=全体 epoch_changed；sweep 60s 由 app lifespan 起（app.py:447-455,457-470）。
- **可疑点（draft 种子）**：
  - D9-1：`sweep` 的 barrier GC 条件 `entries[0].seq > watermark+1`（replay_log.py:536-538）——空窗口保留 barrier（528-530 注释）；若后续 append 使窗口头恰为 W+1 之前 barrier 已 GC（窗口头曾 ≥W+2）→ cursor=W 走 replay_expired 而非 reconnect_no_replay——语义等价（都 resync），但 reason 码不同，客户端统计需兼容两码。
  - D9-2：`_evict_for_bytes` 全局最旧逐出跨域无差别——单域窗口可能被另一域的大帧流量掏空 → 该域重连全部 replay_expired（设计允许；审计观测 `replay_expired` 比例是否异常）。
  - D9-3：`write_barrier(None)` 只覆盖**已创建**域（replay_log.py:485-486）——barrier 写之后才首帧的 token 域无水位；该域 cursor 必然 ≤ 其 last_seq 之前不存在（新域=新订阅），初判安全，留档。
  - D9-4：outcome 计数键含 `up_to_date/ignore_reset/replayed`（449-467）——metrics 键名是否冻结需对 v4-contract §9.1 复核（replay_log.py:338-347）。

## 10. DbAuxiliarySource 断路器（dbaux/lifecycle.py:147-247 + 286-768）

- **状态集**：
  - 源级 `_state`：`disabled`（lifecycle.py:333 初值；原因族 startup/open_failed/gate_failed/query_*/stopped，371,553-554,459,405）→ `available`（560）→ `circuit_open`（679-681）；`stopped` 终态=disable("stopped")（405）
  - Breaker：`closed` ↔ `open`（lifecycle.py:176-179）+ 滑窗样本 `(ts,latency)`（173）
  - generation 单调（330,545）；inode marker（332,546-547）
- **事件集**：`start`（362-381：解析→ro 打开+PRAGMA（499-511）→schema 门（97-107）→失败 disable；起 30s 周期任务 377-380）；`query`（429-463：可用才受理→worker 内 BEGIN..COMMIT+finally ROLLBACK（465-489）→错误分类（114-140：schema/io/cantinit/programming→disable+重探；busy→只计样本 451-453）→`_check_breaker_state`（462,683-689））；`tick`（597-617：①inode/mtime 变→swap（602-609）②circuit_open 到期→probe（611-614）③disabled→reprobe（616-617））；`probe`（619-634：SELECT 1 事务，成功且 breaker.note_probe 恢复→available）；`reprobe`（652-661：重开+门）；`swap`（566-577：关旧→开新→门→generation+1；门失败→disable）；`stop`（383-405：停任务→关连接→有界 drain→disable("stopped")）。
  - Breaker 事件：`record`（197-212：滑窗 60s 剪枝、warmup 前 10 次仅采样 205-208、样本<10 不判 209、P99≥20ms→trip 210-212）；`note_probe`（219-230：P99<10ms 恢复 227-229）；`reset`（232-236：swap/重开成功后清零 563）；`trip`（214-217：开+清窗）。
- **转移函数（初稿）**：

  | 态 | 事件 | 次态 | 守卫 |
  |---|---|---|---|
  | disabled | reprobe 成功 | available | `_reprobe_allowed`（349-352；explicit/upstream-memory 永久禁）|
  | available | query 错误(schema/io/cantinit/programming) | disabled | 448-460 |
  | available | P99≥20ms（record 内部 trip） | circuit_open | `_check_breaker_state` 联动 462,683-689；`_next_probe_at=now+30s`（681）|
  | circuit_open | tick 到期→probe 成功∧P99<10ms | available | 611-614,619-634 |
  | circuit_open | probe 失败 | circuit_open | 保持，重排 30s（622）|
  | available | inode/mtime 变 | (swap) available 或 disabled | 602-609,566-577 |
  | 任意 | stop | stopped（终态） | 383-405，不可恢复（重启新实例）|

- **终态/不变量（初稿）**：连接只在专属单 worker 线程触碰（325-330；§2.2）；短事务+强制 ROLLBACK（478-487）；busy 不禁用只进 P99（451-453）；generation 只在门全过后 +1 且连接所有权移交（521-547 局部所有权纪律）。
- **超时/持久化/恢复**：探针/周期 30s（probe_interval_s 默认，315）；busy_timeout 5000ms（302）；stop drain 5s（app.py:85,605-615）；无持久化。
- **可疑点（draft 种子）**：
  - D10-1：`tick` 在 circuit_open 分支**早 return**（611-614）——熔断期间 inode swap 检查被跳过：旧连接若指向已被替换的文件，半开探针打在旧连接上可能"成功恢复 available"，随后下一 tick 才发现 inode 变化触发 swap——存在一个 30s 窗口读到旧 inode 数据（只读无害，但审计记录）。
  - D10-2：`note_probe` 无最小样本数要求（219-230）——单次快探针即 P99=该样本，<10ms 即闭合；恢复证据弱于 trip 证据（不对称），需对 §2.3-6 冻结口径复核。
  - D10-3：`trip()` 清空窗口（216-217）→ 恢复需全新样本，但 note_probe 首样本即判——与 D10-2 叠加后熔断开↔闭可在两个 30s 周期内振荡（hysteresis 仅靠 10ms 阈值差）。
  - D10-4：`_probe_sync` `assert self._conn is not None`（637）——circuit_open 期间连接恒在（熔断不关连接），但若 stop 与 probe 竞态（stop 先 `_conn=None`）assert 触发 AssertionError 被 `probe` 的 except Exception 吞（626-628 判"探针失败"）——行为正确但掩盖 assert 语义。
  - D10-5：`status()` 副作用（`_check_breaker_state` 可能 trip，691-692）——health 路由高频读 status 会驱动状态翻转（每次读都联查 breaker）；合理但需防 health 轮询成为隐式探针（实际 trip 源仍是 record，此处只联动，低危）。

## 11. SingleFlight（singleflight.py:97-761，plain/leased 双档）

- **状态集**：
  - Entry 所有权态（leased 词表，singleflight.py:108-111）：`IN_FLIGHT` / `GRACE` / `RETAINED` / `FAILED`；层（114-115）：`ACTIVE`（joinable）↔ `RETIRED`（墓碑，不可 join）；plain 用 `expires_at∈None|deadline` 区分 in-flight/grace（177-179）
  - 调用者视角：leader / waiter（refcount `caller_refs`）；`_REJOIN` 哨兵（120）
  - FetchFailed 结果信封（123-134）。
- **事件集**：`fetch`（plain，372-392：join-or-lead 循环）；`fetch_or_bypass`（leased，393-428：串行点 try_reserve→None=bypass）；`_join`（430-455：三分支取消机）；`_lead`（457-481：网络信号量内跑 factory）；`_convert_success`（487-534）；`_fail`（535-554）；`_expire_grace_entry`（577-591，call_later+惰性双入口）；`_release_caller`（613-625，refcount→0 按 state 决定 reap/保留）；`_try_reserve`（683-696，零引用 grace 逐出凑预算）；`shutdown`（709-761）。
- **转移函数（leased 主线，初稿）**：

  | (layer,state) | 事件 | 次态 | 守卫 |
  |---|---|---|---|
  | (absent) | fetch_or_bypass 成功 reserve | (ACTIVE,IN_FLIGHT) | reserve≤max_bytes（683-696）|
  | (ACTIVE,IN_FLIGHT) | factory 成功 | (ACTIVE,GRACE) | 先 set_result 再转换（478-480,522-529）；arm 1s timer（527-529）|
  | (ACTIVE,GRACE) | 1s 到 / 惰性过期 / 被逐 | (RETIRED,RETAINED)；refs==0 即 reap | 577-611 |
  | (ACTIVE,GRACE) | shutdown | (RETIRED,RETAINED) | refs==0 立即 reap（758-761）|
  | (ACTIVE,IN_FLIGHT) | factory 异常/取消 | (RETIRED,FAILED)+立即退款 | 535-554：先释 leader ref→摘 active→入 retired→refund→future 失败（信封/cancel）→无引用 reap |
  | (RETIRED,RETAINED) | 最后 caller release | reap（删+退款） | 613-625 |
  | (RETIRED,IN_FLIGHT) | shutdown 遗留 detached → factory 后到成功 | (RETIRED,RETAINED) | 530-534；工厂最终 resolve 后按引用回收 |

  三分支取消（_join，430-455）：① FetchFailed→重抛同实例（451-454）；② 共享 future 被 cancel（leader 取消）且非本任务取消→释放旧 ref→`_REJOIN`→回串行点重 join/重 lead（446-450；`_current_task_cancelling` 182-186）；③ 本任务取消→释 ref 后上抛（441-445）。
- **终态/不变量（初稿）**：账本不变量 `leased_bytes == Σ reserve{in-flight(含 detached), grace, retained}`（模块 docstring 64-69）；失败不 negative-cache（entry 丢弃/墓碑）；in-flight future 永不被 shutdown cancel（709-761）；plain 保留界 64 项/32MiB（103-104），只逐出已完成项（662-677）。
- **超时/持久化/恢复**：grace 默认 1s（97）；plain 每保留项有活跃 call_later 到期（517-520），shutdown 取消全部 timer；无持久化。
- **可疑点（draft 种子）**：
  - D11-1：分支②检测依赖 `task.cancelling()>0`（182-186，Py3.11+）——若 waiter 在 shield join 中同时被取消且 leader 也取消，归因取决于取消到达顺序（uncancel 语义）；现有测试覆盖主干，边界组合建议 Phase 2 补证。
  - D11-2：leased detached in-flight 若 factory 永不 resolve（如上游挂起），条目与预算永久占用（docstring 自述"keep counting until factory resolves"，64-69）——依赖 factory 自身超时（httpx），审计确认所有 factory 均带超时。
  - D11-3：plain `_evict_over_budget` 可逐出刚完成项自身（509-511 有二次身份检查）；被自身逐出的 entry 已 set_result——waiters 仍可拿到结果，仅 timer 不再 arm（正确），矩阵记录。
  - D11-4：进程级 `fulls`（770）跨 app 实例共享，键含 `id(scope)`（243-254）隔离——`id()` 可复用（GC 后），测试中多 app 生命周期重叠时理论撞键（低危）。

## 12. TransformPool（transform.py:195-326）

- **状态集**：信号量许可 `held∈[0,max_transforms]`；每调用者：`waiting` → `active` → `released`；异常态 `TransformBusy`（transform.py:75-76）；计数 `_active/_waiting`（206-214,276-289）。
- **事件集**：`acquire(timeout=None)`（220-246：默认 `transform_wait_seconds`，wait_for 超时→TransformBusy 242-243）；`release`（248-251）；`async with`（253-259）；`offload`（261-274：同界 worker 池 run_in_executor）；`shutdown(wait_seconds=10)`（291-326：cancel pending→守护线程有界 drain）。
- **转移函数（初稿）**：waiting --获得许可--> active（_active++ 246）；active --aexit/release--> released（_active-- 250,258）；waiting --超时--> TransformBusy（_waiting-- finally 244-245）。offload 不改状态（须在 active 内调用）。
- **终态/不变量（初稿）**：准入先于上游 GET（模块 docstring 13-26）——内存界 `max_transforms×max_response_bytes`（31-44）；offload 排队受准入自然限界（261-274）；shutdown 幂等（306-307）。
- **超时/持久化/恢复**：等待超时=transform_wait_seconds（或调用者传剩余预算，L2-CD-1 226-234）；shutdown drain 10s（app.py:79,326-337）。
- **可疑点（draft 种子）**：
  - D12-1：`acquire()` 手动路径与 `async with` 路径的 `_active` 增减配对靠调用纪律（acquire 成功 ++ / release --；aenter→acquire / aexit→--release）——混用（acquire+__aexit__）会双减；未见防御，审计扫调用点。
  - D12-2：wait_for 取消 `semaphore.acquire()` 的许可泄漏问题在 asyncio 语义下安全（acquire 被取消不占许可），但 `_active` 只在成功后 ++（246）——正确；记录确认无旧版 Python 兼容风险。

## 13. CatalogCache（catalog_cache.py:37-181）

- **状态集**：每 key：`absent` / `fresh(body,fetched_at)` / `expired（惰性删）`；缓存级：`enabled(ttl>0)` ↔ `disabled`（89-96,116-117）；账本 `_retained_bytes`+`entry_count`（77-84）。
- **事件集**：`lookup`（89-104：TTL 惰性过期在串行点删）；`refresh(key,factory)`（106-135：内部 plain SingleFlight 合并同 key 刷新，键 `("catalog-refresh",key)` 134；成功体判定：None 跳过 121-122 / 超 `max_entry_bytes` 旁路 123-124 / JSON 坏 125-128 / 非 list 129-130 / 否则 `_store` 131）；`_store`+`_evict_over_budget`（141-162：替换即先 drop 旧账，oldest-first 逐出）；`shutdown`（173-181）。
- **转移函数（初稿）**：absent --refresh 成功--> fresh；fresh --lookup 时钟超 TTL--> absent（101-103）；fresh --超 max_entries/max_bytes 被逐--> absent（149-162，刚插入项最新永不自逐 152-153）。
- **终态/不变量（初稿）**：只缓存成功体（无负缓存，11-16）；gzip 不入缓存（原始体，7-8）；shutdown 后仍可用（CD-1，173-181）。
- **超时/持久化/恢复**：TTL 秒级（env）；单飞 grace 1s（singleflight.py:97）。
- **可疑点（draft 种子）**：
  - D13-1：`refresh` 恒返回 `cache_state="miss"`（134-135）——straggler 命中 grace 窗时也标 miss；消费路由（agent/command）对该标签的语义使用需对账（是否只是观测标签）。
  - D13-2：`_fetch_and_store` 里 factory 返回 oversize/坏体时**不**写缓存但仍返回 body（123-130）——正确；但同飞 straggler 在 grace 窗内 join 得到同判定（一致）；无问题，记录路径完整性。

## 14. TurnRegistry + IncarnationStore（turn_registry.py:47-314）

- **状态集**：进程级 `incarnation`（启动冻结，231-232）；每 sid：`turn:int`（LRU OrderedDict `_turns`，233）；持久层：incarnation 文件（旧值 → 新值，64,90-98）。
- **事件集**：`load_or_bump`（130-149：新路径→legacy 回退→base+1→原子写新路径 151-200：tmp+fsync+os.replace；失败仅告警 144-148）；`bump_turn(sid)`（235-263：+1→move_to_end→LRU cap 10k 驱逐带告警 254-262）；`snapshot(sid)`（265-277：未见 sid→(inc,0)）；路径分类器 `is_turn_bumping_path`（302-314，POST+prompt_async/abort 才 bump，写路径 bump-before-send，S2 19-27）。
- **转移函数（初稿）**：文件(旧N) --启动--> 进程 inc=N+1 ∧ 文件=N+1；文件缺失/坏 → base=0 → inc=1（100-128,140）；sid turn(n) --bump--> n+1；sid 被驱逐后再 bump → 1（回退，已知取舍 51-61）。
- **终态/不变量（初稿）**：同 incarnation 内 sid turn 单调不降（驱逐唯一例外，告警可见 256-262）；snapshot 在 ingest 时冻结（digest 不回溯改，hub_types.py:162-171; global_hub.py:707-713）；restart 必增 incarnation（O4，17-19）。
- **超时/持久化/恢复**：incarnation 持久（原子写防半写复用旧 fence，151-168）；turn 不持久（O4）。
- **可疑点（draft 种子）**：
  - D14-1：写失败时 inc 仍启用（内存值），重启可能重读旧值 → incarnation 复用（fence 失效窗口）；已声明 best-effort（144-148），运维需监控该告警。
  - D14-2：bump-before-send 的洞（send 失败 turn 已进）是契约批准的放宽（21-27）；审计确认 ocdroid 词典序比较确实容忍洞。
  - D14-3：legacy 路径只在"新路径缺失/坏"时读（131-139）——新路径存在但值更旧（手工回滚场景）会取新值造成 incarnation 回退；单用户部署可接受，记录。

## 15. QpSweepShadow（qp_sweep.py:24-251）

- **状态集**：每目录：`unknown` → `known(next_run=now)`（94-107）→ 每轮评估态 ∈ {skip, budget_exhausted, cold}（185-197）→ 重新 arm（jitter×[0.8,1.2]×1800s，133-135,208）；`seen_at` 驱逐时钟（30 天，20,155-164）；预算态：`_budget_day/_budget_used`（UTC 日滚动，141-145）。
- **事件集**：`observe_directory`（hub 摄入观察者同步回调，app.py:511；运行中 set wake 106-107）；`record_activity`（109-114，q/p IMMEDIATE 时 global_hub.py:674-678）；`_run` 调度循环（211-220：`_next_sleep` min deadline 封顶 30s，166-171）；`run_once`（173-209）；`start/stop`（222-237）。
- **转移函数（初稿）**：known∧due --run_once--> {elapsed<3×interval→skip；used≥budget→budget_exhausted；else→cold∧used++∧est+=2KiB} → next_run=now+jitter。seen_at 30 天未见 → 三表全删（155-164）。
- **终态/不变量（初稿）**：纯影子（无上游 IO，模块 docstring 1-7）；markers deque 256（73）；预算每日重置；jitter 夹逼 [0.8,1.2]（134）。
- **超时/持久化/恢复**：无持久化；睡眠封顶 30s + wake event 即醒。
- **可疑点（draft 种子）**：
  - D15-1：`run_once` 的 `last_activity` 回退 `_seen_at.get(directory, timestamp)`（182）——从未活动过的目录 elapsed=0 → 恒 skip；目录只有持续 90 分钟无 q/p 活动才可能 cold——语义符合"冷目录清扫"，记录口径。
  - D15-2：`observe_directory` 在 `_ingest_directory_source` 每轮把 activity 全量重 touch（124-126）——seen_at 刷新使 30 天驱逐实际只淘汰"完全无摄入"的目录（设计意图）；但 activity 字典本身由 GlobalHub 持有无界增长？——qp_last_activity 由 `_directory_form` 观察写入（global_hub.py:109,678），目录数=客户端使用的目录数，实际有界；审计记录该隐式界。
  - D15-3：wake 风暴：运行中每次 observe 都 set event（107）→ `_run` 醒来 run_once→再睡——高频新目录时循环变忙（run_once 本身便宜）；封顶缺失，低危。

## 16. app lifespan 资源编排（app.py:189-731）

- **状态集**：`AsyncExitStack` 注册序（app.py:216-234 注释）：access-log handler → snapshotter → upstream → transforms → fulls → catalog_cache → raw_fetch_registry(可选) → replay_log(close) → replay_sweep → hubs → qp_sweep(可选) → token_hub → dbaux → maintenance(可选)；关停=LIFO 逆序（728-731）。
- **事件集**：启动序列（236-727：setup_logging/validate 195-196；smoke 620；banner 633-648；后台任务后置 649-726）；`yield`（727）；shutdown/启动失败/取消 → `__aexit__` 全链清理（729-731，每回调独立 try/except 隔离，如 254-265,297-304,310-315,326-337,348-355,368-375,390-397,433-441,457-470,495-500,514-517,543-551,605-615,686-719）。
- **转移函数（LIFO 序，初稿）**：maintenance(30s drain,70,686-719) → dbaux(5s,85,605-615) → token_hub.stop(543-551，须先于 hubs：NB-C4 548-550) → qp_sweep(514-517) → hubs.close(495-500) → replay_sweep stop(457-470) → replay_log.close(433-441) → raw_fetch/catalog/fulls（348-397，均在上游 aclose 前）→ transforms(10s,79,326-337) → upstream.aclose(310-315) → snapshotter.final(297-304) → access-log handlers close(254-265)。
- **终态/不变量（初稿）**：启动失败也走同链（P0-1，216-235）；token_hub.stop 先于 hubs.close；单飞/缓存关停先于 upstream aclose（348-355 rationale）；uvicorn `timeout_graceful_shutdown=5s`（97,780）管连接排空。
- **超时/持久化/恢复**：各级 drain 常量：maintenance 30s / transform 10s / dbaux 5s / uvicorn 5s。
- **可疑点（draft 种子）**：
  - D16-1：LIFO 全链最坏串行 ≈ 30+5+0+…+10+0 ≈ 50s+，远超 uvicorn 5s 连接宽限；docs/operations 的 systemd TimeoutStopSec=15 → SIGKILL 可能截断后段清理（snapshotter 终帧/access-log flush 丢失）。Phase 2 需对 operations.md 核对实际预算。
  - D16-2：`app.state.qp_sweep.stop()` 无 try/except 包裹（514-517，`_stop_qp_sweep` 裸 await）——异常会中断后续 LIFO（token_hub/hubs 不清理）；qp_sweep.stop 内部已吞 CancelledError（qp_sweep.py:228-237），理论不抛，但缺乏与其它回调一致的隔离，属一致性缺口。
  - D16-3：`smoke()` 结果一次成型（620），进程存活期内不刷新——health 的 schema_degraded 可能长期陈旧（见卡 20）。

## 17. dbaux cursor 编解码校验链（dbaux/cursor.py:42-208）

- **状态集**（解码出口）：`None`（缺席/合法跳过）｜`CursorPayload(t,i,f)`｜异常 `InvalidCursorError(reason∈{charset,decode,json,shape,type,empty_anchor})`（58-68）；指纹态：`f={archived,parent,search_hash,allowlist_rev}` 四键全量（54-55）。
- **事件集**：`encode_cursor`（158-162：键序固定 t,i,f、compact、ensure_ascii、base64url 无 padding）；`decode_cursor`（165-208 六段链）；`build_fingerprint`（126-145：search trim→sha256[:16]，None→哨兵 ""；allowlist 归一化集合→canonical JSON→hash；archived/parent 缺省归一 116-123）；`fingerprint_mismatch`（148-155，任何差异→True→调用方 400 invalid_cursor）。
- **转移函数（decode 链，初稿）**：`raw∈{None,""}`→None（181-183）→ 字母表 fullmatch 失败→charset（183-184）→ b64 补 padding 解码失败→decode（186-188）→ UTF-8/JSON 失败→json（190-192）→ 顶层键集≠{t,i,f} 或 f 键集≠四全→shape（193-197）→ t 非 int（bool 显式拒）/i 非 str/f 值非 str→type（199-202）→ i==""→empty_anchor（203-207）→ 成功。
- **终态/不变量（初稿）**：纯函数零 IO；语法校验先于 503（§8.3，模块 docstring 10-12）；同输入两次 encode 逐字节相同（29-30）；哨兵 "" 与 16-hex 无碰撞（24）。
- **超时/持久化/恢复**：无（无状态）。
- **可疑点（draft 种子）**：
  - D17-1：`_B64URL_RE.fullmatch(raw)` 先行（183）而 `isinstance(raw,str)` 检查在其后同一条件（`not isinstance or fullmatch is None`）——FastAPI query 恒 str，防御位次无害；记录。
  - D17-2：超长合法 cursor / 控制字符 i 通过（docstring 177-179 声明"不过度防御"）——B4 业务层是否再限长需核对（防 DoS 面）。
  - D17-3：`fingerprint_mismatch` 对畸形输入恒 True（153-155）——payload 指纹多键/少键已过不了 shape，此处仅防御性，双保险记录。

## 18. ActionRegistry.invoke 单飞/confirm/min_interval（actions.py:466-975）

- **状态集**：每 action 名：`idle` ↔ `in_flight`（484,517-521,569-570）；全局信号量许可（481）；`_last_run[name]`（487）；执行器子态：spawning → running → {exited, timeout, cancelled}（608-740，outcome 639）。
- **事件集**：`invoke(name,confirmed)`（505-570）：enabled 门（509-510）→ found 门（511-513）→ 单飞行门（517-521→429 throttled）→ confirm 门（524-527→409）→ min_interval 门（529-535→429+Retry-After）→ 信号量准入 2s（539-547→503 busy；取消路径审计后重抛 548-556）→ `_execute`（557-559，finally release 560-561）→ 成功审计（565-567）；`finally: _in_flight.discard`（569-570）。
- **转移函数（执行器，初稿）**：spawn（shield 任务，648-651；失败→503 unavailable 652-659）→ 双管道并发 drain（665-671）→ `_wait_exit` returncode 轮询（909-931；超时→504,673-676）→ killpg（683,933-940）→ 有界 drain 5s（684-686,875-907，超时强取消取部分输出 truncated=True）→ build result（687-689）；CancelledError→outcome=cancelled 重抛（690-696）；finally 统一 `_cleanup`（killpg+reap 5s+失败审计，697-740，含 spawn 中途取消的句柄回收 714-731）。
- **终态/不变量（初稿）**：进程组必杀（start_new_session=True，787；killpg 幂等 936-940）；每条退出路径恰一次审计（含 throttle/busy/confirm/disconnect，517-556）；env 白名单 fail-closed（86-100）；shell=False+插值标记扫描（49,73,340-345）。
- **超时/持久化/恢复**：单动作 timeout 1-600s 默认 30（53,362-366）；准入 2s（64）；drain 5s（62）；reap 5s（63）；min_interval 内存态重启清零（486-487）。
- **可疑点（draft 种子）**：
  - D18-1：`_last_run[name]` 在**获得信号量后**才写（558）——parked 期间 min_interval 不计时：极端排队下实际执行间隔可大于声明的 min_interval（行为更保守，无害；语义与"节流自调用起算"的口径差记录）。
  - D18-2：单飞行标记在信号量等待期间已持有（521 add 在 acquire 前）——并发的同名调用在等待期即 429，即使首调用最终 spawn 失败（正确防重入；口径记录）。
  - D18-3：`_wait_exit` 50ms 轮询（931）+ timeout 判定用 wall monotonic——OK；grandchild 逃逸进程组（setsid）时 killpg 不及，drain 5s 兜底后 partial 输出（899-906）——设计已述，泄漏的孙进程不在清理域（记录为已知边界）。
  - D18-4：双 cancel 窄窗（Bug D 自述 732-738）disconnect 审计依赖 `outcome=="cancelled"` 分支——若 spawn 失败 ∧ 取消同时到，走 652-659 的 unavailable 审计而非 disconnect 审计（审计归类可能不精确，低危）。

## 19. access-log 维护循环 + TrafficSnapshotter（access_log.py:668-726 + traffic_snapshot.py:289-433）

- **状态集**：维护循环：`sleeping(interval)` → `compress` → `prune` → `extra_prune`（快照清理 piggyback）→ sleeping；停止态（stop_event）。TrafficSnapshotter：`inactive`（未启/首帧失败/ledger 禁用，341-369）→ `active`（loop 任务，372,402-405）→ `stopped`（终帧后，374-400）。
- **事件集**：循环体（703-726：wait_for(stop_event, interval)→to_thread compress 715→to_thread prune 719→to_thread extra_prune 722-726；每步独立 except 吞错）；`_MAINT_LOCK` 互斥（56,463-470,556-565,597-603）；快照 `_loop`（411-428：sleep interval→_write_once，逐次异常守卫）；`start/stop`（341-400）。
- **转移函数（初稿）**：sleeping --interval 到/stop 超时--> 维护三连（失败仅告警不退环）--> sleeping；stop_event set → 立即退出（708-709）。snapshotter：inactive --start 首帧成功--> active；active --每 interval--> 追加一帧；active --stop--> 终帧（cancel→await→_write_once 389-400）。
- **终态/不变量（初稿）**：循环永不因单次失败退出；gzip/prune 互斥串行；快照停机必补终帧（397-400）；首帧失败=诚实 inactive 不重试（359-369）。
- **超时/持久化/恢复**：维护间隔 env；lifespan drain 30s 后强 cancel（app.py:70,686-719，in-flight to_thread 线程不 join 自行完成，691-700）；快照按日文件（300-303）。
- **可疑点（draft 种子）**：
  - D19-1：循环内三步串行 to_thread——单步卡死（如巨文件 gzip）会延迟整环与快照 prune；`_MAINT_LOCK` 保证互斥但不保证时限（自述 699-700），运维观测点。
  - D19-2：`extra_prune` 绑定用 functools.partial 位置参（app.py:669-674，注释自述历史 TypeError bug）——现状正确；快照 prune 与 access prune 共享 `today`（tick 内一致）。
  - D19-3：snapshotter `stop()` 里终帧 `_write_once()` 失败被忽略（400 注释 best-effort）——关停丢最后一帧的窗口（低危，记账）。

## 20. smoke 探针 + readiness 静态门（app.py:44-186 + readiness.py:1-187）

- **状态集**：smoke：`not_run` → {`valid`, `invalid_schema`(schema_degraded=True), `upstream_unavailable`}，另有"无会话→保持 not_run"（app.py:51-54,139-155,169-186）；一次性、启动后冻结。readiness：`SATISFIED ⊆ REQUIRED`（10 特征，readiness.py:58-69,93），含依赖蕴含 `post-actions∈S ⇒ boundary∈S`（126-144）；随代码版本静态、导入期双重校验（186-187）。
- **事件集**：smoke：startup 单次调用（app.py:620）——/session 列表取 sid（139-151，5s 超时 61）→ /session/{sid}/message 形状校验（156-186）；readiness：无运行时事件——selector 405 门请求时动态读（selector.py:265-266）、versions/health 端点读 payload（readiness.py:163-179）。
- **转移函数（smoke，初稿）**：not_run --取列表失败/消息失败/≥300--> upstream_unavailable；--无会话--> not_run（维持）；--形状合法--> valid；--形状错--> invalid_schema∧degraded。
- **终态/不变量（初稿）**：`schema_degraded=True` 仅 invalid_schema（46-50）；readiness `ready ⇔ f(REQUIRED)⊆f(SATISFIED)` 双向派生（147-160）；§16.3 第四格（boundary∉∧post∈）结构性不可达（readiness.py:35-39,126-144）。
- **超时/持久化/恢复**：smoke 5s 超时（61）；无恢复/不刷新；readiness 无持久化（纯代码态）。
- **可疑点（draft 种子）**：
  - D20-1：smoke 终身不刷新——上游恢复/劣化后 health 展示陈旧（"startup smoke" 语义边界，审计确认 health 消费方理解）。
  - D20-2：readiness 消费"call-time 查模块全局"（readiness.py:45-48）——热替换模块对象的部署方式（罕见）会撕裂一致性；单进程静态加载安全。
  - D20-3：smoke 的 valid 判定只查 `info.id:str` 与 `parts[].type:str`（178-180）——浅形状校验；上游新增必填字段迁移时可能漏报（与 skeleton 投影字段的耦合演进风险）。

---

## 汇总统计

- 卡片总数：**20**
- 可疑点（draft 种子）总数：**D 系列编号 1+2+…+20 号段，共 44 条**（D1:4、D2:3、D3:5、D4:2、D5:3、D6:6、D7:3、D8:2、D9:4、D10:5、D11:4、D12:2、D13:2、D14:3、D15:3、D16:3、D17:3、D18:4、D19:3、D20:3）。
- 状态×事件规模（每卡主转移表口径）：

| # | 卡片 | 状态数×事件数 |
|---|---|---|
| 1 | selector 版本/目录选择器 | 5×11 |
| 2 | catch-all 404 终端边界 | 2×5 |
| 3 | GlobalHub 上游订阅生命周期 | 8×12 |
| 4 | HubRegistry 准入/宽限拆除 | 4×6 |
| 5 | Subscriber 控制面出站队列 | 2×4 |
| 6 | TokenStreamHub 累加器+双账本 | 9×14 |
| 7 | TokenSubscriber 握手/运行分离 | 3×5 |
| 8 | TokenStreamRegistry 准入账本 | 3×5 |
| 9 | ReplayLog+replay_wire | 7×7 |
| 10 | DbAuxiliarySource+LatencyBreaker | 6×8 |
| 11 | SingleFlight plain/leased | 7×8 |
| 12 | TransformPool | 3×4 |
| 13 | CatalogCache | 3×4 |
| 14 | TurnRegistry+IncarnationStore | 3×4 |
| 15 | QpSweepShadow | 5×5 |
| 16 | app lifespan LIFO | 15×3 |
| 17 | dbaux cursor 校验链 | 3×4 |
| 18 | ActionRegistry.invoke | 5×8 |
| 19 | access-log 维护+Snapshotter | 4×4 |
| 20 | smoke+readiness 静态门 | 6×2 |
