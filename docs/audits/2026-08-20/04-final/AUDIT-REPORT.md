# oc-slimapi v4 全面审计报告（2026-08-20）

## 0. 执行摘要

**范围与方法**：对 oc-slimapi（HEAD=0b836e7，release v4.4.0，wire (3,4) 双版本窗口，71 个 src 文件 / 26,452 行 / 54 条 /slimapi 路由 / 109 测试文件 2642 test 函数）执行 rev10 方案四阶段全量审计：Phase 0 绿色基线与快照冻结（脏基线 261 文件快照副本）→ Phase 1 全量探索（E1–E8：文件精读卡片、54 路由×24 列普查 + 580 期望键、20 张状态机卡片、12 条数据流、两契约 35 节逐节摘要、测试普查、opencode v1.18.18 上游对照）→ Phase 2 十五个专项（A1–A15 → D01–D15）→ Phase 3 全量复核与自我证伪（V0×4 边界重验全过、173 条发现锚点失效率 0%、P1 全集逐条证伪、V4 复跑绿→绿）。全程只读：仓库 tracked 文件零改动，运行时写入全部隔离至 /tmp 命名空间。

**总体结论**：oc-slimapi 是一台**契约纪律与测试锚定异常扎实、但存在 4 个 P1 级真实缺陷与一个系统性外围漂移层**的成熟 sidecar。核心判断：
- **v4 面已基本具备取代 v3 的能力**（54 路由 44 等价 + 7 增强 + 3 变形 + 0 缺失路由），唯 per-directory sessions 列表（F-121）无服务端等价物且被 §17 non-goal 封堵；
- **契约质量高**（v4 逐节均值 4.74/5、v3 4.56/5；72 条硬不变量仅 1 条未锁定；错误码三向对账 0 幽灵码）；
- **4 条 P1 全部真实可达**：幽灵事件类型丢帧（F-001）、deploy 模板启动即 crash-loop（F-004）、merged 预算生产默认组合退化（F-006）、E-II 明文无认证面（F-251，部署边界未验证）；
- **无 P0**：未发现重大互操作中断、数据完整性破坏或广泛不可恢复影响。

**发现统计**（02-findings/INDEX.md，状态 ∈ {confirmed 170, refuted 3}；无 unverified_due_to_blocker）：

| 严重度 | confirmed | refuted | 合计 |
|---|---|---|---|
| P0 | 0 | 0 | 0 |
| P1 | 4 | 0 | 4 |
| P2 | 19 | 0 | 19 |
| P3 | 147 | 3 | 150 |
| **合计** | **170** | **3** | **173** |

按归一化大类（INDEX 保留原始标签）：defect ~40、risk ~25、docs ~18、test ~13、design/smell（模块化与坏味道）~15、contract ~9、ops/gap/security ~12 等。

**BLOCKED 单元对覆盖面的影响声明**：**零 BLOCKED**。15 个 A 项、8 个 E 项、全部 Phase 0/3 单元 DONE；全部机器可读产物（route-census / expected-keys / applicability / test-gap-matrix / inventory / manifest 七件套）均为 VALID，无 BLOCKED-STUB，故全文无 coverage-degraded 标注需求。

**覆盖限制**（结论置信边界）：CVE 未联网核查（A15 仅本地可证事项）；Tailscale ACL 实效不可实机验证（E-II 相关结论标「部署边界未验证」）；SQLite 并发语义与 SSE 背压极限为静态推演（无真实上游压测）；E-III stunnel 自身配置（TLS 版本钉扎等）不在审计面内。

## 1. 核心问题裁决（§8.3 七问）

