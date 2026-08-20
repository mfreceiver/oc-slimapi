# D10 — A10 代码质量审计（重复代码 / 错误处理一致性 / 命名对齐 / 死代码 / 魔法数 / 注解密度）

- 快照：`0b836e7`（BASELINE_HEAD）；本报告所有 `file:line` 相对该快照。
- 审计人：A10（Phase 2）；产物：本报告 + F-021/F-024/F-026 更补 + 新建 F-286..F-292。
- 方法：只读；全部结论基于 `rg`/`grep`/AST 全量扫描 + 逐文件精读；抽样仅两处且算法随注记录（§7 注解密度、§2 风格观察内联样本）。
- 输入：`src/oc_slimapi/**`（71 文件 / 18,104 行）、`01-explore/inventory.json`、E1 卡片（parts/e1-01..19）、`02-findings/INDEX.md`。

---

## 1. 重复代码探测（手工三步，全量不抽样）

### 1.i 全部 read-group handler（12 个）与 write endpoint（20 个）结构指纹

**read_groups.py 12 个 handler 指纹（全量）：**

| # | handler | 行 | 结构指纹 |
|---|---|---|---|
| 1 | `file_list` | :149-163 | `_resolve` → `_authorized_file_directory`(403 门) → `read_passthrough_get("/file")` |
| 2 | `file_content` | :166-175 | 同 #1，path=/file/content（与 #1 逐行同构，仅 path 串不同） |
| 3 | `file_status` | :178-186 | 同 #1，path=/file/status |
| 4 | `vcs_info` | :192-198 | `read_passthrough_get("/vcs", _resolve(...))`（3 行） |
| 5 | `vcs_status` | :201-207 | 同 #4 |
| 6 | `vcs_diff` | :210-221 | 同 #4（声明 mode/context 参数仅为保 422 面） |
| 7 | `find_file` | :227-241 | 同 #4，path=/find/file |
| 8 | `config_providers` | :395-409 | `_resolve` → v4∧门控 → `_handle_providers_v4` / else `read_passthrough_get` |
| 9 | `session_single` | :555-576 | `_resolve` → v4∧门控 → `_handle_session_single_v4` / else `read_passthrough_get(project=)` |
| 10 | `session_active` | :582-588 | `read_passthrough_get("/api/session/active")`（1 行委托） |
| 11 | `global_health` | :591-596 | 同 #10 |
| 12 | `session_context` | :619-630 | `_strip_directory_query` → `read_passthrough_get` |

**write_groups.py 20 个 endpoint 指纹（全量）：**

| 类 | endpoint（行） | 结构指纹 |
|---|---|---|
| 纯委托 ×15 | create :256 / update :262 / delete :274 / prompt_async :417 / abort :425 / summarize :432 / fork :440 / revert :448 / respond_permission :455 / reply_question :465 / reject_question :473 / session_command :481 / session_agent :517 / session_model :531 / revert_{stage,clear,commit} :545/:560/:573（+post_update :325, post_delete :398） | `[_strip_directory_query]` → `_write_passthrough(method=, upstream_path=)`（1-2 行体） |
| admission 前奏 ×3 | post_update :325 / post_archive :342 / post_delete :398 | `if not _post_actions_admitted(scope): return _pre_revision_404(request)` 2 行同构前奏 → 纯委托 |
| 自有体 ×1 | post_archive :342-395 | admission 前奏 + **自读 body+cap 循环（:368-376）** + 判空 → 合成/透传双分支 |

**结论（共享率）**：read 侧 12/12 全部走共享管线（`read_passthrough_get` 121 行承载 admission→GET→两级映射→cap→gzip→ETag 全链）；write 侧 20/20 全部走 `_write_passthrough`（137 行）。handler 自有代码占比极小（read_groups ~140 行 v4 分支 + 指纹参数；write_groups ~100 行 archive/admission）。**管线共享率 ≈95%**（handler 独有代码基本只剩 path 字符串与 docstring）——这层是良好范式（对照 `_catalog_common.py` P2-B2 dedup 先例）。重复债不在 32 个 handler 本身，而在下述热区。

### 1.ii `_read_passthrough.py` 共享管线与各 handler 的共享率

