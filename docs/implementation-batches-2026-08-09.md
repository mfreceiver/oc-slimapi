# oc-slimapi Quality Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use ocmar-subagent-driven-development (recommended) or ocmar-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不动 wire 版本（`X-Slimapi-Version` 保持 2）、不做破坏性变更的前提下，按批次修复 `docs/architecture-quality-review-2026-08-09.md` 核实的 P0/P1/P2 问题与文档漂移，补齐运维边界与资源预算，产出可被低能力 agent 独立实施并复核的精确任务集。

**Architecture:** 单进程 asyncio sidecar（uvicorn workers=1）。Composition root = `src/oc_slimapi/app.py` lifespan（AsyncExitStack 事务化接线）；thin routes 共享 `admission → cap-read → project → gzip` 链（`routes/_catalog_common.py`）；catch-all 反代在 `proxy.py`；SSE 在 `sse/`（GlobalHub 控制面 + TokenStreamHub token 面）；turn fence 在 `turn_registry.py`；变换池在 `transform.py`；日志/快照在 `access_log.py` + `traffic_snapshot.py`。约束：单事件循环、`settings` 为 frozen dataclass 全局单例、契约权威 `docs/specs/v2-contract.md`、route↔INTERFACE_MAP 防漂移 gate 由 `scripts/check.sh` 强制。

**Tech Stack:** Python `>=3.11`（`pyproject.toml` 声明；**当前核验环境为 Python 3.14.4**，本计划所有命令在该环境验证）、FastAPI + uvicorn、httpx、orjson、asyncio、pytest（`tests/`，当前 1386 passed）、systemd user unit（`deploy/oc-slimapi.service`）。

---

## A. Global Constraints（全局硬约束，任何 task 都不得违反）

1. **一次只执行一个 task**。每个 task 必须由 fresh implementer + fresh reviewer 完成；**禁止同一 agent 自审自己的 diff**；**禁止两个 writer 并行写同一文件**（文件写域重叠则串行，见 D 节批次切分）。
2. **每个 task 流程固定**：记录 baseline HEAD → 先写失败测试 → 只跑目标测试确认**红** → 最小实现 → 目标测试**绿** → `git diff --check` → 记录 diff。**不要 commit / tag / release**，除非用户明确要求。
3. **门禁**：任何 Python/契约行为改动后，本 task 与所在 batch 的 gate 必须 `./scripts/check.sh` 通过；跨批次最终 gate `./scripts/check.sh --full`。
4. **wire 纪律**：任何 wire 可见变化（含加性）必须同步 `CHANGELOG.md` 与 `docs/specs/INTERFACE_MAP.md`；**除非任务明确授权（仅 Task 13）**，禁止修改 `docs/specs/v2-contract.md`；**破坏性变更一律禁止在本计划中实施**（wire version 恒 2）。
5. **失败即停（必须上报，不得自行绕开）**：
   - 目标测试不是预期红（一开始就绿 = 测试没锁住缺陷，按 H 节处理）；
   - diff 出现**额外文件**（超出任务 Files 声明域）；
   - 无法在源码中定位任务声明的符号/行段（行号漂移时以上下文定位，仍失败则停）；
   - 现有测试先失败（baseline 红，不是本任务引入的）；
   - 需要**新增第三方依赖**；
   - 需要 **wire bump**。
6. **禁止**自由重构、禁止修相邻问题、禁止"顺手"格式化整个文件、禁止把 explorer 误报（审查报告"已证伪"附录）当作需求。
7. **当前分支是 `bundle-slimapi-actions`，不是 `main`：禁止 `scripts/release.sh`、禁止打 tag**。

---

## B. 批次顺序与门禁

| Batch | 任务 | 进入条件 | 退出条件 | 允许文件域（写域） |
|---|---|---|---|---|
| **0** | Task 0 | 无 | Task 0 全部通过 | 无（只读） |
| **1** | Task 1、3、4 并行（3 lanes）；Task 2 紧随 Task 1 之后 | Batch 0 通过 | 1/3/4 各自 gate + Task 2 通过或 BLOCKED 记录 | 见 C 节各 task Files；Task 1 的 docs/operations §4、Task 3/4 的 INTERFACE_MAP 对应行描述，以及三者 CHANGELOG，均由集成 lane 串行统一写（E 节） |
| **2** | Task 5（单独） | Batch 1 通过 | 5 全部验收 + 测试设计 gate | config.py / app.py / routes/questions.py + tests + docs |
| **3** | Task 6、8、12 并行（3 lanes）；Task 7（设计文档）依赖 Task 6 结论 | Batch 2 通过 | 6/8/12 各自 gate；Task 7 产出文档且 reviewer 通过 | 6: access_log.py；8: skeleton.py + routes/messages.py；12: transform.py；7: docs/specs/（新文件） |
| **4** | Task 9、10、11（**串行**，因 app/config/docs 写域重叠，禁止并行） | Batch 3 通过 | 9/10/11 各自 gate | 9: config.py+app.py+turn_registry.py+unit；10: config.py+traffic_snapshot.py+access_log.py+unit；11: actions.py |
| **5** | Task 13、14（**串行**） | Batch 4 通过 | 13/14 各自 gate | 13: docs/specs/v2-contract.md + CLIENT_CHANGES.md + operations.md + manual/traffic-accounting.md；14: scripts/check.sh + .github/workflows（审批后）+ docs |
| **6** | Task 16 → Task 17（严格顺序） | Batch 5 通过 | 16 报告产出或 BLOCKED；17 spec 产出 + 用户批准 | 16: docs/（只读聚合，产出报告）；17: docs/specs/（只写 design，不写实现） |

每 batch 末必须执行 **E 节 Integration Gate**；任一 FAIL 不得进入下一 batch。Task 15 只产出后续计划入口，**不进入任何实施 batch**。

---

## C. 任务定义

> 约定：`PY=.venv/bin/python`；目标测试命令统一为 `$PY -m pytest -q <file>::<test>`；行号以 2026-08-09 HEAD（`216ff0b`）为准，漂移时按符号定位。
> 验收标准 ID 为稳定标识 `T<N>-C<M>`，唯一 owner 见 F 节矩阵。

### Task 0：Baseline evidence（只读）

- **目标**：固化执行起点，任何后续 diff 的对照基线。**未来执行时的 HEAD 可能不同于计划基线**（本计划成稿于 `216ff0b`）；**不要求执行时 HEAD 等于 `216ff0b`**，但必须核验两者差异。
- **Files**：无（不创建、不修改任何文件）。
- **Interfaces**：Consumes：`git`、`./scripts/check.sh`。Produces：证据记录（写入本 task 执行者的工作记录，不落仓）。
- **禁止修改**：所有文件。
- **Acceptance Criteria**：
  - `T0-C1`：`git status --short` **无未提交改动**。工作树可包含本计划两份已跟踪文档（本文件与审查报告）；除这两份文档外无任何未提交改动。
  - `T0-C2`：**记录实际 `git rev-parse HEAD`（不硬编码 `216ff0b`）**，并核验从计划基线（`216ff0b`）到实际 HEAD 之间的 `git diff` 已被审阅（确无与本计划冲突、已落地的改动）。
  - `T0-C3`：`./scripts/check.sh` 全绿（pytest + route↔INTERFACE_MAP gate）。
- **步骤**：
  - [ ] 记录 `git status --short`、`git branch --show-current`、`git rev-parse HEAD`。
  - [ ] 若存在未提交改动（除本计划两份已跟踪文档外）→ **停**（按 A5 上报，不继续）。
  - [ ] 若 `HEAD != 216ff0b`：运行 `git log --oneline 216ff0b..HEAD` 列出增量 commit，逐一核对已审阅（或已在本计划中预期）；把审阅结论写入执行记录。
  - [ ] 运行 `./scripts/check.sh`。
  - [ ] 若测试失败 → **停**（按 A5 上报）。
  - [ ] 将结果写入执行工作记录（基线证据，含实际 HEAD 与基线→HEAD 差异审阅结论）。
- **实现形状**：无代码改动。
- **目标测试与命令**：`./scripts/check.sh`。
- **Reviewer checklist**：
  - [ ] 记录存在且含 HEAD/分支/check 结果，以及（若 HEAD ≠ `216ff0b`）基线→HEAD 差异审阅结论。
  - [ ] 无任何文件被修改（`git status --short` 仅含本计划两份已跟踪文档）。

### Task 1：Graceful shutdown（单元参数层）

- **目标**：让 `uvicorn.run` 显式携带 graceful shutdown 超时（P0-1 的第一半）。真实进程 SIGTERM 集成验证**留给 Task 2**，本 task **不写子进程测试**。
- **Files**：
  - Modify：`src/oc_slimapi/app.py`（常量区 L42-71 附近新增 `_GRACEFUL_SHUTDOWN_TIMEOUT`；`main()` L510-524）
  - Modify：`deploy/oc-slimapi.service`（`[Service]` 段 L15-30）
  - Test（Create）：`tests/test_app_main.py`（新文件）
  - Docs（**Integrator-only**）：`docs/operations.md` §4 服务管理命令（L148-174）shutdown/restart 段——由 batch integrator 在三个实现 lane 完成后串行更新
  - Docs（**Integrator-only**）：`CHANGELOG.md`（Unreleased 段）——由 batch integrator 统一写入
- **Interfaces**：
  - Consumes：`uvicorn.run` 签名（`timeout_graceful_shutdown` 参数，uvicorn 已支持，不新增依赖）、`oc_slimapi.app.main`。
  - Produces：`main()` 向 `uvicorn.run` 传入 `timeout_graceful_shutdown`；unit 新增 `TimeoutStopSec=15`。
- **禁止修改**：`uvicorn` 版本、`lifespan` 逻辑、`tests/test_lifespan.py`、任何 wire 行为。
- **Acceptance Criteria**：
  - `T1-C1`：`main()` 调用 `uvicorn.run` 时传 `timeout_graceful_shutdown == 5.0`（常量名 `_GRACEFUL_SHUTDOWN_TIMEOUT`）。
  - `T1-C2`：`deploy/oc-slimapi.service` 的 `[Service]` 含 `TimeoutStopSec=15`。
  - `T1-C3`：`docs/operations.md` §4 说明 shutdown 语义（SIGTERM → uvicorn 5s 宽限 → systemd 15s 上限）。
  - `T1-C4`：`CHANGELOG.md` Unreleased 记录此行为修复（运维行为，非 wire）。
  - `T1-C5`：既有 `tests/test_lifespan.py` 全部保持绿（不触碰 lifespan）。
- **步骤**：
  - [ ] 记录 baseline HEAD；确认工作树无未提交改动。
  - [ ] 创建 `tests/test_app_main.py`：monkeypatch `oc_slimapi.app.uvicorn.run` 捕获 kwargs + 用 `SimpleNamespace` 替换 `oc_slimapi.app.settings`（含 `validate=lambda: None`），断言 `timeout_graceful_shutdown == 5.0` 与 host/port 透传；先跑确认**红**。
  - [ ] 在 `app.py` 常量区新增 `_GRACEFUL_SHUTDOWN_TIMEOUT = 5.0`（带 P0-1 注释）；`main()` 的 `uvicorn.run` 增加 `timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT`。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] `deploy/oc-slimapi.service` 增加 `TimeoutStopSec=15`（`RestartSec=5` 之后，注释说明与 uvicorn 5s 的关系）。
  - [ ] **实现 lane 不写 docs/CHANGELOG**：`docs/operations.md` §4 与 `CHANGELOG.md` 由 batch integrator 在三个实现 lane 完成后串行更新（见 E 节）。
  - [ ] 跑目标测试 + `./.venv/bin/python -m pytest -q tests/test_lifespan.py`。
  - [ ] 记录 diff（实现文件 + service；**不含 docs/CHANGELOG**，其 diff 由集成 lane 记录）。
- **实现形状**：
  ```python
  # app.py 常量区
  _GRACEFUL_SHUTDOWN_TIMEOUT = 5.0  # P0-1: uvicorn 对活跃连接（SSE）的优雅关闭宽限；
                                    # systemd TimeoutStopSec=15 高于此值，避免 90s SIGKILL。

  # main() 内
  uvicorn.run(
      "oc_slimapi.app:app",
      host=settings.host,
      port=settings.port,
      workers=1,
      timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT,
  )
  ```
  ```python
  # tests/test_app_main.py
  from types import SimpleNamespace
  import oc_slimapi.app as app_mod

  def test_main_passes_graceful_shutdown_timeout(monkeypatch):
      captured: dict = {}
      def fake_run(*args, **kwargs):
          captured["args"] = args
          captured["kwargs"] = kwargs
      # 用 SimpleNamespace 整体替换模块级 frozen settings：避免 monkeypatch
      # frozen dataclass 的 validate 属性在构造后不可靠（frozen 实例禁止赋值）。
      fake_settings = SimpleNamespace(host="127.0.0.1", port=4097, validate=lambda: None)
      monkeypatch.setattr(app_mod, "settings", fake_settings)
      monkeypatch.setattr(app_mod.uvicorn, "run", fake_run)
      app_mod.main()
      assert captured["kwargs"]["timeout_graceful_shutdown"] == 5.0
      assert captured["kwargs"]["host"] == "127.0.0.1"
      assert captured["kwargs"]["port"] == 4097
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_app_main.py::test_main_passes_graceful_shutdown_timeout`；随后 `$PY -m pytest -q tests/test_lifespan.py`；batch gate `./scripts/check.sh`。
- **Reviewer checklist**：
  - [ ] `main()` 仍先 `settings.validate()`（config 错误路径未破坏）。
  - [ ] 常量有 P0-1 注释；没有改 `lifespan`。
  - [ ] systemd unit（`deploy/oc-slimapi.service`）的 `TimeoutStopSec=15` 在 `[Service]` 段且语法正确。
  - [ ] 实现 lane 的 diff **不含** docs/CHANGELOG；docs/operations §4 与 CHANGELOG 描述准确（不提"已实施集成验证"）。
  - [ ] **T1-C3/C4 的 docs 核验由 reviewer 在 batch 集成后执行**（本 task 的 docs 由 integrator 写，单独核验放在集成 gate）。

