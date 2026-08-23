# ocdroid / webui 当前接入清单

> 当前服务面是 **v4-only**。Wire 权威见 [`v4-contract.md`](v4-contract.md)；
> 可直接实现的完整路由/DTO/SSE 导航见 [`PROTOCOL.md`](PROTOCOL.md)。
> 本文件只列消费方必须完成的集成动作，不保留旧版本兼容教程。

## 1. 请求路由与版本

- 除 `GET /slimapi/versions` 外，所有 sidecar 请求都带 query `v=4`。
- 不发送 `X-Slimapi-Version`；它不能替代 `?v=4`。
- directory 只使用 query `directory`；不发送 `X-Opencode-Directory`。
- 不依赖 sidecar catch-all。旧 `/session/**`、`/event`、`/global/event` 与
  未注册 `/slimapi/**` 会本地 404 `thin_route_not_found`。
- 若客户端仍保留 opencode 直连回退，必须显式维护直连 base URL、认证与
  路由表；不能把 sidecar 404 当作“继续由 sidecar 转发”。

## 2. 启动发现

启动先读 `/slimapi/versions`，确认：

```json
{"current":4,"available":[4],"capabilities":{"4":{}},"sidecarVersion":"..."}
```

再读 `/slimapi/health?v=4`。`features` 是能力诊断，不能推导旧路由复活：

- `tokenCoalesce=true` 不表示 `/events?tokens=1` 可用；token 只走
  `/slimapi/sessions/{sid}/stream?v=4`。
- `server.api_version`、`accepted_client_versions` 与 schema min/max 是当前
  诊断字段，不是双版本窗口。

## 3. Sessions 与目录

- 使用 global `/slimapi/sessions` envelope：`items,nextCursor,complete,degraded`。
- session skeleton 的 `directory` 是 workdir 归属；客户端按它分组/筛选。
- `project` absent/null 语义、partial/degraded 规则严格按 `PROTOCOL.md`。
- 不再调用 roots/start/children 等旧聚合协议；children 当前是按 sid 的
  `/slimapi/sessions/{sid}/children`。
- `/slimapi/directories` 只返回 passive-discovered root 目录；
  `discoveryComplete=false` 时不要把缺失目录当作被删除。
- questions/permissions 只有 `authoritativeDirectories:null` 才允许 replace-all；
  数组只授权替换成功目录，保留其它目录的本地 pending UI。

## 4. Messages

- list 消费 `{items,nextCursor,nextSince?,removed?}`，不读取旧 cursor headers。
- 保存 `nextSince`；下轮用 `since`。reset 是正常 200 全量 + 新 token。
- `since` 与 `before` 互斥。`nextSince` absent 时丢弃旧 token。
- 只有字面 `mode=merged` 请求 best-effort full splice；其它 mode 是 baseline。
- 根据 skeleton `expandRefs` 按需调用 expand；413 fragment 可回退 `/full`。
- `/full` 无 ETag；它只在 part 有非空 `time.end` 时可覆盖 token live text。
- patch `files` 按归一化对象数组渲染，不兼容旧 string/mixed 形状。

## 5. SSE

### Global

- 只连 `/slimapi/events?v=4`；不带 directory，不带 `tokens=1`。
- 第一帧必须是 no-id `slimapi.meta`。保存 `epoch` 与 global last seq。
- 处理 `session.digest`、question/permission immediate frames、heartbeat、resync。
- `messagesRevision` 只在同 sid 内比较；changed 是 compact invalidation hint。

### Token

- 每个 active sid 连 `/slimapi/sessions/{sid}/stream?v=4`。
- 第一帧是 token meta；每 sid 保存独立 `(epoch,lastAppliedSeq)`。
- 业务帧只有 `message.part.delta`、`message.removed`、replayable
  `resync{reason:"token_memory_limit"}`。没有 snapshot/`server.connected`/done marker。
- `partEventRevision` 只属于 active-v4 delta；不要当 global revision。
- 冻结 control resync 无 id/seq，触发 HTTP reconciliation；值域只有
  `epoch_changed|replay_expired|replay_gap|reconnect_no_replay`。
- `session_idle` / `session_deleted` 在 active-v4 原 token 连接上均为 STOP-only
  terminal disconnect，不发送同名 resync。删除权威信号来自 global
  `session.digest{deleted:true}`；两者 barrier 后携旧 cursor 重连得到
  `reconnect_no_replay`，再做 HTTP reconciliation。

### 重连

- 重连发送当前域的 `Last-Event-ID`；global/token ledger 不能混用。
- meta 先于 replay/live。收到 `epoch_changed`、`replay_expired`、`replay_gap`、
  `reconnect_no_replay` 时做 authoritative HTTP reconcile。
- `token_memory_limit` 是 advisory，不清其它 part；等 revision 变化后用
  messages since/full 收敛。

## 6. HTTP 缓存、错误与退避

- ETag 路由缓存 200 body + validator；304 复用本地 body。
- identity strong ETag 与 gzip weak ETag 都按弱比较使用。
- `/full`、expand、SSE、write、versions 不做 ETag 缓存。
- 503 按 `Retry-After` 退避；`transform_busy` 不立即自旋。
- 413 应缩小请求/换 expand/full 策略，不能无条件重试相同 payload。
- 502 是确定性 upstream status/shape 问题；400/422 修正 selector/参数/body。
- JSON 忽略未知 key；SSE 忽略未知 event type。

## 7. Controlled writes

- 使用 PROTOCOL 的 20 个 `/slimapi` write route；不要通过 catch-all 写上游。
- success/3xx/4xx body/status 可能来自 upstream；5xx/network 统一为 503。
- archive 空 body 由 sidecar 合成当前 epoch-ms；非空 body 透传。
- POST session-id/delete 等价动作与 PATCH/DELETE 的 body/content-type 语义一致。
- write 无 ETag、无自动级联/重试/补偿事务。

## 8. 完成标准

- [ ] 全部 sidecar 请求只使用 `?v=4`。
- [ ] 代码库不发送旧版本头或 inbound directory header。
- [ ] 无依赖 sidecar catch-all 的 API 路由。
- [ ] sessions/messages/aggregate envelope 与 null/absent 语义已覆盖测试。
- [ ] global/token SSE 独立 ledger、meta-first、resync reconciliation 已覆盖测试。
- [ ] token live text 与 REST terminal full 合并规则已覆盖测试。
- [ ] ETag/304/gzip/Retry-After/413/502/503 已覆盖集成测试。

## 9. 历史迁移资料

旧 v2/v3 wire、双版本窗口、版本头、catch-all 与 token snapshot 的发布历史只
在 `CHANGELOG.md`、`v2-contract.md`、`v3-contract.md` 和带“历史设计”标识的
design 文档中保留。它们用于考古，不能作为当前客户端实现依据。
