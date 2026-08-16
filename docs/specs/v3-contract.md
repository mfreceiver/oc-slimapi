# oc-slimapi v3 wire 契约（design-v3 rev11 — 终态）

> 状态：**正式——2.0.0 实施基线**（design-v3 rev11；2026-08-16 十一轮评审收敛 6.8→8.3→8.9→9.2→9.1→8.3→9.1→9.4→9.4→9.4→9.7 PASS，rev-sgpt 十一评）。
> 方向决策（不可推翻）：单入口终态——`/slimapi/**` 提供完整功能（实测使用集：ocdroid StandardApi 全量端点），catch-all 3.0.0 关闭，全部自定义头退役。两步走（已定）：sidecar 2.0.0 → ocdroid 3.0.0（smoke 门控）→ sidecar 3.0.0。
> v2 权威：`docs/specs/v2-contract.md`。条款标 **[冻结]** 或 **[计划]**。

---

## §0 继承基线与差异清单 [冻结]

1. **v3 = v2 契约在基线 `v1.6.0`（commit `421ffb4`）的全量继承 + 本文件逐条差异覆盖**。凡未提及语义（投影、SSE 帧形、资源上限、错误映射、gzip 族、指纹、catalog TTL/coalescing、token stream 帧形等）**逐字沿用 v2**。
2. 差异面（且仅此）：§1 头退役汇总（§1 仅汇总 §§2/4/5/7 的头语义，无独立差异）；§2 选择器；§3/§3a 发现与 health 双视图；§4 envelope；§5 directory；§6 ETag/Vary/304；§7 SSE 订阅参数与 meta 帧；§8 错误与 catch-all 终局；§9 观测与移除判据；§10 读/写路由收编全集。
3. **两步走原子性**：
   - **sidecar 2.0.0** = v2/v3 并行。`available:[2,3]` 当且仅当 v3 全表面（§2–§7、§9 观测、**§10 全部收编路由（读 7 组 + 写 12 端点）**、§11 矩阵）就绪并通过门控。
   - **ocdroid 3.0.0** = 全量切 v3 + smoke 证据回收（双方 9.5 门控）。
   - **sidecar 3.0.0** = 删 v2 管线/全部自定义头/catch-all 关闭。前置 = ocdroid 3.0.0 已发 + §9.3 判据满足。

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
                   "3": {"envelope": ["messages","sessions"], "directoryQuery": true, "versionHeaderOptional": true, "writeRoutes": true, "readRoutes": ["file","vcs","find","providers","sessionSingle","activeSessions","globalHealth"]}},
 "sidecarVersion": "2.0.0"}
