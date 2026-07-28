# 流量消耗排查报告（一次性快照 + 分析）

> **性质**：一次性数据快照 + 归因分析，供项目组排查流量来源。**非契约、非手册**。与 [`traffic-accounting.md`](traffic-accounting.md)（用法手册）互补但独立；口径定义以该手册为准。
> **数据快照时间**：2026-07-29 07:24（CST，本地）
> **日志窗口**：`2026-07-28 17:00` → `2026-07-29 07:23`（约 **14.4 小时**，214,085 行有效请求）
> **数据来源**：`logs/access.jsonl`（主）+ `logs/access.jsonl.1` ~ `access.jsonl.5`（legacy `RotatingFileHandler`，10MB×5）。本快照采于按天切分改造上线前的 legacy 格式（见 §5「日志位置索引」）。

---

## 1. 概述

本报告回答一个问题：**oc-slimapi 的上游流量成本花在哪了？**

方法：在 `logs/` 目录实跑流式聚合脚本（见 §6），把逐请求 access log 按 **路由桶 / 路径模板 / 小时** 聚合成 `upIn`（从 opencode 拉的字节，即真实成本）与 `downOut`（下发给 ocdroid 的字节，即省流后）。

> **口径提醒（先读再用数）**
> - access log 的 `upIn`/`downOut` 是**逐请求**口径，对普通 HTTP 请求即真实成本。
> - **SSE 是盲区**：`events_sse` / `token_stream_sse` 桶的 `upIn` 在 access log 里是**共享上游连接**口径（本快照中甚至为 `0.00MB`，见 §3），**看不到 SSE 的真实上游成本**。SSE 真实成本只能查内存账本 `/slimapi/metrics.traffic`（见 §5）。本报告所有"总成本"数字**不含 SSE 真实上游成本**。

---

## 2. 数据快照（实跑脚本输出）

脚本实跑时间 2026-07-29 07:24，命令与输出原样如下（数字均来自本次实跑，未沿用任何旧快照）。

### 2.1 总量

| 指标 | 值 |
|---|---|
| 请求总数 | **214,085** |
| `upIn`（上游成本） | **2778.33 MB** |
| `downOut`（下发） | **512.74 MB** |
| 省流字节 | **2265.58 MB** |
| 省流比 | **81.5%** |
| 时间窗口 | 2026-07-28 17:00 → 2026-07-29 07:23（≈14.4h） |

> 脚本首行打印的 `RANGE 2026-07-29T00:21 -> 2026-07-28T18:03` 是**文件读取顺序产物**（脚本先读活跃文件 `access.jsonl`，再依次读 `.1`→`.5`，故 `first_ts` 取到活跃文件首行、`last_ts` 取到 `.5` 末行，时序被打乱）。**真实窗口以 §3.3 小时分布的首末 bucket 为准**：`07-28T17` → `07-29T07`，与各文件 mtime（活跃文件 07-29 07:23、`.5` 07-28 18:03）一致。

### 2.2 按路由桶（按 `upIn` 降序）

| 桶 | 请求数 | upIn | downOut | downOut/upIn |
|---|---:|---:|---:|---:|
| `messages` | 25,132 | **2445.24 MB** | 462.48 MB | 0.19 |
| `sessions` | 4,161 | 306.21 MB | 21.15 MB | 0.07 |
| `passthrough` | 183,178 | 26.85 MB | 26.85 MB | 1.00 |
| `quiz` | 676 | 0.02 MB | 0.04 MB | 2.28 |
| `token_stream_sse` | 648 | 0.00 MB | 0.28 MB | — |
| `events_sse` | 144 | 0.00 MB | 1.92 MB | — |
| `health` | 61 | 0.00 MB | 0.01 MB | — |
| `metrics` | 1 | 0.00 MB | 0.00 MB | — |
| `other` | 84 | 0.00 MB | 0.00 MB | — |

### 2.3 按路径模板 Top 12（按 `upIn` 降序）

