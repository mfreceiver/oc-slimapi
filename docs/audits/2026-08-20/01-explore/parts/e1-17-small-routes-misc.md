# E1-17 精读卡片 —— 小路由与杂项模块（十四文件）

> 审计日期 2026-08-20。引用格式 `src/oc_slimapi/...:行号`。全部文件已全文精读（非抽样）；交叉依赖符号经 grep/定点阅读核实。

---

### src/oc_slimapi/routes/directories.py（202 行）

- **职责**：`GET /slimapi/directories` —— 全局工作目录目录（ocdroid 项目切换器）。发现源为上游 `GET /experimental/session?roots=true&archived=true`（跨 workdir 的 GLOBAL 顶层 session 列表），按 `normalize_directory` 聚合为每 workdir 一行。无任何 query 参数；经 `?v=3` selector 准入。
- **对外符号**：
  - `router`（src/oc_slimapi/routes/directories.py:14）— `APIRouter(prefix="/slimapi", tags=["directories"])`。
  - `directories(request)`（:17-108）— 路由 handler：TransformPool admission（`async with request.app.state.transforms as pool`，:77）→ `fetch_global_root_sessions`（:80-82）→ 严格 schema 守卫（:90-95）→ `pool.offload(_aggregate_and_pack, ...)`（:99-102）→ 200 响应 + `Cache-Control: no-store`（:105-108）。
  - `_num(v)`（:111-115）— 数值抽取；非数（含 bool）→ 0（排序垫底）。
  - `_session_rank(s)`（:118-130）— winner 排序键 `(time.updated, time.created, id)`；`id` 缺失/非 str 强制 `""` 防异构比较 TypeError。
  - `_aggregate_and_pack(sessions, discovery_complete, *, accept_encoding)`（:133-202）— worker 侧聚合 + orjson + 可选 gzip（`compress_if_beneficial`）。聚合规则：group key = `normalize_directory(s["directory"])`；`activeRootSessionCount` = `time.archived` 非数值者；`archivedRootSessionCount` = 数值者（排除 bool，:176）；winner = `max` by rank；`title` 非非空 str → None（:184-186）；输出按 `lastUpdated` DESC、`directory` ASC 排序（:198）。
- **依赖 / 被依赖**：依赖 `directory.normalize_directory`、`discovery._DISCOVERY_LIMIT`（=10_000，src/oc_slimapi/discovery.py:52）与 `discovery.fetch_global_root_sessions`、`gzip_util.compress_if_beneficial`、`transform.TransformBusy`、`upstream_errors.raise_upstream_unavailable`、`_catalog_common.busy_response`。被挂载于 `src/oc_slimapi/app.py:760`（router 列表第 14 位）。
- **状态 / 可变性**：模块自身无状态（router + 纯函数）。无缓存、无 ETag。admission 覆盖 fetch→guard→aggregate 全程（docstring :38-41 "review blocker"）。
- **错误路径**：session 非 dict 或 `directory` 非非空 str → `raise_upstream_unavailable()`（:92、:95）→ 503 `upstream_unavailable`（无 envelope）；`TransformBusy` → `busy_response`（:103-104）→ 503 `transform_busy` + `Retry-After: 2`（`_catalog_common.py:44-52`）；发现阶段自身失败在 discovery 内 → 同样 503。
- **疑问点**（7）：
  1. :8 跨模块导入私有下划线符号 `_DISCOVERY_LIMIT`（风格 / 封装边界）。
  2. docstring :34-35 残留历史陈述 "no `X-Slimapi-Version` bump (still 2)"——该头已于 3.0.0 删除、现为 v3-only + v4 双窗，文档滞后于契约（AGENTS.md 明示冲突以契约为准）。
  3. handler 签名（:18）不接受任何 query 参数，客户端多传的 `?directory=` 等被 FastAPI 静默忽略——与 docstring "No query parameters"（:29-31）是"忽略"而非"拒绝"，契约未写明多余参数语义。
  4. winner 的 `time.updated` 缺失/非数值时 `lastUpdated=0`（:183 经 `_num`）——客户端会把该目录排在 1970 位置；0 占位是否为契约冻结值待核（v3-contract 相应条目）。
  5. `archivedOnly`（:194）= `activeRootSessionCount == 0`——若 group 全部 archived，winner 仍可能带有效 title/lastUpdated，字段间一致性依赖上游。
  6. `discoveryComplete` 判定在 discovery 层（`len(sessions) < limit`，discovery.py:177）而非本文件；docstring :64-65 说 "filled exactly at `_DISCOVERY_LIMIT`" 与实现（`<` 才 true）一致，但语义边界（恰好 == limit 时报 false）是保守取舍，审计时注意它与"真实截断"可能不等价。
  7. :198 排序 key `-it["lastUpdated"]`——`lastUpdated` 为 int|float 混合（`_num` 可返回 float），负号取反对 float 无碍，但混排时 0（缺时间占位）与真实 ms 时间戳差距悬殊，目录顺序对缺失时间数据敏感。

---

### src/oc_slimapi/routes/diff.py（110 行）

- **职责**：T18（2026-08-16）read-only diff thin route：`GET /slimapi/sessions/{sid}/diff` → 上游 `GET /session/{sid}/diff[?messageID=…]`。近恒等投影（上游 `Snapshot.FileDiff` 五字段全保留，`patch` 是路由本体）；gzip + cap + admission 是省流杠杆。
- **对外符号**：
  - `router`（:44）— `prefix="/slimapi/sessions/{sid}"`。
  - `_project_diff(items)`（:47-60）— 逐项 dict 守卫后原样返回（near-identity）；非 dict → `ValueError`。
  - `session_diff(request, sid, directory=None, messageID=None)`（:63-110）— 路由 handler：`resolve_route_directory`（:88）→ `validate_directory`（:90）→ `handle_catalog_request(..., enable_etag=False, merge_directory_vary=True, min_gzip_bytes=MIN_GZIP_BYTES, upstream_params={"messageID": …} iff 非None)`（:95-108）。
