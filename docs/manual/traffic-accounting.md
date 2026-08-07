# 流量记录与分析（省流实证）使用手册

> 如何查询与解读 oc-slimapi 的**双向字节账本** + **结构化 access log** + **内存账本周期快照**，实证 sidecar 的省流效果。
> 特性版本：**v0.7.0+**（`/slimapi/metrics.traffic` + access log）；**2026-07-29**（按天切分 + client 标识字段 + traffic snapshot）；**2026-08-01**（turn-token fence scope 简化为仅 sid；移除 serverGroupFp 字段）。
> 性质：**加性 ops 可观测面**，不 bump `X-Slimapi-Version`；ocdroid 对接无变化（`/slimapi/metrics` 为 T3 ops 端点，非客户端契约）。
> 实现：`src/oc_slimapi/traffic.py`、`src/oc_slimapi/traffic_snapshot.py`、`src/oc_slimapi/middleware/traffic_accounting.py`、`src/oc_slimapi/access_log.py`。

---

## 1. 它量了什么

sidecar 是 ocdroid 与 opencode 之间的字节中继。账本按**路由桶**分别记两条腿的字节：

| 字段 | 含义 | 方向 |
|---|---|---|
| `upIn` | sidecar **从 opencode 拉的字节**（成本 / 省流前） | upstream 响应 |
| `downOut` | sidecar **下发给 ocdroid 的字节**（省流后） | downstream 响应 |
| `downIn` | ocdroid 发给 sidecar 的请求体字节 | downstream 请求 |
| `upOut` | sidecar 发给 opencode 的请求体字节 | upstream 请求 |
| `requests` | 请求 / SSE 连接数 | — |

**省流的核心判据**：`downOut / upIn`（记为 `downOutOverUpIn`）。
- `< 1.0` → 下发比拉取少 = **省了**（如 `0.2` = 省了 80%）。
- `≈ 1.0` → 透传不省流（基线对照）。
- 见 §4 的 **SSE fanout 例外**。

---

## 2. 快速查询

所有 `/slimapi/**` 端点都要版本头 `X-Slimapi-Version: 2`。

```bash
# 本机 loopback（服务默认绑 0.0.0.0:4097）
BASE=http://127.0.0.1:4097
H="X-Slimapi-Version: 2"

# 整个 traffic 块
curl -s -H "$H" $BASE/slimapi/metrics | jq '.traffic'

# 仅各桶字节
curl -s -H "$H" $BASE/slimapi/metrics | jq '.traffic.buckets'

# 仅省流比
curl -s -H "$H" $BASE/slimapi/metrics | jq '.traffic.ratios'

# 累计 totals
curl -s -H "$H" $BASE/slimapi/metrics | jq '.traffic.totals'
```

**远程（mTLS）**：把 `$BASE` 换成 `https://opencode.vectory.cn:14097`，`curl` 带 `--cert`/`--key`/`--cacert`（复用既有客户端证书）。直连 `:4097` 明文仅限 Tailscale/本机。

> 无 `jq` 时可用 `.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['traffic'])"`。

### 一次算出"每桶省了多少字节"

```bash
curl -s -H "$H" $BASE/slimapi/metrics | jq '
  .traffic.buckets | to_entries
  | map({bucket:.key,
         upIn:.value.upIn,
         downOut:.value.downOut,
         saved:(.value.upIn - .value.downOut),
         ratio:.value.downOutOverUpIn})
  | sort_by(-.saved)'
```

---

## 3. 字段与桶说明

### 3.1 `traffic` 顶层形状

```jsonc
{
  "enabled": true,                 // OC_SLIMAPI_TRAFFIC_METRICS_ENABLED=false 时为 false（其余键省略）
  "buckets": {                     // 只有出现过流量的桶才出现
    "<bucket>": {
      "requests": 12,              // 请求/SSE 连接数
      "downIn": 0,                 // client 请求体字节（GET 通常 0，见 §7）
      "downOut": 4321,             // 下发字节（省流后）
      "upIn": 20480,               // 从 opencode 拉的字节（成本）
      "upOut": 0,                  // 发给 opencode 的请求体字节
      "framesEmitted": 0           // 仅 SSE 桶出现：发出的帧数
    },
    ...
  },
  "totals": { "requests":.., "downIn":.., "downOut":.., "upIn":.., "upOut":.. },
  "ratios": {                      // 仅 upIn > 0 的桶
    "<bucket>": { "downOutOverUpIn": 0.21 }
  }
}
```