- 共享管线 277 行 = `read_passthrough_get`（:157-277）+ 4 助手（`_upstream_passthrough_headers` :80-100 / `_raw_upstream_url` :103-116 / `_maybe_pool` :119-134 / `_read_error_body` :137-154）。被 read_groups 12 handler、write_groups（导入 `_PASSTHROUGH_UPSTREAM_HEADERS`/`_raw_upstream_url`/`_read_error_body`/`_upstream_passthrough_headers`，write_groups.py:88-93）双向复用。
- **偏离点（重复而非复用共享管线）**：read_groups 内两条 v4 分支管线自有 ~230 行——
  - `_session_single_native_fallback`（:455-517）：stream→5xx drain→4xx 逐字→cap-read→413→offload→TransformBusy→busy——与 `read_passthrough_get` :194-243 的两级错误链**逐段同构**（~40 行重复），差异仅投影函数与错误体字段（`limit=` vs §12 `limitBytes=`）；
  - `_handle_providers_v4`（:247-368）：§12 十二步自成一链，同样的 fetch→status 映射→cap→pool→offload→emit 骨架再实现一遍（§12 专属语义占少数行，骨架行 ~50 重复）。
  - 对照：sessions.py :266-269 注释自认 v4 fork「deliberately DUPLICATES the upstream-fetch call shape … zero-touch rule for the v3 pipeline」——**v3 冻结零接触策略是有意的、注释在案的重复**（不计为债，但 F-287 记录 read_groups 侧无此豁免注释且无测试钉住 v3 字节等价）。

### 1.iii `def _` 私有助手全清单与复用标注（routes/ 全量 95 个 `def _`）

关键复用/重复标注（全清单基于 `rg -n "^def _|^    def _|^async def _|^    async def _"`）：

| 助手 | 复用情况 |
|---|---|
| `_resolve`（read_groups:93，10× 调用）与 `_resolve`（write_groups:98，1×） | 同名同骨架（`resolve_route_directory`→`validate_directory`），语义分叉（read 有 header 回退通道，write 无）——孪生但非字节重复 |
| `_strip_directory_query` | **read_groups:602-616 与 write_groups:504-514 逐字节相同**（含 docstring 语义）——纯复制 |
| `_busy_response`（messages:268-284） | 模块内 6× 调用；**逻辑与 `_catalog_common.busy_response`（:44-52）逐行相同**，连同模块常量 `TRANSFORM_RETRY_AFTER_SECONDS = 2`（messages:44 vs _catalog_common:41）成对复制 |
| `_stream_upstream`（messages:319-334） | 模块内 2× 调用；与 `_catalog_common.stream_upstream`（:55-94）近重复——**行为漂移：messages 变体不转发 `X-Request-ID`**（仅 `forward_directory_headers`），catalog 变体经 `forward_upstream_headers` 双头转发（§7 契约要求每个 sidecar→opencode 请求携带该头）→ F-288 |
| resolve→validate 四行 stanza | agent:37-39 / command:34-36 / todo:66-68 / children:72-74 / diff:88-90 / sessions:708-710、:832-834 / messages（变体 :309-316）/ read_groups._resolve / write_groups._resolve = **10 处**（其中 6 处逐字节相同） |
| permissions/questions 孪生 6 对私有符号 | `_DirFetchFailure`（perm:28-39 ≡ quest:28-39）、`_MAX_AGGREGATE_ITEMS`（:46≡:47）、`_directories_from_sessions`（:49-65≡:55-75，体相同）、`_pack_*_envelope`（:292-303≡:275-286，体相同）、`_fetch_*_for_dir`（:306-423≡:289-403，~110/118 行相同）、`_collect_with_byte_budget`（:426-526≡:406-504，**121 行逐字节相同**） |

**孪生结构定量化（AST 归一化 diff）**：剥 docstring + 名称归一（permission↔question、semaphore/config-knob/函数名归一）后：permissions 181 行代码 vs questions 179 行，**仅 20 行不同**（= 路由/tag、`_PERMISSION_FIELDS` 白名单、上游 path、flight key、config knob 名）——**~89% 结构同一**；26 个 diff hunk 中绝大多数是注释/docstring 文案差异。

