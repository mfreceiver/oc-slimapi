# 流量优化实施计划（A 上游省流 / B1 ETag / C2a T17 路由 / B3 内容指纹）

> **For agentic workers:** 本计划按批次实施，每批 TDD + `./scripts/check.sh` + rev-gpt ≥9.5 门控；全部完成后 rev-sgpt 整体终审。评审链与门控流程见 §8。

**状态：v1.8（定稿）—— 方案门控 rev-sgpt 9.6 PASS；Batch 1-4 已全部实施，分批门控通过（Batch 1: 9.8 / Batch 2: 9.6 / Batch 3: 9.5 / Batch 4: 9.8）；全量终审进行中（rev-1 FAIL 9.2 → 修复完成待复评，见 §13 实施记录）。**

> **v1.1 修订记录**（闭合 rev-1 / rev-sgpt 2026-08-16 门控 FAIL 7.4 的 4 blocking + 4 conditions）：
> B1 ETag 改为**最终表示**（identity 投影后 body + 表示版本）计算、全量 sha256、合并 Vary、304 复制路由辅助头（含 merged 模式）；A 批 single-flight 改 **join-first**（原始 GET 移出池 admission，per-app registry + lifespan 生命周期 + 运行时开关）；A1 增加**字节预算**与单条上限旁路；B3 选定**无状态指纹方案甲**、发版前单边冻结语义（放弃 provisional-wire-then-drift 路径，方案乙记录为否决）；修正 sessions 列表 turn-merge 漂移；§10/§12 补风险与回归矩阵。
>
> **v1.2 修订记录**（闭合 rev-2 / rev-sgpt 复审 FAIL 8.8 的 3 blocking + 4 conditions）：
> ①A 批 join-first 增加 raw-fetch 并发约束；②B1 改 per-coding 验证器；③B3 指纹定为 merged splice 后重算 + 规范细化 + ocdroid smoke 发版门禁；④B1-C4 断言修正；⑤A1 配置边界测试扩充。
>
> **v1.3 修订记录**（闭合 rev-3 / rev-sgpt 复审 FAIL 9.1 的 1 blocking + 4 conditions）：
> ①raw-fetch 约束升级为引用计数 flight lease + 字节预算；②B1 规范文本统一；③B3 跨表示模式不可比较；④merged degraded 专项回归；⑤指纹语义密码学严谨化。
>
> **v1.4 修订记录**（闭合 rev-4 / rev-sgpt 复审 FAIL 9.3 的 1 blocking + 4 conditions）：
> ①放弃"直接复用 `SingleFlight`"——新增 `LeasedSingleFlight` 显式 API 协议；②默认容量退化显式说明；③清除 3 处旧绝对化表述；④外部 lane 指纹消费限定同一表示模式；⑤Batch 1 配套 knob 计数对齐。
>
> **v1.5 修订记录**（闭合 rev-5 / rev-sgpt 复审 FAIL 9.4 的 1 blocking + 3 conditions）：
> ①冻结取消状态机（三分支：普通异常=FetchFailed；leader 取消=取消 shared future+存活 waiter 重领飞；waiter 自身取消=await 分支恰好释放一次）；②A3 补"预算可用"限定；③registry 引用改单一 ownership 原位转换模型 + 结构化 snapshot；④调用伪代码改合法两阶段 + A1 teardown。
>
> **v1.6 修订记录**（闭合 rev-6 / rev-sgpt 复审 FAIL 9.4 的 1 blocking + 2 conditions）：
> ①预算返还拆双规则（成功=归零返还；失败=立即返还+残留引用纯计数）+ 引用句柄绑定 entry generation；②删除残留非法伪代码；③shutdown 收敛规则（CD-1 式 entry identity 检查）。
>
> **v1.7 修订记录**（闭合 rev-7 / rev-sgpt 复审 FAIL 9.4 的 2 blocking）：
> ①两层 registry 结构（active + retired tombstone）+ 定向释放句柄直接绑定 `_Entry` 对象 + snapshot 统一视图；②shutdown-success = 成功路径无-grace变体（retained 态，预算保留至最后 caller 归零）。
>
> **v1.8 修订记录**（闭合 rev-8 / rev-sgpt 复审 FAIL 9.4 的 1 blocking + 1 condition）：
> ①**blocking（shutdown 原子转换窗口）**：`shutdown()` 清除 active 层时，in-flight entry **立即原子转 retired 层（ownership_state=in-flight，detached）**——继续计入 `leased_bytes`、不可 join、无 timer；随后 factory 成功 → retired 内 in-flight→retained（预算保持到 callers 归零）；factory 失败/取消 → →failed（立即退款）。entry 在任何时刻必属 active 或 retired 之一，ledger 等式无脱离两层集合的计账窗口。A2-C4⑧ 补中间点断言（factory 阻塞时 shutdown → snapshot 出现 `layer=retired, ownership_state=in-flight` 且 `leased_bytes` 仍等于预扣 → 放行后验证 retained 与终态归零）。②**condition**：retired 层总述改分状态表述（不可 join、无 timer；**failed 不占预算，retained / detached in-flight 继续占预算**）。

**Goal:** 基于 2026-08-15 全天 access log 实证（40,108 reqs / upIn 1,631M / downOut 183M），落地四组优化：Batch 1 上游去重/缓存（内部零 wire）、Batch 2 ETag/304 条件请求（加性 wire）、Batch 3 todo/children thin 路由（T17 提案获批，加性 wire）、Batch 4 消息内容指纹（2026-07-26 第2类的单边可冻结子集，加性 wire，语义发版前冻结）。全部向后兼容，`X-Slimapi-Version` 恒 2 不 bump。

**Architecture:** 维持现有 Python sidecar 架构。A 批全部在 sidecar 内部（TTL 缓存 + single-flight 去重，join-first 共享原始 GET），客户端零 wire 感知；B1 在既有 GET 路由上对**最终表示**出 ETag/304；C2a 按 T17 设计稿新增两条只读 thin 路由；B3 为消息 skeleton 加性 `contentFingerprint` 字段（内容确定性指纹，无状态、无单调性声明）。ocdroid 消费侧（B2 digest 驱动 / C1 降频 / ETag 接入 / 指纹消费）走外部 lane。

**Tech Stack:** Python ≥3.11 async sidecar（FastAPI + httpx + orjson + pytest），基线 git `76fc13b`（v1.4.0），tests 基数在 Task 0 实测记录。

---

## 0. 用户决策记录（2026-08-16，不可回退）

1. **范围**：A（A1-A4）+ B（B1 ETag / B2 digest 驱动 / B3 watermark）全做；C 只做 1+2 —— C1 = ocdroid sessions 轮询降频（外部 lane），C2 = 匿名消费方迁移路径就绪（T17 todo/children 提案**获批实施**）。C3（503 退避）、C4（匿名身份排查）**不做**。
2. **门控流程**：本方案 rev-sgpt ≥9.5 → 分批实施，**每批 rev-gpt ≥9.5** → 全部完成后 **rev-sgpt 整体终审** → 发版。方案文档任何修改使既有评分作废，须新 generation 复审。
3. **ocdroid 协同**：对方主会话 `ses_ffadc95a0ffebbDxadBiZalBZ6` 手头任务完成后通知配合项（§9 清单）；对方同样"整理方案→门控→分阶段实施→审阅"。
4. **单边开发授权**：我侧变更不影响 ocdroid 当前版本功能（全部内部/加性）→ 自行先开发，并同步更新契约（v2-contract 加性 rev + CHANGELOG + INTERFACE_MAP + CLIENT_CHANGES）。**AGENTS.md「v2-contract 非用户明确要求不要改」限制由本条解除**（仅限加性 rev）。
5. **B3 暂停解除与单边冻结边界**：2026-07-26 计划"用户明确下一步前暂停第2类 wire"——本指令即下一步。本计划只单边交付**可独立冻结语义**的子集：无状态内容指纹（方案甲，语义 = 在 SHA-256 碰撞概率可忽略的工程模型下，指纹相同指示投影内容相同，同 sidecar 表示版本内）。**不**单边引入有状态 revision（方案乙，见 §6 否决理由）；**不**单边冻结 token idle/resync、SSE 开关 reconcile 三分法等**客户端行为语义**——那些仍按联合计划走双方联调冻结（外部 lane 项 4）。发版契约中的指纹字段语义为**终态**（非 provisional）。

## 1. 证据基线（2026-08-15 access log，40,108 reqs）

### 1.1 总览

| 客户端 | reqs | upIn(sidecar←opencode) | downOut(client←sidecar) |
|---|---:|---:|---:|
| ocdroid（3 设备，0.24.0×2+0.25.0） | 28,867 | 1,630.7M | 182.1M |
| 匿名（无 client 头） | 11,241 | 0.6M | 0.7M |

### 1.2 流量 top（upIn 视角）

| 路径 | reqs | upIn | downOut | 均值 up/req |
|---|---:|---:|---:|---:|
| `GET /slimapi/messages/{sid}` | 2,627 | **1,020.8M (63%)** | 162.0M (89%) | 397.9K |
| `GET /slimapi/agent` + `/command` | 832+832 | **503.6M (31%)** | 6.1M | ~310K |
| `GET /slimapi/sessions` | 18,926 | 69.2M | 11.9M | 3.7K |
| `GET /slimapi/questions` | 2,631 | 36.5M | 0.2M | 14.2K |

### 1.3 轮询模式（中位间隔）

`/slimapi/sessions` 2.0s（占全天 47% 请求）；匿名 legacy `/session/status` 2.0s；`/slimapi/questions` 2.4s / `messages` 3.5s / `sessions/status` 3.0s；catalog（agent/command）8.1s；匿名 todo 5.0s / children 2.0s / diff 4.8s / permission 7.5s。

### 1.4 并发重复抓取（1s 窗口可合并率，实测）

