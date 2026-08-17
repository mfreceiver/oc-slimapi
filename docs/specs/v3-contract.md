# oc-slimapi v3 wire 契约（design-v3 rev11 — 终态）

> 状态：**正式——2.0.0 实施基线，3.0.0（m3 分支）为 v3-only 终态实施**（design-v3 rev11；2026-08-16 十一轮评审收敛 6.8→8.3→8.9→9.2→9.1→8.3→9.1→9.4→9.4→9.4→9.7 PASS，rev-sgpt 十一评）。
> 方向决策（不可推翻）：单入口终态——`/slimapi/**` 提供完整功能（实测使用集：ocdroid StandardApi 全量端点），catch-all 3.0.0 关闭，全部自定义头退役。两步走（已定）：sidecar 2.0.0 → ocdroid 3.0.0（smoke 门控）→ sidecar 3.0.0。
> v2 历史契约（已于 3.0.0 退役）：`docs/specs/v2-contract.md`——本文件 §0 所继承的基线语义以其为准；**当前 wire 权威 = 本文件**（3.0.0 起仅 v3 语义可达）。条款标 **[冻结]** 或 **[计划]**。
> 修订注记（2026-08-17，**4.0.0 包版本 MAJOR**；wire 版本**不变**，仍 v3、`?v=3` selector 不变）：expand 契约收编（设计权威 `docs/specs/design-expand.md` v5，rev-sgpt R4 APPROVED WITH CHANGES；实现已合入 969e9c6..ff71429，check.sh 全绿）。本次仅修订本文件不 bump wire——新增 §4a（消息投影缩减与 expandRefs）/§4b（expand 片段端点）；skeleton 投影缩减（减性）与 expand 片段端点（加性）均直接并入 v3 视图；§3 capabilities 增 `expand` 对象；§5 消费集纳入两 expand 路由；§6 注明 expand 无 ETag；§8 补 expand 错误码与路由内求值序；§10 读组计数 **7→8**（新增第 8 读组 `messages.expand`，raw 受控代理全节条款 carve-out）；§11 测试矩阵扩充。修订处标 **[4.0.0]**。

---

## §0 继承基线与差异清单 [冻结]

1. **v3 = v2 契约在基线 `v1.6.0`（commit `421ffb4`）的全量继承 + 本文件逐条差异覆盖**。凡未提及语义（投影、SSE 帧形、资源上限、错误映射、gzip 族、指纹、catalog TTL/coalescing、token stream 帧形等）**逐字沿用 v2**。
2. 差异面（且仅此）：§1 头退役汇总（§1 仅汇总 §§2/4/5/7 的头语义，无独立差异）；§2 选择器；§3/§3a 发现与 health 双视图；§4 envelope；**§4a 消息投影缩减与 expandRefs；§4b expand 片段端点（[4.0.0]）**；§5 directory；§6 ETag/Vary/304；§7 SSE 订阅参数与 meta 帧；§8 错误与 catch-all 终局；§9 观测与移除判据；§10 读/写路由收编全集（读组 **7→8**，新增第 8 读组 `messages.expand` [4.0.0]）。
3. **两步走原子性**：
   - **sidecar 2.0.0** = v2/v3 并行。`available:[2,3]` 当且仅当 v3 全表面（§2–§7、§9 观测、**§10 全部收编路由（读 7 组 + 写 12 端点）**、§11 矩阵）就绪并通过门控。
   - **ocdroid 3.0.0** = 全量切 v3 + smoke 证据回收（双方 9.5 门控）。
   - **sidecar 3.0.0** = 删 v2 管线/全部自定义头/catch-all 关闭。前置 = ocdroid 3.0.0 已发 + §9.3 判据满足。
4. **4.0.0 修订（2026-08-17；包版本 MAJOR、wire 仍 v3；[冻结]）**：skeleton 投影缩减（§4a）与 expand 片段端点（§4b）并入 v3 视图——§0.1「凡未提及语义逐字沿用 v2」对被 §4a/§4b 覆盖的投影/错误/上限语义**由本节覆盖优先**；**§10 读组计数 7→8**（历史"两步走"块内"读 7 组"保持 2.0.0 交付口径不变，计数修订以本条目与 §10 标题为准）。

## §1 头退役范围（按方向拆分）[冻结]

| 头 | 方向 | 2.0.0（并行期） | 3.0.0（终态） |
|---|---|---|---|
| `X-Slimapi-Version` | 请求→sidecar | v2 语义请求照旧门禁；`v=3` 请求若带则忽略不报错 | **移除**：出现不报错、不解读 |
| `X-Opencode-Directory` | 请求→sidecar | **仍解析**（v2 必需输入；v3 兼容形式，参与 §5.4 冲突判定） | **移除**：消费集出现 → 400 `directory_header_retired`，提示改用 `?directory=` |
| `X-Next-Cursor` | sidecar→响应 | v2 照旧产出；v3 envelope 路由**不产出** | 移除 |
| `X-Complete` | sidecar→响应 | 同上 | 移除 |
| `X-Slimapi-Subscriber-ID` | sidecar→响应 | v2 SSE 照旧产出；v3 SSE **不产出**（§7 meta 帧） | 移除 |

不退役：`X-Client-*`（客户端身份）、`X-Request-ID`（通用追踪）、`ETag`/`Vary`/`Cache-Control`/`Content-Encoding`（标准缓存头）、`X-Accel-Buffering`（SSE 缓冲控制）。

