# E1-04 精读卡片 — src/oc_slimapi/config.py

> 审计探索产物（只读精读），2026-08-20。全文 1158 行已逐行读取，无抽样。
> 引用格式 `src/oc_slimapi/config.py:行号`（省略前缀时均指本文件）。

### src/oc_slimapi/config.py（1158）

## 职责

环境变量唯一的配置入口：`@dataclass(frozen=True, slots=True) Settings` 在**导入期**读取全部 `OC_SLIMAPI_*` 环境变量并实例化为模块级单例 `settings`（:1158）；`Settings.validate()`（:736-1155）在 app lifespan 启动期做 fail-closed 校验（app.py:196 与 :771 两处调用）。唯一非 env 配置例外是只读 actions manifest 文件（`OC_SLIMAPI_ACTIONS_FILE`，模块 docstring :1）。文件还承载三类非 env 内容：

1. token-stream 代码级预算常量（`TOKEN_*`，:46-101，非生产 ops 旋钮，仅 DEBUG env 可越权覆盖其中 3 个）；
2. 校验上限常量（`_MAX_*` / `_MIN_EXPAND_RESPONSE_BYTES`，:106-128）+ 导入期 assert 不变量（:142-163）；
3. directory-allowlist 的 canonical 匹配函数族（`allowlist_roots` / `candidate_canonical` / `match_allowlist` / `directory_allowed`，:255-351，被 read_groups.py 403 门与 global_hub.py SSE 帧过滤共用）。

## 对外符号

### 模块级常量（代码级，非 env 可直接改）

| 常量 | 值 | 行号 | 说明 |
|---|---|---|---|
| `TOKEN_PART_MAX_BYTES` | 1 MiB | :46 | 单 part 累积上限 |
| `TOKEN_LIVE_PARTS_MAX` | 32 | :47 | 全局活跃 LivePart 数上限（C5） |
| `TOKEN_LIVEPARTS_MAX_BYTES` | 4 MiB | :48 | 全局 LivePart 字节上限（Stage E 拆分） |
| `TOKEN_PENDING_MAX_BYTES` | 4 MiB | :49 | 全局未 flush 字节上限 |
| `TOKEN_FLUSH_SECONDS` | 0.1 | :50 | 100 ms flush 窗口 |
| `TOKEN_FLUSH_BYTES` | 4096 | :51 | 4 KiB 提前 flush 阈值 |
| `TOKEN_ACC_IDLE_MS` | 60_000 | :52 | 孤儿 LivePart 60 s idle TTL |
| `TOKEN_HEARTBEAT_SECONDS` | 15 | :53 | SSE keepalive |
| `TOKEN_DISABLED_MAX` | 4096 | :59 | tombstone 有界 map 容量（同 revision cap） |
| `TOKEN_DISABLED_TTL_S` / `_MS` | 300 / 300000 | :60-61 | tombstone TTL |
| `TOKEN_RESYNC_QUEUE_CAP` | 64 | :67 | resink 队列上限 |
| `TOKEN_REMOVED_MESSAGES_MAX` | 1000 | :75 | removed 消息 replay FIFO 上限 |
| `TOKEN_REMOVED_MESSAGES_TTL_MS` | 24 h | :76 | removed TTL |
| `DEFAULT_TOKEN_MAX_FRAME_BYTES` | 1 MiB | :81 | hub 未接线时的缺省帧上限 |
| `TOKEN_HANDSHAKE_ITEMS` | 2048 | :100 | 握手 deque item 上限（assert 锁死下界） |
| `TOKEN_HANDSHAKE_BUFFER_BYTES` | 8 MiB | :101 | 握手 deque 字节上限 |
| `_MAX_MESSAGE_BYTES_CAP` / `_MAX_RESPONSE_BYTES_CAP` | 256 MiB | :106-107 | P1-35 sanity 上限 |
| `_MAX_TRANSFORM_TOTAL_BYTES` | 512 MiB | :112 | P1-30 RSS 上界 |
| `_MAX_RAW_PLUS_TRANSFORM_TOTAL_BYTES` | 576 MiB | :116-118 | raw-fetch + transform 聚合内存界 |
| `_MIN/_MAX_EXPAND_RESPONSE_BYTES` | 1 KiB / 32 MiB | :127-128 | expand 片段窗口 |
| `_ACCESS_LOG_DIR_DEFAULT` / `_ACCESS_LOG_PATH_DEFAULT` | "logs" / "logs/access.jsonl" | :134-135 | 区分"未设"与"显式设为默认值" |
| `_ALLOWLIST_ROOTS_CACHE` | `dict` | :252 | 模块级可变缓存（见状态节） |

导入期 assert（:142-146 `TOKEN_LIVE_PARTS_MAX <= TOKEN_DISABLED_MAX`；:153-160 `TOKEN_HANDSHAKE_ITEMS >= TOKEN_REMOVED_MESSAGES_MAX + 1 + TOKEN_LIVE_PARTS_MAX`；:161-163 握手字节 > 0）：改代码级常量破坏不变量 → **导入即 AssertionError**（env 不可触发）。

### 模块级函数