| 问题 | 裁决 |
|---|---|
| **v4 能否全面取代 v3（供现有 v3 消费方迁移）？** | **基本具备**。残留差距 5 项：G1 per-directory sessions 列表（阻塞度**中**，唯一无服务端等价物项，F-121，§17 non-goal 封堵服务端补齐）；G2 全局 token 统一流 tokens=1（中）；G3 单页 limit>500（低）；G4 roots/start 参数（低）；G5 providers 投影弃字段（中，仅当消费方读取 api/key/env 等字段——与 F-017 安全收益同源）。迁移 checklist 16 项见 v3-retirement-plan.md §2。 |
| **v3 协议可淘汰性？** | 口径 a（迁移完备度）=**基本具备**（同上三值）；口径 b（拆除成本）=**中**：v3-only 路径 12 条（B1–B12 逐条 file:line），涉及 ~2% src 行（≈400-600 行）+ >15% 测试断言面（294 处 `v=3` 字面/32 文件）+ 契约双轨（288+713 行）+ 观测双维度。**现行裁决下唯一政策输出：维持 (3,4) 永久双版本**。可启动的机械性前置准备 5 项（CLIENT_CHANGES v4 章节、字段差集表、文档漂移修正、观测手册样例、resync 值域防线——均不动 wire）。 |
| **legacy/透传遗留是否清理完毕？** | **有残留**（清单+必要性评估见 D03）：proxy.py 终局边界本身**清净**（纯 404 终端 + docstring 五项退役职责负向证据确证）；但存在 shim×2（sse/hub.py、token_hub.py，有真实生产使用者不可直删，下线=7 处 import+35 测试文件的机械迁移）、死代码/只写不读 ~10 项（F-024 等）、v2 残余文档回声（F-019 actions 规范滞留 v2-contract、F-123 INTERFACE_MAP 头部 v3-only 声明）、passthrough 桶死维度、以及 **FastAPI 默认 docs 四路由 200 穿透**（F-137，装配缝隙）。 |
| **契约是否清晰完整？** | v4-contract 逐节均值 **4.74/5**（14×5 分 + 5×4 分）、v3-contract **4.56/5**（§11 测试矩阵 3 分为最低）。全局 = **清晰且完整，附小缺口清单**：4 个无主码（`param_version_mismatch`/`method_not_allowed`/`websocket_not_supported`/`invalid_request_body` 从未在契约错误表字面命名，F-152/153/154/025）、1 处两契约表述互斥（F-102↔F-155，文档级无 wire 破坏）、§4.1 limit 边界双形状待归一（F-025）、1 条硬不变量无自动化锁定（INV-03 版本窗变更=major，流程级）。无影响主路径裁决的矛盾。 |
| **代码质量/可维护性？** | 各维 A–E 档：**并发 B**（全仓 0 把 asyncio.Lock、7 信号量无环无死锁；但关停回调隔离缺口 F-007、事件循环阻塞点 F-201/271）；**SSE 状态机 B−**（replay 四级短路序与冻结文本逐条吻合、epoch 碰撞 10 年 ≈2.1e-10、零可达泄漏；但 2190 行上帝文件 + 76 型上游事件零观测丢弃 F-216 + 幽灵事件名 F-001）；**dbaux B+**（mode=ro+query_only 双保险+全参数化 SQL+URI 注入面封闭；断路器 2 个转移缺口 F-238/F-240）；**性能 C+**（F-006 生产默认必现、messages 列表尾部压缩/哈希在事件循环）；**模块化 C+**（上帝文件 3、questions/permissions 孪生 0.832）；**测试 B+**（580 期望键 0 个 P1/P2 gap、144 降级格字面兑现；35/53 路由 ?v=4 面 HTTP 级未扫 F-316）。Top 问题清单见 §7 与 backlog。 |
| **安全性？** | **按入口分层**：E-I（loopback）——**无高危**（唯一 P2 级为 F-017 providers v3 密钥中转面）；E-II（0.0.0.0 明文，本机实测活 LISTEN）——**风险清单**：F-251 P1 明文无认证全功能面 + F-017 HIGH 密钥面 + F-137 MEDIUM docs 穿透，**均标「部署边界未验证」**（Tailscale ACL 实效不可验）；E-III（stunnel mTLS）——**无高危**（F-252 allowlist 覆盖不完整降 P3）。secrets 扫描 260 tracked 文件**零真阳性**。 |
| **状态机健全性？** | 20 张卡片（超额覆盖 14 项底线）：**健全**——selector 优先链、replay 分类（零未定义转移）、singleflight 七机制、catalog 三预算、transform admission；**有未定义转移/缺口清单**——dbaux not_found 重探 AttributeError（F-012）、熔断期 inode 检查被早 return 跳过（F-238/D10-1）、inode 基线捕获双缺陷（F-240）、registry grace task 残留致 arming 永久失效（F-011）；**泄漏/竞态清单**——零可达任务泄漏（7 处 create_task 全有属主）；残余风险序 F-226 > F-225 > F-222 > F-011。 |

