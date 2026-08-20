# E1-07 精读卡片：singleflight.py / transform.py / catalog_cache.py

审计基线：pyproject `version = "4.4.0"`，`requires-python = ">=3.11"`。引用行号均为当前工作树。

---

### src/oc_slimapi/singleflight.py（770 行）

- **职责**：per-key single-flight 去重共享上游 GET——单一 `SingleFlight` 类承载两个 profile（B6-1 合并产物）：plain（`max_bytes=None`，join-or-lead `fetch()`，完成后结果保留 ~1s grace 供 admission 序列化的同 key 请求合流；调用方 = 进程级 `fulls` 注册表（direct /full + merged fan-out）与 catalog-cache 刷新防踩踏）与 leased（`max_bytes` 必填，`fetch_or_bypass()` 字节预算 admission + Lease 纪律；调用方 = per-app `raw_fetch_registry`（列表路由上游 GET））。只共享 raw fetch，transform 不在本模块视野内（L2-CD-1 oracle §C-2，`singleflight.py:31-33`）。
- **对外符号（逐类逐方法）**：
  - 模块常量：`_DEFAULT_RESULT_GRACE_SECONDS=1.0`（97）、`_MAX_RETAINED_ENTRIES=64`（103）、`_MAX_RETAINED_BYTES=32MiB`（104）；所有权状态 `IN_FLIGHT/GRACE/RETAINED/FAILED`（108-111）；层常量 `ACTIVE/RETIRED`（114-115）；`_REJOIN` 哨兵（120，flight 死亡后调用方须重入串行点）；`FactoryT`（91）。
  - `FetchFailed`（123-137）：失败 RESULT 信封（`__slots__=("exc",)`）；leader 用 `set_result(FetchFailed(exc))` 而非 `set_exception`，零 waiter 失败不触发 "Future exception was never retrieved" 告警（124-131）。
  - `_Entry`（140-179）：一代 flight。`__init__`（157）——`future` 在构造时用 `get_running_loop().create_future()`（160）；`caller_refs`（leased 引用计数，plain 恒 0）；`accounted`（预算已入账标志，dual-refund 规则，166-167）；`expires_at`（None=在飞，monotonic deadline=grace，168）；`timer`（grace 到期 `TimerHandle`，shutdown 可取消，rev-9，171-172）；`size`（plain 完成后按 `len(result)` 计，175）；`in_flight` property（177-179，`expires_at is None`）。
  - `_current_task_cancelling()`（182-186）：3.11+ `task.cancelling() > 0`，区分"我自己被取消"与"flight 死了"。
  - `Lease`（189-240）：leased 调用方句柄。`__init__`（215）、`__aenter__`（221）、`__aexit__`（224，调 `_release`）、`_release`（228-240）：幂等守卫 + **先切断 `self.body=None; self._entry=None` 再递减 caller_refs**（237-240；终审 rev-1 修复：已释放 Lease 不得跨后续 await 持有共享 raw body，防 zombie generation）。
  - `full_fetch_key(scope, sid, mid, directory)`（243-254）：`("full", id(scope), sid, mid, directory)`；scope=app 的 TransformPool，`directory` 必须入 key。
  - `SingleFlight`（257-761）：
    - `__init__`（273-337）：profile 由 `max_bytes is not None` 决定；跨 profile kwargs 即早 `TypeError`（295-308）；plain 注入 retention 上限（330-337）；leased 建 `asyncio.Semaphore(network_concurrency)`（321-324）。
    - `leased_bytes` property（343-347）：当前预算占用。
    - `in_flight(key)`（349-352）：方法名与 `_Entry.in_flight` property 同名异构（方法 vs 属性）。
    - `snapshot()`（354-366）：`{key: [(layer, seq, caller_refs, state), ...]}` 双层账本视图。
    - `fetch(key, factory)`（372-391）【plain】：`while True` 循环——`_expire_if_due` → 无 entry 则登记新 `_Entry` + `_evict_over_budget` + `_lead`（384-388）；有则 `_join`，`_REJOIN` 则重入循环（389-391）。永不 bypass。
    - `fetch_or_bypass(key, factory, reserve_bytes)`（393-428）【leased】：同步串行点（上方无 await）——无 entry：`_try_reserve` 失败返回 `None`（bypass，418）；成功则 `_new_entry` 入账 + **leader ref 在 factory 之前登记**（421）+ `_lead` 结果直接包 `Lease`（422）。有 entry（在飞或 grace）：**waiter ref 在 await 之前 +1**（424）→ `_join` → `_REJOIN` 则 `continue` 重入串行点（426-427）。
    - `_join(entry)`（430-455）：`await asyncio.shield(entry.future)`（438）+ 三分支取消机：`CancelledError` → 先 `_release_caller`（440）→ `_current_task_cancelling()` 为真 → 分支③ 原样 `raise`（441-446，自身取消，共享 future 不受影响）；否则分支② fall-out → 返回 `_REJOIN`（447-450，旧 ref 已释放，重入串行点 re-join/re-reserve/re-lead）。`FetchFailed` → 分支① `_release_caller` 后 `raise result.exc`（451-454，所有 waiter 同一异常实例）。plain 下 `_release_caller` 为 no-op，两 profile 分支结构行为等价（434-436）。
    - `_lead(entry, factory)`（457-481）：factory（leased 且有 `_network_sem` 时在信号量内执行，462-464）；`CancelledError` → 分支② `_fail(entry)` 后 `raise`（467-471）；其他异常 → 分支① `_fail(entry, exc)` 后 `raise`（472-476）；成功 → **先发布 `set_result` 给 waiter，再做所有权转换**（478-480）。
    - `_convert_success(entry, result)`（487-534）：plain——二次身份校验（`_entries.get(key) is entry`，防 shutdown/替换，503）后设 `expires_at`、按 `len(result)` 计 `size` 入 `_retained_bytes`、`_evict_over_budget`、再身份校验后挂 `call_later` 主动到期回调（504-520；新完成 entry 可能被自身逐出，故第二次校验防无谓回调）。leased——`ACTIVE+IN_FLIGHT` 原地转 `GRACE` + 到期 timer（522-529）；`RETIRED+IN_FLIGHT`（shutdown 分离后的迟到成功）→ `RETAINED`，无 grace/timer/重入账（530-533）。
    - `_fail(entry, exc=None)`（535-554）：plain——`_drop` 后 `_fail_future`（542-545）。leased 顺序敏感：**leader ref 先释放 → 摘除 active 注册 → 转 `RETIRED/FAILED` 墓碑 → 立即 `_refund`（先于 waiter 唤醒，使 waiter re-lead 能拿到释放的字节）→ `_fail_future` → 无子嗣即 `_reap`**（546-554）。
    - `_fail_future(entry, exc)`（556-566）：future 已 done 则 no-op；`exc is None` → `future.cancel()`（分支②）；否则 `set_result(FetchFailed(exc))`（分支①）。
    - `_expire_if_due(key)`（568-575）：串行点前的惰性 grace 到期检查。
    - `_expire_grace_entry(key, entry)`（577-591）：timer 回调与惰性尾部共用；身份校验先行（582）；plain 到期即 `_drop`（587-588）；leased 仅处理 `GRACE+ACTIVE`（590-591）。
    - `_expire_grace(entry)`（593-601）：leased grace → 取消 timer → `_drop_grace`。
    - `_drop_grace(entry)`（603-611）：active/grace → retired/retained；无 caller 立即 `_reap`。
    - `_release_caller(entry)`（613-625）：plain 直接 return（614-615）；leased 递减；归零后按状态定命——`RETAINED`/`FAILED` → `_reap`（621-624），`GRACE` 留 body 给迟到者，`IN_FLIGHT`（含 detached）预算继续持有（625-626）。
    - `_reap(entry)`（628-633）：删墓碑 + 条件 refund + 防御性摘 active。
    - `_refund(entry)`（635-638）：`accounted` 双refund守卫。
    - `_detach_from_active(entry)`（640-642）：身份校验后摘除。
    - `_drop(key)`（648-660）：plain 按 key 删除 + 取消 timer + 归还 retained bytes。
    - `_evict_over_budget()`（662-677）：plain 双上限（条数/字节）强制；**只逐出 oldest COMPLETED（`expires_at is not None`），永不逐在飞**；两个串行点调用（插入后 387、完成后 509）。
    - `_try_reserve(needed)`（683-696）：单 flight 超总预算 → False（684-685）；放不下则按插入序逐出**零 caller 的 GRACE** entry（CD-1 纪律）再判（690-696）。
    - `_new_entry(key, reserve_bytes)`（698-703）：seq 自增 + `accounted=True` + 入账。
    - `shutdown()`（709-761）：注册表收敛后**保持可用**（CD-1）。plain：逐 entry 取消 timer + `_drop`，单 key 清理失败被隔离（try/except + 从 entry 自身字段强制退还，730-747）；retained ledger 清零。leased：逐 active entry 原子转换——in-flight → retired/detached（仍计数，future 不动，756-757）；grace → retained，无 caller 立即 reap（758-761）。**在飞 future 永不 cancel**，迟到完成路径靠注册身份复检兜底。
  - `LeasedSingleFlight = SingleFlight`（766）：B6-1 兼容别名。
  - `fulls = SingleFlight()`（770）：进程级 plain 注册表（direct /full + merged fan-out 跨形态去重）。