- **依赖 / 被依赖**：依赖 `_catalog_common.handle_catalog_request/busy_response`、`selector.resolve_route_directory`、`directory.validate_directory`、`gzip_util.MIN_GZIP_BYTES`（=64，src/oc_slimapi/gzip_util.py:25）、`transform.TransformBusy/read_with_cap`。挂载于 app.py:760（以 `diff_routes` 别名，app.py:30）。
- **状态 / 可变性**：无状态——无缓存、`enable_etag=False`（无 ETag / 304，`If-None-Match` 被忽略恒 200）。
- **错误路径**：本文件构造点仅 `TransformBusy` → `busy_response`（:109-110，503 `transform_busy`）。`ValueError`（非 dict item / 非列表 / JSON 解码失败）由共享 handler 映射 503 `upstream_unavailable`（`_catalog_common.py:312-313`）；上游 4xx → 502 `upstream_http_N`、带 `sid` 的 404 → 404 `session_not_found`、超 cap → 413 `response_too_large`（`_catalog_common.py:296-299`）。
- **疑问点**（5）：
  1. `messageID` 空串（`?messageID=`）满足 `is not None`（:91-93）会被原样转发为空值 query——上游按"提供了 messageID"处理 → 200 `[]`；空串语义是否契约冻结待核。
  2. `read_timeout=None`（:102）——无上游读超时；`patch` 本体可能很大，上游慢读时该请求可无限期占住 admission（与 command.py 的 300s 形成对比）。
  3. docstring :8-9、:14-18 锚定 v1.18.16 上游源码路径，而 AGENTS.md 声明当前对齐 v1.18.18——锚点未随 repoint 更新（对照时需重新核验 summary.ts 行号）。
  4. `directory` 经 stash 解析后又 `validate_directory`（:88-90）——注释称幂等纯函数重复校验（selector.py:410-413 确认 stash 已验证），双保险但也是重复。
  5. `upstream_params` 仅 messageID 一个键；其余未知 query 参数不透传（静默丢弃）——与 passthrough 路由行为差异是否已被 ocdroid 文档化。

---

### src/oc_slimapi/routes/todo.py（84 行）

- **职责**：T17 / traffic plan Batch 3（C2a）read-only todo thin route：`GET /slimapi/sessions/{sid}/todo` → 上游 `GET /session/{sid}/todo`。近恒等投影（`Todo.Info` 三字段全保留）。
- **对外符号**：
  - `router`（:29）— `prefix="/slimapi/sessions/{sid}"`。
  - `_project_todo(items)`（:32-44）— 逐项 dict 守卫（rev-6 B2）后原样返回；非 dict → `ValueError`。
  - `session_todo(request, sid, directory=None)`（:47-84）— handler：stash 解析 directory（:66-68）→ `handle_catalog_request(..., enable_etag=False, merge_directory_vary=True, min_gzip_bytes=MIN_GZIP_BYTES)`（:70-82）。
- **依赖 / 被依赖**：同 diff.py（无 `upstream_params`）。挂载于 app.py:760。
- **状态 / 可变性**：无状态（无缓存、无 ETag）。
- **错误路径**：`TransformBusy` → `busy_response`（:83-84）；`ValueError`/JSON 失败 → 503 `upstream_unavailable`；上游 4xx → 502 `upstream_http_N`；带 sid 404 → 404 `session_not_found`；超 cap → 413 `response_too_large`（均在 `_catalog_common`）。
- **疑问点**（4）：
  1. `read_timeout=None`（:77）——同 diff.py 疑问 2，无读超时占 admission。
  2. 设计文档引用 `docs/specs/traffic-route-todo-2026-08-10.md`（:5）——审计下游应确认该文档仍在仓且与实现一致。
  3. `_project_todo` 对 dict item 的内部字段零校验（如 `status` 非 str 原样透传）——近恒等是设计决定，但畸形字段直通客户端。
  4. 空 `[]` 响应走 identity（`min_gzip_bytes=64` gate，:81）——`Vary` 头仍合并 directory（`merge_directory_vary=True`），identity + Vary 组合对中间缓存正确但需契约确认。

---

### src/oc_slimapi/routes/agent.py（52 行）

- **职责**：`GET /slimapi/agent` —— agent 目录骨架投影：白名单 `name/description/mode/hidden/native`（`AGENT_SKELETON_KEYS`，src/oc_slimapi/skeleton.py:790），丢 `prompt`（~34.7%）与 `permission`（~61.2%），实测 ~95.8% raw 省流。
- **对外符号**：
  - `router`（:14）— `prefix="/slimapi"`，tags=["catalog"]。
  - `agent(request, directory=None)`（:17-52）— handler：stash 解析 + validate directory（:37-39）→ `handle_catalog_request(upstream_path="/agent", project_fn=skeleton_agents, cache=getattr(app.state, "catalog_cache", None), merge_directory_vary=True, read_timeout=None)`（:41-50）。
- **依赖 / 被依赖**：依赖 `skeleton.skeleton_agents`（含非 dict item 静默跳过，skeleton.py:807-809）、`_catalog_common`、`selector`、`directory`、`transform`。挂载于 app.py:760（第 4 位）。
- **状态 / 可变性**：路由自身无状态；经 `catalog_cache` 走 TTL body 缓存 + **默认 `enable_etag=True`**（`_catalog_common.py:230`，未传 → True）——与 todo/diff/children 不同，agent/command 带 ETag/304。
- **错误路径**：`TransformBusy` → `busy_response`（:51-52，503 `transform_busy`）；非列表/JSON 失败 → 503 `upstream_unavailable`；4xx → 502 `upstream_http_N`（catalog 路由不传 `sid`，404 不映射 `session_not_found`）；超 cap → 413；缓存工厂错误同 503（`_catalog_common.py:430-431`）。
- **疑问点**（4）：
  1. 未传 `min_gzip_bytes`（默认 None → 无 tiny-body gate）——与 todo/children 的 64B gate 行为分叉（rev-6 C2 只覆盖 Batch 3），小目录无 gate 损益极小但属有意差异，审计应确认契约记录。
  2. `read_timeout=None`（:48）——无读超时；agent 目录 ~250KB，慢上游时占 admission。
  3. `skeleton_agents` 静默跳过非 dict item（skeleton.py:809）——与 todo/diff/children 的"非 dict → 503"守卫方向相反（容错 vs fail-fast），同一 `handle_catalog_request` 链上两种畸形策略并存。
  4. `directory` 被接受并转发但上游全局目录忽略之（docstring :30-32 自认"无害"）——多余 variance 进入 `Vary` 合并（`merge_directory_vary=True`），缓存键被无语义参数放大。