## 2. P1/P1 级发现详述（P1×4；P2 摘要见 INDEX/backlog）

**F-001 幽灵事件类型（P1 defect，SSE）**：`hub_types.py:73-77` IMMEDIATE 集监听 `permission.resolved`/`permission.v2.resolved`，而上游 opencode v1.18.18 发布的是 `permission.replied`（core/permission.ts:225,239,276 → event-v2-bridge.ts:35-44 无过滤直达 /global/event）。真实决议事件落 catch-all **静默丢弃**（零观测），sidecar 的 q/p IMMEDIATE 直推对「权限已决议」场景失效；幽灵名载体为 v2-contract §3/CHANGELOG/INTERFACE_MAP 的文档回声。修复=事件名对齐上游 + F-216 丢弃计数兜底。

**F-004 deploy 模板启动即 crash-loop（P1 contract/ops）**：`deploy/oc-slimapi.service:33` 残留 `OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2`，而 `config.py:817-822` 将该值钉死为恰 (3,4)（fail-closed，`test_config.py:140-145` 锁定）。照抄模板部署 → app.py validate 抛 RuntimeError → SystemExit(1) → Restart=on-failure 无限 crash-loop；且 `operations.md:92-94` 声称「模板已清理」与仓库事实直接矛盾（A14 裁 P1）。生产 unit 已自行清理（威胁面=新部署/重建路径），但权威模板与文档的矛盾必须修正。

**F-006 merged 预算组合退化（P1 defect，performance）**：默认 `max_message_bytes=32MiB > merged_max_bytes=8MiB` 时，fanout 候选 1 的预留即吃满预算，其余 ≤15 候选在领导者首个网络 await 前同步撞上 `cap<=0` 单向门（messages.py:656-657）返回 `_DEGRADED` **永不重试**——生产 systemd 模板 pin 32MiB 即必现，merged 静默退化为每页 1 条 inline（消费方需逐条 /full，流量反升，200 无降级标记）。测试以 `max_message_bytes=256KiB` 显式 pin 反向组合回避了该参数面（test_messages_merged.py:250-258）。

**F-251 E-II 明文无认证全功能面（P1 security，部署边界未验证）**：本机实测 `0.0.0.0:4097` 活 LISTEN（非模板推定）；sidecar 自身零认证零授权、directory allowlist 默认 None 且覆盖不完整（F-252）——E-II 入口下任意本网可达者可全功能调用（含 file 读、session 写、actions exec）。**Tailscale ACL 实效不可实机验证**，故按规则维持「部署边界未验证」标注；但「依赖 ACL 兜底明文面」这一姿态本身已构成应整改项（回环绑定/ACL 校验/告警，见 backlog #14）。

**P2×19 摘要**：运维类 7（F-007 关停隔离/F-008 legacy 日志永不清理/F-009 snapshot 无限增长/F-010 关停超时/F-011 grace 失守/F-015 无界表/F-339 runbook 缺口 19 条）；安全类 3（F-017 密钥面/F-252 allowlist 覆盖/F-137 docs 穿透）；性能类 2（F-201/271 事件循环压缩哈希）；契约/docs 类 4（F-025 limit 边界/F-121 directory 缺口/F-123 INTERFACE_MAP 头部/F-216 零观测丢弃——观测类）；模块化 3（F-301/302/304）。完整链见 02-findings/ 与 refactor-backlog.md。

## 3. v4 取代 v3：结论与差距清单（D01/D02）

