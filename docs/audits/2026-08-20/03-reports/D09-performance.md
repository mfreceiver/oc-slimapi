# D09 — A9 性能与资源审计（性能 / 内存 / 连接 / 延迟 / 启停）

- 快照：BASELINE_HEAD = `0b836e7`（`0b836e78c5de62d0c73b8593bf62c6650043dedf`）；全部 `file:line` 相对该快照。
- 方法：静态推演 + 代码证据 + 契据定级（§3.3）；**未做任何压力实测**（只读纪律），故值域 `measured()` 仅在引用仓库自带探针脚本（`scripts/eqp_matrix.py`）的数据口径时出现。
- 输入：`src/`（config.py 预算值 + 各 hub/cache 实读）、`01-explore/parts/e1-*.md`、`state-machines.md`、`dataflows.md`、`config-census.md`、`docs/specs/v3-contract.md`、`docs/specs/v4-contract.md`、`CHANGELOG.md`（1.5.0 merged 披露 :319-320）。
- 关联发现：F-006（merged 预算组合退化，终判 P1 defect）、F-015（qp_last_activity 无界，终判 P2 risk）、F-010（关停链超时，A9 侧复核佐证）；新建 F-271…F-279。

---

## 1. 内存上界表（核心产物）

值域冻结：`bounded(<来源>) | unbounded | measured(<探针>) | unknown(<理由>)`。合计 **31 行**：bounded 26 / **unbounded 4**（#6 域外壳→F-272、#7 瞬态防抖窗（时间界但无条目/字节 cap，不单独立发现）、#12 qp_last_activity→F-015、#13 QpSweep 三表→F-273）/ **unknown 1**（#31 进程基线 RSS——只读审计禁实测，无法静态编造；`measured` 0——本审计禁压力实测，不自造数据）。

### 1.1 长生命周期结构

