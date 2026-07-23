# 发版移交：oc-slimapi v0.5.0 — Token 批式 SSE

> 移交对象：运维（部署）/ 下一轮开发（post-release backlog）。  
> 双边对接契约权威仍为 [`docs/ocdroid-token-stream-handoff.md`](ocdroid-token-stream-handoff.md) §1 + 本文 §4。  
> 生成日期 2026-07-23。

---

## 1. 一句话状态

oc-slimapi **v0.5.0** 已 push（main + tag）+ Gitea Release 已建（2026-07-23 13:26）+ **本机部署已生效**（reinstall + restart + health 自检通过，2026-07-23）；token 批式 SSE（opt-in 实时流）上线，**全加性 wire，不 bump** `X-Slimapi-Version`（仍 `1`）。双边联合终审 re-gate **GO 9.7**（rev-bgpt）；ocdroid 已 pushed（origin）。**剩余**：仅 post-release backlog（§6）；生产环境 reinstall + restart（如另需部署生产机）+ 实网实证 V-B。

---

## 2. 发版产物

### oc-slimapi（本仓，已 push origin main + tag `v0.5.0` + Gitea Release ✅）

| commit | 内容 |
|---|---|
| `a4c8251` | `release: v0.5.0`（pyproject 0.4.0→0.5.0 + tag `v0.5.0`） |
| `b9521e8` | docs(changelog): fold [Unreleased] → [0.5.0] |
| `786d8fb` | feat(token-stream): Stage A–E 批式 SSE + 双边 handoff |

- 包版本：`0.4.0` → **`0.5.0`**
- Wire API 版本：**`X-Slimapi-Version: 1`（不 bump）**
- 契约：`docs/v1-contract.md` rev J（加性）

### ocdroid（并列仓，已 push origin main ✅；github 镜像待同步）

| commit | 内容 |
|---|---|
| `f448318` | refactor ζ-2：ExpandBatchEngine 抽出（与 token-stream 无关，独立重构） |
| `1986567` | fix(token-stream): `token_memory_limit` 触发重连（rev-bgpt 方案 A） |
| `d4b22da` | fix(token-stream): C-1 done:null 保 buffer + C-2 idle/deleted+UNKNOWN + C-3 删 part_too_large |
| `3c9173f` / `665cf79` / `a6521e0` | 早期重构 wave + token-stream 集成 |

---

## 3. 部署步骤（运维）

### 3.1 push（确认无误后）— ✅ 已完成 2026-07-23

```bash
# oc-slimapi — DONE
git push origin main && git push origin v0.5.0
# ocdroid — DONE（origin）；github 镜像待同步
git -C /home/mar/personal_projects/ocdroid push origin main
```

### 3.2 Gitea Release — ✅ 已完成 2026-07-23 13:26

已为 tag `v0.5.0` 建 Release（title `v0.5.0 — Token 批式 SSE（opt-in 实时流）`，body = `CHANGELOG.md` `[0.5.0]` 节，via `tea releases create`）。archive：`.../oc-slimapi/archive/v0.5.0.tar.gz` / `.zip`。

### 3.3 服务端部署 — ✅ 本机已完成 2026-07-23（生产机如另需部署，同步骤）

```bash
# reinstall（AGENTS.md reinstall 纪律：发版后必须重装，否则 health 版本不刷新）— DONE 本机
.venv/bin/pip install -e '.[test]'
systemctl --user restart oc-slimapi
journalctl --user -u oc-slimapi -f
```

### 3.4 部署自检（必做）— ✅ 通过 2026-07-23

`GET /slimapi/health` 应返回：

- 根级 **`features.tokenStream === true`**（`health.py:35` 硬编码 True，**默认启用**，非 env 门控）
- `server.api_version === 1`
- `sidecar.version === "0.5.0"`（从 `importlib.metadata` 读，重装后刷新）

**实测（2026-07-23，`curl -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health`）**：