| 路径模板（已归一 session/message id） | 请求数 | upIn | downOut |
|---|---:|---:|---:|
| `/slimapi/messages/ses_*`（list 全量拉取） | 24,871 | **2227.79 MB** | 452.96 MB |
| `/slimapi/sessions` | 1,329 | 306.15 MB | 21.03 MB |
| `/slimapi/messages/ses_*/since/0`（cursor=0 全量） | 105 | 56.35 MB | 7.61 MB |
| `/slimapi/messages/ses_*/since/1785239940707` | 9 | 19.30 MB | **0.01 MB** |
| `/session/ses_*/children`（passthrough） | 164,633 | 17.00 MB | 17.00 MB |
| `/slimapi/messages/ses_*/since/1785235400468` | 10 | 13.92 MB | 0.01 MB |
| `/slimapi/messages/ses_*/since/1785240776709` | 7 | 11.27 MB | 0.10 MB |
| `/slimapi/messages/ses_*/since/1785242473958` | 4 | 8.75 MB | 0.00 MB |
| `/slimapi/messages/ses_*/since/1785239629417` | 4 | 6.82 MB | 0.01 MB |
| `/slimapi/messages/ses_*/since/1785242478265` | 6 | 6.70 MB | 0.03 MB |
| `/command`（passthrough） | 64 | 6.60 MB | 6.60 MB |
| `/slimapi/messages/ses_*/since/1785239684989` | 3 | 6.20 MB | 0.00 MB |

### 2.4 按小时

| 小时（本地） | 请求数 | upIn | downOut |
|---|---:|---:|---:|
| 07-28 17:00 | 37,856 | 124.98 MB | 19.72 MB |
| 07-28 18:00 | 44,258 | 212.47 MB | 31.41 MB |
| **07-28 19:00** | **57,789** | **1000.19 MB** | 194.37 MB |
| 07-28 20:00 | 30,354 | 712.08 MB | 124.05 MB |
| 07-28 21:00 | 7,770 | 121.54 MB | 21.34 MB |
| 07-28 22:00 | 7,114 | 62.14 MB | 11.39 MB |
| 07-28 23:00 | 15,230 | 99.56 MB | 20.11 MB |
| 07-29 00:00 | 9,484 | 324.26 MB | 72.37 MB |
| 07-29 06:00 | 3,085 | 64.75 MB | 10.56 MB |
| 07-29 07:00 | 1,145 | 56.35 MB | 7.43 MB |

> 01:00–05:00 无数据（服务/客户端静默）。

### 2.5 状态码分布与错误 Top

```
STATUS {200: 212940, 400: 1137, 500: 8}
```

| 次数 | 错误（method + 归一 path + status） |
|---:|---|
| 637 | `GET /slimapi/sessions/ses_*/status 400` |
| 176 | `GET /slimapi/messages/ses_* 400` |
| 91 | `GET /slimapi/events 400` |
| 83 | `GET /slimapi/permissions 400` |
| 83 | `GET /slimapi/sessions 400` |
| 52 | `GET /slimapi/sessions/ses_*/stream 400` |
| 7 | `POST /question/que_*/reject 404` |
| 5 | `POST /session/ses_*/summarize 500` |

---

## 3. 成本来源分析

### 3.1 按桶：成本几乎全在 `messages`

- **`messages` 桶 2445.24 MB = 总成本 2778.33 MB 的 88.0%**。省流比 0.19（下发 462 MB），是省流的绝对主力，同时也是成本的绝对主力。
- `sessions` 桶 306.21 MB（11.0%），省流比 0.07（投影很激进，下发仅 21 MB）。
- `passthrough` 桶 26.85 MB（<1%），比值 1.00（透传不省流，作为账本基线对照，数值正常）。
- `events_sse` / `token_stream_sse` 的 `upIn` 显示为 `0.00 MB` —— 这是 access log 的 **SSE 盲区**（见 §1 口径提醒），不代表 SSE 没成本。

### 3.2 按路径：单个热 session 反复全量拉取是成本核心

- **`/slimapi/messages/ses_*`（list）一项 2227.79 MB = 总成本的 80.2%**。24,871 次请求集中在少数热 session 上反复全量拉取历史对话，骨架投影后下发 452.96 MB（省 ~80%），但因**反复全量**，绝对成本仍是最高的。
- **cursor 浪费铁证**：一批 `/slimapi/messages/ses_*/since/<旧时间戳>` 请求的 `upIn` 很高、`downOut` 几乎为 0：
  - `/since/1785239940707`：9 次 → upIn **19.30 MB**、downOut **0.01 MB**
  - `/since/1785235400468`：10 次 → upIn 13.92 MB、downOut 0.01 MB
  - `/since/1785242473958`：4 次 → upIn 8.75 MB、downOut 0.00 MB
  - `/since/1785240776709`、`/since/1785239629417`、`/since/1785242478265`、`/since/1785239684989` 同型。
  - 含义：客户端用一个**旧 cursor**向 sidecar 要"自该点之后的新消息"，sidecar 向上游拉了一大段历史（付了 upIn 成本），但骨架投影后真正需要下发的新骨架 ≈ 0（`downOut`≈0）——**上游成本白付**。这是客户端 cursor 管理 / 回退 / 首次同步策略的典型症状。
