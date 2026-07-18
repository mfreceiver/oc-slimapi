# oc-slimapi v1 实现任务书（服务端）

> **来源**：拆自 `~/personal_projects/ocdroid/docs/slimapi-gap-contract-v1-draft.md`（v1 最终契约，三轮评审通过）。  
> **范围**：oc-slimapi sidecar 须实现的全部变更。客户端配套见 ocdroid 侧文档。  
> **实现真源**：本仓库 `docs/INTERFACE_MAP.md` + `docs/design-v2.md`。  
> **v1 全部为加性变更，不 bump `X-Slimapi-Version`**。

---

## 0. 总原则

| # | 原则 |
|---|---|
| 1 | 读热路径 thin，写路径 catch-all 透传（升格为正式支持面） |
| 2 | 消息形状不变（`MessageWithParts` 裸数组/对象）；skeleton 规则沿用 `skeleton.py` |
| 3 | SSE 只走控制面；v1 单全局 `/slimapi/events` |
| 4 | 错误可区分：`404 not_found` ≠ `503 upstream`；session 级 error 不得静默丢 |
| 5 | 校验源单一：v1 只认 query `directory`（仅 `/slimapi/messages/**`）；header/query 冲突 → 400 |
| 6 | 写路径禁止自动重试（mutation timeout 不得双发） |
| 7 | 统一错误码表**仅适用于 thin 路由**；catch-all 透传 upstream body |

---

## 1. 实现批次与顺序

```
B0  前提验证（go/no-go 门禁）
    ├─ 验证 opencode session.error 是否出现在 /global/event
    ├─ 确认 sessionID 可缺失、字段路径 error.data.message、abort name
    ├─ 扫目标 opencode 版本 shell/PTY 路由表
    └─ 产出验证报告

B0 决策：
    - 若 session.error 出现在 /global/event，且字段映射与 abort 分类可确认：
      B2 按 G1 实现；G1 属于 v1。
    - 若 session.error 不出现在 /global/event，或字段映射/abort 分类无法确认：
      G1 从 v1 移出；B2 不启动；本契约不引入 REST error endpoint。
      error REST fallback 另立 RFC，不在本任务书。

B1  服务端 P0（相互独立，可并行 PR）
    ├─ G2      status 404/503 分离
    ├─ G8      full/{mid} 流式 cap（+ transform-busy 502→503）
    ├─ G7-soft  messages allowlist（query 校验）
    └─ shell deny-list（proxy.py + config，默认开，路径表来自 B0）

B2  服务端 error 通道（依赖 B0 决策为「G1 属于 v1」）
    └─ G1  digest.lastError + session-less 帧

B3  契约文档同步（INTERFACE_MAP / CLIENT_CHANGES / design-v2）
    └─ G3 探针契约 / G4 透传矩阵 / 统一错误码表落地

B4  服务端 P1（可与 B1 并行）
    └─ G6  multi-mid full（envelope，MUST 定序，discover 先行）
```

**依赖**：B0 是 go/no-go gate；B2 依赖 B0 决策；B1 三项 + shell 相互独立；B4 可与 B1 并行。

---

## 2. B0 前提验证（go/no-go）

产出验证报告，含：

1. `session.error` 是否出现在 slim 订阅的 `GET /global/event`（G1 go/no-go 硬前提）
2. `session.error.sessionID` 是否可缺失（plugin/skill 实发无 sid）
3. 字段路径：`error.name` + `error.data.message`（已核对：`NamedError.toObject()` 返回 `{name,data}`；`AssistantErrorSchema` 各变体 `{name,data:{message,...}}`）
4. abort name `MessageAbortedError`（已核对：确以 `session.error` 发射，故 G1 name 过滤必需）
5. **shell/PTY 路由表**：扫目标 opencode 版本（`opencode-src/v1.18.3`）路由，列出 shell/PTY 类 HTTP 路径

