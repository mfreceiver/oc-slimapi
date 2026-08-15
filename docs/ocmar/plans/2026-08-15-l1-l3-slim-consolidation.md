# L1-L3 Slim 全收推进实施计划（ocdroid 仅 slimapi 连接 + 4 能力上收）

> **For agentic workers:** REQUIRED SUB-SKILL: Use ocmar-subagent-driven-development (recommended) or ocmar-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **状态：v1.1 —— oracle 审阅通过（PASS with conditions，ses_ffc8327e6ffe5uStA34VDAceQp，2026-08-15）；全部条件（§A-1 BLOCKER flush loop 保活 / §A-2 契约声明 / §B-1 envelope / §C-1 mode 容忍 / §C-2 池槽 / §D-1 预算收窄 / §5 checklist）已落回本文。进入实施。**

**Goal:** 将 oc-slimapi 确立为 ocdroid 唯一 API 提供商：上收 4 项客户端能力（token 并入 events + coalesce / permission 事件化 / 服务端 merge / transform_busy 吸收），退役 ocdroid 直连路径（14096），全部变更经四级评审链后经 release.sh 发版。

**Architecture:** 维持 Python sidecar 消费 opencode legacy HTTP 面的现有架构不变（TS 换核已废弃，双边闭环证据 2026-08-15，见 L1 决策记录）。4 项能力全部以**加性 wire 变更**实现：`X-Slimapi-Version` 维持 2 不 bump，新能力经 `/slimapi/health` 的 `features.*` feature flag 公告，旧客户端（含现有 ocdroid 0.23.x-0.24.x）行为零变化。ocdroid 侧按同一契约删减客户端代码（约 2200 行协调器 + 直连分支），由 ocdroid 仓库自行实施（外部依赖 lane，本计划只锁接口契约）。

**Tech Stack:** Python async sidecar（src/oc_slimapi，pytest 1425 tests 基线）；ocdroid Kotlin（外部仓库）；上游对照 opencode v1.18.16 pinned 快照。

## Global Constraints

- `X-Slimapi-Version` 恒为 `2`：本计划全部变更为**加性**，禁止任何破坏性 wire 变更（版本双轨规则，AGENTS.md）。
- 每个任务完成后 `./scripts/check.sh` 必须通过；基线 = 1425 passed @ git `e68e337c7e1f99ad5999fe7470aa98dd287fbbcb`（2026-08-15 实测）。
- 新增 `/slimapi` 路由（若有）必须同步 `docs/specs/INTERFACE_MAP.md`，否则 check.sh 失败。
- `docs/specs/v2-contract.md` 仅做**加性 rev**（§2 端点表 query param / §3 SSE 事件类型扩充），不做破坏性改写；CHANGELOG 记账归属（oracle §5 统一）：**各 lane 落地时即在 `CHANGELOG.md` `[Unreleased]` 记条目，L3-1 定稿汇总**——发版前齐备即合规。
- 写域纪律：并行 fixer-ds lane 的文件集互不重叠；lane 边界见 L2 章节表。
- 上游对照基线：`opencode-src/current` 实际指向 **v1.18.16**（本仓 AGENTS.md 写 v1.18.13 已过时，L1 Task 2 修正）。
- ocsapi / TS 内核方向**已废弃**（用户决策 2026-08-15）；唯一跟踪触发器见 L1 决策记录。
- 发版必须走 `./scripts/release.sh minor`（加性功能 → minor），禁止手写 tag；发版前 CHANGELOG 必须完整。
- 评审链（用户指令 2026-08-15）：方案 oracle 审 → 每 lane fixer-ds 实现 + rev-gpt 评审 → 每 L rev-cgpt 层级审阅 → 全部完成后 rev-kimi 终审 → 发版。rev-cgpt / rev-grok / rev-opus 为不可信 reviewer（bash 全禁，git_ro 单通道），派发前编排者必须先 `review_prep` 制备 binding 并注入同一 rid（面板同源 P1）。

## 决策记录（L1 归档，不可回退）

1. **TS 换核废弃**：双边独立核验（oc-slimapi orchestrator 本机 pinned 快照 + npm registry 实查；ocdroid 侧 librarian 第二 lane）互证——`@opencode-ai/core` / `@opencode-ai/server` 均 `private: true`、npm 上 `0.0.0-reserved.0` 占位、内核编译进 175MB bun 单体二进制、主包无 exports 不可 import、官方 slack 网关先例也是子进程 + SDK client。Python sidecar 吃 HTTP 面为唯一受支持形态。
2. **跟踪触发器**（不启动、只观察）：`@opencode-ai/core` 或 `@opencode-ai/server` 正式发布且 exports 稳定，或未发布的 `packages/sdk-next`（workspace 依赖 core+server+client+effect）正式发布含内核 SDK → 届时重估 dslima A0 式「import 原生 fold」。
3. **上游仓库迁移**：sst/opencode → anomalyco/opencode（301），后续源码引用换新地址。
4. **ocdroid 仅 slimapi 连接可行性**：4 天 access log 实证 ocdroid 40853 reqs 100% 走 `/slimapi/**`、0 passthrough；客户端代码无 slim→direct 自动回退；直连无 slim 不可替代能力（方向相反：skeleton/策展 SSE/token stream 为 slim 独占）。

---

## L1 — 基线固化（本仓，1 lane）

### Task 1: 基线证据归档 + 决策记录

**Files:**
- Create: `docs/ocmar/reports/2026-08-15-l1-baseline-evidence.md`

**Interfaces:**
- Consumes: 本计划「决策记录」章节内容（逐字搬运）。
- Produces: 归档证据文件；L2/L3 评审与 ocdroid 对接引用其结论。

