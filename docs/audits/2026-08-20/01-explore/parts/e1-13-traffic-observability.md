# E1-13 流量观测链精读卡片（traffic / access_log / snapshot / middleware / request_id / metrics / sse_observability）

> 审计探索卡片，只读产物。引用格式 `src/...:行号`。全部七文件已全文精读（行数经 wc -l 核对）。

---

## src/oc_slimapi/traffic.py（845 行）

### 职责
全链路双向字节账本（TrafficLedger）+ 路径→逻辑 bucket 分类（bucketize）+ expand category 白名单归一 + v3 §9.2 观测矩阵 / SSE 生命周期存量 + v4 sessions degraded 每响应计数器（独立于 ledger 开关）。单 uvicorn worker / 单事件循环模型，`threading.Lock` 仅作诚实防护（docstring :5-8）。

### 对外符号
- `_LATENCY_SAMPLES`（:40）— 每 bucket 延迟样本 deque 上限（1024，最旧逐出）。
- `EXPAND_CATEGORIES`（:50-63）— 12 个 expand category 冻结表（单一事实源：versions 路由 capability 广告 + ledger 白名单）。
- `EXPAND_CATEGORIES_SET`（:64）、`_EXPAND_INVALID_CATEGORY = "invalid"`（:68）。
- `_normalize_expand_category(category)`（:71-81）— 白名单外 category 一律折叠到 `invalid`，防 `_expand` dict 无界增长（rev-gpt R1 M2）。
- `SSE_BUCKETS = {events_sse, token_stream_sse}`（:88）。
- `bucketize(method, path)`（:91-192）— 路径→bucket 映射（前缀序，specific 在前）。
- `_EXPAND_SEGMENT = "expand/"`（:200）、`_expand_tail(path)`（:203-222）— 段严格的 expand 路径尾部提取（空 sid / 裸 `/expand` 不匹配）。
- `expand_category_from_path(path)`（:225-240）— 提取原始 category 段（不白名单化，可返回空串/伪造值）。
- `_UP_IN_KEY/_UP_OUT_KEY/_CACHE_KEY`（:245-247）— scope state stash 键。
- `SESSIONS_SOURCE_STATE_KEY`（:258）、`DEGRADED_503_STATE_KEY`（:259）、`SESSIONS_SOURCE_VALUES = {"db","http"}`（:262）— v4 degraded 状态标记键拼写单一事实源。
- `stash_cache(request, state_value)`（:265-281）— catalog cache hit/miss stash（None 为 no-op）。
- `stash_up_in(request, n)`（:284-300）/ `stash_up_out(request, n)`（:303-313）— handler 累积 upstream 字节到 scope state。
- `_read_state_int(scope, key)`（:316-324）— 读 stash int（bool 拒绝、非正归零）。
- `read_sessions_source(scope)`（:327-337）/ `read_degraded_503(scope)`（:340-345）— 校验式读取 degraded 标记（垃圾值忽略）。
- `SessionsDegradedCounters`（:348-391）— per-response degraded 计数（`record_degraded_200` :378、`record_fail_closed_503` :382、`snapshot` :386），挂 `app.state.sessions_degraded`，刻意不随 traffic 开关关停。
- `SESSIONS_DEGRADED_STATE_ATTR`（:395）、`ensure_sessions_degraded_counters(state)`（:398-417）— 同步 get-then-set 惰性挂载（setattr 失败返回临时实例）。
- `TrafficLedger`（:420-845）：
  - `__init__`（:440-449）：一把 `threading.Lock` + 8 个累积结构（`_buckets/_sse/_latencies/_v3_matrix/_v3_sse/_expand/_v4_degraded`）。
  - `enabled` property（:451-453）。
  - `record_downstream`（:457-490）— 下行 HTTP：requests/downIn/downOut/errors4xx/errors5xx + 有界延迟样本。
  - `record_upstream`（:494-518）— 上行 HTTP：upOut/upIn（`method`/`status` 保留未用 :508-511）。
  - `record_sse_upstream`（:522-541）— 共享 `/global/event` 上行字节（只喂 `events_sse`，防双计）。
  - `record_sse_downstream`（:545-556）— 每帧下行字节 + framesEmitted。
  - `_new_bucket`（:558-568）/ `_new_sse`（:570-572）。
  - `_v3_status_class`（:576-580）— status→"Nxx"（非 int / bool → "none"；int 0 → "0xx"）。
  - `record_selector_request`（:582-611）— v3 §9.2 扁平键 `selectorResult|wireVersion|directoryForm|recordType|statusClass|bucket` 计数（自启动累计）。
  - `record_sse_lifecycle`（:613-635）— SSE open/close 存量（active 钳 0、孤儿 close 计 orphanCloses）。
  - `record_expand`（:640-672）— `category|status` 计数 + bytes（category 先经 :71 白名单化）。
  - `record_sessions_degraded`（:676-705）— v4 扁平键 `degraded|kind|statusClass|bucket`。
  - `snapshot`（:709-845）— `/slimapi/metrics` 的 `traffic` 块（buckets 合并 SSE、totals、ratios（upIn>0 才有）、latencyMs p50/p90/p99/count、`v3.matrix/sseLifecycle/sseActive`、`expand`、`v4.degradedMatrix`（稀疏，首条 degraded 后才出现 :843-844））。