| 函数 | 行号 | 说明 |
|---|---|---|
| `_version_range(value)` | :166-171 | 解析 `"min,max"`；畸形 → `RuntimeError("OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS must be min,max")`（导入期） |
| `_opt_int_env(name)` | :174-180 | 可选 int env：未设/空白 → None；非整数 → **裸 ValueError**（导入期，不点名变量） |
| `_int_env(name, default)` | :183-197 | int env：未设 → default；畸形 → `RuntimeError(f"{name} must be an integer")`（**具名**，仅 3 个字段用） |
| `_directory_allowlist_env()` | :200-213 | 三态解析：env 未设 → None；`""` → `[]`；否则按 `:` 拆分，空白段保留为 `""`（留给 validate 拒绝），条目 `normpath(normalize_directory(...))` |
| `clear_allowlist_roots_cache()` | :255-265 | 配置（重）应用信号：清 `_ALLOWLIST_ROOTS_CACHE`；由 `Settings.validate()`（:757）与 `GlobalHub.set_directory_allowlist()` 调用 |
| `allowlist_roots(allowlist)` | :268-291 | 根 canonical 化（realpath），**按值缓存**；解析失败（OSError/ValueError）的条目跳过（fail-closed：不可解析即不授权） |
| `candidate_canonical(directory)` | :294-318 | 候选**实时** canonical 化（绝不缓存）；非 str/空/相对路径/解析失败 → None（调用方 fail-closed） |
| `match_allowlist(roots, canonical)` | :321-338 | 边界对齐前缀匹配（`canonical == root` / `root == "/"` / `canonical.startswith(root + "/")`）；POSIX 字节大小写敏感 |
| `directory_allowed(allowlist, directory)` | :341-351 | 便捷链：cached roots vs realtime candidate；三态门控留给调用方 |

### Settings 类 — 字段全清单（71 个）

> 行为分类缩写：**导入崩溃** = `settings = Settings()`（:1158）在 import 期解析 env，畸形数值 → 裸 `int()/float()` ValueError（**消息不含 env 名**）；**启动拒绝** = lifespan `validate()` 抛 `RuntimeError`（fail-closed）；**静默 False** = 布尔旋钮任意非真值字符串静默解释为关闭（feature 默认 true，垃圾值 → 功能关闭，无告警）；**无校验**。

