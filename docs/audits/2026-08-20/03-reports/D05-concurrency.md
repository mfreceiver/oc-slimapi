# D05 — A5 并发与 singleflight/缓存审计报告

| 项 | 值 |
|---|---|
| 专项 | A5（并发 / singleflight / 缓存 / 取消 / 关停 / 事件循环阻塞） |
| 快照 | 0b836e7（HEAD = release v4.4.0） |
| 日期 | 2026-08-20 |
| 输入 | 全文自读 `src/oc_slimapi/{singleflight.py, transform.py, catalog_cache.py, app.py, sse/registry.py, upstream.py}` + 调用面抽读（routes/messages.py、routes/_catalog_common.py、routes/_read_passthrough.py、routes/{sessions,questions,permissions,write_groups,directories}.py、sse/{global_hub,tokenstream/hub,tokenstream/subscriber,replay_log}.py、etag.py、gzip_util.py、access_log.py、traffic_snapshot.py、config.py、qp_sweep.py、actions.py、upstream.py）；01-explore/parts/e1-{06,07,10}.md、state-machines.md 卡 11-13、dataflows.md 场景 2-3；tests/test_leased_singleflight.py 全文（回归验证） |
| 方法 | 逐分支机制核对 + 调用面顺序核查 + `rg` 全清单扫描（Lock/Semaphore/Event/shield、CancelledError、同步 IO/CPU）逐点人工判定 + Python 3.14.4 语义实证（wait_for+Semaphore 竞态，30,000 次独立复现脚本，不触仓库） |
| 纪律 | 仓库零写入（本报告与 02-findings 指定文件除外）；未运行 pytest/pip/git |

---

## 0. 结论摘要

- **singleflight.py（770 行）机制整体判定：成立**。三分支取消、FetchFailed RESULT 信封、shield-join、grace、leased exactly-once 释放、raw bytes 引用切断、shutdown 收敛七项机制与 docstring 声明一致，且 `tests/test_leased_singleflight.py` 23 个用例（含 1.5.0 修复的回归锁）覆盖完整——**未发现双释放/泄漏路径**。两缺口复核定级均降为 P3：plain `_fail` 无身份校验仅 shutdown 交错可达（F-210）；grace timer 时钟混用为测试一致性缺口、生产正确（F-211）。
- **transform.py：admission-first 是调用方纪律而非模块强制**，全仓核查：4 类保证方遵守、3 类例外各有独立预算与文档理由（join-first lease / merged phase B / questions·permissions 打包）。**F-016 在 Python 3.14 下证伪**（实证 + CPython 源码补偿逻辑），残留面仅 `requires-python>=3.11` 的 3.11 部署 → 降 P3。
- **catalog_cache.py：三预算原子性成立**（全部变更点无 await）；miss 风暴 per-key 双重防踩踏成立；两个 P3 缝（>1s 排队 straggler 重复刷新 F-208；shutdown 迟到写回 F-209）。
- **锁持有图：全仓 asyncio.Lock = 0 把**；7 个信号量持有面最大深度 2 层、**无环 → 锁序死锁结构性不可能**；唯一跨资源"持有等待"是 lease 跨 transform admission（F-213，预算占用面而非死锁）。
- **关停收敛：LIFO 14 回调序列正确，但存在两处可中止链的缺口**——`_stop_qp_sweep` 无 try/except 隔离（F-007 确认 P2）与 `_remove_hub_after_grace` 无 `except Exception`（F-011 确认 P2）；外加最坏排空预算 45s+ 超 systemd TimeoutStopSec=15s 的 SIGKILL 截断面（F-214 P3）。
- **取消语义：13 处 CancelledError 命中逐点判定全部正确**（re-raise / 防御性消费均有理由）；**未发现取消误映射为 503 信封外泄给 follower 的路径**（branch ② 重新引导 + CancelledError 为 BaseException 不入窄 except）。
- **事件循环阻塞扫描：正面确认 9 处重活已正确 offload；负面 12 处在 loop 上**，其中 7 处立新发现（F-201..F-207），最重要的一个是 **messages 列表/merged 200 尾部的 gzip level-6 压缩在事件循环上执行**（merged 页 ≤8MiB → 最坏数百 ms loop 停摆，直接违背 transform pool 的 offload 设计目标，F-201 P2）。

机制判定表合计 **57 行**（§1:10 + §2:6 + §3:3 + §4:4 + §5:13 + §6:21）。
新发现 **F-201 ~ F-215**（15 条）；更新 F-007 / F-011 / F-016。

---

## 1. singleflight.py 逐分支机制判定（表 10 行）