### 依赖 / 被依赖
- 依赖：仅 stdlib（threading/collections/typing）。零仓库内 import（无循环）。
- 被依赖（rg 反查）：`middleware/traffic_accounting.py:61`、`app.py:38`（TrafficLedger 实例化）、`routes/versions.py:54`（EXPAND_CATEGORIES 广告）、`routes/messages.py:1271-1272`（文件中部 import 冻结表）、`routes/metrics.py:15`、`discovery.py:39` / `routes/_read_passthrough.py:61` / `permissions.py:16` / `health.py:9` / `_catalog_common.py:33` / `read_groups.py:78` / `questions.py:16` / `write_groups.py:83` / `sessions.py:37`（stash_up_* / stash_cache）、`sse/global_hub.py:52` / `sse/registry.py:29`（TYPE_CHECKING）、`sse_observability.py:29`（TYPE_CHECKING）；测试 10+ 文件。

### 状态 / 可变性
- 单把 `threading.Lock`（:441）守全部可变结构；无后台 task、无文件句柄。
- 除 `_latencies`（deque maxlen=1024，:40/:490）外全部单调递增、无上界重置（重启即失——由 traffic_snapshot 落盘补偿）。
- 键空间有界性：`_buckets` 由 bucketize 固定集界定；`_expand` 由 (12+1)×观测 status 界定；`_v3_sse` 靠调用方归一到 5 dim；`_v3_matrix` 界定依赖调用方值域（见疑点 5）。

### 错误路径
- 无 IO。`enabled=False` 时所有 `record_*` no-op、`snapshot()` 返回 `{"enabled": False}`（:762-763）。
- `ensure_sessions_degraded_counters` setattr 失败返回临时实例、绝不 raise（:410-416）。

### 疑问点（14）
1. **`passthrough` 桶名与现实不符（:192）**：3.0.0 反代已关闭，非 `/slimapi` 请求由 `proxy.py:44-51` 统一 404 `thin_route_not_found`；bucket 仍叫 "passthrough"，且 selector 对非 /slimapi 请求 stash `not_applicable`（`selector.py:72,104`）→ v3 矩阵出现 `not_applicable|…|passthrough` 键。`docs/manual/traffic-accounting.md` "按 bucket==passthrough 找未省流请求" 的运维口径已失真（现在全是被拒 404，非真实过境流量）。
2. **`sweep` 桶（:109-110）疑似死桶**：B1b shadow 预留路径 `/slimapi/_shadow/sweep`，而 `routes/metrics.py:40-41` 明言 "no HTTP sweep is issued"——无路由命中，桶永不出现。
3. **`/slimapi/config/` 仅匹配带尾斜杠前缀（:151）**：裸 `/slimapi/config`（无尾斜杠）落入 `other`（:190）而非 `providers`——需对照路由注册形态确认归桶口径一致。
4. **前缀无尾斜杠约束的过匹配**：`startswith("/slimapi/sessions")`（:141）、`startswith("/slimapi/messages")`（:124）会把 `/slimapi/sessionsfoo` 等归入 sessions/messages（此类路径最终 404，影响仅是 404 计入该桶 errors4xx）。
5. **`record_selector_request` 无值域白名单（:602-609）**：selector_result/wire_version/directory_form 由 middleware 透传 selector state 原值（`traffic_accounting.py:284-288`）；若 selector 未来引入新 result 值，矩阵键集静默扩张——与 `_expand` 的白名单防御（:71-81）不对称。
6. **`record_sse_lifecycle` 不校验 `result`（:613-626）**：键可为任意字符串；当前唯一调用方 `sse_observability._dims`（:59-60）已归一到 `SSE_RESULT_DIMS`，但 ledger 侧零防御（对比 :71）。
7. **status=0 产生 "0xx" 键且无错误计数**：middleware `status_code` 初值 0（`traffic_accounting.py:181`），app 未发 `http.response.start` 即正常返回时 status=0 入账；`_v3_status_class`（:577-580）对 int 0 输出 "0xx"；`:486-489` 的 4xx/5xx 判定均不命中 → 该行零错误分类。
8. **分位数口径**：`samples[min(n-1, int(n*0.50))]`（:805-807）为 floor 索引（n=100 时 p99 取第 98 位样本），与常见 nearest-rank ceil 口径略有偏差——分析侧需知。
9. **latency 语义异质**：deque 只保最近 1024 样本（:40），latencyMs 是"近期"分位而非全程；SSE 桶的 duration_ms 是整连接生命周期（连接关闭才记账）→ 与 HTTP 桶延迟不同质，混读易误。
10. **snapshot 持锁做 O(n log n)**：`list(...)` 拷贝 + `sort()`（:800-803）在 `self._lock` 内执行；`/slimapi/metrics` 高频拉取与所有 record_* 争锁（量级小但热点在锁内）。
11. **`record_upstream` 的 method/status 保留未用（:508-511）**：签名宽于用途，易误导调用者以为有 per-method 拆分。
12. **`ensure_sessions_degraded_counters` 临时实例丢计数（:410-416）**：只读 state 对象部署下 sessionsDegraded 恒 0（best-effort 已注释声明，但无可观测信号提示降级发生）。
13. **`snapshot()` SSE 口径差已文档化（:750-757）**：活跃 SSE 期间 `downOut>0` 而 `requests==0`——契约已知项，非 bug，审计下游消费者需容忍。
14. **`v4` 块稀疏出现（:843-844）**：`set(traffic)` 精确形状消费者在首条 degraded 前后看到不同键集——zero-knowledge additive 约定，消费端需容错。

---

## src/oc_slimapi/access_log.py（726 行）

### 职责
结构化 JSONL 访问日志：`DailyAccessHandler` 按天写 `access-YYYY-MM-DD.jsonl`（每行一请求/SSE 生命周期标记），独立函数做 gzip 压缩 / 按保留天数清理 / 旧格式迁移，async 维护循环周期执行；全程 best-effort（失败 warning、绝不 raise，:19-23）。