### 重复热区 Top5 + 合并方向

| # | 热区 | 量级 | 合并方向 | Finding |
|---|---|---|---|---|
| 1 | `permissions.py` ↔ `questions.py` 全模块孪生 | ~320 行重复（121 行调度器逐字节 ×2 + fetch ~110 行 ×2 + discovery 块 ~50 行 ×2 + 类/常量/打包 ~30 行 ×2）；~89% 同构 | 抽 `routes/_aggregate_common.py`（镜像 `_catalog_common` 先例）：参数化 endpoint spec（path `/permission`/`/question`、semaphore、flight-key 前缀、config knob 三元组、item 投影函数——questions=verbatim stamp，permissions=7 字段白名单）；两文件各剩 handler + spec 表 <60 行 | F-286 |
| 2 | read_groups 两条 v4 分支管线重复共享读链骨架 | `_session_single_native_fallback` ~40 行 + `_handle_providers_v4` ~50 行与 `read_passthrough_get`/`_read_error_body` 同构 | 扩展 `_read_passthrough`：加 `status_map`/`error_fields`/`pool_after_fetch` 钩子让 v4 分支走共享骨架；或至少复用 `_read_error_body`+统一 413 字段名 | F-287 |
| 3 | messages.py 本地 `_busy_response`/`_stream_upstream`/常量复制 `_catalog_common`，且 X-Request-ID 转发漂移 | ~30 行重复 + 1 处可观测性行为漂移 | 删除本地副本改 import 共享版；`_stream_upstream` 合并参数（params dict + read_timeout 可加到共享版）——X-Request-ID 漂移需先定契约口径（messages 路径是否豁免 §7 头） | F-288 |
| 4 | `_strip_directory_query` 逐字节 ×2 + resolve/validate stanza ×10 | 13+ 行 + 10 处 ×4 行 | `_strip_directory_query` 上移 `selector.py`（紧邻 `_strip_query_keys`）或 routes 共享模块；stanza 收敛为一个 `resolve_route_directory_or_none()` 助手 | F-292（部分） |
| 5 | write_groups archive 自读 body+cap 循环复制 `_write_passthrough` 同款循环 | :368-376 复制 :135-144（同判界同序，注释自认「SAME loop/order」） | 把「读+cap 循环」提为 `_read_capped_body(request) -> bytes \| Response` 助手供两处调用；漂移风险=一侧改判界另一侧忘改 | F-287 关联注记 |

（次级观察不计热区：sessions v3/v4 fork 重复系注释在案的零接触策略；`read_upstream_response` vs `_read_error_body` vs `aread()` 三种错误体排水策略差异归 §2/F-291。）

---

## 2. 错误处理一致性（全量审）

### 2.0 构造点总量与分组

`rg "raise|JSONResponse|HTTPException"` 全量扫描后，按**构造机制**分组逐点全审（组内全量，无抽样）：

| 组 | 机制 | 点数 | 代表位置 |
|---|---|---|---|
| G1 | `CodedHTTPException(...)` 直接构造（raise/return） | **43** | directory.py:37,41,46,50；upstream_errors.py:45,46,81,83,102,104,105；sessions.py:168,301,385,390,395,400,416,423,530,702,763；messages.py:315,1295,1326,1510,1525；routes/actions.py:51,94,99,104；actions.py:183（to_coded）；events.py:89,95；token_stream.py:113,119；read_groups.py:142,292,311,315,318,328,536 |
| G2 | `error_response(...)` 返回 | **19** | proxy.py:47；messages.py:278,840,1024,1226,1540,1550,1556,1591；_catalog_common.py:46,297,430；_read_passthrough.py:226；read_groups.py:283,496；write_groups.py:139,224,319,371 |
| G3 | selector 手写 `json_response({"code":...})`（中间件层无法 raise） | **5 发射点 + 6 error-dict 字面量** | selector.py:539(405 method_not_allowed),560(400 invalid_version_selector),596(405 method_not_applicable),614(directory 族 400),627(400 unsupported_version)；dict 构造 :672,681,688,692,698,704 + 常量 :206 |
| G4 | SSE 容量错误手写 `json_response`（非 error_response） | **2** | events.py:134-139；token_stream.py:177-183 |
| G5 | 信封内错误码字符串（不 raise） | **10** | `upstream_error_code_for_status` ×2（permissions:369/questions:351）+ `_DirFetchFailure(...)` 构造 ×8（各 4） |
| G6 | `ActionError.to_coded()` 转换族 | **1 转换点（7 子类）** | actions.py:170-186 |
| G7 | 框架原生渲染（无自定义 handler） | **2 隐式路径** | FastAPI `RequestValidationError` 422 `{"detail":[...]}`（如 sessions limit>1000，F-025 已立项）；未捕获异常 500（F-013/F-023/F-253 交叠面） |

