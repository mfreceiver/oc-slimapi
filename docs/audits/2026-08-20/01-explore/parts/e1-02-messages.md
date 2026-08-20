# E1-02 精读卡片 — src/oc_slimapi/routes/messages.py（1643 行）

> 只读审计产物（2026-08-20）。全部行号基于当前 worktree；引用格式 `src/oc_slimapi/routes/messages.py:行号`（下文省略为 `:行号`）。
> 交叉验证文件：`src/oc_slimapi/{transform,singleflight,skeleton,etag,envelope,gzip_util,errors,directory,selector,readiness,upstream_errors,traffic}.py`、`src/oc_slimapi/routes/_catalog_common.py`、`src/oc_slimapi/app.py`、`src/oc_slimapi/config.py`、`tests/test_messages_merged.py`。

## 职责（一句）

`/slimapi/messages/{sid}` 前缀下的四个消息消费路由：skeleton 列表投影（含 `mode=merged` 服务端合并展开、上游 list GET 单飞合并、ETag/304）、单消息 `/full/{mid}` 全量投影（strip LSP diagnostics、与 merged fan-out 同 key 单飞）、以及两个 design-expand 碎片展开端点（12 类白名单 category，与 `/full` 共享单飞 GET）。

## 对部符号清单（名字 + 行号 + 职责）

### 模块级对象

| 符号 | 行号 | 职责 |
|---|---|---|
| `router` | :40 | `APIRouter(prefix="/slimapi/messages/{sid}", tags=["messages"])`，四个 GET 路由的挂载点 |
| `TRANSFORM_RETRY_AFTER_SECONDS = 2` | :44 | admission 超时 503 的固定 `Retry-After`（body 字段 + 头双写，见 :278-284） |
| `_V4_EXPAND_FEATURE = "messages.expand.v4"` | :55 | §3.3 readiness 门控 feature id（expandRefs href 是否用 `?v=4`） |
| `_REL_PARAM_RE` | :168 | RFC 5988 `rel=` link-param 的编译正则（IGNORECASE，防 `title="rel=next"` 误匹配） |
| `_PLACEHOLDER_PART_ID_PREFIX = "thin_placeholder_"` | :361 | skeleton 塌缩占位 part 的 id 前缀（对 `skeleton.py:618` 的字符串复制，见疑问点 10） |
| `_DEGRADED = object()` | :365 | merged fan-out 单项失败/超预算的哨兵（该消息保留 skeleton 投影） |
| `_EXPAND_CATEGORIES` / `_EXPAND_CATEGORIES_SET` | :1271-1272 | **mid-file import**，从 `traffic.py` 引入 12 类冻结 category（单一事实源） |
| `_EXPAND_MESSAGE_LEVEL_CATEGORIES` | :1275 | `frozenset({"info_summary_diffs"})` — 唯一 message 级 category |
| `_EXPAND_APPLICABLE_TYPES` | :1278-1290 | part 级 category → 适用 part.type 元组（如 `part_state_output` → `("tool",)`、`part_snapshot` → `("step-start","step-finish")`） |
| `_EXPAND_EXTRACTORS` | :1471-1484 | category → extractor 函数表（12 项，与 `_EXPAND_APPLICABLE_TYPES` 键一一对应） |

### 类

| 符号 | 行号 | 职责 |
|---|---|---|
| `_CapExceeded(Exception)` | :368-387 | 内部哨兵异常：单飞 factory 内 read-cap 截断（`None` 翻译而来），携带 `cap`；使 flight entry 被 drop 而非以 `None` 结果 grace 保留，joiner（更大 cap）可 re-lead 重试 |

### 函数（非路由）