| # | 机制 | 代码证据 | 判定 |
|---|---|---|---|
| S1 | **lead 失败 → follower 错误信封**：leader 常规异常经 `_fail(entry, exc)` → `_fail_future` 用 `set_result(FetchFailed(exc))`（非 `set_exception`）——零 waiter 失败不触发 "Future exception was never retrieved"；waiter 在 `_join` unwrap 后 `raise result.exc`，**所有 caller 同一异常实例**；entry 丢弃，绝不负缓存 | singleflight.py:472-476、556-566、451-454；测试 tests/test_leased_singleflight.py:113-143（同实例断言 `all(exc is boom ...)`，:139） | **成立** |
| S2 | **三分支取消**：① factory 常规异常（同 S1）；② leader 取消 → `_fail(entry)` exc=None → `future.cancel()`（未包裹）→ 存活 waiter 旧 ref 先释放 → `_REJOIN` → 重入串行点 re-join/re-reserve/re-lead（**立即 refund 先于 waiter 唤醒**是单预算下 re-lead 可预约的关键，:539-551）；③ waiter 自身取消（含 registered-ref→await 窗口）→ own ref exactly-once 释放 + 原样上抛，共享 future 与他人不受影响 | :467-471（②lead 侧）、439-450（②③判别）、441-446（③）；`_current_task_cancelling` :182-186（3.11+ `task.cancelling()>0` 归因）；测试 :168-207（③）、:213-253（②，单预算 re-lead 只能靠 dual-refund 成功，:241-243 注释自证）、:460-505（shutdown 后 ② residual refs 纯计数） | **成立** |
| S3 | **shield-join**：`await asyncio.shield(entry.future)`（:438）——waiter 取消不传导进共享 future；future 被 cancel 时 shield 向 waiter 抛 CancelledError，`cancelling()==0` 判为 ②。边界：自身取消与 flight 死亡同时到达 → `cancelling()>0` 优先走 ③（保守归因为自身取消，该 caller 不再 re-lead）；此 tie-break 未在 docstring 明示（信息级，不立 F） | :438、441-450 | **成立**（tie-break 记录在案） |
| S4 | **grace 窗口**：plain 完成结果 joinable 保留 `_DEFAULT_RESULT_GRACE_SECONDS=1.0`（:97），主动 `call_later` 到期 + 惰性 `_expire_if_due` 双入口（:517-520、568-575）；保留界 64 条/32MiB 双上限，只逐出 oldest COMPLETED、永不逐在飞（:662-677，两个串行点 :387/:509 强制）。leased：ACTIVE+IN_FLIGHT → GRACE（:522-529）→ 到期 RETAINED → last-release reap（:603-611、613-625） | :97、503-520、522-529；测试 :260-283（到期退款）、:288-299（straggler 窗口内 join） | **成立** |
| S5 | **leased exactly-once 释放（1.5.0 修复回归验证）**：`Lease._release` 幂等守卫（:229-231）+ **先切断引用再递减**（:237-240）；`_fail` 先释 leader ref（:546）；waiter 在 `_join` ②③分支各自释放（:440/:453）；`accounted` dual-refund 守卫（:635-638）；Release 绑定 entry 对象而非按 key 查找（跨代定向正确） | 回归测试：:39-63（正常路径+手动双释放幂等）、:785-802（切断不破坏释放账目）、:507-591（双 waiter 代际 interleave——release 定向旧 entry、新代不被误减）、:664-720（30 worker 混合生命周期精确 ledger 方程）、:430-457（shutdown 后 detached leader cancel 恰一次 refund） | **回归完整，未发现双释放/泄漏路径** |
| S6 | **共享 raw bytes 引用切断（1.5.0 修复回归验证）**：released Lease 的 `body`/`_entry` 置 None（:237-238）——已释放句柄跨后续 await 不再持有共享体（防 zombie generation）。全 src 消费点核查：messages.py:837-838（`async with lease:` 内解包）、questions.py:183-198（parse-inside-lease + `del sessions_payload, raw_body` + :200 `del lease`）、sessions/permissions lease 路径同型（sessions.py:726-732、permissions.py:330-338 docstring 声明 parse-inside-lease） | :200-240（docstring 语义）；测试 :731-782（weakref 探针：grace reap 后 body 不可达即使 Lease 对象仍活） | **回归完整** |
| S7 | **plain `_fail` 按 key 删除缺身份校验**（:542-544 `self._drop(entry.key)`，与 `_convert_success` :503/:510、`_expire_grace_entry` :582 的身份校验纪律不一致）：若 leader factory 运行期间同 key 注册 entry 被替换，旧 leader 失败会误删**新 entry**。可达性复核：plain 注册表内唯一能替换 in-flight entry 的路径是 `shutdown()` 清空后新 caller 重建（`_evict_over_budget` 永不逐在飞 :673-675；无其他 detach 点）→ **仅 app teardown 交错可达**；后果 = 去重瞬时丢失 + 在飞条目被绕过逐出（无 ledger 损坏：in-flight `size=0`、timer=None）。e1-07 Q1 初判 [中] → 复核降 **P3**（F-210） | :542-545 vs :503/:510/:582；shutdown 路径 :729-747 | **确认存在，降 P3** |
| S8 | **grace timer 混用注入 clock 与 loop time（两缺口复核定级）**：① plain timer 延迟 = `expires_at - self._clock()`（注入 clock 差值）喂 `call_later`（loop 时钟），而 `_expire_grace_entry` plain 分支用注入 clock 复检 `expires_at <= clock()`（:587）——注入 clock 慢于 loop 时钟时 timer 触发但复检不过 → entry 滞留（无后续 timer，靠同 key 惰性/churn 逐出）；② leased timer 直接 `call_later(self._grace)` 完全绕开注入 clock（:527-528），与 plain 口径不一致。生产（`time.monotonic` 注入）差值口径下 epoch 抵消、速率一致 → **生产正确**；纯测试注入面缺口。e1-07 Q2 初判 [低] → 复核维持 **P3**（F-211） | :517-520 vs :527-528 vs :587 | **确认存在，P3（test-only）** |
| S9 | **FetchFailed 包裹 CancelledError 的理论面**：`_lead` 的 `except BaseException`（:472-476）会把"非本任务取消的 CancelledError"（factory 内部子任务取消泄漏等）包成信封 → follower re-raise 一个非自身取消的 CancelledError（路由面表现同断连）。可达性：当前全部 factory（`_dedicated_full_get`→httpx send、`_fetch_list_raw`、discovery/status factory）只在任务取消时见 CancelledError → **不可达**；记录为 latent 契约注记，不立 F | :472-476 | **latent，不可达** |
| S10 | **shutdown 收敛**：plain 逐 entry 隔离清理（timer.cancel 失败被隔离，force-remove + 从 entry 自身字段退款，:730-747）+ retained ledger 清零；leased 原子转换（in-flight → retired/detached 仍计数、future 不动；grace → retained、caller-less 立即 reap，:749-761）；**在飞 future 永不 cancel**，迟到完成路径二次身份复检防重注册/重入账/重挂 timer（:503/:510/:530-534）；注册表保持可用（CD-1） | :709-761；测试 :351-384（detached→retained）、:387-403（grace→retained+timer 取消）、:406-423（detached 失败立即退款）、:649-661（shutdown 后可用） | **成立** |