**显式构造点合计 80（发射点口径）/ 86（含 6 个 selector dict 字面量），7 组。** 唯一注册的异常 handler = `CodedHTTPException`（errors.py:55-62，app.py:735）；全仓 **零 `JSONResponse`、零裸 `HTTPException`**（除基类）——机制面收敛良好。

### 2.1 三路并存（G1 handler 渲染 vs G2 error_response vs G3 手写）逐维评审

- **错误体形状**：三路最终都经 `gzip_util.json_response` 渲染 `{"code": ..., **fields}`——形状同源一致（errors.py:44-52 handler 内部就是 json_response）。**无 detail/code 双形逃逸**（唯 G7 框架原生 422/500 是第 4 形，契约归宿已由 F-025/F-152/F-153/F-154 立项覆盖，A10 不重复立项）。
- **gzip 行为**：三路全部经 `json_response` → **全部绕过 `MIN_GZIP_BYTES`/收益门**（gzip_util.py:110-123 无条件 compresslevel=6，对照 compress_if_beneficial :75-107 三门）。F-026 的 A10 增补：影响面不止 selector 小错误体——**全部 80 个发射点的错误体**在客户端带 `Accept-Encoding: gzip` 时均被压缩，典型 ~31-44 字节错误体（版本门 400 ~44B、invalid_directory ~31B）gzip 后更大（gzip 固定开销 ~18B+deflate 框架）。F-026 置信度升 high，维持 P3（CPU+字节浪费，无正确性影响；SSE/token 流已豁免）。
- **no-store**：错误体 no-store **仅 providers §12 面系统性加盖**（read_groups.py:285,294,308,313,317,320,330,349——§12.5.3「全部错误 no-store」）与 selector 405 method_not_applicable（:605）；其余各路由错误体（413/400/422/503）**不带 Cache-Control**。契约仅对 §12 有此要求 → 非违约，但「错误体缓存性」策略二分的意图未在契约总则说明——观察项（不入 findings）。
- **日志伴随**：routes/、errors.py、upstream_errors.py、gzip_util.py、proxy.py、selector.py **零 logger 调用**（rg 全量验证）——错误构造完全静默，可观测性全部下沉 access log（§9.1）+ middleware。一致（负向确认：无「有的记有的不记」漂移）。
- **Retry-After**：三种来源——busy_response 具名常量 `TRANSFORM_RETRY_AFTER_SECONDS=2`（体字段 `retry_after` + 头）；`_AUX_RETRY_AFTER="30"`（sessions:274）；events/token_stream 容量 503 **字面量 `"5"` ×2**（events.py:137、token_stream.py:180）——具名/字面混用 → F-292 收录。
- **raise vs return 混用**：同一语义（413 response_too_large）在 sessions/messages 走 raise（CodedHTTPException），在 read/write groups 走 return（error_response）——形状等价，机制二择无规范说明；风格观察（抽样：G2 全 19 点 + G1 413 相关 8 点逐点比对，形状全部 `{"code","limit"(或 limitBytes)}` 一致）。

### 2.2 错误体字段命名漂移（同一语义不同字面量）→ F-289

