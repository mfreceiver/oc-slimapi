# Backend Reliability & Correctness Audit

- **审计时间**：2026-08-22 17:01 – 18:55（无人值守，3h 预算内完成）
- **对象**：`oc-slimapi` @ HEAD `e3a3002`（v4.11.0），工作区 clean（仅未跟踪 `.audit-scratch/` 审计脚本目录）
- **方法**：主 agent 建立运行模型 + 3 并行探索 agent（数据完整性 / 并发可靠性 / API 失败模式）+ 1 对抗复核 agent（targeted reproduction）；所有发现要求 REPRODUCED 或 STATICALLY_PROVEN 并经对抗复核
- **环境注意**：本机为 Windows 开发机（生产为 Linux/systemd）。测试基线中 7+62 个失败经逐项归因**全部为 Windows 环境伪影**（见 §Commands / Tests Executed），不构成产品缺陷。

## Executive Summary

总体结论：**代码库可靠性工程成熟度很高**。single-flight 双 profile 记账不变量、SinceCache CAS 谱系、ReplayLog 环形不变量、TransformPool 许可转移、SSE 订阅生命周期等核心并发不变量在随机化混沌交错下全部成立（agent2 共 12 项攻击性实验，11 项 REFUTED 即不变量保持）；API 契约一致性在 40+ 复现用例中与 `v4-contract.md` 逐格吻合；request_id 观测链路零缺失。

发现 **2 个 P2（已复现并经对抗复核确认的产品缺陷）**、**2 个 P3**、**1 个 P3 条件性加固项**，以及一组 P4 文档/观察项（含对抗复核的两个降级）：

| ID | 级别 | 一句话 | 状态 |
|---|---|---|---|
| BE-001 | P2 | since-diff 对缺 `info.time.created` 的退化行产生**假阳性 `removed`**（违反契约 §10.3 冻结不变量） | REPRODUCED（双重复现；对抗复核 CONFIRMED，触发前提收窄为上游数据异常） |
| BE-002 | P2 | hub grace 移除的 cancel-unwind 窗口内新订阅者落到**僵尸 GlobalHub**（无帧无心跳直到该连接断开）；token-stream 入口同样命中 | REPRODUCED（双重复现；对抗复核 CONFIRMED：控制面+token 两入口动态证实，窗口亚毫秒但真实） |
| BE-003 | P3 | 读组路由 f-string 拼 URL：query 中原始 `#` 后字节被 httpx 当 fragment **静默截断** | REPRODUCED 端到端（真实 uvicorn/h11 接受裸 `#`，三个位点全部截断；对抗复核不可降级为不可达） |
| BE-004 | P3 | access-log 压缩 TOCTOU：active-path 单次快照后打开的过日期 fd 会被 unlink（Linux 下 straggler 行消失） | 构造交错复现（对抗复核：可达性收窄至"tick 内跨午夜时钟回拨"，接近不可达） |
| BE-006 | P3(加固) | `wait_for(semaphore.acquire())` 许可丢失竞态敏感区间 **3.11.0–3.11.4**；生产实测 **3.14.4 免疫** | 条件性隐患（对抗复核 DOWNGRADED：建议 `requires-python>=3.11.5` 一行加固） |

无 P0/P1（无数据损坏可提交路径、无死锁、无契约级破坏）。SQLite 写域红线（mode=ro + query_only）经静态核查未被任何代码路径穿透。4.11.0 新 since 家族（9007aeb 单 commit 引入、**尚无后续修复提交**）是 BE-001 的载体——新代码首版缺陷的典型分布。对抗复核另有两个降级：错误体 gzip 膨胀实为契约 §9(P0-5) 明文要求且测试钉死（BE-005 → 仅注释失实）；`wait_for` 许可丢失在生产 3.14.4 下免疫（BE-006 → 加固建议）。

## Runtime Architecture Model

**请求管道**（同步面）：

```text
ocdroid ─(stunnel mTLS 14097)→ uvicorn(FastAPI 单 worker, :4097 loopback)
  → RequestIdMiddleware（最外，X-Request-ID 注入/回显/换新）
  → TrafficAccountingMiddleware（纯 ASGI，down/up 字节账本，SSE 透传）
  → SlimapiSelectorMiddleware（?v=4 唯一合法门；v 剥离；directory 消费矩阵；
    非 /slimapi 前缀 → 已关闭 catch-all → 404/405）
  → 路由层：
      (a) 直连上游 GET（共享 httpx AsyncClient，32 连接池，read_with_cap 截断→413）
      (b) LeasedSingleFlight raw_fetch_registry（list 路由 raw GET 合并，字节预算准入）
      (c) plain SingleFlight `fulls`（/full 消息体跨形状去重，1s grace）
      (d) CatalogCache（agent/command TTL 体缓存 + 内置防 stampede + epoch 失效）
      (e) SinceCache（messages ?since= CAS 快照谱系；commit 为事件循环串行点）
      (f) dbaux（SQLite mode=ro + query_only 投影，单 worker executor，circuit breaker）
  → TransformPool（admission 信号量 max_transforms + 有界线程池；parse/project/
    serialize/gzip/ETag judge 全部 offload；TransformBusy→503）
  → 响应（gzip 协商 + coding 标注 ETag/304；错误经 errors.py coded envelope）
```

**SSE 管道**（流式面）：