> **加性**：未接线 ledger 时 `/slimapi/metrics` 没有 `traffic` 键（老 fixture/老客户端形状不变）。生产部署已接线，恒出现。

### 3.2 路由桶

| 桶 | 对应路径 | 省流机制 |
|---|---|---|
| `messages` | `/slimapi/messages/**`（list / `/since` / `/full`） | **骨架投影** full→thin（核心省流） |
| `events_sse` | `/slimapi/events`（控制面 SSE） | **策展**：上游全量 token/tool 流 → `session.digest`+q/p 小帧（最大省流点） |
| `token_stream_sse` | `/slimapi/sessions/{sid}/stream` | gzip + done-marker（见 §4 注意） |
| `sessions` | `/slimapi/sessions/**`（列表/`/status`/`/children`） | session/child skeleton 投影 |
| `command` | `/slimapi/command`、`/slimapi/command/**` | **骨架投影**：catalog whitelist（`name/description/agent/hints`），丢 `template`（~97.7%） |
| `agent` | `/slimapi/agent`、`/slimapi/agent/**` | **骨架投影**：catalog whitelist（`name/description/mode/hidden/native`），丢 `prompt`+`permission`（>96%） |
| `questions` | `/slimapi/questions`（跨目录聚合，加性回归） | 聚合 envelope（envelope 透传，per-dir fan-out） |
| `passthrough` | catch-all `/**`（发消息等写） | **不省流**，透传（基线，比值≈1） |
| `health` / `metrics` / `other` | 各自端点 | 元数据/探活 |

---

## 4. 怎么读"省了多少"

| 想知道 | 看哪里 | 怎么读 |
|---|---|---|
| **历史对话拉取省了多少** | `ratios.messages.downOutOverUpIn` | 单值；如 `0.2` = 下发只有成本的 20%（省 80%） |
| **SSE 策展省了多少** | `events_sse` 的 `upIn` vs `downOut` | 单订阅时 `downOut ≪ upIn`（digest 几百 B vs token 流几十 KB） |
| **透传基线对照** | `ratios.passthrough` | 应 ≈ `1.0`（不省流，用来验证账本没系统性偏差） |
| **累计节省字节** | 各桶 `upIn - downOut` 之和，或 `totals.upIn - totals.downOut` | 绝对量 |

### ⚠️ SSE fanout 例外（务必先读）

`events_sse` / `token_stream_sse` 桶的 `downOutOverUpIn` 是**聚合下发字节 / 共享上游成本**：
- `upIn` 只计**一条**共享 `/global/event` 上游连接（成本只算一次）。
- `downOut` 按**每个订阅者每帧**累加（N 个 ocdroid 设备连着 → 翻 N 倍）。

所以**多订阅 fanout 下该比值可 > 1.0**——这不是"省流失效"，而是"一份上游成本被扇出到多个客户端"。**真正的单连接省流证据 = 单订阅时 `downOut ≪ upIn`**。家里 2–5 台设备同连时，用 `downOut / 订阅数` 估算单设备省流比。

---

## 5. access log 离线分析

每请求一行 JSON-lines，**按天切分**文件 `access-YYYY-MM-DD.jsonl`（`YYYY-MM-DD` = 当天日期）。默认目录 `logs/`（相对服务 CWD）；生产 systemd 覆盖到 `~/.local/state/oc-slimapi/logs/`（见 `docs/operations.md` §5.2）。

```jsonc
{"ts":"2026-07-24T13:02:11+08:00","method":"GET","path":"/slimapi/messages/ses_x","bucket":"messages","status":200,"durationMs":12.3,"downIn":0,"downOut":4321,"upIn":20480,"upOut":0,"requestId":"a1b2c3...","client":"ocdroid","clientVer":"1.2.3","clientId":"5f4d3c2b1a098765"}
```

字段说明（`client`/`clientVer`/`clientId` 为 2026-07-29 加性字段；缺省 `null`）：

| 字段 | 含义 |
|---|---|
| `ts` / `method` / `path` / `bucket` / `status` / `durationMs` | 请求元数据 |
| `downIn` / `downOut` / `upIn` / `upOut` | 字节账本（见 §1） |
| `requestId` | `X-Request-ID`（跨 sidecar↔opencode 关联） |
| `client` | 客户端 app 名（来自 `X-Client-Name`，明文，**不 hash**） |
| `clientVer` | 客户端版本（来自 `X-Client-Version`，明文，**不 hash**） |
| `clientId` | 设备标识 hash（来自 `X-Client-Id`，默认 `sha256(raw)[:16]`；设 `OC_SLIMAPI_CLIENT_ID_SALT` 时为 `hmac_sha256(salt,raw)[:16]`） |

