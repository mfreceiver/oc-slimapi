# B3 slim-token shim 退役 — 跨项目交叉影响评估

> **日期**：2026-08-05
> **性质**：跨项目交叉影响评估记录。**非契约**。
> **触发**：ocdroid 项目审计反馈 B3（`🔴 高 / 中`）：4 个 `@Deprecated` vestigial shim（`captureSlimCommitToken` 等）状态机已退役，真实防护下沉 `epoch` + `ReloadIdentity`。
> **评估方法**：ocdroid 源码调查（explorer，4 shim + epoch/ReloadIdentity）+ oc-slimapi 发出面复查（`turn_registry.py` turn-fence）。

---

## 1. 结论（TL;DR）

**纯 ocdroid 内部重构，oc-slimapi 零 wire 影响，无需任何改动。**

oc-slimapi 发出的两个 token/fence 机制——turn-fence（`turnIncarnation`/`turn`）与 token-stream 去重（`partEventRevision`）——**仍被 ocdroid 活跃消费**，未被 `epoch`/`ReloadIdentity` 取代。4 个退役 shim **从未消费任何 oc-slimapi wire 数据**。

---

## 2. 4 个退役 shim（纯客户端，零 sidecar 依赖）

均位于 `ocdroid/.../data/repository/OpenCodeRepository.kt`：

| shim | 行号 | 消费的 sidecar 数据 |
|---|---|---|
| `captureSlimCommitToken()` | :337-351 | **无**——捕获 `ConnectionIdentityStore.capture()`（本地 epoch）+ `currentClientBundle()`（本地 generation） |
| `isSlimCommitTokenCurrent(token)` | :354-363 | **无**——比对本地 identityStore + clientBundle |
| `commitIfSlimTokenCurrent(token, commit)` | :365-376 | **无**——委托 `identityStore.commitIfCurrent()` |
| `requireSlimTokenCurrent(token)` | :378-381 | **无**——包装上一项，失败抛 `StaleSlimCommitException` |

（另有 `StaleSlimCommitException` :313-315，异常类非 shim。）

**这 4 个 shim 全程操作纯客户端构造（`ConnectionIdentityStore` epoch + `ClientBundle` generation），从未触碰 oc-slimapi 任何 wire 字段。**

## 3. 新机制 `epoch` + `ReloadIdentity`（同样纯客户端）

- **`epoch`**（`ConnectionIdentityStore.kt`）：进程级单调 `AtomicLong`，`beginReconfigure()` 时 +1。`capture()`/`isCurrent()`/`commitIfCurrent()` 守卫「此响应是否来自当前连接身份？」——host/profile 切换时作废所有在途旧连接 fetch。**纯客户端**。
- **`ReloadIdentity`**（实为 `SkeletonReloadCoordinator.LaunchTicket` + `routeInstance` CAS，:156-167）：skeleton 重载发起时捕获 `connectionIdentity`/`bundleStamp`/`routeInstance`；`preHttpGuard()` (:480-496) 发 HTTP 前全维度复检，防 A→B→A token laundering 与 stale 传输响应。**纯客户端**。

## 4. 关键：新机制与 sidecar turn-fence 是**正交**防护，非取代

| 防护 | 守卫的威胁 | 归属 |
|---|---|---|
| `epoch` / `ReloadIdentity` | 「此响应是否来自当前**连接/路由化身**？」（host/profile 切换作废、token laundering） | ocdroid 客户端 |
| `turnIncarnation` / `turn`（sidecar） | 「此状态事件是否来自当前**执行轮次（prompt turn）**？」（因果排序、丢弃旧轮 digest） | oc-slimapi wire |
| `partEventRevision`（sidecar） | token-stream 帧级 strict `>` 去重（防乱序/重放） | oc-slimapi wire |

退役 shim 不触及后两者的消费。

## 5. sidecar 发出面仍被活跃消费（证据）

| sidecar 机制 | ocdroid 消费点 | 状态 |
|---|---|---|
| **turn-fence** `turnIncarnation`/`turn`（digest SSE + `/slimapi/sessions/status` flat 顶层） | `SessionSyncCoordinator.kt:259-263` 解析 → `ServerRound(inc,turn)` → `StatusFanOutApplier` → `AuthorityReducer` turn-fence 因果排序 | ✅ 活跃 |
| **token-stream 去重** `partEventRevision`（`/slimapi/sessions/{sid}/stream` 帧） | `TokenStreamCoordinator.kt:789-827` 读取 → `PartRevisionLedger.admit()` (`PartRevisionLedger.kt:198-201`) strict `>` 去重 | ✅ 活跃 |

oc-slimapi 发出面（`turn_registry.py`：`IncarnationStore` persisted-epoch + per-sid `turn`，`snapshot(sid)` → `(incarnation, turn)`；stamp 到 digest/status）保持不变，无需改动。

---

## 6. 行动

- **oc-slimapi：无需行动**。turn-fence + partEventRevision 确认仍被活跃消费，非 dead code。
- **ocdroid：B3 是其自身清理**（移除 4 个 `@Deprecated` vestigial shim + `StaleSlimCommitException`）。纯客户端，可安全删除；删除不影响任何 oc-slimapi wire 契约。

## 7. 契约健康度备注

本次评估顺带确证：oc-slimapi 的两个加性 fence/dedup wire 机制（`turnIncarnation`/`turn` 与 `partEventRevision`）在 ocdroid 侧有真实活跃消费者——它们不是 dead 字段。若未来 ocdroid 重构 turn-fence 或 token-stream 去重（例如用客户端自产 epoch 取代 `turnIncarnation` 消费），需重新评估 sidecar 是否继续发出。

---

## 附录：调研产物

| lane | 主体 | 产出 |
|---|---|---|
| ocdroid 源码 | explorer（exp-1） | 4 shim 映射 + epoch/ReloadIdentity 机制 + 活跃消费点 file:line + 交叉影响定论 |
| sidecar 发出面 | orchestrator | `turn_registry.py`（incarnation + per-sid turn → digest/status stamp）复查 |