| # | 结构 | 位置 | 上界判定 | 说明 |
|---|---|---|---|---|
| 1 | catalog cache 条目（三预算） | `src/oc_slimapi/catalog_cache.py:69`；预算 `src/oc_slimapi/config.py:380-391` | bounded(16 entries / 16 MiB 总额 / 单条 1 MiB，TTL 300s；oldest-first 逐出于 serial point `catalog_cache.py:149-162`) | 仅成功 list body 入缓存（`catalog_cache.py:119-132`）；TTL=0 整体禁用 |
| 2 | catalog 刷新 singleflight 保留 body | `src/oc_slimapi/catalog_cache.py:71`（内部 plain `SingleFlight`） | bounded(64 entries / 32 MiB / grace 1.0s；`src/oc_slimapi/singleflight.py:97,103-104`) | 完成态才计数实际字节，in-flight 由 admission 界 |
| 3 | ReplayLog 帧数维（per-domain） | `src/oc_slimapi/sse/replay_log.py:93`；接线 `src/oc_slimapi/app.py:426-431` | bounded(2048 帧/域，环形覆盖；`replay_log.py:568-570`) | GLOBAL 域 + 每订阅出现过的 `t:<sid>` 域 |
| 4 | ReplayLog 字节维（进程总额） | `replay_log.py:94`；`config.py:670`（65536 KiB） | bounded(64 MiB，全局最旧帧逐出；`replay_log.py:572-592`) | 单帧超预算仍保留（never drops the frame it just accepted，:576-578）——需单帧 >64 MiB 才触发，不可达 |
| 5 | ReplayLog TTL 维 | `replay_log.py:95`；`config.py:671` | bounded(900s 帧龄；append/replay 惰性 + 60s sweep `app.py:91`) | fail-closed 校验拒 nan/inf（`config.py:1121-1129`） |
| 6 | ReplayLog per-sid 域外壳（shell） | `replay_log.py:240-257,495-511` | **unbounded**（epoch 内 sid 基数无 cap；`recycle_domain` 只清帧不清 shell，注释自认「per-epoch sid cardinality is small… real GC is process restart」） | 每外壳 ≈ 数百字节（`_DomainState` slots，`replay_log.py:214-225`）；→ **F-272（P3）** |
| 7 | GlobalHub `pending` digest 表 | `src/oc_slimapi/sse/global_hub.py:106` | **unbounded**（无条目/字节 cap；唯一约束 = 0.25s 防抖窗整体换出 `global_hub.py:452-478` + `hub_types.py:92`） | 瞬态：每 flush 全量 swap（`self.pending, self.pending = …`）；窗内条目数 = 上游 0.25s 内活跃 sid 数，条目为小 `DigestFields`（~200B）。单用户生产不可达危险量级；**不单独立发现**（时间界 + 窗口换出构成事实约束），记录在案 |
| 8 | GlobalHub `_last_updated_at_by_sid` | `global_hub.py:60,121` | bounded(10_000 LRU；`global_hub.py:629-642`) | |
| 9 | GlobalHub `sticky_last_error` | `global_hub.py:133` | bounded(10_000 FIFO；`global_hub.py:644-649`) | P1-21 加固 |
| 10 | GlobalHub `deleted_tombstones` | `global_hub.py:144` | bounded(10_000 FIFO；`global_hub.py:651-656`；resync_all 清空 :981) | |
| 11 | GlobalHub `_retired_messages` | `global_hub.py:166` | bounded(1000 FIFO + 24h TTL；`global_hub.py:601-627`，常量 `config.py:75-76`) | 与 token hub 同语义对齐 |
| 12 | GlobalHub `qp_last_activity` | `global_hub.py:109`；写点 `:678` | **unbounded**（键 = 上游 q/p IMMEDIATE 帧的 directory 字符串；全仓无删除路径；且记录发生在 allowlist 闸门**之前**——` :678` 先于 `:684` `_emit_directory_frame`） | → **F-015（终判 P2）**；放大器见 #13 |
| 13 | QpSweepShadow `_known_dirs`/`_seen_at`/`_next_run` | `src/oc_slimapi/qp_sweep.py:59-61`；ingest `:124-131`；evict `:155-164` | **unbounded**（对存在于 `_activity` 的目录，30 天逐出被结构性击穿：`run_once` 先 `_ingest_directory_source()`（刷新 `seen_at`）再 `_evict_stale_directories()`，:175-177 → 逐出条件永假；`_activity` 本身即 #12） | → **F-273（P3）**；markers deque 另为 bounded(256, `qp_sweep.py:73`) |
| 14 | TokenStreamHub `live_parts` | `src/oc_slimapi/sse/tokenstream/hub.py:254`；常量 `config.py:47-48` | bounded(32 parts / 4 MiB 全局 LIVE 字节；LRU 逐出 + `resync{token_memory_limit}`) | Stage E 4+4 拆分（`config.py:35-44`） |
| 15 | TokenStreamHub `_pending`（DeltaAccumulator 影子缓冲） | `hub.py:275`；常量 `config.py:49` | bounded(4 MiB 全局 PENDING；超限 force-flush → LRU 逐出) | 同一 delta 物理上双驻 LIVE+PENDING，两预算各自护各自缓冲（无双重计数） |
| 16 | TokenStreamHub `_disabled_parts`/`_nontext_parts`/`_part_revisions` | `hub.py:273-274,325`；常量 `config.py:59-61` | bounded(4096 条 / 300s TTL；on-insert prune) | `TOKEN_LIVE_PARTS_MAX ≤ TOKEN_DISABLED_MAX` 静态断言（`config.py:142-146`） |
| 17 | TokenStreamHub `_removed_messages` | `hub.py:335`；常量 `config.py:75-76` | bounded(1000 FIFO + 24h TTL) | |
| 18 | TokenStreamHub `_session_status`/`_busy_sids` | `hub.py:116,282-283` | bounded(10_000 FIFO `_SESSION_STATUS_MAX`) | |
| 19 | TokenStreamHub `_pending_session_resinks` | `hub.py:285`；常量 `config.py:67` | bounded(64；溢出丢最旧) | |
| 20 | TokenStreamHub `_subs_by_sid` | `hub.py:288` | bounded(token 订阅准入 8；`config.py:466-468`，独立账本) | |
| 21 | token 订阅者 runtime 队列 | `src/oc_slimapi/sse/tokenstream/subscriber.py:292`；`config.py:469-475` | bounded(8 × (64 items / 512 KiB 缓冲 / 1 MiB 帧上限)) | T3 背压：溢出断连（v4 STOP-only） |
| 22 | token 订阅者握手缓冲 | `subscriber.py:303`；`config.py:100-101` | bounded(8 × (2048 items / 8 MiB)；**溢出 fail-loud 503** 不丢帧（`subscriber.py:43-44,114-130`）) | 静态断言保 2048 ≥ 1000+1+32 全量 pre-fill（`config.py:147-160`） |
| 23 | 控制面 Subscriber 队列（`/events`） | `src/oc_slimapi/sse/hub_types.py:213-346`；`config.py:447-453` | bounded(16 × (256 items / 2 MiB / 256 KiB 帧)；溢出 = 立即断连 + resync) | per-directory 8 / total 16 双准入 |
| 24 | singleflight `fulls` 保留 body（grace 窗口） | `src/oc_slimapi/singleflight.py:770`（进程级 plain 实例）；界 `:97,103-104` | bounded(64 entries / 32 MiB / grace 1.0s；完成态按实际字节记账 + 活跃 `call_later` 到期（`:487-521`），两级 serial point 强制（`:662-677`）) | /full + expand + merged fan-out 三方共键 |
| 25 | `raw_fetch_registry`（LeasedSingleFlight） | `src/oc_slimapi/app.py:384-388`；`config.py:403-408` | bounded(64 MiB 预算 / 4 并发；每 flight 预留全 `max_response_bytes`=64 MiB → 默认仅容 **1** 个并发 flight（`config.py:899-906` 自认 deliberate）) | 预算满 → bypass 直连（非错误） |
| 26 | TransformPool 在制 body | `src/oc_slimapi/transform.py:195-212`；`config.py:363-365,878-892` | bounded(默认 1 × max(64 MiB response, 8 MiB expand)=64 MiB；P1-30 RSS 上界校验 512 MiB + raw 合计 576 MiB fail-closed（`config.py:106-118`) | admission 先于 GET（防 OOM 关键序） |
| 27 | merged 页瞬态缓冲（请求作用域） | `src/oc_slimapi/routes/messages.py:591-693` | bounded-with-disclosed-exception（merged 自领导读取 ≤ `merged_max_bytes + fanout×64KiB` ≈ 8.5 MiB；**顺风车例外**：join direct-led flight 可瞬持 ~32 MiB，CHANGELOG 1.5.0 :319 已披露；splice 后响应内联 ≤ 8 MiB） | 与 F-006 机制同源（见 §6） |
| 28 | TrafficLedger | `src/oc_slimapi/traffic.py:420-449` | bounded(固定 bucket 键空间（`bucketize` :91-203）× 每桶 `deque(maxlen=1024)` 延迟样本（`:40`）；expand 白名单 12+invalid 封键（`:42-68`）) | 线程锁 + 固定键空间 = 无路径注入增长 |
| 29 | traffic snapshotter 累积器 | `src/oc_slimapi/traffic_snapshot.py:289-` | bounded(无内存累积：每 tick 由 ledger 累计快照写一行即弃；聚合在分析期（docstring :9-33）) | |
| 30 | TurnRegistry `_turns` | `src/oc_slimapi/turn_registry.py:61,233-263` | bounded(10_000 LRU；逐出有 ops warning :254-262) | 逐出 → 同 incarnation 内 turn 回归 1（前瞻披露接受，:51-61） |
| 31 | Python 进程基线 RSS | —（无结构；聚合口径需要） | **unknown**(只读审计纪律禁实测/禁启动进程；无仓库内 RSS 探针数据可引。设计自述「Python/Baseline RSS 384M 内」（deploy/oc-slimapi.service MemoryMax 注释 + `transform.py:31-44`），但无 measured 值) | 唯一 unknown 行 |

