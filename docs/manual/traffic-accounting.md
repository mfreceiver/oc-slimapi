# 流量记录与分析（省流实证）使用手册

> 如何查询与解读 oc-slimapi 的**双向字节账本** + **结构化 access log** + **内存账本周期快照**，实证 sidecar 的省流效果。
> 特性版本：**v0.7.0+**（`/slimapi/metrics` 响应的 `traffic` 块 + access log）；**2026-07-29**（按天切分 + client 标识字段 + traffic snapshot）；**2026-08-01**（turn-token fence scope 简化为仅 sid；移除 serverGroupFp 字段）；**2026-08-16**（v3 Batch A 加性观测字段：`wireVersion`/`selectorResult`/`directoryForm`/`recordType`/`lifecycleId` + SSE 开关行 + snapshot `v3` 节 + `aggregate_v3_observability`，见 §5.1/§9.4）；**2026-08-18**（4.0.0 双版本期：`wireVersion` 增 `"4"`、`selectorResult` 增 `v4` 取值 + `/slimapi/metrics` 新增 `dbaux` 观测块，见 §5.1）。
>
> **术语澄清**：本手册中出现的 `/slimapi/metrics.traffic` / `/metrics.traffic` 等写法，**不是**独立 HTTP 路由——代码里只有 `GET /slimapi/metrics`（`src/oc_slimapi/routes/metrics.py`），流量账本是该响应 JSON 的 `traffic` 子键。下文为简洁起见用 "`metrics.traffic`" 作为该数据块的简称。
> 性质：**加性 ops 可观测面**（3.0.0 起请求头通道删除、`?v=` selector 为唯一版本通道；4.8.0 起 v4-only 单版本窗口）；ocdroid 对接无变化（`/slimapi/metrics` 为 T3 ops 端点，非客户端契约）。
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
| `cache` | 可选字段：`"hit" | "miss"`（仅 catalog 缓存路径——`/slimapi/agent`、`/slimapi/command`——且 `OC_SLIMAPI_CATALOG_CACHE_TTL_SECONDS > 0` 时的 access log 记录写入；TTL=0 禁用缓存与其余记录均无此字段） | — |

**省流的核心判据**：`downOut / upIn`（记为 `downOutOverUpIn`）。
- `< 1.0` → 下发比拉取少 = **省了**（如 `0.2` = 省了 80%）。
- `≈ 1.0` → 透传不省流（基线对照）。
- 见 §4 的 **SSE fanout 例外**。

---

## 2. 快速查询

`/slimapi/**` 端点须带查询参数 `?v=4`（4.8.0 起 v4-only 单版本窗口：缺 `v` / `v=3` / 不支持值 → 400 `unsupported_version supported=[4]`；`X-Slimapi-Version` 头已删除、出现不解读；详见契约 `docs/specs/v4-contract.md` §0。历史：4.0.0–4.7.0 为 (3,4) 双版本窗口）。`GET /slimapi/versions` 无条件豁免。

```bash
# 本机 loopback（服务默认绑 0.0.0.0:4097）
BASE=http://127.0.0.1:4097
V="v=4"

# 整个 traffic 块
curl -s "$BASE/slimapi/metrics?$V" | jq '.traffic'

# 仅各桶字节
curl -s "$BASE/slimapi/metrics?$V" | jq '.traffic.buckets'

# 仅省流比
curl -s "$BASE/slimapi/metrics?$V" | jq '.traffic.ratios'

# 累计 totals
curl -s "$BASE/slimapi/metrics?$V" | jq '.traffic.totals'
```

**远程（mTLS）**：把 `$BASE` 换成 `https://opencode.vectory.cn:14097`，`curl` 带 `--cert`/`--key`/`--cacert`（复用既有客户端证书）。直连 `:4097` 明文仅限 Tailscale/本机。

> 无 `jq` 时可用 `.venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['traffic'])"`。

### 一次算出"每桶省了多少字节"

