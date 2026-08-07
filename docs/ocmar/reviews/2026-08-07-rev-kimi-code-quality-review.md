# rev-kimi：oc-slimapi 全面代码质量评审

**日期：** 2026-08-07
**评审基线：** `main @ b38fd4e`（v1.1.3，工作区干净）
**评审方：** rev-kimi (k3) · session `ses_0243eebddffe3qrtSEI2CtAN6j`
**范围：** `src/oc_slimapi/` 全部 40 个源文件 + `tests/` + `scripts/` + 契约/文档对照
**评审维度：** 代码质量、模块化、可维护性、冗余与死代码、安全与运维
**结论：** **可上线，无前置阻塞。** 代码层无 P0/P1 正确性与安全问题；建议下次发版前修掉 4 项 P1 文档/一致性缺口。

> 本文档供 handoff 使用。文末附「Handoff 行动清单」，可直接派工。

---

## 1. 总体评价

**整体健康度：高。** 这是一个经过多轮评审锤炼、纪律性极强的代码库。

**最突出的优点：**

1. **契约纪律真实落地**——`v2-contract.md` 与实现逐点同步（版本门禁 `(2,2)`、错误码集、`X-Next-Cursor` 逐字节透传、questions envelope、catalog skeleton 均验证一致），`CHANGELOG.md` 对每次行为变更记录了根因与上游源码确证位置。
2. **资源防线（T3）设计深度罕见**——live/pending 内存双预算、handshake/runtime 队列物理分离、fail-loud 溢出、配置静态断言（`config.py:113-127`），内存算术与 `MemoryMax=384M` 闭环。
3. **错误面高度一致**——`CodedHTTPException` 结构化错误贯穿 thin 路由与 catch-all，上游 4xx/5xx/网络/坏 JSON 的映射规则统一且中间流错误也被包裹（`sessions.py:64-105` 等）。

**最紧迫的风险**不在代码正确性，而在：
- 面向集成方的**文档漂移**（README / INTERFACE_MAP / traffic 手册三处与实现矛盾）；
- 两处**一致性缺口**（questions 无内存 cap、cap-bail 时 upIn 记账顺序不一）。

---

## 2. 严重问题（P0/P1）

### P0：无。

### P1-1｜README.md「范围」节严重过期，直接误导 ocdroid 集成方

- **位置：** `README.md:39`（"messages（list·**since**·full skeleton 投影）"）、`README.md:46`（"**SSE：永不 gzip**"）、`README.md:48`（"v2 已移除 … **questions** / permissions / **since** / session children·**status** 等端点"）
- **现象：** 三条陈述均与当前实现矛盾——
  - `since` 端点 v2 已删（实现无此路由）；
  - token stream SSE **默认 gzip**（`routes/token_stream.py:35-38` lever 2，契约 §9 唯一例外）；
  - `GET /slimapi/questions` 与 `GET /slimapi/sessions/status` 已于 v1.1.0/v1.1.3 加性回归（`routes/questions.py`、`routes/sessions.py:143`）。
- **根因：** v1.1.x 加性回归时只同步了契约/CHANGELOG/INTERFACE_MAP，漏了 README。
- **建议：** 按 `v2-contract.md §2` 端点表重写「范围」节；考虑在 `check.sh` 中增加 README 关键词漂移检查（或接受人工维护）。

### P1-2｜INTERFACE_MAP.md 两处上游错误映射描述与实现/契约矛盾

- **位置：** `docs/specs/INTERFACE_MAP.md:24`（`/slimapi/messages/{sid}` 行："上游错误**原状态透传**"）、`:25`（`/full/{mid}` 行："上游 400/404/5xx **原状态/body 透传**"）
- **现象：** 实现为 404→404 `session_not_found`、其他 4xx→502 `upstream_http_N`、5xx/网络→503 `upstream_unavailable`（`routes/messages.py:292-301`、`:386-402`），契约 §7 亦如此定义。INTERFACE_MAP 的"原状态透传"是 v1 残留描述。
- **根因：** lite-v2 错误面重写时漏改这两格。`check_routes_doc.py` 只校验路由**存在性**、不校验语义，故漏检（见 P2-5）。
- **建议：** 改为与契约 §7 一致的映射描述。

