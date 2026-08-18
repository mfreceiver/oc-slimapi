# ocdroid 侧改造方案（B5a/B5b 消费者适配）

> **owner 终态裁决 2026-08-18**：协议封顶 4 系，(3,4) 永久双版本，5.0.0/B6-2/v2 退役取消。
>
> 日期：2026-08-17｜归属：三项目并行开发体系·第二份（基准 oc-slimapi v2.2 方案；ocdroid/webui 三份并列，供 omni-orch 统管并行开发）
> 上游基准：`system-architecture-proposal-2026-08-17.md`（v2.2，下称 **v2.2**）§3.1/§3.2/§3.2a/§3.4/§3.5/§6/§7/§8/§9
> 现状输入：`ocdroid-needs-audit-2026-08-17.md`（下称 **audit**）+ 实读 ocdroid 代码（路径均标注 `文件:行号`）
> 唯一可写文件：本文；**ocdroid 仓库零改动**（本方案为规划稿，供 omni-orch 派发实施）

---

## 1. 目标与范围

### 1.1 承接 v2.2 §6 ocdroid 节（B5a 先行 / B5b 跟进）

ocdroid 作为消费者 1（移动端），在 v2.2 体系中的全部改造诉求收敛为：

| v2.2 §6 条目 | 本方案承接 | 阶段 |
|---|---|---|
| B5a.1 识别 `capabilities["4"]`（不存在 → 继续 v=3）；未知能力容忍 | §2.1 T-A1（**R2：versions 先行三概念协商**） | **B5a（P2，先于 sidecar 4.0.0）** |
| B5a.2 DirectoryHeaderInterceptor 豁免 `/slimapi/sessions` directory 注入（v4 400 防护） | §2.1 T-A2 | **B5a** |
| B5a.3 status 改单次全局调用（即刻可做，不依赖 v4） | §2.1 T-A3 | **B5a** |
| B5a.4 q/p asked 帧载荷直投评估（若 sidecar B1b 核对为已完整 → 纯客户端改动） | §2.1 T-A4（**R2：即时面=permission.asked/resolved + question.asked；question 关闭走对账**） | **B5a（依赖 B1b 结论）** |
| B5b.1 会话列表全局拉取（`parent=none` 替代 `roots=true`；省略=all 语义知悉）+ WorkdirGroups 本地分组 | §2.2 T-B1/T-B2 | **B5b（sidecar B3a 后）** |
| B5b.2 翻页 cursor-aware（低频 workdir 首页可能不在第一屏——组内空 ≠ 无会话） | §2.2 T-B2（**R2：受限 drain + complete&&!degraded 空组判据**） | **B5b** |
| B5b.3 C1-C3 修复（图片经 slimapi /file/content 反代、健康探测改 /slimapi/health+/ready） | §2.2 T-B3（**现状核实修订，见 §1.3**） | **B5b** |
| B5b.4 404-sticky + 三形状解析随 v4 退役（生产流量证明后） | §2.2 T-B5（**R2：4.0.0 起 v4 连接短路 sticky + Last-Event-ID；机制删除 5.0.0/B6**） | **B5b 末期/B6（硬前提见 §2.2）** |
| B5b.5 context 用量接入；agent/model 切换接入 | §2.2 T-B4 | **B5b（依赖 P1 的 B4 路由）** |

### 1.2 明确不做什么

1. **不做扇出**（v2.2 附三件套分工）：ocdroid 不承担跨目录聚合——q/p 聚合、全局会话目录、目录发现全部由 sidecar 全局门面提供；客户端只做**本地分组/过滤**与 mTLS/离线缓存。冷启动 `restoreWorkdirs` 的 per-workdir fan-out（`ConnectionInitialDataLoader.kt:114-121`）正是 B5b 要消除的对象，**不是**扩展它。
2. **不做能力探测补偿（v4 起）**：v4 起能力 = 静态能力键广告（v2.2 §3.1「capabilities 边界」：`auxiliaryFilters` 等静态键存在即广告，不随 DB 抖动）；瞬态可用性 = 503 + `/slimapi/health` 扩展字段 + metrics。客户端**禁止**自造 404-sticky / 探测循环补偿 v4 能力（404-sticky 机制仅服务 v3 时代 legacy sidecar，B5b 末期随生产流量证明退役）。v4 瞬态不可用（503 `auxiliary_unavailable`）走**显式错误处理**（Retry-After + degraded 披露），**不**回退 v3。
3. **不做 PTY / OAuth / Project copy**（v2.2 §1.4 明确不做；决策 4）。
4. **不做 zstd**（v2.2 决策 5）。
5. **不做 /global/event 直连**：SSE 事件源唯一入口 = `/slimapi/events`（现状已如此，`SSEClient.kt` `SLIM_EVENTS_PATH="/slimapi/events"`）；v2.2 §3.2 事件源实证（GlobalBus 携带全部 booted 实例流）由 sidecar 收编，客户端零新增连接。

### 1.3 事实修订（实读代码 vs audit/routing 文档脱节）

探索中发现 **audit ④ C1/C2/C3 与 `slim-mode-api-routing.md` C 桶描述已过时**，B5b 的 C1-C3 任务实为「残留清理 + allowlist 场景验证」，非从零修复：

| 违规 | audit 描述 | 代码现状（实读） | 结论 |
|---|---|---|---|
| **C1** 图片直连 | `HttpImageHolder.kt:143-148,282-316` 独立 client 直连 | v3.0.1 已落地 M-C1a+c：`/file/<path>` 图片改经 repository 层 `getFileContent` 反代（`HttpImageHolder.kt:311-313` 注释），`SlimImageUrlRewriter` 仅做缓存 key 归一化；**外站图床（host 不匹配）与非 /file/ URL 仍直抓**（D 桶合理保留） | 大部分已修复；剩余 = 外站直抓保留 + allowlist fail-closed 场景验证（v2.2 §3.5：`/slimapi/file/**` 空 allowlist → 403） |
| **C2** 证书捕获直探 /global/health | `OpenCodeRepository` mTLS/TOFU | **L7 已删除 TOFU machinery**（`ConnectionHealthProbe.kt:54-60`「the pre-L7 TOFU trust machinery … was DELETED in L7」），`captureServerCert` 符号已彻底删除（全仓无定义无调用，仅 5 处 KDoc 历史引用）；代码中无 `/global/health` 探活路径 | 已修复（L7 删除）；仅剩 `HttpHeaders.kt:54` CACHEABLE_PATHS 残留 `"/global/health"` 常量（不产生流量）+ `ServerCompatProfile.kt:10` 陈旧注释 |
| **C3** host 测试直探 /global/health | `OpenCodeRepository` | `ConnectionGateway.kt:75-85` checkHealth 与 `:197-241` checkHealthFor **均走 `{base}/slimapi/health?v=3`**（裸 client + `X-Opencode-Skip-Dir: 1` + identity headers）；legacy `/global/health` 死臂已随 L3-波2 删除（`:76-77`、`:206-207`） | 已修复（L3-波2）；`/slimapi/ready`（`SlimApi.kt:274`）已存在，仅 ActionsViewModel 重启轮询用（`ActionsViewModel.kt:143,271,306`）——B5b 补连接引导场景 |

> **必做修订**（R2 从开放问题移入必做清单，见 §7）：audit 与 `slim-mode-api-routing.md` C 桶需随本方案同步修订（随 T-B3 落地），避免 omni-orch 按过时描述派发「从零修复」任务。

---

## 2. 阶段执行计划（对齐 v2.2 §7 发布路线）

### 2.1 B5a（P2，先于 sidecar 4.0.0 发布，最高优先）

v2.2 §8：`B5a | 消费者兼容版（= P2，先于 B3 发布）：capabilities["4"] 探测 + 未知容忍 + v3 回退 + ocdroid 拦截器豁免 + status 单次全局调用 + webui q/p 直投（若零 wire 变更） | B0 | 低`。

sidecar 侧 3.3.0（P1，加性零 breaking）发布后即可开工；目标：**4.0.0 上线时 v3 客户端仍在协商范围内（available 含 3）且功能无损**。

---

#### T-A1 版本协商（versions 先行）→ capabilities["4"] 探测 + 未知容忍 + 空交集 fail-closed

> **R2（Blocking 1）核心修订**：原方案「探测挂在 v=3 health 门后」存在时序死锁——`ConnectionGateway` 健康探活硬编码 `?v=3`（`ConnectionGateway.kt:112/:216`），而 `isSlimapiClientAccepted()` fail-closed 判 `3 ∈ accepted`（`ServerCompatProfile.kt:367-371`）；sidecar 5.0.0 accepted=[4,4] 时 **health 本身 400 → 连接判死 → versions 探测永不执行**，§4.2 矩阵右列按自身时序不可达。R2 起改为**三概念模型 + versions 先行**（与 webui 方案 W-lane 同解法，措辞对齐）。

- **涉及模块/文件**：
  - `data/repository/http/SlimapiContract.kt`（`SLIMAPI_CLIENT_VERSION=3` 硬编码 → **退役为 legacy 语义**；`SLIMAPI_HEALTH_PATH`）
  - `data/repository/ServerCompatProfile.kt`（`slimapiServerApiVersion`/`slimapiAcceptedMin/Max`/`isSlimapiClientAccepted()` `:367-371`/`updateSlimapi(payload)` `:357-364` → 承载三概念状态机）
  - `data/repository/gateway/ConnectionGateway.kt`（`probeSlimapiHealth` `:97-148`、`parseSlimapiHealth` `:150-195`、`checkHealthFor` `:197-241`——**health 探活 selector 硬编码 `?v=3` 改 selectedWireVersion**）
  - 新增 `data/api/VersionsApi.kt`（`GET /slimapi/versions`——v3 契约已存在，AGENTS.md 版本协商；**selector 豁免**：`versions.py:5-6`「Exempt from the selector judgement… reachable without any v」，**no-store**：`versions.py:59-83`「Cache-Control: no-store、无 ETag，discovery 必须始终 revalidate」）
  - `data/repository/http/V3SelectorInterceptor.kt`（`?v=3` 恒追加 → 改 selectedWireVersion 感知）
- **三概念模型**（R2 引入；与 webui W-lane 措辞一致）：
  - **`clientSupportedWireVersions`** = 本客户端支持的 wire 版本集（B5b 适配完成后 = `{3, 4}`；B5a 阶段 = `{3}`）；`SLIMAPI_CLIENT_VERSION=3` 常量语义**退役**——不再是「唯一版本」，而是 `clientSupportedWireVersions` 的下界/legacy 锚点，仅用于推导与兼容期断言。
  - **`serverAcceptedRange`** = `GET /slimapi/versions` 响应的可用版本集（现 `available: [3]`，`versions.py:37`；v4 契约定稿后按 `accepted`/`available` 实际字段名读取，留适配缝——以 v4-contract §3 为准）。
  - **`selectedWireVersion = max(clientSupportedWireVersions ∩ serverAcceptedRange)`** = 每次连接协商出的生效 wire 版本，**写死到连接存续期的请求面**（拦截器/selector/健康探活共用，经 `ServerCompatProfile`，同 connectionIncarnation 范式）。
- **实现要点**：
  1. **探测时序（versions 先行）**：连接建立后**第一步**打 `GET /slimapi/versions`——**裸 client、无 selector**（端点本就 selector 豁免 + no-store，为此设计，`versions.py:5-6,59-83`），不带 `?v=`、不做版本门判断（该端点不受任何版本门约束）。解析 `available`/`accepted` 集 + `capabilities` 对象 → 计算 `selectedWireVersion` 并写入 `ServerCompatProfile`（新字段：`selectedWireVersion: Int?` + `capabilitiesV4: Boolean` + 四能力键位，均 fail-closed 默认 false）。**探测失败 ≠ 空交集**（见要点 5 两分支）。
  2. **capabilities["4"] 键读取（R3 语义冻结）**：`capabilitiesV4` **冻结定义为纯探测结果**——「versions 响应 `capabilities["4"]` 键存在」（与 `selectedWireVersion` 是否选中 v4 **无关**：B5a dormant 期 `selectedWireVersion=3` 时同样探测并存储；选中与否只影响请求面，不影响该位）。功能启用门控一律用 **`selectedWireVersion==4` 且对应能力键存在**（双条件）。`versions.py` 现状 `capabilities` 按版本字符串键控的结构即此模型（`versions.py:16,40-56`）；静态能力键四枚（v2.2 §7 修订清单 §3）：`globalSessions`/`sseReplay`/`qpImmediateFull`/`auxiliaryFilters`。
  3. **逐键门控（R3 新增，替代单布尔压扁）**：四键**逐键读取、逐键门控**——**缺键 = 该功能 fail-closed**（缺键时的最终行为 = **下方「缺键唯一行为表」按 key × available 集的唯一格子**，跨段一致、无通配规则），门控点：
     - `sseReplay` → `Last-Event-ID` 发送（T-B5a；缺键 → 不发，保持 no-replay 现状分支）；
     - `globalSessions` → v4 全局列表切换与 v3 per-directory 路径退役（T-B1；缺键 → **不退役 v3 路径**——最终行为由要点 3 末唯一规则函数决定：`available` 含 3 → 整体重协商覆写 `selectedWireVersion=3`、全端点切 v3；`available=[4]` → 保持 v4 禁用新列表 UI，**无「保持 v3 拉取或禁用」二分支**）；
     - `qpImmediateFull` → q/p 载荷直投（T-A4「已完整」分叉的启用条件；缺键 → 保持轮询/对账现状）；
     - `auxiliaryFilters` → `archived`/`parent` 过滤参数与过滤 UI 暴露（T-B1/T-B2；缺键 → 过滤 UI 不暴露、请求不带过滤参数）；