| 语义 | 字段名 | 位置 | 契约出处 |
|---|---|---|---|
| 响应字节上限值 | `limit` | write_groups:142,227,374；_read_passthrough:229；_catalog_common:299,432；sessions:170,532,765；messages:842,1026；read_groups:499（**13 发射点**） | v3 §10 语义（todo/children 设计文档 :152 例） |
| 同上（providers §12） | `limitBytes` | read_groups:334 | v4-contract:512 **冻结** `{"code":"response_too_large","limitBytes":...}` |
| expand 族字节上限 | `limitBytes` | messages:1226,1526,1593 | v3-contract:134,142 冻结 |
| 投影限额 | `limit`(名称)+`limitValue`(数值) | read_groups:356 | v4-contract:517 冻结 |
| 容量上限 | `limit`+`current`(+`bufferBytes`) | events:135、token_stream:178-180 | v2-contract:472-473 冻结 |

即：**同一 `response_too_large` 码在 §12 面带 `limitBytes`、在其余 13 点带 `limit`**——两形各自契约冻结（非 bug），但同码异字段是实现/契约两轨的历史分叉，客户端需双解析。另有 body 字段 snake_case（`retry_after`、`timeout_s`、`limit`、`current`）与 camelCase（`sessionID`、`limitBytes`、`queryDirectory`、`headerDirectory`）并存——同样全部有契约出处（v3:197 `transform_busy（retry_after + Retry-After）`）。结论：**命名漂移是「冻结的历史层积」而非实现走样**；F-289 定性 docs/quality P3（给 ocdroid 的解析提示 + 未来 major 的统一窗口）。

### 2.3 错误体排水策略不一致（cap-protected vs 无界 aread）→ F-291

上游错误体读取三种策略并存：
1. `_read_passthrough._read_error_body`（:137-154）：`read_with_cap` cap 保护，超限→503（§10.a:141 冻结）——read/write groups 全用；
2. `_catalog_common.read_upstream_response`（:132-143）：`await response.aread()` **无界排水**（:134）——agent/command/todo/children/diff/sessions(status)/messages 全用；
3. `discovery.fetch_global_root_sessions`（discovery.py:89）：`aread()` **无界排水**——questions/permissions/directories 发现步。

write_groups 注释（:198-203）明言 §10.a:141 的 cap 保护「applies to the §10.b unified behaviour」，但 catalog/discovery 链的上游 4xx/5xx 错误体仍整读入内存（无 max_response_bytes 界）。上游若回超大错误体（恶意/故障上游），2/3 链路无内存界（对照 A8/F-255 的 raw 族缓冲面，此为错误路径上的独立缺口）。P3 risk（mTLS 单客户端 + loopback 部署缓解；与 F-274 硬编码 limits 相关但不同路径）。

---

## 3. 命名与契约对齐漂移

| 术语 | 代码实现 | 契约 | 对齐判定 |
|---|---|---|---|
| `degraded`（item 级） | skeleton.py:1176 `single["degraded"] = partial or fallback` | v4 §13.4 公式 `partial ⇒ degraded`（单向蕴含，fallback 单独成立） | **对齐**（公式逐字实现；envelope 侧 sessions.py:475-484 `any(item.degraded) ∨ fallback` 同样对齐） |
| `partial` | skeleton.py:1175 + 各不可得分支置位（:898-905 注释群） | §13.2b 三态（业务 null 不触发；来源不可得才触发） | **对齐**（nullable 三态分支逐段核对 :1100-1177） |
| `degraded`（观测位） | `request.state.slimapi_degraded_503`（sessions:316）/`slimapi_sessions_source="http"`（:560）+ snapshot `degradedCounts` 键族 `degraded|kind|statusClass|bucket`（traffic_snapshot.py:123-164） | v4 §9.1/§9.2 | 对齐（A13 辖区，A10 不深审） |
| `envelope` | 三义：messages 打包（envelope.py `messages_envelope_bytes`）、聚合信封（questions/permissions items/errors/authoritativeDirectories）、sessions 信封（nextCursor/complete/degraded） | 各自契约段均有定义 | **多义但各自文档锚定**——无未定义使用；建议 INTERFACE_MAP 术语表（观察项） |
| `skeleton` | skeleton.py 投影族 + `SessionSkeletonV4`（§13.1） | v3 §4 / v4 §13 | 对齐 |
| `response_too_large` 字段 | §2.2 表 | 双形各自冻结 | **同码双字段名**——F-289 |
| `transform_busy` 体字段 `retry_after` | _catalog_common:46-52 / messages:278-284 | v3:197 冻结（retry_after + Retry-After 双通道） | 对齐（契约冻结的 snake_case 例外） |