### 1.2 聚合口径（静态推演，非承诺值）

各独立最大值**不同时**达峰，但理论上界合计值得记录（对照 `deploy/oc-slimapi.service` `MemoryMax=384M`）：

- SSE 满配（控制面 16×2 MiB = 32 MiB + token 满配 76 MiB（`config.py:457-458` 公式））≈ 108 MiB
- replay 64 MiB + token LIVE/PENDING 8 MiB + singleflight 保留 32 MiB + catalog 16 MiB + raw_fetch 64 MiB + transform 64 MiB ≈ 248 MiB
- 合计 ≈ 356 MiB + 基线 RSS（unknown）→ **理论上界已贴近/可能越出 384M**；但要求 replay 全满 + 双类订阅满配 + 三类缓存同时满 + 恰有 64 MiB 级响应在制——单用户部署不可达的组合。结论：MemoryMax 对**单一结构失控**有足够余量（最大单结构 76 MiB），对**全部同时达峰**无余量。不立发现（组合不可达性无法证明为生产风险），记录为运维口径。

---

## 2. 热路径成本

### 2.1 `GET /slimapi/messages/{sid}`（列表 / merged）CPU 主成本点

流水（直连路径 `src/oc_slimapi/routes/messages.py:1009-1141`；lease 路径 :808-945 同构尾部）：