### 文件切分与压缩

- **按天切分**：每天一个 `access-YYYY-MM-DD.jsonl`，跨天自动切新文件。
- **启动压缩**：服务启动时把**早于今天的**（日期 `< today`）未压缩 `.jsonl` 原子压缩为 `.gz`（写 `.gz.tmp` → rename → 删源）。当天文件不压缩（活跃写入中）。
- **legacy 迁移（仅当前目录）**：`migrate_legacy_access_log` 只处理**当前 `access_log_dir` 内**的无日期文件（旧 `access.jsonl` / `access.jsonl.N`），按 mtime 归档为 `access-legacy-{mtimeYYYYMMDD}-{N}.jsonl.gz`。**不跨目录迁移**：生产部署从旧相对目录（如 cwd 下 `logs/`）升级到 `StateDirectory`（`~/.local/state/oc-slimapi/logs`）时，旧位置的历史日志**不会自动迁移**——运维需手动移动（历史日志的清理也由运维处理）。
- **`access-legacy-*.jsonl.gz` 不受 retain 自动清理**：prune 的严格匹配只认 `access-YYYY-MM-DD.jsonl(.gz)`，迁移产出的 `access-legacy-*.jsonl.gz` **永久保留**，清理由运维手动处理。
- **后台 maintenance**（默认 1h）：周期 compress + prune（`OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS`，默认 `0`=不删）。
- 读取压缩历史文件用 `zcat access-2026-07-23.jsonl.gz | jq ...` 或 `jq` 直接读管道。

### 常用分析（`jq`）

```bash
# 当天文件
LOG=logs/access-$(date +%F).jsonl

# 各桶累计 upIn / downOut / 省流字节（按成本降序）
jq -s 'group_by(.bucket)
       | map({bucket:.[0].bucket,
              upIn:(map(.upIn)|add),
              downOut:(map(.downOut)|add)})
       | map(.saved=(.upIn - .downOut))
       | sort_by(-.upIn)' "$LOG"

# 某时段（按 ts 前缀，如某小时）的累计省流
jq -s 'map(select(.ts >= "2026-07-24T13" and .ts < "2026-07-24T14"))
       | {upIn:(map(.upIn)|add), downOut:(map(.downOut)|add)}' "$LOG"

# 慢请求 Top 10（按 durationMs）
jq -s 'map({path,durationMs,bucket,status}) | sort_by(-.durationMs) | .[0:10]' "$LOG"

# 非 2xx 请求
jq -s 'map(select(.status >= 400)) | length' "$LOG"

# 按设备 hash 分组请求量（区分多设备）
jq -s 'group_by(.clientId) | map({clientId:.[0].clientId, count:length, client:.[0].client, clientVer:.[0].clientVer})' "$LOG"

# 跨多天（含压缩历史）汇总
for f in logs/access-2026-07-2*.jsonl logs/access-2026-07-2*.jsonl.gz; do
  [ -f "$f" ] || continue
  case "$f" in *.gz) zcat "$f";; *) cat "$f";; esac
done | jq -s 'group_by(.bucket) | map({bucket:.[0].bucket, upIn:(map(.upIn)|add)})'
```

> `jq -s` 把整个文件 slurp 进内存（适合单文件几 MB 级）。大文件可改用 `jq` 逐行 + `awk` 求和，或导出给其它工具。

### 跟 `/metrics.traffic` 对不上？

access log 的 `downOut` 是 **wire 级**字节（中间件视角，含 SSE 连接的真实响应体）；而 ledger 的 SSE 桶 `downOut` 是 **per-subscriber-per-frame 聚合**（见 §4 fanout）。两者口径不同，**不应直接对照**。SSE 统计以 `/slimapi/metrics.traffic` 为准；逐请求审计以 access log 为准。

---

## 6. 配置（环境变量）