## §2 版本选择器状态机 [冻结]

**作用域**：selector 仅覆盖 `/slimapi/**` 路由。**非-slim catch-all（透传代理）不经 selector、不经版本头门禁、零消费零剥离**——一切 query 参数（含 `?v=`、`?directory=`）逐字透传上游（现状：`proxy.py:106-132`、INTERFACE_MAP:12）。`v` 的消费剥离**仅发生在 `/slimapi/**` 路由**（§5.2）。不存在"v3 形态 catch-all"——v3 消费者使用 §10 收编路由（2.0.0 全就绪）。3.0.0 catch-all 关闭后此类别消失。

选择器 = query 参数 `v`（sidecar **保留参数**，dispatch 层消费，**永不转发上游**——v2/v3 请求均剥离，见 §5.2）。

**词法**：合法值 = `^[1-9][0-9]*$`。`0`、`03`、`+3`、` 3`、`3.0`、空串 → **词法非法**（`invalid_version_selector`）。

| 请求形态（`/slimapi/**`） | 判定 | 行为 |
|---|---|---|
| 无 `v` | v2（缺省） | 现行 v2 管线，含 `X-Slimapi-Version` 头门禁（缺头 → `version_required`）。 |
| `v=3` | v3 | v3 语义；版本头若同时出现被忽略不报错。 |
| `v=2` | v2（显式） | 同缺省（含头门禁）；`v` 在 `/slimapi/**` 被消费（§5.2），不影响该路由其余参数的既有语义。 |
| `v` 词法合法但不在支持集（4、5…） | 不支持 | 400 `{"code":"unsupported_version","supported":[2,3]}`（3.0.0 起 `[3]`）。 |
| `v` 词法非法（含 `0`）/ 多值不同 | 畸形 | 400 `{"code":"invalid_version_selector"}`。多值**同值**宽容折叠。 |
| `GET /slimapi/versions`（归一化路径） | 无条件豁免 | 不经 selector、不经头门禁；**非 GET → 405 + `Allow: GET`，优先级高于一切**（§8.3）。 |

SSE 两端点同表；畸形/不支持在**开流前** 400（普通 JSON 错误体）。

**退役后（3.0.0）冻结行为**：无 `v` / `v=2` → 400 `{"code":"unsupported_version","supported":[3]}`（端点存在、协议版本已退役；不静默 404）。

## §3 发现端点 [冻结]

```
GET /slimapi/versions → 200
{"current": 3, "available": [2, 3],
 "capabilities": {"2": {"etag": true, "contentFingerprint": true, "thinRoutes": ["todo","children","diff"]},
                   "3": {"envelope": ["messages","sessions"], "directoryQuery": true, "versionHeaderOptional": true, "writeRoutes": true, "readRoutes": ["file","vcs","find","providers","sessionSingle","activeSessions","globalHealth"],
                         "expand": {"categories": [<§4b.2 12 类目表序>], "fragmentMaxBytes": <live 配置值>}}},
 "sidecarVersion": "2.0.0"}
```

约束：`current ∈ available`；`available` 唯一升序；`capabilities` map（key=版本字符串）；消费方必须忽略未知字段；`sidecarVersion` = importlib 动态包版本；`Cache-Control: no-store`；无 ETag（收益取舍）；gzip 族 = `json_response` 无条件压缩族，`Vary: Accept-Encoding`。3.0.0 起 `available:[3]`。**`capabilities["3"]["expand"]` 形状冻结（[4.0.0]）**：`categories` = 12 类目数组（§4b.2 表序；单一事实源 `src/oc_slimapi/traffic.py::EXPAND_CATEGORIES`，versions 广告与流量记账同源）、`fragmentMaxBytes` = `OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES` 运行时值（随配置变化，消费方每请求重读或容忍变化）。客户端以该 key 存在性作 expand 能力探测（勿用 `sidecarVersion` 字符串比较）。

## §3a health 双视图 [冻结]

- `/slimapi/health`：**根级** `slimapi_contract`（顶层字段，`health.py:24` 现状）。v2 语义请求 = 2；v3 = 3。`server.api_version` 同步（2/3），`accepted_client_versions` 2.0.0=`[2,3]`、3.0.0=`[3,3]`。
- **`schema.version` 双视图冻结**：与 `server.api_version` 同源同值（`health.py:37` 现状）——v2 视图 = 2、v3 视图 = 3，禁止 3/2 组合。`schema.clientMin/clientMax` 同步 accepted range。
- `/slimapi/ready`：无 contract 标识字段（部署探针，`health.py:71-104` 现状）；`schema` 节三字段同源规则同上。
- health/ready 属 `/slimapi` 表面：v2 语义请求照旧要求 `X-Slimapi-Version`；v3 免。`/slimapi/versions` 是唯一豁免端点。

## §4 envelope [冻结]（仅 messages/sessions 列表；status 不 envelope 化）

1. **`GET /slimapi/messages/{sid}`（v=3）**：`{"items": [<v2 裸数组逐字>], "nextCursor": <string|null>}`——语义同 v2 `X-Next-Cursor`（游标不回退照旧）；无 `complete`。
2. **`GET /slimapi/sessions`（v=3）**：`{"items": [...], "complete": <bool>}`——语义同 v2 `X-Complete`，**继承其非权威性强制语言全文**；无 `nextCursor`。
3. **`GET /slimapi/sessions/status`（v=3）**：不 envelope 化（map，无分页头）；v3 差异仅 §5。
4. 边界：错误响应不 envelope；304 无 body（§6.4）；`?v=3` 与其他 query 任意组合。