| # | 字段 | env | 默认值 | 校验规则（validate 行号） | 非法值行为 | 定义行 |
|---|---|---|---|---|---|---|
| 1 | `host` | `OC_SLIMAPI_HOST` | `"127.0.0.1"` | ∈ {127.0.0.1, ::1, localhost, 0.0.0.0}（:773） | 启动拒绝（"must be loopback or 0.0.0.0"） | :356 |
| 2 | `port` | `OC_SLIMAPI_PORT` | `4097` | 1 ≤ port ≤ 65535，0 不支持（:786） | 数值畸形→导入崩溃；越界→启动拒绝 | :357 |
| 3 | `upstream` | `OC_SLIMAPI_UPSTREAM` | `http://127.0.0.1:4096`（rstrip "/"） | scheme==http 且 hostname∈loopback（:779）；无 user/pass/query/fragment（:781） | 启动拒绝（"must be fixed loopback HTTP" / "must not contain credentials..."） | :358 |
| 4 | `max_message_bytes` | `OC_SLIMAPI_MAX_MESSAGE_BYTES` | 32 MiB | 仅上限 ≤ 256 MiB（:845）；**无 >0 下界**（见疑问 Q3） | 畸形→导入崩溃；>256 MiB→启动拒绝；0/负数→**通过** | :359 |
| 5 | `max_transforms` | `OC_SLIMAPI_MAX_TRANSFORMS` | `1` | ≥1（:837）；×max(resp,expand) ≤ 512 MiB（:878-892） | 启动拒绝 | :363 |
| 6 | `transform_wait_seconds` | `OC_SLIMAPI_TRANSFORM_WAIT_SECONDS` | `2`（float） | >0（:839）；**无 isfinite**（见 Q4） | 畸形→导入崩溃；≤0→启动拒绝；nan→**通过** | :364 |
| 7 | `max_response_bytes` | `OC_SLIMAPI_MAX_RESPONSE_BYTES` | 64 MiB | >0（:841）且 ≤256 MiB（:850） | 启动拒绝 | :365 |
| 8 | `max_expand_response_bytes` | `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` | 8 MiB | ∈[1 KiB, 32 MiB]（:860） | 畸形→**具名** RuntimeError（_int_env :197）；越界→启动拒绝 | :372-374 |
| 9 | `catalog_cache_ttl_seconds` | `OC_SLIMAPI_CATALOG_CACHE_TTL_SECONDS` | `300`（float） | ≥0（0=禁用缓存，:930）；无 isfinite | 畸形→导入崩溃；负→启动拒绝；nan→**通过** | :380-382 |
| 10 | `catalog_cache_max_entries` | `OC_SLIMAPI_CATALOG_CACHE_MAX_ENTRIES` | `16` | ≥1（:935） | 启动拒绝 | :383-385 |
| 11 | `catalog_cache_max_bytes` | `OC_SLIMAPI_CATALOG_CACHE_MAX_BYTES` | 16 MiB | ≥1 MiB（:937，**下界高**） | 启动拒绝 | :386-388 |
| 12 | `catalog_cache_max_entry_bytes` | `OC_SLIMAPI_CATALOG_CACHE_MAX_ENTRY_BYTES` | 1 MiB | ≥1（:942）且 ≤ max_bytes（:948） | 启动拒绝 | :389-391 |
| 13 | `coalesce_enabled` | `OC_SLIMAPI_COALESCE_ENABLED` | `true` | 无 | 静默 False（绕过合并注册表） | :400-402 |
| 14 | `raw_fetch_concurrency` | `OC_SLIMAPI_RAW_FETCH_CONCURRENCY` | `4` | ≥1（:907） | 启动拒绝 | :403-405 |
| 15 | `raw_fetch_max_bytes` | `OC_SLIMAPI_RAW_FETCH_MAX_BYTES` | 64 MiB | >0（:909）；raw+transform ≤576 MiB 聚合（:911-923） | 启动拒绝 | :406-408 |
| 16 | `etag_enabled` | `OC_SLIMAPI_ETAG_ENABLED` | `true` | 无 | 静默 False（ETag/304 全关，字节回退） | :414-416 |
| 17 | `message_fingerprint_enabled` | `OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED` | `true` | 无 | 静默 False（`contentFingerprint` 字段全省略） | :425-427 |
| 18 | `smoke_session_id` | `OC_SLIMAPI_SMOKE_SESSION_ID` | None | 无校验 | 任意字符串照单全收 | :428 |
| 19 | `server_api_version` | ~~`OC_SLIMAPI_SERVER_API_VERSION`~~（**已废弃**） | 常量 `SERVER_API_VERSION`=4（钉死，非 env） | ≥1（:806）且 ∈accepted 区间（:827）——常量下均为不变式 | env 存在→**warning+忽略**（:796-804），启动不破 | :436 |
| 20 | `accepted_client_versions` | `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` | `"3,4"`（来自 ACCEPTED_CLIENT_VERSIONS） | 语法解析导入期（:170 RuntimeError）；**必须严格等于 (3,4)**（:817-822，P1-13 fail-closed 钉死，不可加宽/收窄） | 畸形→导入崩溃（具名）；≠(3,4)→启动拒绝 | :437-442 |
| 21 | `max_subscribers_per_directory` | `OC_SLIMAPI_MAX_SUBSCRIBERS_PER_DIRECTORY` | `8` | ≥1（:978） | 启动拒绝 | :447-449 |
| 22 | `max_total_subscribers` | `OC_SLIMAPI_MAX_TOTAL_SUBSCRIBERS` | `16` | ≥ per_directory（:980） | 启动拒绝 | :450 |
| 23 | `sse_queue_items` | `OC_SLIMAPI_SSE_QUEUE_ITEMS` | `256` | ≥2（:984，溢出终态路径需容纳 resync+STOP 两帧） | 启动拒绝 | :451 |
| 24 | `sse_buffer_bytes` | `OC_SLIMAPI_SSE_BUFFER_BYTES` | 2 MiB | >0（:995） | 启动拒绝 | :452 |
| 25 | `sse_max_frame_bytes` | `OC_SLIMAPI_SSE_MAX_FRAME_BYTES` | 256 KiB | >0（:997） | 启动拒绝 | :453 |
| 26 | `token_stream_max_subscribers` | `OC_SLIMAPI_TOKEN_STREAM_MAX_SUBSCRIBERS` | `8` | ≥1（:1004） | 启动拒绝 | :466-468 |
| 27 | `token_stream_queue_items` | `OC_SLIMAPI_TOKEN_STREAM_QUEUE_ITEMS` | `64` | ≥2（:1006） | 启动拒绝 | :469 |
| 28 | `token_stream_buffer_bytes` | `OC_SLIMAPI_TOKEN_STREAM_BUFFER_BYTES` | 512 KiB | >0（:1013） | 启动拒绝 | :470-472 |
| 29 | `token_stream_max_frame_bytes` | `OC_SLIMAPI_TOKEN_STREAM_MAX_FRAME_BYTES` | 1 MiB | >0（:1015） | 启动拒绝 | :473-475 |
| 30 | `token_stream_debug_live_budget_bytes` | `OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_BUDGET_BYTES` | None | 设了须 >0（:1019）；DEBUG 专用 | 畸形→**裸** ValueError（导入）；≤0→启动拒绝 | :482-484 |
| 31 | `token_stream_debug_part_max_bytes` | `OC_SLIMAPI_TOKEN_STREAM_DEBUG_PART_MAX_BYTES` | None | 设了须 >0（:1023） | 同上 | :485-487 |
| 32 | `token_stream_debug_live_parts_max` | `OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_PARTS_MAX` | None | 设了须 >0 且 ≤ TOKEN_DISABLED_MAX=4096（:1027-1041，防 revision 回退） | 同上 + 越上界启动拒绝 | :488-490 |
| 33 | `shell_deny_list_enabled` | `OC_SLIMAPI_SHELL_DENY_LIST_ENABLED` | `1`（true） | 无；ops break-glass，关闭≠安全隔离（:491-494 注释） | 静默 False（deny-list 关闭） | :495-497 |
| 34 | `directory_allowlist` | `OC_SLIMAPI_DIRECTORY_ALLOWLIST` | None（三态） | 条目：非空/绝对/无 `\0`/无控制字符/≤4096（:738-750）；realpath 可解析（:758-766） | None=**不过滤**（默认放行）；`""`→`[]`（/file 路由 reject-all，SSE hub 不过滤——见 Q5）；坏条目→启动拒绝 | :498 |
| 35 | `deployment_revision` | `OC_SLIMAPI_DEPLOYMENT_REVISION` | None | 无（best-effort） | 无 | :501 |
| 36 | `deployment_revision_file` | `OC_SLIMAPI_DEPLOYMENT_REVISION_FILE` | None | 无；读文件失败→warning+None（:700-707） | warning+忽略 | :502 |
| 37 | `actions_file` | `OC_SLIMAPI_ACTIONS_FILE` | None（空串→None） | 无（actions.py 自行处理） | 无 | :507 |
| 38 | `actions_max_concurrent` | `OC_SLIMAPI_ACTIONS_MAX_CONCURRENT` | `4` | ≥1（:1044） | 启动拒绝 | :509 |
| 39 | `traffic_metrics_enabled` | `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | `true` | 无 | 静默 False | :516-518 |
| 40 | `access_log_enabled` | `OC_SLIMAPI_ACCESS_LOG_ENABLED` | `true` | 无 | 静默 False | :519-521 |
| 41 | `access_log_path` | `OC_SLIMAPI_ACCESS_LOG_PATH`（**DEPRECATED**） | `"logs/access.jsonl"` | 无直接校验；仅当 `OC_SLIMAPI_ACCESS_LOG_DIR` 未设且值≠默认时，其 parent 作为回退目录（:711-734）+ app.py:210-215 warning | warning+回退（不破启动） | :527 |
| 42 | `access_log_dir` | `OC_SLIMAPI_ACCESS_LOG_DIR` | `"logs"` | 显式设置永远压过废弃 path（:727-734） | 无 | :533 |
| 43 | `access_log_compress_on_startup` | `OC_SLIMAPI_ACCESS_LOG_COMPRESS_ON_STARTUP` | `true` | 无 | 静默 False | :534-536 |
| 44 | `access_log_retain_days` | `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS` | `0`（=不清理） | ≥0（:1054） | 启动拒绝 | :537 |
| 45 | `access_log_maintenance_interval_s` | `OC_SLIMAPI_ACCESS_LOG_MAINTENANCE_INTERVAL_S` | `3600` | ≥60（:1050，防热循环） | 启动拒绝 | :538-540 |
| 46 | `traffic_snapshot_enabled` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_ENABLED` | `true` | 无 | 静默 False | :546-548 |
| 47 | `traffic_snapshot_interval_s` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_INTERVAL_S` | `300` | ≥1（:1056） | 启动拒绝 | :549-551 |
| 48 | `traffic_snapshot_path` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` | `"logs/traffic-snapshot.jsonl"` | 无 | 无 | :552-554 |
| 49 | `traffic_snapshot_retain_days` | `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS` | `0`（=不清理；生产 systemd 设 30） | ≥0（:1059） | 启动拒绝 | :561-563 |
| 50 | `client_id_hash` | `OC_SLIMAPI_CLIENT_ID_HASH` | `true`（fail-closed 哈希） | 无 | 静默 False（明文记 device id——隐私回退） | :571-573 |
| 51 | `client_id_salt` | `OC_SLIMAPI_CLIENT_ID_SALT` | None | 无（有则 HMAC-SHA256，无则裸 SHA-256） | 无 | :574 |
| 52 | `skeleton_inline_output_max_bytes` | `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES` | 4 KiB | >0（:955）且 ≤16 MiB（:966） | 启动拒绝 | :579-581 |
| 53 | `skeleton_inline_output_max_message_bytes` | `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES` | 16 KiB | >0（:959）且 ≤16 MiB（:970） | 启动拒绝 | :582-584 |
| 54 | `questions_max_response_bytes` | `OC_SLIMAPI_QUESTIONS_MAX_RESPONSE_BYTES` | 2 MiB | >0（:1068） | 启动拒绝 | :590-592 |
| 55 | `questions_max_aggregate_bytes` | `OC_SLIMAPI_QUESTIONS_MAX_AGGREGATE_BYTES` | 16 MiB | ≥ per-dir（:1072）且 ≤128 MiB（:1077） | 启动拒绝 | :593-595 |
| 56 | `questions_fanout_concurrency` | `OC_SLIMAPI_QUESTIONS_FANOUT_CONCURRENCY` | `8` | ∈[1,16]（:1064） | 启动拒绝 | :596-598 |
| 57 | `permissions_max_response_bytes` | `OC_SLIMAPI_PERMISSIONS_MAX_RESPONSE_BYTES` | 2 MiB | >0（:1090） | 启动拒绝 | :611-613 |
| 58 | `permissions_fanout` | `OC_SLIMAPI_PERMISSIONS_FANOUT` | `8` | ∈[1,16]（:1086） | 启动拒绝 | :614-616 |
| 59 | `permissions_max_aggregate_bytes` | `OC_SLIMAPI_PERMISSIONS_MAX_AGGREGATE_BYTES` | 16 MiB | ≥ per-dir（:1094）且 ≤128 MiB（:1099） | 启动拒绝 | :617-619 |
| 60 | `qp_sweep_enabled` | `OC_SLIMAPI_QP_SWEEP_ENABLED` | `true` | 无 | 静默 False | :622-624 |
| 61 | `qp_sweep_interval_seconds` | `OC_SLIMAPI_QP_SWEEP_INTERVAL_SECONDS` | `1800.0`（float） | >0（:1103）；**无 isfinite** | 畸形→导入崩溃；≤0→启动拒绝；nan→**通过** | :625-627 |
| 62 | `qp_sweep_daily_budget` | `OC_SLIMAPI_QP_SWEEP_DAILY_BUDGET` | `100` | ≥0（:1130） | 启动拒绝 | :628-630 |
| 63 | `merged_fanout` | `OC_SLIMAPI_MERGED_FANOUT` | `8` | ∈[1,16]（:1132） | 启动拒绝 | :631-633 |
| 64 | `merged_max_fulls_per_page` | `OC_SLIMAPI_MERGED_MAX_FULLS_PER_PAGE` | `16` | ∈[1,64]（:1136） | 启动拒绝 | :634-636 |
| 65 | `merged_max_bytes` | `OC_SLIMAPI_MERGED_MAX_BYTES` | 8 MiB | >0（:1140）且 ≤128 MiB（:1144） | 启动拒绝 | :637-639 |
| 66 | `transform_absorb_budget_seconds` | `OC_SLIMAPI_TRANSFORM_ABSORB_BUDGET_SECONDS` | `2.5`（float） | >0（:1148）；**无 isfinite** | nan→**通过** | :640-642 |
| 67 | `state_dir` | `OC_SLIMAPI_STATE_DIR` | `"state"`（相对路径） | 非空/非纯空白（:1154） | 启动拒绝 | :648 |
| 68 | `dbaux_probe_interval_s` | `OC_SLIMAPI_DBAUX_PROBE_INTERVAL_S` | `30`（float） | >0（:1109）；**无 isfinite** | nan→**通过** | :658-660 |
| 69 | `replay_max_count` | `OC_SLIMAPI_REPLAY_COUNT`（env 名无 MAX_ 前缀） | `2048` | ≥1（:1117） | 畸形→**具名** RuntimeError；越界→启动拒绝 | :669 |
| 70 | `replay_max_bytes_kb` | `OC_SLIMAPI_REPLAY_BYTES_KB`（**单位 KiB**） | `65536`（=64 MiB） | ≥1（:1119）；×1024 换算在 app.py:429 | 畸形→具名 RuntimeError；<1→启动拒绝 | :670 |
| 71 | `replay_ttl_s` | `OC_SLIMAPI_REPLAY_TTL_S` | `900`（float，秒） | `math.isfinite` 且 >0（:1121，**唯一**带 nan/inf 防护的 float 旋钮） | 畸形→导入崩溃；nan/inf/≤0→启动拒绝（消息含 got {value!r}） | :671 |

