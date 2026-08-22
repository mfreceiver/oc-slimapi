# 4.11.0 实施方案 v6：ocdroid 提案 P1–P6 全量落地

> 状态：v6（吸收 rev-sgpt R5 唯一 MAJOR——executor 队列已取消未出队 WorkItem 积累；待六审）。
> owner 裁定（2026-08-22）：P1 仅做阶段1；阶段2 non-goal。单一 minor 4.11.0；方案审通过后开发；代码门控 ≥9.5 后发版。
> v5→v6 变更（定点修订）：P5 释放语义升级为**严格许可存续**（permit-outlives-cancellation）——offload 提交后 permit 存续至 executor future 真正终结（`asyncio.shield(fut)` + done_callback 延迟释放），在途 permit 持有工作项恒 ≤ W，队列零无背书积累，`(A_amp+1)×W` 上界成立；C lane 在 transform.py **追加** `offload_strict` 新原语（现有 `async with` 使用方零改动），transform.py 纳入 C 写域；取消测试补连续多波场景。
> 历史轨迹：v1 7.2 FAIL → v2 7.8 FAIL → v3 8.2 FAIL → v4 8.8 FAIL → v5 8.9 FAIL（唯一残留：队列积累）→ v6 → **v6.1**（2026-08-22 编排者裁定：§3.2 cq_hash 失配改判 reset，消除与 §3.6 的表述冲突；实现侧 fix-2 原按 400 实现须同步改为 reset）。

## 0. 总裁决矩阵

| # | 内容 | 路线 | 量级 |
|---|---|---|---|
| P1 | messages `?since=` 差分 | 单当前快照 + CAS + `next_cursor is None` 穷尽权威 | M-L |
| P2 | thin 路由 ETag/304 | 翻 `enable_etag`，保持 `no-store` | XS |
| P3 | readiness 旗标 | REQUIRED 追加 | XS |
| P4 | digest `messagesRevision` | 进程级全局单调 int | S |
| P5 | `/slimapi/file/raw` | 裸二进制 + admission-before-buffering + 每 permit 乘数预算 | M |
| P6 | health dbaux | 已实现，仅文档指引 | 文档 |

## 1. 批次与写域（R3-MAJOR-1/2 最终版）

```
Phase A（XS，先行合并；交付前全仓 check.sh 绿——A 不新增路由，门禁无涉）
Phase B（P1）∥ Phase C（P5）——基于 A 后基线，业务代码零交集
集成步（编排者，C 返回后）：app.py 注册 file_raw router → 全仓 check.sh 绿
docs lane（编排者串行）：v4-contract / INTERFACE_MAP 注记 / CLIENT_CHANGES / CHANGELOG → 终检 check.sh
门控 rev-sgpt ≥9.5 → 提交 main clean → release.sh minor → 部署 + smoke
```

**写域矩阵（零并行交集；顺序触碰标明）**：