## §4a 消息投影缩减与 expandRefs [冻结]（[4.0.0]；设计权威 `docs/specs/design-expand.md` §4/§5）

**范围**：`GET /slimapi/messages/{sid}`（缺省 skeleton 与 `mode=merged`）默认投影的缩减语义。原则：skeleton 内字段**整字段保留或整字段省略，不做部分截断**；多字节按 UTF-8 编码字节计数。省略字段（除 §2.3 /full-only 清单）必携带 `expandRefs`。

1. **阈值与省略规则**：
   - `info.summary.diffs`：**总是省略** → `null` + 消息级 `info.expandRefs`（`category:"info_summary_diffs"`）；summary 其余 key 保留；仅省略时刻非 null/非空 list 才生成 ref。
   - `TextPart.text` / `ReasoningPart.text`：UTF-8 编码字节 > **2048** → 整字段 `null` + `omitted:["text"]` + `hasFull:true` + part 级 `expandRefs`（`part_text` / `part_reasoning`）；≤ 2048 原样内联。
   - `ToolPart.state.output/error`：现状阈值（4 KB/字段、16 KB/消息）不变，省略时新增 `expandRefs`（`part_state_output` / `part_state_error`）。
   - `tool state.input`/`metadata`/`attachments`（`object|null` / `object|null` / `object[]|null`）、`file.url`/`source`、`step-start`/`step-finish` 的 `snapshot`、compaction 整体超限：按 §4b.2 映射生成 `expandRefs`。
   - **/full-only**（省略但不生成 refs，显式穷举，design-expand §2.3）：`state.structured/result/raw`、tool input 非白名单单个 key、step-finish `reason/cost/tokens`、reasoning `metadata/time`、text `synthetic/ignored/time`、未知上游字段、`omitted:["*"]`（compaction 超限除外）、`thin_placeholder` 的 `omitted:["parts"]`。
2. **PatchPart（P0 修复）**：`files` 为上游 v1.18.16 `string[]`，skeleton **原样保留 verbatim**（+ 保留 `hash`）——不省略、不生成 refs、不做 dict 数组假设。
3. **`expandRefs` 为 sidecar 拥有键**：上游同名键**一律剥离/确定性替换**（skeleton 深拷贝时丢弃上游值后按本映射重建），永不透传、永不进 `omitted`。元素形状 `{category, messageID, partID?, href}`（数组）；去重：每 `(category, messageID, partID?)` 至多 1 条（tool input 多 key 省略 → 仅 1 条 `part_state_input_full`）；排序：category 字典序 + 同 category 内 partID 字典序（确定性）；`href` 含 `?v=3`，directory 由客户端追加；空/null 上游字段与未知字段不生成。基数上界：每 part 理论 ≤ 5（tool part 可同时省略 input/metadata/attachments/output/error）；每条 raw 120–180 B，gzip 后 <100 B/消息。
4. **可渲染性**：skeleton 模式 `text:null` 但携带 `expandRefs` 的 part **计为可渲染**（part 骨架 + 展开入口，非整页 placeholder）；消息级省略（diffs）不参与可渲染判定；`thin_placeholder` 语义不变（无任何可渲染 part 时注入）。
5. **merged 语义（best-effort，显式不承诺 null-free）**：候选集 = 占位消息 ∪ 任一 part 携带 `expandRefs` 的消息（消息级 `info.expandRefs` **不进入候选**——diffs 永不 batch 恢复）；**placeholder-first** 优先级（占位消息按页面顺序优先占用预算，行为与现状完全一致、不被 ref 候选挤占）；ref 候选仅在剩余预算内按页面顺序 best-effort 还原；**交集去重**：同一消息同时属于两类时按 mid 去重——占位身份优先、只占 1 个 slot、只发起 1 次 full fetch；还原范围与现有 merge 相同（仅替换 `parts`），`info.summary.diffs` 在 merged 输出**恒 `null` + expandRefs**；预算耗尽/源超限/上游失败/畸形 body → 该项保留 skeleton（含 `null` text + expandRefs），客户端有展开入口兜底——**这是特性而非缺陷**。
6. **指纹/ETag**：`contentFingerprint` 与列表 ETag 基于 skeleton 字节，缩减后自动确定性变化（测试锁定）。

## §4b expand 片段端点 [冻结]（[4.0.0]；设计权威 `docs/specs/design-expand.md` §2/§3/§6）