- **依赖 / 被依赖**：仅依赖 stdlib（`asyncio`、`time`）。被依赖（rg 反查）：`src/oc_slimapi/app.py:32`（import `LeasedSingleFlight, fulls`；385 建 `raw_fetch_registry`，348-355/390-397 lifespan shutdown）；`src/oc_slimapi/routes/messages.py:18`（`full_fetch_key, fulls`；566 direct /full 共享 GET；994-1001 列表 join-first lease 路径）；`src/oc_slimapi/routes/sessions.py:727,838`、`routes/questions.py:144`、`routes/permissions.py:166`（lease 消费方）；`src/oc_slimapi/catalog_cache.py:32`（刷新防踩踏）；`src/oc_slimapi/config.py:393`（文档引用）。测试：`tests/test_leased_singleflight.py`（协议全锁）、`tests/test_full_absorb.py:305-527`（plain 单元）、`tests/test_messages_merged.py`、`tests/test_messages_coalesce.py`、`tests/test_sessions_coalesce.py`、`tests/test_questions_coalesce.py`。
- **状态 / 可变性**：单线程 asyncio 专用（docstring 270 声明 Not thread-safe）。核心可变状态：`_entries`（joinable 层，plain 平铺视图 = leased ACTIVE 层）、`_retired`（leased 墓碑 `(key, seq)→entry`）、`_leased_bytes`（预算 ledger，不变式 = Σ reserve of {in-flight(含 detached), grace, retained}，64-69）、`_retained_bytes`（plain retained 计量）、`_seq`（代序号）、`_network_sem`（仅 leader factory）。锁语义完全靠"串行点"（无 await 的同步段）而非 mutex。
- **错误路径**：lead 常规异常 → `FetchFailed` RESULT 信封 → 全部 waiter `raise result.exc`（**同一异常实例**，451-454），entry 丢弃，绝不负缓存（下一个请求重试）；lead 被取消 → `future.cancel()`（未包裹，564）→ 存活 waiter 走分支② `_REJOIN` → 重入串行点 re-lead（立即 refund 使 re-lead 可预约）；waiter 自身取消（含 registered-ref→await 窗口）→ 分支③ 自身 ref exactly-once 释放 + `CancelledError` 传播，共享 future 与他人不受影响。路由层后果：joined caller 与 leader 一起等 httpx 超时 → 503 `upstream_unavailable`（CHANGELOG 1.5.0 行为披露②，`CHANGELOG.md:284`）。
- **疑问点（16）**：
  1. **[中] plain `_fail` 按 key 删除不做身份校验**（542-545 `self._drop(entry.key)`）：若 leader 的 factory 运行期间 key 的注册 entry 被替换（如 shutdown 清空后新 caller 重建同 key flight），旧 leader 失败会把**新 entry** 从注册表摘掉——新 flight 的后续 joiner 无法发现它（去重瞬时丢失、多发一次上游 GET），且 `_evict_over_budget` 的"在飞不逐出"不变式被绕过。与 `_convert_success`（503/510）和 `_expire_grace_entry`（582）的身份校验纪律不一致。无内存泄漏（新 leader 的 `_lead`/`_convert_success` 有身份兜底），但属防御缺口。
  2. **[低] 混合时钟**：plain grace timer 用 `call_later(max(0.0, entry.expires_at - self._clock()))`（517-519）把**注入 clock** 的差值喂给 **loop time**；leased timer 却直接 `call_later(self._grace, ...)`（527-528）完全绕开注入 clock；`_expire_grace_entry` plain 分支又用 `self._clock()` 复检 `expires_at <= clock()`（587）。测试注入快进 clock 时，timer 回调触发瞬间 `clock()` 可能尚未越过 `expires_at` → `_drop` 被跳过且无后续 timer → entry 滞留（直到同 key 下次 fetch 惰性触发或其他 key churn 引发逐出）。生产 monotonic 时钟下窗口极小。
  3. **[低] `self._leased = max_bytes is not None` 赋值两次**（293 与 309）：冗余，纯代码卫生。
  4. **[低] `id(scope)` 键复用**：`full_fetch_key` 用 `id(TransformPool)`（254）。app 销毁后 pool 对象被 GC、新 app 的 pool 恰好复用同一 id 时，可与旧 app 尚在 grace（≤1s）的 entry 撞 key 共享 body。shutdown() 正常清空 + 1s grace 使窗口极小，但测试中快速建/拆 app 且未 shutdown 时可复现。
  5. **[信息] 分支②/③ 竞态合流**：waiter 自身取消与 flight 死亡同时发生时 `_current_task_cancelling()` 判真 → 走分支③ `raise`（441-446），此时该 caller 不再 re-lead——一次取消同时命中两种语义时保守取"自身取消"，可接受但未在 docstring 明示。
  6. **[低] leased `_try_reserve` 逐出只看插入序**（690-695）：`dict` 插入序 ≈ oldest first，但没有按 reserve 大小或等待时长的公平性——大数据集 flight 先到先占，小 reserve 请求在零-caller GRACE 耗尽后直接 bypass（返回 None 直取，行为正确，无饥饿错误，但去重覆盖率对小请求不公平）。
  7. **[信息] reserve 不按实际 body 调整**（26-28 明示 deliberate）：messages lease 路径 `reserve_bytes=config.max_response_bytes`（`routes/messages.py:833`），默认 64 MiB（`config.py:365`）= `raw_fetch_max_bytes` 全部预算（`config.py:404-406`）→ **默认配置下同时只容 1 个 leased flight**，第二个并发不同 key 直接 bypass 直取（与 CHANGELOG.md:283 披露一致）。预算利用率与去重率在此默认下最低。
  8. **[信息] 共享 raw bytes 免拷贝**：joiner 与 leader 拿到**同一 body 对象**（`Lease.body`，428）。正确性依赖所有消费方只读（messages/questions/permissions 均 `orjson.loads` 后 `del` 局部引用，`routes/questions.py:186-198`）；任何路由就地修改共享 body 会污染并发 joiner。当前无违例，属隐式契约。
  9. **[信息] exactly-once 释放回归**：`Lease._release` 幂等守卫（229-231）+ 引用切断（237-238）+ `_fail` 先释放 leader ref（546）+ waiter 在 `_join` 两分支各自释放（440/453）+ `_release_caller` plain no-op（614-615）——leased 引用计数在三分支下均恰好一次；`accounted` 守卫（635-638）保证 refund 恰好一次。`tests/test_leased_singleflight.py:724-774` 锁定引用切断。**回归完整**，未见双释放/泄漏路径。
  10. **[信息] 取消→503 误映射检查**：分支③ 的 `CancelledError` 原样 `raise`（446），`fetch_or_bypass`/`fetch` 均不吞——路由层客户端断连时 uvicorn 取消 handler 任务，传播为连接中止而非 503；`TransformPool.acquire` 的 `TimeoutError` 才映射 `TransformBusy`（`transform.py:242-243`）。未发现取消被误映射为 503 的路径。
  11. **[信息] grace 窗口竞态（join 一致性）**：grace entry 被 `_expire_grace`/`_drop_grace` 转 retired 的瞬间，已通过 424 行 `caller_refs += 1` 且正在 `await self._join(entry)` 的 straggler 不受影响——shield 等的是 future（已完成），retained 状态下 `_release_caller` 归零才 reap（621-622）。竞态闭合。
  12. **[低] `_evict_over_budget` 的条数上限把在飞 entry 计入**（669）：>64 个并发不同 key 在飞时循环因"无 completed 可逐"而 break——上限对在飞不成立，docstring 101-102 已声明由调用方 admission 兜底（plain 调用方是 `fulls`，其 caller 在 transform pool 内，`max_transforms=1` 默认下在飞数实际 ≤1+merged fan-out；但 merged fan-out 的 per-mid fetch 在 admission 释放后执行（oracle §C-2，`routes/messages.py:1042-1045`），fan-out 宽度 `merged_fanout`（≤16，`config.py:1081`）+ 并发 direct /full 理论上可超 64——仅损失 grace 保留，无内存上界破坏（retained bytes 上界仍被强制）。
  13. **[信息] `shutdown()` 后迟到的 leased `_fail`**：detached in-flight leader 失败 → `_fail` 走 leased 分支：`_detach_from_active` 身份 no-op、墓碑重复写入同 `(key,seq)`（550，幂等覆盖）、refund 恰一次——账本闭合。
  14. **[低] `_release_caller` 对 `caller_refs` 已为 0 的 entry 调用时静默跳过递减**（616-617）：不视为 bug（所有路径配对），但守卫写法意味着"多释放一次"不会炸、只会漏检——审计上弱失败模式。
  15. **[信息] `_REJOIN` 无限循环风险**：分支② 后 `while True` 重入；若极端交错下每次 re-join 都遇上被取消的 flight，理论上活锁（每次都有新 leader 被取消）。实际受上游 httpx 超时与调用方取消约束，未见现实路径，记录备查。
  16. **[信息] `snapshot()` 不做 plain/leased 区分**：plain 调用 `snapshot()` 返回 state 恒 `IN_FLIGHT` 的伪账本（145-149 声明 plain 语义下 state 恒 IN_FLIGHT）——仅 ops 观测，无消费方依赖（rg 未见 src 内调用，仅测试）。