```text
GET /slimapi/events|/sessions/{sid}/stream
  → HubRegistry（惰性 per-directory GlobalHub；每目录一条共享 /global/event 上游 SSE）
      run() 重连环（指数退避 1s→30s）→ epoch 切换 → upstream-loss 回调 →
      CatalogCache.invalidate（生成栅栏）→ 订阅者 resync 帧
  → publish()：session.digest / q/p 直推 / slimapi.meta 首帧
      → TokenStreamHub（delta 累积 + flush engine + 每订阅者预算）
      → ReplayLog（epoch + TTL 15min 环形，60s sweep；重连 replay）
      → TurnRegistry（turnIncarnation/turn 盖章；IncarnationStore tmp+fsync+os.replace）
  → 每订阅者有界队列（满→terminal 对 resync+STOP 强断；超大小→dropped_frames 计量）
```

**后台任务**：access-log 维护 loop（to_thread gzip/prune，_MAINT_LOCK）、traffic snapshotter（间隔快照+自管保留）、replay sweep、qp_sweep shadow、dbaux probe、burst watch（5xx 突发 WARNING）。

**线程模型**：单事件循环 + 两个线程池（TransformPool executor、dbaux 单 worker）+ access-log 维护 to_thread。所有协作对象（SingleFlight×2/SinceCache/Hub/TokenHub/ReplayLog/Ledger）**设计上非线程安全**，依赖"只在事件循环上触碰"纪律——本次审计枚举了全部线程边界（transform 纯 CPU / access-log 隔离文件 IO+锁 / dbaux sqlite+breaker），未发现纪律破坏（唯一跨线程共享的可变状态 dbaux LatencyBreaker `_samples` 为 GIL 原子的 cosmetic 观察，见 Rejected A2-3）。

**停机链**：uvicorn graceful 5s → AsyncExitStack LIFO（维护→dbaux→token_hub→qp_sweep→hubs→replay sweep→replay_log→raw_fetch→catalog→fulls→transforms 10s→upstream→snapshotter→access-log handler），逐项异常隔离，systemd TimeoutStopSec=60 覆盖。

## Git Baseline

- HEAD `e3a3002`（2026-08-22 16:56 +0800），工作区 clean。
- 近 30 天 **319 commits**——极高频演进；4.8.0→4.11.0 四个 minor 在 3 天内发布。
- 风险聚集点（按 git log -p）：
  - `9007aeb`（4.11.0 P1-P6）：since 家族（`since_cache.py` + `_list.py` diff 机制）**单 commit 引入后无任何修复提交**——BE-001 载体。
  - `0430118`（4.10.1）：catalog epoch 失效修复，注释自认"stale 有界于 SSE 重连窗口"；对抗复核确认该界成立，但记录到机制边界：**零 SSE 消费者时 run() 退出、无 epoch 丢失回调，退化为纯 TTL（300s）**——as-designed 但值得运维知悉（A2-9）。
  - `e2f4bab`（4.10.0）：批量扇出 cancel-and-await；取消传播经实测完备（A2-5 REFUTED）。
  - `d5bc8a3`（4.9.1）：上一轮审计整改，相关面本次未发现回归。
- 历史一致性：dbaux/`message-v2` 分页等上游对照面无近期改动；`007d6e7` repoint 的 sidecar 契约文件 diff 零漂移声明与本次抽查一致。

## Commands / Tests Executed

```text
# 环境（Windows 开发机，生产为 Linux）
python -m venv .venv && .venv/Scripts/python.exe -m pip install -e '.[test]'   # OK
.venv/Scripts/python.exe -m compileall -q src                                  # OK（字节码 0 错）

# 测试基线
(1) 全量 pytest（首次）：被终止于 ~5%（疑似缓慢；用户手动停止）
(2) 失败签名探针：pytest tests/test_access_log*.py tests/test_actions.py --tb=line
    → 62 failed / 79 passed，全部失败根因：actions.py:280 owner-only-write 权限门
      （st_mode & 0o022）在 Windows 上判所有 manifest 为 group/other-writable →
      actions 功能禁用 → 整族测试失败。生产 Linux 语义正确，属环境伪影。
(3) 目标子集（55 文件 / 1727 测试，覆盖全部被审计模块）：
    pytest <singleflight|since|transform|catalog|sse|events|token|messages|expand|
            selector|version|etag|access_log|traffic|dbaux|replay|config|errors|
            app_assembly|batch3|burst|cursor|digest|...> -q
    → **1720 passed / 7 failed / 71.5s**
    7 个失败逐项归因（全部 Windows 伪影）：
      - test_config.py ×2：POSIX 路径归一化（'/var/log/slim' vs '\\var\\log\\slim'）
        与 chmod 权限模拟（Windows ACL 不映射 0o 权限位）
      - test_dbaux_*.py ×5：os.replace 覆盖**打开中的 SQLite 文件**被 WinError 5 拒绝
        （Linux rename-over-open-file 合法；测试的 inode 切换语义在生产平台成立）

# 复现脚本（全部位于 .audit-scratch/，均以生产代码真实类/httpx MockTransport 驱动）
agent1: orjson canonical / since-removed 矩阵 / CAS 谱系 / etag+gzip judge /
        dbaux cursor 矩阵 / file_raw / 批量扇出 / envelope 杂项（a,b,c,d,e,f,g,h）
agent2: singleflight 账本混沌 ×200 轮 / plain 保留 / grace 移除窗口（含真实 httpx）/
        僵尸自愈 / pool wait_for / offload_strict / 生命周期风暴 / replay 压力 /
        fanout 取消 / token 溢出+退订幂等 / catalog 栅栏 / breaker+accesslog TOCTOU
agent3: selector 边角 13 例 / 上游失败映射 / SSE raw-ASGI 帧级 / 观测链路 32 请求
agent4: （对抗复核脚本，见各 finding）
主 agent 复测：agent1_b（S4 假阳性复现）、agent2_grace_removal_httpx（ZOMBIE 复现）
```

## Verified Findings

