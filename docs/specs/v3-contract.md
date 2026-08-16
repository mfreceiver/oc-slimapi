# oc-slimapi v3 wire 契约（design-v3 rev6 — 终态）

> 状态：**DRAFT rev6**（2026-08-16；按用户终态决策重写：不透传完整功能 + 彻底抛弃全部自定义头 + 版本跳 3.0.0；融合五评 3B+3C 修复）。待六评 ≥9.5 转正式。
> 方向决策（不可推翻）：单入口终态——`/slimapi/**` 提供完整功能（实测使用集 + ocdroid 回执 12 写端点），catch-all 代理在 3.0.0 关闭，全部自定义头退役（含响应头 `X-Slimapi-Subscriber-ID`）。
> v2 权威：`docs/specs/v2-contract.md`。发版时序（两步走，已定）：sidecar 2.0.0 → ocdroid 3.0.0（smoke 门控）→ sidecar 3.0.0。
> 条款标 **[冻结]** 或 **[计划]**。

---

## §0 继承基线与差异清单 [冻结]

1. **v3 = v2 契约在基线 `v1.6.0`（commit `421ffb4`）的全量继承 + 本文件逐条差异覆盖**。凡未提及语义（端点集、投影、SSE 帧形、资源上限、错误映射、gzip 族划分、指纹、catalog TTL/coalescing、token stream 帧形等）**逐字沿用 v2**。
2. 差异面（且仅此）：§2 选择器；§3/§3a 发现与 health 双视图；§4 envelope；§5 directory 接受形式；§6 ETag/Vary/304；§7 SSE 订阅参数与 meta 帧；§9 观测与移除判据；§10 写路由收编；§11 catch-all 终局。
3. **两步走原子性**：
   - **sidecar 2.0.0** = v2/v3 并行。`available:[2,3]` 当且仅当 v3 全表面（§2–§7、§9 观测字段、§10 写路由 12 端点、§11 测试矩阵）就绪并通过门控——单一 release，无部分 v3。
   - **ocdroid 3.0.0** = 全量切 v3 + smoke 证据回收（双方门控 9.5 纪律）。
   - **sidecar 3.0.0** = 删 v2 管线/全部自定义头/catch-all 关闭（§8 终局）。发版前置 = ocdroid 3.0.0 已发 + §9.3 判据满足。

## §1 头退役范围（按方向拆分）[冻结]

| 头 | 方向 | 2.0.0（并行期） | 3.0.0（终态） |
|---|---|---|---|
| `X-Slimapi-Version` | 请求→sidecar | v2 语义请求照旧门禁；`v=3` 请求若带则忽略不报错 | **移除**：出现不报错、不解读（与 `?v=` 无关） |
| `X-Opencode-Directory` | 请求→sidecar | **仍解析**（v2 必需输入；v3 兼容形式，参与 §5.4 冲突判定） | **移除**：v3 消费集出现 → 400 `directory_header_retired`，提示改用 `?directory=` |
| `X-Next-Cursor` | sidecar→响应 | v2 语义请求照旧产出；v3 envelope 路由**不产出** | 移除（无产出方） |
| `X-Complete` | sidecar→响应 | 同上 | 移除 |
| `X-Slimapi-Subscriber-ID` | sidecar→响应 | v2 SSE 照旧产出；v3 SSE **不产出**（改 §7 meta 帧） | 移除 |

不退役：`X-Client-*`（客户端→sidecar 身份，非协议协商）、`X-Request-ID`（通用追踪）、`ETag`/`Vary`/`Cache-Control`/`Content-Encoding`（标准缓存头）、`X-Accel-Buffering`（SSE 缓冲控制）。

## §2 版本选择器状态机 [冻结]

选择器 = query 参数 `v`（sidecar **保留参数**，dispatch 层消费，**永不转发上游**——v2/v3 请求均剥离，见 §5.2）。

**词法**：合法值 = `^[1-9][0-9]*$`（无符号/无空白/无前导零/非空；`03`、`+3`、` 3`、`3.0`、空串 → 词法非法）。