---

### src/oc_slimapi/routes/children.py（90 行）

- **职责**：T17 children thin route：`GET /slimapi/sessions/{sid}/children` → 上游 `GET /session/{sid}/children`。每个 child 是 `Session.Info`，逐个经 `skeleton_session()` 投影（与 sessions 列表路由同语义）；STATELESS re-add（v1 缓存一致性机制保持删除）。
- **对外符号**：
  - `router`（:35）— `prefix="/slimapi/sessions/{sid}"`。
  - `_project_children(items)`（:38-52）— 逐项 dict 守卫后 `[skeleton_session(item) for item in items]`（SESSION_KEYS 白名单 + time/summary/revert 嵌套 pick，skeleton.py:729-743）。
  - `session_children(request, sid, directory=None)`（:55-90）— handler：stash 解析 + validate（:72-74）→ `handle_catalog_request(..., enable_etag=False, merge_directory_vary=True, min_gzip_bytes=MIN_GZIP_BYTES)`（:76-88）。
- **依赖 / 被依赖**：依赖 `skeleton.skeleton_session`；其余同 todo.py。挂载于 app.py:760。
- **状态 / 可变性**：无状态（docstring :13-16 明确：无 `X-Children-Version`、无 digest 字段、无 cache/single-flight/SSE 失效）。
- **错误路径**：同 todo.py（`ValueError` → 503 `upstream_unavailable`；404+sid → 404 `session_not_found`；TransformBusy → 503；413）。
- **疑问点**（4）：
  1. `read_timeout=None`（:83）——同前，无读超时。
  2. 非守卫覆盖的 `skeleton_session` 内部畸形（`time` 非 dict → 跳过嵌套 pick 而非报错，skeleton.py:739-741）——静默降级为缺键。
  3. 投影丢弃 `parentID` 之外的重组信息：child 自身 `parentID` 保留（SESSION_KEYS 含 parentID），但列表顺序由上游决定——顺序稳定性未在文件内声明。
  4. 设计文档 `docs/specs/traffic-route-children-2026-08-10.md`（:5）与 §6.2 guardrail 的引用需下游审计核对。

---

### src/oc_slimapi/routes/command.py（49 行）

- **职责**：`GET /slimapi/command` —— command 目录骨架投影：白名单 `name/description/agent/hints`（`COMMAND_SKELETON_KEYS`，skeleton.py:768），丢 `template`（~97.7%），实测 ~97.6% raw 省流。
- **对外符号**：
  - `router`（:14）— `prefix="/slimapi"`。
  - `command(request, directory=None)`（:17-49）— handler：stash 解析 + validate（:34-36）→ `handle_catalog_request(upstream_path="/command", project_fn=skeleton_commands, read_timeout=300.0, cache=..., merge_directory_vary=True)`（:38-47）。
- **依赖 / 被依赖**：同 agent.py（`skeleton_commands`，非 dict item 静默跳过，skeleton.py:782-787）。挂载于 app.py:760（第 5 位）。
- **状态 / 可变性**：经 `catalog_cache` TTL 缓存 + 默认 `enable_etag=True`；无路由自身状态。
- **错误路径**：同 agent.py（TransformBusy → 503 `transform_busy`；JSON/非列表 → 503 `upstream_unavailable`；4xx → 502 `upstream_http_N`；413 `response_too_large`）。
- **疑问点**（4）：
  1. `read_timeout=300.0`（:45）——**唯一带读超时的 catalog 路由**（agent=None、todo/diff/children=None）；同族内不一致的理由未在文件内记录。
  2. 未传 `min_gzip_bytes`（同 agent.py 疑问 1）。
  3. 300s 超时同时充当 connect/read/write 的 read/write 值（`stream_upstream` 中 connect 固定 5.0，`_catalog_common.py:84-90`）——write 也 300s 对 GET 无意义但无害。
  4. `directory` 同样被全局目录忽略（docstring :26-28）——与 agent.py 疑问 4 相同的 Vary 放大。

---

### src/oc_slimapi/routes/health.py（141 行）

- **职责**：两个诊断端点。`GET /slimapi/health`（进程/契约自描述）与 `GET /slimapi/ready`（上游连通 readiness 探针）。
- **对外符号**：
  - `router`（:12）— `prefix="/slimapi"`。
  - `READY_VIEW = 3`（:19）— /ready 冻结到终态 v3 形（契约 §12：零 v4 差异）。
  - `health(request)`（:22-102）— `/slimapi/health`：`view = wire_view_from_scope(request.scope)`（:30，selector stash；无 selector 直调默认 3）。响应：`slimapi_contract=view`、`sidecar.{ok,version}`、`server.{api_version=view, accepted_client_versions}`、`schema.{degraded, version=view, clientMin, clientMax}`、`features = {**FEATURES, tokenStream: True, thresholdedSkeleton: True, skeletonInlineOutputMaxBytes, allowlist:{enabled[, droppedEvents]}}`；`view>=4` 时追加 `auxiliary`（dbaux.status().auxiliary_view()，无 dbaux → `{"available": False, "mode": "http"}`，:81-85）；`deploymentRevision` 可选（:87-89）。
  - `ready(request)`（:105-141）— `/slimapi/ready`：转发 `X-Request-ID`（:118-121）GET 上游 `/global/health`（timeout 5.0，:115-117）；`stash_up_in` 记账（:125）；`ok = status_code < 300`（:126）；任何异常 → `ok=False`（:127-128）；响应 `upstream.{ok, latencyMs}` + 冻结 v3 的 `server`/`schema` 块；HTTP 200 iff ok else 503（:141）。