### BE-001 — since-diff 假阳性 `removed`：退化行（缺 `info.time.created`）浮顶 + 截断窗口

**Severity**: P2（中）｜**Confidence**: 高（双重复现）｜**Verdict**: REPRODUCED（对抗复核见下）

- **Affected Component**: `src/oc_slimapi/routes/messages/_list.py` — `_created_sort_key`（:77-105）、`_boundary_key`（:496-499）、`_artifact_from_array_bytes` removed 推断（:548-556）；`src/oc_slimapi/since_cache.py`（谱系承载）
- **Trigger**: (a) 上游消息行缺/坏 `info.time.created`（代码防御分支明确声称要处理的输入）；(b) 会话长于 `?limit=`（Link/nextCursor 非空，窗口未耗尽）；(c) baseline 含有滚出最新窗口的消息。
- **Execution Path**: `GET /slimapi/messages/{sid}?since=<valid>` → `_parse_sort_project`（退化行以 0 键浮到页首）→ `_artifact_from_array_bytes`：`fresh_oldest_key=(0, deg_mid)` → 对每个 baseline mid：`old_key > fresh_oldest_key` 恒真 → 全部进 `removed`。
- **Transaction / Concurrency Context**: 单请求纯函数路径；与并发无关。
- **Evidence / Reproduction**: `.audit-scratch/agent1_b_since_removed.py` S4；主 agent 独立重跑输出 `S4_removed: ['mold', 'm1']`（契约要求 `[]`）、`S4_items_order: ['deg1','n1','n2']`（退化行同时打乱 §8 排序）。确定性 100%。
- **Expected / Actual**: removed 按契约不变量不得假阳性 / 实际整窗 baseline 被误报删除。
- **Root Cause**: `_created_sort_key` 对缺 created 行返回 `0`（而非排序键 None/排除），`_boundary_key` 进一步把它当作合法窗口边界参与严格比较；边界任一侧退化时应按契约 `oldest is None → False` 处理。
- **Impact**: 客户端收到"这些消息已删除"的假信号 → 可能本地删除仍存在的消息视图；缓解：digest `messagesRevision` + 周期全量 If-None-Match 调和可自愈；触发需上游行缺 created（上游 schema 声称必填）。
- **Git Context**: 9007aeb（4.11.0）首次引入，无后续修复；现有测试所有 fixture 均设置 created，零覆盖。
- **Recommended Fix**: `_boundary_key` 在行缺有限 created 时返回 `None`（等价契约的 `oldest is None` 分支 → 不报 removed）；`_created_sort_key` 考虑将退化行稳定沉底而非浮顶（同时修复排序面）。
- **Regression Test**: 缺 created 行 + nextCursor 非空 + baseline 含窗口外 mid → 断言 `removed == []`。
- **Adversarial Review**: **CONFIRMED（维持中危）**。契约 §10.3 行 588-594 明文冻结"removed 不得出现假阳性（契约级不变量）"——非仅 `oldest is None` 分支。上游 v1.18.21 schema（GitHub `packages/schema/src/v1/session.ts`：`User.time.created: Timestamp`、`Assistant.time.created: NonNegativeInt` 均**必填**）使正常写路径不可产生缺 created 行——触发需 DB 损坏/schema 漂移；但 `_created_sort_key` docstring 自认防御对象就是 "degenerate upstream rows"，且 `message-v2.ts:80` 读路径对 DB data blob 无再校验。前提精化（`agent4_a11_preconditions.py`）：id 合法 + created 缺失/字符串/bool 均触发 FP；退化行连 id 也缺 → 保守不报（E2）；窗口穷尽时报告的是真删除（E5）；before-present 的 artifact 层 removed 路由不可达（since+before→400，非缺陷）。

### BE-002 — 僵尸 GlobalHub：grace 移除 cancel-unwind 窗口内接纳的订阅者无帧无心跳

**Severity**: P2（中）｜**Confidence**: 高（双重复现，真实 httpx）｜**Verdict**: REPRODUCED