### P1-3｜questions 发现调用与 per-dir fan-out 缺少 `read_with_cap` 内存防线

- **位置：** `routes/questions.py:118-124`（`/experimental/session` 发现调用，全 buffer `response.json()`）、`:279-283`（per-dir `GET /question`，同样全 buffer）
- **现象：** v1.1.2 刚为 `/slimapi/sessions` 补上流式 `read_with_cap`（CHANGELOG："无 body cap（known limitation）→ 64MiB 内存防线"），但 v1.1.0 引入的 questions 路由未享受同一硬化。发现调用 `limit=10000` 返回全量 SessionInfo，病态上游可返回数十 MB 无上限缓冲。`MemoryMax=384M` 兜底所以不是 P0，但与 sidecar 自身标榜的 T3 纪律不一致。
- **建议：**
  - 对发现调用加 `read_with_cap(config.max_response_bytes)`（超限→503 total failure）；
  - per-dir `/question` 可加较小 cap（超限→该 dir 计入 `errors[]`）。

### P1-4｜traffic-accounting 手册桶表与 `bucketize()` 实现漂移

- **位置：** `docs/manual/traffic-accounting.md:107`（`quiz` 桶含 `/slimapi/questions`）、`:109`（`projects` 桶） vs `src/oc_slimapi/traffic.py:49-91`（`bucketize` 无 `quiz`/`projects` 桶；`/slimapi/questions` 实际落入 `"other"`）
- **现象：** 按手册查 questions 省流会查不到数据（在 `other` 桶里）。
- **建议：** 要么在 `bucketize` 加 `questions` 专用桶（推荐，与 command/agent 平权），要么修手册为 `other` 并说明。

---

## 3. 改进建议（P2/P3）

### P2-1｜413 cap-bail 路径 `stash_up_in` 顺序不一致，且注释自相矛盾

- **位置：**
  - `routes/messages.py:302-309`（list：`body is None`→413 return **之后**才 stash → 超限读不计 upIn）；
  - `routes/sessions.py:74-80`（同样 raise 之后才 stash）；
  - 而 `routes/agent.py:118-131`、`routes/command.py`、`routes/messages.py:417`（/full）均**先** stash 再判 None。
- **矛盾点：** `messages.py:417` 注释明写 "counted even on cap-bail, matching the list convention"——但 list 恰恰**不**count。
- **影响：** 超限场景 upIn 漏记，省流审计口径不一致。无任何测试钉住该行为（`grep too_large tests/test_traffic_*.py` 无命中）。
- **建议：** 统一为"先 stash 后判 None"（agent/command/full 模式），并补一条 cap-bail upIn 断言测试。

### P2-2｜`routes/agent.py` 与 `routes/command.py` ~95% 逐行重复

- 两个文件各约 130 行，除上游路径（`/agent` vs `/command`）、投影函数、command 的 300s 读超时外完全同构（`_project_*_and_pack` / `_busy_response` / `_stream_upstream` / 路由体）。
- **影响：** 重复使后续错误面修复需双写，已出现漂移风险。
- **建议：** 抽取 catalog 公共骨架（参数化 path/projection/timeout），保留各自 docstring 中的省流实测数据。

### P2-3｜`TurnRegistry._turns` 无界增长

- **位置：** `turn_registry.py:171-176`（`bump_turn` 只写不删）
- **影响：** 长跑进程中每个出现过的 sid 永久驻留一个 dict 项。单项约百字节，十万 session 量级才 ~10MB，属慢泄漏；但 sidecar 其余状态（tombstone/replay queue/high-water）都有界，此处是**唯一无界点**。
- **建议：** 加 FIFO 上限（如 10k，对齐 `_LAST_UPDATED_AT_BY_SID_MAX` 模式）或随 `session.deleted` 清理。

### P2-4｜`smoke()` schema 校验逻辑无直接测试