| 符号 | 行号 | 职责 |
|---|---|---|
| `_expand_wire_view(scope)` | :58-64 | §3.3 门控下的 href view：selector view==4 且 `messages.expand.v4 ∈ readiness.SATISFIED` → 4，否则折回 3（动态读模块全局，支持 monkeypatch） |
| `_created_sort_key(msg)` | :77-92 | 排序键 `info.time.created` ASC；畸形行默认 0（排最前，见疑问点 15） |
| `_parse_sort_project(body, *, limits, sid, wire_view)` | :95-133 | worker 入口：orjson 解析 + 非列表守卫（ValueError→503）+ 按 created ASC 排序 + skeleton 投影（不序列化）；merged 路径需要 dict 中间态 |
| `_project_list_sorted_and_pack(...)` | :136-165 | worker 入口：`_parse_sort_project` + `orjson.dumps` → identity bytes（pre-gzip；`accept_encoding` 仅为调用点对称保留） |
| `_link_rel_tokens(attrs)` | :179-190 | Link 属性串里 `rel` 值 → 小写 token 列表（RFC 5988 §3 多 token / 大小写不敏感） |
| `_extract_before_verbatim(query)` | :193-218 | 从原始 query 切出 `before=` 值，**绝不 percent-decode / `+`→space**（保护上游 opaque base64url cursor 往返）；`?before` 无值 → None（fail-safe） |
| `_parse_link_next_cursor(link_header)` | :221-265 | RFC 5988 Link 解析：取 `rel~next` URL query 中的 `before` cursor 原样返回（我们自己的 `nextCursor` 通道；见疑问点 8 的逗号切分） |
| `_busy_response(accept_encoding)` | :268-284 | 503 `transform_busy` + `Retry-After: 2`（body 含 `retry_after` 字段 :280-282，头 :283 后置；经 `error_response` 走 gzip 协商 + `Vary: Accept-Encoding`） |
| `_resolve_messages_directory(request, directory)` | :287-316 | directory 解析：selector stash 替换（:309）→ None 直通 → query/header 冲突 400 `directory_not_allowed`（:314-315）→ `validate_directory` 归一化（400 `invalid_directory` 在 directory.py:23）；**async 但无 await**（疑问点 14） |
| `_stream_upstream(request, path, params, directory)` | :319-334 | 构造并发出流式上游 GET（`forward_directory_headers`）；初始 `httpx.RequestError` → `raise_upstream_unavailable`（503）；调用者必须 `aclose()` |
| `_placeholder_pairs(projected)` | :390-422 | 页序收集携带 `thin_placeholder_{mid}` part 的 (index, mid) — merged 高优先队列；mid 优先取占位 part 的 `messageID`（skeleton.py:618-625 写入），回退 `info.id`；无可用 id 跳过 |
| `_expand_ref_pairs(projected)` | :425-451 | 页序收集**任一 part 携带 part 级 `expandRefs`** 的 (index, mid)；message 级 `info.expandRefs`（diffs 引用，~105KB）永不入候选集（§4.3.1） |
| `_merged_candidate_pairs(projected, config)` | :454-481 | 合并候选：placeholder 先占 `merged_max_fulls_per_page`（默认 16）个槽（页序），ref 候选只填剩余槽、按 mid 去重（placeholder 身份优先） |
| `_dedicated_full_get(request, sid, mid, directory, cap)` | :484-514 | 单消息 ONE 专用流式 GET + `read_upstream_response` cap-read（`sid=sid` → 404 映射 `session_not_found`，见疑问点 2）；返回 body 或 None（截断）；`finally: aclose()` |
| `_fetch_full_shared(request, pool, sid, mid, directory, *, cap)` | :517-588 | 经 `singleflight.fulls`（key=`("full", id(pool), sid, mid, directory)`）共享 GET；`cap=None`（direct /full）→ 全量 `max_message_bytes`；截断 raise `_CapExceeded` drop entry；join 小 cap 截断 → 最多 3 次尝试 re-lead；direct 耗尽后专用 GET fallback（:584-587），merged 显式无 fallback（:588 返回 None → 降级） |
| `_merge_fulls(...)` | :591-693 | merged phase B+C：预算预留模型 fan-out（`remaining` 可变单元 :651，reserve=min(max_message_bytes, remaining) :655-658，refund :666-671，逐项 CodedHTTPException→`_DEGRADED`）+ `asyncio.gather`（无 return_exceptions，疑问点 3）+ windfall 累计检查 :678-686 + phase C `async with pool` 下单次 offload splice :688-693 |
| `_merge_fulls_and_pack(projected, fetched, *, accept_encoding, fingerprint)` | :696-738 | worker 入口（phase C）：逐项 parse full → `strip_diagnostics_message` → 替换 `parts`（保留 LIST 的 `info`）；坏 JSON/非 dict/非 list parts 逐项降级；成功 splice 后 `recompute_fingerprint`（fingerprint 开启时）；`orjson.dumps` → identity bytes |
| `_canonical_list_query(limit, before, mode)` | :752-766 | 排序键序拼 query 串（coalescing key 用）；`mode` 参与 key（merged/默认页互不共享 flight，即使 ignored 值也分 key） |
| `_messages_list_key(request, sid, directory, limit, before, mode)` | :769-781 | list flight key：`("messages-list", id(upstream_client), sid, directory, canonical_query)` — 嵌 upstream client 身份防跨 app 共享 |
| `_fetch_list_raw(request, sid, params, directory, *, cap)` | :784-805 | 单飞 factory body：ONE list GET + cap-read + Link→cursor 捕获（aclose 前）；返回 `(body, next_cursor)` 给所有 joiner |
| `_messages_via_lease(...)` | :808-945 | join-first 租约路径（coalesce 开启时优先走）：`registry.fetch_or_bypass`（预算满返回 None → 调用方走直连）→ 自有 pool admission + offload 投影 → merged 则 `_merge_fulls` → envelope → ETag/Vary/gzip 尾部（与直连尾部逐行同构，:887-943） |
| `_expand_shape_error()` | :1293-1295 | raise `CodedHTTPException(502, code="upstream_invalid_shape")` — parsed-but-malformed 上游体 |
| `_expand_locate_part(message, part_id)` | :1298-1329 | 在 parts 中定位 part_id：parts 非 list/元素非 dict/id 不可用/重复 id → 502；找到列表尾仍无 → 404 `expand_target_not_found` reason=`part_missing` |
| `_expand_str_field(obj, field)` | :1332-1340 | 嵌套字符串字段：missing/null → None；非字符串 → 502 |
| `_extract_info_summary_diffs` | :1343-1362 | `info.summary.diffs` → `{"diffs": FileDiff[]|null}`（message 级；逐层 null 容忍、逐层类型校验） |
| `_extract_part_text` / `_extract_part_reasoning` | :1365-1372 | `part.text` → `{"text": str|null}` |
| `_expand_state(part)` | :1375-1383 | tool state 访问器：missing/null → None；非 dict → 502 |
| `_extract_part_state_output` / `_extract_part_state_error` | :1386-1395 | `state.output` / `state.error` → `{"output"|"error": str|null}` |
| `_extract_part_state_input_full` | :1398-1408 | `state.input` → `{"input": object|null}`；非 dict → 502 |
| `_extract_part_state_metadata_full` | :1411-1424 | `state.metadata` → `{"metadata": object|null}`，**剔除 `diagnostics` 键**（与 /full 同一 strip 语义） |
| `_extract_part_state_attachments` | :1427-1442 | `state.attachments` → `{"attachments": object[]|null}`；非 list 或元素非 dict → 502 |
| `_extract_part_url` / `_extract_part_source` / `_extract_part_snapshot` | :1445-1462 | `part.url`(str) / `part.source`(dict) / `part.snapshot`(str) → 单键 dict |
| `_extract_compaction_full` | :1465-1468 | 完整 compaction part **减去 sidecar 注入的 `expandRefs`**（白名单式构建） |
| `_expand_fragment_worker(body, *, category, mid, part_id, limit, accept_encoding)` | :1487-1528 | worker 入口（§3.1 步骤 4d-7）：parse（失败→ValueError→503）→ 定位 part → type 适配校验（400）→ extractor → envelope `{category, messageID, data[, partID]}` → 序列化后按 `limit` 检查（413）→ `compress_if_beneficial` |
| `_expand_fragment(request, sid, category, mid, part_id, directory)` | :1531-1613 | 两个 expand 路由的共享实现（§3.1 严格求值序，见下文路由段） |