- **矩阵**：54 路由全量填格——**等价 44 / 增强 7（providers 投影、session parity、expand href、v4 ETag、POST 等效族×3 等）/ 变形 3（sessions dbaux 化、events id:/重放、stream 独立 id:）/ 退役 0 路由级（5 个子面退役：sessions directory、tokens=1、SSE 握手抑制、resync 值域、legacy reason 终结）/ 缺失 0**（contract_only 行=0，经双计数机械验证）。
- **期望键 580** = happy_path×54 + v3_face×50 + v4_face×53 + feature_off×10 + boundary×14 + error_*×399；与 A1 矩阵、A12 CSV 三方集合相等。
- **差距 5 项**（§1 表）中 G1 为唯一无服务端等价物项：多工作目录客户端在 `?v=4` 只能全量拉取 + 客户端过滤（或双面并存期间继续用 v3 面）——D01 判阻塞度中，D02 给客户端侧缓解路径。
- v4 新增面实现完整度：readiness 十 ID 与 required ≡ U 自洽（蕴含守卫 ⑦ 有结构性强制点）、`capabilities["4"]` 静态性有测试锁定、§12 投影含修订三 limit（指纹 providers-projection-v2 与 v3 域结构性隔离）、§13 parity 同 projector 不变量成立、§16 POST 族等效性（archive octet 缺省判据/合成体/DELETE 实体原样转发）与契约逐条吻合。

## 4. v3 退役：双口径结论（v3-retirement-plan.md）

口径 a（迁移）：16 项 checklist，核心工作量集中 sessions 参数换轨、directory 缺口决策、503 降级处理、SSE 重连模型；oc-webui 已在 v4 且其踩坑（limit 分母丢失 → 修订三恢复）为唯一实证迁移风险样本。口径 b（**成本模型，非政策建议**）：12 条 v3-only 路径、拆除顺序要害 = v4 契约自包含化（B12）必须先行；拆除后回归网将失去 294 处 `v=3` 字面锚点（>15% 断言面）。**维持成本量化**：~2% src 行 + >15% 测试断言面 + 契约双轨 1001 行 + 观测双维度。**政策输出（合规）**：维持 (3,4) 永久双版本；机械性前置准备 5 项可启动。

## 5. legacy/透传遗留处置建议（D03）

26 编号遗留物裁决：**保留理由成立 10 / 不成立 14 / 部分成立 1 / 悬置 3**。核心建议：① proxy.py 终局确认清净（唯装配缝隙 F-137 需关 docs）；② 两 shim 随下次 sse 包 minor 收敛（7 import + 35 测试文件机械迁移），零成本摘除项仅 `_LAST_UPDATED_AT_BY_SID_MAX` re-export；③ F-024 死代码簇 + F-290/292 残链一次清扫；④ actions manifest **非死配置**（生产 unit override 已启用 + ~/.config 在位——修正 E1 初判）；⑤ qp_sweep 阶段 2 无排期无取消 → F-140 需 owner 决策（保留/取消二选一，不宜悬置）。

## 6. 契约质量：逐节评分与修订建议（D04）

两契约 35 节逐节四问全量完成。v4 低分节：§3.2（4 分，allowlist 位置措辞与 v3 §3a 互斥 F-102/155）、§4（4 分，limit 边界 F-025 + 422 触发集超 §4.3 枚举）、§8（4 分，四无主码）、§9/§11（4 分，矩阵三处偏差 F-156）；v3 最低 §11（3 分，测试矩阵与 tests/ 现状三处漂移）。修订建议按优先序：① 错误表补四个无主码字面命名；② §4.1 limit 矩阵与实现边界归一（1..500 vs 501-1000/>1000 双形状）；③ §3.2/§3a allowlist 措辞勘误；④ v3 §2/§3 supported 窗口 [3]→[3,4] 加注（F-151/125）；⑤ CHANGELOG 9 个历史码标注退役。硬不变量 72 条仅 INV-03（流程级）无锁定——建议在 release.sh 加版本窗断言（机械前置准备类）。

## 7. 工程质量总览（D05–D12 executive summary）