**结论**：核心投影术语（degraded/partial/skeleton）**零漂移**；漂移集中在错误体字段命名层积（F-289）。

---

## 4. 死代码（F-024 复核 + E1 delta）

### 4.1 F-024 七项快照复核（A3 已 verified；A10 抽点确认仍在位）

快照直读确认全部在位：`_busy_sids`（hub.py:283 写点群）、`last_touch`（replay_log.py:216 等）、recycle 近 no-op（:495-513）、`directory_source`（qp_sweep.py:41,124-129）、`strip_hop_by_hop`+`HOP_BY_HOP`/`FORBIDDEN_*`（upstream.py:11-37,49-111）、`build_sessions_query` dead import（sessions.py:12）、`_V4_PARENT_RESERVED`（sessions.py:272）。**维持 verified/P3，裁决不变**（详见 D03 §8；A10 无翻案证据）。

### 4.2 E1「被依赖=∅」delta 清单（F-024/F-246 之外的新验证）

| 符号 | 位置 | rg 验证（src+tests） | 处置 |
|---|---|---|---|
| `_accepts_gzip` | token_stream.py:84-85 | 调用点 **∅**（含 tests）；`accepts_gzip` import :51 唯一消费者即它 | 新 F-290 |
| gzip 恒 identity 残链 | token_stream.py:203 `use_gzip = False` 常量死条件 → :213-226 compressor/encode gzip 分支不可达 → metrics `gzip_raw/compressed_bytes_total`（:224-225）写点不可达，`gzipRawBytesTotal`/`gzipCompressedBytesTotal`（subscriber.py:869-870）**恒 0 上报** | v3 终态「SSE 流恒 identity」注释自认（:25-35,197-201）；死链非契约面 | 新 F-290 |
| `import logging` | global_hub.py:11 | 文件内 `logging.` 使用 **∅**（logger 来自 logging_config.get_logger :25） | 并入 F-292 |
| `getattr(config,"max_expand_response_bytes", 8MiB)` 陈旧回退 | messages.py:1567-1569 | Settings 已有该字段（config.py:372-374，dataclass 必存在）→ 回退常量**永不触发**（lane C 注释自认「keep until integration」，集成已发生） | 并入 F-292 |

（`batch` 恒 null 键 traffic_snapshot——契约形状冻结项，非死代码；e1-13:279 已定论，不立项。）

**死代码净条数：F-024 维持 7 项 + 新增 2 项（F-290 gzip 残链计 1 项簇、F-292 含 2 个小项）= 10 项符号级，归 3 个 finding 载体。**

---

## 5. inventory todo_markers（2 处）处置评估

两处均位于 `sse/tokenstream/hub.py`：:663 `# TODO(§13.2): confirm live wire key casing for properties.part.` 与 :760 `# TODO(§13.2): confirm live wire key casing for properties fields.`——快照直读确认原文在位（:663 后 `props.get("part")`/`part.get("sessionID")`/:760 后 `props.get("field")` 全 camelCase）。

**A10 处置意见：与 F-030（verified/P3，A4+A6 双复核）完全一致**——上游真值（v1.18.18 schema session.ts:81-85,:612-620）已定论 camelCase，实现按键正确、零行为风险，TODO 属**已可解除而未解除的注释债**。处置=改写为定论注释（「casing 已对上游 v1.18.18 验证 = camelCase」）或直接删除；无新发现、无升级。附带 A6 已记的契约级观察（field≠"text" 门静默丢未来新字段）不在本 TODO 辖区。

---

## 6. 魔法数清单

过滤规则：剔除 HTTP 状态码字面量（400/404/413/502/503 惯用法）、config.py env 默认值（具名可调）、已具名模块常量。**存活清单 9 条**：