- **Affected Component**: `src/oc_slimapi/sse/registry.py` `_remove_hub_after_grace`（:354-394）、`src/oc_slimapi/sse/global_hub.py` `ensure_upstream`（:254-258）与 `_make_group_done_callback`（:328-368）；第二入口：`TokenStreamRegistry.subscribe` → `cancel_pending_removal` + `hub.ensure_upstream()`。
- **Trigger**: 最后一个消费者离开 → 30s grace 到期 → 移除任务 cancel 4 个 hub 任务 → `run()` 经 `client.stream(...)` 的 `__aexit__`（上游 SSE 连接 aclose，毫秒级）展开 → **恰在此窗口**新 `/slimapi/events` 或 `/slimapi/sessions/{sid}/stream` 请求到达。
- **Execution Path**: cancel(hub.task) → `await asyncio.gather(*tasks)` 挂起 → 新订阅者 `ensure_upstream()` 的守卫 `if not self.task or self.task.done()` 在"已取消但未完成"的任务上判 False → 不重生 → gather 完成 → 重检 `has_consumers()` 为 True → 放弃移除（hub 保留在 registry）→ `done_callback` 对 cancelled 任务视为拆卸、不重建 → 订阅者挂在零活任务的 hub 上。
- **Transaction / Concurrency Context**: 事件循环上的生命周期间隙（cancel 与 unwind 完成之间的任务状态窗口），非数据竞争。
- **Evidence / Reproduction**: `.audit-scratch/agent2_grace_removal_httpx.py`（真实 `GlobalHub.run()` + MockTransport SSE，aclose 80ms）；主 agent 独立重跑输出 `ZOMBIE HUB: True`（`hub.task done: True cancelled: True` 且 `total_subscribers: 1`）。确定性。
- **Expected / Actual**: 新订阅者获得活 hub（组重生）/ 订阅者零帧零心跳直到其客户端自身超时断开。
- **Root Cause**: `ensure_upstream` 的完成判定与"取消中但未完成"任务状态不相交；移除任务放弃路径未补 `ensure_upstream()`。
- **Impact**: 单连接受害者（该订阅者静默无数据）；自愈路径已验证：下一个订阅者触发重建（`hub.task.done()` 为真 → 重生）、僵尸订阅者断开后空闲 grace 移除 hub → 后续干净。窗口窄（每次 grace 到期一次、时长≈一次连接关闭），但常驻进程 + 自动重连客户端长期运行下非零命中。
- **Git Context**: F-011/NB-B1 等既有修复均针对身份条件槽清除与 pending 移除取消，未覆盖本窗口。
- **Recommended Fix**: `ensure_upstream` 在 `task.done() or task.cancelling()` 时重生；或移除任务在 `has_consumers()` 放弃分支显式重调 `ensure_upstream()`。
- **Regression Test**: 注入慢 aclose 的 MockTransport，在移除 gather 期间并发 subscribe，断言 `hub.task` 存活且订阅者收到 meta/心跳。
- **Adversarial Review**: **CONFIRMED（维持中危）**。(a) 窗口宽度实测：真实 TCP socket + `sleep(0)` 采样粒度下 **6/6 命中**（窗口含至少一个事件循环让出点；1ms 轮询粒度下 0/10——环回 aclose 亚毫秒，生产命中概率低但窗口真实）。(b) 僵尸期订阅者完全静默饿死：events 路由 generator 只 `await queue.get()`，心跳唯一来源是被取消的 `heartbeat_loop`（`routes/events.py:194` + `global_hub.py:546`）。(c) **token-stream 入口动态证实**（`agent4_a21_token_zombie.py`，3/3）：`TokenStreamRegistry.subscribe` 落窗时 `cancel_pending_removal` 取消 removal task（其 gather 抛 CancelledError 提前 return、不恢复）+ `ensure_upstream` no-op → 同样僵尸。根因补强：`_make_group_done_callback` 首行 `if task.cancelled(): return`——取消路径明确不触发 INV-1 重建。自愈确认：后续订阅重建（3/3）。

### BE-006 —（条件性加固）`wait_for(semaphore.acquire())` 许可丢失竞态：敏感区间 Python 3.11.0–3.11.4

**Severity**: P3（加固建议；**当前生产免疫**）｜**Verdict**: DOWNGRADED（对抗复核后由 P2 条件降级）

- **Affected Component**: `src/oc_slimapi/transform.py:238`（TransformPool admission，`max_transforms` 默认 1）；全仓清点同类敏感位仅两处——另一处 `src/oc_slimapi/actions.py:541`（ActionRegistry admission，同模式同区间）。`_aggregate_fanout.py`/`questions.py` 无独立 wait_for/acquire（经 `async with pool` 汇聚到 transform.py:238 单一咽喉）。
- **机制**: 取消已唤醒的 `Semaphore.acquire` 丢许可——gh-90155 修复入 3.12.0 并**回移 3.11.5**；wait_for 基于 asyncio.timeout 的重写（gh-96764）在 3.12 消灭 bpo-42130/43389 一族。⇒ 敏感区间 **3.11.0–3.11.4**（及 ≤3.10.12）。`max_transforms=1` 下一次丢失 = 池永久 busy → skeleton 路由全 503 到重启。
- **生产裁决**: `docs/operations.md:33` 记载生产实测 **Python 3.14.4**（deploy unit 用项目 venv）→ 当前不可触发。本机仅 3.13（亦免疫；120 轮 hammer HELD）。
- **残余风险**: 未来在 Debian bookworm（system 3.11.2）等环境重建 venv 即落入敏感区间。
- **Recommended Fix**: `requires-python` 提升至 `>=3.11.5`（一行）或 operations.md 增部署注意。
- **Adversarial Review**: DOWNGRADED 成立依据：版本事实（gh-90155 回移 3.11.5 / gh-96764 3.12 重写）+ 生产版本记载 + 本机无法复现（无 3.11 解释器）。

### BE-003 — 读组路由 query 中原始 `#` 被 httpx 当 fragment：静默语义截断

**Severity**: P3（低-中）｜**Confidence**: 高（端到端复现）｜**Verdict**: REPRODUCED（端到端，对抗复核 CONFIRMED）

- **Affected Component**: `src/oc_slimapi/routes/_read_passthrough.py:164` `_raw_upstream_url`（覆盖全部 9 组 §10.a 读组与 `/session/{sid}` 族）；`src/oc_slimapi/routes/file_raw.py:125`。
- **Trigger**: 请求行 query 携带未编码 `#`（如 `?v=4&path=a#b&extra=1`）。
- **Execution Path**: query bytes latin-1 解码 → f-string 拼 `/file?...&path=a#b...` → httpx URL 解析 → `#b&extra=1` 成 fragment → 上游只收到 `path=a`，`extra` 整体丢失。
- **Evidence**: `.audit-scratch/agent3_upstream_failures.py` raw-ASGI 直发 `query_string=b"v=4&path=a#b&extra=1"` → mock 上游收到 URL `http://127.0.0.1:4096/file?path=a`。100%。
- **Impact**: 合规 HTTP 客户端（OkHttp/curl）会编码 `#` 为 `%23`（正常逐字转发）——仅非合规客户端可触发，但后果静默且方向错误（查错文件/丢参数），违背 §5.2 "byte-verbatim" 精神。
- **Recommended Fix**: URL 构造改 `params=` 或对 query 做 fragment 字节防御（`#` 重编码为 `%23`）。
- **Adversarial Review**: **CONFIRMED（不可降级为不可达）**。决定性实验（`agent4_a32_uvicorn_hash.py`）：生产安装无 httptools → **h11 即生产解析路径**；裸 h11 probe 原样接受 `/slimapi/file?v=4&path=a#b&extra=1`（无拒绝无编码）；真 uvicorn 子进程 + 原生 socket 端到端：`?path=a#b&extra=1` → 上游 `GET /file?path=a`（`#b&extra=1` 全部消失，sidecar 200 掩盖）；`/vcs`、`/find/file`、`/file/raw` 三位点同样截断；编码控制组 `%23` 逐字节保真。触发需客户端未编码拼 URL（RFC 上应编码），维持低-中危。

