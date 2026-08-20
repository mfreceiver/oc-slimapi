# E5 — 关键场景数据流追踪（12 场景端到端）

> 审计 Phase 1 / E5。只读产物；证据格式 `路径:行号`（相对仓库根，src 侧省略 `src/oc_slimapi/` 前缀时均指 `src/oc_slimapi/` 下同名文件——本文一律写全相对路径去掉 `src/oc_slimapi/`，即 `messages.py:949` = `src/oc_slimapi/routes/messages.py:949`；跨模块以文件名区分：`selector.py`=`src/oc_slimapi/selector.py`，`transform.py`=`src/oc_slimapi/transform.py`，`singleflight.py`=`src/oc_slimapi/singleflight.py`，`lifecycle.py`=`src/oc_slimapi/dbaux/lifecycle.py`，`subscriber.py`=`src/oc_slimapi/sse/tokenstream/subscriber.py`，`hub.py`(token)=`src/oc_slimapi/sse/tokenstream/hub.py`，`global_hub.py`=`src/oc_slimapi/sse/global_hub.py`，`registry.py`=`src/oc_slimapi/sse/registry.py`，`replay_log.py`=`src/oc_slimapi/sse/replay_log.py`，`replay_wire.py`=`src/oc_slimapi/sse/replay_wire.py`，`hub_types.py`=`src/oc_slimapi/sse/hub_types.py`，`traffic_accounting.py`=`src/oc_slimapi/middleware/traffic_accounting.py`，`_catalog_common.py`/`_read_passthrough.py`/`read_groups.py`/`sessions.py`/`messages.py`/`events.py`/`token_stream.py`/`write_groups.py`/`metrics.py`=`src/oc_slimapi/routes/` 下同名）。
>
> 行号基于 2026-08-20 工作树（v4 后、readiness 全亮态：`readiness.py:93` `SATISFIED = frozenset(REQUIRED)`，10/10 feature 满足 → §16.2 POST 等效族已激活、§14/§12/§13/§15 修订面全部生效）。
>
> **公共中间件栈**（`app.py:747/753/755` add_middleware 逆序包裹）：`RequestIdMiddleware`（最外，`middleware/request_id.py:78`）→ `TrafficAccountingMiddleware`（`traffic_accounting.py:161`）→ `SlimapiSelectorMiddleware`（`selector.py:510`）→ FastAPI 路由表（`app.py:760`）→ catch-all（`app.py:762` `install_proxy`）。异常渲染：`CodedHTTPException` → `errors.py:44` `coded_exception_handler`（`{"code":…,**fields}` + gzip 协商；仅此一类被注册，`errors.py:55-63`）。

---

## 场景 1 — `GET /slimapi/messages/{sid}?v=3`（skeleton 投影 + ETag/304 + 单飞；含 limit 422）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `RequestIdMiddleware.__call__@middleware/request_id.py:78` | 生成/透传 `X-Request-ID` → `scope["state"]["request_id"]`（access 行 requestId 源） |
| 2 | `TrafficAccountingMiddleware.__call__@traffic_accounting.py:161` → `bucketize@traffic.py:91` | `/slimapi/messages/{sid}` 先过 `_expand_tail@traffic.py:203`（非 expand）→ `bucket="messages"`；非 SSE |
| 3 | `SlimapiSelectorMiddleware.__call__@selector.py:510` → `_is_slimapi_path@versioning.py:29` | http + /slimapi 路径 → 进入选择器（非 slimapi 在 `selector.py:516-522` zero-touch） |
| 4 | `_directory_form@selector.py:336` | 命中消费集 `^/slimapi/messages/[^/]+$@selector.py:147` → directoryForm=query/absent 记入 scope state（§9.1） |
| 5 | `_collect_v_values@selector.py:424` + 词法/取值校验 `selector.py:558/567` | `v=3`：`_SELECTOR_LEXICAL_RE@selector.py:127` 通过；`3 ∈ SUPPORTED_WIRE_VERSIONS@selector.py:135`（= `ACCEPTED_CLIENT_VERSIONS (3,4)@versioning.py:44`） |
| 6 | `_stash@selector.py:579` | `selectorResult=v3, wire="3"`（路由经 `wire_view_from_scope@selector.py:368` 读回，缺省 3） |
| 7 | §16 method-405 检查 `selector.py:592` | wire≠4 → 跳过（本路径无 POST 面） |
| 8 | `_consume_directory@selector.py:611`（本体 `selector.py:636-717`） | query-only 单值 → `validate_directory` + stash `V3_DIRECTORY_STATE_KEY@selector.py:707-709` + 字节保真剥离 directory 对（`_strip_query_keys@selector.py:478`）；多值异归一→400 `invalid_directory_selector`（`selector.py:679-681`）；header 出现→400 `directory_header_retired`（`selector.py:702-704`） |
| 9 | `_forward@selector.py:719` → `_strip_v_segments@selector.py:497` | 下游 query 无 `v`；其余字节原样 |
| 10 | `messages@messages.py:949`（`limit: Query(40, ge=1, le=200)@messages.py:952`） | **limit 422 分支**：`limit=0/201/abc` → FastAPI `RequestValidationError` → 默认 422 `{"detail":[…]}`（无自定义 RequestValidationError handler，`errors.py:55-63` 仅挂 CodedHTTPException）——发生在路由体之前，ETag/上游一概不触 |
| 11 | `_resolve_messages_directory@messages.py:287` → `resolve_route_directory@selector.py:406` | stash 替代已剥离的 query 值；query+header 归一化冲突→400 `directory_not_allowed@messages.py:314` |
| 12 | coalesce 判定 `messages.py:994-995`（registry ∧ `coalesce_enabled`）→ `_messages_via_lease@messages.py:808` | **join-first 单飞**：key=`_messages_list_key@messages.py:769`（含 `id(upstream)`、sid、directory、canonical query@`messages.py:752`——`mode` 参与 key 使 merged/默认页隔离）；`LeasedSingleFlight.fetch_or_bypass@singleflight.py:393`（字节预算 `raw_fetch_max_bytes`；满→`None`→落入直连路径 `messages.py:1002-1139`，链路等价） |
| 12a | 工厂 `_fetch_list_raw@messages.py:784` → `_stream_upstream@messages.py:319` | ONE 上游 `GET /session/{sid}/message?limit=N[&before=…]`（before 为 opencode 不透明 base64url cursor 原样转发，`messages.py:980-987`） |
| 12b | `read_upstream_response@_catalog_common.py:97` → `read_with_cap@transform.py:143` | 4xx→drain+`raise_upstream_status_code`（sid 在场→404 映射 `session_not_found`）；2xx→64KiB chunk 流式 cap-read（cap=`max_response_bytes`=64MiB，`config.py:365`），`on_read=stash_up_in@traffic.py:284` 逐 chunk 记 upIn；超 cap→`(None,total)`→413 `response_too_large@messages.py:840-844` |
| 12c | `_parse_link_next_cursor@messages.py:221` → `_extract_before_verbatim@messages.py:193` | 上游 `Link: <…>; rel="next"` → 不透明 cursor **字节原样**提取（禁 percent-decode，防 cursor 损坏）；无→None |
| 13 | `SkeletonLimits` 构造 `messages.py:845-849`；`wire_view=_expand_wire_view@messages.py:58` | limits=per-app config（`skeleton_inline_output_*` + fingerprint 开关）；v=3→wire_view=3（§3.3 门 `messages.expand.v4∈SATISFIED` 只影响 v=4 的 href） |
| 14 | `async with pool@transform.py:253`（`acquire@transform.py:220`） | **准入先于投影**（lease 路径 GET 已出槽外，offload 前重新占位）；等待 `transform_wait_seconds`=2s（`config.py:364`）超时→`TransformBusy@transform.py:75` |
| 15 | `pool.offload(_project_list_sorted_and_pack)@messages.py:870-875` → `_parse_sort_project@messages.py:95` | worker 线程：`orjson.loads@120`；**非 list/非 dict 元素→ValueError@messages.py:121-129**（路由映射 503 `upstream_unavailable@messages.py:876-877`）；`parsed.sort(key=_created_sort_key@messages.py:77)` 强制 `info.time.created` ASC；`skeleton_messages@skeleton.py:637` 投影（parts 白名单/inline 预算/`expandRefs` href 带 `?v=3`/`contentFingerprint`）；`orjson.dumps@messages.py:165` |
| 16 | `messages_envelope_bytes@envelope.py:21` | identity 字节拼 `{"items":<v2数组字节>,"nextCursor":<string|null>}`——**envelope 字节即 ETag 规范输入**；`X-Next-Cursor` 头已退役（cursor 进 envelope） |
| 17 | `response_rep_version@etag.py:91`（wire_view=3） | rep=None（`etag_enabled=false`）→ 跳过判定，字节同 pre-ETag；否则 `representation_version@etag.py:49`（scheme+skeleton-v1+两 inline 限额+fingerprint 开关+`wire=v3` 域标 `etag.py:87`） |
| 18 | `judge_conditional@etag.py:179` | **压缩前单候选 304 判定**：identity-only（无 gzip 或 len<64B `MIN_GZIP_BYTES@gzip_util.py:25`）→ 精确判 identity 强 tag；gzip-capable → 只判 gzip 弱 tag（identity tag→保守 200，C5）；`*`→返回 `"*"` 由路由压一次并回真实 coding tag（`messages.py:913-928`） |
| 19 | 304：`not_modified_response@etag.py:246` | 304 = ETag+Vary+`Cache-Control: no-store`，无 body/无 aux 头（零压缩路径） |
| 20 | 200：`compress_if_beneficial@gzip_util.py:75` + `compute_etag@etag.py:101` | gzip level6 且更小才压；ETag=sha256(rep+NUL+coding+NUL+identity)（gzip→`W/"…"`）；`Vary=merged_vary("Accept-Encoding")@etag.py:171`（单值）；`Cache-Control: no-store` |
| 21 | `TransformBusy` 兜底 → `_busy_response@messages.py:268` | 503 `transform_busy` + `Retry-After: 2`（`TRANSFORM_RETRY_AFTER_SECONDS@messages.py:44`），gzip 协商 body |
| 22 | `_record@traffic_accounting.py:249` → `write_access_log@access_log.py:267` | 请求尾：access 行（bucket/status/durationMs/downIn/downOut/upIn=stash 聚合/upOut/wireVersion="3"/selectorResult="v3"/directoryForm）+ ledger `record_downstream/record_upstream@traffic_accounting.py:370-417` |

