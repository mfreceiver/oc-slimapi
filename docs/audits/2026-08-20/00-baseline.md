# Phase 0 基线（00-baseline.md）

## 快照锚点（U0.1 / U0.8 / U0.9 / U0.10）

- BASELINE_HEAD：`0b836e78c5de62d0c73b8593bf62c6650043dedf`（短 `0b836e7`，`release: v4.4.0`）
- 审计时间窗起始：2026-08-20T22:46:16+08:00（U0.0 执行时刻）；Phase 0 完成：2026-08-20T23:0x+08:00
- `git status --porcelain` 起点：仅 `?? docs/ocmar/plans/2026-08-20-v4-comprehensive-audit.md`（untracked 非忽略）
- attempt 判定：**新建分支**（`docs/audits/2026-08-20/` 不存在 → 排他创建）；attempt-id = `primary`；AUDIT_ROOT = 仓库内 `docs/audits/2026-08-20/`
- **U0.0/U0.9 symlink 验证记录**（全部通过，无 fallback 触发）：
  - 写入根 `docs/audits/` 链：`/home`、`/home/mar`、`/home/mar/personal_projects`、`…/oc-slimapi`、`…/docs` 均真实目录（无 symlink）；`docs/audits`、`docs/audits/2026-08-20` 当时不存在 → 排他新建。
  - 写入根 `/tmp/opencode/` 链：`/tmp`（01777 真实目录）、`/tmp/opencode`（0775 真实目录）→ 通过。
  - 只读执行根 `.venv/`：真实目录（非 symlink）→ 允许只读使用。
  - 命名空间根 `/tmp/opencode/{probes,runtime-cache,baseline-snapshot}`：逐级排他创建，链上无 symlink。
- **基线冻结（U0.2(b)/U0.10）**：唯一入口 `freeze_baseline.py` 已产出三件套——`manifest/input-paths.txt`（261 个存在输入文件）、`manifest/deleted-paths.txt`（**空墓碑**）、`manifest/file-hash-manifest.txt`（261 条 sha256）。
- **脏基线判定：dirty=true**（`modified_tracked=[]`；`untracked_inputs=[docs/ocmar/plans/2026-08-20-v4-comprehensive-audit.md]`；墓碑空）→ 惰性启用 baseline-snapshot 命名空间 `/tmp/opencode/baseline-snapshot/0b836e78…/primary/`（261 文件全量复制 + sha256 复验一致 + `namespace-manifest.json` + `COMPLETE` 哨兵）。后续基线内容读取优先走该副本（§0.4 例外规则的载体）。
- ignored 副产物基线（U0.2(c)，C13 对照基准）：`.ocmar/ .omni-orch/ .pytest_cache/ .venv/ logs/ scripts/__pycache__/ src/oc_slimapi.egg-info/ src/oc_slimapi/**/__pycache__/ state/ tests/__pycache__/`（**既有** `src/oc_slimapi.egg-info/` 记录在案；C13 口径 = 零新增）。

## 环境与运行模式（U0.3 / U0.4 / U0.5）

- **三检（经统一执行器 `run_isolated.py` + bootstrap env，全部通过）**：
  1. `.venv/bin/python --version` → `Python 3.14.4`（rc=0，run b-1）
  2. `.venv/bin/python -m pip check` → `No broken requirements found.`（rc=0，run b-2）
  3. `.venv/bin/python -c "import pytest, oc_slimapi"` → OK（pytest 8.4.2，rc=0，run b-3）
- **运行模式 = 工作区**（workspace）：`manifest/runtime-mode.json`（schema_version=2），U0.4 完成后 `state=final`；`check_root=仓库根`、`python=.venv/bin/python`、`check_argv=["./scripts/check.sh"]`。venv-source 命名空间保持 `unused`（未触发副本模式）。
- **绿色基线（U0.4）**：`./scripts/check.sh` → **rc=0 全绿**：`3316 passed, 18806 warnings in 127.47s` + 路由↔文档一致性「54 条 /slimapi 路由均已在 INTERFACE_MAP.md 表行记录且 method 一致（其中 7 条通过语义校验）」+ compileall 通过（run r-5，duration=131.317s）。输出全文：`logs/check-baseline.txt`。
- **U0.5 耗时登记**：check.sh 总时长 127.47s（pytest 段）/ 131.3s（整脚本）；测试计数 **3316 passed**（收集自 pytest 输出；`rg -c "def test_"` 计 2642 个 test 函数，分布 109 个测试文件——函数级 vs 用例级计数差异源于参数化，V5 定稿时以 pytest 输出为准）。
- 依赖快照：`logs/pip-list.txt`（28 行含表头，直接依赖 + 传递闭包）、`logs/pip-check.txt`（无损坏依赖）。