### 四个路由 handler

#### 1) `messages` — `GET /slimapi/messages/{sid}`（`@router.get("")` :948，handler :949-1141）

逐段：

- **入参** :950-955：`sid` path；`limit: int = Query(40, ge=1, le=200)`（越界 → FastAPI 原生 422，非 coded body，疑问点 18）；`before`（opaque cursor，原样转发 :981-987，注释论证 base64url 字符集在 FastAPI percent-decode 下安全）；`directory`、`mode`（仅字面 `"merged"` 生效 :990，其余值静默忽略，永不 400）。
- **directory 解析** :979 → `_resolve_messages_directory`。
- **coalescing 前置** :994-1001：`raw_fetch_registry` 存在且 `coalesce_enabled` → `_messages_via_lease`；返回 None（预算满/禁用/旧实例）→ 落入下方直连路径。
- **直连 admission** :1009 `async with pool`（**GET 前先 admission** — 关键防 OOM 修复，注释 :1005-1008）。
- **上游 GET** :1010-1012 `_stream_upstream` → `GET /session/{sid}/message?limit=&before=`。
- **cap-read** :1017-1022 `read_upstream_response(cap=max_response_bytes)`；`None` → 413 `response_too_large`（:1024-1028，early return 于 `finally aclose` :1075-1076 保护内，仍持 admission 至 `async with` 退出）。
- **cursor 捕获** :1035 `_parse_link_next_cursor(response.headers.get("Link"))`；上游 Link 头**不**外泄（v3 契约 cursor 通道 = envelope `nextCursor`）。
- **wire_view** :1050 `_expand_wire_view(request.scope)`（事件循环上读 selector stash）。
- **投影 offload** :1051-1072：merged → `_parse_sort_project`（仅 dict，不 pack）；默认 → `_project_list_sorted_and_pack`（直接 identity bytes）；`(orjson.JSONDecodeError, ValueError, TypeError, AttributeError)` → 503 `upstream_unavailable`（:1073-1074）。
- **merged B+C** :1077-1084 `_merge_fulls`（phase A 的 admission 已释放；phase B 无 slot fan-out；phase C 内部再 admission :688）。
- **envelope** :1089 `messages_envelope_bytes(identity, next_cursor)` → `{"items":<bare array bytes>,"nextCursor":<str|null>}`（envelope.py:21-28，字节拼接不重序列化；envelope bytes = ETag 输入）。
- **ETag/Vary/gzip 尾部** :1090-1139：
  - `Cache-Control: no-store` :1090；
  - `rep_version = etag_mod.response_rep_version(config, wire_view=3)` :1098-1099 — **硬编码 3**（疑问点 5）；
  - `vary_value = etag_mod.merged_vary("Accept-Encoding")` :1102（实际恒返回 `"Accept-Encoding"`，directory 维度已退役 — 注释 :1100-1101 与实现错位，疑问点 7）；
  - `judge_conditional`（pre-compression 单候选判定）:1107-1112；`"*"` → 压缩一次 + 回显实际 coding 的 tag :1113-1122；命中 → 304 `not_modified_response`（ETag+Vary+no-store，无 body、无 aux）:1123-1125；
  - miss → `compress_if_beneficial` :1126-1128（gzip 协商 + MIN_GZIP_BYTES + 实际收益三重门，gzip_util.py:75-108）；
  - 200：`final_headers = c_headers` + `Vary` 覆写 :1129-1130 + 条件 ETag（按实际 coding 强/弱 tag）:1131-1135 → `Response(..., media_type="application/json")` :1136-1139。