**关键发现**：整条链有两条互为镜像的等价路径（lease 单飞共享**仅 raw GET+cursor**，投影/序列化/ETag 恒 per-caller），且 422（limit 域）是唯一不经选择器之后任何 sidecar 语义、直接由 FastAPI 参数校验产生的错误形态（body 为默认 `{"detail":…}` 而非 coded 形）。

---

## 场景 2 — 同上 `&mode=merged`（placeholder 填槽 + fanout + 预算；32MiB vs 8MiB 组合）

前置步骤 1-13 与场景 1 相同（`mode` 参与 coalesce key，`messages.py:764-765`；`merged_mode = mode=="merged"@messages.py:990` 字面量精确匹配，其它值静默忽略→回落场景 1）。

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| A1 | `pool.offload(_parse_sort_project)@messages.py:864-867`（lease）/`1052-1061`（直连） | **阶段 A**：只 parse+sort+投影（返回 dict 列表，**不打包**）；异常映射同场景 1 |
| B0 | `_merge_fulls@messages.py:591` → `_merged_candidate_pairs@messages.py:454` | **阶段 B（不占 pool slot，oracle §C-2）**：候选 = `_placeholder_pairs@messages.py:390`（parts 含 `thin_placeholder_{mid}` 标记@`messages.py:361`，页序）截 `merged_max_fulls_per_page`=16（`config.py:634`）+ `_expand_ref_pairs@messages.py:425`（**part 级** `expandRefs`；`info.expandRefs`（diffs，~105KB）永不入选）按 mid 交集去重填剩余槽 |
| B1 | `asyncio.Semaphore(merged_fanout=8@config.py:631)`；`remaining=[merged_max_bytes=8MiB@config.py:637]@messages.py:650-651` | per-request 预算池（可变单元共享） |
| B2 | `_fetch_one@messages.py:653` | `cap = min(max_message_bytes=32MiB@config.py:359, remaining)@655`；`cap<=0`→`_DEGRADED` **不发任何上游请求**（`messages.py:656-657`）；`remaining -= cap` 预留（串行点，无 await，`messages.py:658`） |
| B3 | `_fetch_full_shared(cap=cap)@messages.py:517` → `fulls.fetch@singleflight.py:372` | key=`full_fetch_key@singleflight.py:243`（`("full", id(pool), sid, mid, directory)`）——**与并发 direct /full 同 key 去重；key 不含 cap** |
| B3a | 工厂 `_upstream_get@messages.py:555` → `_dedicated_full_get@messages.py:484` | ONE `GET /session/{sid}/message/{mid}` + cap-read；读截断（None）→ `_CapExceeded(flight_cap)@messages.py:562` → **singleflight 条目 drop**（不留截断体毒化 joiner） |
| B3b | 重试循环 `messages.py:552-572` | join 到更小 cap flight 且被 drop → 以自身 cap re-lead（≤3 次）；`exc.cap>=full_cap` → 终态 None；**3 次耗尽且 `cap is None`（direct /full）→ 专属兜底 GET `messages.py:584-588`；merged（显式小 cap）→ 返回 None 走预算降级 `messages.py:589`** |
| B4 | per-item 异常→`_DEGRADED@messages.py:664-665`；finally 退款 `remaining += max(0, cap-len(body))@messages.py:666-671` | 结构化上游错误只降级该项，页永不失败；退款亦串行点 |
| B5 | `asyncio.gather@messages.py:674` | 全部 fanout 结果汇合 |
| B6 | splice 前过滤 `messages.py:678-686` | `_DEGRADED`/None 跳过；**windfall（join 到 32MiB direct-led flight 的完整 body）若使累计 > `merged_max_bytes` 也丢弃**——响应内联总量硬 ≤8MiB；页内瞬态峰值不受此保证（可持 max_message_bytes 级共享体，`messages.py:621-637` 注释三层边界明示） |
| C1 | `async with pool` → `pool.offload(_merge_fulls_and_pack)@messages.py:688-693` | **阶段 C（重新准入，同款 busy 语义）** |
| C2 | `_merge_fulls_and_pack@messages.py:696` | 每项：`orjson.loads@728`（坏 JSON→该项降级）；`strip_diagnostics_message@skeleton.py:676` 取 `parts` 整体替换 `projected[index]["parts"]@735`（保留 LIST 的 info——排序键不变）；`recompute_fingerprint@skeleton.py:62@messages.py:736-737`（开关开时，skeleton 期指纹已失效）；`orjson.dumps@738` |
| C3 | 尾部 `messages_envelope_bytes` + ETag/304/200 | 与场景 1 步骤 16-20 逐字节同链（`messages.py:887-945`） |

**关键发现（32MiB × 8MiB 组合行为）**：`max_message_bytes`(32MiB) 是**每次上游读**的硬上限与 direct /full 的专属 cap；`merged_max_bytes`(8MiB) 是 merged 页的 **true-fetch 预算**（reserve/refund，串行点不变量：预留和 ≤ 预算；单读可过一个 64KiB chunk 过冲）+ **响应内联后验硬上限**；二者经由「不含 cap 的 singleflight key」耦合——merged 小 cap waiter 可 windfall 到 32MiB direct-led 共享体（页内瞬态超 8.5MiB 公式），正确性由 B6 的 splice 前累计检查与 B3b 的 direct 兜底 GET 双向兜住。

---

## 场景 3 — `GET /slimapi/messages/{sid}/full/{mid}`（single-flight + transform 池 + absorb + strip diagnostics）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1-9 | 公共栈（同场景 1；消费集 `^/slimapi/messages/[^/]+/full/[^/]+$@selector.py:148`） | bucket="messages" |
| 2 | `message@messages.py:1145` | 无 `mode`/`known.*` 语义（lite-v2 已冻结为纯 on-demand expand，无 304 短路） |
| 3 | absorb 循环 `messages.py:1205-1214` | `deadline = now + transform_absorb_budget_seconds(2.5s, config.py:640)`；每轮 `pool.acquire(min(transform_wait_seconds=2, remaining))@1211`——**逐次收窄**，最坏累计等待 ≤ 预算；预算尽→`raise TransformBusy@1208-1209`（不变量：503 transform_busy 前绝无上游请求） |
| 4 | `_fetch_full_shared(request,pool,sid,mid,directory)@messages.py:1224`（`cap=None`） | `full_cap = max_message_bytes@messages.py:544-547`；`fulls.fetch` join-or-lead（同场景 2 B3/B3b 语义：join 小 cap 截断→re-lead≤3→**兜底专属 GET 保证 direct /full 永不因 merged 预算假 413**） |
| 5 | `body is None` → 413 `message_too_large`（`limitBytes=max_message_bytes`）`messages.py:1226-1230` | 真 32MiB 截断 |
| 6 | `pool.offload(strip_diagnostics_and_pack)@messages.py:1235-1239` → `strip_diagnostics_and_pack@transform.py:104` | worker：`orjson.loads@128`；**非 dict→ValueError@transform.py:129-135**（空 body/垃圾/数组同判）；`strip_diagnostics_message@skeleton.py:676` 原地剥唯一字段 `state.metadata.diagnostics`（LSP map，ocdroid 不消费）；`_pack_json@transform.py:79`（orjson.dumps + compress_if_beneficial + `merge_directory_vary=True`→Vary 单值） |
| 7 | `(orjson.JSONDecodeError, ValueError, TypeError, AttributeError)` → `raise_upstream_unavailable@messages.py:1240-1241` | 坏上游 200 → 结构化 503，非裸 500 |
| 8 | `finally: pool.release()@messages.py:1242-1243` | 与 acquire 严格配对 |
| 9 | 200：`Response(encoded, headers={Cache-Control:no-store, **extra})@messages.py:1244-1247` | extra=Vary(+Content-Encoding)；无 ETag（/full 无 validator 面） |
| 10 | `TransformBusy` → `_busy_response@messages.py:1248-1249` | 503 + Retry-After:2 |

**关键发现**：/full 的准入-GET-变换三段中，准入采用「预算吸收循环」而非单次等待——2.5s 总预算内反复窄重试，保证 503 只在真饱和且预算耗尽时出现，且失败路径零上游放大（GET 仅在准入成功后发生一次）。

---

## 场景 4 — `GET /slimapi/messages/{sid}/expand/{category}/{mid}[/{partID}]`（§4b 求值序全链）

入口分叉：`expand_message_fragment@messages.py:1616`（message 级，partID=None）/ `expand_part_fragment@messages.py:1631` → 统一 `_expand_fragment@messages.py:1531`。消费集两个 pattern `selector.py:152-153`；bucket="messages.expand"（`_expand_tail@traffic.py:203` 先于 "messages" 判定，`traffic.py:122`）。