```json
{
  "sidecar": {"ok": true, "version": "0.5.0"},
  "server": {"api_version": 1, "accepted_client_versions": [1, 1]},
  "schema": {"degraded": false, "version": 1, "clientMin": 1, "clientMax": 1},
  "features": {"tokenStream": true}
}
```

三项门控全 PASS（`sidecar.version=0.5.0` / `api_version=1` / `tokenStream=true`，`schema.degraded=false`）。

客户端经 `:14097` mTLS 访问；`features.tokenStream===true` 后 ocdroid 才连 `GET /slimapi/sessions/{sid}/stream`。

---

## 4. 双边 wire 契约（post-fix 权威，ocdroid 确认 2026-07-23）

**事件（SSE `event:`）**：`server.connected` / `server.heartbeat` / `message.part.snapshot` / `message.part.delta` / `resync`；未知 event → 跳过（forward compat）。JSON：`ignoreUnknownKeys` + `isLenient` + `coerceInputValues`。

**PartSnapshot 键**：`sessionID`/`messageID`/`partID`/`text`(nullable)/`done`(默认 false)/`truncated`(默认 false)。
- `done=true` → DONE（**C-1=A**：text 缺/null 保累计 buffer，非 null 替换，零 effect）
- `truncated=true` → 清 part + authoritative `/since`（优先于 done）

**PartDelta**：`sessionID`/`messageID`/`partID`/`text`(required)，仅 part STREAMING 时 append。

**Resync**：`reason` + `sessionID`(nullable)。

**ResyncReason wire 5**：`reconnect_no_replay` / `subscriber_backpressure` / `token_memory_limit` / `session_idle` / `session_deleted`；未知 → `UNKNOWN`（**不丢帧**）。**不发** `part_too_large`（超限 → `truncated:true`）。

**两档恢复**（`triggersReconnect`）：
- `true`（重订阅 stream）：`reconnect_no_replay` | `subscriber_backpressure` | `token_memory_limit`
- `false`（仅清态 + `/since`）：`session_idle` | `session_deleted` | `UNKNOWN`

**resync reducer**：`ClearPartState(sid)` + `TriggerSinceFetch(authoritative=true)` + `Reconnect(sid iff triggersReconnect)`。

**杠杆**：lever1 `done`=marker（null 保 buffer），权威全文走 `/since`；lever2 token stream **默认 gzip**（OkHttp 透明，服务端决定），`/events` **不 gzip**。

**health 门控**：root `features.tokenStream===true` 才连 stream（dual-read root‖server，fail-closed）。

**transport**：`readTimeout(0)` + 15s heartbeat 复位 watchdog；45s 客户端 watchdog → 清态 + `/since` + reconnect。

---

## 5. 验证证据

| 项 | 结果 |
|---|---|
| oc-slimapi `./scripts/check.sh` | **763 passed**（release.sh 内置门禁） |
| ocdroid `./scripts/check.sh`（全量） | **GREEN**（3m31s，含 ExpandBatchEngine refactor） |
| 联合终审（rev-bgpt） | NO-GO 8.4 → 修方案 A → **re-gate GO 9.7** |
| 关键阻塞闭合 | `token_memory_limit` → 重连重建 snapshot 锚点；无残留 orphan 路径 |

---

## 6. Post-release backlog（细化版；P1–P4，按优先级）

> **本机 P3–P4 可执行计划**：[`docs/ocmar/plans/2026-07-23-token-stream-p3-p4.md`](ocmar/plans/2026-07-23-token-stream-p3-p4.md)（任务拆解 / 依赖 / 验收；默认路径 S-1 → S-3a）。  
> 事实基准（2026-07-23 recon）：`token_hub.py` 实际路径 `src/oc_slimapi/sse/token_hub.py`（1509 行）；flush `TOKEN_FLUSH_SECONDS=0.1`、early-flush `TOKEN_FLUSH_BYTES=4096`、heartbeat `TOKEN_HEARTBEAT_SECONDS=15`、内存 4+4MiB 均在 `config.py`；gzip 在 `routes/token_stream.py:107-116`（`zlib` level 6 + `Z_SYNC_FLUSH` per-frame）。**注意**：原 §6 中 S-3 的「中位 1.47x / re-anchor ~1.5x」数字在代码中不存在——真实基线是 `scripts/measure_token_overhead.py` 的 `≤1.2x median overhead_x_gzip` 目标。