**缺键唯一行为表（R5 冻结——消除 R4「任一缺键→整体降级」通配与 :78/:80-81 「仅关单功能」两套互斥）**：按**「该键在 v3 是否有功能等价物」**推导每键行为，**每键唯一、跨段一致**，无通配规则：
      - *推导依据*：`globalSessions` → v3 有 per-directory 目录级列表（`/slimapi/sessions?directory=&roots=true`，`SessionGateway.kt:53-55`）——[3,4] 缺此键时降回 v3 保留目录级浏览，**比 v4 禁用列表更可用**；`sseReplay` → v3 契约冻结无 replay（T9 no-replay，`SSEClient.kt:245-259`）——降 v3 也得不到 replay，**降级无意义**；`qpImmediateFull` → v3 未广告 q/p 直投时本就回轮询路径（`InteractionGateway.kt:225-272`）——轮询是**客户端本地行为、非 wire 承诺**，保持 v4 亦可回轮询；`auxiliaryFilters` → v3 无三态过滤（过滤是客户端本地 `recentSessionsInWorkdirScope`，`WorkdirGroups.kt:22-38`）——降 v3 无服务端过滤，**降级无意义**。
      - **每键唯一行为表（四行；行为 = 缺键时的最终下落）**：

        | 能力键 | `available` 含 3（[3,4] 缺键） | `available=[4]`（缺键） |
        |---|---|---|
        | `globalSessions` | **整体降 3**：`selectedWireVersion` 覆写为 3（单值）、全端点一致切 v3（health/SSE/token stream），v3 per-directory 列表路径续用，v4 列表 UI 隐藏 | 保持 v4：新列表 UI 禁用 + 提示，**v3 请求数 = 0** |
        | `sseReplay` | **保持 v4**：不发 `Last-Event-ID`（no-replay），**不降级** | 保持 v4：不发 `Last-Event-ID`（no-replay） |
        | `qpImmediateFull` | **保持 v4**：q/p 直投不启用，回轮询/对账 | 保持 v4：q/p 直投不启用，回轮询/对账 |
        | `auxiliaryFilters` | **保持 v4**：`archived`/`parent` 过滤 UI 不暴露、请求不带过滤参数 | 保持 v4：过滤 UI 不暴露、请求不带过滤参数 |

      - **单值语义声明**：`selectedWireVersion` 是**唯一事实源**——整体降 3（仅 `globalSessions` 缺键且 [3,4]） = **覆写该值**（不引入 `effective` 等并存值）；重协商触发条件与频率 = **每次 versions 探测时重算**（非仅连接建立；`onConnectionReconfigured` 重置后同参数重算）。
  4. **健康探活 selector 改用 selectedWireVersion**：`probeSlimapiHealth`/`checkHealthFor` 的 `?v=3` 硬编码（`ConnectionGateway.kt:112/:216`）→ `?v=${selectedWireVersion ?: 3}`——**versions 协商完成后才允许 health 探活**；versions 探测失败（要点 5 分支 A）→ 视为协商失败走 v3 回退（不翻位、不 sticky 错误），health 继续用 v=3（现状行为，零回归）。
  5. **探测失败 vs 空交集两分支（R3 显式区分，修复 R2 双策略矛盾）**：
     - **分支 A —— 探测失败**（404/5xx/超时/畸形 JSON）：**重试 ≤2 次**（Retry-After 感知；此重试属协商层，独立于弱网重试窗），仍失败 → **按静默 v3 回退**（`selectedWireVersion=3` 兜底，health/请求面不变）——**探测不可用 ≠ 版本不兼容**（网络断/超时/畸形响应不能证明服务端只收 v4），走既有连接失败 UX，**不弹升级文案**；
     - **分支 B —— 探测成功但空交集**：versions **200 成功响应**证明 `clientSupportedWireVersions ∩ serverAcceptedRange == ∅`（未适配客户端 {3} 遇 5.0.0 [4]）→ **拒绝连接**，UI 呈现**可读错误「服务端要求升级客户端」（专用文案，区别于通用连接失败）**；`isSlimapiClientAccepted()` 语义迁移为「交集非空」（不再判单一常量）。**仅未适配客户端（{3}）遇 5.0.0（[4]）走分支 B**；适配后客户端（{3,4}）× 5.0.0（[4]）→ **成功选 v4，不 gate 失败**。
  6. **未知容忍**：`capabilities` 中不认识的键（未来 v5+）**一律忽略**，不做任何分支；`selectedWireVersion==3` → 全部 slim 请求保持 `?v=3`（现状行为）。
  7. **B5a dormant / B5b 激活分阶段**：B5a 只做「探测 + 协商 + 存储」，请求面保持 v=3（selectedWireVersion=3 被强制或 B5b 才翻转）；B5b 起请求面按 selectedWireVersion=4 切换（T-B1 起）。拦截器同步：T-A1 的 selectedWireVersion 判定结果供 T-A2 消费（`DirectoryHeaderInterceptor` 经 `ServerCompatProfile` 读，同 connectionIncarnation 范式）。
- **验收标准**：
  - 连旧 sidecar（≤3.2.x，`available=[3]` 无 capabilities["4"]）：`selectedWireVersion=3`，所有请求仍 `?v=3`，功能与改造前逐项一致（会话/消息/q/p/digest/health 全流程）。
  - 连 4.0.0（`available=[3,4]`，capabilities["4"] 存在）：**B5a 阶段（client={3}）**：协商 `selectedWireVersion=3`，`capabilitiesV4=true` **纯探测结果持久化**（dormant——证明「探测+存储」正确，请求面不变）；**B5b 后（client={3,4}）**：协商 `selectedWireVersion=4`，请求面切 v4——验收分两段覆盖 dormant/激活分离。
  - 连 5.0.0（`available=[4]`）适配后客户端：`selectedWireVersion=4`，**成功选 v4**，全流程可用（B5b 完成后验证）。
  - 连 5.0.0（`available=[4]`）未适配客户端：**fail-closed 拒绝连接**，UI 呈现「服务端要求升级客户端」可读错误（非通用连接失败）。
  - **探测失败（分支 A）**：versions 端点 404/5xx/超时/畸形 JSON → **重试 ≤2 次仍失败 → 静默回退 v3**（`selectedWireVersion=3` 兜底），无崩溃无 UI 报错——**不弹升级文案**（探测不可用 ≠ 版本不兼容；单测覆盖 4 个失败形态 × 重试后仍失败/重试后成功两路径）。
  - **探测成功但空交集（分支 B）**：versions 200 成功响应且 `clientSupportedWireVersions ∩ serverAcceptedRange == ∅` → fail-closed 拒连 + 可读文案（分支 A 与 B 行为显式不同，单测断言分别触发）。
  - 未知能力键（构造 `capabilities: {"5": {...}}`）→ 无行为变化。
  - **逐键门控（B2/R3 + R4 规则唯一化）**：`capabilities["4"]` 存在但缺 `sseReplay` → 不触发 `Last-Event-ID` 发送（no-replay 保持）；缺 `globalSessions` → v3 per-directory 列表路径不退役（**结果唯一：`available` 含 3 → 整体重协商覆写 `selectedWireVersion=3` 并全端点切 v3；`available=[4]` → 保持 v4 禁用新列表 UI**）；缺 `qpImmediateFull` → q/p 直投不启用；缺 `auxiliaryFilters` → archived/parent 过滤 UI 不暴露——四键各自 fail-closed 断言，**无「降级或禁用」二分支（R4：唯一下落点 = 唯一规则函数）**。
- **测试/验证方式**：
  - JVM 单测：`ServerCompatProfileTest`（三概念状态机：交集计算/空交集/探测失败兜底；`capabilitiesV4` 纯探测位生命周期：协商→bump→`onConnectionReconfigured()` 重置重协商——**断言 dormant 期 `selectedWireVersion=3` 时 `capabilitiesV4=true` 可独立存储**）；`ConnectionGatewayTest`（mock /slimapi/versions 各响应 + health selector 随 selectedWireVersion 变化 + 探测失败重试路径）。
  - B2 逐键门控单测（R4 唯一规则断言）：`[3,4] 缺少 globalSessions → 整体降级：断言 selectedWireVersion 实际变更为 3 且后续所有请求（含 health/SSE）均为 ?v=3`；`[4] 缺少 globalSessions → 保持 selectedWireVersion=4、断言 v3 请求数=0 + 新列表 UI 禁用`；`[4] 缺少 sseReplay → 不发 Last-Event-ID`——逐键参数化，**每个缺键场景断言唯一结果（无「降级或禁用」二义）**；**其他三键 [3,4] 保持 v4 断言（R5 补）**：`[3,4] 缺少 sseReplay/qpImmediateFull/auxiliaryFilters 各自 → selectedWireVersion 保持 4、不发生整体降级、对应单功能禁用/回退`（逐键参数化 × available 两态全覆盖，与唯一行为表逐格对齐）。
  - 集成：`ConnectionViewModelTest` 连接的 mock sidecar 升级/降级切换场景（3→4→5 三档）。
  - 真机手工：连生产 sidecar 3.x 确认零回归（`TrafficTracker` 断言全部请求 `v=3`）；5.0.0 拒绝态 UI 文案冒烟。
- **依赖**：sidecar **B0 定稿**（v4-contract §3 capabilities 键名/形状 + versions 响应 `accepted` 字段名冻结——协商代码按契约写，B0 未出前以 v2.2 §7 清单 + `versions.py` 现状为准并留适配缝）；sidecar **3.3.0（P1）已发布**（versions 端点 v3 即存在，无硬依赖，但 B1a digest changed 消费见 T-A5 依赖 3.3.0）。

---

#### T-A2 DirectoryHeaderInterceptor 豁免 `/slimapi/sessions` directory 注入

- **涉及模块/文件**：`data/repository/http/DirectoryHeaderInterceptor.kt`（`:46-124`：`X-Opencode-Directory` header ↔ `/slimapi/` 路径 `?directory=` query 双形态转换）
- **实现要点**：
  1. **豁免规则（精确路径）**：当 `selectedWireVersion == 4` **且** 请求路径精确为 `GET /slimapi/sessions`（不含子路径）时，**不**将 header 移入 query（v2.2 §3.1「v4 sessions 豁免 ocdroid DirectoryHeaderInterceptor 注入——v4 sessions 会 400 `directory_retired_in_v4`」）。
  2. **内部 header 剥离 + 显式 query 不吞（R2 修订）**：v4 豁免时，拦截器**剥离内部注入的 `X-Opencode-Directory` header**（客户端自带的目录语义在 v4 sessions 已失效，header 残留会造成下游「此请求本不该有目录」的歧义——行为以 v4-contract §4 定稿为准，预留适配缝）；**显式 `?directory=` query 不吞**——客户端若自行携带 query（理论上 v4 客户端不会，但测试/透传路径可能），**原样透传让 sidecar 400 `directory_retired_in_v4`**（错误信号由契约方裁决，拦截器不自行消化）。
  3. **范围边界**：豁免**仅**限 sessions 列表端点本身；`/slimapi/sessions/{sid}/...`（children/todo/diff）、`/slimapi/sessions/status`、`/slimapi/messages/**` 等 v4 下仍消费 directory query（v2.2 §7 §5 directory 消费矩阵：「v3 继续消费；v4 sessions 拒绝」——指 sessions 列表拒绝，其余路径不变）。
  4. **wireVersion==3 行为不变**：完整保留现转换逻辑（header→query 注入），保证 3.0.x 兼容期零回归。
  5. **实现位置**：拦截器读 `ServerCompatProfile` 的 `selectedWireVersion`/`capabilitiesV4` 快照（与 T-A1 共用同一能力面；注意拦截器链在 ETag 之前、V3Selector 之前，须无锁 volatile 读）。**门控条件明确用 `selectedWireVersion == 4`**；`capabilitiesV4` 仅作探测位随快照带出（不单独作为豁免条件——dormant 期 capabilitiesV4=true 但 selectedWireVersion=3 时**不豁免**，保持 v3 注入行为）。
- **验收标准**：
  - v4 模式 + `X-Opencode-Directory: /proj` + `GET /slimapi/sessions` → **无** `?directory=` query、**无 header 残留**（header 剥离生效）；v4 模式 + 显式 `?directory=` → 原样透传（不被吞），sidecar 400 语义由契约方裁决。
  - v4 模式 + `GET /slimapi/sessions/status` → query 注入照旧（回归断言）。
  - v3 模式全部路径 → 行为与现状逐字节一致（既有 interceptor 测试全绿）。