### BE-004 — access-log 压缩 TOCTOU：active-path 单次快照 vs 过日期 fd 后开

**Severity**: P3（低）｜**Confidence**: 高（构造复现；真实前提罕见）｜**Verdict**: REPRODUCED（构造交错）

- **Affected Component**: `src/oc_slimapi/access_log.py` `compress_old_access_logs`（:484-527）+ `DailyAccessHandler.emit`（:185-210）。
- **Trigger**: 维护线程对 `_active_handler_ref.current_path` 快照后，handler 恰以**过日期** `record.created` 打开新文件（现实前提：NTP 时钟回拨类事件；正常路径 record.created 恒新鲜且跨午夜空闲场景已被 P1-25 防护覆盖）。
- **Evidence**: `.audit-scratch/agent2_breaker_accesslog.py::test_access_log_toctou`：压缩继续 unlink 被打开源文件。Windows 上 unlink 抛 PermissionError（被捕获）→ `.jsonl` 与 `.gz` 双存；**Linux（生产）unlink 成功 → handler fd 指向孤儿 inode → straggler 行从可见文件消失**。
- **Impact**: 观测完整性（单行级 straggler 丢失或归档重复），非服务可用性问题；下一 tick 自纠正。
- **Recommended Fix**: 压缩循环内 unlink 前二次校验 `current_path`（TOCTOU 窗口收窄到微秒级）或压缩与 emit 同锁（_MAINT_LOCK 已存在，可复用）。
- **Adversarial Review**: **CONFIRMED 机制 / 可达性收窄至接近不可达**。`record.created` 恒新鲜 → 唯一途径是**单次维护 tick 内部**发生跨午夜时钟回拨（tick 之间回拨则 `today=date.today()` 循环内现算、旧文件被跳过，无洞）；维护间隔默认 3600s（config.py:579-580）、压缩窗 ~10-100ms——触发链 = NTP step 跨午夜 × 恰落 tick 压缩窗内 × 并发 emit。确定性构造复现（`agent4_a22_toctou.py`：unlink 执行时 handler 正持有该 fd）。附带：`compress_old_access_logs` 行 485-486 注释称 `_MAINT_LOCK` 防 setup flip，实际 setup 只持 `_setup_lock`——注释失实（无可利用后果）。

### BE-005 —（降级）错误体 gzip 膨胀：行为系契约要求，缺陷仅为注释失实

**Severity**: P4（注释修正）｜**Confidence**: 高｜**Verdict**: DOWNGRADED（对抗复核：原 P3 判定撤销）

- **Affected Component**: `src/oc_slimapi/gzip_util.py:19-24` 注释；行为面 `json_response`（selector 400/405、coded 错误、health/versions 载荷）。
- **对抗复核结论**: P1-31 提交（`git show a49a4cd`）原文自认："json_response / error_response are UNCHANGED — contract §9 (P0-5) requires ALL JSON routes including small error bodies to honour gzip negotiation, and test_proxy.py asserts this"；`tests/test_proxy.py:150` 断言错误体 gzip。**行为是契约制裁且测试钉死的——改行为反而违约**。实测膨胀（46B→59B，+28%）是既定代价。
- **残余缺陷**: `gzip_util.py:19-24` 注释声称 version-gate 400 体"canonical examples"被保护返回 raw，与实际路径矛盾（该矛盾 2026-08-20 审计 e1-16 已记录过仍未修）。**Recommended Fix**: 仅修注释。
- **A1-6 同源观察**（会话 §15 小信封 63B→82B）随本降级一并归为契约制裁行为，非缺陷。

### BE-007 — config 诊断质量：int 旋钮 env 解析失败无字段名；allowlist 空条目过滤为不可达代码

**Severity**: P4（info）｜**Verdict**: STATICALLY_PROVEN + REPRODUCED

- `OC_SLIMAPI_PORT=abc` → import 时裸 `ValueError`（无字段名；`_int_env` 具名错误只覆盖 3 个旋钮）；`OC_SLIMAPI_DIRECTORY_ALLOWLIST="/a:"` → validate 空条目 RuntimeError（fail-fast 达成，仅诊断质量）。路由侧 `[e for e in allowlist if e]` 空条目过滤因此不可达。`.audit-scratch/agent3_*` 直测。

### BE-008 — 契约文档缺陷：§10.3 并列注释与实现/自身伪代码矛盾

**Severity**: P4（文档）｜**Verdict**: STATICALLY_PROVEN

- `v4-contract.md` §10.3 括号注释"同时间戳并列防御性不报"与实现（严格元组比较 `(created, id)`，id 破并列）及其自身伪代码矛盾。实现方向在上游 `ORDER BY desc(time_created), desc(id)` 分页下**可证合理**（含并列场景，agent1 S3 复现支撑）——应修文档文本而非代码（契约权威规则）。

### BE-009 — file/raw 4xx "verbatim 透传" 实际新增 `Cache-Control: no-store`