**决策**：
- 通过 → B2 按 G1 实现
- 不通过 → G1 移出 v1；B2 不启动；不预写 REST fallback

---

## 3. B1 — G2 status 错误语义

### `GET /slimapi/sessions/{sid}/status`

| 条件 | HTTP | body |
|---|---|---|
| upstream 404 / 明确 not found | **404** | `{"code":"session_not_found","sessionID":"…"}` |
| discover 得到的 directory ∉ allowlist | **400** | `{"code":"directory_not_allowed"}` |
| upstream 其它 4xx（401/403/409 等） | **502** | `{"code":"upstream_http_N"}` |
| upstream 超时 / 5xx / JSON 坏 | **503** | `{"code":"upstream_unavailable"}` |
| 成功但 map 无 sid | **200** | `{"type":"idle"}`（仅在 `GET /session/{sid}` 成功后；假 idle 风险注明：session 已删但 status map 滞后时可能误报 idle） |

### 实现要点

- 现状 `routes/sessions.py:121-131` 用 `raise_for_status()` + `except Exception → 503`，**同时吞掉 allowlist 400 和 upstream 404**。
  - **现实校正（v1 B1 run, 2026-07-18）**：status handler 实际跨 `sessions.py:121-149`，`except Exception → 503` 有**两处**（discover 段 + status map 段）；G2 改造须同时覆盖两处。详见 `docs/ocmar/specs/2026-07-18-v1-b0-b1-design.md` §2 reality 表（行 A）+ §3.1。
- 修法：**re-raise `HTTPException`**（保留 allowlist 400）；对 upstream 用 `HTTPStatusError` 精确判 404；其它 4xx → 502；仅网络/5xx/解析失败 → 503。✅ B1 已实现。
- 批量 `GET /slimapi/sessions/status` 语义不变。
- **错误 body 结构化迁移**：现网 `HTTPException(detail=str)` 渲染 `{"detail":"…"}`；G2/G7/G8 改造为 `{"code":…}`。✅ B1 已实现（`src/oc_slimapi/errors.py` 引入 `CodedHTTPException` + FastAPI `exception_handler`）。

---

## 4. B1 — G8 full/{mid} 流式 cap（P0）

### 现状问题

`routes/messages.py:419-435` full 模式用 `upstream.get()` 完整 body 读入内存后才检查 `max_message_bytes`（32 MiB）。单消息极大时 → 峰值内存打满、连累其它请求（sidecar `MemoryMax=384M`）。

### 目标

- **full 与 skeleton 均**使用累计字节 cap（对齐 `read_with_cap`）。
- **超过 `max_message_bytes` 立即中止 upstream 读取** → `413 {"code":"message_too_large","limitBytes":…}`。
- **禁止**「先完整下载再查 32MiB」。
- cap 计量：解压后逻辑 JSON 字节（与 list/since 口径一致，写死）。实现期确定 full 边读边按解压字节计数的流式实现（full 现为 passthrough，需在流上解压计数）。
- 超限后须关闭 upstream response（防连接泄漏）。
- **transform-busy 归一**：~~现状 `full/{mid}` skeleton 转换忙返回 **502**（INTERFACE_MAP line 23），与 list/since 及统一错误码表的 **503 `transform_busy`** 冲突；~~ G8 顺带把 `full/{mid}` transform-busy ~~从 502 **归一为 503 `transform_busy`**~~（文字同步实际代码；真相见下方现实校正）。
  - **现实校正（v1 B1 run, 2026-07-18）**：代码层 `full/{mid}` transform-busy **实际一直返 503**（`_busy_response()` 写死 503；测试 `test_messages_route_returns_503_for_single_message_when_admission_saturated` 已断言 503）。即"502→503 归一"在代码层**本就完成**，G8 仅需同步更新 `docs/INTERFACE_MAP.md` line 23 文字。详见 `docs/ocmar/specs/2026-07-18-v1-b0-b1-design.md` §2 reality 表（行 B）+ §3.2。✅ B1 文档已同步。