- **/health 与 /ready 的差异**（重点）：/health 是**双视图**（?v=4 → v4 face 带 `auxiliary`；?v=3 → 字节等同 v3 终态），不触上游、恒 200；/ready **不 fork 版本**（`READY_VIEW=3` 硬编码，:109），触上游 ping、按上游状态返 200/503，且**不含** `slimapi_contract`/`features`/`auxiliary`。**readiness 聚合**：/ready 的聚合面只有上游 ping（<300 即 ok）+ `schema.degraded` 透传；v4 契约 §3.3 的九/十-ID readiness gate **不在 /ready**，而在 `GET /slimapi/versions` 的 `capabilities["4"].readiness`（src/oc_slimapi/readiness.py，经 versions.py:124 暴露）——两处 "readiness" 语义不同，易混淆。
- **依赖 / 被依赖**：依赖 `__version__`、`features.FEATURES`（静态全 True dict，src/oc_slimapi/features.py:15）、`gzip_util.json_response`、`selector.wire_view_from_scope`、`traffic.stash_up_in`、`upstream.forward_upstream_headers/request_id_from_scope`、app.state（config/schema_degraded/deployment_revision/hubs/dbaux）。两路由被 `traffic.bucketize` 归入 `health` 桶（src/oc_slimapi/traffic.py:103-104）。挂载于 app.py:760（第 1 位）。
- **状态 / 可变性**：无自身状态；全部读 app.state / config。`accepted_client_versions` 经 `list()` 拷贝（:40、:133）防外泄可变引用。
- **错误路径**：本文件无 CodedHTTPException 构造；/ready 上游失败 → 结构化 503（json body `upstream.ok=false`，非错误码 envelope）；/health 恒 200。`hubs.get_global()` 异常被裸 `except Exception` 吞为 `hub=None`（:96-98）→ 省略 `droppedEvents`。
- **疑问点**（8）：
  1. /health 受 selector 管控（需 `?v=3`/`?v=4`，无 v → 400 `unsupported_version`），/versions 无条件豁免——监控探针若不带 `?v=`，health 会 400 而 versions 200；部署侧（systemd/smoke）是否已适配待核（docs/operations.md）。
  2. `ok = response.status_code < 300`（:126）——3xx 重定向也算 ready；上游若被反代改写返回 301 仍报 ok。
  3. /health 与 /ready 响应**均未显式设 `Cache-Control: no-store`**（:102、:141 无 headers 参数）——对比 directories/versions 都设了 no-store；诊断端点可被缓存是否符合意图待核。
  4. :96-98 裸 `except Exception` 吞 `hubs.get_global()` 异常——无日志，静默丢 `droppedEvents`。
  5. `schema.clientMin/clientMax`（:49-50、:138-139）直接取 `accepted_client_versions[0]/[1]`——若 tuple 长度非 2 会 IndexError；config `_version_range` 保证恰 2 元（config.py:167-172），防线在别处。
  6. `features` 字面量键（`tokenStream`/`thresholdedSkeleton`/`skeletonInlineOutputMaxBytes`，:70-72）排在 `**FEATURES` 之后——若 FEATURES 未来加入同名键会被字面量覆盖（当前无冲突，顺序敏感是隐患）。
  7. /health 的 `auxiliary` 占位 `{"available": False, "mode": "http"}`（:85）与 dbaux 真实 reason 字段（dbaux/lifecycle.py:278-285 有条件带 `reason`）形状不对称——测试 app 与生产 wire 形状在此处分叉。
  8. /ready 的 `latencyMs` 含 `stash_up_in` 记账耗时（:125-130 之间）——测量值略大于纯上游 RTT；无关正确性，仅精度语义。

---

### src/oc_slimapi/routes/versions.py（162 行）

- **职责**：`GET /slimapi/versions` —— v4-contract §3 版本发现端点。无参数、无条件豁免 selector（selector.py:546-548）、非 GET → 405 + `Allow: GET`（selector.py:534-540，优先于一切）。
- **对外符号**：
  - `router`（:57）— `prefix="/slimapi"`。
  - `CURRENT_VERSION = SERVER_API_VERSION`（:61）— 常量 4（versioning.py:38）。
  - `AVAILABLE_VERSIONS: list[int]`（:62-64）— `list(range(3, 5)) == [3, 4]`，导入时固定。
  - `CAPABILITIES: dict[str, dict]`（:67-98）— `"3"` face：envelope/directoryQuery/versionHeaderOptional/writeRoutes/readRoutes（终态冻结）；`"4"` face 四静态键：`globalSessions/auxiliaryFilters/sseReplay(经 **META_CAPABILITY_KEYS)/qpImmediateFull`。
  - `EXPAND_FEATURE_ID = "messages.expand.v4"`（:101）。
  - `_capabilities4(satisfied=None)`（:104-132）— 组装 `"4"` face：`satisfied=None` → **运行时模块全局查找** `readiness_mod.SATISFIED`（:120-121，flip batch 零编辑传播）；`caps4["readiness"] = readiness_payload(sat)`（:124，恒 advertised）；`EXPAND_FEATURE_ID ∈ sat` → `caps4["expand"] = {categories: EXPAND_CATEGORIES, fragmentMaxBytes: settings.max_expand_response_bytes}`（:127-131，iff 双侧不变量）。
  - `versions(request)`（:135-162）— handler：`capabilities = {"3": {**CAPABILITIES["3"], "expand": {...live fragmentMaxBytes...}}, "4": _capabilities4()}`（:143-152）→ `json_response({current, available, capabilities, sidecarVersion}, headers={"Cache-Control": "no-store"})`（:153-162）。**无 ETag**（docstring :38-39 "discovery must always be revalidated"）；`Vary: Accept-Encoding` 经 json_response 协商族。
