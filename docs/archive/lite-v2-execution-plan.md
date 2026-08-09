# oc-slimapi lite-v2 执行计划

> **状态**：Track B（文档/契约对齐）已完成（commit 74b5261）；W1-W3 实现部分待核查
> **基线**：oc-slimapi main 分支（v1 contract rev M）
> **目标**：配合 ocdroid v2.7 方案，删除精确同步协议，简化为 skeleton 投影 + digest + token stream
> **分支**：`lite-v2`（从 main 新建）
> **预计工期**：3 周（W1-W3）

---

## 0. TL;DR

净删除 ~450 行 + ~2 行新增。删除 10 个端点、5 个文件、整套 seq / fingerprint / children 状态。保留 catch-all 透传 + token stream + digest debounce。

核心信条：**sidecar 不再是「精确同步状态机的权威」，而是「opencode skeleton 的薄投影 + token stream 透传」。** 所有按消息体内容做 diff / 推断 / 序号化的逻辑全部下线。

---

## 1. 端点删除清单（10 个）

> 来源：ocdroid-lite-aggressive-plan.md §3.1
> 所有删除完成后，相关路由必须返回 404（不注册即天然 404）。

| 端点 | handler 文件 | 处理 |
|---|---|---|
| `GET /slimapi/messages/{sid}/full?ids=` | `src/oc_slimapi/routes/messages.py` | 删 handler（批量展开端点） |
| `GET /slimapi/messages/{sid}/since/{ts}` | `src/oc_slimapi/routes/messages.py` | 删 handler（增量同步端点） |
| `GET /slimapi/sessions/{sid}/children` | `src/oc_slimapi/routes/sessions_children.py` | **整个文件删** |
| `GET /slimapi/sessions/{sid}/status` | `src/oc_slimapi/routes/sessions.py` | 删 handler |
| `GET /slimapi/sessions/status` | `src/oc_slimapi/routes/sessions.py` | 删 handler（批量 status） |
| `GET /slimapi/questions` | `src/oc_slimapi/routes/questions.py` | **整个文件删** |
| `GET /slimapi/permissions` | `src/oc_slimapi/routes/questions.py` | 同上（随 questions.py 整体退役） |
| `POST /slimapi/questions/{qid}/reply` | `src/oc_slimapi/routes/questions.py` | 同上 |
| `POST /slimapi/questions/{qid}/reject` | `src/oc_slimapi/routes/questions.py` | 同上 |
| `POST /slimapi/sessions/{sid}/permissions/{pid}` | `src/oc_slimapi/routes/questions.py` | 同上 |
| `GET /slimapi/projects` | `src/oc_slimapi/routes/sessions.py` | 删 handler + 删相关 import |

> 上表「questions / permissions」族按客户端语义算 1 组、按 HTTP 路径算多个，统一随 `questions.py` 文件退役（§3）。

**验收**：删完后用 `pytest` + 一条集成测试断言这些路径均返回 404。

---

## 2. 端点简化（2 个）

> 来源：ocdroid-lite-aggressive-plan.md §3.2

| 端点 | 当前位置 | 改动 |
|---|---|---|
| `GET /slimapi/messages/{sid}/full/{mid}` | `routes/messages.py`（单条展开，~`messages.py:989` 起） | 删 `?known.*` 查询参数、删 304 短路、删 `X-Message-Event-Seq` 响应头、删 `seq_pre` / `seq_post` 双采样逻辑；**降级为纯按需展开**（不再被任何自动同步路径调用，仅客户端手动 expand 时触发） |
| `GET /slimapi/messages/{sid}` | `routes/messages.py`（列表，~`messages.py:479` 起） | **删 `?mode=full` 列表分支**；统一 skeleton 投影；保留 `?limit=` + `?before=` 分页参数 |

**`mode` 字面量收窄**：全仓库把 `Literal["skeleton", "full"]` 收敛为只剩 `skeleton`（列表端点）；单条展开端点保留 `full` 投影但无 304 / 无 seq 头。