### 对外符号
- `_LOGGER_NAME`/`_setup_lock`（:43-44）；`_MAINT_LOCK`（:56）— 跨线程序列化 compress/prune/migrate（整函数粒度）。
- `_active_handler_ref`（:63）— 当前已装 handler 引用（P1-25：维护期避免 unlink 活 handler 持有的 .jsonl）。
- `_get_maint_log()`（:71-75）— 维护日志（独立于 access logger，防诊断 warning 污染 jq 解析的 jsonl，:65-68）。
- `_ACCESS_LOG_RE`（:79）— 严格日文件名正则 `^access-(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$`。
- `get_access_logger()`（:87-89）。
- `hash_client_id(raw, salt=None)`（:92-103）— sha256/hmac-sha256 前 16 hex。
- `DailyAccessHandler(logging.Handler)`（:111-203）：`__init__` :123、`__del__` :129、`_ensure_dir` :137、`_open_file` :140（append 模式）、`_close_current_fh` :144、`current_path` property :155-169（P1-25 读口径）、`emit` :173-198（按 `record.created` 定日期跨零点换文件；单调用行写 :195 + flush :196）、`close` :200。
- `setup_access_log(*, enabled, dir)`（:211-259）— 幂等安装/清理旧 handler；失败降级 `logger.disabled=True` 绝不 raise。
- `write_access_log(...)`（:267-364）— 每请求行（固定键集 + 稀疏 `cache`/`sessionsSource`/`degraded503` 尾字段，:350-363）。
- `write_sse_lifecycle_log(...)`（:367-405）— sse_open/sse_close 行（无字节/时长字段）。
- `_unique_tmp_path(base, suffix)`（:413-425）— `.{suffix}.{pid}.{uuid8}` 唯一临时名。
- `_cleanup_leftover_tmp(dir)`（:428-446）— 删孤儿 tmp（含 PID 域变体与 legacy 变体）。
- `compress_old_access_logs(dir, today)`（:449-544）— 压缩 < today 的 .jsonl：严格命名校验、已存在 .gz 跳过、活 handler 文件跳过（P1-25 :509-515）、unique tmp + `os.replace` 原子提交、源删除失败保 .gz。
- `prune_old_access_logs(dir, retain_days, today)`（:547-583）— 删除 `file_date < today - retain_days` 的 .jsonl/.jsonl.gz（边界日保留；retain<=0 no-op）。
- `migrate_legacy_access_log(dir)`（:586-625）— 旧 `access.jsonl(.N)` → `access-legacy-{mtime:%Y%m%d}-{N}.jsonl.gz`。
- `_migrate_one(path, label, log)`（:628-660）— 单文件 gzip+替换+删源（BaseException 清理 tmp）。
- `run_access_log_maintenance_loop(*, dir, retain_days, interval_s, stop_event, extra_prune)`（:668-726）— 循环 compress→prune→extra_prune（均 `asyncio.to_thread`），单失败不杀循环；不负责 cancel 时 join 线程（:691-700 契约注释）。

### 依赖 / 被依赖
- 依赖：stdlib + `.logging_config.get_logger`。
- 被依赖：`middleware/traffic_accounting.py:58`（get_access_logger/hash_client_id/write_access_log）、`sse_observability.py:23`（get_access_logger/write_sse_lifecycle_log）、`app.py:14-17`（setup/migrate/maintenance loop）；测试 `tests/test_access_log.py` 等。

### 状态 / 可变性
- 模块级单例：`_active_handler_ref`（:63，仅 setup_access_log 在 `_setup_lock` 下写）、`_MAINT_LOG`（:67 惰性）。
- `_MAINT_LOCK`（:56）进程内锁——**跨进程无效**（见疑点 4）。
- handler 持一个 append 模式文件句柄，跨零点首条 emit 时切换（:186-191）；每行 flush（:196）、无 fsync。
- 维护循环在 app.py 作为 asyncio task 运行（`app.py:680-683`），shutdown 经 stop_event + drain 超时 cancel（`app.py:693-718`）。

### 错误路径
- emit 异常 → `handleError`（:198，logging 默认行为：`raiseExceptions=True` 时写 stderr）。
- setup 失败 → warning + `logger.disabled=True`（:252-258）；app.py:243-251 以实际安装结果 gate 维护循环（P1-39）。
- compress/prune/migrate 单文件失败 → warning 继续；循环内三段独立 try（:711-726）。