1. **上游流式读**（事件循环，async，成本可忽略）：`read_with_cap` 64 KiB chunk（`transform.py:143-192`）。
2. **worker 线程**（占 admission）：`orjson.loads` → `_created_sort_key` 排序 O(n log n) → `skeleton_messages` 投影（含每消息 `contentFingerprint` sha256，`src/oc_slimapi/skeleton.py:42-62`）→ `orjson.dumps` → identity bytes。全部 C 实现/纯 CPU，隔离于事件循环——设计正确。
3. **路由尾部（事件循环上）——本审计主成本点**：
   - `judge_conditional`（`messages.py:1107-1112`；lease 路径 :893-943 内同构）对**完整 identity body** 做 sha256（`etag.py:236/238`）；带 `If-None-Match` 的 200 路径**再算一次** `compute_etag`（`:1134-1135`）→ 每请求 1-2 次全量 sha256；
   - `compress_if_beneficial`（`messages.py:1126-1128`）= **gzip level 6**（`gzip_util.py:103`）也在事件循环上。
   - 量级推演：sha256 ≈ 1.5-2 GB/s、gzip-6 ≈ 20-60 MB/s 单核。3.2.0 起 `TextPart.text` 永远全量内联（CHANGELOG :186）+ merged splice ≤8 MiB → identity 可达数 MiB 级；4 MiB body ≈ 2×2-3 ms hash + **80-200 ms on-loop gzip** → 期间全部 SSE 订阅者（心跳/`session.digest`/q/p 直推）停摆。对照：`/full` 与 catalog 的 gzip 在 worker 内（`transform.py:79-101`、`_catalog_common.py:328-347` offload）；sessions 列表同样 on-loop 但载荷小且代码自认（`sessions.py:100-131` 注释）。→ **F-271（P2）**。
4. **ETag canonical hash 输入规模**（`etag.py:101-113`）：`rep_version + \0 + coding + \0 + identity_body` —— 即**整个 envelope identity 字节**（`envelope.py` 拼接，`messages.py:1089`），非逐消息增量。上界：投影后列表（受上游读 cap `max_response_bytes`=64 MiB 的入侧约束，投影通常缩小）+ merged 内联 ≤8 MiB；典型几十-几百 KiB。

### 2.2 v4 sessions SQL 索引假设（dbaux）

- 投影 SQL：`ORDER BY s.time_updated DESC, s.id DESC` + `LIMIT ? + 1` + 可选 LIKE search（`src/oc_slimapi/dbaux/projection.py:238-239`；模板同 `scripts/eqp_matrix.py` SQL_TMPL）。sidecar 侧**零索引**（SQLite 写域硬规则禁止 DDL）——性能完全依赖上游真库自带索引形态。
- `scripts/eqp_matrix.py`（自述 :1-24）：48 组合（archived 3 × parent 4 × cursor 2 × search 2）EXPLAIN QUERY PLAN + 真库 P50/P99 采样。定位 = **手工实证脚本**：草稿库模式断言 EQP 结构特征（SCAN/SEARCH/TEMP B-TREE，不断言文案），真库模式「数据采集不断言」。
- 测试固化：`tests/test_eqp_matrix.py` + `tests/v4_fixture.py:44-55,211-226` 经 importlib 装载该脚本，在**合成临时库**上跑矩阵——固化的是 SQL 文本与合成 DDL 行为，**不固化真库索引可用性**；脚本未接 `scripts/check.sh`（check.sh 仅 pytest + 路由文档一致性）。
- 运行时兜底：LatencyBreaker（60s 窗 / ≥10 样本 / P99≥20ms trip / <10ms recover，`dbaux/lifecycle.py:150-165`）+ `busy_timeout=5000`（:302，对齐上游 database.ts）+ 503 `Retry-After: 30`。→ **F-278（P3 gap）**：真库索引假设每 release 无机械校验，漂移只靠运行时熔断兜底。

### 2.3 其他热路径观察

- `GlobalHub.flush` 每 0.25s 机会式 prune 三张表 + retired gate（TTL 剪枝 O(1000) 上限，`global_hub.py:452-465` 自述 negligible）——确认无问题。
- `ReplayLog._evict_for_bytes` 每次逐出需扫全部域头取最小 order（`replay_log.py:580-591`）：O(D) 每帧、O(K×D) 每次批量逐出（D=域基数、K=逐出帧数）。D 由 sid 基数决定（小），K 在字节压力下才非零——实际可忽略，不立发现。
- access log 每请求一次同步 `write+flush`（`access_log.py:173-196`）在调用线程（多为事件循环）——单 syscall 量级，单用户面可忽略。
- `_expand_fragment` 的 type-mismatch 400 发生在 admission + 上游 GET 之后（`messages.py:1509-1513` vs 求值序契约 §4b.3）——每次白打一次上游 GET + transform 槽；契约冻结如此，成本记录不立发现。

---

## 3. 连接与池