- **测试/验证方式**：`DirectoryHeaderInterceptorTest` 增 v4 豁免用例（参数化：路径 × wireVersion × header 有无 × query 有无——覆盖剥离/透传两分支）；既有全部用例回归。
- **依赖**：T-A1（selectedWireVersion 来源）；sidecar **4.0.0 未发布也可先行**（豁免代码按 `selectedWireVersion==4` 门控，生产 sidecar 无 v4 时永不触发）。

---

#### T-A3 status 单次全局调用（即刻可做，不依赖 v4）

- **涉及模块/文件**：
  - `ui/controller/StatusPollOrchestrator.kt`（`launchLoadSessionStatusSlim` `:202-213` 逐目录循环）
  - `data/repository/gateway/SessionGateway.kt`（`getSlimapiSessionsStatus(directory)` `:78-95`；`getSlimapiSessionStatusOutcome(sessionId, directory)` `:216-235` 每会话 N 倍重复查询源头）
  - `data/api/SlimApi.kt:108-112`（`getSlimapiSessionsStatus(@Query("directory") directory: String)` 单值**必填** → **改可选**）
- **实现要点**：
  1. **现状根因**（v2.2 §1.2 status 事实澄清）：上游 `/session/status` 返回**全局内存 map，directory 不影响结果**（v2.2 实证：30.6K 次/4 日放大是**消费者调用策略问题**）；sidecar 侧 docstring 已明示（`routes/sessions.py:350-372`）：directory 参数完全被忽略、**「callers SHOULD omit directory and call once for the whole map」**；`StatusPollOrchestrator.kt:136-137` 注释自证「the upstream directory is a no-op — every call returns the host-wide global map」，`:188-193` 却仍「Query every known workdir and merge」。
  2. **改造**：`launchLoadSessionStatusSlim` 的 `for (directory in directories) { getSlimapiSessionsStatus(directory) }` → **单次 `getSlimapiSessionsStatus()`（省略 directory）**（v3 端点即全局，无需 v4）。合并逻辑保留（防将来 sidecar 行为变化，成本近零）。
  3. **Retrofit 签名改可选**：`SlimApi.kt:108-112` `@Query("directory") directory: String` 必填 → `directory: String? = null`（省略即全局——与 sidecar 侧「omit directory」SHOULD 对齐）。
  4. **零 workdir 分支**：`directories` 为空（会话列表未加载/无会话）时复用现状路径 `StatusPollOrchestrator.kt:184-187` 的 `complete(true)`——不发起 REST，读端将缺失 status 投影为「Unknown」（非 idle）；digest relay + 后续 sweep 填充。本任务不改变该分支，只保证**有会话时也只打一次**。
  5. **N 倍重复查询消除**：`getSlimapiSessionStatusOutcome(sessionId, directory)` 目前每会话每次调用都触发一次全量 status 查询——改为复用 `applySlimStatusResult` 已产出的 `Map<sid,SessionStatus>` 快照（`StatusPollOrchestrator` 单次拉取后由 slice 分发），彻底去掉 per-session 重复请求（audit ① 表「会话状态 busy/idle」行 + `ServerCompatProfile.kt:183-207` 注记的 fan-out 短路再收口）。
  6. **触发路径保留**：SWEEP 触发 + `sseDigestRelayEffective`（sseConnected && !sseDisabled）时零 REST no-op 的逻辑不动（`StatusPollOrchestrator.kt:83-84,107-126`）——本任务只改「拉取时拉几次」。
- **验收标准**：
  - 任意会话数 N：一次状态轮询周期内 `/slimapi/sessions/status` 请求数 = **1**（改造前 = 目录数 + 会话级调用数）。
  - busy/idle/retry 语义与改造前逐会话一致（`buildAuthorityApplySnapshot` + `activeSessionIds` intersect 逻辑不动）。
  - 401/403/503 等错误处理路径不回归（单次调用的错误 = 全局错误，UI 语义同前）。
- **测试/验证方式**：
  - `StatusPollOrchestratorTest`/`AppCoreOrchestrationTest` 断言调用次数（mock repository 计数）。
  - `TrafficTracker`/access log 对比：改造前后 status 桶请求数（生产 30.6K/4 日 → 预期数量级下降，v2.2 §4 请求数基线）。
  - 真机：3 目录 × 多会话场景 status 往返对比。
- **依赖**：无 sidecar 依赖（v3 端点即全局，v2.2 决策 2）；**即刻可做**，与 B5a 并行。

---

#### T-A4 q/p asked 帧载荷直投评估（依赖 sidecar B1b 核对结论）

- **涉及模块/文件**：
  - `data/api/SSEClient.kt`（`parseSseEvent` `:445-485` 三形状解析——flat q/p 形状已解析出 `{directory?, type, properties?}`）
  - `data/repository/gateway/InteractionGateway.kt`（`getSlimapiQuestions` `:225-272` 现状轮询聚合；`getSlimapiPermissions` `:322-358`）
  - `service/events/` + `service/bridge/SseEventBridge.kt`（SSE digest/q/p 帧 → UI 卡片）
- **实现要点**：
  1. **前置核对**（sidecar B1b，v2.2 §3.2a-4「载荷直投评估前置」）：逐字段核对 GlobalHub 转发的 `properties` 是否已是完整 `QuestionRequest`/`PermissionRequest` 对象（上游 `question.asked`/`permission.asked` payload）。
  2. **即时事件面边界（R2 修订）**：sidecar 侧 raw 转发**仅覆盖 IMMEDIATE 集**（`global_hub.py:524-525` raw 转发分支 + `hub_types.py:71-75` IMMEDIATE = `question.asked`/`question.v2.asked`/`permission.asked`/`permission.resolved`/`permission.v2.*`）——**`question.answered`/`question.rejected` 帧不存在**（ocdroid `SlimSseHandler.kt:33-36` KDoc 明示「question.replied / question.rejected / session.* / todo.* are NOT forwarded … folded into session.digest or dropped」）。故：
     - **即时**：`permission.asked` / `permission.resolved` → 卡片出现/消失即时（均在 IMMEDIATE 集内）；
     - **question 关闭不即时**：`question.asked` 即时出现，但 question 的关闭（answered/rejected）**不在 IMMEDIATE 集** → 走 digest/resync 对账，**延迟 ≤ 对账周期**（resync 驱动 + 30min±jitter sweep，v2.2 §3.2a）；不承诺「收到关闭帧即时消失」。
  3. **分叉**：
     - **B1b 核对 = 已完整** → 纯客户端改动：SSE flat q/p 帧直接驱动卡片状态（**新增/更新即时；关闭靠对账**），q/p 聚合轮询降为 resync 兜底（SSE 重连/resync 帧时触发 `getSlimapiQuestions` 全量对账，v2.2 §3.2a-2）+ 30min sweep 已由 sidecar 兜（客户端侧对账在 resync 时做即可）。
     - **B1b 核对 = 缺字段** → 移 B3b（sidecar v4-only 帧补全）；客户端本任务挂起，仅留适配缝（帧解析已有，直投消费逻辑待 v4 帧形冻结）。
  4. **30s 轮询兜底评估**：`UnreadSoakController` 的 active 轮询（audit ① 表「活跃会话 30s 轮询」`GET /slimapi/api/session/active`）不在 q/p 直投范围内（那是 active 会话集合，非 q/p）——本任务**不改** unread 轮询，只改 q/p 卡片数据源。
- **验收标准**：
  - （已完整分叉）SSE 收到 `permission.asked`/`permission.resolved` 帧 → 卡片即时出现/消失（<1s，无轮询延迟）；收到 `question.asked` 帧 → 卡片即时出现；**question 关闭（answered/rejected）经 digest/resync 对账收敛，延迟 ≤ 对账周期**（验收不得隐含「关闭帧即时」——该帧不存在，见上）；resync 帧后全量对账一致。
  - （缺字段分叉）无行为变化，B5a 完成时标记「等待 B3b」。
- **测试/验证方式**：`SSEClientTest` flat 形状载荷断言；事件桥直投单测（mock 帧 → 卡片状态机：asked 即时新增 + 对账收敛关闭）；真机双端对比（卡片出现时机 <1s vs 轮询 ≤30s；question 关闭在 digest 对账周期内收敛）。
- **依赖**：sidecar **B1b 核对结论**（B0 出口之一）；已完整 → 零 wire 变更纯客户端；缺字段 → 依赖 sidecar **B3b（v4）**。

---

#### T-A5（B5a 附属，P1 后即刻可做）digest `changed` 字段消费（定向精拉）

> v2.2 §6 ocdroid 未显式列此任务，但 §3.2「消费端从整表重拉 → 定向精拉」是 B1a 的消费侧；3.3.0 发布后即可渐进采纳（零 breaking），不阻塞 4.0.0。

- **涉及模块/文件**：SSE digest 帧消费端（`SseEventBridge`/`SseSessionListReducers`——digest 帧当前按变更信号触发会话列表重拉；`SessionDigestProcessor.kt:56-129` 当前解析 sessionID/status/archived/deleted/lastError/updatedAt/messageID，**不消费顶层 `directory` 字段**——本任务补上）；`data/repository/gateway/SessionGateway.kt`（定向精拉入口，复用 `getSession(sessionId, directory)`）
- **实现要点**：
  1. 解析 `session.digest` 帧新增可忽略字段 `changed: [sid…]`（v2.2 §3.2 B1a：v3 安全加性，帧形加字段，旧客户端忽略；`hub_types.py:171-195` 已含 directory 字段不得重复新增）。
  2. **精拉请求绑定 digest.directory（R2 修订）**：digest 帧**顶层已携带 `directory`**（`hub_types.py:152` `DigestFields.directory` + `:173-174` `to_payload` 写入 `payload["directory"]`；`global_hub.py:542-544` ingest 时 `entry.directory = directory`）——精拉请求 `getSession(sid, directory)` **必须绑定该字段**（不绑定的裸 `getSession(sid)` 会落到 sidecar 的默认/当前目录解析，错目录）。`SessionDigestProcessor.kt:56-129` 增加对顶层 `directory` 的读取并透传给精拉入口。
  3. **处理单位 = 全部去重 (directory, sid) 对（R3 补混合成败隔离；R4 补确定性失败补偿）**：digest 帧的 `changed` 列表按 (directory, sid) 去重后**逐会话精拉**——200 → upsert 会话行；404（`SESSION_NOT_FOUND`/目录不符）→ 本地 remove（驱逐）。**删除「新增批量 `getSessions(ids)`」臂**（R2 修订：v4 无按 id 批量路由，且批量 API 引入新的契约冻结面；逐会话 `getSession` 循环 + **有界并发**（≤4 并发）足够消化 digest 窗口内的变更量）。**混合成败隔离（R3 冻结）**：单个 (directory, sid) 精拉失败（网络/5xx/超时）**不取消其余**并发项——已完成项按各自结果照常提交（**部分成功**：成功的 upsert、404 的 remove 独立生效，不整体回滚）；**重试语义按 (directory, sid) 对绑定**（重试/重拉必须带同一 directory，绝不裸 `getSession(sid)`）。**失败补偿 = 本地 pending-retry 集合（R4——废弃 R3 的「丢弃 + watermark/下次 digest 自然覆盖」错误保证）**：
     - *为什么不能丢弃*：sidecar digest pending **每次 flush 后清空**（`global_hub.py:379-423`——窗口内 delta 是非持久重放队列，非持久化）；`digest.updatedAt` 只是进程内高水位优化（`global_hub.py:139-148`，**不保存失败 sid**，仅当该 session **再次变化**才重含于下次 digest）；resync 只发生在重连，**不是 HTTP 失败的确定恢复路径**——丢弃 = 该次失效通知永久丢失（会话无后续变化则客户端永久保留旧 skeleton）。
     - *修法（确定性对账链）*：失败项进入 **本地 pending-retry 集合**（`(directory, sid)` 对的内存表）——① 单项失败后**有界退避重试**（≤2 次，指数退避，如 300ms→900ms ± jitter）——消除瞬时故障丢失；② 仍失败 → **留在 pending 集合**，**下次 digest 到达时取并集（changed ∪ pending）合并重试**（不依赖该 sid 再次变化）；③ 集合上限（如 50）防膨胀——**超限触发一次全量列表刷新并清空集合**；④ **resync/重连时清空并全量对账**（既有路径，作为加速通道/最终兜底）；⑤ **必达唤醒（R5 补——消除「重试耗尽后无限期挂起」）**：pending **非空**时并入**既有 status 轮询周期**——每轮 status 轮询（T-A3 改造后单次全局调用，客户端**必达的周期锚点**）顺带触发 pending 集合重试（**有界：每轮 ≤8 项 round-robin**，不引入新定时器组件，集合清空即自然脱钩）。
      - *收敛论证*：任何失败项**最迟在下一轮 status 周期**被重试唤醒，系统**无无限期挂起态**——⑤ 为**必达主通道**（status 轮询客户端侧必达，不依赖 digest 帧或重连发生）；② digest 并集与 ④ resync 为**加速通道**（事件驱动的提前重试）；③ 上限全量刷新为**膨胀保险**（防集合失控）。四通道交集覆盖 → 确定性收敛。
  4. **>20 阈值退化**：单帧 `changed` 长度 > 20 → 放弃逐会话精拉，退化为现状整表重拉（防长尾风暴；阈值常量可调）。
  5. 无 `changed` 字段（旧 sidecar）→ 现状整表重拉（兼容）。
  6. `digest.updatedAt` 非单调 watermark（audit ④ 表：`(updatedAt, messageID)` 二元组字典序）逻辑不动，仅数据源换精拉——**该 watermark 只承担帧去重/乱序丢弃，不承担失败恢复**（失败恢复 = 要点 3 pending-retry 集合，R4 职责分离）。