- **并发（D05）**：0 锁 7 信号量无环；singleflight 七机制成立、1.5.0 两修复回归完整；唯一结构性问题=事件循环阻塞簇（F-201/271，messages 200 尾部 gzip-6+sha256）与关停隔离缺口（F-007）；F-016 经 30k 次实验证伪（3.14 语义下零泄漏）。
- **SSE（D06）**：上游 89 型事件中 **76 型进 catch-all 零观测丢弃**（含 6 个 q/p 决议族——F-001 的系统性背景）；replay 分类零未义转移、barrier 原子性成立；TokenStreamHub 五族职责挤在 2190 行单类（F-301）；断连清理四路闭环，零可达泄漏。
- **dbaux（D07）**：只读双保险+单线程亲和+全参数化（3 SELECT+2 PRAGMA 门）扎实；WAL 陈旧读边界在契约明示范围内；缺口集中在断路器转移（F-238/240/012）与路径解析边角（F-241/242/248）。
- **安全（D08）**：三入口结论见 §1；header 注入/走私、JSON 炸弹、SSRF、secrets 四面全过；E-II 组合面（F-251+017+137）为唯一需要部署侧动作的簇。
- **性能（D09）**：内存上界表 31 行（bounded 26/unbounded 4——replay 域外壳、pending 防抖瞬态、qp_last_activity、QpSweep 三表）；延迟常量 33 条；F-006/F-201/271 为三大热点；MemoryMax=384M 与上界表 ≈356MiB 贴脸（无余量告警）。
- **代码质量（D10）**：错误构造 80 点三路并存但形状同源零裸抛；重复热区 Top1 = questions/permissions 孪生（0.832）；魔法数 9；注解密度高（返回 89%/形参 75%）但无 mypy 门禁。
- **模块化（D11）**：25 入围文件（数据驱动）→ 8 拆分建议/17 保持论证；反向依赖 0；app.state 25 键 service locator（DI 化可行性中高）；变更热点×缺陷：questions.py 4 轮重复缺陷居首、proxy.py 拆分后缺陷归零（拆分效度实证）。
- **测试（D12）**：580 期望键矩阵 gap_severity NONE 339/P3 241/**P1 P2 双零**；v4 面 35/53 路由 HTTP 级未扫（F-316）；flaky 面：真延迟 148 处 ≈86s（最长 10s×2）；金样 2 份消费方唯一。

## 8. 整改 backlog Top（refactor-backlog.md，§8.4 冻结评分）

Top10（全 23 项 P1/P2 + P3 快赢簇见 backlog）：F-004（20.0）→ F-025（18.0）→ F-001（15.0）→ F-006（15.0）→ 九项 P2 并列 12.0（F-007/008/009/011/015/123/137/216/339）→ F-251（10.0）→ F-201/252/271（9.0）。依赖要害：F-216→F-001；F-252+F-339→F-251；F-201→F-271→F-301/302。P3 快赢簇：文档批量提交（~20 条一次关闭）+ 死依赖/死代码清扫。

## 9. 审计过程元数据

- **BASELINE_HEAD**：0b836e78c5de62d0c73b8593bf62c6650043dedf（0b836e7）；attempt=primary（新建分支，docs/audits/2026-08-20/）；脏基线（untracked 方案文件 1 个）→ 261 文件快照副本冻结于 /tmp/opencode/baseline-snapshot/。
- **attempt 沿革**：无恢复/无重跑（命名空间 recovery 事件 0 次；logs/superseded/ 仅常规自建文件版本归档）。
- **check.sh 首尾**：基线 `3316 passed, 18806 warnings in 127.47s` ✅ → 复跑 `3316 passed, 18806 warnings in 116.48s` ✅（绿→绿一致，仅时长差）。
- **V0 边界重验**：seq 1–4 全过（261/261、零漂移、墓碑恒空）。
- **耗时**：Phase 0 ~15min；Phase 1（18+6 agent 批次）~2h；Phase 2（4+5+6 agent 批次）~3.5h；Phase 3（3 复核 agent + V4/V5）~1h；合计约 7 小时墙钟（agent 并行折算 ~40+ 核时）。
- **覆盖限制声明**：CVE 未联网核查；Tailscale ACL 实效未验证（E-II 结论标注规则已执行）；SQLite 并发/SSE 背压为静态推演（E6 接受残余风险）；stunnel 自身不在面内；pytest 用例级计数以运行输出为准（rg 函数级 2642 为静态计数）。
- **产物**：AUDIT_ROOT 3.5MB / 273 文件（00-baseline、01-explore×14、02-findings×174、03-reports×15、04-final×6、manifest×9+phase-verify×5、logs）。

### 验收清单（§11，C1–C16）

| # | 项 | 判据 | 结果 |
|---|---|---|---|
| C1 | 七问离散裁决 | §8.3 表填满无越权 | ✅ §1（口径 b 已标成本模型；无「建议淘汰」表述） |
| C2 | 发现 file:line + 快照 | 抽查 INDEX 前 20 可回溯 | ✅ V1 锚点失效率 0/173 |
| C3 | 负向断言 rg 证据 | 抽查 10 条 | ✅（F-001 replied 零处理、F-016 实验证伪、D03 五项职责负向证据等） |
| C4 | P0/P1 双轨证伪；blocker 关联 | verification-log V2 | ✅ 4×P1 逐条三步证伪；0 blocker |
| C5 | A1 键集合==期望键 | 集合差双向空 | ✅ 580=580（validate_gap_matrix VALID） |
| C6 | 错误码三向对账全量 | D04 vs inventory | ✅ 实现 40 code ↔ 契约 ↔ CHANGELOG；4 无主/0 幽灵/1 live 文档码（inventory 34 的正则局限已声明） |
| C7 | 硬不变量全量带锁定列 | ≥40 或说明 | ✅ 72 条，未锁定 1（INV-03 流程级 P3） |
| C8 | 状态机 ≥14 卡 | state-machines + D06 | ✅ 20 卡 + D06 三子系统深化 |
| C9 | A8 九项×三入口分层 | E-II 标注 | ✅ D08 矩阵；E-II 全部标「部署边界未验证」 |
| C10 | test-gap-matrix 四项校验 | validation.txt | ✅ RESULT: VALID（主键唯一/枚举/非空/集合相等） |
| C11 | backlog 锚点冻结评分 | refactor-backlog | ✅ 23 项 score+排序+依赖 |
| C12 | check.sh 首尾一致 | logs/ 两份 | ✅ 3316→3316 绿→绿 |
| C13 | 工作区零污染 | git status + manifest + ignored diff | ✅ V0 seq4 过；tracked 零改动；ignored 副产物 diff = 0 新增（egg-info 零新增——pip/check 全经执行器隔离） |
| C14 | CHANGELOG 4.2.0–4.4.0 逐条 | D14 表 | ✅ 20/20 全对 |
| C15 | 数字可溯 inventory/命令 | 抽查 D01–D15 各 3 处 | ✅ V5 定稿一致（口径差已声明） |
| C16 | A15 资产枚举=全集；CVE 声明 | D15 | ✅ 260 tracked 全分类；CVE 覆盖限制已列 |

## 10. 附录：产物索引（相对 AUDIT_ROOT）

- `00-baseline.md`；`manifest/`（baseline-head/input-paths/deleted-paths/file-hash-manifest/ignored-baseline/runtime-mode(final)/namespaces/bootstrap-env/phase-verify{1-4,index}）
- `01-explore/`：inventory.json、file-cards.md（含 parts/ 19 份）、route-census.csv+md+validation.log、expected-keys.csv、inherited-error-applicability.csv+validation.txt、config-census.md、state-machines.md、dataflows.md、docs-notes.md、test-census.md、upstream-notes.md、exploration-log.md
- `02-findings/`：F-001…F-370（173 份）+ INDEX.md
- `03-reports/`：D01–D15
- `04-final/`：AUDIT-REPORT.md、v3-retirement-plan.md、refactor-backlog.md、test-gap-matrix.csv+validation.txt、verification-log.md
- `logs/`：check-baseline/final.txt、pip-list/check.txt、u011-counts.txt、probes/（六冻结脚本+hash）、superseded/
- `/tmp/opencode/`：probes/runtime-cache/baseline-snapshot 命名空间（attempt 后随 /tmp 清理）