---

### src/oc_slimapi/transform.py（326 行）

- **职责**：有界 transform 池——`asyncio.Semaphore(max_transforms)` admission（**先于上游 GET 获取**，约束内存：任意时刻至多 `max_transforms` 份 body 被缓冲，31-44 RSS 模型）+ 同尺寸 `ThreadPoolExecutor` 承载 CPU 工作（orjson parse → 投影 → dumps → gzip），保证事件循环对 SSE 心跳空闲（1-29）。附带模块级工具：`read_with_cap` cap-read、`strip_diagnostics_and_pack` /full worker 入口、`_pack_json` 序列化+gzip+Vary。
- **对外符号（逐类逐方法）**：
  - `TransformConfig`（66-73，frozen dataclass）：`max_transforms` / `transform_wait_seconds` / `max_response_bytes` 三旋钮快照。
  - `TransformBusy`（75-76）：admission 超时异常，路由映射 503 `transform_busy`。
  - `_pack_json(value, accept_encoding, *, merge_directory_vary=False)`（79-101）：dumps → `compress_if_beneficial` → 恒带 `Vary: Accept-Encoding`；`merge_directory_vary=True`（v3 契约 §6.2 gate B1）追加 `X-Opencode-Directory` 维度。纯 CPU，worker 安全。
  - `strip_diagnostics_and_pack(body, *, accept_encoding, merge_directory_vary=False)`（104-140）：/full worker 入口——`orjson.loads`（128）→ 非 dict `raise ValueError("upstream single-message body is not a dict")`（129-135，防坏上游 200 以 200 透出）→ `strip_diagnostics_message` 原地轻剥离（无 deepcopy，116-118）→ `_pack_json`。空/坏 JSON 抛 `orjson.JSONDecodeError`，路由映射 503。
  - `read_with_cap(response, max_bytes, *, chunk_size=64KiB, on_read=None)`（143-192）：流式 cap-read——`max_bytes<=0` 短路 `(None, 0)`（180-181）；逐 chunk 累计后**先 `on_read(len(chunk))` 再判 cap**（185-190，三条出口路径的字节归因统一，P0-9）；越限返回 `(None, total)`（未缓冲整个超限 body，≤ max_bytes+chunk）；中途异常 chunk 已归因后原样传播（169-173）。
  - `TransformPool`（195-326）：
    - `__init__(config)`（206-214）：semaphore + `ThreadPoolExecutor(max_workers=max_transforms, thread_name_prefix="oc-slimapi-transform")` + `_active/_waiting` 计数器。
    - `config` property（216-218）。
    - `acquire(timeout=None)`（220-246）：`wait_for(semaphore.acquire(), timeout=timeout if not None else transform_wait_seconds)`（L2-CD-1 预算收窄：重试者传剩余墙钟预算，防 N× 全额等待，221-231）；`TimeoutError → TransformBusy`（242-243）；`_waiting` 在 finally 归还（235/244-245）；成功后 `_active += 1`（246）。成功 acquire 必须配对恰一次 `release`。
    - `release()`（248-251）：`_active -= 1` + `semaphore.release()`。
    - `__aenter__`（253-255）→ `acquire()`；`__aexit__`（257-259）→ 复制 release 逻辑（未复用 `self.release()`）。
    - `offload(func, *args, **kwargs)`（261-274）：`loop.run_in_executor(self._executor, ...)`，kwargs 用 `functools.partial` 包装（270-273）；executor 与 admission 同尺寸 → 排队天然有界（调用方持 slot 期间 await offload，264-267）。
    - `snapshot_metrics()`（276-289）：`{"active", "waiting"}`（P2-3，供 `sse/registry.py:373` 读取，不摸私有信号量字段）。
    - `shutdown(wait_seconds=10.0)`（291-326，P1-41）：`executor.shutdown(wait=False, cancel_futures=True)` 立即取消 pending（310）→ 守护线程内 `shutdown(wait=True)` 有界 drain（313-325）→ `done.wait(timeout)` 超时即返回，不阻塞事件循环/进程退出（302-304）。幂等。