**Severity**: P4（info）｜**Verdict**: REPRODUCED

- `routes/file_raw.py` Gate-MAJOR-2.2 刻意加 no-store；status/body/头白名单保真。对 §19 "verbatim" 措辞的字面偏离，行为更保守无害，代码内有自文档。建议契约措辞补一句豁免。

### BE-010 — 错误体形状碎片化（各处单独契约合规，客户端无法单轨编程）

**Severity**: P4（info）｜**Verdict**: REPRODUCED

- 同语义多形状：`{"detail":[...]}` FastAPI 422（file 缺 path 等）vs `{"code":...}`；413 字段 `limitBytes`（providers，§12.5.3 冻结）vs `limit`（file/raw 等）；session 404 上游逐字（§13.2 native fallback）vs `session_not_found`（thin 路由）。每处均契约冻结，但建议给 ocdroid 汇总一张「按路由的错误形状表」（可放 CLIENT_CHANGES.md）。

### BE-011 — merged 模式提交快照的非确定性 → 跨运行伪 `changed` 波动

**Severity**: P4（info）｜**Verdict**: STATICALLY_PROVEN（机制）

- `_merge_fulls` fan-out 预算/降级使同页不同请求的 canonical 快照可不同 → 下一轮 since-diff 把降级差异报为 changed。无 removed 假阳性风险；客户端调和路径同 BE-001 缓解。仅 merged 模式 + 高压降级时可见。

### BE-012 — 重复消息 id（上游自违反 PK）下 diff 保守假阴性 + 自愈

**Severity**: P4（degenerate-input）｜**Verdict**: INCONCLUSIVE（上游 id 唯一，不可达前提）

### BE-013 —（已终裁）`permissions.py:63` docstring 称 /question 返回 `{"pending":[]}` 系陈旧文档

**Severity**: P4（文档）｜**Verdict**: STATICALLY_PROVEN（上游源码终裁）

- 实现（`_aggregate_fanout.py` 按 `isinstance(payload, list)` 判定）与自测（mock 裸数组）一致且**正确**：opencode v1.18.21 `packages/opencode/src/question/index.ts` 的 `Question.Service.list()` 返回 `Array.from(pending.values(), (x) => x.info)`——裸数组，无 `{pending:[]}` 包装（HTTP handler 为纯透传）。docstring 陈旧，修文档即可。

## Data Integrity Review

**验证稳固**（实验证据）：since-diff 正常路径（changed=新增/指纹变化；removed 仅在窗口耗尽或复合键严格高于边界时报告，方向在上游 desc(time),desc(id) 分页下含并列可证合理）；CAS 谱系（进程单调 generation 无重用/ABA，完整 commit 分支矩阵合契约，retained_bytes 记账一致，一切失败路径自愈为 reset）；orjson OPT_SORT_KEYS canonical 进程内全维度确定（float/int/2^60/-0.0/Unicode 键序；NaN/Inf 解析期拒绝）；ETag/coding 标注/gzip judge 矩阵（单候选 304 判定、coding 精确标注、file_raw 按实际响应头标注）；dbaux keyset 分页严格无重无漏（含并列 + 坏 JSON 行、LIMIT+1 完成判定、前缀 allowlist 无兄弟吞噬、ESCAPE 搜索）；file_raw 二进制字节精确透传 + 预算；批量扇出 all-or-nothing（部分上游失败绝不产生混合载荷，cap 优先于缺失）；信封键序冻结/重置族无 removed/before 页无键/cq_hash 轴隔离；时区单一（本地时区一致分桶，快照单采样点）。

**缺口**：BE-001（退化行假阳性 removed——唯一契约级不变量违反）；BE-008（契约文本与实现矛盾）；BE-011/BE-012（degenerate 输入下的保守性/自愈观察）。

**SQLite 写域红线**：静态核查 dbaux 打开路径（mode=ro URI + query_only PRAGMA 只读设置）、全仓无 DDL/DML 写入上游业务库的代码路径；`.audit-scratch` 临时库均为测试自建。红线保持。

## Concurrency / Async Review

**验证稳固**（混沌交错实验）：leased singleflight 账本不变量（`leased_bytes == Σ reserve{in-flight,grace,retained}`，200 轮随机取消/失败/shutdown 中途/关闭后复用，归零收敛）；plain 保留界 + call_later 定时器无泄漏 + 延迟完成不重记账；Lease 调用点纪律（4 处 `fetch_or_bypass` 全部无泄漏释放路径，STATICALLY_PROVEN）；`collect_with_byte_budget` 取消传播（在用 worker 亦被取消，无 exception 泄漏）；`offload_strict` 许可精确一次转移（40/40）；注册表退订幂等 + token 溢出 terminal 对（v3 resync+STOP / v4 STOP-only）；CatalogCache 生成栅栏（失效中航班不落死代存储，领导者仍获服务）；ReplayLog 窗口连续性/字节界/栅栏语义（120 轮随机交错）；生命周期风暴收敛（双账本归零、flush 循环随最后 detach 停、hub 空闲移除后零存活任务）；LIFO 停机链跨组件约束全部满足；IncarnationStore tmp+fsync+os.replace 原子（partial write 读到旧值或新值，绝不截断）。

**缺陷**：BE-002（僵尸 hub 生命周期间隙——本轮唯一真实并发缺陷）；BE-004（维护线程 TOCTOU，观测完整性）；BE-006（3.11 条件性许可丢失）。

**事件循环阻塞点**（有界、已接受但列出）：access-log emit 每请求 write+flush（P1-2 设计）；snapshotter 每 tick 同步写+prune glob（默认 300s）；启动同步 migrate/compress/prune + IncarnationStore fsync（延迟 ready 非服务）；停机期 `Event.wait` 阻塞 ≤10s/5s（systemd 60s 覆盖）。