| 项 | 证据 | 结论 |
|---|---|---|
| httpx 客户端 limits | `src/oc_slimapi/upstream.py:44`：`max_connections=32, max_keepalive_connections=16` | **硬编码**，非 env 可调（Settings 无对应字段）；复核确认初判。`timeout=connect 5/read 30/write 300/pool 5`（:43） |
| 上游 SSE 长连接 × 共享池 | `global_hub.py:1002`：同一 client，per-request `httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)` | 全进程**唯一**一条 `/global/event` 长连接（token 流经 mirror 复用，不另开连接），永久占用 32 池中 1 条。`read=None` = 无读超时：上游半开（TCP 活着无数据）**永远检测不到**，恢复仅靠 EOF/异常（e1-05 疑问 8 独立确认）→ **F-275（P3）** |
| 无 admission 的上游 GET 并发面 | merged fan-out 8（`messages.py:650`，phase B 显式无池槽）+ questions 8（`config.py:596-598`）+ permissions 8（`:614-616`）+ 写透传 + raw_fetch 4 | 理论并发 ≈ 8+8+8+writes+1(SSE) ≈ 25-30，贴近 32 上限；admission 类路由被 `max_transforms=1` 串行化不贡献并发。突发下 `pool=5s` 超时 → `PoolTimeout`（httpx.RequestError 族）→ 503 `upstream_unavailable`。单用户现实负载远低；硬编码不可调是债务 → **F-274（P3）** |
| 单 worker 事件循环饱和 | `app.py:779` `workers=1`；CPU 工作经 TransformPool 单 worker 线程 | 饱和风险点清单：① messages 列表尾部 sha256+gzip（F-271，主项）；② sessions 列表尾部 `orjson.dumps`+`json_response` gzip（`sessions.py:122-131`，自认、载荷小）；③ 访问日志 write+flush（可忽略）。SSE 帧路径（publish/flush/put）全 O(1)——不受体量影响 |

---

## 4. 延迟预算表（全部人为延迟 / 超时 / TTL 常量，rg 全量清点）

计 **33 条**。契约依据缩写：C3=`docs/specs/v3-contract.md`、C4=`docs/specs/v4-contract.md`、CH=CHANGELOG、DT=design-token-stream.md。

### 4.1 SSE / hub

| 常量 | 值 | 位置 | 依据 | 评估 |
|---|---|---|---|---|
| DEBOUNCE_SECONDS | 0.25s | `hub_types.py:92` | C3 §7（digest 防抖；session.error 立即 flush 例外 :182） | 合理；flush prune 同频自述 negligible |
| HEARTBEAT_SECONDS | 10s | `hub_types.py:93` | C3 §7（heartbeat 帧形冻结） | 合理 |
| TOKEN_HEARTBEAT_SECONDS | 15s | `config.py:53` | DT §5.6（vs stunnel/proxy idle） | 合理（控制面 10 / token 15 双轨有意） |
| GRACE_SECONDS | 30s | `hub_types.py:94` | 生命周期设计（最后订阅者离开后拆 hub） | 合理；registry 同值（`registry.py:284`） |
| 上游重连退避 | 1→30s 指数 | `global_hub.py:994,1000,1071-1072,1089-1090` | — | 合理；client=None 防热循环 |
| 上游 SSE timeout | connect 5 / read None / write 30 / pool 5 | `global_hub.py:1002` | — | read=None 见 F-275 |
| token flush 窗 | 0.1s | `config.py:50` | DT §5.4 | 合理（100ms 批量） |
| token 早刷阈值 | 4096B | `config.py:51` | DT | 合理 |
| 孤儿 LivePart TTL | 60s | `config.py:52` | DT | 合理 |
| token tombstone TTL | 300s / 24h | `config.py:60,76` | DT §16-B/P.2 | 合理 |

### 4.2 请求路径

| 常量 | 值 | 位置 | 依据 | 评估 |
|---|---|---|---|---|
| 上游默认 timeout | connect 5 / read 30 / write 300 / pool 5 | `upstream.py:43` | — | write=300 为大 prompt 留量；合理 |
| singleflight 结果 grace | 1.0s | `singleflight.py:97` | CH L2-CD-1（:320「≤1s join 窗口」披露） | 合理（admission 排队 drain 覆盖） |
| transform_wait_seconds | 2.0s（默认） | `config.py:364` | C3 §11 transform_busy 语义 | 合理 |
| transform 吸收预算 | 2.5s（默认） | `config.py:640-642`；循环 `messages.py:1205-1214,1573-1582` | CH L2-CD-1（:320：最坏累计 ≤ 预算） | 合理（收窄重试防放大） |
| Retry-After（transform_busy） | 2 | `messages.py:44`、`_catalog_common.py:41` | C3 §11（`retry_after`+头双写） | 与单次 wait 对齐，合理 |
| Retry-After（SSE 准入满） | 5 | `events.py:137`、`token_stream.py:180` | C3 §6（T3） | 合理 |
| Retry-After（auxiliary_unavailable） | 30 | `sessions.py:274` | C4 §4.2（= 熔断恢复探针周期） | 与 dbaux 探针 30s 同源对齐，合理 |
| merged re-lead 重试上限 | 3 次 | `messages.py:552` | CH L2-CD-2 | 最坏 +1 次专用 GET，可接受 |
| /ready 上游探针 | 5s | `health.py:117` | — | 与 _SMOKE_TIMEOUT 对齐 |
| agent 读超时 | None（无限） | `agent.py:48` | — | 见 4.4 备注 |
| command 读超时 | 300s | `command.py:45` | — | 与写超时对齐 |