**Acceptance Criteria:**
- `T1-C1`: 文件存在且含四节：① check.sh 基线（1425 passed @ e68e337c7e1f99ad5999fe7470aa98dd287fbbcb + 日期）；② 流量实证摘要（ocdroid 40853 reqs / 16 条 /slimapi 路由 / 0 passthrough / 2 设备 0.23.3-0.24.0 / 4 天窗口 08-12~08-15）；③ TS 换核废弃决策（决策记录 1-3 逐字）；④ ocdroid 仅 slimapi 结论（决策记录 4）。
- `T1-C2`: `./scripts/check.sh` 通过（新报告文件不影响测试）。

- [ ] **Step 1: 写入报告文件**（内容 = 本计划「决策记录」四项 + 上述基线数字，纯 markdown，无代码）
- [ ] **Step 2: Run `./scripts/check.sh`** → Expected: 1425 passed + 路由一致 + compileall ✅
- [ ] **Step 3: Record diff**（`git rev-parse HEAD` 基线 + `git diff --stat`）

### Task 2: AGENTS.md 上游对齐版本修正

**Files:**
- Modify: `AGENTS.md`（「当前对齐版本」行）

**Interfaces:**
- Consumes: `readlink /home/mar/personal_projects/ocdroid/opencode-src/current` → `v1.18.16`
- Produces: 文档与实际 symlink 一致

**Acceptance Criteria:**
- `T2-C1`: AGENTS.md 中对齐版本行写 v1.18.16，且 `readlink` 输出与之一致。
- `T2-C2`: `./scripts/check.sh` 通过。

- [ ] **Step 1: `readlink` 验证实际指向** → Expected: `v1.18.16`
- [ ] **Step 2: 编辑 AGENTS.md 该行**（v1.18.13 → v1.18.16，一句话，不重排其他内容）
- [ ] **Step 3: `./scripts/check.sh`** → ✅；Record diff

### L1 评审门

- lane 评审：rev-gpt（Task 1+2 diff vs 基线 e68e337）
- 层级评审：rev-cgpt 审 L1（派发前编排者 `review_prep`，rid 注入）

---

## L2 — 4 能力上收（本仓，3 条并行 lane）

> **状态：v1.0 —— 任务分解已按 exp-3 源码结构图（2026-08-15）补全，permission 面有事实修正（见 B）。**

### Wire 契约设计（加性，oracle 审阅重点）

**A. token 并入 `/slimapi/events` + 服务端 coalesce**
- `/slimapi/events` 新增可选 query param `tokens=1`（缺省 = 现行为不变）；值非字面 `"1"` → 400 `invalid_tokens`（与 messages 的 `directory_not_allowed` 400 同级严格性）。
- 开启时策展 SSE 追加 `token` 帧：`{"type":"token","sessionID":...,"messageID":...,"partID":...,"delta":"<本窗拼接>"}`——复用 `TokenStreamHub` DeltaAccumulator 既有 100ms flush / 4KiB 提前 flush 节奏做服务端 coalesce。
- **范围（oracle 审点）**：`tokens=1` 收**全部 session** 的 coalesced delta（单用户 T3，并发活跃 session 极少；MVP 不做 per-sid 过滤）。
- **帧面**：仅 delta 帧；**不**向 events 发 snapshot/truncated/resync（终态/截断/恢复以 `session.digest` + `/messages/{sid}` 权威 reconcile——本来就是凌驾路径，§3.x.2 杠杆1）。
- events 连接**维持非 gzip**（§3.x.3 的 gzip 例外仅限 per-session token stream 连接）。
- 背压语义不变：token 帧计入 events Subscriber 既有 T3（256 项 / 2MiB，`hub_types.py:191-302`），溢出仍走既有 `resync{subscriber_backpressure}` 断连。
- **flush loop 生命周期（oracle BLOCKER §A-1）**：events `tokens=1` 订阅者是 TokenStreamHub flush loop 的**一等消费者**——`TokenStreamRegistry`（`sse/tokenstream/subscriber.py:604,729`）的 start/stop 账本从「per-session 订阅数」扩展为「per-session 订阅数 + events-tokens 订阅数」合计（NB-C4 first-attach/last-detach 生命周期扩展）；否则 ocdroid 退役 per-session stream 后零 token 订阅者 → flush loop 停转 → `events?tokens=1` 断流。账本对称性测试镜像 NB-C4/NB-D1。
- **契约声明（oracle §A-2）**：events token 帧**不含** `partEventRevision`（per-session stream 有），丢帧不可检测，终态一致性由 `session.digest` + `/messages/{sid}` 权威兜底；迁移期客户端**不得**同时消费 per-session stream 与 `tokens=1`（否则双份投递）；帧省略 `directory` 字段的依据 = sessionID 全局唯一（单用户 T3）。
- `/slimapi/sessions/{sid}/stream` 保留不删（additive-only；契约标注 deprecated-in-favor，ocdroid 迁移后自然闲置）。
- health 公告 `features.tokenCoalesce: true`。