- **响应形状（重点——capabilities["4"] 静态性）**：`{"current": 4, "available": [3, 4], "capabilities": {"3": {envelope, directoryQuery, versionHeaderOptional, writeRoutes, readRoutes, expand{categories, fragmentMaxBytes}}, "4": {globalSessions, auxiliaryFilters, sseReplay, qpImmediateFull, readiness{...}, expand?{...}}}, "sidecarVersion": "__version__"}`。静态性边界：`"4"` 的**前四个键**冻结字面（不随 runtime/DB/replay-log 配置变化）；`readiness`/`expand` 随**代码版本**（readiness.SATISFied 的 flip batch）变化——当前 `SATISFIED = frozenset(REQUIRED)` 全集（readiness.py:93，2026-08-19 修订二 close-out）→ `ready=True`、`expand` 出现；`fragmentMaxBytes` 每请求从 Settings 读（env 可调，config.py:372 + 校验 config.py:860-866）——严格说是 **runtime-config 派生**，与 docstring :20-21 "never vary with runtime state" 存在措辞张力（注释 :137-140 已自认 live）。
- **依赖 / 被依赖**：依赖 `versioning.ACCEPTED_CLIENT_VERSIONS/SERVER_API_VERSION`、`readiness` 模块（REQUIRED/SATISFIED/readiness_payload，含未知 ID RuntimeError 守卫与依赖蕴含 ⑦ 守卫）、`config.settings`、`gzip_util.json_response`、`sse.replay_wire.META_CAPABILITY_KEYS`（= `{"sseReplay": True}`，replay_wire.py:96——meta 帧与 versions 通道同源不漂移）、`traffic.EXPAND_CATEGORIES`（12 类冻结表，traffic.py:50-63）、`__version__`。挂载于 app.py:760（第 2 位）。
- **状态 / 可变性**：模块级 `CURRENT_VERSION/AVAILABLE_VERSIONS/CAPABILITIES` 导入时冻结；请求期动态部分仅 readiness 集合与 settings 读取。无缓存。
- **错误路径**：本文件无错误构造点（端点永不因未知输入拒绝——根本不接受参数）；`readiness_payload` 内部对未知 ID 抛 RuntimeError（readiness.py:116-127）→ 会变成未捕获 500，但 SATISFIED 为字面常量，结构上不可达。
- **疑问点**（7）：
  1. `AVAILABLE_VERSIONS/CURRENT_VERSION` 源自 versioning **常量**，而 /health 的 `accepted_client_versions/clientMin/clientMax` 源自 **config**（可被 env 解析）——双源；被 config.py:817-822 的 fail-closed 校验（必须恰等 `(3,4)` 否则启动 RuntimeError）钉死对齐，当前无分歧路径，但一致性靠校验维持而非单一来源。
  2. `_capabilities4(satisfied)` 的参数是测试后门（docstring :118 "tests pass explicit sets"）——生产恒 None；若未来被误用于注入非全集集合，模块级守卫只在 readiness.py 侧，本文件无二次校验。
  3. `"3"` face 的 `expand` 无条件存在且 `fragmentMaxBytes` 为 live 配置（:147-149）；`"4"` face 的 `expand` 受 iff 门控（:126-131）——两 face 的 expand 存在性规则不同（3 无条件 / 4 iff），契约 §0.5 v3 冻结与 §14 的组合结果，审计需确认客户端不假设对称。
  4. `CAPABILITIES` 为可变模块级 dict 且被 `_capabilities4` 以 `dict(CAPABILITIES["4"])` 浅拷贝（:123）——外层防改，但内层值（列表/bool）仍共享引用；当前全为不可变标量，无实际风险。
  5. `sidecarVersion` 在未安装 checkout 上为 `"0.0.0+unknown"`（`__init__.py:9`）——发现端点会广播占位版本；运维应保证生产经 pip 安装。
  6. docstring :8 "Non-GET → 405 + Allow: GET (enforced by the selector middleware)"——该行为不在本文件，审计断言依赖 selector.py:534-540 的实现顺序（①versions 405 优先于版本 400），跨文件耦合。
  7. `AVAILABLE_VERSIONS` 是 `list`（可变类型作模块常量，:62）——无 `Final`/tuple 保护，风格上可被意外 mutate。

---

### src/oc_slimapi/qp_sweep.py（250 行）

- **职责**：B1b 阶段 1 **shadow-only** q/p sweep 调度器：模块内零上游客户端、零 HTTP 操作；每次 touch 只记录"未来真实 sweep 会做什么"的 marker，使调度节奏与预算可在生产安全观察。
- **对外符号**：
  - `_EVICTION_AFTER = 30*86400`（:20）、`_MAX_SLEEP_SECONDS = 30.0`（:21）。
  - `QpSweepShadow`（:24-250）：
    - `ESTIMATED_DIRECTORY_BYTES = 2*1024`（:34）— shadow 估算每目录 sweep 字节成本。
    - `__init__(*, activity=None, directories=(), directory_source=None, interval_seconds=1800.0, daily_budget=100, enabled=True, now=time.time, jitter=None, eviction_after_seconds=30d)`（:36-80）— 参数校验：interval<=0 / budget<0 / eviction<=0 → `ValueError`（:49-54）；`markers = deque(maxlen=256)`（:73）。
    - `activity` property（:82-84）、`task` property（:86-88）、`running` property（:90-92）。
    - `observe_directory(directory, *, now=None)`（:94-107）— 非 str/空串静默返回；新目录 `_next_run[dir]=timestamp`（下次扫描即评估）；运行中 set `_wake_event`。
    - `record_activity(directory, *, now=None)`（:109-114）— 写 `_activity[dir]` + observe。
    - `record_request_activity(directories, *, now=None)`（:116-122）— 一次聚合 q/p 请求对其全部目录记 activity（questions.py:218-220、permissions.py:234-236 调用）。
    - `_ingest_directory_source()`（:124-131）— 遍历 `_activity` 以旧时间戳 observe；再拉 `directory_source()`（item 取 `getattr(item, "directory", None)`）。
    - `next_delay()`（:133-135）— `interval * clamp(jitter(), 0.8, 1.2)`（默认 jitter uniform(0.8,1.2)，:68）。
    - `_utc_day`（:137-139）— UTC 日 ISO 串。
    - `_reset_budget_if_needed`（:141-145）— 跨 UTC 日清零 `_budget_used`。
    - `_due_directories`（:147-153）— `next_run <= now`，按字典序排序遍历。
    - `_evict_stale_directories`（:155-164）— `now - seen_at >= 30d` 的目录从三表移除。
    - `run_once(*, now=None)`（:173-209）— **shadow 决策核心**：ingest → evict → reset budget → 对每个 due 目录：`_triggers_total+=1`；`elapsed = now - last_activity`（activity 优先、seen_at 兜底，:182）；`elapsed < interval*3` → `skip`；`_budget_used >= daily_budget` → `budget_exhausted`；否则 `cold`（`would_sweep=True`、`_budget_used+=1`、`_est_bytes_total += 2KB`）；marker 入 deque；**每目录重排 `next_run = now + next_delay()`（含 skip/exhausted）**（:208）。
    - `_run()`（:211-220）— 主循环：sleep 至最近 deadline（cap 30s；`_wake_event` 可提前唤醒）→ `run_once()`。
    - `start()`（:222-226）— `enabled=False` 或已运行 → 返回现 task；否则 `asyncio.create_task(_run(), name="qp-sweep-shadow")`。
    - `stop()`（:228-237）— cancel + await，吞 CancelledError。
    - `metrics()`（:239-247）— 六键：`triggers_total/cold_hits/skips/budget_exhausted/est_bytes_total/known_directories`。
    - `snapshot()`（:249-250）— metrics + `markers` 列表。
