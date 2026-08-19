# slimapi 复杂度与状态机全景清单（2026-08-17，exp-s 探索报告）

> 用途：为「全局单实例」架构重塑提供复杂度基线与删除清单。所有结论 file:line 实证。

## ① 路由 × 参数矩阵

### /slimapi/sessions 组

| 路由 | 方法 | Query 参数 | Directory 消费 | 文件:行 |
|---|---|---|---|---|
| `/slimapi/sessions` | GET | `search`, `roots`, `limit`, `start`, `directory` | 消费 (`?directory=`) | `routes/sessions.py:33-40,241-265` |
| `/slimapi/sessions/status` | GET | `directory` | 消费 | `routes/sessions.py:350-351` |
| `/slimapi/sessions/{sid}/todo` | GET | `directory` | 消费 | `routes/todo.py:18-21` |
| `/slimapi/sessions/{sid}/children` | GET | `directory` | 消费 | `routes/children.py:18-21` |
| `/slimapi/sessions/{sid}/diff` | GET | `directory`, `messageID` | 消费 | `routes/diff.py:18-22` |
| `/slimapi/sessions/{sid}/stream` | GET | `directory` (query-only 不消费, §5.6 no-op) | 消费集 | `routes/token_stream.py:30-33` |

**跨目录 fan-out 现状**：`/slimapi/sessions` **无** fan-out——`?directory=` 直接转发上游单目录。仅 questions/permissions 有跨目录聚合（见③）。**无 archived 参数**（上游 `/experimental/session` 有，未收编）。

### /slimapi/messages 组

| 路由 | 方法 | Query 参数 | 文件:行 |
|---|---|---|---|
| `/slimapi/messages/{sid}` | GET | `limit`, `start`, `directory`, `mode=merged` | `routes/messages.py:843+` |
| `/slimapi/messages/{sid}/full/{mid}` | GET | `directory` | `routes/messages.py:1032+` |
| `/slimapi/messages/{sid}/expand/{category}/{mid}[/{partID}]` | GET | `directory` | `routes/messages.py:1208+`（v3.1.0 新增） |

### 其他关键路由

| 路由 | 方法 | Query 参数 | 文件:行 |
|---|---|---|---|
| `/slimapi/events`（SSE） | GET | `tokens` (0/1) | `routes/events.py:30-33` |
| `/slimapi/questions` | GET | `directory`（可选, **逗号分隔多目录**） | `routes/questions.py:30-35` |
| `/slimapi/permissions` | GET | `directory`（可选, 逗号分隔多目录） | `routes/permissions.py:30-35` |
| `/slimapi/agent` / `/slimapi/command` | GET | `directory` | `routes/agent.py:18` / `command.py:18` |
| `/slimapi/directories` | GET | — | `routes/directories.py:18`（发现源=`GET /experimental/session?roots=true`） |
| `/slimapi/file` + `/file/content` + `/file/status` | GET | `directory` + 各自参数 | `routes/read_groups.py:30-73` |
| `/slimapi/vcs` + `/vcs/status` + `/vcs/diff` | GET | `directory` | `routes/read_groups.py:90-133` |
| `/slimapi/find/file` | GET | `directory` | `routes/read_groups.py:150-153` |
| `/slimapi/session/{sid}`（写×10：POST/PATCH/DELETE/prompt_async/abort/summarize/fork/revert/command/permissions） | POST 等 | `directory` | `routes/write_groups.py:30-213` |

## ② 状态机/缓存清单（15 个有状态组件）