§4b 严格求值序（400→admission→单飞→cap→decode→locate→extract→cap）：

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| ① | category 白名单 `messages.py:1538-1544` | `category ∉ _EXPAND_CATEGORIES_SET`（`traffic.py:64`；12 类唯一源 `traffic.py:50-63`，路由**不得私持副本**）→ 400 `invalid_expand_category` + `validCategories`（12 类全列） |
| ② | level 匹配 `messages.py:1547-1560` | `info_summary_diffs`（唯一 message 级，`_EXPAND_MESSAGE_LEVEL_CATEGORIES@messages.py:1275`）带 partID→400 `expectedLevel=message`；part 级（其余 11 类）无 partID→400 `expectedLevel=part` |
| ②b | `_resolve_messages_directory@messages.py:1562` | directory 解析/冲突守卫同场景 1 步骤 11 |
| — | `fragment_limit = max_expand_response_bytes@messages.py:1567-1569` | 默认 8MiB（`config.py:372`；getattr 兜底同值，lane C 遗留） |
| ③ | transform 准入 absorb 循环 `messages.py:1573-1582` | 与场景 3 步骤 3 同款——**pool 满 503 transform_busy 先于一切 part 级 40x/413** |
| ④ | `_fetch_full_shared@messages.py:1586` | 与 /full **同一 singleflight key** 去重（§3.5）；leader/join 语义同场景 3 步骤 4 |
| ④c | `body is None` → 413 `expand_source_too_large`（`limitBytes=max_message_bytes`）`messages.py:1591-1595` | 源体超 32MiB 在 **JSON decode 之前**截断（cap-read 先行——超大且畸形的 body 仍 413，R4-M1） |
| ④d | `pool.offload(_expand_fragment_worker)@messages.py:1598-1602` → `_expand_fragment_worker@messages.py:1487` | worker 线程执行 ④d-⑦；`(JSONDecodeError, ValueError)` → 503 `upstream_unavailable@messages.py:1603-1605`（decode 失败/顶层非 dict，`messages.py:1499-1503`） |
| ⑤ | `_expand_locate_part@messages.py:1298` | parts 非 list→502（`_expand_shape_error@messages.py:1293`）；非 dict 元素/不可用 id（缺/非 str/空）/重复 partID→502 `upstream_invalid_shape@1311-1322`；良构列表不含 partID→404 `expand_target_not_found, reason=part_missing@1326-1328` |
| ⑥ | 类型适配 `messages.py:1507-1513`（表 `_EXPAND_APPLICABLE_TYPES@messages.py:1278-1290`） | `part.type ∉ 适用集`（如 part_text 遇 reasoning part）→ 400 `expand_category_mismatch` + `expectedTypes` |
| ⑦ | 提取 `_EXPAND_EXTRACTORS[category]@messages.py:1514/1471-1484` | 每 category 冻结提取器：如 `_extract_part_state_metadata_full@messages.py:1411`（剥 `diagnostics` 键@1422-1424，同 /full 口径）、`_extract_part_state_attachments@1427`（元素级 object 校验→502）、`_extract_compaction_full@1465`（白名单式剔除注入的 `expandRefs`）；嵌套类型规则：missing/null→null 键，类型错→502（`_expand_str_field@1332` 等） |
| ⑦b | envelope 组装 `messages.py:1515-1522` | `{category, messageID, data[, partID]}`；`identity=orjson.dumps` |
| cap | fragment cap `messages.py:1523-1527` | `len(identity) > limit` → 413 `expand_fragment_too_large`（`limitBytes`=8MiB）——**gzip 之前**的序列化 cap |
| ⑧ | `compress_if_beneficial@messages.py:1528` | identity/gzip 协商 |
| 9 | 200 `Response(headers={Cache-Control:no-store, **extra})@messages.py:1608-1611`；`TransformBusy→_busy_response@1612-1613` | 503+Retry-After:2 |
| 10 | 记账加层：`_record@traffic_accounting.py:391-397` → `expand_category_from_path@traffic.py:225`（非法 category 折叠 `"invalid"` 桶 `traffic.py:68-81`）→ `ledger.record_expand(category,status,resp_bytes)` | expand 专属 per-category|status 维度（不占新 bucket） |

**关键发现**：求值序把「客户端可修复的 400」放在任何资源占用（准入/网络）之前、把「上游体量 413」放在任何解析之前、把「上游结构 502/404」放在 worker 内——即同一请求的失败归类严格按 §4b 冻结顺序短路，且 503 transform_busy 在 ①②（纯内存判断）之后、④（网络）之前才可能出现。

---

## 场景 5 — `GET /slimapi/sessions?v=3` vs `?v=4`（v3: 上游+coalesce+envelope+ETag；v4: dbaux SQL+§4.2 降级矩阵+cursor+无/有 ETag）

