# oc-slimapi

`oc-slimapi` 是面向 ocdroid 的 opencode 省流中间层。它只通过 HTTP 调用
opencode legacy API，不读取 SQLite；对历史消息生成保持
`List<MessageWithParts>` 形状的 skeleton，并向手机提供 curated SSE。

权威契约为 [`docs/v1-contract.md`](docs/v1-contract.md)。

## 拓扑

```text
                              ┌─ :14096 mTLS ─▶ opencode :4096（直连回退）
ocdroid ── 公网纯 TCP ──▶ stunnel
                              └─ :14097 mTLS ─▶ oc-slimapi :4097
                                                     │ loopback HTTP
                                                     └────────▶ opencode :4096
```

sidecar 只允许监听 loopback，upstream 也必须是固定 loopback HTTP。公网认证沿用
stunnel mTLS。双入口确保 sidecar 故障时客户端可真正回退直连。

## 快速开始

```bash
cd /home/mar/personal_projects/oc-slimapi
python -m venv .venv
.venv/bin/pip install -e './sidecar[test]'
openssl rand -base64 48 > /tmp/oc-slimapi-route-secret
chmod 600 /tmp/oc-slimapi-route-secret
OC_SLIMAPI_ROUTE_SECRET_FILE=/tmp/oc-slimapi-route-secret \
  .venv/bin/python -m oc_slimapi.app
```

另一个终端：

```bash
curl --fail -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health
.venv/bin/python -m pytest sidecar/tests/
```

生产环境不要使用 `/tmp` secret；使用 `deploy/oc-slimapi.service` 的 systemd
`LoadCredential`。secret 必须持久且至少 32 bytes，服务不会随机生成。

## 范围

- `/slimapi/*`：sessions/projects/messages/questions/permissions/status/health/curated SSE。
- 每个 `/slimapi/**` 请求（包含 SSE）必须带整数头 `X-Slimapi-Version: 1`；缺失、
  非整数或不在服务端接受区间会返回 400。版本只在破坏性变更时递增，加性变更保持兼容。
- 其他 HTTP path：流式反代至 `127.0.0.1:4096`。
- WebSocket：501 语义，不支持 PTY；需要时在前方部署专用 HTTP/WS proxy。
- REST 精简 JSON：按 `Accept-Encoding` 自 gzip并返回 `Vary: Accept-Encoding`。
- SSE：永不 gzip。

版本探测示例（health 本身也必须带版本头）：

```bash
curl -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health
```

设计与接口见 [`docs/design-v2.md`](docs/design-v2.md) 和
[`docs/INTERFACE_MAP.md`](docs/INTERFACE_MAP.md)；客户端配套见
[`docs/CLIENT_CHANGES.md`](docs/CLIENT_CHANGES.md)。