### 疑问点（12）
1. **【实证】legacy 归档永不清理**：`prune_old_access_logs` 的 glob `"access-*.jsonl.gz"`（:566）会命中 `access-legacy-20260701-1.jsonl.gz`，但 `_ACCESS_LOG_RE`（:79）要求 `access-` 后紧跟 `\d{4}-\d{2}-\d{2}`，legacy 名不匹配 → `continue`（:568-569）。RETAIN_DAYS 永远触不到 legacy 归档，一旦迁移产生便永久留存（已用 Python re 实测：legacy→False，daily→True）。
2. **每行 write+flush 两次系统调用、无 fsync（:195-196）**：进程硬崩丢页缓存尾部行；且与 traffic_snapshot P1-27 的"单 write 调用 POSIX 原子性"口径不同源（此处 write(msg+"\n") 已是单 write 调用 :195，但紧随 flush，注释自认"缩小而非消除半行窗口"）。
3. **行内 ts 与文件名日期双时钟**：文件名日期取 `record.created`（:186），行内 `ts` 取 `write_access_log` 调用时的 `datetime.now()`（:335）——logger 无队列时几乎同刻，但两采样点独立，理论上可分属两日（与 snapshot P1-26 单采样点修复对照，此处未做同等处理）。
4. **`_MAINT_LOCK` 进程内锁 + tmp 清理无年龄判断（:428-446）**：多 sidecar 实例共享同一 logs 目录时，B 进程的 `_cleanup_leftover_tmp` 可误删 A 进程 in-flight 的 unique tmp（→ A 的 os.replace 失败降级 warning）。单 worker 假设散见注释但代码处无断言/无锁文件。
5. **损坏 .gz 无自愈**：已存在 .gz 即跳过（:500-501，注释自认 "a damaged .gz … is not re-compressed"），且源 .jsonl 已删 → 该日数据可用性依赖人工干预。
6. **压缩成功但源删除失败（:526-532）→ .jsonl/.gz 双存**：下次 tick 因 .gz 已存在而跳过（:500-501），源文件不会被重试删除；仅当 retain_days>0 时由 prune 兜底（:566-575 会清 .jsonl）；retain_days=0（默认）下永久双存。
7. **`prune` 边界多留一天（:562,575）**：`deadline = today - retain_days`、`file_date < deadline` 才删 → retain_days=3 实际保留 4 个日历日文件（today-3 边界保留）。与直觉"保留 3 天"差一天，文档口径需核对。
8. **`handleError` 走 logging 默认（:198）**：`logging.raiseExceptions=True`（开发默认）时向 stderr 打 traceback——生产噪音渠道，未接 get_logger 体系。
9. **recordType 过滤陷阱（任务点名）**：`write_sse_lifecycle_log` 的 sse 行同样带 bucket/status（:392-404），聚合侧 `aggregate_v3_observability` 将其计入 counts 矩阵（键含 recordType 维）——消费 jq 若忘按 `recordType=="request"` 过滤，SSE 桶"请求数"被 open/close 行放大约 3 倍。
10. **`hash_client_id` 16 hex = 64 bit（:102-103）**：生日碰撞 ~2^32 量级；单部署设备数下够用，但无 salt 时跨部署可链接（sha256 无密钥）——隐私声明依赖运维配 salt。
11. **shutdown 不 join in-flight gzip 线程（:691-700）**：app.py drain 超时后 cancel（`app.py:703-718`），进程退出时后台 gzip 线程可能被硬杀留下 unique tmp——依赖下次启动 `_cleanup_leftover_tmp`（:428）兜底，冷启动前目录残留。
12. **`write_access_log` 的 `status` 形参未做值域检查（:339）**：middleware 传 0 时行内 `"status": 0`（联动 traffic_accounting 疑点 8），jq 按状态类过滤时 0 行成黑洞。

---

## src/oc_slimapi/traffic_snapshot.py（541 行）

### 职责
两块：(a) `prune_old_snapshots` 按天清理旧快照文件；(b) v3 §9.2 纯分析函数 `aggregate_v3_observability`（access log 行 → 跨日矩阵/SSE 配对序列）；(c) `TrafficSnapshotter` 后台循环把 `TrafficLedger.snapshot()` 全量落盘为每日 `traffic-snapshot-YYYY-MM-DD.jsonl`（SSE 上游字节成本的唯一持久载体，重启即失的补偿）。

### 对外符号
- `_snapshot_file_re(stem)`（:75-76）— 快照文件名正则。
- `prune_old_snapshots(directory, stem, retain_days, today)`（:79-101）— 删 `file_date < today - retain_days`（retain<=0 no-op）。
- `_SSE_DIMS`（:111）— `("v2","v3","v4","absent","not_applicable")`，与 `selector.SSE_RESULT_DIMS` 手工双拷贝（注释自认 grep-verified）。
- `_DEGRADED_KINDS`（:117）、`_DEGRADED_SEED_KEYS`（:122-125）— 每日 degraded 图固定种子键。
- `_v3_row_key(row)`（:128-139）— 行→扁平矩阵键（`str(x or "null")` 空值折叠）。
- `_degraded_row_key(row)`（:142-164）— 稀疏标记 → `degraded|kind|statusClass|bucket`（无标记返回 None）。
- `aggregate_v3_observability(records)`（:167-286）— 输出 `counts/countsByDate/sseActive(窗口期初存量)/sseOpens/sseMatchedCloses(按 lifecycleId §11.8 配对)/sseOrphanCloses/sseLive/degradedCounts(degradedCountsByDate)`。
- `TrafficSnapshotter`（:289-541）：`__init__` :318-335（dir+stem 模板、bootTs/runId/pid/start_monotonic）、`start` :341-372（首帧同步写，失败永久 inactive）、`stop` :374-400（cancel 后必写终帧）、`active` property :402-405、`_loop` :411-428（逐迭代兜异常）、`_path_repr` :430-432、`_write_once` :434-541（单时钟采样点 P1-26 :452/501；mkdir best-effort :502-510；单 write 调用 P1-27 :532-533；明确不 fsync :524-531）。

### 依赖 / 被依赖
- 依赖：stdlib only（`ledger` 参数 duck-typed 防循环，:321）。
- 被依赖：`app.py:39`（TrafficSnapshotter + prune_old_snapshots）、`app.py:291-299`（实例化/stop 回调）、`app.py:668-674`（prune 经 functools.partial 挂为 access-log 循环 extra_prune）、`app.py:720-722`（start，双开关 gate）；`aggregate_v3_observability` 无 src 内调用方（纯分析时工具，tests + manual 使用）。

### 状态 / 可变性
- snapshotter 持一个 asyncio Task（:335）；每帧 open→write→close，无常驻句柄（:513-534）。
- 累积器上界：ledger 侧见 traffic.py 卡片；快照**文件**每 interval（默认 300s，config:552-554）追加一行全量 ledger JSON，无单文件大小 cap、无压缩（对比 access log 的 gzip 链）——文件体积 = 帧数 × 全量键集大小，仅按天 rotate + retain prune 约束。
- `aggregate_*` 为纯函数（局部状态）。