- **阶段 1 shadow 逻辑与 metrics 暴露（重点）**：无真实 sweep——`would_sweep` 只是 marker 布尔；"cold" 判定 = 该目录 3×interval 无 q/p 活动（:184 `elapsed < self.interval_seconds * 3` 硬编码系数 3）；预算按 UTC 日重置。**metrics 暴露**：`GET /slimapi/metrics` 的 `hubs["sweep"]` 块 = `qp_sweep.metrics()` 六计数键（src/oc_slimapi/routes/metrics.py:42-44，`app.state.qp_sweep` 存在且 `enabled` 时）；**含 markers 的 `snapshot()` 在生产代码无调用方**（仅 tests/test_b1b_sweep_shadow.py）——marker 明细实际不可经 wire 观察。
- **依赖 / 被依赖**：仅标准库（asyncio/random/time/collections/datetime）。被装配于 `src/oc_slimapi/app.py:501-525`（`qp_sweep_enabled` 时：`activity=global_hub.qp_last_activity` **共享可变 dict 引用**、`interval/budget` 来自 settings `OC_SLIMAPI_QP_SWEEP_*`（config.py:622-630）、`global_hub.set_directory_observer(qp_sweep.observe_directory)` 同步观察每个有效事件目录、lifespan stop 回调；disabled → `app.state.qp_sweep = None`）；消费方 questions.py:218、permissions.py:234、metrics.py:42-44。预留桶 `/slimapi/_shadow/sweep`（traffic.py:106-108）无实际路由。
- **状态 / 可变性**：全部可变状态集中于实例（`_known_dirs/_seen_at/_next_run` 三表 + 计数器 + deque + `_budget_day/_budget_used` + `_task/_wake_event`）；`_activity` 与 GlobalHub 共享引用（外部可写）。单事件循环假设下无锁（方法全同步）。
- **错误路径**：构造期 `ValueError`（:49-54）；运行期无异常构造点——`observe_directory` 对非法输入静默 return（:95-96）、`_ingest_directory_source` 对非 str 对象 `getattr(..., None)` 后同样静默（:130-131）。
- **疑问点**（10）：
  1. `_ingest_directory_source`（:125-126）用 activity dict 中**旧时间戳**作 observe 的 now——若 hub 重启后 `qp_last_activity` 残留旧值，目录 observe 即刻携带旧 `seen_at`，`run_once` 开头的 `_evict_stale_directories`（:177）可能**当轮即逐出**刚 observe 的目录（30d 活动旧值），行为正确但微妙，值得测试覆盖（tests 已有部分）。
  2. `directory_source` 分支（:127-131）在生产装配（app.py:504-508）未传入——当前为死代码路径；保留 API 但无调用方。
  3. 预算按 **UTC 日**重置（:138-139）而注释/命名说 "daily"——本地时区部署的运维对账需注意日界限。
  4. "cold" 阈值 `interval*3`（:184）魔法数硬编码，未配置化也未命名常量。
  5. `_run()` 主循环（:211-220）**无 try/except**——`run_once` 若抛未预期异常（如 jitter 回调抛错），task 静默死亡且无日志、无重启；当前纯内存逻辑难触发，但调度器无自愈/可观测死亡是结构缺口。
  6. `stop()` 后 `_task=None`（:230），`start()` 可再启——生产只用一次；重复 start/stop 循环下的 `_wake_event` 残留 set 状态未清理（`_run` 开头 clear 在 delay>0 时才发生）。
  7. `next_delay` 对 jitter 结果再 clamp（:134）与默认 uniform(0.8,1.2) 双重保险——若注入自定义 jitter 越界会被静默截断，测试语义可能失真。
  8. `metrics()` 的 `est_bytes_total` 以每目录恒 2KB 估算累加（:197）——粗估模型，无 README 级别说明（仅类常量名自述）。
  9. markers 观测断层：deque 保存 256 条 marker 但生产唯一出口 `metrics()` 只暴露聚合计数（metrics.py:44）——`snapshot()` 的 markers 无 wire 出口（traffic.py:106 的 `_shadow/sweep` 桶是预留无路由）；"观察 cadence" 的阶段 1 目标实际只能看到计数，看不到 per-directory 节奏。
  10. `record_request_activity`（:116-122）由 q/p 请求路径同步调用（questions/permissions handler 内）——每次遍历全部目录写共享 dict；目录数大时（上限受 discovery 10k 影响）在请求热路径上的同步开销未设上限。

---

### src/oc_slimapi/turn_registry.py（314 行）

- **职责**：turn token 强 fence——给转发的 `session.digest` SSE 事件盖 `turnIncarnation` + `turn` 两个平顶层字段，供 ocdroid 做因果栅栏。冻结决策：O2 策略 A（`persisted_last+1`，单进程免文件锁）、O3 单实例（scope key = sid alone）、O4 turn 不持久化（重启清零，靠 incarnation 抬升）、S2 commit 点 = bump-before-send（发送失败留洞——契约 §4.2 的批准放宽）。
- **对外符号**：
  - `_FALLBACK_INCARNATION = 1`（:49）；`_TURNS_MAX = 10_000`（:61，LRU 上限）；`_INCARNATION_FILENAME = "incarnation"`（:64）。
  - `IncarnationStore`（:67-200）：
    - `__init__(state_dir, legacy_state_dir=None)`（:90-98）— 新路径优先，legacy（旧 access_log 目录）仅在新路径 missing/corrupt 时回读。
    - `_read_path(path)`（:100-128）— missing/None 路径 → `(False, 0)` 静默；OSError/空串/非整数/负数 → `(False, 0)` + warning（各 :112-127）。
    - `load_or_bump()`（:130-149）— 读新 → 不 valid 再读 legacy → `base+1`；**只写新路径**；写失败仍返回内存值 + warning（:143-148）。
    - `_write_persisted(inc)`（:151-200）— 原子写：mkdir → 写兄弟 `.tmp` → `flush` + **`os.fsync`** → `os.replace`（POSIX 原子）；失败清理 tmp、warning、返回 False，**永不 raise**（防 restart-incarnation-reuse：半写文件会被解析为 corrupt → 回退 0 → 复用 incarnation 1）。
  - `TurnRegistry`（:203-277）：
    - `__init__(incarnation)`（:231-233）— `incarnation` 进程期冻结；`_turns: OrderedDict[str, int]`。
    - `bump_turn(sid)`（:235-263）— `+1`、`move_to_end`（LRU 刷新）；`len > 10_000` 时 `popitem(last=False)` 逐出 + observability warning（:254-262，B7/P1-23：行为不变仅加日志——新 incarnation 疗法被否决因 incarnation 是进程级冻结）。
    - `snapshot(sid)`（:265-277）— 恒返 `(incarnation, turn)` 二元组；未观察 sid → `(incarnation, 0)`；调用方（GlobalHub.publish）在 DigestFields 上冻结值，后续 bump 不回溯（契约 §7.4/V10）。
  - 路径分类器（自退役 catch-all 迁入，:280-314）：
    - `_SESSION_SID_RE`（:283）、`_TURN_BUMPING_SUFFIX_RE`（:284-285，`/session/{sid}/(prompt_async|abort)/?`，容忍尾斜杠）。
    - `extract_sid_from_path(norm_path)`（:288-299）— `/session/{sid}/…` 首段；非 session 路径 / 空 sid → None；不硬编码上游 id 格式。
    - `is_turn_bumping_path(norm_path)`（:302-314）— 仅路径匹配；**POST 检查留给调用方**；sync `/session/{sid}/prompt` 不再识别（M3-3：从未收集、catch-all 已关）。