**参数不变**。

---

## 5. B1 — G7-soft messages allowlist

### 现状

`/slimapi/messages/**` 的 `directory` 只转发 header，不做 allowlist（与 sessions/pending 不一致）。

### v1 契约（soft）

| 条件 | 行为 |
|---|---|
| 未传 query `directory` | 不拦（依赖上游默认）；**v1 不强制必填** |
| 传了 query `directory` 且 ∈ allowlist | 通过 |
| 传了 query `directory` 且 ∉ allowlist | **400** `{"code":"directory_not_allowed"}`；允许 miss 时刷新 projects 一次 |

**校验源（写死）**：v1 **只认 query `directory`** 走 `require_directory()`。  
若同时存在 `X-Opencode-Directory` header 且与 query 冲突 → **400**。  
**禁止**宣称 soft = 多租户隔离；隔离靠 stunnel/mTLS + 网络边界。

### 涉及路径

- `GET /slimapi/messages/{sid}`
- `GET /slimapi/messages/{sid}/since/{ts}`
- `GET /slimapi/messages/{sid}/full/{mid}`
- `GET /slimapi/messages/{sid}/full`（G6）

---

## 6. B1 — shell deny-list

### 现状问题

catch-all（`proxy.py`）**无路径黑名单**，不识别语义；shell/PTY 经 HTTP 可达 = 安全风险。

### 目标

- **默认配置 deny-list**：命中 shell/PTY 类路径（路径表来自 B0 扫表，写死，不臆造）→ `403 {"code":"shell_not_allowed"}`。
- 实现：`proxy.py` + `config.py`；默认开启屏蔽，提供 ops 配置项可关闭（关闭**不属于安全保证**，仅作为非默认运维模式）。
- WebSocket 继续 501。
- **注意**：catch-all 不识别路径语义是结构性事实；deny-list 是 best-effort 第二道，真实隔离仍靠 stunnel mTLS + 网络边界 + upstream 权限。漏路径即可绕过。

---

## 7. B2 — G1 error 可见性（依赖 B0 通过）

### 硬约束

1. **B0 前提**：只有 B0 验证通过后，才允许实现 G1 hub 通道。B0 未通过时，v1 不承诺 slim error 可见性；**不得以未定义的 REST fallback 代替 G1**。
2. **不可仅 G1-A**：opencode `session.error.sessionID` 可为 optional（plugin/skill 实发无 sid），A-only 会静默丢 session-less error。
3. **排除主动中止**：`MessageAbortedError`（确以 `session.error` 发射）及等价 abort **不得**写入 lastError / error 帧。判定以 `error.name === "MessageAbortedError"` 为准。
4. **脱敏（可测算法）**：`message` **禁止**使用 `Cause.pretty` 全文。算法固定为：取首行 → 替换绝对路径（Unix `/…`、Windows `C:\…`）→ 删除 stack frame 样式（`at file:line:col`）→ 删除 secret pattern（token/key/bearer）→ 截断 ≤512。
5. **sticky + clear**：lastError 跨 debounce 窗口 sticky，直到 clear；error 到达时 **立即 flush** digest（不等 250ms 窗口）。
6. **schema 映射**：`error.name` + `error.data.message`（已核对 `NamedError.toObject()` + `AssistantErrorSchema`）。
7. **message 缺失回落**：`MessageOutputLengthError` 等变体 `data={}` 无 message → `message` 回落为 `name` 或固定文案 `"(no detail)"`。

### clear 规则（冻结，唯一）

- 新非-abort error 到达：写入 `lastError` 对象 + **立即 flush** digest。
- clear 触发：**显式发送一帧 `digest` 含 `"lastError": null`**；触发时机 = 该 session 出现新 `status=busy`（开始新工作）。
- session `deleted=true` 后：**不保留** lastError（与行删除一致）。
- sidecar 重启：进程内 sticky 状态丢失（无 durable replay 承诺，由客户端 resync 覆盖）。