```

约束：`current ∈ available`；`available` 唯一升序；`capabilities` map（key=版本字符串）；消费方必须忽略未知字段；`sidecarVersion` = importlib 动态包版本；`Cache-Control: no-store`；无 ETag（收益取舍）；gzip 族 = `json_response` 无条件压缩族，`Vary: Accept-Encoding`。3.0.0 起 `available:[3]`。

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

## §5 directory 矩阵 [冻结]

1. **canonical：`?directory=` query**（v=3）；语义与 v2 头逐字相同。
2. **消费剥离规则（按参数拆分）**：
   - `v`：在 `/slimapi/**` 路由上**无条件消费剥离**（v2/v3 均剥离，永不转发；§2）。
   - `directory`：消费/转换**限 v3**（消费集内转上游 `X-Opencode-Directory`——wire 等价）。v2 语义请求不消费不剥离；显式 `v=2` 时 `v` 被剥离、其余 query（含 `directory`）**保持编码、顺序、重复项逐字**（`proxy.py:182-203` 锁定）。非-slim catch-all：一切 query 逐字原样透传（§2 作用域，零消费零剥离）。
3. **消费集**：`messages/{sid}`、`sessions`（列表+status）、`todo`/`children`/`diff`、`agent`/`command`、**§10 全部收编路由（按 §10.a/§10.b 各自 directory 列——以上游组声明为准：file=FileQuery、file/status=WorkspaceRoutingQuery、vcs=WorkspaceRoutingQuery、find=FindFileQuery、providers=WorkspaceRoutingQuery、session 单查=WorkspaceRoutingQuery 等）**。catch-all 代理**不在消费集**（§2 作用域：零消费零剥离）。
4. **双现规则（仅消费集）**：query 与 `X-Opencode-Directory` 头同时出现——归一化后同值 → 正常；不同值 → 400 `{"code":"directory_conflict","queryDirectory":<str>,"headerDirectory":<str>}`。
5. **不在消费集**（宽容忽略）：`questions`/`permissions`（跨目录自发现聚合）、`events`、`health`/`versions`/`ready`/`metrics`/`actions`/`directories`。
6. **stream 例外（v2 守卫逐字继承 + v3 多值前置新增）**：v2 单值行为 = **query-only directory 接受**（no-op，不报错）；仅 query 与头**同时存在且归一化后不同值** → 400 `directory_not_allowed`（`token_stream.py:51-69` 实际语义，rev6 表述有误以此为准）。v3 新增仅一条前置：`?directory=` **多值异值** → 400 `invalid_directory_selector`（消费集统一规则）；单值化后按上述 v2 规则判定。
7. **3.0.0 终态**：消费集内 directory 头出现 → 400 `directory_header_retired`。

## §6 ETag / Vary / 304 [冻结]

1. **validator 域隔离**：`representation_version` 输入含 wire 版本标记——v2/v3 validator 互不匹配。
2. **Vary**：并行期一切 **directory-sensitive 且接受 `X-Opencode-Directory` 头**的路由（原 4 路由 messages/sessions/agent/command + §10.a 收编 directory-消费读路由 + §10.b 写路由）统一 `Vary: Accept-Encoding, X-Opencode-Directory`；directory-不消费路由（active/global health 等）仅 `Vary: Accept-Encoding`。`?v=`/`?directory=` 属 URI 不加 Vary。3.0.0 头退役后全部路由去 directory Vary 值。
3. ETag/`If-None-Match`/`*`/judge 三态沿用 v2；envelope 路由 canonical 输入 = envelope body。**收编路由 ETag = §10.a 全集**（file/vcs/find/providers/session 单查/active/global health 七组全部 GET）；§10.b 写路由不启用。上游自身 ETag 头不透传（sidecar 生成域，§6.1 隔离）。
4. **v3 304 头集合**：仅 `ETag` + `Vary` + `Cache-Control: no-store`；不复制 `X-Next-Cursor`/`X-Complete`。

## §7 SSE [冻结]

1. 两端点（`events`、`/stream`）接受 `?v=3`；帧名/帧形/`Last-Event-ID`/resync/heartbeat 零变化。`?v=3&tokens=1` 合法；`?v=3&directory=` 按 §5.6。
2. **meta 帧（v3）**：v3 SSE 不产出 `X-Slimapi-Subscriber-ID`（v2 照旧）。开流**首帧**元事件：`event: slimapi.meta\ndata: {"subscriberId": "<id>", "tokens": <bool>}\n\n`——早于任何业务帧、heartbeat、**及 Last-Event-ID resync 回放**。**`tokens` 取值冻结**：`/events` = `tokens=1` 时 `true` 否则 `false`；`/stream` 恒 `true`。SSE 流不做 content-encoding（帧字节原样）。客户端从 meta 取 id（无重连 API，观测/对账用途——ocdroid 已回执确认无需求）。
3. 选择器畸形/不支持 → 开流前 400 普通 JSON。

## §8 错误体与 catch-all 终局 [冻结]

1. 错误体沿用 v2 全集；新增：`unsupported_version`（`supported`）、`invalid_version_selector`、`directory_conflict`（`queryDirectory`/`headerDirectory`）、`invalid_directory_selector`；3.0.0 追加 `directory_header_retired`。422 形态不变。
2. **catch-all 终局**：
   - 2.0.0：catch-all 照旧盲转——**零消费零剥离**（§2 作用域），一切 query（含 `?v=`/`?directory=`）逐字透传，**不因 `?v=3` 改变行为**；v3 消费者应使用 §10 收编路由，误经 catch-all 的请求按 v2 盲转处理（安全兜底，非 v3 语义）。
   - 3.0.0：catch-all **关闭**。未收编路径 → 404 `{"code":"thin_route_not_found"}`；**收编全集 = 既有读路由 + §10 读 7 组 + 写 12 端点 + `versions`/`health`/`ready`/`metrics`/`actions`/`directories`**（= ocdroid StandardApi 全量端点闭包 + 匿名消费方实测基线）。
3. **终态错误优先级（3.0.0，高→低）**：① 非 GET `/versions` → 405；② selector 400（`invalid_version_selector`/`unsupported_version`）；③ directory 400（`invalid_directory_selector` 多值异值 → `directory_conflict` 双现 → `directory_header_retired` 头出现）；④ 路由匹配失败 → 404 `thin_route_not_found`。低优先级仅在更高优先级全部通过后评估。

## §9 观测与移除判据 [冻结]

1. **access log 加性字段**（随 2.0.0 交付）：`wireVersion`（`"2"|"3"|null`，null=rejected/exempt/not_applicable）；`selectorResult`（`absent|v2|v3|rejected|exempt|not_applicable`——**catch-all 透传 = not_applicable**）；`directoryForm`（`query|header|both|absent|null`）；`recordType`（`request|sse_open|sse_close`——消费口径按 `recordType=="request"` 过滤，`traffic-accounting.md` 同步）；`lifecycleId`（进程内单调，open/close 同值）。
2. **snapshot 聚合**（留存 ≥30 天）：`date × selectorResult × wireVersion × directoryForm × recordType × statusClass × bucket` 计数矩阵。`sseActive` **聚合键 = `selectorResult`，维度覆盖 SSE 可达四值 `{v2, v3, absent, not_applicable}`**：每日快照记录各维度窗口起点活跃 SSE 存量（前日 close 未覆盖的 open 存量，孤儿补记 close 后校正）。`absent` = 无 `v` 的 SSE（§2 判 v2——旧客户端回归形态）；`not_applicable` = catch-all SSE；`rejected`/`exempt` 无 SSE 端点恒 0。
3. **sidecar 3.0.0 启动判据（全部满足，谓词显式化）**：
   - ① ocdroid 3.0.0 已发 + smoke 证据全绿；
   - ② 连续 ≥7 天窗口：REST 成功请求中 **`selectorResult ∈ {v2, absent}`** 为 0（exempt=发现端点自身、rejected=已拒请求、not_applicable=catch-all——三者由 ④/①另行覆盖，不参与本谓词，避免发现轮询永久阻塞判据）；且每日 `sseActive(v2 ∪ absent)` == 0 且窗口内 `selectorResult ∈ {v2, absent}` 的 `sse_open` 为 0；
   - ③ `directoryForm ∈ {header, both}` 成功请求为 0（含写路径）；
   - ④ **`selectorResult == "not_applicable"`（catch-all/passthrough）**：每日 `sseActive(not_applicable) == 0` **且**窗口内该维度 `sse_open` 为 0 **且**其成功 REST 为 0——全部流量已收敛 `/slimapi`；
   - ⑤ webui 生产流量全 `v=3`；⑥ ocdroid 组书面确认。

## §10 路由收编全集 [冻结]（读 7 组 + 写 12 端点；ocdroid StandardApi 全量 + 实测基线）

**设计原则**：受控代理——sidecar 不改写成功语义，叠加保护 + 审计 + `?v=`/`?directory=` 消费。路径 = legacy 路径加 `/slimapi` 前缀。**错误两级制（冻结）**：成功（2xx）状态码+body 逐字透传；**4xx 状态码+body 逐字透传**（客户端校验错误原样到达）；**上游 5xx/网络错误 → 503 `upstream_unavailable`**（显式例外，与既有读路由一致——legacy 直连会收到上游原始 5xx 码，此为已知迁移点）。**admission（冻结）**：请求超限 → 413（既有 `max_request_bytes` 语义）；响应超限 → 413 `response_too_large`（既有读路由 code 复用）；**纯 raw 受控代理不占 transform 池**（无投影变换，仅流式透传+上限检查，不产生 `transform_busy`）。

### 10.a 读路由（7 组，2.0.0 交付）

| 组 | v3 路由 | 上游 legacy | 方法 | directory | ETag |
|---|---|---|---|---|---|
| file | `/slimapi/file`、`/slimapi/file/content`、`/slimapi/file/status` | `/file*` | GET | 消费（`/file`、`/file/content`=FileQuery 族；**`/file/status`=WorkspaceRoutingQuery**） | **启用** |
| vcs | `/slimapi/vcs`、`/slimapi/vcs/status`、`/slimapi/vcs/diff` | `/vcs*`（instance.ts:46-48） | GET | 消费（WorkspaceRoutingQuery） | **启用** |
| find | `/slimapi/find/file` | `/find/file`（FindFileQuery） | GET | 消费 | **启用** |
| providers | `/slimapi/config/providers` | `/config/providers` | GET | **消费**（`WorkspaceRoutingQuery`，`groups/config.ts:38-40`） | **启用** |
| session 单查 | `/slimapi/session/{id}` | `/session/{id}` | GET | 消费 | **启用** |
| active | `/slimapi/api/session/active` | `/api/session/active` | GET | 不消费 | **启用** |
| global health | `/slimapi/global/health` | `/global/health` | GET | 不消费 | **启用** |

（既有 thin：sessions/messages/status/todo/children/diff/permission/question/agent/command 不重复列。）

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

## §12 里程碑 [计划]

- **M1 = sidecar 2.0.0**（批次：A 选择器+发现+health 双视图+观测 / B envelope+ETag 域隔离+directory query / C 读 7 组+写 12 路由 / D SSE meta+头停发+catch-all v 规则；每批 rev-gpt 门控 9.5 → rev-sgpt stage 门 9.5 → 发版）。
- **M2 = ocdroid 3.0.0**（前置：本契约定稿 + sidecar 2.0.0）。
- **M3 = sidecar 3.0.0**（§9.3 判据 → 删 v2/头/catch-all；`available:[3]`；major）。
