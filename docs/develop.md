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
| `OC_SLIMAPI_SERVER_API_VERSION` | `3` | 服务端当前整数 API 版本（v3-only 终态） |
| `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS` | `3,3` | 接受的客户端版本闭区间（v3-only） |
| `OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES` | `4096` | 骨架投影单字段 inline 字节上限（超阈值则降级为引用占位） |
| `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | `1` | 双向字节账本总开关；`0` 时 traffic 账本快照（嵌入 `/slimapi/metrics` 响应的 `traffic` 块）与 ledger 全 no-op |
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

所有 `/slimapi/**` 请求（包括 `/slimapi/events` SSE）必须带查询参数 `?v=3`（v3-only 终态）：缺 `v` / `v=2` / 不支持值 → 400 `unsupported_version supported=[3]`（SSE 在开流前拒）；`X-Slimapi-Version` 头已删除、出现不解读；词法非法的 `v` → 400 `invalid_version_selector`。非 `/slimapi/**` 路径已随 catch-all 关闭统一 404 `thin_route_not_found`（§8.2）。

## 测试 / 质量门禁

**本机 `./scripts/check.sh` 是质量门禁**，默认已含三项（每次改动必跑）：

1. `pytest tests/`
2. 路由↔INTERFACE_MAP 一致性 gate（`scripts/check_routes_doc.py`，防 `/slimapi` 路由与文档漂移）
3. `compileall src`（字节码编译检查）

```bash
./scripts/check.sh           # 默认：上述三项全跑
./scripts/check.sh --full    # 兼容别名，行为等价于默认
```

> **验证策略**：本项目**本机验证，不使用线上 CI**（用户决策 2026-08-09）。所有改动校验在本机通过 `./scripts/check.sh` 完成。

gzip 检查：

```bash
curl -sS --compressed -D- 'http://127.0.0.1:4097/slimapi/messages/SID?limit=40&v=3' -o /dev/null
curl -sS -H 'Accept-Encoding: identity' -D- 'http://127.0.0.1:4097/slimapi/messages/SID?limit=40&v=3' -o /dev/null
```