### P1 文档对齐（低成本，防对接漂移）

| ID | 项 | 仓 | 状态 |
|---|---|---|---|
| **D-1** | `ocdroid-token-stream-handoff.md` §0 历史叙述收口为终态 | oc-slimapi | **✅ 已完成 2026-07-23**（§0 已收口为终态声明，过程细节归档至 §8–§11） |
| **C-4** | ocdroid 客户端文档对齐：`token-stream-client-design.md`（旧 4-reason）/ `dev-plan.md` / design V6 §5.1「SSE 不 gzip」→ 对齐 lever1 / 5 reason / 两档 resync / lever2 gzip | ocdroid | 待办（不阻塞） |

### P2 运维 / 实网（非码阻塞）

| ID | 项 | 谁 | 验收 |
|---|---|---|---|
| **V-B** | 生产路径长连 idle 实证：`:14097` mTLS 抓 ≥30s 静默流，确认 15s `server.heartbeat`（`config.py:TOKEN_HEARTBEAT_SECONDS`）穿透 stunnel（客户端 45s watchdog = 3×15s 对齐） | 运维 | 抓包见 ≥1 个 `event: server.heartbeat` 且连接不被 stunnel idle 断；服务端已发 `X-Accel-Buffering:no` / `Cache-Control:no-cache,no-transform` |
| **V-A′** | busy 会话 + 弱网观察 done:true 后 blank 窗口 digest 迟达（可选） | 可选 | 观察到 digest step-finish → `/since` 覆盖时延分布；评估是否需 C-1=B 补强 |
| **V-M** | 压测诱发 `token_memory_limit` 后确认重连 + 新 snapshot 锚点（可选，单测已闭合） | 可选 | 触发 `_evict_part_for_memory`（`token_hub.py:970-982`）→ 客户端方案 A 重连 → `attach_subscriber:778-814` 重发 `snapshot{done:false}`；观察无 orphan delta |

### P3 工程债

> **本轮进展（2026-07-23，ocmar workflow `token-stream-p3-p4`）：** **S-1 ✅ + S-3a ✅ + S-2 ✅** 已完成（每 lane rev-grok 评审 APPROVED → rev-opus 终审 APPROVED → fresh verifier 767 passed）。**S-3b / S-4** 仍待办（S-3b 需 harness 数据；S-4 跨仓）。实现详见 [`docs/ocmar/plans/2026-07-23-token-stream-p3-p4.md`](ocmar/plans/2026-07-23-token-stream-p3-p4.md)。

#### S-1 拆 `token_hub.py`（1509 行）→ `sse/tokenstream/` 包 — ✅ 已完成 2026-07-23

