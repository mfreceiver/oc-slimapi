# E2 路由普查（route-census.md）

> 机器真值源 = `route-census.csv`（24 列冻结 schema）；本文件仅由 CSV 渲染。
> 继承错误适用表 = `inherited-error-applicability.csv`（337 行）；两份 validation log 同目录。
> 证据基线 BASELINE_HEAD=0b836e7。

## 双计数声明（完成判据）

- ① `presence ∈ {actual_only, both}` 的唯一 (method, path) 数 = **54** = inventory routes 唯一 (method,path) 集合大小（54）✓
- ② 总行数 = **54** = 联合主键数（实现 54 ∪ v3 契约路由表 ∪ v4 契约路由表 = 54，契约声明路由全部已实现）
- contract_only 行数 = **0**（无「契约要求但实现缺失」路由 → 零 contract-violation draft）

## 关键列速览（全列见 CSV）

| method | path | 版本面 | directory | projection | cache | bucket | actual local codes | feature gate | boundary |
|---|---|---|---|---|---|---|---|---|---|
| DELETE | `/slimapi/session/{session_id}` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/actions` | dual | tolerant | none | none | other | NONE | NONE | NO |
| GET | `/slimapi/agent` | dual | query | skeleton | catalog_cache | agent | response_too_large, transform_busy, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/api/session/active` | dual | none | none | none | session_active | response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/command` | dual | query | skeleton | catalog_cache | command | response_too_large, transform_busy, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/config/providers` | dual | query | mixed | none | providers | provider_projection_limit, provider_upstream_malformed, response_too_large, transform_busy, upstream_http_<N>, upstream_unavailable | providers.redacted.v4 | YES |
| GET | `/slimapi/directories` | dual | tolerant | skeleton | none | directories | response_too_large, transform_busy, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/events` | dual | tolerant | none | none | events_sse | invalid_tokens, sse_subscriber_limit_directory, sse_subscriber_limit_total, tokens_stream_retired_in_v4 | NONE | YES |
| GET | `/slimapi/file` | dual | query | none | none | file | directory_not_allowed, response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/file/content` | dual | query | none | none | file | directory_not_allowed, response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/file/status` | dual | query | none | none | file | directory_not_allowed, response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/find/file` | dual | query | none | none | find | response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/global/health` | dual | none | none | none | global_health | response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/health` | dual | tolerant | none | none | health | NONE | NONE | NO |
| GET | `/slimapi/messages/{sid}` | dual | query | mixed | singleflight | messages | directory_not_allowed, response_too_large, transform_busy, upstream_http_<N>, upstream_unavailable | messages.expand.v4 | YES |
| GET | `/slimapi/messages/{sid}/expand/{category}/{mid}` | dual | query | none | singleflight | messages.expand | expand_category_mismatch, expand_fragment_too_large, expand_source_too_large, expand_target_not_found, invalid_expand_category, session_not_found, transform_busy, upstream_http_<N>, upstream_invalid_shape, upstream_unavailable | messages.expand.v4 | YES |
| GET | `/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}` | dual | query | none | singleflight | messages.expand | expand_category_mismatch, expand_fragment_too_large, expand_source_too_large, expand_target_not_found, invalid_expand_category, session_not_found, transform_busy, upstream_http_<N>, upstream_invalid_shape, upstream_unavailable | messages.expand.v4 | YES |
| GET | `/slimapi/messages/{sid}/full/{mid}` | dual | query | none | singleflight | messages | message_too_large, response_too_large, session_not_found, transform_busy, upstream_http_<N>, upstream_invalid_shape, upstream_unavailable | messages.expand.v4 | YES |
| GET | `/slimapi/metrics` | dual | tolerant | none | none | metrics | NONE | NONE | NO |
| GET | `/slimapi/permissions` | dual | tolerant | skeleton | none | other | response_too_large, upstream_http_<N>, upstream_unavailable | NONE | YES |
| GET | `/slimapi/questions` | dual | tolerant | none | none | questions | response_too_large, upstream_http_<N>, upstream_unavailable | NONE | YES |
| GET | `/slimapi/ready` | dual | tolerant | none | none | health | NONE | NONE | NO |
| GET | `/slimapi/session/{sid}` | dual | query | mixed | none | session_single | auxiliary_unavailable, response_too_large, session_not_found, upstream_http_<N>, upstream_unavailable | session.single.projection.v4 | NO |
| GET | `/slimapi/session/{sid}/context` | dual | tolerant | none | none | session_context | response_too_large, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/sessions` | dual | query | mixed | singleflight | sessions | auxiliary_unavailable, directory_retired_in_v4, invalid_cursor, param_version_mismatch, response_too_large, transform_busy, upstream_http_<N>, upstream_unavailable | representation.vary.v4 | YES |
| GET | `/slimapi/sessions/status` | dual | query | none | none | sessions | response_too_large, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/sessions/{sid}/children` | dual | query | skeleton | none | sessions | response_too_large, session_not_found, transform_busy, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/sessions/{sid}/diff` | dual | query | none | none | sessions | response_too_large, session_not_found, transform_busy, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/sessions/{sid}/stream` | dual | query | none | none | token_stream_sse | directory_not_allowed, invalid_directory, invalid_directory_selector, sse_token_handshake_overflow, sse_token_subscriber_limit | NONE | YES |
| GET | `/slimapi/sessions/{sid}/todo` | dual | query | skeleton | none | sessions | response_too_large, session_not_found, transform_busy, upstream_http_<N>, upstream_unavailable | NONE | NO |
| GET | `/slimapi/vcs` | dual | query | none | none | vcs | response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/vcs/diff` | dual | query | none | none | vcs | response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/vcs/status` | dual | query | none | none | vcs | response_too_large, upstream_unavailable | NONE | NO |
| GET | `/slimapi/versions` | none | tolerant | none | none | other | method_not_allowed | NONE | NO |
| PATCH | `/slimapi/session/{session_id}` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | YES |
| POST | `/slimapi/actions/{name}` | dual | tolerant | none | none | other | action_busy, action_confirm_required, action_not_found, action_throttled, action_timeout, action_unavailable, actions_disabled, invalid_request_body, request_too_large | NONE | YES |
| POST | `/slimapi/question/{request_id}/reject` | dual | query | none | none | write_question | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/question/{request_id}/reply` | dual | query | none | none | write_question | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}` | v4 | query | none | none | write_session | request_too_large, response_too_large, thin_route_not_found, upstream_unavailable | session.post-actions.v4 | YES |
| POST | `/slimapi/session/{session_id}/abort` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/agent` | dual | tolerant | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/archive` | v4 | query | none | none | write_session | request_too_large, response_too_large, thin_route_not_found, upstream_unavailable | session.post-actions.v4 | YES |
| POST | `/slimapi/session/{session_id}/command` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/delete` | v4 | query | none | none | write_session | request_too_large, response_too_large, thin_route_not_found, upstream_unavailable | session.post-actions.v4 | NO |
| POST | `/slimapi/session/{session_id}/fork` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/model` | dual | tolerant | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/permissions/{permission_id}` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/prompt_async` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/revert` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/revert/clear` | dual | tolerant | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/revert/commit` | dual | tolerant | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/revert/stage` | dual | tolerant | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |
| POST | `/slimapi/session/{session_id}/summarize` | dual | query | none | none | write_session | request_too_large, response_too_large, upstream_unavailable | NONE | NO |