**B. permission 冷启动聚合（事实修正：事件化 wire 已存在）**
- **exp-3 发现**：`permission.asked/resolved` + v2 双形态**已在** IMMEDIATE 直推集合（`sse/hub_types.py:73-74` → `global_hub.py:525-535` 原样直推），契约 §3 L262-266 已成文（客户端双形态必收）——**事件面零新增，B 不做任何 SSE 改动**。
- **真缺口 = 冷启动恢复**：v2 删除 permission 聚合端点（契约 L393「permission 无等价聚合端点」），冷启动/重连只能轮询 catch-all `GET /permission`（匿名侧实证 1127 reqs/4 天即此浪费）。
- **B 交付**：新增 `GET /slimapi/permissions`——跨目录聚合 pending permission 卡片，镜像 `/slimapi/questions` 两阶段 fan-out 模式（`routes/questions.py`；配置组先例 `config.py:330-338`）。**envelope 与 questions 对齐（oracle §B-1）**：`{"items":[],"errors":[],"authoritativeDirectories":[],"discoveryComplete":true}`——某 directory 失败时 items 聚合成功目录、`errors` 记账、`authoritativeDirectories` 仅含成功目录（防客户端 replace-all 丢 pending 卡 → 用户无法批准 → session 卡死）。应答路径不变（catch-all）。
- **上游事实（oracle 已对照 v1.18.16 核验）**：`GET /permission` → `Permission.Service.list()` → per-`Location` `InstanceState`（`opencode-src/current/src/permission/index.ts:15,46,169`），per-directory 语义确认 → fan-out 方向正确；上游响应为**裸数组** `PermissionV1.Request[]`（非 `{items:}`，与 questions 的 `{pending:[]}` 包裹不同）——投影白名单由 B1 锁定实际字段后决定（agent catalog 的 `Permission.Ruleset` 剔除先例是 catalog 字段，勿与 pending request 形状混淆）。
- health 公告 `features.permissionEvents: true`（语义 = permission 面完整：events 直推【已有】+ 聚合端点【新】）。

**C. 服务端 merge（skeleton + lazy-full 合并上收）**
- `/slimapi/messages/{sid}` 新增可选 query param `mode=merged`（缺省 = skeleton 现行为）。**mode 语义（oracle §C-1）**：仅字面 `merged` 激活合并；`full` 及其他未知值**维持现行静默忽略**（`messages.py:269` + full handler docstring `:362-364` 已文档化容忍过渡客户端——400 化会破坏「旧客户端零行为变化」的加性承诺）。
- merged 模式：skeleton 投影后，对当页含 `thin_placeholder` part 的消息**并发 fan-out** 拉 full（`GET /session/{sid}/message/{mid}`，走 CD-1 single-flight 管道），将 placeholder 替换为 full 投影（strip_diagnostics 后形状，`omitted:false`）单响应内联返回；不引入跨请求会话缓存（无状态、每请求自包含——明确否决「sidecar 常驻 session 消息存储」）。
- **超预算渐进降级（不 413）**：fan-out 并发 `merged_fanout=8`、每页 full 上限 `merged_max_fulls_per_page=16`、合并响应字节上限 `merged_max_bytes=8MiB`（T0 预置 config）；超限项**保持 skeleton 原样**，客户端可对残余项自行 `/full`（与今天逻辑一致）。
- `X-Next-Cursor` 语义不变。health 公告 `features.serverMerge: true`。

**D. 503 transform_busy 服务端吸收**
- `/full/{mid}`：route 层 single-flight（per-mid in-flight 共享——并发同 mid 请求共享 **1 次上游 raw GET**；transform 各 caller 自行执行，oracle §C-2）+ 内部有界等待预算 `transform_absorb_budget_seconds=2.5`（T0 预置；吸收 `transform_wait_seconds=2` 的等待 + Retry-After 语义）；预算耗尽才 503（错误形状不变，仅频率大降）。
- **预算收窄（oracle §D-1）**：重试循环每次 pool acquisition 的等待须按**剩余预算**收窄（给 `TransformPool` 加可选 per-attempt timeout 参数，不改 `TransformBusy` 抛出语义）——否则最坏等待 ≈ 2×`transform_wait_seconds`=4s 超 2.5s 预算。重试不放大上游负载（admission 先于上游 GET，`TransformBusy` 时上游请求未发出）。
- 不改 `transform.py` 抛出语义（`TransformBusy` 抛点 `transform.py:203-216` 保持）；吸收层在 `routes/messages.py`（现 503 生成点 `:195-211`）+ 新模块 `sse/singleflight.py`（实施偏离：计划原写 `src/oc_slimapi/singleflight.py`，最终落位 `src/oc_slimapi/sse/singleflight.py`，与 SSE 层基础设施同域）。
- merged 模式（C）内部 full 拉取同走此管道。health 公告 `features.transformAbsorb: true`。

### Lane 划分（写域互斥，锚点 = exp-3 实测）

| Lane | 能力 | 写域（精确，互斥） | 评审 |
|---|---|---|---|
| L2-T0（串行先行） | 公共接线 | 新 `src/oc_slimapi/features.py`；`config.py`（新字段组）；`routes/health.py`（features 聚合）；新 `tests/test_health_features.py` | rev-gpt |
| L2-A | token coalesce | `routes/events.py`（`:10-93` 加 param）；`sse/hub_types.py`（帧常量）；`sse/global_hub.py`（tap 分发）；`sse/tokenstream/hub.py`（events-mode flush 出口）；`sse/tokenstream/subscriber.py`（**flush loop 账本扩展，oracle §A-1**）；新 `tests/test_events_tokens.py` | rev-gpt |
| L2-B | permission 聚合 | 新 `routes/permissions.py`；`app.py`（router 注册 1 行，`:539-541` 区）；`docs/specs/INTERFACE_MAP.md`（加 1 行）；新 `tests/test_permissions.py` | rev-gpt |
| L2-CD | merge + 吸收 | `routes/messages.py`；新 `src/oc_slimapi/sse/singleflight.py`；新 `tests/test_messages_merged.py`、`tests/test_full_absorb.py` | rev-cgpt |

顺序：**T0（串行）→ A ∥ B ∥ CD（三 fixer-ds 并行）**。共享文件防护：`config.py`/`health.py` 仅 T0；`app.py`/`INTERFACE_MAP.md` 仅 B；`hub_types.py`/`global_hub.py` 仅 A；`messages.py` 仅 CD；`tests/test_hub_behavior_lock.py` **零改动**（新帧行为全进新测试文件）。T0 依赖：features flag 为**静态公告**（四能力同一发版列车，非灰度开关），T0 一次性全置 true + health 聚合接线，lane 不再碰共享文件。