- **盘上计数与 sweep（重点）**：**盘上计数** = incarnation 单行整数文件（state_dir/incarnation，app.py:582-584 注入 `settings.state_dir` 与 legacy access_log 目录），启动一次 read→+1→原子写回；turn 本身**不落盘**（O4）。**"sweep"** 对应 `_turns` 的 LRU 逐出（`_TURNS_MAX=10_000`，:254-262）——已知上界权衡：同 incarnation 内被逐出 sid 再 bump 会从 1 重来（lex-LOWER 回归，客户端 fence 视为 stale 直到爬回原值）；披露为"实际不可达（需单进程 >10_000 个 distinct bumped sids）"。
- **依赖 / 被依赖**：依赖 `logging_config.get_logger`（:41-43）。被依赖：`app.py:580-588`（lifespan 构造 + `hubs.set_turn_registry`）；`routes/write_groups.py:182-186`（`POST` + `is_turn_bumping_path` → `extract_sid_from_path` → `bump_turn(sid)`，**send 之前**）；`sse/global_hub.py:712-713`（publish 时 `snapshot(session_id)` 盖章）；`routes/sessions.py:250-254、870-874`（status 路径 per-caller merge turn 字段）；`sse/registry.py:104-137`（set_turn_registry 传播至 live hub）；`proxy.py:19-20` 仅注释提及。
- **状态 / 可变性**：`IncarnationStore` 持两路径（不可变）；`TurnRegistry.incarnation` 冻结、`_turns` 可变 OrderedDict。全部同步纯 dict 操作，单事件循环免锁（契约 §7.2）。
- **错误路径**：无 HTTP 错误码构造点；全部 I/O 失败降级为 warning + 内存值（`_read_path`/`load_or_bump`/`_write_persisted`）。`validate` 类 RuntimeError 不在本文件（在 readiness.py）。
- **疑问点**（8）：
  1. incarnation 回退窗口（:137-139 + :143-148 组合）：上次进程成功持久化 inc=N 后**新路径文件损坏** → 回读 legacy M（可能 < N）→ 新 inc = M+1 ≤ N——同 fence 值复用。需要"新文件在成功写入后被外部损坏"才触发，概率极低，但 docstring 只披露了"写失败"路径未点名这条"损坏后 legacy 回退"链。
  2. legacy 文件永不删除（:136 注释）——迁移完成后成为永久回退隐患（与疑问 1 同链）。
  3. `load_or_bump` 写失败时返回内存 inc（:143-148）——若随后进程长期运行并再次重启，文件仍 stale，两次重启可能拿到相同 inc（注释已自认 "restart may re-read a stale value"）。
  4. `bump_turn` 留洞语义（S2 放宽）——`send` 抛错时 turn 已前进；审计下游应确认 ocdroid 侧 lex 比较确实容忍洞（docstring :24-26 声称已批准）。
  5. LRU 逐出回归（:254-262）只在**逐出发生时**告警，被逐 sid 后续 bump 回 1 时**无第二次告警**——实际回归发生点不可见。
  6. `is_turn_bumping_path` 不校验 method（:311-313 自述），`write_groups.py:182` 负责 `POST` 检查——若未来新增调用方漏检 method，GET 也会 bump；契约防线在调用方而非分类器。
  7. `extract_sid_from_path` 对 URL 编码 sid 不解码（正则原始段）——与 write_groups 侧 norm_path 的构造方式耦合（须确认上游路径未 percent-encode sid；一般 zod/路由下不会，但未见断言）。
  8. `IncarnationStore` 无防重入（`load_or_bump` 假设 startup 期无并发，:77-78）——若未来 lifespan 变并行或被测试重复调用，read→bump→write 可能交错；当前 app.py 只调一次（:582-586）。

---

### src/oc_slimapi/logging_config.py（146 行）

- **职责**：`oc_slimapi` 根 logger 的 stderr StreamHandler 配置（`OC_SLIMAPI_LOG_LEVEL` env 驱动、幂等、uvicorn 热重载安全）+ 独立的 `oc_slimapi.actions_audit` WARNING 级审计 logger + `get_logger` 前缀工厂。不触碰 `oc_slimapi.access` logger（由 access_log 的 DailyAccessHandler 管理）。
- **对外符号**：
  - `_ROOT_LOGGER_NAME = "oc_slimapi"`（:26）、`_AUDIT_LOGGER_NAME`（:27）、`_setup_lock`（:28，threading.Lock）。
  - `_resolve_log_level()`（:31-52）— 读 env（默认 "INFO"）大写化后查 `logging._nameToLevel`（stdlib 私有表，:44）；无效值 → warning + 回退 INFO。
  - `setup_actions_audit_logging()`（:55-85）— audit logger：`setLevel(WARNING)` + `propagate=False`（:67-68，独立于根级）；锁内幂等检查（已有 stderr StreamHandler 则 return，:71-75）；固定 WARNING handler + 统一 formatter。
  - `setup_logging()`（:88-123）— 根 logger setLevel(env)；锁内幂等检查（:104-110）；DEBUG 级 handler 让 logger 级做门（:113）；末尾引导 audit logger（:123）。
  - `get_logger(name)`（:126-137）— 已带 `oc_slimapi` 前缀则原样，否则加前缀。
  - `redact(secret)`（:140-146）— 恒返 `"<redacted>"`；**测试专用，无生产调用方**（:143-145 自认，tests/test_logging_config.py:19 钉契约）。