1. **路由与 selector**：`GET /slimapi/messages/{sid}/expand/{category}/{mid}?v=3[&directory=...]`（消息级，仅 `info_summary_diffs` 合法）与 `GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}?v=3[&directory=...]`（part 级，其余 11 类目）。两路由均挂 messages 面、require `?v=3`（v3-only）、消费 `?directory=`（§5.3 消费集）；`category` 以 str 接收 + 手工白名单校验（不用 Enum 路径参数）→ 400 `invalid_expand_category`。响应**无 ETag/304**（成功恒 200）、`Cache-Control: no-store`、`Vary: Accept-Encoding`、无自定义辅助头。
2. **Category 枚举（12 项，冻结；单一事实源 `src/oc_slimapi/traffic.py::EXPAND_CATEGORIES` 表序——versions capabilities 广告与流量记账同源）**：

   | category | 级别 | 适用 part 类型 | 返回 `data` |
   |---|---|---|---|
   | `info_summary_diffs` | 消息级 | — | `{diffs: <FileDiff[]> \| null}`（`info.summary.diffs`） |
   | `part_text` | part | text | `{text: string \| null}`（`part.text`） |
   | `part_reasoning` | part | reasoning | `{text: string \| null}`（`part.text`） |
   | `part_state_output` | part | tool | `{output: string \| null}`（`state.output`） |
   | `part_state_error` | part | tool | `{error: string \| null}`（`state.error`） |
   | `part_state_input_full` | part | tool | `{input: object \| null}`（`state.input`） |
   | `part_state_metadata_full` | part | tool | `{metadata: object \| null}`（**剥离 `diagnostics`**） |
   | `part_state_attachments` | part | tool | `{attachments: object[] \| null}`（`state.attachments`） |
   | `part_url` | part | file | `{url: string \| null}`（`part.url`） |
   | `part_source` | part | file | `{source: object \| null}`（`part.source`） |
   | `part_snapshot` | part | step-start / step-finish | `{snapshot: string \| null}`（`part.snapshot`，均 optional） |
   | `compaction_full` | part | compaction | 完整 compaction part（**剥离 `expandRefs`**） |

   **映射要点**：state 类 category 全部 **tool-only**（PatchPart `{type:"patch", hash, files: string[]}` 无 state；跨类型请求 → 400 `expand_category_mismatch`）；`part_snapshot` 覆盖 step-start 与 step-finish 两类型；无 `part_files_full`（`files` 为路径数组，skeleton 原样保留）；extractor 全部白名单构造，不暴露 category 外字段。
3. **冻结求值顺序链（路由内；独立于 §8.3 middleware 链——selector/directory 400 仍先于路由内错误）**：
   ```
   ① category ∈ 白名单（str 校验，12 项）        → 400 invalid_expand_category（附有效类目表序）
   ② 级别匹配（part 级缺 partID / 消息级 category 携带 partID）
                                               → 400 expand_category_mismatch（附 expectedLevel）
   ③ transform pool 准入（镜像 /full 吸收语义）  → 503 transform_busy（Retry-After）——先于一切 part 级错误
   ④ 共享 single-flight GET（与 direct /full、merged fan-out 同键去重；键含 scope+sid+mid+directory）：
      a. 发送/网络失败                          → 503 upstream_unavailable
      b. 上游错误状态（≥400）：drain 失败        → 503 upstream_unavailable；drain 成功按状态映射——
         sid 级 404                             → 404 session_not_found（带 sessionID）
         5xx                                   → 503 upstream_unavailable
         其余 4xx                              → 502 upstream_http_N
      c. 上游 2xx cap-read 网络失败              → 503 upstream_unavailable
         源 body 超 max_message_bytes          → 413 expand_source_too_large（limitBytes）——先于 JSON 解码（超限且畸形 body 仍 413）
      d. JSON 解码失败 / 顶层非 dict body        → 503 upstream_unavailable
   ⑤ parts 定位/形状（parts 缺失/null/标量/元素非对象/重复 partID）
                                               → 502 upstream_invalid_shape；partID 未命中 → 404 expand_target_not_found（附 reason:"part_missing"）
   ⑥ part.type ∈ category 适用集               → 400 expand_category_mismatch（附 expectedTypes）
   ⑦ 提取（白名单构造）+ 嵌套类型校验：字段存在但类型与冻结 schema 不符 → 502 upstream_invalid_shape；
      缺失或 JSON null                          → 200 + data 对应键 null
   ⑧ 片段字节 cap（序列化后、gzip 前）超 max_expand_response_bytes
                                               → 413 expand_fragment_too_large（limitBytes）
   ```
   **错误码唯一命名**：全文仅存在 `expand_target_not_found`（附 `reason:"part_missing"` 字段）与 `expand_category_mismatch` 两码，无独立 `part_missing`/`category_mismatch` 码。要点：503/413(源)/502 **可能先于** 404(part)/400(类型) 出现——"准入在前、先取后析"既有管线（与 /full 同构）的固有序，如实冻结为契约。
4. **响应 envelope（200）**：消息级 `{"category": <str>, "messageID": <mid>, "data": {...}}`；part 级 `{"category", "messageID", "partID", "data": {...}}`——`data` 形状按 ② 表对应键，缺失或显式 null 均 `data.<key> = null`。无 `contentFingerprint`（片段端点不适用）。成功响应携带 `messageID`/`partID` 供对账；**读当前态**（非 skeleton 快照）：当前态字段值；part 已删 → 404（`reason:"part_missing"`）；part 类型已变 → 400——客户端据此刷新 skeleton。
5. **配置**：`OC_SLIMAPI_MAX_EXPAND_RESPONSE_BYTES`（默认 **8 MiB**，界 **1 KiB–32 MiB** 含边界，非法值启动 `RuntimeError`）；全局内存信封按 `max(max_response_bytes, max_expand_response_bytes) × max_transforms` 计账（expand worker 同时持有原始 full-message bytes、解析对象、片段序列化 bytes、可选 gzip bytes），expand cap 配置导致信封超限的组合 **startup 拒绝**。
6. **观测**：traffic ledger/snapshot/metrics 新增 `expand` 块——按 category × status 聚合（12 类目白名单），伪造/非法 category 折叠进固定 `invalid` 桶（防基数 DoS）；`traffic-snapshot-YYYY-MM-DD.jsonl` 持久化含 expand。