| 路径 | total | 可合并 | 比例 | max burst |
|---|---:|---:|---:|---:|
| `/slimapi/sessions` | 18,650 | 12,017 | **64%** | 43 |
| `/slimapi/messages/{sid}` | 2,606 | 758 | **29%** | 5 |
| `/slimapi/questions` | 2,599 | 1,331 | **51%** | 5 |
| `/slimapi/sessions/status` | 1,800 | 631 | 35% | 5 |
| catalog agent/command | 815/817 | 60/63 | 7-8% | 3 |

### 1.5 其它实证

- 503 聚集：15:15:33–15:17:10（opencode 重启，restart action 自身 504）97s 内 1,143 次 503；匿名轮询无退避（`/question` 错误率 79%）。
- SSE 很便宜：events 97 连接（均值 158s）仅 1.58M；token stream 972 连接 218K —— 策展 SSE 有效，成本在 REST 轮询。
- v1.4.0 四能力（merged/transformAbsorb/permissions/tokenCoalesce）当天无人消费（刚发版，预期内）。
- 匿名消费方：无任何 client 头，全天 09:00–22:39 活跃，走 catch-all legacy 路径（含 121 次 `POST /session/{sid}/prompt_async`）。

## 2. 范围与分批总览

| Batch | 内容 | wire | 预期收益（upIn / downOut 每天基线） | 评审 |
|---|---|---|---|---|
| **1** | A1 catalog TTL 缓存 + A2 messages single-flight + A3 sessions(含 status) single-flight + A4 questions/permissions single-flight（全部 join-first，`coalesce_enabled` 开关） | 内部（catalog TTL 有新鲜度语义，CHANGELOG 披露） | upIn 省 ~600-750M（A1 250-390M + A2 ~300M + A3 ~44M + A4 ~18M） | rev-gpt ≥9.5 |
| **2** | B1 ETag/304（messages 列表 + sessions 列表 + agent + command；最终表示 ETag） | 加性 | downOut 省 ~10-60M（catalog 304 ~6M + messages/sessions 命中率待实测） | rev-gpt ≥9.5 |
| **3** | C2a：`GET /slimapi/sessions/{sid}/todo` + `GET /slimapi/sessions/{sid}/children`（T17 设计稿） | 加性 | 匿名 2,054 reqs/天迁移就绪（gzip ~22-30% downOut） | rev-gpt ≥9.5 |
| **4** | B3：消息 `contentFingerprint` 加性字段（方案甲，语义发版前冻结）+ 契约 + 设计文档 | 加性 | B2/C1 结构性省流的地基（客户端按内容变化拉取） | rev-gpt ≥9.5 |
| 终审 | rev-sgpt 全量 diff 审阅 → `./scripts/release.sh minor` | — | — | rev-sgpt ≥9.5 |

顺序严格串行：1 → 2 → 3 → 4 → 终审。外部 lane（§9）与 Batch 1-4 并行推进（不占本仓写域）。

---

## 3. Batch 1 — A：上游去重/缓存（内部）

### Task 1.0: 基线记录

- [ ] `./scripts/check.sh` 全绿，记录 passed 数 + `git rev-parse HEAD`（应为 `76fc13b` 或其后续，若 HEAD 已前进须核对增量 diff 与本计划无冲突）

### 3.x 共享设计约定（A2-A4 single-flight，join-first）

> 回应 rev-1 blocking 2：键、生命周期、admission/grace 语义在此统一闭合，三个任务共同遵守。

- **Join-first（原始 GET 移出池 admission）**：共享单元 = 上游 GET + cap-read（`read_with_cap` 流式限量读，内存有界）。caller 先经 `registry.fetch_or_bypass` 取得原始 body（合法两阶段调用形态见下文 `Lease` 条目），**再**各自走现行池 admission + offload 投影（admission 语义、`transform_busy` 503/吸收路径逐字节不变）。由此："一次 GET"由构造保证（全部 caller join 同一 in-flight leader），**不依赖 completion grace 覆盖 admission 排队时长**——grace（沿用 1.0s 默认）只服务完成后短窗内到达的 straggler。
  - 与 CD-1（`sse/singleflight.py`，/full 直取为 admission-first）的差异是有意的：列表路由的 key 数少（distinct sid/directory/query 组合个位数），join-first 不会造成上游 GET 并发爆炸；而 admission-first + 固定 grace 在 max_transforms=1 串行投影下无法稳定保证合并 AC（rev-1 指出）。上游 hang 场景的行为差异（caller 等待共享 GET 的 httpx 超时而非快速 503 transform_busy）记入 §10 风险表与 CHANGELOG 内部行为披露。
  - 回归锁定：`transform_busy` 在投影 admission 处仍可发生（与今天相同的触发条件），既有 CD-1/transform 测试必须全绿不动。
