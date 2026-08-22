# oc-slimapi 运维手册

> 部署、服务管理、日志策略、排障。  
> 面向 **oc-slimapi 操作者**（运行 sidecar 的人）与 **ocdroid 项目组**（理解客户端如何接入）。  
> Wire 契约见 [`v3-contract.md`](specs/v3-contract.md)（v2 已于 3.0.0 退役，历史见 [`v2-contract.md`](specs/v2-contract.md)）；发版见 [`release.md`](release.md)。

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
  - **`0.0.0.0:4097`**（opt-in，默认关闭；2026-08-21 起默认回环）：显式开启后允许通过 Tailscale 地址直接访问，**不强制 mTLS**。**安全模型**：远程暴露需依赖 Tailscale ACL / 主机防火墙隔离；该入口**无**应用层鉴权（thin routes 自身不验证客户端身份），版本选择器（`?v=4` 终态门禁）仍生效。
- **:14097 仍为推荐的 mTLS 入口**；明文直连仅为 Tailscale 内网/运维便利，不应在不可信网络暴露。
- upstream 固定 `http://127.0.0.1:4096`（opencode legacy HTTP API）——**无论 host 如何**，`config.validate()` 始终强制 upstream 必须是 fixed loopback HTTP（SSRF guard 不放松）。
- **单进程单 worker**：多 worker 会为同一 directory 重复建立 upstream SSE，禁止。

### 1.1 运行依赖

| 依赖 | 说明 |
|---|---|
| Python ≥ 3.11.5（venv 推荐，Debian/Ubuntu PEP 668 不要系统强装） | 当前实测 3.14.4 |
| opencode :4096 已启动 | 启动时 smoke 探针会打 `/session?limit=1` + `/session/{sid}/message?limit=1` |
| systemd user instance + `Linger=yes` | 保证登出后服务存活；本机已开 |

---

## 2. 安装

```bash
cd /home/mar/personal_projects/oc-slimapi
python -m venv .venv
.venv/bin/pip install -e '.[test]'
```

升级代码后（`git pull`）**必须**重装一次 editable 包，再重启服务：

```bash
.venv/bin/pip install -e '.[test]'
systemctl --user restart oc-slimapi
```

**为何必须 reinstall**：`oc_slimapi.__version__` 来自已安装包的 dist-info（`importlib.metadata.version("oc-slimapi")`，见 `src/oc_slimapi/__init__.py`），由 `pyproject.toml` 在 **install 时**写入。`release.sh` / `git pull` 只改工作树与 `pyproject.toml`，**不会**自动刷新 dist-info。若跳过 reinstall 只 restart，进程会跑新代码，但 `/slimapi/health` 的 `sidecar.version` 仍可能报旧版本（v0.4.0 发版时踩过：代码已是 0.4.0，health 仍报 0.3.1，直到 `pip install -e .` + restart）。

验证：

```bash
curl -s 'http://127.0.0.1:4097/slimapi/health?v=4'
# 期望 sidecar.version 与 pyproject.toml / 刚发的 tag 一致
```

---

## 3. systemd user 服务（生产部署）

### 3.1 route secret

> v2 已移除 routeToken/route_secret，无需 route secret。

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
# F-010/F-214: SIGTERM 最坏关停链 = uvicorn 连接排水 5s（_GRACEFUL_SHUTDOWN_TIMEOUT）
# + lifespan LIFO 清理：维护排水 30s（_MAINT_DRAIN_TIMEOUT）+ transform 池排水 10s
# （_TRANSFORM_DRAIN_TIMEOUT）+ dbaux 排水 5s（_DBAUX_DRAIN_TIMEOUT）+ 中间回调秒级
# ≈ 50s；60s 覆盖全链并留 ~10s 余量。改动 app.py 排水常量时须同步此值
# （审计 docs/audits/2026-08-20/02-findings/F-010.md / F-214.md）。
TimeoutStopSec=60

Environment=OC_SLIMAPI_HOST=127.0.0.1   # 默认回环；0.0.0.0 为 opt-in（须自担网络层隔离，见 §11）
Environment=OC_SLIMAPI_PORT=4097
Environment=OC_SLIMAPI_UPSTREAM=http://127.0.0.1:4096
Environment=OC_SLIMAPI_MAX_MESSAGE_BYTES=33554432
# OC_SLIMAPI_SERVER_API_VERSION / OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS 已弃用：版本窗自 4.0.0 起由代码钉死
# （4.8.0 起 (4,4) v4-only）fail-closed——ACCEPTED_CLIENT_VERSIONS 设非 (4,4) 启动即 RuntimeError 拒绝；
# SERVER_API_VERSION 设置仅产生启动 warning 并被忽略。deploy 模板已同款清理，版本 env 不可配置。
Environment=PYTHONUNBUFFERED=1

# v1.0.0: access log + traffic snapshot 落 StateDirectory（systemd 自动建
# ~/.local/state/oc-slimapi）。代码默认相对 logs/（本地开发 cwd 可写），
# 生产由下面几行 env 覆盖到 state dir。RETAIN_DAYS=3 自动清理早于 3 天的 access log。
# T9/P1-4: incarnation 状态文件分离到独立 %S/oc-slimapi（与 access logs 平级，
# 不再放进 logs/ 子目录）；详见 §5.2.1。
StateDirectory=oc-slimapi
Environment=OC_SLIMAPI_ACCESS_LOG_DIR=%S/oc-slimapi/logs
Environment=OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH=%S/oc-slimapi/logs/traffic-snapshot.jsonl
Environment=OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS=3
Environment=OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS=30
Environment=OC_SLIMAPI_STATE_DIR=%S/oc-slimapi

StandardOutput=journal
StandardError=journal
SyslogIdentifier=oc-slimapi

# Memory hard cap. max_transforms=1 + max_response_bytes=64MiB + 16MiB inline
# cap + Python/Baseline RSS fits under 384M（见 design-v2 §0.10、transform.py docstring）。
# cgroup-enforced OOM kill 保护宿主，防 runaway upstream body 绕过 read_with_cap。
# 与 deploy/oc-slimapi.service 一致（该文件为权威模板）。
MemoryMax=384M