```bash
curl -s "$BASE/slimapi/metrics?$V" | jq '
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
| `token_stream_sse` | `/slimapi/sessions/{sid}/stream` | identity SSE；v4-native delta/removed/replayable resync（无 snapshot/done marker） |
| `sessions` | `/slimapi/sessions/**`（列表/`/status`/`/children`） | session/child skeleton 投影 |
| `command` | `/slimapi/command`、`/slimapi/command/**` | **骨架投影**：catalog whitelist（`name/description/agent/hints`），丢 `template`（~97.7%） |
| `agent` | `/slimapi/agent`、`/slimapi/agent/**` | **骨架投影**：catalog whitelist（`name/description/mode/hidden/native`），丢 `prompt`+`permission`（>96%） |
| `questions` | `/slimapi/questions`（跨目录聚合，加性回归） | 聚合 envelope（envelope 透传，per-dir fan-out） |
| `permissions` | `GET /slimapi/permissions`（**精确匹配**，2026-08-22 Q6 增补） | 聚合 envelope（镜像 `questions`；见 §3.3 Q6 注记） |
| `versions` | `GET /slimapi/versions`（**精确匹配**，2026-08-22 Q6 增补） | 版本发现端点（无上游 fan-out，`upIn` 常为 0） |
| `actions` / `write_actions` | `GET /slimapi/actions`（**精确**）/ `POST /slimapi/actions/{name}`（2026-08-22 Q6 增补） | action catalog 发现 / manifest action 调用（write_* 命名随 `write_session`/`write_question` 惯例） |
| `other` | 无专属桶的 `/slimapi/**` 残留（Q6 后仅剩子路径/错误方法 405 噪声等） | **非 passthrough**；Q6 前曾含 `permissions`/`versions`/`actions`——见 §3.3 Q6 注记 |
| `passthrough` | catch-all `/**`（**3.0.0 起已关闭**——未收编路径 404 `thin_route_not_found`） | **哨兵桶（3.0.0 起教学口径）**：catch-all 关闭后不再有 200 透传；本桶只剩 404/405 拒绝噪声，`upIn`/`upOut` 恒 0——**任何 2xx 出现即意外穿透，应排查**（「基线 ≈1.0」口径仅适用 ≤2.x 历史日志） |
| `health` / `metrics` / `other` | 各自端点 | 元数据/探活 |

### 3.3 新 bucket 口径（L1–L3 slim 整合，2026-08-15）

**Q6 注记（2026-08-22，owner 裁决）**：8h 生产日志实证 `other` 桶残留——`GET /slimapi/permissions` ×140（最高频）、`GET /slimapi/versions` ×53、`GET /slimapi/actions` ×7 + `POST /slimapi/actions/{id}` ×5。`bucketize()`（`traffic.py`）据此增补四个桶：`permissions` / `versions` / `actions`（GET **精确匹配**，镜像 health 先例 + write_question 的方法门控——非 GET 是 FastAPI 405，落 `other`）与 `write_actions`（POST `/slimapi/actions/{name}`，`write_*` 命名惯例）。子路径/错误方法保持 `other`。**下述 2026-08-15 的「permissions 归 `other`」口径自此成为历史**（增量观测枚举，不 bump wire）：

**（历史，≤ Q6 前）`/slimapi/permissions` 归 slim 侧（非 passthrough），但 bucketize 无独立 `permissions` 桶 → 落入 `other`**：

- `GET /slimapi/permissions`（跨目录 pending permission 聚合，镜像 `/slimapi/questions`）在 `bucketize()`（`traffic.py`）中**未设独立桶**——`/slimapi/**` 前缀命中后无 `permissions` 分支，回落到 `return "other"`（`traffic.py` line 91）。因此该端点流量与其它无专属桶的 `/slimapi/**` 端点一起计入 **`other`** 桶，**不**进 `passthrough`（`passthrough` 仅 catch-all `/**`）。
- **记账内容**：`upIn` = 聚合抓取的上游字节（发现 `/experimental/session` + 各 directory 的 `/permission` fan-out 成本，经 `stash_up_in` 计入）；`downOut` = 聚合 envelope 下发字节（白名单投影后）。
- **运维含义**：查"permissions 省了多少"不能在 `ratios.permissions` 找到独立比值——需从 `other` 桶读数，或按 access log 的 `bucket=="other"` + `path=="/slimapi/permissions"` 过滤（见 §5）。未来如需独立桶，可在 `bucketize()` 加 `permissions` 分支（纯 ops 面，不 bump wire）。

**当前 SSE 分桶**：

- `events_sse` 仅承载全局 `session.digest`、question/permission 直推与 control；
  `GET /slimapi/events?tokens=1` 已退役并返回 400
  `tokens_stream_retired_in_v4`，不会产生 token 增量流量。
- token 增量只走 `token_stream_sse`；两条 SSE 均恒 identity。`downOut` 按
  实际成功下发的 subscriber×frame 记账；共享上游 `upIn` 仍只计一次。
- 普通 subscriber queue 溢出清空并 STOP；控制面 reconciliation 由 replay/
  control resync 驱动。未成功下发的帧不计 `downOut`。

---

## 4. 怎么读"省了多少"

| 想知道 | 看哪里 | 怎么读 |
|---|---|---|
| **历史对话拉取省了多少** | `ratios.messages.downOutOverUpIn` | 单值；如 `0.2` = 下发只有成本的 20%（省 80%） |
| **SSE 策展省了多少** | `events_sse` 的 `upIn` vs `downOut` | 单订阅时 `downOut ≪ upIn`（digest 几百 B vs token 流几十 KB） |
| **passthrough 哨兵对照** | `ratios.passthrough` | 3.0.0 起 catch-all 关闭，该桶只剩 404/405 拒绝（`upIn`/`upOut` 恒 0、无有效比值）；**任何 2xx 出现即意外穿透告警点**。历史「透传基线 ≈1.0 验证账本无偏差」口径仅适用 ≤2.x 旧日志 |
| **累计节省字节** | 各桶 `upIn - downOut` 之和，或 `totals.upIn - totals.downOut` | 绝对量 |

### ⚠️ SSE fanout 例外（务必先读）

`events_sse` / `token_stream_sse` 桶的 `downOutOverUpIn` 是**聚合下发字节 / 共享上游成本**：
- `upIn` 只计**一条**共享 `/global/event` 上游连接（成本只算一次）。
- `downOut` 按**每个订阅者每帧**累加（N 个 ocdroid 设备连着 → 翻 N 倍）。

所以**多订阅 fanout 下该比值可 > 1.0**——这不是"省流失效"，而是"一份上游成本被扇出到多个客户端"。**真正的单连接省流证据 = 单订阅时 `downOut ≪ upIn`**。家里 2–5 台设备同连时，用 `downOut / 订阅数` 估算单设备省流比。

---

## 5. access log 离线分析

**每个 HTTP 请求一行 `recordType=="request"`**；另有 SSE 连接建立/断开各一行的 `sse_open`/`sse_close` 生命周期行（`events_sse`/`token_stream_sse` 两端点，见 §5.1）。**历史注记**：≤2.x 的 catch-all 透传 SSE（`/event`、`/global/event`，bucket=`passthrough`，`selectorResult=not_applicable`）也写过这对生命周期行（2026-08-16 起）；**3.0.0 起 catch-all 关闭，这些路径 404，不再产生任何生命周期行**——旧日志文件中仍可查到。按天切分文件 `access-YYYY-MM-DD.jsonl`（`YYYY-MM-DD` = 当天日期）。默认目录 `logs/`（相对服务 CWD）；生产 systemd 覆盖到 `~/.local/state/oc-slimapi/logs/`（见 `docs/operations.md` §5.2）。

```jsonc
{"ts":"2026-07-24T13:02:11+08:00","method":"GET","path":"/slimapi/messages/ses_x","bucket":"messages","status":200,"durationMs":12.3,"downIn":0,"downOut":4321,"upIn":20480,"upOut":0,"requestId":"a1b2c3...","client":"ocdroid","clientVer":"1.2.3","clientId":"5f4d3c2b1a098765"}
```

字段说明（`client`/`clientVer`/`clientId` 为 2026-07-29 加性字段；`wireVersion`/`selectorResult`/`directoryForm`/`recordType`/`lifecycleId` 为 2026-08-16 v3 Batch A 加性字段；缺省 `null`）：

| 字段 | 含义 |
|---|---|
| `ts` / `method` / `path` / `bucket` / `status` / `durationMs` | 请求元数据（`ts` 为请求**完成时刻**的时间戳——响应已发出的时间点，非开始时刻；`durationMs` 才是耗时） |
| `downIn` / `downOut` / `upIn` / `upOut` | 字节账本（见 §1） |
| `requestId` | `X-Request-ID`（跨 sidecar↔opencode 关联） |
| `client` | 客户端 app 名（来自 `X-Client-Name`，明文，**不 hash**） |
| `clientVer` | 客户端版本（来自 `X-Client-Version`，明文，**不 hash**） |
| `clientId` | 设备标识 hash（来自 `X-Client-Id`，默认 `sha256(raw)[:16]`；设 `OC_SLIMAPI_CLIENT_ID_SALT` 时为 `hmac_sha256(salt,raw)[:16]`） |
| `wireVersion` | `"2"` \| `"3"` \| `"4"` \| `null`——当前成功进入业务路由的值只有 `"4"`；`"2"`/`"3"` 是旧日志兼容维度。被拒、豁免与非 `/slimapi` 请求为 `null`。 |
| `selectorResult` | 当前生产者为 `v4`、`rejected`、`exempt`、`not_applicable`；`absent`/`v2`/`v3` 只为历史日志与 frozen matrix 解读保留。`not_applicable` 当前主要表示终局 404/405 边界，不表示可用 catch-all。 |
| `directoryForm` | `query` \| `header` \| `both` \| `absent` \| `null`——记录 selector 看到的输入形态；当前消费方只应发送 query。header/both 可出现在被拒记录中，不能据此推断 header 仍受支持。 |
| `recordType` | `request`（每 HTTP 请求一行）\| `sse_open` \| `sse_close`（SSE 建立断开标记行）。**消费口径：统计请求数/字节时必须过滤 `recordType=="request"`** |
| `lifecycleId` | 进程内单调递增 int；同一条 SSE 连接的 `sse_open`/`sse_close` 行同值（配对键）。仅生命周期行有值，`request` 行为 `null`。`requestId` 在 SSE 重连时可复用，仅辅助关联，**配对以 `lifecycleId` 为准** |
| `sessionsSource` | `"db"` \| `"http"` \| 缺席——v4 `GET /slimapi/sessions` 数据面来源（4.0.0 加性稀疏字段：DB 投影源成功→`"db"`；降级矩阵 Class A HTTP 降级 200→`"http"`；v3 路径/其他路由/被拒请求**字段缺席**=否定语义，不写 `null`）。配套聚合：ledger 矩阵键 `degraded\|<kind>\|<statusClass>\|<bucket>`（kind=`http`/`fail_closed`）、`/slimapi/metrics` `sessionsDegraded` 块（`degraded_200`/`fail_closed_503` 按响应逐次计数）、snapshot `v4.degradedMatrix` |
| `degraded503` | `true` \| 缺席——v4 sessions fail-closed 503（`auxiliary_unavailable` 族）标记（4.0.0 加性稀疏字段；**仅 503 降级写 `true`，永不写 `false`**；TransformBusy 拥塞 503 不属降级语义，不置位；v3/其他路由缺席）。与 `sessionsSource` 互斥使用：503 时两者同现（`"http"`+`true`） |

### 5.1 SSE 生命周期行（2026-08-16 加性）

`GET /slimapi/events` 与 `GET /slimapi/sessions/{sid}/stream`（`token_stream_sse`）在流真正开始产出（200 + `text/event-stream` 确立）时写一行 `sse_open`，生成器退出（客户端断开/服务端终结）时写一行 `sse_close`。**catch-all 透传 SSE（≤2.x 历史）**：`/event`、`/global/event`（bucket=`passthrough`）在 2.x 同样写过这对生命周期行（`selectorResult=not_applicable`、`wireVersion=null`）；**3.0.0 起 catch-all 关闭（404），不再产生**——其判定口径=**响应性质**：上游响应 200 且 content-type 为 `text/event-stream`（容忍 charset 等参数）才算 SSE——404/503/JSON 等非 SSE 响应**无生命周期行**，仅按普通 request 行记账。close 行在生成器 teardown 的 finally 中、于任何 `await` 之前写入——aclose 失败/取消路径 close 行必达（open/close 配对不泄漏）。两行**不含**字节/耗时字段（`downIn`/`downOut`/`upIn`/`upOut`/`durationMs` 不出现——生命周期行是标记，不是账目；字节记在该连接的 `request` 行与 ledger 桶里），字段为：`ts`/`method`/`path`/`bucket`/`status`/`recordType`/`lifecycleId`/`wireVersion`/`selectorResult`/`directoryForm`/`requestId`。旧文件没有这些行 → 分析脚本须容忍缺失（jq 对缺 key 的 `select` 天然跳过）。

### 文件切分与压缩

- **按天切分**：每天一个 `access-YYYY-MM-DD.jsonl`，跨天自动切新文件。
- **启动压缩**：服务启动时把**早于今天的**（日期 `< today`）未压缩 `.jsonl` 原子压缩为 `.gz`（写 `.gz.tmp` → rename → 删源）。当天文件不压缩（活跃写入中）。
- **legacy 迁移（仅当前目录）**：`migrate_legacy_access_log` 只处理**当前 `access_log_dir` 内**的无日期文件（旧 `access.jsonl` / `access.jsonl.N`），按 mtime 归档为 `access-legacy-{mtimeYYYYMMDD}-{N}.jsonl.gz`。**不跨目录迁移**：生产部署从旧相对目录（如 cwd 下 `logs/`）升级到 `StateDirectory`（`~/.local/state/oc-slimapi/logs`）时，旧位置的历史日志**不会自动迁移**——运维需手动移动（历史日志的清理也由运维处理）。
- **`access-legacy-*.jsonl.gz` 纳入 retain 自动清理**：prune 先严格匹配 `access-YYYY-MM-DD.jsonl(.gz)`，迁移产出的 legacy 档案（`access-legacy-YYYYMMDD-*.jsonl.gz`）按**名内归档日期**纳入 `RETAIN_DAYS` 同一保留窗口自动清理（同一判据、同一边界）。
- **后台 maintenance**（默认 1h）：周期 compress + prune（`OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS`，默认 `0`=不删）。
- 读取压缩历史文件用 `zcat access-2026-07-23.jsonl.gz | jq ...` 或 `jq` 直接读管道。

### 常用分析（`jq`）

> 2026-08-16 起 access log 混有 SSE 生命周期行——**字节/请求数统计一律先 `select(.recordType == "request")`**（旧文件无该字段，容错写法 `select(.recordType != "sse_open" and .recordType != "sse_close")`）。

```bash
# 当天文件
LOG=logs/access-$(date +%F).jsonl

# 各桶累计 upIn / downOut / 省流字节（按成本降序）
jq -s 'map(select(.recordType == "request")) | group_by(.bucket)
       | map({bucket:.[0].bucket,
              upIn:(map(.upIn)|add),
              downOut:(map(.downOut)|add)})
       | map(.saved=(.upIn - .downOut))
       | sort_by(-.upIn)' "$LOG"

# 某时段（按 ts 前缀，如某小时）的累计省流
jq -s 'map(select(.recordType == "request" and .ts >= "2026-07-24T13" and .ts < "2026-07-24T14"))
       | {upIn:(map(.upIn)|add), downOut:(map(.downOut)|add)}' "$LOG"

# 慢请求 Top 10（按 durationMs）
jq -s 'map(select(.recordType == "request")) | map({path,durationMs,bucket,status}) | sort_by(-.durationMs) | .[0:10]' "$LOG"

# 非 2xx 请求
jq -s 'map(select(.recordType == "request" and .status >= 400)) | length' "$LOG"

# 按设备 hash 分组请求量（区分多设备）
jq -s 'map(select(.recordType == "request")) | group_by(.clientId) | map({clientId:.[0].clientId, count:length, client:.[0].client, clientVer:.[0].clientVer})' "$LOG"

# v3 观测：按 selectorResult 分组请求量（v3 采用率速览）
jq -s 'map(select(.recordType == "request")) | group_by(.selectorResult) | map({selectorResult:.[0].selectorResult, count:length})' "$LOG"

# v3 观测：SSE 活跃连接核对（open/close 按 lifecycleId 配对）
jq -s 'map(select(.recordType == "sse_open" or .recordType == "sse_close"))
       | group_by(.selectorResult)
       | map({selectorResult:.[0].selectorResult,
              opens:(map(select(.recordType == "sse_open"))|length),
              closes:(map(select(.recordType == "sse_close"))|length)})' "$LOG"

# 跨多天（含压缩历史）汇总
for f in logs/access-2026-07-2*.jsonl logs/access-2026-07-2*.jsonl.gz; do
  [ -f "$f" ] || continue
  case "$f" in *.gz) zcat "$f";; *) cat "$f";; esac
done | jq -s 'map(select(.recordType == "request")) | group_by(.bucket) | map({bucket:.[0].bucket, upIn:(map(.upIn)|add)})'
```

> `jq -s` 把整个文件 slurp 进内存（适合单文件几 MB 级）。大文件可改用 `jq` 逐行 + `awk` 求和，或导出给其它工具。

### 跟 `metrics.traffic` 块对不上？

access log 的 `downOut` 是 **wire 级**字节（中间件视角，含 SSE 连接的真实响应体）；而 ledger 的 SSE 桶 `downOut` 是 **per-subscriber-per-frame 聚合**（见 §4 fanout）。两者口径不同，**不应直接对照**。SSE 统计以 `GET /slimapi/metrics` 响应的 `traffic` 块为准；逐请求审计以 access log 为准。

---

## 6. 配置（环境变量）

| env | 默认 | 作用 |
|---|---|---|
| `OC_SLIMAPI_TRAFFIC_METRICS_ENABLED` | `1` | 内存账本总开关；`0` 时 `traffic`=`{enabled:false}`、所有 `record_*` no-op |
| `OC_SLIMAPI_ACCESS_LOG_ENABLED` | `1` | access log 落盘开关；`0` 时不建文件、纯 no-op |
| `OC_SLIMAPI_ACCESS_LOG_DIR` | `logs` | access log 目录（按天文件 `access-YYYY-MM-DD.jsonl` 落在其下；父目录 best-effort 创建）。生产 systemd 覆盖为 `%S/oc-slimapi/logs` |
| `OC_SLIMAPI_ACCESS_LOG_COMPRESS_ON_STARTUP` | `1` | 启动时压缩早于今天（`< today`）的 `.jsonl` → `.gz` |
| `OC_SLIMAPI_ACCESS_LOG_RETAIN_DAYS` | `0` | prune 早于 N 天的 `access-YYYY-MM-DD.jsonl(.gz)`；**代码默认 `0`=不删**（本地开发/测试）；**生产 unit 配置 `3`**（见 `deploy/oc-slimapi.service` / `docs/operations.md` §3.2）。`access-legacy-YYYYMMDD-*.jsonl.gz` 按名内归档日期同一判据清理 |
| `OC_SLIMAPI_ACCESS_LOG_MAINTENANCE_INTERVAL_S` | `3600` | 后台 compress+prune 周期（≥60） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_ENABLED` | `1` | 内存账本周期快照开关（见 §9） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_INTERVAL_S` | `300` | 快照周期（≥1） |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` | `logs/traffic-snapshot.jsonl` | 快照文件名 stem（按天生成 `<stem>-YYYY-MM-DD.jsonl`）；生产 systemd 覆盖为 `%S/oc-slimapi/logs/traffic-snapshot.jsonl` |
| `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS` | `0` | prune 早于 N 天的 `traffic-snapshot-YYYY-MM-DD.jsonl(.gz)`；**代码默认 `0`=不删**（本地开发/测试）；**生产 unit 配置 `30`**（见 `deploy/oc-slimapi.service` / `docs/operations.md` §3.2/§5.3）。snapshotter 循环每 tick 顶部自持 prune，**不受 `ACCESS_LOG_ENABLED` 影响**，边界（`today - retain_days`）保留；与 access-log 不同，**不压缩**仅按天清理 |
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
- **`totals` 跨桶字节口径异质**：curated 桶是解码后逻辑字节（≤2.x 旧日志的 `passthrough.upIn` 曾是 gzip wire 字节——3.0.0 起 catch-all 关闭后该桶不再产生流量，见 §3.2 哨兵口径）。`totals` 粗糙，**per-bucket `ratios` 比 `totals` 更有意义**。

---

## 9. 内存账本周期快照（`traffic-snapshot-YYYY-MM-DD.jsonl`）

> **2026-07-29 加性 ops 面**。`GET /slimapi/metrics` 响应的 `traffic` 块是内存账本的实时快照（per-process、in-memory），进程重启即清零。`TrafficSnapshotter` 周期（默认 300s）把 ledger 的 **cumulative 视角**写入 JSONL 文件，用于跨重启的长期趋势分析。

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
  "buckets": { /* 同 metrics 响应 .traffic.buckets，cumulative */ },
  "totals": { /* 同 metrics 响应 .traffic.totals */ },
  "ratios": { /* 同 metrics 响应 .traffic.ratios */ },
  "v3": { /* 2026-08-16 加性：v3 观测节，见 §9.4 */ }
}
```

- **cumulative 语义**：`buckets`/`totals` 是该进程**自启动以来**的累计值（含 SSE 真实成本 `upIn`/`downOut`），即 `GET /slimapi/metrics` 响应 `traffic` 块的内存账本逐字段 dump。
- **跨进程分段**：进程重启后 `bootTs`/`runId` 变化、`uptimeS` 归零 → 新的一段 cumulative。分析侧通过 `bootTs`/`runId` 识别进程边界，对相邻两帧算 delta 得到该段时间增量。
- **跨天切分**：日期变更时自动切新文件 `traffic-snapshot-YYYY-MM-DD.jsonl`（当天文件 append 写入，不压缩）。**snapshot 文件不经 access log 的 compress**（后者只认 `access-` 前缀），但 **F-009 起由 snapshotter 循环每 tick 顶部自持清理**（`OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS`，默认 `0`=不删；生产 unit 配置 `30`；**不受 `ACCESS_LOG_ENABLED` 影响**）：删除早于 N 天的 `traffic-snapshot-YYYY-MM-DD.jsonl(.gz)`（边界 `today - retain_days` 保留）。prune 失败仅告警，不中断循环。
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

### 9.3 与 access log / `metrics.traffic` 块的口径差异

| 来源 | 口径 | 重启后 |
|---|---|---|
| `GET /slimapi/metrics` 响应 `traffic` 块 | 内存账本实时快照（per-process cumulative；含加性 `v3` 节） | 清零 |
| `traffic-snapshot-YYYY-MM-DD.jsonl` | 内存账本周期 dump（同 `metrics.traffic` 块，持久化；按天切分；**不经自动压缩**；Task 10 起按 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS` 自动 prune，默认 `0`=不删） | 新段（`bootTs`/`runId` 变化） |
| access log（`access-*.jsonl`） | **逐请求** wire 级字节（含 `requestId`/`client*`/v3 观测字段）+ SSE 生命周期行 | 不受影响（按天文件） |