- **`LeasedSingleFlight` 显式 API 协议（v1.4，回应 rev-4 blocking——放弃"直接复用 `SingleFlight`"：其 `fetch()`/`_drop()`/`shutdown()` 无 caller 引用钩子、无预算旁路原子性，不足以承载 lease 生命周期，必须新增类）**：新模块 `src/oc_slimapi/leased_singleflight.py`（复用 `singleflight.py` 的 `FetchFailed` 信封、leader-取消自愈、serial-point 淘汰纪律与 docstring 级语义说明，但**独立实现**，不改 CD-1 在用类——`fulls` registry 保持字节不动）。协议：
  - **`async fetch_or_bypass(key, factory, reserve_bytes) -> Lease | None`**：调用点在**同步无 await 的 serial point** 完成判定——已有 flight 则原子 join（先计 waiter 引用再 await future）；无 flight 则 `try-reserve(reserve_bytes)` 预算：成功 → 注册 entry（登记 **registry in-flight ownership**）并领飞；失败 → **不注册 entry、返回 `None`**，caller 立即走现行 admission-first 直取路径（该 key 本轮不 coalesce——已 join 的既有 waiter 不受影响，未发布的 body 绝不脱离预算，rev-4 指出的"无 lease 共享 body"通道在构造上不存在）。
  - **registry 引用与预算返还 = 单一 ownership 原位转换 + 双规则返还（v1.7 与两层结构一致）**：flight 创建时在 active 层登记**一份** registry ownership（in-flight 态）并预扣整笔 `reserve_bytes`；**成功完成时该 ownership 原位转换为 grace 态，不新增第二份引用**；`_drop()`（grace 到期/预算淘汰）/`shutdown()` 释放 grace 态 ownership 一次。**预算返还双规则**：
    - **成功路径**（body 已产生并驻留——leader 引用、waiter 引用、grace/retained ownership 均关联真实 bytes）：**grace ownership 已释放 且 caller 引用归零**才返还整笔预算（覆盖 "GET → 等待 admission → caller 消费完成" 全生命周期）。
    - **失败路径例外**（factory 普通异常或 leader 取消——**从未产生 body，无字节驻留**）：registry 在 entry 移除 active 层时**立即返还预扣预算**，entry 转 retired（failed 态）——此后残留的 caller 引用（waiter 尚未走到释放点）**仅为纯计数，不再关联任何预算**——不违反 ledger 不变量（`leased_bytes == sum(在账成功/在飞 flight 的 reserve_bytes)`，failed retired entry 不占预算），且使**自愈重领飞在默认单 flight 预算下可行**（旧 flight 预算已返还，被唤醒的 waiter 重新 try-reserve 能成功而非被迫 None 旁路）。
  - **两层 registry 结构 + 引用句柄绑定 entry generation（v1.7，rev-7 blocking 1——补全 retired-entry 生命周期）**：registry 维护两层结构——
    - **`active` lookup**：`{key: _Entry}`，每 key 最多一个可 join 的 flight（join/reserve/lead 均只作用于 active 层）；
    - **`retired` tombstone 层**：`{(key, seq): _Entry}`，保存"已从 active 移除、但仍有 caller 引用或预算在账"的旧 generation——**不可 join、无 timer**；预算归属**分状态**（v1.8，rev-8 condition）：`failed` 态（未产生 body）**不占预算**、`retained` 态（成功后 body 驻留）**继续占预算至最后 caller 引用归零**、`in-flight` detached 态（shutdown 清除 active 时尚未完成，见下条）**继续占预算**。**统一删除条件 = `caller_refs == 0` 且该 entry 已不计入 `leased_bytes`**（detached 态即使 caller 引用归零，factory 未完成、预算在账期间不删除，v1.8.1 rev-9 cond 2）。失败 flight 移除 active 时**转 retired（failed 态）**；shutdown 清除 active 时 in-flight entry **原子转 retired（in-flight detached 态）**（见下条）。
    - **定向释放句柄直接绑定 `_Entry` 对象**（`Lease`/waiter 登记持有 entry 引用本身，`(key, seq)` 仅为 snapshot 展示与测试断言用）——释放时**绝不重查 active lookup**（该位置可能已是 `seq=N+1` 新 flight），只递减所属 entry 自身计数——同 key 新旧 flight 并存时（leader 取消后 waiter A 先建新 flight、waiter B 后释放旧引用），按对象定向释放不误减新 entry。
    - 新 flight 的 grace/drop 独立操作其自身 entry 的 ownership；**A2-C6 场景中的 failed 旧 generation** 在 retired 层仅等计数归零，不参与预算、不参与 join、不接受新 timer。**双 waiter 交错确定性测试**（A2-C6）锁定该协议。
  - **`Lease`（async context manager）**：`lease.body`（原始 bytes）+ `__aenter__/__aexit__`；**caller 在取得 result 之前已计入引用**（join 时 waiter 引用先于 `await future` 登记；leader 领飞时 leader 引用先于 factory 执行登记）；`__aexit__`（投影成功/异常/取消，经 `finally` 语义）**恰好释放一次**（幂等去重防 double-release）。
  - **调用形态（v1.5，rev-5 condition 3——两阶段、合法 Python、`None` 分支显式；Lease 返回到进入 context 之间无 await，杜绝悬挂引用窗口）**：
    ```python
    lease = await registry.fetch_or_bypass(key, fetch_raw, reserve_bytes)
    if lease is None:
        return await admission_first_direct_fetch(...)   # 现行直取路径
    async with lease:                                    # __aexit__ 恰好释放一次
        body = lease.body
        ...  # 池 admission + offload 投影
    ```
  - **取消状态机（v1.5 冻结，回应 rev-5 blocking——消除"FetchFailed 传播 vs 重领飞"矛盾，覆盖"引用登记后、future resolve 前"取消窗口；镜像 CD-1 `singleflight.py:143-190` 对两类取消的区分，在 `LeasedSingleFlight` 内实现）**：
    - **普通 factory 异常**（网络错误/上游 5xx/解析失败）：释放 leader 引用，entry 移除并按**失败路径规则立即返还预扣预算**（未产生 body，无驻留），**不进 grace**；异常经 `FetchFailed` 信封传播，全部 waiter 各自释放自身引用后抛出同一异常实例（此时残留引用为纯计数）。
    - **leader 被取消**：释放 leader 引用，entry 移除并按**失败路径规则立即返还预扣预算**；**取消 shared future**（非 `FetchFailed` 包装——与 CD-1 同判：取消语义必须可被 waiter 区分"是我的取消还是 flight 的死亡"）；存活 waiter 的 shield 分支**先恰好释放旧 flight（按其 `(key, seq)` 定向）的 waiter 引用**，再回到无 await serial point 重新 join/reserve/lead（重领飞——旧 flight 预算已返还，重新 try-reserve 可成功）；每次循环**旧引用先释放、新引用后登记**，绝不跨 flight 累积。
    - **waiter 自身被取消**（含 rev-5 指出的泄漏窗口：join 引用已登记、`fetch_or_bypass` 尚未返回 Lease、`__aexit__` 不存在的路径）：registry 的 await 分支以 `try/except CancelledError` 包裹——**先判断是否自身取消**（`task.cancelling() > 0`，CD-1 `_current_task_cancelling` 同判），是则**恰好释放一次自身 waiter 引用、向 caller 传播取消**；**绝不取消 shared future、绝不影响 leader 与其他 waiter、绝不重领飞**（自身取消的 caller 语义 = 放弃本次请求，与现行直取路径被取消等价）。leader 后续成功时其余 waiter 照常取得 body。
    - **不变量**：任一取消/异常路径走完后，该 caller 在 ledger 中的引用恰好归零（幂等释放保证多次触发不 double-release）；`shutdown()` 与上述路径并发时引用释放仍恰好一次（shutdown 释放 registry 引用，caller 引用由 caller 路径释放，互不侵占）。
  - **registry 引用与释放路径（v1.8 与双规则/两层结构一致）**：entry 成功完成 → registry ownership 原位转 grace 态（active 层内）；`_drop()`（grace 到期/预算淘汰/`shutdown()`）释放 grace ownership 一次，entry 转 retired（retained 态）；grace ownership 释放且 caller 引用归零 → 返还整笔预算并删 retired entry（成功路径）。factory 失败/leader 取消 → entry 移除 active 并**立即返还预扣**，转 retired（failed 态，纯计数，归零即删）。**`shutdown()` 原子转换（v1.8，rev-8 blocking——消除"清 active 后、factory 完成前"的账本脱离窗口）**：shutdown 清除 active 层时，每个 in-flight entry **立即原子转 retired 层（ownership_state=in-flight，detached 态）**——继续计入 `leased_bytes`、不可 join、无 timer；entry 在任何时刻必属 active 或 retired 之一（ledger 等式无脱离两层集合的计账窗口）。此后 factory 走向二选一：**成功** → detached in-flight 转 **retained 态**（成功路径无-grace变体：不进 grace、不设 timer、不再记账，释放 registry ownership 但预算保留至最后一个 caller 引用归零——body 已实际产生并交给 waiter，leader 引用释放不触发退款）；**失败/取消** → detached in-flight 转 **failed 态**（立即退款，残留引用纯计数）。已 await 的 waiter 仍从 future 取得 body（结果不撤回），各自释放 caller 引用后预算才归零、retired entry 才删除。**shutdown 收敛规则**（identity 检查语义并入上述转换：完成路径核验 entry 状态，detached 态不重入 active/grace、不新建 timer，镜像 CD-1 `singleflight.py:202-232`）。
  - **ledger 账本（可观测不变量，v1.8 结构更新）**：`leased_bytes`（当前驻留预算）+ 结构化 snapshot 为 **active + retired 统一视图**：`flights: {key: [(layer, seq, caller_refs, ownership_state)]}`——`layer` ∈ active / retired；`ownership_state` ∈ in-flight（active 层在飞，或 retired 层 detached——shutdown 原子转换产物，两者均计预算）/ grace / retained（成功移入 retired，预算仍在账）/ failed（失败移入 retired，预算已返还，**不计入 `leased_bytes`**）。**retired 条目在 snapshot 中可见**（供 A2-C6 新旧并存断言与失败/Shutdown 中间态断言），仅 in-flight（含 detached）/ grace / retained 计入 `leased_bytes`。不变量 = **`leased_bytes == sum(ownership_state ∈ {in-flight, grace, retained} 的 reserve_bytes)`，且每一在账 entry 在任何时刻必属 active 或 retired 之一**（无脱离两层集合的计账窗口，v1.8）；retired entry 的 caller_refs 归零且预算已返还后从 snapshot 消失。并发完成/淘汰/释放均在事件循环 serial point 执行（无 await 间隙），A2-C4 直接断言 ledger 峰值与终态归零。
  - **预扣不返还差额（v1.4，rev-4 condition 1——维持完整预留不变量）**：预算按 `reserve_bytes = max_response_bytes` 预扣，**读取完成后不按实际 body 大小返还差额**（避免"读取期间完整上限预留"被打破）；实际 body < 预扣的利用率损失接受为保守正确性代价。**默认容量退化显式说明**：默认 `raw_fetch_max_bytes=64 MiB` × `max_response_bytes=64 MiB` → **默认配置下同时只有 1 个 leased distinct flight**，`raw_fetch_concurrency=4` 在默认预算下不可达——这是刻意的保守默认（内存证明优先）；调优指引（预算 ≥ 期望并行 flight 数 × max_response_bytes）写入 `docs/operations.md` knob 说明。各"一次 GET"AC 一律限定"**预算可用、未触发降级时**"；预算耗尽的压力断言改为"响应正确 + ledger 有界"（不要求 coalesce）。
- **`raw_fetch_concurrency`（默认 4）= 纯网络读取并发限制**（同时 in-flight 的上游 GET 数 ≤ N，防连接/句柄耗尽），与内存上界解耦。
- **validate() 组合上界校验**：`raw_fetch_max_bytes + max_transforms × max_response_bytes` 记入聚合内存推导注释与校验（与既有 T3 校验同模式：两项之和须 ≤ `MemoryMax` 预算口径），两套预算不再各自独立通过 512 MiB 级校验。
- 旁路（`coalesce_enabled=false` 或预算满返回 `None`）维持现行 admission-before-GET 顺序，不经 lease/semaphore。同 key waiter join 不新增预算占用（共享同一 flight 的预扣）。
- **Registry 归属与生命周期**：**不建进程级全局 registry**。每 app 实例一套 `LeasedSingleFlight`，挂在 `app.state`（与 `upstream`/transform pool 同层），lifespan teardown 调 `shutdown()`（见上条协议：清 active entries、取消 grace timer、释放 registry 引用——**shutdown 后 registry 显式可复用**（与 CD-1 同语义；实现 `leased_singleflight.py` `shutdown()`：shutdown 后新的 `fetch_or_bypass` 正常建新 entry、正常记账，服务受控重启与测试 shutdown→断言 ledger 归零→复用的模式；安全性论证：shutdown 前的 entry 经 detached 收敛规则**不可能回流 active/grace、不新建 timer**，跨 shutdown 边界 ledger 不变量 `leased_bytes == sum(在账 entry)` 保持——detached entry 在 factory 完成前继续在 retired 层计账，v1.8.1 rev-9 cond 1；终审 C1 措辞对账，2026-08-16）。key 额外嵌入 `id(app.state.upstream)` 作 defense-in-depth（镜像 `full_fetch_key` 的 `id(scope)` 惯例），杜绝跨 app/test/upstream 串数据。
- **key 构造**：query 参数规范化排序后参与 key（防同义 query 拆键）；directory 原样参与（同资源不同 directory = 不同上游资源）。
- **运行时开关**：`coalesce_enabled`（默认 true；false = 完全旁路 registry 走现行直取路径，行为与今天一致）。旁路路径是受测代码分支，不是死代码。
- **错误语义**：leader 失败 → 同一异常实例经 `FetchFailed` 信封传播给全部 waiter（信封概念复用 CD-1，实现于 `LeasedSingleFlight`）；绝不 negative-cache；leader 被取消 → 存活 waiter 重新领飞（CD-1 自愈语义，在 `LeasedSingleFlight` 内实现）。

### Task 1.1 (A1): catalog TTL 缓存（agent / command）

**Files:** Create `src/oc_slimapi/catalog_cache.py`、`tests/test_catalog_cache.py`；Modify `src/oc_slimapi/config.py`（新字段组）、`src/oc_slimapi/routes/agent.py`、`src/oc_slimapi/routes/command.py`、`docs/operations.md`（knob 表）

