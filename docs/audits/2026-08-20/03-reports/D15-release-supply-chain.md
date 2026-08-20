# D15 — 发布与供应链审计（A15）

> Phase 2 专项 A15。产出：本报告 + F-018 更新（主辖）+ F-361..F-370（新建）。
> 快照：`0b836e78c5de62d0c73b8593bf62c6650043dedf`（0b836e7，= tag v4.4.0）。日期：2026-08-20。
> 输入：git ls-files（260 tracked files）、scripts/{check.sh, release.sh, check_routes_doc.py, eqp_matrix.py, measure_token_overhead.py(+.md)}、deploy/×3、pyproject.toml、docs/release.md、logs/{pip-list.txt, pip-check.txt, check-baseline.txt}（U0.3 存档）、01-explore/parts/e1-18-assets.md。

## 0. 覆盖限制（元数据）

1. **离线审计，未联网核查 CVE/advisory**：无法访问 OSV/PyPI advisories/GHSA。本报告不对任何依赖版本做「有/无已知漏洞」的安全结论；依赖面仅裁决**本地可证事项**（窗口结构、解析结果合规、固定机制有无）。见 §6。
2. **仓库无 CI 系统**（`git ls-files` 无 .github/gitlab-ci/woodpecker/drone 等配置，grep rc=1）：「CI 风险」均指 *若将来接入 CI / 把脚本接入门禁* 的行为分析，现实门禁只有本地 `check.sh`（AGENTS.md:61、:73）。
3. 传递依赖仅经 `logs/pip-list.txt`（U0.3 存档快照）核对，无哈希清单（本仓无任何锁定机制，§5.4）。
4. ocdroid / opencode 上游仓不在本次辖域；`deploy/` 模板中本机绝对路径指向的仓外脚本（actions.manifest.example.toml:25,31,39）不可核。
5. 审计工作区 `docs/audits/` 为 untracked（`git status --short` 实证），不影响发版干净检查语义（release.sh:29-33 只查已跟踪）。

---

## 1. 资产枚举（git ls-files 260 文件分类）

### 1.1 分类计数