- **busy** :1140-1141 `except TransformBusy → _busy_response`。

租约路径 `_messages_via_lease`（:808-945）逐段：accept-encoding 读取 :823 → `_factory`（cap=config.max_response_bytes）:825-828 → `fetch_or_bypass(reserve_bytes=max_response_bytes)` :830-834 → None→直连 :835-836 → `async with lease`（**lease 持有跨越 merged fan-out 与 admission 等待**，疑问点 9b）:837 → 共享 body None → 413 :838-844 → 自有 admission + offload :862-877（异常面同直连）→ merged `_merge_fulls` :878-886 → envelope :892 → 与直连完全同构的 ETag/Vary/gzip 尾部 :893-943（rep_version pin 同样在 :902-903）→ TransformBusy :944-945。注意：租约路径上游 GET 在 admission **之外**（factory 内），与直连「admission-before-GET」不变量不同（plan §3.x 设计，非 bug）。

#### 2) `message` — `GET /slimapi/messages/{sid}/full/{mid}`（`@router.get("/full/{mid}")` :1144，handler :1145-1249）

- **入参** :1146-1147：`sid`、`mid` path、`directory` query；`?mode=` / `?known.*` 静默忽略（lite-v2 §2 降级为纯 on-demand expand，无 304 短路、无 `X-Message-Event-Seq`）。
- **directory** :1181；config/pool/accept_encoding :1182-1184。
- **admission absorb 循环** :1205-1214：`deadline = now + transform_absorb_budget_seconds`（默认 2.5s）；每轮 `pool.acquire(min(transform_wait_seconds, remaining))`（默认单轮 2s）；耗尽 → `raise TransformBusy()`（不变量：503 `transform_busy` 从未发出上游请求）。
- **单飞共享 GET** :1224 `_fetch_full_shared(cap=None)` — key=`("full", id(pool), sid, mid, directory)`，与 merged fan-out、expand 路由同 key 去重（singleflight.py:243-254；`fulls = SingleFlight()` plain profile，result grace 默认 1.0s）。
- **None → 413 `message_too_large`**（limitBytes=max_message_bytes）:1225-1230。
- **offload** :1235-1239 `strip_diagnostics_and_pack(body, accept_encoding, merge_directory_vary=True)`（worker：strip `state.metadata.diagnostics` + pack + gzip + Vary）；`(JSONDecodeError, ValueError, TypeError, AttributeError)` → 503 :1240-1241。
- **release** :1242-1243（`finally`，与 acquire 严格配对）。
- **响应** :1244-1247：200 + `Cache-Control: no-store` + extra（Vary / Content-Encoding；**无 ETag / 无 304** — lite-v2 冻结行为）。
- **busy** :1248-1249。

#### 3) `expand_message_fragment` — `GET /slimapi/messages/{sid}/expand/{category}/{mid}`（:1616-1628）

薄壳：调 `_expand_fragment(..., part_id=None, ...)`。仅 `info_summary_diffs` 为 message 级；其余 category 在此 400（expectedLevel=part）。

#### 4) `expand_part_fragment` — `GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`（:1631-1643）