**设计：**
- `CatalogCache`：TTL 缓存，key = `(kind, directory_or_None)`（上游对 catalog 的 directory 是路由入参——command 上游忽略、agent 同构；按 directory 分桶缓存保正确）。value = `(status, raw_body_bytes, fetched_at)`。仅缓存 200 响应；4xx/5xx/坏 JSON 一律不缓存。
- **字节预算（回应 rev-1 blocking 3）**：双重上限——`catalog_cache_max_bytes`（总预算，默认 **16 MiB**）+ `catalog_cache_max_entry_bytes`（单条上限，默认 **1 MiB**；上游 body 超限则**旁路缓存**直接透传，不入账）。淘汰 = 按 `fetched_at` 最旧优先，**插入时与完成时即时执行**（镜像 SingleFlight 的 serial-point 淘汰纪律，事件循环单线程内无 await 间隙）。config `validate()` 对齐 config.py 既有 knob 的聚合内存校验模式：`max_entry_bytes ≤ max_bytes`、`max_bytes ≥ 1 MiB` 下限、非负 TTL 等，违反即启动失败。
- 刷新防击穿：TTL 过期后的并发刷新经独立 `SingleFlight` 实例去重（CD-1 类 plain registry，per-app，**由 `CatalogCache` 持有并在 app teardown 调用 `shutdown()`**——grace timer/retained body 不残留于停止的 app 生命周期之后，v1.5 rev-5 condition 3；**catalog 路径维持 admission-first 顺序不变**——不经 join-first/lease，内存仍受 transform admission + 缓存字节预算双重约束，rev-4 blocking 不适用于此）。
- transform/admission 语义不变：命中缓存时仍走池 offload 投影；未命中时现行 admission → 上游 GET → cap-read → 缓存 → offload。
- config：`catalog_cache_ttl_seconds`（默认 **300**，0=禁用回退现行为）、`catalog_cache_max_entries`（默认 16）、上述两个 bytes knob。`validate()` 断言齐备。
- gzip 不缓存：gzip 在响应路径按命中 body 现算（300K gzip ~ms 级，且 A1 落地后 catalog 请求大幅变便宜）。

**Acceptance Criteria:**
- `A1-C1`: TTL 内第 2 次 GET `/slimapi/agent`（同 directory）上游 handler 恰被调 1 次，两次响应 body 逐字节一致。
- `A1-C2`: TTL 过期后（测试用 0.2s TTL）并发 20 请求 → 上游恰 1 次刷新（single-flight 防击穿）。
- `A1-C3`: 上游 5xx/坏 JSON 不缓存（后续请求仍打到上游）；`catalog_cache_ttl_seconds=0` 时行为与今天逐字节一致。
- `A1-C4`: 字节预算回归——单条超限旁路（不入账、行为=未缓存）；总预算超限按最旧淘汰（构造 3 条小预算夹具验证逐条挤出顺序）。
- `A1-C5`（v1.2，condition 4）：配置边界矩阵——`catalog_cache_max_entries=1`（最小值语义）、TTL=0（禁用）、entry_bytes > total_bytes（validate() 拒绝启动）、entry_bytes=1MiB 与 total=16MiB 默认值上下界各一例；**淘汰与并发刷新一致性**——TTL 过期触发并发刷新的同时另一 kind 插入触发淘汰，终态账本一致（retained bytes 精确归零/入账，无双重计数）。
- `A1-C6`: `./scripts/check.sh` ✅；access log 新增可选字段 `cache: "hit"|"miss"`（ops 侧加性，`access_log.py` + `traffic-accounting.md` 一句话说明），供 §11 验证。

### Task 1.2 (A2): messages 列表 single-flight + `LeasedSingleFlight` 模块

**Files:** Create `src/oc_slimapi/leased_singleflight.py`（§3.x 显式 API 协议实现）、`tests/test_leased_singleflight.py`（模块级生命周期/预算/引用回归，§13 矩阵"A 批生命周期/raw-fetch 生命周期"行落点）；Modify `src/oc_slimapi/routes/messages.py`（列表 handler 接 3.x registry；`mode=merged` 的 phase-B fan-out 维持现行 `singleflight.fulls` 不动）；Create `tests/test_messages_coalesce.py`

**设计：** 按 3.x 约定。key = `("messages-list", id(upstream), sid, directory, canonical_query)`。共享单元 = 列表上游 GET + cap-read；投影/打包 per caller。merged 模式的列表页 GET 同样经此 registry（full 明细仍走 `fulls`，两级去重互不干扰）。

**Acceptance Criteria:**
- `A2-C1`: 并发 20 同 `(sid, query)` 列表请求（**预算可用、未触发降级时**）→ 上游列表 GET 恰 1 次（**join-first 构造性保证，不依赖 grace**），全部拿到一致结果（200，或一致的池忙 503——投影 admission 语义不变，与今天同触发条件）。
- `A2-C2`: 不同 query（limit/cursor/mode 不同）不互相合并；不同 sid 不合并；**跨 app 实例（两个 TestClient 各自 app）同 key 不合并**（scope 隔离回归）。
- `A2-C3`: 上游 5xx 时全部 waiter 收到同一异常实例映射的 503 `upstream_unavailable`；leader 取消后存活 waiter 重新领飞成功；`coalesce_enabled=false` 时上游调用数 = caller 数（旁路回归）；既有 tests 全绿。
- `A2-C4`（v1.4）：lease ledger 直接断言——①并发 N>预算容量个**不同 key** 大 body 请求时，超额 key 降级直取（`fetch_or_bypass` 返回 None 路径），全部返回正确结果；②慢投影夹具（admission 排队）下 **`registry.leased_bytes` 全程 ≤ `raw_fetch_max_bytes` 且全部 caller 完成后终态归零**（直接读 ledger 属性断言，不做间接推断）；③同 key 多 caller 共享一次预扣（ledger 峰值 = 单 flight 预扣）；④`raw_fetch_concurrency` 限在飞 GET 峰值（网络并发独立断言）；⑤validate() 组合上界（raw 预算 + transform 路径之和）回归；⑥`test_leased_singleflight.py` 覆盖 §3.x 协议逐路径释放（**九类**，每类断言 snapshot（per-entry `seq`/`caller_refs`/`ownership_state`）与 `leased_bytes` 精确归还）：waiter join 前 reserve 失败返回 None（无引用无占用）、`__aexit__` 正常释放、factory 普通异常（FetchFailed 传播+全 waiter 释放+不进 grace+**预占立即返还**）、**waiter 自身取消于"引用登记后、future resolve 前"窗口**（registry await 分支恰好释放一次、shared future 不被取消、leader 后续成功其余 waiter 照常取 body、最终清零）、leader 取消（取消 shared future、存活 waiter 释放旧引用后重领飞、旧引用先释放再登记新引用、**预算立即返还使重领飞在单 flight 预算下成功**）、grace 到期（ownership 释放）、预算淘汰、`shutdown()`（含与 waiter 取消/leader 失败并发时恰好释放一次，**含 shutdown 中间点与收敛全程断言（v1.8）**：①factory 阻塞时执行 shutdown → 立即断言 snapshot 出现 `layer=retired, ownership_state=in-flight`（detached）且 `leased_bytes` 仍等于该 flight 预扣；②放行 factory 成功 → waiter 仍取得 body、entry 转 retained、不进 grace 不设 timer；③waiter 持 Lease 期间 `leased_bytes` 仍在账 → 全部 caller 引用释放后才归零；④分流变体：放行 factory 失败 → entry 转 failed、立即退款、`leased_bytes` 归零；**⑤detached-leader-cancel 分流（v1.8.1 rev-9 cond 2）**：shutdown 后 detached 态 leader 被 cancel → entry 转 failed、恰好一次退款、`leased_bytes` 归零、残留 waiter 引用为纯计数（锁定 exactly-once 退款），rev-7/8 blocking）、**ownership 原位转换专项**（成功完成不新增第二份 registry 引用，snapshot 前后对比）。
- `A2-C6`（v1.6 立、v1.7 对齐两层结构——generation 绑定交错测试）：**双 waiter 交错释放/重领飞确定性测试**——leader 取消后 waiter A 率先重领飞建立新 entry（seq=N+1，active 层）、waiter B 随后才释放旧引用（seq=N，retired 层 failed 态）：断言释放句柄直接绑定旧 `_Entry` 对象（不重查 active lookup）、只作用于旧 entry 计数（新 entry 计数不受影响）、新旧并存期间统一视图 snapshot 两 entry 分列（layer=active/retired 标注）、旧 entry 归零后从 snapshot 消失、最终新 flight 正常完成且 ledger 归零。
- `A2-C5`（v1.2）：慢投影专项——人为慢 offload 夹具下"N caller 一次 GET"仍成立（join-first 构造性；预算可用时）。

### Task 1.3 (A3): sessions 列表 + sessions/status single-flight

**Files:** Modify `src/oc_slimapi/routes/sessions.py`；Create `tests/test_sessions_coalesce.py`

**设计勘误（回应 rev-1 condition 1）**：`GET /slimapi/sessions` 现行为 = 上游列表 GET + `skeleton_session()` 投影（**无 TurnRegistry merge**——turn merge 在 `GET /slimapi/sessions/status`，`sessions.py:119` 起）。因此：
- `/slimapi/sessions`：key = `("sessions-list", id(upstream), directory, canonical_query)`，共享单元 = 上游 GET + cap-read，投影（含 `X-Complete` 计算）per caller。无 TTL（新鲜度语义不变：轮询间隔内全部 join 同一 in-flight 抓取，完成后新抓——不存在"跨轮询周期的陈旧缓存"）。
- `/slimapi/sessions/status`（1,812 reqs/35% 可合并）：共享单元 = 上游 `GET /session/status` 原始 body（key 含 directory）；**TurnRegistry turn-merge 在 per-caller 段执行**（turn 状态随时间变化，禁止与共享 body 同刻化——每个 caller 拿共享 body 后各自读当前 turn 注册表合并）。

**Acceptance Criteria:**
- `A3-C1`: 并发 20 请求 `/slimapi/sessions`（模拟 3 设备 burst；**预算可用、未触发降级时**）→ 上游恰 1 次 GET；投影与 `X-Complete` 逐 caller 正确。
- `A3-C2`: `/slimapi/sessions/status` 上游 body 共享 1 次（**同上限限定**）、turn merge per-caller（测试断言：leader 完成后 turn registry 状态变化，后 join 的 caller 能看到新 turn 值）。
- `A3-C3`: 跨 app 不合并；错误传播一致；`coalesce_enabled=false` 旁路回归；既有 tests 全绿。