### 错误路径
- 首帧失败 → 永久 inactive + warning（:359-369）；循环迭代失败 → warning 继续（:424-428）；终帧 best-effort 忽略返回值（:399-400）；mkdir/open 失败 warning 返回 False（:502-541）。
- prune 单文件 unlink 失败 → warning（:97-100）。

### 疑问点（11）
1. **快照文件永不压缩**：prune 只删不压（:79-101），无 compress 对应物——与 access log 的 gzip 生命周期不对称；300s 全量帧（含全 v3 矩阵键）长期裸存，磁盘占用可观（生产 retain=30 天时 30 × 288 帧全量 JSON）。
2. **RETAIN_DAYS 清理链耦合缺陷（任务点名）**：snapshot prune **只**经 extra_prune 挂在 access-log 维护循环上（`app.py:668-683`），而该循环整体 gated on `access_log_active`（`app.py:243` 安装实际结果）——`OC_SLIMAPI_ACCESS_LOG_ENABLED=false`（或目录安装失败）+ snapshot enabled 时，snapshotter 照常每 300s 写文件（`app.py:720-722` 独立启动）但**清理永不运行** → 快照目录无界增长。无任何告警暴露此状态。
3. **`_SSE_DIMS` 双拷贝漂移风险（:111）**：与 `selector.SSE_RESULT_DIMS`（selector.py:109）靠注释纪律同步（"the only two copies; grep-verified"），无 import 复用。
4. **聚合假设 append 序（:168-170）**：`day_start_stock` 在每日首行处理前冻结（:228-231）——若输入非严格时间序（gz 解压拼接顺序错乱），期初存量取错；纯约定无排序防御。
5. **缺 ts 行落 "unknown" 伪日期（:224）**：`str(row.get("ts",""))[:10] or "unknown"`——聚合输出出现 "unknown" 日期键并占 day_order 一位。
6. **counts 不过滤 recordType（:239-241）**：request/sse_open/sse_close 全计入矩阵（键第 4 段可区分）——消费侧忘过滤即三倍计数（联动 access_log 疑点 9）。
7. **lifecycleId 配对跨日但 open_ids 无窗口回收**：`open_ids[dim]`（:213）跨整个聚合窗口累积，未关闭的 open（活跃连接或进程崩溃遗留）永不回收——窗口末 `sseLive` 含全部悬挂 open（by design :194）；巨窗聚合时 set 大小 = 活跃+悬挂连接数，无上界告警。
8. **终帧与循环末帧可能同秒重复**：stop 先 cancel（sleep 处生效）再写终帧（:389-400）——相邻两帧可能零 delta 重复；分析侧 delta 推导需容忍（设计已知，未在输出标记 final 帧）。
9. **首帧失败无自愈（:359-369）**：瞬时磁盘满也永久 inactive 直至重启；snapshotter 的 active 状态不经 `/slimapi/metrics` 暴露（仅日志可见）——运维盲区。
10. **prune glob 未转义 stem（:85）**：regex 侧 `re.escape`（:76）已防，glob 侧 `f"{stem}-*.jsonl*"` 未转义——stem 含 glob 元字符（`[`/`*`）时行为漂移（现实 stem 为固定配置值，低风险）。
11. **`_write_once` 的 enabled=False 早退 return True（:463-464）**：start 已在 :356 检查且 enabled 不可变——防御性死分支，无害但暗示对 ledger 状态的不信任未消除。

---

## src/oc_slimapi/middleware/traffic_accounting.py（435 行）

### 职责
纯 ASGI 记账中间件：包 receive/send 计下行线字节（downIn/downOut，wire 口径），请求结束时读 handler stash 的 upstream 字节并入账，写一行 access log，喂 v3 矩阵 / expand / v4 degraded；SSE 真流（200+text/event-stream）downOut 交由 per-frame 计数器防双计。

### 对外符号
- `_ledger_from_scope(scope)`（:80-88）/ `_config_from_scope(scope)`（:91-99）— app.state best-effort 查找。
- `_CLIENT_IDENT_HEADERS`（:103-107）— X-Client-Name/Version/Id 头名→槽位。
- `_read_client_headers(scope)`（:110-144）— 校验（≤128 UTF-8 字节、无控制字符、非空白；重复头首个有效值胜）。
- `TrafficAccountingMiddleware`（:147-246）：`__init__` :152-159、`__call__` :161-246（非 http 直通 :162-165；client 头 stash :173-177；`counted_receive` :185-193 / `counted_send` :195-212（status+content-type 捕获）；BaseException 路径先记账再 raise :216-233）。
- `_record(...)`（:249-435）— 请求终点记账：access log 写入（:297-344）→ ledger 记账（record_downstream/selector/expand/degraded/upstream :346-419）→ app.state degraded 计数器（:425-435，独立于 ledger 开关）。

### 依赖 / 被依赖
- 依赖：`access_log`（get_access_logger/hash_client_id/write_access_log）、`logging_config`、`selector`（SELECTOR_STATE_KEY/DIRECTORY_FORM_STATE_KEY）、`traffic`（bucketize/SSE_BUCKETS/stash 键/read_* 等）、`request_id`（REQUEST_ID_KEY）。
- 被依赖：`app.py:26,753`（add_middleware）；测试 test_traffic_integration/test_expand_config 等。

### 状态 / 可变性
- 无实例级可变状态（`__slots__ = ("app","logger")`，:150）；计数全部委托 ledger / logging。
- 作用域内闭包计数器 `down_in/status_code/down_out/content_type`（:179-182）随连接生命周期。

### 错误路径
- `_record` 三段独立 try/except（access log :343-344；ledger :418-419；degraded 计数器 :434-435），全部 warning 吞异常——记账绝不破坏请求。
- 中间件异常路径记账 `status_code or 500`（:225）。