| 请求形态 | 判定 | 行为 |
|---|---|---|
| 无 `v` | v2（缺省） | **现行 v2 管线，含 `X-Slimapi-Version` 头门禁**（缺头 → `version_required`）。缺省 ≠ "无头即 v2"。 |
| `v=3` | v3 | v3 语义；版本头若同时出现被忽略不报错。 |
| `v=2` | v2（显式） | 同缺省（含头门禁）；**`v` 剥离后其余 raw query 逐字转发**（§5.2）。 |
| `v` 词法合法但值不支持（4、1、0…） | 不支持 | 400 `{"code":"unsupported_version","supported":[2,3]}`（3.0.0 起 `supported:[3]`）。 |
| `v` 词法非法 / 多值不同 | 畸形 | 400 `{"code":"invalid_version_selector"}`。多值**同值**宽容折叠。 |
| `GET /slimapi/versions`（归一化路径） | 无条件豁免 | 不经选择器、不经版本头门禁（§3）；**非 GET → 405 + `Allow: GET`，优先级高于一切**。 |

SSE 两端点同表；畸形/不支持在**开流前** 400（普通 JSON 错误体）。

**退役后（3.0.0）冻结行为**：无 `v` / `v=2` → 400 `{"code":"unsupported_version","supported":[3]}`（端点存在、协议版本已退役；不静默 404）。`v=3` 正常。

## §3 发现端点 [冻结]

```
GET /slimapi/versions → 200
{"current": 3, "available": [2, 3],
 "capabilities": {"2": {"etag": true, "contentFingerprint": true, "thinRoutes": ["todo","children","diff"]},
                   "3": {"envelope": ["messages","sessions"], "directoryQuery": true, "versionHeaderOptional": true, "writeRoutes": true}},
 "sidecarVersion": "2.0.0"}
```

约束：`current ∈ available`；`available` 唯一升序；`capabilities` = map（key=版本字符串，value=object[string→bool|string[]]）；消费方必须忽略未知字段与未知 capability key；`sidecarVersion` = importlib 动态包版本；`Cache-Control: no-store`；无 ETag（低频小响应，304 收益取舍——`no-store` 与 ETag 可并存是本仓既有实践）；gzip 族 = `json_response` 无条件压缩族（同 health/ready/metrics），`Vary: Accept-Encoding`。3.0.0 起 `available:[3]`、`current:3`。

## §3a health/ready 双视图 [冻结]

- `/slimapi/health`：**根级** `slimapi_contract`（顶层字段，非嵌套于 `slimapi.*` 下——与 `health.py:24` 现状一致）。v2 语义请求 = 2；v3（`?v=3`）= 3。`server.api_version` 同步（2/3），`accepted_client_versions` 2.0.0=`[2,3]`、3.0.0=`[3,3]`。
- **`schema.version` 双视图冻结**：与 `server.api_version` 同源同值（`health.py:37` 现状），v2 视图 = 2、v3 视图 = 3——禁止出现 `server.api_version=3` 而 `schema.version=2` 的组合。`schema.clientMin/clientMax` 同步 accepted range。`schema.degraded` 语义不变。
- `/slimapi/ready`：**无任何 contract 标识字段**（`ready` 是部署探针非协议表面，`health.py:71-104` 现状）；ready 的 `schema` 节同样三字段（version/clientMin/clientMax），同源规则同上。
- 版本头门禁：health/ready 属 `/slimapi` 表面，v2 语义请求照旧要求 `X-Slimapi-Version`；v3 免。`/slimapi/versions` 是唯一豁免端点。

## §4 envelope [冻结]（仅 messages/sessions 列表；status 不 envelope 化）

1. **`GET /slimapi/messages/{sid}`（v=3）**：`{"items": [<v2 裸数组逐字>], "nextCursor": <string|null>}`——语义同 v2 `X-Next-Cursor`（游标不回退照旧）；无 `complete`。
2. **`GET /slimapi/sessions`（v=3）**：`{"items": [...], "complete": <bool>}`——语义同 v2 `X-Complete`，**继承其非权威性强制语言全文**（不得据此判定权威全集/空/覆盖完整性/冷启动结束）；无 `nextCursor`。
3. **`GET /slimapi/sessions/status`（v=3）**：不 envelope 化（v2 为 map，无分页头）；v3 差异仅 §5。
4. 边界：错误响应不 envelope；304 无 body（§6.4）；`?v=3` 与其他 query 任意组合。

## §5 directory 矩阵 [冻结]