### Settings 方法

| 方法 | 行号 | 说明 |
|---|---|---|
| `read_deployment_revision()` | :673-709 | env 优先（strip 后空白视为空，P1-40）；否则文件（`CREDENTIALS_DIRECTORY` 回退 :691-692）；未设/NotFound→静默 None；Permission/Unicode/OSError→warning+None（lazy import logging_config :703） |
| `effective_access_log_dir()` | :711-734 | 废弃优先级：仅当 `OC_SLIMAPI_ACCESS_LOG_DIR` **未出现在 os.environ** 且 `access_log_path != 默认` 时用废弃 path 的 parent；返回 `(dir, deprecated_used)` |
| `validate()` | :736-1155 | 全部 fail-closed 校验（见错误路径节）；:757 顺带 `clear_allowlist_roots_cache()` |

### 模块级单例

- `settings = Settings()`（:1158）——**导入期**求值：所有 env 读取/解析都发生在 import 时；此后进程内不再读 env（例外：`read_deployment_revision` 的 CREDENTIALS_DIRECTORY、`effective_access_log_dir` 的 `in os.environ` 探测在调用期）。

## 依赖（内部 imports）

- `.directory.normalize_directory`（:12；实现 directory.py:12-20，纯 rstrip("/")，根 "/" 保留）
- `.versioning.ACCEPTED_CLIENT_VERSIONS, SERVER_API_VERSION`（:13；versioning.py:38 `SERVER_API_VERSION = 4`，:44 `ACCEPTED_CLIENT_VERSIONS = (3, 4)`）
- lazy：`.logging_config.get_logger`（:703、:797——仅 warning 路径，避免导入环）
- 标准库：dataclasses（dataclass/field）、math（isfinite）、os、pathlib.Path、typing.Any、urllib.parse.urlsplit