### 疑问点（13）
1. **404/405 覆盖、501 不覆盖（任务点名）**：HTTP catch-all 404（`proxy.py:44-51`）走正常路由栈 → 记账（bucket=passthrough、errors4xx）；405（FastAPI 路由层异常经 ExceptionMiddleware 转 response，过 counted_send）覆盖；**WS 501 stub（`proxy.py:34-38`）scope type="websocket" → `__call__` :162-165 直接放行，无 access log 行、无 ledger 计数**——websocket 类型对记账全盲。
2. **未处理异常 500 的响应字节丢失**：Starlette ServerErrorMiddleware 在本中间件**之外**生成 500 响应，绕过 counted_send/send_with_rid → 该响应体不计 downOut、无 X-Request-ID 回显头；except 路径（:216-233）行内 down_out 仅为异常前已发字节（通常 0）。注释 :217-218 "disconnects / 500s still count" 对行成立、对字节不成立。
3. **"Outermost" 文档漂移（:148）**：类 docstring 自称 outermost，但 `app.py:755` RequestIdMiddleware 后加（Starlette last-added=outermost）→ 真实序 RequestId > TrafficAccounting > Selector；`app.py:750-752` 注释同病。
4. **SSE 桶 upstream stash 一律忽略（:410-417）**：`if not is_sse and (up_in>0 or up_out>0)`——SSE 路径上的**非流式**错误响应（400/503，is_sse 按 bucket 恒真）若 handler 曾 stash up_in（如已向上游发请求），字节只进 access log 行、不进 ledger（events_sse.upIn 恒由 hub 独供）。是否所有 SSE 错误路径零 stash 需 routes/events.py、token_stream.py 佐证（本次范围外）。
5. **early-reject body 不计 downIn（:25-34 文档化）**：version gate 400 等未 consume 的请求体字节不入账——wire 真实口径的代价，已知约定。
6. **SSE 双计防护依赖 content-type 约定（:354-368）**：`is_real_sse_stream` 需 status==200 且 content-type 含 text/event-stream；注释自认未来 SSE 变体漏设头即静默双计 downOut——无断言/无测试外的防线。
7. **status=0 入账（:181,239）**：app 未发 response.start 即正常返回（理论路径）→ 行/矩阵出现 status 0 / "0xx"（联动 traffic.py 疑点 7）；异常路径有 `or 500` 兜底、正常路径无。
8. **`_record` ledger 段异常标签误导（:419）**：统一 log "record_upstream failed"，但该段含 downstream/selector/expand/degraded 全部 record 调用——排障时日志指向错误。
9. **access log 行先于 ledger 写（:319 → :346）**：ledger 记账失败时行已落盘——行与账本可短暂不一致（各自 best-effort，无补偿）。
10. **client 头与 request_id 的重复头策略不一致（:127-128 vs request_id.py:45-59）**：client 头"首个**有效**值胜"（先出现的无效值不阻塞后续重复头）；request_id"首个匹配头即定，无效则整头弃用"（不尝试第二个有效值）。
11. **client_id 明文模式（:314-317）**：`client_id_hash=false` 时设备 id 明文入日志（运维开关的隐私权衡；fail-closed 默认 hash :310-313 正确）。
12. **`traffic_client_*` state 键信任边界（:174-177 写、:300-303 读）**：中间件写后 handler 理论可覆盖 state 值（当前无此用例）；读取无二次校验。
13. **content-type 取首头（:203-207 break）**：畸形多 content-type 请求按首头判 SSE——与 RFC 单头假设一致，无实质风险（记录在案）。

---

## src/oc_slimapi/middleware/request_id.py（111 行）

### 职责
纯 ASGI X-Request-ID 注入/提取：入站头（可打印 ASCII、≤128 字符）采用否则生成 uuid4.hex；存 `scope["state"]["request_id"]`；HTTP 响应头回注（过滤内层同名头）；WebSocket 仅存 state。

### 对外符号
- `REQUEST_ID_KEY = "request_id"`（:23）。
- `_find_request_id(scope)`（:29-60）— 入站头校验（strip、非空、≤128、全字节 0x20-0x7e；P1-15 非 ASCII 拒绝防 httpx build 时异常 500）。
- `RequestIdMiddleware`（:63-111）：`__slots__=("app",)` :73、`__init__` :75-76、`__call__` :78-111（非 http/ws 直通 :79-81；ws 分支仅存 state :109-111；`send_with_rid` :94-106 过滤+追加单头）。

### 依赖 / 被依赖
- 依赖：stdlib（uuid）。
- 被依赖：`app.py:25,755`（最外层中间件）、`upstream.py:9,140`（读 REQUEST_ID_KEY 转发上游）、`sse_observability.py:72`（函数内 import 读 rid）；`traffic_accounting.py:72`（import 常量，access log 行 requestId 字段）；测试 test_request_id/test_command_routes。

### 状态 / 可变性
- 无可变状态（纯转发 + state 注入）。

### 错误路径
- 无显式 try/except：入站头解析全部防御式返回 None → 生成新 id；无 raise 路径。

### 疑问点（6）
1. **客户端可注入 request_id（:84-86 直接采用）**：rid 进 access log（`traffic_accounting.py:330`）且被 proxy 转发上游（upstream.py:140）——排障关联性可被外部污染（固定 rid 混淆归因）；服务端未区分"内生成 vs 外来"（无前缀命名空间）。
2. **重复 X-Request-ID 头只看第一个（:45-59）**：首个无效（超长/非 ASCII）→ 弃用整头重新生成，不尝试后续重复头的有效值——与 `_read_client_headers` 的 lenient 策略相反（见 card 4 疑点 10）。
3. **内层 X-Request-ID 被无条件替换（:97-105）**：handler 显式设置的不同 rid 会被覆盖（当前仓库无此用例；upstream 回显场景两值相同无实害）。
4. **WS 无 rid 回显（:109-111）**：WS 501 响应无 X-Request-ID 头，且 traffic middleware 对 ws 不记账（联动 card 4 疑点 1）——WS 探测请求在两个观测面都不可见。
5. **`rid.encode("utf-8")`（:104）**：校验已限 ASCII，等价 ascii 编码——防御冗余无害。
6. **docstring 顺序声明（:8-12）与实际栈序一致但表述反直觉**："registered *after* the traffic-accounting middleware" 才能使其更外层——读者易误解为内层；与 card 4 疑点 3 的文档漂移同源。