### Task L2-T0: 公共接线（features + config + health）

**Files:** Create `src/oc_slimapi/features.py`、`tests/test_health_features.py`；Modify `src/oc_slimapi/config.py`（`:330-338` questions 配置组旁新增一组）、`src/oc_slimapi/routes/health.py`

**Interfaces:**
- Produces: `features.FEATURES: dict[str, bool]`（含 `tokenCoalesce/permissionEvents/serverMerge/transformAbsorb` 全 true）；config 新字段 `permissions_max_response_bytes=2MiB`、`permissions_fanout=8`、`permissions_max_aggregate_bytes=16MiB`（对齐 questions 三旋钮，oracle §B-1）、`merged_fanout=8`、`merged_max_fulls_per_page=16`、`merged_max_bytes=8MiB`、`transform_absorb_budget_seconds=2.5`（A/B/CD lane 只读）。

**Acceptance Criteria:**
- `T0-C1`: `GET /slimapi/health` 的 `features` 含四新 key 全 true 且 `tokenStream` 仍 true（零回归）。
- `T0-C2`: `Settings().validate()` 对新字段通过（正数/字节上限检查，镜像 questions 组写法）；`./scripts/check.sh` ✅。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_health_features.py
import httpx
from oc_slimapi.app import app

async def test_health_features_advertise_four_new_capabilities():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/slimapi/health", headers={"X-Slimapi-Version": "2"})
        f = r.json()["features"]
        assert all(f[k] is True for k in
                   ("tokenCoalesce", "permissionEvents", "serverMerge", "transformAbsorb"))
        assert f["tokenStream"] is True  # 既有能力零回归