| env | 默认 | 作用 |
|---|---|---|
| `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | `1` | 内存账本总开关；`0` 时 `traffic`=`{enabled:false}`、所有 `record_*` no-op |
| `OC_SLIMAPI_ACCESS_LOG_ENABLED` | `1` | access log 落盘开关；`0` 时不建文件、纯 no-op |
| `OC_SLIMAPI_ACCESS_LOG_DIR` | `logs` | access log 目录（按天文件 `access-YYYY-MM-DD.jsonl` 落在其下；父目录 best-effort 创建）。生产 systemd 覆盖为 `%S/oc-slimapi/logs` |
| `OC_SLIMAPI_ACCESS_LOG_COMPRESS_ON_STARTUP` | `1` | 启动时压缩早于今天（`< today`）的 `.jsonl` → `.gz` |
| `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS` | `0` | prune 早于 N 天的 `access-YYYY-MM-DD.jsonl(.gz)`；**代码默认 `0`=不删**（本地开发/测试）；**生产 unit 配置 `3`**（见 `deploy/oc-slimapi.service` / `docs/operations.md` §3.2）。**不含** `access-legacy-*.jsonl.gz`（永久保留） |
| `OC_SLIMAPI_ACCESS_LOG_MAINTENANCE_INTERVAL_S` | `3600` | 后台 compress+prune 周期（≥60） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_ENABLED` | `1` | 内存账本周期快照开关（见 §9） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_INTERVAL_S` | `300` | 快照周期（≥1） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` | `logs/traffic-snapshot.jsonl` | 快照文件名 stem（按天生成 `<stem>-YYYY-MM-DD.jsonl`）；生产 systemd 覆盖为 `%S/oc-slimapi/logs/traffic-snapshot.jsonl` |
| `OC_SLIMAPI_CLIENT_ID_HASH` | `1` | 设备 id hash 开关（fail-closed 默认开；读到 false 时才落明文） |
| `OC_SLIMAPI_CLIENT_ID_SALT` | `None` | HMAC salt（非空时 `sha256`→`hmac_sha256`） |

> **deprecated（保留兼容）**：`OC_SLIMAPI_ACCESS_LOG_PATH`（旧单文件路径；若设非默认值，取 parent dir 作 `ACCESS_LOG_DIR` 兜底）、`OC_SLIMAPI_ACCESS_LOG_MAX_BYTES`、`OC_SLIMAPI_ACCESS_LOG_BACKUPS`（后两者 unused since daily rotation，保留字段不影响行为）。

改完须 `systemctl --user restart oc-slimapi`。临时关闭省流计量（如排障）设 `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED=0`，不影响业务。

---

## 7. 读数时须知的限制（已知，非 bug）

- **`downIn` 对 GET 为 0**：`downIn` 只在路由实际读请求体时才计（ASGI `receive` 惰性）；GET 不读 body → `downIn=0`。正常。
- **SSE `requests`/`downIn` 在连接关闭时才落账**：长连接 SSE 活跃期间取快照会看到"有 `downOut` 但 `requests=0`"——连接断开才补记。
- **SSE upstream 字节按 LF 行尾估算**：`+1` 假定 `\n`；若上游用 CRLF 每行少计 1 字节（**保守偏向**，让省流比看起来更少，不夸大）。opencode `/global/event` 预期为 LF。
- **children-cache fetch 不入 per-bucket `upIn`**：single-flight coalescing 下归属不公（多请求共享一次 fetch），有意不计 → `sessions`/`children` 桶的省流比**略偏乐观**。要绝对上游总量需另加全局计数器（未来工作）。
- **`totals` 跨桶字节口径异质**：`passthrough.upIn` 可能是 gzip wire 字节，curated 桶是解码后逻辑字节。`totals` 粗糙，**per-bucket `ratios` 比 `totals` 更有意义**。

---

## 9. 内存账本周期快照（`traffic-snapshot-YYYY-MM-DD.jsonl`）

> **2026-07-29 加性 ops 面**。`/slimapi/metrics.traffic` 是内存账本的实时快照（per-process、in-memory），进程重启即清零。`TrafficSnapshotter` 周期（默认 300s）把 ledger 的 **cumulative 视角**写入 JSONL 文件，用于跨重启的长期趋势分析。

### 9.1 它记什么

每帧一行 JSON-lines，按天切分到 `traffic-snapshot-YYYY-MM-DD.jsonl`（命名规则与 access log `<stem>-YYYY-MM-DD.jsonl` 统一）。`OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` 定义文件名 stem（默认 `logs/traffic-snapshot.jsonl` → 实际文件如 `logs/traffic-snapshot-2026-07-29.jsonl`）；生产 systemd 覆盖到 `~/.local/state/oc-slimapi/logs/traffic-snapshot-YYYY-MM-DD.jsonl`：