### 5a. `?v=4`（`sessions@sessions.py:678` → wire≥4 fork `sessions.py:693` → `_sessions_v4@sessions.py:371`）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 0 | selector：`^/slimapi/sessions$` 在 v4 退休表（`_DIRECTORY_V4_RETIRED_PATTERNS@selector.py:197`） | v4 + 任何 directory 输入（query 键存在或 header 任何形态）→ 400 `directory_retired_in_v4`（统一体 `selector.py:205-212`，优先于 v3 阶梯，`selector.py:668-673`）；无输入→全局 facade 放行 |
| ④ | 参数版本 `sessions.py:383-402`（presence-based `_raw_query_keys@sessions.py:282`） | `roots`/`start` 出现→422 `param_version_mismatch`（hint: v4 用 parent 轴）；`limit>500`（`_V4_LIMIT_MAX@sessions.py:273`）→422；`archived ∉ {omit,only,all}`→422；`parent` 空→422 |
| ⑤ | cursor 校验 `sessions.py:409-427`（**优先于 503**，§8.3） | `build_fingerprint@dbaux/cursor.py:126`（archived/parent 归一 + `search_hash@88`（trim 后 sha256-16hex；缺席=哨兵 `""`）+ `allowlist_rev@100`）；`decode_cursor@cursor.py:165` 语法错→`InvalidCursorError@58`→400 `invalid_cursor`；`fingerprint_mismatch@148`→400（filter 上下文不一致） |
| ⑥ | dbaux 分支 `sessions.py:430-487` | `dbaux.status().available@lifecycle.py:691` → `fetch_sessions_page@dbaux/projection.py:332` |
| ⑥a | `build_sessions_query@dbaux/projection.py:135` | 全参数化 SQL：archived 谓词@175-179；parent 四态@181-187；search `(? IS NULL OR s.title LIKE ? ESCAPE '\')` 字面子串（`escape_like@projection.py:104` 仅转义 `%_\`）@189-197；allowlist 子树二进制前缀（`= ? OR substr(1,?)=?`，根 `/` 特例）@199-215；cursor keyset 下推 `(t<? OR (t=? AND s.id<?))`@217-227；`ORDER BY s.time_updated DESC, s.id DESC` + `LIMIT ?+1`@234-240 |
| ⑥b | `source.query@lifecycle.py:429` → `_run_query@lifecycle.py:465` | 专属单 worker：`BEGIN`(deferred snapshot)→execute→fetchall→`COMMIT`；finally `ROLLBACK`(若 in_transaction)+游标 close+`breaker.record`@472-489（快照内完成投影+complete 判定）；错误分类 `classify_sqlite_error@lifecycle.py:114`：busy→原样上抛（不禁用）@448-454；schema/io/cantinit/programming→`_disable@459`+`AuxiliaryUnavailableError@460` |
| ⑥c | 路由边界：`AuxiliaryUnavailableError`→降级矩阵 `sessions.py:443-444`；`sqlite3.Error`→`_fail_closed_503@sessions.py:309-317/445-450` | fail-closed 503 `auxiliary_unavailable` + `Retry-After: 30`（`sessions.py:274/299-306`）+ `request.state.slimapi_degraded_503=True@316`（access 行 degraded503 标记源） |
| ⑥d | `rows_to_records@dbaux/projection.py:248` | 行级容忍：缺 id 跳行@260-262；JSON 列（`JSON_COLUMNS@75-77` 含 model）解析失败跳行+warning@263-292；model 形状门（非对象 JSON 也跳）@278-284 |
| ⑥e | §13 门 `_v4_session_single_revision_active@sessions.py:576`（`session.single.projection.v4∈SATISFIED`→开） | 开：逐行 `canonical_session_skeleton_v4@skeleton.py:1017`（required nullable 恒发+degraded 标记）；`None`（不可表示）→整响应 fail-closed 503 `sessions.py:458-461`；关：4.0.0 形态 `project_rows_to_v4_skeletons@skeleton.py:827` |
| ⑥f | nextCursor `sessions.py:467-472` | `not page.complete ∧ page.anchor is not None`（`_window_anchor@projection.py:315`——坏行不丢锚点）→ `encode_cursor@cursor.py:158`（base64url(JSON {t,i,f}) 无 padding）；`request.state.slimapi_sessions_source="db"@473` |
| ⑥g | envelope `sessions_envelope_v4@envelope.py:46` | 键序 `(items,nextCursor,complete[,degraded])`；修订面 `degraded=any(item.degraded)@sessions.py:479-486`（门控关→稀疏形态逐字节保留） |
| ⑥h | `_v4_json_response@sessions.py:598` | **无/有 ETag 分叉**：`representation.vary.v4` 门控关→4.0.0 行为：无 ETag 且**摘 Vary**（`sessions.py:618-623`）；开→`Vary: Accept-Encoding` 恒发 + `ETag=compute_etag(identity, coding, rep(wire_view=4))@626-640` + `conditional_304@etag.py:266` 弱比较→304（头=ETag+Vary+no-store）；`etag_enabled=false`→无 validator 但 Vary/no-store 仍发 `sessions.py:628-633` |
| ⑦ | 降级矩阵（DB 不可用/竞态）`sessions.py:488-568` | 顺序 fail-closed：allowlist 非空→503@489-491；`has_wildcard(normalized)@projection.py:113`（含 `%_\`）→503@492-494；cursor 在场→503@495-497；非 Class A（archived=only 或 parent∉{all,none}）→503@498-502 |
| ⑦a | Class A HTTP 回退 `sessions.py:504-568` | 上游 `GET /session`（params：limit；`parent=none→roots=true`@507；search 传 **normalized**@509）；pool 准入+cap-read（>64MiB→413@529-533）；parse 守卫→503@534-541；§13 开→`_project_http_sessions_v4_canonical@659`（`native_session_to_record@skeleton.py:909` 归一化→唯一 canonical projector，`fallback=True` 恒 degraded；不可表示→503@673）/关→`_project_http_sessions_v4@654`（`_http_session_to_v4@320`，`project:null@353`）；`complete=len<limit`（best-effort）；nextCursor 恒 null；`degraded:true`；`sessions_source="http"@560`（access 行 sessionsSource 源） |

### 5b. `?v=3`

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| v3×v4-only 参数 | `sessions.py:700-705` | `archived/parent/cursor` 任何出现（presence）→ 422 `param_version_mismatch` |
| directory | `resolve_route_directory@sessions.py:708-712` | 消费集 `^/slimapi/sessions$@selector.py:154`——query-only 单值被 selector 消费，上游仅走 `X-Opencode-Directory` 头（§5.2 终态） |
| coalesce | `_sessions_via_lease@sessions.py:141` | key `("sessions-list", id(upstream), directory, canonical query)@155-162`；工厂 `_fetch_sessions_raw@68`（GET /session + cap-read）；body None→413 `response_too_large@167-171`；lease 内 per-caller：准入+parse+守卫（非 list/元素非 dict→503 `sessions.py:184-192`）+offload `_project_sessions@801`（`skeleton_session@skeleton.py:734` 白名单） |
| 直连 | `sessions.py:737-791` | admission 先行@738；同款 cap/parse/守卫/offload |
| 尾部 | `_finalize_sessions_response@sessions.py:92` | `complete = len(sessions) < limit@115`；payload=`sessions_envelope_payload@envelope.py:32`（`{"items":[…],"complete":bool}`）；rep=None→原样 json_response+Vary@119-124；有 rep：identity=orjson.dumps@125→etag（coding 派生弱/强）@126-127→`conditional_304@etag.py:266`→304 或 200(ETag+Vary)@129-138；`X-Complete` 头永不发（envelope 承载） |

**关键发现**：v4/v3 是同一 URL 上的两条完全异构管线（dbaux SQL+keyset cursor+canonical 投影 vs 上游 HTTP+skeleton 投影+信封），分叉点在路由入口一行（`sessions.py:693`）；v4 的 ETag 面本身还有第二层门控（`representation.vary.v4`），门控关闭时 v4 响应连 Vary 都没有——与 v3 的恒 Vary 形成不对称。

---

## 场景 6 — `GET /slimapi/config/providers?v=3` vs `?v=4`（透传 vs §12 投影 ⑥-⑫ 全链 + 限额 + 指纹 providers-projection-v2）

入口：`config_providers@read_groups.py:396`；消费集 `^/slimapi/config/providers$@selector.py:176`；bucket="providers"（`traffic.py:151`）。

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 0 | `_resolve@read_groups.py:93` → stash/validate | directory 解析（v3 通道） |
| 分叉 | `read_groups.py:403-409` | `wire_view==4 ∧ _V4_PROVIDERS_REVISION_ACTIVE()@read_groups.py:382`（`providers.redacted.v4∈SATISFIED`）→ §12 管线；否则（**v3 或门控关的 v4**）→ v3 透传 |

### 6a. v3 透传（`read_passthrough_get@_read_passthrough.py:157`，`project=None`）

| 步骤 | 函数@file:line | 说明 |
|---|---|---|
| v3-1 | `_raw_upstream_url@_read_passthrough.py:103` | 上游 URL=post-selector 原样 query（再幂等剥 v）；`transforms=None@191-192` → **无 pool 准入、无 transform_busy** |
| v3-2 | `stream_upstream@_catalog_common.py:55` | GET /config/providers（头：X-Opencode-Directory + X-Request-ID） |
| v3-3 | 状态两段 `_read_passthrough.py:203-218` | ≥500→drain（cap 保护 `_read_error_body@137`）+503 `upstream_unavailable`；≥400→**verbatim** 4xx（status+body+冻结头集 `_upstream_passthrough_headers@80`） |
| v3-4 | `read_with_cap@_read_passthrough.py:220-230` | >64MiB→413 `response_too_large` |
| v3-5 | ETag `judge_conditional@_read_passthrough.py:249-257`（rep wire_view=3@186） | coding-specific 单候选 304；200→compress+ETag（skeleton 域 REP，`etag.py:49`） |

### 6b. v4 §12 投影（`_handle_providers_v4@read_groups.py:247`；§12.5.2 冻结 12 步）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| ③ | `stream_upstream@read_groups.py:290`（**不持 permit**） | 网络错→503 `upstream_unavailable`（no-store）@291-294 |
| ③b | 非 200：`_read_error_body@305`（cap 保护；drain 失败/超 cap→503） | ≥500→503@310-313；其它 2xx（含 204）→502 `provider_upstream_malformed@314-317`；3xx/4xx→502 `upstream_http_{N}@318-320`；**全部错误 no-store**（`_v4_error@282-286`/逐处 stamp） |
| ④ | `read_with_cap@read_groups.py:324-327`（on_read=stash_up_in） | parse **之前**的源体 cap（大而畸形仍 413 非 502）；None→413 `response_too_large` + `limitBytes`（§12.5.3 字段）@331-334 |
| ⑤ | `async with pool@read_groups.py:340` | **网络等待后、worker 提交前**才取 permit——transform_busy 唯一可能点 |
| ⑥ | `_loads_strict@providers_projection.py:149`（`object_pairs_hook=_reject_duplicate_members@137`） | stdlib json 解码；**任意层重复成员名→502**（orjson 会静默取尾值故不用） |
| ⑦ | `_validate@providers_projection.py:187` | 顶层恰 `{providers,default}`@199-208；provider id/name 必填 str+UTF-8 可编码（`_ensure_utf8@161`，P0-4 孤代理→502）；models 必 map@224-226；model id/name/providerID 必填@227-232；`variants` 在场非 map→502（唯一 optional 错）@236-243；map-key==Model.id@249-252；嵌套 providerID==容器 id@254-257；provider id 全局唯一@259-261；default 三元组（key∈provider ids、value∈该 provider models、该 model.providerID==key）@265-280 |
| ⑧ | `_project@providers_projection.py:295` | 白名单投影+计数绊线（先触发者胜、**无截断**）：providers>256@300-301；每 provider models>1024@306-308；每 model variants>64@326-328；providers 按 id **UTF-8 字节序**@304；models 按上游 map-key 字节序@312；variants 仅出排序键数组@330-331；optional source/status str-else-omit@321-323/363-365；`limit` 修订三：`{context,input,output}` 子键白名单、逐子键 int-else-omit（bool 排除+orjson 64 位域 `_ORJSON_INT_MIN/MAX@80-81`）@344-355 |
| ⑨ | `orjson.dumps(OPT_SORT_KEYS)@providers_projection.py:409` | canonical 序列化（default 键排序冻结） |
| ⑩ | `len(canonical) > 8MiB`→`ProviderProjectionLimit("projected_body_bytes")@410-412` | identity（pre-gzip）体量上限（`MAX_PROJECTED_BODY_BYTES@57`） |
| ⑪ | `compress_if_beneficial@415-416` + `compute_etag@428-432` | **canonical 字节即 wire body**（无重排序副本）；ETag 指纹=`providers_rep_version@111`：`etag-v1 NUL providers-projection-v2 NUL 256 NUL 1024 NUL 64 NUL 8388608 NUL wire=v4`（`PROVIDERS_REPRESENTATION_VERSION@66`；`etag_enabled=false`→None→无 ETag/无 304 判定）；identity 强/gzip 弱 |
| ⑥-⑪ 异常归一 | `providers_projection.py:417-426` | 序列化期 ValueError/UnicodeEncodeError 归一 502（无未分类异常逃逸成 500）；`ProviderProjectionLimit` 原样重抛（合法 413） |
| 路由映射 | `read_groups.py:346-356` | TransformBusy→busy+no-store；`ProviderUpstreamMalformed`→502；`ProviderProjectionLimit`→413 `provider_projection_limit`（`limit`=常量名, `limitValue`=值） |
| ⑫ | 主上下文 `etag_mod.if_none_match_matches@etag.py:116`→`read_groups.py:360-368` | 条件判定+发放在 **main context**（worker 已产出 validator）；命中→`not_modified_response@etag.py:246`（ETag+Vary）；未命中→200 `no-store + extra(Vary/ETag/Content-Encoding)` |

**关键发现**：v4 投影把 decode→validate→project→count→serialize→cap→gzip→ETag **整段打包为一个纯函数 worker job**（`project_and_pack@providers_projection.py:376`），事件循环只保留 I/O 与最终 INM 比较；限额是固定 wire 常量（无 env 覆盖）且 first-triggered-wins 无截断；指纹域 `providers-projection-v2` 与 v3 透传的 skeleton REP 域结构性隔离（透传→投影切换本身即全量 validator 轮换）。

---

## 场景 7 — `POST /slimapi/session/{sid}`（v4 POST≡PATCH）与 `POST …/archive`（octet 缺省判据与合成体）；v3 面 404 thin_route_not_found

### 7a. 选择器层（v=4 POST 到达路由前的两级门）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | v 准入（同公共栈）→ §16.1 过渡 405 检查 `selector.py:592` → `_v4_method_boundary_405_live@selector.py:257` | 双条件合取：`method.boundary.v4∈SATISFIED ∧ session.post-actions.v4∉SATISFIED`。**当前态**：`readiness.py:93` 全亮（post-actions∈SATISFIED）→ 返回 False → §16.3 激活态：POST 组合**放行**到路由注册表（4.2.0 过渡态的 coded 405 `method_not_applicable@selector.py:596-609` 已熄灭；其 Allow 字面量冻结于 `selector.py:247-254`：`POST /session/{sid}`→`(GET,PATCH,DELETE)`，archive/delete→空 Allow） |
| 2 | `_consume_directory@selector.py:611` | `^/slimapi/session/[^/]+$@selector.py:183（177）` 属消费集（GET/PATCH/DELETE/POST 共用 pattern）→ v3 阶梯照常（query 单值消费→stash→剥；header→400） |
| 3 | `_forward@selector.py:719` | strip v 后进路由表 |

### 7b. handler 层（`write_groups.py` §16 修订二）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `post_update_session@write_groups.py:326` → `_post_actions_admitted@write_groups.py:303` | `wire_view>=4 ∧ session.post-actions.v4∈readiness.SATISFIED`（动态读，flip 零改码）→ True → `_write_passthrough(method="PATCH", upstream_path=f"/session/{session_id}")@338-339`——**与 `update_session@262-271` 逐字节同管线**，仅客户端所选 wire method 不同 |
| 2 | `_write_passthrough@write_groups.py:112` | 请求体读一次 + cap：`len>max_message_bytes(32MiB)`→413 `request_too_large@135-144`（上游调用之前）；`preset_body` 路径跳过读 socket@145-149 |
| 3 | `stash_up_out@write_groups.py:155`（`traffic.py:303`） | upOut=将发送字节 |
| 4 | `_resolve@write_groups.py:98` | stash→`validate_directory`→`X-Opencode-Directory` 头通道 |
| 5 | `forward_upstream_headers@upstream.py:117` + content-type verbatim `write_groups.py:166-169` | 头集合 = directory + X-Request-ID；客户端 content-type 永不改标（payload 契约归上游验证） |
| 6 | `build_request@write_groups.py:171-176`；turn bump `write_groups.py:182-186` | 仅 POST prompt_async/abort 在 send 前 `turn_registry.bump_turn(sid)`（S2 强栅栏 commit 点；本路径 PATCH 不触发） |
| 7 | 上游响应两段 `write_groups.py:196-228` | ≥500→cap 保护 drain→503 `upstream_unavailable@196-203`；≥400→**verbatim** 4xx（status+body+冻结头集 `_upstream_passthrough_headers@_read_passthrough.py:80`（default_content_type=None 严格 present-only））@204-216；2xx/3xx 体 cap→413 `response_too_large@218-228` |
| 8 | 成功尾 `write_groups.py:235-248` | `compress_if_beneficial`（sidecar 自有 gzip 门）；`Cache-Control: no-store` + 冻结头集 + `Vary: Accept-Encoding`（§6.2 终态单值，覆写 compressor 所设）；3xx 不 follow（upstream client `follow_redirects=False`） |

### 7c. `POST /slimapi/session/{sid}/archive`（§16.2-c octet 级缺省判据）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `post_archive_session@write_groups.py:343` → admitted 检查 `write_groups.py:358-359` | 非 v4/门控关→`_pre_revision_404`（见 7d） |
| 2 | entity 自读 + cap `write_groups.py:368-376` | 与 PATCH 管线**同一循环同一顺序**（413 先于上游调用）——判据需缓冲 octets，socket 只读一次，经 `preset_body` 交给管线（不再读） |
| 3 | 非空判据 `write_groups.py:378-385` | **len(body)>0 → 一律不解析、不判形**（含 `{}`、纯空白字节）→ 透传 PATCH 管线（client content-type verbatim） |
| 4 | 空判据→合成 `write_groups.py:387-395` | `archived_ms = int(time.time()*1000)`（判空后**立即**读 sidecar wall-clock，冻结求值点，不读上游）；合成体字节恰 `{"time":{"archived":<ms>}}`（紧凑无空格）+ `content_type_override="application/json"`（客户端 CT 随被替换的空实体丢弃，`write_groups.py:160-165`）；随后同 §16.2-a PATCH 等效管线（上游 4xx 原样、5xx/网络→受控 503，零新错误码） |

### 7d. v3 面 404 路径（含门控关穿透的防御面）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `?v=3`（或 selector-less）POST 到三组合 | selector 从不拦 v3（§16 面仅 `wire=="4"`，`selector.py:592`）→ 路由表命中 `post_update_session/post_archive_session/post_delete_session` |
| 2 | `_post_actions_admitted@write_groups.py:303` → False | wire_view=3 → 非 admitted |
| 3 | `_pre_revision_404@write_groups.py:314-322` | `error_response("thin_route_not_found", 404)`（gzip 协商 + Vary，无 Allow/无 no-store）——与 catch-all 基线（`proxy.py:47-51`）**逐字节一致**（v3-contract §8.2 冻结不变）；路由注册前 v3 请求本就落入 catch-all（`app.py:760-762` 路由先于 install_proxy，miss 后兜底），注册后由 handler 原样复现该答案 |
| 4 | （对照）`POST …/delete@write_groups.py:398-414` | ≡ DELETE 管线（无 ignore-body 分支；上游递归子删/吞错语义继承） |

**关键发现**：三组合的「等效」不是 selector 改写 method，而是 handler 侧显式路由进 `_write_passthrough` 并**改写 upstream method 参数**（POST→PATCH/DELETE）；archive 是唯一带 sidecar 合成体的端点，判据纯 octet 级（Content-Type 不参与判据）；v3 面的 404 现在有**两个产生点**（catch-all 与 handler `_pre_revision_404`），字节形态被刻意冻结为一致。

---

## 场景 8 — `GET /slimapi/events`（v3 vs v4 握手 + id 序列 + Last-Event-ID 四级分类 + barrier + T3 准入 + tokens=1 400）

bucket="events_sse"（`traffic.py:115`）；选择器正常消费 `v`（`/slimapi/events` 非消费集路径——directory 宽容不消费，`selector.py:674-677`）。

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `events@events.py:41`；tokens 校验 `events.py:88-89` | `tokens ∉ {None,"1"}` → 400 `invalid_tokens`（严格字面量） |
| 2 | v4 退役 `events.py:91-99`（`_request_wire_v4@events.py:27`←`wire_view_from_scope@selector.py:368`） | v4 ∧ tokens=="1" → 400 `tokens_stream_retired_in_v4`（冻结体 `events.py:21-24`），**流打开前**拦截（无 SSE 字节、无订阅位消耗） |
| 3 | replay 装配 `events.py:103-124`（**必须先于 subscribe**——分类冻结 T0 快照，订阅 T1>T0，无帧两投/无 gap） | v4∧replay_log→`classify_reconnect@replay_wire.py:169`；v4 无 log∧有 LEI→`ReplayResync(reconnect_no_replay)@events.py:119-124`（fail-safe） |
| 3a | ①② `parse_last_event_id@replay_wire.py:126` | 全局面：恰 `g:<epoch>:<seq>` 3 段；epoch=16 小写 hex（`_EPOCH_RE@82`）、seq=纯数字；任何违规（含 `t:…` 跨端点）→None=**ignore+reset**（首连语义，静默，无 resync） |
| 3b | ③④ `ReplayLog.replay@replay_log.py:399` | ③ epoch≠本进程 → `ReplayResync(epoch_changed)@423-425`；④ 序：`seq<=barrier watermark`→`Resync(reconnect_no_replay)@433-436`（禁跨上游丢失 barrier 补帧；watermark 写入点 `write_barrier@472-493`）→ future cursor→`ReplayIgnoreReset@439-442`→ 窗口头已逐出→`Resync(replay_expired)@448-458`→ 非连续（防御）→`Resync(replay_gap)@459-466`→ 连续窗口→`ReplayFrames@467-468`（空=已最新，非 resync） |
| 4 | T3 准入 `request.app.state.hubs.subscribe(wire_v4=v4)@events.py:132` → `HubRegistry.subscribe@registry.py:187` | **单无 await 临界区**（`registry.py:211-229`）：per-directory cap@212-218 与 total cap@219-225 检查+add；溢出→`SubscriberCapacityError`→路由 503 `{code,limit,current}` + `Retry-After: 5@events.py:133-139`；`subscriber.wire_v4` 标记@227（v4 抑制 `server.connected` 欢迎帧+fanout id 盖章） |
| 5 | meta 冻结（handler 时）`events.py:147-155` | `{subscriberId, tokens}` + v4 扩展 `meta_v4_extension@replay_wire.py:212`（`capabilities{sseReplay:true}`（`META_CAPABILITY_KEYS@96`）+ epoch + seqBase=本域已发布 max seq）——seqBase 必须与 3 步分类快照同源，惰性生成会让 fanout 抢跑；meta 帧 `sse_frame(...,"slimapi.meta")@hub_types.py:105`，**自身永不带 id** |
| 6 | tokens=1（仅 v3 存活）`token_registry.attach_events_subscriber@events.py:160-162` → `subscriber.py:584` | 控制面订阅者注册进 `TokenStreamHub.events_tap`；flush loop 首附启动（`subscriber.py:606`，双 ledger A-C5） |
| 7 | `generate@events.py:176` | `sse_open@events.py:182`（access sse_open 行）；**meta 先行** `events.py:188-189`（先于一切业务帧/心跳/replay/resync），`_accounted@167-174`→`record_sse_downstream(bucket="events_sse")`（downOut 唯一记账点） |
| 8 | replay 输出 `events.py:190-206` | `ReplayResync`→resync 帧（`{"reason":…}`，无 id）@195-198；`ReplayFrames`→逐条 `frame_with_id@replay_wire.py:116`（`id: g:<epoch>:<seq>\n` 前缀，`sse_id_line@104`）严格 seq 递增、先于任何新帧@199-205；`ReplayIgnoreReset`→无输出 |
| 9 | v3 LEI 分支 `events.py:207-213` | v3：任何 Last-Event-ID（值忽略）→首帧 `resync{reconnect_no_replay}`（无 replay 设施的 v3 冻结语义）；v4 永不进此分支 |
| 10 | 主循环 `events.py:214-231` | `queue.get()`→`STOP`哨兵 break@216-217；`subscriber.ack(item)@220`（byte ledger 回账）；`_accounted`+yield@230-231 |
| 11 | finally `events.py:232-242` | tokens detach 先于控制面槽位释放（flush loop 真末位停）；`hubs.unsubscribe@241`（经 registry，total 计数不漏）；`sse_close@242`（同 lifecycleId 配对） |
| 12 | 响应 `events.py:244-253` | `StreamingResponse(media_type="text/event-stream")` + `Cache-Control: no-cache, no-transform` + `X-Accel-Buffering: no`；**控制面流永不 gzip**（token 流的 zlib 是唯一 SSE 例外且 v3 下也恒 identity，见场景 9） |
| 13 | 帧源头 id 盖章（发布路径）`GlobalHub.publish@global_hub.py:658` → `_emit_directory_frame@global_hub.py:584` | allowlist 过@585-587（未过=未发布不占 seq）→ `_replay_publish@global_hub.py:547`：`replay.append(GLOBAL_DOMAIN, frame)@566`（每域 seq 从 1 单调、tombstone/业务同占 seq 无洞，`replay_log.py:376-378`；count/bytes/TTL ring 界@389-395）→ `sse_id_line@replay_wire.py:104`；fanout：`subscriber.wire_v4 → put(id_line+frame)` else `put(frame)`（`global_hub.py:593-597`——**v3 字节零改动**，id 只存在于 v4 订阅者的副本） |
| 14 | 溢出断连（订阅者侧）`Subscriber.put@hub_types.py:264` → 溢出 `hub_types.py:304-324` | 超帧→drop 计数@288；超 queue/byte 预算→`closed=True`+清队@304-308；**v4：STOP-only**（`subscriber_backpressure ∉ V4_RESYNC_REASONS@replay_wire.py:72-77`，`hub_types.py:310-319`）；**v3：`resync{subscriber_backpressure}`+STOP 冻结对**@320-324 |

**关键发现**：`id:` 序列的单一 choke point 在 hub 发布侧（`_replay_publish`）而非 SSE 路由——「已发布帧」语义（REPLAY-007：背压/离线期间发布的帧仍可重放）与 v3 零字节改动由此同时成立；v3/v4 的握手差异只有三处（welcome 帧抑制、meta 扩展、LEI 语义），流本体帧格式跨版本一致（除 id 前缀）。

---

## 场景 9 — `GET /slimapi/sessions/{sid}/stream`（token 流 per-sid 序列、tombstone、预算、溢出断连 v3 resync vs v4 STOP-only）

bucket="token_stream_sse"（`traffic.py:113`，先于 sessions 前缀判定）；selector 对该路径是消费集成员但 §5.6 例外：query-only 单值=accepted no-op（不 stash 不剥，`selector.py:699-701`）；header 形错误（conflict/retired）照常 400。

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `token_stream@token_stream.py:125` → `_resolve_directory_conflict@token_stream.py:88` | 多值异归一→400 `invalid_directory_selector@112-113`；query+header 归一化异→400 `directory_not_allowed@116-119`（directory 对 fanout 是 NO-OP——accumulator 以 sid 为键，单用户 T3 下 sid 全局唯一） |
| 2 | replay 分类 `token_stream.py:153-164`（先于 subscribe，同场景 8 理由） | `classify_reconnect(domain=token_domain(sid)@replay_log.py:111, token_sid=sid)`；①② `parse_last_event_id@replay_wire.py:154-166`：`t:<sid>:<epoch>:<seq>` ≥4 段、sid=中段 rsplit 聚合（含冒号 sid 可往返）、**跨 sid → None**（ignore+reset）；③④ 同场景 8 3b |
| 3 | 准入 `registry.subscribe(sid, wire_v4=v4)@token_stream.py:172` → `TokenStreamRegistry.subscribe@subscriber.py:625` | **独立 ledger**（不占 MAX_TOTAL_SUBSCRIBERS）：cap 检查@668-674→`TokenSubscriberCapacityError@subscriber.py:515`（code=`sse_token_subscriber_limit`/`sse_token_handshake_overflow`）→路由 503+`Retry-After:5`（body 含 `bufferBytes` 当 handshake 溢出）`token_stream.py:173-182` |
| 3a | 副作用段 `subscriber.py:694-711`（INV-3） | `hub_registry.get_global().ensure_upstream()+cancel_pending_removal@696-699`（NB-B1）；`token_hub.start()@701`（flush loop 首附启动）；`attach_subscriber@705`——**v4：no-prefill 握手**（无 server.connected/无历史 tombstone/无 live-part snapshot，`tokenstream/hub.py:1289-1300`）；v3：server.connected→message.removed tombstones→snapshot→入 fanout（`hub.py:1238+`）；任何异常→`_rollback_failed_attach@709`（对称回滚：flush 停+grace 重臂） |
| 3b | closed 回检 `subscriber.py:725-742`（MAJOR 4/5） | handshake 溢出（`_handshake_overflow`）/超帧 guard→不计数+完全回滚→503（code 区分）；成功→`total_subscribers+=1@743` |
| 4 | meta 冻结 `token_stream.py:190-197` | `{subscriberId, tokens:true}`（恒 true——token 流必带 token）+ v4 扩展（`capabilities/epoch/seqBase=t 域 last_seq`）；`use_gzip=False@203`——**v3 恒 identity**（v3-contract §7.2 冻结：SSE 不做 content-encoding），v4 同样 identity（zlib deflater 是 v2 遗留杠杆） |
| 5 | `generate@token_stream.py:208` | `compressor=None`（encode=透传）@214；meta 先行@260-262；`_accounted→record_sse_downstream(bucket="token_stream_sse")@228-245`（线上字节=帧字节） |
| 6 | replay 输出 `token_stream.py:263-281` | Resync→`_resync_frame(sid, reason)@frames.py:125`（**每个 token resync 帧带 sessionID**，§16-D）；Frames→`frame_with_id(entry.payload, token_domain(sid), …)@273-280`（`id: t:<sid>:<epoch>:<seq>`）；IgnoreReset→无输出 |
| 7 | v3 LEI 分支 `token_stream.py:282-292` | v3：任何 LEI（值忽略）→`resync{reconnect_no_replay, sessionID}` 先行（握手帧排其后） |
| 8 | 主循环 `token_stream.py:293-302` | queue.get→STOP break；`subscriber.ack@299`（handshake/runtime 双 ledger 路由，`subscriber.py:500-512`）；encode（透传）+`_accounted`+yield |
| 9 | finally `token_stream.py:303-307` | `registry.unsubscribe@subscriber.py:789`（成员 guard NB-D1 真幂等；last-detach（双 ledger 空）停 flush loop@831-832；`maybe_arm_grace_if_idle@registry.py:161` B-D1 对称重臂）；`sse_close@307` |
| 10 | 序列/tombstone 发布侧：`TokenStreamHub._replay_publish_token@tokenstream/hub.py:1371` | live fanout 与 `_fanout_message_removed` 的 choke point；**`message.part.snapshot` 家族不进 log**（`_v4_frame_eligible` 前置，`hub.py:1387-1390`）；`message.removed` tombstone：`on_message_removed@hub.py:835`→`_replay_publish_token(kind=FRAME_KIND_TOMBSTONE@replay_log.py:83)`——**占 seq 无洞**（REPLAY-012），重放时发轻量 revocation 形 |
| 11 | 状态失效 barrier：`_write_replay_barrier@tokenstream/hub.py:1396` | session idle retire/内存逐出/session deleted 每处**无条件**写（无视在线订阅者）——失效后 `LEI==last_seq` 不得判 up-to-date，重连一律 `resync{reconnect_no_replay}`→HTTP 全量对齐 |
| 12 | 预算（帧侧） | runtime：`queue_items`/`buffer_bytes`/`max_frame_bytes`（config `token_stream_*`，env debug 覆盖仅联调 `app.py:307` `apply_debug_budget_overrides`）；handshake 独立：2048 items / 8MiB bytes（`subscriber.py:302-303`）fail-loud 不 drop-oldest |
| 13 | 溢出断连 `TokenSubscriber.put@subscriber.py:362` | closed→静默 drop@386-390；STOP→runtime 终哨兵@391-396；超帧→drop+计数（不关流）@397-406；handshake 溢→`closed=True`+503 retry@407-423（CRITICAL 2）；runtime 溢→`closed=True`+`clear_runtime()`（**只清 runtime，handshake 存活**，CRITICAL 3）@428-442；**v4：STOP-only@450-452**（`subscriber_backpressure` 不在冻结四原因域 `replay_wire.py:72-77`）；**v3：`resync{subscriber_backpressure, sessionID}`+STOP 终结对@453-458**；服务端主动终止 `terminate(reason)@subscriber.py:460`：v4 仅当 reason∈冻结域才发 resync，否则 silent STOP@472-479 |

**关键发现**：token 流的「per-sid 序列」由 `t:<sid>` 独立 replay 域承载，tombstone 与业务帧共占 seq（序列无洞使 cursor 语义成立）；内存预算三层（handshake 8MiB fail-loud / runtime queue+bytes / per-frame ceiling）与「溢出即断连」配合，v3/v4 的差异仅在终止帧形态（resync+STOP vs STOP-only），恢复路径统一收敛到 LEI 重连+重放/HTTP 对齐。

---

## 场景 10 — catch-all 终局链路（非 /slimapi 与 /slimapi 未收编路径 → 404 thin_route_not_found；traffic 记账 not_applicable；WS 501）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| A1 | 非 `/slimapi` 请求：`SlimapiSelectorMiddleware.__call__@selector.py:516-522` | **zero-touch**：不读 v/directory，仅 `_stash(SELECTOR_NOT_APPLICABLE, None)@518` + `directoryForm=None@519-521`（§9.1 归因用）；query 字节原样下传 |
| A2 | `TrafficAccountingMiddleware`（外层已包）→ `bucketize@traffic.py:192` | bucket=`"passthrough"`（catch-all 聚合桶——3.0.0 后即「未省流面」的记账残留维度） |
| A3 | FastAPI 路由表 miss → `catch_all_closed@proxy.py:44-51` | `error_response("thin_route_not_found", 404)`（gzip 协商 + Vary；`proxy.py:47-51`）——v2 透明反代已退役（`proxy.py:1-24` 模块头），/event、/global/event 直连面同样落此 |
| B1 | `/slimapi/**` 未收编路径：selector 全链**先于** catch-all 生效 | 优先链（§8.3 冻结）：① `/slimapi/versions` 非 GET→405+`Allow: GET`（`selector.py:533-545`，slash 归一 `_normalize_path@283` 后判）；② v 缺席/值域外→400 `unsupported_version`（`selector.py:551-556` + `_reject_version@622-634`）；词法非法/多值冲突→400 `invalid_version_selector@558-565`；§16 过渡 405（若双条件成立）`selector.py:592-609`；③ directory 400 族 `selector.py:611-619`（invalid_directory_selector/directory_conflict/directory_header_retired/directory_retired_in_v4） |
| B2 | 通过者 `_forward@selector.py:620`（strip v）→ 路由表 miss → `catch_all_closed@proxy.py:44` | 同 A3 的 404 `thin_route_not_found`——**这是 ④ route-miss 终局类**（proxy 只表达最后一类，`proxy.py:9-14`） |
| B3 | 已注册路径但未注册 method（如 TRACE） | catch-all `api_route` 注册了 GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS（`proxy.py:41-43`）→ 其余 method 由 Starlette 答 405（非 coded 体） |
| C1 | WebSocket：`websocket_not_supported@proxy.py:34-38` | `accept()`→`send_json({"code":"websocket_not_supported","status":501})`→`close(code=1011)`——WS 面 501 存根（选择器对 `scope["type"]!="http"` 直通，`selector.py:511-513`；traffic 中间件同样直通，`traffic_accounting.py:162-165`——**WS 无 access 行**） |
| D1 | traffic 记账维度现状 | 非 slimapi：`selectorResult="not_applicable"`、bucket="passthrough"；slimapi 未收编：selectorResult=v3/v4（或 rejected——被 ②③ 拦者）+ 对应子桶/`other@traffic.py:190`；access 行照写（`write_access_log@traffic_accounting.py:319-342`）；「not_applicable」因此是**维度现状**而非缺失：3.0.0 关闭反代后 passthrough 桶只剩 404/405/501 噪声 |
| D2 | SSE 兼容 | 纯 ASGI 包装不改流语义（`traffic_accounting.py:9-15`），catch-all 面不会截断任何流（此处无流） |

**关键发现**：终局链路的错误优先序完全由选择器前置决定——catch-all 只可能收到「已通过全部 400/405 门但无路由」的请求；`passthrough` 桶与 `not_applicable` 选择器结果在 3.0.0 终态后成为「未省流面是否复发」的哨兵维度（一旦该桶出现 200，即有路径意外穿透）。

---

## 场景 11 — `GET /slimapi/metrics` 与 access log / snapshot 写盘链路（RETAIN_DAYS、access-legacy 永不清理问题）

### 11a. `/slimapi/metrics`（`metrics@metrics.py:20`；需过选择器（非豁免路径）→ `?v=3|4`；bucket="metrics" `traffic.py:105`）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `hubs.snapshot_metrics@registry.py:327` | `{sse:{subscribers{current,limit,rejectedTotal},hubs[…],clients[…]}, skeleton:{activeTransforms,waitingTransforms,cacheEnabled:false}}`；skeleton 数来自 `TransformPool.snapshot_metrics@transform.py:276`（计数器非信号量窥探）；`batch=None@metrics.py:23` |
| 2 | tokenStream 块 `metrics.py:29-31` → `snapshot_token_metrics@subscriber.py:838` | current/…+gzipRaw/gzipCompressed（v2 遗留计量）+flush 计量+maxSubscriberQueueDepth（**仅 runtime 深度**，handshake 突发不计） |
| 3 | traffic 块 `metrics.py:36-38` → `TrafficLedger.snapshot` | buckets/totals/ratios + v3 矩阵/sseLifecycle/sseActive + expand + v4.degradedMatrix |
| 4 | sweep 块 `metrics.py:42-44` | shadow-only 计数器（无真实上游请求） |
| 5 | dbaux 块 `metrics.py:51-69` → `DbAuxiliarySource.snapshot@lifecycle.py:707` | available/mode/reason/generation/breaker{p50,p99,samples,total}/counters{queries,probes,trips,swaps,disables}/source（解析通道）——**path 不回显**（§8 no-leak，与 health 同姿态） |
| 6 | sessionsDegraded 块 `metrics.py:80-84` | middleware 懒挂的 per-response 计数器（`SESSIONS_DEGRADED_STATE_ATTR@traffic.py:395`；`ensure_sessions_degraded_counters@traffic.py:398`）；注意 handler 先于本请求的 middleware 记账执行→首请求前无此块（`metrics.py:76-79`） |
| 7 | replay 块 `metrics.py:97-107` → `metrics_snapshot@replay_log.py:338` | epoch/domains/frames/bytes/barriers + outcome counters（replayed/up_to_date/ignore_reset/四个 resync 原因）——只计数与尺寸，无 payload/路径 |
| 8 | `json_response@metrics.py:108-111` | gzip 协商 + Vary |

### 11b. access log 写盘链（每请求一行）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | 请求尾 `_record@traffic_accounting.py:249`（正常与异常路径都记：`traffic_accounting.py:214-246`，异常 `status or 500`） | 汇集 duration/downIn(计数 receive)/downOut(计数 send)/upIn/upOut(scope state stash)/selector 结果/client 三头（`_read_client_headers@traffic_accounting.py:110`，>128B、控制字符拒收）/clientId 哈希（`hash_client_id@access_log.py:92`，fail-closed 默认 HMAC） |
| 2 | `write_access_log@access_log.py:267` | 行结构 `access_log.py:333-364`：ts/method/path/bucket/status/durationMs/downIn/downOut/upIn/upOut/requestId/client/clientVer/clientId（恒在，null 占位）+ 稀疏键 cache/wireVersion/selectorResult/directoryForm/recordType="request"/lifecycleId(null)/sessionsSource(仅 db|http)/degraded503(仅 true) |
| 3 | `logger.info(json.dumps(separators))@access_log.py:364` → `DailyAccessHandler.emit@access_log.py:173` | 以 `record.created` 定日期@186（消除 ts/文件名跨午夜漂移）；换日关旧开新@187-191；文件 `access-YYYY-MM-DD.jsonl@140-142`；**单次 write 调用+flush@195-196**（缩小半行窗口） |
| 4 | 维护循环 `run_access_log_maintenance_loop@access_log.py:668`（lifespan 装配 `app.py:655-684`；仅 `access_log_active@245` 安装成功才启动） | 每 tick `to_thread`：`compress_old_access_logs@715` → `prune_old_access_logs@719` → `extra_prune@722-726`（snapshot prune 搭车，同一 today） |
| 5 | 压缩 `compress_old_access_logs@access_log.py:449` | <today 且无 .gz 且非活动句柄正持有（fd 保护 `access_log.py:477-515`，P1-25 inode 泄漏防护）→ `.gz.tmp.<pid>.<token>`（`_unique_tmp_path@413`）→ `os.replace` 原子提交@517-523 → 删源 |
| 6 | 清理 `prune_old_access_logs@access_log.py:547` | `retain_days<=0`→no-op@560-561；`deadline = today - retain_days@562`；**严格 `<**@575`（边界保留）；glob 两个模式 `access-*.jsonl(.gz)@566` × `_ACCESS_LOG_RE@79` 校验 |
| **问题锚点** | `access_log.py:566-570` | **access-legacy-*.jsonl.gz 永不清理**：glob `access-*.jsonl(.gz)@566` 能匹配 `access-legacy-20260801-current.jsonl.gz`，但 `_ACCESS_LOG_RE@79`（`^access-(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$`）不匹配 legacy 命名 → `continue@568-570`——历史迁移档案（`migrate_legacy_access_log@586` 产物）无限期留存（磁盘缓慢累积，best-effort 纪律下无告警） |
| 7 | 启动维护 `app.py:269-280` | migrate→（可选）compress→prune 一次；失败仅 warning 不炸 lifespan |

### 11c. traffic snapshot 写盘链

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | 装配 `app.py:286-304`（`TrafficSnapshotter(ledger, interval_s, path)`） | 累计量唯一持久载体（重启即失的 in-memory ledger 的定期落盘） |
| 2 | `_loop@traffic_snapshot.py:411` | sleep→`_write_once`，单迭代异常不杀循环 |
| 3 | `_write_once@traffic_snapshot.py:434` | **单一时间采样@452**（ts 字段与文件名同日，消除跨午夜错桶 P1-26）；`ledger.snapshot()`；record `{ts,bootTs,runId,uptimeS,pid,enabled,buckets,totals,ratios,v3,expand,v4}@466-497`（v3/expand/v4 尾巴=唯一 ≥RETAIN_DAYS 载体）；日文件 `{stem}-YYYY-MM-DD.jsonl@501`；追加单次写@512-517（P1-27 半行防护） |
| 4 | 清理 `prune_old_snapshots@traffic_snapshot.py:79` | 挂为 access 维护循环 `extra_prune`（`app.py:668-674` partial 位置绑定——注释记录了历史上的 keyword 绑定 TypeError 事故） |

**关键发现**：metrics 是纯只读聚合（无自暴露路径/payload）；access/snapshot 两条写盘链共享同一维护循环与「单次写+原子替换」纪律，但 `prune_old_access_logs` 的正则白名单把 legacy 迁移档案排除在清理之外（`access_log.py:566-574`）——是本仓明确可锚定的留存缺口。

---

## 场景 12 — dbaux 全环：path_resolution 发现 → lifecycle 连接（mode=ro+query_only）→ projection SQL → cursor → 断路 → 恢复 → 关停

### 12a. 路径发现（`dbaux/path_resolution.py`，纯函数无连接副作用）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `resolve_db_path@path_resolution.py:82`（lifespan 调用 `app.py:598`） | 优先级 §3.1 冻结（rev-1 fail-closed） |
| 1a | 显式 `OC_SLIMAPI_OPENCODE_DB@95-101` | `":memory:"`→`DisabledResolution(explicit-memory)@97-98`；否则 normpath→`ResolvedPath(source="explicit-env")` |
| 1b | 上游 `OPENCODE_DB@105-119` | memory→disable@107-108；绝对/~ 前缀→`upstream-env@110-114`；相对→挂 `_data_dir@75-79`（`XDG_DATA_HOME`|`~/.local/share` + `/opencode`，global.ts 复刻）→`upstream-env-relative@116-119` |
| 1c | 候选发现 `@124-133` | glob `opencode*.db`：恰 1→采用+`SINGLE_CANDIDATE_WARNING@38`；>1→`Disabled(path_ambiguous)`（带候选列表 detail）；0→`Disabled(not_found)`（带 data_dir detail）——**不猜测**（R3 冻结） |

### 12b. 连接生命周期（`dbaux/lifecycle.py`；装配 `app.py:599-615`，`probe_interval_s=30@config.py:658`）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `start@lifecycle.py:362` | 幂等；Disabled→`_state=disabled@370-371`（`explicit/upstream-memory` 永不重探 `@349-352`，其余周期重探）；否则 `_open_and_gate("startup")@373`；随后起周期任务 `@377-380`（`_periodic@584`） |
| 2 | `_open_and_gate_sync@521`（**全在专属单 worker**：`ThreadPoolExecutor(max_workers=1)@lifecycle.py:325`，`check_same_thread` 默认 True） | `_close_conn@513`→`_open_conn@499`：URI `file:{path}?mode=ro@506` + `sqlite3.connect(uri=True)@507` + **`PRAGMA query_only=ON@509`**（双层防御第二层）+ `PRAGMA busy_timeout=5000@510`（§2.3-3 与上游同值）；`immutable` 完全弃用（§1.3） |
| 3 | schema 门 `schema_gate_missing@97` | `PRAGMA table_info(session)@103` ⊇ `SESSION_PROJECTION_COLUMNS`（24 列 @65-90）；`table_info(project)@105` ⊇ 3 join 列@94；缺列→OperationalError@534-536 → **门期异常先关局部连接再抛**（MAJOR-1：全门通过前连接只归本栈帧，绝不泄 fd）@536-543 |
| 4 | 成功转移 `@544-547` + `_open_and_gate@549` 尾 | `self._conn` 接管、`generation+=1`、inode 标记 `stat_inode_marker@path_resolution.py:136`（`(st_ino, st_mtime_ns)`，-wal/-shm 不参与）；`state=available@560`；`breaker.reset()@563`（新连接=新延迟画像，warmup 重算）；失败→`_disable(gate_failed|open_failed)@552-558`（detail 仅入日志） |

### 12c. 查询通道与投影（v4 sessions 消费）

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | `query@lifecycle.py:429` | 唯一合法通道（§2.3-7②）；`status().available@440-442`——不可用→`AuxiliaryUnavailableError(reason)@254`；`status@691` 内部 `_check_breaker_state@683`（breaker 已 trip→联动 circuit_open） |
| 2 | `_run_query@465`（worker 内同步） | 显式短事务：`BEGIN@472`（deferred→snapshot）→`execute@475`→`fetchall@476`→`COMMIT@477`；finally：`in_transaction→ROLLBACK@479-487`（防嵌套事务脏态）+游标 close+`breaker.record@489`（busy 等待同样计样本） |
| 3 | 错误分类 `classify_sqlite_error@114` → 处置 `query@448-460` | `busy`→原样上抛（P99 路径自洽，不禁用）@448-454；`schema/io/cantinit/programming`→log+`_disable(reason=query_*)@459`+`AuxiliaryUnavailableError@460`；成功→`_check_breaker_state@462`（本样本可能刚推过阈值→立即熔断后续请求） |
| 4 | 投影消费（列表）：`fetch_sessions_page@dbaux/projection.py:332` | SQL 组装见场景 5 ⑥a；**同事务快照内**完成投影+complete（`len(rows)<=limit@357`，LIMIT+1 窗口）+anchor（`_window_anchor@315`） |
| 5 | 投影消费（单查）：`_handle_session_single_v4@read_groups.py:520` | `_SESSION_SINGLE_SQL@read_groups.py:425-437`（同 SELECT 形状点查）；`AuxiliaryUnavailableError`→native 回退（§4.2 矩阵）`@528-530`；`sqlite3.Error`→fail-closed 503 `@531-533`；空行→404 `session_not_found@536-537`；`rows_to_records@538`（容忍跳行）→canonical 投影→None→503 `@542-546` |
| 6 | cursor 编解码（`dbaux/cursor.py`） | `encode_cursor@158`（base64url(JSON{t,i,f}) 无 padding、确定性键序）；`decode_cursor@165`（字母表/长度/UTF-8/JSON/键集 {t,i,f}/类型/空锚点全查——空锚点在解码层拒，DB 可用时不再逃逸 500，BLOCKER-2） |

### 12d. 熔断、恢复、swap、关停

| 步骤 | 函数@file:line | 数据形态变化 / 守卫 |
|---|---|---|
| 1 | 熔断器 `LatencyBreaker@lifecycle.py:147` | 60s 环窗惰性剪枝@181-184；`record@197`：warmup（前 10 次仅采样）@205-207 + 样本<10 不判@208-209 + **P99≥20ms→trip@210-212**；`trip@214` 清窗（恢复需新鲜证据）；`note_probe@219`：半开探针计入后 **P99<10ms（hysteresis）→闭合**@226-230；`reset@232`（swap/重开清零） |
| 2 | 状态联动 `trip_breaker@675`/`_check_breaker_state@683` | query 路径自查→circuit_open+warning「degrading to http」；`status@691` 恒先自查再报 |
| 3 | 周期 `tick@597`（30s，三类共用调度器） | ① available→inode/mtime 对比@601-609：变→`swap@566`（任务入同一 worker FIFO=等活跃查询自然交接→重开+门→generation+1，`counters["swaps"]@577`；失败→disable+周期重试）；② circuit_open→半开 `probe@619`（`_probe_sync@636`：BEGIN SELECT 1 COMMIT；探针延迟 `note_probe@630`→恢复 available@631-633 或保持）；③ disabled→`reprobe@652`（`_open_and_gate` 重试；memory 类禁用永不重探） |
| 4 | barrier（跨模块语义）：上游丢失确认时 `write_barrier@replay_log.py:472` | 域低水位（全局域+全部已建域）；cursor ≤ watermark → `reconnect_no_replay`（禁跨 barrier 补帧）——dbaux 熔断与 SSE 重放屏障是两个独立护栏 |
| 5 | 关停 `stop@383`（lifespan 回调 `app.py:605-615`，drain 5s=`_DBAUX_DRAIN_TIMEOUT@app.py:85`） | stop_event set→task 有界 drain（超时 cancel）@386-397 → worker 内 `_close_conn@399-401` → `_bounded_executor_shutdown@402/407-423`（cancel pending+守护线程有界等待，不阻塞退出）→ `_disable("stopped")@405`（生命周期终态，不属降级计数 `@668-671`） |

**关键发现**：dbaux 全环的写域纪律由结构保证——`mode=ro` URI + `PRAGMA query_only` 双层、全部连接操作约束在 max_workers=1 的专属线程（`check_same_thread` 天然不破）、显式短事务 + finally ROLLBACK；对 wire 只暴露 `available/reason/mode` 三态（503 auxiliary_unavailable / Class A 降级 200 / DB 常态 200），SQLite 细节（错误分类、路径、schema）只进日志与 metrics 计数。

---

## 附：跨场景公共不变量汇总

1. **准入先于网络**（skeleton/投影族）：`async with pool` / absorb 循环在一切上游 GET 之前（场景 1/3/4/5/6）；纯透传族（read_groups raw、write_groups）无 pool 语义。
2. **cap-read 三层**：`max_response_bytes`(64MiB，列表/透传体)、`max_message_bytes`(32MiB，单消息/请求体)、`max_expand_response_bytes`(8MiB，expand 序列化后)——各错误码不同（response_too_large/message_too_large/request_too_large/expand_source_too_large/expand_fragment_too_large）。
3. **ETag 域隔离**：rep_version 一律带 `wire=v{view}` 标（`etag.py:87`），providers 另立 `providers-projection-v2` 域——跨面/跨版本 If-None-Match 结构性不可 304。
4. **SSE 双 choke point**：全局域 `GlobalHub._replay_publish@global_hub.py:547` / token 域 `TokenStreamHub._replay_publish_token@tokenstream/hub.py:1371`——id 盖章只在发布侧，v3 订阅者字节零改动。
5. **fail-closed 家族**：dbaux 一切异常→503/降级矩阵（sessions.py:488-502）；expand 上游结构错→502/404；providers 校验错→502；从不泄内部细节（路径/schema/SQLite 分类）。