### Task 2：Graceful shutdown integration（subprocess 级）

- **目标**：用独立测试文件验证真实进程收到 SIGTERM 后优雅退出。**若无法在不新增依赖、不依赖真实 opencode 的前提下稳定实现，停并上报，禁止写脆弱 sleep 测试。**
- **Files**：
  - Test（Create）：`tests/test_graceful_shutdown.py`（唯一新建文件；**不创建 `tests/fixtures/`**，fake upstream 直接在该测试文件内用标准库实现）
- **Interfaces**：Consumes：`oc_slimapi.app:app`（模块 import 路径）、`TrafficSnapshotter`（最终帧）、fake upstream 端口（经 `OC_SLIMAPI_UPSTREAM` 注入）。Produces：退出码/耗时/最终帧证据断言。
- **禁止修改**：`app.py`、`deploy/oc-slimapi.service`、现有测试；禁止新增依赖；禁止**单次固定等待后假定 ready** 与无界 sleep 轮询（有界 connect loop 内每次失败后的短 sleep 可用，见 T2-C4）。
- **fake upstream（测试文件内，标准库实现）**：用 `ThreadingHTTPServer` + `BaseHTTPRequestHandler` 在随机端口起 fake upstream，必须支持：
  - `GET /global/health` → 200；
  - `GET /global/event` → 返回 `text/event-stream` 响应并**保持连接打开**（周期性写注释帧或挂起，模拟持续 SSE）；
  - `GET /session`（**忽略 query**，返回 JSON `[]`）：`smoke_session_id=None` 时 `smoke()` 实际会先调 `GET /session?limit=1`（见 `app.py:99-116`）；返回 `[]` 后 sid 为空 → `smoke_status=not_run`，**不会**再调用 message smoke 路径（`/session/{sid}/message` 永不调用）；
  - 其它路径：handler 返回 404 即可。
- **Acceptance Criteria**：
  - `T2-C1`：subprocess 收到 SIGTERM 后 `communicate(timeout=15)` 返回 `returncode == 0`。
  - `T2-C2`：从 SIGTERM 到退出耗时 `< 15s`（满足 systemd `TimeoutStopSec=15`）。
  - `T2-C3`：退出前 `TrafficSnapshotter` 写入最终帧——按 **dir+stem template**（`OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH` 为 `logs/traffic-snapshot.jsonl`，实际文件为派生出的 `traffic-snapshot-YYYY-MM-DD.jsonl`）严格 glob 恰有一个日期文件，逐行 `json.loads` 且**至少 2 条有效 JSONL**（startup + shutdown final）；同时 **stderr 含正常关闭 sentinel**（`"Application shutdown complete"`）。**不得声称能从父进程检查子进程内部 `_access_log_stop_event`。**
  - `T2-C4`：测试是 deterministic 控制（有界 connect loop + `threading.Event`，见步骤），无单次固定等待后假定 ready。
  - `T2-C5`：**若当前 uvicorn 行为使 `returncode` 非 0 但 teardown 证据成立（最终帧/日志均落盘）→ 必须停并上报 BLOCKED**，不得放宽准则（不得把 returncode 断言放宽为"非 0 也接受"）。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 在 `tests/test_graceful_shutdown.py` 内实现 fake upstream（`ThreadingHTTPServer` + `BaseHTTPRequestHandler`，随机端口）：`/global/health` 200、`/global/event` text/event-stream 保持连接、`/session`（忽略 query）返回 JSON `[]`（使 smoke 走 `not_run`，message smoke 路径永不调用）。
  - [ ] `subprocess.Popen([sys.executable, "-m", "oc_slimapi.app"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=<env 覆盖，完整代码见实现形状>)`，env 覆盖：`OC_SLIMAPI_UPSTREAM=http://127.0.0.1:<fake_port>`、临时 `OC_SLIMAPI_ACCESS_LOG_DIR`/`OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH`/`OC_SLIMAPI_STATE_DIR`、`OC_SLIMAPI_HOST=127.0.0.1`、`OC_SLIMAPI_PORT=<随机端口>`。
  - [ ] **ready 判定**：有界 socket connect loop——设 deadline（如 20s），循环尝试 `socket.create_connection((host, port))`，每次失败用 `select`（或短暂 `time.sleep(0.05)`）后重试，直到成功或超时；**禁止单次固定等待后假定 ready**。
  - [ ] SSE 客户端放**独立线程**：线程内用 `urllib.request.Request(url, headers={"X-Slimapi-Version": "2", "Accept-Encoding": "identity"})` 发起 `GET /slimapi/events`（**必须带 `X-Slimapi-Version: 2`**，否则 version middleware 返回 400），读到响应头（200 + `text/event-stream`）后 `threading.Event.set()`；**线程内异常必须保存到 list**；主线程 `event.wait(timeout=10)` 确认头已收到（未 set 时把线程异常内容并入 AssertionError），然后保持该线程连接打开。
  - [ ] 发送 SIGTERM 前记录 `started = time.monotonic()`；发送 SIGTERM 后 `out, err = proc.communicate(timeout=15)`，再读 `rc = proc.returncode`（**禁止 `communicate()[0]`**），计算 `elapsed = time.monotonic() - started` 并断言 `< 15`。
  - [ ] 断言 `rc == 0`、`elapsed < 15`、stderr 含 `"Application shutdown complete"`（可观察 teardown sentinel）、snapshot 恰一个 `traffic-snapshot-YYYY-MM-DD.jsonl` 且逐行 `json.loads` ≥ 2 条有效 JSONL（startup + final）。
  - [ ] 若 `returncode != 0` 但 teardown 证据成立 → 记录 **BLOCKED**（证据 + 原因），**不得放宽 returncode 断言**；若无法确定性控制 → 同样 BLOCKED，**不得用 sleep 兜底**。
  - [ ] 记录 diff（若实现）或 BLOCKED 记录。
- **实现形状（结构示意，spike 后细化）**：
  ```python
  # tests/test_graceful_shutdown.py
  import json, os, signal, socket, subprocess, sys, threading, time, urllib.request
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

  class _FakeUpstream(BaseHTTPRequestHandler):
      def do_GET(self):
          path = self.path.split("?")[0]           # 忽略 query
          if path == "/global/health":
              self.send_response(200); self.send_header("Content-Type", "application/json")
              self.end_headers(); self.wfile.write(b'{"ok":true}')
          elif path == "/session":
              # smoke_session_id=None 时 smoke() 先调 GET /session?limit=1（app.py:99-116）；
              # 返回 [] → sid 为空 → smoke_status=not_run，不再调 /session/{sid}/message
              self.send_response(200); self.send_header("Content-Type", "application/json")
              self.end_headers(); self.wfile.write(b"[]")
          elif path == "/global/event":
              self.send_response(200)
              self.send_header("Content-Type", "text/event-stream")
              self.end_headers()
              while True:              # 保持连接打开直到进程退出
                  try:
                      self.wfile.write(b": keepalive\n\n"); self.wfile.flush()
                      time.sleep(1)
                  except (BrokenPipeError, ConnectionResetError):
                      return
          else:
              self.send_response(404); self.end_headers()
      def log_message(self, *a): pass

  def test_graceful_shutdown_subprocess(tmp_path):
      server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
      threading.Thread(target=server.serve_forever, daemon=True).start()
      fake_port = server.server_address[1]
      with socket.socket() as s:       # 先取一个空闲端口给 sidecar
          s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
      # snapshot 参数是 dir+stem template：实际文件为 traffic-snapshot-YYYY-MM-DD.jsonl
      snapshot_template = tmp_path / "logs" / "traffic-snapshot.jsonl"
      proc = subprocess.Popen(
          [sys.executable, "-m", "oc_slimapi.app"],
          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
          env={**os.environ,
               "OC_SLIMAPI_UPSTREAM": f"http://127.0.0.1:{fake_port}",
               "OC_SLIMAPI_ACCESS_LOG_DIR": str(tmp_path / "logs"),
               "OC_SLIMAPI_TRAFFIC_SNAPSHOT_PATH": str(snapshot_template),
               "OC_SLIMAPI_STATE_DIR": str(tmp_path / "state"),
               "OC_SLIMAPI_TRAFFIC_SNAPSHOT_INTERVAL_S": "1",
               "OC_SLIMAPI_HOST": "127.0.0.1", "OC_SLIMAPI_PORT": str(port)},
      )
      try:
          # ready：有界 socket connect loop（deadline + 每次失败后 select/短 sleep），
          # 禁止单次固定等待后假定 ready
          deadline = time.monotonic() + 20
          while time.monotonic() < deadline:
              try:
                  with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                      break            # 端口可连 → ready
              except OSError:
                  time.sleep(0.05)
          else:
              raise AssertionError("sidecar 未在 deadline 内就绪")
          # SSE 客户端放独立线程，threading.Event 确认响应头收到后保持连接打开；
          # 必须带 X-Slimapi-Version: 2（否则 version middleware 400）
          headers_ok = threading.Event()
          sse_errors: list[BaseException] = []
          def sse_client():
              try:
                  req = urllib.request.Request(
                      f"http://127.0.0.1:{port}/slimapi/events",
                      headers={"X-Slimapi-Version": "2", "Accept-Encoding": "identity"},
                  )
                  with urllib.request.urlopen(req, timeout=30) as resp:
                      assert resp.status == 200
                      assert resp.headers.get_content_type() == "text/event-stream"
                      headers_ok.set()
                      while resp.read(1):  # 保持流打开直到进程退出
                          pass
              except BaseException as exc:
                  sse_errors.append(exc)  # 线程内异常保存，避免后台 assert 被吞
          t = threading.Thread(target=sse_client, daemon=True); t.start()
          if not headers_ok.wait(10):
              raise AssertionError(f"SSE 响应头未在 10s 内收到: {sse_errors!r}")
          started = time.monotonic()      # SIGTERM 前记录起点
          proc.send_signal(signal.SIGTERM)
          out, err = proc.communicate(timeout=15)
          rc = proc.returncode            # 从 proc.returncode 读取，不用 communicate()[0]
          elapsed = time.monotonic() - started
          assert rc == 0                  # 不得放宽为非 0
          assert elapsed < 15
          assert "Application shutdown complete" in err   # 可观察 teardown sentinel
          # snapshot：严格 glob 恰有一个 traffic-snapshot-YYYY-MM-DD.jsonl，逐行 json.loads ≥2 帧
          matches = list((tmp_path / "logs").glob("traffic-snapshot-????-??-??.jsonl"))
          assert len(matches) == 1
          lines = [json.loads(l) for l in matches[0].read_text().splitlines() if l.strip()]
          assert len(lines) >= 2          # startup 帧 + shutdown final 帧
      finally:
          if proc.poll() is None:
              proc.kill()
              proc.communicate(timeout=5)  # kill 后回收 stdout/stderr 管道
          server.shutdown()
          server.server_close()
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_graceful_shutdown.py`。
- **Reviewer checklist**：
  - [ ] 无固定 `sleep` 兜底；ready 判定为有界 connect loop + 明确 sentinel。
  - [ ] 不依赖真实 opencode；不新增依赖；不创建 `tests/fixtures/`。
  - [ ] fake upstream 支持 `/global/health` 200、`/global/event` 保持连接、`/session`（忽略 query）返回 `[]`（smoke 走 `not_run`，message smoke 不调用）；其余路径 404 即可。
  - [ ] SSE 请求带 `X-Slimapi-Version: 2`（否则 400）；头确认用 `threading.Event`（独立线程，线程内异常入 list 不外吞，headers 未在 10s set 时并入 AssertionError）。
  - [ ] `Popen` 配 `stdout=PIPE, stderr=PIPE, text=True`；`communicate(timeout=15)` 后读 `proc.returncode`（**不取 `communicate()[0]`**）；`started` 在 SIGTERM 前记录、elapsed `< 15`；stderr 含 `"Application shutdown complete"`。
  - [ ] snapshot 按 dir+stem template 派生日期文件：严格 glob 恰一个 `traffic-snapshot-YYYY-MM-DD.jsonl`，逐行 `json.loads` ≥ 2 条有效 JSONL（startup + final）。
  - [ ] finally：kill 后 `proc.communicate(timeout=5)` 回收管道；server `shutdown()` + `server_close()`。
  - [ ] 若 BLOCKED：记录含证据，且**没有**把脆弱测试带进仓；returncode 断言未被放宽。
  - [ ] `timeout` 参数与断言时间边界一致（<15s）。

### Task 3：exact `/slimapi` root 不再透传

- **目标**：精确 `/slimapi`（无尾斜杠）与 `/slimapi/**` 一致返回 404 `thin_route_not_found`（P1-5）。属**用户可见错误路径修复**。
- **Files**：
  - Modify：`src/oc_slimapi/proxy.py`（L128）
  - Test：`tests/test_proxy.py`（新增用例，建议放在 `test_slimapi_unknown_still_404` L131 附近）
  - Docs（**Integrator-only**）：`docs/specs/INTERFACE_MAP.md` catch-all 行——明确 exact `/slimapi` 与 `/slimapi/**` 均 sidecar 404（实现 lane 不写，Batch 1 integrator 统一改描述）
  - Docs（**Integrator-only**）：`CHANGELOG.md`（Unreleased 段）
