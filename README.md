# oc-slimapi

`oc-slimapi` 是 **ocdroid**（Android 客户端）与 **opencode**（上游 server）之间的 **Python 省流 sidecar**。它通过 HTTP 调 opencode legacy API，并经只读投影源（SQLite `mode=ro`，4.0.0 起）为 v4 会话全局面提供 DB 投影数据（**绝无写入**）；对历史消息生成 skeleton 投影，向手机提供策展 SSE + 消息骨架，并提供流量记账、token stream、资源限制等 T3 能力。

权威 wire 契约为 [`docs/specs/v3-contract.md`](docs/specs/v3-contract.md)（v3 基准）与 [`docs/specs/v4-contract.md`](docs/specs/v4-contract.md)（4.0.0 起 (3,4) 双版本窗口的 v4 面）；[`docs/specs/v2-contract.md`](docs/specs/v2-contract.md) 为 ≤2.x 历史契约存档（`v1-contract.md` 已废弃移除）。Agent / 开发入口索引见 [`AGENTS.md`](AGENTS.md)。

## 拓扑

```text
ocdroid ── 公网纯 TCP ──▶ stunnel
                              ├─ :14097 mTLS ─▶ oc-slimapi :4097 ── loopback HTTP ──▶ opencode :4096（ocdroid 唯一入口）
                              └─ :14096 mTLS ───────────────────────────────────────▶ opencode :4096（目标态：ocdroid 完成 C1/C3 前置后仅服务匿名消费方）
```

sidecar 只监听 loopback，upstream 为固定 loopback HTTP。公网认证靠 stunnel mTLS。**目标态：ocdroid 仅经 `:14097`（slimapi）**——当前 ocdroid 生产流量已 100% 走 `:14097`（实证）；直连 `:14096` **保留至 ocdroid 完成 C1/C3 前置**（此前仍是 ocdroid 的回退路径），前置完成后仅服务匿名消费方（非 ocdroid 的其它 HTTP 客户端直连，sidecar 不干预）。退役说明见 [`docs/specs/CLIENT_CHANGES.md`](docs/specs/CLIENT_CHANGES.md)「直连退役」。

## 快速开始

```bash
cd /home/mar/personal_projects/oc-slimapi
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m oc_slimapi.app
```

另一终端验证：

```bash
curl --fail 'http://127.0.0.1:4097/slimapi/health?v=3'
.venv/bin/python -m pytest tests/
```

生产部署走 systemd **user** service（`deploy/oc-slimapi.service` 部署到 `~/.config/systemd/user/`），日志落 `StateDirectory`，详见 [`docs/operations.md`](docs/operations.md) §3。

## 范围（`?v=` selector；(3,4) 双版本窗口）

- `/slimapi/**`：sessions / sessions/status / messages（list·full skeleton 投影）/ questions（跨目录聚合）/ permissions（跨目录聚合）/ command / agent（catalog skeleton）/ events（策展 SSE）/ token-stream / metrics / health，以及 §10 收编的受控代理读/写路由（file/vcs/find/providers/session 单查等）。每个请求（含 SSE）必须带查询参数 `?v=3` 或 `?v=4`（4.0.0 起 (3,4) 双版本窗口；`GET /slimapi/versions` 无条件豁免）；缺失、`v=2` 或不支持值 → 400 `unsupported_version supported=[3,4]`。`X-Slimapi-Version` 请求头已删除（出现不解读）。版本只在破坏性变更时递增，加性变更保持兼容（见 [`docs/release.md`](docs/release.md)）。
- 其他 HTTP path：catch-all 反代**已于 3.0.0 关闭**——未收编路径直接 404 `thin_route_not_found`（不再转发上游）。
- WebSocket：501 语义，不支持 PTY；需要时在前方部署专用 HTTP/WS proxy。
- REST 精简 JSON：按 `Accept-Encoding` 自 gzip 并返回 `Vary: Accept-Encoding`。
- SSE：控制面 `/slimapi/events` 与 token stream `/slimapi/sessions/{sid}/stream` 均恒 identity、不 gzip（lever2 压缩路径已随 v3 终态移除）。

> v2→v3 已移除 routeToken/route_secret、projects / since / session children 等端点及 catch-all 透传；`questions`（跨目录聚合）、`sessions/status`、`permissions`（跨目录聚合）、children/todo/diff 等已加性回归或收编（详见 [`CHANGELOG.md`](CHANGELOG.md)）。

## 流量记账与日志

- **内存账本**：`GET /slimapi/metrics` 暴露按路由桶的双向字节账本（`upIn` = 从 opencode 拉的成本，`downOut` = 下发的省流后字节），含 SSE 真实上游成本。
- **access log**（逐请求，按天切分 `access-YYYY-MM-DD.jsonl`）+ **内存账本周期快照**（`traffic-snapshot-YYYY-MM-DD.jsonl`，cumulative，含 SSE 真实成本，进程重启前的唯一回溯来源）：生产落 systemd `StateDirectory`（`~/.local/state/oc-slimapi/logs/`），启动压缩历史、后台周期 maintenance、`RETAIN_DAYS=3` 自动清理。可选客户端标识头 `X-Client-Name`/`X-Client-Version`/`X-Client-Id`（设备 id 默认 hash，不透传上游）。
- **应用日志**（startup banner / warning / smoke）：走 journald（`journalctl --user -u oc-slimapi`）。
- 查询与口径手册：[`docs/manual/traffic-accounting.md`](docs/manual/traffic-accounting.md)。

## 文档

| 文件 | 用途 |
|---|---|
| [`docs/specs/v3-contract.md`](docs/specs/v3-contract.md) | Wire 契约权威（v3 基准） |
| [`docs/specs/v4-contract.md`](docs/specs/v4-contract.md) | v4 wire 契约（4.0.0 实施基线 + 修订冻结） |
| [`docs/specs/v2-contract.md`](docs/specs/v2-contract.md) | ≤2.x 历史契约存档 |
| [`docs/specs/design-v2.md`](docs/specs/design-v2.md) | v2 时代设计（历史；现行态以 v3/v4 契约为准） |
| [`docs/specs/INTERFACE_MAP.md`](docs/specs/INTERFACE_MAP.md) | 端点级实现追踪 |
| [`docs/specs/CLIENT_CHANGES.md`](docs/specs/CLIENT_CHANGES.md) | ocdroid 侧配套改动清单 |
| [`docs/operations.md`](docs/operations.md) | 部署 / 运维 / 日志 |
| [`docs/develop.md`](docs/develop.md) | 开发 / 运行 / 测试备忘 |
| [`docs/manual/traffic-accounting.md`](docs/manual/traffic-accounting.md) | 流量/省流查询手册 |
| [`docs/release.md`](docs/release.md) | 发版流程规范 |
| [`CHANGELOG.md`](CHANGELOG.md) | 接口行为变更记录 |