## 被依赖（主要使用方）

| 使用方 | 用法 |
|---|---|
| `src/oc_slimapi/app.py:21` | `from .config import settings`；lifespan 两处 `settings.validate()`（:196、:771）；`app.state.config = settings`（:197）后几乎所有子系统 wiring：transform pool（:321-323）、catalog cache（:362-365）、coalesce（:384-387）、GlobalHub SSE 限额（:479-483）+ allowlist（:492）、token hub（:539-566）、`apply_debug_budget_overrides(settings)`（:307）、TurnRegistry state_dir（:583）、dbaux probe（:601）、ReplayLog（:428-430，`max_bytes=settings.replay_max_bytes_kb * 1024` 的 KiB→B 换算在此）、snapshot（:668-720）、smoke（:138） |
| `src/oc_slimapi/routes/read_groups.py:51,140-141` | `allowlist_roots/candidate_canonical/match_allowlist` —— /file 族 403 `directory_not_allowed` 门；None→原样透传，[]→`allowlist_roots([])=()` → 恒 False → reject-all |
| `src/oc_slimapi/sse/global_hub.py:22-23,534,572-585` | `clear_allowlist_roots_cache` / `directory_allowed` —— SSE 帧过滤；**`if not allowlist: return True`（:574）把 None 和 [] 都放行** |
| `src/oc_slimapi/upstream.py:8,40` | `Settings` 作类型标注（`create_client(config: Settings)`） |
| `src/oc_slimapi/routes/versions.py:130,148` | `settings.max_expand_response_bytes` → `fragmentMaxBytes` |
| `src/oc_slimapi/routes/health.py:40-50,72,87,133-139` | `accepted_client_versions`（clientMin/clientMax）、skeleton inline caps、deployment_revision |
| `src/oc_slimapi/actions.py:424-433` | `settings.actions_file`（None=功能关闭）、`settings.actions_max_concurrent` |
| `src/oc_slimapi/etag.py:45-46,73-96`、`providers_projection.py:121` | skeleton caps 进 ETag REP_VERSION；`etag_enabled`/`message_fingerprint_enabled` 开关（getattr 带默认 True） |
| `src/oc_slimapi/routes/messages.py:545-546,846-1083` | `max_message_bytes`（/full 413 界）、skeleton inline caps、fingerprint 开关 |
| `src/oc_slimapi/middleware/traffic_accounting.py:311-313` | `client_id_hash`（getattr 默认 True）、`client_id_salt` |
| `src/oc_slimapi/sse/tokenstream/hub.py:126` | `apply_debug_budget_overrides` 消费 3 个 DEBUG 字段，**运行时改写 hub 模块全局** TOKEN_LIVEPARTS_MAX_BYTES / TOKEN_PART_MAX_BYTES / TOKEN_LIVE_PARTS_MAX |
| tests | `tests/test_config.py` 专测 + 约 80 个测试文件 import config/settings/Settings |