- **依赖 / 被依赖**：依赖 `orjson`、`.etag.merged_vary`、`.gzip_util.compress_if_beneficial`、`.skeleton.strip_diagnostics_message`、stdlib（`asyncio/functools/threading/concurrent.futures/dataclasses`）。被依赖：`app.py:40,320-324`（lifespan 构造 + 326-337 有界 drain）；`discovery.py:40`；`routes/messages.py:26-28`（TransformBusy/read_with_cap/strip_diagnostics_and_pack）；`routes/_read_passthrough.py:62`（TransformPool 类型 + admission 上下文）；`routes/{directories,diff,todo,agent,command,sessions}.py`（TransformBusy + read_with_cap）；`routes/{permissions,questions,write_groups}.py`（read_with_cap）；`sse/registry.py:71,87`（snapshot_metrics 接线）。测试：`tests/test_transform.py` 及全部路由测试。
- **状态 / 可变性**：`_semaphore`（admission 许可）、`_executor`（线程池——**唯一跨线程边界**：worker 内运行 `_pack_json`/`strip_diagnostics_and_pack`/投影，输入输出经 `run_in_executor` 拷贝语义传递，无共享可变状态）、`_active/_waiting` 纯 int 计数（仅事件循环线程读写，`_active` 在 `__aexit__`/`release` 由循环线程改）。
- **错误路径**：admission 超时 → `TransformBusy` → 各路由 503 `transform_busy`（+Retry-After）；worker 内 `orjson.JSONDecodeError`/`ValueError` 等 → 调用方 catch 映射 503 `upstream_unavailable`（如 `routes/messages.py:1073-1074,1246-1248`）；cap 超限 → `read_with_cap` 返回 `(None, total)` → 路由 413 `response_too_large`/`message_too_large`；admission 后上游异常 → `async with pool` 退出时释放 slot（26）。
- **疑问点（8）**：
  1. **[中] `wait_for(semaphore.acquire())` 取消竞态可泄漏许可**（238-241）：任务在 `semaphore.acquire()` 内部完成的同一瞬间被取消时，`asyncio.wait_for` 在 3.11 仍存在"结果丢失"边缘（bpo-42130 语义 3.12 才收口）——许可已扣但 `TimeoutError`/`CancelledError` 抛出，`_active` 不加、`release` 永不发生 → 池容量永久 -1。触发面 = 请求恰在 admission 排队获得许可的瞬间断连。3.11 基线下值得复核（3.12+ 无此问题；Python 3.11.x 的 wait_for 修复情况需按小版本确认）。
  2. **[低] `acquire` 成功与 `_active += 1` 之间被取消**：`wait_for` 返回后到 246 行之间无 await，事件循环不会插入取消——安全；但若未来有人在中间插 await 则 `_active` 计数漂移 + 许可泄漏。当前代码安全，属脆弱性备注。
  3. **[信息] admission 先于上游 GET 的顺序保证**：这是**调用方纪律**而非本模块强制——模块只在 docstring（15-17,197-203）约定 `async with pool:` 内发 GET。逐路由抽查均遵守（`routes/messages.py:1005-1012`（直取路径注释明示）、`_catalog_common.py:432`、`_read_passthrough.py` admission 上下文）；例外是**join-first lease 路径**（`routes/messages.py:830-862`：先 `fetch_or_bypass` 拿共享 body，**后**进自己的 admission）——这是 1.5.0 设计变更（join-first），内存上界由 leased 注册表的 `reserve_bytes` 预算（`raw_fetch_max_bytes`）接管，但两条路径的缓冲上界模型不同（admission-first：max_transforms×body；join-first：raw_fetch 预算 + joiner 各自的 admission×body）。审计 E2 阶段建议对 lease 路径的并发内存峰值单独建模。
  4. **[信息] absorb 预算/池满公平性**（`routes/messages.py:1195-1213` 消费本模块）：while 循环每次 `pool.acquire(min(transform_wait_seconds, remaining))`，`TransformBusy` 则 continue 收窄重试——总等待 ≤ `transform_absorb_budget_seconds`，且"503 transform_busy 永不伴随已发出的上游请求"不变式保持（1210-1212 注释）。但 `asyncio.Semaphore` 唤醒为 FIFO，`max_transforms=1` 默认下 absorb 重试者与新鲜请求同队竞争，无优先级——大预算 absorb 者可能反复排队失败直至预算耗尽（正确但可能"等了很久仍 503"）。
  5. **[信息] `max_transforms=1` 默认吞吐**：默认配置（`config.py:363`）下全 sidecar 串行变换——每请求至少 1 次 offload（列表=parse+project+pack；/full=strip+pack），worker 内 gzip level 6 大 body 可达数百 ms，1 并发 + `transform_wait_seconds=2s` 等待上限意味着并发客户端极易 503 `transform_busy`。这是刻意的内存保守（docstring 41-44），且 config.validate 以 `_MAX_TRANSFORM_TOTAL_BYTES`（512 MiB）约束上调幅度；审计应关注默认吞吐是否满足 ocdroid 单客户端 + 偶发并发场景（当前看是够的，SSE 心跳不受影响）。
  6. **[低] `__aexit__` 不复用 `release()`**（257-259）：三行重复，若未来 `release` 增加逻辑（如 finalizer/审计）会分叉。
  7. **[信息] `read_with_cap` 的 `on_read` 回调在事件循环线程同步执行**（187-188）：回调若做重活会阻塞循环——现有调用方均为 `stash_up_in`（计数器累加），安全。
  8. **[信息] `shutdown(cancel_futures=True)` 对 awaiting offload 的路由**：pending future 被取消 → `run_in_executor` 的 awaiter 收 `CancelledError`——与请求取消同形，路由无专门处理；仅发生在 lifespan teardown（此时请求应已排空），可接受。