[Install]
WantedBy=default.target
```

> 这是 **user service** 模板（`systemctl --user`）：**不含** `ProtectSystem`/`ProtectHome` 等 sandbox 指令——它们需要 root（capability drop），user manager 无权设置。进程隔离靠 stunnel mTLS（:14097/:14096）+ Tailscale ACL（见 §11）。仓库内 `deploy/oc-slimapi.service` 是同结构模板（含注释），**以它为权威模板**（本节示例若与之冲突以 deploy 为准）。

调参（订阅上限、buffer 字节预算、transform 并发等）只需在 `[Service]` 加 `Environment=OC_SLIMAPI_*` 行，参见 [`develop.md`](develop.md) §配置。

### 3.3 Deployment revision 注入

Ops 可通过两种方式注入部署修订标识（如 git SHA / 版本号）：

1. **环境变量**：在 service 的 `[Service]` 区加 `Environment=OC_SLIMAPI_DEPLOYMENT_REVISION=<git-sha-or-release>`（如 `v0.3.1-beta`）。
2. **凭据文件**（systemd `LoadCredential` 模式）：创建文件 `~/.config/oc-slimapi/deployment-revision` 写入字符串（无换行），并在 service `[Service]` 加 `LoadCredential=deployment-revision:%h/.config/oc-slimapi/deployment-revision`；sidecar 自动读取 `CREDENTIALS_DIRECTORY` 下的同名文件。

该值出现在 `GET /slimapi/health` 响应 `server.deploymentRevision` 字段（仅当设置时出现；未设置则整字段省略）。参考 [`docs/specs/v2-contract.md`](specs/v2-contract.md) §4。

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

> **shutdown 语义**：`systemctl --user stop` / `restart` 发出 SIGTERM 后，uvicorn 给活跃连接（含 SSE 订阅者 drain）最多 5 秒宽限（`timeout_graceful_shutdown`）自然结束；随后进程进入 lifespan 的 AsyncExitStack LIFO 清理链（access-log 维护排水 30s + transform 池排水 10s + dbaux 排水 5s + 中间回调秒级）。systemd 的 `TimeoutStopSec=60` 作为总上限覆盖整条最坏链（5+30+10+5+余量 ≈ 50s < 60s）。旧值 15s 只覆盖 uvicorn 一段，会让 SIGKILL 截断链尾的最终流量快照帧与 access-log flush（审计 F-010/F-214，`docs/audits/2026-08-20/02-findings/`）。若需调整，在 service unit `[Service]` 修改 `TimeoutStopSec=`（须 ≥ 全链最坏合计，且不低于 uvicorn 的 5s 宽限窗口），并同步 `deploy/oc-slimapi.service` 模板与本节算术。

代码升级 / 发版后部署流程（**三步缺一不可**）：

> **分支纪律（batch3 v4 分支，B6）**：本机部署**仅允许 main 分支 HEAD 且已发版 tag**——部署前先 `git branch --show-current`（必须 `main`）+ `git describe --tags --exact-match`（必须命中 tag）。`v4` 分支**拒绝部署**（merge gate 未通过；merge 回 main 并 `release.sh minor` 之后才可在 main 部署——owner 2026-08-21 裁定收窄发 minor）。`scripts/release.sh` 对 v4 分支已内置同样的非零退出防线。

```bash
cd /home/mar/personal_projects/oc-slimapi
git pull                                          # 1. 拉代码（含 pyproject version）
.venv/bin/pip install -e '.[test]'                # 2. 刷新 editable dist-info（否则 health.version 滞后）
systemctl --user restart oc-slimapi               # 3. 重启进程加载新代码 + 新 __version__
curl -s 'http://127.0.0.1:4097/slimapi/health?v=4'
# 确认 sidecar.ok=true 且 sidecar.version 与 tag 一致
```

发版侧完整步骤见 [`release.md`](release.md) §3.3（含 Gitea Release 与 reinstall 提醒）。

---

## 5. 日志策略

### 5.1 两类日志：journald（应用日志）+ 落盘文件（access log / traffic snapshot）

oc-slimapi 有两类日志输出，**分别处理**：

| 类别 | 落点 | 内容 | 持久化策略 |
|---|---|---|---|
| **应用日志** | journald（stdout/stderr） | uvicorn access / app INFO/WARN/ERROR、startup banner | systemd journald 自动轮转 |
| **access log**（结构化） | 落盘 JSONL | 每请求一行 `{ts,method,path,bucket,status,durationMs,bytes,requestId,client?,clientVer?,clientId?}`（`ts` 为请求**完成时刻**的时间戳——响应已发出的时间点，非请求开始时刻；`durationMs` 才是耗时） | **按天切分** `access-YYYY-MM-DD.jsonl`，启动压缩早于今天 → `.gz`，后台 retain |
| **traffic snapshot**（内存账本） | 落盘 JSONL | 周期（默认 300s）cumulative 字节账本（含 SSE 真实成本） | 按天切分 `traffic-snapshot-YYYY-MM-DD.jsonl`，shutdown 写终态；不经自动压缩；按 OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS 自动 prune |

**应用日志走 journald** 是有意的决定：一个请求的 INFO/WARN/ERROR 留在同一流里，重建时序方便。journald 提供持久化 / 按级别 / 按时间窗 / tail -f / 轮转 / grep，无需应用配置额外 handler。

**access log 与 traffic snapshot 落盘**（v0.7.0+ 引入 access log，2026-07-29 改为按天切分 + StateDirectory 可写）：用于离线省流分析与运维排障（"哪些请求未省流"按 `bucket==passthrough` 过滤、按 `clientId` 区分设备）。两者 best-effort、**不阻断服务启动**：access log handler 初始化失败 → disabled（纯 no-op）；snapshotter **首帧写入失败 → inactive**（不创建后台 task、不周期重试，需排查磁盘/路径后重启恢复）。

### 5.2 access log / snapshot 落盘目录（生产 vs 本地开发）

| 场景 | 目录 | 来源 |
|---|---|---|
| **systemd 生产** | `~/.local/state/oc-slimapi/logs/` | unit `StateDirectory=oc-slimapi`（systemd 自动建目录、管理生命周期）+ env `OC_SLIMAPI_ACCESS_LOG_DIR=%S/oc-slimapi/logs`、`OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH=%S/oc-slimapi/logs/traffic-snapshot.jsonl`（`%S` = `~/.local/state`） |
| **本地开发**（手动跑） | `<cwd>/logs/` | 代码默认（相对路径；cwd 可写） |

> **为何用 StateDirectory**：把 access log / snapshot 收拢到 XDG 标准的 state 目录（`~/.local/state/oc-slimapi/`），而非污染项目工作树（`<cwd>/logs/` 会落在 git-tracked 目录下）。systemd 自动创建目录并设置属主；`StateDirectory=oc-slimapi` 是 user service 的标准做法。代码默认仍为相对 `logs/`（本地开发 cwd 可写），生产由 unit env 覆盖。

### 5.2.1 incarnation 状态文件（与 access logs 分离，T9/P1-4）

> T9（P1-4）起，incarnation 状态文件与 access logs **分离**到独立目录。这是**运维行为变更**，不涉及 wire（历史注：该变更发生于 v2 时代、未 bump 版本头；3.0.0 起请求头通道删除，wire 版本由 `?v=` selector 唯一表达——4.8.0 起 (4,4) v4-only 单版本窗口（4.0.0–4.7.0 曾为 (3,4) 双版本）。

| 路径 | 来源 |
|---|---|
| **systemd 生产** | `%S/oc-slimapi/incarnation`（即 `~/.local/state/oc-slimapi/incarnation`），由 unit `Environment=OC_SLIMAPI_STATE_DIR=%S/oc-slimapi` 指定 |
| **本地开发**（手动跑） | `<cwd>/state/incarnation`（代码默认 `state`） |
| **旧位置（已弃用，仍可读）** | `<access_log_dir>/incarnation`（如 `%S/oc-slimapi/logs/incarnation`） |

- **新路径优先**：启动时先读 `OC_SLIMAPI_STATE_DIR` 下的 `incarnation` 文件。
- **单调迁移，不 reset**：当新路径文件**缺失或损坏**而旧 access-log 目录下的 `incarnation` 文件存在且有效时，sidecar 回退读取旧值（`legacy + 1`）并写入新路径——incarnation **不**归零、**不**回退。
- **旧文件保留不删**：迁移是非破坏性的 copy-on-upgrade，旧位置文件**永久保留**（不自动清理、不删除）。
- **首次升级流程**：升级到 T9+ 后首次启动，旧 access-log 目录下的 incarnation 文件被读取 → `+1` 后写入新 `OC_SLIMAPI_STATE_DIR/incarnation`；后续重启新路径即权威来源。
- **新路径损坏**：若新路径文件可读但内容损坏（非整数 / 负数 / 空），fallback 读取旧路径；若旧路径也无效则视为 fresh start（base=0 → inc=1，仅当两个文件都缺失/损坏时才发生）。
- **持久化失败**：写新路径失败（目录不可写 / 权限不足）→ 计算出的 inc 值在内存中继续生效（best-effort，不 crash lifespan）；重启会重新读盘可能拿到 stale 值，但 fence 的"进程内单调"保证仍成立。

代码实现见 `src/oc_slimapi/turn_registry.py::IncarnationStore`（`__init__(state_dir, legacy_state_dir=None)`）与 `src/oc_slimapi/app.py` lifespan（`legacy_state_dir=access_log_dir`）。

### 5.3 access log 维护（压缩 / retain / 后台 loop）

- **按天切分**：文件名 `access-YYYY-MM-DD.jsonl`（`YYYY-MM-DD` = 当天日期）。跨天自动切新文件。
- **启动压缩**：服务启动时将**早于今天的**（日期 `< today`）未压缩 `.jsonl` 原子压缩为 `.gz`（写 `.gz.tmp` → rename → 删源）。当天文件不压缩（活跃写入中）。
- **legacy 迁移（仅当前目录）**：`migrate_legacy_access_log` 只处理**当前 `access_log_dir` 内**的无日期文件（旧 `access.jsonl`/`access.jsonl.N`），按 mtime 归档为 `access-legacy-{mtimeYYYYMMDD}-{N}.jsonl.gz`。**不跨目录迁移**：从旧相对目录（如 cwd 下 `logs/`）升级到 `StateDirectory`（`~/.local/state/oc-slimapi/logs`）时，旧位置的历史日志**不会自动迁移**——运维需手动移动。
- **`access-legacy-*.jsonl.gz` 纳入 retain 自动清理**：prune 先严格匹配 `access-YYYY-MM-DD.jsonl(.gz)`，legacy 档案（`access-legacy-YYYYMMDD-*.jsonl.gz`）按**名内归档日期**纳入 `RETAIN_DAYS` 同一保留窗口自动清理（同一判据、同一边界）。
- **后台 maintenance loop**（默认 1h 周期，`OC_SLIMAPI_ACCESS_LOG_MAINTENANCE_INTERVAL_S`）：周期执行 compress + prune，不依赖重启。
- **prune**：`OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS`（**代码默认 `0` = 不删**；**生产 unit 配置 `3`**——见 `deploy/oc-slimapi.service` / §3.2），删除早于 N 天的 `access-YYYY-MM-DD.jsonl(.gz)`；`access-legacy-YYYYMMDD-*.jsonl.gz` 按名内归档日期同一判据清理。
- **snapshot 清理由 snapshotter 自持**：`traffic-snapshot-YYYY-MM-DD.jsonl` 不经 access log 的 compress（后者只认 `access-` 前缀，不自动压缩），也不经 access log 的 maintenance loop。**F-009 起，snapshotter 循环每 tick 顶部自持 prune**（`OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS`，**代码默认 `0` = 不删**；**生产 unit 配置 `30`**——见 `deploy/oc-slimapi.service` / §3.2），**不受 `ACCESS_LOG_ENABLED` 影响**：删除早于 N 天的 `traffic-snapshot-YYYY-MM-DD.jsonl(.gz)`（边界 `today - retain_days` 保留；删 `.jsonl` 与 `.jsonl.gz`）。prune 失败仅告警，不中断循环、不写入降级。

### 5.4 磁盘增长估算

- **access log**：单日 raw 视请求量约 **50–100 MB**（每行 ~200–400 B × 请求数）；压缩后约 **1/8**（~6–12 MB/天）。
- **traffic snapshot**：极小（每帧 ~1–2 KB × 每 300s = ~300 KB/天）；**不经自动压缩**，**Task 10 (P2-1) 起按 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS` 自动 prune**（代码默认 `0`=不删；生产 unit `30`）。30 天保留上限约 30 × 300 KB ≈ 9 MB。
- **建议**：生产 unit 已配 `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS=3`（保留 3 天 access log，约 3×6–12 MB ≈ 20–36 MB 压缩后）+ `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS=30`（保留 30 天 snapshot，约 9 MB）；如需更久的历史可调大此两值。`access-legacy-*.jsonl.gz` 按名内归档日期随同一 retain 窗口自动清理。journald 自身由 systemd 按 `/etc/systemd/journald.conf` 轮转，不在此列。