## §5 directory 矩阵 [冻结]

1. **canonical：`?directory=` query**（v=3）；语义与 v2 头逐字相同。
2. **消费剥离规则（按参数拆分）**：
   - `v`：在 `/slimapi/**` 路由上**无条件消费剥离**（v2/v3 均剥离，永不转发；§2）。
   - `directory`：消费/转换**限 v3**（消费集内转上游 `X-Opencode-Directory`——wire 等价）。v2 语义请求不消费不剥离；显式 `v=2` 时 `v` 被剥离、其余 query（含 `directory`）**保持编码、顺序、重复项逐字**（`proxy.py:182-203` 锁定）。非-slim catch-all：一切 query 逐字原样透传（§2 作用域，零消费零剥离）。
3. **消费集**：`messages/{sid}`（含 **expand 两路由，§4b——同样消费 `?directory=`** [4.0.0]）、`sessions`（列表+status）、`todo`/`children`/`diff`、`agent`/`command`、**§10 全部收编路由（按 §10.a/§10.b 各自 directory 列——以上游组声明为准：file=FileQuery、file/status=WorkspaceRoutingQuery、vcs=WorkspaceRoutingQuery、find=FindFileQuery、providers=WorkspaceRoutingQuery、session 单查=WorkspaceRoutingQuery 等）**。catch-all 代理**不在消费集**（§2 作用域：零消费零剥离）。
4. **双现规则（仅消费集）**：query 与 `X-Opencode-Directory` 头同时出现——归一化后同值 → 正常；不同值 → 400 `{"code":"directory_conflict","queryDirectory":<str>,"headerDirectory":<str>}`。
5. **不在消费集**（宽容忽略）：`questions`/`permissions`（跨目录自发现聚合）、`events`、`health`/`versions`/`ready`/`metrics`/`actions`/`directories`。
6. **stream 例外（v2 守卫逐字继承 + v3 多值前置新增）**：v2 单值行为 = **query-only directory 接受**（no-op，不报错）；仅 query 与头**同时存在且归一化后不同值** → 400 `directory_not_allowed`（`token_stream.py:51-69` 实际语义，rev6 表述有误以此为准）。v3 新增仅一条前置：`?directory=` **多值异值** → 400 `invalid_directory_selector`（消费集统一规则）；单值化后按上述 v2 规则判定。
7. **3.0.0 终态**：消费集内 directory 头出现 → 400 `directory_header_retired`。

## §6 ETag / Vary / 304 [冻结]

1. **validator 域隔离**：`representation_version` 输入含 wire 版本标记——v2/v3 validator 互不匹配。
2. **Vary**：并行期一切 **directory-sensitive 且接受 `X-Opencode-Directory` 头**的路由（原 4 路由 messages/sessions/agent/command + §10.a 收编 directory-消费读路由 + §10.b 写路由）统一 `Vary: Accept-Encoding, X-Opencode-Directory`；directory-不消费路由（active/global health 等）仅 `Vary: Accept-Encoding`。`?v=`/`?directory=` 属 URI 不加 Vary。3.0.0 头退役后全部路由去 directory Vary 值。
3. ETag/`If-None-Match`/`*`/judge 三态沿用 v2；envelope 路由 canonical 输入 = envelope body。**收编路由 ETag = §10.a 全集**（file/vcs/find/providers/session 单查/active/global health 七组全部 GET）；§10.b 写路由不启用。上游自身 ETag 头不透传（sidecar 生成域，§6.1 隔离）。**expand 两路由（§4b）不在 ETag 全集 [4.0.0]**——成功恒 200、无 304/`If-None-Match` 判定；其响应头冻结（`Cache-Control: no-store`、`Vary: Accept-Encoding`、无 ETag、无自定义辅助头）见 §4b.1。
4. **v3 304 头集合**：仅 `ETag` + `Vary` + `Cache-Control: no-store`；不复制 `X-Next-Cursor`/`X-Complete`。

## §7 SSE [冻结]

1. 两端点（`events`、`/stream`）接受 `?v=3`；帧名/帧形/`Last-Event-ID`/resync/heartbeat 零变化。`?v=3&tokens=1` 合法；`?v=3&directory=` 按 §5.6。
2. **meta 帧（v3）**：v3 SSE 不产出 `X-Slimapi-Subscriber-ID`（v2 照旧）。开流**首帧**元事件：`event: slimapi.meta\ndata: {"subscriberId": "<id>", "tokens": <bool>}\n\n`——早于任何业务帧、heartbeat、**及 Last-Event-ID resync 回放**。**`tokens` 取值冻结**：`/events` = `tokens=1` 时 `true` 否则 `false`；`/stream` 恒 `true`。SSE 流不做 content-encoding（帧字节原样）。客户端从 meta 取 id（无重连 API，观测/对账用途——ocdroid 已回执确认无需求）。
3. 选择器畸形/不支持 → 开流前 400 普通 JSON。

## §8 错误体与 catch-all 终局 [冻结]

