# oc-slimapi

`oc-slimapi` 是 **ocdroid**（Android 客户端）与 **opencode**（上游 server）之间的 **Python 省流 sidecar**。它只通过 HTTP 调 opencode legacy API（不读 SQLite）；对历史消息生成 skeleton 投影，向手机提供策展 SSE + 消息骨架，并提供流量记账、token stream、资源限制等 T3 能力。

权威 wire 契约为 [`docs/specs/v2-contract.md`](docs/specs/v2-contract.md)（`v1-contract.md` 已废弃移除）。Agent / 开发入口索引见 [`AGENTS.md`](AGENTS.md)。

## 拓扑

```text
                              ┌─ :14096 mTLS ─▶ opencode :4096（直连回退）
ocdroid ── 公网纯 TCP ──▶ stunnel
                              └─ :14097 mTLS ─▶ oc-slimapi :4097
                                                     │ loopback HTTP
                                                     └────────▶ opencode :4096
```

sidecar 只监听 loopback，upstream 为固定 loopback HTTP。公网认证靠 stunnel mTLS；双入口确保 sidecar 故障时客户端可真正回退直连。

## 快速开始

```bash
cd /home/mar/personal_projects/oc-slimapi
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m oc_slimapi.app
```

另一终端验证：

```bash
curl --fail -H 'X-Slimapi-Version: 2' http://127.0.0.1:4097/slimapi/health
.venv/bin/python -m pytest tests/
```

生产部署走 systemd **user** service（`deploy/oc-slimapi.service` 部署到 `~/.config/systemd/user/`），日志落 `StateDirectory`，详见 [`docs/operations.md`](docs/operations.md) §3。

## 范围（v2 / wire `X-Slimapi-Version: 2`）

- `/slimapi/**`：sessions / sessions/status / messages（list·full skeleton 投影）/ questions（跨目录聚合）/ command / agent（catalog skeleton）/ events（策展 SSE）/ token-stream / metrics / health。每个请求（含 SSE）必须带整数头 `X-Slimapi-Version: 2`；缺失、非整数或不在服务端接受区间返回 400。版本只在破坏性变更时递增，加性变更保持兼容。
- 其他 HTTP path：流式反代至 `127.0.0.1:4096`（catch-all）。
- WebSocket：501 语义，不支持 PTY；需要时在前方部署专用 HTTP/WS proxy。
- REST 精简 JSON：按 `Accept-Encoding` 自 gzip 并返回 `Vary: Accept-Encoding`。
- SSE：控制面 `/slimapi/events` 永不 gzip；token stream `/slimapi/sessions/{sid}/stream` **默认 gzip**（lever2，首个 SSE gzip 例外，按 `Accept-Encoding` 协商）。

> v2 已移除 routeToken/route_secret，以及 projects / permissions / since / session children 等端点；`questions`（跨目录聚合）与 `sessions/status` 已于 1.1.x 加性回归（详见 [`CHANGELOG.md`](CHANGELOG.md)）。

## 流量记账与日志

- **内存账本**：`GET /slimapi/metrics` 暴露按路由桶的双向字节账本（`upIn` = 从 opencode 拉的成本，`downOut` = 下发的省流后字节），含 SSE 真实上游成本。
- **access log**（逐请求，按天切分 `access-YYYY-MM-DD.jsonl`）+ **内存账本周期快照**（`traffic-snapshot-YYYY-MM-DD.jsonl`，cumulative，含 SSE 真实成本，进程重启前的唯一回溯来源）：生产落 systemd `StateDirectory`（`~/.local/state/oc-slimapi/logs/`），启动压缩历史、后台周期 maintenance、`RETAIN_DAYS=3` 自动清理。可选客户端标识头 `X-Client-Name`/`X-Client-Version`/`X-Client-Id`（设备 id 默认 hash，不透传上游）。
- **应用日志**（startup banner / warning / smoke）：走 journald（`journalctl --user -u oc-slimapi`）。
- 查询与口径手册：[`docs/manual/traffic-accounting.md`](docs/manual/traffic-accounting.md)。

## 文档

| 文件 | 用途 |
|---|---|
| [`docs/specs/v2-contract.md`](docs/specs/v2-contract.md) | Wire 契约权威 |
| [`docs/specs/design-v2.md`](docs/specs/design-v2.md) | 当前态设计 |
| [`docs/specs/INTERFACE_MAP.md`](docs/specs/INTERFACE_MAP.md) | 端点级实现追踪 |
| [`docs/specs/CLIENT_CHANGES.md`](docs/specs/CLIENT_CHANGES.md) | ocdroid 侧配套改动清单 |
| [`docs/operations.md`](docs/operations.md) | 部署 / 运维 / 日志 |
| [`docs/manual/traffic-accounting.md`](docs/manual/traffic-accounting.md) | 流量/省流查询手册 |
| [`docs/release.md`](docs/release.md) | 发版流程规范 |
| [`CHANGELOG.md`](CHANGELOG.md) | 接口行为变更记录 |