1. **canonical：`?directory=` query**（v=3）；语义与 v2 头逐字相同（选工作目录实例；缺省 = sidecar 默认）。
2. **消费剥离规则（按参数拆分）**：
   - `v`：**无条件消费剥离**（v2 显式 / v3 均剥离，dispatch 层保留参数永不转发；§2）。
   - `directory`：消费/转换**限 v3**（消费集内转为上游 `X-Opencode-Directory` 转发——wire 等价）。v2 语义请求**不消费不剥离**，raw query 逐字转发上游（`proxy.py:182-203` 现状）；仅当 `v` 被剥离时，其余 query（含 `directory` 及一切参数）**保持编码、顺序、重复项逐字**。
   - 无 `v` 的 catch-all：query 逐字原样转发（现状不变）。
3. **消费集**（directory 参与路由/转发的端点）：`messages/{sid}`、`sessions`（列表+status）、`todo`/`children`/`diff`、`agent`/`command`、**§10 全部 12 写路由**、**catch-all 代理路径（v3 形态，2.0.0 过渡期）**。
4. **双现规则（仅消费集）**：query 与 `X-Opencode-Directory` 头同时出现——归一化后同值 → 正常；不同值 → 400 `{"code":"directory_conflict","queryDirectory":<str>,"headerDirectory":<str>}`。
5. **不在消费集**（宽容忽略任何 directory 形式，无冲突检查）：`questions`/`permissions`（跨目录自发现聚合）、`events`、`health`/`versions`/`ready`/`metrics`/`actions`/`directories`。
6. **stream 例外（守卫继承 + 多值前置）**：`/slimapi/stream` directory 为 no-op 但保留 v2 结构性冲突守卫（`token_stream.py:51-85` 逐字继承）。**多值处理前置**：任何消费集端点（含 stream）`?directory=` 多值——异值 → 400 `invalid_directory_selector`；同值折叠为单值。stream 单值化后再过守卫：query+头双现不同值 → `directory_not_allowed`（不改码继承）；纯 query → v2 语义为守卫拦截场景，v3 继承同码。
7. **3.0.0 终态**：消费集内 `X-Opencode-Directory` 头出现 → 400 `directory_header_retired`（§1）。

## §6 ETag / Vary / 304 [冻结]

1. **validator 域隔离**：`representation_version` 输入含 wire 版本标记——v2/v3 validator 互不匹配（envelope body 不同，防跨语义误 304）。
2. **Vary**：并行期 directory 头仍被接受 → 4 路由（messages/sessions/agent/command）`Vary: Accept-Encoding, X-Opencode-Directory` 照旧；`?v=`/`?directory=` 属 URI（进 cache key），不加 Vary。3.0.0 评估去 directory Vary 值。
3. ETag 生成/`If-None-Match`/`*`/judge 三态（coding-specific）沿用 v2；envelope 路由 canonical 输入 = envelope body。写路由（§10）无 ETag（非 GET 语义）。
4. **v3 304 头集合**：仅 `ETag` + `Vary` + `Cache-Control: no-store`；不复制 `X-Next-Cursor`/`X-Complete`——客户端从缓存 envelope 取。

## §7 SSE [冻结]

1. 两端点（`events`、`/stream`）接受 `?v=3`（§2 状态机）；帧名/帧形/`Last-Event-ID`/resync/heartbeat 零变化。组合：`?v=3&tokens=1` 合法（token 订阅参数 query 化已有）；`?v=3&directory=` 按 §5.3/§5.6 规则。
2. **subscriber id 传递机制改版（v3）**：v3 SSE **不产出** `X-Slimapi-Subscriber-ID` 响应头（v2 照旧）。改为**开流首帧元事件**：`event: slimapi.meta\ndata: {"subscriberId": "<id>", "tokens": <bool>}\n\n`——订阅建立后立即下发，早于任何业务帧/heartbeat。客户端从 meta 帧取 id（原响应头消费方迁移点；该 id 现无重连 API，仅观测/对账用途）。v2 SSE 帧序列不含 meta 帧（零变化）。
3. 选择器畸形/不支持 → 开流前 400 普通 JSON（§2）。

## §8 错误体与 catch-all 终局 [冻结]