| # | 字面量 | 位置 | 性质 | 建议 |
|---|---|---|---|---|
| 1 | `connect:5.0, pool:5.0`（extensions dict 内） | _catalog_common.py:86,89 | read 超时参数化但 connect/pool 裸字面 | 具名（复用 upstream.py 或 config） |
| 2 | 客户端超时 `5.0/30.0/300.0/5.0` + limits `32/16` | upstream.py:43-44 | 全局客户端硬编码（**config 化缺口 = F-274 已立项**，此处记字面量面） | 随 F-274 一并 |
| 3 | `read_timeout=300.0` | command.py:45 | 路由层裸字面（唯一非 None 字面 read_timeout 路由） | 具名常量 |
| 4 | `4096` 目录长度上限 | directory.py:49 | 判界裸字面（docstring 有名） | 具名 |
| 5 | `128` request-id 长度上限 | middleware/request_id.py:50 | 判界裸字面 | 具名 |
| 6 | `512` 首行截断 | sse/hub_types.py:59 | 判界裸字面 | 具名 |
| 7 | `"5"` Retry-After 字面 ×2 | events.py:137、token_stream.py:180 | 对照具名的 2（busy）/「30」（aux） | 具名（容量族共享常量） |
| 8 | `8 * 1024 * 1024` 陈旧回退 | messages.py:1568 | **死默认**（§4.2） | 删 getattr 直取 |
| 9 | 退避 `1.0`→`30.0` | global_hub.py:999,1071,1089 | 重连退避上下界裸字面（局部变量 delay） | 具名（低优先） |

（`int(time.time()*1000)` epoch-ms 惯用、`// 100` 状态类归类、`compresslevel=6` gzip 级别惯例——不计。）

## 7. 类型注解密度（确定性抽样）

**算法**：`sorted(inventory.src_files)[2::3]`（字母序每第 3 个；1-based 位 3,6,…,69）。**样本 23 / 总量 71**。AST 测度：函数返回注解率 + 形参注解率。

- 样本合计：236 函数，**返回注解 89%**（212/236），**形参注解 75%**（330/435）。
- 弱点分布：路由 handler 层返回注解率 ~50%（read_groups 12/24、events 2/4、routes/actions 3/5、command/metrics 0/1——FastAPI handler 无返回注解惯例使然，类型仍由框架校验输入侧）；dbaux/lifecycle 形参 40%（41 函数内部助手）；tokenstream/subscriber 形参 50%（27 函数）。
- **配套缺口**：仓库**无任何静态类型/风格门禁**——pyproject 无 mypy/ruff 配置，check.sh = pytest + 路由文档 gate + compileall（scripts/check.sh 全读）。89%/75% 属自觉水平；无防退化门（观察项，随 F-292 建议记入 refactor backlog 候选）。

---

## 8. A10 负向结论（审计过、判定无问题）

- 路由 handler 层零 `JSONResponse`/裸 `HTTPException`；三路错误构造体形状同源（json_response）——除契约已立项的框架原生 422/500（F-025/F-152/F-153/F-154）外无第四形状逃逸。
- 错误构造点零日志调用——一致（观测下沉 access log），无部分漂移。
- degraded/partial/skeleton 术语实现与 v4 §13.2-13.4 逐条对齐（含 envelope 公式、三态、单向蕴含）。
- 32 个 read/write handler 的共享管线复用率 ~95%——thin-route 范式执行到位（`_catalog_common` dedup 先例被 agent/command/todo/children/diff 五路由复用）。
- permissions/questions 的 _collect 调度器虽逐字节复制，但行为一致无漂移（孪生债归档 F-286，非 defect）。
- config.py 魔法数全部 env 化具名（对照 §6 清单仅 #1-#3 涉及非 config 面）。

## 9. 产物索引

- 新建 findings：F-286（孪生）、F-287（read_groups v4 管线重复+write body 循环）、F-288（messages 助手复制+X-Request-ID 漂移）、F-289（错误体字段命名层积）、F-290（token_stream gzip 死链）、F-291（无界错误体排水 ×2）、F-292（卫生合集：dead import logging/陈旧回退/_strip_directory_query ×2/stanza ×10/Retry-After 字面）。
- 更新 findings：F-021（A10 证据链填充：upstream 区分 MessageNotFound 而侧车 404+sid 一律 session_not_found）、F-024（A10 复核注记 + delta 指针）、F-026（A10 影响面升级：三路全错误体绕 gzip 收益门，draft→verified 建议，置信 high）。
- INDEX.md 增补 A10 段。