## 与 INTERFACE_MAP 对账

- 54/54 路由在 INTERFACE_MAP.md 均有表行命中（route-raw.json 汇聚；与 check.sh「54 条路由↔文档一致」互证）。
- check_routes_doc.py 为存在性 + method + 7 条语义白名单的部分对账；本 census 为全量人工对账，未发现 INTERFACE_MAP 多出/缺失路由行。
- 描述级差异（语义列内容 vs 实现现状）留给 A14 文档漂移审计。

## 契约引用覆盖说明

- 5 条路由无契约路径字面命中但契约散文覆盖：`GET /slimapi/actions`、`POST /slimapi/actions/{name}`（v3 §5 宽容集 + §8 收编全集；**详细 wire 规范留存于 v2-contract §2 历史**——D14 漂移项）；`children`/`diff`（v3 §10.a 注 + §5 消费集）；`full/{mid}`（v3 §4a/§4b）。
- `provider_projection_limit`/`sse_*_limit`/`invalid_expand_category` 等码经多行构造，未被 inventory 正则捕获（inventory 34 → 实际全集见 census 列；A4 三向对账以 rg 为准）。

## schema 冻结扩展记录

- `projection` 枚举在冻结五值外扩展 `mixed`（v3/v4 双面异构投影：sessions= envelope|dbaux、providers= 透传|providers_v4、session single= skeleton|dbaux、messages= skeleton+envelope）——validator 已同步冻结该扩展。
- 错误码族记法：`upstream_http_<N>` 表示 f-string 动态族（upstream_errors.py:60/83/104）。
