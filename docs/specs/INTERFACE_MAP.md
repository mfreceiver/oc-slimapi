# INTERFACE_MAP — 当前 `/slimapi` 路由追踪

> **当前实现索引，不是历史协议说明。** Wire 权威是
> [`v4-contract.md`](v4-contract.md)；webui/ocdroid 完整导航与 DTO 见
> [`PROTOCOL.md`](PROTOCOL.md)。`v2-contract.md` / `v3-contract.md` 仅是历史存档。

当前服务面是 **v4-only**：除 `GET /slimapi/versions` 外，每个端点只接受
`?v=4`。旧版本头不参与协商；未注册/retired 路径本地返回
`thin_route_not_found`，不存在 catch-all 上游转发。

## 共同实现边界

- selector：`src/oc_slimapi/selector.py`；版本常量：`versioning.py`。
- JSON/ETag/gzip/error：`routes/_common.py`、`compression.py`、
  `upstream_errors.py`、`error_handler.py`。
- 所有 wire body 都有请求/响应预算；`transform_busy` 与部分 503 带
  `Retry-After`。上游 5xx/网络错误不原样透传。
- directory canonical 通道是 query；入站 directory header 已退役。
- SSE：meta 首帧/no-id，global/token 独立 replay ledger，共享 process epoch；
  没有 `server.connected` 或 snapshot handshake。
- 路由表由 `scripts/check_routes_doc.py` AST gate 与源码 decorator 对齐。

## 当前 56 个 method/path