- **位置：** `app.py:35-58`
- **现象：** `grep schema_degraded tests/` 显示测试均手工置位（如 `test_health.py:57`），没有任何测试驱动 `smoke()` 的 payload 校验分支（非 list / 缺 info.id / parts type 非 str / 异常路径）。
- **建议：** 补 3-4 个 `smoke()` 单测（MockTransport 上游）。

### P2-5｜`check_routes_doc.py` 只防"路由缺失"，不防"语义漂移"

- **位置：** `scripts/check_routes_doc.py:31-60`：仅断言路由路径字符串出现在 INTERFACE_MAP 中。
- **影响：** 本次发现的 P1-2（错误映射描述错误）因此漏检。
- **建议：** 至少对每条路由行的关键错误码（`session_not_found`/`upstream_http_`/`upstream_unavailable`/`transform_busy`）做关键词存在性校验；或明确接受其仅为存在性门禁并在文档注明。

### P3-1｜gzip 协商为子串匹配，`gzip;q=0` 边界错误

- **位置：** `gzip_util.py:18`、`transform.py:64`、各 `_project_*_and_pack`：`"gzip" in (accept_encoding or "").lower()`
- **现象：** 客户端显式 `Accept-Encoding: gzip;q=0`（拒绝 gzip）时仍会被 gzip。RFC 7231 语义违规，实际客户端几乎不会发，但属协议正确性瑕疵。

### P3-2｜`health.py` 读模块级 `settings` 而非 `app.state.config`

- **位置：** `routes/health.py:5,57`（`skeletonInlineOutputMaxBytes` 取自 import 时单例）
- **现象：** 与文件内其余字段（均走 `request.app.state.config`）不一致；测试用自定义 config 时该字段不反映覆盖值。

### P3-3｜版本门禁 400 响应不协商 gzip

- **位置：** `versioning.py:36-53` 直接 `JSONResponse`，未走 `gzip_util`
- **现象：** body 极小，仅一致性问题（契约 §9 "所有 JSON 路由"的字面覆盖范围）。

### P3-4｜入站 `?before=` 依赖 FastAPI query 解码的往返稳定性

- **位置：** `routes/messages.py:262-265`
- **现象：** Starlette `parse_qsl` 做 `unquote_plus`，httpx 再编码——对 base64url cursor 安全（不含 `+`/空格），代码注释也已声明该假设；若上游 cursor 字符集变化会静默损坏。
- **建议：** 加一条注释锚定"opencode cursor 为 base64url"的前置条件，或改用手动切 query 的方式（与 `_extract_before_verbatim` 对称）。

---

## 4. 冗余 / 死代码清单

| 位置 | 内容 | 说明 | 处置建议 |
|---|---|---|---|
| `upstream.py:62-68` | `decoded_body_headers()` | 全仓 src 零调用（`grep` 仅定义点） | **删除** |
| `logging_config.py:103-105` | `redact()` | 生产代码零调用，仅 `test_logging_config.py` 使用；`app.py:204` 注释提 "redacting" 但 banner 实无 secret | 删除或注明 test-util |
| `config.py:244-247` + `:438-441` | `access_log_max_bytes` / `access_log_backups` | RotatingFileHandler 时代残留：仍读 env、仍 validate（可致启动失败），但无任何消费方。文档已标 deprecated | 下一 minor 删除字段；至少取消 validate |
| `sse/global_hub.py:196-200,242-247` | `GlobalHub.unsubscribe()` / `stop_after_grace()` | 生产路径只走 `HubRegistry.unsubscribe` + `_remove_hub_after_grace`；hub 级方法仅测试使用 | 注明 test-only 或私有化 |
| `traffic.py:10` | docstring 引用 `oc_slimapi.observability.BatchLedger` | 该类已随 lite-v2 删除，引用悬空 | 改述为"单事件循环+锁" |
| `tests/test_traffic_upin_gaps.py:8-26` | docstring 提及 "G6 batch fetch_one" 与 "lite-v2 removed the /slimapi/questions family" | G6 已删、questions 已回归，两处描述均过时 | 更新 docstring |
| `tests/test_traffic_upin_gaps.py:42,63` | 夹具用 `X-Slimapi-Version: 1` + `accepted_client_versions=(1,1)` | 自洽但与生产 `(2,2)` 相反，易误导 | 顺手对齐 v2 |