### 方案：G1-A + G1-B 组合

**G1-A（digest 加性，承载有 sid 的 error）**

```json
{
  "sessionID": "ses_…",
  "directory": "/abs/path",
  "status": "idle",
  "messageID": "msg_…",
  "updatedAt": 1710000000000,
  "lastError": {
    "name": "UnknownError",
    "message": "short human-readable",
    "at": 1710000000000
  }
}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `lastError` | object \| null \| 省略 | 否 | 进程内 sticky error 状态非空时为对象；clear 时为 `null`（显式帧）；未变化时省略 |
| `lastError.name` | string | 是（对象时） | 来自 `error.name`，截断 ≤128 |
| `lastError.message` | string | 是（对象时） | 脱敏算法见硬约束 4，截断 ≤512；缺失回落 name 或 `"(no detail)"` |
| `lastError.at` | int epoch ms | 否 | 事件到达时间或上游 time |

**实现提示**：`DigestFields` 须区分「null（clear）」与「省略（未变）」，用 sentinel 而非 `None` 承载；`to_payload()` 相应改造。

**G1-B（session-less / 即时精简帧，承载无 sid 的 error）**

```text
event: session.error
data: {"sessionID"?,"directory"?,"name","message","at"}
```

- 立即推送（不进 250ms debounce）。
- **仅当 upstream error 无 sessionID 时必须走 B**（或全局 lastError 桶），不可丢。
- 有 sid 时：A + 立即 flush 即可，B 可选（避免双通道）。
- 同样排除 abort；同样脱敏算法；同样 message 缺失回落。
- 客户端 UI 默认落点：session-less（无 sid）→ 全局 toast；有 sid → 该 session 行/banner。

### 上游映射

| upstream `/global/event` payload | slim 行为 |
|---|---|
| `type == session.error` 且 `name == MessageAbortedError`（abort） | **静默丢弃**（正常中止） |
| `type == session.error` 且有 sessionID | A 立即 flush；B 可选 |
| `type == session.error` 且无 sessionID | **必须** B 帧（或全局桶） |
| 其它已 DROP 类型 | 不变 |

---

## 8. B4 — G6 multi-mid full（新端点）

### 接口

`GET /slimapi/messages/{sid}/full`（与单条 `full/{mid}` 并存；旧路径保留）

| 参数 | 位置 | 类型 | 默认 | 约束 |
|---|---|---|---|---|
| `sid` | path | string | — | 必填 |
| `ids` | query | string | — | **必填**；逗号分隔 messageId，**1–20**；去重保序 |
| `mode` | query | `skeleton` \| `full` | `full` | 与单条一致 |
| `directory` | query | string? | — | 转 `X-Opencode-Directory`；G7-soft 校验 |

### 响应（envelope；mid 级部分失败 200；整 session 不存在 404；两者严格分离）

```json
{
  "items": [ { "/* MessageWithParts */": "…" } ],
  "errors": [
    { "messageID": "msg_missing", "code": "message_not_found" }
  ]
}
```

| HTTP | 条件 |
|---|---|
| **200** | session 存在；任意 mid 成功，或 mid 级失败进 `errors[]`（**即使全部 mid 404 仍 200 + 全 errors**） |
| **400** | `ids` 存在但为空 / >20 / 逗号解析失败 → `{"code":"invalid_ids"}` |
| **422** | `ids` **缺失**（FastAPI 参数校验） |
| **404** | **仅** discover 判定 session 不存在 → `{"code":"session_not_found","sessionID":"…"}`（**无 envelope**） |
| **413** | 累计响应超 `max_response_bytes` → `{"code":"response_too_large"}` |
| **503** | `transform_busy` / discover 或 mid 拉取整体 upstream 不可用 |

### 整 session 不存在判定（写死，禁止猜测）

1. 展开 mid **之前** 必须先：`GET {upstream}/session/{sid}`（带与本请求相同的 directory 转发）。
2. discover **404** → 立即 **404** `session_not_found`，**不得**再请求任何 mid。
3. discover **2xx** → session 视为存在；之后任意 mid **404** → **只**进 `errors[]` `code=message_not_found`；**即使全部 mid 404 仍 HTTP 200** + 全 errors。
4. discover **超时 / 5xx / 体不可解析** → **503** `upstream_unavailable`。
5. discover **其它 4xx** → **502** `upstream_http_N`（与 G2 对齐）。

### MUST

- `items` **严格按** `ids` 去重后顺序（并发回填后重排）。
- 单 mid 超 `max_message_bytes` → `errors[]` `message_too_large`（**不**整请求 413，除非累计超 `max_response_bytes`）。
- 累计预算 64 MiB 在 batch 内共享；超限 → **413** `response_too_large` 并中止后续 mid。
- `ids` 只校验数量 1–20 与逗号解析；**不**校验 mid 字符集。
- `Cache-Control: no-store`；支持 gzip。
- 路由注册：`GET .../full` **先于** `GET .../full/{mid}`。

### 实现

```
# 1) discover
r = GET upstream /session/{sid}（带 directory 转发）
if r.status == 404: return 404 {code: session_not_found, sessionID}
if r.status in 5xx or timeout or bad json: return 503 {code: upstream_unavailable}
if r.status >= 400: return 502 {code: upstream_http_{status}}