---

### src/oc_slimapi/catalog_cache.py（181 行）

- **职责**：catalog 路由（`/slimapi/agent`、`/slimapi/command`）的 TTL body 缓存——仅缓存成功上游 body（200 + 可解析 JSON list + 双预算内），TTL 窗口内重复 GET 不打上游；gzip/身份编码不入缓存（缓存 raw body，压缩按请求协商，1-7）。
- **对外符号（逐类逐方法）**：
  - `FactoryT`（34）：`Callable[[], Awaitable[bytes | None]]`（None = cap 超限）。
  - `CatalogCache`（37-181）：
    - `__init__(*, ttl_seconds, max_entries, max_bytes, max_entry_bytes, clock=time.monotonic, refresh_singleflight=None)`（52-71）：三预算（条数/总字节/单条字节）+ 可注入 clock 与外部 SingleFlight；默认自建 plain `SingleFlight()` 做刷新防踩踏。
    - `retained_bytes` property（77-79）/ `entry_count` property（81-83）：ops/测试观测。
    - `lookup(key)`（89-104）：同步新鲜度检查——TTL≤0 直接 None（95-96）；过期条目在此惰性删除（串行点无 await，101-103）；命中返回**同一 bytes 对象**（免拷贝，消费方只读）。
    - `refresh(key, factory)`（106-135）：**须在 transform admission 内调用**（docstring 44-45；调用方 `_catalog_common.py:432` 遵守）。TTL≤0 → `await factory(), None`（禁用=逐请求直取，无 cache 标签，117）。`_fetch_and_store`（119-133）：factory None（cap）→ 不缓存（122）；超 `max_entry_bytes` → 旁路不入账（124）；坏 JSON / 非 list → 不缓存（126-130）；合格则 `_store`（store+evict 同串行点，131）。经内部 `_sf.fetch(("catalog-refresh", key), ...)` 去重（134），straggler 在 1s grace 内加入刚完成的刷新；返回 `(body, "miss")`。
    - `_store(key, body)`（141-147）：先 `_drop` 旧条目再追加新条目（replace-in-place 不双计，143-145）→ `_evict_over_budget`。
    - `_evict_over_budget()`（149-162）：双上限强制，oldest（dict 插入序=fetch 序）先逐；新插入条目最新故不会被逐（validate 保证单条 fits 双预算，152-153）。
    - `_drop(key)`（164-167）：删除 + 归还字节。
    - `shutdown()`（173-181）：`_sf.shutdown()` + 清空 entries + ledger 清零；CD-1 语义——缓存此后仍可用（下次访问重新填充）。由 app lifespan teardown 调（`app.py:368-375`）。