**资源上界**：共享 httpx 客户端 32 连接 + 5s 池超时——超 32 并发上游流量退化为 PoolTimeout→503（有界等待，非死锁）；network_sem 仅工厂期持有，与 transform admission 无环。

## API / Failure Handling Review

**契约一致性总评：高。** selector 状态表（§2，13 边角全吻合）、directory 消费矩阵（§5）、错误优先级链（§8.3）、file/raw 错误族（§19）、questions/permissions envelope（§10.1）、读组两级错误制（4xx 逐字 / 5xx→503）与 catalog 族（4xx→502 upstream_http_N）在 40+ 复现用例中全部按契约。上游连接拒绝/超时统一 503 `upstream_unavailable`；413 截断稳定；3xx 不跟随逐字透传。SSE 失败模式健康：首连失败订阅者立即得 meta 后保持连接（hub 退避重连 + journal WARNING），连接丢失后 20ms 级 `resync{reconnect_no_replay}`，无无声挂起。POST 等效动作族（§16）激活态正确（405 面已关）。

**注意项**：BE-003（`#` 截断，唯一语义级 API 缺陷）；BE-005/BE-009/BE-010（形状/膨胀碎片化）；/ready 与 /health 无 `?v=4` 即 400（契约如此，但外部探活工具易踩坑——建议 operations.md 明示）；catch-all 404-for-405 是契约冻结的 RFC 偏离（迁移点已声明）；HEAD 探活全域不可用（selector 非 GET → 405）。

## Performance Review

只报告有证据的问题：错误体/小信封 gzip 膨胀（实测 +13~28%，46B→59B / 63B→82B）——经对抗复核确认系契约 §9(P0-5) 制裁行为而非缺陷（BE-005），列于此处供流量视角知悉；gzip 线字节嵌 mtime（≥1s 分离，信息性；不影响 ETag——验证器只哈希 identity 字节）。未发现 N+1、无界查询或热路径算法问题：dbaux keyset 分页每请求固定窗口 LIMIT+1；list 路由单上游 GET + 单 offload；merged fan-out 预算封顶且有静默降级。事件循环上的同步 IO 均有界且低频（见并发 review 列表）。

## Observability Review

**强**：request_id 链路完整（32/32 请求行零缺失，含 selector 400/catch-all 404/SSE lifecycle；非法入站值 fail-closed 换新；上游转发透传）；access log 维度与 §9.1 冻结枚举吻合；SSE open/close 以 lifecycleId 配对、孤儿 close 不冲减存量的对账模型文档化；5xx burst watcher 实测触发（含路径分布，synthetic-5xx 排除守卫有效）；metrics 与 ledger 同源、重置语义（bootTs/runId 分段）有文档；dbaux 状态机事件 vs 每响应计数分离。

**弱**：(1) hub 首连失败期订阅者零显式信号（无错误帧、心跳照常）——上游不可用只能靠 journal 侧 WARNING 发现，建议运维手册写明「订阅静默 + journal 重连告警」对应关系；(2) config 大多数 int 旋钮解析失败无字段名（BE-007）；(3) 杂项文档/注释漂移（均不影响 wire 行为，但误导维护者）：permissions.py `{pending:[]}` 表述（BE-013，上游源码终裁为陈旧）、`gzip_util.py:19-24` P1-31 注释失实（BE-005）、`compress_old_access_logs` `_MAINT_LOCK` 注释失实（setup 实持 `_setup_lock`，BE-004 附带）、`_read_passthrough` Vary 双值注释过时、allowlist 空条目过滤不可达代码。

## Maintainability Observations

- 4.11.0 since 家族单 commit 引入（~千行级）且当日发版——测试覆盖广但缺"契约不变量负例"（退化行 fixture 全设置 created）；建议对契约 §10.3 每个不变量句建立负例测试索引。
- 文档-实现漂移点已列（BE-008/BE-009/permissions 注释/Vary 注释）——契约权威机制健全，但缺"契约断言 → 测试"的双向追溯。
- `.audit-scratch/` 的 30+ 复现脚本建议处置：挑 BE-001/BE-002 的转为正式 regression test 后整目录删除（见 Remediation）。

## Rejected Hypotheses（有证据的反证，抽样列举）

| 假设 | 反证证据 |
|---|---|
| orjson canonical 不稳定（float/int/键序/往返） | 12 维探测全稳定（agent1_a） |
| SinceCache CAS 有 ABA/generation 重用/记账漂移 | 12 探针全过 + 全调用点在事件循环（agent1_c） |
| singleflight 账本可在取消交错下漂移 | 200 轮混沌 HELD（agent2_sf_ledger/plain） |
| `wait_for(semaphore)` 丢许可（**3.13 上**） | 120 轮 HELD@3.13.15（版本条件性 → BE-006） |
| fanout cancel-and-await 逃逸在用 worker | 实测在用 worker 亦被取消（agent2_fanout_cancel） |
| SSE 退订/溢出有双断言或 QueueFull 逃逸 | 幂等守卫 + terminal 对实测（agent2_sse_misc） |
| ReplayLog 并发 append/sweep 破坏窗口连续性 | 120 轮 HELD（agent2_replay_stress） |
| selector 与契约状态表漂移 | 13 边角逐格吻合（agent3, A3-18 REFUTED） |
| allowlist 空列表静默放行 | 空→403 fail-closed 实测（A3-12） |
| request_id 有缺失行 | 32/32 全覆盖（A3-14，正向反证） |
| metrics 重置语义未文档化 | traffic-accounting.md §284-348 明示（A3-16） |
| LatencyBreaker 跨线程共享致 crash | 70 万交错无 crash，GIL 原子（A2-3，cosmetic 保留） |
| 批量扇出部分失败产生混合载荷 | all-or-nothing 实测（A1-9） |
| 错误体 gzip 膨胀是产品缺陷 | 证伪：契约 §9(P0-5) 明文要求 + test_proxy.py:150 钉死（对抗复核降级 BE-005 至注释修正） |
| fanout/questions 有独立 wait_for/acquire 咨询点 | 证伪：全仓清点仅 transform.py:238 + actions.py:541，其余经 `async with pool` 汇聚（A4-T2） |
| before-present 页 artifact 层 removed 可达 | 证伪：since+before → 400，路由不可达（agent4 E6） |