---

## src/oc_slimapi/routes/metrics.py（111 行）

### 职责
`GET /slimapi/metrics`（T3 观测端点）：聚合 hub registry 快照 + 可选附加块（tokenStream/traffic/sweep/dbaux/sessionsDegraded/replay），gzip 协商响应。自身零业务逻辑，纯拼装。

### 对外符号
- `router = APIRouter(prefix="/slimapi", tags=["metrics"])`（:17）。
- `metrics(request)`（:20-111）— 唯一 handler：
  - `hubs.snapshot_metrics()`（:22）→ `{sse:{subscribers,hubs,clients},skeleton}` + `batch=None`（:23）。
  - `tokenStream`（:29-31，有 token_registry 才有）。
  - `traffic`（:36-38，有 ledger 才有，直接内联 `ledger.snapshot()` 全量）。
  - `sweep`（:42-44，qp_sweep 存在且 enabled 才有）。
  - `dbaux`（:51-69，available/mode/reason/generation/source/latency{p50_ms,p99_ms,samples,total}/breaker_open/counters；注释 :48-50 声明不回显 DB 路径）。
  - `sessionsDegraded`（:80-84，计数器已挂载才有，`{degraded_200,fail_closed_503}`）。
  - `replay`（:97-107，epoch + domains/frames/bytes/barriers + 其余计数器；注释 :94-96 声明不泄帧载荷/目录路径）。
  - `json_response(..., accept_encoding=...)`（:108-111）。

### 依赖 / 被依赖
- 依赖：`gzip_util.json_response`、`traffic.SESSIONS_DEGRADED_STATE_ATTR`、app.state（hubs/token_registry/traffic_ledger/qp_sweep/dbaux/replay_log）。
- 被依赖：`app.py:29`（import）+ `app.py:760`（include_router 元组）；INTERFACE_MAP 有记录（check_routes_doc 校验对象）。

### 状态 / 可变性
- 无自有状态；每次调用现拉各源快照（`ledger.snapshot()` 在 ledger 锁内做拷贝+排序，见 traffic.py 疑点 10）。

### 错误路径
- `request.app.state.hubs`（:22）无 getattr 容错——未挂 hubs 的 app 直接 AttributeError→500（生产恒挂；对比后续块全部 getattr 容错）。
- 其余块缺席即省略（zero-knowledge additive 约定）。

### 疑问点（9）
1. **敏感信息审查（任务点名）**：暴露字段全集中无 query string、无目录名、无文件路径——`subscriberId` 是随机 token（`sse/hub_types.py:240` `"sub_" + secrets.token_hex(4)`）；replay `domains` 是计数（:100-103）；traffic buckets 仅桶名。**待查项**：`dbaux.reason`（:57-58）的具体取值本文件不可见（注释 :48-50 声称不回显 DB 路径、`source` 仅通道标签）——需 dbaux.snapshot 卡片佐证 reason 字符串不含路径/错误原文。
2. **`/slimapi/metrics.traffic` 命名口径（文档 vs wire）**：AGENTS.md 与 traffic.py 注释（:120,:128,:144）以 `/slimapi/metrics.traffic` 指称 traffic 块，但**无此路由**（rg 全仓无注册）——实际是 `/slimapi/metrics` 响应内的 `traffic` 子块；文档写法易被读成独立端点。
3. **metrics 端点受版本选择器管辖**（docstring :7-9）：`GET /slimapi/metrics` 需带 `?v=3`，否则 400 version_required（唯一豁免是 /slimapi/versions）——监控探针/告警抓取必须带版本参数，运维便利性折损且易踩坑。
4. **`state.hubs` 无防御（:22）**：与后续块的 getattr 容错风格不一致（测试 app 面）。
5. **`sessionsDegraded` 首请求前缺席（:80-84 + 注释 :74-79）**：刚启动进程的 metrics 响应无此块，首个过栈请求后才出现——监控需容忍字段缺席（且 handler 先于中间件记账执行，首次 GET 自身不触发挂载）。
6. **`getattr(qp_sweep, "enabled", True)`（:43）默认 True**：无 enabled 属性的 sweep 对象会被当作启用调用 metrics()——与注释 "test apps intentionally omit"（:41）意图相悖的兜底方向（缺属性应默认 False 更保守）。
7. **`batch` 恒 null（:23）**：死键为兼容保留——契约形状冻结项，无消费逻辑。
8. **`hubs_snapshot["sse"]["tokenStream"]`（:31）假定 "sse" 键存在**：registry.snapshot_metrics 恒返回该键（registry.py:359-367）成立——隐式耦合无断言。
9. **无 rate-limit/缓存控制**：每次拉取全量 ledger 快照（含锁内排序）——loopback+stunnel 部署模型下可接受；高频拉取与记账争锁（联动 traffic.py 疑点 10）。

---

## src/oc_slimapi/sse_observability.py（130 行）

### 职责
SSE 生命周期观测：每条 SSE 连接写 `sse_open`/`sse_close` 两行 access log（共享进程单调 lifecycleId 配对）并同步 bump ledger 的 sseActive 存量；全部 best-effort 绝不断流。

