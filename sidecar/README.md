# sidecar

## 安装

从项目根目录：

```bash
python -m venv .venv
.venv/bin/pip install -e './sidecar[test]'
```

由于 Debian/Ubuntu 的 PEP 668，推荐使用 venv，不要向系统 Python 强装依赖。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---:|---|
| `OC_SLIMAPI_HOST` | `127.0.0.1` | 仅允许 loopback |
| `OC_SLIMAPI_PORT` | `4097` | HTTP 监听端口 |
| `OC_SLIMAPI_UPSTREAM` | `http://127.0.0.1:4096` | 固定 loopback upstream |
| `OC_SLIMAPI_MAX_JSON_BYTES` | `67108864` | skeleton 页面上限 |
| `OC_SLIMAPI_MAX_MESSAGE_BYTES` | `33554432` | 单消息上限 |
| `OC_SLIMAPI_ROUTE_SECRET` | 无 | 测试用持久 secret |
| `OC_SLIMAPI_ROUTE_SECRET_FILE` | systemd credential | 生产 secret 文件 |
| `OC_SLIMAPI_SMOKE_SESSION_ID` | 无 | 启动字段漂移 smoke 的已知 sid |
| `OC_SLIMAPI_SERVER_API_VERSION` | `1` | 服务端当前整数 API 版本 |
| `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` | `1,1` | 接受的客户端版本闭区间 |

## 运行

```bash
OC_SLIMAPI_ROUTE_SECRET_FILE=/secure/route-secret \
  .venv/bin/python -m oc_slimapi.app
```

或：

```bash
OC_SLIMAPI_ROUTE_SECRET_FILE=/secure/route-secret \
  .venv/bin/uvicorn oc_slimapi.app:app --host 127.0.0.1 --port 4097 --workers 1
```

必须单 worker；多 worker 会为同一 directory 重复建立 upstream SSE。

所有 `/slimapi/**` 请求（包括 `/slimapi/events` SSE）必须带：

```http
X-Slimapi-Version: 1
```

缺头、非整数或区间外版本均返回 400；非 `/slimapi/**` 的透明反代不受门闩影响。

## 测试

```bash
.venv/bin/python -m pytest sidecar/tests/
.venv/bin/python -m compileall -q sidecar/src
```

gzip 检查：

```bash
curl -sS --compressed -H 'X-Slimapi-Version: 1' -D- 'http://127.0.0.1:4097/slimapi/messages/SID?limit=40' -o /dev/null
curl -sS -H 'X-Slimapi-Version: 1' -H 'Accept-Encoding: identity' -D- 'http://127.0.0.1:4097/slimapi/messages/SID?limit=40' -o /dev/null
```