对抗复核另对三个 REFUTED 做了加严抽查，全部维持：singleflight 账本（grace 窗内 shutdown + 同 key re-flight + 跨 shutdown 迟释放 ×100 轮新种子，HELD）；SinceCache CAS（60 种子 × 300 随机操作模型 hammer，HELD）；selector 边角（真 uvicorn 重放 4/4 契约吻合）。

## Inconclusive Investigations

1. BE-012（重复 mid degenerate 输入）：上游 PK 约束下不可达，保守假阴性 + 自愈，维持观察。
2. ~~permissions.py `/question` 形状~~ → **已终裁**（BE-013）：上游 v1.18.21 源码返回裸数组，docstring 陈旧。

（原 BE-INCONCLUSIVE-1 经 GitHub v1.18.21 上游源码终裁关闭：`packages/opencode/src/question/index.ts::list()` 返回裸数组。）

## Coverage / Blind Spots

- **全量测试套件未在 Windows 跑完**（平台伪影 + 速度）；权威基线仍是 Linux 侧 `./scripts/check.sh`。目标子集 1720/1727（7 伪影）绿。
- **opencode 上游源码快照不在本机**，但三项关键上游断言已用 GitHub **v1.18.21** 现场源码终裁：`session.ts`（time.created 必填 → BE-001 触发前提收窄）、`message-v2.ts:80`（DB blob 无再校验）、`question/index.ts`（裸数组 → BE-013 关闭）。分页排序方向仍基于仓库内冻结审计笔记（docs/audits/2026-08-20）。
- **生产解释器版本已有记载**（operations.md:33：3.14.4）→ BE-006 条件性已裁决为当前免疫；但该记载非机器强制（requires-python 仍 >=3.11），未来重建 venv 有回归敏感区间的可能。
- stunnel/mTLS 层、真实网络时序（毫秒级 unwind 窗口的生产命中率）、长时间内存曲线（TokenStream budget 上界的真实余量）未覆盖。
- actions 子系统在 Windows 完全不可测（权限门伪影），其 Linux 侧行为本轮仅静态审阅。

## Prioritized Remediation Plan

| 优先 | 项 | 动作 | 工作量 |
|---|---|---|---|
| P2-1 | BE-001 | `_boundary_key` 退化行返回 None + 退化行沉底排序 + 负例回归测试 | 小（~20 行 + 测试） |
| P2-2 | BE-002 | `ensure_upstream` 加 `task.cancelling()` 重生分支（或移除放弃路径重调 ensure；`done_callback` 取消分支重建）+ 并发窗口回归测试（控制面+token 两入口） | 小 |
| P3-1 | BE-003 | `_raw_upstream_url` 改 params= 构造或 `#`→`%23` 防御 | 极小 |
| P3-2 | BE-004 | 压缩 unlink 前二次校验 current_path | 极小 |
| P3-3 | BE-006 | `requires-python` 提至 `>=3.11.5`（或 operations.md 部署注意） | 一行 |
| P4 | BE-007..BE-013 | 文档/注释修正：§10.3 并列注释（BE-008）、permissions docstring（BE-013）、gzip_util P1-31 注释（BE-005）、`_MAINT_LOCK` 注释（BE-004 附带）、Vary 双值注释；+ 错误形状表（给 ocdroid）+ 运维手册补记（探活带 ?v=4、订阅静默↔journal 告警） | 文档级 |

## Workspace Integrity

- 分支 `main` @ `e3a3002`，`git status` clean（tracked 零改动）。
- 新增未跟踪：`.audit-scratch/`（40+ 个审计复现脚本：agent1_*/agent2_*/agent3_*/agent4_*）与 gitignored `.venv/`（本机新建）。`docs/automatic/20260822-1701_backend-reliability.md` 为本报告。
- src/tests/docs/specs 在审计期间零写入（各 agent 纪律确认）。
- 建议处置：BE-001/BE-002 转正式回归测试后删除 `.audit-scratch/`；报告文件可留档或按仓库惯例处理。

---

*审计由 4 个并行子 agent + 主 agent 对抗复核流水线完成；所有 REPRODUCED 结论至少一次独立重跑。对抗复核（agent4）对 6 项交付了攻击结论：BE-001/BE-002/BE-003/BE-004 CONFIRMED（含 token 路径补证、真 uvicorn 端到端、可达性收窄），BE-005/BE-006 DOWNGRADED（契约制裁行为 / 生产 3.14.4 免疫），三个 REFUTED 抽查加严后全部维持；另经 GitHub v1.18.21 上游源码关闭 BE-INCONCLUSIVE-1（→BE-013）。上游源码与 CPython issue 依据：`packages/schema/src/v1/session.ts`、`packages/opencode/src/session/message-v2.ts`、`packages/opencode/src/question/index.ts` @ tag v1.18.21；gh-90155（回移 3.11.5）、gh-96764（3.12 wait_for 重写）。*