```

- [ ] **Step 2: 跑测试确认失败**（KeyError: 'tokenCoalesce'）
- [ ] **Step 3: 实现**——`features.py` 四常量 dict；`routes/health.py` features 响应合并 `features.FEATURES`（保持既有 `tokenStream` 来源不变）；`config.py` 新字段组（命名/默认值照上表，env 前缀 `OC_SLIMAPI_` 不变，`validate()` 加正数断言）
- [ ] **Step 4: `./scripts/check.sh`** → ✅（1425+1 passed）；Record diff

### Task L2-A: token 并入 events（param + coalesced tap）

**Files:** Modify `routes/events.py`、`sse/hub_types.py`、`sse/global_hub.py`、`sse/tokenstream/hub.py`、`sse/tokenstream/subscriber.py`（flush loop 账本）；Create `tests/test_events_tokens.py`

**Interfaces:**
- Consumes: T0 features（无直接依赖，仅公告）；`TokenStreamHub` 既有 flush 管线（`tokenstream/hub.py:371-404` flush_loop、DeltaAccumulator `models.py`）；`Subscriber.try_put`（`hub_types.py:271-281` 背压语义）。
- Produces: events 订阅选项 `tokens: bool`（`hubs.subscribe(..., tokens=True)`）；新帧类型常量 `hub_types.TOKEN_FRAME_TYPE = "token"`；TokenStreamHub 新出口 `events_tap`（可空回调列表，flush 时对每 (sid,mid,pid) 窗口拼接调用）；**`TokenStreamRegistry`（`sse/tokenstream/subscriber.py:599-644,729+`）start/stop 账本扩展：合计「per-session 订阅数 + events-tokens 订阅数」做 first-attach start / last-detach stop（NB-C4 生命周期扩展，oracle §A-1）**。

**Acceptance Criteria:**
- `A-C1`: `?tokens=2` → 400 `invalid_tokens`；缺省/`tokens=1` 正常建流（既有 `server.connected` 首帧不变）。
- `A-C2`: `tokens=1` 订阅下，同 (sid,mid,pid) 连发 2 个上游 `message.part.delta`，~100ms 窗口后收到**单帧** `{"type":"token",...,"delta":d1+d2}`（coalesce 生效；测试用「宽松超时等帧到达 + 断言形状」而非精确时间窗断言，防 CI 抖动——oracle §4）。
- `A-C3`: 缺省订阅（无 tokens）收到 0 个 token 帧（现行为锁定）；既有 1425+ tests 全绿（含 `test_hub_behavior_lock.py` 零改动通过）。
- `A-C4`: token 帧溢出 subscriber 队列时仍走既有 `resync{subscriber_backpressure}` 断连（复用 `Subscriber` guard，不新增路径）。
- `A-C5`（oracle §A-1）: **仅** events `tokens=1` 订阅（零 per-session stream 订阅）时 flush loop 运行、token 帧按 ~100ms cadence 到达；全部订阅断开后 flush loop 停止（账本对称性，镜像既有 NB-C4/NB-D1 测试模式）。

- [ ] **Step 1: 写失败测试**（镜像 `tests/test_hub.py:197-206` 的 `make_global_event`/`ev`/`parse`/`drain` 助手模式 + `conftest.py` `upstream_factory`）

```python
# tests/test_events_tokens.py（关键用例；助手从 test_hub.py 复制）
async def test_tokens_invalid_value_rejected(client):
    r = await client.get("/slimapi/events?tokens=2",
                         headers={"X-Slimapi-Version": "2"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_tokens"

async def test_tokens_coalesced_into_single_frame(fresh_hub):
    sub = await fresh_hub.hubs.subscribe(directory="/p", tokens=True)
    await fresh_hub.publish(ev("/p", "message.part.updated",
        {"info": {"sessionID": "s1", "messageID": "m1", "part": {"id": "t1", "type": "text"}}}))
    await fresh_hub.publish(ev("/p", "message.part.delta",
        {"sessionID": "s1", "messageID": "m1", "partID": "t1", "delta": "Hel"}))
    await fresh_hub.publish(ev("/p", "message.part.delta",
        {"sessionID": "s1", "messageID": "m1", "partID": "t1", "delta": "lo"}))
    frames = await drain(sub, timeout=1.0)
    token_frames = [f for f in frames if f.get("type") == "token"]
    assert len(token_frames) == 1                      # coalesce：两 delta 一帧
    assert token_frames[0]["delta"] == "Hello"
    assert token_frames[0]["sessionID"] == "s1"

async def test_no_tokens_by_default(fresh_hub):
    sub = await fresh_hub.hubs.subscribe(directory="/p")   # 缺省
    # …同样发布 part.delta 序列…
    frames = await drain(sub, timeout=0.5)
    assert not any(f.get("type") == "token" for f in frames)
```

- [ ] **Step 2: 跑测试确认失败**（400 未实现 / token 帧不存在）
- [ ] **Step 3: 实现**——① `routes/events.py:10-93`：query 解析 `tokens`（仅 `"1"`/缺省合法），透传 `hubs.subscribe(..., tokens=...)`；② `hub_types.py`：`TOKEN_FRAME_TYPE` 常量 + `Subscriber` 无改动；③ `tokenstream/hub.py`：`events_tap` 注册表 + flush 出口（既有 100ms/4KiB flush 点 `:371-404` 对每个完成窗口拼接调用 tap，载荷 `{type:"token",sessionID,messageID,partID,delta}`）；④ `global_hub.py`：`subscribe(tokens=True)` 时把该 Subscriber 的 enqueue 注册为 tap（走既有 `try_put`，自动继承背压 guard）；无 token 订阅者时 tap 列表空、零开销。
- [ ] **Step 4: `./scripts/check.sh`** → ✅；Record diff

### Task L2-B: `/slimapi/permissions` 冷启动聚合

**Files:** Create `routes/permissions.py`、`tests/test_permissions.py`；Modify `app.py`（`:539-541` router 元组加 `permissions`）、`docs/specs/INTERFACE_MAP.md`（加 1 行）

**Interfaces:**
- Consumes: T0 `permissions_max_response_bytes`/`permissions_fanout`/`permissions_max_aggregate_bytes`；`_catalog_common.read_upstream_response`/`busy_response`/`raise_upstream_unavailable`（`routes/_catalog_common.py:1-215`）；questions 两阶段 fan-out 先例（`routes/questions.py`）。
- Produces: `GET /slimapi/permissions` → **envelope 镜像 questions（oracle §B-1）**：`{"items":[...],"errors":[...],"authoritativeDirectories":[...],"discoveryComplete":true}`；某 directory 失败时 items 聚合成功目录、errors 记账、authoritativeDirectories 仅含成功目录（防客户端 replace-all 丢 pending 卡）。

**Acceptance Criteria:**
- `B-C1`: B1 研究产物：上游 `GET /permission` handler 源码锚点（oracle 已预核验：`opencode-src/current/src/permission/index.ts:15,46,169` per-Location、裸数组 `PermissionV1.Request[]`；B1 补字段级形状）+ 响应形状 + directory 语义记入 `routes/permissions.py` 模块 docstring 与测试 fixture——**先读上游再写实现**（AGENTS.md 硬规则）。
- `B-C2`: 上游返回空 → sidecar `200 {"items":[],"errors":[],"authoritativeDirectories":[...],"discoveryComplete":true}`；上游有 pending → 投影瘦身（白名单由 B1 锁定 `PermissionV1.Request` 实际字段后决定——注意与 agent catalog 的 `Permission.Ruleset` 剔除先例是不同形状，勿混淆，oracle §B-1）。某 directory 失败 → items 聚合成功目录、errors 记账、authoritativeDirectories 仅含成功目录。
- `B-C3`: `check.sh` 路由↔文档门通过（INTERFACE_MAP 新行）；版本门禁覆盖新路由（缺 `X-Slimapi-Version` → 既有门禁行为）。

- [ ] **Step 1（B1 研究步）**: 读 `opencode-src/current` 中 `GET /permission` handler（oracle 预核验入口 `src/permission/index.ts:15,46,169` + HTTP handler 层），锁定：`PermissionV1.Request` 字段级形状（裸数组）、directory 头语义（per-Location 已确认 → fan-out）、pending/resolved 过滤口径；结论写入实现文件 docstring + 测试 fixture 常量。
- [ ] **Step 2: 写失败测试**（fixture 形状 = B1 锁定值；下面以假设形状示意，B1 后修正）

```python
# tests/test_permissions.py（upstream mock 用 conftest.upstream_factory；上游 = 裸数组）
EMPTY_ENVELOPE = {"items": [], "errors": [],
                  "authoritativeDirectories": ["/proj"], "discoveryComplete": True}

async def test_permissions_empty(client, upstream_factory):
    upstream_factory.route("GET", "/permission", json=[])   # 上游裸数组
    r = await client.get("/slimapi/permissions", headers=HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == [] and body["errors"] == []
    assert body["discoveryComplete"] is True

async def test_permissions_pending_projection(client, upstream_factory):
    upstream_factory.route("GET", "/permission",
        json=[PENDING_PERMISSION_FIXTURE])   # B1 锁定的 PermissionV1.Request 形状
    r = await client.get("/slimapi/permissions", headers=HDR)
    item = r.json()["items"][0]
    assert item["id"] == PENDING_PERMISSION_FIXTURE["id"]   # 白名单投影（B1 后细化断言）

async def test_permissions_partial_failure_keeps_successful_directory(client, upstream_factory):
    # 目录 A 成功返回 1 pending；目录 B 5xx → errors 记账、items 仍含 A 的卡
    ...
```

- [ ] **Step 3: 实现**——`routes/permissions.py` 镜像 questions 结构：transform 池准入 → per-directory fan-out 上游 GET（per-Location 已确认）→ `read_upstream_response(cap=permissions_max_response_bytes)` + 聚合上限 `permissions_max_aggregate_bytes` → 池内白名单投影 → questions 同款 envelope（items/errors/authoritativeDirectories/discoveryComplete）→ 413/busy/upstream-unavailable 错误语义全复用 `_catalog_common`；`app.py` 注册；INTERFACE_MAP 加行。
- [ ] **Step 4: `./scripts/check.sh`** → ✅（路由门过）；Record diff

### Task L2-CD-1: transform_busy 吸收（single-flight + 预算）

**Files:** Create `src/oc_slimapi/sse/singleflight.py`、`tests/test_full_absorb.py`；Modify `routes/messages.py`（`:347-424` full handler）

**Interfaces:**
- Consumes: T0 `transform_absorb_budget_seconds`；既有 `TransformPool`/`TransformBusy`（`transform.py:74-75,203-216`）；`_busy_response`（`messages.py:195-211`，503 形状不动）。
- Produces: `singleflight.SingleFlight`（**共享单元 = 上游 GET**：`async def fetch(key, factory)`，per-key in-flight 去重——direct `/full` 与 merged fan-out 共用同键去重，oracle §C-2；进程级实例 `singleflight.fulls`）；CD-2 复用同一实例。**transform 不进 single-flight 单元**：direct `/full` 在 fetch 外各自做 pool admission + offload（同 mid 并发时后续 fetch 已缓存，各自 transform 快速通过）；merged 的 transform 并入最终打包单次 offload。

**Acceptance Criteria:**
- `CD1-C1`: 20 并发同 mid `/full` 请求 → 上游 handler 恰被调 **1 次**，全部 200 且 body 一致。
- `CD1-C2`: transform 槽被占 **2.2s** 后释放（> `transform_wait_seconds=2`、< 预算 2.5s——1.9s 在无吸收的今天也能过，2.2s 才真正隔离吸收行为，oracle §D-1）→ 客户端 200；占满 >2.5s → 503 `transform_busy` + `Retry-After: 2`（形状与今天逐字节一致）。
- `CD1-C3`: 不同 mid 并发不互相 single-flight（key 隔离）；`./scripts/check.sh` ✅。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_full_absorb.py
async def test_single_flight_merges_concurrent_same_mid(client, upstream_factory):
    calls = {"n": 0}
    async def handler(request):
        calls["n"] += 1
        await asyncio.sleep(0.2)        # 放大竞争窗口
        return httpx.Response(200, json=FULL_MESSAGE)
    upstream_factory.route("GET", "/session/s1/message/m1", handler=handler)
    results = await asyncio.gather(*[
        client.get("/slimapi/messages/s1/full/m1", headers=HDR) for _ in range(20)])
    assert calls["n"] == 1
    assert all(r.status_code == 200 for r in results)

async def test_busy_absorbed_within_budget(client, upstream_factory):
    # 用慢上游 full 请求占住唯一 transform 槽（admission 先于上游 GET，槽横跨 GET+offload）
    async def slow(request):
        await asyncio.sleep(2.2)                  # >wait 2s、<budget 2.5s：隔离吸收行为
        return httpx.Response(200, json=FULL_MESSAGE)
    upstream_factory.route("GET", "/session/s1/message/m_hold", handler=slow)
    upstream_factory.route("GET", "/session/s1/message/m1", json=FULL_MESSAGE)
    holder = asyncio.create_task(
        client.get("/slimapi/messages/s1/full/m_hold", headers=HDR))
    await asyncio.sleep(0.2)                      # 确保 holder 拿到槽
    r = await client.get("/slimapi/messages/s1/full/m1", headers=HDR)
    assert r.status_code == 200                   # 槽 2.2s 释放 < 预算 2.5s → 吸收（无吸收则 admission 2s 超时 503）
    await holder

async def test_busy_over_budget_503_shape_unchanged(client, upstream_factory):
    async def slow(request):
        await asyncio.sleep(3.5)                  # 超预算
        return httpx.Response(200, json=FULL_MESSAGE)
    upstream_factory.route("GET", "/session/s1/message/m_hold", handler=slow)
    upstream_factory.route("GET", "/session/s1/message/m1", json=FULL_MESSAGE)
    holder = asyncio.create_task(
        client.get("/slimapi/messages/s1/full/m_hold", headers=HDR))
    await asyncio.sleep(0.2)
    r = await client.get("/slimapi/messages/s1/full/m1", headers=HDR)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "transform_busy"
    assert r.headers["Retry-After"] == "2"        # 形状与今天逐字节一致
    await holder
```

- [ ] **Step 2: 确认失败**（calls==20 / 吸收未实现即 503）
- [ ] **Step 3: 实现**——`singleflight.py`（`dict[key, asyncio.Future]` + 完成清理；异常传播给全部 waiter）；full handler 改造：**pool admission 先于 fetch**（保持现行为 `messages.py:291` admission→GET 顺序；TransformBusy 时上游请求未发出，重试不放大上游负载）→ admission 成功后 `await singleflight.fulls.fetch(("full", sid, mid), _upstream_get)`（并发同 mid 共享同一次 GET）→ `pool.offload(strip_diagnostics_and_pack)` → 释放。admission 重试循环：捕获 TransformBusy → 按剩余预算收窄每次等待（给 `TransformPool` 加可选 per-attempt timeout 参数，不改抛出语义，oracle §D-1；否则最坏 2×2s=4s 超预算）→ 预算耗尽 re-raise → 外层 `_busy_response`（503 形状零变化）。
- [ ] **Step 4: `./scripts/check.sh`** → ✅；Record diff

### Task L2-CD-2: `mode=merged` 服务端合并

**Files:** Modify `routes/messages.py`（`:257-344` 列表 handler）；Create `tests/test_messages_merged.py`

**Interfaces:**
- Consumes: CD-1 `singleflight.fulls`（**raw fetch 语义**：merged 的 full 抓取**不经 per-full pool admission**——由 `merged_fanout` 信号量限流 + `merged_max_bytes` 累计记账，oracle §C-2；理由：`max_transforms` 默认 1，per-full admission 会使 16 个 full 串行持槽横跨网络 GET，饿死并发 transform）；T0 `merged_fanout`/`merged_max_fulls_per_page`/`merged_max_bytes`；`skeleton_messages`/`thin_placeholder` 判定（`skeleton.py:386-395`）；`strip_diagnostics_message`（`skeleton.py:421-454`）。
- Produces: `GET /slimapi/messages/{sid}?mode=merged`（响应 envelope 仅加性：placeholder part 被内联 full 替换）。

**Acceptance Criteria:**
- `CD2-C1`: 列表 2 条消息、其中 1 条含 thin_placeholder part → merged 响应中该 part 为 full 投影（`omitted:false`，diagnostics 已剥），其余消息与 skeleton 模式逐字节一致；`X-Next-Cursor` 不变。
- `CD2-C2`: 当页 placeholder 消息数 > `merged_max_fulls_per_page` → 前 16 条内联、其余保持 skeleton 原样（降级不 413、不报错）。
- `CD2-C3`: `mode=full` 及其他未知值 → **静默忽略**（现行为锁定，`messages.py:269` 文档化容忍；**不 400**——oracle §C-1 加性承诺）；仅字面 `merged` 激活；缺省 mode 行为与今天逐字节一致（既有 tests 全绿）。
- `CD2-C4`: merged 内部 full 抓取与直接 `/full` 并发时经 `singleflight.fulls` 同键去重（上游仅 1 次调用）。
- `CD2-C5`: merged 期间并发 direct `/full` 不被 merged fan-out 饿死（merged 不持 per-full 池槽；最终打包单次 offload 争用遵循既有 busy 语义）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_messages_merged.py（上游 mock：列表 + full 两级路由）
LIST_BODY = {"items": [MSG_WITH_PLACEHOLDER, MSG_PLAIN_TEXT]}   # msg1 含 thin_placeholder part
FULL_BODY = {"id": "msg_1", "parts": [PART_TOOL_WITH_DIAGNOSTICS, ...]}

async def test_merged_inlines_full_for_placeholder(client, upstream_factory):
    upstream_factory.route("GET", "/session/s1/message", json=LIST_BODY)
    upstream_factory.route("GET", "/session/s1/message/msg_1", json=FULL_BODY)
    merged = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)
    plain = await client.get("/slimapi/messages/s1", headers=HDR)      # 缺省对照
    assert merged.status_code == plain.status_code == 200
    assert merged.headers.get("X-Next-Cursor") == plain.headers.get("X-Next-Cursor")
    part = next(p for p in merged.json()["items"][0]["parts"]
                if p["id"] == "part_tool")
    assert part.get("omitted") is False            # placeholder → full 内联
    assert "diagnostics" not in orjson.dumps(part) # strip_diagnostics 生效
    assert merged.json()["items"][1] == plain.json()["items"][1]  # 无 placeholder 消息不变

async def test_merged_degrades_beyond_page_cap(client, upstream_factory):
    upstream_factory.route("GET", "/session/s1/message",
                           json={"items": [MSG_WITH_PLACEHOLDER(f"msg_{i}") for i in range(20)]})
    calls = {"n": 0}
    async def full(request):
        calls["n"] += 1
        return httpx.Response(200, json=FULL_BODY)
    upstream_factory.route("GET", "/session/s1/message/msg_0", handler=full)  # …msg_0..19 同路由前缀
    r = await client.get("/slimapi/messages/s1?mode=merged", headers=HDR)
    assert calls["n"] == 16                        # merged_max_fulls_per_page=16
    items = r.json()["items"]
    assert items[15]["parts"][0].get("omitted") is False   # 前 16 内联
    assert items[16]["parts"][0]["type"] == "thin_placeholder"  # 其余降级原样

async def test_merged_unknown_mode_ignored(client, upstream_factory):
    # 现行为锁定：mode=full 及未知值静默忽略（messages.py:269 文档化容忍，不 400）
    upstream_factory.route("GET", "/session/s1/message", json=LIST_BODY)
    r = await client.get("/slimapi/messages/s1?mode=full", headers=HDR)
    plain = await client.get("/slimapi/messages/s1", headers=HDR)
    assert r.status_code == plain.status_code == 200
    assert r.json() == plain.json()               # 未知 mode == 缺省，逐字节一致
```
- [ ] **Step 2: 确认失败** → **Step 3: 实现**——列表 handler 加 `mode` 解析；merged 分支：skeleton 投影（既有 `pool.offload` 一次）→ 收集当页 placeholder (mid 集合，cap `merged_max_fulls_per_page`) → `asyncio.gather`（Semaphore `merged_fanout`）逐个 `singleflight.fulls.run(...)` → 累计字节 ≤ `merged_max_bytes` 内做替换 → 单次 `pool.offload` 投影打包（避免 N 次串行 transform）→ 超预算项原样保留。
- [ ] **Step 4: `./scripts/check.sh`** → ✅；Record diff

### L2 评审门

- 每 lane：fixer-ds 实现（TDD）→ rev-gpt 评审该 lane diff → 修复循环至 PASS
- 层级：rev-cgpt 审 L2 全部 diff（派发前 `review_prep` + rid 注入；对照 v2-contract 加性 rev 一致性）

---

## L3 — 直连退役配套（本仓 docs 1 lane + ocdroid 侧外部 lane）

### Task L3-1（本仓）: 契约与文档配套更新

**Files:**
- Modify: `docs/specs/v2-contract.md`（加性 rev：§2 端点表 `tokens`/`mode` query param、§3 SSE `token` 帧类型 + 帧语义声明（无 partEventRevision/无 directory/迁移期互斥，oracle §A-2）、§2 `/slimapi/permissions` 端点、features 清单）
- Modify: `docs/specs/CLIENT_CHANGES.md`（新增「4 能力接入」章节 + 「直连退役」章节）
- Modify: `docs/specs/INTERFACE_MAP.md`（permissions 路由行已由 L2-B 落；此处补 §3 帧类型表 token 行）
- Modify: `docs/manual/traffic-accounting.md`（新 bucket 口径：`/slimapi/permissions` 归 slim bucket；events 非 gzip 下 token 帧增量流量说明，oracle §5）
- Modify: `CHANGELOG.md`（`[Unreleased]` 定稿汇总：4 能力 + 直连退役说明）
- Modify: `README.md`（拓扑说明：ocdroid 仅 14097；14096 不再服务 ocdroid）

**Acceptance Criteria:**
- `T-L3-1-C1`: `./scripts/check.sh` 通过（含路由↔文档一致性门）。
- `T-L3-1-C2`: v2-contract diff 仅加性（grep 校验：无既有行删除，除版本历史表外）。
- `T-L3-1-C3`: CLIENT_CHANGES 含 ocdroid 侧改动清单：tokenCoalesce 接入（`tokens=1` + 弃用 per-session stream）、permissionEvents 接入（删轮询）、serverMerge 接入（`mode=merged` + 删客户端合并协调器）、transformAbsorb（删 503 处理路径）、C1 图片改走 catch-all `GET /file`、C3 连接测试改打 `/slimapi/health`、退役 slim=false 分支/Manual 源/Standard 轴/迁移残留。

- [ ] **Step 1: v2-contract 加性 rev**（逐节编辑，保留既有行）
- [ ] **Step 2: CLIENT_CHANGES / INTERFACE_MAP / CHANGELOG / README 同步**
- [ ] **Step 3: `./scripts/check.sh`** → ✅；Record diff

### L3 评审门

- lane 评审：rev-gpt（docs diff）
- 层级评审：rev-cgpt 审 L3

### ocdroid 侧外部 lane（本计划只锁契约，实施由 ocdroid 仓库承担）

依赖：L2 四能力 health flag 上线后按 flag 渐进迁移；C1/C3 修复可在 L2 期间先行（不依赖 L2）。
范围：删 `slim` 开关/Manual 连接源/`StreamingMode.Standard` 轴/4097→14097 迁移残留/4096 默认值；C1 `HttpImageHolder` 改走 sidecar catch-all；C3 `checkHealthFor` 改 `/slimapi/health`；L2 能力落地后删 ~2200 行客户端协调器。

---

## 终审与发版

1. rev-kimi 终审全分支（L1+L2+L3 全部 diff vs `e68e337`，对照本计划验收矩阵），终审 checklist（oracle 补充）：
   - health features 四 flag ↔ 四能力落地状态一致（防 T0 预置与 lane 实际落地漂移）
   - INTERFACE_MAP §3 帧类型表已更新（`token` 帧；路由门只校验路由存在性，参数级变更靠此处兜底）
   - `git diff` tests/ 仅新增文件；mode 容忍语义既有测试（如存在）零修改
   - v2-contract diff 仅加性（无既有行删除）
2. 终审 PASS → `./scripts/release.sh minor`（CHANGELOG [Unreleased] 落版本号）
3. 发版后通知 ocdroid 侧 orchestrator 对齐版本，ocdroid 侧按自身节奏实施外部 lane

## Criterion Ownership Matrix

| Criterion ID | Spec requirement | Owner task | Cross-task deps | Verification | Final-only? |
|---|---|---|---|---|---|
| T1-C1/C2 | 基线证据归档 | L1 Task 1 | — | 文件内容审查 + check.sh | N |
| T2-C1/C2 | AGENTS.md 版本行 | L1 Task 2 | — | readlink 一致 + check.sh | N |
| T0-C1/C2 | features 公告 + config 字段组 | L2-T0 | — | `pytest tests/test_health_features.py` PASS + check.sh | N |
| A-C1..C5 | tokens param + coalesced token 帧 + flush loop 账本 | L2-A | T0（features 公告） | `pytest tests/test_events_tokens.py` PASS + behavior_lock 零改动 | N |
| B-C1..C3 | /slimapi/permissions 聚合（questions envelope） | L2-B | T0（config 字段） | `pytest tests/test_permissions.py` PASS + 路由门 | N |
| CD1-C1..C3 | single-flight fetch + 预算吸收 | L2-CD-1 | T0（budget 字段） | `pytest tests/test_full_absorb.py` PASS | N |
| CD2-C1..C5 | mode=merged 合并（raw fetch 不持池槽） | L2-CD-2 | T0（merged 字段组）、CD-1（singleflight） | `pytest tests/test_messages_merged.py` PASS | N |
| T-L3-1-C1..C3 | 契约/文档配套 | L3 Task 1 | L2 全 lane | check.sh + diff 加性校验 | N |
| 全局：X-Slimapi-Version==2 不变 | 版本门禁 | — | 所有 | versioning 测试 + 契约审查 | Y |
| 全局：旧客户端零行为变化 | 向后兼容 | — | 所有 | 现有 1425 tests 全绿（不删不改既有断言） | Y |
| 全局：token 帧不破坏 events T3 语义 | 背压不变 | — | L2-A | A-C4 + behavior_lock 背压用例 | Y |
| 全局：契约 diff 仅加性 | 契约权威 | — | L2/L3 | rev-cgpt 层审 grep 校验（无既有行删除） | Y |