| 类别 | 文件数 | 说明 |
|---|---|---|
| src/oc_slimapi | 71 | 产品代码 |
| tests/ | 114 | 测试 |
| docs/ | 60 | 契约/设计/运维/审计文档 |
| **scripts/** | **6** | 质量门禁 + 发版 + 实证/测量 harness（§1.2 逐项） |
| **deploy/** | **3** | 部署模板（§1.2 逐项） |
| **构建/包配置** | **1** | pyproject.toml |
| 根部 | 5 | AGENTS.md、CHANGELOG.md、README.md、LICENSE、.gitignore |

### 1.2 发布/供应链资产逐项（一行职责 + e1-18 卡核对结论）

| 资产 | 模式 | 职责（一行） | e1-18 卡核对 |
|---|---|---|---|
| scripts/check.sh（37 行） | 755 | 改动校验质量门禁：venv 门 + pytest + 路由↔文档对账 + compileall | ✅ 行为/行号全符（:9 set -e；:13-17 venv 门；:21-22 pytest；:24-25 对账；:27-28 compileall；:30-35 MODE 后置校验）；疑问点 1/3 确认（→F-364） |
| scripts/release.sh（96 行） | 755 | 发版唯一入口：main+干净树前置 → check.sh → semver 推算 → CHANGELOG 门禁 → pyproject 写回 → release commit → annotated tag → 打印 push（不自动 push） | ✅ 全符；**卡疑问点 5 需修正**：tag message 首行**不是**节标题——git 对 `-F` 注释做 comment 行清理，`## [4.4.0]` 标题行被剥掉（实证 `git cat-file tag v4.4.0` 首行为 `> 动因：…`，CHANGELOG.md:30），良性 |
| scripts/check_routes_doc.py（323 行） | 644（有 shebang :1） | 路由↔INTERFACE_MAP 防漂移 enforced gate：AST 收集 routes/*.py 静态声明 → 存在性 + method + 7 路径语义关键词校验 | ✅ 逻辑/行号全符（卡记 322 行，实际 323，差 1 行尾，非实质）；疑问点 1-5 全部复核成立（→F-366；L311 v2 提示属 F-020/A14 辖） |
| scripts/eqp_matrix.py（563 行） | 644（有 shebang :1） | B0-6(b) EQP 48 组合矩阵 + 真库 P99 采集 harness（draft 断言模式 / real-db 采集模式） | ✅ 全符；静默路径实证见 §4（→F-367） |
| scripts/measure_token_overhead.py（416 行） | 755 | token-stream SSE overhead 自包含测量 harness（12 trace，gzip PRIMARY），main() 恒 return 0 | ✅ 全符；:11-12 陈旧 hub.py:110 引用确认（归 F-020/A14）；无门禁联动 →F-370 |
| scripts/measure_token_overhead.md（85 行） | 644 | 上项脚本的结果报告快照（判定 NO：gzip median 1.47x） | ✅（快照无防漂移机制 →F-370 一并覆盖） |
| deploy/oc-slimapi.service（73 行） | 644 | systemd **user** service 模板（无 sandbox 是刻意：user manager 无权；Restart/TimeoutStopSec=15/StateDirectory/MemoryMax=384M） | ✅；L33 `ACCEPTED_CLIENT_VERSIONS=2,2` 启动即崩 = 已立案 **F-004（P1，A13/A4 辖）**，L32 → F-005；MemoryMax 算术过时面归 A13（F-010 族） |
| deploy/stunnel.conf（29 行） | 644 | stunnel 服务端 mTLS 双入口模板（14096 直连 / 14097 sidecar；verifyChain + 12h idle + NODELAY/KEEPALIVE） | ✅；TLS 版本/套件未钉 + 未钉对端身份 = A8 安全辖域（D08 已审 stunnel 边界，不重复立案） |
| deploy/actions.manifest.example.toml（55 行） | 644 | /slimapi/actions manifest 参考（4 动作；启用路径 + 权限校验语义说明） | ✅；:3 引用已退役 v2-contract §2（F-019/A14 族辖） |
| pyproject.toml（32 行） | 644 | 包定义 + 构建系统 + pytest 配置（详见 §5） | ✅ |

### 1.3 可执行位一致性备注（不足立案，记录在案）

- 755 + shebang：check.sh、release.sh、measure_token_overhead.py；**644 + shebang**：check_routes_doc.py、eqp_matrix.py（均以 `python script` 方式调用——check.sh:25——故无功能影响；风格不一致而已）。
- deploy/ 三文件均 644 无 shebang（配置文件，正确）。

---

## 2. release.sh 审读（96 行）

### 2.1 失败关闭（fail-closed）实证结论

**结论：CHANGELOG 门禁是失败关闭的，且先于一切仓库写操作。**

序列（行号）：参数校验 :22-23 → main 分支 :26-27 → 已跟踪干净树 :29-33 → check.sh 门禁 :35-37 → 版本解析 :42-43 → **CHANGELOG 门禁 :55-59（`grep -qE "^## \[VERSION\]"` 不命中 → exit 1）** → pyproject 写回 :61-67 + 复核 :68 → commit :71-73 → tag note 提取 :76-84 → `git tag -a` :85 → 只打印 push :88-96。`set -euo pipefail`（:18）兜底任一步非零。

即：**CHANGELOG 校验缺失（无 `## [X.Y.Z]` 节）时发版在写 pyproject 之前中止，仓库零改动。** 其余失败点（参数/分支/脏树/门禁/解析失败/写回复核失败/note 提取为空）均显式 exit 1。

**例外——中途失败窗口（→F-361）**：commit（:73）先于 note 提取校验（:84）与打 tag（:85）。若 CHANGELOG 目标节存在但**为空**（`awk` 提取后 `-s` 判空失败 :84），或 tag `vX.Y.Z` **已存在**（`git tag -a` 自身失败 :85，脚本无预检），仓库将残留 release commit 而无 tag，无自动回滚（人工 `git reset --hard HEAD~1` 恢复）。触发概率低（空节/重复同版本都是异常人工操作），但这是唯一一个「已突变仓库后才失败」的路径。

### 2.2 版本一致性检查覆盖面

| 检查 | 有无 | 证据 |
|---|---|---|
| CHANGELOG 含**新**版本节 | ✅ | release.sh:55-59 |
| pyproject 写回成功 | ✅ | release.sh:68（grep 复核） |
| pyproject 当前版本 ↔ 最新 tag 一致（防人工回拨后 bump 撞已有 tag） | ❌ | 无对应代码；撞 tag 只在 :85 git 层面失败（commit 已成）→F-362 |
| tag 已存在预检 | ❌ | 同上 →F-361/F-362 |
| `[Unreleased]` 节已清空/折叠 | ❌ | 仅注释建议（:57）；当前 CHANGELOG.md:1067 恰有空 `[Unreleased]` 尾节，形态合规但无机制保证 →F-362 |
| CHANGELOG 日期格式 | ❌ | 无校验（标题 `- YYYY-MM-DD` 仅文档约定） |
| 代码内版本常量 ↔ pyproject | 不适用 | 双轨设计：代码无包版本常量，`__version__` 读 dist-info（docs/release.md:141） |

**快照三角实证**：pyproject.toml:7 `version = "4.4.0"` = CHANGELOG.md:29 `## [4.4.0] - 2026-08-20` = tag `v4.4.0` → `0b836e78…`（= HEAD，`git cat-file tag v4.4.0` object 行）。当前态零漂移。

### 2.3 与 docs/release.md 规范逐条符合性（§3.2 九步）

| 规范条（release.md §3.2） | 脚本 | 判定 |
|---|---|---|
| 1. 校验分支 == main | :26-27（detached HEAD 时 `git branch --show-current` 为空 → 同样 exit 1，失败关闭） | ✅ |
| 2. 已跟踪干净（允许 untracked） | :29-33（`git diff --quiet HEAD` + `--cached`；untracked 不拦） | ✅ 与 §3.2.2「允许 untracked」明文一致（untracked 也不可能进 commit：add 限定两文件 :71） |
| 3. 跑 check.sh | :35-37 | ✅ |
| 4. 读 version 推算 | :42-50 | ✅（纯 semver；带后缀版本会在 `$((PATCH+1))` 算术错——失败关闭，当前无影响） |
| 5. CHANGELOG 目标节必须存在，缺失失败 | :55-59 | ✅ |
| 6. 写回 pyproject | :61-68 | ✅ |
| 7. `git add pyproject CHANGELOG`（「及必要的契约文件，若有」）并 commit | :71-73（**只收两文件**，契约等由调用方事先 commit，:72 注释明示） | ⚠️ 与规范括注措辞有偏差：规范似允许脚本多收，脚本刻意窄收——**窄收是更安全方向**（防误收未审改动），偏差良性 |
| 8. annotated tag（注释 = CHANGELOG 节） | :76-85 | ✅（`## [X.Y.Z]` 标题行被 git comment 清理剥除，实证 v4.4.0 tag 首行为 `> 动因：…`；v4.3.0 tag 则是另一手写风格——历史注记风格不统一，非问题） |
| 9. 打印 push 命令，不自动 push | :88-96 | ✅ |

§2 人工清单（行为变更入 CHANGELOG / 破坏性走契约 / directory allowlist 三态确认）与 §2.1 P3 major 前置均无机械门禁——**规范本身定位为人工步骤**，脚本不越权，符合性无缺口。

### 2.4 危险操作清点（防误触）

- **不自动 push**：:88-96 仅打印；规范 §0/§6「禁止随手 tag」「禁止复用已 push tag」为流程纪律。
- **commit 范围硬限**：`git add` 仅 pyproject.toml + CHANGELOG.md（:71）。
- **main-only**（:27）+ **干净已跟踪树**（:29-33）双前置。
- **tag**：annotated（`-a`）而非 lightweight（:85）。
- 残余风险面：untracked 文件不拦（规范明示允许，本地 secret 备忘场景）；tag 重复发版靠 git 报错（无友好预检，F-361）；脚本不校验本地 main 是否落后 origin（push 阶段会暴露，人工步骤兜底）。

---

## 3. check.sh + check_routes_doc.py 审读

### 3.1 check.sh 检查项全集 vs 声称

实际执行（序）：① venv 存在门（:13-17，缺 → exit 1 带安装指引）；② `pytest tests/ -q`（:21-22）；③ `python scripts/check_routes_doc.py`（:24-25）；④ `python -m compileall -q src`（:27-28）；⑤ MODE 参数校验（:30-35，**后置于全部检查**→F-364）；⑥ 通过横幅（:37）。基线实证（logs/check-baseline.txt）：3316 passed / 127s；「54 条 /slimapi 路由…（其中 7 条通过语义校验）」；compileall 过。

声称对照：

| 声称源 | 措辞 | 判定 |
|---|---|---|
| AGENTS.md:61 | 「pytest + 路由↔文档一致性…+ docs/release.md §质量门禁」 | ✅ 未夸大（漏提 compileall，属低估） |
| AGENTS.md:73 | 「（当前 = `pytest tests/`）」 | ⚠️ **滞后**：实际三项（含对账 + compileall）→F-363 |
| docs/release.md:16（§0 表） | 质量门禁 = `./scripts/check.sh`（`pytest tests/`） | ⚠️ 同上低估 →F-363 |
| docs/release.md:152-161（§4） | 「最小集合（当前）：pytest…可选扩展（**后续**）：compileall、ruff/mypy…」 | ⚠️ **滞后**：compileall 已是现状 enforced 项而非「后续可选」→F-363 |

### 3.2 check_routes_doc.py：查什么 / 不查什么（**A14 消费清单**）

**查（4 项）**：
1. 存在性（代码→文档**单向**）：`routes/*.py` 静态声明（AST :162-180，`@router.<attr>` :57/:102-138，`api_route(methods=)` :141-150）的每条路由必须出现在 INTERFACE_MAP **表行**（`|` 开头物理行，:60-62/:188-204；prose/历史段不满足，:15-18 防残留路径字符串）。
2. HTTP method 一致（:265-266）。
3. 语义白名单 **7 条路径**的关键词子串（SEMANTIC_CHECKS :71-87；边界正则 :207-209 防前缀假匹配；多行 join :212-233）。
4. 文档文件缺失 → exit 2（:279-281）；任一失败 → exit 1 带分类明细（:286-312）。

**不查——盲区 8 条**：
1. **反向漂移**：文档表行残留「代码已删的路由」不报错——`validate()` 只遍历代码路由（:261-275）；当前 84 条表行 vs 54 条 method 标题行（grep 实证），30 行差异无机制解释/校验。
2. **语义白名单仅 7/54**：其余 47 条路由的语义（错误码等）完全不在校验内（:25-29 自认「刻意保守」）。
3. **描述列正确性（白名单外）**：除白名单关键词子串外，描述列的参数/默认值/错误码任意漂移不检。
4. **动态注册 / app 级路由不可见**：collector 只扫 `routes/*.py` 的 `@router.<attr>`——实证 `src/oc_slimapi/proxy.py:34-43` 的 `app.websocket` 501 stub 与 `app.api_route` catch-all 即在门外（今日是设计上的边界路由、非 `/slimapi`，脚本 :34-38 已自我声明该局限；**未来任何 app 级/`add_api_route` 的 /slimapi 路由将静默逃逸门禁**）。
5. **collector 耦合脆弱性**：router 变量必须恰名 `router`（:116-119 `func.value.id == "router"`）；`APIRouter(prefix=…)` 必须双引号字面量（:53 `_PREFIX_RE`）——改单引号/f-string/换名 → **整模块静默漏采**（漏检而非误报）；今日 18 个 router 模块全合规（grep 实证，全部 `router = APIRouter(prefix="…")`）。
6. **SyntaxError 静默 continue**（:169-170）：语法损坏文件路由全漏检——check.sh 的 compileall/pytest 提供兜底，但属静默吞错路径。
7. **`"12"` 纯子串匹配**（:80-84）：文档行内任意含 `12` 的串（12288、年份…）即命中，实际有效守卫只有 `EXPAND_CATEGORIES`；类目计数锁定是名义性的。
8. **文档行 finditer 多标题**（:201-203）：同一物理表行若并列多个 `**METHOD \`path\`**` 标题全部计入 `by_path`，理论上可放宽 method_mismatch 判定（当前文档形状未触发，边界情况）。

（另有 L311 修复提示指向已退役 v2-contract §7——文案陈旧，已属 F-020/A14 辖，不重复立案。）

### 3.3 check.sh 自身小缺口

- MODE 校验后置（:30-35）：传错参数仍完整跑完 pytest+对账+compileall 才报 usage——浪费不误放行（→F-364）。
- `--full` 与默认完全等价（:7、:31）：死别名（→F-364 一并）。
- compileall 在 src/ 产生 `__pycache__` 副产物无清理（:27-28）；树内 `scripts/__pycache__`、`src/` 残留实证（被 .gitignore:2 `__pycache__/` 吸收，无入库风险；→F-365）。
- 不运行 measure_token_overhead.py / eqp_matrix.py CLI（数字资产无防漂移，→F-370/F-367）。

---

## 4. eqp_matrix.py（563 行）

- **用途**：B0-6(b) 实证 harness——① draft 模式：/tmp 临时 WAL 库（:194、:231）48 组合（archived 3 × parent 4 × cursor 2 × search 2，:86-98）EXPLAIN QUERY PLAN 结构断言（SCAN + TEMP B-TREE，S-B08 :309-329）+ Python 镜像 oracle `expected_window` 行集精确匹配（:277-302）；② real-db 模式：`mode=ro`（:429）+ `PRAGMA query_only=ON`（:431）只读采样计时（P50/P99）。
- **被测试引用方式（实证）**：`tests/v4_fixture.py:50-55` `importlib.util.spec_from_file_location("eqp_matrix_under_test", …/scripts/eqp_matrix.py)` 按**文件路径**装载（scripts/ 非包）；`tests/test_eqp_matrix.py:15-17` 用其 `build_draft_db/all_combos/parse_eqp/expected_window` 作 **oracle**，被测对象是真实组装器 `oc_slimapi.dbaux.build_sessions_query`（draft 断言 + `rows_to_records` 管道不改行集）；:26-38 对 draft 库 `model` 纯文本做 JSON 归一（B0 冻结不改的绕过）。即测试**只消费其纯函数**，不执行其 CLI。
- **静默 exit 0 裁决（实证修正任务前提）**：
  - 真库**路径不存在** → `sys.exit("ERROR: real DB not found…")`（:427-428）——**响亮的非零退出，非静默**。
  - 真库 **schema 兼容门失败** → 静默：`gate_passes` 仅记录（:440-447），主流程**只在通过时打印**（:544-545，**无 else 分支、无 WARNING**），报告照写（:557-559），`return 0`（:560）。docstring :27 自辩「真库模式为数据采集不断言」——但 gate 失败连告警都没有，采集到不兼容 schema 的库也是绿灯。→**F-367**。
- **CI 风险定性**：该脚本未接入 check.sh/测试 CLI 模式（check.sh 无引用；tests 仅 importlib 复用函数）——当前零 CI 面；风险仅在**将来**有人把 `--real-db` 当门禁接入时才会把「schema gate 失败 = 通过」的语义带进门禁。与 F-278（A9：真库索引漂移无机械门禁）互补：F-278 说「没有门」，F-367 说「这把尺本身有一处失效刻度」。

---

## 5. 依赖与安装面

### 5.1 声明窗口 vs 实际解析（logs/pip-list.txt，U0.3 存档）

| 依赖 | 声明（pyproject.toml） | 实际解析 | 合规 |
|---|---|---|---|
| fastapi | `>=0.115,<1`（:11） | 0.139.2 | ✅ |
| httpx | `>=0.28,<1`（:12） | 0.28.1 | ✅ |
| orjson | `>=3.10,<4`（:13） | 3.11.9 | ✅ |
| uvicorn | `>=0.34,<1`（:14） | 0.51.0 | ✅ |
| pytest | `>=8,<9`（test，:19） | 8.4.2 | ✅ |
| pytest-asyncio | `>=0.25,<1`（test，:20） | 0.26.0 | ✅ |
| respx | `>=0.22,<1`（test，:21） | 0.23.1 | ✅ 窗内，但**全仓零代码引用**（→F-018） |
| oc-slimapi | `version = "4.4.0"`（:7） | 4.4.0（editable，指回本仓） | ✅ |

`pip-check`：`No broken requirements found.`（logs/pip-check.txt:1）——环境自洽，无冲突依赖。

**传递依赖清单引用**（完整 26 行表见 `logs/pip-list.txt`）：annotated-doc 0.0.4、annotated-types 0.7.0、anyio 4.14.2、certifi 2026.6.17、click 8.4.2、h11 0.16.0、httpcore 1.0.9、idna 3.18、iniconfig 2.3.0、packaging 26.2、pip 25.1.1、pluggy 1.6.0、pydantic 2.13.4、pydantic_core 2.46.4、Pygments 2.20.0、starlette 1.3.1、typing_extensions 4.16.0、typing-inspection 0.4.2。运行态 venv = **Python 3.14**（check-baseline 中 `.venv/lib/python3.14/` 路径实证）。

### 5.2 lock / 哈希固定机制：**无**（→F-368，定级 P3）

- `git ls-files` 无任何 lockfile/constraints/requirements（grep `lock|requirements|constraints` 仅命中两个测试文件名）；安装指令一律范围解析（AGENTS.md:88 `pip install -e '.[test]'`、check.sh:15、docs/release.md:136）。
- **漂移已实际发生**：fastapi 声明下界 0.115 → 本机解析 0.139.2（跨 24 个 minor）；不同时间/机器重装得到不同依赖树。
- 定级理由（P3 而非更高）：单机 systemd 部署钉死 venv 路径（deploy/oc-slimapi.service:18 `.venv/bin/python -m oc_slimapi.app`）；唯一消费方 ocdroid 经 mTLS 专线；无 PyPI 发布面（不发 wheel，editable 自装）；风险面 = 换机重建/未来重装时的行为漂移与供应链暴露窗口，非即时缺陷。
- 无 pip-audit/safety 等审计工具配置（离线亦不可运行）。

### 5.3 构建后端与包发现

- `requires = ["setuptools>=75"]`（pyproject.toml:2，**无上界**）+ `setuptools.build_meta`（:3）；`requires-python = ">=3.11"`（:9，**无上界**）——venv 已在 3.14，且门禁输出已见 pytest-asyncio 0.26.0 的 `get_event_loop_policy` **DeprecationWarning（ slated for removal in Python 3.16）**（logs/check-baseline.txt 尾部）：无上界声明 + 无版本上限测试 = 向前兼容裸奔，2 个 minor 内已有可预见的断点（→F-369）。
- 包发现：`[tool.setuptools.packages.find] where = ["src"]`（:27-28）src-layout；src/ 下唯一包树 oc_slimapi（71 文件），默认 include 全量无风险；无 namespace 需求。
- 入口脚本 `oc-slimapi = "oc_slimapi.app:main"`（:24-25）与生产 `python -m oc_slimapi.app`（service:18）双通道并存（一致指向同一入口，无漂移）。

### 5.4 editable 安装路径面（供应链含义）

- `src/oc_slimapi.egg-info/` 存在于工作树（ls 实证）——被 `.gitignore:4 *.egg-info/` 吸收，无入库风险；但它是 **editable 安装的生成元数据**：`PKG-INFO:14` / `requires.txt:9` 携带含 respx 的依赖窗口（死依赖随安装面扩散，F-018 佐证）。
- **陈旧元数据风险已被文档化**：发版后必须手动 `pip install -e '.[test]'` 刷新 dist-info，否则 `health.sidecar.version` 报旧版（docs/release.md:133-141 明示 `__version__` 读 importlib.metadata 而非 pyproject）——属已知人工步骤，无机制保证（可接受，单机运维模式）。
- 生产以 editable 方式直接跑 git 工作树（service:17-18 WorkingDirectory/ExecStart 指向本仓路径）：**部署面 = 仓库 HEAD**，无构建产物隔离——本仓特有部署形态（单机 sidecar），审计记录其含义：git 工作树污染/意外 commit 直接改变生产行为（由 release.sh 干净树门禁 :29-33 部分缓解）。

---

## 6. CVE / advisories（离线裁决）

- **未联网核查 CVE**（覆盖限制 §0.1）：fastapi 0.139.2 / httpx 0.28.1 / orjson 3.11.9 / uvicorn 0.51.0 / pydantic 2.13.4 / starlette 1.3.1 等具体版本**无任何安全结论**（既不断言安全也不断言有洞）。
- 本地可证事项（已裁决）：
  1. 窗口均为「下界钉 + major 上界」结构（`<1`/`<4`/`<9`），**允许大幅 minor 漂移**（fastapi 有 0.115→0.999 的解析空间）——结构上无法把已知坏版本排除在外，也无法锁定已知好版本（F-368 同根）。
  2. 无哈希固定、无依赖审计工具、无 lockfile（§5.2）——供应链完整性完全依赖 PyPI 传输 + 范围解析。
  3. 依赖面收敛：4 直接运行依赖（极小面，正面结论）；传递面 18 项全部为主流维护包（pip-list 快照），无可疑/弃维护命名。
- 结论：依赖安全核查**待联网后补**；本报告仅立复现性/结构发现（F-368/F-369），不给安全定性。

---

## 7. secrets 全仓扫描结果消费（A8 → D15 归档）

引用并归档 A8（D08-security.md §2.8 / §5，T8 扫描）结论：**260 tracked 文件扫描 0 真阳性**——关键词（api[_-]?key/secret/password/token）命中 3210 行/143 路径全为领域术语误报（token stream 术语、`secrets.token_hex` 等）；高熵模式（sk-/AKIA/ghp_/xox/PRIVATE KEY/JWT 形）**0 命中**；字面赋值形 0；stunnel.conf 仅含证书**路径**非凭据。

供应链视角补充核验（本次新增）：发版产物面（tag annotation）无凭据泄漏——`git cat-file tag v4.4.0` 全文为 CHANGELOG 节内容（纯行为描述）；release commit 范围限 pyproject+CHANGELOG（release.sh:71），secrets 意外入库的发版通道不存在。`.gitignore` 明确排除 `.venv/`、`logs/`、`state/`、`*.egg-info/`（:1-4,10-13），本机密钥/运行态路径不入库。

---

## 8. A15 发现清单（主辖）

| 编号 | 状态 | 严重度 | 类别 | 一句话 | 位置 |
|---|---|---|---|---|---|
| F-018（更新） | verified | P3 终判 | quality | respx 测试依赖全仓零代码引用（死依赖，且随 egg-info 元数据/安装面扩散） | pyproject.toml:21 |
| F-361 | verified | P3 | quality(ops) | release.sh 中途失败窗口：commit(:73) 先于 note 校验(:84)/打 tag(:85)；空节或 tag 已存在 → 残留 release commit 无 tag、无回滚 | scripts/release.sh:71-85 |
| F-362 | verified | P3 | gap | release.sh 版本一致性前置缺口：不校验 pyproject↔最新 tag、不预检 tag 存在、不验 [Unreleased] 已折叠/日期格式 | scripts/release.sh:42-59 |
| F-363 | verified | P3 | docs | 质量门禁描述三处滞后低估：release.md §0 表(:16)与 §4(:152-161) 称「pytest 最小集/compileall 后续可选」、AGENTS.md:73 括注「当前=pytest」——实际三项 enforced | docs/release.md:16,152-161；AGENTS.md:73 |
| F-364 | verified | P3 | quality | check.sh MODE 参数校验后置于全套检查之后 + `--full` 死别名（等价默认） | scripts/check.sh:7,30-35 |
| F-365 | verified | P3 | quality | check.sh compileall 产生 src/__pycache__ 无清理（scripts/__pycache__ 树内残留实证；gitignore 兜底无入库风险） | scripts/check.sh:27-28 |
| F-366 | verified | P3 | gap | check_routes_doc 对账盲区 8 项（反向漂移/语义白名单 7/54/描述列/"12" 子串/collector 耦合 router 命名+双引号前缀/app 级与动态注册不可见/SyntaxError 静默/文档行多标题）——A14 消费 | scripts/check_routes_doc.py |
| F-367 | verified | P3 | defect | eqp_matrix 真库 schema 门失败静默 exit 0（:447 计算、:544-545 仅通过分支有输出）；真库**缺失**倒是响亮 exit 1（:427-428） | scripts/eqp_matrix.py:440-447,544-545 |
| F-368 | verified | P3 | risk | 无 lockfile/哈希固定：7 依赖全范围解析（fastapi 0.115→0.139.2 漂移已发生），重装不可复现；单机 editable 部署缓解 | pyproject.toml:10-22 |
| F-369 | verified | P3 | risk | requires-python>=3.11 与 setuptools>=75 均无上界：venv 已 3.14，pytest-asyncio 0.26.0 的 3.16 移除告警已在门禁输出出现——向前断点可预见且无声明护栏 | pyproject.toml:2,9 |
| F-370 | verified | P3 | gap | 测量资产无门禁联动：measure_token_overhead.py main() 恒 return 0、零测试引用、.md 数字快照无防漂移机制 | scripts/measure_token_overhead.py:412；scripts/measure_token_overhead.md |

负向结论（审计过、判定无问题）：release.sh CHANGELOG 门禁失败关闭且先于一切写操作（§2.1）；发版危险操作五重防护到位（§2.4）；release.sh 与 release.md §3.2 九步逐条符合（§2.3，1 处良性窄收偏差）；依赖 7/7 窗内 + pip-check 无冲突（§5.1）；部署模板 P0/P1 项均已由 A13/A4 立案（F-004/F-005），不重复；tag/commit 产物面无 secrets（§7）。

## 9. 汇总

- 资产枚举：**260** tracked files；发布/供应链资产 10 项（scripts 6 + deploy 3 + pyproject 1）逐项核对 e1-18 卡，结论基本全符，2 处修正（行数差 1；tag 首行实为 git 剥除 `##` 标题后的正文）。
- release 失败关闭：**是**（CHANGELOG 缺失在写 pyproject 前 exit 1、仓库零改动）；唯一例外是 commit 之后 tag 阶段的低概率中途失败窗口（F-361）。
- check_routes_doc：enforced 且当前 54 路由全绿，但**盲区 8 条**（供 A14 消费）。
- 依赖面：7/7 窗内合规、无 broken；**无 lockfile 定级 P3**（单机 editable 部署缓解）；无上界 Python/setuptools 前向断点 P3；CVE 未核查（离线，覆盖限制）。
- 产出：D15（本文件）+ F-018 更新 + F-361..F-370 新建。注：按写入授权限制，02-findings/INDEX.md 未同步 A15 段——请协调方或 Phase 3 合并时补录。