> **注：** `sse/hub.py`、`sse/token_hub.py` 的 re-export shim 与 `frames.py`/`hub_types.py` 的 `sse_frame`/`_now_ms` 重复是**有意的**循环导入规避，文档注明，**不算死代码**。

---

## 5. 测试缺口

1. **`smoke()`**：schema 校验四个分支（非 list、缺 `info.id`、parts type 非 str、异常→degraded）无任何测试（见 P2-4）。
2. **cap-bail upIn 记账**：超限读时 upIn 是否入账无任何断言（见 P2-1）。
3. **questions 资源边界**：发现调用超限、`_fanout_sem` 并发上限（16）、per-dir 大 body 均无测试；现有 27 个 questions 用例聚焦正确性语义。
4. **gzip 协商边界**：`q=0`、大小写、`br,gzip` 组合无测试（现状为子串匹配，见 P3-1）。
5. **文档↔实现一致性**：bucketize 桶名 vs traffic 手册、INTERFACE_MAP 错误码描述无自动校验（P1-2/P1-4 均因此漏检）。
6. **`check_routes_doc.py` 自身**：无单测（其边界正则 `?![\w/]` 的假阳/假阴行为未钉住）。

> **覆盖面肯定：** 版本门禁五态、cursor 逐字节/多 token rel/大小写、turn fence（bump/冻结/abort/prompt_async/方法门）、T3 溢出终态对、retired-gate TTL/cap/move_to_end、traffic 中间件 SSE 口径均有重行为锁定测试，关键路径覆盖密度高于同类项目。

---

## 6. 不确定项（需进一步验证）

- **`/experimental/session` 上游稳定性：** 该端点位于 opencode `experimental` 命名空间，CHANGELOG 称已对照 v1.18.4 源码确证（`server-compat.ts:147` 等），但上游跨版本重命名/改语义时 sidecar 无协商机制，发现调用会整体 503。升级 opencode 对齐版本（`opencode-src/current`）时需复核。
- **`/question` per-Location 语义：** 依赖 opencode v1.18.4 快照行为；后续上游若改为全局，聚合端点语义需重审（当前为超集 fan-out，行为仍正确但可能重复）。
- **`_upstream_line_bytes` 的 CRLF 少计：** 代码注释已自承 CRLF 时每行少计 1 字节（保守偏向），上游实测为 LF，属已知可接受偏差。

---

## 7. 结论

**建议：可合并/上线，无前置阻塞条件。**

代码层无 P0/P1 正确性与安全问题；契约权威地位真实、错误面统一、T3 防线严密。建议在**下一次发版之前**修掉四项 P1 文档/一致性缺口：

1. README 范围节（P1-1）
2. INTERFACE_MAP 两处错误映射（P1-2）
3. traffic 手册桶表（P1-4）
4. questions 内存 cap（P1-3）

前三者是面向 ocdroid 集成方的事实性错误，后者是与自身 T3 纪律的一致性欠款。P2 项（stash 顺序统一、agent/command 去重、`_turns` 上限、smoke 测试）可排入后续迭代。

---

## 附录：Handoff 行动清单

> 接手人可按优先级直接派工。每条已标注文件:行与处置方向。

### A. 必修（P1）—— 建议发版前完成

| # | 任务 | 文件 | 处置 |
|---|---|---|---|
| A1 | 重写 README「范围」节 | `README.md:39,46,48` | 按 `v2-contract.md §2` 对齐：删 `since`、token stream 改"默认 gzip"、补 questions/status |
| A2 | 修 INTERFACE_MAP 错误映射 | `docs/specs/INTERFACE_MAP.md:24-25` | 改为契约 §7 的映射（404→404 `session_not_found`、4xx→502 `upstream_http_N`、5xx→503 `upstream_unavailable`） |
| A3 | questions 加内存 cap | `src/oc_slimapi/routes/questions.py:118-124,279-283` | 发现调用走 `read_with_cap(config.max_response_bytes)`；per-dir `/question` 加较小 cap |
| A4 | 修 traffic 桶表漂移 | `docs/manual/traffic-accounting.md:107,109` 或 `src/oc_slimapi/traffic.py:49-91` | 推荐在 `bucketize` 加 `questions` 桶（与 command/agent 平权） |