### 4.3 后台任务 / 缓存 TTL

| 常量 | 值 | 位置 | 依据 | 评估 |
|---|---|---|---|---|
| catalog TTL | 300s | `config.py:380-382` | CH 1.5.0 披露①（:282） | 已披露；可关 |
| replay TTL / sweep | 900s / 60s | `config.py:671`、`app.py:91` | C4 §7（CH 4.0.0 :146 默认三值） | 合理 |
| dbaux 探针周期 | 30s | `config.py:658-660` | design-v4-dbaux §2.3-6 冻结 | 合理 |
| dbaux busy_timeout | 5000ms | `dbaux/lifecycle.py:302` | 上游 database.ts:29 同值冻结 | 合理 |
| LatencyBreaker | 60s 窗 / 10 样本 / 20ms trip / 10ms recover | `dbaux/lifecycle.py:162-165` | design-v4-dbaux | hysteresis 合理 |
| access-log 维护周期 | 3600s | `config.py:538-540` | — | ≥60s 校验防热循环 |
| traffic snapshot 周期 | 300s | `config.py:549-551` | — | 合理 |
| qp_sweep 周期 / 预算 / 逐出 | 1800s / 100 每天 / 30d | `config.py:625-630`、`qp_sweep.py:20` | — | shadow-only（零真实上游请求）；逐出缺陷见 F-273 |

### 4.4 启停（对照 F-010）

| 常量 | 值 | 位置 | 评估 |
|---|---|---|---|
| _SMOKE_TIMEOUT | 5s | `app.py:61` | 启动冒烟 2 次 GET + /global/health 1 次 = **串行最坏 ~15s**（上游 hang 而非 refused 时；refused 即时失败）→ F-277 |
| 启动日志维护 | 无超时上限 | `app.py:269-280`（migrate/compress/prune 同步于事件循环） | 阻塞启动；RETAIN_DAYS=3 生产下通常亚秒，但无界 → F-276 |
| _MAINT_DRAIN_TIMEOUT | 30s | `app.py:70` | 见下 |
| _TRANSFORM_DRAIN_TIMEOUT | 10s | `app.py:79` | 见下 |
| _DBAUX_DRAIN_TIMEOUT | 5s | `app.py:85` | 见下 |
| _GRACEFUL_SHUTDOWN_TIMEOUT | 5s | `app.py:97,780` | uvicorn 连接排水 |
| **关停最坏总值** | **≈50s** | LIFO 链：maintenance(≤30) → dbaux(≤5) → … → transforms(≤10) + uvicorn 排水(5) | **> systemd TimeoutStopSec=15**（deploy/oc-slimapi.service）→ 15s 即 SIGKILL；且 `_stop_snapshotter`（最终快照）在 LIFO 中排后——maintenance 卡 30s 时快照/日志 flush **必然被杀**。**F-010 复核成立并加重**（F-010 初判「30+5+10s 级」≈45s；A9 计入 uvicorn 5s 连接排水后 ≈50s，且指出快照清理序在受害链末端） |
| TimeoutStopSec / MemoryMax | 15s / 384M | deploy/oc-slimapi.service | 单位注释自认 15s 为「5s 之上」设计，但未计入 30s 维护排水 |

---

## 5. 启动 / 关停时长

**启动串行链**（`app.py:189-727`）：logging → validate → access-log 装载 + 同步维护（F-276）→ ledger/snapshotter 构造 → httpx client → transforms/fulls/catalog/raw_fetch/ replay 构造 → hub registry → qp_sweep → token hub/registry → turn registry（同步 fsync 写 incarnation 文件 `turn_registry.py:151-200`）→ `dbaux.start()`（路径解析 + ro 打开 + schema 门，:603）→ `smoke(app)`（2×5s，:620）→ `/global/health`（5s，:627）→ banner → 后台任务。正常（上游在线）亚秒级；上游 hang 最坏 ~15s + 本机 IO。

**smoke test 内容**（`app.py:129-186`）：`GET /session?limit=1` 取 sid → `GET /session/{sid}/message?limit=1` 校验 list 形状（`info.id` str、parts[].type str）→ 状态机 `SMOKE_*`（not_run/unavailable/invalid_schema/valid）；非阻塞（失败仅记录）。