- **`/slimapi/messages/ses_*/since/0`（cursor=0 全量拉）**：105 次、56.35 MB。cursor=0 即"从头拉全部"，单次量大，且 105 次说明被反复触发（疑似冷启动 / 重装 / 切 directory 重拉）。
- `/session/ses_*/children`（passthrough）：**164,633 次**、17.00 MB。单请求字节很小（透传不省流），但**请求次数异常密集**（窗口内 16 万+ 次），疑似客户端 polling 过密（见 §4）。

### 3.3 按小时：晚间单小时冲到 1000 MB

- **07-28 19:00 单小时 upIn 1000.19 MB**（占整窗 36%），是绝对峰值；20:00 紧随 712.08 MB。这两个小时合计占 61.7%。
- 07-29 00:00 出现次级尖峰 324.26 MB（请求仅 9,484），疑似深夜重连 / 同步风暴。
- 01:00–05:00 静默（无请求），06:00 起恢复低流量。

---

## 4. 关键发现

整合"已验证历史洞察"与本次实跑数据对照（高度一致，仅在范围与新现象上补充）：

1. **成本结构稳定**：总量 2778 MB（历史 2693 MB）、省流 81.5%（历史 81.4%）、`messages` 桶占 88%（历史 89%）、`sessions` 占 11%（历史 10%）、`passthrough` <1%——**全部吻合**，说明成本归因稳定可复现。

2. **成本核心 = 热历史对话反复全量拉取**：`/slimapi/messages/ses_*`（list）一项独占 80% 成本。骨架投影本身省流有效（0.19），但"反复全量"放大了绝对成本。优化方向不在投影，在**客户端拉取策略**（增量、去重、限频）。

3. **cursor 回退浪费首次量化到请求级**：`/since/<旧时间戳>` 系列（9~10 次 × 十几 MB，`downOut`≈0）是"上游成本白付"最直接的证据。历史只笼统说"明显浪费"，本次拿到逐请求数字。**指向 ocdroid cursor 管理 bug 或回退路径**（如重连后用旧 watermark 重拉）。

4. **`/since/0` 全量拉 105 次**：cursor=0 即全量。105 次不是偶发，疑似冷启动 / 切 directory / 重装的固定行为，每次都付一次全量成本。

5. **`/session/*/children` 请求密度异常**：164,633 次/14.4h ≈ **190 次/分钟**，纯 passthrough、字节小（17 MB），但请求量是 `messages` 的 6.6 倍。疑似客户端轮询过密（polling 节流候选）。

6. **时间峰值复现 19:00 单小时 1000 MB**：与历史观测精确一致。属使用时段集中（晚间活跃），非异常突发。

7. **SSE 盲区确认**：access log 的 `events_sse` `upIn`=0.00 MB，**看不到真实 SSE 上游成本**。历史曾从内存账本测得"270 MB / ~46 min、策展省流 99.9%"。本快照**未含 SSE 真实成本**（`logs/traffic-snapshot.jsonl` 尚未生成，见 §5），如需评估 SSE 长期成本须另起内存账本采样（见 §7）。

8. **400 错误面比历史更广**：历史只记 `status` 400 居多（637 次）；本次发现 400 共 **1,137 次**，分布在 **6 个 `/slimapi/**` GET 端点**（`status` 637 / `messages` 176 / `events` 91 / `permissions` 83 / `sessions` 83 / `stream` 52）。集中在一批 `/slimapi/` GET 上、且全是 400（非 404/500），**疑似客户端缺/错版本头 `X-Slimapi-Version` 或参数校验系统性问题**，而非偶发。另有 8 个 500（含 `POST /session/*/summarize` 5 次，上游侧错误）。

