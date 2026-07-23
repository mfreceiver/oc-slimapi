# oc-slimapi 项目状态 + 待办（压缩锚点）

> **用途**：上下文压缩后的恢复锚点。压缩后先读本文件，再执行当前任务（见 §4）。
> **更新**：2026-07-23　**分支**：`dev`（R2 双边工作）/ `main`（已发版稳定态）

---

## 1. 当前交付态

### main（稳定，已 push）
- **v0.5.0**：token 批式 SSE（opt-in，lever1 done-marker + lever2 gzip）已发版+部署生效。
- **P3 r1**（commit `7a1861a`）：S-1 拆 `token_hub.py`→`sse/tokenstream/` 包；S-3a 加 5 观测指标；S-2 method B 半成品（eviction 后对非 current 的剩余 live part 重发 snapshot）。
- **清理**（`1bf0c4c`）：2-M1/2-M2/2-M3/O2 minors。
- **O1**（`c21ca3b`）：`_reserve→evict` re-entrancy 闭合（`skip_key` 排除 current key，4 调用点）。
- **R1 计划状态**（`90983da`）：§6.1/§6.4 标 O1 ✅。
- `./scripts/check.sh` = **769 passed**。

### dev（R2 双边工作，已 push）
- `cb59e96`：R2 ocdroid 配合计划 + 双边契约/协商 handoff。
- `694d73d`：merge main（R1 计划副本同步）。
- 文档清理：`.ocmar/workflows/token-stream-p3-p4/`（FINISHED 工作流产物，2.1M，gitignored）已删；5 个无引用的过期 docs/ 文档已删。
- **MB-P-S1**（`3e4b3b7`）：method-B eviction current-key 锚点闭合。新增 `_emit_snapshot_or_truncated_nodrop`（截断不 drop）；`_evict_part_for_memory` re-snapshot 重新纳入 current key 走 nodrop，非 current 维持 C6 drop；`skip_key` 保留。rev-grok APPROVED_WITH_NITS（NIT #1 docstring + #2 多 sub 测已修），fresh verifier 773 passed。**服务端 R2 unilateral 工作至此尽**——余项皆阻塞于 ocdroid（D-MB-P/D-F-1/D-F-2）或产品 go。

---

## 2. 文档地图（权威/当前）

| 文档 | 角色 |
|---|---|
| `AGENTS.md`（根） | 入口索引 |
| `docs/v1-contract.md` | **wire 契约权威**（rev J，`X-Slimapi-Version: 1`） |
| `docs/design-token-stream.md` | token-stream 设计权威 |
| `docs/design-v2.md` / `docs/INTERFACE_MAP.md` / `docs/CLIENT_CHANGES.md` | 设计/接口追踪/客户端改动 |
| `docs/release.md` / `docs/operations.md` / `docs/develop.md` | 发版/运维/开发 |
| `CHANGELOG.md`（根） | 接口行为变更记录 |
| `docs/ocdroid-token-stream-handoff.md` | R1 v0.5.0 双边 handoff（终态 wire） |
| `docs/release-v0.5.0-token-stream.md` | v0.5.0 发版移交 |
| `docs/ocdroid-cooperation-r2-handoff.md` | **R2 双边契约/协商**（D-MB-P/D-F-1/D-F-2 决策清单） |
| `docs/ocmar/plans/2026-07-23-ocdroid-cooperation-r2.md` | **R2 完整任务计划** |
| `docs/ocmar/plans/2026-07-23-token-stream-p3-p4.md` | R1 P3-P4 计划（backlog，S-1/S-3a/S-2/O1 已完成） |

> docs/ocmar 下其余 plans/reports/specs/reviews 多为历史审计（被 CHANGELOG/v1-contract 引用，**勿删**——删需先修引用）。

---

## 3. 完整待办（R2，按是否被 ocdroid 阻塞）

| ID | 项 | 服务端 | ocdroid | wire | 状态 |
|---|---|---|---|---|---|
| **MB-P-S1** | method B 硬前置（current-key 锚点闭合） | ✅ 完成 `3e4b3b7`（dev） | — | 无 | **✅ 完成** |
| **MB-P** | method B 产品化（flip `triggersReconnect` true→false） | MB-P-S1 先做 | flip + flow 测 | 无 | 阻塞于 MB-P-S1 + D-MB-P |
| **F-1** | reasoning/tool-input 流式 | 停 drop + 扩 wire | reducer+UI | 待裁定（D-F-1） | 阻塞于产品 go + D-F-1 |
| **F-2** | busy-open 占位帧 | `server.connected{busy:true}` | UX skeleton | 加性（不 bump） | 阻塞于产品 go + D-F-2 |
| **S-4** | ocdroid flow 级测 | 提供契约 | 实施 | 无 | 任意时点（跨仓） |
| **C-4** | ocdroid 文档对齐 | 提供终态 wire | 对齐 | 无 | 任意时点（跨仓） |
| **V-B** | 生产长连 idle 实证 | heartbeat+防代理头 | 45s watchdog+抓包 | 无 | 运维（任意时点） |

**双边关键路径**：ocdroid 收 R2 handoff → 回 D-MB-P/D-F-1/D-F-2 → 解锁 MB-P flip / F-1 / F-2。
**服务端先行（已完成）**：MB-P-S1 ✅（`3e4b3b7`）。服务端 unilateral R2 工作已尽；余项皆双边/产品门控。下一可推动项 = ocdroid 回 D-MB-P（MB-P 仅剩 ocdroid flip，服务端无活）。

