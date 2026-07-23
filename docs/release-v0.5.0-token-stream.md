# 发版移交：oc-slimapi v0.5.0 — Token 批式 SSE

> 移交对象：运维（部署）/ 下一轮开发（post-release backlog）。  
> 双边对接契约权威仍为 [`docs/ocdroid-token-stream-handoff.md`](ocdroid-token-stream-handoff.md) §1 + 本文 §4。  
> 生成日期 2026-07-23。

---

## 1. 一句话状态

oc-slimapi **v0.5.0** 已 push（main + tag）+ Gitea Release 已建（2026-07-23 13:26）；token 批式 SSE（opt-in 实时流）上线，**全加性 wire，不 bump** `X-Slimapi-Version`（仍 `1`）。双边联合终审 re-gate **GO 9.7**（rev-bgpt）；ocdroid 已 pushed（origin）。**剩余**：本机/生产 reinstall + 重启（§3.3）+ post-release backlog（§6）。

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

### 3.3 服务端部署（**剩余 — 待运维执行**）

```bash
# reinstall（AGENTS.md reinstall 纪律：发版后必须重装，否则 health 版本不刷新）
.venv/bin/pip install -e '.[test]'
systemctl --user restart oc-slimapi
journalctl --user -u oc-slimapi -f
```

### 3.4 部署自检（必做）

`GET /slimapi/health` 应返回：

- 根级 **`features.tokenStream === true`**（`health.py:35` 硬编码 True，**默认启用**，非 env 门控）
- `server.api_version === 1`
- `sidecar.version === "0.5.0"`（从 `importlib.metadata` 读，重装后刷新）

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

## 6. Post-release backlog（P1–P3，按优先级）

### P1 文档对齐（低成本，防对接漂移）

| ID | 项 | 仓 |
|---|---|---|
| **C-4** | ocdroid 客户端文档对齐：`token-stream-client-design.md`（旧 4-reason）/ `dev-plan.md` / design V6 §5.1「SSE 不 gzip」→ 对齐 lever1 / 5 reason / 两档 resync / lever2 gzip | ocdroid |
| **D-1** | `ocdroid-token-stream-handoff.md` §0 历史叙述收口为终态 | oc-slimapi |

### P2 运维 / 实网（非码阻塞）

| ID | 项 | 谁 |
|---|---|---|
| **V-B** | 生产路径长连 idle 实证：`:14097` mTLS 抓 ≥30s 静默流，确认 15s `server.heartbeat` 穿透 stunnel（客户端 45s watchdog 对齐） | 运维 |
| **V-A′** | busy 会话 + 弱网观察 blank 窗口 digest 迟达（可选） | 可选 |
| **V-M** | 压测诱发 `token_memory_limit` 后确认重连 + 新 snapshot 锚点（可选，单测已闭合） | 可选 |

### P3 工程债（明确 post-release，本轮 **不做**）

| ID | 项 | 仓 |
|---|---|---|
| **S-1** | 拆 `token_hub.py`（1509 行）→ `sse/tokenstream/` 包（行为保持，设计原定 post-release 单文件发版） | oc-slimapi |
| **S-2** | 方案 B（可选）：memory-limit 后对现有 sub 重发剩余 live snapshots；A 已落地则非必须 | oc-slimapi |
| **S-3** | perf 残余调参：flush 100→200ms / gzip cadence / level（当前中位 1.47x 已达 re-anchor ~1.5x 目标） | oc-slimapi |
| **S-4** | ocdroid coordinator 对 idle/deleted/UNKNOWN 的 flow 级测；token stream × `/events` digest eviction 顺序/重复测 | ocdroid |

### P4 产品扩展（非目标 / 延后）

- **F-1** reasoning / tool-input part 流式（现仅 text；非 text delta 静默 drop）
- **F-2** busy-open UX 占位（skeleton 等首帧 snapshot）
- **F-3** 自适应 flush 窗

---

## 7. 已知残余 / 风险

- **服务端未 reinstall/重启**（§3.3）：tag/release 已 push，但本机/生产 editable install 仍跑旧码；须 `pip install -e` + `systemctl restart` 后 `health.sidecar.version` 才刷新为 `0.5.0`。
- **ocdroid github 镜像未同步**：仅 push 了 Gitea origin；github `mfreceiver/oc-droid` 需另 `git push github main`（可选）。
- **token stream 长连生产实证未做**（V-B）：代码侧已给 15s heartbeat + `X-Accel-Buffering:no` + `Cache-Control:no-cache,no-transform`；最终实证需生产路径抓包。
- **`token_memory_limit` 重连有 backoff 窗口**（实时暂停）；由 authoritative `/since` + 新订阅 snapshot 恢复，非数据一致性风险。
- **`session_deleted` eviction 依赖 `/events` digest 独立路径**（reducer 不发 EvictSession）—— 联合跨路径顺序测试属 S-4。

---

## 8. 回滚

- 代码：`git revert a4c8251 b9521e8 786d8fb`（或 reset 到 `cacdd5b`，v0.4.0 之前）。
- feature 软关闭：当前 `features.tokenStream` 硬编码 True（无 env 开关）。如需部署级关闭，需后续加 `OC_SLIMAPI_TOKEN_STREAM_ENABLED` flag（**不在本轮**）。ocdroid 侧 dual-read fail-closed：若 health 缺/404/405 → 自动降级「完成后整条出现」（零回归）。