### 与历史洞察不一致 / 新现象

- **无矛盾**，量级与结构均吻合。
- **新现象 1**：cursor 浪费首次拿到逐请求级数字（`downOut`≈0 的 `/since/<旧 ts>`），可直接定位为客户端侧问题。
- **新现象 2**：400 错误不止 `status`，而是**一批 `/slimapi/**` GET 的系统性 400**，建议作为独立排查项（见 §7）。
- **新现象 3**：07-29 00:00 次级尖峰（324 MB / 9k 请求），历史未提及，疑似深夜重连。

---

## 5. 日志位置索引（排查数据源速查）

> 排查流量时按此表定位数据源。**所有路径/命令均已在仓库核对存在。**

### 5.1 access log（逐请求，离线审计主源）

- **当前位置/格式（本快照态）**：`logs/access.jsonl`（主）+ `logs/access.jsonl.1` ~ `access.jsonl.5`。legacy `RotatingFileHandler`，单文件 10 MB、保留 5 份，约覆盖最近 ~13.5–14.4 小时，**会轮转覆盖最老**。
- **当前字段（实跑核对）**：`ts, method, path, bucket, status, durationMs, downIn, downOut, upIn, upOut, requestId`。
- **变更预告（仓库已落地、运行态尚未切换）**：将改为**按天切分** `logs/access-YYYY-MM-DD.jsonl`；服务启动时把非当天 `.jsonl` 原子压缩为独立 `.jsonl.gz`（`.gz.tmp`→rename→删源），后台周期 compress + `retain_days` 清理；并**新增字段** `client`（app 名，明文）/ `clientVer`（版本，明文）/ `clientId`（设备 id，默认 `sha256(raw)[:16]`，可配 `OC_SLIMAPI_CLIENT_ID_SALT` 改为 `hmac_sha256`）。实现见 `src/oc_slimapi/access_log.py`（`_ACCESS_LOG_RE`、`DailyAccessLogHandler`、`prune_old_access_logs`、`migrate_legacy_access_logs`）。本快照的 `logs/` 仍是 legacy 格式（无 client 三字段），后续复跑会落在按天文件上。

### 5.2 内存账本周期快照（进程重启前的唯一回溯）

- **目标文件**：`logs/traffic-snapshot.jsonl` —— cumulative 总量快照，**含 SSE 真实上游成本**。
- **状态（实跑核对）**：**本快照时刻该文件尚未生成**（`ls logs/traffic-snapshot.jsonl` → 不存在）。该能力为 2026-07-29 加性改造（实现 `src/oc_slimapi/traffic_snapshot.py`），上线后每行带 `ts / bootTs / runId / uptimeS / pid`，用于跨重启分段。**进程重启后内存账本清零，此文件是回溯唯一来源。**

### 5.3 实时查询（进程内内存账本）

所有 `/slimapi/**` 端点须带版本头 `X-Slimapi-Version: 2`。

```bash
# 本机 loopback（服务默认 0.0.0.0:4097）
curl -s -H "X-Slimapi-Version: 2" http://127.0.0.1:4097/slimapi/metrics | jq '.traffic'

# 仅各桶字节 / 仅省流比 / 累计 totals
curl -s -H "X-Slimapi-Version: 2" http://127.0.0.1:4097/slimapi/metrics | jq '.traffic.buckets'
curl -s -H "X-Slimapi-Version: 2" http://127.0.0.1:4097/slimapi/metrics | jq '.traffic.ratios'
curl -s -H "X-Slimapi-Version: 2" http://127.0.0.1:4097/slimapi/metrics | jq '.traffic.totals'
```

- **远程 mTLS**：`$BASE=https://opencode.vectory.cn:14097`，`curl` 带 `--cert`/`--key`/`--cacert`（复用既有客户端证书，SAN=`opencode.vectory.cn`）。
- **SSE 真实成本看这里**（`events_sse`/`token_stream_sse` 桶的 `upIn`），**不看 access log**。

### 5.4 相关手册与规约

| 文件 | 用途 |
|---|---|
| [`docs/manual/traffic-accounting.md`](traffic-accounting.md) | 字段 / 桶 / 口径 / 配置 env 详解（§3 桶定义、§4 SSE fanout 例外、§5 jq 片段） |
| [`docs/specs/INTERFACE_MAP.md`](../specs/INTERFACE_MAP.md) | 端点级实现追踪 |
| [`docs/specs/v2-contract.md`](../specs/v2-contract.md) | §2 `/slimapi/metrics`、§6 资源限制（契约权威） |