---

## 4. 最近完成：MB-P-S1 ✅（dev `3e4b3b7`，2026-07-23）

### 4.1 目标
method B 产品化（`token_memory_limit` clear-only 不重连）的**服务端硬前置**：闭合 eviction 后 current key 的客户端锚点缺口。

### 4.2 现状（post-O1，main `c21ca3b`）
- 文件：`src/oc_slimapi/sse/tokenstream/hub.py`，方法 `_evict_part_for_memory(self, key, skip_key=None)`。
- O1 让 re-snapshot 循环 `for live_key in sorted(k for k in self.live_parts if k[0]==sid and k != skip_key)` **跳过 current key**（4 调用点传 `skip_key`）。→ clear-only（method B）下 current key 客户端锚点不恢复。
- 当前客户端 `TOKEN_MEMORY_LIMIT.triggersReconnect=true`（R1 既有）→ 重连 handshake 恢复 current key；**MB-P flip（=false）后此缺口暴露**。

### 4.3 要做什么
重新纳入 current key 到 re-snapshot，但用**「截断不 drop」新发射路径**（避免 O1 re-entrancy）：
- current key snapshot 帧 **≤ `max_frame_bytes`** → 发正常 `snapshot{done:false}`（保动画）。
- current key snapshot 帧 **> `max_frame_bytes`**（近 1MiB）→ 发 `snapshot{truncated:true}` + **不 `drop_part`**（客户端 `/since` 拉权威全文）。
- **关键**：现有 `_emit_snapshot_or_truncated` 超限时走 `_truncate_part_for_all`→`drop_part`（正是 O1 re-entrancy 源）——**不可用于 current key**。需新路径（如 `_emit_snapshot_or_truncated_nodrop`）：发 truncated 帧但**保留 LivePart、不 drop**，从而不 invalidate 调用方 `_reserve`/`on_part_delta` 持有的 `live` 引用。
- ~~`skip_key` 参数随之可移除/重构~~ **（更正：`skip_key` 保留）**——nodrop 路径仍需区分 current key（`live_key == skip_key` 走 nodrop，其余走带 drop 的 C6 backstop）。rev-grok 评审确认「保留 `skip_key` 比移除更稳」。

### 4.4 已知取舍（须在 D-MB-P 让 ocdroid 裁定，但实现时记住）
large-part 分支下，即便服务端保留 LivePart，客户端收 `truncated` 后清该 part、停 append → 服务端继续累计的 delta 在客户端 orphan 被丢。即 **large current key 动画不可救（仍 blank 至 `/since`）；仅 small current key 真 snapshot 分支保住动画**。

### 4.5 验收
- 新测：eviction + small current key → 现有 sub 收该 key 的 `snapshot{done:false}`（锚点恢复）。
- 新测：eviction + 近 1MiB current key → 收 `snapshot{truncated:true}`；LivePart 保留（`in live_parts`，`not in _disabled_parts`）；`_total_live_bytes` 无漂移；无 orphan delta。
- **O1 不回归**：current key 绝不被 `drop_part` mid-reserve（更新现有 `test_o1_evict_skips_current_key_being_reserved` 以反映 current key 现被重新纳入且安全——保留「不 drop + 无 gauge 漂移」不变量）。
- `./scripts/check.sh` 全绿。

### 4.6 流程与评委（用户既定）
1. **实现**：优先 `fixer-zlm`；失败 3 次换 `fixer`。空返回时**通过文件核实**完成（grep/wc/check.sh 独立验证，不信任自报）。
2. **lane 评审**：**仅 rev-grok**（用户「换回 grok」）。
3. **终审**：**rev-opus**（多 lane 完成后）。
4. **验证**：fresh `_priv-verifier` 跑 `pytest -p no:cacheprovider tests/`（live rerun）。
5. 改 Python 后必跑 `./scripts/check.sh`；加性 wire 不 bump `X-Slimapi-Version`；commit 走 Conventional Commits + 中文描述。

### 4.7 落点
- 实现在 `dev` 分支（R2 method-B 工作）；commit + push origin/dev。
- 详细契约背景：`docs/ocdroid-cooperation-r2-handoff.md` §2.1 + `docs/ocmar/plans/2026-07-23-ocdroid-cooperation-r2.md` §2 MB-P。

---

## 5. 恢复指令（压缩后）

1. 读本文件。
2. 切到 `dev`：`git switch dev && git pull`。
3. **MB-P-S1 已完成**（§4，`3e4b3b7`）。服务端 unilateral R2 工作已尽。
4. **下一步取决于双边输入**：
   - 若 ocdroid 回 **D-MB-P**（接受 S1 变体）→ MB-P 仅剩 ocdroid flip `triggersReconnect`，服务端无活；可转 S-4/C-4/V-B（提供契约/运维）或等 ocdroid 联调。
   - 若产品 go + ocdroid 回 **D-F-2** → 做 F-2（busy 占位，加性字段；流程同 §4.6）。
   - 若产品 go + ocdroid 回 **D-F-1** → 做 F-1（reasoning/tool-input 流式，wire 决策先行；最大双边项）。
5. 用户给定新任务时，按 §4.6 评委约定（fixer-zlm → rev-grok lane → rev-opus 终审[多 lane] → fresh `_priv-verifier` live rerun）。