| 文件 | A | B | C | 集成步 | docs lane |
|---|---|---|---|---|---|
| readiness.py / todo.py / children.py / diff.py / sse/hub_types.py / sse/global_hub.py | ✍ | — | — | — | — |
| config.py（since_cache_* 四项 + file_raw_max_envelope_bytes + **启动内存预算校验扩展**，见 §4.1） | ✍ | — | — | — | — |
| 新 since_cache.py（模块+实例化）+ messages/_list.py + **app.py SinceCache wiring** + tests | — | ✍ | — | — | — |
| 新 routes/file_raw.py + **transform.py（追加 offload_strict 新原语，现有行零改动）** + tests + **INTERFACE_MAP.md file/raw 单行**（R3-MAJOR-2：check_routes_doc.py:1-23 静态扫描 routes/**，无此行 C 的 check.sh 必红——C 的唯一 docs 触碰，窄域冻结；R6-MINOR-2：transform.py 归 C 写域） | — | — | ✍ | — | — |
| app.py file_raw router 注册（两行） | — | — | — | ✍ | — |
| v4-contract.md / INTERFACE_MAP messages 行注记 / CLIENT_CHANGES.md / CHANGELOG.md | — | — | — | — | ✍ |

- **SinceCache wiring 时序（R3-MAJOR-1 修复）**：B 同时落 `since_cache.py` 模块与 `app.py` 实例化挂载——模块在 wiring 时已存在，B 交付时 check.sh 绿；A 仅落 config 键（无模块 import 依赖），彻底删除 v3 的「getattr 防御/占位 or B 落地」歧义。app.py 由 B（wiring）→ 集成步（file_raw 注册）**顺序**触碰，无并行重叠。
- **check.sh 声明（R3-MAJOR-2 修复）**：A 不新增路由 → 门禁无涉，全绿；B 在既有 messages 路由加参数 → 门禁无涉（messages 已在 INTERFACE_MAP），全绿；C 交付含 INTERFACE_MAP file/raw 单行 → 静态扫描满足，全绿；集成步注册后全绿；docs lane 完成后终检全绿。删除 v3「临时本地 patch」说法——所有 check.sh 绿的声明均针对**交付态**。
- 每 lane 交付报告附 check.sh 绿输出（A/B/C + 集成 + docs 终检共五次）。

## 2. Phase A 规格

### A1（P3）readiness 旗标
REQUIRED（readiness.py:57-66）追加 `sessions.details.v4`（`session.post-actions.v4` 之后）；无依赖蕴含；测试 payload 含新 ID、ready=true、normalize 不变式。

### A2（P2）thin 路由 ETag（保持 no-store）
三路由 `enable_etag=True`（todo.py:79/children.py:85/diff.py:104），`_catalog_common.py` 零改动；唯一行为变更（恒 200→可能 304）显式进契约+CHANGELOG。测试全集：INM 命中→304（完整 ETag/Vary/Cache-Control 头）、同 body 强 ETag 稳定、gzip `W/` 弱 validator+弱比较、identity/gzip validator 域不混用、`If-None-Match: *`、多 validator、不带 INM 恒 200、agent/command no-store 回归断言。

### A3（P4）digest messagesRevision
进程级全局单调 int；relevant 事件（updated/appended/removed）bump；flush 携带窗口末值；session-only digest omit；**不跨进程 epoch（重启）比较；同进程内 SSE 重连/upstream resync 可比较（resync 不清零）**；removed 分支语义序：retired gate → prune → token hub → 追加 bump。测试全集同 v3。

## 3. Phase B 规格（P1 阶段1）

### 3.1 状态机与 CAS lineage（已闭合，R3 NOTE 确认）
- `observed_snapshot`（所有无 before 请求开始时捕获，仅用于 CAS）与 `diff_baseline`（token 精确匹配当前 gen 才建立）显式区分。
- 完成时三分支：①entry 不存在→安装+签发新 token；②CAS 成功→byte-identical 复用 gen（稳定）/不同→替换+签发；③CAS loser→identical 复用当前 gen+签发 / **differing 丢弃写入+omit nextSince**。
- **原子性（R3 NOTE 实现要求）**：「读取 current → 比较 → 替换/复用」为**无 await 的同步原子 cache 操作**。
- **token 唯一性措辞收紧（R3-MINOR-3 + R4-MINOR-2）**：`epoch` = 每次进程启动生成的随机 nonce，进程内固定；`gen` = 同一进程级**单调计数器 allocator** 在**entry 首次安装**与**每次内容不同的成功替换**时分配的进程内唯一值——两者独立，永不复用、永不回落。byte-identical 复用（CAS 成功或 loser identical）**不递增** allocator。
- 重试=安全 reset（非幂等）；CAS loser omit 语义契约化「并发竞争降级」。

### 3.2 token 与 nextSince
token `{v:1, epoch, sid, cq_hash, gen}` base64url；epoch 随机 nonce 失配→reset。**仅无 before 响应可签发；CAS-loser differing 为唯一 omit 例外**（带 before 响应一律 omit）。解析分类（**v6.1 编排者裁定**：吸收 fix-2 发现的 §3.2/§3.6 冲突——cq_hash 失配从 400 组移入 reset 组，依据 R1-MINOR-3「格式合法但语义过期→reset」+ R2 NOTE「查询轴变化 reset 可接受」；查询轴变化是客户端合法行为，reset 全量+新 token 无浪费路径，400 仅保留给真错误）：语法损坏/非对象/版本不支持/**sid 失配**/超长(>512B)→400 `invalid_params`；格式合法但 gen 过期/epoch 失配/miss/bypass/**cq_hash 失配（limit/directory/mode 变化）**→reset。

### 3.3 差分算法（R3-MINOR-1/2 修正后冻结）

```
raw = 上游解析后原始 item 列表（投影前）
next_cursor = _parse_link_next_cursor(上游 Link)
fresh = 投影后列表（上限 limit）
window_exhausted = (before 缺席) and (next_cursor is None)   # 唯一权威（R3 NOTE 确认：
                                                              # 上游 limit+1 判 more，仅确有下一页才发 cursor/Link）
changed = [i for i in fresh if mid ∉ cache.fingerprints or 指纹变化]
removed = [mid for mid in cache.fingerprints
           if mid ∉ fresh_mids
           and (window_exhausted or boundary_newer(mid, fresh_oldest))]
boundary_newer(mid, fresh_oldest):
    if fresh_oldest is None: return False      # ★ R3-MINOR-1 冻结：非穷尽空投影不推断任何 removal
    边界键 = (time.created, id) 严格比较；mid > fresh_oldest → True；== 或 < → False（保守防御）
```

- **排序事实修正（R3-MINOR-2）**：上游实际交付序为 `(time_created ASC, id ASC)`（message-v2.ts:435-465 `desc,desc` 查询后 `items.reverse()`），sidecar 时间稳定排序保留同时间戳 ID 升序——**边界二元组与现行分页序一致**，distinct MID 不可能与 fresh_oldest 完全相等；`==` 分支为**防御性设计**（防上游排序演进），**不是现行盲区**——契约按此准确表述，不把不存在的限制写入正式契约。
- 指纹+差分在 transform admission 内 offload；cq_hash canonicalization 同 v2（v1 前缀/omitted==默认/tolerated mode 归一/directory effective 值）。

### 3.4 缓存预算
`since_cache_enabled`(true)/`max_entries`(256)/`max_bytes`(64MiB)/`max_entry_bytes`(1MiB)（A 落 config，B 消费）。记账保守公式：`len(canonical_items) + Σ_mid(len(mid_utf8)+32+64)`，测试断言实际 retained；oversized→bypass（omit nextSince）；总量超限 LRU 逐出；逐出/bypass 后旧 token→reset。

### 3.5 路由集成
`since` Query 参数；singleflight flight key 不含 since；flight 返回后 per-request 投影→差分→envelope→ETag（`judge_conditional` 复用）；上游拉取零改动。

### 3.6 测试全集
CAS lineage 组（两相同 token 并发必出 loser 且 identical→复用/differing→omit；full+since 并发；新先旧后不回滚；loser identical 复用；loser differing omit；重试→reset 无错差分）；穷尽组（删除穷尽页最旧元素；截断窗口滚出不误报——next_cursor 非空构造；**投影空但未穷尽（raw 恰满 limit+有下一页）→ removed 为空**；同时间戳并列防御不报）；before 组（带 before 无 nextSince 键；before+since→400）；reset/400 分类；形状回归（无 since golden 仅 +nextSince，loser 例外契约化）；merged/limit 变化；资源组（记账/LRU/bypass）。B 交付 check.sh 绿（无新路由，门禁无涉）。

### 3.7 契约修订（docs lane）
修订五 owner 裁定头 + §7.5/§10 新节（含 CAS loser omit、穷尽权威、防御性 == 分支的准确表述、重试=reset、epoch 随机 nonce 不跨进程、字节预算/bypass、盲区=截断窗口滚出+删除并存不可区分、P4 对账兜底）。

## 4. Phase C 规格（P5，R3-MAJOR-3 补齐预算模型）

### 4.1 资源模型（每 permit 乘数 + 启动校验 + 取消重叠）

```
effective_cap = min(max_response_bytes, file_raw_max_envelope_bytes)   # 默认 32 MiB
A_amp = 4   # 每 permit 放大常数（保守因子，不依赖实现期生命周期纪律）：
            # 信封 bytes + parse 树/decoded 中间对象 + 响应 bytes + gzip 候选/常量开销
            # ——四者按可能同时共存取 4；实现中仍做分阶段释放（encode 后立即 del parse
            # 树与 content 引用再进 gzip）作为纵深防御，但预算不依赖该纪律
W = max_transforms（transform worker 数；file-raw 与其他 transform 共享同一组 W）
```

- **时序**：先取 transform permit（`TransformBusy`→503+Retry-After）→ permit 持有期间上游 GET + `read_with_cap(effective_cap)` → 变换经 `TransformPool` offload → **严格许可存续释放**（R5-MAJOR，见下）。
- **严格许可存续（permit-outlives-cancellation，冻结语义）**：offload 一旦提交，**permit 存续至底层 executor future 真正终结（完成或被 worker 出队确认取消）才释放**——请求协程被取消时**不**提前释放 permit：`await asyncio.shield(fut)` 使取消不传播到 future，`finally` 中 future 未 done 则挂 `add_done_callback` 延迟释放。由此：**任一时刻持有 permit 的在途工作项（运行中 + 排队中）总数 ≤ W**，信封缓冲仅发生在 permit 持有期间（admission-before-buffering），executor 队列中不存在无 permit 背书的 WorkItem——连续多波取消无法积累队列内存。实现为 C lane 在 `transform.py` **追加**新原语（如 `offload_strict` / 暴露 acquire+deferred-release；不改动现有行——现有 `async with TransformPool` 语义对其他使用方零影响），transform.py 纳入 C 写域（A/B 不触碰，零并行交集）。
- **峰值内存模型（单一公式，契约/注释/实现/测试统一；严格存续下成立）**：

  `peak = (A_amp + 1) × W × effective_cap`

  严格存续使同时存活的信封缓冲 ≤ W（每个背后有 permit）；`A_amp×W` 覆盖 W 个在途转换的对象放大，`+1×W` 为防御余量（保守超集，覆盖释放回调调度窗口等瞬态）。旧 worker 数受 W 硬约束（bounded executor），在途取消不可能制造无背书的工作项。
- **启动预算校验（A 落 config.py，扩展现有聚合校验 config.py:875-930，组合方式冻结）**：

  ```
  file_raw_bound = (A_amp + 1) × W × effective_cap
  transform_bound = max(existing_transform_bound, file_raw_bound)   # 共享同一组 W worker → 取 max，不相加
  raw_plus_transform = raw_fetch_max_bytes + transform_bound        # 纳入既有 aggregate 断言
  ```

  与既有 512/576 MiB 设计口径一致——高并发+大 cap 组合启动即 fail-fast，不留运行时惊喜。
- 测试：permit 先于上游 GET 时序断言（mock pool 记录）；**取消场景精确拆分（R6-MINOR-1）**——①有限并发：W=2 且仅 1 个旧 worker 阻塞 → 新请求取得剩余 permit 完成全流程（admission→GET→cap-read→offload→响应）；②满载背压：W 个旧工作项占满 permit → 连续新请求均停 admission 被 `TransformBusy` 503 拒，断言 upstream GET / `read_with_cap` / executor submit **均未发生**（队列零无背书 WorkItem）；③恢复：future 终结后 permit 逐一恢复、新请求可进入、无双重释放；④exactly-once release 竞态（R6-MINOR-3）：取消与完成同时发生的边界——future 已 done 则同步释放 vs 未 done 则仅 callback 释放（permit 所有权一次性转移，两路径互斥），断言每 permit 恰好释放一次；⑤单次取消严格存续：阻塞旧 worker → 取消其请求 → 断言 permit **未提前释放**（存续至 future 终结）。另：启动校验对超预算组合 fail-fast；effective_cap 取小边界；`offload_strict` 对现有使用方（messages/catalog/sessions/read-groups 现有 `offload()`/`async with` 路径）零影响回归。

### 4.2 响应语义
同 v3：binary 裸字节 + mimeType 白名单验证（非法/缺失→octet-stream）+ 跳过 gzip + 强 ETag + `Vary: Accept-Encoding`；text→`text/plain; charset=utf-8`+gzip 协商+`W/`；Content-Length 不手工设置；`Cache-Control: no-store`；畸形信封→502 `raw_decode_failed`（house renderer）；4xx verbatim/5xx→503；cap 边界→413；`?v=4`；directory allowlist；file 桶免改。

### 4.3 交付与测试
C 交付 = `routes/file_raw.py` + `tests/test_file_raw*`（fixture 自注册）+ **INTERFACE_MAP.md file/raw 单行**（门禁必需）。测试全集同 v3 §4.3 + admission 时序/取消重叠/启动校验组。C 交付 check.sh 绿（含 INTERFACE_MAP 行后静态扫描满足）。

## 5. 集成步 + docs lane + 发版（编排者）

1. C 返回后：app.py 注册 file_raw router（两行）→ 全仓 check.sh 绿。
2. docs lane 串行：v4-contract（修订五头+§6.4+§7.5+§10 新节）→ INTERFACE_MAP（messages 行注记；file/raw 行已在 C 交付中）→ CLIENT_CHANGES（P1/P2/P4/P5/P6 全指引）→ CHANGELOG `[4.11.0]` → 终检 check.sh。
3. rev-sgpt 代码门控 ≥9.5（MAJOR 清零复审）。
4. 提交全部至 main clean → `./scripts/release.sh minor` → v4.11.0 → `pip install -e .` + restart + smoke（since 差分实测/file/raw 304/thin 304/readiness/health auxiliary）。

## 6. non-goals
P1 阶段2；同 token 幂等响应（多代保留）；改显示排序（边界 == 防御分支保留）；P5 Range/immutable hash URL；P2 digest 信号变体；active 退役；上游改造；per-SID revision map；Cache-Control 切 no-cache。