### 5.5 systemd 部署下的日志落点

- 部署单元：`deploy/oc-slimapi.service`（systemd user）。
- 生产日志落在 `StateDirectory`：`~/.local/state/oc-slimapi/logs/`（即 `$XDG_STATE_HOME/oc-slimapi/logs/`）。
- 本地开发：相对服务 CWD 的 `logs/`（即本快照目录）。
- 运维 / 日志 / 自启 / 排障见 [`docs/operations.md`](../operations.md)（§5.2 日志路径、§10 边界验证）。

### 5.6 口径提醒（务必读，避免误读）

1. **access log 的 SSE `downOut` 是 wire 级**（实际发到 socket 的字节）；**内存账本的 SSE `downOut` 是 per-subscriber-per-frame 聚合**（fanout 时可 >1）。**两者不可直接对照。**
2. **SSE 统计以 `/slimapi/metrics.traffic` 为准**；**逐请求审计以 access log 为准**。
3. access log 的 SSE `upIn` 是**共享上游连接**口径（本快照中甚至记为 0），**不等于** SSE 真实上游成本。
4. 多订阅 fanout 下，`events_sse` 的 `downOutOverUpIn` 可 > 1.0，这不是"省流失效"，而是"一份上游成本扇出到多设备"（详见 traffic-accounting.md §4）。

---

## 6. 分析方法（可复现）

### 6.1 本报告所用流式聚合脚本

在 `logs/` 目录执行（流式，避免 slurp 内存爆炸；归一 session/message id 后聚合）：

```bash
cd /home/mar/personal_projects/oc-slimapi/logs && python3 - <<'PY'
import json, glob, collections, re
files = ["access.jsonl"] + sorted(glob.glob("access.jsonl.*"))
bucket = collections.defaultdict(lambda: dict(req=0, upIn=0, downOut=0, downIn=0, upOut=0))
path_tmpl = collections.defaultdict(lambda: dict(req=0, upIn=0, downOut=0))
hour = collections.defaultdict(lambda: dict(upIn=0, downOut=0, req=0))
errors = collections.Counter(); status_dist = collections.Counter()
total_upIn = total_downOut = 0; n = bad = 0; first_ts = last_ts = None
def norm_path(p):
    p = re.sub(r'/ses_[A-Za-z0-9]+', '/ses_*', p)
    p = re.sub(r'/msg_[A-Za-z0-9]+', '/msg_*', p)
    p = re.sub(r'/[A-Za-z0-9]{20,}', '/*', p); return p
for f in files:
    try: fh = open(f)
    except FileNotFoundError: continue
    for line in fh:
        line=line.strip()
        if not line: continue
        try: r=json.loads(line)
        except: bad+=1; continue
        n+=1; b=r.get("bucket","?")
        ui=int(r.get("upIn",0) or 0); do=int(r.get("downOut",0) or 0)
        bk=bucket[b]; bk["req"]+=1; bk["upIn"]+=ui; bk["downOut"]+=do
        bk["downIn"]+=int(r.get("downIn",0) or 0); bk["upOut"]+=int(r.get("upOut",0) or 0)
        total_upIn+=ui; total_downOut+=do
        ts=r.get("ts","")
        if ts:
            if not first_ts: first_ts=ts
            last_ts=ts; hp=ts[:13]; h=hour[hp]; h["upIn"]+=ui; h["downOut"]+=do; h["req"]+=1
        st=int(r.get("status",0) or 0)
        status_dist[st//100*100 if st else 0]+=1
        if st>=400: errors[f'{r.get("method","?")} {norm_path(r.get("path",""))} {st}']+=1
        pt=path_tmpl[norm_path(r.get("path",""))]; pt["req"]+=1; pt["upIn"]+=ui; pt["downOut"]+=do
def MB(x): return f"{x/1048576:.2f}M"
print(f"RANGE {first_ts} -> {last_ts}")
print(f"TOTAL req={n} upIn={MB(total_upIn)}B downOut={MB(total_downOut)}B saved={MB(total_upIn-total_downOut)}B ({(1-total_downOut/total_upIn)*100:.1f}%)")
print("BY_BUCKET upIn:")
for b,v in sorted(bucket.items(), key=lambda kv:-kv[1]["upIn"]):
    print(f"  {b:<18} req={v['req']:>7,} upIn={MB(v['upIn'])+'B':>10} downOut={MB(v['downOut'])+'B':>10} ratio={v['downOut']/v['upIn'] if v['upIn'] else 0:.2f}")
print("BY_PATH top12:")
for p,v in sorted(path_tmpl.items(), key=lambda kv:-kv[1]["upIn"])[:12]:
    print(f"  {p:<55} req={v['req']:>6,} upIn={MB(v['upIn'])+'B':>9} downOut={MB(v['downOut'])+'B':>9}")
print("BY_HOUR:")
for hp in sorted(hour):
    v=hour[hp]; print(f"  {hp[5:]} req={v['req']:>6,} upIn={MB(v['upIn'])+'B':>9} downOut={MB(v['downOut'])+'B':>9}")
print(f"STATUS {dict(status_dist)}")
if errors:
    print("ERRORS top:")
    for k,c in errors.most_common(8): print(f"  {c:>5} {k}")
PY
```