## 状态 / 可变性

- `Settings` 为 `frozen + slots` dataclass：实例不可重绑字段；但 `directory_allowlist: list[str]` 是**可变 list 挂在 frozen 实例上**（:498）——冻结只防 `settings.directory_allowlist = x`，不防 list 原地变异（仓内无变异点，纯防御性事实）。
- **env 快照时点 = import 时**（:1158）。uvicorn reload / 测试 monkeypatch env 后必须重新 import 才生效；`validate()` 不再读数值 env（除 :727 的存在性探测与 :796 的废弃探测）。
- 模块级可变状态两处：`_ALLOWLIST_ROOTS_CACHE`（:252，按 allowlist 值缓存 canonical roots，`clear_allowlist_roots_cache` 清空，无淘汰——键空间=进程内出现过的 distinct allowlist 值）；hub.py 的 `TOKEN_*` 全局被 `apply_debug_budget_overrides` 就地改写（跨模块可变状态，仅 DEBUG env 设置时发生）。
- allowlist roots 的 ops 语义（:234-241）：root 自身被 symlink 重定向后**不影响**已缓存判定，直至配置重应用或进程重启——有意的运维语义。
- 导入期 assert（:142-163）意味着常量误编辑会让任何 import 该模块的进程（含 pytest 收集）直接崩。

## 错误路径（全部抛点）

### 导入期（`settings = Settings()` :1158 或模块 assert）

| 行号 | 异常 | 触发 / 消息关键词 |
|---|---|---|
| :142, :153, :161 | AssertionError | 代码级常量不变量被破坏（env 不可达） |
| :170 | RuntimeError | `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS must be min,max`（畸形 min,max） |
| :180 | ValueError（**裸**） | 3 个 DEBUG 字段非整数（`int()` 直抛，不点名） |
| :197 | RuntimeError（具名） | `{name} must be an integer` —— 仅 `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` / `OC_SLIMAPI_REPLAY_COUNT` / `OC_SLIMAPI_REPLAY_BYTES_KB` |
| :357, :359, :363-408, :447-539... | ValueError（**裸**） | 其余全部 `int(os.getenv(...))` / `float(...)` 字段：畸形数值 → 裸异常，**消息不含 env 名**（_int_env docstring :184-189 自认此诊断缺口） |

### lifespan `validate()`（全部 RuntimeError，fail-closed；行号=raise 处）

