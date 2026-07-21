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
    ├──(Tailscale 明文直连 :4097)──▶ 同上（推荐仅内网/Tailscale ACL 受限）
    │
    └──(stunnel mTLS :14096)──▶ opencode :4096   # 直连回退，不经 sidecar
```

- **两个 :4097 入口**（明文）：
  - **`127.0.0.1:4097`**（默认，loopback）：仅供本机 + stunnel mTLS（:14097）终结后转发的推荐路径。
  - **`0.0.0.0:4097`**（可选，明文直连入口）：允许通过 Tailscale 地址直接访问，**不强制 mTLS**。**安全模型**：远程暴露需依赖 Tailscale ACL / 主机防火墙隔离；该入口**无**应用层鉴权（thin routes 自身不验证客户端身份），版本门禁（`X-Slimapi-Version`）仍生效。
- **:14097 仍为推荐的 mTLS 入口**；明文直连仅为 Tailscale 内网/运维便利，不应在不可信网络暴露。
- upstream 固定 `http://127.0.0.1:4096`（opencode legacy HTTP API）——**无论 host 如何**，`config.validate()` 始终强制 upstream 必须是 fixed loopback HTTP（SSRF guard 不放松）。
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
.venv/bin/pip install -e '.[test]'
```

升级代码后（`git pull`）重装一次即可：`.venv/bin/pip install -e '.[test]'`。

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

Environment=OC_SLIMAPI_HOST=0.0.0.0   # 或 127.0.0.1（仅 loopback，更保守）
Environment=OC_SLIMAPI_PORT=4097
Environment=OC_SLIMAPI_UPSTREAM=http://127.0.0.1:4096
Environment=PYTHONUNBUFFERED=1

StandardOutput=journal
StandardError=journal
SyslogIdentifier=oc-slimapi

[Install]
WantedBy=default.target
```

调参（订阅上限、buffer 字节预算、transform 并发等）只需在 `[Service]` 加 `Environment=OC_SLIMAPI_*` 行，参见 [`develop.md`](develop.md) §配置。

### 3.3 Deployment revision 注入

Ops 可通过两种方式注入部署修订标识（如 git SHA / 版本号）：

1. **环境变量**：在 service 的 `[Service]` 区加 `Environment=OC_SLIMAPI_DEPLOYMENT_REVISION=<git-sha-or-release>`（如 `v0.3.1-beta`）。
2. **凭据文件**（类似 route secret）：创建文件 `~/.config/oc-slimapi/deployment-revision` 写入字符串（无换行），并在 service `[Service]` 加 `LoadCredential=deployment-revision:%h/.config/oc-slimapi/deployment-revision`；sidecar 自动读取 `CREDENTIALS_DIRECTORY` 下的同名文件。

该值出现在 `GET /slimapi/health` 响应 `server.deploymentRevision` 字段（仅当设置时出现；未设置则整字段省略）。参考 [`docs/v1-contract.md`](v1-contract.md) §4。

### 3.4 启用与开机自启

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
.venv/bin/pip install -e '.[test]'
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
oc-slimapi[...]: INFO: Uvicorn running on http://0.0.0.0:4097 (Press CTRL+C to quit)
```

> 若 `OC_SLIMAPI_HOST=127.0.0.1`，则日志显示 `http://127.0.0.1:4097`。

启动失败常见原因：route secret 缺失/不足 32 字节、upstream 不可达、`OC_SLIMAPI_HOST` 非 loopback 且非 `0.0.0.0`、`OC_SLIMAPI_UPSTREAM` 非 loopback HTTP。

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

- `sidecar.version` = `pyproject.toml` 的版本。
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
| 经 sidecar mTLS 入口（推荐） | stunnel `:14097` → sidecar `127.0.0.1:4097` |
| 经 sidecar 明文直连入口（Tailscale 等） | Tailscale 地址`:4097` → sidecar `0.0.0.0:4097`（依赖 Tailscale ACL / 防火墙；无 mTLS） |
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
|---|---|---|
| [`v1-contract.md`](v1-contract.md) | Wire 契约权威 |
| [`release.md`](release.md) | 发版流程 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | 接口行为变更记录 |
| [`develop.md`](develop.md) | 配置项速查 + 开发运行 |
| [`../AGENTS.md`](../AGENTS.md) | Agent 入口索引 |