- **Interfaces**：Consumes：`error_response`、`_normalize_path`。Produces：统一 404 语义。
- **禁止修改**：其它 proxy 逻辑（shell deny、turn bump、raw query、超时分类）；实现 lane **不写 docs**（INTERFACE_MAP catch-all 行描述与 CHANGELOG 由 Batch 1 integrator 更新，只改描述、不增删路由 inventory）。
- **Acceptance Criteria**：
  - `T3-C1`：`GET /slimapi` → 404 `{"code":"thin_route_not_found"}`。
  - `T3-C2`：该请求 **zero** upstream 调用（用 `upstream_factory` 计数断言）。
  - `T3-C3`：`GET /slimapi/anything` 仍 404（既有 `test_slimapi_unknown_still_404` 保持绿）。
  - `T3-C4`：非 slimapi 路径仍正常反代（既有 proxy 测试全绿）。
  - `T3-C5`：`docs/specs/INTERFACE_MAP.md` catch-all 行明确 exact `/slimapi` 与 `/slimapi/**` 均 sidecar 404（**Integrator-only**，集成后核验）；`CHANGELOG.md` Unreleased 记录此错误路径修复。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 在 `tests/test_proxy.py` 新增 `test_exact_slimapi_root_not_proxied`：断言 404、code、upstream 零调用；先跑确认**红**。
  - [ ] 修改 `proxy.py:128` 条件（见形状）。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `tests/test_proxy.py` 全文件确认无回归。
  - [ ] 实现 lane **不写 docs**：INTERFACE_MAP catch-all 行描述与 CHANGELOG 由 Batch 1 integrator 在三个实现 lane 完成后统一更新（见 E 节）。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # proxy.py L128 附近（替换现有条件）
  if norm_path == "/slimapi" or norm_path.startswith("/slimapi/"):
      return error_response(
          "thin_route_not_found", 404,
          accept_encoding=request.headers.get("accept-encoding"),
      )
  ```
  ```python
  # tests/test_proxy.py —— 复用本文件已有 helper（_upstream_passthrough / _build_app / _settings）
  async def test_exact_slimapi_root_not_proxied(upstream_factory):
      handler, seen = _upstream_passthrough()
      upstream = upstream_factory(handler)
      app = _build_app(_settings(), upstream)
      transport = httpx.ASGITransport(app=app)
      async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
          response = await client.get("/slimapi")
      assert response.status_code == 404
      assert response.json()["code"] == "thin_route_not_found"
      assert seen["path"] is None  # zero upstream calls（upstream 从未被触达）
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_proxy.py::test_exact_slimapi_root_not_proxied`；随后 `$PY -m pytest -q tests/test_proxy.py`。
- **Reviewer checklist**：
  - [ ] 条件同时覆盖精确根与子路径；`/slimapi/`（尾斜杠）也被 `startswith` 覆盖。
  - [ ] 未触碰其它 proxy 分支；INTERFACE_MAP 路由 inventory 无增删（仅描述校准由 integrator 在集成后做，T3-C5 由 reviewer 集成后核验）。

### Task 4：sessions transform_busy 带 Retry-After

- **目标**：`GET /slimapi/sessions` 转换池饱和时与 catalog 路由一致：503 + body `retry_after:2` + header `Retry-After:2`（P1-6）。属**加性 wire 行为**（新增可选 body 字段 + 头，客户端可忽略，不 bump）。
- **Files**：
  - Modify：`src/oc_slimapi/routes/sessions.py`（import 区 L1-17；L100-101 TransformBusy 分支）
  - Test：`tests/test_sessions_routes.py`（新增用例）
  - Docs（**Integrator-only**）：`docs/specs/INTERFACE_MAP.md` sessions 行——记录 body `retry_after:2` + 头 `Retry-After:2`（实现 lane 不写，Batch 1 integrator 统一改描述）
  - Docs（**Integrator-only**）：`CHANGELOG.md`（Unreleased 段）
- **Interfaces**：Consumes：`_catalog_common.busy_response`、`TransformBusy`。Produces：统一 `transform_busy` 语义。
- **禁止修改**：`_catalog_common.py`、`agent/command/directories/messages` 的 busy 行为；实现 lane **不写 docs**（INTERFACE_MAP sessions 行描述与 CHANGELOG 由 Batch 1 integrator 更新，只改描述、不增删路由 inventory）。
- **Acceptance Criteria**：
  - `T4-C1`：busy 时 body 含 `"code":"transform_busy"` 与 `"retry_after":2`（`retry_after == 2`，其余 envelope 字段维持既有形状）。
  - `T4-C2`：响应头 `Retry-After == "2"`。
  - `T4-C3`：该响应 **zero** upstream 调用（busy 发生在 admission，不发出 GET）。
  - `T4-C4`：既有 sessions 测试全绿；`CodedHTTPException` 保留（`sessions.py:71` 413 路径仍使用）。
  - `T4-C5`：`docs/specs/INTERFACE_MAP.md` sessions 行记录 body `retry_after:2` + 头 `Retry-After:2`（**Integrator-only**，集成后核验）；`CHANGELOG.md` Unreleased 记录（加性，不 bump）。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 新增 `tests/test_sessions_routes.py::test_sessions_transform_busy_returns_retry_after_without_upstream_call`：用饱和的 TransformPool（max_transforms=1 + 占用槽位）触发 TransformBusy，断言 body/header/零 upstream；先跑确认**红**。
  - [ ] `sessions.py` 引入 `from ._catalog_common import busy_response`；`except TransformBusy` 分支改为 `return busy_response(request.headers.get("accept-encoding"))`。
  - [ ] 确认 `CodedHTTPException` 仍被 L71 使用 → **保留 import**；若确认无其它使用才删除。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `tests/test_sessions_routes.py` + `tests/test_agent_routes.py::test_agent_transform_busy_when_admission_saturated` + `tests/test_command_routes.py::test_transform_busy` 确认无回归。
  - [ ] 实现 lane **不写 docs**：INTERFACE_MAP sessions 行描述与 CHANGELOG 由 Batch 1 integrator 在三个实现 lane 完成后统一更新（见 E 节）。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # sessions.py —— import 区（新增）
  from ._catalog_common import busy_response, read_upstream_response
  ```
  ```python
  # sessions.py —— TransformBusy 分支（替换原分支；既有路由主体不变）
  except TransformBusy as exc:
      return busy_response(request.headers.get("accept-encoding"))
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_sessions_routes.py::test_sessions_transform_busy_returns_retry_after_without_upstream_call`。
- **Reviewer checklist**：
  - [ ] body 与 header 均有 Retry-After；`accept_encoding` 透传保证 gzip 契约（§9）。
  - [ ] `CodedHTTPException` import 未被误删（413 路径仍用）。
  - [ ] 无对 catalog 路由的连带改动；INTERFACE_MAP 路由 inventory 无增删（仅描述校准由 integrator 在集成后做，T4-C5 由 reviewer 集成后核验）。

### Task 5：questions 内存预算（sliding window scheduler）

- **目标**：消除 P1-1 的 16×64MiB 病态上界：per-dir 读上限、聚合字节预算、fanout 并发上限全部配置化；预算触发时**不再启动新目录**、取消已启动未消费 task，并保证关闭所有 response。**高风险：必须先测试设计再实现。**
- **Files**：
  - Modify：`src/oc_slimapi/config.py`（Settings 字段区 L164-315 加 3 字段；validate L380-578 加校验）
  - Modify：`src/oc_slimapi/app.py`（lifespan L284 附近创建 `app.state.questions_semaphore`，跨请求全局 `/question` 并发上限）
  - Modify：`src/oc_slimapi/routes/questions.py`（L20-41 删模块级 `_FANOUT_CONCURRENCY`/`_fanout_sem`，**保留 `_MAX_AGGREGATE_ITEMS`**；L109-224 路由改用 scheduler；L241-311 worker 改签名）
  - Test：`tests/test_questions_routes.py`（新增用例 + **改既有 `test_fanout_concurrency_bound_*`：不再 import `_FANOUT_CONCURRENCY`，改从 app config 读取或传 override**；测试内 `_build_app` **必须创建 `app.state.questions_semaphore`**）+ `tests/test_config.py`（新增校验用例）
  - Docs：`docs/operations.md`（§5 或新小节说明 3 个 env knob）+ `docs/specs/INTERFACE_MAP.md`（questions 行：记录内部 per-dir/aggregate/concurrency caps 及预算触发仍沿用 `truncated`/partial authority）+ `CHANGELOG.md`