三者为**不同口径**，不直接对照：snapshot 是聚合 cumulative，access log 是逐请求明细，`metrics.traffic` 块是实时聚合。分析"某时段省了多少"用 access log（§5）；分析"长期趋势 / 跨重启累计"用 snapshot。

### 9.4 历史命名保留：`v3` 观测节（2026-08-16 加性）

`metrics.traffic.v3` 与每帧 snapshot 的 `v3` 键（同源，均来自内存 ledger）。
这里的 `v3` 是冻结的 **ops schema label**，不是 v3 wire 支持声明：

```jsonc
{
  "matrix": {                       // 扁平键 "selectorResult|wireVersion|directoryForm|recordType|statusClass|bucket" → 计数
    "v2|2|null|request|2xx|sessions": 42,
    ...
  },
  "sseLifecycle": {                 // 按 SSE 可达五维 {v2,v3,v4,absent,not_applicable}
    "v3": { "opens": 3, "closes": 2, "active": 1, "orphanCloses": 0 },
    ...
  },
  "sseActive": { "v2": 0, "v3": 1, "v4": 0, "absent": 0, "not_applicable": 0 }  // 当前活跃（= sseLifecycle[k].active）
}
```

- **matrix** 维度 = `selectorResult × wireVersion × directoryForm × recordType × statusClass × bucket` 计数矩阵（access log 离线聚合另含 date）；`statusClass` 形如 `"2xx"`，无 status 时 `"none"`。`v2`/`v3`/`absent` 枚举只为旧文件对账保留，当前不产出成功旧 wire 流量。
- **sseActive 语义**：`v4` = `?v=4` SSE（4.0.0 起维度，与 `selectorResult` 的 `v4` 取值同源）；`not_applicable` = catch-all 透传 SSE（≤2.x 历史维度：`/event`、`/global/event` 曾不经省流面但计入观测；**3.0.0 起 catch-all 关闭，该维度不再增长**）；`absent` = 无 `v` 参数的旧客户端 SSE。`rejected`/`exempt` 无 SSE 端点，恒不出现。**3.0.0 终态：`v2`/`absent` 维度自然归零（旧客户端开流前即 400）；五维枚举（4.0.0 起含 `v4`）保留供历史帧对账与退役判据（§9.3）核验。**
- **离线对账**：跨日 carry-in 公式 `sseActive[D+1,k] = sseActive[D,k] + sse_open[D,k] − matched_sse_close[D,k]`（`sseActive[D,k]` 取 D 日首行时的窗口起点存量）。**matched/orphan 配对按 `lifecycleId`**（§11.8）：close 行的 `lifecycleId` 能配到先前未匹配 open（同 dim、跨日 carry）→ `matched_sse_close`（计入 close 当日，并从待配对集合移除）；配不到（open 早于数据窗口 / sidecar 重启后集合已空 / id 缺失）→ **孤儿 close**，只补记计数，**不冲减存量**（避免错误消耗他人活跃连接）。`traffic_snapshot.aggregate_v3_observability(records)` 提供该纯函数实现（输入按天 access log 解析出的记录列表，输出 `counts`/`countsByDate`/`sseActive`/`sseOpens`/`sseMatchedCloses`/`sseOrphanCloses`/`sseLive`），供运维脚本与测试对账复用。

---

## 10. 相关

- 当前 wire / consumer：[`docs/specs/v4-contract.md`](../specs/v4-contract.md)、[`docs/specs/PROTOCOL.md`](../specs/PROTOCOL.md)；`v2-contract.md` / `v3-contract.md` 仅用于解释旧日志格式的来源
- 运维手册：[`docs/operations.md`](../operations.md)（§5 日志策略、§5.2 落盘目录、§5.3 维护）
- 变更记录：[`CHANGELOG.md`](../CHANGELOG.md) `[0.7.0]`（access log + traffic 首版）、`[1.0.0]`（按天切分 + client header + snapshot）
