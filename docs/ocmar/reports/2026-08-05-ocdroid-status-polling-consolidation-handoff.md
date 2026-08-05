# ocdroid 告知：session status 轮询合并（消除按目录冗余 fan-out）

> 本仓 sidecar 已上线 A（`/slimapi/sessions/status` 的 `directory` 改可选，commit `5e4e750`，live）。
> 本文件是给 **ocdroid 侧**的 B（轮询合并）告知提示词。下方「提示词正文」整段可转发给 ocdroid 开发者/agent。
> 调研证据链：[`../specs/2026-08-05-s4-batch-status-research.md`](../specs/2026-08-05-s4-batch-status-research.md)。

## 本仓侧状态（A 已完成）

- `GET /slimapi/sessions/status` 的 `directory` 由**必填改为可选**（加性，wire 未 bump，仍 2）。live smoke：不带 directory 现返 200（原 422）。
- 上游 `GET /session/status` 的 `directory` 是 **no-op**（源码确证：handler 零参数，`statusSvc.list()` 返全量 `Map<SID,Info>`），故本端点**恒返全局状态图**，与 directory 取值无关。
- **不建** batch endpoint（原 S4 计划）——directory no-op 使 batch/envelope/fan-out 全部失效。

---

## 提示词正文

```text
请合并 ocdroid 的 session status 轮询，消除按目录冗余 fan-out。先读现有三个轮询循环的 status-fetch 路径，确认无依赖按目录结果后，改为每周期单次全局调用；不要合并下游业务逻辑。灰度可回退。

== 背景（调研已确证，源码 + 运行时）==

1. 上游 opencode `GET /session/status` 的 `directory` 参数是 **no-op**：
   - handler 零参数（`handlers/session.ts:77-79`），`statusSvc.list()` 返全量 in-memory `Map<SessionID, Info>`（`session/status.ts:35-37`）；
   - `directory` 仅给 `WorkspaceRoutingMiddleware` 做 local/remote 路由判定，从不转发给 handler 做过滤。
   - 故响应恒为**全局** `Record<SID, {type:"busy"|"idle"|"retry"}>`，与传什么 directory（或完全不传）无关。

2. 本仓 sidecar 的 `GET /slimapi/sessions/status?directory=` 因此也恒返全局 map + turn merge（turnIncarnation/turn）。`directory` 现已改为**可选**（commit 5e4e750，live）：不传→200 全局 map、不转发 directory；传→normalize+透传（上游仍 no-op）。

3. ocdroid 现状（三个循环都按目录 fan-out，每次返回同一份全局 map → 纯冗余）：
   - `StatusPollOrchestrator`（前台 4s）：SSE 健康时 no-op；SSE 断时 `dirList.map{async{getSlimapiSessionsStatus(dir)}}.awaitAll()`（`StatusPollOrchestrator.kt:394-406`）→ N 请求。
   - `ProcessStatusPoller`（后台 30s）：`for (dir in registeredWorkdirs) getSlimapiSessionsStatus(dir)`（`StatusFetchService.kt:83-87`）→ N 请求。
   - `BackgroundUnreadPoller`（后台 30s）：`directories.map{async{getSlimapiSessionsStatus(dir)}}.awaitAll()`（`BackgroundUnreadPoller.kt:319-333`）→ N 请求。
   - 后台两个循环 30s 重叠跑、查同一目录集 → **2N 冗余请求/30s**。典型 N=1–3。

== ocdroid 要做的 ==

1. 三循环改为每周期**单次全局调用**（替代按目录 fan-out）：
   - 调一次 `getSlimapiSessionsStatus(...)`，拿全局 map；
   - 客户端侧用已有的 `allSessionsById`（含 session→directory 归属）做本地过滤——本就在做（status map 是 sid→status，无 directory 字段，per-dir 调用本就没给额外信息）。
   - directory 参数怎么传（二选一，结果都是全局 map）：
     · 【推荐，零兼容成本】传任意单个 directory（如首个 registeredWorkdir，或主项目目录）——在**所有 sidecar 版本**上都返全局 map，无需 capability 探测；
     · 【语义更干净，需新 sidecar】不传 directory——仅在 sidecar ≥ 5e4e750 上返 200（旧 sidecar 返 422）。若选此，需 capability 探测（首遇 422 即回退传 directory）。

2. 去重后台两个重叠循环的 **status-fetch**（ProcessStatusPoller + BackgroundUnreadPoller 都 30s 查同目录）：
   - 合并它们的 status 获取（共享一次 fetch 结果 / 一个委托另一个的缓存），2N→1（或 2）。
   - **不要合并下游业务逻辑**——两者职责不同：ProcessStatusPoller 驱动 `GlobalBusyState`/FGS 生命周期；BackgroundUnreadPoller 驱动未读/空闲通知。只去重 fetch，不合并 consumer。

3. 前台 `StatusPollOrchestrator`：SSE 健康时已 no-op（正确，保持）；确保 SSE 断时的 fallback 路径也走单次调用（同上 directory 处理）。

== ocdroid 绝对不能做的 ==

1. 不要假设上游按 directory 过滤——它是 no-op；若未来某次发现返回的 map 相对已知 session 异常小（暗示上游加了过滤），才回退按目录调用并告知本仓。
2. 不要因「全局 map」而丢弃 `allSessionsById` 的目录列表与按目录归属——本地过滤仍需要它；保留按目录调用能力作为 fallback。
3. 不要合并两个后台循环的**下游**（FGS 生命周期 vs 未读通知）——只去重 status fetch。
4. 不要把单次全局调用误当成「只关心一个目录」——它覆盖所有已知 session（全局 map），各循环的 consumer 照常用 `allSessionsById` 过滤。

== 兼容性 ==

1. wire 版本未 bump（仍 X-Slimapi-Version: 2）。
2. 传 directory 的单次调用在**所有 sidecar 版本**工作（旧 sidecar directory 必填但接受任意值返全局 map；新 sidecar directory 可选）→ 零 gating 灰度可行。
3. 不传 directory 仅 sidecar ≥ 5e4e750 支持；旧 sidecar 返 422（capability 探测可识别）。
4. 旧 ocdroid（未合并）继续按目录 fan-out——行为不变（只是冗余），无回归。

== 收益（量化）==

- 后台：2N → 1 req/30s（N=2：4→1，省 75%；N=3：6→1，省 83%）。即使不去重两循环、仅单次化：2N → 2（省 50%）。
- 前台（SSE 断时）：N → 1（N=2：省 50%）。
- 收益在**请求数/电量/RTT**（非字节；status 响应本就 89% 空、很小）。基线：status 55,999 次/5天，相当部分是这些冗余按目录调用。

== 验收 ==

1. 接入后 sidecar access log `bucket=sessions` 的 `/slimapi/sessions/status` 请求频次显著下降（后台趋近每 30s 1 次/循环）。
2. 全局 map 覆盖所有已知 session（用 `allSessionsById` 对照，不应缺 sid）。
3. FGS 生命周期 / 未读通知 / 状态徽章行为不回归（去重只动 fetch，不动 consumer）。
4. 交付：改动文件清单（预计涉及 StatusPollOrchestrator / ProcessStatusPoller / BackgroundUnreadPoller / StatusFetchService / SlimStatusFanOut 等）+ 灰度方案 + 监控观测。

在确认前：不要把 status map 当目录局部结果；不要合并下游 consumer；不要单方改 wire。
```

---

## 本仓侧已完成的（A，不阻塞 ocdroid）

- `/slimapi/sessions/status` 的 `directory` 改可选（commit `5e4e750`，live smoke 通过）。
- 不建 batch endpoint（S4 原计划被调研推翻）。
- 调研报告：`docs/ocmar/specs/2026-08-05-s4-batch-status-research.md`。

## 备注

- ocdroid 的合并**不强依赖** A：传任意 directory 的单次调用在所有 sidecar 版本上工作。A 只是让「不传 directory」的干净语义可用（新 sidecar）。
- 真正的杠杆在 ocdroid 侧（消除 N→1 + 去重 2 循环）；sidecar 侧已无更多可做。
