# oc-slimapi 运维手册

> 部署、服务管理、日志策略、排障。  
> 面向 **oc-slimapi 操作者**（运行 sidecar 的人）与 **ocdroid 项目组**（理解客户端如何接入）。  
> Wire 契约见 [`v1-contract.md`](v1-contract.md)；发版见 [`release.md`](release.md)。

---

## 1. 部署拓扑

```text
ocdroid (Android)
   │
   ├──(stunnel mTLS :14097)──▶ oc-slimapi :4097 (loopback)  ──HTTP──▶ opencode :4096
   │                             Python FastAPI 省流 sidecar            legacy /session/**
   │
   └──(stunnel mTLS :14096)──▶ opencode :4096   # 直连回退，不经 sidecar
```

- **oc-slimapi 仅监听 loopback**（`127.0.0.1:4097`），公网暴露由 stunnel mTLS 负责。
- upstream 固定 `http://127.0.0.1:4096`（opencode legacy HTTP API）。
- **单进程单 worker**：多 worker 会为同一 directory 重复建立 upstream SSE，禁止。

### 1.1 运行依赖

| 依赖 | 说明 |
|---|---|
| Python ≥ 3.13（venv 推荐，Debian/Ubuntu PEP 668 不要系统强装） | 当前实测 3.14.4 |
| opencode :4096 已启动 | 启动时 smoke 探针会打 `/session?limit=1` + `/session/{sid}/message?limit=1` |
| systemd user instance + `Linger=yes` | 保证登出后服务存活；本机已开 |

---

## 2. 安装

```bash
cd /home/mar/personal_projects/oc-slimapi
python -m venv .venv
.venv/bin/pip install -e './sidecar[test]'
```

升级代码后（`git pull`）重装一次即可：`.venv/bin/pip install -e './sidecar[test]'`。

---

## 3. systemd user 服务（生产部署）

### 3.1 route secret

secret 持久化到用户家目录（**不入仓**）：

```bash
mkdir -p ~/.config/oc-slimapi
openssl rand -base64 48 > ~/.config/oc-slimapi/route-secret
chmod 600 ~/.config/oc-slimapi/route-secret
```

约束：文件至少 32 字节（`config.py` 校验）。轮换时覆盖该文件 → `systemctl --user restart oc-slimapi`。

### 3.2 service 单元

路径：`~/.config/systemd/user/oc-slimapi.service`

```ini
[Unit]
Description=oc-slimapi (Python 省流 sidecar for ocdroid ↔ opencode)
Documentation=file:///home/mar/personal_projects/oc-slimapi/AGENTS.md
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/mar/personal_projects/oc-slimapi
ExecStart=/home/mar/personal_projects/oc-slimapi/.venv/bin/python -m oc_slimapi.app
Restart=on-failure
RestartSec=5

# route secret 经 systemd LoadCredential 注入；config.py 自动从
# CREDENTIALS_DIRECTORY 读取（无需再设 OC_SLIMAPI_ROUTE_SECRET_FILE）。
LoadCredential=route-secret:%h/.config/oc-slimapi/route-secret

Environment=OC_SLIMAPI_HOST=127.0.0.1
Environment=OC_SLIMAPI_PORT=4097
Environment=OC_SLIMAPI_UPSTREAM=http://127.0.0.1:4096
Environment=PYTHONUNBUFFERED=1

StandardOutput=journal
StandardError=journal
SyslogIdentifier=oc-slimapi

[Install]
WantedBy=default.target
```

调参（订阅上限、buffer 字节预算、transform 并发等）只需在 `[Service]` 加 `Environment=OC_SLIMAPI_*` 行，参见 [`sidecar/README.md`](../sidecar/README.md) §配置。

### 3.3 启用与开机自启

```bash
systemctl --user daemon-reload
systemctl --user enable --now oc-slimapi
```

确认 `Linger=yes`（保证用户登出后 user instance 不被杀）：

```bash
loginctl show-user "$USER" | grep Linger   # 期望 Linger=yes
# 若没开：
sudo loginctl enable-linger "$USER"
```

---

## 4. 服务管理命令

| 操作 | 命令 |
|---|---|
| 启动 | `systemctl --user start oc-slimapi` |
| 停止 | `systemctl --user stop oc-slimapi` |
| 重启（代码/配置变更后） | `systemctl --user restart oc-slimapi` |
| 状态 | `systemctl --user status oc-slimapi` |
| 开机自启 | `systemctl --user enable oc-slimapi` |
| 关闭自启 | `systemctl --user disable oc-slimapi` |
| 改了 unit 文件后 | `systemctl --user daemon-reload` 然后 `restart` |

代码升级流程：

```bash
cd /home/mar/personal_projects/oc-slimapi
git pull
.venv/bin/pip install -e './sidecar[test]'
systemctl --user restart oc-slimapi
```

---

## 5. 日志策略

### 5.1 决定：日志走 journald，**不**落项目内文件

oc-slimapi 应用本身**不配置任何 logging handler**——uvicorn 把 access log / app log 打到 stdout/stderr，systemd 接进 journald。这是有意的决定，理由：

| 维度 | journald 现状 |
|---|---|
| 持久化 | ✅ 落盘，`Linger=yes` 保证登出后存活 |
| 按级别 | ✅ `journalctl -p warning` |
| 按时间窗 | ✅ `--since "1 hour ago"` |
| tail -f | ✅ `journalctl -f` |
| 轮转/保留 | ✅ systemd 自动（按 size，`/etc/systemd/journald.conf` 可调） |
| grep | ✅ `journalctl ... \| rg <pattern>` |