- **现状**：单文件 `src/oc_slimapi/sse/token_hub.py` 1509 行，承载 framing + models + budget + flush + session routing + fanout + ingest + subscriber/registry 九类职责。
- **目标包结构**（纯移动，不改行为）：
  - `sse/tokenstream/frames.py` ← `_now_ms`/`sse_frame`/`_snapshot_frame`/`_delta_frame`/`_truncated_frame`/`_resync_frame`/`_connected_frame`/`_heartbeat_frame`（134–205）
  - `sse/tokenstream/models.py` ← `LivePart`/`DeltaAccumulator`/`_TokenMetrics`/`PartKey`（209–278）
  - `sse/tokenstream/budget.py` ← `_reserve`/`_evict_part_for_memory`/`_check_pending_budget`/`_start_part`/`drop_part`/tombstone 族（922–1173）
  - `sse/tokenstream/flush.py` ← `flush_loop`/`flush`/`flush_sid`/`finish_part`/`ttl_sweep`/resync 队列（398–653, 739–773, 1178–1204）
  - `sse/tokenstream/session.py` ← `on_session_status`/`on_session_deleted`/`_retire_session`/`on_upstream_reconnect`（658–737, 1210–1234）
  - `sse/tokenstream/fanout.py` ← `attach_subscriber`/`detach_subscriber`/`has_subscriber`/`_fanout_*`/`_emit_snapshot_or_truncated`/`_truncate_part_for_all`（778–917）
  - `sse/tokenstream/subscriber.py` ← `TokenSubscriber`/`TokenSubscriberCapacityError`/`TokenStreamRegistry`（1242–1509）
  - `sse/tokenstream/hub.py` ← `TokenStreamHub` 聚合壳（280–1235）；config 仍留在 `config.py`
- **验收**：`./scripts/check.sh` 763 全绿且无新增/删除测试；公开导出（`TokenStreamHub`/`TokenStreamRegistry`/`TokenSubscriber`/异常）签名不变；`__init__.py` re-export 保持调用方零改动。
- **纪律**：纯结构性 PR，**不夹带行为改动**（便于 review）；若需行为改动另起 PR。
- **估工**：M（机械移动 + import 重连）。

#### S-2 方案 B（可选）：memory-limit 后对现有 sub 重发剩余 live snapshots — ✅ 已完成 2026-07-23

- **现状**：`_evict_part_for_memory`（`token_hub.py:970-982`）驱逐一个 LivePart 后仅 `_fanout_resync(sid,"token_memory_limit")`，**不**对现有 sub 重发任何 snapshot。只有 `attach_subscriber`（778–814）在**新** sub 接入时重发剩余 LivePart 的 `snapshot{done:false}`。当前恢复依赖客户端方案 A（`triggersReconnect=true` → 重连）。
- **范围**：在 `_evict_part_for_memory` 扇 resync 后，对该 sid 仍 live 的其余 part，向**现有** fanout 集重发 `snapshot{done:false}`（复用 `_emit_snapshot_or_truncated`，传当前 sub 集而非新 sub）。
- **验收**：新增测试——驱逐 part A（part B 仍 live）→ 现有 sub 先收 `resync{token_memory_limit}` 再收 part B 的 `snapshot{done:false}`；B 的后续 delta 不再 orphan。
- **影响**：纯加性；客户端方案 A 已出货，B 落地后可在未来把 `token_memory_limit` 的 `triggersReconnect` 放宽为 false（双边协调，属独立 wire 决策，不在本项）。
- **估工**：S–M。**依赖**：S-1 拆包后更易改（可选先做 S-1）。

#### S-3 perf：先观测后调参（**修正原条目的臆测数字**）

> ⚠️ 原条目「当前中位 1.47x 已达 re-anchor ~1.5x 目标」无代码依据。真实基线 = `scripts/measure_token_overhead.py` 目标 `≤1.2x median overhead_x_gzip`。且当前**无**压缩比 / flush 时延 / 每 sub 背压指标，调参前必须先补观测。

- **S-3a（先做，观测）**：补 metrics → 经 `snapshot_token_metrics()` 暴露 `sse.tokenStream.*`：
  - 压缩比 gauge：`routes/token_stream.py:107-116` zlib encode 边界累计 raw vs compressed bytes；
  - flush 时延：`flush_loop`（398–430）每 tick 计时；
  - 每 sub 队列深度：`TokenSubscriber` 暴露当前 queue depth（`TokenSubscriberCapacityError` 前的背压信号）。