- **Interfaces**：Consumes：`read_with_cap`（返回 `(body, total_bytes)`）、`config.questions_*` 三字段、`app.state.questions_semaphore`。Produces：envelope 沿用既有 `{items,errors,authoritativeDirectories,discoveryComplete,truncated?}`——**wire 形状不变，沿用 `truncated` 字段，不 bump**。
- **禁止修改**：envelope 字段名/语义、`_DISCOVERY_LIMIT`/`fetch_global_root_sessions` 发现逻辑、`_pack_questions_envelope` 的 offload 用法；不得新增依赖。
- **Acceptance Criteria**：
  - `T5-C1`：`app.state.questions_semaphore` 限制**跨请求**总上游 `/question` 并发；单请求 fanout 并发不超过 `config.questions_fanout_concurrency`（用假 upstream 并发计数断言）。
  - `T5-C2`：单目录 body 超 `questions_max_response_bytes` → 该目录进 `errors[]`（`upstream_unavailable`）且 `body_bytes=0`（不占 accepted aggregate），不 abort 整体。
  - `T5-C3`：预算触发（字节 cap 或 `_MAX_AGGREGATE_ITEMS` item cap）→ `truncated:true`，**不再启动新目录**，并**取消已启动未消费的 task**。
  - `T5-C4`：截断时 `authoritativeDirectories` == 已成功目录数组（partial），**非 null**；被取消/未启动目录**不写 errors[]**（靠 `truncated` + partial authority 表达）。
  - `T5-C5`：取消时所有已打开的 response 均被 `aclose`（spy aclose 计数断言）。
  - `T5-C6`：config 校验（冻结）：`questions_fanout_concurrency` ∈ 1..16；`questions_max_response_bytes > 0`；`questions_max_aggregate_bytes >= questions_max_response_bytes`（aggregate >= per_dir）且 `<= 128 MiB` → 违反即 RuntimeError。
  - `T5-C7`：既有 questions 测试全绿（含 `test_fanout_concurrency_bound_completes_with_many_dirs` L954；该系列**不再 import `_FANOUT_CONCURRENCY`**，改从 app config 读取或传 override）。
  - `T5-C8`：docs/operations + INTERFACE_MAP（questions 行）+ CHANGELOG 记录 3 个内部 knob（标注非 wire）；INTERFACE_MAP questions 行注明预算触发仍沿用 `truncated`/partial authority 语义（行为可见但加性、不 bump）。
  - `T5-C9`：`X-Slimapi-Version` 不变（`truncated` 为既有加性字段）。
  - `T5-C10`：`_MAX_AGGREGATE_ITEMS` **保留**为第二层 item-count 上限；触发任一 cap（byte 或 item）都 `truncated:true` 并取消后续（保持既有防线）。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] **先写测试设计**：确定每个新用例的假 upstream 行为（目录数、各目录 body 大小、并发计数、aclose spy）——写成用例清单并在此步骤冻结。
  - [ ] 在 `tests/test_config.py` 写 3 个字段默认值 + 非法值校验用例（含 128 MiB 上界与 concurrency 1..16）；在 `tests/test_questions_routes.py` 写 T5-C1..C5 用例；**同步改既有 `test_fanout_concurrency_bound_*`：不再 import `_FANOUT_CONCURRENCY`，改从 app config 读取或传 override**；确认测试内 `_build_app` 创建 `app.state.questions_semaphore`。全部先跑确认**红**。
  - [ ] `config.py` 加字段（冻结默认：2 MiB / 16 MiB / 8）与 validate（per_dir>0；aggregate>=per_dir；aggregate<=128 MiB；concurrency 1..16）；`app.py` lifespan 创建 `app.state.questions_semaphore = asyncio.Semaphore(settings.questions_fanout_concurrency)`。
  - [ ] `questions.py`：删模块级 `_FANOUT_CONCURRENCY`/`_fanout_sem`，**保留 `_MAX_AGGREGATE_ITEMS`**（作为第二层 item cap 传入 scheduler）；worker `_fetch_questions_for_dir` 加 `cap: int` 参数并返回 `(items, error_code, body_bytes)`——成功时 `body_bytes` = `read_with_cap` 返回的 `total_bytes` 原始值；per-dir 超限/失败时 `body_bytes=0`（读取字节仍经 traffic stash 计量，但不占 accepted aggregate）——在 `async with request.app.state.questions_semaphore:` 内执行，保留 `finally: await response.aclose()`。
  - [ ] 实现 `_collect_with_byte_budget` sliding window（冻结设计见下），替换 `asyncio.gather` 调用点；`finally` 统一取消并 await 仍在 tasks 中未消费的任务。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `tests/test_questions_routes.py` 全文件 + `tests/test_config.py` 全文件。
  - [ ] 更新 docs/operations + INTERFACE_MAP（questions 行）+ CHANGELOG。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # config.py（冻结默认值；校验规则见 T5-C6）
  questions_max_response_bytes: int = int(os.getenv("OC_SLIMAPI_QUESTIONS_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))
  questions_max_aggregate_bytes: int = int(os.getenv("OC_SLIMAPI_QUESTIONS_MAX_AGGREGATE_BYTES", str(16 * 1024 * 1024)))
  questions_fanout_concurrency: int = int(os.getenv("OC_SLIMAPI_QUESTIONS_FANOUT_CONCURRENCY", "8"))
  # validate()（冻结）：
  #   questions_fanout_concurrency ∈ [1, 16]；
  #   questions_max_response_bytes > 0；
  #   questions_max_aggregate_bytes >= questions_max_response_bytes（aggregate >= per_dir）且 <= 128 MiB
  ```
  ```python
  # routes/questions.py —— worker 返回原始 body 字节数（序列化前可证明的预算）
  async def _fetch_questions_for_dir(
      upstream_client, request, directory, *, cap: int,
  ) -> tuple[list[dict], str | None, int]:
      async with request.app.state.questions_semaphore:   # 跨请求全局并发上限
          try:
              response = await upstream_client.send(
                  upstream_client.build_request(
                      "GET", "/question",
                      headers=forward_directory_headers(directory),
                  ),
                  stream=True,
              )
          except httpx.RequestError:
              return [], UPSTREAM_UNAVAILABLE, 0
          try:
              status = response.status_code
              if status >= 400:
                  # 错误响应也必须受 per-dir cap **有界 drain**（不得 aread 无界读取）；
                  # 无论 body 是否超限，按 status 返回对应 upstream error，body_bytes=0
                  await read_with_cap(
                      response, cap, on_read=lambda n: stash_up_in(request, n),
                  )
                  return [], upstream_error_code_for_status(status), 0
              body, total = await read_with_cap(
                  response, cap, on_read=lambda n: stash_up_in(request, n),
              )
              # per-dir cap 超限 / 读取失败 → error + body_bytes=0：
              # 读取字节仍经 traffic stash 计量，但不占 accepted aggregate。
              if body is None:
                  return [], UPSTREAM_UNAVAILABLE, 0
              try:
                  payload = orjson.loads(body)
              except (orjson.JSONDecodeError, ValueError):
                  return [], UPSTREAM_UNAVAILABLE, 0
              if not isinstance(payload, list):
                  return [], UPSTREAM_UNAVAILABLE, 0
              items = [
                  {**entry, "directory": directory}
                  for entry in payload if isinstance(entry, dict)
              ]
              return items, None, total
          except httpx.RequestError:
              return [], UPSTREAM_UNAVAILABLE, 0
          finally:
              await response.aclose()
  ```
  ```python
  # routes/questions.py —— sliding window scheduler（替换 gather 调用点；冻结设计）
  #
  # 冻结设计：
  # - app.state.questions_semaphore 为跨请求全局 /question 并发上限；测试 _build_app 必须创建它。
  # - tasks: dict[int, asyncio.Task] 按 directory index 保存；并发执行，但严格按原 index
  #   顺序 await/消费：消费完 index i 后才 launch 下一个未启动 index。同一时刻在途 task
  #   数 ≤ concurrency，内存上界 = accepted aggregate + concurrency × per_dir_cap + overhead。
  # - aggregate 只对成功响应的 raw body bytes 计费；per-dir 超限/失败 → error + body_bytes=0。
  # - 任一 cap（byte cap 或 _MAX_AGGREGATE_ITEMS item cap）触发：当前目录不进入
  #   items/succeeded，truncated=True，取消所有 index>i 的 task 并 await gather(
  #   return_exceptions=True)；未启动目录永不启动。errors 只含已消费且失败的目录。
  # - cleanup：finally 取消并 await 仍在 tasks 中未消费的任务（保证 finally: aclose 执行）。
  async def _collect_with_byte_budget(
      upstream_client, request, directories, *,
      concurrency: int, per_dir_cap: int, aggregate_cap: int, item_cap: int,
  ) -> tuple[list[dict], list[dict], list[str], bool]:
      tasks: dict[int, asyncio.Task] = {}
      next_to_launch = 0                  # 下一个未启动的 directory index
      consume_index = 0                   # 下一个待消费的 index（严格原顺序）
      used_bytes = 0
      used_items = 0
      truncated = False
      items: list[dict] = []
      errors: list[dict] = []
      succeeded: list[str] = []

      def launch(index: int) -> None:
          tasks[index] = asyncio.create_task(
              _fetch_questions_for_dir(upstream_client, request, directories[index], cap=per_dir_cap)
          )

      # 初始窗口：至多 concurrency 个 task
      while next_to_launch < len(directories) and next_to_launch < concurrency:
          launch(next_to_launch)
          next_to_launch += 1

      try:
          while consume_index < len(directories):
              task = tasks.get(consume_index)
              if task is None:            # 该 index 未启动（预算截断后不会发生）
                  break
              try:
                  outcome = await task    # 每个 index 只消费/计费一次
              except asyncio.CancelledError:
                  raise                   # 自身被取消 → finally 统一清理
              except Exception as exc:    # 只把普通 Exception 当目录失败；
                  outcome = exc           # SystemExit/KeyboardInterrupt 不吞、继续传播

              if isinstance(outcome, Exception):
                  # 已消费且失败的目录才进 errors
                  errors.append({"directory": directories[consume_index], "code": UPSTREAM_UNAVAILABLE})
              else:
                  dir_items, error_code, body_bytes = outcome
                  if error_code is not None:
                      # per-dir cap 超限/上游不可用：error，body_bytes=0，不占 aggregate
                      errors.append({"directory": directories[consume_index], "code": error_code})
                  elif (used_bytes + body_bytes > aggregate_cap
                        or used_items + len(dir_items) > item_cap):
                      # 预算触发：当前目录不加入 items/succeeded；取消 index>i 并等待其 aclose
                      truncated = True
                      for idx, t in tasks.items():
                          if idx > consume_index:
                              t.cancel()
                      await asyncio.gather(*tasks.values(), return_exceptions=True)
                      break
                  else:
                      items.extend(dir_items)
                      succeeded.append(directories[consume_index])
                      used_bytes += body_bytes
                      used_items += len(dir_items)

              # 消费完 index i 后才 launch 下一个未启动 index（窗口 ≤ concurrency）
              if next_to_launch < len(directories):
                  launch(next_to_launch)
                  next_to_launch += 1
              consume_index += 1
      finally:
          # cleanup：取消并 await 仍在 tasks 中未消费的任务（保证 finally: aclose 执行）
          pending = [t for idx, t in tasks.items() if idx > consume_index]
          for t in pending:
              t.cancel()
          if pending:
              await asyncio.gather(*pending, return_exceptions=True)

      return items, errors, succeeded, truncated
  ```
- **目标测试与命令**（逐一）：
  - `$PY -m pytest -q tests/test_questions_routes.py::test_fanout_concurrency_respects_config_cap`
  - `$PY -m pytest -q tests/test_questions_routes.py::test_per_dir_read_cap_applies_questions_budget`
  - `$PY -m pytest -q tests/test_questions_routes.py::test_aggregate_byte_budget_truncates_and_cancels_remaining`
  - `$PY -m pytest -q tests/test_questions_routes.py::test_aggregate_truncation_degrades_authority`
  - `$PY -m pytest -q tests/test_questions_routes.py::test_cancel_closes_inflight_responses`
  - `$PY -m pytest -q tests/test_config.py::test_questions_budget_defaults`（以及非法值用例名按 config 惯例）
  - 既有并发边界用例（已改为从 app config 读取/传 override）：`$PY -m pytest -q tests/test_questions_routes.py::test_fanout_concurrency_bound_completes_with_many_dirs`
  - 回归：`$PY -m pytest -q tests/test_questions_routes.py tests/test_config.py`
- **Reviewer checklist**：
  - [ ] 预算基于**原始 body bytes**（`read_with_cap` 的 `total_bytes`），非序列化后估算；per-dir 失败/超限返回 `body_bytes=0`（不占 accepted aggregate）。
  - [ ] **所有状态码的 body 均受 per-dir cap**：status>=400 的错误响应走 `read_with_cap` 有界 drain，**不得 `aread()` 无界读取**。
  - [ ] 每个 index 只消费/计费一次；消费完 index i 后才 launch 下一个未启动 index（同一时刻在途 task ≤ concurrency）。
  - [ ] 预算触发路径存在：取消 `index>i` 的 task + `await asyncio.gather(*tasks.values(), return_exceptions=True)`，确保 `finally: aclose` 执行；`finally` cleanup 覆盖所有仍在 tasks 中的未消费任务。
  - [ ] 成功目录按原 `directories` 顺序输出；`authoritativeDirectories` 截断时非 null；errors 只含已消费且失败的目录。
  - [ ] 模块级 `_FANOUT_CONCURRENCY`/`_fanout_sem` 已移除；`_MAX_AGGREGATE_ITEMS` **保留**并作为 item cap 传入 scheduler；既有 `test_fanout_concurrency_bound_*` 不再 import `_FANOUT_CONCURRENCY`。
  - [ ] 测试 `_build_app` 创建 `app.state.questions_semaphore`。
  - [ ] wire 版本未动；`truncated` 语义与契约 §2 一致。

### Task 6：access-log 单调用行写入

- **目标**：`DailyAccessHandler.emit` 的两次 `write` 合成一次，缩小应用层两次调用之间的半行写入窗口（P1-2 的第一半）。这是 **best-effort** 的窗口缩小，**不是** fsync/事务级原子保证——单次 TextIO `write` 不提供 POSIX/崩溃原子性。**本 task 不实现 async queue**（那是 Task 7 设计门禁内容）。
- **Files**：
  - Modify：`src/oc_slimapi/access_log.py`（L191-196 emit 内）
  - Test：`tests/test_access_log.py`（新增用例）
- **Interfaces**：Consumes：`DailyAccessHandler.emit`。Produces：单次 `write(msg + "\n")`。
- **禁止修改**：`write_access_log` 记录字段、压缩/prune/migrate、维护循环。
- **Acceptance Criteria**：
  - `T6-C1`：spy 文件对象记录 `write` 恰好调用一次（每次 emit）且 `flush` 恰好调用一次；**spy 必须同时实现 `write` 与 `flush`**（否则 emit 内部捕获 AttributeError 会走 `handleError`，测试假绿）。
  - `T6-C2`：传入内容为 `msg + "\n"`（含行尾，仍是单行 JSONL）。
  - `T6-C3`：既有 access log 测试全绿（行为不变）。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 新增 `tests/test_access_log.py::test_emit_writes_single_call_with_newline`：构造 `DailyAccessHandler`，monkeypatch `_current_fh` 为 spy（**同时实现 `write` 与 `flush`**，分别记录 write 内容与 flush 次数），构造 LogRecord 触发 `emit`；断言 `writes == ["{}\n"]` 且 `flush_calls == 1`；先跑确认**红**。
  - [ ] `access_log.py` L194-195 合并为 `self._current_fh.write(msg + "\n")`。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `tests/test_access_log.py` 全文件。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # access_log.py emit 内
  self._current_fh.write(msg + "\n")   # P1-2: 单调用行写入，缩小两次调用间的半行窗口（best-effort，非 fsync/事务）
  self._current_fh.flush()
  ```
  ```python
  # tests/test_access_log.py
  def test_emit_writes_single_call_with_newline(tmp_path):
      handler = DailyAccessHandler(directory=str(tmp_path))
      writes: list[str] = []
      flush_calls = 0
      class Spy:
          def write(self, s: str) -> None:
              writes.append(s)
          def flush(self) -> None:
              nonlocal flush_calls
              flush_calls += 1
      handler._current_date = date.today()
      handler._current_fh = Spy()
      record = logging.LogRecord("oc_slimapi.access", logging.INFO, "", 0, "{}", None, None)
      handler.emit(record)
      assert writes == ["{}\n"]
      assert flush_calls == 1  # 若 spy 缺 flush，emit 会捕获 AttributeError 假绿，必须同时断言 flush
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_access_log.py::test_emit_writes_single_call_with_newline`。
- **Reviewer checklist**：
  - [ ] 两次 write → 一次；`flush` 保留（既有语义）。
  - [ ] 未触碰 `write_access_log` 与维护循环。
  - [ ] 文档/注释**不得声称**单次 write 提供 POSIX/崩溃原子保证（只说缩小应用层窗口、best-effort）。

### Task 7：access-log bounded writer 设计门禁（只产文档）

- **目标**：产出 `docs/specs/access-log-writer-design.md`，冻结 bounded async writer 设计；**本计划内不实现**，未来须另开实施计划且经 reviewer 放行。
- **Files**：
  - Create：`docs/specs/access-log-writer-design.md`
- **Interfaces**：Consumes：Task 6 结论（单调用行写入，缩小应用层半行窗口、非原子保证）、`DailyAccessHandler.emit` 现状。Produces：冻结设计文档。
- **禁止修改**：任何代码文件。
- **Acceptance Criteria**：
  - `T7-C1`：文档必须**冻结**：queue cap（数值 + 依据）、drop policy（丢弃语义与触发）、shutdown drain（顺序与超时）、metrics（计数项）、thread/loop ownership（谁写文件、如何避免阻塞事件循环）。
  - `T7-C2`：本 task diff 仅含该文档（`git status` 无代码文件）。
  - `T7-C3`：reviewer 按设计完整性与可行性通过。
  - `T7-C4`：明确声明"本计划不实现，实施需另开计划"。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 阅读 Task 6 diff 与 `access_log.py` 现状（`emit`/`write_access_log`/维护循环）。
  - [ ] 撰写文档：现状问题、目标、**冻结决策**（每项给出确定值/策略而非范围）、风险与回滚、验收清单。
  - [ ] 提交 reviewer；未过 reviewer 前不得进入后续实现设想。
  - [ ] 记录 diff（仅文档）。
- **实现形状**：文档章节标题与必含冻结项（内容为设计决策，**全部为已冻结值/策略，不允许"待定"**）：
  ```markdown
  # access-log bounded writer design
  ## 现状与问题（P1-2）
  ## 目标
  ## 冻结决策
  - queue cap: 1024 records（固定值；依据：单请求单行 JSONL，QPS 千级缓冲余量）
  - enqueue: 事件循环线程内 `put_nowait`；queue full 时**丢弃最新记录**（保留已排队顺序，`drop_newest`），不阻塞业务路径
  - thread ownership: **单 writer thread 独占** DailyAccessHandler / file handle，事件循环永不直接写文件
  - warning rate-limit: 丢弃与写错误告警按 60 秒 rate-limit（首条立即，后续 60s 窗口内合并计数）
  - metrics: `queued`（当前排队数）、`written`（累计写出）、`dropped`（累计丢弃）、`writeErrors`（累计写错误）、`queueDepth`（峰值深度）
  - shutdown drain: stop accepting → drain 最多 5 秒（排空剩余队列）→ 超时则丢剩余并累计 `dropped` → close handler
  - init 失败: 沿用 disabled/no-op（access log 关闭等价现状，不 crash 启动）
  - 不允许无界 queue；不允许业务请求 await writer（写路径与请求完全解耦）
  ## 风险与回滚
  ## 验收清单
  ## 实施边界（本文件不实施；另开计划）
  ```
- **目标测试与命令**：无代码测试；reviewer gate 为唯一门禁。
- **Reviewer checklist**：
  - [ ] 五项冻结项均为确定值/策略，无"待定/视情况"。
  - [ ] 无代码改动；文档不声称已实现。

### Task 8：skeleton 配置注入（SkeletonLimits）

- **目标**：消除 P1-3 配置双轨——`skeleton.py` 不再读全局 `settings`，改用不可变 `SkeletonLimits` 显式注入；默认值来自模块常量（不读 settings），仅直接纯函数测试使用默认。
- **Files**：
  - Modify：`src/oc_slimapi/skeleton.py`（L10 删 import；L149-179 `_maybe_inline_state_field`；L181-289 `_tool`/`_patch`；L309-370 `skeleton_part`/`skeleton_message`/`skeleton_messages`）
  - Modify：`src/oc_slimapi/routes/messages.py`（L63-90 `_project_list_sorted_and_pack` 加 `limits` 参数；L317 调用点由 route 从 `request.app.state.config` 构造传入）
  - Test：`tests/test_skeleton.py`（新增纯函数注入用例）+ `tests/test_messages_routes.py`（新增 route 级用例 `test_messages_route_uses_per_app_skeleton_limits`）
- **Interfaces**：Consumes：`config.skeleton_inline_output_max_bytes` / `_max_message_bytes`（由 route 读取）。Produces：`SkeletonLimits` 数据类 + 各函数签名。
- **禁止修改**：投影语义（阈值逻辑、`_mark`/`_pick`、`hasFull`/`omitted`、diffStats 注入）、`routes/health.py` 的 `skeletonInlineOutputMaxBytes` 输出。
- **Acceptance Criteria**：
  - `T8-C1`：两个 app 用不同 caps 调用同一纯函数，输出不同且无跨 app 泄漏（同一模块、不同 limits 参数）。
  - `T8-C2`：既有 skeleton 测试保持绿（默认 `DEFAULT_SKELETON_LIMITS` 与当前默认 4KiB/16KiB 一致）。
  - `T8-C3`：`skeleton.py` **不再 import** `.config.settings`（`grep` 证实）。
  - `T8-C4`：`routes/messages.py` 从 `request.app.state.config` 构造 `SkeletonLimits` 并传给 worker。
  - `T8-C5`：`messages` 路由既有测试全绿。
  - `T8-C6`：测试**必须覆盖 route 级不同 Settings**——两个 `_build_app` 各用不同 caps 的 Settings 走真实 messages route 路径，断言投影输出随之不同；**不只是纯函数两参数**。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 新增 `tests/test_skeleton.py::test_skeleton_limits_injectable_no_cross_app_leak`（同一 `skeleton_messages` 调用分别传大/小 limits，断言输出不同）；先跑确认**红**（当前无法注入）。
  - [ ] **route 级用例写入 `tests/test_messages_routes.py`**：新增 `test_messages_route_uses_per_app_skeleton_limits`——两个 `_build_app` 各用不同 caps 的 Settings 走真实 messages route 路径，断言投影输出随之不同；先跑确认**红**。
  - [ ] `skeleton.py`：加 `@dataclass(frozen=True, slots=True) class SkeletonLimits: field_bytes: int; message_bytes: int`；加 `DEFAULT_SKELETON_LIMITS = SkeletonLimits(field_bytes=4 * 1024, message_bytes=16 * 1024)`（注释说明默认仅供纯函数测试，生产由 route 构造）。
  - [ ] 逐函数改签名（**冻结签名**，见下）：`_maybe_inline_state_field` 内 `settings.*` 改为 `limits.*`；`skeleton_message` 调 `skeleton_part(part, budget=budget, limits=limits)`；`skeleton_messages` 调 `skeleton_message(message, limits=limits)`。
  - [ ] **冻结签名**（按此实现，不留其它形态）：
  ```python
  def _maybe_inline_state_field(..., budget, limits: SkeletonLimits) -> None
  def _tool(part, *, budget=None, limits=DEFAULT_SKELETON_LIMITS)
  def _patch(part, *, budget=None, limits=DEFAULT_SKELETON_LIMITS)
  def skeleton_part(part, *, budget=None, limits=DEFAULT_SKELETON_LIMITS)
  def skeleton_message(message, *, limits=DEFAULT_SKELETON_LIMITS)
  def skeleton_messages(messages, *, limits=DEFAULT_SKELETON_LIMITS)
  def _project_list_sorted_and_pack(body, *, accept_encoding, limits)
  ```
  > 说明：`_maybe_inline_state_field` 的 `...` 表示既有前导参数（`thin_state, state, key, omitted`）不变，仅新增 `budget`（既有）与 `limits`（新增）尾参；`budget` 保持既有 `dict | None` 形态。
  - [ ] 删 `from .config import settings`。
  - [ ] `routes/messages.py`：`_project_list_sorted_and_pack(body, *, accept_encoding, limits)`（新增 `limits: SkeletonLimits` 尾参，签名按冻结版）；调用点 `_project_list_sorted_and_pack(body, accept_encoding=request.headers.get("accept-encoding"), limits=SkeletonLimits(field_bytes=config.skeleton_inline_output_max_bytes, message_bytes=config.skeleton_inline_output_max_message_bytes))`（config 取自 `request.app.state.config`）。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `tests/test_skeleton.py` + `tests/test_messages_routes.py` 全文件。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # skeleton.py
  @dataclass(frozen=True, slots=True)
  class SkeletonLimits:
      field_bytes: int
      message_bytes: int

  # 默认仅供直接纯函数测试；生产路径由 route 从 request.app.state.config 构造
  DEFAULT_SKELETON_LIMITS = SkeletonLimits(field_bytes=4 * 1024, message_bytes=16 * 1024)
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_skeleton.py::test_skeleton_limits_injectable_no_cross_app_leak`；`$PY -m pytest -q tests/test_messages_routes.py::test_messages_route_uses_per_app_skeleton_limits`；回归 `$PY -m pytest -q tests/test_skeleton.py tests/test_messages_routes.py`。
- **Reviewer checklist**：
  - [ ] `grep -n "from .config import settings" src/oc_slimapi/skeleton.py` 无输出。
  - [ ] 默认值与既有 settings 默认一致（4KiB/16KiB），纯函数测试行为不变。
  - [ ] route 侧构造来源唯一（`request.app.state.config`）。

### Task 9：incarnation 独立 state directory

- **目标**：P1-4——incarnation 状态移出 logs 目录；`OC_SLIMAPI_STATE_DIR` 新路径优先，legacy 路径原子迁移且**不 reset**、**不删旧文件**。
- **Files**：
  - Modify：`src/oc_slimapi/config.py`（字段区加 `state_dir`；validate 区校验非空）
  - Modify：`src/oc_slimapi/app.py`（L385-390 IncarnationStore 构造）
  - Modify：`src/oc_slimapi/turn_registry.py`（L64-199 IncarnationStore）
  - Modify：`deploy/oc-slimapi.service`（Environment 区 L36-38 附近）
  - Test：`tests/test_turn_registry.py`（新增用例）
  - Docs：`docs/operations.md`（§5 状态路径 + 新增小节）、`CHANGELOG.md`
- **Interfaces**：Consumes：`settings.state_dir`、旧 `access_log_dir`。Produces：`IncarnationStore(state_dir, legacy_state_dir)` + 迁移语义。
- **禁止修改**：`TurnRegistry` 语义、`_INCARNATION_FILENAME` 文件名、`load_or_bump` 返回类型。
- **Acceptance Criteria**：
  - `T9-C1`：仅 legacy 文件存在（值为 5）时 `load_or_bump()` 返回 6，新路径文件写入 6，旧文件仍为 5（迁移保单调）。
  - `T9-C2`：新旧路径均存在（新=10，旧=5）→ 返回 11（**新路径优先**）。
  - `T9-C3`：迁移后旧文件**保留**（不删除，防回滚风险）。
  - `T9-C4`：新路径写失败 → **仍返回计算出的 inc（base+1）**且**不 crash**（既有 best-effort 语义保持；**无固定 fallback**）。
  - `T9-C5`：unit 增加 `Environment=OC_SLIMAPI_STATE_DIR=%S/oc-slimapi`。
  - `T9-C6`：docs/operations 与 CHANGELOG 记录状态路径行为。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] `tests/test_turn_registry.py` 新增 5 用例（legacy 迁移、新优先、**新损坏回退 legacy**、旧保留、**新写失败仍返回 base+1**）；先跑确认**红**（构造器无 `legacy_state_dir`）。
  - [ ] `config.py` 加 `state_dir: str = os.getenv("OC_SLIMAPI_STATE_DIR", "state")`；validate 拒绝空串。
  - [ ] `turn_registry.py`：`IncarnationStore.__init__(self, state_dir: str, legacy_state_dir: str | None = None)`；`load_or_bump()` 实现迁移逻辑（见形状）。
  - [ ] `app.py:387` → `IncarnationStore(state_dir=settings.state_dir, legacy_state_dir=access_log_dir)`。
  - [ ] `deploy/oc-slimapi.service` 加 `Environment=OC_SLIMAPI_STATE_DIR=%S/oc-slimapi`（注释说明 state 与 logs 分离）。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `tests/test_turn_registry.py` 全文件。
  - [ ] 更新 docs/operations 与 CHANGELOG。
  - [ ] 记录 diff。
- **实现形状**（**冻结实现，不留 `...`**）：
  ```python
  # turn_registry.py
  class IncarnationStore:
      def __init__(self, state_dir: str, legacy_state_dir: str | None = None) -> None:
          # 保存两个 path：新 state_dir 与 legacy（旧 access_log dir）
          self._path = Path(state_dir) / _INCARNATION_FILENAME
          self._legacy_path = (
              Path(legacy_state_dir) / _INCARNATION_FILENAME
              if legacy_state_dir else None
          )

      def _read_path(self, path: Path) -> tuple[bool, int]:
          """返回 (exists_and_valid, value)。

          missing → 静默 (False, 0)；empty/corrupt/unreadable → (False, 0) 并按
          现有策略记录 warning（与 _read_persisted 的日志语义一致）。
          """
          if path is None or not path.exists():
              return False, 0
          try:
              text = path.read_text(encoding="utf-8").strip()
          except OSError:
              logger.warning(
                  "turn-registry: unreadable incarnation file %s; treating as fresh",
                  path, exc_info=True,
              )
              return False, 0
          if not text:
              logger.warning("turn-registry: empty incarnation file %s; treating as fresh", path)
              return False, 0
          try:
              value = int(text)
          except ValueError:
              logger.warning("turn-registry: corrupt incarnation file %s; treating as fresh", path)
              return False, 0
          if value < 0:
              logger.warning("turn-registry: negative incarnation in %s; treating as fresh", path)
              return False, 0
          return True, value

      def load_or_bump(self) -> int:
          # 1) 先 _read_path(new)：valid 则用新值
          # 2) 否则 _read_path(legacy)：valid 则用旧值（新路径损坏但旧路径有效时用旧，
          #    避免 incarnation 回退；仅新值不存在/损坏才回退）
          # 3) 否则 base = 0（全新）
          # inc = base + 1；只写新路径；旧文件永不删除。
          valid, base = self._read_path(self._path)
          if not valid and self._legacy_path is not None:
              valid, base = self._read_path(self._legacy_path)
          inc = base + 1
          # 只写新路径；写失败仍返回计算出的 inc（best-effort，不 crash，不返回固定 fallback）
          if not self._write_persisted(inc):
              logger.warning(
                  "turn-registry: failed to persist incarnation %d to %s; "
                  "using value in-memory only (restart may re-read a stale value)",
                  inc, self._path,
              )
          return inc
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_turn_registry.py` 全文件（5 个新用例：`test_legacy_migration_preserves_monotonicity`、`test_new_path_preferred_over_legacy`、`test_corrupt_new_path_falls_back_to_legacy`、`test_legacy_file_remains_after_migration`、`test_unwritable_new_path_returns_computed_inc`）。
- **Reviewer checklist**：
  - [ ] 迁移单调（+1），不存在"读到旧值当新值用"的 reset。
  - [ ] 旧文件永不删除；新路径 mkdir 由 `_write_persisted` 的 `parent.mkdir` 覆盖。
  - [ ] unit 环境变量与 docs 一致。

### Task 10：traffic snapshot retention

- **目标**：P2-1——snapshot 每日文件按 `traffic_snapshot_retain_days` 清理；0 = 不删。
- **Files**：
  - Modify：`src/oc_slimapi/config.py`（字段 `traffic_snapshot_retain_days`；validate `>= 0`）
  - Modify：`src/oc_slimapi/traffic_snapshot.py`（新增 `prune_old_snapshots` + 严格匹配 regex）
  - Modify：`src/oc_slimapi/access_log.py`（`run_access_log_maintenance_loop` 加 `extra_prune` 参数）
  - Modify：`src/oc_slimapi/app.py`（维护循环调用点传 `extra_prune` partial）
  - Modify：`deploy/oc-slimapi.service`（Environment 加 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS=30`）
  - Test：`tests/test_traffic_snapshot.py`（新增）+ `tests/test_access_log.py`（维护循环调用断言）+ `tests/test_config.py`（校验）
  - Docs：`docs/manual/traffic-accounting.md`、`docs/operations.md`、`CHANGELOG.md`
- **Interfaces**：Consumes：`date`/`Path`/严格文件名。Produces：`prune_old_snapshots(directory: Path, stem: str, retain_days: int, today: date) -> int`。
- **禁止修改**：snapshot 帧内容、`TrafficSnapshotter._loop` 的写帧逻辑、access log 的 compress/prune 行为。
- **Acceptance Criteria**：
  - `T10-C1`：`retain_days=0` → no-op（返回 0，不删）。
  - `T10-C2`：边界保留（日期 == `today - retain_days` 的文件不删；更早的删）。
  - `T10-C3`：早于边界的 `traffic-snapshot-YYYY-MM-DD.jsonl` 与 `traffic-snapshot-YYYY-MM-DD.jsonl.gz` 均被删（**锁定两类**）。
  - `T10-C4`：非匹配文件（`access-*.jsonl`、`foo-2026-01-01.jsonl`）不受影响。
  - `T10-C5`：维护循环每 tick 以**同一 `today`** 依次调用 access prune 与 `extra_prune(today)`（monkeypatch spy 断言调用次数与入参 `today` 一致）。
  - `T10-C6`：config validate：`traffic_snapshot_retain_days < 0` → RuntimeError。
  - `T10-C7`：unit 设置 `OC_SLIMAPI_TRAFFIC_SNAPSHOT_RETAIN_DAYS=30`。
  - `T10-C8`：docs/manual + operations + CHANGELOG 更新。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 新增 prune 测试用例（T10-C1..C4）；维护循环用例（T10-C5）；config 校验用例（T10-C6）；先跑确认**红**。
  - [ ] `traffic_snapshot.py` 加 `prune_old_snapshots(directory: Path, stem: str, retain_days: int, today: date) -> int`：严格匹配，边界保留；**锁定删除 `.jsonl` 与 `.jsonl.gz` 两类**；regex **由 `re.escape(stem)` 动态构造**（支持自定义 `traffic_snapshot_path` stem，非硬编码 `traffic-snapshot`）。
  - [ ] `access_log.py` `run_access_log_maintenance_loop(*, dir, retain_days, interval_s, stop_event, extra_prune: Callable[[date], int] | None = None)`（即冻结接口 `run_access_log_maintenance_loop(..., extra_prune=...)` 的展开——`...` = 既有 keyword-only 参数 `dir/retain_days/interval_s/stop_event` 不变）：维护循环用**同一 `today`** 依次传给 access prune 与 `extra_prune(today)`（异常 caught + warning）。
  - [ ] `app.py` 维护循环调用点：从 `Path(settings.traffic_snapshot_path)` 派生 `snapshot_dir = Path(settings.traffic_snapshot_path).parent` 与 `stem = Path(settings.traffic_snapshot_path).stem`，`extra_prune=functools.partial(prune_old_snapshots, directory=snapshot_dir, stem=stem, retain_days=settings.traffic_snapshot_retain_days)`——**partial 只绑定 directory/stem/retain_days**；`today` 由维护循环的同一 `today` 传入 `extra_prune(today)`（此形态已锁定）。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] unit 加 env；更新 docs 三处 + CHANGELOG。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # traffic_snapshot.py —— 冻结实现：删除 .jsonl 与 .jsonl.gz 两类；regex 由 re.escape(stem) 动态构造
  def _snapshot_file_re(stem: str) -> re.Pattern:
      # 支持自定义 traffic_snapshot_path 的 stem（非硬编码 "traffic-snapshot"）；
      # re.escape 防 stem 含正则元字符
      return re.compile(rf"^{re.escape(stem)}-(\d{{4}}-\d{{2}}-\d{{2}})\.jsonl(\.gz)?$")

  def prune_old_snapshots(directory: Path, stem: str, retain_days: int, today: date) -> int:
      if retain_days <= 0:
          return 0
      deadline = date.fromordinal(today.toordinal() - retain_days)
      pattern = _snapshot_file_re(stem)
      count = 0
      for p in directory.glob(f"{stem}-*.jsonl*"):
          m = pattern.match(p.name)
          if not m:
              continue
          try:
              file_date = date.fromisoformat(m.group(1))
          except ValueError:
              continue
          if file_date < deadline:
              try:
                  p.unlink()
                  count += 1
              except OSError:
                  # unlink 失败必须 warning，不静默 pass
                  logger.warning("traffic-snapshot prune: failed to remove %s", p, exc_info=True)
      return count
  ```
  ```python
  # access_log.py run_access_log_maintenance_loop 内（每 tick，用同一 today）
  today = date.today()
  await asyncio.to_thread(prune_old_access_logs, dir, retain_days, today)
  if extra_prune is not None:
      try:
          await asyncio.to_thread(extra_prune, today)   # 同一 today 传给 snapshot prune
      except Exception:
          log.warning("Access log maintenance extra_prune failed", exc_info=True)
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_traffic_snapshot.py::test_prune_retain_zero_noop`（及 `::test_prune_keeps_boundary`、`::test_prune_deletes_old`、`::test_prune_deletes_gz_too`、`::test_prune_ignores_unrelated_files`）、`$PY -m pytest -q tests/test_access_log.py::test_maintenance_loop_calls_extra_prune`。
- **Reviewer checklist**：
  - [ ] 严格 regex 由 `re.escape(stem)` 动态构造，不误删 `access-*`/无关文件；删除覆盖 `.jsonl` 与 `.jsonl.gz` 两类。
  - [ ] unlink 失败必须 warning（不静默 pass）。
  - [ ] `extra_prune` 异常被维护循环捕获（不 kill 循环）；`today` 单一来源（同一 `today` 传 access prune 与 `extra_prune(today)`）。
  - [ ] app 侧 partial 只绑定 `directory/stem/retain_days`（stem 派生自 `Path(settings.traffic_snapshot_path)`）。
  - [ ] `retain_days=0` 为安全默认（不删）。

### Task 11：actions 子进程环境 allowlist

- **目标**：P2-2——action 子进程只继承固定 allowlist 环境，fail-closed 剔除 `OC_SLIMAPI_*` 等 sidecar 专属变量。
- **Files**：
  - Modify：`src/oc_slimapi/actions.py`（常量区 L43-77 加 allowlist；`_spawn` L752-766）
  - Test：`tests/test_actions.py`（新增）+ 运行既有 actions 测试
  - Docs：`docs/operations.md`（§11.3 安全注意事项）、`CHANGELOG.md`（Security）
- **Interfaces**：Consumes：`os.environ`。Produces：`_build_action_env(source: Mapping[str, str] | None = None) -> dict[str, str]`。
- **禁止修改**：`_spawn` 的其它参数（`cwd`/`start_new_session`/pipes）、manifest 校验、audit、admission。
- **Acceptance Criteria**：
  - `T11-C1`：`_build_action_env()` 只复制 allowlist 中**存在**的键。
  - `T11-C2`：`OC_SLIMAPI_*` 变量被剔除（即使 allowlist 未含也天然排除；测试显式断言）。
  - `T11-C3`：`_spawn` 传 `env=_build_action_env()`。
  - `T11-C4`：既有 actions 测试（`tests/test_actions.py`、`tests/test_actions_routes.py`）全绿（行为不变——现有 action 如 `/usr/bin/systemctl --user` 依赖 `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR`，allowlist 含这两项）。
  - `T11-C5`：docs/operations §11.3 + CHANGELOG Security 记录。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] `tests/test_actions.py` 新增 `test_build_action_env_copies_only_allowlist_keys` 与 `test_build_action_env_drops_oc_slimapi_vars`；先跑确认**红**（函数不存在）。
  - [ ] `actions.py` 加常量 `_ACTION_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")` 与 `_build_action_env`（形状见下；注释：fail-closed allowlist，不做"名称含 secret"模糊规则）。
  - [ ] `_spawn` 的 `create_subprocess_exec` 加 `env=_build_action_env()`。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `$PY -m pytest -q tests/test_actions.py tests/test_actions_routes.py`。
  - [ ] 更新 docs/operations §11.3 与 CHANGELOG Security。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # actions.py
  _ACTION_ENV_ALLOWLIST = (
      "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
      "TMPDIR", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
  )

  def _build_action_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
      env = dict(os.environ) if source is None else dict(source)
      return {k: v for k, v in env.items() if k in _ACTION_ENV_ALLOWLIST}
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_actions.py::test_build_action_env_copies_only_allowlist_keys`、`$PY -m pytest -q tests/test_actions.py::test_build_action_env_drops_oc_slimapi_vars`。
- **Reviewer checklist**：
  - [ ] allowlist 覆盖现有 manifest 的 `systemctl --user` 依赖（DBUS/XDG）。
  - [ ] 无模糊"name contains secret"规则；行为 fail-closed。
  - [ ] 既有动作测试未受影响（无 action 依赖被剔除变量）。

### Task 12：TransformPool metrics counters

- **目标**：P2-3——`snapshot_metrics()` 不再读 `_semaphore._value/_waiters`，内部维护 `_active`/`_waiting` 计数器。
- **Files**：
  - Modify：`src/oc_slimapi/transform.py`（`TransformPool.__init__` L189-195、`__aenter__` L201-209、`__aexit__` L211-212、`snapshot_metrics` L229-248）
  - Test：`tests/test_transform.py`（新增用例）
- **Interfaces**：Consumes：`asyncio.Semaphore`。Produces：`snapshot_metrics() -> {"active": int, "waiting": int}`（字段名不变，HubRegistry 调用方兼容）。
- **禁止修改**：admission 语义（超时/等待行为）、`offload`、`shutdown`。
- **Acceptance Criteria**：
  - `T12-C1`：`__aenter__` 成功 → `_active` 增 1；`__aexit__` → 减 1。
  - `T12-C2`：等待期（semaphore 被占满）→ `_waiting` 增 1；进入后 `_waiting` 减 1。
  - `T12-C3`：超时（TransformBusy）→ `_waiting` 不泄漏（归零）。
  - `T12-C4`：等待期取消 → `_waiting` 不泄漏。
  - `T12-C5`：`snapshot_metrics()` 返回值与 `_active/_waiting` 一致，且**不再读私有字段**（`grep "_value\|_waiters"` 无命中）。
  - `T12-C6`：既有 transform 测试全绿（admission 语义未变）。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 新增 `tests/test_transform.py` 用例（T12-C1..C4）；先跑确认**红**（无计数器）。
  - [ ] `transform.py`：`__init__` 加 `self._active = 0`、`self._waiting = 0`；`__aenter__` 在 acquire 前 `self._waiting += 1`、`finally: self._waiting -= 1`、acquire 成功后 `self._active += 1`；`__aexit__` 先 `self._active -= 1` 再 `release()`；`snapshot_metrics()` 改为返回计数器。
  - [ ] 目标测试**绿**；`git diff --check`。
  - [ ] 跑 `tests/test_transform.py` 全文件。
  - [ ] 记录 diff。
- **实现形状**：
  ```python
  # transform.py TransformPool
  def __init__(self, config: TransformConfig) -> None:
      self._config = config
      self._semaphore = asyncio.Semaphore(config.max_transforms)
      self._executor = ThreadPoolExecutor(max_workers=config.max_transforms, thread_name_prefix="oc-slimapi-transform")
      self._active = 0
      self._waiting = 0

  async def __aenter__(self) -> "TransformPool":
      self._waiting += 1
      try:
          try:
              await asyncio.wait_for(self._semaphore.acquire(), timeout=self._config.transform_wait_seconds)
          except TimeoutError as exc:
              raise TransformBusy() from exc
      finally:
          self._waiting -= 1
      self._active += 1
      return self

  async def __aexit__(self, exc_type, exc, tb) -> None:
      self._active -= 1
      self._semaphore.release()

  def snapshot_metrics(self) -> dict[str, int]:
      return {"active": self._active, "waiting": self._waiting}
  ```
- **目标测试与命令**：`$PY -m pytest -q tests/test_transform.py::test_metrics_active_increments_and_decrements`（及 `::test_metrics_waiting_increments_while_blocked`、`::test_metrics_waiting_not_leaked_on_timeout`、`::test_metrics_waiting_not_leaked_on_cancel`）。
- **Reviewer checklist**：
  - [ ] 单事件循环模型下无锁正确（计数器只在 loop 线程改）。
  - [ ] 超时/取消路径 finally 保证 `_waiting` 回退。
  - [ ] `snapshot_metrics` 字段名不变（`active`/`waiting`），HubRegistry 无连带改动。

### Task 13：文档漂移修复（仅描述，不 bump wire）

- **目标**：修复审查报告"文档漂移清单"D1-D7。**本 task 明确授权修改 `docs/specs/v2-contract.md` 与 `docs/specs/INTERFACE_MAP.md`，但只修描述性文字，禁止改 wire version、禁止增删/改路由 method/path inventory（防 check_routes_doc gate 破裂）、禁止改任何代码。**
- **Files**：
  - Modify：`docs/specs/v2-contract.md`（D1/D2/D3/D5 相关段落）
  - Modify：`docs/specs/INTERFACE_MAP.md`（D2/D3 描述校准——仅措辞层，不增删/改路由 method/path inventory）
  - Modify：`docs/specs/CLIENT_CHANGES.md`（D4/D5）
  - Modify：`docs/operations.md`（D2/D6/D7）
  - Modify：`docs/manual/traffic-accounting.md`（D6）
- **Interfaces**：Consumes：审查报告漂移清单 + 代码证据。Produces：校准后的文档。
- **禁止修改**：除上述 5 个文档外的任何文件；`v2-contract.md` 中 wire version 数字、路由表行；**INTERFACE_MAP 可修 D2/D3 描述，但禁止增删/改路由 method/path inventory**（防 check_routes_doc gate 破裂）；禁止改任何代码。
- **Acceptance Criteria**：
  - `T13-C1`：D1 legacy-only 文字对照当前路由面清理（注明 legacy 仅指 opencode 上游 API）。
  - `T13-C2`：D2 `slimapi_contract` 描述明确为静态常量（代码 `health.py:23` 硬编码 2，bump 需同步改代码）。
  - `T13-C3`：D3 `directory_not_allowed` 适用范围统一为 **messages 与 token-stream 的 query/header conflict** 结构性守卫（对照 `routes/messages.py:230`、`routes/token_stream.py:67` 与 `v2-contract.md:449/452`、`INTERFACE_MAP.md:89`）。
  - `T13-C4`：D4 CLIENT_CHANGES token-stream `truncated`/`session_deleted` 措辞对照 `sse/tokenstream/hub.py:922-974` 校准。
  - `T13-C5`：D5 gzip 措辞明确三层语义：**控制面 `/slimapi/events` 不 gzip**；**token stream `/slimapi/sessions/{sid}/stream` 可按 `Accept-Encoding: gzip` 压缩**；**普通 JSON/catalog 按内容协商且仅 beneficial 时压缩**（不得写成「SSE 都不 gzip」）。
  - `T13-C6`：D6 access log `ts` 语义明示为请求完成（end）时刻。
  - `T13-C7`：D7 incarnation 状态路径运维说明补齐——Task 13 在 Batch 4 之后执行，**以 Task 9 已落地的新 state_dir 为现状**（写明 `OC_SLIMAPI_STATE_DIR` 路径与迁移行为），**不写"预告"措辞**。
  - `T13-C8`：全文 wire version 数字不变（`grep -rn "2,2\|版本 2\|v2"` 对比 baseline 无新增变更）；**INTERFACE_MAP 路由 inventory（method/path 行）不变**，仅允许 D2/D3 描述校准。
  - `T13-C9`：`./scripts/check.sh` 通过（route↔INTERFACE_MAP gate 不破裂）。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 逐条（D1→D7）定位文档现状与代码证据，写出拟改文字（先在执行记录中列变更清单，避免顺手大改）。
  - [ ] 按清单修改 5 个文档；每处修改仅限措辞/描述层。
  - [ ] `git diff` 全量审阅确认无 wire 数字变化、无路由 method/path inventory 增删。
  - [ ] 跑 `./scripts/check.sh`。
  - [ ] 记录 diff。
- **实现形状**：无代码；以变更清单 + diff 为产物。示例（D2，v2-contract/operations）：
  ```markdown
  `/slimapi/health` 的 `slimapi_contract` 是**静态常量 2**（`routes/health.py` 硬编码），
  不随 `SERVER_API_VERSION` 自动派生；任何 wire 契约 bump 必须同时改代码该常量并更新本文档。
  ```
- **目标测试与命令**：`./scripts/check.sh`；`git diff docs/ | grep -n "X-Slimapi-Version"` 对比 baseline。
- **Reviewer checklist**：
  - [ ] 仅 5 个文档被改；无代码改动；INTERFACE_MAP 仅描述校准（D2/D3），路由 method/path inventory 零变化。
  - [ ] wire 版本/路由表文字零变化；check.sh 绿。

### Task 14：CI / quality baseline

- **目标**：quality baseline 最小化——`scripts/check.sh` 默认纳入 compileall + 文档；CI 工作流是**决策门禁**（仅在用户确认托管平台支持 GitHub Actions 后创建，否则 BLOCKED）。
- **Files**：
  - Modify：`scripts/check.sh`（L27-36 模式分支）
  - Create（仅审批后）：`.github/workflows/ci.yml`
  - Docs：`docs/develop.md`（quality gate 说明）
- **Interfaces**：Consumes：`compileall`（stdlib）。Produces：默认门禁含 compileall；可选 CI。
- **禁止修改**：`pyproject.toml` 依赖、pytest 配置、`tests/` 任何文件。coverage/ruff/pip-audit **必须单独用户批准**，本 task 不引入。
- **Acceptance Criteria**：
  - `T14-C1`：决策 gate 已记录（用户答复"是 GitHub Actions"或"否/其它平台"）。
  - `T14-C2`：`./scripts/check.sh` 默认执行 compileall（`--full` 保留为等价别名或继续可用）。
  - `T14-C3`：若平台确认 → `.github/workflows/ci.yml` 存在且 matrix **固定 Python `3.11` 与 `3.14`**，job **只调用 `./scripts/check.sh`**（默认已含 compileall + pytest + route↔INTERFACE_MAP gate，**不重复手拼 pytest/route/compileall 命令**）；若平台不是 GitHub Actions → 记录 BLOCKED（不创建其它平台文件，等待用户指示平台名）。
  - `T14-C4`：docs/develop.md 更新门禁说明。
  - `T14-C5`：未新增任何第三方依赖。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] **决策 gate**：用 `question` 工具询问用户托管平台（GitHub Actions / 其它——按答复分支）。此步为必做，不得默认。
  - [ ] 若 = GitHub Actions：创建 `.github/workflows/ci.yml`——matrix **固定 Python `3.11` 与 `3.14`**；job 命令**只调用 `./scripts/check.sh`**（默认已含 compileall），**不重复手拼 pytest/check_routes_doc/compileall 命令**；`pip install -e '.[test]'`。
  - [ ] 若 ≠ GitHub Actions：记录 BLOCKED，等待平台名；本 task 只做 compileall 与文档部分。
  - [ ] `check.sh`：默认路径也执行 compileall（把 `--full` 的 compileall 提前到默认；`--full` 保持接受）。
  - [ ] 本地跑 `./scripts/check.sh` 验证默认含 compileall。
  - [ ] 更新 docs/develop.md。
  - [ ] 记录 diff 与决策结果。
- **实现形状**：
  ```bash
  # scripts/check.sh 默认也含 compileall；--full 保持兼容
  case "$MODE" in
    --full|default|"")
      "$PY" -m compileall -q src ;;
    *)
      echo "用法: check.sh [--full]" ; exit 1 ;;
  esac
  ```
- **目标测试与命令**：`./scripts/check.sh`（默认）与 `./scripts/check.sh --full` 均通过。
- **Reviewer checklist**：
  - [ ] 决策 gate 有用户答复记录；未擅自创建 CI。
  - [ ] 无新依赖；check.sh 两种模式均可用。

### Task 15：maintainability refactors（只定义后续计划入口）

- **目标**：**只**产出后续独立计划入口文档/记录，不与本批次修复混做、不实施。
- **Files**：Create（固定路径）：`docs/maintainability-refactor-plan.md`（**不修改本计划文件自身**，不追加附录）。
- **Interfaces**：Consumes：审查报告 P2-4/P2-5/P2-6 与 P1-6 的 busy_response 收敛点。Produces：后续计划清单。
- **Acceptance Criteria**：
  - `T15-C1`：`docs/maintainability-refactor-plan.md` 存在并列出：`GlobalHub.publish` 拆分、prune helper 统一（access log 与 snapshot 的 prune 复用）、`busy_response` 单一来源（`messages._busy_response` 收敛到 `_catalog_common`）、app services 组装（lifespan 接线抽取）、test builder/fake clock。
  - `T15-C2`：明确"不在当前执行批次实施"。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 撰写入口文档（每项含目标/范围/风险/验收方向）。
  - [ ] reviewer 确认无代码改动。
- **实现形状**：文档清单（无代码）。
- **目标测试与命令**：无代码测试；`git diff` 仅含文档。
- **Reviewer checklist**：
  - [ ] 无任何 src/tests 改动；不声称已实施。

### Task 16：production traffic evidence（只读）

- **目标**：按 `docs/manual/traffic-accounting.md` 聚合 3 天生产 access log 的 `bucket=="passthrough"` 请求，输出机会报告。**禁止读取/记录 query、headers、clientId 原值、消息内容。**
- **Files**：
  - Create：`docs/traffic-opportunity-report-<execution-date>.md`（`<execution-date>` 为实际执行日期，非硬编码）
- **Interfaces**：Consumes：生产 access log（`~/.local/state/oc-slimapi/logs/access-YYYY-MM-DD.jsonl`，`RETAIN_DAYS=3`）。Produces：聚合报告。
- **禁止修改**：任何代码；日志原文件。
- **Acceptance Criteria**：
  - `T16-C1`：报告存在，含 3 天窗口说明与只读聚合命令。
  - `T16-C2`：聚合按 `method+path` 分组的 `requests`/`upIn`/`downOut`（排序表）。
  - `T16-C3`：报告不含 query、headers、clientId 原值、消息内容（隐私纪律）。
  - `T16-C4`：若无生产日志（路径无文件/无 passthrough 记录）→ **BLOCKED** 记录（不编造数据）。
- **步骤**：
  - [ ] 记录 baseline HEAD。
  - [ ] 确认日志路径存在且可读；列出窗口内 `access-*.jsonl(.gz)`。
  - [ ] 用只读 Python heredoc（标准库 `gzip, json, re, collections, pathlib, datetime`，见实现形状）读取**最近 3 天**的 `.jsonl` 与 `.jsonl.gz`，过滤 `bucket=="passthrough"`，按 `(method,path)` 累加 `requests/upIn/downOut`，按 `upIn` 降序取 top。
  - [ ] 写报告：方法、只读聚合命令、top 表、与 INTERFACE_MAP 对照的"可省流候选"。
  - [ ] 复核报告无敏感字段。
  - [ ] 若无日志 → BLOCKED 记录。
- **实现形状**：只读 Python heredoc 聚合示例（标准库；只解析 `method/path/bucket/upIn/downOut`，**不输出 clientId/query/headers/消息内容**）：
  ```bash
  python3 - <<'PY'
  import gzip, json, re, collections, pathlib, datetime as dt

  LOG_DIR = pathlib.Path("~/.local/state/oc-slimapi/logs").expanduser()
  NAME_RE = re.compile(r"^access-(\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$")
  today = dt.date.today()
  window_start = today - dt.timedelta(days=2)      # 最近 3 天（含今天）
  agg = collections.defaultdict(lambda: [0, 0, 0])  # (method, path) -> [requests, upIn, downOut]

  def _num(v):
      # upIn/downOut 只接受 int/float（bool 排除）；否则按 0，避免坏日志 TypeError
      return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

  for p in sorted(LOG_DIR.iterdir()):
      m = NAME_RE.fullmatch(p.name)                # 严格文件名日期过滤（不用 substring 切片）
      if not m:
          continue
      file_date = dt.date.fromisoformat(m.group(1))
      if not (window_start <= file_date <= today):
          continue
      opener = gzip.open if p.name.endswith(".gz") else open
      with opener(p, "rt", encoding="utf-8") as fh:
          for line in fh:
              try:
                  rec = json.loads(line)
              except json.JSONDecodeError:
                  continue
              if rec.get("bucket") != "passthrough":
                  continue
              method, path = rec.get("method"), rec.get("path")
              if not isinstance(method, str) or not isinstance(path, str):
                  continue                          # method/path 非 str 跳过，避免 TypeError
              key = (method, path)
              r = agg[key]
              r[0] += 1
              r[1] += _num(rec.get("upIn"))
              r[2] += _num(rec.get("downOut"))

  for (method, path), (n, up, down) in sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True):
      print(f"{n}\t{up}\t{down}\t{method}\t{path}")
  PY
  ```
- **目标测试与命令**：无 pytest；产物为报告文件。
- **Reviewer checklist**：
  - [ ] 报告不含敏感字段；只读未改日志。
  - [ ] 聚合口径与 manual 一致。

### Task 17：product route selection/design（只有 Task 16 证据后执行）

- **目标**：为 top 1-2 只读路径写独立 design/spec（候选 children/diff/file/providers），**不写实现**；须用户批准后再另开实施计划。
- **Files**：
  - Create：`docs/specs/traffic-route-<name>-<execution-date>.md`（每候选一个；`<execution-date>` 为实际执行日期，非硬编码）
- **Interfaces**：Consumes：Task 16 报告、`docs/specs/v2-contract.md`、opencode 上游源码（`opencode-src/current/packages/opencode/src/server/routes/instance/httpapi/handlers/`）。Produces：design 文档。
- **禁止修改**：任何代码；INTERFACE_MAP；v2-contract（除非后续正式 bump 流程）。
- **Acceptance Criteria**：
  - `T17-C1`：对 top 1-2 只读路径各产出一份 design/spec。
  - `T17-C2`：每份含：upstream schema（读上游源码核实）、client 消费字段、估算节省（基于 Task 16 数据）、T3 cap、fallback（404 thin_route_not_found 回退）、wire classification（加性/不 bump）、测试设计。
  - `T17-C3`：明确"需用户批准后再另开实施计划"。
  - `T17-C4`：无实现代码产出。
- **步骤**：
  - [ ] 记录 baseline HEAD；确认 Task 16 报告存在（否则 BLOCKED）。
  - [ ] 选 top 1-2 路径；先读 opencode 上游 handler/schema 源码核实字段。
  - [ ] 撰写 design 文档（7 要素齐全）。
  - [ ] 提交用户批准（不自动实施）。
  - [ ] 记录 diff。
- **实现形状**：design 文档（无代码）。
- **目标测试与命令**：无 pytest；reviewer + 用户批准门禁。
- **Reviewer checklist**：
  - [ ] 上游 schema 有源码证据；估算基于 Task 16 数据。
  - [ ] 无实现代码；未动契约文档。

---

## D. 批次 lane 切分（文件写域互斥）

| Batch | Lane 1 | Lane 2 | Lane 3 | 串行/说明 |
|---|---|---|---|---|
| 0 | Task 0 | — | — | 单 lane |
| 1 | **Task 1**（app.py+service） | **Task 3**（proxy.py+test_proxy） | **Task 4**（sessions.py+test_sessions） | 三个实现 lane 文件域**互不重叠**；Task 1 的 docs/operations §4、Task 3 的 INTERFACE_MAP catch-all 行描述、Task 4 的 INTERFACE_MAP sessions 行描述，以及三者 CHANGELOG，均为 **Integrator-only**，由**集成 lane** 在三个 task 完成后串行统一写入（见 E 节），避免写冲突 |
| 1b | **Task 2**（test_graceful_shutdown.py） | — | — | 依赖 Task 1 完成后串行 |
| 2 | **Task 5** | — | — | 高风险单 lane |
| 3 | **Task 6**（access_log.py+test_access_log） | **Task 8**（skeleton.py+messages.py+对应 tests） | **Task 12**（transform.py+test_transform） | 写域互斥；**Task 7**（新文档）在 Task 6 结论后作为 docs lane 并行/紧随 |
| 4 | **Task 9** → **Task 10** → **Task 11** | — | — | **串行**：9/10/11 均触碰 config/app/unit/docs，写域重叠，禁止并行 |
| 5 | **Task 13** → **Task 14** | — | — | 串行：13 独占文档写域，14 触碰 scripts |
| 6 | **Task 16** → **Task 17** | — | — | 严格顺序 |

写域规则：同一文件在同一时刻最多一个 writer；任何 batch 内 lane 若发现实际触碰同一文件 → 立即串行化，不得继续并行。

---

## E. 每 batch 末 Integration Gate

1. **范围检查**：`git diff --name-only <batch-baseline>` 只允许本 batch 各 task 的 Files 声明并集（docs/CHANGELOG、Task 1 的 docs/operations.md §4、Task 3/4 的 INTERFACE_MAP 对应行描述例外：由集成 lane 在批末统一写入后包含）。出现额外文件 → FAIL，按 H 节处理。
2. **目标测试集合**：本 batch 所有 task 的目标测试命令逐一执行，全绿。
3. **全量门禁**：Python/行为类 batch 跑 `./scripts/check.sh`；仅文档类 batch（Task 7/13/16/17 的纯文档产物批次除外，但 Task 13 必须跑 `./scripts/check.sh`）。**最终批次后**跑 `./scripts/check.sh --full`（Batch 6 完成后由 Final verifier 执行）。
4. **`git diff --check`**：无 whitespace 错误。
5. **Criterion matrix**：reviewer 按 F 节矩阵逐项 PASS/FAIL；任一 FAIL → 修复后重跑本 gate，**不得进入下一 batch**。
6. **docs/CHANGELOG 集成 lane**（Batch 1/3/4 等含多 lane 的批次）：由**集成 agent**（G 节 prompt）统一写 `CHANGELOG.md` 与受影响的 docs 段落。**Batch 1 集成范围** = Task 1 的 `docs/operations.md` §4 shutdown/restart 段 + Task 3 的 INTERFACE_MAP catch-all 行描述（exact `/slimapi` 与 `/slimapi/**` 均 sidecar 404）+ Task 4 的 INTERFACE_MAP sessions 行描述（body `retry_after:2` + 头 `Retry-After:2`）+ 三者合并写入 `CHANGELOG.md` Unreleased——由 integrator 在三个实现 lane 完成后串行更新，**禁止改动逻辑代码**。各 task reviewer 在集成后核验其 docs 准则（T1-C3/C4、T3-C5、T4-C5）。

---

## F. Criterion Ownership Matrix

> Owner 列：**TR** = task reviewer（该 task 完成后即验）；**FV** = final verifier（`check.sh --full` 阶段验）。Final-only = 仅在最终 gate 验证的项。命令中 `$PY` = `.venv/bin/python`。

| 准则 | Task | Owner | Verification Command | Final-only |
|---|---|---|---|---|
| T0-C1/C2/C3 | 0 | TR+FV | `git status --short`; `git rev-parse HEAD`; `./scripts/check.sh` | 否 |
| T1-C1/C2/C3/C4 | 1 | TR | `$PY -m pytest -q tests/test_app_main.py::test_main_passes_graceful_shutdown_timeout`；`grep TimeoutStopSec deploy/oc-slimapi.service`；docs（T1-C3/C4）在 **batch 集成后**由 reviewer 核验 | 否 |
| T1-C5 | 1 | TR | `$PY -m pytest -q tests/test_lifespan.py` | 否 |
| T2-C1/C2/C3/C4 或 C5 | 2 | TR | `$PY -m pytest -q tests/test_graceful_shutdown.py`（或 BLOCKED 记录审阅） | 否 |
| T3-C1/C2 | 3 | TR | `$PY -m pytest -q tests/test_proxy.py::test_exact_slimapi_root_not_proxied` | 否 |
| T3-C3/C4 | 3 | TR | `$PY -m pytest -q tests/test_proxy.py` | 否 |
| T3-C5 | 3 | TR | 集成后 diff 审阅 INTERFACE_MAP catch-all 行 + CHANGELOG（Integrator-only） | 否 |
| T4-C1/C2/C3 | 4 | TR | `$PY -m pytest -q tests/test_sessions_routes.py::test_sessions_transform_busy_returns_retry_after_without_upstream_call` | 否 |
| T4-C4 | 4 | TR | `$PY -m pytest -q tests/test_sessions_routes.py` | 否 |
| T4-C5 | 4 | TR | 集成后 diff 审阅 INTERFACE_MAP sessions 行 + CHANGELOG（Integrator-only） | 否 |
| T5-C1..C5 | 5 | TR | 6 个目标命令（见 Task 5）逐一 | 否 |
| T5-C6 | 5 | TR | `$PY -m pytest -q tests/test_config.py` | 否 |
| T5-C7 | 5 | TR+FV | `$PY -m pytest -q tests/test_questions_routes.py` | 否 |
| T5-C8/C9 | 5 | TR | docs/operations + INTERFACE_MAP questions 行 + CHANGELOG diff 审阅；`grep X-Slimapi-Version CHANGELOG.md` | 否 |
| T6-C1/C2 | 6 | TR | `$PY -m pytest -q tests/test_access_log.py::test_emit_writes_single_call_with_newline` | 否 |
| T6-C3 | 6 | TR | `$PY -m pytest -q tests/test_access_log.py` | 否 |
| T7-C1..C4 | 7 | TR | 文档审阅（冻结项齐全、无代码改动） | 否 |
| T8-C1 | 8 | TR | `$PY -m pytest -q tests/test_skeleton.py::test_skeleton_limits_injectable_no_cross_app_leak` | 否 |
| T8-C2/C5 | 8 | TR | `$PY -m pytest -q tests/test_skeleton.py tests/test_messages_routes.py` | 否 |
| T8-C3/C4 | 8 | TR | `grep -n "from .config import settings" src/oc_slimapi/skeleton.py`（空）；diff 审阅 messages.py | 否 |
| T8-C6 | 8 | TR | `$PY -m pytest -q tests/test_messages_routes.py::test_messages_route_uses_per_app_skeleton_limits` | 否 |
| T9-C1..C4 | 9 | TR | `$PY -m pytest -q tests/test_turn_registry.py`（5 目标用例） | 否 |
| T9-C5/C6 | 9 | TR | `grep OC_SLIMAPI_STATE_DIR deploy/oc-slimapi.service docs/operations.md` | 否 |
| T10-C1..C4 | 10 | TR | `$PY -m pytest -q tests/test_traffic_snapshot.py`（4 目标用例） | 否 |
| T10-C5 | 10 | TR | `$PY -m pytest -q tests/test_access_log.py::test_maintenance_loop_calls_extra_prune` | 否 |
| T10-C6 | 10 | TR | `$PY -m pytest -q tests/test_config.py` | 否 |
| T10-C7/C8 | 10 | TR | diff 审阅 unit/docs | 否 |
| T11-C1/C2 | 11 | TR | `$PY -m pytest -q tests/test_actions.py`（2 目标用例） | 否 |
| T11-C3 | 11 | TR | diff 审阅 `_spawn` | 否 |
| T11-C4 | 11 | TR+FV | `$PY -m pytest -q tests/test_actions.py tests/test_actions_routes.py` | 否 |
| T11-C5 | 11 | TR | diff 审阅 operations/CHANGELOG | 否 |
| T12-C1..C4 | 12 | TR | `$PY -m pytest -q tests/test_transform.py`（4 目标用例） | 否 |
| T12-C5 | 12 | TR | `grep -n "_value\|_waiters" src/oc_slimapi/transform.py`（空） | 否 |
| T12-C6 | 12 | TR+FV | `$PY -m pytest -q tests/test_transform.py` | 否 |
| T13-C1..C7 | 13 | TR | diff 审阅 5 文档 | 否 |
| T13-C8 | 13 | TR+FV | `git diff docs/` 对比 baseline（wire 数字零变化；INTERFACE_MAP 路由 inventory 零变化） | 否 |
| T13-C9 | 13 | TR | `./scripts/check.sh` | 否 |
| T14-C1/C2/C4/C5 | 14 | TR | 决策记录；`./scripts/check.sh`；diff 审阅 | 否 |
| T14-C3 | 14 | TR | CI 文件存在（仅审批后）或 BLOCKED 记录 | 否 |
| T15-C1/C2 | 15 | TR | 文档审阅；`git diff --name-only` 无 src/tests | 否 |
| T16-C1..C3 | 16 | TR | 报告审阅（无敏感字段） | 否 |
| T16-C4 | 16 | TR | BLOCKED 记录（若适用） | 否 |
| T17-C1..C4 | 17 | TR+用户 | spec 审阅 + 用户批准记录 | 否 |
| **跨批次/最终** | 全部 | FV | `./scripts/check.sh --full`；`git diff --check`；Batch 范围检查 | **是** |

---

## G. Agent prompt templates

### G.1 Implementer prompt（每个 task 一个 agent）

```text
你是 implementer。只执行「Task <N>: <名称>」这一个任务。

约束（不可违反）：
- 工作目录 <repo>。当前分支非 main：禁止 commit/tag/release。
- 只允许修改本任务 Files 声明的文件；出现额外文件即停并上报。
- 流程：记录 baseline HEAD → 先写失败测试 → 只跑目标测试确认红 → 最小实现 → 目标测试绿 → `git diff --check` → 记录 diff。
- 目标测试一开始就绿 = 测试没锁住缺陷，重写测试，不要继续实现。
- 现有测试先失败（非本任务引入）→ 停并上报，不修无关测试。
- 禁止新增第三方依赖、禁止 wire bump、禁止自由重构、禁止改 docs/specs/v2-contract.md（除非任务=Task 13）。
- 完成后不要 commit。向 orchestrator 输出：改动文件清单、目标测试命令与结果、diff 摘要。
```

### G.2 Reviewer prompt（只读检查该 task diff + 准则）

```text
你是 reviewer。审阅「Task <N>: <名称>」的 diff（只读，不修改任何文件）。

过程：
- `git diff` 该 task 的改动；`git status` 确认无越界文件。
- 逐条跑 F 节矩阵中该 task 的验证命令（目标测试 + 回归 + grep 断言）。
- 按本任务 Reviewer checklist 逐项核验（见 C 节）。
- 禁止提出新功能或超出任务范围的"顺手改进"。
- 输出：每条准则 PASS/FAIL；任一 FAIL 明确列出修复点。不得批准含未知越界的 diff。
```

### G.3 Batch integrator prompt（仅处理冲突与统一 docs/CHANGELOG）

```text
你是 batch integrator，负责 Batch <N> 的 docs/CHANGELOG 集成。

约束：
- 只处理文档冲突与统一写入 CHANGELOG/受影响的 docs 段落；禁止改任何逻辑代码（src/、tests/、scripts/、deploy/ 的实现部分）。
- 将本 batch 各 task 的 CHANGELOG 条目合并到 Unreleased 段（每 task 一条，行为级描述，不写实现细节）。
- **Batch 1**（Integrator-only 清单，三个实现 lane 1/3/4 完成后由你串行更新，实现 lane 不得触碰）：
  - Task 1：`docs/operations.md` §4 shutdown/restart 段；
  - Task 3：`docs/specs/INTERFACE_MAP.md` catch-all 行描述——明确 exact `/slimapi` 与 `/slimapi/**` 均 sidecar 404（只改描述，不增删路由 inventory）；
  - Task 4：`docs/specs/INTERFACE_MAP.md` sessions 行描述——记录 body `retry_after:2` + 头 `Retry-After:2`（只改描述，不增删路由 inventory）；
  - 三者合并写入 `CHANGELOG.md` Unreleased。
- 若多个 task 需要 docs/operations.md 同一小节，按语义合并为一段，不得互相覆盖。
- 完成后跑 `git diff --name-only <batch-baseline>` 核对范围，输出合并 diff 供 reviewer（含 Task 1/3/4 reviewer）复核。
```

### G.4 Final verifier prompt（fresh context 跑全门禁）

```text
你是 final verifier（fresh context，不依赖任何中间结论）。

过程：
- 先读 docs/implementation-batches-2026-08-09.md 的 F 节矩阵与 E 节 Integration Gate。
- `git status --short`、`git rev-parse HEAD`；`git diff --check`。
- 跑 `./scripts/check.sh --full`（含 compileall）。
- 抽查 F 节标 TR+FV 的准则命令。
- 输出：全部门禁逐项证据（命令 + 结果 + 输出片段）；任一 FAIL 即整体 FAIL 报告，不得通过。
```

---

## H. 失败处理与回滚

1. **不使用 `git reset` / `git checkout` 丢弃任何改动**（可能误伤用户未提交内容）。撤销某 task 的 diff 一律通过 `git apply -R <该 task 的 diff 文件>`（反向 apply）或 `apply_patch` 反向撤销，且只针对该 task 的改动文件。
2. **writer 越界**（改动了 Files 之外的文件）：立即停止该 lane，保留现场，交人工审查后再决定撤销或继续。
3. **目标测试一开始就绿**：说明测试没有锁住缺陷，**必须重写测试**（更强断言或更贴近真实路径），不得"测试已过就继续"。
4. **现有测试 baseline 红**：不是本任务引入的 → 停并上报；**禁止修无关测试**来"让绿"。
5. **BLOCKED 判定**（Task 2/14/16/17 等）：必须产出**书面 BLOCKED 记录**（含证据与失败原因），由 orchestrator 决定是否跳过/改期；不允许以脆弱手段（sleep、跳过断言）替代。
6. **batch gate FAIL**：该 batch 不回退其它已通过 task，只修 FAIL 项（仍走 fresh implementer/reviewer），重跑 E 节 gate。

---

## I. 执行选项

- **推荐**：ocmar-subagent-driven-development（每 task 独立 implementer/reviewer 子 agent，天然满足"fresh + 不自审"约束）。备选：ocmar-executing-plans。
- **不询问用户**：本计划不要求用户在启动前做选择（唯一例外是 Task 14 的托管平台决策 gate 与 Task 17 的 spec 批准，这两个是任务内部必做的 question 调用）。
- **执行起点**：未来任何执行都必须从 **Task 0** 开始（记录基线），不得直接跳入任一 task。
- 本计划与审查报告（`docs/architecture-quality-review-2026-08-09.md`）配套；批次顺序、门禁、准则矩阵以本文件为准。