补充（不改判定）：`fetch()` plain 循环 `_REJOIN` 后 `while True` 重入的理论活锁（每次 re-join 都遇被取消 flight）受 httpx 超时与调用方取消约束，无现实路径（e1-07 Q15 维持信息级）；`full_fetch_key` 的 `id(scope)` GC 复用撞键窗口 ≤1s grace + shutdown 清空，测试面 only（e1-07 Q4，维持信息级，不占 F 号）。

---

## 2. transform.py 判定（表 6 行）

| # | 机制 | 证据 | 判定 |
|---|---|---|---|
| T1 | **admission 先于上游 GET：调用方纪律而非模块强制**。保证方（核查通过）：messages 直连列表（messages.py:1009 `async with pool:` 先于 :1010 `_stream_upstream`）、/full（:1205-1214 absorb 后 :1224 GET）、expand（:1573-1582）、catalog（_catalog_common.py:284、:422）、read_passthrough pooled（_read_passthrough.py:195）、sessions v3 直连（sessions.py:733 `async with ... transforms`）、directories（directories.py:96-100 admission + offload） | 上列 file:line | **纪律面成立** |
| T2 | **三类例外（各有独立预算/理由，非违例）**：① **join-first lease 路径**（messages.py:830-834 先 `fetch_or_bypass`，:837 `async with lease:` 包住 :862 `async with pool:`——内存上界移交 `raw_fetch_max_bytes`（reserve=64MiB=全预算）；sessions `_sessions_via_lease`/`_status_via_lease` 同型（sessions.py:726-732、837-842））；② **merged phase B 无 slot**（messages.py:649-676，oracle §C-2：请求级 `remaining` 预算串行点 reserve/refund + 三层边界披露 :620-637——windfall 瞬态可持 32MiB 共享体，仅响应内联后验 ≤8MiB）；③ **questions/permissions 打包 offload 无 admission**（questions.py:261-266 注释：聚合内存受 `_MAX_AGGREGATE_ITEMS`+per-dir cap 界，offload 纯为 CPU；fan-out 并发由各自 Semaphore 管） | 上列 file:line | **例外均有文档化预算** |
| T3 | **absorb 预算与 503 形状**：`deadline = now + transform_absorb_budget_seconds(2.5s 默认)`；每轮 `pool.acquire(min(transform_wait_seconds, remaining))`（transform.py:220-246 收窄语义），TransformBusy → continue 收窄重试；预算尽 → `raise TransformBusy()` → `_busy_response` 503 + Retry-After:2。**不变式「503 transform_busy 前绝无上游请求」成立**（GET 仅在 acquire 成功后发出，messages.py:1205-1214 / 1573-1582） | transform.py:220-246；messages.py:1205-1214、1573-1582；dataflows 场景 3 步骤 3 | **成立** |
| T4 | **池满公平性**：`asyncio.Semaphore` 唤醒 FIFO；但 absorb 重试者在 TransformBusy 后**重新排到等待队列尾**（wait_for 超时已将其从 waiter 队列摘除）——持续到达流下重试者可被反复超越直至预算尽（正确但"等了很久仍 503"，absorb 收益在负载下退化） | transform.py:238-241（超时摘除）；CPython 3.14 `asyncio.locks.Semaphore`（waiter deque FIFO）；messages.py:1206-1214 | **确认退化面，P3（F-212）** |
| T5 | **`max_transforms=1` 默认吞吐**：全 sidecar 变换串行——列表（parse+project+pack 1 次 offload）、merged（两段各 1 次 + fan-out 无 slot）、/full、expand、catalog hit/miss、directories、sessions 投影均需 slot；worker 内 gzip L6 大 body 可达数百 ms；默认等待上限 2s（absorb 路径 2.5s 总预算）→ 并发客户端极易 503。这是刻意内存保守（transform.py:31-44 RSS 模型 + config.py:109-110 `_MAX_TRANSFORM_TOTAL_BYTES`=512MiB 守卫）；SSE/心跳不受影响（SSE 路径零触本模块）。单 ocdroid 客户端 + 偶发并发场景够用 | config.py:363（默认 1）、364（2s）、640（2.5s）；transform.py:41-44 | **设计权衡成立，记录吞吐面** |
| T6 | **F-016 复核（Python 3.14 语义定性）→ 证伪，降 P3**。运行时 = 3.14.4（.venv）。① CPython 3.14 `Semaphore.acquire` 的取消补偿：`except CancelledError: if fut.done() and not fut.cancelled(): self._value += 1`（许可归还计数器而非丢失）+ 尾部 FIFO wake 循环；② 3.12+ `wait_for` 重写为 `asyncio.timeout` 薄包装，完成/取消边界不再丢结果（≤3.11 旧实现的 bpo-42130 族竞态）。**实证**：独立脚本 20,000 次 timeout-vs-release 竞态 + 10,000 次 parked-waiter 外部取消竞态，**零许可泄漏**。`_active += 1`（transform.py:246）与 wait_for 返回间无 await → 取消不可投递于其间，计数安全。残留面：`requires-python = ">=3.11"`（pyproject.toml:9）——3.11.x 部署仍带旧 wait_for → 保留 P3 版本注记 | 实证脚本输出（本报告方法栏）；CPython 3.14 asyncio.locks 源码；transform.py:238-246 | **3.12+ 无竞态；P3（3.11 残留）** |