- **验收标准**：3.3.0 sidecar 下 digest 变更 → 会话列表更新但 `/slimapi/sessions` 请求数大幅下降（access log 断言）；**精拉请求携带与 digest 帧一致的 `?directory=`（绑定正确性断言）**；404 驱逐路径正确（删会话的 digest → 本地移除）；旧 sidecar 下行为同现状；**混合部分失败（R3/R4）**：同帧 3 项中 1 项失败 → 其余 2 项照常生效（部分成功，无整体回滚）；**失败项不丢（R4）**：有界退避重试后成功收敛；耗尽 → 留在 pending 集合、下次 digest 到达时并集补投成功；连续失败超上限 → 全量列表刷新清空集合；**必达唤醒（R5）**：耗尽后**无新 digest、无重连** → **下一轮 status 轮询触发重试并成功收敛**（pending 项最迟一个 status 周期被唤醒）。
- **测试/验证方式**：digest 帧解析单测（有/无 `changed` 字段两分支 + `directory` 绑定透传断言）；`SessionGatewayTest` 精拉/驱逐双分支 + >20 阈值退化 + **混合部分失败用例（R3：并发 3 项 1 败 2 成，断言部分生效）** + **R4 确定性补偿用例**：① 失败 → 有界重试成功收敛（断言最终 upsert，无丢失）；② 失败 → 重试耗尽 → 下次 digest 补投（断言 pending 集合保留 (directory, sid) 对、并集合并重试命中）；③ 连续失败 > 上限 → 全量刷新触发（断言集合清空 + 全量列表请求发起）；**R5 必达唤醒用例 ④**：失败 → 重试耗尽 → **无新 digest、无重连** → 推进 status 轮询周期 → 断言下一轮轮询触发 pending 重试并成功收敛（**不依赖 digest/重连**，仅依赖 status 轮询锚点）；`TrafficTracker` 对比。
- **依赖**：sidecar **3.3.0（P1）**（含 B1a）；独立于 4.0.0。

---

### 2.2 B5b（sidecar B3a/B3b 后）

v2.2 §8：`B5b | 消费者 v4 适配：全局列表 + cursor-aware 分组/收藏 + C1-C3 + /file | B3a/B3b | 中`。

前提：sidecar **4.0.0（P3）** 已发布（wire (3,4) 双版本，B3a selector+DB 投影源、B3b SSE id:/replay）。

---

#### T-B1 会话列表全局拉取（v4 sessions 参数矩阵适配）

- **涉及模块/文件**：
  - `data/api/SlimApi.kt:49-56`（`getSlimapiSessions(directory List?, roots Boolean?, limit, search)` → 改 v4 签名：`parent`/`archived`/`cursor` 参数，`roots` 退役）
  - `data/model/`（`SlimapiSessionsEnvelope{items, complete}` → v4 `SessionSkeletonV4` 投影模型：`directory` + `project` 对象 + v4-only 字段；v2.2 §3.1「每项含 directory/project」）
  - `data/repository/gateway/SessionGateway.kt`（`getSessions(limit)` `:49-52` 全局、`getSessionsForDirectory(directory)` `:53-55` roots=true → 改造）
  - `data/repository/ConnectionInitialDataLoader.kt`（`restoreWorkdirs` fan-out `:114-121` → 单次全局拉取）
  - `ui/sessions/`（`SessionViewModel`/`SessionListActions` 双桶 sessions+directorySessions 结构）
- **实现要点**：
  1. **请求面**（`selectedWireVersion == 4` 时启用）：`GET /slimapi/sessions?v=4&archived=omit&parent=none&limit=…&cursor=…`；**`parent=none` 精确替代现状 `roots=true`**（v2.2 §3.1「`roots`/`start` 退役：`roots` 语义由 `parent=none` 精确承接」；决策 8）。**省略=all 语义知悉**：不带 `parent` = 全量（含子会话）——客户端本地分组按行内 `parentID` 建树，故**必须显式 `parent=none`** 做 roots 视图（v2.2 §3.1「默认 all 保证不带参数的全局列表语义直觉」——ocdroid 首页需要 roots 视图，显式传参）。
  2. **冷启动**：`ConnectionInitialDataLoader.loadInitialData` 的 `restoreWorkdirs` per-workdir 循环（`ConnectionInitialDataLoader.kt:114-121`）→ **单次** `parent=none` 全局拉取（limit 默认），返回行自带 `directory` 字段 → 客户端按 directory 本地分组进双桶（sessions + directorySessions 由 reducer 按 directory 切片）。`restoreWorkdirSessions` 的 identity-guarded CAS（`:141-158`、`:212-216`）保留（防陈旧响应覆盖）。
  3. **archived 三态**：v3 现状 = 客户端过滤 `!it.isArchived`（audit ① 表「客户端 archived 过滤」`recentSessionsInWorkdirScope` `WorkdirGroups.kt:22-38`）→ v4 改为服务端 `archived=omit` 谓词（DB 投影源实现，客户端删过滤——audit ①「客户端过滤」列退役）。`archived=only`（恢复归档视图）为 v4 新能力，B5b 仅接 omit 默认值，only/all 视图 UI 另行评估（见开放问题 #4）。
  4. **cursor 翻页**：`nextCursor`/`complete`（v4 同 snapshot LIMIT+1 窗口）接入 `SlimSessionsPage` 翻页状态；**complete=false 时**首页不足 → 触发下页拉取（见 T-B2 组内空 UX）。
  5. **degraded 处理 + 分页终态（R3 修订）**：200 + `degraded:true`（HTTP 降级路径，v2.2 §3.1 降级矩阵第一格）→ 客户端照常消费（过滤语义等价，仅排序/complete 弱化——不显示错误）；**503 `auxiliary_unavailable`**（`archived=only`/`parent=only|<sid>`/带 cursor 的不可表达组合）→ 显式错误 + `Retry-After` 处理（客户端统一 503 族处理，v2.2 §4 弱网优化）。**degraded 分页终态（R3 定义）**：`items=[]+complete=true+degraded=true` → **不判空、不因 complete 停止 drain**（无 nextCursor 时组状态收敛为 **partial：数据可能不完整 + 手动刷新入口**——非 loading 悬挂、非「组确为空」空态；degraded 响应本身**不证明**无更多会话）；**cursor 第二页 503** → 同收敛 **partial + `Retry-After` 后手动重试入口**（与 T-B2 组状态机衔接，T-B2 要点 4）。
  6. **v3 兼容路径保留**：`selectedWireVersion == 3` → 全部走现状 v3 签名（roots=true 分支保留），B5b 交付时 v3 路径为 legacy 死代码状态、随 P4 流量归零后清理（T-B5b）。
  7. **merged 消息消费零改动兼容（R3 确认，与 webui B5a-3 对齐）**：v3/v4 两模式切换仅涉及 sessions 列表数据源与参数（v4 签名），**merged 消息消费、message-v2 cursor 分页、skeleton 投影的客户端处理逻辑不受影响**——两模式下帧形/合并语义一致（serverMerge 能力不变），裁剪逻辑按 `selectedWireVersion` 切换数据源后仍走原 merged 流程；验收补「两模式 merged 消费结果等价」断言（防止实现时误将 merged 逻辑卷入 wire 切换）。
- **验收标准**：
  - v4 sidecar 下冷启动 `/slimapi/sessions` 请求 = **1**（改造前 = 1 + 目录数，`ConnectionInitialDataLoader.kt:114-121` fan-out 消除）；会话完整性：全部 workdir 根会话可见（含 MRU 外目录——v2.2 §3.1 全局列表语义）。
  - `parent=none` 结果 = 现状 `roots=true` 逐目录结果的并集（等价性对比测试）。
  - cursor 翻页跨页正确（含低频 workdir 首页不在第一屏场景）。
  - degraded 响应消费无错误 UI；503 显式错误 + 重试。
  - **merged 消费（R3）**：v3/v4 两模式下 merged 会话消息展开结果逐条等价（同会话同窗口），仅数据源参数不同。
- **测试/验证方式**：`SessionGatewayTest` v4 签名 + 降级矩阵逐格（mock 200+degraded / 503 auxiliary_unavailable / 400 invalid_cursor）；`ConnectionInitialDataLoaderTest` 断言单次调用；**merged 两模式等价对比测试（R3）**；真机对比改造前后冷启动请求数。
- **依赖**：sidecar **4.0.0（B3a）**；T-A1/T-A2（selectedWireVersion + 拦截器豁免）。

---

#### T-B2 WorkdirGroups 本地分组改 cursor-aware（组内空 ≠ 无会话）

- **涉及模块/文件**：
  - `ui/sessions/WorkdirGroups.kt`（`buildWorkdirGroups` `:146-214`、`recentSessionsInWorkdirScope` `:22-38`）
  - `ui/sessions/SessionsScreen.kt`（`workdirGroups` derived `:267-282`、`onToggleWorkdir` expand `:361-367` → 翻页触发）
  - `ui/viewmodel/SessionListHelper.kt`（`expandedSessionIds` 按 id 键控 `:84-86`——展开态留存）
  - `ui/sessions/SessionViewModel.kt`/`SessionListActions.kt`（翻页状态 + drain 逻辑）
- **实现要点**：
  1. **问题**（v2.2 §3.1 parent 默认 + §6 ocdroid 2）：全局分页后，低频 workdir（MRU 尾部）的根会话按 `time_updated DESC` 可能不在已拉页内 → 组内 `0-live`（`WorkdirGroups.kt:198-205` Step 3 0-live placeholder）**≠** 无会话。
  2. **分组状态 cursor-aware**：`buildWorkdirGroups` 需感知「该 workdir 是否已确认完整」——引入 per-workdir `completeness` 标记：全局页 `complete=false` 时，**未覆盖的 visible workdir 组显示「加载中/继续加载」loading 占位**，**不**显示「无会话」空态；`complete=true`（全部拉完）→ 组内 0-live 才可判定为空。
  3. **「组确为空」权威判据（R2 修订）**：`complete === true && degraded !== true`——**两条件同时满足**才算组确为空、可显示空态；`degraded` 响应（HTTP 降级路径）**不作空组证明**（degraded 下 complete/排序弱化，过滤语义虽等价但数据源可能不完整）——**与 webui 方案 W-lane Blocking 4 同一判据，措辞一致**。
  4. **expand = 受限 drain（R2 修订，删除死臂）**：**删除「复用 `refreshDirectorySessions(workdir)`」臂**——v4 无 per-directory 路由，该调用必 400 `directory_retired_in_v4`。唯一可行臂 = **全局下一批 + 本地切分**：expand 时沿 `nextCursor` 连续拉页，直至①该 workdir 的根会话被覆盖（本地命中），或②`complete=true`；设**页数上限**（如 ≤3 页，常量可调）防长尾风暴——超限 → **partial 状态**：组内已拉到的会话正常显示 + 「手动加载更多」入口（再次 drain）。整个 drain 过程该组保持 loading 态（见 2），不得闪空态。
     **drain 异常终态（R3，与 T-B1 要点 5 衔接）**：drain 中途遇 `degraded=true`（无 nextCursor / `items=[]+complete=true+degraded=true`）→ 组收敛 **partial（数据可能不完整）+ 手动刷新入口**（非 loading 悬挂、非「组确为空」空态）；**cursor 第二页 503** → 组收敛 **partial + `Retry-After` 后手动重试入口**（不自动无限重试、不静默当空组）。
  5. **本地分组本身**：v4 返回行自带 `directory` → 客户端按目录键分组（`WorkdirGroups.kt` Step1-4 结构保留，数据源从双桶 fan-out 改为全局拉取切片）；`recentWorkdirs ∪ draftWorkdir` visible 门（`:146-151`）语义保留。
  6. **展开态留存（正面确认）**：`buildWorkdirGroups` 为纯函数（同输入同输出），`expandedSessionIds` 按 id 键控（`SessionListHelper.kt:84-86` `contains/±sessionId`）→ drain 补拉产生的重切片**不丢**已展开状态；重切片后按 `id ∈ expandedSessionIds` 恢复展开——无需额外状态迁移。
  7. **重切片滚动位置与稳定 key（R3，oracle 微修 3）**：列表行 key = `normalizedWorkdirKey:sessionId`（稳定、跨 drain 不变），LazyColumn `key` 用它；滚动锚点按**行 key**（而非序号）保持——re-slice 只是「组内追加行 + 组序不变」，滚动位置经 rememberLazyListState 锚定首个可见行 key 恢复，避免 drain 补拉导致列表跳动。