### 5.5 Fan-out 与内存预算（内部 knob，非 wire）

> 以下三组环境变量控制聚合类端点（`/slimapi/questions`、`/slimapi/permissions`）与 merged 列表（`/slimapi/messages/{sid}?mode=merged`）、`/full` transform 吸收的资源预算。它们是**内部 ops knob**（不改变 wire 契约），仅影响 sidecar 的内存/并发行为。

**Questions（`/slimapi/questions`）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_QUESTIONS_MAX_RESPONSE_BYTES` | 2 MiB | > 0 | 单个 `/question` 上游响应的读取上限（per-dir cap）。超过该上限时该目录进 `errors[]`（`upstream_unavailable`），不占用 aggregate 预算。 |
| `OC_SLIMAPI_QUESTIONS_MAX_AGGREGATE_BYTES` | 16 MiB | >= per_dir, <= 128 MiB | 跨目录聚合的累积字节预算。超过时 envelope 标记 `truncated: true`，取消后续未消费的目录。 |
| `OC_SLIMAPI_QUESTIONS_FANOUT_CONCURRENCY` | 8 | 1–16 | 跨请求全局 `/question` 并发上限。单次 `/slimapi/questions` 请求的 fan-out 不超过此值。 |

触发任一预算上限时，envelope 复用既有的加性字段 `truncated`（`true`）和 `authoritativeDirectories`（降级为已成功目录列表，非 null）（历史注：该行为加入时未 bump 版本头；3.0.0 起请求头通道删除，`?v=` selector 为唯一版本通道——4.8.0 起 (4,4) v4-only 单版本窗口（4.0.0–4.7.0 曾为 (3,4) 双版本）。详见 [`../CHANGELOG.md`](../CHANGELOG.md) Unreleased 与 [`docs/specs/INTERFACE_MAP.md`](specs/INTERFACE_MAP.md) questions 行。

**Permissions（`/slimapi/permissions`，2026-08-15 起；语义与 questions 同款）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_PERMISSIONS_MAX_RESPONSE_BYTES` | 2 MiB | > 0 | 单个 `/permission` 上游响应的读取上限（per-dir cap）。超过时该目录进 `errors[]`，不占用 aggregate 预算。 |
| `OC_SLIMAPI_PERMISSIONS_MAX_AGGREGATE_BYTES` | 16 MiB | >= per_dir, <= 128 MiB | 跨目录聚合的累积字节预算。超过时 envelope 标记 `truncated: true` 并停止后续目录。 |
| `OC_SLIMAPI_PERMISSIONS_FANOUT` | 8 | 1–16 | 全局 `/permission` 并发上限（专用 semaphore）。 |

**Merged 列表（`/slimapi/messages/{sid}?mode=merged`，2026-08-15 起）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_MERGED_MAX_FULLS_PER_PAGE` | 16 | 1–64 | 单页最多 fan-out 的 `/full` 数量上限；超出的消息保持 skeleton。 |
| `OC_SLIMAPI_MERGED_MAX_BYTES` | 8 MiB | > 0, <= 128 MiB | 单页内联全文的累计字节预算（预留/退款模型；读取层 chunk 粒度越界至多 `merged_fanout × chunk_size`）。超预算的项降级为 skeleton，不 500 整页。 |
| `OC_SLIMAPI_MERGED_FANOUT` | 8 | 1–16 | fan-out 并发上限（独立于 transform 池，不占用池槽）。 |

**/full transform 吸收（2026-08-15 起）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_TRANSFORM_ABSORB_BUDGET_SECONDS` | 2.5 | > 0 | `transform_busy` 503 前的服务端吸收预算（single-flight + 按剩余预算收窄的池等待重试）。503 形状不变，频率大幅下降。 |

**Catalog TTL 缓存与上游去重（2026-08-16 起）**