| 组件 | 位置 | 持有状态 | 生命周期 | 存在原因 | 归因 |
|---|---|---|---|---|---|
| **GlobalHub** | `sse/global_hub.py:56` | subscribers, pending digests, _last_updated_at_by_sid, sticky_last_error, deleted_tombstones, _retired_messages, 4 async tasks | 进程级单例；idle GRACE 后拆除 | 上游 SSE 单连接→多订阅者扇出+digest 策展 | **(a) essential** |
| **HubRegistry** | `sse/registry.py:34` | _global, total_subscribers, rejected_total | app lifespan | T3 准入+生命周期 | (a) |
| **TokenStreamHub** | `sse/tokenstream/hub.py:178` | live_parts, _pending (DeltaAccumulator), _nontext/_disabled_parts, _part_revisions, _session_status, _busy_sids, _subs_by_sid, _removed/_retired, _total_live/pending_bytes, _flush_task | app lifespan | token delta 逐 part 累加+按 session 扇出 | **(a) essential** |
| **TokenStreamRegistry** | `sse/tokenstream/registry.py` | _hub, _subscribers | app lifespan | token 订阅准入 | (a) |
| **SingleFlight (fulls)** | `sse/singleflight.py:329` | _entries, _retained_bytes | 模块级全局 | /full 同 key 上游 GET 去重 | **(b) compensation** |
| **LeasedSingleFlight** | `leased_singleflight.py:160` | _active, _retired, _leased_bytes, _network_sem | app.state | 列表路由上游 GET 合并 | **(b) compensation** |
| **CatalogCache** | `catalog_cache.py:37` | _entries (TTL), _sf | app.state | agent/command 目录 TTL 缓存 | **(b) compensation** |
| **TransformPool** | `transform.py:195` | _semaphore, _executor, _active, _waiting | app.state | CPU 密集变换准入+离线程 | **(a) essential** |
| **TrafficLedger** | `traffic.py:300` | _buckets, _sse, _latencies, _v3_matrix, _expand | app.state | 字节记账+v3 可观测 | (a) |
| **TrafficSnapshotter** | `traffic_snapshot.py` | 定时落盘 | app.state | SSE 成本持久化 | (a) |
| **TurnRegistry** | `turn_registry.py:203` | incarnation, _turns | app.state | ocdroid 因果 fence | (a) |
| **IncarnationStore** | `turn_registry.py:67` | 磁盘 incarnation 文件 | 进程级 | 计数器持久化 | (a) |
| **ETag 模块** | `etag.py` | 纯函数无状态 | — | 304 条件请求 | **(b) compensation** |
| **questions_semaphore** | `app.py:373` | Semaphore | app.state | 跨目录聚合并发上限 | **(b) compensation** |
| **permissions_semaphore** | `app.py:379` | Semaphore | app.state | 同上 | **(b) compensation** |
| **SlimapiSelectorMiddleware** | `selector.py:337` | 无状态 | — | v3 选择+directory 消费 | (a) |

**归因统计**：essential 10 / compensation 5 / historical 0。

## ③ directory/instance 模型耦合点

- `selector.py:432-497` `_consume_v3_directory()` — 消费集路由 directory 验证+剥离+存 scope state
- `selector.py:116-161` `_DIRECTORY_CONSUMING_PATTERNS` — ~25 个 pattern 白名单
- `selector.py:241-256` `resolve_route_directory()`
- `directory.py:12-52` normalize/validate 纯函数
- questions/permissions 跨目录聚合：`?directory=dir1,dir2` 逗号分隔 → per-directory `asyncio.gather`（semaphore 上限）+ LeasedSingleFlight + merge（`routes/questions.py:30-35`、`permissions.py:30-35`）
- `_catalog_common.py:55-94` / `_read_passthrough.py:157-277` — directory 作为 `X-Opencode-Directory` 头转发上游

## ④ 简化机会汇总

| 若上游提供/架构改为... | 可删除 | 行数量级 |
|---|---|---|
| 原生全局 sessions 查询（/experimental/session 收编） | questions/permissions 跨目录 fan-out + 2 semaphores | ~400 行 |
| 原生 GET 去重 | SingleFlight + LeasedSingleFlight（两个独立实现） | **~770 行（最大杠杆）** |
| 原生目录缓存 | CatalogCache | ~180 行 |
| 原生 token stream | TokenStreamHub + Registry（核心省流特性，慎动） | ~800 行 |
| 原生 ETag | etag.py + 路由判断 | ~300 行 |
| 原生因果 fence | TurnRegistry + IncarnationStore | ~250 行 |

## ⑤ 测试面

78 个测试文件 / ~366 个测试函数 / ~125 测试类。`tests/sse/`（GlobalHub+TokenStreamHub）占比最大——状态机复杂度代理指标。

---
*探索者注：本报告路由参数以源码 grep 为准；`/slimapi/sessions` 的 `archived` 参数在初版表格误报，实际上游 `/experimental/session` 才有 archived（见 interface-priority-assessment-2026-08-17.md）。*