- **验收标准**：
  - 30 个 recent workdir 场景（MRU 上限，audit ② `WorkdirPrefs`）首屏：高频组有会话、低频组显示 **loading 占位而非空态**；expand 后受限 drain 正确补拉显示会话。
  - 低频组确实无会话（`complete=true && degraded!==true`）→ 空态文案回归现状。
  - 无翻页需求（limit 覆盖全部）时行为与现状一致（completeness 恒 true 路径）。
  - drain 超页数上限 → partial + 「手动加载更多」，不无限拉页。
  - expand 后重切片展开态不丢（补拉会话仍保持展开）。
  - **状态机三用例（R3，B4）**：① 首屏即 degraded（`items=[]+complete=true+degraded=true`）→ 组显示 **partial + 手动刷新**（不判空、不 loading 永挂）；② drain 中途 degraded → 组收敛 partial + 手动刷新；③ cursor 第二页 503 → 组收敛 partial + Retry-After 后手动重试。
- **测试/验证方式**：`WorkdirGroupsTest` 增 cursor/complete/degraded 参数化用例（complete=true/false × degraded × 组内有无会话 六格 + **三个状态机用例：首屏即 degraded / drain 中途 degraded / 第二页 503**）；`SessionsScreen` UI 测试（空态 vs loading 态 vs partial 态三分）；`SessionListHelperTest` 展开态留存断言；真机 30 目录场景。
- **依赖**：T-B1（v4 全局拉取 + complete 语义）。

---

#### T-B3 C1-C3 残留清理 + allowlist 场景验证

- **涉及模块/文件**：
  - `data/repository/http/HttpHeaders.kt:54`（删除 `"/global/health"` CACHEABLE_PATHS 残留）
  - `data/repository/ServerCompatProfile.kt:10`（陈旧注释修正）
  - `ui/util/HttpImageHolder.kt`（外站直抓保留判定 + 403 `directory_not_allowed` 处理）
  - `ui/controller/ConnectionHealthProbe.kt`/`ConnectionBootstrapHealthProbe.kt`（`/slimapi/ready` 用于连接引导：区分 sidecar 存活 vs opencode 就绪）
  - `data/repository/http/SlimapiErrorCodes.kt`（`DIRECTORY_NOT_ALLOWED` 常量 `:40` 已含、无新增；本任务只补 403 处理路径）
- **实现要点**：
  1. **残留清理**：`HttpHeaders.kt:54` 删 `"/global/health"`；`ServerCompatProfile.kt:10` 注释改为 health 来源描述。
  2. **allowlist 场景**（v2.2 §3.5）：sidecar 配置 `OC_SLIMAPI_DIRECTORY_ALLOWLIST` 非空时——`/slimapi/file/**` fail-closed（空 allowlist → 403 `directory_not_allowed`，v2.2 决策 6）。客户端图片加载（`HttpImageHolder` `/file/` 路径）遇 403 → 显式占位图 + 可读错误（不崩溃、不重试风暴）；**空列表呈现 = 保守二义（R3 修订）**：v2.2 §3.5「消费者需可区分空因过滤 vs 空因无会话」，但两场景 **wire 表现相同**（`getDirectories` 返回空 + 无 allowlist 状态字段时无法从响应本身区分）→ 客户端按 **「不可用/未知」** 保守呈现（空因提示「当前无可用目录/目录被限制，可能受 allowlist 配置影响」——**与 webui 方案 W-lane 三分类（可空/不可用/未知）的保守端对齐**）；**精确分流预留**：若 sidecar 在 health 广播 allowlist 非空状态字段（不泄露清单），则按该字段升级为「allowlist 过滤」明确文案——**该字段形状属 sidecar wire 契约开放问题（记入 §7 开放问题 #8），本任务不隐含依赖**。
  3. **`/slimapi/ready` 接入连接引导**：`ConnectionHealthProbe`/`ConnectionBootstrap` 在健康探活链上，区分「sidecar 活着但 opencode 未就绪」（`/slimapi/health` sidecar.ok vs `/slimapi/ready`）——audit C3 场景闭环（host 测试钮已走 `/slimapi/health`，补 ready 探针避免「sidecar 挂时误报 opencode 状态」）。
  4. **文档修订（R2 必做，Non-blocking 2）**：audit ④ C1/C2/C3 行 + ocdroid `docs/slim-mode-api-routing.md` C 桶同步修订为本方案 §1.3 事实基准（明细见 §7「必做文档修订」）——防 omni-orch 按过时描述派发「从零修复」任务；修订随本任务一并交付验收。
- **验收标准**：
  - `CACHEABLE_PATHS` 无 `/global/health`；全仓 grep 无 legacy health 直探**功能**引用（常量/调用 = 0）。误导性注释（`HttpHeaders.kt:54`、`ServerCompatProfile.kt:10`）随本任务清零；`ConnectionGateway.kt:76`、`:206-207` 记录 L3-波2 删除事实的历史注记**保留**（不属误导性残留）。
  - allowlist 非空 sidecar：file 403 显示占位 + 无重试风暴（日志断言）；directories 空 → **保守二义「不可用/未知」文案**（不承诺精确区分「无目录 vs allowlist 过滤」——wire 无该区分；若开放问题 #8 字段落地再升级明确文案）。
  - `/slimapi/ready` 探针在 opencode 重启动作后（`ActionsViewModel` 现状）与连接引导共用，行为一致。
  - **audit + routing.md 两文档 C 桶与 §1.3 事实一致**（diff 复核）。
- **测试/验证方式**：单测（403 解析 + 错误文案）；真机 allowlist sidecar 场景；traffic-snapshot 断言无 direct-opencode 桶流量（v2.2 §8 B5b 验收口径：**流量归零证明**——C 桶不再有请求，载体见 §5.3）。
- **依赖**：sidecar **4.0.0**（allowlist 全局面过滤语义）；**health 扩展字段（allowlist 非空广播）不构成依赖（R4 漂移消除）**——本任务只依赖 allowlist fail-closed 语义本身；开放问题 #8 的健康字段若落地，**仅升级错误文案**（「allowlist 过滤」明确提示），**非行为依赖**（行为 = 403 处理 + 保守二义呈现，均已冻结）；图片 403 路径在 v3 侧 3.3.0+ 已可用（B4 allowlist fail-closed 是 P1 内容）——可提前到 B5a 期实施，列入 B5b 因资源排布。

---

#### T-B4 context 用量 + agent/model 切换接入（B4 路由消费）

- **涉及模块/文件**：
  - `data/api/SlimApi.kt` 或 `StandardApi.kt`（新增 `GET /slimapi/session/{sid}/context`、`POST /slimapi/session/{sid}/agent|model`——v2.2 §3.4 B4 路由，v=3 即可用）
  - `data/repository/OpenCodeRepository.kt`（forwarder）
  - `ui/chat/ChatContextUsageDialog.kt`（context 用量：现状 `POST /summarize` 返回 Boolean 拒信（audit ① 表「上下文压缩」`SummarizeServerRejectedException`）→ 补用量展示源）
  - `ui/sheets/PickerSheets.kt`（agent/model 切换：现状仅创建/发消息时选择（`PromptRequest.agent/model`）→ 补运行中切换入口）
- **实现要点**：
  1. **context 用量**：`ChatContextUsageDialog` 打开时调 `GET /slimapi/session/{sid}/context`（token 用量感知，v2.2 §1.4 真实缺口补齐）；与 `POST /summarize`（压缩动作）职责分离：用量展示读 context，压缩动作仍走 summarize。
  2. **agent/model 切换**：会话顶栏/设置入口 → `POST /slimapi/session/{sid}/agent` + `/model`（运行中切换，v2.2 §3.4）；成功回调刷新会话详情；失败（404 session/上游不支持）→ 可读错误，不破坏现有创建路径。
  3. **revert 三段式**（v2.2 §3.4 `revert/stage|clear|commit`）不在 ocdroid 现有范围（现有单次 `POST .../revert`，audit ① 表 Fork/Revert）——**本方案不接**，留观察（见开放问题 #5）。
- **验收标准**：context 用量数字与 upstream 一致（sidecar 转发验证）；运行中切 agent/model 生效（会话卡片 agent 标签更新）；切换失败不中断会话流。
- **测试/验证方式**：`ChatViewModelTest`/`PickerSheets` UI 测试；真机连 3.3.0+ sidecar 验证。
- **依赖**：sidecar **3.3.0（P1，B4 路由）**（v=3 即可用，无需 4.0.0——可提前到 B5a 期，列入 B5b 因 UI 资源排布）。

---

#### T-B5 404-sticky + 三形状 SSE 解析退役（分 T-B5a/T-B5b 两阶段；生产流量证明前提）

> **R2 修订**：证据载体、证据维度、replay/解析退役时序三处修正（Non-blocking 1/5/6）——见下。

- **涉及模块/文件**：
  - `data/repository/ThinRouteCapabilityFlags.kt`（todo/children/diff 三位）——删除 + 调用点短路
  - `data/repository/ServerCompatProfile.kt`（`supportsSlimStatus`/`useSlimCatalog`/`supportsSlimQuestions`/`supportsSlimDirectories`/`supportsSlimActions` 五老位——存量注记 `ThinRouteCapabilityFlags.kt:32-38`「老位待后续清理轮统一迁移」）
  - `data/repository/gateway/`（`SessionGateway.kt` children/todo/diff 404-sticky 分支 `:96-160`；`CatalogGateway.kt` `:71-96`；`InteractionGateway.kt` `:225-272`）
  - `data/api/SSEClient.kt`（`parseSseEvent` `:445-485` 三形状 → v4 帧形冻结后只保留 event-typed 单形状；`Last-Event-ID` 重发接入）
  - `data/repository/http/SlimapiErrorCodes.kt`（`THIN_ROUTE_NOT_FOUND` 保留或降级为普通错误）
- **实现要点**：
  1. **硬前提**：sidecar **P4 判据**（v2.2 §7：access log v3 流量归零 + SSE active 无 v3 连接）+ 生产流量证明（audit ④ 404-sticky 表「随 v4 退役，生产流量证明前提」，v2.2 §5 保留主张）。前提不满足 → **本任务顺延 B6**，不做。
  2. **sticky 位退役（T-B5b）**：全部 404-sticky 探测机制删除（fail-open 默认 true + mark* + generation fence 整体移除），调用点直接打 thin 路由（v4 契约冻结路由全集，v2.2 §7 §10 路由全集逐条）；旧 sidecar 兼容由版本协商承担（available 不含 3 → 空交集拒连，无需 sticky）。
  3. **三形状解析退役（分两阶段，R2 修订）**：
     - **4.0.0 阶段（B5b 内）**：接入 `Last-Event-ID` 重发（v2.2 §3.2 SSE id: 重放——epoch+seq、有界重放、gap→resync；连接建 requests 加 `Last-Event-ID` 头；`SSEClient.kt:245-259` 现状 T9 no-replay 注释翻转）——**v4 连接专属**：`selectedWireVersion==4` 时启用 id:/重发，v3 连接保持 no-replay；三形状解析此时**仍在**（v4 帧形与 legacy 并存期的兼容读取，fail-open：解析器对不认识的帧形跳过不崩）。
     - **5.0.0 阶段（B6，随 P4 前提）**：v4 契约冻结帧形后，`parseSseEvent` 删除 legacy `{payload:{...}}` 与 flat q/p 形状，**只保留 event-typed 单形状**（+id:/replay 解析，不再有 no-replay 分支）；三形状解析与 `Last-Event-ID` 逻辑在此合并收敛为 v4-only。
  4. **sticky 退役同样两阶段**：4.0.0 起新客户端带 `selectedWireVersion` 门——v4 连接直接短路 404-sticky 分支（不再探测）；机制删除（代码级移除）留 5.0.0 阶段。
- **验收标准**：
  - 4.0.0 阶段：v4 连接 `Last-Event-ID` 重发正确（断线恢复 O(缺口) 收益）；v3 连接 no-replay 行为不变（回归）；sticky 分支在 v4 连接短路（`TrafficTracker` 断言无 404 探测往返）。
  - 5.0.0 阶段：删除 sticky 机制后连 4.0.0+ sidecar 全功能正常；SSE 仅 event-typed 帧消费正确；v3 流量归零证明归档。
- **测试/验证方式**：存量 sticky 测试删除 + 直连测试替换；`SSEClientTest` 单形状 + `Last-Event-ID` 重发（含 gap→resync 路径）；sidecar 侧流量归零审计。
- **依赖**：sidecar **4.0.0（P3，B3b SSE id:/replay）** 起 `Last-Event-ID` 可用（本任务 4.0.0 阶段）；**5.0.0 阶段（P4）** + **生产流量归零数据** 才做机制删除；本任务为 v2.2 §8 **B6** 内容（v4 稳定后清理），B5b 交付时**仅当**前提满足才执行。

---

### 2.3 阶段依赖总表（任务 × sidecar 版本 × 顺序）