**反模式（已拒绝，勿再提）**：
- `log-{level}-{date}-{hh}.log` 按级别分文件 → 一个请求的 INFO/WARN/ERROR 被撕进 3 个文件，重建时序极痛；每小时 5 文件 × 24 = 120 文件/天。
- "启动时删 24h 前日志" → 保留期与重启频率耦合，本机长期不重启就堆积、频繁重启就丢历史。保留应时间触发，不是事件触发。

若未来确有 journald 满足不了的需求（远程收集、结构化 JSON 给 ELK/Loki），再走 `TimedRotatingFileHandler(when="midnight", backupCount=7)` 单文件方案，**不要**回到上面的分文件方案。

### 5.2 查询手册

```bash
# 实时跟踪（最常用）
journalctl --user -u oc-slimapi -f

# 最近 100 行
journalctl --user -u oc-slimapi -n 100 --no-pager

# 今天
journalctl --user -u oc-slimapi --since today

# 最近 1 小时
journalctl --user -u oc-slimapi --since "1 hour ago"

# 只要 warning 及以上
journalctl --user -u oc-slimapi -p warning

# 搜关键词（cursor / SSE / backpressure / 413 ...）
journalctl --user -u oc-slimapi --since today | rg cursor

# 导出一批给外部看（替代文件日志的场景）
journalctl --user -u oc-slimapi --since today > oc-slimapi-today.log
```

### 5.3 启动期日志样本（健康态）

```
systemd[...]: Started oc-slimapi.service ...
oc-slimapi[...]: INFO: Started server process [PID]
oc-slimapi[...]: INFO: Waiting for application startup.
oc-slimapi[...]: INFO: Application startup complete.
oc-slimapi[...]: INFO: Uvicorn running on http://127.0.0.1:4097 (Press CTRL+C to quit)
```

启动失败常见原因：route secret 缺失/不足 32 字节、upstream 不可达、`OC_SLIMAPI_HOST` 非 loopback、`OC_SLIMAPI_UPSTREAM` 非 loopback HTTP。

---

## 6. 健康自检

### 6.1 服务级

```bash
systemctl --user is-active oc-slimapi    # active
systemctl --user is-enabled oc-slimapi   # enabled
```

### 6.2 应用级

```bash
# 必须带版本头
curl -s -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health | jq .
```

期望响应：

```json
{
  "sidecar": { "ok": true, "version": "0.1.0" },
  "server":  { "api_version": 1, "accepted_client_versions": [1, 1] },
  "schema":  { "degraded": false }
}
```

- `sidecar.version` = `sidecar/pyproject.toml` 的版本。
- `schema.degraded=true` → 启动 smoke 探针发现 opencode 响应字段漂移，需查上游是否升级/改了 schema。
- 不带版本头 → `400`（版本门禁生效，符合契约）。

### 6.3 ready 检查（轻量）

```bash
curl -s -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/ready
```

---

## 7. ocdroid 项目组须知（接入侧）

ocdroid 客户端**不直接操作** sidecar 进程，只通过 stunnel mTLS 接它。需要知道的：

| 项 | 值 |
|---|---|
| 经 sidecar 入口 | stunnel `:14097` → sidecar `127.0.0.1:4097` |
| 直连回退（不经 sidecar） | stunnel `:14096` → opencode `127.0.0.1:4096` |
| 所有 `/slimapi/**` 请求必带头 | `X-Slimapi-Version: 1`（缺/非整数 → `400 version_required`；越界 → `400 version_incompatible`） |
| 非 `/slimapi/**` | 透明反代 opencode，**不带**版本头 |
| 健康自检（客户端侧） | `GET /slimapi/health` 读 `server.api_version` / `accepted_client_versions` 做运行时兼容判断 |
| Wire 行为变更来源 | 本仓 [`CHANGELOG.md`](../CHANGELOG.md)（路径/头/错误码以本仓 + [`v1-contract.md`](v1-contract.md) 为准） |
| 客户端配套改动清单 | [`CLIENT_CHANGES.md`](CLIENT_CHANGES.md) |

sidecar 进程的启停、日志、升级由 **服务端运维** 负责，ocdroid 侧无需介入；但理解拓扑有助于排障（例如 sidecar 重启时 SSE 会断、客户端应收 `resync` 重连）。

---

## 8. 排障速查

| 症状 | 先查 |
|---|---|
| 服务起不来 | `journalctl --user -u oc-slimapi -n 50`；多半是 secret / upstream / host 校验失败 |
| `schema.degraded=true` | opencode 升级了；查 `opencode-src/current/` 对照字段，或临时设 `OC_SLIMAPI_SMOKE_SESSION_ID` 跳过随机探针 |
| 客户端连上但 400 | 版本头缺失/越界；查 `accepted_client_versions` 与客户端发的 `X-Slimapi-Version` |
| SSE 卡顿/断 | `journalctl --user -u oc-slimapi \| rg 'backpressure\|resync\|503'`；查 `/slimapi/metrics` 的订阅者计数 |
| 升级后行为变化 | 先看 [`CHANGELOG.md`](../CHANGELOG.md) 对应版本节 |

---

## 9. 相关文档

| 文件 | 用途 |
|---|---|
| [`v1-contract.md`](v1-contract.md) | Wire 契约权威 |
| [`release.md`](release.md) | 发版流程 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | 接口行为变更记录 |
| [`../sidecar/README.md`](../sidecar/README.md) | 配置项速查 + 开发运行 |
| [`../AGENTS.md`](../AGENTS.md) | Agent 入口索引 |