---
## 10. G-ACL 部署姿态与边界验证（0.0.0.0:4097 + 14097 mTLS 隧道）

> **参照**：`docs/ocmar/reports/2026-07-21-g-acl-ops-evidence.md`（本日证据报告）  
> **部署姿态**：用户最终接受 `0.0.0.0:4097` 明文监听 + `:14097` mTLS 隧道（stunnel `requireCert=yes verifyChain=yes`，复用既有证书）作为 steady-state。以下为 ops 维护的边界验证 runbook。

### 10.1 稳态拓扑

```
ocdroid ──(stunnel mTLS 14097)──▶ oc-slimapi 0.0.0.0:4097 (plaintext, all interfaces)
                    │
                    ╰──(Tailscale 明文直连 :4097)──▶ oc-slimapi 0.0.0.0:4097 (plaintext; Tailscale ACL 受限)
```

- **`:4097`（sidecar 明文端口）**：绑定 `0.0.0.0`（所有接口），**用户接受的稳态**。直接 `:4097` 的明文访问**必须**被网络边界（主机防火墙 / Tailscale ACL）阻塞，外部客户端须经 `:14097` mTLS 隧道。
- **`:14097`（mTLS 入口）**：stunnel 终结后转发至 `127.0.0.1:4097`，公网唯一可达入口。任何未持有有效 CA 签名客户端证书的连接在 TLS 层即被拒绝。
- **安全边界关键**：`0.0.0.0` 本身不提供接入控制——**依赖**网络边缘（防火墙 / Tailscale ACL）阻断公共/LAN 对 `:4097` 的直接明文 TCP。这就是使 `0.0.0.0` 可接受的安全约束。
- **loopback-only（`127.0.0.1:4097`）** 属于更严格的替代姿态（代码支持 `config.validate()` 允许），但**不是**当前部署选择。

### 10.2 负向探针（边界验证）

> **目的**：证实 `:14097` 仅可通过 mTLS（cert enforced）可达，且公共/LAN 不可直接到达 `:4097` 明文。

```bash
# 从外网（非 Tailscale 节点）扫描 14097 — 预期 port open（stunnel 响应）
nmap -p 14097 opencode.vectory.cn

# 从外网扫描 4097（明文）— 预期 filtered/closed（防火墙/ACL 阻断）
nmap -p 4097 opencode.vectory.cn

# 从外网尝试 mTLS 连接，预期 TLS 握手失败（无有效客户端证书）
curl -v https://opencode.vectory.cn:14097/slimapi/health

# 从外网尝试明文连接 4097，预期拒绝/超时（边界阻断）
curl http://opencode.vectory.cn:4097/slimapi/health

# 本机 loopback 验证（明文可达——此路径应被边界阻止，但本机不受限）
curl -s -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health

# mTLS 回环（须带客户端证书；本机可用 stunnel 自签名测试）
curl -s --cert client-cert.pem --key client-key.pem \
  https://127.0.0.1:14097/slimapi/health
```

**负向探针结果写入**：`docs/ocmar/reports/2026-07-21-g-acl-ops-evidence.md` §3，由 ops 从外部 vantage 执行后填充。

### 10.3 正向验证（本机）

```bash
# sidecar 健康（本机 loopback 明文）
curl -s -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health

# mTLS 回环（须带客户端证书）
curl -s --cert client-cert.pem --key client-key.pem \
  https://127.0.0.1:14097/slimapi/health
```

### 10.4 Cert 复用说明

- **Server cert/key**：`/home/mar/.config/stunnel/certs/server-cert.pem` + `server-key.pem`，SAN=`opencode.vectory.cn`，已用于 `:14097` mTLS 入口。
- **CA cert**：`/home/mar/.config/stunnel/certs/ca-cert.pem`，用于签发客户端证书。
- **Client cert**：ocdroid 客户端持有 `client-cert.pem` + `client-key.pem`（CA 签发）。**本轮 patch 无需轮换**。

> **注意**：`docs/mtls-setup-guide.md` 不在此仓库；ops-maintained。证书更新流程由 ops 自行维护。
