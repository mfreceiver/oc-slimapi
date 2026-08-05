# S4 batch status 调研报告

> **日期**：2026-08-05
> **性质**：调研报告 + 设计决策建议。**非契约**。
> **证据链**：opencode 源码（v1.18.4 快照）+ ocdroid 源码 + live probe（opencode v1.18.13 @ :4096）。
> **背景**：候选接口评估 [`2026-08-05-slimapi-candidate-interfaces-assessment.md`](./2026-08-05-slimapi-candidate-interfaces-assessment.md) §5 S4「批量 status」原计划建 `GET /slimapi/sessions/status/batch?directory=<repeatable>` + envelope；本调研**推翻该计划**——核心前提（directory 过滤）不成立。

---

## 1. 结论（TL;DR）

**不要建 elaborate batch endpoint。** 上游 `GET /session/status` 的 `directory` 参数是 **no-op**（返回全局全量 map），因此：

- **无需 batch**：1 次调用即返回全量；ocdroid 现在的 N 次按目录调用**每次都返回相同的全量 map**——纯冗余。
- **envelope / 部分失败语义无意义**：1 次 upstream 请求，全有或全无。
- **真正的浪费在 ocdroid 侧**：3 个轮询循环、按目录 fan-out、且后台 2 个循环重叠跑（2N 冗余/30s）。batch endpoint **解决不了这个冗余**——只有 ocdroid 自己合并能解决。

**建议**：① sidecar 把 `/slimapi/sessions/status` 的 `directory` 改为**可选**（语义对齐：它本就是全局）；② ocdroid 三循环改为**单次全局调用 + 客户端侧按已知 session→directory 归属过滤**，并**去重后台 2 个重叠循环**。无需新路由。

---

## 2. Finding 1 —— 上游 `directory` 是 no-op（源码确证）

`GET /session/status` **完全忽略 `directory`**，恒返回全局全量 `Record<SessionID, Info>`。

| 环节 | 证据 |
|---|---|
| group 声明 | `packages/opencode/.../groups/session.ts:121-131` — query 用 `WorkspaceRoutingQuery`（含 `directory?`），但仅给 `WorkspaceRoutingMiddleware` 做 local/remote 路由判定 |
| handler | `packages/opencode/.../handlers/session.ts:77-79` — `statusSvc.list()` **零参数**调用；对比同文件 `list` handler（:64-75）**会**读 `ctx.query.directory` 传给 `session.list()`——status handler 不读 |
| service | `packages/opencode/src/session/status.ts:35-37` — `list()` 返回整个 in-memory `Map<SessionID, Info>`，无 directory 参数、无过滤谓词 |
| 路由层 | `packages/opencode/.../shared/workspace-routing.ts:7,21` — `/session/status` 标 `action:"forward"`，directory **不被转发给 handler 做过滤** |
| 响应形状 | `StatusMap = Record<String, SessionStatus.Info>`，`Info = {type:"idle"} \| {type:"busy"} \| {type:"retry",attempt,message,action?,next}`（`packages/schema/src/session-status-event.ts:9-32`） |

> 本仓 `INTERFACE_MAP.md:26` 早已记录「上游 `statusSvc.list()` 全量；directory 参数对上游是 no-op」——本次源码追溯确证该注记。

**设计含义**：1 次 upstream 请求即足够，**不需要 N fan-out**；现 `GET /slimapi/sessions/status?directory=X` 无论 X 为何都返回同一全局 map。

---

## 3. Finding 2 —— ocdroid 3 个轮询循环、按目录 fan-out、后台 2× 重叠

ocdroid 有 **3 个独立循环**查 session status，slim 模式下**每个循环按目录 fan-out**（每目录 1 个 `GET /slimapi/sessions/status?directory=X`）：