---

## 3. 文件整体退役（5 个）

> 来源：ocdroid-lite-aggressive-plan.md §3.3
> 删除前需 `rg` 确认零引用（除彼此互引），逐个删除 + 跑测试。

| 文件 | 行数 | 退役依据 |
|---|---|---|
| `src/oc_slimapi/routes/questions.py` | ~185 | routeToken 全删；客户端零消费 |
| `src/oc_slimapi/routes/sessions_children.py` | ~20 | 客户端走 legacy children，不再走 sidecar |
| `src/oc_slimapi/tokens.py` | ~150 | routeToken HMAC 零引用（questions.py 删后唯一消费者消失） |
| `src/oc_slimapi/discovery.py` | ~200 | questions.py + `/projects` 删后零消费者 |
| `src/oc_slimapi/children_cache.py` | ~199 | 客户端走 legacy children 直连 opencode |

**退役顺序建议**：先删路由文件（questions.py、sessions_children.py）→ 再删被它们依赖的 tokens.py / discovery.py / children_cache.py。每删一个文件 `pytest` 一次确认没有遗漏引用。

**`app.py` 中对应的 router 注册也要一并删除**（见 §6）。

---

## 4. digest 帧简化 + bump updatedAt

> 来源：ocdroid-lite-aggressive-plan.md §3.4
> 文件：`src/oc_slimapi/hub.py`（实际仓库内未单独建 hub.py 文件，digest 逻辑由 `routes/messages.py` 配合 `transform.py` / `skeleton.py` 完成；以下行号来自 ocdroid-lite-aggressive-plan §3.4 的设计参考，执行时按当前仓库实际结构定位）。

### 4.1 字段删除

| 字段 / 代码 | 处理 | 设计参考行号 |
|---|---|---|
| `DigestFields.content_revisions` 声明 + `to_payload` | **删** | hub.py:189-195, 215-218 |
| `DigestFields.children_version` 声明 + `to_payload` | **删** | hub.py:186, 209-210 |
| `publish()` 中 contentRevisions 写入 + 清理 | **删** | hub.py:982, 1030, 1056-1064, 1118-1128 |
| `publish()` 中 children_version 写入 | **删** | hub.py:827-836 |
| `session.created` 分支 | **删整个分支** | hub.py:826-836 |
| `GlobalHub._children_cache` 引用 | **删** | hub.py:360, 1318-1357 |

删完后 digest 帧 schema 收敛为 **6 字段**：

```jsonc
{
  "sessionID": "ses_...",
  "directory": "/path",
  "status": "busy|idle|retry",
  "messageID": "msg_...",
  "updatedAt": 1753000000000,
  "archived": 1753000000000,
  "deleted": true,
  "lastError": {"name","message","at"} | null
}
```

**删**：`contentRevisions`、`childrenVersion`
**不加**：不新增任何字段

> **注（2026-07-28）：本节 bump_updated_at / part 事件触发 digest 的方案已被 v2-contract.md §3 取代。** 实际 v2 实现中 `message.part.updated` / `message.part.removed` **不触发 digest**（digest 仅由 `session.*` / `message.updated` / `message.appended` 驱动）。本节保留作历史记录。

### 4.2 bump updatedAt（~2 行新增）

| 代码位置 | 改动 | 设计参考行号 |
|---|---|---|
| `MESSAGE_EVENTS` 分支 updatedAt 统一 | **改为 `_now_ms()`** | hub.py:845, 851 |
| `message.part.updated` 分支新增 bump | **新增 `entry.updated_at = _now_ms()`**（经单调化函数，见 §4.3） | hub.py:982 附近 |
| `message.part.removed` 分支新增 bump | **新增 `entry.updated_at = _now_ms()`**（经单调化函数） | hub.py:1030 附近 |