薄壳：调 `_expand_fragment(..., part_id=partID, ...)`。`info_summary_diffs` + partID → 400 expectedLevel=message；未知 partID → 404。

#### `_expand_fragment` 共享实现（§3.1 严格求值序，:1531-1613）

1. **category 白名单** :1539-1544（plain str 手工白名单，故意不用 FastAPI Enum 避免 422）→ 400 `invalid_expand_category` + `validCategories`（12 类全表广播）。
2. **level 匹配** :1548-1560 → 400 `expand_category_mismatch`（expectedLevel=message/part）——**在 admission 之前**。
3. **directory** :1562；`fragment_limit = config.max_expand_response_bytes`（默认 8MiB，getattr 回退）:1567-1569。
4. **admission absorb 循环** :1573-1582（同 /full；「pool 满 503 先于一切 part 级 40x」）。
5. **共享单飞 GET** :1586（与 /full 同 key、cap=None 全量）→ None → 413 `expand_source_too_large` :1587-1595（cap-read 先于 JSON decode，oversize+malformed 仍 413）。
6. **offload worker** :1598-1602 `_expand_fragment_worker`（定位 404/502、type 400、序列化后 413、gzip 都在 worker 内）；route 捕 `(orjson.JSONDecodeError, ValueError)` → 503 :1603-1605（捕获面窄于其他路由，疑问点 12）。
7. **响应** :1608-1611（200 + no-store + extra）。
8. **busy** :1612-1613。

## 依赖（内部 imports）

| 来源 | 符号 | 行号 |
|---|---|---|
| `..errors` | `CodedHTTPException` | :13 |
| `..etag` | `etag_mod`（`response_rep_version`/`compute_etag`/`merged_vary`/`judge_conditional`/`not_modified_response`） | :14 |
| `..envelope` | `messages_envelope_bytes` | :15 |
| `..gzip_util` | `compress_if_beneficial`, `error_response` | :16 |
| `..selector` | `resolve_route_directory`, `wire_view_from_scope` | :17 |
| `..singleflight` | `full_fetch_key`, `fulls` | :18 |
| `..skeleton` | `SkeletonLimits`, `recompute_fingerprint`, `skeleton_messages`, `strip_diagnostics_message` | :19-24 |
| `..transform` | `TransformBusy`, `read_with_cap`, `strip_diagnostics_and_pack` | :25-29 |
| `..upstream` | `forward_directory_headers` | :30-32 |
| `..upstream_errors` | `raise_upstream_unavailable` | :33-35 |
| `._catalog_common` | `read_upstream_response` | :36 |
| `..directory` | `validate_directory` | :37 |
| `..readiness` | `readiness_mod`（动态读 `SATISFIED`） | :38 |
| `..traffic` | `EXPAND_CATEGORIES`, `EXPAND_CATEGORIES_SET`（**mid-file**） | :1271-1272 |

外部：`orjson`, `httpx`, `fastapi`(APIRouter/Query/Request), `starlette.responses.Response`, `asyncio`, `re`, `time`, `urllib.parse.urlparse`。

## 被依赖（rg 反查）

- **生产**：`src/oc_slimapi/app.py:29`（import `messages`）、`src/oc_slimapi/app.py:760-761`（`include_router`，位于 health/versions/actions/agent/command/sessions/children/todo/diff 之后、events 等之前）。
- **注释引用**：`src/oc_slimapi/skeleton.py:118`（SkeletonLimits 的 route 侧说明反向指向本文件）。
- **测试**（直接 import）：`tests/test_messages_routes.py:33`（并直接导入私有 `_parse_link_next_cursor` :1009/:1023/:1036/:1054、`msgs_mod` :308）；`tests/test_messages_merged.py:44`；`tests/test_errors.py:9`；`tests/test_upstream_error_boundary.py:37`；`tests/test_v3_envelope.py:25`；`tests/test_sessions_coalesce.py:30`；`tests/test_b2_merged_text_compat.py:28`。间接：`tests/test_readiness_gating_integration.py`（`messages.expand.v4` 门控）。

## 状态 / 可变性