卫生注记：`__aexit__`（transform.py:257-259）复制 release 逻辑不复用 `self.release()`（e1-07 Q6 维持）；`shutdown` 的有界 drain（291-326，cancel pending + 守护线程 bounded wait）本身不阻塞事件循环（e1-06 Q5 的疑虑排除——`done.wait(timeout)` 是唯一阻塞点且 bounded 10s，同步回调内可接受但有界面：见 §4/F-214）。

---

## 3. catalog_cache.py 判定（表 3 行）

| # | 机制 | 证据 | 判定 |
|---|---|---|---|
| C1 | **三预算原子性（无-await 串行点）**：`lookup` 惰性过期（catalog_cache.py:89-104）、`_store`（replace-in-place 先 drop 旧账再插入，:141-147）、`_evict_over_budget`（oldest-first 至双帽，:149-162）全部同步无 await；单事件循环单线程前提下原子成立。单条 fit 双帽由 `Settings.validate` 保证（绕过 validate 直接构造时"新条目不自逐"假设失效，`for...else` 兜底终止不死循环——e1-07 卡 Q4 维持 P3 信息级） | :89-104、141-162 | **成立** |
| C2 | **并发 miss 风暴 per-key 去重**：同 key 并发 refresh 经内部 plain SingleFlight 合并（key=`("catalog-refresh", key)`，:134；leader fetch + 1s grace straggler join）；叠加 miss 路径整体在 transform admission 内（_catalog_common.py:422）→ `max_transforms=1` 默认下同 key 并发被 admission 序列化，**双重防踩踏成立**；不同 key 各自刷新（符合语义）。**缝**：admission 排队 > 1s grace 的同 key straggler（lookup 只在 admission 前 :399；`refresh` 内部不复查 lookup）→ 发起**第二次上游 GET**（响应一致、缓存重存；纯去重效率损失，默认 transform_wait_seconds=2s > grace 1s 使该窗口现实可达） | :106-135；_catalog_common.py:399、422 | **防踩踏成立；重复刷新缝 P3（F-208）** |
| C3 | **shutdown 后迟到 leader 写回**：`shutdown()` 先 `_sf.shutdown()` 再清 entries 归零（:173-181）；in-flight refresh leader 的 `_fetch_and_store` 迟到完成时 `_store`（:131）无 shutdown 标志检查 → 条目写回**已清空的缓存**。SF 层面无害（`_convert_success` 身份复检 → 不再 grace 保留，singleflight.py:503）；cache 层面"shutdown 清空"不变式被破，CD-1"保持可用"语义下实际影响为零（TTL 正常过期、消费方正常）。lifespan LIFO 下 C6（catalog_cache）先于 C3（upstream.aclose）→ leader 大概率在 shutdown 前完成，窗口极小 | :131、173-181；app.py:368-375 vs :310-315 | **确认，P3（F-209）** |