**关停**：见 §4.4——AsyncExitStack LIFO 全回调隔离，单组件最坏值如上；理论总值 ≈50s vs 15s 上限 → F-010（A13 主辖，A9 提供时序证据）。

---

## 6. F-006 复核（merged 预算组合退化）——终判 P1 defect

### 6.1 逐行机制推演（默认 32 MiB × 8 MiB）

`_merge_fulls`（`messages.py:591-693`）：

1. `:649-651`：候选 ≤16（`_merged_candidate_pairs` :454-481，placeholder 优先占槽）；`remaining=[8 MiB]`；`semaphore(merged_fanout=8)`。
2. `:674-676` `asyncio.gather` 按**创建序**调度 `_fetch_one` 协程。每个 `_fetch_one` 的同步前缀（semaphore acquire（有空位时不挂起）→ `cap = min(max_message_bytes, remaining[0])`（:655）→ reserve（:658））一路运行到**首个真实挂起点** = 领导者 factory 内 `await upstream.send(...)`（:501-503）。
3. 候选 1：reserve `min(32 MiB, 8 MiB)=8 MiB` → `remaining=0`，挂起于网络。
4. 候选 2-8（semaphore 有空位）：同步前缀立即执行，`cap=min(32 MiB, 0)=0` → `:656-657 return _DEGRADED`——**无重试**（函数已返回）。许可释放唤醒 9-16 → 同样 `_DEGRADED`。
5. 候选 1 完成（网络毫秒级）后 refund（:666-671）——**对已返回 `_DEGRADED` 的候选为时已晚**（事件循环确定性：全部候选的同步前缀先于任何网络回调执行）。
6. 净效果（默认配置、≥2 候选）：**每页恰好 inline 第 1 条，其余 ≤15 条静默保持 skeleton**；`merged_fanout=8` 的并发实效 = 1（单预留吃满预算）。副作用：fan-out 阶段实际只发 1 个上游 GET——省了流量但功能整体失效。

**「每页最多 inline 1 条且候选永不重试」的确切机制**即上述 4-5：`cap<=0 → 立即 return`（:656-657）是单向门，refund（:666-671）只惠及**尚未到达预约点**的任务，而 gather 的调度序保证在领导者首个网络 await 之前，同 semaphore 窗内的全部候选都已到达过预约点。

### 6.2 测试为何未覆盖

- 基线 `_settings`（`tests/test_messages_merged.py:126-142`）确实与生产默认一致（32 MiB/8/16/8 MiB）——但**唯一**用基线（无 override）的多候选用例不存在：`test_merged_inlines_full_for_placeholder`（:164-224）只有 **1 个**候选（1 候选无竞争，恰好成功）。
- `test_merged_degrades_beyond_page_cap`（:250-258）**显式 pin `max_message_bytes=256 KiB`**，注释自述目的：「reservations cannot exhaust merged_max_bytes before all 16 items start — isolating the PAGE-CAP criterion from the byte budget」——即测试作者知道组合效应，但用 pin 隔离掉了生产参数组合本身。
- 预算测试 `:495-540`（`max_message_bytes=8000 < merged_max_bytes=10000`）与 `:543-590`（refund 语义，fanout=1）全部运行在 `max_message_bytes < merged_max_bytes` 的反向组合上。
- 结论：**默认组合（≥ 且 2+ 候选）零覆盖**——不是断言错误，是参数空间回避。

### 6.3 消费方可观察后果

- `mode=merged` 响应仍 200、无任何 degraded 标记（降级项就是 skeleton 投影本身，与正常 skeleton 字节无异）；ocdroid 侧 merged 消费者拿到的页面与不请求 merged 几乎相同（仅第 1 条展开）→ 需回退逐条 `/full`，**流量不降反升**，直击该特性存在目的（省流）。
- 触发面：生产 systemd 单位 pin `OC_SLIMAPI_MAX_MESSAGE_BYTES=33554432`（deploy/oc-slimapi.service）= 默认组合；即**按模板部署即必现**。
- CHANGELOG 1.5.0 :319 已披露「超预算渐进降级」与顺风车内存例外，但**未披露默认参数组合下退化为常态主导行为**（披露口径将其呈现为边角：「预算耗尽的未开始项不发起抓取」）；handler docstring（`messages.py:966-970`：「fetched fan-out style under … budgets」+ `_merged_candidate_pairs` 的 16 槽语义）与实际默认行为不符。

### 6.4 定级

