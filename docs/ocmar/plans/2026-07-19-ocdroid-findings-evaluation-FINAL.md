# FINAL — ocdroid 评审修复 + v1 补齐 + SSE 生命周期修复（完成 checkpoint）

> **用途**：context compaction 后的 resume 入口。本批已**全部完成 + 双门控 PASS**。读此文件即可生成总结报告 / commit / 处理 follow-up，无需重读过程产物。
> **状态**：✅ **SHIP-ready**。190 tests green（live rerun）。未 commit（全部在 working tree，base `9373550`）。
> **日期**：2026-07-20｜**slug**：`ocdroid-findings-evaluation`｜**base**：`9373550`

---

## 0. 最终结论：SHIP ✅

**双门控全 PASS**：
- **Gate 1 独立 verifier**（`_priv-verifier` live rerun，清 `__pycache__` 禁缓存）：`EXIT=0, FAILURES=0, 190 passed`。日志 `/tmp/ocmar-verify-sse-fix.log`。
- **Gate 2 final rev-cgpt 整支 + SSE 验证**（`rev-13`）：整批 SHIP + 3 个扩展修复（SSE teardown / queued_bytes ack / G1 error.name）正确 + 3 集成测试真锁定 blocker + impl-status 同步（190/G7-soft）+ 无新问题。

## 1. 完整交付范围（original + expanded）

### 原始 scope（用户最初确认）
- **F1** `/questions`+`/permissions` directory 可选（null=聚合 allowlist）。
- **F2** per-session `/sessions/{sid}/status` 放宽 allowlist（sid 自洽；批量 status 不变）。
- **F3** allowlist 启动暖机（lifespan `warm_allowlist`）+ routeToken `_token` 走 `require_directory`（miss 自动刷新）。
- **F4** `CLIENT_CHANGES.md` SSE 节重写（删过期 query 参数）。
- **F5** 契约 §1 `accepted:[1,1]` 闭区间说明。
- **§5** 契约新增 directory 三态表 + allowlist 机制节 + cold-start 暖机 + 同步纪律。
- **G1** 错误可见性：`session.digest` 加 `lastError?`（三态 + sticky + busy clear + deleted 省略）+ 新 `event: session.error` session-less 帧 + `MessageAbortedError` 过滤 + message 脱敏（path/stack/secret/截断 512）。
- **G6** 批量展开 `GET /slimapi/messages/{sid}/full?ids=`：chunk-ledger 累计预算 + mid 级 envelope errors[] + discover 先行 + 网络 503 优先 413。
- **D1–D8** 文档同步（design-v2 §1.4/1.7/1.9/1.10/§3/§1.13 + impl-spec B0 GO + G1/G6 标已实现 + AGENTS v1.18.3 + 契约 §11 closed）。

### 扩展 scope（终审发现，用户批准一并修）
- **🔴 SSE teardown registry 配对**：`events.py` teardown 从 `GlobalHub.unsubscribe`（不减计数）改走 `HubRegistry.unsubscribe`（减 `total_subscribers`）——修 pre-existing 计数泄漏（达上限永久 503）。
- **🔴 `queued_bytes` 消费扣账**：新增 `Subscriber.ack()`（镜像 `put` 的 size 计算），`events.py` 消费时扣——修 pre-existing 健康消费者累计误判背压。
- **🟠 G1 `error.name` 类型防御**：coerce 非字符串 name→None，防 TypeError 逃出 publish 触发 SSE 重连。
- **config 校验**：`sse_queue_items >= 2`（保证 overflow 的 resync+STOP 都能入队）。
- **G6 2xx 坏 JSON** → envelope `upstream_error`（wrap `orjson.loads`）。

**全部加性，不 bump `X-Slimapi-Version`（仍 `1`，`ACCEPTED_CLIENT_VERSIONS==(1,1)`）。**

## 2. 关键决策（执行中确定，报告须记）

| 决策 | 理由 |
|---|---|
| **共享 tree 并行**（非 worktree） | 核验发现测试 bypass lifespan，flux 风险可忽略；避免 worktree venv 开销 |
| `load_projects`→`load_products` rename | codebase 原名 load_projects，plan/测试用 load_products |
| G6 mid **5xx → envelope** `upstream_http_N`（整请求 200） | 非 whole-request 5xx；与 envelope 哲学一致 |
| G6 网络 `httpx.RequestError` → **503 优先于 413** | 网络层不可达 = 整请求失败 |
| G6 **chunk-ledger（oracle C′）** 并发预算 | `aiter_bytes` 逐 chunk 同步扣账（无 await 夹入 check 与 charge）；防超限 + 不欠取 |
| G6 `message_too_large` **触发 chunk 计入 ledger** | rev-5 catch；oracle intent：已读字节不回滚 |
| G1 显式 `props.get("sessionID")`（**禁** `_extract_session_id`） | 后者回落 evt_id 误当 sid |
| G1 复用 `_now_ms()`（非 `time.time()`） | 既有 helper |
| `lastError` 省略条件 | 无新对象 + 无 clear + **且无 sticky**；deleted 强制省略 |
| SSE 背压 = **clear→resync{subscriber_backpressure}→STOP** | 非「丢最旧续发」（pre-existing 文档 bug 顺带修） |
| **reviewer 出错处理** | rev-3 regex 误读（`_` 其实在字符集）→ 用代码+实测否决；rev-10 空 completion → resume 重发 |