---

## 4. 关停收敛与锁持有图（表 4 行）

### 4.1 LIFO 关停序列（正常 shutdown，app.py:235-731 AsyncExitStack）

```
C14 _stop_maintenance(719)      # 30s drain → force cancel（最先，防与 ledger/hub 清理竞态）
C13 _stop_dbaux(615)            # 5s drain
C12 _stop_token_hub(551)        # sync（NB-C4：早于 hubs.close）
C11 _stop_qp_sweep(517)         # 无 try/except 隔离！（F-007）
C10 _close_hubs(500)            # hub 4 task + _removal_task cancel + gather
C9  _stop_replay_sweep(470)     # set event + 立即 cancel（无 drain）
C8  _close_replay_log(441)
C7  _shutdown_raw_fetch_registry(397)  # 条件（coalesce_enabled）
C6  _shutdown_catalog_cache(375)
C5  _shutdown_fulls(355)        # 在 upstream 关闭前（在途 fetch 可能仍在等 GET）
C4  _shutdown_transforms(337)   # 10s bounded drain（daemon 线程）
C3  _aclose_upstream(315)       # 所有 fetch 层 registry 之后
C2  _stop_snapshotter(304)      # 最终快照
C1  _close_access_log_handlers(265)     # flush + 释放句柄，最后
```

跨组件顺序约束全部满足：token_hub 先于 hubs（app.py:548-551）；replay sweep 先于 replay_log.close（:442-446）；fetch 层 registry（fulls/catalog/raw_fetch）全部先于 upstream.aclose（:342-347、:359-360、:383 注释）；maintenance 最先（:687-688）。

### 4.2 判定表