### Task 1.4 (A4): questions / permissions 聚合 single-flight

**Files:** Modify `src/oc_slimapi/routes/questions.py`、`src/oc_slimapi/routes/permissions.py`（共用 discovery 模式同构处理）；Create `tests/test_questions_coalesce.py`

**设计：** 按 3.x 约定，两级去重：discovery 调用（`GET /experimental/session?...`）single-flight（key 固定 + scope）；per-dir `GET /question` / `GET /permission` single-flight（key = ("question-dir"/"permission-dir", id(upstream), dir)）。envelope 聚合 per caller（纯内存拼接，无需池 admission——与两路由现行行为一致，不新增也不移除 admission）。

**Acceptance Criteria:**
- `A4-C1`: 并发 3 请求 `/slimapi/questions`（**预算可用、未触发降级时**）→ discovery 上游恰 1 次、每 dir `/question` 恰 1 次（v1.4 注：A4 多 dir fan-out 在默认 `raw_fetch_max_bytes` 下可能仅部分 dir 走 coalesce、其余降级直取——断言限定预算可用；预算耗尽压力场景改按 A2-C4①式断言"响应正确 + ledger 有界"）；envelope 聚合结果正确（与逐请求直取结果一致）。
- `A4-C2`: `/slimapi/permissions` 同构回归；既有 questions/permissions tests 全绿；`./scripts/check.sh` ✅。

### Batch 1 配套

- CHANGELOG `[Unreleased]`：内部性能项 + 两点行为披露——①catalog TTL 新鲜度（响应可能滞后至多 TTL 秒；`catalog_cache_ttl_seconds=0` 关闭）；②join-first 语义（上游 hang 时同 key caller 共同等待 httpx 超时而非部分快速 503；正常路径响应字节不变）。
- `docs/operations.md` knob 表加 7 项：`catalog_cache_ttl_seconds` / `catalog_cache_max_entries` / `catalog_cache_max_bytes` / `catalog_cache_max_entry_bytes` + `coalesce_enabled` + `raw_fetch_concurrency` + `raw_fetch_max_bytes`（含默认容量退化说明与调优指引：预算 ≥ 期望并行 flight 数 × max_response_bytes）。
- **不改** v2-contract 端点行为（响应字节逻辑不变——TTL 缓存返回的是历史成功响应原文，single-flight 返回的是共享成功响应）。

---

## 4. Batch 2 — B1：ETag / 304 条件请求（加性 wire）

> 回应 rev-1 blocking 1 + condition 2：ETag 基于**最终选定表示**，gzip/identity 经 `Vary: Accept-Encoding` 区分协商而非共用验证器语义；表示版本入 hash 杜绝投影/配置/B3 字段演进导致的误 304。

**Files:** Create `src/oc_slimapi/etag.py`、`tests/test_etag.py`；Modify `routes/messages.py`、`routes/sessions.py`、`routes/agent.py`、`routes/command.py`、`config.py`（`etag_enabled` 默认 true，回退开关）；契约/文档见 §7。

**设计：**
- **验证器算法（v1.3 统一为唯一规范文本，回应 rev-3 condition 1——此前"per-coding 强验证器 / gzip 强 ETag / hash(identity body)"三种表述作废，实现与文档一律以本条为算法源）**：
  - identity 表示：**强 ETag** `"<sha256hex(REP_VERSION + b"\\0" + b"identity" + b"\\0" + identity_body_bytes)>"`（全量 hex 不截断；`identity_body_bytes` = 投影序列化未压缩字节）。
  - gzip 表示：**弱 ETag** `W/"<sha256hex(REP_VERSION + b"\\0" + b"gzip" + b"\\0" + identity_body_bytes)>"`（canonical 输入**恒为 identity 字节** + coding_id；弱标记表达"语义等价"而非"字节相同"，规避 gzip 压缩字节跨压缩器版本/级别不稳定）。
  - 两 coding 的 opaque tag 必然不同（coding_id 入 hash）；跨 coding 复用验证器 → 不匹配 → 保守 200。`Vary: Accept-Encoding` 保留（表示选择指导），不作为验证器共享依据。RFC 9110 GET `If-None-Match` 弱比较（忽略 weakness 标志、按 opaque tag 比较）。
- **REP_VERSION** = skeleton 投影版本常量 + 影响表示的相关 config 指纹（含 B3 指纹开关）+ ETag 方案版本。任何投影代码/配置/B3 字段变化 → REP_VERSION 变 → ETag 全变 → 最坏一轮 200 重取，**构造上不可能误 304**。merged 模式 full 内容变化必然反映在 final body → ETag 变化（blocking 1 的 `/full` 依赖场景闭合）。
- **管线照常执行**：ETag 不短路抓取/投影（上游成本由 A 批摊薄）；命中时省的是**下行传输体**（identity：省序列化后传输；gzip：canonical hash 后若命中则零压缩+零传输）。序列化本身仍发生——诚实计入设计；orjson 序列化为 ms 级。
- **304 响应头集合**：`ETag`（同值）+ `Vary`（见下，与 200 完全一致）+ `Cache-Control: no-store`（维持现状）+ **路由辅助头复制**：messages 的 `X-Next-Cursor`、sessions 的 `X-Complete` 在 304 中照发（值来自本次管线计算——上游 GET 照常发生，Link 头/投影计数可用；分页/完整性语义不因 304 丢失）。304 无 body。
- **Vary 合并**：目录相关路由（messages/sessions）`Vary: Accept-Encoding, X-Opencode-Directory`；catalog（agent/command）维持 `Vary: Accept-Encoding`（directory 影响 catalog 上游资源，`agent.py`/`command.py` 现行为已按 directory 路由——实施时核实：若 directory 影响 agent 上游响应则同样追加，以实测为准并在 INTERFACE_MAP 标注）。**追加不覆盖**：在 `gzip_util`/`_catalog_common` 既有 `Vary: Accept-Encoding` 基础上合并，禁止整值替换。
- **匹配规则**：RFC 9110 `If-None-Match` 列表匹配（弱比较：entity-tag opacity 级字符串比较）+ `*`。只对 GET 生效；HEAD 维持现状透传。错误响应（4xx/5xx）不带 ETag、不参与 304。
- **`Cache-Control: no-store` 与验证器保留的关系**：`no-store` 约束 HTTP 缓存不得存储响应；ocdroid 轮询器在**应用内存**中保留上次 ETag 并主动发 `If-None-Match`，属客户端逻辑而非 HTTP 缓存行为，不违反 `no-store`。此说明写入 CLIENT_CHANGES（OkHttp HTTP cache 不得用于这些路由——`no-store` 本就使其跳过）。**验证器与 coding 绑定提醒**：客户端应固定 `Accept-Encoding: gzip`（或固定 identity）以保证轮询间 validator 可比；coding 切换后首轮必然 200（保守正确）。
- `etag_enabled=false`：不输出 ETag、不判 304（完全回退，逐字节等价今天）。
- 与 A1 复合：catalog 命中缓存的 raw body 投影后的 final body 即 ETag 输入；304 时零 gzip 成本。

**Acceptance Criteria:**
- `B1-C1`: 首次 GET 200 带 `ETag`（全量 hex；identity=强 ETag，gzip=`W/` 弱 ETag）；同值 `If-None-Match` → 304 无 body、头集合完备（ETag/Vary/Cache-Control/路由辅助头）；`If-None-Match: *` → 304；不匹配 → 200 新 ETag。
- `B1-C2`: 上游内容变化 → ETag 变化 → 旧 If-None-Match 收 200。**merged 模式专项**：列表 body 不变、`/full` 明细变化 → final body 变 → 新 ETag → 旧验证器收 200（无陈旧 304）。
- `B1-C3`: 表示版本专项：测试内 bump REP_VERSION（monkeypatch 常量）→ 同 body 同 query 的 ETag 改变（旧验证器收 200）——锁定"投影演进不误 304"。
- `B1-C4`（v1.2 断言修正）：directory 语义隔离——同 path 不同 directory 的请求**经 `Vary: X-Opencode-Directory` 区分**；不同 directory 得到的最终表示逐字节相同时**允许相同 ETag**（合法：validator 标识表示而非来源）；**必须断言的是**：①directory A 的响应不会被 directory B 的 If-None-Match 误命中（用 A/B 内容不同的夹具：A 的 ETag 发到 B 请求 → 200 而非 304）；②`Vary` 值为合并列表（Accept-Encoding 在前），gzip/identity 两态请求均覆盖；③identity 与 gzip 的 ETag 必不相同（per-coding 验证器回归）。
- `B1-C5`（v1.2）：跨 coding 交叉——gzip 响应的 ETag 用于 identity 请求的 If-None-Match → 200（保守正确，非误命中）；反向同理。
- `B1-C6`: `etag_enabled=false` → 行为与今天逐字节一致；304 计入 access log（status=304，downOut≈0）。
- `B1-C7`: 4 路由各覆盖 happy/miss/disable/辅助头复制/双 coding 五态；`./scripts/check.sh` ✅。

**契约配套（本批内完成）：** v2-contract §2 各端点行加性标注 `ETag`/`304`/`Vary` 合并语义；CLIENT_CHANGES「ETag 接入（可选，推荐）」含验证器内存保留说明；CHANGELOG 加性条目（未 bump）。

---

## 5. Batch 3 — C2a：T17 todo / children thin 路由（加性 wire）

**Files:** Create `src/oc_slimapi/routes/todo.py`、`src/oc_slimapi/routes/children.py`、`tests/test_todo_routes.py`、`tests/test_children_routes.py`；Modify `src/oc_slimapi/app.py`（router 注册）、`docs/specs/INTERFACE_MAP.md`（2 行）