```jsonc
{
  "ts": "2026-07-29T13:00:00+08:00",   // 快照时间
  "bootTs": "2026-07-29T08:00:00+08:00", // 进程启动时间（构造时固定一次）
  "runId": "a1b2c3d4e5f60718",           // 进程内 16-hex（uuid4 前 16）
  "uptimeS": 18000,                      // 自启动秒数（time.monotonic 差，抗 NTP 回拨）
  "pid": 12345,
  "enabled": true,
  "buckets": { /* 同 /metrics.traffic.buckets，cumulative */ },
  "totals": { /* 同 /metrics.traffic.totals */ },
  "ratios": { /* 同 /metrics.traffic.ratios */ }
}
```

- **cumulative 语义**：`buckets`/`totals` 是该进程**自启动以来**的累计值（含 SSE 真实成本 `upIn`/`downOut`），即 `/metrics.traffic` 的内存账本逐字段 dump。
- **跨进程分段**：进程重启后 `bootTs`/`runId` 变化、`uptimeS` 归零 → 新的一段 cumulative。分析侧通过 `bootTs`/`runId` 识别进程边界，对相邻两帧算 delta 得到该段时间增量。
- **跨天切分**：日期变更时自动切新文件 `traffic-snapshot-YYYY-MM-DD.jsonl`（当天文件 append 写入，不压缩）。**snapshot 文件不经 access log 的 compress/prune 维护**（后者只认 `access-` 前缀），即不自动压缩、不自动清理。
- **inactive（首帧失败即停，不重试）**：snapshotter 首帧写入失败（磁盘满 / 路径不可写 / 权限不足）→ 标 **inactive**：**不创建后台 task、不周期重试**，该进程内不再写快照。需运维排查磁盘/路径后**重启**服务恢复。这是有意的 fail-loud 设计（避免无声重试掩盖根因）。
- **shutdown 终态**：进程优雅退出时写一帧终态（尽可能捕捉最后一段数据）；非优雅退出则缺终态（下次启动补第一帧时由 `bootTs` 变化体现）。

### 9.2 分析示例（`jq`）

```bash
# 当天文件
SNAP_TODAY=logs/traffic-snapshot-$(date +%F).jsonl

# 查看最近一帧（当前 cumulative 视角）
tail -1 "$SNAP_TODAY" | jq .

# 跨多天汇总所有 snapshot 帧（snapshot 文件不经自动压缩，均为 .jsonl）
cat logs/traffic-snapshot-*.jsonl > /tmp/snap-all.jsonl

# 跨进程分段：列出每个 runId 的首末帧，算该进程累计
jq -s 'group_by(.runId)
       | map({runId:.[0].runId, bootTs:.[0].bootTs,
              firstTs:.[0].ts, lastTs:.[-1].ts,
              totals:.[-1].totals})' /tmp/snap-all.jsonl

# 两帧间 delta（某进程内的省流增量）
jq -s '
  def delta(a;b): a - b;
  map(select(.runId == "a1b2c3d4e5f60718"))
  | {upIn_delta: (delta(.[-1].totals.upIn; .[0].totals.upIn)),
     downOut_delta: (delta(.[-1].totals.downOut; .[0].totals.downOut))}' /tmp/snap-all.jsonl

# 按时间序列看 messages 桶省流比趋势
jq -c '{ts, ratio: .ratios.messages.downOutOverUpIn}' /tmp/snap-all.jsonl
```

### 9.3 与 access log / `/metrics.traffic` 的口径差异

| 来源 | 口径 | 重启后 |
|---|---|---|
| `/slimapi/metrics.traffic` | 内存账本实时快照（per-process cumulative） | 清零 |
| `traffic-snapshot-YYYY-MM-DD.jsonl` | 内存账本周期 dump（同 `/metrics.traffic`，持久化；按天切分，不经自动压缩/prune） | 新段（`bootTs`/`runId` 变化） |
| access log（`access-*.jsonl`） | **逐请求** wire 级字节（含 `requestId`/`client*`） | 不受影响（按天文件） |

三者为**不同口径**，不直接对照：snapshot 是聚合 cumulative，access log 是逐请求明细，`/metrics.traffic` 是实时聚合。分析"某时段省了多少"用 access log（§5）；分析"长期趋势 / 跨重启累计"用 snapshot。

---

## 10. 相关

- 契约 / 设计：[`docs/specs/v2-contract.md`](../specs/v2-contract.md)（§2 `/slimapi/metrics`、§7 可观测性/access log/client header、§12 流量查询）
- 运维手册：[`docs/operations.md`](../operations.md)（§5 日志策略、§5.2 落盘目录、§5.3 维护）
- 变更记录：[`CHANGELOG.md`](../CHANGELOG.md) `[0.7.0]`（access log + traffic 首版）、`[1.0.0]`（按天切分 + client header + snapshot）