| # | 机制 | 证据 | 判定 |
|---|---|---|---|
| K1 | **在途请求与 SSE 订阅者归宿**：uvicorn `timeout_graceful_shutdown=5.0`（app.py:97、780）→ SIGTERM 停接新连接 → 在途连接 ≤5s → 强断 → SSE StreamingResponse 生成器收取消/finally → unsubscribe（HubRegistry / TokenStreamRegistry）→ **lifespan（C14-C1）在连接排空之后运行**。控制面/双账本空 → token_hub.stop + grace arm；上游 `/global/event` 连接由 C10 收敛。残余面：events_tokens/flush loop 依赖 Starlette aclose 必达（generator 从未被迭代则泄漏——e1-10 Q10，外部前提）；token_registry 无 lifespan 回调（e1-06 Q3——依赖 5s 强断先于 C12/C10，排空语义下成立） | app.py:97/780、495-500、543-551；sse/registry.py:232-256、389-408；tokenstream/subscriber.py:789-836 | **归宿闭合（两残余注记）** |
| K2 | **死锁窗口排查——锁/信号量全清单持有图**（`rg "asyncio\.(Lock|Semaphore|Event|shield)" src/`）：**asyncio.Lock 全仓 0 把**（锁序死锁结构性不可能）。7 个持有面：① `TransformPool._semaphore`（transform.py:208）——持有期间 await：httpx 上游 GET、`pool.offload`（executor）、`fulls.fetch`（plain，内部无锁）；② `SingleFlight._network_sem`（singleflight.py:322）——仅 leader factory 内、持有期间 await：httpx GET（叶节点）；③ merged fanout per-request Semaphore（messages.py:650）——持有期间 await：`_fetch_full_shared`→fulls（无锁）；④ `questions_semaphore`（app.py:401）——持有期间 await：httpx GET；⑤ `permissions_semaphore`（app.py:407）——同④；⑥ actions `_semaphore`（actions.py:481）——持有期间 await：子进程 spawn/wait/killpg；⑦（间接）Lease grace ref——跨 ② admission 等待持有（messages.py:837→862）。**嵌套深度 ≤2、无环**（admission→{IO/offload}；lease→admission→offload；fanout→fulls）。threading 锁 2 把（access_log.py:44 `_setup_lock`、:56 `_MAINT_LOCK`）均在启动主线程或 to_thread 维护路径，非协程持有 | rg 全清单（报告头方法栏）；上列 file:line | **无死锁窗口；唯一"持等待"= lease 跨 admission（F-213 预算占用面）** |
| K3 | **F-007 确认（P2）**：`_stop_qp_sweep`（app.py:514-517）是 14 个关停回调中唯一无 try/except 隔离者，直接违反 app.py:221-223 声明。`AsyncExitStack.__aexit__` 语义：回调抛错 → 异常传播、**剩余回调全部跳过**（C10-C1：hubs.close、upstream.aclose、最终快照、access-log flush 全丢）。触发面：`qp_sweep._run`（qp_sweep.py:222-231）对 `run_once` 无异常兜底，`run_once`（:173-207）同步代码任何 bug → task 带异常死 → `stop()` 的 `await task` re-raise（qp_sweep.py:234-238 仅吞 CancelledError） | app.py:514-517 vs :221-223；qp_sweep.py:173-238 | **确认 P2 defect** |
| K4 | **F-011 确认（P2）+ 关停预算截断面（F-214 P3）**：F-011——`_remove_hub_after_grace`（sse/registry.py:258-325）仅处理 CancelledError（:285-286、:306-311），无 `except Exception`：`on_upstream_reconnect()`（:322）或 gather 后任何同步段抛错 → task 带 exception 死亡 → `_removal_task` 残留非 None → `maybe_arm_grace_if_idle` 的 `if self._removal_task is not None: return`（:183-184）**永久失效** → B-D1 修的 hub/连接泄漏回归 + "Task exception was never retrieved" 告警；`close()`（:400-402）进程关停时会 cancel 残留 task，**运行期长进程为主要影响面**。F-214——最坏 lifespan 排空 = 30s（maintenance）+ 5s（dbaux）+ 10s（transforms）+ 其余 > systemd TimeoutStopSec=15（app.py:96 注释自认）→ SIGKILL 截断尾部回调（C2 最终快照、C1 access-log flush 丢失）；各 drain 为上限非常量，典型关停快，仅卡死 worker 时触发 | sse/registry.py:183-184、258-325、389-408；app.py:70/79/85/97 | **F-011 确认 P2；F-214 新立 P3** |

---

## 5. 取消语义逐点判定（`rg -n "CancelledError" src/` 全部 30 命中 → 13 判定点）

| # | 位置 | 语义 | 判定 |
|---|---|---|---|
| X1 | singleflight.py:439-450 | 三分支判别（§1 S2/S3） | 正确 |
| X2 | singleflight.py:467-471 | leader 取消 → `_fail` + re-raise（branch ②） | 正确 |
| X3 | routes/permissions.py:480-482、routes/questions.py:458-460 | fan-out 消费循环 `except CancelledError: raise`（self-cancel 传播，finally 清理未启动 task）；docstring 明示 BaseException 不入 `except Exception`（permissions.py:340-342） | 正确 |
| X4 | actions.py:548-555 | admission 停车被断连 → 先 `_audit` 再 re-raise（Bug E rev-13 修复） | 正确 |
| X5 | actions.py:690-692 | 执行期取消 → outcome 标记后 re-raise，finally 统一 teardown | 正确 |
| X6 | actions.py:717、968 | 清理路径消费 CancelledError：(717) shield 恢复 spawn 句柄时吞（句柄已失，清理继续）；(968) `_cleanup` 内 `wait_for(proc.wait())` 吞——**二次取消落在最终 reap 上**的已接受权衡（Bug D 注释明示"Never raises"契约） | 正确（文档化权衡） |
| X7 | app.py:464-465、712-713 | lifespan 停后台任务：await 已 cancel 的 task 吞 CancelledError | 正确 |
| X8 | sse/registry.py:285-286、306-311 | grace task sleep/gather 期被 cancel → return（不置空——依赖 canceller 已处理） | 正确 |
| X9 | sse/global_hub.py:292、1073 | done-callback 防御消费 / run 重连循环 re-raise | 正确 |
| X10 | tokenstream/subscriber.py:706-707 | subscribe attach 段 `except CancelledError: raise` 不走 rollback（当前段无 await，理论面；e1-10 Q5 注记维持） | 正确（含注记） |
| X11 | tokenstream/hub.py:460、516 | done-callback 防御 / flush loop re-raise | 正确 |
| X12 | qp_sweep.py:236-238 | stop() 吞 task 的 CancelledError；**非 Cancelled 异常 re-raise** → 正是 F-007 触发链 | 正确（风险归 F-007） |
| X13 | traffic_snapshot.py:393-394、422-423 | stop/loop：cancel 后 await 吞 / loop 内 re-raise | 正确 |