### B. 次要（P2）—— 后续迭代

| # | 任务 | 文件 | 处置 |
|---|---|---|---|
| B1 | 统一 cap-bail upIn 顺序 + 测试 | `src/oc_slimapi/routes/messages.py:302-309`、`routes/sessions.py:74-80`、`routes/messages.py:417` 注释 | 统一"先 stash 后判 None"，修自相矛盾注释，补 cap-bail upIn 断言测试 |
| B2 | agent/command 去重 | `src/oc_slimapi/routes/agent.py`、`routes/command.py` | 抽公共骨架（参数化 path/projection/timeout） |
| B3 | `TurnRegistry._turns` 加上限 | `src/oc_slimapi/turn_registry.py:171-176` | FIFO 上限（对齐 `_LAST_UPDATED_AT_BY_SID_MAX`）或随 deleted 清理 |
| B4 | 补 `smoke()` 单测 | `tests/`（对应 `src/oc_slimapi/app.py:35-58`） | 4 分支：非 list / 缺 info.id / parts type 非 str / 异常 |
| B5 | `check_routes_doc.py` 增语义校验或注明局限 | `scripts/check_routes_doc.py:31-60` | 关键错误码关键词校验，或文档注明仅为存在性门禁 |

### C. 协议/一致性微调（P3）—— 可选

| # | 任务 | 文件 | 处置 |
|---|---|---|---|
| C1 | gzip 协商改 q-value 解析 | `src/oc_slimapi/gzip_util.py:18` 等 | 处理 `gzip;q=0`；补边界测试 |
| C2 | health.py 走 app.state.config | `src/oc_slimapi/routes/health.py:5,57` | 与同文件其余字段一致 |
| C3 | 版本门禁 400 协商 gzip | `src/oc_slimapi/versioning.py:36-53` | 走 gzip_util（一致性） |
| C4 | `?before=` 前置条件注释锚定 | `src/oc_slimapi/routes/messages.py:262-265` | 注明"opencode cursor 为 base64url"假设 |

### D. 清理（冗余/死代码）

| # | 任务 | 文件 | 处置 |
|---|---|---|---|
| D1 | 删 `decoded_body_headers()` | `src/oc_slimapi/upstream.py:62-68` | 删除（零调用） |
| D2 | 处置 `redact()` | `src/oc_slimapi/logging_config.py:103-105` | 删除或注明 test-util |
| D3 | 删 access_log 残留字段 | `src/oc_slimapi/config.py:244-247,438-441` | 删字段 + env 读取 + validate |
| D4 | 处置 GlobalHub test-only 方法 | `src/oc_slimapi/sse/global_hub.py:196-200,242-247` | 注明 test-only 或私有化 |
| D5 | 修 `traffic.py` docstring 悬空引用 | `src/oc_slimapi/traffic.py:10` | 改述（BatchLedger 已删） |
| D6 | 更新 `test_traffic_upin_gaps.py` 过时描述 + 夹具版本 | `tests/test_traffic_upin_gaps.py:8-26,42,63` | docstring 改述、夹具对齐 v2 |

### E. 验证要求（按 AGENTS.md 硬规则）

- 所有 Python / 契约相关改动完成后必须 `./scripts/check.sh` 通过（= `pytest tests/` + 路由↔文档一致性）。
- 若涉及 wire 行为变更，编辑 `CHANGELOG.md`（加性变更不 bump `X-Slimapi-Version`，破坏性变更才 bump）。
- 文档类改动（A1/A2/A4）不触发 check.sh 的路由存在性校验，但应人工核对与契约 §2/§7 一致。

---

*评审全文（含每条结论的 git grep 证据）见 rev-kimi session `ses_0243eebddffe3qrtSEI2CtAN6j`。*