| 路由 | 实现 | 当前成功形状 / 关键错误 |
|---|---|---|
| **GET `/slimapi/versions`** | `routes/versions.py` | `{current:4,available:[4],capabilities,sidecarVersion}`；selector-exempt，GET-only |
| **GET `/slimapi/health`** | `routes/health.py` | sidecar/server/schema/features/auxiliary；保留 `tokenCoalesce` 与版本诊断字段 |
| **GET `/slimapi/ready`** | `routes/health.py` | `{upstream,server,schema}`；不 ready 时同形状 503 |
| **GET `/slimapi/metrics`** | `routes/metrics.py` | 动态 ops blocks；保留 `traffic.v3` 历史 schema label，不表示 v3 wire |
| **GET `/slimapi/actions`** | `routes/actions.py` | `{enabled,actions[]}` |
| **POST `/slimapi/actions/{name}`** | `routes/actions.py` | query/exec action DTO；`action_not_found`, `action_confirm_required`, `action_throttled`, `request_too_large` |
| **GET `/slimapi/directories`** | `routes/directories.py` | `{items,directory counters...,discoveryComplete}`；allowlist filter |
| **GET `/slimapi/sessions`** | `routes/sessions.py` | `{items,nextCursor,complete,degraded}`；`upstream_http_N`, `upstream_unavailable`, `auxiliary_unavailable`, `invalid_cursor` |
| **GET `/slimapi/session/{sid}`** | `routes/read_groups.py` | 裸 `SessionSkeletonV4`；`session_not_found`, `upstream_http_N`, `upstream_unavailable`, `auxiliary_unavailable` |
| **POST `/slimapi/sessions/details`** | `routes/sessions_details.py` | `{sessions,missing}`；`invalid_body`, `too_many_sids`, `transform_busy`, `upstream_unavailable` |
| **GET `/slimapi/sessions/status`** | `routes/sessions_status.py` | `Record<sid,status>`；`upstream_http_N`, `upstream_unavailable` |
| **GET `/slimapi/messages/{sid}`** | `routes/messages/list.py` | `{items,nextCursor,nextSince?,removed?}`；baseline/merged、before/since；`session_not_found`, `upstream_http_N`, `upstream_unavailable`, `transform_busy` |
| **GET `/slimapi/messages/{sid}/full/{mid}`** | `routes/messages/full.py` | full message，无 ETag；`session_not_found`, `message_too_large`, `upstream_http_N`, `upstream_unavailable`, `transform_busy` |
| **GET `/slimapi/messages/{sid}/expand/{category}/{mid}`** | `routes/messages/expand.py` | message-level expand；`EXPAND_CATEGORIES` 共 `12` 类；category/target/source/fragment/busy errors |
| **GET `/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}`** | `routes/messages/expand.py` | part-level expand；`EXPAND_CATEGORIES` 共 `12` 类；category/target/source/fragment/busy errors |
| **GET `/slimapi/sessions/{sid}/todo`** | `routes/todo.py` | `{content,status,priority}[]`；ETag/304；session/upstream/cap/busy errors |
| **GET `/slimapi/sessions/{sid}/children`** | `routes/children.py` | session skeleton array；ETag/304；session/upstream/cap/busy errors |
| **GET `/slimapi/sessions/{sid}/diff`** | `routes/diff.py` | `{file?,patch?,additions,deletions,status?}[]`；ETag/304 |
| **GET `/slimapi/agent`** | `routes/agent.py` | whitelist array；`upstream_http_N`, `upstream_unavailable`, `transform_busy` |
| **GET `/slimapi/command`** | `routes/command.py` | whitelist array；`upstream_http_N`, `upstream_unavailable`, `transform_busy` |
| **GET `/slimapi/config/providers`** | `routes/read_groups.py` + `providers_projection.py` | canonical ProviderResult；malformed/limit/cap/upstream/busy errors |
| **GET `/slimapi/file`** | `routes/read_groups.py` | `LegacyEntry[]` 受控 passthrough；ETag/304/cap/upstream errors |
| **GET `/slimapi/file/content`** | `routes/read_groups.py` | `LegacyContent` 受控 passthrough；ETag/304/cap/upstream errors |
| **GET `/slimapi/file/status`** | `routes/read_groups.py` | `LegacyStatus[]` 受控 passthrough；ETag/304/cap/upstream errors |
| **GET `/slimapi/file/raw`** | `routes/file_raw.py` | binary/text raw；query-only directory；`directory_not_allowed`, `raw_decode_failed`, cap/upstream errors |
| **GET `/slimapi/vcs`** | `routes/read_groups.py` | `Vcs.Info` 受控 passthrough；ETag/304/cap/upstream errors |
| **GET `/slimapi/vcs/status`** | `routes/read_groups.py` | `Vcs.FileStatus[]` 受控 passthrough |
| **GET `/slimapi/vcs/diff`** | `routes/read_groups.py` | diff array；mode/context query |
| **GET `/slimapi/find/file`** | `routes/read_groups.py` | `string[]`；query/dirs/type/limit |
| **GET `/slimapi/api/session/active`** | `routes/read_groups.py` | `{data:Record<sid,SessionActive>}` 受控 passthrough |
| **GET `/slimapi/global/health`** | `routes/read_groups.py` | `{healthy,version}` 受控 passthrough |
| **GET `/slimapi/session/{sid}/context`** | `routes/read_groups.py` | `{data:unknown[]}`；directory tolerant-ignore |
| **GET `/slimapi/questions`** | `routes/questions.py` | `{items,errors,authoritativeDirectories,discoveryComplete,truncated?}` |
| **GET `/slimapi/permissions`** | `routes/permissions.py` | aggregate envelope；permission whitelist + directory |
| **GET `/slimapi/events`** | `routes/events.py` + `sse/global_hub.py` | native-v4 global SSE：meta/replay/digest/q/p/error/heartbeat/resync；无 token multiplex |
| **GET `/slimapi/sessions/{sid}/stream`** | `routes/token_stream.py` + `sse/tokenstream/` | native-v4 token SSE：meta/delta/removed/token_memory_limit/heartbeat/control-resync |
| **POST `/slimapi/session`** | `routes/write_groups.py` | controlled create passthrough；request/response cap；5xx/network→503 |
| **PATCH `/slimapi/session/{session_id}`** | `routes/write_groups.py` | controlled update passthrough |
| **DELETE `/slimapi/session/{session_id}`** | `routes/write_groups.py` | controlled delete passthrough |
| **POST `/slimapi/session/{session_id}`** | `routes/write_groups.py` | 与 PATCH 等效 |
| **POST `/slimapi/session/{session_id}/archive`** | `routes/write_groups.py` | 空 body 合成 archived epoch-ms，否则 body 透传 |
| **POST `/slimapi/session/{session_id}/delete`** | `routes/write_groups.py` | 与 DELETE 等效，保留 body/content-type |
| **POST `/slimapi/session/{session_id}/prompt_async`** | `routes/write_groups.py` | PromptPayload passthrough |
| **POST `/slimapi/session/{session_id}/abort`** | `routes/write_groups.py` | abort passthrough |
| **POST `/slimapi/session/{session_id}/summarize`** | `routes/write_groups.py` | SummarizePayload passthrough |
| **POST `/slimapi/session/{session_id}/fork`** | `routes/write_groups.py` | optional messageID；返回 forked session |
| **POST `/slimapi/session/{session_id}/revert`** | `routes/write_groups.py` | `{messageID,partID?}` passthrough |
| **POST `/slimapi/session/{session_id}/permissions/{permission_id}`** | `routes/write_groups.py` | `{response}` passthrough |
| **POST `/slimapi/question/{request_id}/reply`** | `routes/write_groups.py` | ReplyPayload passthrough |
| **POST `/slimapi/question/{request_id}/reject`** | `routes/write_groups.py` | reject passthrough |
| **POST `/slimapi/session/{session_id}/command`** | `routes/write_groups.py` | CommandPayload passthrough |
| **POST `/slimapi/session/{session_id}/agent`** | `routes/write_groups.py` | `{agent}`；204；directory ignored |
| **POST `/slimapi/session/{session_id}/model`** | `routes/write_groups.py` | `{model}`；204；directory ignored |
| **POST `/slimapi/session/{session_id}/revert/stage`** | `routes/write_groups.py` | `{messageID,files?}`；`{data}` |
| **POST `/slimapi/session/{session_id}/revert/clear`** | `routes/write_groups.py` | 204 |
| **POST `/slimapi/session/{session_id}/revert/commit`** | `routes/write_groups.py` | 204 |

## 保留但不代表旧 wire 支持

- `metrics.traffic.v3`、traffic snapshot `v3`：历史 ops schema label。
- read-passthrough ETag 的旧 representation label：validator 稳定性。
- `features.tokenCoalesce=true`：当前能力诊断字段；`/events?tokens=1` 仍退役。
- `LegacyContent`、upstream `question.v2.*` / `permission.v2.*`：上游命名。
- deprecated config/access-log/turn-state migration：部署/状态迁移，不开放 v1/v2/v3。