**取消 → 503 信封外泄判定：无路径**。① follower 不见 leader 取消：branch ② `_REJOIN` re-lead（新 GET 替代已死 GET，正确——leader 的 httpx GET 随其连接取消而死）；② `FetchFailed` 只包常规异常（`_fail` 分支①），且当前 factory 不可达"非任务取消的 CancelledError"（§1 S9 latent 注记）；③ 路由层 except 全为窄类型（`CodedHTTPException`/`TransformBusy`/`(orjson.JSONDecodeError, ValueError, TypeError, AttributeError)`），CancelledError 是 BaseException 不被捕获；④ `TransformBusy` 仅由 `TimeoutError` 映射（transform.py:242-243）。

---

## 6. 事件循环阻塞扫描（`rg "\.result\(|time\.sleep|sha256|gzip\.|realpath|orjson\.dumps|\.write\(" src/` 逐点人工判定）

### 6.1 正面确认——重活已正确 offload（9 项）

| # | 项 | 证据 |
|---|---|---|
| P1 | transform `_pack_json` / `strip_diagnostics_and_pack`（/full strip+pack+gzip） | transform.py:79-140（worker 入口，经 pool.offload：messages.py:1235-1239） |
| P2 | messages 4 个 worker 入口（`_project_list_sorted_and_pack`/`_parse_sort_project`/`_merge_fulls_and_pack`/`_expand_fragment_worker`：parse+project+dumps；expand 的 gzip 也在 worker :1528） | messages.py:136-165、696-738、1487-1528 |
| P3 | directories `_aggregate_and_pack`（聚合+dumps+gzip） | directories.py:93-100、190-202 |
| P4 | questions `_pack_questions_envelope`（P1-28 offload，无 admission——内存另界） | questions.py:260-267、278-286 |
| P5 | permissions 打包 worker（dumps+gzip） | permissions.py:278-303 |
| P6 | catalog `make_project_and_pack`（parse+project+dumps+**ETag sha256 判定**+gzip 全在 worker） | _catalog_common.py:146-215 |
| P7 | providers_projection §12（"⑪ negotiation + compression both stay in the worker — the event loop never serializes or gzips"） | providers_projection.py:400-418 |
| P8 | access-log 维护 compress/prune → `asyncio.to_thread` | access_log.py:712-724 |
| P9 | read_passthrough pooled 投影 offload（`pool.offload(project, body)`） | _read_passthrough.py:233-239 |

### 6.2 负面/观察——在事件循环上（12 项）

| # | 项 | 位置 | 量级与定级 |
|---|---|---|---|
| N1 | **messages 列表/merged 200 尾部 gzip level-6 在 loop 上**（B1 设计把压缩移出 worker：`_project_list_sorted_and_pack` docstring 明示"compression moved OUT of the worker to the route"；lease 与直连两尾都压）——merged 页 identity ≤ `merged_max_bytes`=8MiB（config.py:637）→ gzip L6 ≈ 20-60MB/s → **最坏 ~130-400ms loop 停摆**；非 merged 骨架页受 inline 4KiB/16KiB 字段帽（config.py:579-583）约束为亚 MB（~ms）。同路 `judge_conditional`/`compute_etag` 的 sha256 也在 loop（8MiB ≈ 5-25ms） | messages.py:920、932、1114、1126；etag.py:101-113；gzip_util.py:98-107 | **P2（F-201）** |
| N2 | read_passthrough 尾部 sha256 判定 + gzip 在 loop（**raw 路由无 admission**，body cap=`max_response_bytes`=64MiB）——现实 body（providers/session 单个）小，但结构性无 offload、无上界保护 | _read_passthrough.py:245-259 | **P3（F-202）** |
| N3 | sessions v3/v4 响应尾：`orjson.dumps`（payload 双跑——identity + json_response 内部再 dumps）+ sha256 + json_response 的 gzip 在 loop；envelope 为白名单投影（小） | sessions.py:101-135、636-650；gzip_util.py:115-122 | **P3（F-203）** |
| N4 | write_groups POST 回显 gzip 在 loop（回显体小） | write_groups.py:235 | **P3（F-204）** |
| N5 | access-log `DailyAccessHandler.emit`：每请求同步 `fh.write + flush` 在 loop（含跨午夜 rollover 的 close/open）；行小、page-cache flush、设计自称 best-effort | access_log.py:172-199（write :195）；调用链 middleware/traffic_accounting.py:319 | **P3（F-205）** |
| N6 | `TrafficSnapshotter._write_once` 同步 open+write 在 loop（tick :421 + stop 终帧 :400）——间隔默认 300s；与维护循环的 to_thread 纪律不一致 | traffic_snapshot.py:397-428、505-535 | **P3（F-206）** |
| N7 | `candidate_canonical` 每判定 `os.path.realpath`（非缓存，rev-2 sub-1 安全设计）：global_hub 每帧目录入 + read_groups file 路由；syscall 级（~几十 µs/组件数） | config.py:294-318；global_hub.py:572-582；read_groups.py:140 | **P3（F-207，by-design 记录）** |
| N8 | SSE 帧序列化 `sse_frame`（orjson.dumps per frame）在 loop（hub flush + token flush 路径）——受 `max_frame_bytes` 界，by-design | hub_types.py:105-107；tokenstream/frames.py:33-43 | 观察（不立 F） |
| N9 | `ReplayLog._default_size_of`：每追加帧 orjson.dumps 计尺寸在 loop——帧级有界 | replay_log.py:115-127 | 观察 |
| N10 | `messages_envelope_bytes` / cursor dumps（envelope.py:28）——微小 | envelope.py:24-28 | 观察 |
| N11 | `IncarnationStore` fsync 写盘——lifespan 一次性（启动），bounded | turn_registry.py:175-190；app.py:582-586 | 观察 |
| N12 | actions realpath / `spawn_task.result()`：manifest 加载（启动一次性）；`.result()` 仅在 `spawn_task.done()` 后的清理分支（非阻塞） | actions.py:273、346、718-722 | 观察（正确） |