## 3. 测试证据

- **190 passed**（base `9373550` → working tree），EXIT=0，FAILURES=0。
- 独立 verifier **live rerun**（清 `__pycache__`，确认非 FROM-CACHE）。
- 关键新测试（锁死各修复，旧实现必 FAIL）：
  - G6：barrier 并发 TOCTOU（4×40KiB/cap64KiB→413，`mid_calls==4`）/ 累计预算 / `message_too_large` charges ledger / 2xx 坏 JSON→`upstream_error` / 网络→503 / discover 坏 JSON→503 / 路由顺序静态 / 全 mid 404 仍 200 / items 去重保序。
  - G1：sticky 跨窗口 / busy clear / deleted 清除 / abort 过滤 / sanitize golden（含 `access_token`/`refresh_token`/`client_secret`）/ 非字符串 name 不崩。
  - SSE：`test_events_teardown_releases_registry_slot`（5 轮 connect/aclose，`total_subscribers` 回落 0）/ `test_subscriber_queued_bytes_decrements_on_consume` / config `queue_items=1` 拒绝 / `queue_items=2` 接受。

## 4. 改动文件（未 commit）

**Code (7)**：`routes/sessions.py` / `routes/questions.py` / `routes/messages.py` / `routes/events.py` / `sse/hub.py` / `app.py` / `config.py`。
**Tests (4)**：`test_sessions_routes.py` / `test_questions_routes.py` / `test_messages_routes.py` / `test_hub.py`。
**Docs (8)**：`docs/{v1-contract, design-v2, v1-impl-spec, CLIENT_CHANGES, INTERFACE_MAP, v1-contract-implementation-status}.md` + `AGENTS.md` + `CHANGELOG.md`。

## 5. 已接受限制（defer，非阻塞）

- **G1 脱敏边缘**：Bearer-no-space+等号 / stack regex 对自然语言误剥（`look at home:5`）/ path 带空格 Unicode 覆盖窄。（defense-in-depth on loopback，非主边界）
- **G6 mid body 形状错误**（合法 JSON 但非 MessageWithParts）：未 envelope 映射（保持 500）。仅 JSON 解析错映射 `upstream_error`。
- **G1 deleted flush 后迟到 `session.error`**：可能重建 entry（无 durable tombstone）。
- **T1 `warm_allowlist` 无日志**：cold-start 可观测性弱（不影响正确性）。
- **rev-13 🟡 维护项**：(a) 加端到端 events body iterator 的 ack 测试（现测试直接调 `ack()`，未来若删 `events.py:46` 不会捕获）；(b) `test_hub.py:369` 注释滞后（提 `hub.unsubscribe`，实际 `_MockHubs.unsubscribe` 委托）。

## 6. 总结报告生成清单（post-compaction）

写 `docs/ocmar/reports/2026-07-19-ocdroid-findings-evaluation.md`（`write_ocmar_review` kind=final，slug=`ocdroid-findings-evaluation`）：

1. **标题 + 元数据**：日期 2026-07-20，slug，base 9373550，SHIP verdict。
2. **交付范围**：用本文件 §1（original + expanded）。
3. **关键决策**：用本文件 §2 表。
4. **验证证据**：190 passed（live rerun）+ 双门控（verifier + rev-13）+ 关键测试列表（§3）。
5. **改动文件**：§4。
6. **已接受限制**：§5（诚实声明，含 G6 shape 500 / SSE 真实 HTTP streaming 未覆盖 / sanitize 边缘）。
7. **残留风险（发布后关注）**：G6 真实 HTTP streaming 早停/取消行为（MockTransport 未完全证明）；allowlist warm 无日志；deleted 后迟到 error；SSE 修复后监控 `sse.subscribers.current` 回落 / `bufferBytes` 随消费下降 / 健康消费者不触发 `subscriber_backpressure`。
8. **发布建议**：未 commit；用户决定是否 commit（`git add` 相关文件 + commit message 参照 CHANGELOG `[Unreleased]`）；不 bump wire；发版走 `scripts/release.sh`（用户决定 patch/minor）。

## 7. Resume 触发词

- 「**生成报告**」/「**继续**」→ 按本文件 §6 写总结报告 → `notify_task_done` 通知。
- 「**commit**」→ `git status`/`git diff` 核查 → `git add` 相关文件 → commit message 参照 CHANGELOG（不 commit secrets/.venv）。
- 「**发版**」→ `./scripts/release.sh patch|minor`（走 docs/release.md，不自创命令）。
- 调整任何文件 → 先读本文件 §4 写域 + §5 限制。

## 8. Reusable sessions（post-compaction follow-up 可复用）

- `ora-1`（oracle T7 budget design）、`rev-7`/`rev-8`（rev-gpt T4/T6）、`rev-12`（rev-cgpt cross-doc）、`rev-13`（rev-cgpt 整审+SSE 验证）、`fix-15`（config queue_items）、`fix-16`（SSE lifecycle+G1）、`_pr-1`（verifier）。

---

> **compaction note**：本文件 + 既有 `docs/ocmar/plans/2026-07-19-ocdroid-findings-evaluation-HANDOFF.md`（pre-execution checkpoint）+ spec + plan 四份足以往后恢复全部状态。所有决策（§2）、证据（§3）、限制（§5）、报告清单（§6）已 capture。无需 re-dispatch 任何 explorer/oracle。