1. 错误体沿用 v2 全集；新增：`unsupported_version`（`supported`）、`invalid_version_selector`、`directory_conflict`（`queryDirectory`/`headerDirectory`）、`invalid_directory_selector`；3.0.0 追加 `directory_header_retired`。422 形态不变。**4.0.0 追加 expand 错误码（求值序上下文见 §4b.3）**：`invalid_expand_category`（附有效类目表序）、`expand_category_mismatch`（`expectedLevel`/`expectedTypes`）、`expand_target_not_found`（附 `reason:"part_missing"`）、`expand_source_too_large`/`expand_fragment_too_large`（`limitBytes`）、`upstream_invalid_shape`；复用既有码：`session_not_found`（sid 404）、`upstream_http_N`、`upstream_unavailable`、`transform_busy`（`retry_after` + `Retry-After`）。
2. **catch-all 终局**：
   - 2.0.0：catch-all 照旧盲转——**零消费零剥离**（§2 作用域），一切 query（含 `?v=`/`?directory=`）逐字透传，**不因 `?v=3` 改变行为**；v3 消费者应使用 §10 收编路由，误经 catch-all 的请求按 v2 盲转处理（安全兜底，非 v3 语义）。
   - 3.0.0：catch-all **关闭**。未收编路径 → 404 `{"code":"thin_route_not_found"}`；**收编全集 = 既有读路由 + §10 读 7 组 + 写 12 端点 + `versions`/`health`/`ready`/`metrics`/`actions`/`directories`**（= ocdroid StandardApi 全量端点闭包 + 匿名消费方实测基线）。
3. **终态错误优先级（3.0.0，高→低）**：① 非 GET `/versions` → 405；② selector 400（`invalid_version_selector`/`unsupported_version`）；③ directory 400（`invalid_directory_selector` 多值异值 → `directory_conflict` 双现 → `directory_header_retired` 头出现）；④ 路由匹配失败 → 404 `thin_route_not_found`。低优先级仅在更高优先级全部通过后评估。**expand 两路由（§4b）的 400/404/413/502/503 属路由内错误 [4.0.0]**，按 §4b.3 冻结求值序评估；本链的 selector/directory 层（②③）仍先于这些路由内错误。

## §9 观测与移除判据 [冻结]

1. **access log 加性字段**（随 2.0.0 交付）：`wireVersion`（`"2"|"3"|null`，null=rejected/exempt/not_applicable）；`selectorResult`（`absent|v2|v3|rejected|exempt|not_applicable`——**catch-all 透传 = not_applicable**）；`directoryForm`（`query|header|both|absent|null`）；`recordType`（`request|sse_open|sse_close`——消费口径按 `recordType=="request"` 过滤，`traffic-accounting.md` 同步）；`lifecycleId`（进程内单调，open/close 同值）。
2. **snapshot 聚合**（留存 ≥30 天）：`date × selectorResult × wireVersion × directoryForm × recordType × statusClass × bucket` 计数矩阵。`sseActive` **聚合键 = `selectorResult`，维度覆盖 SSE 可达四值 `{v2, v3, absent, not_applicable}`**：每日快照记录各维度窗口起点活跃 SSE 存量（前日 close 未覆盖的 open 存量，孤儿补记 close 后校正）。`absent` = 无 `v` 的 SSE（§2 判 v2——旧客户端回归形态）；`not_applicable` = catch-all SSE；`rejected`/`exempt` 无 SSE 端点恒 0。
3. **sidecar 3.0.0 启动判据（全部满足，谓词显式化）**：
   - ① ocdroid 3.0.0 已发 + smoke 证据全绿；
   - ② 连续 ≥7 天窗口：REST 成功请求中 **`selectorResult ∈ {v2, absent}`** 为 0（exempt=发现端点自身、rejected=已拒请求、not_applicable=catch-all——三者由 ④/①另行覆盖，不参与本谓词，避免发现轮询永久阻塞判据）；且每日 `sseActive(v2 ∪ absent)` == 0 且窗口内 `selectorResult ∈ {v2, absent}` 的 `sse_open` 为 0；
   - ③ `directoryForm ∈ {header, both}` 成功请求为 0（含写路径）；
   - ④ **`selectorResult == "not_applicable"`（catch-all/passthrough）**：每日 `sseActive(not_applicable) == 0` **且**窗口内该维度 `sse_open` 为 0 **且**其成功 REST 为 0——全部流量已收敛 `/slimapi`；
   - ⑤ webui 生产流量全 `v=3`；⑥ ocdroid 组书面确认。

## §10 路由收编全集 [冻结]（读 **8** 组 + 写 12 端点；ocdroid StandardApi 全量 + 实测基线；**读组计数 7→8 修订 [4.0.0]**）

**设计原则**：受控代理——sidecar 不改写成功语义，叠加保护 + 审计 + `?v=`/`?directory=` 消费。路径 = legacy 路径加 `/slimapi` 前缀。**错误两级制（冻结）**：成功（2xx）状态码+body 逐字透传；**4xx 状态码+body 逐字透传**（客户端校验错误原样到达）；**上游 5xx/网络错误 → 503 `upstream_unavailable`**（显式例外，与既有读路由一致——legacy 直连会收到上游原始 5xx 码，此为已知迁移点）。**admission（冻结）**：请求超限 → 413（既有 `max_request_bytes` 语义）；响应超限 → 413 `response_too_large`（既有读路由 code 复用）；**纯 raw 受控代理不占 transform 池**（无投影变换，仅流式透传+上限检查，不产生 `transform_busy`）。**4.0.0 carve-out [冻结]**：本节"纯 raw 受控代理不占 transform 池 / 成功 2xx 状态码+body 逐字透传 / 错误两级制"条款**对第 8 读组 `messages.expand`（§4b 两路由）不适用**——该组为**转换端点**：变换成功 body（§4b.4）、生成自有错误码（§4b.3，非逐字透传）、占用 transform pool（池满 503 `transform_busy` + `Retry-After`）；其余 7 组（含其 4xx 逐字透传与不占池条款）**完全不变**。