> **updatedAt 语义**：所有 bump 统一用 `_now_ms()`（sidecar wall-clock）。digest.updatedAt = 「sidecar 最后观察到该 session 有变化的 wall-clock 时间」。客户端 strict `>` 单调比较触发 reload。250ms debounce 限频。
>
> **时钟回退处理**：sidecar 重启后 `_now_ms()` 可能低于客户端保存的旧 bookmark。客户端检测到 `digest.updatedAt < bookmark` 时，视为 sidecar 重启 → 清除 bookmark + 强制 reload（digest reset 协议）。实现：SSE 连接建立时 sidecar 在首个 digest 帧附加 `reset: true` 标记（或客户端检测 updatedAt 跳变为 0 / 回退）。

> **注（2026-07-28）：同上——§4.2 的 bump_updated_at 逻辑（part 事件触发 digest 且严格递增）已被 v2-contract.md §3 取代。** 实际 v2 实现中 part 事件不触发 digest；`updatedAt` 的单调性保证请以 v2-contract.md §3/§5 为准（跨窗口不保证严格单调，同一进程窗口内可保障）。
> 本节的 `bump_updated_at` 函数设计保留作历史参考。

### 4.3 updatedAt 单调化

为防止同一毫秒内 `message.part.updated` / `message.part.removed` 与原 bump 碰撞（客户端 strict `>` 比较会漏掉同毫秒事件），新增一个单调化辅助函数（**唯一真正的新增逻辑**）：

```python
def bump_updated_at(entry: DigestFields) -> None:
    now = _now_ms()
    previous = entry.updated_at if isinstance(entry.updated_at, int) else 0
    entry.updated_at = max(now, previous + 1)  # 保证严格递增，防同毫秒碰撞
```

§4.2 的两处新增 bump 均通过 `bump_updated_at(entry)` 调用，而不是裸写 `entry.updated_at = _now_ms()`。

---

## 5. hub 状态简化

> 来源：ocdroid-lite-aggressive-plan.md §3.5
> **整体删除**驱动精确同步的内部簿记。

| 状态 / 函数 | 处理 | 退役依据 / 设计参考行号 |
|---|---|---|
| `_part_state` | **整体删** | 零消费者 |
| `_session_event_seq` | **整体删** | 驱动 contentRevisions，已删 |
| `_bump_message_seq()` | **整体删** | hub.py:683-725 |
| `_bump_session_event_seq()` | **整体删** | hub.py:635-653 |
| `get_part_fingerprint()` | **整体删** | hub.py:598-633 |
| `_retired_messages` | **保留** | 防止 late `message.part.updated` 复活 token hub 状态（hub.py:964 直接守卫） |
| `publish()` 中 part 事件的 token hub 转发 | **保留** | `_token_hub.on_part_updated()` / `on_part_removed()` / `on_message_removed()` |
| `publish()` 中 `message.part.delta` 路由 | **保留**（完全独立） | hub.py:1091-1094 |

> **`_retired_messages` 是必须保留的安全网**：message 被 removed 后，若 upstream 仍乱序投递一条 late `message.part.updated`，没有这个守卫会重新在 token hub 里建出僵尸状态。

### 5.1 G1 安全机制（保留不动）

- `sticky_last_error` / `deleted_tombstones` / `_sanitize_error_message` / `lastError` 三态 —— **全部保留**，不删不改。

---

## 6. app.py 清理

> 来源：ocdroid-lite-aggressive-plan.md §3.7
> 文件：`src/oc_slimapi/app.py`

| 字段 / 调用 | 处理 |
|---|---|
| `route_secret` / `batch_ledger` | **删** |
| `directory_allowlist` / `allowlist_lock` / `allowlist_ready` | **删** |
| `warm_allowlist(app)` | **删** |
| `children`（ChildrenCache 实例） | **删** |
| `hubs.set_children_cache()` | **删** |
| `X-Discovery-Directories` 响应头 | **删** |
| `X-Discovery-Ready` 响应头 | **删** |
| §3 中退役路由文件的 `router.include_router(...)` 注册 | **删**（questions / sessions_children / projects handler） |