| 行号 | 消息关键词（env 名） |
|---|---|
| :747 | DIRECTORY_ALLOWLIST entries must be non-empty absolute...（坏条目） |
| :762 | DIRECTORY_ALLOWLIST ... canonically resolvable（realpath 失败） |
| :774 | HOST must be loopback or 0.0.0.0 |
| :779 | UPSTREAM must be fixed loopback HTTP |
| :781 | UPSTREAM must not contain credentials/query/fragment |
| :786 | PORT must be in [1, 65535] |
| :799 | （**warning 非 error**）SERVER_API_VERSION is deprecated and ignored |
| :806 | slimapi version configuration is invalid |
| :817 | ACCEPTED_CLIENT_VERSIONS must be (3, 4) — fail-closed to the pinned range |
| :827 | SERVER_API_VERSION ... must be within ... range（常量钉死后实际不可达，见 Q2） |
| :837/:839/:841 | MAX_TRANSFORMS >= 1 / TRANSFORM_WAIT_SECONDS > 0 / MAX_RESPONSE_BYTES > 0 |
| :845/:850 | MAX_MESSAGE_BYTES / MAX_RESPONSE_BYTES <= 256 MiB |
| :860 | MAX_EXPAND_RESPONSE_BYTES in [1 KiB, 32 MiB] |
| :882 | MAX_TRANSFORMS × max(...) exceeds 512 MiB — risk of OOM |
| :907/:909/:912 | RAW_FETCH_CONCURRENCY >= 1 / RAW_FETCH_MAX_BYTES > 0 / raw+transform exceeds 576 MiB |
| :930-:953 | CATALOG_CACHE_TTL_SECONDS >= 0 / MAX_ENTRIES >= 1 / MAX_BYTES >= 1 MiB / MAX_ENTRY_BYTES >= 1 且 <= MAX_BYTES |
| :955-:973 | SKELETON_INLINE_OUTPUT_MAX_BYTES(_MESSAGE_BYTES) > 0 且 <= 16 MiB |
| :978-:998 | MAX_SUBSCRIBERS_PER_DIRECTORY >= 1 / MAX_TOTAL >= PER_DIRECTORY / SSE_QUEUE_ITEMS >= 2 / SSE_BUFFER_BYTES > 0 / SSE_MAX_FRAME_BYTES > 0 |
| :1004-:1041 | TOKEN_STREAM_MAX_SUBSCRIBERS >= 1 / QUEUE_ITEMS >= 2 / BUFFER_BYTES > 0 / MAX_FRAME_BYTES > 0 / 3 个 DEBUG 字段 > 0 when set / DEBUG_LIVE_PARTS_MAX <= TOKEN_DISABLED_MAX |
| :1044 | ACTIONS_MAX_CONCURRENT >= 1 |
| :1050-:1060 | ACCESS_LOG_MAINTENANCE_INTERVAL_S >= 60 / ACCESS_LOG_RETAIN_DAYS >= 0 / TRAFFIC_SNAPSHOT_INTERVAL_S >= 1 / TRAFFIC_SNAPSHOT_RETAIN_DAYS >= 0 |
| :1064-:1080 | QUESTIONS_FANOUT_CONCURRENCY in [1,16] / QUESTIONS_MAX_RESPONSE_BYTES > 0 / QUESTIONS_MAX_AGGREGATE >= per-dir 且 <= 128 MiB |
| :1086-:1106 | PERMISSIONS_FANOUT in [1,16] / PERMISSIONS_MAX_RESPONSE_BYTES > 0 / PERMISSIONS_MAX_AGGREGATE 同型 / QP_SWEEP_INTERVAL_SECONDS > 0 |
| :1109 | DBAUX_PROBE_INTERVAL_S > 0 |
| :1117-:1129 | REPLAY_COUNT >= 1 / REPLAY_BYTES_KB >= 1 / REPLAY_TTL_S must be a finite number > 0 (got ...) |
| :1130-:1151 | QP_SWEEP_DAILY_BUDGET >= 0 / MERGED_FANOUT in [1,16] / MERGED_MAX_FULLS_PER_PAGE in [1,64] / MERGED_MAX_BYTES > 0 且 <= 128 MiB / TRANSFORM_ABSORB_BUDGET_SECONDS > 0 |
| :1154 | STATE_DIR must be non-empty |

非抛点降级：`read_deployment_revision` 读文件失败 → warning + None（:700-707）；`OC_SLIMAPI_SERVER_API_VERSION` 存在 → warning + 忽略（:796-804）；`OC_SLIMAPI_ACCESS_LOG_PATH` 废弃回退 → app.py:210-215 warning。

## 疑问点（可疑处，含行号）