1. 错误体 `{"code": …, …上下文字段}` 沿用 v2 全集；新增：`unsupported_version`（`supported`）、`invalid_version_selector`、`directory_conflict`（`queryDirectory`/`headerDirectory`）、`invalid_directory_selector`（多值异值）；3.0.0 追加 `directory_header_retired`。422 形态不变。
2. **catch-all 终局**：
   - 2.0.0：catch-all 照旧盲转（v2 语义）；v3 形态请求（`?v=3`）经 catch-all → 正常转发 + `directory` 按 §5.3 转换（过渡期兜底）。
   - 3.0.0：catch-all **关闭**。未收编路径（不在 `/slimapi` 路由表）→ 404 `{"code":"thin_route_not_found"}`；收编全集 = 既有读路由 + §10 写路由 + `versions`/`health`/`ready`/`metrics`/`actions`/`directories`。

## §9 观测与移除判据 [冻结]（对实现的硬要求）

1. **access log 加性字段**（`traffic_accounting.py`/`access_log.py`，随 2.0.0 交付）：
   - `wireVersion`: `"2" | "3" | null`（null = 无法归属：rejected/exempt/not_applicable）；
   - `selectorResult`: `"absent" | "v2" | "v3" | "rejected" | "exempt"（/versions）| "not_applicable"`；
   - `directoryForm`: `"query" | "header" | "both" | "absent" | null`（null = 非消费路由）；
   - `recordType`: `"request" | "sse_open" | "sse_close"`（SSE 建立与断开各一行；现有「每请求一行」消费口径改为按 `recordType=="request"` 过滤——`traffic-accounting.md` 手册同步更新，2.0.0 交付物）；
   - `lifecycleId`：服务端进程内单调分配（SSE 开流时生成，open/close 两行同值；`X-Request-ID` 可复用仅辅助关联，不作唯一键）。
2. **snapshot 聚合维度**（`traffic_snapshot.py`，留存 ≥30 天）：`date × selectorResult × wireVersion × directoryForm × recordType × statusClass × bucket` 计数矩阵。**SSE carry-in**：每日快照额外记录按 `wireVersion` 分维度的**窗口起点活跃连接数** `sseActive`（前日 close 未覆盖的 open 存量，含孤儿补记 close 后校正）。
3. **sidecar 3.0.0 启动判据（全部满足）**：
   - ① ocdroid 3.0.0 已发版且 smoke 证据回收全绿（§11 矩阵 + 双方 9.5 门控纪律）；
   - ② 连续 ≥7 天窗口：REST `selectorResult ∈ {absent,v2}` 成功请求为 0；且每日 `sseActive(v2) == 0` **且**窗口内 `wireVersion=="2"`（含 null 保守计 v2）的 `sse_open` 为 0（孤儿 close 不影响判据——只看 open 与 carry-in）；
   - ③ `directoryForm ∈ {header,both}` 成功请求为 0（含写路径——头形式全退役前置）；
   - ④ webui 生产流量全 `v=3`（webui 侧确认）；
   - ⑤ ocdroid 组书面确认。

## §10 写路由收编 [冻结]（12 端点，ocdroid 回执全集）

**设计原则**：受控代理——sidecar 不改写成功语义（**错误/幂等形状与 legacy 端点逐字对齐**：上游状态码 + 响应体透传），叠加 sidecar 保护（请求/响应 admission 上限 → 413/503 既有 code 形态）+ access log 审计 + `?v=`/`?directory=` 消费（§5.3）+ `Last-Request-ID` 类上游响应头透传。路径 = legacy 子路径加 `/slimapi` 前缀（迁移成本最低：改前缀即可）。

