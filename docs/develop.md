# oc-slimapi 开发指南

## 安装

从项目根目录：

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
```

由于 Debian/Ubuntu 的 PEP 668，推荐使用 venv，不要向系统 Python 强装依赖。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---:|---|
| `OC_SLIMAPI_HOST` | `127.0.0.1` | 允许 loopback（`127.0.0.1`/`::1`/`localhost`）或 `0.0.0.0`（明文直连入口，远程暴露需依赖 Tailscale ACL / 主机防火墙；14097 仍为推荐 mTLS 入口） |
| `OC_SLIMAPI_PORT` | `4097` | HTTP 监听端口 |
| `OC_SLIMAPI_UPSTREAM` | `http://127.0.0.1:4096` | 固定 loopback upstream（无论 host 如何，upstream 必须保持 loopback HTTP） |
| `OC_SLIMAPI_MAX_MESSAGE_BYTES` | `33554432` | 单消息上限 |
| `OC_SLIMAPI_SMOKE_SESSION_ID` | 无 | 启动字段漂移 smoke 的已知 sid |
| `OC_SLIMAPI_SERVER_API_VERSION` | `2` | 服务端当前整数 API 版本 |
| `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` | `2,2` | 接受的客户端版本闭区间 |
| `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES` | `4096` | 骨架投影单字段 inline 字节上限（超阈值则降级为引用占位） |
| `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | `1` | 双向字节账本总开关；`0` 时 `/slimapi/metrics.traffic` 与 ledger 全 no-op |
| `OC_SLIMAPI_ACCESS_LOG_DIR` | `logs` | access log 目录（按天文件 `access-YYYY-MM-DD.jsonl`）；生产 systemd 覆盖为 `%S/oc-slimapi/logs` |
| `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS` | `0` | prune 早于 N 天的 `access-YYYY-MM-DD.jsonl(.gz)`（**代码默认 `0`=不删**；生产 unit 配 `3`） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_ENABLED` | `1` | 内存账本周期快照开关（按天 `traffic-snapshot-YYYY-MM-DD.jsonl`） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` | `logs/traffic-snapshot.jsonl` | 快照文件名 stem；生产 systemd 覆盖为 `%S/oc-slimapi/logs/traffic-snapshot.jsonl` |

> 上表为**速查**（高频运维 knob）。**完整权威清单与默认值见 [`src/oc_slimapi/config.py`](../src/oc_slimapi/config.py) 的 `Settings` dataclass**（含 T3/SSE 上限、token-stream 预算、transform 池、deployment revision、client-id hash 等，共 35+ 项）。落盘日志/流量查询手册见 [`manual/traffic-accounting.md`](manual/traffic-accounting.md)。

> v2 已移除 routeToken/route_secret，无需 route secret。

## 运行

### 开发 / 手动

```bash
.venv/bin/python -m oc_slimapi.app
```

或：

```bash
.venv/bin/uvicorn oc_slimapi.app:app --host 127.0.0.1 --port 4097 --workers 1
```

必须单 worker；多 worker 会为同一 directory 重复建立 upstream SSE。

### 生产 / systemd

完整部署、service 单元、开机自启、日志策略见 **[`operations.md`](operations.md)**。

速查：

```bash
systemctl --user start oc-slimapi       # 启动
systemctl --user status oc-slimapi      # 状态
journalctl --user -u oc-slimapi -f      # 实时日志
```

> 应用日志走 journald；**access log（`logs/access-YYYY-MM-DD.jsonl`）与流量快照（`logs/traffic-snapshot-YYYY-MM-DD.jsonl`）落盘**到 `access_log_dir`（systemd 下为 `StateDirectory` `~/.local/state/oc-slimapi/logs`）。理由与查询手册见 `operations.md` §5。

所有 `/slimapi/**` 请求（包括 `/slimapi/events` SSE）必须带：

```http
X-Slimapi-Version: 2
```

缺头、非整数或区间外版本均返回 400；非 `/slimapi/**` 的透明反代不受门闩影响。

## 测试

```bash
.venv/bin/python -m pytest tests/
.venv/bin/python -m compileall -q src
```

gzip 检查：

```bash
curl -sS --compressed -H 'X-Slimapi-Version: 2' -D- 'http://127.0.0.1:4097/slimapi/messages/SID?limit=40' -o /dev/null
curl -sS -H 'X-Slimapi-Version: 2' -H 'Accept-Encoding: identity' -D- 'http://127.0.0.1:4097/slimapi/messages/SID?limit=40' -o /dev/null
```