## 机器可读 inventory（U0.6，唯一数字源）

`01-explore/inventory.json`（生成器 `gen_inventory.py` sha256 已登记 probes-manifest）：

| 指标 | 值 |
|---|---|
| src .py 文件 | **71** |
| src 总行数 | **26,452** |
| 测试文件 | **109**（`def test_` 函数 2642） |
| 路由（AST + prefix 解析） | **54**（与 check.sh「54 条 /slimapi 路由」一致） |
| `OC_SLIMAPI_*` env 读点（src 内） | 74 个唯一名 |
| 错误码（src 内 code= / "code": 构造点） | 34 个唯一码 |
| TODO/FIXME/XXX/HACK | **2**（均 `sse/tokenstream/hub.py`，:663 与 :760，均为「confirm live wire key casing for properties」） |
| tracked 可执行/部署/构建资产 | 10（deploy/×3、scripts/×5 含 1 md、pyproject.toml、check.sh） |
| tracked 文件总数 | 260（+1 untracked 输入 = 261 冻结输入） |

### 与 §1.2 事实基线对照

- **A 组（已验证）全部复核吻合**：71/26452；`tokenstream/hub.py` 2190 行（inventory 详见 file-cards）；`messages.py` 1643；`config.py` 1158；`proxy.py` 51；`token_hub.py` 23；`versioning.py` 44；`SERVER_API_VERSION=4`/`ACCEPTED_CLIENT_VERSIONS=(3,4)`；deploy 残留 v2 env（E6/A13 再锚定）；ocdroid@v3、oc-webui@v4。
- **B 组（预测绘）声明作废**：以 inventory.json 为准（如 B 组称 `global_hub.py` 1090 行、测试 107 文件等均与 inventory 不同，正文中不再引用 B 组数字）。

## U0.11 计数复核命令块

输出存 `logs/u011-counts.txt`（含命令与输出）：71 / 26452 total / 2642 / TODO×2（tokenstream/hub.py:663,760）/ `sqlite3.connect` 仅 `dbaux/lifecycle.py:507`（`sqlite3.execute` 零命中——execute 均经 cursor 对象）/ `@router.*` 装饰器分布 19 文件合计 54 / `git ls-files` = 260。

## U0.7 上游与部署自检

- 上游快照：`/home/mar/personal_projects/ocdroid/opencode-src/current` 可读（AGENTS.md/README 等在）；附录 B 锚点存在性：`message-v2.ts`(737 行)、`httpapi/handlers/session.ts`(442)、`packages/server/src/handlers/session.ts`(385)、`httpapi/handlers/event.ts`(99)、`packages/server/src/handlers/event.ts`(52)、`httpapi/groups/event.ts`(29)、`core/src/session.ts`(486)、`schema/src/v1/session.ts`(676) 全部 **OK**；`packages/protocol/src/v1/` 不存在（附录 B 该行漂移——E8 时以 rg 在 `packages/protocol/src/` 实际定位 event 类型定义）。
- deploy 资产存在：`oc-slimapi.service`(73 行)、`stunnel.conf`(29)、`actions.manifest.example.toml`(55)。全文精读归 E6。

## blockers（U0.12）

无。`01-explore/exploration-log.md` blockers 节已初始化（空表）。

## Phase 0 完成判据自查

`00-baseline.md` + `manifest/*`（baseline-head.txt、input-paths.txt、deleted-paths.txt、file-hash-manifest.txt、ignored-baseline.txt、runtime-mode.json(final)、namespaces.json、bootstrap-env.json、phase-verify/）+ `01-explore/inventory.json` 齐备且相互引用一致；README 进度表已初始化并更新；无 BLOCKED-STUB 需求。