| 循环 | 文件 | 触发/间隔 | 行为 | 请求数/周期 |
|---|---|---|---|---|
| **StatusPollOrchestrator**（前台） | `ui/controller/StatusPollOrchestrator.kt` | `UnreadSoakController` 每 **4s** | **SSE 健康时 no-op**（digest `status` 中继为稳态源，:93-163）；SSE 断时回退 slim REST fan-out（`launchLoadSessionStatusSlim` :328，`dirList.map{async{...}}.awaitAll()` :394-406） | N（仅 SSE 断时） |
| **ProcessStatusPoller**（后台） | `service/streaming/ProcessStatusPoller.kt` | 前台→后台启动，**30s** | `StatusFetchService.fetch()` → `for (dir in registeredWorkdirs) getSlimapiSessionsStatus(dir)`（`StatusFetchService.kt:83-87`） | N |
| **BackgroundUnreadPoller**（后台） | `ui/controller/BackgroundUnreadPoller.kt` | `AppLifecycleMonitor.startBackgroundPolling()`，**30s** | `loadSlimSessionStatus` → `directories.map{async{getSlimapiSessionsStatus(dir)}}.awaitAll()`（:319-333） | N |

**重复确证**：后台 `ProcessStatusPoller` 与 `BackgroundUnreadPoller` **都** 30s 跑、**都**查同一目录集 → **2N 冗余请求/30s**。典型 N=1–3 目录。

**per-session slim fan-out 当前已短路**（`StreamingModule.kt:129`：`slimPerSessionStatusEndpointAvailable` 默认 false）——已委派给批量端点（即本 S4）。

**目录列表来源**：`allSessionsById`（顶级 + directorySessions + childSessions 合并）；`ProcessStatusPoller` 用 `StatusSnapshot.registeredWorkdirs`。

**消费方**：`sessionStatuses` UI 投影（会话列表状态徽章）、`StatusAggregatorImpl`（GlobalBusyState → FGS 生命周期）、`UnreadSoakController`（未读标记）、重试卡片、会话选择器等（见 exp-2 报告清单）。

---

## 4. Finding 3 —— live probe（运行时佐证）

- `GET :4096/session/status`（无 directory）/ `?directory=/nonexistent-xyz-12345`（伪造）/ `?directory=/`（根）→ **三者皆返回 `{}`（0 条）**。当前无 busy session → 无法用运行时区分「过滤」vs「no-op」（空结果两种假设都自洽）。与流量评估「89% 空」一致。
- **结论**：live probe **inconclusive**，但**不推翻**源码结论（no-op）。源码是权威。
- **目录景观**：当前 2 个有 session 的目录（`/home/mar/opencode_wd` 24 个、`/home/mar/personal_projects/omni-manage` 2 个）。故本部署 N≤2。

---

## 5. S4 原计划的崩塌与新建议

### 原计划（评估 §5 S4）

> 新增 `GET /slimapi/sessions/status/batch?directory=<repeatable>`，先验上游是否全量，envelope 表达部分失败（`{complete, snapshotAt, results:[{directory, ok, statuses|error}]}`），不用 HTTP 207。

**前提「directory 过滤」不成立** → envelope / 部分失败 / fan-out 全部失效。1 次 upstream 请求即全量，无部分失败。

### 新建议（双轨，sidecar 极小 + ocdroid 主力）

#### A. sidecar（极小，可选）

把 `GET /slimapi/sessions/status` 的 `directory` 由**必填改为可选**（additive）：
- 传则 `validate_directory` + 透传（上游 no-op，但仍透传以兼容 + 保留路由语义）；
- 不传则不附 `X-Opencode-Directory`，直接返全局 map + turn merge。

**理由**：该端点语义上就是全局投影；强制 `directory` 必填是误导（INTERFACE_MAP 早已标注 no-op）。改后签名诚实反映现实。**零破坏**：旧调用方传 directory 仍正常。

> 即使不做 A，ocdroid 今天用任意 directory 调一次也能拿到全局 map——A 纯粹是语义整洁化。

#### B. ocdroid（真正收益所在）