- 初判 P1 → **终判 P1（defect，置信度 high）**。§3.3 路径：可达输入（默认配置 + ≥2 折叠消息的页面——长会话翻页常态）触发错误行为（承诺的服务端合并 ≤16 条实际恒 1 条）且消费方可观察（响应内容退化为 skeleton）。不满足 P0 门槛（无互操作中断——响应仍是合法 skeleton 页；无数据损坏；无需重启恢复）。
- 建议方向（非实现）：预约粒度改为按候选数均分或迭代预留（如 `cap=min(max_message_bytes, remaining, merged_max_bytes//len(pairs))`）；或 `cap<=0` 候选进入待重试队列由 refund 唤醒；或启动校验拒绝 `max_message_bytes > merged_max_bytes` 且未显式确认的组合（fail-fast 提示语义变化）。

## 6.5 F-015 复核（qp_last_activity 无界）——终判 P2 risk

- 增长路径（已证）：`global_hub.py:678` 对每个 IMMEDIATE q/p 事件以**上游提供的 directory 字符串**为键写入（在 allowlist 闸门 :684 之前，被拒目录同样入键）；全仓无删除路径（rg 证实仅 :678 写、`app.py:505` 传引用给 sweep、`qp_sweep.py:113` 写同 dict）；QpSweepShadow 的 30 天逐出对 `_activity` 键**结构性失效**（§1.1 #13 / F-273），且 `_ingest_directory_source` 每 tick 把 `_activity` 键重新灌回 `_known_dirs`。
- 生产影响（按 §3.3 无上界规则检验）：键空间 = opencode 实例工作目录集合——单用户工作站自然基数极小（十级）；每条目 ≈100-300 B；达到 100 MiB 需 ~10⁵-10⁶ 个不同目录字符串，需上游（信任边界内固定 loopback opencode）或客户端经会话创建慢速刷目录——利用条件不现实。**无法证明「输入源可控 + 无自然约束」的 P1 要件 → 维持 P2**（初判 P2 → 终判 P2，置信度 high；修复方向：与 sticky 表同款 10k FIFO cap 或复用 sweep 的 TTL 语义并修复 F-273 的逐出失效）。

---

## 7. A9 新发现清单（详见 02-findings/F-271…F-279）

| 编号 | 严重度 | 类别 | 摘要 |
|---|---|---|---|
| F-271 | P2 | defect(perf) | messages 列表尾部 ETag sha256（1-2 次）+ gzip-6 在事件循环上执行——大 identity（text 全量内联 + merged splice）可停摆全部 SSE 订阅者百毫秒级 |
| F-272 | P3 | risk | ReplayLog per-sid 域外壳 epoch 内永不删除——sid 键无 cap（帧有界、外壳无界；设计自认，重启 GC） |
| F-273 | P3 | defect | QpSweepShadow 30d 逐出被 `_ingest_directory_source` 先行刷新 seen_at 结构性击穿——`_known_dirs`/`_seen_at`/`_next_run` 随 qp_last_activity 有效无界（F-015 放大器） |
| F-274 | P3 | risk | 上游 httpx limits 32/16 硬编码不可调；无 admission 并发面（merged 8 + q 8 + p 8 + 写 + SSE 1）≈25-30 贴近上限，突发 → pool 5s 超时 → 503 |
| F-275 | P3 | risk | 上游 SSE read=None 无读超时——半开上游连接永久检测不到，帧静默丢失直至 TCP 死亡 |
| F-276 | P3 | ops | lifespan 启动日志维护（migrate/compress/prune）同步阻塞事件循环且无超时上限——大积压延迟启动/就绪 |
| F-277 | P3 | ops | 启动冒烟 2×5s + /global/health 5s 串行——上游 hang（非 refused）时服务就绪延迟最坏 ~15s |
| F-278 | P3 | gap | v4 sessions SQL 索引假设仅靠手工 eqp_matrix.py 实证（合成库断言 / 真库仅采集不断言）+ 测试固化合成 fixture——真库索引漂移无 CI 门禁，只靠运行时熔断兜底 |
| F-279 | P3 | risk | merged 请求全程持有 raw-fetch lease（跨 fan-out + phase C admission 等待）——默认预算仅容 1 flight，一个慢 merged 页阻塞其余全部列表去重 |

## 8. 完成判据自查

- 内存表：31 行、四值域无空格（bounded 26 / unbounded 4 / unknown 1 / measured 0——本审计禁实测，无自造 measured）；unbounded 项均给出增长路径与 §3.3 定级处置。
- 延迟常量表：33 条（§4.1-4.4），rg 清单含 `SECONDS|_MS|timeout|INTERVAL|DELAY|TTL|Retry-After` 全量；每条附契约/设计依据与合理性评估。
- 热路径 / 连接池 / 启停 / F-006 / F-015 五项均有代码级证据与结论。