**设计权威：** `docs/specs/traffic-route-todo-2026-08-10.md` + `docs/specs/traffic-route-children-2026-08-10.md`（T17 设计稿，已获批）。实施前 fixer 必读两稿 + 按其 §1 锚点复核上游 v1.18.16 源码（AGENTS.md 硬规则：先读上游再写实现）。

**要点（两稿共性）：**
- 复用 `_catalog_common.read_upstream_response/busy_response/raise_upstream_unavailable` + 全局 `max_response_bytes` cap + admission 先于上游 GET（新路由与 catalog 同构，不接 A 批 registry——匿名迁移后量级小，YAGNI；ETag/条件 GET 同为 follow-up，见 §12.2）。
- gzip 协商（小 body 阈值行为对齐 catalog 实现）；错误映射对齐 sessions/messages（sid 感知 404→`session_not_found`）。
- todo：投影近恒等（Todo.Info 3 字段已最小）；children：按 T17 children 稿白名单。
- INTERFACE_MAP 2 行（check_routes_doc 门必过）；CHANGELOG 加性条目。

**Acceptance Criteria:**
- `C2a-C1`: 两路由 happy path（fake upstream 数组 → 200 投影 body）；gzip 协商三态。
- `C2a-C2`: 错误映射：上游 4xx→502 `upstream_http_N`（sid 路由 404→`session_not_found`）；5xx/网络/坏 JSON/非 list→503 `upstream_unavailable`；cap→413 `response_too_large`；池满→503 `transform_busy`+`Retry-After:2`。
- `C2a-C3`: `directory` 可选 query 校验 + `X-Opencode-Directory` 转发（`validate_directory` 400 路径）。
- `C2a-C4`: `./scripts/check.sh` ✅（路由↔文档门）；旧客户端 fallback 语义（404 `thin_route_not_found`）由既有 catch-all 测试模式覆盖。

---

## 6. Batch 4 — B3：消息内容指纹（第2类单边可冻结子集）

> 回应 rev-1 blocking 4：放弃 provisional-wire-then-drift 路径。选定**方案甲（无状态内容指纹）**，其语义可单边冻结且在 SHA-256 碰撞概率可忽略的工程模型下无假阴性；方案乙（有状态 revision）否决（漏 digest 事件 → revision 不动 → 假阴性，incarnation 只治重启不治漏事件；且引入进程状态 + 冷启动窗口）。客户端行为语义（token idle/resync、SSE 开关 reconcile 三分法、watermark 推进规则）**不在本批**，仍走联合计划联调冻结（外部 lane 项 4）。

### Task 4.1 (设计定稿): 指纹形状与语义冻结

**Files:** Create `docs/specs/design-message-watermark.md`

**必做勘察：**
1. 读上游 v1.18.16 `packages/opencode/src/session/message-v2.ts` + `packages/schema/src/v1/session.ts`（消息/part 的 time 字段、同秒追加可达性、现有 `updatedAt` 语义）。
2. 读联合计划（ocdroid 仓 `docs/ocmar/plans/2026-07-26-slim-message-reliability-joint-plan.md` §4.3/§4.4/§7）与 ICD 开放问题清单（since 覆盖范围、since vs full merge 优先级、同 tuple 内容变化）——确认指纹如何作为**补充证据**嵌入既有 `(updatedAt, messageId)` 双水印框架（联合计划 §3：tuple 仍是基线，指纹是内容变化判定的强化，不替代水印推进规则）。
3. 设计定稿（形状冻结，写入设计稿 + v2-contract 终态语义节）：
   - 字段：消息 skeleton 加性 `contentFingerprint: string`，格式 `"<vN>:<sha256hex>"`（版本前缀 + 全量 hex）。`vN` = **独立指纹规范版本常量**（`FINGERPRINT_VERSION`），bump 条件 = **指纹输入的规范化规则变化**（增删参与字段、序列化规则改变）；**不与包版本/REP_VERSION 绑定**——包发布本身不 bump vN（回应 rev-2 condition 3）。
   - **输入与生成位置（v1.2，回应 rev-2 blocking 3——指纹是最终消息内容的函数，非 skeleton 期产物）**：输入 = 该消息**最终对外表示内容**（投影保留字段 + 最终 parts 集合）。生成点 = **消息最终组装点**：非 merged 列表 = `skeleton_message()` 投影完成时；`mode=merged` = **full parts splice 完成后重算**（`messages.py` splice 站点对每条被 splice 的消息调用 `recompute_fingerprint(msg)` 覆盖 skeleton 期指纹）。由此冻结语义对 merged/非 merged 一致成立：full 明细变化（parts 内容变）→ 最终内容变 → 指纹变；skeleton 字段变 → 指纹变。**二选一定案：选"splice 后重算"**（语义完整性优先；重算成本 = per-message 一次 hash，merged 本就是重路径，可接受）。
   - **规范化规则（在设计稿冻结，实现遵守）**：①**排除 `contentFingerprint` 字段自身**（防自引用）；②canonical 序列化 = orjson `sort_keys=True` + 确定性 parts 排序（parts 按上游顺序，不重排——上游序即语义序）；③数值/字符串原样参与（不做数值规范化——投影产物无浮点歧义）；④规则全文 + 示例向量（固定输入→固定指纹的 golden vector）写入设计稿，测试锁定。
   - **语义（终态，发版即冻结；v1.3 密码学严谨化，回应 rev-3 condition 4）**：同 `vN` 下，**相同规范化输入必得相同指纹**（确定性构造保证）；**指纹不同必然表示规范化输入不同**；**相同指纹仅以 SHA-256 碰撞概率可忽略为前提**（2^-256 量级工程保证，非数学双射）指示内容相同。**不提供**单调性/时序语义（设计选择：无状态使指纹不依赖事件观测，在碰撞可忽略工程模型下不产生观测型假阴性；"没有 revision 排序"以显式放弃换正确性）。客户端消费 = 字符串不等即重拉（B2 digest 驱动的判变证据）。
   - **跨表示模式不可比较（v1.3，回应 rev-3 condition 2）**：`contentFingerprint` 的比较命名空间 = **单一表示模式**。默认 skeleton 列表与 `mode=merged` 的最终 parts 表示不同（skeleton parts vs full parts），同一上游消息在两模式下通常得到**不同指纹**——这是"指纹 = 最终表示的函数"的直接推论。**客户端不得跨模式比较指纹**（默认列表指纹 vs merged 历史页指纹不构成"内容变化"信号）。此限制写入 v2-contract 字段语义 + 设计稿 + CLIENT_CHANGES；实现侧 vN 前缀不区分模式（模式属请求参数而非指纹规范），以契约文字约束比较范围。
   - 乱序/重复 digest、重启、多设备：指纹是内容的纯函数——无状态可穿越重启（重启后同内容同指纹，测试锁定）；不依赖事件观测，漏 digest 不产生假"未变化"。
4. 输出：字段 spec、规范化规则（含 golden vector）、与双水印/digest 的关系声明、方案乙否决记录（含 blocking 4 的假阴性论证）、开放问题清单（仅客户端消费侧，联调项）。

**Acceptance Criteria:**
- `B3-C1`: 设计稿含上游源码锚点（file:line）、方案甲/乙对比表 + 乙否决论证、字段级 spec（上述语义逐条）、与既有 digest turn/watermark 框架的关系声明。
- `B3-C2`: 设计稿 rev-gpt 审 ≥9.5（并入 Batch 4 门控）。

### Task 4.2 (实现): skeleton 加性字段 + merged splice 重算

**Files:** Modify `src/oc_slimapi/skeleton.py`（投影 + 指纹函数 `compute_message_fingerprint` / `recompute_fingerprint`）、`src/oc_slimapi/routes/messages.py`（merged splice 站点对被 splice 消息重算指纹）、`config.py`（`message_fingerprint_enabled` 默认 true，ops 回退开关）；Create `tests/test_message_fingerprint.py`

**定案（消除 rev-1 condition 4 的二选一歧义）：默认开启**（加性字段；JSON 解析器对未知字段的标准行为是忽略——ocdroid 现行 moshi/kotlinx 解析均如此，且 v1.3/v1.4 已有 `turnIncarnation` 等加性字段先例，生产未破坏）。`message_fingerprint_enabled=false` 时字段缺省（响应与今天逐字节一致），REP_VERSION 同步纳入该开关状态（与 B1 联动正确）。**旧客户端兼容证据升级为发版门禁（v1.2，回应 rev-2 condition 2）**：§9 外部 lane 项 6 的「ocdroid 0.24.x / 0.25.x × 新 sidecar」冒烟（未知字段忽略 + 304 未接入时 200 路径）为**发版前置条件**——终审通过但 smoke 未回收前不执行 release.sh；smoke 证据记录（对方会话确认或联调记录）入终审 ocmar 报告。

**Acceptance Criteria:**
- `B3-C3`: 同一上游响应重算指纹确定性一致；上游内容变化（part 增删改、文本变化）→ 指纹变化；未变化 → 不变。**merged 专项（v1.2）**：`mode=merged` 下 full parts splice 后指纹反映 splice 后内容——full 明细变化（列表 body 不变）→ merged 响应指纹变化（非 merged 路径不受影响）。**merged degraded 专项（v1.3，回应 rev-3 condition 3）**：full 获取失败 / 预算不足 / 坏 JSON / 非 dict / `parts` 非 list 五类降级路径**不执行重算**——消息保留 skeleton 期指纹（最终对外表示即原 skeleton，指纹与其一致；测试逐类断言重算未被调用且指纹不变）。**跨"重启"确定性**（新建 app 实例同输入同指纹）与 **digest 事件无关性**（不发 digest / 乱序 digest / 重复 digest 三态下指纹不变）回归锁定。**golden vector**：设计稿示例向量进测试（固定输入→固定指纹）。**跨模式差异锁定**：同消息默认模式指纹 ≠ merged 模式指纹（表示不同即不同，契约"不可跨模式比较"的回归锚点）。
- `B3-C4`: 加性兼容——现有全部 skeleton/messages 测试不改断言仍绿；新字段存在性 + 格式断言（`vN:hex64`）新增；规范化规则专项（指纹输入排除 `contentFingerprint` 自身——构造含指纹的消息重算不变）。
- `B3-C5`: `./scripts/check.sh` ✅；`message_fingerprint_enabled=false` 逐字节回归。