| 任务 | sidecar 依赖版本 | 前置任务 | 并行性 |
|---|---|---|---|
| T-A1 版本协商（versions 先行） | B0 定稿（契约键名/字段名）+ 3.3.0 发布（versions 端点 v3 即存在） | — | 独立（连接引导链独占） |
| T-A2 拦截器豁免 | 4.0.0 契约（可先行门控） | T-A1 | 依赖 T-A1 |
| T-A3 status 单次 | **无**（v3 即全局；`routes/sessions.py:350-372` SHOULD omit） | — | 独立，即刻可做 |
| T-A4 q/p 直投 | B1b 核对结论（3.3.0 或 B3b） | — | 独立 |
| T-A5 digest changed（directory 绑定精拉） | 3.3.0（B1a） | — | 独立 |
| T-B1 全局拉取 v4 | **4.0.0（B3a）** | T-A1, T-A2 | 依赖 B5a |
| T-B2 分组 cursor-aware（受限 drain） | 4.0.0（B3a） | T-B1 | 依赖 T-B1 |
| T-B3 C1-C3 残留+allowlist | 3.3.0（B4 allowlist）+ 4.0.0 | — | 可提前 B5a |
| T-B4 context/agent/model | 3.3.0（B4 路由） | — | 可提前 B5a |
| T-B5a（4.0.0 阶段）v4 连接短路 sticky + `Last-Event-ID` | **4.0.0（P3，B3b）** | T-A1, T-B1 | 依赖 B5a |
| T-B5b（5.0.0/B6 阶段）机制删除 + 单形状 | **5.0.0（P4）+ 流量归零证明** | T-B5a | 硬前提门控 |

---

## 3. 写域矩阵（omni-orch 并行开发用）

### 3.1 owner 包制（一包一主；R2 修订——替代 R1 的「5 lane 无共享文件」声明）

> **R2（Blocking 6）修订**：R1 的「5 lane 无共享文件」被 §3.1 自身矩阵推翻（T-A1×T-B4 共享 SlimApi.kt/StandardApi.kt；T-A3×T-A5 共享 SessionGateway.kt；T-A1×T-B3 共享 ConnectionGateway.kt；T-A4×T-A5 共享 SSEClient.kt）。R2 改为 **owner 包制**：协议网络层 / 能力模型 / 网关层 各一包一主；跨包任务显式标注协作边界（`[拆]`），omni-orch 按 lane 派发、按包验归属。

| owner 包 | 文件/目录 | 涉及任务 | 串行链 / 协作 |
|---|---|---|---|
| **P1 协议网络层**（一主） | `data/api/SlimApi.kt`、`OpenCodeApi.kt`、`VersionsApi.kt`(新)；`data/repository/http/`（`DirectoryHeaderInterceptor.kt`、`V3SelectorInterceptor.kt`、`SlimapiContract.kt`、`SlimapiErrorCodes.kt`、`HttpHeaders.kt`） | T-A1(versions 端点/selector 改造)、T-A2、T-B1(v4 签名——与 P3 的 SessionGateway 消费侧同批)、T-B3(HttpHeaders 清理)、T-B4(新端点)、T-B5b(错误码收敛) | **串行：T-A1 → T-A2 → T-B1 → T-B4**（Retrofit 面 + 拦截器链唯一归属，同包高频冲突——T-B1 的 v4 签名与本包 T-A1 的 selector 改造同写 SlimApi.kt，故纳入串行链） |
| **P2 能力模型**（一主） | `data/repository/ServerCompatProfile.kt`、`ThinRouteCapabilityFlags.kt` | T-A1(三概念状态机/`selectedWireVersion`/`capabilitiesV4`)、T-B5b(sticky 位删除) | 低频单文件；随 T-A1/T-B5 推进；供 P1/P5 经 volatile 读（无锁） |
| **P3 会话网关**（一主） | `data/repository/gateway/SessionGateway.kt` | T-A3(status)、T-A5b(精拉入口+去重/阈值)、T-B1(v4 签名侧) | **串行：T-A3 → T-A5 → T-B1**（单文件按序） |
| **P4 SSE**（一主） | `data/api/SSEClient.kt`、`service/events/`、`service/bridge/SseEventBridge.kt`、`ui/controller/SseSessionListReducers.kt`、`SessionDigestProcessor.kt` | T-A4、T-A5a(帧解析+directory 绑定)、T-B5a(Last-Event-ID)、T-B5b(单形状) | **SSE lane：T-A4 + T-A5a 同主**（同文件族）；与 P9 经事件总线 |
| **P5 连接引导**（一主） | `data/repository/gateway/ConnectionGateway.kt`、`ui/controller/ConnectionHealthProbe.kt`、`ConnectionBootstrapHealthProbe.kt` | T-A1(versions 先行时序/health selector)、T-B3(ready 接入) | **串行：T-A1 → T-B3**（T-A1 的探测时序与 P1 的端点/selector 改动同批次交付） |
| **P6 状态轮询**（一主） | `ui/controller/StatusPollOrchestrator.kt` | T-A3(循环收敛) | 独立；只调 P3 的 `getSlimapiSessionsStatus()`（改签名时接口先行） |
| **P7 会话 UI**（一主） | `ui/sessions/`（`WorkdirGroups.kt`、`SessionsScreen.kt`、`SessionViewModel.kt`）、`ui/viewmodel/SessionListHelper.kt`、`ui/action/SessionListActions.kt` | T-B1(双桶切片)、T-B2 | **串行：T-B1 → T-B2** |
| **P8 冷启动**（一主） | `data/repository/ConnectionInitialDataLoader.kt` | T-B1(单次全局拉取) | 依赖 P3 v4 签名完成 |
| **P9 q/p+目录网关**（一主） | `data/repository/gateway/InteractionGateway.kt`、`CatalogGateway.kt` | T-A4(对账兜底)、T-B5b(404-sticky 分支删除) | 与 P4 经事件总线；T-A4 先行 |
| **P10 图片/文件**（一主） | `ui/util/HttpImageHolder.kt` | T-B3(403 处理) | 独立 |
| **P11 上下文/切换 UI**（一主） | `ui/chat/ChatContextUsageDialog.kt`、`ui/sheets/PickerSheets.kt` | T-B4 | 独立（依赖 P1 新端点，接口先行） |
| **P12 文档**（一主） | ocdroid `docs/slim-mode-api-routing.md`；oc-slimapi 仓库文档**仅只读引用**（audit 报告、契约、部署文档——不写 oc-slimapi 仓库任何文件） | 文档修订（**必做**，见 §7「必做文档修订」a/b/c） | 收尾统一（随 T-B3 一并交付） |

**跨包任务拆解**（omni-orch 派发时按此拆，各 owner 只写自己包内文件）：
- **T-A1 `[拆 P1+P2+P5]`**：P1 写 `VersionsApi.kt` + `V3SelectorInterceptor` 改造（selectedWireVersion 感知）+ `SlimapiContract` 常量退役；P2 写 `ServerCompatProfile` 三概念状态机（`selectedWireVersion`/`clientSupportedWireVersions`/`capabilitiesV4` 字段 + 生命周期）；P5 写 `ConnectionGateway` 的 versions 先行时序 + health selector 参数化。**三包改动同一批次交付**（协商链跨包，先定 P2 状态机接口，P1/P5 各自实现）。
- **T-A3 `[拆 P3+P6+P1]`**：P3 改 `getSlimapiSessionsStatus()` 签名（directory 可选）+ 内部；P6 改 `launchLoadSessionStatusSlim` 循环收敛 + 复用快照；P1 改 `SlimApi.kt:108-112` Retrofit 参数（可选）。**P1 签名先行，P3/P6 跟进**。
- **T-A5 `[拆 P3+P4]`**：P4（SSE lane 主）写帧解析 + directory 绑定 + `changed` 去重（T-A5a）；P3 写 `getSession(sid, directory)` 精拉入口 + >20 阈值（T-A5b）。两包以「(directory, sid) 对列表」为接口（P4 产出 → P3 消费）。
- **T-B1 `[拆 P1+P3+P7+P8]`**：P1 改 Retrofit 签名；P3 改 `SessionGateway` v4 请求面；P8 改冷启动单拉；P7 改双桶切片消费。**P1 先行，P3→P8→P7 顺次**。
- **T-B3 `[拆 P5+P1+P10+P12]`**：P5 ready 接入；P1 HttpHeaders 清理；P10 图片 403；P12 文档。

### 3.2 可并行 vs 串行（R2 修订）

**可并行 lane（R2：B5a 实际可并行 ≈ 2-3 lane）**：
- **B5a 期**：① 版本协商链（T-A1 拆 P1+P2+P5，**1 lane**——三包同批次但属同一功能面，串行交付）；② SessionGateway lane（T-A3，即刻可做、纯收益，**1 lane**）；③ SSE lane（T-A4，等 B1b 结论；T-A5a 等 3.3.0——**视依赖到货可并第 3 lane**）。**日历估 5-6 日**（R1 的 5 lane ~3-4 日改为 2-3 lane 现实排布）。
- **B5b 期**：① 会话 v4 链（T-B1 → T-B2，P1→P3→P8→P7 顺次，**1 lane**）；② SSE v4 链（T-B5a：**Last-Event-ID 接入——三形状解析保留**，**1 lane**，P4 主；**单形状收敛属 T-B5b/5.0.0**，不在 B5b 排期）；③ UI 切换链（T-B4，P11 主，依赖 P1 端点先行）；④ 图片/健康（T-B3，P5+P10，**1 lane**）。

**串行（同文件/依赖链）**：
- T-A1 → T-A2（拦截器豁免依赖 selectedWireVersion 判定，P1 内串行）
- T-A3/T-A5 → T-B1（SessionGateway.kt 单文件按序：status 单次 → digest 精拉 → v4 签名）
- T-B1 → T-B2（分组依赖全局拉取 + complete 语义）
- T-A1 → T-B1（selectedWireVersion 先于 v4 请求面）
- T-B5a（4.0.0 阶段）→ T-B5b（5.0.0/B6 阶段，硬前提门控）

**推荐 omni-orch 排布**：B5a 期 = 2-3 条并行 lane（版本协商链 / SessionGateway 链 / SSE 链视依赖）；B5b 期 = 3-4 条 lane（会话 v4 链、SSE v4 链、UI 切换链、图片健康链）。

---

## 4. 与 oc-slimapi 的接口冻结点

### 4.1 依赖的 wire 契约条目（按 v2.2 §7 v4-contract 修订清单）

| # | 冻结点 | 契约出处 | 客户端用途 | 客户端冻结需求 |
|---|---|---|---|---|
| F1 | **`capabilities["4"]` 能力键 + versions 响应 `available`/`accepted` 字段** | v2.2 §7 修订清单 §3（`globalSessions`/`sseReplay`/`qpImmediateFull`/`auxiliaryFilters`，静态能力不随瞬态抖动）；`versions.py:37,76-78`（现状 `available: [3]`） | T-A1 三概念协商输入 | 键名 + `available`/`accepted` 字段名 + 布尔/对象形状**必须 B0 定稿**（适配缝：按 v4-contract 实际字段名读取）；未知键容忍；**versions 端点保持 selector 豁免 + no-store（现状即然，冻结不得回退）** |
| F2 | **v4 sessions 参数矩阵** | v2.2 §3.1：`archived=omit\|only\|all`（默认 omit）/`parent=all\|none\|only\|<sid>`（**省略=all**）/`search`/`cursor`/`limit=1..500`；`roots`/`start` 退役 | T-B1 请求面 | `parent=none` 精确语义、`archived=omit` 服务端过滤、cursor 指纹（f 含 search-hash+allowlist-rev） |
| F3 | **`directory_retired_in_v4`（400）** | v2.2 §3.1「v4 收到 directory → 400」 | T-A2 豁免触发条件 | 豁免规则以契约定稿（是否 header 也拒）为准 |
| F4 | **503 `auxiliary_unavailable` + `Retry-After`** | v2.2 §3.1 降级矩阵（`archived=only`/`parent=only\|<sid>`/带 cursor → 503）+ §7 §8 错误族 | T-B1 错误处理 | 503 语义 = 能力面内显式不可用（**客户端不回退 v3**）；Retry-After 规范 |
| F5 | **`degraded:true` 成功响应 schema** | v2.2 §3.1（进入正式成功响应；仅 HTTP 降级路径出现；过滤语义等价）| T-B1 消费 | degraded 时排序/complete 弱化可接受、不报错 |
| F6 | **digest `changed: [sid…]` 字段** | v2.2 §3.2 B1a（v3 安全加性；`directory` 字段已存在不得重复新增）| T-A5 定向精拉 | 帧形加字段、旧客户端忽略 |
| F7 | **SSE `id:` / `Last-Event-ID` 重放**（v4-only）| v2.2 §3.2（epoch+seq、作用域、有界重放、gap→resync）| T-B5a 重发（4.0.0 阶段） | 帧 id 分配规则 + gap 处理 + 重放顺序与 meta-first/背压顺序 |
| F8 | **v4 SessionSkeletonV4 投影** | v2.2 §3.1（`directory` + `project` 对象 + v4-only 字段）| T-B1 模型 | `directory` 字段**必须保留**（客户端本地分组依赖）；`project` 可空（组装容忍） |
| F9 | **health 扩展：`auxiliary: {available, mode: "db"\|"http"}`** | v2.2 §3.1 capabilities 边界（瞬态可用性走 health 不抖 versions）| T-A1 探测补充 | 与 F1 分工：静态键 = versions；瞬态 = health + 503 |
| F10 | **allowlist 全局面过滤 + health 广播非空状态** | v2.2 §3.5（空因过滤 vs 空因无会话可区分，不泄露清单）| T-B3 | health 广播字段形状定稿 |