1. **Q1 — AGENTS.md 与 versioning.py 钉值漂移**：仓库 AGENTS.md 称 `ACCEPTED_CLIENT_VERSIONS` 当前 `[3,3]`，实际 versioning.py:44 为 `(3, 4)`（4.0.0 双版本窗口）。config.py:817 的钉死校验用 `!= ACCEPTED_CLIENT_VERSIONS` 常量比较，逻辑正确；漂移在文档侧（AGENTS.md 未随 4.0.0 更新）。
2. **Q2 — OC_SLIMAPI_SERVER_API_VERSION 废弃语义**：:796-804 确为 **warning+忽略**（env 出现即告警，值不解读，启动永不破——与 docstring "settable without breaking startup" 一致）。但 :827-832 的区间校验错误消息仍点名 `OC_SLIMAPI_SERVER_API_VERSION`，而 `server_api_version` 已常量钉死为 4（:436），该分支在生产 env 下**不可达**（除非改 versioning.py 常量），错误消息具误导性（死错误路径 + 张冠李戴的 env 名）。
3. **Q3 — max_message_bytes 无下界**：:845 只查 `> 256 MiB` 上限，不查 `> 0`（对比 max_response_bytes :841 有 `<= 0` 检查）。`OC_SLIMAPI_MAX_MESSAGE_BYTES=0` 或负数可通过 validate；消费方 messages.py:545 `min(cap, config.max_message_bytes)` 会把每次 /full 上限压成 0/负 → 行为未定义（413 或 0 字节读）。疑似遗漏。
4. **Q4 — float 旋钮的 nan/inf 旁路（rev-gate MAJOR-1 只修了一处）**：:1121-1129 仅 `replay_ttl_s` 有 `math.isfinite` 防护（注释明说 nan 使 `age > ttl_s` 恒 False、TTL 驱逐静默失效）。但同型 `<= 0` 检查的 float 旋钮——`transform_wait_seconds`（:839）、`catalog_cache_ttl_seconds`（:930，nan 还会绕过 `< 0`）、`qp_sweep_interval_seconds`（:1103）、`dbaux_probe_interval_s`（:1109）、`transform_absorb_budget_seconds`（:1148）——`float("nan") <= 0` 均为 False，`OC_SLIMAPI_...=nan` 全部**通过校验**；inf 同理。是否算未修完的同族缺口值得后续核对（`float("inf")` 的 env 字符串 "inf"/"nan" 可被 float() 接受）。
5. **Q5 — directory allowlist 三态不对称**：默认 None = **完全放行**（不过滤，read_groups.py:139 原样返回、global_hub.py:574 放行）；`OC_SLIMAPI_DIRECTORY_ALLOWLIST=""` → `[]` 在 /file 路由 = reject-all（roots 为空 tuple 恒不匹配），但在 SSE hub（global_hub.py:574 `if not allowlist: return True`）= **不过滤**。同一 `[]` 值在两个执行点语义相反；config.py:346-349 docstring 明示这是有意设计，但运维设空串期望"全拒"时 SSE 帧仍外发——审计时建议按安全语义复核。另：`_directory_allowlist_env`（:209-211）把空白段保留为 `""` 条目交给 validate 拒绝（fail-closed，消息是通用的 entries 报错，不指明哪个段）。
6. **Q6 — 导入期裸 ValueError 诊断缺口**：:357-:671 间约 40 个字段用裸 `int(os.getenv(...))` / `float(...)`，畸形值在 **import 期**抛不含 env 名的 ValueError；_int_env（:183-197）的具名模式只覆盖 3 个字段（docstring 自认 "a bare ValueError at import time would not name the offending variable"）。排障时须靠 traceback 定位字段行。
7. **Q7 — REPLAY 三参数命名/单位陷阱**：env 名 `OC_SLIMAPI_REPLAY_COUNT` / `OC_SLIMAPI_REPLAY_BYTES_KB` / `OC_SLIMAPI_REPLAY_TTL_S`（:669-671）与字段名 `replay_max_count` / `replay_max_bytes_kb` / `replay_ttl_s` 不同形（env 无 MAX_ 前缀）；`replay_max_bytes_kb` 单位是 **KiB**，×1024 换算发生在 app.py:429 而非 config——运维直接写字节数会放大 1024 倍（仅受 :1119 `>= 1` 下界与上游 ReplayLog 自身约束，config 侧无字节上限校验，与 max_response_bytes 系列的 256 MiB sanity cap 风格不一致）。
8. **Q8 — 布尔旋钮静默 False**：13 个布尔字段（:400,414,425,495,516,519,534,546,571,622 等）接受任意字符串，非 {1,true,yes,on} 一律 False 且无告警——typo（如 "ture"）会**静默关闭** ETag、fingerprint、traffic metrics、client_id_hash（隐私回退：明文记录 device id）等默认开启的能力，无任何日志。
9. **Q9 — 冻结实例上的可变 list**：`directory_allowlist: list[str]`（:498）在 frozen dataclass 内是可变对象；仓内无变异点，但 `app.state.config` 全局共享该 list，任何下游原地 append 都会无声改变授权面（只读审计未发现实际变异者）。
10. **Q10 — `effective_access_log_dir` 依赖 env 存在性而非字段值**（:727）：判定"新 dir 是否显式设置"用 `"OC_SLIMAPI_ACCESS_LOG_DIR" in os.environ`，而字段值已在 import 时定格——若进程启动后有人 `os.environ` 补设/删除该键再调用此方法，判定与 `settings.access_log_dir` 值可能脱节（生产 lifespan 只调一次 :209，实际风险低）。
11. **Q11 — 废弃字段清单不完整披露**：代码内明确 DEPRECATED 的只有 `access_log_path`（:522-527，未用自轮转改造起）与 `OC_SLIMAPI_SERVER_API_VERSION`（:791-804，S-B04）。`server_api_version` 字段本身（:436）保留但已无 env 输入（仅作 health 展示与常量载体），`smoke_session_id`（:428）等无废弃标记。CHANGELOG 是否同步这两处废弃需在 E2/E3 阶段核对。
12. **Q12 — catalog_cache_max_bytes 下界 1 MiB 偏高**（:937）：想配 512 KiB 小缓存的部署会被启动拒绝（"must be >= 1 MiB"），与同组 `max_entry_bytes >= 1`（:942）的宽松风格不一致；属有意（注释自述）但反直觉。