> 以下 8 个 knob 控制省流 sidecar 的上游流量优化与内容指纹（前者内部行为 wire 响应字节不变，后者为加性 wire 字段开关）：`/slimapi/agent`、`/slimapi/command` 的 catalog TTL 缓存，与列表类端点（messages 列表 / sessions 列表与 status / questions / permissions 的 discovery + per-dir 抓取）的 join-first single-flight 去重，以及 messages skeleton 的 `contentFingerprint` 字段。

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_CATALOG_CACHE_TTL_SECONDS` | 300 | >= 0 | catalog 响应缓存窗口（秒）。`0` 完全禁用（行为回到逐请求直取，access log 亦不产生 `cache` 字段）。仅缓存成功（200）且合法的 catalog body；4xx/5xx/坏 JSON 不缓存。 |
| `OC_SLIMAPI_CATALOG_CACHE_MAX_ENTRIES` | 16 | >= 1 | catalog 缓存最大条目数（按 `(kind, directory)` 分桶）。 |
| `OC_SLIMAPI_CATALOG_CACHE_MAX_BYTES` | 16 MiB | >= 1 MiB | catalog 缓存总字节预算；超限按最旧优先即时淘汰。 |
| `OC_SLIMAPI_CATALOG_CACHE_MAX_ENTRY_BYTES` | 1 MiB | <= MAX_BYTES | 单条缓存上限；超过该大小的响应旁路缓存直接透传（不入账）。 |
| `OC_SLIMAPI_COALESCE_ENABLED` | true | bool | 上游去重总开关。`false` = 完全旁路（行为与未上线去重时逐字节一致）。 |
| `OC_SLIMAPI_RAW_FETCH_CONCURRENCY` | 4 | >= 1 | 同时 in-flight 的去重上游 GET 数上限（纯网络并发限制，与内存预算解耦）。 |
| `OC_SLIMAPI_RAW_FETCH_MAX_BYTES` | 64 MiB | > 0，与 transform 预算之和 <= 576 MiB | 去重 flight 的字节预算：每个 distinct flight 预扣整笔 `OC_SLIMAPI_MAX_RESPONSE_BYTES`（读取完成后不返还差额——保守正确性优先）。 |
| `OC_SLIMAPI_MESSAGE_FINGERPRINT_ENABLED` | true | bool | messages skeleton 加性字段 `contentFingerprint`（`"<vN>:<sha256hex>"`）开关。`false` = 不输出该字段（逐字节等价开关关闭前行为）。**注意**：开关状态参与 ETag `REP_VERSION`——关闭/重开会轮换全部 ETag 验证器（客户端 304 全部 miss 一次，属预期）。 |

> **默认容量退化说明（重要）**：默认 `RAW_FETCH_MAX_BYTES=64 MiB` × `MAX_RESPONSE_BYTES=64 MiB` → **默认配置下同时只有 1 个去重 flight**；`RAW_FETCH_CONCURRENCY=4` 在默认预算下不可达（预算先到顶）。这是刻意的保守默认（内存证明优先）。**调优指引**：期望 N 个并行去重抓取时，设 `OC_SLIMAPI_RAW_FETCH_MAX_BYTES >= N × OC_SLIMAPI_MAX_RESPONSE_BYTES`；预算满时新 key 自动降级为现行直取路径（行为正确，只是不去重）。聚合内存校验：`RAW_FETCH_MAX_BYTES + MAX_TRANSFORMS × MAX_RESPONSE_BYTES <= 576 MiB`（两项预算并发峰值之和，超限启动失败）。

**SSE 控制面资源限制（`/slimapi/events`）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_MAX_SUBSCRIBERS_PER_DIRECTORY` | 8 | >= 1 | 单 directory 的 digest SSE 订阅上限；超出 → 400（订阅被拒，见 §9 排障行）。 |
| `OC_SLIMAPI_MAX_TOTAL_SUBSCRIBERS` | 16 | >= per-directory | 进程级 SSE 订阅总量上限（须 >= 单 directory 上限，否则启动失败）。 |
| `OC_SLIMAPI_SSE_QUEUE_ITEMS` | 256 | >= 2 | 每订阅者发送队列条目数；溢出触发背压 `resync{subscriber_backpressure}` + 断连。 |
| `OC_SLIMAPI_SSE_BUFFER_BYTES` | 2 MiB | > 0 | 每订阅者发送队列字节预算（与条目数双限，先到先溢）。 |
| `OC_SLIMAPI_SSE_MAX_FRAME_BYTES` | 256 KiB | > 0 | 单帧序列化上限；超限帧整帧丢弃（防单帧打爆队列）。 |

**Token stream 资源限制（`/slimapi/sessions/{sid}/stream`）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_TOKEN_STREAM_MAX_SUBSCRIBERS` | 8 | >= 1 | 单 sid 的 token stream 订阅上限（admission limit，超出拒绝）。 |
| `OC_SLIMAPI_TOKEN_STREAM_QUEUE_ITEMS` | 64 | >= 2 | 每订阅者队列条目数。 |
| `OC_SLIMAPI_TOKEN_STREAM_BUFFER_BYTES` | 512 KiB | > 0 | 每订阅者队列字节预算。 |
| `OC_SLIMAPI_TOKEN_STREAM_MAX_FRAME_BYTES` | 1 MiB | > 0 | 单帧上限（契约 §7 帧上限的实现旋钮）。 |

> `OC_SLIMAPI_TOKEN_STREAM_DEBUG_*`（`LIVE_BUDGET_BYTES`/`LIVE_PARTS_MAX`/`PART_MAX_BYTES`）为 **debug/联调-only** 覆盖，生产勿动（`config.py` 头注）。

**SSE 断线重放（replay buffer）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_REPLAY_COUNT` | 2048 | >= 1 | 重放缓冲最大帧数。 |
| `OC_SLIMAPI_REPLAY_BYTES_KB` | 65536 | >= 1 | 重放缓冲字节预算。**单位是 KiB**（64 MiB）——env 名无 `MAX_` 前缀，勿与字节单位混淆。 |
| `OC_SLIMAPI_REPLAY_TTL_S` | 900 | > 0 | 缓冲条目 TTL（秒）。 |

**questions/permissions 后台巡检（qp sweep）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_QP_SWEEP_ENABLED` | true | bool | 跨目录 questions/permissions 后台预取巡检总开关。 |
| `OC_SLIMAPI_QP_SWEEP_INTERVAL_SECONDS` | 1800.0 | > 0 | 巡检周期（秒）。 |
| `OC_SLIMAPI_QP_SWEEP_DAILY_BUDGET` | 100 | >= 0 | 每日巡检预算（上游 fan-out 次数上限，防后台流量失控）。 |

**v4 DB 辅助源探测周期**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_DBAUX_PROBE_INTERVAL_S` | 30 | > 0 | dbaux 可用性重探周期（秒）——熔断后恢复探测的节奏（§7.3「恢复」）。 |

**客户端标识 hash（隐私）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_CLIENT_ID_HASH` | 1 | bool（fail-closed） | access log `clientId` 脱敏开关；读到 false 才落明文（fail-closed 默认 hash）。 |
| `OC_SLIMAPI_CLIENT_ID_SALT` | None | 任意串 | HMAC salt（设非空时 `sha256`→`hmac_sha256`），防止彩虹表反查设备 id。 |

**ETag 总开关**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_ETAG_ENABLED` | true | bool | 全部 `/slimapi` ETag 路由的 ETag/304 生成总开关。关闭的**副作用**是重开时轮换全部 ETag 验证器（客户端 304 全部 miss 一次，属预期）。 |