### 对外符号
- `_lifecycle_lock`/`_lifecycle_counter`（:31-32）。
- `next_lifecycle_id()`（:35-38）— 进程单调 id（自 1 起，锁内 next）。
- `_access_logger()`（:41-43）— 测试注入点。
- `_dims(scope)`（:46-60）— (selectorResult, wireVersion, directoryForm, sseActive-dim)；缺 selector state → "absent" 维；None scope → 全 null + absent。
- `_ledger_from_scope(scope)`（:63-68）、`_request_id(scope)`（:71-80，函数内 import REQUEST_ID_KEY）。
- `_emit(scope, *, bucket, record_type, lifecycle_id, status)`（:83-114）— 写 lifecycle 行（异常 pass）+ ledger.record_sse_lifecycle（异常 pass）。
- `sse_open(scope, *, bucket)`（:117-125）— open 行（status 恒 200）返回 lifecycle id。
- `sse_close(scope, *, bucket, lifecycle_id)`（:128-130）— close 行（status None）。

### 依赖 / 被依赖
- 依赖：`access_log`（get_access_logger/write_sse_lifecycle_log）、`selector`（SELECTOR_STATE_KEY/DIRECTORY_FORM_STATE_KEY/SSE_RESULT_DIMS）、`traffic`（TYPE_CHECKING）、`middleware.request_id`（函数内）。
- 被依赖：`routes/events.py:16,182,242`（bucket="events_sse"）、`routes/token_stream.py:67,252,307`（bucket="token_stream_sse"）。

### 状态 / 可变性
- 进程级 `_lifecycle_counter`（itertools.count）+ 锁——重启归零（跨重启 orphan 由聚合侧容忍，traffic_snapshot.py:188-192）。

### 错误路径
- `_emit` 两段各自 `except Exception: pass`（:107-108,:113-114）——观测丢失**无任何日志**（连 warning 都没有；与 access_log 模块 "warning + 继续" 的姿态不同）。

### 疑问点（8）
1. **模块 docstring dim 列表漏 "v4"（:12-13）**：写 "v2/v3/absent/not_applicable"，而 `SSE_RESULT_DIMS`（selector.py:109）与 `traffic_snapshot._SSE_DIMS` 均含 v4——文档漂移。
2. **观测丢失零可见性（:107-108,:113-114）**：lifecycle 行/ledger bump 失败静默——"never break the stream" 的代价是 SSE 观测链路自身无健康信号（对比 middleware `_record` 至少 warning）。
3. **sse_open 恒记 status=200（:124）**：调用点在 generator 内流真正建立后，但若 StreamingResponse 实际未发出任何字节（客户端即刻断开），open 行的 200 是断言而非观测事实（close 行 status=None 部分补偿）。
4. **open 后进程崩溃 → 永久悬挂 open**：ledger sseActive 与聚合 sseLive 均高估直至重启（孤儿 close 机制只处理"多 close"，不处理"少 close"）；无对账/心跳校正。
5. **`_dims` 的 None scope 分支（:50-51,117-122）为测试专用**：生产 scope 恒在——测试路径进了生产函数签名（`scope | None`），调用点均 `getattr(request,"scope",None)`（events.py:182 等）防御。
6. **`_request_id` 函数内 import（:72）过度防御**：`middleware.request_id` 不依赖本模块，无真实循环——每次调用多一次 import 查表（开销可忽略，样式噪音）。
7. **`not_applicable` dim 的现实来源存疑**：SSE 端点都在 `/slimapi/**` 下（selector 必然给出 v2/v3/v4/absent 之一）；`not_applicable` 只对非 /slimapi 请求 stash（selector.py:72）——SSE 生命周期行的该 dim 理论上不可达（死维度，与 traffic.py 疑点 1 的 passthrough 残余同源）。
8. **lifecycle 行不含字节字段（by design，access_log.py:385-388）**：SSE 全量观测 = lifecycle 行（open/close 配对）+ request 行（连接级字节，关闭时）+ ledger per-frame 计数三处拼合——排障需按 lifecycleId/requestId join，无单一视图。

---

## 附：跨文件链路要点（RETAIN_DAYS 清理链 / 写盘链 / 记账覆盖面）

- **RETAIN_DAYS 链**：`config.py:537` `access_log_retain_days` 默认 0（生产 systemd 设 3）→ 启动一次性 migrate/compress/prune（`app.py:268-277`）+ 维护循环每小时 compress→prune→extra_prune（`app.py:676-683`，interval 默认 3600s `config.py:540-541`）→ `access_log.py:715-726`。snapshot retain（`config.py:561-562` 默认 0，生产 30）仅经 extra_prune 挂靠该循环（`app.py:668-674`）——**access log 关闭/安装失败时 snapshot 清理随之停摆**（见 traffic_snapshot 疑点 2）。
- **access log 写盘链**：handler 单句柄 append + 每行 flush 无 fsync（access_log.py:195-196）→ 跨零点按 record.created 换文件（:186-191）→ 维护期 gzip（unique tmp + os.replace 原子提交 :517-523，活文件跳过 :509-515）→ prune 双格式（:566-575）。legacy 归档（`access-legacy-*`）例外：**regex 不匹配 → 永不 prune**（access_log 疑点 1）。
- **middleware 记账覆盖面**：HTTP 200/4xx/5xx/404(catch-all)/405(路由层) 全覆盖（含异常路径 500 记行）；盲区 = websocket 类型（WS 501 stub 不记账）、ServerErrorMiddleware 生成的 500 响应字节、early-reject 请求体 downIn。
- **request_id 传播**：入站头/生成（request_id.py:84-89）→ state → traffic_accounting 读入 access log（:299,:330）→ proxy 转发上游（upstream.py:140）→ 响应头回注（request_id.py:94-106，替换内层同名头）。