### Task 4.3 (契约配套)

- v2-contract：`§消息列表` 加性节——字段、终态语义（§6 Task 4.1 定稿文本：确定性/差异指示/SHA-256 碰撞可忽略前提三项表述 + 跨表示模式不可比较限制）、"不提供单调性"显式声明。**无 provisional 字样**（语义已冻结；客户端何时消费属 CLIENT_CHANGES 范畴）。
- CLIENT_CHANGES：「内容指纹消费（建议随 B2 digest 驱动一起接入）」+ 联合计划联调项指引（token idle/resync、reconcile 三分法——这些客户端行为语义仍待联调，不在本仓冻结范围）。
- CHANGELOG 加性条目。

---

## 7. 文档与契约配套汇总（各批内完成，勿拖到终审）

| 文档 | Batch 1 | Batch 2 | Batch 3 | Batch 4 |
|---|---|---|---|---|
| `CHANGELOG.md` [Unreleased] | ✅（内部+TTL/join-first 披露） | ✅ 加性 | ✅ 加性 | ✅ 加性 |
| `docs/specs/v2-contract.md` | — | ✅ 加性（ETag/304/Vary） | ✅ 加性（2 端点行） | ✅ 加性（终态语义） |
| `docs/specs/INTERFACE_MAP.md` | — | ✅（4 路由行为列补 ETag） | ✅（2 新行，**路由门强制**） | — |
| `docs/specs/CLIENT_CHANGES.md` | — | ✅（If-None-Match 接入+验证器内存保留） | ✅（todo/children 迁移） | ✅（指纹消费+联调指引） |
| `docs/operations.md` | ✅（7 knob：catalog 4 + coalesce + raw_fetch_concurrency + raw_fetch_max_bytes） | ✅（etag_enabled） | — | ✅（fingerprint 开关） |
| `docs/manual/traffic-accounting.md` | ✅（cache 字段一句） | — | — | — |

## 8. 门控与执行流程（用户指令 2026-08-16）

1. **方案门**：本计划 rev-sgpt ≥9.5（不满足则修订重审——**方案文档任何修改使既有评分作废，须 review_prep 新 generation 后复审**；不满足不进实施）。
2. **实施**：Batch 1→2→3→4 严格串行；每批 = fixer TDD 实现 → `./scripts/check.sh` 全绿 → **rev-gpt ≥9.5 门**（审该批全 diff vs 前批基线；不满足则修复重审，评审新 generation）→ 通过才进下一批。
3. **终审**：全部批次通过后，rev-sgpt 对全量 diff（vs `76fc13b`）整体审阅 ≥9.5。
4. **发版**：终审通过 → **ocdroid 旧版 smoke 证据回收**（§9 项 6：0.24.x/0.25.x × 新 sidecar 未知字段忽略 + 200 路径冒烟，v1.2 起为发版前置条件）→ `./scripts/release.sh minor`（全部加性/内部 → minor）。发版前 CHANGELOG [Unreleased] 必须齐备。
5. 评审 binding：每轮评审前编排者 `review_prep` 一次，同轮 reviewer 共用同一 rid（面板同源）；rev-sgpt/rev-gpt 为可信 reviewer（bash 兜底），git_ro 为主通道。
6. ocdroid 外部 lane 通知在对方当前任务完成后发出（§9）；其进度不阻塞本仓 Batch 1-4（单边授权——B3 已收敛为单边可冻结子集，无联调依赖留在发版关键路径上）。

## 9. 外部 lane（ocdroid 侧配合项，通知 ses_ffadc95a0ffebbDxadBiZalBZ6）

对方流程同构：整理方案 → 门控（其评审链自定，建议同 9.5 标准）→ 分阶段实施 → 审阅。配合项清单：

1. **B2 digest 驱动拉取**（结构性省流核心）：digest SSE 已带 `turnIncarnation`/`turn`，客户端改为「digest 变化才拉 `/slimapi/messages/{sid}`」，替代 3.5s 固定轮询；保留 bounded 兜底轮询。Batch 4 上线后可用 `contentFingerprint` 作判变证据（指纹不同才重拉；**仅在同一表示模式内比较**——默认 skeleton 与 `mode=merged` 的指纹属独立命名空间，跨模式比较无意义，见契约字段语义）。
2. **C1 sessions 列表降频**：2s → 15-30s 兜底 + digest 驱动刷新（当前占全天 47% 请求）。
3. **B1 客户端接入（可选，推荐）**：轮询 GET 带 `If-None-Match`，处理 304（省 downOut）。验证器由轮询器在应用内存保留（这些路由 `Cache-Control: no-store`，禁 HTTP 缓存存储；OkHttp cache 不适用）。
4. **B3 客户端消费（联调门控）**：指纹判变消费可随 B2 接入；token idle/resync 清态、SSE 开/关 reconcile 三分法、watermark 推进规则——按联合计划 B 阶段流程联调冻结后方可进生产路径。
5. **C2 匿名消费方**：若匿名流量（todo/children/diff/status/question 轮询）属 ocdroid Standard/legacy 模式或其知晓的工具，请归属方迁移至 `/slimapi/**` 等价路由（todo/children 路由本计划 Batch 3 交付）；否则待用户排查（本仓 §12 开放问题）。
6. **旧客户端兼容性证据（回应 rev-1 detail 5，v1.2 升级为发版门禁）**：请对方在联调中补「ocdroid 0.24.x / 0.25.x × 新 sidecar」冒烟（重点：未知 JSON 字段忽略、304 处理未接入时的普通 200 路径）——服务端"既有 tests 全绿 + 加性 diff"只能证明服务端回归，旧客户端零破坏需对方侧证据闭环（先例：v1.3/v1.4 加性字段未引发破坏，风险低但需显式验证）。**本仓在 smoke 证据回收前不执行 release.sh（§8.4）。**

依赖声明：1/2/3 不依赖本仓新 wire（digest 字段已在 v1.4.0；ETag 待 Batch 2 上线后生效）；4 的指纹消费依赖 Batch 4 上线，其余联调项无新 wire 依赖。

## 10. 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| A1 catalog TTL 陈旧（配置变更后最长 300s 滞后） | CHANGELOG/operations 披露；TTL 可调 | `catalog_cache_ttl_seconds=0` |
| **A1 缓存内存增长**（rev-1 blocking 3） | 总字节预算 + 单条上限旁路 + 最旧优先即时淘汰 + `validate()` 聚合校验；A1-C4 回归锁定 | 同上（0 即全关） |
| **A 批 registry 跨 app 串数据**（rev-1 blocking 2） | registry 挂 `app.state` per-app + key 嵌 `id(upstream)` + lifespan `shutdown()`；A2-C2/A3-C3 跨 app 回归 | `coalesce_enabled=false` |
| **A 批 join-first 上游 hang 行为变化**（同 key caller 共同等待超时而非部分快速 503） | key 数少（个位数 distinct）；httpx 超时仍兜底；正常路径响应字节不变；CHANGELOG 披露 | `coalesce_enabled=false`（旁路=现行直取路径，受测分支） |
| **join-first 突破 T3 聚合内存上界**（rev-2/rev-3 blocking：不同 key 在 admission 前并发缓冲 raw body，且驻留超出 GET 生命周期） | 引用计数 flight lease + `raw_fetch_max_bytes` 驻留字节预算（持有期覆盖 GET→等待 admission→caller 消费完成；预算满降级现行直取路径）；`raw_fetch_concurrency` 限网络读取并发；validate() 校验 raw + transform 组合上界；A2-C4 生命周期回归 | `coalesce_enabled=false`；调低 `raw_fetch_max_bytes` |
| **慢投影 vs grace**（rev-1 blocking 2 残余） | join-first 使"一次 GET"不依赖 grace；grace 仅覆盖完成后 straggler（1.0s 与 CD-1 同值）；A2-C1 构造性断言 | — |
| **single-flight 错误传播放大**（一次上游 5xx 波及合并请求） | 语义与 CD-1 一致（本就共享错误）；绝不 negative-cache，下一请求即重试 | `coalesce_enabled=false` |
| **ETag 误命中（陈旧 304）**（rev-1 blocking 1） | 验证器算法 = §4 唯一规范文本（REP_VERSION + coding_id + identity 字节；gzip 为 `W/` 弱 ETag）：merged full 变化/投影演进/配置变化/B3 开关/coding 切换全部改变 opaque tag 或保守 200，构造性杜绝；B1-C2/C3/C5 回归锁定 | `etag_enabled=false` |
| **304 丢分页/完整性头** | `X-Next-Cursor`/`X-Complete` 在 304 照发（本次管线照常计算）；B1-C1 断言 | 同上 |
| **Vary 覆盖既有值** | 合并列追加（Accept-Encoding 恒在），禁止整值替换；B1-C4 断言 | 同上 |
| **B3 指纹语义与后续联合结论冲突** | 指纹语义 = 最终消息内容（含 merged splice 后）的纯函数（终态冻结）；客户端**行为**语义留联调，不在本仓冻结范围——冲突面已收敛到"消费方式"，字段本身无演进压力；若联调弃用，字段保持无人消费即零影响 | `message_fingerprint_enabled=false` |
| **B3 merged 指纹遗漏**（rev-2 blocking 3：splice 后内容变而指纹不变） | 指纹在 splice 完成后重算（B3-C3 merged 专项回归锁定）；vN 版本化保证规则变化可检测 | 同上 |
| todo 小 body gzip 净负 | 按 catalog 阈值行为；T17 稿开放问题 #3 | 实现时确认阈值 |
| 与 ocdroid 并行开发的契约冲突 | 全部加性 rev + CHANGELOG 记账；对方以本仓契约为准 | — |