### 10.a 读路由（8 组：既有 7 组 2.0.0 交付 + `messages.expand` 4.0.0 交付 [4.0.0]）

| 组 | v3 路由 | 上游 legacy | 方法 | directory | ETag |
|---|---|---|---|---|---|
| file | `/slimapi/file`、`/slimapi/file/content`、`/slimapi/file/status` | `/file*` | GET | 消费（`/file`、`/file/content`=FileQuery 族；**`/file/status`=WorkspaceRoutingQuery**） | **启用** |
| vcs | `/slimapi/vcs`、`/slimapi/vcs/status`、`/slimapi/vcs/diff` | `/vcs*`（instance.ts:46-48） | GET | 消费（WorkspaceRoutingQuery） | **启用** |
| find | `/slimapi/find/file` | `/find/file`（FindFileQuery） | GET | 消费 | **启用** |
| providers | `/slimapi/config/providers` | `/config/providers` | GET | **消费**（`WorkspaceRoutingQuery`，`groups/config.ts:38-40`） | **启用** |
| session 单查 | `/slimapi/session/{id}` | `/session/{id}` | GET | 消费 | **启用** |
| active | `/slimapi/api/session/active` | `/api/session/active` | GET | 不消费 | **启用** |
| global health | `/slimapi/global/health` | `/global/health` | GET | 不消费 | **启用** |
| **messages.expand** | **`GET /slimapi/messages/{sid}/expand/{category}/{mid}`、`GET /slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`**（§4b；**转换端点**，**非 raw 受控代理**） | `/session/{sid}/message/{mid}`（singleflight 共享 GET） | GET | 消费（§5.3） | **不启用**（恒 200） |

（既有 thin：sessions/messages/status/todo/children/diff/permission/question/agent/command 不重复列。）

**§10.b「统一行为」段对本节的适用性 [冻结]**：上游响应头透传集合冻结（`Content-Type`、`Location`、`Retry-After`、上游 `X-Request-ID`/`Last-Request-ID`）、content-coding 规则（上游 `Content-Encoding` 不透传、实体字节口径）、错误两级制、admission 冻结条款——**均同样适用于本节全部读路由**（"统一行为"是 §10 全集行为，非仅写路由）。**例外 [4.0.0]**：第 8 读组 `messages.expand` 不适用其中"错误两级制"与"上游响应头透传"——其响应头由 sidecar 全权拥有（§4b.1）、错误映射冻结于 §4b.3。

**错误 body 读取的资源上限 [冻结]**：错误两级制的"4xx status+body 逐字透传"以 body 可安全读入为前提——错误路径 body 读取同样受 response cap（`max_response_bytes`）保护；超限时无法逐字透传，降级为 503 `upstream_unavailable`（资源保护优先于逐字义务）。**投影路由（session 单查）的投影执行域**：仅当上游响应为 2xx 且 body 为合法 JSON object 时投影；其余一切状态（含 204 空 body、3xx 非 JSON）逐字透传不投影。投影属转换工作，须按 admission 冻结条款经转换池 offload 执行（事件循环不承载 JSON 解析/序列化）。

### 10.b 写路由（12 端点，2.0.0 交付；**directory 列 = 全部消费**——上游 `groups/session.ts:203-397`、`groups/question.ts:32-48` 均声明 `WorkspaceRoutingQuery`）

| # | v3 路由 | 上游 | 方法 | 备注 |
|---|---|---|---|---|
| 1 | `/slimapi/session` | `/session` | POST | createSession |
| 2 | `/slimapi/session/{id}` | `/session/{id}` | PATCH | **双 shape 透传**：title/metadata/permission（UpdatePayload）与 time.archived——上游校验，sidecar 不区分 |
| 3 | `/slimapi/session/{id}` | `/session/{id}` | DELETE | deleteSession |
| 4 | `/slimapi/session/{id}/prompt_async` | `/session/{id}/prompt_async` | POST | PromptPayload 透传 |
| 5 | `/slimapi/session/{id}/abort` | `/session/{id}/abort` | POST | abortSession |
| 6 | `/slimapi/session/{id}/summarize` | `/session/{id}/summarize` | POST | SummarizePayload 透传 |
| 7 | `/slimapi/session/{id}/fork` | `/session/{id}/fork` | POST | ForkPayload；**`messageID` 为可选 body JSON 字段**（groups/session.ts:49-74 ForkPayload=omit(ForkInput,"sessionID")），非 query |
| 8 | `/slimapi/session/{id}/revert` | `/session/{id}/revert` | POST | RevertPayload（messageId+partId body） |
| 9 | `/slimapi/session/{id}/permissions/{permissionId}` | 同名 | POST | respondPermission |
| 10 | `/slimapi/question/{requestId}/reply` | 同名 | POST | replyQuestion |
| 11 | `/slimapi/question/{requestId}/reject` | 同名 | POST | rejectQuestion |
| 12 | `/slimapi/session/{id}/command` | `/session/{id}/command` | POST | CommandPayload 透传 |