---

## 7. 发现索引

| 编号 | 摘要 | 定级 |
|---|---|---|
| F-007（更新） | `_stop_qp_sweep` 无 try/except 隔离——抛错跳过其后全部 LIFO 清理 | P2 确认 |
| F-011（更新） | `_remove_hub_after_grace` 无 except Exception → `_removal_task` 残留 → grace 拆除面永久失效 | P2 确认 |
| F-016（更新） | wait_for(semaphore.acquire) 取消竞态——Python 3.14 实证证伪；残留 3.11 部署面 | 降 P3 |
| F-201 | messages 列表/merged 200 尾 gzip L6 + ETag sha256 在事件循环（merged ≤8MiB → 数百 ms 停摆；违背 transform pool offload 目标） | P2 |
| F-202 | read_passthrough 尾 sha256+gzip 在 loop（raw 路由无 admission，cap 64MiB） | P3 |
| F-203 | sessions 尾双 dumps + sha256 + json_response gzip 在 loop（envelope 小） | P3 |
| F-204 | write_groups POST 回显 gzip 在 loop（小） | P3 |
| F-205 | access-log emit 每请求同步 write+flush 在 loop | P3 |
| F-206 | traffic snapshot `_write_once` 同步写盘在 loop（tick+终帧；与 to_thread 纪律不一致） | P3 |
| F-207 | `candidate_canonical` 每判定 realpath 在 loop（安全 by-design，记录） | P3 |
| F-208 | catalog straggler 排队 >1s grace 重复刷新上游（refresh 不复查 lookup） | P3 |
| F-209 | catalog 迟到 leader `_store` 写回已 shutdown 缓存（清空不变式破；CD-1 下无害） | P3 |
| F-210 | plain `_fail` 按 key 删除缺身份校验（仅 shutdown 交错可达；去重瞬失） | P3 |
| F-211 | grace timer 注入 clock 与 loop time 混用（plain/leased 口径不一；生产正确、测试面缺口） | P3 |
| F-212 | absorb 重试失去 FIFO 位（重排队尾）→ 持续负载下可被超越至预算尽 | P3 |
| F-213 | join-first lease 跨 admission 等待持有 raw_fetch 预算（默认单 flight 预算被独占至 offload 完） | P3 |
| F-214 | 关停最坏排空 45s+ > systemd TimeoutStopSec=15 → SIGKILL 截断尾部回调 | P3 |
| F-215 | plain `fulls` 在飞数可超 64 条数上限（fanout 16 + 并发 direct；仅 grace 保留损失，字节上界仍强制） | P3 |

## 8. 方法附注（F-016 实证）

独立脚本（/tmp，stdin 注入，零仓库写入）于 Python 3.14.4：
- 竞态 A（timeout-vs-release 同瞬）：20,000 次 → 许可值恒 1，**零泄漏**；
- 竞态 B（parked waiter 外部取消 + holder 释放）：10,000 次 → 值恒 1，**零泄漏**；
- 源码对照：CPython 3.14 `asyncio.locks.Semaphore.acquire` 含 `except CancelledError: if fut.done() and not fut.cancelled(): self._value += 1` 补偿 + 尾部 FIFO wake；`wait_for` 为 `asyncio.timeout` 薄包装（3.12+ 重写）。
（初版竞态 B 脚本一次误报系脚本自身缺陷——waiter 已成功获取后迟到 cancel，值=0 为正确持有态，非泄漏；已修正重跑。）
