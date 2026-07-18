# Changelog

本文件记录 **oc-slimapi 的接口与行为变更**，供 **ocdroid** 对接与运维查阅。

格式 loosely 遵循 [Keep a Changelog](https://keepachangelog.com/)，版本遵循 [SemVer](https://semver.org/)。

## 版本双轨（必读）

| 轨道 | 是什么 | 何时变 |
|---|---|---|
| **包版本** `vX.Y.Z`（本文件标题 + git tag + `sidecar/pyproject.toml`） | 产品发版版本 | 每次 `./scripts/release.sh` |
| **Wire API 版本** `X-Slimapi-Version`（整数，见 `versioning.py` / 契约 §1） | 协议兼容门禁 | **仅破坏性** wire 变更 bump；加性变更 **不** bump |

ocdroid 对接时：

1. 读本文件了解**行为**变更；
2. 读 `docs/v1-contract.md` 了解**当前完整契约**；
3. 用 `/slimapi/health` 的 `server.api_version` / `accepted_client_versions` 做运行时兼容自检。

### 维护规约

- **每次**用户可见 / 客户端可观测的 wire 行为变更，必须在对应版本下增加条目（Added / Changed / Fixed / Removed / Security）。
- 条目写**行为与路径**，不写实现细节（避免“改了哪行 Python”）。
- 破坏性变更：同时更新 `docs/v1-contract.md` + bump wire API 版本 + 在本文件 **Changed** 中显式写 `X-Slimapi-Version` 与客户端必改点。
- 发版时由 `./scripts/release.sh` 校验本文件含有目标版本标题（见 `docs/release.md`）。

---

## [Unreleased]

> 开发中、尚未打 tag 的变更写在这里；`release.sh` 发版时把本节内容折叠进新版本标题下。

### Added

### Changed

### Fixed

### Removed

---

## [0.1.0] - 2026-07-18

首个可交付 v1 收敛版。Wire API 版本 = **1**（`X-Slimapi-Version: 1`）。

### Added

- **版本门禁**：所有 `/slimapi/**`（含 SSE）必须带整数头 `X-Slimapi-Version: 1`；缺/非整数 → `400 version_required`；越界 → `400 version_incompatible`（带 `client`/`accepted`）。
- **健康检查**：`GET /slimapi/health`、`GET /slimapi/ready`（均受版本门禁）；health 暴露 `server.api_version`、`accepted_client_versions`、`schema.degraded` 等。
- **会话 / 项目 / 状态**：`GET /slimapi/sessions`、`GET /slimapi/projects`、`GET /slimapi/sessions/status`、`GET /slimapi/sessions/{sid}/status`（骨架裁剪 + directory allowlist）。
- **消息（扁平路径，契约 §2）**：
  - `GET /slimapi/messages/{sid}` — 骨架分页（`?limit`/`before`/`mode=skeleton|full`）。
  - `GET /slimapi/messages/{sid}/since/{ts}` — **A2=A**：返回 `info.time.updated >= ts` 的骨架（含边界）；`?limit`（默认 50，上限 200）+ `?before`；多页扫描共用单 transform admission + 累计字节预算；超限 → `413 response_too_large`。
  - `GET /slimapi/messages/{sid}/full/{mid}` — 单条按需展开（默认 `mode=full`）。
- **分页游标**：`X-Next-Cursor` = opencode 响应 **`Link: rel="next"`** 中 `before=` 的 **opaque 字符串原样透传**（不 decode/re-encode）。客户端翻页：`?before=<X-Next-Cursor>`。opencode cursor 为 base64url；含 percent-encoding 的非规范 cursor 经 FastAPI/httpx 会规范化（见契约实现边界）。
- **SSE 策展**：`GET /slimapi/events` — 单上游 `/global/event`；吐 `session.digest`（debounce）+ question/permission 直推 + `server.connected`/`heartbeat`/`resync`；丢弃 text.delta / part.* / tool.*。
- **digest `archived`**：`session.updated` 的 `info.time.archived` → digest 字段 **`archived` = epoch ms int**（粘滞；无值则不输出该键）。客户端据此本地隐藏 ses。
- **T3 资源限制**：订阅上限（per-directory / total）、每 subscriber buffer 字节预算与单帧上限、溢出立即清 + `resync{reason:subscriber_backpressure}` + STOP；超限建立订阅 → `503 sse_subscriber_limit_*` + `Retry-After`。
- **指标**：`GET /slimapi/metrics`（订阅者 / hub / transform 摘要）。
- **q/p 聚合与写**：`GET /slimapi/questions`、`GET /slimapi/permissions`；`POST .../reply|reject`、`POST /slimapi/sessions/{sid}/permissions/{pid}`（routeToken）。
- **gzip §9**：JSON 路由按 `Accept-Encoding` 协商 gzip（含错误体 `error_response` 可选协商）；SSE **永不** gzip。
- **catch-all**：非 `/slimapi/**` 流式反代 opencode（写路径客户端自带 `X-Opencode-Directory`）。

### Changed

- （相对早期原型）消息路径由嵌套 `/slimapi/sessions/{sid}/messages/...` **改为** 契约扁平路径（见上）。
- （相对早期原型）`/since` 由 anchor/messageID 探测改为 **`/since/{ts}` 时间戳锚点**；不再使用 `X-Sync-Snapshot-Latest` / `X-Anchor-Found` / `409 resync_required`（锚点语义）。
- skeleton 模式下 **不再** 把上游 `Link` 头原样复制给客户端；改为解析后下发 `X-Next-Cursor`。

### Removed

- **`GET .../latest-message-id`**：契约 §2 未纳入；客户端未使用，已删除。冷启动 / resync 用 sessions + q/p + `/since/{ts}` + SSE digest，不再需要单独 ID 探针。

### Fixed

- SSE 慢消费者：queue/buffer 溢出改为**立即清**并下发 `resync` + STOP（不再尾部排 STOP 后继续灌旧帧）。
- 测试卫生：hub 订阅 teardown 避免 `Task was destroyed but it is pending`。

### Security

- sidecar **仅 loopback** 监听；公网认证依赖 stunnel mTLS（双入口 14096 直连 / 14097 经 sidecar）。
- routeToken：HMAC 签名、绑 kind+requestID+sessionID+directory、约 1h 过期；secret 经 `OC_SLIMAPI_ROUTE_SECRET_FILE` / systemd credential，**禁止**入库。

---

## 链接

- 契约：[`docs/v1-contract.md`](docs/v1-contract.md)
- 发版：[`docs/release.md`](docs/release.md)
- 客户端清单：[`docs/CLIENT_CHANGES.md`](docs/CLIENT_CHANGES.md)