**统一行为**（依据上游快照 **opencode v1.18.16**（`opencode-src/current`，后续 repoint 时本节逐条复核））：请求 body（含 content-type）透传；上游**响应头透传集合冻结** = `Content-Type`、`Location`（上游 3xx 重定向：状态码 + body 均逐字透传，sidecar 不跟随不重写）、`Retry-After`、上游 `X-Request-ID`/`Last-Request-ID` 追踪头；其余上游自定义头不透传。**content-coding 规则**：上游 `Content-Encoding` 不透传——上游响应经解码后取实体字节（httpx 自动解码，与既有读路由一致），admission 按实体字节计，sidecar 按自身 gzip 族重新编码并生成自己的 `Content-Encoding`/`ETag`（"body 逐字透传"均指实体字节）。**ETag 冻结子集**：§10.a 全部 GET 路由启用（含 file/content——大正文 304 收益最大；受既有 gzip 受益门与 validator 规则约束）；§10.b 写路由不启用。错误两级制与 admission 冻结条款适用全集。

## §11 测试矩阵 [冻结]

`available:[2,3]` 公告前必须全部通过：
1. selector 全状态（§2 表逐行，词法边界含 `0`/`03`/`+3`/` 3`/`3.0`/空/多值同值异值/405 优先级/**catch-all 携带 `?v=2/3` 逐字透传断言**）；
2. directory 组合（无/仅 query/仅头/双现同值/双现冲突/非消费集忽略/questions-permissions 断言/**stream：query-only 接受 no-op、双现异值 directory_not_allowed、多值异值前置 invalid_directory_selector**）；
3. ETag 4 路由 + **§10.a 全集收编 GET 路由**（identity/gzip × 200/304，v2/v3 validator 隔离）；
4. envelope 两端点（nextCursor/complete 非权威语言）；
5. 错误面（413/422/version_required + 四新 code）；
6. 两 SSE 端点（v3 开流/**meta 首帧先于业务帧/heartbeat/resync 回放**/tokens 端点映射断言/lifecycle/`tokens=1` 组合/stream directory 守卫）；
7. `/versions`（豁免/405/形状/readRoutes capability/未知字段容忍）；
8. 观测字段（null/exempt/not_applicable/recordType/lifecycleId/**sseActive 四维 `{v2,v3,absent,not_applicable}`：无-v 旧客户端 SSE 归 absent 断言、跨日 carry-in 对账 `sseActive[D+1,k] = sseActive[D,k] + sse_open[D,k] − matched_sse_close[D,k]`（k=维度；§9.2 孤儿 close 校正适用；测试序列必须含"当日新开跨日未关"与"跨日后关闭"两种）、open/close 生命周期配对**）；
9. **读路由 7 组回归**（每组：happy 透传逐字节/上游 4xx 透传/上游 5xx→503/响应超限 413/directory query 转发断言/幂等 GET ETag 往返（启用子集））；
10. **写路由 12 端点回归**（每端点：happy/4xx 透传/5xx→503/请求超限 413/directory 转发/PATCH 双 shape/fork messageID body 字段）；
11. **catch-all raw-query 保序回归**（一切 query 含 `v`/`directory` 编码/顺序/重复项逐字透传——`proxy.py:182-203` 锁定，**无任何剥离**；携带 `?v=2/3` 断言同款）；
12. 退役形态模拟（无 v/`v=2` → 400 `[3]`；头 → `directory_header_retired`；catch-all 404；**§8.3 优先级链逐级断言**）；
13. 存量回归：旧 ocdroid 形态（无 v + header=2）逐字节不变。
14. **expand 回归矩阵（[4.0.0]，design-expand §12 全集）**：selector/directory（缺 v / v=2 / 畸形 → 400 先于路由内错误；expand 消费 directory）；求值序全链（池满 503 先于 part 404；源超限 413 先于解码；sid 404 → `session_not_found`；超限且畸形 body → 413；错误码 JSON 精确断言——无独立 `part_missing`/`category_mismatch` 码）；级别匹配（消息级带 partID → 400；partID 未命中 → 404 + `reason:"part_missing"`；patch part 请求 state 类 → 400；step-finish 请求 `part_snapshot` → 200）；extractor（12 category 各正反例 + 类型错配矩阵；解码失败/顶层非 dict → 503 与解析成功后结构畸形 → 502 显式区分；缺失 vs 显式 null 双断言 → 200 + data 键 null；metadata 无 diagnostics 泄露；compaction_full 剥离 expandRefs）；双上限边界（源 1 字节 / 片段 8 MiB 两侧）；singleflight（同消息同/异 category 并发 1 次上游 GET、expand 与 /full 共享、1s grace 后重取）；skeleton/引用（阈值两侧、引用排序去重、可渲染、diffs 恒 null + ref、patch files verbatim）；merged（placeholder-first、预算外保留 null+refs、diffs 永不 batch 还原、交集去重）；配置（cap 超界 startup 拒绝、aggregate 信封超限拒绝）；capabilities 广告形状；check_routes_doc 语义关键词检查通过（12 category 数量一致）。

## §12 里程碑 [计划]

- **M1 = sidecar 2.0.0**（批次：A 选择器+发现+health 双视图+观测 / B envelope+ETag 域隔离+directory query / C 读 7 组+写 12 路由 / D SSE meta+头停发+catch-all v 规则；每批 rev-gpt 门控 9.5 → rev-sgpt stage 门 9.5 → 发版）。
- **M2 = ocdroid 3.0.0**（前置：本契约定稿 + sidecar 2.0.0）。
- **M3 = sidecar 3.0.0**（§9.3 判据 → 删 v2/头/catch-all；`available:[3]`；major）。