## 11. 验证指标（发版后次日用 access log 复测）

- upIn 总量（基线 1,631M/天）→ 目标 < 1,000M（A 批全部生效）。
- `/slimapi/agent`+`/command` upIn（基线 503.6M）→ 目标 < 150M。
- `/slimapi/messages` upIn（基线 1,020.8M）→ 目标 < 800M（29% 合并上限 ~300M）。
- `/slimapi/sessions` 上游 GET 次数（基线 18,926）→ 目标 < 8,000。
- 304 占比（messages/sessions/catalog 轮询）→ 报告实测命中率。
- access log `cache` 字段 hit/miss 统计（A1）。
- 匿名 `/session/{id}/todo`+`/children`（基线 2,054 reqs）→ 观察迁移（属外部，只观测不负责）。

## 12. 开放问题

1. **匿名消费方身份**（C4 未批准排查）：todo/children 路由就绪后迁移责任方待用户指认；本计划只交付路由。
2. **todo/children 的 ETag 与 A 批 coalesce**：本批不接（匿名迁移后量级小，YAGNI）；若迁移后频率仍是主成本，另行立项。
3. **指纹输入的规范化规则细节**（字段遍历稳定性、parts 序、数值规范化）：Task 4.1 设计稿定稿；原则=确定性 + orjson `sort_keys` 级稳定。
4. **agent/command 的 directory 是否影响上游响应**：Batch 2 实施时实测核实，决定 Vary 是否追加 `X-Opencode-Directory`（以实测为准并同步 INTERFACE_MAP）。

## 13. 回归矩阵（rev-1 condition 4 汇总，分散到各批 AC 执行）

| 类别 | 用例 | 落点 |
|---|---|---|
| A 批隔离 | 跨 app/upstream 同 key 不合并 | A2-C2 / A3-C3 |
| A 批生命周期 | lifespan teardown → `shutdown()`（无残留 timer/entries；registry 可复用语义与 CD-1 一致） | Batch 1 新增公共测试（3.x 约定） |
| A 批领导取消 | leader 取消 → 存活 waiter 重新领飞 | A2-C3 |
| A 批慢投影 | 慢 offload 下"N caller 一次 GET"仍成立（join-first 构造性） | A2-C5 |
| A 批 raw-fetch 生命周期 | 不同 key 驻留 raw body ≤ `raw_fetch_max_bytes`（含 admission 排队窗口）；超额降级直取；在飞 GET ≤ `raw_fetch_concurrency`；组合上界 validate()（v1.3） | A2-C4 |
| A1 字节预算 | 单条超限旁路 / 总预算最旧淘汰 / 边界矩阵 + 淘汰-并发刷新一致性（v1.2） | A1-C4 / A1-C5 |
| B1 表示变化 | merged full 单独变化 → 新 ETag；REP_VERSION bump → 新 ETag；gzip/identity Vary 合并；**identity/gzip ETag 必不同 + 跨 coding 交叉保守 200（v1.2）** | B1-C2/C3/C4/C5 |
| B1 304 完备性 | 辅助头（X-Next-Cursor/X-Complete）照发；禁用开关逐字节回退 | B1-C1/C6 |
| B3 无状态 | 跨重启同指纹；digest 漏发/乱序/重复下指纹不变；golden vector | B3-C3 |
| B3 merged | full splice 后指纹变化（列表 body 不变场景） | B3-C3 merged 专项 |
| 全局 | 版本恒 2；契约 diff 仅加性；旧客户端零破坏（服务端侧=既有 tests 不改断言全绿 + 加性 diff 审查；客户端侧=外部 lane 项 6，发版门禁） | 各批 + §9.6 |

## Criterion Ownership Matrix

| Criterion | Owner | Verification | Final-only? |
|---|---|---|---|
| A1-C1..C6 | Batch 1 Task 1.1 | pytest + check.sh | N |
| A2-C1..C6 | Batch 1 Task 1.2 | pytest + check.sh | N |
| A3-C1..C3 | Batch 1 Task 1.3 | pytest + check.sh | N |
| A4-C1..C2 | Batch 1 Task 1.4 | pytest + check.sh | N |
| B1-C1..C7 | Batch 2 | pytest + check.sh | N |
| C2a-C1..C4 | Batch 3 | pytest + check.sh + 路由门 | N |
| B3-C1..C5 | Batch 4 | 设计稿评审 + pytest + check.sh | N |
| 全局：X-Slimapi-Version==2 | 全批 | versioning 测试 + 契约审查 | Y |
| 全局：旧客户端零破坏 | 全批 | 既有 tests 全绿（不删不改既有断言）+ 加性 diff 审查 + §9.6 外部证据（发版门禁） | Y |
| 全局：契约 diff 仅加性 | 全批 | rev-gpt 各批 grep 校验（无既有行删除，除版本历史） | Y |
| 终审 + 发版 | §8 | rev-sgpt ≥9.5 + smoke 证据回收 + release.sh minor | Y |

## §13 实施记录（2026-08-16，随实施追加）

> 本节为实施侧事实记录（设计正文 §1-§12 冻结于 v1.8 定稿）。工作区未提交；测试基线随批演进：1486（Batch 1 前）→ 1570 → 1617（Batch 2）→ 1653（Batch 3）→ 1689（Batch 4）→ 1694（终审修复后）。

### Batch 1（A：上游去重/缓存，内部零 wire 变更）— 门控 9.8 PASS（rev-1 FAIL 6.5 → 修复 → rev-2 PASS；另有两轮定点修复）

- Task 1.1 catalog TTL 缓存：新建 `src/oc_slimapi/catalog_cache.py` + `tests/test_catalog_cache.py`；`config.py` 4 个 `catalog_cache_*` knob；`routes/agent.py`/`routes/command.py` 接缓存；`access_log.py` 可选 `cache` 字段。
- Task 1.2 `LeasedSingleFlight`：新建 `src/oc_slimapi/leased_singleflight.py`（§3.x 协议全量：两层 registry/取消三分支/预算双规则/shutdown 原子转换/ledger 不变量）+ `tests/test_leased_singleflight.py`；messages 列表 join-first；`config.py` `coalesce_enabled`/`raw_fetch_concurrency`/`raw_fetch_max_bytes`。
- Task 1.3 sessions 列表 + status 共享 body、turn merge per-caller。
- Task 1.4 questions/permissions 两级去重（discovery + per-dir）。
- 组件批 rev-gpt 修复要点：sessions lease 路径补 TransformPool admission（TransformBusy→503 与旧路径等价）；A2-C6 双 waiter 交错测试补齐；ledger 断言升级为精确等式。

### Batch 2（B1：ETag/304 条件请求，加性 wire）— 门控 9.6 PASS（rev-1/rev-2 FAIL → 5 轮修复 → PASS）

- 新建 `src/oc_slimapi/etag.py`（强/弱 ETag、RFC 9110 弱比较、`judge_conditional` 单候选判定——gzip 弱 tag 回显合法性的构造论证见模块注释）；messages/sessions/agent/command 四路由接入；Vary 合并 `X-Opencode-Directory`（directory 实测影响上游 workspace 路由，4 路由一致）；`config.py` `etag_enabled`。
- 关键设计固化：catalog/sessions 无条件压缩保留（ETag coding 预测依赖其确定性）；304 头集合 = ETag 同值 + Vary 同 200 + no-store + 辅助头复制。

### Batch 3（C2a：T17 todo/children thin 路由，加性 wire）— 门控 9.5 PASS（rev-6 FAIL 5.5 → 修复 → PASS）

- 新建 `routes/todo.py`/`routes/children.py` + 两测试文件；复用 `_catalog_common` admission-first 管线。
- rev-6 修复要点：两路由显式禁用 ETag（`enable_etag=False`）；per-item 非 dict 守卫 → 503；空 `[]` 受益门跳 gzip（`min_gzip_bytes`）；v2-contract children 删除行 strikethrough + 加性回归注记。

### Batch 4（B3：消息内容指纹，加性 wire）— 门控 9.8 PASS（rev-6 锚点/性能实测校正后）

- 设计稿 `docs/specs/design-message-watermark.md`（方案甲定稿：`contentFingerprint` = `"<vN>:<sha256hex>"`，merged splice 后重算，五类降级不重算）。
- `skeleton.py` `compute_message_fingerprint`/`recompute_fingerprint`（开关经 `SkeletonLimits.fingerprint` 携带，worker 签名不变）；`config.py` `message_fingerprint_enabled`（REP_VERSION 联动）；`tests/test_message_fingerprint.py`。

### 全量终审（进行中）

- rev-1 FAIL 9.2（2 blocking + 2 conditions）→ 已修复：
  - **B1（blocking）A4 discovery 租约/内存不变数**：questions/permissions 的 LEVEL 1 discovery 共享值由展开 JSON 图改为 **capped raw bytes**（`discovery.py` 拆出 `fetch_global_root_sessions_raw`，旧函数保持兼容）；joiner 在租约内 parse → 派生自有 directory 字符串副本 → `del` 展开图后释放租约；LEVEL 2 per-dir 审计确认本就共享 raw bytes（合规，测试钉住）。
  - **B2 发布文档不一致**：本文件状态行 + 本实施记录节；v2-contract §9 gzip 按路由族改写（见该文件）。
  - **C1 shutdown 措辞**：§3.x Registry 归属条目改为与实现一致（shutdown 后可复用，见该条目）。
  - **C2 A4 回归测试**：`tests/test_questions_coalesce.py` 新增 5 用例（raw bytes 类型断言/派生时租约在持有快照断言/weakref 图探针/per-dir 钉住/permissions 镜像）。
- 状态：待 rev-sgpt 复评（≥9.5 后走 §8 发版：release.sh minor + smoke 证据回收）。