- **模块级长生命周期对象**：`router` :40（注册后只读）；`TRANSFORM_RETRY_AFTER_SECONDS` :44；`_V4_EXPAND_FEATURE` :55；`_REL_PARAM_RE` :168（编译正则，不可变）；`_PLACEHOLDER_PART_ID_PREFIX` :361；`_DEGRADED` 哨兵对象 :365；`_EXPAND_MESSAGE_LEVEL_CATEGORIES` frozenset :1275；`_EXPAND_APPLICABLE_TYPES` dict :1278-1290 与 `_EXPAND_EXTRACTORS` dict :1471-1484（**类型上是可变 dict，但构造后仅读**）；`_EXPAND_CATEGORIES`（traffic.py 导入的 **list**，可变类型仅读）。
- **本模块无锁、无 task、无线程**；所有 CPU 工作经 `pool.offload` 进入 `TransformPool` 的 `ThreadPoolExecutor`（worker 函数 `_parse_sort_project`/`_project_list_sorted_and_pack`/`_merge_fulls_and_pack`/`_expand_fragment_worker` + transform.py 的 `strip_diagnostics_and_pack` 必须线程安全 — 均为纯函数）。
- **引用（非拥有）的进程级共享可变状态**：
  - `singleflight.fulls`（:18；plain `SingleFlight()`，completed 结果 grace 保留 1.0s，singleflight.py:97/:278/:770）— /full、expand、merged fan-out 三方共键；
  - `app.state.transforms`（`TransformPool`：信号量 + executor；`acquire/release` 手动配对路径 :1211/:1243、:1579/:1607）；
  - `app.state.raw_fetch_registry`（`LeasedSingleFlight(max_bytes=raw_fetch_max_bytes=64MiB 默认, network_concurrency=4 默认)`，app.py:385-388）；
  - `app.state.upstream`（httpx AsyncClient）；
  - `readiness_mod.SATISFIED` — **调用时动态读**（:62），测试可 monkeypatch 翻转门控；
  - `_merge_fulls` 的 `remaining` 可变单元（:651）只在事件循环串行点触达（reserve :658 / refund :671，无 await 穿插）。
- 模块导入即完成路由注册副作用（`@router.get` 装饰器）；mid-file imports（:1271-1272）在 import 期执行，无循环依赖（traffic.py 不反向 import routes）。

## 错误路径（全部构造点）

### 本文件直接构造

| 位置 | 状态码 / code | 触发 |
|---|---|---|
| :278-284 `_busy_response`（调用点 :945, :1141, :1249, :1613） | 503 `transform_busy`（+ body `retry_after` + 头 `Retry-After: 2`） | admission absorb 预算耗尽 / `TransformBusy` |
| :315 | 400 `directory_not_allowed` | query 与 `X-Opencode-Directory` 头冲突（rstrip("/") 归一比较） |
| :840-844（lease）/:1024-1028（direct） | 413 `response_too_large`（limit=max_response_bytes） | list body 超 cap（共享 flight 截断同样 413） |
| :1226-1230 | 413 `message_too_large`（limitBytes=max_message_bytes） | /full body 超全量 cap（含 direct 专用 GET fallback 后的真 413） |
| :1295 `_expand_shape_error`（worker 内多点调用：:1308/:1313/:1319/:1321/:1339/:1349/:1354/:1361/:1382/:1407/:1421/:1439/:1441/:1456） | 502 `upstream_invalid_shape` | parsed-but-malformed（parts 非 list、part 无 id、重复 id、嵌套字段类型不符等） |
| :1326-1328 | 404 `expand_target_not_found`（reason=`part_missing`） | partID 不在消息 parts |
| :1510-1513（worker 内） | 400 `expand_category_mismatch`（expectedTypes） | part.type 不适用该 category |
| :1525-1527（worker 内） | 413 `expand_fragment_too_large`（limitBytes=max_expand_response_bytes） | 序列化后 fragment 超 cap（gzip 前 identity 判定） |
| :1540-1544 | 400 `invalid_expand_category`（validCategories=12 类全表） | category 不在白名单 |
| :1549-1554 / :1556-1560 | 400 `expand_category_mismatch`（expectedLevel=message/part） | level/category 不匹配 |
| :1591-1595 | 413 `expand_source_too_large`（limitBytes=max_message_bytes） | expand 源 body 超全量 cap |

### 经由依赖模块的构造（本文件调用点）

| 调用点 | 来源 | 结果 |
|---|---|---|
| :334, :505 | `raise_upstream_unavailable`（httpx.RequestError） | 503 `upstream_unavailable` |
| :877, :1074, :1241, :1605 | `raise_upstream_unavailable`（JSONDecodeError/ValueError/TypeError/AttributeError；:1605 仅前两种） | 503 `upstream_unavailable`（上游坏 JSON / 非法形状） |
| :507-512, :796-801（经 `read_upstream_response` → `raise_upstream_status_code`，`sid` 传入） | upstream_errors.py:63-77 | **404+sid → 404 `session_not_found`**；其他 4xx → 502 `upstream_http_N`；5xx → 503 `upstream_unavailable`；mid-stream RequestError → 503 |
| :316（`validate_directory`，directory.py:23-53） | 400 `invalid_directory` | `..`/`.` 段、NUL、控制字符、>4096 |

### 非 coded 错误面

