# 流量记录与分析（省流实证）使用手册

> 如何查询与解读 oc-slimapi 的**双向字节账本** + **结构化 access log**，实证 sidecar 的省流效果。
> 特性版本：**v0.7.0+**（`/slimapi/metrics.traffic` + `logs/access.jsonl`）。
> 性质：**加性 ops 可观测面**，不 bump `X-Slimapi-Version`；ocdroid 对接无变化。
> 实现：`src/oc_slimapi/traffic.py`、`src/oc_slimapi/middleware/traffic_accounting.py`、`src/oc_slimapi/access_log.py`。

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

所有 `/slimapi/**` 端点都要版本头 `X-Slimapi-Version: 1`。

```bash
# 本机 loopback（服务默认绑 0.0.0.0:4097）
BASE=http://127.0.0.1:4097
H="X-Slimapi-Version: 1"

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
| `quiz` | `/slimapi/questions`、`/slimapi/permissions` | 聚合 + 投影 |
| `passthrough` | catch-all `/**`（发消息等写） | **不省流**，透传（基线，比值≈1） |
| `health` / `metrics` / `projects` / `other` | 各自端点 | 元数据/探活 |

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

每请求一行 JSON-lines，默认写 `logs/access.jsonl`（相对服务 CWD）：

```jsonc
{"ts":"2026-07-24T13:02:11+08:00","method":"GET","path":"/slimapi/messages/ses_x","bucket":"messages","status":200,"durationMs":12.3,"downIn":0,"downOut":4321,"upIn":20480,"upOut":0}
```

轮转：默认 **10 MiB × 5 份**（`RotatingFileHandler`，见 §6 可配）。

### 常用分析（`jq`）

```bash
LOG=logs/access.jsonl

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
```

> `jq -s` 把整个文件 slurp 进内存（适合轮转后的单文件几 MB 级）。大文件可改用 `jq` 逐行 + `awk` 求和，或导出给其它工具。

### 跟 `/metrics.traffic` 对不上？

access log 的 `downOut` 是 **wire 级**字节（中间件视角，含 SSE 连接的真实响应体）；而 ledger 的 SSE 桶 `downOut` 是 **per-subscriber-per-frame 聚合**（见 §4 fanout）。两者口径不同，**不应直接对照**。SSE 统计以 `/slimapi/metrics.traffic` 为准；逐请求审计以 access log 为准。

---

## 6. 配置（环境变量）

| env | 默认 | 作用 |
|---|---|---|
| `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | `1` | 内存账本总开关；`0` 时 `traffic`=`{enabled:false}`、所有 `record_*` no-op |
| `OC_SLIMAPI_ACCESS_LOG_ENABLED` | `1` | access log 落盘开关；`0` 时不建文件、纯 no-op |
| `OC_SLIMAPI_ACCESS_LOG_PATH` | `logs/access.jsonl` | JSON-lines 路径（相对 CWD；父目录自动创建） |
| `OC_SLIMAPI_ACCESS_LOG_MAX_BYTES` | `10485760`（10 MiB） | 单文件轮转阈值 |
| `OC_SLIMAPI_ACCESS_LOG_BACKUPS` | `5` | 保留旧份数 |

改完须 `systemctl --user restart oc-slimapi`。临时关闭省流计量（如排障）设 `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED=0`，不影响业务。

---

## 7. 读数时须知的限制（已知，非 bug）

- **`downIn` 对 GET 为 0**：`downIn` 只在路由实际读请求体时才计（ASGI `receive` 惰性）；GET 不读 body → `downIn=0`。正常。
- **SSE `requests`/`downIn` 在连接关闭时才落账**：长连接 SSE 活跃期间取快照会看到"有 `downOut` 但 `requests=0`"——连接断开才补记。
- **SSE upstream 字节按 LF 行尾估算**：`+1` 假定 `\n`；若上游用 CRLF 每行少计 1 字节（**保守偏向**，让省流比看起来更少，不夸大）。opencode `/global/event` 预期为 LF。
- **children-cache fetch 不入 per-bucket `upIn`**：single-flight coalescing 下归属不公（多请求共享一次 fetch），有意不计 → `sessions`/`children` 桶的省流比**略偏乐观**。要绝对上游总量需另加全局计数器（未来工作）。
- **`totals` 跨桶字节口径异质**：`passthrough.upIn` 可能是 gzip wire 字节，curated 桶是解码后逻辑字节。`totals` 粗糙，**per-bucket `ratios` 比 `totals` 更有意义**。

---

## 8. 相关

- 契约 / 设计：[`docs/specs/v1-contract.md`](../specs/v1-contract.md)（§2 `/slimapi/metrics`、§6 资源限制）
- 运维手册：[`docs/operations.md`](../operations.md)
- 变更记录：[`CHANGELOG.md`](../CHANGELOG.md) `[0.7.0]`