| # | v3 路由 | 上游 legacy | 方法 | 备注 |
|---|---|---|---|---|
| 1 | `/slimapi/session` | `/session` | POST | createSession |
| 2 | `/slimapi/session/{id}` | `/session/{id}` | PATCH | **双 shape 透传**：title（UpdateSessionRequest）/ archived 时间（UpdateSessionTimeRequest）——上游校验，sidecar 不区分 |
| 3 | `/slimapi/session/{id}` | `/session/{id}` | DELETE | deleteSession |
| 4 | `/slimapi/session/{id}/prompt_async` | `/session/{id}/prompt_async` | POST | sendMessage（既有匿名画像最大写项） |
| 5 | `/slimapi/session/{id}/abort` | `/session/{id}/abort` | POST | abortSession |
| 6 | `/slimapi/session/{id}/summarize` | `/session/{id}/summarize` | POST | summarizeSession |
| 7 | `/slimapi/session/{id}/fork` | `/session/{id}/fork` | POST | messageId 可选（query 或 body 透传，上游定义） |
| 8 | `/slimapi/session/{id}/revert` | `/session/{id}/revert` | POST | messageId+partId |
| 9 | `/slimapi/session/{id}/permissions/{permissionId}` | `/session/{id}/permissions/{permissionId}` | POST | respondPermission；directory 语义 §5.3 |
| 10 | `/slimapi/question/{requestId}/reply` | `/question/{requestId}/reply` | POST | replyQuestion |
| 11 | `/slimapi/question/{requestId}/reject` | `/question/{requestId}/reject` | POST | rejectQuestion |
| 12 | `/slimapi/session/{id}/command` | `/session/{id}/command` | POST | executeCommand；跨 workdir directory 语义 §5.3 |

**统一行为**：请求 body 原样透传（含 content-type），仅施加上限（`max_request_bytes` → 413）；上游响应（状态码+body+`X-Request-ID` 类追踪头）逐字透传；上游 5xx/网络错误 → 503 `upstream_unavailable`（与读路由一致）；`transform_busy` → 503 + `Retry-After`（复用池语义，若适用）。2.0.0 起全部就绪（§0.3 原子性）；3.0.0 后 legacy 原路径（catch-all）不可达（§8.2）。

## §11 测试矩阵 [冻结]

`available:[2,3]` 公告（2.0.0 发版）前必须全部通过：
1. selector 全状态（§2 表逐行，含词法边界 `03`/`+3`/` 3`/`3.0`/空/多值同值异值/405 优先级）；
2. directory 组合（无/仅 query/仅头/双现同值/双现冲突/非消费集忽略/questions-permissions 无 directory 断言/**stream 多值异值→invalid_directory_selector → 单值→守卫 directory_not_allowed 前置链**）；
3. ETag 4 路由（identity/gzip × 200/304，v2/v3 validator 隔离断言）；
4. envelope 两端点（nextCursor 语义/complete 非权威语言复制）；
5. 错误面（413/422/transform_busy/version_required + 五新 code）；
6. 两 SSE 端点（v3 开流/meta 首帧早于业务帧与 heartbeat/畸形开流前 400/lifecycle 记录/tokens=1 组合/双现 directory 守卫）；
7. `/versions` 端点（豁免/405/形状/capability/未知字段容忍）；
8. 观测字段（null/exempt/not_applicable/recordType/lifecycleId/sseActive carry-in）；
9. **写路由 12 端点回归**（每端点：happy 透传断言逐字节/上游 4xx 透传/上游 5xx→503/请求上限 413/directory query 转发断言/PATCH 双 shape）；
10. **v=2 catch-all 保序回归**（剥离 `v` 后 `directory` 及其余 raw query 编码/顺序/重复项逐字——`proxy.py:182-203` 行为锁定）；
11. **退役后形态模拟**（3.0.0 单测提前就位：无 v/`v=2` → 400 `supported:[3]`；头出现 → `directory_header_retired`；catch-all 404）；
12. 存量回归：旧 ocdroid 形态（无 v + header=2）逐字节不变。

## §12 里程碑 [计划——非契约承诺]

- **M1 = sidecar 2.0.0**（批次：A 选择器+发现+health 双视图+观测 / B envelope+ETag 域隔离+directory query / C 写路由 12 端点 / D SSE meta+subscriber 头停发+catch-all v 剥离规则；每批 rev-gpt 门控 9.5 → rev-sgpt stage 门 9.5 → 发版公告 `available:[2,3]`）。
- **M2 = ocdroid 3.0.0**（前置：本契约定稿 + sidecar 2.0.0；Phase 1 读迁移 minor → v3 全量 major；smoke 证据回收）。
- **M3 = sidecar 3.0.0**（§9.3 判据满足 → 删 v2 管线/全部自定义头/catch-all 关闭；`available:[3]`；发版 major）。
- 联调 tag 经跨会话通知送达 webui/ocdroid。