### 4.2 版本时序矩阵（ocdroid × sidecar 兼容 2×2；R2 三概念重推）

> **R2（Blocking 1）重推**：每格用三概念 `(clientSupportedWireVersions, serverAcceptedRange) → selectedWireVersion` 推导，消除 R1 的时序死锁（R1：health 门后探测，serve 5.0.0 时 health 先 400 → 探测永不执行）。R2 起探测 = **versions 先行（裸 client 无 selector）→ health 用 selectedWireVersion**。

| ocdroid 版本 ↓ / sidecar 版本 → | **≤3.2.x（v3-only，available=[3]）** | **4.0.0+（available=[3,4]）** | **5.0.0+（available=[4]，v3 退役）** |
|---|---|---|---|
| **3.0.x 现状（client={3} 硬编码 legacy；无 versions 先行状态机）** | versions 先行（若被回填）→ `{3}∩[3]={3}` → ✅ v=3 全功能；**现状无新状态机，至多被既有版本门（health accepted 判 3∈[3]）放行** | `{3}∩[3,4]={3}` → ✅ v=3 全功能（v4 存在但客户端宣称仅支持 3；status 多调用但可用）——既有版本门放行 | `{3}∩[4]=∅` → ⛔ **既有版本门拒绝**（health 用 `?v=3` → 400 → 通用连接失败）——**旧客户端至多被既有版本门拒绝，无 new versions 状态机、无可读升级文案**（升级文案仅 T-A1 适配后客户端具备） |
| **B5a 适配后（client={3}，仅探测+存储）** | `{3}∩[3]={3}` → ✅ 协商 v=3，行为同现状（零回归） | `{3}∩[3,4]={3}` → ✅ 协商 v=3；`capabilitiesV4=true` **纯探测位**已存储，**请求面不变**（dormant） | `{3}∩[4]=∅` → ⛔ **分支 B：探测成功但空交集 → fail-closed 拒连 + 可读升级文案**（B5a 版已有 versions 状态机，**非** 3.0.x 的既有版本门路径） |
| **B5b 适配后（client={3,4}）** | `{3,4}∩[3]={3}` → ✅ 协商 v=3 全功能（v4 键缺失 → 未知容忍，零回归路径） | `{3,4}∩[3,4]=max{3,4}=4` → ✅ 协商 v=4：全局列表 + status 单次 + digest 精拉 + `Last-Event-ID`；v=3 路径仅 legacy 死代码 | `{3,4}∩[4]=max{3,4}=4` → ✅ **成功选 v4 全功能**（不 gate 失败）；sticky/三形状已退役 |

**关键窗口**：
- **4.0.0 发布日**：旧 3.0.x 客户端**无需同时升级**（available=[3,4] 双版本窗口，v2.2 §7 P3「release.sh major 一次到位」）——B5a 保证的就是这个窗口的兼容。
- **5.0.0 发布日**（R3 修订：按客户端形态分两条路径；R4 再拆 B5a/B5b——**仅 B5a 是空交集**）：
  - **B5a 适配后客户端（client={3}）**：versions 先行协商检出空交集（`{3}∩[4]=∅` 分支 B）→ 可读拒连「服务端要求升级客户端」——**不依赖 health 版本门**（R1 死锁的根治点）；
  - **B5b 适配后客户端（client={3,4}）**：`{3,4}∩[4]=max{3,4}=4` → **成功选 v4，不 gate 失败**（与矩阵 :411、验收矩阵 :421 一致，非空交集路径）；
  - **旧 3.0.x 客户端**：无 versions 状态机——被**既有版本门**（health `?v=3` → 400 → 通用连接失败）拒绝，无升级文案；
  - 结论：客户端须在 4.0.0 窗口期内完成升级（v2.2 §7 P4 判据 = v3 流量归零；归零后才发 5.0.0，故风险自消）。
- **灰度顺序**：sidecar 4.0.0 上线前 → B5a 客户端先行发布（P2 消费者兼容版，v2.2 §7）→ 4.0.0 → B5b 客户端 → 流量归零观测 → 5.0.0。

**验收矩阵对齐（R3：5 个交集场景，见 §5.2）**：`{3}×[3]` → v3；`{3}×[3,4]` → v3（dormant 探测）；`{3,4}×[3,4]` → v4；`{3,4}×[4]` → v4 成功；`{3}×[4]` → fail-closed 拒连。

**补格（R3，B2 逐键门控；R4/R5 规则唯一化）——「选中 v4 但缺单键」**：`[3,4]` 选中 v4 后，逐键行为 = **「缺键唯一行为表」（T-A1 要点 3，:82-90）按 key × available 的唯一格子**（按「该键在 v3 是否有功能等价物」推导，无通配规则）：缺 `sseReplay` → 保持 v4 + 不发 `Last-Event-ID`（v3 无 replay，降级无意义）；缺 `globalSessions` → `available` 含 3 时**整体重协商** `selectedWireVersion` 覆写为 3、全端点切 v3、v4 功能 UI 隐藏（v3 目录级列表保留等价能力）；`available=[4]` 时保持 v4 + 列表禁用 + v3 请求数=0；缺 `qpImmediateFull` → 保持 v4 + q/p 直投不启用（轮询对账保持，客户端行为非 wire 承诺）；缺 `auxiliaryFilters` → 保持 v4 + archived/parent 过滤 UI 不暴露（v3 无三态过滤，降级无意义）。**selectedWireVersion 是唯一事实源**（重协商 = 覆写，无 effective 并存值；每次 versions 探测时重算）；除 `globalSessions` × `available 含 3` 外，任何缺键组合**不触发**整体降级。

---

## 5. 测试与验收策略

### 5.1 3.0.x 兼容回归（每任务必做门槛）

- **回归基线**：`./gradlew test` 全量（ocdroid `app/src/test/java/cn/vectory/ocdroid/`：`AppCoreOrchestrationTest`/`B0FreshnessTokenTest`/`B2DetailAuthorityTest`/`ConnectionViewModelTest` 等）+ 手工真机清单（会话/消息/q/p/digest/图片/文件/VCS 全功能）。
- **关键断言**：T-A1 落地后连生产 sidecar（3.x）→ versions 先行协商 `selectedWireVersion=3`，所有 `/slimapi` 请求 `v=3` 且无新增参数（TrafficTracker/代理抓包对比改造前）。
- **回归范围**：T-A2/T-A3 为行为等价改造（豁免门控 + 循环收敛），须证明「未触发时行为逐字节一致」；T-A5 为数据源替换（整表重拉→精拉），须证明「结果集等价」。

### 5.2 版本协商路径测试（T-A1/T-A2；R3：对齐三概念 5 个交集场景）

> R3 修订：R2 的「4 用例」实为 5 个交集场景（计数校正）；验收表改为「versions 先行协商」用例 + 两分支（B3）+ 逐键门控（B2）显式区分；每行标注三概念推导。

| 场景（三概念推导） | 期望 |
|---|---|
| `<={3,4}×[3]`（sidecar ≤3.2.x，无 capabilities["4"]） | versions 先行协商 → `selectedWireVersion=3` → 全 v=3，无版本键读取 |
| `{3}×[3,4]`（B5a 阶段连 4.0.0） | 协商 v=3；`capabilitiesV4=true` **纯探测结果**存储（dormant）；B5a 期请求面不变 |
| `{3,4}×[3,4]`（B5b 后连 4.0.0） | 协商 v=4：全局列表 + status 单次 + digest 精拉 + `Last-Event-ID` |
| `{3,4}×[4]`（B5b 后连 5.0.0） | **成功选 v4**（不 gate 失败），全流程可用——R1 死锁场景根治验证 |
| `{3}×[4]`（未适配连 5.0.0） | **探测成功但空交集（分支 B）** → fail-closed 拒连，UI「服务端要求升级客户端」可读错误 |
| **分支 A**：versions 端点 404/5xx/超时/畸形 | 重试 ≤2 次仍失败 → **静默回退 v3**（探测不可用 ≠ 版本不兼容；不弹升级文案；4 形态 × 重试后仍失败/成功两路径单测） |
| **分支 B**：versions 200 成功 + 空交集 | 拒连 + 升级文案（与分支 A 行为显式不同，单测断言分别触发） |
| 未知能力键 `{"5":...}` | 无行为变化 |
| **B2 逐键**：`[4]` + 缺 `sseReplay` | **不发 `Last-Event-ID`**（no-replay 保持；fail-closed 断言） |
| **B2 逐键**：`[3,4]` 方缺 `globalSessions` | **整体重协商降回 v3**（唯一结果）：`selectedWireVersion` **被覆写为 3**（单值），全端点（health/SSE/token stream）断言 `?v=3`，v4 功能 UI 隐藏 |
| **B2 逐键**：`[4]` + 缺 `globalSessions` | 保持 `selectedWireVersion=4`，新列表 UI 禁用 + 提示，**v3 请求数 = 0**（无 v3/v4 混合形态） |
| **B2 逐键**：`[4]` + 缺 `qpImmediateFull` | q/p 直投不启用（轮询对账保持） |
| **B2 逐键**：`[4]` + 缺 `auxiliaryFilters` | archived/parent 过滤 UI 不暴露 |
| v4 模式下 `/slimapi/sessions` + directory header | 拦截器豁免（无 directory 注入、header 剥离） |
| v4 模式下 `/slimapi/sessions` + 显式 `?directory=` | 透传不吞，sidecar 400 语义由契约裁决 |
| v4 模式下 `/slimapi/sessions/status` + directory | 注入照旧（回归） |
| 连接重建（`onConnectionReconfigured`） | `selectedWireVersion`/`capabilitiesV4` 重置重协商（同 connectionIncarnation 范式） |
| health 探活 selector | 随 `selectedWireVersion` 变化（v=3→v=4），断言 URL 参数 |

### 5.3 C1-C3 验证（流量归零证明，v2.2 §8 B5b 验收口径）

> **R2 修订（Non-blocking 1/6）**：证据载体与观测窗口修正——R1 的「连续 7 天 access log」不可行（生产 `RETAIN_DAYS=3` → access log 第 4 天起清理）；「7 天」平面观测维度不足——需多渠道版本分布 + 最长合理重连窗。

- **工具**：
  - **主证据 = `traffic-snapshot-YYYY-MM-DD.jsonl`**（按天快照；**其保留期需配置 > 7 天**——`RETAIN_DAYS` 仅控 access log，snapshot 保留策略在 `docs/manual/traffic-accounting.md`；或**归档 `metrics.traffic` 累计快照**）——两类载体任选其一方可覆盖 7 天，omni-orch 按部署环境定；
  - 辅助：`TrafficLogger` 客户端侧 + `slim-mode-api-routing.md` 四桶分类 + **客户端版本分布**（当日活跃客户端 versionName 分布，证明观测期内无老版本残留）。
- **判据**：
  - C 桶（direct-opencode 5 条）请求数 = **0**（**连续 ≥7 天观测，载体 = snapshot/metrics 归档**；audit ⑤「C1-C4 违规待迁移」+ 本文 §1.3 修订后 = C 桶全部清零）。
  - **多重证据角（R2 n6）**：① 时间维度：连续 ≥7 天归零（覆盖最长合理重连窗——SSE 重连预算 `MAX_RETRY_ATTEMPTS=10`（1s→30s ×2 指数退避 ≤ 约 30 分钟上限，`SSEClient.kt`），故 7 天窗口 >> 任何单连接生命周期）；② 版本维度：观测期内活跃客户端 versionName 分布 ≥95% 为适配后版本（排除老版本流量假阴性）；③ 连接维度：sidecar SSE active 无 v3 连接（P4 判据第二项）。
  - `/global/health` 直探路径：grep 全仓无功能性引用（常量/调用 = 0）+ 误导性注释清零（随 T-B3）；L3-波2 删除史注记（`ConnectionGateway.kt:76,206-207`）保留。
  - 图片 `/file/` 全部经 `/slimapi/file/content`（snapshot 断言无直连 host）。
  - allowlist 场景：403 处理无重试风暴（sidecar 403 计数平稳）。
- **归档**：归零证明（以上三重证据打包）随 T-B5b 前提归档（v2.2 §7 P4 判据输入）。

### 5.4 B5b v4 功能验收

| 任务 | 验收用例 |
|---|---|
| T-B1 | 冷启动 `/slimapi/sessions` = 1 次请求；`parent=none` ≡ 旧 `roots=true` 并集；cursor 翻页；degraded 消费无错；503 显式处理 |
| T-B2 | complete=false 且 degraded 时低频组 loading 态（非空态）；expand 受限 drain 正确（≤页数上限、超限 partial + 手动加载更多）；`complete=true && degraded!==true` 后空态回归；重切片展开态不丢 |
| T-A4 | `permission.asked/resolved` 即时；`question.asked` 即时出现；question 关闭经 digest/resync 对账收敛（≤对账周期）；resync 后全量一致 |
| T-A5 | digest `changed` 精拉且请求绑定 digest.directory；200 upsert / 404 remove；单帧 >20 退化整表重拉；旧 sidecar（无 changed）行为同现状 |
| T-B4 | context 用量 = upstream 一致；运行中切 agent/model 生效；失败可读 |
| T-B5a（4.0.0 阶段） | v4 连接 `Last-Event-ID` 重发正确（gap→resync 路径）；v3 连接 no-replay 回归；sticky 分支 v4 短路（无 404 探测往返） |
| T-B5b（5.0.0/B6 阶段） | sticky 机制删除后连 4.0.0+ 全功能；SSE 单形状消费；三重证据（时间/版本/连接）归零证明归档 |