**观测面开关（流量账本 / access log / snapshot）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | 1 | bool | `0` 时 `/slimapi/metrics` 的 `traffic` 块退化为 `{enabled:false}`、ledger no-op——**排障时关闭它会让省流观测全部消失**（业务不受影响）。 |
| `OC_SLIMAPI_ACCESS_LOG_ENABLED` | 1 | bool | `0` 时 access log 完全不落盘（snapshot 循环**不受此开关影响**，仍自持 prune）。 |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_ENABLED` | 1 | bool | `0` 时停止周期快照（历史趋势断档，access log 不受影响）。 |
| `OC_SLIMAPI_ACCESS_LOG_COMPRESS_ON_STARTUP` | 1 | bool | 启动时把早于今天的 `.jsonl` 压缩为 `.gz`（后台 maintenance 周期默认 1h 亦做同款，见 §5.3）。 |

**Directory allowlist（[3.3.0] 起）**

| 变量 | 默认值 | 范围 | 说明 |
|---|---|---|---|
| `OC_SLIMAPI_DIRECTORY_ALLOWLIST` | 未配置 | 三态 | 键缺失 = 机制禁用（零行为变化）；显式空串 `""` = reject-all；非空 = 冒号分隔子树清单。详见 §5.5.1 运维节。 |

**完整权威清单**：以上与 §5.5 各族 knob 的权威定义/默认值均在 `src/oc_slimapi/config.py` 的 `Settings`（本节为运维速查镜像）。

### 5.5.1 directory allowlist 运维（[3.3.0] 起；现状边界声明）

- **三态语义**（解析于 `src/oc_slimapi/config.py` 的 `_directory_allowlist_env`）：键缺失（默认）→ 机制禁用，一切行为零变化；显式空串 `""` → 机制启用 + **reject-all**（`/slimapi/file/**` 三端点全 403 `directory_not_allowed`）；非空 → 冒号分隔清单，canonical（realpath 双边）子树匹配。
- **不对称语义（排障易踩）**：显式空清单（reject-all）下 `/slimapi/file/**` 全 403，但 **SSE hub 不过滤帧**（GlobalHub 帧过滤仅对**非空清单**生效）——勿以 SSE 行为反推 file 门状态。
- **现状边界（重要——不是全局门）**：allowlist 门当前仅覆盖三处半——① `/slimapi/file`、`/file/content`、`/file/status` 三端点 403 门；② GlobalHub SSE 帧过滤（digest + q/p 直推，仅非空清单）；③ v4 全局 sessions 列表 DB 谓词 + 降级 fail-closed；③a health `features.allowlist` 回演（只读观测）。**vcs/find/providers/session 单查/messages 族/todo/children/diff/写族 17 端点/token stream/questions/permissions 均不经过 allowlist 判定**——把非空清单当作全局目录隔离会得到错误的安全感。逐条挂载点/未覆盖清单与扩面影响评估见 [`docs/ocmar/reviews/2026-08-21-allowlist-global-gate-impact.md`](ocmar/reviews/2026-08-21-allowlist-global-gate-impact.md)（R-1b 只读评审，结论=现状非全局门，扩面属未决 owner 决策）。
- **生效时机**：清单 env 在进程启动时读取一次；改清单须 `systemctl --user restart oc-slimapi`。health `features.allowlist.enabled` 反映启动时三态结论（未配置=false）。
- **故障模式联动**：allowlist 非空 × dbaux 不可用 → v4 全局 sessions 列表 **503 fail-closed**（`/slimapi/search` 通配 × db-down 同理，见 §7.3 场景矩阵）；SSE 丢帧观察 `features.allowlist.droppedEvents` 计数（不泄露清单内容）。

### 5.6 journald 查询手册（应用日志）

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

### 5.6 启动期日志样本（健康态）

```
systemd[...]: Started oc-slimapi.service ...
oc-slimapi[...]: INFO: Started server process [PID]
oc-slimapi[...]: INFO: Waiting for application startup.
oc-slimapi[...]: INFO: Application startup complete.
oc-slimapi[...]: INFO: Uvicorn running on http://127.0.0.1:4097 (Press CTRL+C to quit)
```

> 若 `OC_SLIMAPI_HOST=127.0.0.1`，则日志显示 `http://127.0.0.1:4097`。

启动失败常见原因：upstream 不可达、`OC_SLIMAPI_HOST` 非 loopback 且非 `0.0.0.0`（validate 白名单事实——`0.0.0.0` 为 opt-in 选项，非默认）、`OC_SLIMAPI_UPSTREAM` 非 loopback HTTP。

### 5.7 503 burst WARNING 观测（[4.10.1] 起）

背景：2026-08-21 观测到两簇自愈 503（01:24–01:37 连环 16 次、21:19–21:27 零星），此前无告警面可查。4.10.1 起 sidecar 对 5xx 突发输出结构化 WARNING，一条即可被 journald warning 级捕获：

```bash
journalctl --user -u oc-slimapi -p warning | grep upstream_5xx_burst
```

样本消息：

```
upstream_5xx_burst count=5 window_s=60 codes={"503":5} paths={"/slimapi/session/ses_a…":3,"/slimapi/sessions":2}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `count` | 触发窗口内 5xx 响应数（触发即第 5 次，恒 =5；更长突发会拆成多条 WARNING） |
| `window_s` | 滑动窗口长度（秒），固定 60 |
| `codes` | 窗口内 per-status 分布（fail-closed 503 / upstream 5xx 映射；4xx 不计） |
| `paths` | 窗口内出现最多的 ≤3 个请求路径 |

语义要点：

- **只统计 sidecar 发出的 5xx**（fail-closed 503、upstream 5xx 映射为 503），不统计 4xx；也不含响应未开始即中断的合成 500（异常路径的记账占位，实际未向客户端发出 5xx 响应）。
- **去抖**：触发一条 WARNING 即重置窗口；同一突发只出一条，下一条需新的完整窗口（≥5 次）。
- 单进程内存态，无落盘、无 config 面；阈值常量出处 `src/oc_slimapi/burst_watch.py`（`_BURST_WINDOW_S=60`、`_BURST_THRESHOLD=5`，刻意硬编码）。
- 与 `/slimapi/metrics.traffic` 的 fail-closed 计数（§6.2）互补：metrics 看累计，burst WARNING 看时间聚集。

---

## 6. 健康自检

### 6.1 服务级

```bash
systemctl --user is-active oc-slimapi    # active
systemctl --user is-enabled oc-slimapi   # enabled
```

### 6.2 应用级

```bash
# 必须带版本参数 ?v=4（X-Slimapi-Version 头已删除）
curl -s 'http://127.0.0.1:4097/slimapi/health?v=4' | jq .
```

期望响应（v4-only 视图，`?v=4`；4.8.0 起单版本窗口——4.0.0–4.7.0 曾为 (3,4) 双版本、v3 视图已退役）：

```json
{
  "slimapi_contract": 4,
  "sidecar": { "ok": true, "version": "<包版本，读 dist-info>" },
  "server":  { "api_version": 4, "accepted_client_versions": [4] },
  "schema":  { "degraded": false, "version": 4, "clientMin": 4, "clientMax": 4 },
  "features": { "tokenStream": true, "thresholdedSkeleton": true, "skeletonInlineOutputMaxBytes": 4096, "allowlist": { "enabled": false } },
  "auxiliary": { "available": true, "mode": "db" }
}
```

- `slimapi_contract` = 当前请求生效的 wire 视图（4.8.0 起恒 4——`routes/health.py` 单一视图源 `wire_view_from_scope`；恒带根级 `auxiliary`，语义见 v4-contract §3.2）；权威契约 = `docs/specs/v4-contract.md`。
- `sidecar.version` = `pyproject.toml` 的版本（示例勿照抄——以部署版本为准；editable install 升级后须 reinstall 刷新，见 §4）。
- `server.api_version` = 4；`accepted_client_versions` = `[4]`（4.8.0 起 (4,4) v4-only 窗口：`?v=4` 唯一合法，缺 `v`/`v=3`/不支持值被 `400 unsupported_version supported:[4]` 拒绝——`X-Slimapi-Version` 头已删除）。
- `features.allowlist` = [3.3.0] 起的 allowlist 机制回演（`enabled` 反映 `OC_SLIMAPI_DIRECTORY_ALLOWLIST` 三态，未配置=false；可达时另有 `droppedEvents` 计数，见 §5.5.1）。
- `schema.degraded=true` → 启动 smoke 探针发现 opencode 响应字段漂移，需查上游是否升级/改了 schema。
- 不带 `?v=4` → `400 unsupported_version`（版本选择器终态门禁，符合契约）。

### 6.3 ready 检查（轻量）

```bash
curl -s 'http://127.0.0.1:4097/slimapi/ready?v=4'
```

---

## 7. v4 DB 辅助源运维（B0 预置，随 B3a 生效）

> **B0 批预置文档**：本章为 v4（**4.0.0**，B3a 批次）DB 辅助投影源
> （`/slimapi/sessions?v=4` 常态路径）的运维说明，**功能随 4.0.0 生效**（生产已部署）。
> 设计权威：`docs/system-architecture-proposal-2026-08-17.md` §3.1/§3.5；
> wire 见 `docs/specs/v4-contract.md`（4.0.0 实施基线 + 2026-08-19 正式修订冻结）；
> **写域约束：sidecar 零 DDL/DML/PRAGMA 写**（见 `AGENTS.md` 硬规则「SQLite 写域」）。

### 7.1 DB 路径解析

DB 辅助源以 `mode=ro` 只读打开 opencode 的 SQLite。路径解析顺序（裁决行 98）：

1. **`OC_SLIMAPI_OPENCODE_DB` 显式配置**（生产推荐）——直接使用。
2. **`OPENCODE_DB` 继承**——复刻上游解析：`InstallationChannel` 分库
   `latest`/`beta`/`prod` → `opencode.db`；其余 channel → `opencode-<channel>.db`。
3. **`:memory:` 禁用辅助源**——内存库无文件可读，禁用辅助降级 HTTP。
4. **启动 log 打实际解析路径**——运维确认到底在读哪个文件；备份恢复 /
   channel 切换换 DB 文件后尤其重要（配合 7.3 的 inode 校验 / 重探观察）。

### 7.2 索引运维程序（运维显式动作，sidecar 零写入）

- **sidecar 永不写上游 DB（含 DDL）**；索引建立属**运维显式动作（含定义校验）**，
  不在 sidecar 内（proposal 行 107-108）。
- **触发条件**：仅当生产 EQP + P99 数据证明必要时由运维手动执行。当前设计为
  **首期无索引直跑**：真库 384 行全表扫温测 ~0.015ms，`P99 < 20ms` 熔断护栏兜底
  （超限 → 熔断降级 HTTP + 告警）。
- **候选索引**：sort-shaped 独立 `(time_updated DESC, id DESC)` 索引
  （服务 keyset 排序）；**不是** v2.1 拟议的复合索引（filter-shaped，EQP 实证
  仅部分覆盖）。
- **程序**（对生产库手动执行）：

```sql
CREATE INDEX IF NOT EXISTS idx_session_time_updated_id
  ON session (time_updated DESC, id DESC);

PRAGMA index_xinfo(idx_session_time_updated_id);  -- 定义校验（防同名异构误判）
```

> **为什么必须 `PRAGMA index_xinfo` 校验**：`CREATE INDEX IF NOT EXISTS`
> 不验证列定义——同名索引存在但列/顺序异构时会**静默成功**；`index_xinfo`
> 输出实际列序（`seqno`/`cid`/`name`/`desc`），运维对照 `<time_updated DESC,
> id DESC>` 逐列核对，防同名异构误判。

### 7.3 熔断排障

DB 辅助源熔断策略（proposal 行 100/106；护栏 7）：

- **P99 > 20ms 熔断**：查询延迟滑动窗口超限 → 熔断禁用。
- **错误分类熔断**：`SQLITE_SCHEMA` / `no such table|column` / I/O /
  WAL-SHM 不可达 → 熔断禁用辅助。
- **恢复**：周期重探（成功 → 解除熔断；持续失败保持禁用）。
- **inode/mtime 定期校验**：备份恢复 / channel 切换换 DB 文件 → sidecar 持旧
  fd 读已删 inode → 校验发现 → 重开重探（挂熔断器周期）。
- 熔断 = **全降级 HTTP `/experimental/session`**（v3 完全不受影响；v4 sessions
  走降级矩阵，`degraded`/503 语义见 v4-contract §4）。
- **观察**：`GET /slimapi/health` 的 `auxiliary: {available, mode}` 字段。

**503 场景矩阵（v4 sessions 面；与 200 降级对照）**

| 场景 | dbaux 状态 | allowlist | 结果 | 语义 |
|---|---|---|---|---|
| 常态 | 可用 | 任意 | 200（DB 投影） | v4 正常路径 |
| Class A 降级 | 不可用/熔断 | **未配置/空外的默认态** | 200（HTTP 降级，`sessionsSource:"http"`） | 白名单 ⊆ 结果集可由上游保证时降级放行 |
| **fail-closed（最严列）** | 不可用/熔断 | **非空** | **503**（`degraded503:true`） | 「白名单 ⊆ 结果集」不可保证时宁可 503（ora B-2 选②；见 §5.5.1） |
| **search 通配 × db-down** | 不可用/熔断 | —（通配跨目录，语义同非空清单） | **503** | 通配搜索无法在 DB 层收敛目录集，db 不可用时 fail-closed |
| TransformBusy | —（与 dbaux 无关） | 任意 | 503（`transform_busy`，`degraded503` **不置位**） | transform 池拥塞，非降级语义；排障见 §9 |

### 7.4 runbook：升级 opencode 后第一步（n6）

升级 opencode 后，**第一步观察** `/slimapi/health` 的
`auxiliary.available` / `auxiliary.mode`：

- **熔断（`available: false`）= 等价性失败的信号**——上游 schema 可能漂移；
- 对照 `docs/refactor-plans/slimapi-refactor-plan.md` §6.2
  （DB 投影源整体禁用开关）逐链排查：等价性锚定测试失败 → 禁用辅助 →
  全降级 HTTP；
- **确认根因前不要手动放开熔断**（等效性测试失败即禁用辅助是设计决策，
  非临时故障）。
- 当前 3.x 阶段 `auxiliary` 字段尚不存在（**4.0.0** B3a 起出现）。

---

## 8. ocdroid 项目组须知（接入侧）

ocdroid 客户端**不直接操作** sidecar 进程，只通过 stunnel mTLS 接它。需要知道的：

| 项 | 值 |
|---|---|
| 经 sidecar mTLS 入口（推荐） | stunnel `:14097` → sidecar `127.0.0.1:4097` |
| 经 sidecar 明文直连入口（Tailscale 等） | Tailscale 地址`:4097` → sidecar `0.0.0.0:4097`（依赖 Tailscale ACL / 防火墙；无 mTLS）（opt-in 非默认；2026-08-21 起默认回环） |
| 直连回退（不经 sidecar） | stunnel `:14096` → opencode `127.0.0.1:4096` |
| 所有 `/slimapi/**` 请求必带 query | `?v=4`（4.8.0 起 v4-only 单版本窗口；无 `v`/`v=3`/不支持值 → `400 unsupported_version supported:[4]`；`X-Slimapi-Version` 头已删除不解读） |
| 非 `/slimapi/**` | **3.0.0 已关闭**——未收编路径 404 `thin_route_not_found`（2.x 为透明反代，历史行为见 CHANGELOG） |
| 健康自检（客户端侧） | `GET /slimapi/health?v=4` 读 `server.api_version` / `accepted_client_versions` 做运行时兼容判断 |
| Wire 行为变更来源 | 本仓 [`CHANGELOG.md`](../CHANGELOG.md)（路径/头/错误码以本仓 + [`v4-contract.md`](specs/v4-contract.md) 为准） |
| 客户端配套改动清单 | [`CLIENT_CHANGES.md`](specs/CLIENT_CHANGES.md) |
| 4.11.0 能力交接简报（webui/ocdroid 组） | [`specs/HANDOVER-4.11.0.md`](specs/HANDOVER-4.11.0.md)（新接口/变动/推荐用法一页总览） |

sidecar 进程的启停、日志、升级由 **服务端运维** 负责，ocdroid 侧无需介入；但理解拓扑有助于排障（例如 sidecar 重启时 SSE 会断、客户端应收 `resync` 重连）。

---

## 9. 排障速查

| 症状 | 先查 |
|---|---|
| 服务起不来 | `journalctl --user -u oc-slimapi -n 50`；多半是 upstream / host 校验失败 |
| `schema.degraded=true` | opencode 升级了；查 `src-ref/opencode/current/`（本仓上游源码快照）对照字段，或临时设 `OC_SLIMAPI_SMOKE_SESSION_ID` 跳过随机探针 |
| 客户端连上但 400 | 无 `?v=4` / `v=3` / 不支持值 → `unsupported_version`；directory 相关 → `invalid_directory_selector`/`directory_conflict`/`directory_header_retired`（消费集头通道已退役，用 `?directory=`） |
| SSE 卡顿/断 | `journalctl --user -u oc-slimapi \| rg 'backpressure\|resync\|503'`；查 `/slimapi/metrics?v=4` 的订阅者计数（**metrics 探针自身须带 `?v=4`**，否则 400） |
| SSE 订阅被 400 拒 | 订阅数触顶：digest 面 `MAX_SUBSCRIBERS_PER_DIRECTORY`（8）/`MAX_TOTAL_SUBSCRIBERS`（16），token stream 面 `TOKEN_STREAM_MAX_SUBSCRIBERS`（8）；按 §5.5 表调 env 后重启 |
| `transform_busy` 503 持续 | transform 池拥塞（非 dbaux 降级，`degraded503` 不置位）：先看 `OC_SLIMAPI_MAX_TRANSFORMS`/`TRANSFORM_ABSORB_BUDGET_SECONDS`（默认吸收 2.5s）与上游延迟；偶发属预期，持续才调参 |
| questions/permissions 结果缺目录 | envelope `errors[]`（该目录 `upstream_unavailable` 等）与 `truncated:true`（聚合预算触顶）——降级字段观察法，非故障 |
| crash-loop（反复重启） | `systemctl --user status oc-slimapi` 看 Restart 计数 + `journalctl` 找启动即崩原因：端口占用、预算校验 `RuntimeError`（如 `ACCEPTED_CLIENT_VERSIONS` 非 (4,4)）、state dir 不可写；unit 层加固参考 `deploy/oc-slimapi.service`（`Restart=on-failure` + `RestartSec=5` + `TimeoutStopSec=60`） |
| `systemctl stop` 后快照缺终帧 / journal 见 SIGKILL | 核对 unit `TimeoutStopSec` 是否 ≥ 关停链合计（5+30+10+5s，见 §4 shutdown 语义，F-010/F-214）；维护 gzip 卡死场景查 `~/.local/state/oc-slimapi/logs/` 残留 tmp 与启动日志 `_cleanup_leftover_tmp` 兜底 |
| 观测面自身健康 | access log 当天文件（`~/.local/state/oc-slimapi/logs/access-$(date +%F).jsonl`）在增长；snapshot 每周期（默认 300s）有新帧——**journal 关键词 `snapshot` + `inactive`**（首帧失败即停不重试，须重启恢复，见 traffic-accounting.md §9.1） |
| 升级后行为变化 | 先看 [`CHANGELOG.md`](../CHANGELOG.md) 对应版本节 |

---

## 10. 相关文档

| 文件 | 用途 |
|---|---|---|
| [`v3-contract.md`](specs/v3-contract.md) | ≤4.7.0 历史契约存档（v3 wire 版本已退役） |
| [`v4-contract.md`](specs/v4-contract.md) | **Wire 契约权威**（4.0.0 实施基线 + 2026-08-19 修订冻结；4.8.0 起 v4-only 自包含） |
| [`v2-contract.md`](specs/v2-contract.md) | v2 契约（已于 3.0.0 退役，历史参考） |
| [`release.md`](release.md) | 发版流程 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | 接口行为变更记录 |
| [`develop.md`](develop.md) | 配置项速查 + 开发运行 |
| [`../AGENTS.md`](../AGENTS.md) | Agent 入口索引 |

---
## 11. G-ACL 部署姿态与边界验证（历史 0.0.0.0:4097 + 14097 mTLS 隧道；2026-08-21 起默认回环，本节为 opt-in 部署 runbook）

> **参照**：`docs/ocmar/reports/2026-07-21-g-acl-ops-evidence.md`（本日证据报告）  
> **历史部署姿态（2026-08-20 前 steady-state）**：`0.0.0.0:4097` 明文监听 + `:14097` mTLS 隧道（stunnel `requireCert=yes verifyChain=yes`，复用既有证书）作为 steady-state；**2026-08-21 起（R-1a 裁决）默认部署为回环 `127.0.0.1`，直连入口默认关闭**，本节保留为 opt-in 部署的边界验证 runbook。

### 11.1 opt-in 部署拓扑（历史稳态同构）

> 下图为 opt-in（`0.0.0.0`）部署的历史稳态拓扑；默认部署中 sidecar 绑定 `127.0.0.1`，stunnel（:14097）转发目标 `127.0.0.1:4097` 不变。

```
ocdroid ──(stunnel mTLS 14097)──▶ oc-slimapi 0.0.0.0:4097 (plaintext, all interfaces)
                    │
                    ╰──(Tailscale 明文直连 :4097)──▶ oc-slimapi 0.0.0.0:4097 (plaintext; Tailscale ACL 受限)
```

- **`:4097`（sidecar 明文端口）**：绑定 `0.0.0.0`（所有接口），**opt-in 部署的稳态（历史）**。直接 `:4097` 的明文访问**必须**被网络边界（主机防火墙 / Tailscale ACL）阻塞，外部客户端须经 `:14097` mTLS 隧道。
- **`:14097`（mTLS 入口）**：stunnel 终结后转发至 `127.0.0.1:4097`，公网唯一可达入口。任何未持有有效 CA 签名客户端证书的连接在 TLS 层即被拒绝。
- **安全边界关键**：`0.0.0.0` 本身不提供接入控制——**依赖**网络边缘（防火墙 / Tailscale ACL）阻断公共/LAN 对 `:4097` 的直接明文 TCP。这就是使 opt-in `0.0.0.0` 部署可接受的安全约束。
- **loopback-only（`127.0.0.1:4097`）**：**2026-08-21 起（R-1a 裁决）为默认部署姿态**（代码 `config.validate()` 支持）；`0.0.0.0` 为 opt-in（见本节头部声明）。

### 11.2 负向探针（边界验证）

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
curl -s 'http://127.0.0.1:4097/slimapi/health?v=4'

# mTLS 回环（须带客户端证书；本机可用 stunnel 自签名测试）
curl -s --cert client-cert.pem --key client-key.pem \
  https://127.0.0.1:14097/slimapi/health
```

**负向探针结果写入**：`docs/ocmar/reports/2026-07-21-g-acl-ops-evidence.md` §3，由 ops 从外部 vantage 执行后填充。**注记**：该回填为外部人工动作，sidecar/仓库内无自动回填或提醒机制——重跑边界验证后须人工同步该报告（历史缺口，见审计 F-339 ⑲）。

### 11.3 正向验证（本机）

```bash
# sidecar 健康（本机 loopback 明文）
curl -s 'http://127.0.0.1:4097/slimapi/health?v=4'

# mTLS 回环（须带客户端证书）
curl -s --cert client-cert.pem --key client-key.pem \
  https://127.0.0.1:14097/slimapi/health
```

### 11.4 Cert 复用说明

> **路径约定（通用模板 vs 本机实例）**：仓库内 `deploy/stunnel.conf` 用系统级路径 `/etc/stunnel/`（`cert`/`key`/`CAfile` 均指向 `/etc/stunnel/*.pem`），是**通用部署模板**的写法。本机实际部署把证书放在 `~/.config/stunnel/certs/`（user-space 实例，无需 root），本节下文以本机路径为准。两者是"通用模板 `/etc/stunnel/` ↔ 本机实例 `~/.config/stunnel/certs/`"的关系，**不是错误**——部署时按实际 stunnel 实例类型选择其一即可。

- **Server cert/key**：`/home/mar/.config/stunnel/certs/server-cert.pem` + `server-key.pem`，SAN=`opencode.vectory.cn`，已用于 `:14097` mTLS 入口。
- **CA cert**：`/home/mar/.config/stunnel/certs/ca-cert.pem`，用于签发客户端证书。
- **Client cert**：ocdroid 客户端持有 `client-cert.pem` + `client-key.pem`（CA 签发）。**本轮 patch 无需轮换**。

> **注意**：`docs/mtls-setup-guide.md` 不在此仓库；ops-maintained。证书更新流程由 ops 自行维护。

---

## 12. actions 管理功能

> 本节记录 `/slimapi/actions` 的运维注意事项。功能详见 `docs/specs/INTERFACE_MAP.md` §2「Actions 本地端点」（历史形状存档：`v2-contract.md` §2）。

### 12.1 安全风险声明

> **这是风险接受声明，非安全保证。**

`/slimapi/actions` 提供在服务器端执行任意命令的能力（由 manifest 声明）。暴露面有两层：

1. **manifest 声明的动作**：由 `OC_SLIMAPI_ACTIONS_FILE` TOML 文件定义，仅 owner 可写。
2. **既有 catch-all → opencode control 端点**（`/global/upgrade`、`/global/config PATCH`、`/global/dispose`、`/instance/dispose` 等）——这些先于 actions 功能存在，经明文 `:4097` 均可达。

运维须明确接受本功能与既有 catch-all 相同的风险类：

- **明文 `:4097` 可达**：能访问 `:4097` 的任何设备均可触发 manifest 中声明的任意动作。版本选择器（`?v=4`；`X-Slimapi-Version` 头已删除）≠ 鉴权，`confirm` ≠ 授权，`X-Client-Id` 不可信。
- **不做 token、不加 loopback 闸门**：本功能延续既有安全模型，不引入额外鉴权层。
- **缓解措施**（已纳入，非授权替代）：
  - 默认空 manifest（功能 opt-in，默认禁用）
  - spawn 并发上限（`OC_SLIMAPI_ACTIONS_MAX_CONCURRENT`，默认 4）
  - single-flight + min_interval 限频
  - manifest owner-only-write 保护（启动校验：文件须 owner 写权限，禁止 group/other write 位）
  - 动作名称仅作白名单字典键查找（无可变参数、无 shell=True）
  - 结构化审计日志（所有调用 WARNING 级写入 journald，不受 `OC_SLIMAPI_LOG_LEVEL` 影响）

### 12.2 manifest 配置

manifest 是一个 TOML 文件，路径由 `OC_SLIMAPI_ACTIONS_FILE` 指定。仓库内 [`deploy/actions.manifest.example.toml`](../deploy/actions.manifest.example.toml) 是与本机部署一致的 4-action 参考模板（复制到机器本地路径后改 argv[0]）。

**当前部署实例**：`~/.config/oc-slimapi/actions.toml`（owner-only-write，`chmod 0600`），声明 4 个 actions：

```toml
# TOML 文件路径由 OC_SLIMAPI_ACTIONS_FILE 指定
# 校验规则见 src/oc_slimapi/actions.py _load_manifest：regular file（拒绝 symlink）、
# owner-only-write（拒绝 group/other write 位）、owner 为 sidecar 运行用户。

[actions.plan_limit]
kind = "query"
argv = ["/home/mar/.config/opencode/scripts/plan_limit.py"]
description = "查询上游厂商订阅配额（GLM/Kimi/GPT/LongCat 余额与用量）"
timeout_s = 90
max_output_bytes = 65536

[actions.list_model]
kind = "query"
argv = ["/home/mar/.config/opencode/scripts/list_model.py"]
description = "列出 opencode 已配置 provider 及其可用模型"
timeout_s = 15
max_output_bytes = 16384

[actions.list_agent]
kind = "query"
argv = ["/home/mar/.config/opencode/scripts/list_agent.py"]
description = "列出 opencode 已配置 agent（slim preset / 内建 / 自定义）及模型"
timeout_s = 15
max_output_bytes = 32768

[actions.restart]
kind = "exec"
argv = ["/usr/bin/systemctl", "--user", "restart", "opencode-web"]
description = "重启 opencode-web 服务（require_confirm）"
timeout_s = 30
min_interval_s = 60
require_confirm = true
```

部署步骤：

1. 写 manifest 到 `~/.config/oc-slimapi/actions.toml`，`chmod 0600`（owner-only-write）。
2. 确保 argv[0] 脚本可执行（`chmod +x`，带 shebang）；exec 类用绝对路径二进制（如 `/usr/bin/systemctl`）。
3. 在 service unit `[Service]` 加 `Environment=OC_SLIMAPI_ACTIONS_FILE=/home/mar/.config/oc-slimapi/actions.toml`（模板见 `deploy/oc-slimapi.service`，默认注释）。
4. `systemctl --user daemon-reload && systemctl --user restart oc-slimapi`。
5. 验证：`GET /slimapi/actions` 返回 `enabled:true` 且 actions 列表非空；启动日志无 `manifest rejected` / `actions disabled`。

> **action 脚本来源**：`plan_limit.py` / `list_model.py` / `list_agent.py` 由 opencode 侧维护，位于 `~/.config/opencode/scripts/`。`list_model` 读 `http://127.0.0.1:4096/config/providers`，`list_agent` 读 opencode 配置 + `/agent` API，`plan_limit` 查询上游厂商订阅配额（均只读）。

### 12.3 运维注意事项

- **manifest 文件安全**：启动校验强制 manifest 必须是 regular file（拒绝 symlink）、owner-only-write（拒绝 group/other write 位）、owner 为 sidecar 运行用户。修改 manifest 后须重启 sidecar。
- **action 脚本勿 daemonize**：脚本若 fork 子进程后父进程退出，sidecar 的 `killpg` 无法收回孤儿进程（进程组 ID 会变）。action 脚本应同步执行，不要后台化/daemonize。
- **exec 200 ok:true ≠ opencode ready**：exec 动作返回 `ok:true` 仅表示子进程 exit 0。如果动作涉及重启 opencode（如 `restart_opencode`），客户端须轮询 `/slimapi/ready` 确认 opencode 重新就绪。
- **query stdout 可能含任意内容**：`markdown` 字段是脚本 stdout 投影。运维定义 action 时应注意脚本输出内容；客户端渲染应使用 sandboxed markdown renderer。
- **子进程环境（P2-2 fail-closed allowlist）**：action 子进程**不**继承 sidecar 全量环境，而是只继承固定 allowlist：`PATH`/`HOME`/`LANG`/`LC_ALL`/`LC_CTYPE`/`TMPDIR`/`XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS`，以 sidecar 运行用户（`mar`）身份执行，可读 `~/.config/opencode/` 凭证。这是有意设计（systemctl/plan_limit 需 `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR`）。sidecar 自身的 `OC_SLIMAPI_*` 等配置变量（upstream URL、路径、版本门禁、salt……）被 **fail-closed 剔除**，绝不泄漏进 action 环境——无模糊的「name contains secret」规则，纯 allowlist 白名单。
- **审计**：所有 action 调用（含 timeout/spawn-fail/disconnect/throttle）以 WARNING 级别写入 journald，不受 `OC_SLIMAPI_LOG_LEVEL` 影响。查询审计：`journalctl --user -u oc-slimapi -p warning | rg action`。
- **进程重启 = 限频归零**：min_interval 限频是内存态（`time.monotonic()`），sidecar 进程重启后清零。这是有意设计（启动后各 action 均可立即调用一次）。

---

## 13. 4.11.0 流量优化族运维面（P1 since 缓存 / P5 file·raw）

> 客户端消费指引见 `specs/CLIENT_CHANGES.md` §4.11.0；权威契约 `specs/v4-contract.md` 修订五（§10.3/§19）。本节只讲服务端运维视角。

### 13.1 P1 since 差分缓存（`src/oc_slimapi/since_cache.py`）

- 内存有界 LRU（默认 256 条 / 总 64 MiB / 单条 1 MiB；env 见 `develop.md` 配置表 `OC_SLIMAPI_SINCE_CACHE_*`）。键 = (session, cursor)；缓存的是**投影快照**用于差分，不是上游响应缓存——每次请求仍实时拉上游（新鲜度语义不变，§10.3 修订五）。
- **逐出 ≠ 故障**：LRU 逐出 / 单条超限 / `OC_SLIMAPI_SINCE_CACHE_ENABLED=false` 旁路，客户端下一轮差分收到**全量 reset**（正常路径，非错误）。观测口径：access log 中 messages 路由响应体骤增即 reset，频率高 → 考虑调大 `MAX_ENTRIES`/`MAX_BYTES`。
- token 属**进程域**（含启动随机 epoch）：sidecar 重启后客户端首轮差分必然 reset 全量，属设计内行为。
- 关停面：`OC_SLIMAPI_SINCE_CACHE_ENABLED=false` 后差分请求退化为恒 reset（响应仍 200 合法），无错误面——可用于紧急回退。

### 13.2 P5 `/slimapi/file/raw` 信封预算

- 单请求上游信封上限：生效值 = `min(OC_SLIMAPI_MAX_RESPONSE_BYTES, OC_SLIMAPI_FILE_RAW_MAX_ENVELOPE_BYTES)`（默认 min(64 MiB, 32 MiB) = 32 MiB）；超限 → 413（客户端 verbatim 收到）。
- 启动校验（fail-closed）：`transform_bound = max(既有 transform 预算, (A_AMP+1) × W × file_raw 生效 cap)`，与 raw_fetch 预留相加 ≤ 576 MiB，否则启动期 `RuntimeError`（`src/oc_slimapi/config.py` `validate()`）。调小 `OC_SLIMAPI_MAX_TRANSFORMS`（W，默认 1）或 envelope cap 可解。
- 流量记账归 `file` 桶（与 `/slimapi/file` 组同桶，`traffic.py` 前缀归并）。

### 13.3 效果观测（上线后）

```bash
# messages 桶字节应显著下降（webui 迁移差分后预期 -70%+）
ls ~/.local/state/oc-slimapi/logs/ && zcat -f ~/.local/state/oc-slimapi/logs/access-$(date +%F).jsonl* 2>/dev/null | python3 -c "
import json,sys
from collections import defaultdict
b=defaultdict(lambda:[0,0,0])
for line in sys.stdin:
    try: r=json.loads(line)
    except: continue
    if r.get('recordType')!='request': continue
    x=b[r['bucket']]; x[0]+=1; x[1]+=r.get('upIn',0); x[2]+=r.get('downOut',0)
for k,v in sorted(b.items(),key=lambda x:-x[1][1]): print(k, *v)"
```

- thin 路由 304 生效率：access log 中 todo/children/diff 行状态码分布（304 占比）。
- file/raw 采用率：`file` 桶请求数变化。