### 6.1 catch-all 透传（保留不动）

sidecar 的非 `/slimapi/**` catch-all 反代逻辑完全不动 —— 这是 token stream + 普通 opencode API 透传的基础，不在本次改造范围。

---

## 7. 版本协商

> 来源：ocdroid-lite-aggressive-plan.md §2.5
> 文件：`src/oc_slimapi/versioning.py`、`src/oc_slimapi/config.py`、`src/oc_slimapi/routes/health.py`

- **客户端发送**：`X-Slimapi-Version: 2`
- **`versioning.py`**：`ACCEPTED_CLIENT_VERSIONS = (2, 2)`（min=max=2，只接受 v2）
- **`/slimapi/health`** 响应暴露 `slimapi_contract: 2`（新增一个静态字段）
- **token stream gate**：v2 协议下 `tokenStreamEnabled = slimConnection`（**不再 probe health `features.tokenStream`**）—— 客户端侧改动，sidecar 无需为此字段做特殊处理，保留 token stream 路由即可。

> `config.py:162` 的 `accepted_client_versions` 默认值会从 `versioning.ACCEPTED_CLIENT_VERSIONS` 派生，改一处即可（同时确认 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` 环境变量在部署配置里也同步为 `2,2`）。

---

## 8. skeleton 端点排序契约（新增）

> 这是 lite-v2 唯一新增的**行为契约**（非删除类），客户端 `reloadSkeletonPage` 强依赖此排序。

**契约**：`GET /slimapi/messages?mode=skeleton`（即 `GET /slimapi/messages/{sid}` 列表端点，skeleton 投影）**必须按 `time.created` 升序返回**。

**当前实现**（`routes/messages.py`）：
- `_stream_upstream()`（定义于 `messages.py:229`）直接转发 upstream opencode `/session/{sid}/message`。
- 列表端点在 `messages.py:574` 处调用 `_stream_upstream(...)`，upstream opencode 默认按 created 升序返回。
- sidecar 的 skeleton 投影（`skeleton.py`）保持顺序不变换。

**风险点**：sidecar 本身不做显式 ORDER BY，依赖 upstream opencode 的默认排序。lite-v2 客户端 `reloadSkeletonPage` 假设升序来合并分页结果，若 upstream 默认排序变化会破坏合并。

**验证方法**：加一条集成测试（见 §9.3）：
1. mock upstream 返回指定顺序的 message 列表（含打乱的 created 时间戳）；
2. 调 `GET /slimapi/messages/{sid}` skeleton 端点；
3. 断言响应按 `time.created` 升序。

> **若该测试失败**：说明 upstream 不保证升序，则 sidecar 必须在 skeleton 投影后加一层 `sorted(items, key=lambda m: m["time"]["created"])`。这是 §9.3 测试要捕获的核心风险。

---

## 9. 测试要求

### 9.1 删除的测试（~200 条）

随 §3 退役文件整体删除对应的测试文件：

| 被删测试文件 | 对应退役源文件 | 备注 |
|---|---|---|
| `tests/test_questions_routes.py` | `routes/questions.py` | 整文件删 |
| `tests/test_sessions_children_route.py` | `routes/sessions_children.py` | 整文件删 |
| `tests/test_sessions_children_hint.py` | sessions children hint 逻辑 | 整文件删（与 children 路由配套） |
| `tests/test_children_cache.py` | `children_cache.py` | 整文件删 |
| `tests/test_hub_children_invalidation.py` | children invalidation 逻辑 | 整文件删 |
| `tests/test_stage_b_part_revision.py` | part revision / fingerprint 逻辑 | 整文件删（contentRevisions 退役） |

> **执行纪律**：每删一个测试文件，对应源文件必须先删 / 改完；避免「测试已删但源码还在」的中间状态。删除前后各跑一次 `pytest` 确认集合一致。

### 9.2 修改的测试（~20 条）

因端点删除 / digest 简化 / 行为变化而需要**修改**（非删除）的测试：

| 测试文件 | 修改原因 |
|---|---|
| `tests/test_messages_routes.py` | 删除 `/full?ids=` / `/since/{ts}` / `/full/{mid}` 的 304 / `?known=` / `X-Message-Event-Seq` 相关 case；删 `?mode=full` 列表分支 case；保留 `/full/{mid}` 纯展开 + `/messages?mode=skeleton` case |
| `tests/test_sessions_routes.py` | 删除 `/sessions/{sid}/status` / `/sessions/status` / `/projects` 相关 case |
| `tests/test_hub.py` | 删除 contentRevisions / childrenVersion / `_session_event_seq` / `_bump_message_seq` / `get_part_fingerprint` 相关 case；为 `message.part.updated` / `message.part.removed` 增加 updatedAt bump 断言 |
| `tests/test_hub_behavior_lock.py` | 删 part_state / fingerprint 相关行为锁 case |
| `tests/test_token_hub.py` | 确认 `_retired_messages` 守卫仍生效（保留），删 part revision 相关断言 |
| `tests/test_token_hub_flush.py` | 同上 |
| `tests/test_token_hub_lifecycle.py` | 同上 |
| `tests/test_globalhub_retired_gate.py` | 保留 `_retired_messages` 守卫测试，验证 late `message.part.updated` 不复活 |
| `tests/test_health.py` | 增加 `slimapi_contract: 2` 断言；accepted_client_versions 改为 (2, 2) |
| `tests/test_versioning.py` | accepted = (2, 2)；v1 客户端被拒绝、v2 通过 |
| `tests/test_skeleton.py` | 增加 created 升序断言（§8） |

### 9.3 新增的测试（~10 条）

| 测试 | 文件 | 断言 |
|---|---|---|
| digest 帧字段收敛 | `test_hub.py` | digest 帧 JSON 不含 `contentRevisions` / `childrenVersion`，只含 §4.1 的 6 字段 |
| `message.part.updated` 后 updatedAt 严格递增 | `test_hub.py` | 同毫秒内连续两次 part.updated，第二次 `digest.updatedAt > 第一次` |
| `message.part.removed` 后 updatedAt 严格递增 | `test_hub.py` | 同上，针对 part.removed |
| `bump_updated_at` 单调化 | `test_hub.py` | 同毫秒碰撞场景：`previous=now` 时 `entry.updated_at = now + 1` |
| skeleton 端点 created 升序 | `test_messages_routes.py` | mock upstream 返回打乱顺序的 created，响应严格升序 |
| `/full/{mid}` 无 `?known=` / 无 304 / 无 `X-Message-Event-Seq` | `test_messages_routes.py` | 请求带 `?known=...` 不报错但不短路；响应无 seq 头；永远 200 |
| `/messages?mode=full` 已删 | `test_messages_routes.py` | 传 `?mode=full` 被当作 skeleton（或参数被忽略），响应是 skeleton 投影 |
| `versioning.py` accepted = (2, 2) | `test_versioning.py` | v1 拒绝 400 + `version_incompatible`；v2 通过 |
| `/slimapi/health` `slimapi_contract: 2` | `test_health.py` | 响应 JSON 含 `slimapi_contract: 2` |
| 删除的端点返回 404 | `test_messages_routes.py` + `test_sessions_routes.py` + 新文件或 conftest fixture | §1 表中 10 个端点全部断言 404 |

---

## 10. 执行时间线

| 周 | 任务 | 验收标准 |
|---|---|---|
| **W1** | 删 10 端点 + 删 5 文件 + 简化 `/full/{mid}` + `/messages` | 被删端点 404；保留端点 200；`/full/{mid}` 无 304 / 无 seq 头；`/messages` 只剩 skeleton |
| **W2** | 简化 digest（删 2 字段）+ 删 hub 状态（part_state / seq / fingerprint）+ bump updatedAt + 清理 `app.py` | digest 帧 6 字段；part 事件 bump updatedAt；`app.py` 无 children / discovery / allowlist 残留 |
| **W3** | bump version 2 + 修测试 + 全测试 GREEN | `pytest` 全绿；新增 §9.3 的 10 条测试通过 |

> 每周末提交一次到 `lite-v2` 分支，附 WIP tag（`lite-v2-w1` / `lite-v2-w2` / `lite-v2-w3`），方便 ocdroid 侧阶段性拉取联调。

---

## 11. 与 ocdroid 的协调点

ocdroid 侧对应改造见 `ocdroid/docs/ocdroid-lite-aggressive-plan.md` §4（客户端改造清单）。

| 阶段 | oc-slimapi | ocdroid | 产物 |
|---|---|---|---|
| W1-W3 | 实现 lite-v2 | 实现 v2.7（删 13 文件 + `reloadSkeletonPage` 等） | 双方各自分支可独立编译 / 单测通过 |
| W3 结束 | **sidecar `lite-v2` 分支冻结**（不再有 breaking 改动） | — | sidecar 冻结 commit hash 同步给 ocdroid |
| W4 | 待命修 bug | **切到 sidecar W3 提交点联调**（客户端 token stream / skeleton reload 接 sidecar） | 联调问题清单 |
| W5 | 联调修复 | 联调修复 | 双向 bug 收敛 |
| W6 | 全测试 GREEN + staging 部署 | 全测试 GREEN + 模拟器回归 | staging 双端可用 |
| W7 | **同时合并 PR**（sidecar → main、ocdroid → main） | 同时合并 PR | 生产发版 |

**协调契约（必须双方对齐）**：
- `X-Slimapi-Version: 2`（客户端发，sidecar 收）
- digest 帧 6 字段（§4.1）
- digest.updatedAt 严格单调（§4.3）
- skeleton 升序（§8）
- token stream 透传（不改）
- catch-all 透传（不改）

---

## 12. 回滚

合并前在 oc-slimapi main 分支打 tag `pre-lite-v2-<date>`（如 `pre-lite-v2-2026-07-28`）。

**P0 bug 时**：从此 tag 重新部署 sidecar（ocdroid 同步回滚到 lite-v2 之前的 release）。lite-v2 是 contract breaking，**不能只回滚一端**——必须 sidecar + ocdroid 同时回滚到 pre-lite-v2 状态。

> tag 命名规范：`pre-lite-v2-YYYY-MM-DD`，与 ocdroid 侧同名 tag 一一对应。

---

## 附录 A：执行前自检清单

开始 W1 前，在 `lite-v2` 分支（从 main 新建）上确认：

- [ ] `git checkout main && git pull && git checkout -b lite-v2`
- [ ] `pytest` baseline 全绿（记录通过数，作为 W3 收敛的对照基线）
- [ ] `rg "contentRevisions|childrenVersion|_session_event_seq|get_part_fingerprint|_bump_message_seq"` 输出归档（作为 W2 删除完整性的验收依据）
- [ ] `rg "ACCEPTED_CLIENT_VERSIONS"` 输出归档（确认只改一处）
- [ ] 部署配置（`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` 环境变量）同步改为 `2,2`

## 附录 B：验收 checklist（W3 末）

- [ ] §1 的 10 个端点全部 404
- [ ] §3 的 5 个文件全部从仓库消失（`rg` 零命中）
- [ ] digest 帧 JSON 只含 §4.1 的 6 字段
- [ ] `message.part.updated` / `removed` 后 digest.updatedAt 严格递增
- [ ] `versioning.ACCEPTED_CLIENT_VERSIONS == (2, 2)`
- [ ] `/slimapi/health` 含 `slimapi_contract: 2`
- [ ] `/slimapi/messages/{sid}` skeleton 升序
- [ ] `pytest` 全绿（含 §9.3 的 10 条新测试）
- [ ] 打 tag `pre-lite-v2-<date>`
- [ ] 把 W3 末 commit hash 同步给 ocdroid 侧

## 附录 C：hub.py 拆分时机决策

> **结论：同意后续拆，但不在本轮 lite-v2 期间拆。**

### C.1 现状（执行前）

`src/oc_slimapi/sse/hub.py` 共 1587 行，可识别的功能簇：

| 簇 | 行号 | 行数 | 内聚度 |
|---|---|---|---|
| 辅助函数（`_sanitize_error_message` / `sse_frame` / `_now_ms` / `_upstream_line_bytes`） | 73-160 | ~90 | 高（纯函数） |
| `DigestFields` dataclass | 164-221 | ~58 | 高 |
| `Subscriber` + `SubscriberCapacityError` | 223-335 + 1265 | ~140 | 高（自包含） |
| `GlobalHub`（核心） | 336-1237 | **~900** | 低（混合多职责） |
| ┣ 构造 + upstream 生命周期 | 339-533 | ~195 | 中 |
| ┣ flush / heartbeat 循环 | 531-587 | ~57 | 高 |
| ┣ **精确同步簿记**（lite-v2 删除靶） | 598-726 | ~128 | — |
| ┣ `publish()` 巨型方法 | 727-1098 | **372** | 低 |
| ┣ resync / run / notify | 1099-1237 | ~140 | 中 |
| `HubRegistry` | 1280-1587 | ~300 | 高（自包含） |
| `_extract_session_id` | 1238-1264 | ~27 | 高（纯函数） |

### C.2 不在本轮拆的理由

1. **lite-v2 本身就是最大幅度的拆分（减重）**。删 `_part_state` / `_session_event_seq` / `_bump_message_seq` / `get_part_fingerprint` / `_bump_session_event_seq` + `publish()` 内 contentRevisions/fingerprint 分支 + `_children_cache` 链 + DigestFields 2 字段后，预计 hub.py **从 1587 行降到约 1100 行**（GlobalHub ~700 行、publish() ~250 行）。先做物理拆分再删，等于把要删的代码先搬到新文件再删，净负效益。
2. **拆分靶点与 lite-v2 删除靶点完全重叠**。GlobalHub 和 publish() 既是拆分候选又是 lite-v2 改动靶，叠加双重改动违反最小风险原则。
3. **循环依赖前科**。`sse/tokenstream/frames.py:25-36` 注释明示故意复制 `_now_ms` 而非 import 以避免成环；任何拆分都要先验证导入图。
4. **测试 monkeypatch 路径耦合**。`monkeypatch.setattr("oc_slimapi.sse.hub.asyncio.sleep", ...)` 和 `oc_slimapi.sse.hub.GRACE_SECONDS` 等路径在拆分后需要 facade re-export，否则测试断裂。

### C.3 lite-v2 完成后的拆分清单（follow-up）

lite-v2 落地后作为**独立重构**重新评估。届时基于已瘦身的稳定形态拆分：

- `sse/frames.py`：`sse_frame` + `_now_ms` + `_upstream_line_bytes` + `_sanitize_error_message` + 相关常量
- `sse/digest.py`：`DigestFields`
- `sse/subscriber.py`：`Subscriber` + `SubscriberCapacityError`
- `sse/registry.py`：`HubRegistry`
- `sse/hub.py`：只剩 `GlobalHub`，并通过 `from .xxx import *` 做 facade re-export 维持公共 API + monkeypatch 路径

**先决条件**：
- lite-v2 已合并到 main 且测试稳定 GREEN
- 拆分独立分支，单独评审
- 拆分**纯物理搬迁**，不改行为（行为重构应另起 PR）