> ⚠️ **窗口读取注意**：脚本先读活跃 `access.jsonl` 再读 `.1`~`.5`，导致打印的 `RANGE` 首末行时序被打乱。**真实窗口以 `BY_HOUR` 的首末 bucket 为准**（本报告即如此取窗）。

### 6.2 日常单文件 / 时段分析（`jq`）

切到按天格式后，复用 `traffic-accounting.md` §5 的 jq 片段（按桶累计、按时段、慢请求 Top、非 2xx 计数、按 `clientId` 分设备）。当前 legacy 多文件场景用上面的 python 流式脚本更稳。

---

## 7. 建议与下一步

按"影响 × 可行性"排序：

1. **【高优】排查 ocdroid cursor 管理 / 回退路径**
   - 现象：`/since/<旧时间戳>` upIn 高、downOut≈0（成本白付），以及 `/since/0` 全量拉 105 次。
   - 动作：在 ocdroid 侧核对重连 / 冷启动 / 切 directory 后的 watermark 是否回退到旧值或 0；确认 `compareWatermark` 在 `onReconcileSuccess/needsReconcile/needsCatchUp/reduceSlimDigest` 各站点未被误清。预期收益：可消除 §2.3 中 `/since/*` 系列约 70+ MB/窗 的纯浪费，并降低热 session 反复全量拉的频次。

2. **【高优】热 session list 拉取去重 / 限频**
   - 现象：`/slimapi/messages/ses_*`（list）24,871 次 / 2227 MB，独占 80% 成本。
   - 动作：客户端对同一热 session 的全量 list 加去重 / 节流 / ETag-style 缓存；优先用 `/since` 增量而非 list 全量。这是降本最大杠杆。

3. **【中】`/session/*/children` polling 节流**
   - 现象：164,633 次/窗（≈190 次/分钟），字节小但请求量极大。
   - 动作：核对客户端是否对 children 做高频轮询；考虑改 SSE 事件驱动或拉长间隔。

4. **【中】系统性 400 排查（6 个 `/slimapi/**` GET）**
   - 现象：1,137 次 400，分布 `status/messages/events/permissions/sessions/stream`。
   - 动作：抓 1–2 条 400 请求的 `requestId`，对照 sidecar 日志确认是版本头缺失/错误还是参数校验；若是版本头，属客户端发版对齐问题。

5. **【中】SSE 真实成本长期采样**
   - 现象：access log 看不到 SSE 真实上游成本（盲区）。
   - 动作：等 `logs/traffic-snapshot.jsonl` 上线后，周期采样 `events_sse.upIn`（内存账本），建立 SSE 成本基线；历史单点测过 270 MB/46min、策展省流 99.9%，需长期曲线确认。

6. **【低】`POST /session/*/summarize` 500（5 次）**
   - 字节影响可忽略，但属上游侧错误，顺手核对 opencode 侧 summarize 是否稳定。

---

> **复跑说明**：本报告为一次性快照。日志会轮转覆盖（legacy 约 14h 窗口），后续复跑用 §6.1 脚本即可；切换到按天格式后用 §6.2 的 jq。数字以实跑为准，勿沿用本文件旧数。