- `limit` 越界（:952 `Query(ge=1, le=200)`）→ **FastAPI 原生 422**（`{"detail":[...]}` 形状，非 `{"code":...}`）。**本文件不存在 `invalid_cursor` / `request_too_large` 构造点**（`before` 完全透传不校验）。
- `_CapExceeded`（:562 raise）与 `TransformBusy`（:1209, :1577 raise；:944, :1140, :1248, :1612 catch）为内部异常，不直接上 wire。

## 疑问点（draft 种子，宁多勿漏；按可疑度排序）

1. **merged 预算预留粒度 × 默认配置 → fan-out 实际退化为每页最多 1 条**（:649-672，config.py 默认 `max_message_bytes=32MiB` > `merged_max_bytes=8MiB`）。首个候选一次性 reserve `min(32MiB, 8MiB)=8MiB`（:655-658，第一个真 await 之前完成），`remaining` 归零；同批 gather 顺序调度的其余任务 `cap<=0` → `_DEGRADED`（:656-657）**且永不重试**（refund :666-671 只惠及尚未启动的任务）。`gather` 按创建序跑到首个挂起点是确定性语义 → 默认配置下每页恰好 inline 第一条 placeholder，其余 15 条（`merged_max_fulls_per_page=16`）静默降级，与 handler docstring（:966-970）和 `_merged_candidate_pairs` 语义不符。测试 `tests/test_messages_merged.py:250-258` 明确 pin `max_message_bytes=256KiB`（"reservations cannot exhaust merged_max_bytes before all 16 items start"）恰好绕开生产默认参数组合；而 :498/:539/:593 的预算测试也全用 `max_message_bytes=8000 < merged_max_bytes=10000`。**默认配置组合疑似无人测试覆盖**。
2. **/full 与 expand 的上游 404 映射为 `session_not_found`**（:507-512 `_dedicated_full_get` 以 `sid=sid` 调 `read_upstream_response` → `raise_upstream_status_code` 404+sid 无条件映射 `session_not_found`，upstream_errors.py:75-76）。消息不存在而 session 存在时（上游 `GET /session/{sid}/message/{mid}` 404），客户端收到 404 `session_not_found` — 语义错位（可能误导 ocdroid 清 session 缓存）；是否契约冻结需查 v3-contract §错误表。
3. **`_merge_fulls` 的 `asyncio.gather` 无 `return_exceptions`**（:674-676）：`_fetch_one` 只捕 `CodedHTTPException`（:664-665）；`singleflight` 注册表 shutdown 竞态、`RuntimeError`、`CancelledError` 等会逃出 gather → 整页 500/中断，与 :641-642 "merging must never fail the page" 的强声明不完全一致（该声明仅对结构化上游错误成立）。
4. **windfall 峰值内存自认超标**（docstring :619-637 层 2/层 3）：单飞 key 不含 cap（:567），merged 小 cap waiter 可 join direct-led 32MiB flight（或其 grace 结果），`gather` 结果持有共享 body 直到 :678-686 splice 排除 → 页面瞬态可持有 ~`max_message_bytes`（32MiB），远超 `merged_max_bytes + fanout×chunk ≈ 8.5MiB` 公式。已文档化，但 systemd `MemoryMax=384M` 下的实测峰值值得 draft 阶段核查（与 `max_transforms=1` 并发放大）。
5. **`response_rep_version(config, wire_view=3)` 硬编码 3**（:902-903, :1098-1099）与 `_expand_wire_view` 可返回 4（:854, :1050）不对称：REP_VERSION salt 固定 `wire=v3`（etag.py:85-88），而 identity bytes 可能含 `?v=4` href。ETag 正确性不受影响（bytes 变则 tag 变），但跨 view 的 rep-version 语义不一致 — 是有意（保持 v3 ETag 基线稳定）还是遗漏，需对照 v4-contract §14。
6. **merged 页的 ETag 是「本次获取结果指纹」而非「内容指纹」**（:683-686 windfall 跳过 + 单飞 grace 1s 复用 + 预算时序）：同一页面内容在不同并发环境下（是否恰有 direct /full 同 key flight）产生不同 identity/ETag。方向上是保守正确的（内容变→200），仅损失 304 命中率；但契约若声称 messages ETag 表征内容，merged 模式下该声明需限定。
7. **Vary 注释与实现错位**：:904-907 / :1100-1102 注释称 "directory Vary dimension is unconditional"，而 `etag.merged_vary`（etag.py:171-177）恒返回 `"Accept-Encoding"`、directory 维度已随 §5.7 退役。注释中 "unconditional" 实指「Vary 头的发射不依赖 ETag 开关」，易误导审计者/后续维护者以为 Vary 含 directory。
8. **`_parse_link_next_cursor` 按 `,` 切分 Link**（:246）：RFC 5988 允许引号串内逗号；注释 :240-242 自认依赖 "opencode cursors 不含逗号"。上游（或中间层）异常 Link 可错切 → cursor 解析失败 → None → fail-safe（无 nextCursor），影响面小但属于对上游形态的未防御假设。
9. **merged phase B 的并发面**：(a) fan-out 无 admission（:653-676，默认 `merged_fanout=8`）→ 同时最多 8 个上游 full GET + 直连 /full/expand 并发，是否受 `app.state.upstream` httpx client 连接上限约束需在 app.py/上游客户端卡片核对；(b) **lease 路径下 merged 请求在 fan-out + phase C admission 等待期间一直持有 list lease**（:837 `async with lease` 包住 :882 `_merge_fulls`）→ 拉长 `raw_fetch_max_bytes`（默认 64MiB，仅容 1 个 flight）的占用窗口，降低其他请求 coalescing 可用性。
10. **`thin_placeholder_` 前缀跨模块字符串复制**（:358-361 注释自认 skeleton.py 在当时写域外）：skeleton.py:618 构造 `f"thin_placeholder_{message_id}"`，本文件 :361 复制前缀做识别 — 隐式契约无共享常量；skeleton 侧改名则 merged 候选集静默为空（列表仍 200，仅 merged 功能失效）。
11. **expand 400 的双时序**：level mismatch 400 在 admission 前（:1548-1560），type mismatch 400 在 admission + 上游 GET 之后（worker :1509-1513）— 同码不同成本；契约 §3.1 step 6 如此冻结（"pool-full 503 precedes every part-level 40x" 注释 :1571-1572），但意味着 type mismatch 每次消耗一次上游 GET + transform slot（可否在 list/skeleton 元数据预判为 draft 议题）。
12. **expand offload 异常捕获面窄**（:1603 仅 `(orjson.JSONDecodeError, ValueError)`，对比 :876/:1073/:1240 的 4 种）：extractors 均 isinstance 防御、`orjson.dumps` 对 NaN/Infinity 输出 null（已实测 orjson 3.11.9），实际暴露面小，但任何意外 `TypeError` 会以裸 500 上 wire 而非 503。
13. **mid-file imports**（:1271-1272）：`from ..traffic import ...` 位于文件中部 expand 段落（有意将冻结表贴着唯一消费者），非顶部 — 风格异常，工具链（import 排序/lint）与读者易漏；无循环依赖（traffic 不 import routes）。
14. **`_resolve_messages_directory` 为 async 但无 await**（:287-316，纯历史形态）；且 header-only directory（只发 `X-Opencode-Directory` 不发 query）被静默忽略不转发（:294-296 自认 "unchanged behaviour"）— 只发头的客户端会静默失去 directory 语义。
15. **`_created_sort_key` 畸形行默认 0 排最前**（:77-92）：缺 `info.time.created` 的上游行会跃居所有正常消息之前，改变可见顺序（文档化的防御性排序的副作用）；0 亦为合法 epoch-ms 值，不可区分。
16. **直连 413 `response_too_large` 不携带 cursor**（:1024-1028 early return）：超大列表页既拿不到 body 也拿不到 `nextCursor`，客户端无法降 limit 重试翻页 — 契约是否提供 recovery 路径可查。
17. **`_fetch_full_shared` 的 re-lead 逻辑与 singleflight entry-drop 语义强耦合**（:552-588）：正确性依赖「factory raise → entry 被 drop（不 grace 保留 None）」与「joiner 收到同一异常」两点（`_CapExceeded` docstring :370-380 声明），需在 singleflight 卡片交叉验证 `SingleFlight.fetch` 的异常传播路径；3 次上限与 direct fallback（:584-587）的「worst case 多一次 GET」已被注释接受。
18. **`limit` 校验错误为 FastAPI 原生 422**（:952）：非 `{"code":...}` 形状，与契约 §11 错误体约定的一致性（以及 ocdroid 是否依赖 coded body）值得核对；另 `_canonical_list_query` 让被忽略的 `mode` 值（如 `mode=full`）参与 flight key（:761-766，A2-C2 保守分 key）— 有意但降低命中率。

## 附：与测试的关键对齐点（供 draft 复核）

- `tests/test_messages_merged.py:140-142` 测试基线 settings 与生产默认一致（32MiB/8/16/8MiB），但具体预算测试全部 override（见疑问点 1）。
- `tests/test_messages_merged.py:750-800`：merged 小 cap flight + direct join 的 re-lead/降级语义端到端锁定（`full_calls==2`、direct 200 非 413）。
- `tests/test_readiness_gating_integration.py:256-266`：`messages.expand.v4` 门控开/关 → href `?v=` 折回行为。