- **依赖 / 被依赖**：仅标准库。被依赖极广：`app.py:24`（setup_logging 于 :195 lifespan 启动）、`turn_registry.py:41-43`、`sse/replay_wire.py:255-257`、`sse/tokenstream/hub.py:83-111`、`dbaux/projection.py:51-59`；测试 tests/test_logging_config.py、tests/test_sse_logging.py:15。
- **状态 / 可变性**：模块级 `_setup_lock`；logger/handler 状态由 logging 全局管理；幂等检查以 `h.stream is sys.stderr` 身份判定（:72-74、:107-109）。
- **错误路径**：无异常路径——env 无效降级 INFO（:46-52）；audit 与主 setup 均不 raise。
- **疑问点**（5）：
  1. `logging._nameToLevel`（:44）是 stdlib **私有** API——注释自认知，但 CPython 升级无兼容承诺（虽长期稳定）。
  2. `setup_actions_audit_logging` 的 `setLevel/propagate`（:67-68）在锁外、幂等检查在锁内——微小竞态窗口（值恒定无实际危害，仅次序不严格）。
  3. `NOTSET` 被列为合法值（docstring :39）——根 logger 设 NOTSET（=0）等于全量放行 DEBUG，与运维对 "NOTSET=继承" 的直觉相反（根上无继承语义）。
  4. `_resolve_log_level` 的回退 warning 在 handler 装配**之前**发出（:47-51）——经 lastResort 输出 stderr，实际可见，但绕过统一 formatter。
  5. 幂等判定用 `h.stream is sys.stderr` 身份比较——若 uvicorn/reload 或测试替换 `sys.stderr` 对象，旧 handler 不再匹配 → 可能叠加第二个 handler；现实中低风险（测试已覆盖幂等主线）。

---

### src/oc_slimapi/__init__.py（9 行）

- **职责**：包版本单一来源：`__version__ = importlib.metadata.version("oc-slimapi")`（dist-info，release.sh bump 自动传播）；未安装 checkout 回退 `"0.0.0+unknown"`（`PackageNotFoundError`，:8-9）。
- **对外符号**：`__version__`（:7/:9）——唯一符号。
- **依赖 / 被依赖**：仅标准库 importlib.metadata。被 `routes/health.py:5`（sidecar.version）、`routes/versions.py:49`（sidecarVersion）、app.py 等消费。
- **状态 / 可变性**：导入期一次性求值，不可变字符串。
- **错误路径**：无（PackageNotFoundError 已捕获）。
- **疑问点**（2）：
  1. 回退值 `"0.0.0+unknown"` 会原样进入 `/slimapi/health.sidecar.version` 与 `/slimapi/versions.sidecarVersion`——裸源码运行时客户端/运维看到占位版本，无标记区分"未安装"与"真实 0.0.0"（PEP 440 本地版本段 `+unknown` 略有区分，但依赖消费方解读）。
  2. 模块导入即触发 dist-info 查询（每次进程一次，开销可忽略）——无缓存问题，仅记录。

---

### src/oc_slimapi/middleware/__init__.py（1 行）

- **职责**：ASGI middleware 包标记（docstring："oc-slimapi ASGI middleware."）。
- **对外符号**：无——包内 `request_id.py`、`traffic_accounting.py` 由调用方按子模块路径直接导入，不经 `__init__` 再导出。
- **依赖 / 被依赖**：无依赖；被依赖：app.py 装配 middleware 时直接 import 子模块（`middleware/request_id.py`、`middleware/traffic_accounting.py`）。
- **状态 / 可变性**：无。
- **错误路径**：无。
- **疑问点**（1）：
  1. `__init__` 不导出子模块公共符号——新调用方需知道完整子模块路径；纯风格，无再导出约定（`selector.py` 位于包根而非 middleware/ 下，命名归属略有不一致——selector 事实上的 ASGI middleware 却不在 middleware 包内）。

---

### src/oc_slimapi/routes/__init__.py（1 行）

- **职责**：thin API 路由包标记（docstring："Thin API route package."）。
- **对外符号**：无——app.py:29-30 直接 `from .routes import actions, agent, children, command, directories, events, health, messages, metrics, permissions, questions, read_groups, sessions, todo, token_stream, versions, write_groups` + `from .routes import diff as diff_routes`。
- **依赖 / 被依赖**：无依赖；被 app.py:760 的挂载循环消费（18 个 router 按固定顺序注册：health → versions → actions → agent → command → sessions → children → todo → diff → messages → events → metrics → questions → permissions → directories → token_stream → read_groups → write_groups）。
- **状态 / 可变性**：无。
- **错误路径**：无。
- **疑问点**（1）：
  1. router 挂载顺序散落在 app.py:760 一处元组里（本包 `__init__` 不提供 `ROUTERS` 聚合）——顺序敏感（如 token_stream 须在 catch-all 之前，INTERFACE_MAP.md:13 说明）但无本地单一常量可查；新增路由改 app.py 元组 + INTERFACE_MAP 双处，漂移由 check_routes_doc.py 拦截路由↔文档一致性，但**顺序**本身无守卫。

---

## 汇总

| 文件 | 行数 | 疑问点数 |
|---|---|---|
| routes/directories.py | 202 | 7 |
| routes/diff.py | 110 | 5 |
| routes/todo.py | 84 | 4 |
| routes/agent.py | 52 | 4 |
| routes/children.py | 90 | 4 |
| routes/command.py | 49 | 4 |
| routes/health.py | 141 | 8 |
| routes/versions.py | 162 | 7 |
| qp_sweep.py | 250 | 10 |
| turn_registry.py | 314 | 8 |
| logging_config.py | 146 | 5 |
| __init__.py | 9 | 2 |
| middleware/__init__.py | 1 | 1 |
| routes/__init__.py | 1 | 1 |

疑问点合计：70。