# 2) expand
items = []; errors = []; order = dedupe(ids); succeeded = {}
for mid in order (concurrency ≤ 4, 共享累计字节计数):
    GET upstream /session/{sid}/message/{mid}
    on mid 404 → errors.append({messageID: mid, code: message_not_found})
    on mid size > max_message_bytes → errors.append({messageID: mid, code: message_too_large})
    on 累计 > max_response_bytes → abort → 413 {code: response_too_large}
    on mid 2xx → 按 mode 转换后放入 succeeded[mid]
reassemble items strictly in order（仅 succeeded mid）
return 200 {items, errors}
```

---

## 9. G3 latest 探针（契约收敛，文档-only）

客户端 slim 模式探针使用：

```http
GET /slimapi/messages/{sid}?limit=1&mode=skeleton
X-Slimapi-Version: 1
```

响应 `MessageWithParts[]` 长度 0 或 1。**不新增端点**。

**诚实限制（写入 INTERFACE_MAP）**：
- skeleton **保留 `text` part 全文**；末条大文本时**不保证 body≤数 KB**。
- 前提：upstream 分页默认「最新优先」——须集成测试确认。
- `schema_degraded=true` 强制 full 时探针变重；行为冻结：**探针仍允许调用**，文档明确可能返回 full body（接受降级）。
- 空会话 → `[]`（200）；不存在的 sid → 透传 upstream 状态（通常 404）。
- **不做**可选响应头；以 body 为准。
- **不做** G3-B 独立 latest-message 探针（v2 延后）。

---

## 10. G4 catch-all 透传矩阵（文档-only）

### 正式支持面（经 catch-all → opencode，**不经版本门闩**）

| 能力 | sidecar path | 方法 | directory | read timeout | 可重试 | 备注 |
|---|---|---|---|---|---|---|
| 新建会话 | `/session` | POST | header | 30s | **否**（mutation） | — |
| 改标题/归档 | `/session/{id}` | PATCH | header | 30s | **否** | body 含 title / time.archived |
| 删除会话 | `/session/{id}` | DELETE | header | 30s | **否** | 破坏性 |
| 子会话 | `/session/{id}/children` | GET | header | 30s | 是 | — |
| 发送消息 | `/session/{id}/prompt_async` | POST | header | 30s | **否**（timeout 禁双发） | 主发送路径 |
| 中止 | `/session/{id}/abort` | POST | header | 30s | **否** | — |
| 压缩 | `/session/{id}/summarize` | POST | header | 30s | **否** | **可能超 30s timeout，客户端勿自动重试** |
| fork / revert | `/session/{id}/fork`、`/revert` | POST | header | 30s | **否** | — |
| slash 命令 | `/command`、`/session/{id}/command` | GET/POST | header | **300s** | 否 | 长 command |
| 模型/agent | `/config/providers`、`/agent` | GET | — | 30s | 是 | 低频 |
| 文件 | `/file`、`/file/content`、`/file/status`、`/find/file` | GET | query | 30s | 是 | 大 body 客户端 guard |
| VCS | `/vcs`、`/vcs/status`、`/vcs/diff` | GET | query | 30s | 是 | — |
| diff / todo | `/session/{id}/diff`、`/todo` | GET | header | 30s | 是 | — |
| active | `/api/session/active` | GET | — | 30s | 是 | 未读 soak（对接时核对目标版本是否仍在） |
| health 回退 | `/global/health` | GET | — | 5s | 是 | 非 slim host 用 |

### 明确不承诺

| 项 | 说明 |
|---|---|
| 全量 `message.part.delta` 经 slim SSE | 设计 DROP；要动画走 v2 |
| `message.removed` 经 slim SSE | 设计 DROP；客户端靠 resync 后 sessions/since 对齐 |
| WebSocket / PTY | WS→501 |
| shell 默认开放 | 见 §6 deny-list |
| catch-all 错误统一映射 | upstream 异常可能 500；统一错误码表**仅适用于 thin 路由** |

---

## 11. 统一错误码表（仅 thin 路由）

| code | HTTP | 场景 | 是否新 |
|---|---|---|---|
| `version_required` / `version_incompatible` | 400 | 版本门闩 | 现有 |
| `directory_not_allowed` | 400 | allowlist miss（G2/G7） | **新（结构化）**：现网 `{"detail":"…"}` → 改造为 `{"code":…}` |
| `session_not_found` | 404 | session 不存在（G2/G6 整 session） | **新** |
| `message_not_found` | 404 单 mid 透传；或 G6 envelope `errors[]` | 单 mid 不存在 | **新** |
| `message_too_large` | 413 | G8 / G6 单 mid | 现有 |
| `response_too_large` | 413 | list/since/batch 累计 | 现有 |
| `transform_busy` | 503 | 转换槽满（含 G8 归一后的 full/{mid}） | 现有（语义扩展） |
| `upstream_unavailable` | 503 | 超时/5xx/解析失败（G2） | **新（统一命名）** |
| `upstream_http_N` | **502** (top-level body) | G2 `sessions/{sid}/status` discover/status-map other 4xx；`GET /slimapi/projects` discovery 4xx | **B1 语义扩展**（原仅 envelope） |
| `upstream_http_N` / `upstream_timeout` / `upstream_error` | **envelope 内 code**（非顶层 HTTP） | questions/permissions 聚合 `errors[]`；整体 200 部分成功 / 503 全败 | 现有 |
| `invalid_ids` | 400 | G6 ids 空/超限/解析失败（缺失走 422） | **新** |
| `invalid_directory_count` | 400 | questions directory 数量守卫（repeated query 去重后须 1–32） | **新（B1 引入），加性，不 bump** |
| `invalid_route_token` | 400 | questions routeToken 校验失败（签名/版本/iat/exp/kind/requestID/sessionID/directory 任一不通过） | **新（B1 引入），加性，不 bump** |
| `shell_not_allowed` | 403 | G4 deny-list 命中 | **新** |
| `websocket_not_supported` | —（WS 消息内 501，非 HTTP body） | WebSocket | 现有 |
| `thin_route_not_found` | 404 | 未知 slim path | 现有 |

- FastAPI 参数/body 校验：**422**（保持）。
- 业务校验主动抛：**400**。
- 错误 body 统一形状：`{"code":string, "message"?:string, ...}`。
- 命名一致：禁止裸 `not_found`。

---

## 12. 版本与兼容

| 变更 | bump X-Slimapi-Version？ |
|---|---|
| digest 加可选 `lastError` | 否（加性） |
| status 404 替代部分 503 | 否（修 bug） |
| multi-mid 新路径 | 否（新端点） |
| G7-soft（提供才校验） | 否（仅收紧非法 directory） |
| G8 流式 cap | 否（413 已存在） |
| shell deny-list 默认开 | 否（运维配置） |
| 统一错误码 | 否（加性 code） |

---

## 13. 实现落点

| 变更 | 文件 |
|---|---|
| G1 lastError + session-less 帧 | `src/oc_slimapi/sse/hub.py` |
| G2 status 404 | `src/oc_slimapi/routes/sessions.py` |
| G6 multi full | `src/oc_slimapi/routes/messages.py`（路由注册先于 `{mid}`） |
| G7-soft allowlist | `routes/messages.py` + 复用 `require_directory` |
| G8 流式 cap | `routes/messages.py`（对齐 `read_with_cap`） |
| shell deny-list | `proxy.py` + `config.py` |
| 错误码统一 | 各 route + helper |
| 契约文档 | `docs/INTERFACE_MAP.md` / `docs/CLIENT_CHANGES.md` / `docs/design-v2.md` |

---

## 14. 验收清单

- [ ] **B0**：验证报告产出（session.error 上 global event / sessionID 可缺 / 字段路径 / abort name / shell 路由表）
- [ ] G2：upstream 404 → 404；allowlist miss → 400；其它 4xx → 502；网络/5xx → 503
- [ ] G8：mock 超大 body 边读边 413；峰值 RSS 不爆；upstream response 关闭；transform-busy 归一 503
- [ ] G7-soft：无 directory 不 400；非法 directory 400；query/header 冲突 400
- [ ] G1：生成失败立即见 lastError；**abort 不亮 banner**；session-less error 走 B；message 脱敏算法 golden（路径/stack/secret）；clear 显式 `lastError:null`
- [ ] G4：透传矩阵含超时/重试/shell deny-list；shell 命中 403
- [ ] G6：items 严格按 ids 序；mid 部分失败 200+errors；ids 缺失 422 / 空超限 400；**整 session 404（discover 先行）vs mid 404 严格分离**；累计超限 413
- [ ] 错误码：统一形状（thin 路由）；`session_not_found`/`message_not_found` 命名一致；`upstream_*` 仅 envelope
- [ ] 回归：现有 routeToken/since/skeleton/events backpressure 全绿

---

## 15. 测试矩阵

| 区域 | 用例 |
|---|---|
| G1 hub | session.error 有/无 sid；MessageAbortedError 不产 lastError；跨多窗口 sticky；clear 显式 `lastError:null`；脱敏算法 golden（Unix/Win 路径、`at file:line`、JSON secret、换行堆栈）；message 缺失回落 name |
| G1 e2e | `/global/event` 实发 session.error → 订阅者可见（B0 验证） |
| G2 | upstream 404 / 其它 4xx / 500 / 超时 / allowlist miss / 无 directory 六分支；假 idle 注明 |
| G8 | gzip/identity 下大响应；client disconnect 后 upstream 关闭；并发大响应；transform-busy 返 503（非 502） |
| G6 | items 定序；重复 id；部分 404；部分过大；全 mid 404（仍 200+全 errors）；**整 session 404（discover 先行，禁止再请求 mid）**；ids 缺失 422 vs 空/超限 400；累计字节超限 413；路由注册顺序 |
| G7 | query directory 合法/非法/缺省；header 冲突 |

---

## 16. v2 延后（不在本任务书，仅记录）

G3-B（独立探针）、G7-strict（必填 directory）、G9（focus SSE）、G10（until）、G11（around）、G12（批式 delta）、G13（durable replay）——触发条件与是否 bump 见契约原档。