---

## 6. 风险与回退

### 6.1 老版本共存（0.26 / 0.28 / 3.0.0）

- **事实**：ocdroid 版本 git 派生（`app/build.gradle.kts:28-37`，versionName=`<nearest-tag>-<hash>`，无硬编码基线）；0.26/0.28 为 v2 契约时代客户端（`X-Slimapi-Version` 头时代），3.0.0 起 v3-only（AGENTS.md「v2 语义已于 3.0.0 退役」）。
- **共存策略**：
  - sidecar 4.0.0 的 available=[3,4]：**0.26/0.28 已被 sidecar 3.0.0 版本门拒绝**（available 不含 2）——本轮改造不改变这一事实，**无需额外处理**；其存量用户已随 v3 迁移完成。
  - 3.0.x 客户端：4.0.0 窗口内零改动可用（双版本窗口）；5.0.0 前必须升级（P4 判据保证归零后才发）。
  - **5.0.0 拒绝态 UX（R2 新增，Non-blocking 3）**：空交集拒连时 UI 必须区分「服务端要求升级客户端」与通用连接失败——专用可读文案（同上 T-A1 第 4 点措辞统一）+ 引导入口（跳 Store/检查更新）；**不**显示技术性诊断（accepted 数组等），避免困惑非技术用户。
- **验证**：升级前用 3.0.x 真机 + sidecar 4.0.0 全功能冒烟（版本矩阵左上↔中上格）；mock 5.0.0 空交集拒绝态 UI 冒烟（含引导入口）。

### 6.2 灰度策略

1. **sidecar 先行**：sidecar 4.0.0 在测试环境 + staging 灰度（B3a/B3b 独立 rev gate，v2.2 §8）。
2. **客户端 B5a 先行发布**（P2 消费者兼容版）：Play/内测渠道先发 versions 协商版 → 观察 crash/回退率（versions 失败静默回退 v3 应零用户感知）→ 全量。
3. **B5b 按功能 flag 灰度**：`selectedWireVersion`（v4 请求面）/ `digestChanged` / `singleStatus` 三个 feature flag 独立开关——先开 `singleStatus`（纯收益零风险）→ `digestChanged` → `selectedWireVersion=v4`（最大面，最后开）。
4. **回退**：任一 flag 关闭即回旧路径；`selectedWireVersion` 协商本身无持久化风险（fail-closed 兜底 v3）。

### 6.3 主要风险

| 风险 | 缓解 |
|---|---|
| B0 契约未定稿导致 T-A1 返工 | 协商代码按 v2.2 §7 清单 + `versions.py` 现状预留适配缝；`available`/`accepted` 字段名冻结为 T-A1 硬依赖（§4.1 F1） |
| v3 流量未归零、5.0.0 延迟 → B5b 交付滞后 | B5b 独立于 P4 判据（v4 双版本窗口内即可交付）；T-B5b 明确前提门控顺延 B6；T-B5a（Last-Event-ID）随 4.0.0 先做 |
| versions 探测失败（网络/5xx/超时）被误判为 v3 而最新 sidecar 是 v4-only | 罕见组合（5.0.0 上线后短暂窗口）；**R3 两分支显式区分**：探测失败（404/5xx/超时/畸形）→ 重试 ≤2 次（Retry-After 感知）仍失败 → **静默 v3 回退**（探测不可用 ≠ 版本不兼容，走既有连接失败 UX，不弹升级文案）；**仅版本 200 成功且空交集** → 才触发放弃升级文案（T-A1 要点 5） |
| `parent=none` 与旧 `roots=true` 语义偏差（低频 workdir 首屏缺失） | T-B2 的 complete-aware 分组 + 受限 drain 双保险；等价性对比测试 |
| degraded 响应被误报为错误 / 误作空组证明 | 契约 F5 明确 degraded=成功（客户端仅披露不报错）；**空组判据 = `complete===true && degraded!==true`**（T-B2） |
| q/p 直投缺字段（B1b 核对未完整） | T-A4 分叉挂起，适配缝保留；不阻塞 B5a 其他任务 |
| question 关闭依赖对账导致卡片残留 | 属现状语义（`SlimSseHandler.kt:33-36` 无关闭帧）；对账周期内收敛；如需即时化 → 开放问题 #7（sidecar 契约修订诉求） |
| 拦截器豁免误伤其他路径 | 精确路径匹配（仅 `/slimapi/sessions` 本体）+ 参数化测试覆盖（剥离/透传双分支） |

---

## 7. 开放问题（不擅自决定，提交 omni-orch/owner 裁决）

> **R2 修订**：#1 由开放问题升级为**必做清单**（§2.2 T-B3 范围内实施，见下「必做文档修订」）；开放问题保留 1-7；新增 #7 的 question 关闭即时化诉求属 sidecar 契约修订候选，本方案验收不隐含。

### 必做文档修订（Non-blocking 2，R2 从开放问题 #1 移入，随 T-B3 落地）

audit ④ C1/C2/C3 与 `slim-mode-api-routing.md` C 桶描述 vs 代码现状（§1.3 已实锤）脱节——**两份文档必须随 T-B3 同步修订**（防 omni-orch 按过时描述派发「从零修复」任务）；本方案 §1.3 表为改后事实基准：

- **a. audit（oc-slimapi 仓库）**：④ 表 C1 改「大部分已修复（v3.0.1 M-C1a+c）——剩余外站直抓保留 + allowlist 验证」；C2 改「已修复（L7 删 TOFU）」；C3 改「已修复（L3-波2 走 /slimapi/health）——补 /slimapi/ready 引导场景」。
- **b. ocdroid `docs/slim-mode-api-routing.md`**：C 桶 C1/C2/C3 三行标记状态→已清结（保留历史注记）；L3-波1/波2 决策区核对一致。
- **c. 本方案 §1.3 表**即修订期间的事实源；修订完成时三处（audit / routing.md / 本表）形态一致，check 以本表为准。

1. **`capabilities["4"]` 承载端点与形状**——【已就地解决（核准轮：v2.2 文本无歧义；R2 追加三概念确认）】：§3.1「`/versions` 能力面恒定」+ §7 修订清单 §3「versions/health 双视图」合读明确——**静态能力键 `capabilities["4"]` 承载于 `GET /slimapi/versions`**（selector 豁免 + no-store，`versions.py:5-6,59-83`）；health 只承载瞬态字段（`auxiliary:{available,mode}`）+ 503。T-A1 探测点/F1 承载端点据此定稿；B0 仅剩 `available`/`accepted` 字段名与对象形状细节冻结（仍为 T-A1 硬依赖，适配缝保留）。
2. **v4 下 `X-Opencode-Directory` header 对 `/slimapi/sessions` 的行为**：400 `directory_retired_in_v4` 是仅拒 query 还是连 header 拒？影响 T-A2 豁免是否剥离 header（R2 已按「内部 header 剥离、显式 query 透传」预案落进 T-A2，行为以 v4-contract §4 定稿为准）。
3. **q/p 直投核对结论（sidecar B1b）**：`properties` 是否已完整对象——决定 T-A4 走纯客户端（B5a 内完成）还是挂起等 B3b。
4. **`archived=only` 恢复归档视图**：v4 提供三态过滤（audit ①「会话归档」目前客户端 PATCH + 本地过滤）；是否在 B5b 范围做归档恢复 UI？本方案默认只接 `omit`，only/all 视图待裁决。
5. **revert 三段式（stage/clear/commit）**：v2.2 §3.4 提供但本方案不接——ocdroid 是否要（移动端回滚预览 UI 价值评估）？待 owner 定。
6. **SSE `Last-Event-ID` 重放的客户端接入深度**：R2 已定 **4.0.0 阶段接入**（T-B5a，v4 连接专属）+ 5.0.0 阶段单形状收敛——接入深度已从待裁决变为既定，本项保留仅记录「是否提前到 3.3.0 窗口（v3 连接也启用）」的剩余选择。
7. **question 关闭即时化（R2 新增，Blocking 2 上抛）**：`question.answered/rejected` 帧不存在（`hub_types.py:71-75` IMMEDIATE 集无此二事件；`SlimSseHandler.kt:33-36` 明示 NOT forwarded）——本方案验收按「关闭走 digest/resync 对账（延迟 ≤ 对账周期）」；**若 owner 要求 question 关闭即时化**（卡片消失 <1s），需 sidecar 契约修订（B1b/B3b 范围：IMMEDIATE 集扩展或 digest 帧承载关闭信号），记为 **sidecar 侧契约修订诉求，本方案不隐含验收**。
8. **allowlist 空列表精确分流字段（R3 新增，Major 1 上抛）**：v2.2 §3.5「消费者需可区分空因过滤 vs 空因无会话」——但两场景 **wire 表现相同**（`getDirectories` 返回空 + 无附加状态字段）；本方案 T-B3 已按**保守二义「不可用/未知」**落地（与 webui W-lane 三分类保守端对齐），**不隐含 wire 依赖**。若 owner 要求精确分流（明确「allowlist 过滤」文案），需 sidecar 在 **health 广播 allowlist 非空状态字段**（只广播非空布尔、不泄露清单，v2.2 §3.5「不泄露清单」约束）——**该字段形状/命名属 sidecar 契约修订诉求（B0/v4-contract 范围），落地后 T-B3 再升级文案**。

---

## 8. 工作量估计（S/M/L）

| 任务 | 复杂度 | 估计 | 说明 |
|---|---|---|---|
| T-A1 版本协商（versions 先行） | M | 2-3 人日 | VersionsApi + 三概念状态机 + ConnectionGateway 时序 + health selector 参数化 + 空交集 UI + 4 回退分支测试（跨 P1+P2+P5 三包同批次） |
| T-A2 拦截器豁免 | S | 0.5-1 人日 | 精确路径门控 + header 剥离/query 透传 + 参数化测试 |
| T-A3 status 单次 | S | 1 人日 | 循环收敛 + 签名可选 + outcome 复用快照 + 零 workdir 分支保留（**即刻可做，纯收益**） |
| T-A4 q/p 直投 | M（已完整分叉）/ S（挂起） | 2-3 人日 / 0.5 人日 | 即时面 = permission.asked/resolved + question.asked；关闭走对账；直投消费逻辑 + resync 对账 + 测试；挂起仅留缝 |
| T-A5 digest changed（directory 绑定） | S | 1-1.5 人日 | 帧解析 + directory 绑定 + (directory,sid) 去重 + 精拉/驱逐 + >20 阈值（拆 P3/P4 两包） |
| **B5a 合计** | | **6.5-9.5 人日** | **2-3 lane 可并行，日历 5-6 日**（R2：R1 的 5 lane ~3-4 日历日不现实——T-A1 跨三包串行交付、T-A4/T-A5 依赖 B1b/3.3.0 到货，实际并发受限） |
| T-B1 全局拉取 v4 | L | 3-4 人日 | v4 签名 + 模型 + 冷启动重构 + 降级矩阵测试（拆 P1/P3/P7/P8 四包顺次） |
| T-B2 分组 cursor-aware（受限 drain） | M | 2-3 人日 | completeness+degraded 状态 + 受限 drain（页数上限/partial）+ 展开态留存 + UI 测试 |
| T-B3 C1-C3 残留+allowlist+**文档修订** | S | 1.5-2.5 人日 | 清理 + 403 处理 + ready 接入 + 流量规避证明 + **audit/routing 文档同步修订（必做，§7）** |
| T-B4 context/切换 | M | 2-3 人日 | 新 API + 两处 UI + 测试 |
| T-B5a（4.0.0 阶段）v4 连接短路 sticky + `Last-Event-ID` | S-M | 1-1.5 人日 | v4 连接短路分支 + 重发接入（v3 no-replay 回归）；**随 4.0.0 窗口执行** |
| T-B5b（5.0.0/B6 阶段）机制删除 + 单形状 | M | 1.5-2 人日 | sticky 机制整体删除 + 单形状解析 + 三重证据归零归档（**硬前提门控，可能顺延 B6**） |
| **B5b 合计** | | **11-16 人日** | 3-4 lane ~5-8 日历日（不含 T-B5b 顺延） |
| 文档修订 + 回归收尾 | S | 0.5-1 人日 | 收尾文档一致性核对 + 全量回归 |

**总计**：约 18-26.5 人日（B5a 6.5-9.5 + B5b 11-16 + 收尾 0.5-1），并行 lane 下日历周期约 **2 周**（B5a 5-6 日历日先行，与 sidecar 4.0.0 开发并行）。

---

*本方案为规划稿；所有 wire 契约依赖以 oc-slimapi `docs/specs/v3-contract.md` 修订版（B0 定稿）+ 本方案 §4.1 冻结点为准；发现矛盾以 v2.2 为权威基准并回报开放问题。*