1. **三循环改单次全局调用**：不再按目录 fan-out；调一次拿全局 map，客户端用已有的 `allSessionsById`（含 session→directory 归属）做本地过滤。每个循环 N→1。
2. **去重后台 2 个重叠循环**：`ProcessStatusPoller` 与 `BackgroundUnreadPoller` 都 30s 查同一目录集——合并 status-fetch，或一个委托另一个的缓存结果。2N→1（或 2）。
3. **前台**：`StatusPollOrchestrator` 已在 SSE 健康时 no-op（正确）；只需保证 SSE 断时的 fallback 也走单次调用。

#### C. 不建 elaborate batch endpoint

`/slimapi/sessions/status/batch` + envelope 不做。理由：directory no-op → 无需 batch（1 调即全量）；envelope 部分失败失效（1 upstream，全有或全无）；batch endpoint **除非 ocdroid 也改成单次调用**否则不解决冗余——而 ocdroid 改成单次后，现有单路由已足够，batch 无增量价值。

---

## 6. 收益量化

| 场景 | 现状 | 改后（B 单次 + 去重） | 降幅 |
|---|---|---|---|
| 后台（2 循环 × N 目录） | 2N req/30s | 1 req/30s | N=2 → 4→1（**75%**）；N=3 → 6→1（83%） |
| 后台（不去重，仅单次化） | 2N req/30s | 2 req/30s | N=2 → 4→2（50%） |
| 前台（SSE 断时） | N req/4s | 1 req/4s | N=2 → 2→1（50%） |

流量评估基线：status **55,999 次/5天**（89% 空）。其中相当部分是这些冗余按目录调用。单次化 + 去重可大幅削减 status 请求数（后台为主）。**收益在请求数/电量/RTT，非字节**（单次响应本就很小、89% 空）。

---

## 7. 风险与边界

1. **no-op 是结构性事实，非契约保证**：上游 handler 零参数、service 返全量 Map——若未来 opencode 重写 handler 加 directory 过滤，本假设破。**概率低**（需 handler+service 重写）。若发生，ocdroid 单次调用会拿到子集 → 需重新引入按目录调用（或此时再做 batch）。
   - **缓解**：建议 ocdroid 保留「目录列表 + 按目录调用」能力作为 fallback；当前默认单次全局，若发现返回 map 相对已知 session 异常小，则回退按目录。
2. **后台 2 循环去重需谨慎**：`ProcessStatusPoller`（驱动 FGS 生命周期/GlobalBusyState）与 `BackgroundUnreadPoller`（驱动未读/空闲通知）职责不同——去重 status-fetch 可行，但**不要合并它们的下游逻辑**。
3. **turn merge**：现有 `/slimapi/sessions/status` 的 `turnIncarnation`/`turn` merge 是 per-sid 的（turn_registry.snapshot(sid)），全局 map 单次调用同样能 merge 全部 sid——无影响。

---

## 8. 给 ocdroid 的协同要点（可提取为告知材料）

1. 上游 `/session/status` 的 `directory` 是 **no-op**，返回全局 `Record<SID, {type}>`——源码确证（handler 零参数）。现 `/slimapi/sessions/status?directory=X` 无论 X 返回同一全局 map + turn merge。
2. 现按目录 fan-out 是**纯冗余**（每次返回相同全量 map）；改为单次全局调用 + 客户端侧按 `allSessionsById` 过滤即可。
3. 后台 `ProcessStatusPoller` + `BackgroundUnreadPoller` 30s 重叠查同目录——去重 status-fetch（勿合并下游逻辑）。
4. sidecar 拟把 `directory` 改可选（additive，零破坏）；旧调用方无需改。
5. 不建 batch endpoint——无增量价值。
6. **若未来发现 status map 相对已知 session 异常小**（暗示上游加了过滤），回退按目录调用并告知本仓。

---

## 附录：调研产物

| lane | 主体 | 产出 |
|---|---|---|
| lane-1 上游源码 | explorer（exp-1） | directory no-op 确证 + 源码引用链 |
| lane-2 ocdroid 轮询 | explorer（exp-2） | 3 循环架构 + 后台 2N 冗余 + 消费方清单 |
| lane-3 live probe | orchestrator | status 当前空（inconclusive）+ 目录景观（N≤2） |