- **S-3b（后做，调参，凭 S-3a + harness 数据）**：flush `TOKEN_FLUSH_SECONDS` 0.1→0.2（latency vs CPU/ratio 权衡）；gzip level 6↘（`routes/token_stream.py:108`，ratio vs CPU）；early-flush `TOKEN_FLUSH_BYTES` 4096 调整。
- **验收**：S-3a = 新 metrics 可见 + 单测；S-3b = `measure_token_overhead.py` 仍 `≤1.2x median` 且 p95 flush 时延在预算内；`./scripts/check.sh` 全绿。
- **估工**：S-3a = S–M；S-3b = S（数据驱动）。**依赖**：S-3a 必须先于 S-3b。

#### S-4 ocdroid flow 级测试（跨仓）

- **范围**：ocdroid `TokenStreamCoordinator` 对 `session_idle`/`session_deleted`/`UNKNOWN` 的 flow 级测；token stream × `/events` digest eviction 的顺序/重复测（`session_deleted` 不发 `EvictSession`，依赖 digest 独立路径）。
- **仓**：ocdroid（非本仓）。**估工**：M。

### P4 产品扩展（非目标 / 延后）

| ID | 项 | 现状（grounding） | 范围 / 前置 |
|---|---|---|---|
| **F-1** | reasoning / tool-input part 流式 | `on_part_updated:516-520` 记 `_nontext_parts` 墓碑；`on_part_delta:554,565-566` 静默 drop 非 text delta（C3 故意）；模块 docstring 40–44 明确「非 text 静默 drop」 | 扩 wire：`PartSnapshot`/`PartDelta` 加 `partType` 或新设 `reasoning.*`/`tool_input.*` 事件；按 `(messageID,partID,field)` 键建 LivePart。**前置**：产品 go + ocdroid 协调发版 + 评估是否 wire bump。**估工**：L |
| **F-2** | busy-open UX 占位 | 服务端**无**占位：`attach_subscriber:778-814` 仅发 `server.connected{sessionID}`；无 LivePart 时客户端空等。`_busy_sids`（`on_session_status:679-681`）仅 TTL guard 用，不发帧 | 可选：attach 时若 `sid ∈ _busy_sids` 且无 LivePart，发 `server.connected{...,busy:true}` 占位让客户端立即渲染 skeleton。加性 wire；**估工**：S（服务端）+ ocdroid UX |
| **F-3** | 自适应 flush 窗 | flush cadence 固定 `TOKEN_FLUSH_SECONDS=0.1`；early-flush 固定 4KiB | 按 sub 数 / 观测时延动态调 cadence（设 min/max 护栏）。**依赖**：S-3a metrics。**估工**：M |

---

## 7. 已知残余 / 风险

- **服务端本机已 reinstall/重启**（§3.3，2026-07-23）：`health.sidecar.version` 已刷新为 `0.5.0`（§3.4 自检通过）。**生产机**如与本机分离，仍需各执行一次 `pip install -e` + `systemctl restart`。
- **ocdroid github 镜像未同步**：仅 push 了 Gitea origin；github `mfreceiver/oc-droid` 需另 `git push github main`（可选）。
- **token stream 长连生产实证未做**（V-B）：代码侧已给 15s heartbeat + `X-Accel-Buffering:no` + `Cache-Control:no-cache,no-transform`；最终实证需生产路径抓包。
- **`token_memory_limit` 重连有 backoff 窗口**（实时暂停）；由 authoritative `/since` + 新订阅 snapshot 恢复，非数据一致性风险。
- **`session_deleted` eviction 依赖 `/events` digest 独立路径**（reducer 不发 EvictSession）—— 联合跨路径顺序测试属 S-4。

---

## 8. 回滚

- 代码：`git revert a4c8251 b9521e8 786d8fb`（或 reset 到 `cacdd5b`，v0.4.0 之前）。
- feature 软关闭：当前 `features.tokenStream` 硬编码 True（无 env 开关）。如需部署级关闭，需后续加 `OC_SLIMAPI_TOKEN_STREAM_ENABLED` flag（**不在本轮**）。ocdroid 侧 dual-read fail-closed：若 health 缺/404/405 → 自动降级「完成后整条出现」（零回归）。