- **依赖 / 被依赖**：依赖 `time`、`orjson`、`.singleflight.SingleFlight`。被依赖：`app.py:20,361-366`（构造，旋钮来自 `config.py:380-391`：TTL 默认 300s / 16 条 / 16 MiB / 1 MiB 单条）；`routes/_catalog_common.py:241,263,369`（`_handle_catalog_cached` 唯一消费链——hit 路径免上游 GET 只做 admission+offload（405-412）；miss 路径 admission-first + `cache.refresh` + offload（432-445））；`routes/agent.py:49`、`routes/command.py:46`（注入 cache）。测试：`tests/test_catalog_cache.py`、`tests/test_etag.py:32,833+`、`tests/test_vary_directory_unconditional.py`。
- **状态 / 可变性**：`_entries`（`dict[key, (body, fetched_at)]`，插入序=逐出序）、`_retained_bytes`、`_sf`（内部 plain SingleFlight）。全部仅事件循环线程触达；所有变更（lookup 惰性过期、_store、_evict、shutdown）均为无 await 串行点 → **三预算原子性成立**（store+evict 之间无 await，149-162；插入前先 drop 旧条目，144）。
- **错误路径**：factory 异常 → 经 SingleFlight 分支① 信封 → 所有 joiner 同实例 re-raise → 路由按 discovery 异常映射（4xx/5xx/网络 → 502/503），**绝不负缓存**（模块 docstring 11-13）；cap 超限（None）与坏 JSON/非 list 均不入缓存（122-130）；TTL=0 全禁用（字节等价旧行为，`_catalog_common.py:435-438` 的 None 标签省略 access log `cache` 字段）。
- **疑问点（5）**：
  1. **[信息] 并发 miss 风暴 per-key 去重：有**——同 key 并发 refresh 经内部 `_sf.fetch` 合并（134，leader fetch + 1s grace straggler 加入）；且 miss 路径整体在 transform admission 内（`_catalog_common.py:432`），`max_transforms=1` 默认下同 key 并发请求被 admission 串行化后，后到者 `lookup` 已命中 leader 存入的条目 → 实际上双重防踩踏（admission 串行 + singleflight）。**不同 key** 并发 miss 各自刷新（无跨 key 合并，符合语义）。
  2. **[低] `refresh` 恒返回 `"miss"` 标签**（135）：grace 内加入 leader flight 的 straggler 也记 `miss`——access log 的 `cache: hit|miss` 语义中"合流命中"被计为 miss，省流审计上轻微低估去重收益（CHANGELOG.md:285 只承诺 hit/miss 二值）。
  3. **[信息] cap 超限结果（None）经 grace 共享**：leader `None` 在 1s grace 内被 straggler 直接复用（同为 413 `response_too_large`，同 body 同判定）——一致；但 None 不是 FetchFailed，属"正常结果"负语义，无负缓存问题（None 不 `_store`）。
  4. **[低] `_evict_over_budget` 无 while 内进展保证的注释依赖外部 validate**：若绕过 `Settings.validate` 直接构造（测试可以）且 `max_entry_bytes > max_bytes`，新条目插入后循环会连续逐出包括最新条目在内的一切直至空 dict（`for...else break` 兜底终止，154-162）——不死循环，但"新条目不被逐"的注释假设（152-153）仅在 validate 成立时为真。
  5. **[信息] shutdown 时序**：`shutdown()` 先 `_sf.shutdown()` 再清 entries（179-181）——若此刻仍有 in-flight refresh leader，其 `_fetch_and_store` 迟到完成时 `_store` 会把条目写回**已 shutdown 的缓存**（单飞 late-completion 只对注册表身份兜底，`_store` 无 shutdown 标志检查）→ shutdown 后缓存非空。CD-1"保持可用"语义下无害（条目 TTL 正常过期），但"shutdown 清空"不变式可被迟到写入打破；调用方 `_catalog_common.py` 的 offload 仍会正确消费该 body。与 app lifespan LIFO（upstream 后关）共同决定 leader 大概率在 shutdown 前完成，窗口极小。

---

## 汇总

| 文件 | 行数 | 疑问点数 | 高优先级 |
|---|---|---|---|
| src/oc_slimapi/singleflight.py | 770 | 16 | plain `_fail` 无身份校验（Q1）、混合时钟（Q2） |
| src/oc_slimapi/transform.py | 326 | 8 | 3.11 `wait_for`+`Semaphore.acquire` 取消竞态许可泄漏（Q1） |
| src/oc_slimapi/catalog_cache.py | 181 | 5 | 无中危；shutdown 迟到写回（Q5）为信息级 |
