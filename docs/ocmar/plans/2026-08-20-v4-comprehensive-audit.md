# oc-slimapi v4 全面审计方案（无人值守执行版）— rev10

> **For agentic workers:** 本方案供一个**全程无人值守、无工作量/时长限制**的审计 agent 独立执行。按 Phase 顺序推进，每个单元完成后更新进度清单。本方案是**只读审计**：除 §0.1 允许的产物目录外**禁止一切写入**。方案中不存在需要向人类提问的节点；遇到未预见情况按 §10 的阻塞规则处理，**永远不要停下来提问**。

**目标（一句话）**：对 oc-slimapi（当前 4.4.0，wire (3,4) 双版本窗口）做一次覆盖「契约完备性 / v3 可淘汰性 / legacy 透传遗留 / v4 全面取代能力 / 代码质量·可维护性·安全性·模块化·冗余·上帝文件·状态机 / 测试与可观测性 / 发布与供应链」的全量审计，产出带证据、经复核、可执行的发现清单与整改 backlog。

**方法（2-3 句）**：四阶段流水线——Phase 0 建立绿色基线、快照 manifest 与机器可读 inventory；Phase 1 按「文件级精读 + 路由/配置/状态机/测试四张普查表」做全量探索；Phase 2 按 15 个专项（A1–A15）深度分析，每项产出领域报告与结构化发现；Phase 3 对全部发现做证据复核、自我证伪与快照一致性验证；Phase 4 汇总为最终报告 + 三个专项交付物。

**技术栈**：Python 3.11+ / FastAPI / httpx / orjson / uvicorn / sqlite3(只读) / pytest。上游对照源码：opencode v1.18.18 快照。

## 修订记录（rev-1 门控意见落实对照）

本 rev2 针对 rev-sgpt 门控 FAIL（7.4/10）的 11 条意见逐条落实：

| # | 门控意见（摘要） | 落实位置 |
|---|---|---|
| 1 | BLOCKED 状态与 §8.1/§10.2 冲突；fallback 目录与终止判据冲突 | §2（AUDIT_ROOT）、§8.1、§10.2 统一为 `DONE/N/A/BLOCKED+降级记录` 三态；BLOCKED 对结论的机械影响 §10.3 |
| 2 | 事实基线多处错误（行数/符号/消费方/proxy 现状） | §1.2 拆为「已验证/须重生成」两类；U0.6 机器可读 inventory 成为唯一数字源；§1.2 末尾禁用规则 |
| 3 | 上帝文件白名单过时 | A11 改为数据驱动阈值 + 动态 Top-N，禁用固定清单 |
| 4 | 威胁模型错误固定 loopback/mTLS；漏 deploy/oc-slimapi.service | §1.1 部署姿态三分；A8 三层入口威胁模型；deploy 文件入必读（E6/U0.7）与对账（A13/A15）；ACL 不可实机验证 → 「部署边界未验证」标注规则 |
| 5 | 遗漏非 src/ 可执行与供应链资产 | E1 范围扩到全部 tracked 可执行/部署文件；新增 A15（发布与供应链）专项 + D15 报告 |
| 6 | 权威层级混用规范与事实；测试可错误销案契约违反 | §0.2 拆双轨：规范裁决序 vs 行为事实序；V2 重写（测试只能反驳行为误判，不能豁免契约违反） |
| 7 | §8.3 「建议重启裁决」越权 | §8.3 刻度收敛为「维持/可启动机械前置准备」两值；假设性拆除明确标注为成本模型 |
| 8 | 写入白名单无防覆盖/symlink 逃逸/重跑策略 | §0.1（symlink 验证、no-clobber、attempt 语义）；U0.9/U0.10 |
| 9 | 无长时审计快照一致性保障 | U0.10 tracked-file hash manifest；§7 V0 每阶段边界重验；跨快照证据禁令（§0.4） |
| 10 | 严重度自动升级规则冲突 | §3.3 统一严重度决策表；check.sh 失败→blocker 根因分类；缺测试/无上界不再自动定级 |
| 11 | 有限全集仍抽样；判据不可机械自查；占位命令 | A4/A10/A12 改全量对账；A9 值域 bounded/unbounded/measured/unknown+理由；A12 CSV 冻结 schema；§8.4 评分锚点；附录 A 删除占位命令 |

### rev3（第二轮门控 8.3/10 → 修复清单）

| # | 第二轮意见（摘要） | 落实位置 |
|---|---|---|
| N1 | 安全预检晚于首次写入；恢复态无法更新自建文件 | 新增 U0.0（symlink 校验+AUDIT_ROOT 选定+排他建根目录，先于一切文件写入）；§0.1 允许「本 attempt 自建文件」tmp+原子替换更新 |
| N2 | check.sh/pytest/pip 在白名单外写 `__pycache__`/`.pytest_cache`/egg-info | §0.1 副产物隔离规则（PYTHONPYCACHEPREFIX + PYTEST_ADDOPTS）；U0.2 记录 ignored 基线；C13 增加 ignored 副产物 diff |
| N3 | BLOCKED 与发现二态终态不兼容 | §3.2/§8.1/§10.2/§10.3/C4 新增第三终态 `unverified_due_to_blocker` |
| N4 | 脏工作区基线不可重放；`git show HEAD` 会切换内容 | U0.2 基线冻结扩到 tracked+相关 untracked 非忽略文件（NUL-safe）；脏基线复制只读快照副本，后续读取优先走快照；§0.4/§7 V1 禁止用 HEAD 版本替代 WIP 基线 |
| N5 | A12 CSV 唯一性校验验证不了声明的主键 | A12 冻结完整 header/字段顺序/RFC 4180 转义；改用 python csv 三元组校验（附录 A 同步）；C10 更新 |
| N6 | A1 行模型与 C5「行数=路由数」矛盾 | A1/C5 改为「期望键集合 ⊕ 实际键集合完全相等」判据 |
| N7 | P0 缺实质影响门槛 | §3.3 defect/contract-violation 的 P0 加必要条件（重大互操作中断/数据完整性/安全边界/广泛不可恢复）；其余消费方可观察偏差默认 P1；第三步改为「带记录的重定级」（可升可降，须写理由） |
| N8 | `hub_types.py`（实际 419 行）残留在 >500 行示例清单 | E1 删除固定示例清单，>500 行集合完全由 inventory 动态生成 |

### rev4（第三轮门控 8.6/10 → 修复清单）

| # | 第三轮意见（摘要） | 落实位置 |
|---|---|---|
| M1 | manifest 会把 AUDIT_ROOT 自身产物纳入输入集 | U0.2(b) 改两步法：先冻结 NUL 分隔输入路径清单（排除 AUDIT_ROOT/历史 attempt/fallback/`/tmp`），再仅对清单 hash；产物永不入输入集 |
| M2 | editable install egg-info 违反只读铁律 | §0.1/U0.3/H4：禁止在源工作区 editable 安装；重建 venv 一律在 `/tmp/opencode/venv-source/` 源副本中进行；C13 改为 egg-info **零新增**（取消豁免） |
| M3 | 期望键集合无确定性生成算法 | E2 新增结构化列（supported_versions/route_error_codes/feature_gate/has_boundary_case）+ 冻结生成函数 → versioned `expected-keys.csv`，A1/A12 共用 |
| M4 | baseline-snapshot 无 attempt 命名空间，跨轮自覆盖 | U0.2(b)：`/tmp/opencode/baseline-snapshot/<BASELINE_HEAD>/<attempt-id>/`，同 U0.0 symlink 校验 + 排他创建 + 复制后全量复验，既有命名空间禁覆盖 |
| M5 | §10.1(4) 兜底与 git-show 禁令冲突 | §10.1(4)：仅干净 tracked 文件可用 git show；脏文件只读已验证快照副本；副本不可用 → BLOCKED + `unverified_due_to_blocker` |
| M6 | U0.0 允许在验证前写 /tmp/opencode | U0.0(a)：三路径验证完成前只允许进程内存暂存；恢复分支结束条件改为「既有 AUDIT_ROOT 验证成功」 |
| M7 | V0 校验动作未冻结（抽验 vs 全量二义） | V0：每边界对冻结输入清单**全量**校验，唯一入口 `sha256sum -c`，非零退出/缺文件/新文件/hash 不符一律触发 §0.4 漂移流程 |

### rev5（第四轮门控 8.7/10 → 修复清单）

| # | 第四轮意见（摘要） | 落实位置 |
|---|---|---|
| Q1 | 外部 venv 分支无法执行（check.sh 固定仓库根 `.venv`；复制范围开放结尾） | §0.1/U0.3/U0.4：venv-source 命名空间 `<BASELINE_HEAD>/<attempt-id>/` + **完整仓库树副本** + 在副本内运行**该副本的** check.sh；审计解释器规则冻结；附录 A 双态命令 |
| Q2 | `supported_versions` 只从实现推导会漏「契约要求但实现缺失」面 | E2 拆 `actual_/contract_` 双列；生成函数取**并集**（禁以实现缩小规范期望集）；两侧差集自动生成 contract-violation draft |
| Q3 | A12「以 inventory 全局错误码为准逐码一行」与 E2 route-local 冲突 | A1/A12 期望键**唯一来源 = expected-keys.csv**；inventory 仅校验码合法性不增行；删除「E2+inventory 展开」字样 |
| Q4 | 冻结快照不能表达 tracked 已删除文件 | freeze_baseline.py 产出 `deleted-paths.txt` 墓碑集；hash 清单只覆盖存在的文件；V0 三查含「墓碑仍不存在」 |
| Q5 | manifest/V0 附录命令不可重放（input-paths.z 无生成命令） | 冻结唯一入口两脚本 `freeze_baseline.py` / `verify_baseline.py`（附录 A 调用式；内部逻辑正文冻结）；禁止旁路手工拼管道 |

### rev6（第五轮门控 8.7/10 → 修复清单）

| # | 第五轮意见（摘要） | 落实位置 |
|---|---|---|
| R5-N1 | 契约独有路由是完备性盲区（E2 行全集仅取实现侧） | E2 行全集改为联合主键「inventory routes ∪ v3 契约路由表 ∪ v4 契约路由表」；新增 `actual_defined_at` / `contract_refs` / `presence` 列；contract-only 行强制生成缺失实现 draft 并照常进入期望键；完成判据双计数 |
| R5-N2 | V4/§10.2 硬编码工作区 check.sh，外部 venv 模式无法收尾 | U0.3 产出 `manifest/runtime-mode.json`（`CHECK_ROOT`/`PYTHON`/`CHECK_COMMAND` 三键冻结）；U0.4、一切定点 pytest、V4、C12、§10.2 只读该文件执行，禁止后续环节重新选路 |
| R5-N3 | freeze/verify 未冻结同一路径收集函数，审计自身产物会造成假漂移 | 两脚本共享同一份归档实现 `collect_present_input_paths()`（tracked ∪ untracked 非忽略 ∩ 磁盘存在 − 冻结排除前缀）；V0 第三查 = 该函数重采当前集做双向比较；消失路径仅由墓碑集核对 |
| R5-N4 | 同快照恢复与排他命名空间自冲突（中断后同命名空间已存在被误判 blocker） | §0.1 新增「命名空间恢复规则」机械分支：owner 元数据 + manifest hash + 完成标记 + 逐文件 hash 全一致 → 只读复用；否则 `recovery-<n>` 递增子命名空间 + `CURRENT` 指针原子更新；始终禁止覆盖 |
| R5-N5 | 基础验证脚本固定全局路径，跨 attempt 污染且不可追溯 | 全部探针迁入 `/tmp/opencode/probes/<BASELINE_HEAD>/<attempt-id>/` 排他命名空间；脚本副本 + SHA-256 归档 `AUDIT_ROOT/logs/probes/`；每次调用前校验 hash |
| R5-N6 | expected-keys 生成输入允许两种未冻结格式 | `route-census.csv` 为强制机器真值源：冻结 header 列序 / RFC 4180 / 集合字段 `|` 编码按字典序 / 主键排序 / 空值 `NONE`；route-census.md 仅由 CSV 渲染；生成器只接受 CSV |
| R5-N7 | `.venv --version` 成功不代表依赖可用 | 三检判据：解释器启动 + `python -m pip check` + `python -c "import pytest, oc_slimapi"` 全过 → 工作区模式；任一环境性失败 → 自动构建 venv-source 并以副本重跑一次基线，仍失败才记 blocker |

### rev7（第六轮门控 8.6/10 → 修复清单）

| # | 第六轮意见（摘要） | 落实位置 |
|---|---|---|
| R6-N1 | 恢复规则未贯穿实际路径解析（命令仍指向固定 `<HEAD>/<attempt-id>/`；U0.2 残留「冲突即 blocker」） | §0.1 冻结 `resolve_active_namespace(kind)` 前置解析 + 结果持久化 `manifest/namespaces.json`；一切命令只引用解析返回路径（附录 A 以 `<PROBES_NS>` 占位）；U0.2 冲突句删除改走恢复分支 |
| R6-N2 | COMPLETE/manifest 生命周期未定义（probes 分期创建无法用全局 COMPLETE） | §0.1 分 kind 冻结状态机：baseline-snapshot/venv-source 一次性产物（COMPLETE 哨兵 + 全量 hash）；probes 追加式（无全局 COMPLETE，逐脚本提交 `probes-manifest.json`，恢复校验仅查已登记条目） |
| R6-N3 | 期望集漏继承错误面与规范侧 feature/boundary | E2 错误码拆 local/inherited 两层（继承层含机械适用规则）；feature_gate/has_boundary 拆 actual/contract 双列；生成器全部双侧并集，差集自动 draft |
| R6-N4 | census schema 不能机械生成（复合列/重复语义） | E2 表改一概念一列，**显式冻结 header 列表**；新增 `validate_route_census.py`；重复注册 = draft 发现不阻塞，双计数按唯一 (method,path) 集合 |
| R6-N5 | 三检把源码导入缺陷误判为环境 blocker | §0.1 冻结失败分类算法：fresh 副本 pip check 干净而同源码导入仍失败 = 产品缺陷（红基线+发现），不得记 blocker |
| R6-N6 | CHECK_COMMAND 未结构化（quoting/执行方式歧义） | runtime-mode.json schema 冻结：schema_version/mode/check_root/python/check_argv 数组/env 对象；执行方式冻结 `subprocess.run(argv, cwd, env, shell=False)` |
| R6-N7 | 白名单保留仓库 `.venv` 写通道与新规则冲突 | 写入白名单缩为两处；`.venv` 只读（三检通过方可使用），一切创建/修复/安装限于 venv-source 活动命名空间 |
| R6-N8 | 附录 A 写命令违反 no-clobber/recovery（`>` 截断、`mkdir -p` 绕过排他、`cp -n||true` 吞错） | 三处替换为排他创建/原子替换/hash 比对复用语义；归档失败必须 blocker，禁止 `\|\| true` |
| R6-N9 | 交付物目录树缺 rev6 新增机器产物 | §2 树补 manifest 七件+phase-verify.log、route-census.csv+expected-keys.csv、logs/probes/+superseded/+check 两份；§10.2 新增强制机器产物验收项 |

### rev8（第七轮门控 8.4/10 → 修复清单）

| # | 第七轮意见（摘要） | 落实位置 |
|---|---|---|
| R7-N1 | pytest/pip/TMPDIR/pycache 写域逃出白名单 | 新增第四类 `runtime-cache` 活动命名空间；runtime-mode `env` 冻结键集（TMPDIR/PIP_CACHE_DIR/attempt 级 PYTHONPYCACHEPREFIX/每次运行唯一 --basetemp）；启用前逐级 symlink 校验 |
| R7-N2 | 条件性 namespace 前置无条件创建 → 恢复必造 recovery | 惰性启用：probes/runtime-cache 前置；baseline-snapshot 仅脏基线后、venv-source 仅副本模式后启用；未启用 kind 在 namespaces.json 记 `state:"unused"` 不建实体目录 |
| R7-N3 | 一次性 namespace 缺可机械恢复的完整性记录 | 冻结 `namespace-manifest.json` schema（相对路径→{type,sha256} 全量树 + excluded 节，venv 不稳定文件四类 glob 显式排除）；COMPLETE 仅在 manifest 全量验证后原子提交；恢复复用 venv-source 前重跑三检等效校验 |
| R7-N4 | 基础脚本「四个/五个」计数矛盾 + 未登记既有脚本处理未定义 | 冻结唯一五项清单（含 validate_route_census.py），生命周期/归档/目录树/§10.2 全部引用；目标脚本名存在但未登记 → 判不完整切 recovery，禁止忽略后续建 |
| R7-N5 | BLOCKED 允许态与强制机器产物验收死锁 | 机器产物终态二选一：`VALID` 或机器可读 `BLOCKED-STUB`（blocker ID/缺失输入/已完成范围/受影响裁决）；§8.1+§10.2 同规则，STUB 传播 coverage-degraded |
| R7-N6 | `MISSING` 违反 census 冻结枚举 | actual 侧枚举列（supported_versions/feature_gate/has_boundary）逐列声明允许保留字 `MISSING` 且仅 `presence=contract_only` 行合法（validator 强制）；生成器入口将 MISSING 规范化为空集合再并集 |
| R7-N7 | inherited-error 概括规则误含豁免端点、漏 method 边界族 | 改为显式**继承错误适用表**（错误族 × path/method/wire 版本/feature 态布尔矩阵，初始五族含 selector 豁免清单与 §16 `method_not_applicable`），E2 实读 selector.py 等补全并随 census 归档；期望键只消费该表 |
| R7-N8 | runtime-mode「只读冻结」与 U0.4 自动转换冲突 | 状态机拆 `provisional`（U0.3 写）/`final`（U0.4 最多一次转换后原子写入）；final 后禁变更；恢复只复用 final，provisional 从 U0.4 重走 |
| R7-N9 | inventory/自由探索脚本绕过 probes 登记机制 | U0.6 与 §9 全部改 `<PROBES_NS>/`（gen_inventory.py 等），逐脚本登记 probes-manifest.json，产物记录脚本 hash |
| R7-N10 | secrets 扫描 `$(git ls-files)` 非 NUL-safe | 改 `git grep -n -i -E ... -- .`（tracked 全集原生匹配）或 python `git ls-files -z` 逐文件扫描；归档扫描路径数与失败数 |
| R7-N11 | 目录树/终止验收仍缺 validation.txt 与五脚本闭合 | §2 补 `test-gap-matrix.validation.txt`；§10.2 按 VALID/BLOCKED-STUB 验收全部强制产物（含五个基础脚本归档、runtime-mode final 态） |

### rev9（第八轮门控 8.7/10 → 修复清单）

| # | 第八轮意见（摘要） | 落实位置 |
|---|---|---|
| R8-N1 | 首次三检/pip 先于 runtime-mode 生成 → 隔离循环污染仓库 | 新增第六个基础脚本 `<PROBES_NS>/run_isolated.py`（统一执行器，U0.2 最先创建）+ `manifest/bootstrap-env.json`（冻结键集）；三检/venv 构建一律经执行器以 bootstrap env 执行，禁止裸 shell |
| R8-N2 | 静态 `env` 与「每次唯一 basetemp」互斥 | `env` 改 `env_template`（含 `<RUN_ID>` 占位）；`<RUNTIME_NS>/run-sequence.json` 原子排他递增分配 RUN_ID（锁文件串行化）；pycache/TMPDIR/basetemp 全部收进 `runs/<RUN_ID>/`；附录 A 删全局 pycache 旧示例、改执行器调用式 |
| R8-N3 | runtime-cache 不能套 probes 追加式 hash 生命周期 | 独立生命周期：只验 owner.json + run-sequence.json；run 子目录排他创建不可复用；旧 run 只读封存（不执行其中代码/不复用 pycache）；禁以 probes-manifest.json 表示可变运行时文件 |
| R8-N4 | namespace-manifest schema 不可确定性实现 | 判别式 schema：文件 `{type:"file",sha256}` / 目录 `{type:"dir"}`（无目录 hash）；路径 POSIX 相对+排序；排除 glob 逐字冻结（`**/__pycache__`、`**/*.pyc`、`.venv/pyvenv.cfg`、`.venv/bin/`）+ fnmatch 语义；生成/验证入口 = freeze 系脚本内共享函数；清单外新增路径 = 不完整 |
| R8-N5 | BLOCKED-STUB 未贯穿 Phase 0/C5/C10/§10.2 | stub schema 分型冻结（JSON 五键 / CSV 两行 / txt 单行）；Phase 0 完成判据、C5、C10、§10.2 全部改「VALID 正常断言；STUB 验 schema+blocker 关联+传播」 |
| R8-N6 | applicability CSV 承载不了声明维度 | schema 扩为 10 列（method,path,family,side,wire_version,feature_id,feature_state,applicable,error_codes,evidence_ref），主键七列冻结，feature 态展开规则冻结，新增跨表 validator（与 census 继承列聚合一致） |
| R8-N7 | contract-only 行其他 actual 字段缺一致缺失表示 | 错误码 local/inherited 双列同允许受限 MISSING→空集；行为类单值列（directory/upstream/projection/cache 等）冻结为规范侧单值 + `actual_defined_at=MISSING` 表达缺失（不拆双列）；validator 校验 presence 组合合法性 |
| R8-N8 | applicability 产物未入交付物/恢复闭环 | §2 树 + §10.2 二态验收 + 跨表一致性校验（同二态）全部纳入 |
| R8-N9 | namespaces 注释「三类」漂移；runtime schema 需 bump | 树注释改四类；runtime-mode `schema_version` bump = 2；恢复遇 schema 不匹配 → 确定性迁移或 BLOCKED，禁猜缺省 |
| R8-N10 | phase-verify.log 直接 append 与原子更新规则冲突 | 改一条一文件 `phase-verify/<seq>.json`（排他创建）+ 原子重建 `index.json`（旧 index 归档 superseded/） |

---

## 0. 执行者契约（宪法，优先级最高）

### 0.1 只读铁律与写入白名单

- **禁止**修改、创建、删除以下任何内容：`src/`、`tests/`、`scripts/`、`deploy/`、`docs/specs/`、`docs/manual/`、`docs/release.md`、`docs/operations.md`、`docs/develop.md`、`CHANGELOG.md`、`AGENTS.md`、`pyproject.toml`、`*.sh`、git 配置/分支/tag/commit。
- **禁止**运行任何改变仓库状态的 git 命令（`commit`/`add`/`checkout`/`stash`/`tag`/`config`/`rebase`/`restore`/`clean` 等）。只允许只读 git 子命令（`status`/`log`/`diff`/`show`/`blame`/`grep`/`rev-parse`/`ls-files`）。
- **禁止**写入/修改上游 opencode 的 SQLite 业务数据；禁止对任何 `.db`/`.sqlite` 文件做 DDL/DML/PRAGMA 写。审计中确需直连 DB 时只允许 `mode=ro` 只读 URI。
- **写入白名单（仅此两处）**：
  1. `docs/audits/2026-08-20/`（按 §2 的 AUDIT_ROOT/attempt 语义）；
  2. `/tmp/opencode/`（草稿、探针脚本、中间输出、venv-source 副本）。
  仓库 `.venv/` **只读**：通过 §0.1 三检的既有 `.venv` 允许读取执行；一切创建/修复/安装只发生在 venv-source 活动命名空间——**禁止写仓库 `.venv/`**（其缺失/损坏/三检不过时直接切副本模式，不在工作区重建）。
- **symlink 逃逸防御**：U0.0（最先执行，先于任何产物写入）对**校验根组**中两个写入根（`docs/audits/`、`/tmp/opencode/`）逐级 `os.lstat`/`os.path.realpath` 验证（检查每一中间层级）——任何一级是 symlink 且 realpath 落在对应允许根之外 → 该路径禁用，AUDIT_ROOT 直接改定 `/tmp/opencode/audit-fallback/`（同样验证），并在 README 顶部声明。只读执行根（仓库 `.venv/`、后续经 `resolve_active_namespace` 启用的各活动命名空间）在**首次使用前**单独逐级验证；仓库 `.venv/` 为 symlink 时不跟随安装、仅尝试只读使用（使用前对 realpath 复验）。
- **防覆盖 / 重跑策略（no-clobber + attempt 语义 + 自建文件更新规则）**：
  - 首次创建的产物文件使用 no-clobber 语义（存在即不再直接覆盖：`cp -n` / python `open(..., 'x')` / 先 `test -e` 再写）。
  - **自建文件更新**：状态类文件（README 进度表、INDEX、F-NNN 状态字段等审计自身产出的定稿文件）允许更新，但必须走「写同目录临时文件 → `os.replace` 原子替换」，且替换前将旧版本复制到 `AUDIT_ROOT/logs/superseded/`（文件名带时间戳）归档；attempt-N 目录内全部文件默认自建，同规则适用。
  - `docs/audits/2026-08-20/` 已存在时：读取目录内 manifest（U0.10 产物）——若其中 HEAD 与当前 `git rev-parse HEAD` 一致且 manifest 校验通过 → 判定为**同快照恢复**，续写未完成单元（目录视为本审计谱系自建，既有文件按上一条「自建文件更新」规则演进）；否则 → 以排他创建新建子目录 `docs/audits/2026-08-20/attempt-<n>/`（n = max(现有 attempt 编号)+1，`os.mkdir` 排他创建，编号冲突则 n+1 重试）作为新的 AUDIT_ROOT，旧目录原样封存不清空。fallback 目录同规则。
  - **命名空间解析与恢复（适用一切 `/tmp/opencode` 下按 `<kind>/<BASELINE_HEAD>/<attempt-id>/` 建立的排他命名空间，kind ∈ {baseline-snapshot, venv-source, probes, runtime-cache}）**：
    - **解析规则（惰性启用）**：`probes` 与 `runtime-cache` 两类在 U0.2 开始前**前置**执行 `resolve_active_namespace(kind)`：命名空间根不存在 → 排他创建（逐级 symlink 校验 + `os.mkdir` 排他）并写 `owner.json`（attempt-id、创建时间、所属 AUDIT_ROOT 绝对路径）→ 即活动命名空间；已存在 → 走下方恢复校验。`baseline-snapshot` / `venv-source` 为**条件性 kind，惰性启用**（启用条件见生命周期条：前者仅在脏基线判定后、后者仅在副本模式判定后），前置阶段仅在 `namespaces.json` 记 `state:"unused"`，**不创建实体目录**；启用时再走同款解析/恢复流程并把状态原子更新为 `active`。**解析结果持久化为 `manifest/namespaces.json`（kind → 绝对路径 + owner.json hash），此后本审计一切涉及该 kind 的命令只能引用解析返回的活动路径**（附录 A 以 `<PROBES_NS>` 等占位符表示），更新走自建文件原子替换规则。
    - **恢复校验（机械分支，不立即判 blocker）**：① `owner.json` 存在且 attempt-id / AUDIT_ROOT 匹配本 attempt；② 按 kind 的完成记录语义校验（见下）；③ 已登记文件逐个 sha256 一致。**全部通过 → 判定为本 attempt 前次中断的自身产物，只读复用为活动命名空间**（不重建不覆盖）；任一缺失/不符 → 视为不完整残留，以排他创建新建 `recovery-<n>`（n 从 1 递增，冲突则 n+1）子命名空间承载本次产物，并将命名空间根下 `CURRENT` 指针文件原子更新（临时文件 + `os.replace`）指向新子命名空间，旧目录原样封存；**始终禁止覆盖既有目录内容**。
    - **生命周期（分 kind 冻结）**：① `probes` = 追加式——**不设全局 COMPLETE**，每个脚本创建后立即将 `名字 → sha256` 原子提交进命名空间内 `probes-manifest.json`（临时文件 + `os.replace`）；**六个基础验证脚本**（唯一清单，其余文本一律引用：`run_isolated.py` / `freeze_baseline.py` / `verify_baseline.py` / `gen_expected_keys.py` / `validate_route_census.py` / `validate_gap_matrix.py`）允许按需分期创建（run_isolated/freeze/verify 在 U0.2、gen/validate_route_census 在 E2、validate_gap_matrix 在 A12），一次性分析探针（含 inventory 生成器、§9 探索脚本）同规则逐个登记；恢复校验只要求 owner.json 匹配 + probes-manifest.json 已登记条目逐文件 hash 一致——**目标脚本名已存在但未登记 → 该命名空间判不完整残留，切 recovery 子命名空间重建，禁止忽略后原地续建**。② `baseline-snapshot` / `venv-source` = 一次性条件产物（惰性启用，见上）——启用时排他创建 + owner.json；产出单元完成时生成 `namespace-manifest.json`（schema 冻结：`{files: {相对路径: {type: file|dir, sha256}}, excluded: [glob]}` 全量树清单；`venv-source` 的 `.venv/` 内已知不稳定文件——`*.pyc` / `__pycache__/` / `.pytest_cache/` / pip 缓存与日志——显式列入 `excluded`，排除集合冻结为该四类 glob）并全量验证（excluded 语义 = 跳过 hash 但目录结构仍在）后**原子写 `COMPLETE` 哨兵**；恢复校验要求 owner.json 匹配 + COMPLETE 存在 + manifest 全量复验一致，任一不符 → recovery 分支；**恢复复用 venv-source 时除 manifest 校验外须对副本 venv 重跑 §0.1 三检等效校验**，失败 → recovery 重建。
  - **AUDIT_ROOT** = 本轮实际生效的产物根（正常态 `docs/audits/2026-08-20/`，重跑态 `.../attempt-N/`，降级态 `/tmp/opencode/audit-fallback/`）。本方案后续所有产物路径均相对 AUDIT_ROOT 解释；§10.2 终止判据中的路径检查同样以 AUDIT_ROOT 为准。
- **构建/测试副产物隔离**：运行 `./scripts/check.sh`、任何 pytest、任何 pip、任何导入 `src` 的 python 命令时，一律经**统一执行器** `<PROBES_NS>/run_isolated.py`（**第六个基础验证脚本**，U0.2 最先创建并归档）：读 `manifest/runtime-mode.json` 的 `env_template` + 从 run-sequence 分配本次 RUN_ID 展开为本次 env，再 `subprocess.run(argv, cwd=check_root, env={**os.environ, **env}, shell=False)`。**bootstrap 阶段**（runtime-mode.json 尚未生成时的三检与 venv-source 副本构建）：执行器改读 U0.2 持久化的 `manifest/bootstrap-env.json`（键集与 env_template 相同；RUN_ID 同样经 run-sequence 分配唯一值，形态 `b-<n>`，无固定值）——**禁止任何裸 shell 直接执行三检 / pip / 导入 src 的命令**。**禁止在本仓库工作区执行任何向 `src/` 写入的安装操作**：若 H4 判定需重建 venv，冻结流程 = 在 venv-source 活动命名空间（经 §0.1 `resolve_active_namespace('venv-source')` 惰性启用，活动路径持久化于 `manifest/namespaces.json`，下文以 `<VENV_NS>` 指代；创建前执行与 U0.0 同级 symlink 校验 + 排他创建）复制**冻结输入清单对应的完整仓库树**（含 `docs/specs/INTERFACE_MAP.md` 等 check.sh 及其测试所需的一切文件——以「在该副本内运行 `./scripts/check.sh` 能完整执行」为完备判据），在副本内经统一执行器以 bootstrap env 执行 `python -m venv .venv` 与 `.venv/bin/pip install -e '.[test]'`，此后 check.sh 与一切 pytest **在该副本目录内运行该副本的脚本与 venv**（副本与基线 sha256 逐文件一致，故其 check 结果即基线结果）；仓库工作区**零 egg-info 零安装副产物**，C13 对 `src/*.egg-info` 零新增（无豁免通道）。**审计解释器规则（运行模式单点持久化）**：可用性判据冻结为三检（全部经统一执行器以 bootstrap env 执行，不产生仓库内副产物）——① `.venv/bin/python --version` 成功；② `.venv/bin/python -m pip check` 退出码 0；③ `.venv/bin/python -c "import pytest, oc_slimapi"` 成功。三检全过 → 运行模式 = **工作区**（check_root=仓库根）。**失败分类算法（冻结）**：①/② 失败，或 ③ 中 pytest 本身不可导入 → **环境故障** → 切换 venv-source 副本模式（按本节冻结流程构建，经 `resolve_active_namespace('venv-source')`，完成后写 `COMPLETE` 哨兵）；③ 中 `import oc_slimapi` 失败 → 在新建副本内重装后重试导入——**副本中 `pip check` 干净而同源码导入仍失败 → 判定为被审计源码自身缺陷**（SyntaxError/循环导入/初始化异常）：按 §3.3 记缺陷发现（红基线），运行模式仍取副本（可跑 pytest 子集定位），**不得记环境 blocker**；副本中导入成功 → 环境故障，副本模式成立。副本模式确立后**以副本重跑一次基线 check** 再终判。判定结果由 U0.3 持久化为 `manifest/runtime-mode.json`，**schema 冻结**：`schema_version`（=2；恢复读到其他版本 → 按字段差异确定性迁移或记 BLOCKED，禁止猜缺省值）、`mode`（`workspace` | `venv_copy`）、`state`、`check_root`（绝对路径）、`python`（解释器绝对路径）、`check_argv`（字符串数组，如 `["./scripts/check.sh"]`）、`env_template`（环境**模板**，冻结键集：`PYTHONPYCACHEPREFIX`=`<RUNTIME_NS>/runs/<RUN_ID>/pycache/`；`TMPDIR`=`<RUNTIME_NS>/runs/<RUN_ID>/tmp/`；`PIP_CACHE_DIR`=`<RUNTIME_NS>/pip-cache/`（或等价改设 `PIP_NO_CACHE_DIR=1`）；`PYTEST_TEMPLATE`=`-p no:cacheprovider --basetemp=<RUNTIME_NS>/runs/<RUN_ID>/pytest/`）。**动态展开规则（统一执行器冻结）**：每次执行前在 `<RUNTIME_NS>/run-sequence.json`（内容 `{"next": <n>}`）上经锁文件 `run-sequence.lock` 串行化地原子读-增-写，分配唯一 `RUN_ID`（形态 `<prefix>-<n>`：bootstrap 调用 prefix=`b`、runtime 调用 prefix=`r`，n 为全局唯一递增正整数）；本次 env = env_template 全键展开 `<RUN_ID>`，且当 argv 为 pytest 调用时附加 `PYTEST_ADDOPTS=<PYTEST_TEMPLATE 展开值>`；每次运行的 basetemp/pycache/TMPDIR 写入限于 `runs/<RUN_ID>/` 内，**run 目录不复用**；`<RUNTIME_NS>` = **runtime-cache 活动命名空间**（`resolve_active_namespace('runtime-cache')`，U0.2 前置启用），承接 pytest basetemp / TMPDIR / pip 缓存 / pycache 全部运行时写入，**禁止散写 `/tmp/pytest-*`、`/tmp/pip-*`、`~/.cache/pip` 或跨 attempt 共享全局 pycache**。**runtime-cache 独立生命周期（不套用 probes 追加式 hash 登记）**：可变运行时内容不做逐文件 hash、不写 probes-manifest.json；完整性只验 `owner.json` 与 `run-sequence.json` 存在且格式合法；每次运行排他创建不可复用的 `runs/<RUN_ID>/` 子目录；恢复后旧 run 目录一律只读封存——不从中执行代码、不复用 pycache、不删改；run 分配异常 → recovery 子命名空间。**状态机**：本 schema 另含 `state` 键——U0.3 写入 `provisional`；U0.4 最多执行一次 workspace→venv_copy 转换（按失败分类算法）后原子更新为 `final`；**final 后**一切定点 pytest、V4、C12、§10.2 只读该文件，执行方式冻结为 `subprocess.run(check_argv, cwd=check_root, env={**os.environ, **env}, shell=False)`（数组 argv，无 shell 解析），禁止再变更模式；恢复时只复用 `final`，读到 `provisional` → 从 U0.4 重走转换。
- **探针脚本**一律放 probes 活动命名空间（§0.1 `resolve_active_namespace('probes')`，恢复与登记规则见上）。**六个基础验证脚本**（唯一清单见 §0.1 生命周期条）创建后立即将脚本副本 + SHA-256 归档至 `AUDIT_ROOT/logs/probes/`，此后**每次调用前先校验脚本 hash 与归档一致**（防跨 attempt 污染与外部篡改，校验失败 = 环境异常 blocker）；一次性分析探针（含 U0.6 inventory 生成器、§9 自由探索脚本）无归档要求，但同样只写本命名空间并逐个登记 probes-manifest.json。**不**向项目环境安装新 pip 依赖（`pip install -e '.[test]'` 仅允许在 venv-source 副本内执行；禁止引入 lint/formatter 等新工具链——方法论用手工 rg + 读码替代）。

### 0.2 双轨裁决规则（规范 vs 事实，禁止混用）

**轨一：规范裁决序**（判定「什么是对的」，用于裁定契约违反）：
1. `docs/specs/v3-contract.md` 与 `docs/specs/v4-contract.md`（wire 权威）；
2. `CHANGELOG.md`（行为变更记录）；
3. `AGENTS.md`、`docs/release.md`、`docs/operations.md`（工程规约）；
4. `docs/specs/design-*.md`（设计历史，已知行号漂移，仅作背景）。

**轨二：行为事实裁决序**（判定「系统实际怎么做」，用于描述现状）：
1. 源码（审计基线快照，见 §0.4）；
2. 测试（通过中的测试 = 行为被锁定的证据）；
3. 本地实验输出（/tmp/opencode 探针）。

**两轨交互规则（关键）**：
- 规范与事实冲突 → **必须**记为发现：类别 `contract`，表述为「实现偏离契约」，规范方为权威。**测试的存在与通过不能豁免契约违反**——若测试锁定了偏离行为，发现额外注明「测试正在锁定偏离行为，修复需同步改测试」。
- 轨二内部冲突（代码与测试矛盾）→ 以可复现实验裁定，记录实验过程。
- 轨一内部冲突（两契约节互斥）→ 本身即发现（类别 `contract`）。
- 设计文档与契约冲突 → 不是发现（设计文档低于契约），只在 D14 记漂移。

### 0.3 已冻结的 owner 终态裁决（审计不得推翻，只验证落地）

- 协议版本**封顶 4 系**；wire (3,4) 为**永久双版本**；5.0.0 计划已取消（4.1.0 节）。
- B6-2（sticky/三形状退役）已取消。
- **cascade 编排层**与 **cross-session search** 为**永久 non-goal**（4.3.0 §17）。
- 直连回退通道（ocdroid→opencode 14096）目标态退役、仅服务匿名消费方。
- v3 语义**冻结**：`?v=3` 面逐字节不变是 4.x 系列发布的回归基线。

**审计口径约束**：「v3 可淘汰性」按双口径评估——(a) 消费方迁移到 `?v=4` 的完备度与差距清单；(b) 假设未来淘汰 v3 的拆除成本/风险清单。口径 (b) 是**成本模型**，不是政策建议——最终报告中不得出现「建议淘汰/建议重启双版本裁决」类结论；现行裁决下的输出刻度仅限「维持现状」与「可启动的机械性迁移前置准备」（如补测试、补文档，见 §8.3）。

### 0.4 快照一致性与引用规则

- 行号以**审计基线快照**为准：Phase 0 记录 `BASELINE_HEAD=$(git rev-parse HEAD)` 并生成 tracked-file hash manifest（U0.10）。
- 长审计期间工作区可能被外部进程改动。每个 Phase 边界必须重验（§7 V0）：HEAD 未变 且 `git status --porcelain` 除 AUDIT_ROOT/白名单外无新增改动 且 manifest 复验通过。**变化时的处理**：封存当前 attempt（见 §0.1 重跑策略），受影响单元基于新一致快照重跑；或者改用 `git show <BASELINE_HEAD>:<path>` 读取基线版本内容继续（**例外**：U0.2(b) 记录的脏基线文件禁止用 `git show` 替代——HEAD 版本 ≠ WIP 基线内容，此类文件的基线内容以 `/tmp/opencode/baseline-snapshot/` 快照副本为准）。**禁止把跨快照的证据合并进同一结论**——每份报告/发现必须声明其证据所属快照（BASELINE_HEAD 短哈希）。
- 报告与发现用**中文**；代码符号、路径、错误码、HTTP 术语保持英文原文。所有发现必须引用 `文件路径:行号`。

### 0.5 无疑问原则

- 本方案已内置全部假设（§1.3）。执行中出现方案未覆盖的情况：按 §10 阻塞规则记录并继续其余工作，**不提问、不等待**。
- §9 的「自由探索」是**有边界的授权**，不是模糊指令。

---

## 1. 背景事实与假设

### 1.1 项目定位与部署姿态（三分入口）

oc-slimapi 是 ocdroid（Android 客户端）与 opencode（上游 server :4096，legacy `/session/**` API）之间的 Python 省流 sidecar。v4 起经只读投影源读 opencode SQLite（`mode=ro`，绝无写入）。catch-all 反代已于 3.0.0 关闭：`proxy.py` 现为**纯 404 终端边界**（未收编路径一律 404 `thin_route_not_found`，WebSocket 501 stub）。

**部署姿态（审计必须三分处理，不得假设单一入口）**——依据 `deploy/oc-slimapi.service`（U0.7 必读）与 `docs/operations.md`：

| 入口 | 事实 | 审计含义 |
|---|---|---|
| E-I loopback | `127.0.0.1:4097` + 本机 stunnel mTLS 14097 | 原设计姿态；客户端输入仍不可信 |
| E-II 全接口 | systemd 单元实际设 `OC_SLIMAPI_HOST=0.0.0.0`（`:28`），依赖防火墙/Tailscale ACL | **当前稳态**（operations.md 认可）；绕过 stunnel 直打 4097 的明文面必须纳入威胁模型 |
| E-III stunnel mTLS | 14097 → loopback 4097 | 认证由 stunnel 提供，sidecar 自身无认证层 |

ACL 实际配置不在本机可验证范围内（H6）：凡依赖「ACL 挡住了 E-II」的论断，一律标注 **「部署边界未验证」**，不得据此裁决「无高危」。

### 1.2 事实基线

**A 组：本方案撰写时已亲自验证（2026-08-20，HEAD=0b836e7 release: v4.4.0）**：

- `src/oc_slimapi/`：**71 个 .py，共 26,452 行**（`find src -name '*.py' | wc -l` = 71；`wc -l` 合计 26452）。
- 关键文件行数：`sse/tokenstream/hub.py` **2190**、`routes/messages.py` **1643**、`config.py` **1158**、`proxy.py` **51**（纯 404 边界）、`sse/token_hub.py` **23**、`versioning.py` 44。
- `src/oc_slimapi/versioning.py`：`SERVER_API_VERSION = 4`（:38）、`ACCEPTED_CLIENT_VERSIONS: tuple[int, int] = (3, 4)`（:44）。**不存在 `FALLBACK_API_VERSION` 符号**。
- `deploy/oc-slimapi.service`：`OC_SLIMAPI_HOST=0.0.0.0`（:28）、`OC_SLIMAPI_PORT=4097`（:29）、`OC_SLIMAPI_UPSTREAM=http://127.0.0.1:4096`（:30）、`OC_SLIMAPI_MAX_MESSAGE_BYTES=33554432`（:31）、**残留 v2 时代 env**：`OC_SLIMAPI_SERVER_API_VERSION=2`（:32）、`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2`（:33）、snapshot retain 30 天（:45）、access log retain 3 天（:46）。
- 消费方现状：oc-webui 为 **v4 消费方**（CHANGELOG 4.4.0「oc-webui（v4 消费方）」、4.0.0 建议 oc-webui 经 versions 探测启用 v4）；ocdroid 仍在 `?v=3`（4.0.0 消费者行动项）。4.2.0 所称「v4 尚无消费方」已被 4.4.0 的表述演进覆盖。
- `proxy.py` docstring 自述：v2 时代透明反代 catch-all 已退役；turn-fence S2 bump 已移至 `routes/write_groups.py::_write_passthrough`；shell/PTY deny list、directory 校验、raw-query 转发等随 forwarder 一并删除。

**B 组：预测绘数据（来自一次快速探索，已证明部分失真）——仅作线索，禁止作为事实引用**：

- 早期测绘称 ~70 文件/~14,200 行、`global_hub.py` 1090 行、`app.py` 785 行、`singleflight.py` 770 行、`dbaux/lifecycle.py` 768 行、`hub_types.py` 756 行、`read_groups.py` 745 行、`write_groups.py` 636 行、`sessions.py` 625 行、测试 107 文件/2642 test、TODO 2 处（`tokenstream/hub.py`）、SQLite 触点 4 文件、`mode=ro`+`PRAGMA query_only` 双保险（`dbaux/lifecycle.py`）。
- **禁用规则**：上表所有数字与行号在审计正文中**一律不得引用为事实**；每个用到处必须先经 U0.6 inventory 或亲自 `wc -l`/`rg` 重锚定。A 组数字可直接引用但仍须在 U0.6 复核。

### 1.3 工作假设（不需要确认，直接采用）

| # | 假设 | 依据 |
|---|---|---|
| H1 | 今日为 2026-08-20；工作区 = `/home/mar/personal_projects/oc-slimapi`，主线 main，HEAD=0b836e7 | 本方案撰写时验证 |
| H2 | 上游源码快照可读：`/home/mar/personal_projects/ocdroid/opencode-src/current` → v1.18.18 完整 monorepo | AGENTS.md |
| H3 | 消费方：ocdroid 在 `?v=3`；oc-webui 在 `?v=4`（见 §1.2 A 组） | CHANGELOG 4.4.0/4.0.0 |
| H4 | `.venv` 通过 §0.1 三检（`--version` + `pip check` + `import pytest, oc_slimapi`）；失败按 §0.1 分类算法处置（环境故障 → `<VENV_NS>` 副本模式；产品缺陷 → 发现+红基线），源工作区零安装副产物 | AGENTS.md + §0.1 |
| H5 | 审计不连接真实上游/生产；测试体系为 MockTransport | 项目测试现状 |
| H6 | 不做实机部署验证（systemd/防火墙/Tailscale ACL 不在可达范围）；凡涉 ACL 前提的结论标「部署边界未验证」 | 无人值守约束 |
| H7 | 审计开始时 git 工作区干净；若不干净，U0.2(b) 冻结脏基线（manifest + `/tmp/opencode/baseline-snapshot/` 快照副本）后照常审计（以工作区现状为基线） | — |
| H8 | `deploy/oc-slimapi.service` 是生产部署的权威样本（E-II 姿态），但本机不是该生产机；单元文件与 `docs/operations.md` 的差异本身是审计对象 | §1.1 |

### 1.4 重要的历史 bug / 已知陷阱（复核修复完整性 + 举一反三）

| 陷阱 | 事实 | 审计动作 |
|---|---|---|
| `session.status` 对象格式 | 3.3.1：上游携带 `{"type":"busy"}` 对象，sidecar 曾按字符串比较 → sticky lastError 永不清除；修复 = `normalize_session_status()`（`sse/hub_types.py`，行号待重锚定） | A6：核对全部 `session.status` 消费点归一化；举一反三：全部「对上游事件/响应形状做 isinstance 假设」的代码点逐一对照 v1.18.18 实际形状 |
| 4.0.0 删 Vary bug | v4 sessions 曾漏 `Vary: Accept-Encoding`，4.2.0 §15 修复 | A4：验证修复测试锁定存在 |
| 3.1.0 text 折叠事故 | 折叠超阈 6 字节致客户端空壳；3.2.0 回滚全量内联 | A12：确认回归矩阵仍在 |
| 行号漂移 | v4-contract §4.1 上游行号曾漂移（勘误：session.ts 486 行，`list()` :261-299） | 上游引用一律以 Phase 1 实读为准 |
| merged 内存上界 | 1.5.0：merged 页瞬时持有缓冲可超「8 MiB 公式」（顺风车 body 可达单消息上限量级）——已披露代价 | A9：验证披露与代码一致，评估未披露路径 |
| PATCH 双 shape | 2.0.0：PATCH 同时接受 title/metadata/permission 与 `time.archived` | A4：契约与实现一致性 |
| deploy 残留 v2 env | `deploy/oc-slimapi.service:32-33` 仍设 `OC_SLIMAPI_SERVER_API_VERSION=2`、`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2`；而 4.0.0 起 `SERVER_API_VERSION` env 已废弃（设置时 warning 并忽略）、`ACCEPTED_CLIENT_VERSIONS` fail-closed 钉死 (3,4) | A13/A15：验证「warning+忽略」「fail-closed 拒启动」的实际行为与测试；deploy 文件滞后本身记发现 |

---

## 2. 交付物与目录结构

**AUDIT_ROOT**（§0.1 定义的产物根）下创建（全部 UTF-8，Markdown 为主）：

```
<AUDIT_ROOT>/
├── README.md                     # 索引 + 总进度清单（含 attempt 元数据：BASELINE_HEAD、时间窗、恢复/新建判定）
├── manifest/                     # 快照一致性（U0.10）
│   ├── baseline-head.txt         # BASELINE_HEAD 全哈希 + 短哈希
│   ├── input-paths.txt           # 冻结输入路径全集（freeze_baseline.py）
│   ├── deleted-paths.txt         # 删除墓碑
│   ├── file-hash-manifest.txt    # 存在文件 sha256 清单（唯一入口 freeze_baseline.py，见附录 A）
│   ├── ignored-baseline.txt      # ignored 副产物基线（C13 对照）
│   ├── runtime-mode.json         # 运行模式（schema §0.1）
│   ├── namespaces.json           # 四类 /tmp 命名空间活动路径解析结果
│   ├── bootstrap-env.json        # bootstrap 阶段冻结执行环境（统一执行器输入）
│   └── phase-verify/             # 每次边界重验一条一文件（<seq>.json）+ 原子重建 index.json
├── 00-baseline.md                # Phase 0 产物
├── 01-explore/
│   ├── inventory.json            # U0.6 机器可读 inventory（唯一数字源，schema 见 U0.6）
│   ├── file-cards.md             # E1（含 src + tracked 可执行/部署文件）
│   ├── route-census.csv          # E2 机器真值源（schema 冻结）
│   ├── route-census.md           # E2（由 CSV 渲染）
│   ├── route-census-validation.log
│   ├── expected-keys.csv         # 期望键集合（A1/A12 共用）
│   ├── inherited-error-applicability.csv      # 继承错误适用表（schema 见 E2）
│   ├── inherited-error-applicability.validation.txt
│   ├── config-census.md          # E3
│   ├── state-machines.md         # E4
│   ├── dataflows.md              # E5
│   ├── docs-notes.md             # E6
│   ├── test-census.md            # E7
│   ├── upstream-notes.md         # E8
│   └── exploration-log.md        # 自由探索日志 + blockers 节
├── 02-findings/
│   ├── F-001.md ... F-NNN.md
│   └── INDEX.md
├── 03-reports/
│   ├── D01-v4-completeness.md    # A1
│   ├── D02-v3-retirement.md      # A2
│   ├── D03-legacy-passthrough.md # A3
│   ├── D04-contract-quality.md   # A4
│   ├── D05-concurrency.md        # A5
│   ├── D06-sse-state-machines.md # A6
│   ├── D07-dbaux.md              # A7
│   ├── D08-security.md           # A8
│   ├── D09-performance.md        # A9
│   ├── D10-code-quality.md       # A10
│   ├── D11-modularity.md         # A11
│   ├── D12-test-quality.md       # A12
│   ├── D13-observability-ops.md  # A13
│   ├── D14-docs-drift.md         # A14
│   └── D15-release-supply-chain.md # A15
├── 04-final/
│   ├── AUDIT-REPORT.md
│   ├── v3-retirement-plan.md
│   ├── refactor-backlog.md
│   ├── test-gap-matrix.csv       # schema 冻结见 A12
│   ├── test-gap-matrix.validation.txt  # validate_gap_matrix 输出（VALID / BLOCKED-STUB）
│   └── verification-log.md
└── logs/
    ├── check-baseline.txt        # U0.4 基线 check 输出
    ├── check-final.txt           # V4 复跑输出
    ├── pip-list.txt / pip-check.txt
    ├── probes/                   # 基础验证脚本副本 + SHA-256 归档
    └── superseded/               # 自建文件原子替换的旧版本归档
```

---

## 3. 证据与发现规范

### 3.1 证据规范

- 每个非平凡论断必须有 `路径:行号` + 关键代码原文（3-10 行摘录）或命令输出摘录，且声明证据所属快照（BASELINE_HEAD 短哈希）。
- 引用契约精确到节号（如「v4-contract §12.3」）。引用上游行为指向 `opencode-src/current/` 具体 file:line。
- 声称「不存在 X」必须附带负向搜索证据（rg 命令 + 空输出摘录）。
- 实验类证据（/tmp/opencode 探针）附：输入、命令、输出、可复现脚本路径。

### 3.2 发现文件格式（`02-findings/F-NNN.md`）

```markdown
# F-NNN：<一句话标题>
- 严重度：P0/P1/P2/P3（按 §3.3 决策表定级，须写明命中路径）
- 类别：contract|security|concurrency|sse|dbaux|performance|quality|modularity|test|docs|ops|supply-chain|retirement
- 位置：<file:line 列表>
- 置信度：high|medium|low
- 快照：<BASELINE_HEAD 短哈希>
- 状态：draft → verified → confirmed / refuted / unverified_due_to_blocker（第三终态仅当证据源因 BLOCKED 不可达：须关联 blockers 节编号、列明缺失证据与受影响裁决；**不计入 confirmed 统计**，关联核心裁决自动标 `coverage-degraded`；refuted 销案须留案卷）
## 证据
## 分析（机制、影响面、触发条件、可达性）
## 契约/文档对照（规范轨引文）
## 复核记录（Phase 3 填写：自我证伪尝试、反证搜索命令与结果）
## 建议方向（非实现）
```

### 3.3 严重度决策表（统一定级，禁止自动升级）

**第一步：根因分类**。任何候选发现先归类：`defect`（行为错误）/ `contract-violation`（规范违反）/ `risk`（潜在风险）/ `gap`（缺口：测试/文档/观测）/ `smell`（坏味道）。环境/工具故障（如 check.sh 因依赖损坏变红）**不是发现**，是 blocker（§10.1）。

**第二步：按类别走决策表**：

| 类别 | P0 条件 | P1 条件 | P2 条件 | P3 |
|---|---|---|---|---|
| defect | 可达输入触发错误行为，**且**满足 P0 影响门槛（见下） | 可达输入触发错误行为且消费方可观察，但未达 P0 门槛（如仅状态码/头部/表示细节偏差）；或需罕见但真实的条件组合；或影响仅内部记账 | 边角畸形输入；无消费方触达路径 | 理论性 |
| contract-violation | 违反冻结契约，消费方可观察，**且**满足 P0 影响门槛（见下） | 违反冻结契约且消费方可观察但影响限于非关键表示细节；或违反冻结契约但当前无消费方使用该面；或契约内部矛盾影响未来裁决 | 漂移/滞后类 | 措辞歧义 |
| risk（安全/并发/资源） | 威胁模型内可达 + 利用条件现实 + 影响 = 数据泄露/写越界/挂死 | 可达但利用条件苛刻，或影响 = DoS/降级 | 需管理员误配配合 | 深层防御建议 |
| gap | — | 硬不变量缺测试锁定 **且** 该面被消费方直接使用 | 硬不变量缺测试锁定但面未被使用；文档缺口 | 观测增强 |
| smell | — | — | 冗余/重复热区 | 风格 |

**第三步（带记录的重定级，可升可降）**：初判后结合可达性与影响修正等级——可达性不足（需不现实的 precondition）降一级；「无上界」类发现必须先证明可增长路径与生产影响（输入源可控、无 TTL/容量自然约束）才可定 P1，否则 P2 起步；反之若初判 P1 但复核发现满足 P0 影响门槛则升 P0。每次重定级必须在发现文件中记录「初判 → 终判 + 理由」。

**P0 影响门槛（defect / contract-violation 升 P0 的必要条件，至少满足其一）**：重大互操作中断（消费方正常流程被破坏，非仅表示细节）；数据完整性受损（丢/坏/重数据）；安全边界突破（越权/泄露/写越界）；广泛不可恢复影响（需重启/人工干预才能恢复，且触发面广）。

**校准锚点**：P0=需立即处理；P1=重大风险/债务，应进 backlog 前列；P2=应排期；P3=顺手做。

---

## 4. Phase 0 —— 环境与基线

> 每个单元完成后在 README.md 进度表打勾。产出：`00-baseline.md` + `manifest/` + `01-explore/inventory.json`。

- [ ] **U0.0 安全预检与 AUDIT_ROOT 选定（最先执行，先于任何产物写入）**：(a) 对 §0.1 校验根组的两个写入根（`docs/audits/`、`/tmp/opencode/`）逐级 symlink 验证（python `os.lstat` + `os.path.realpath`，检查每一中间层级）——**全部写入根验证完成前只允许进程内存暂存结果，禁止写任何文件（含 `/tmp/opencode/`，它本身正是待验证路径之一）**；只读执行根（仓库 `.venv/` 与后续惰性启用的活动命名空间）在首次使用前单独验证；(b) 按 §0.1 规则选定 AUDIT_ROOT（`docs/audits/2026-08-20/` 已存在 → 读其 manifest 判同快照恢复 / 新建 attempt-<n>（n = max(现有编号)+1，`os.mkdir` 排他创建，冲突则 n+1 重试）；不存在 → 直接创建；symlink 违例 → `/tmp/opencode/audit-fallback/` 同规则）；(c) 结束条件二选一：**新建分支** = 排他创建 AUDIT_ROOT 根目录成功；**恢复分支** = 既有 AUDIT_ROOT 通过验证（manifest 存在、HEAD 匹配、manifest 校验通过）。（d） 判定完成后**立即创建 §2 目录骨架**（仅空目录，不写任何常规文件：`manifest/`（含 `phase-verify/`）、`01-explore/`、`02-findings/`、`03-reports/`、`04-final/`、`logs/probes/`、`logs/superseded/`；新建分支逐级 `os.mkdir` 排他、恢复分支 `exist_ok=True` 幂等核对既存骨架）——目录骨架不算「产物文件」，此后 U0.1 起一切文件写入均有落点；（d）完成前本单元不写任何文件。违例与判定结果记入后续 baseline。
- [ ] **U0.1 快照锚点**：`git rev-parse HEAD`（全哈希）、`git status --porcelain`、`git log --oneline -5` 记入 baseline 与 `manifest/baseline-head.txt`。工作区不干净（H7）时逐条记录 `git diff --stat`。
- [ ] **U0.2 工作目录、基线冻结与 ignored 基线**：(a) 前置：经 §0.1 排他启用 probes / runtime-cache 两命名空间（目录骨架已由 U0.0(d) 创建，此处只写 `manifest/namespaces.json`）；创建统一执行器 `run_isolated.py`（登记 + 归档）并生成 `manifest/bootstrap-env.json`（键集见 §0.1）；随后创建 README.md（进度表模板 + attempt 元数据），全部新建文件走 no-clobber；(b) **基线冻结（唯一入口 = 冻结脚本 `<PROBES_NS>/freeze_baseline.py`，归档与调用前 hash 校验规则见 §0.1 探针脚本条；禁止旁路手工拼管道）**：脚本内部逻辑冻结为——① 路径集 = 单一共享函数 `collect_present_input_paths()`（该函数实现同时编译进 verify_baseline.py，**两脚本禁止各写一套**）：（`git ls-files -z` 全部 tracked ∪ `git ls-files --others --exclude-standard -z` 全部 untracked 非忽略）∩ 磁盘存在，再排除当前 AUDIT_ROOT、`docs/audits/` 下全部历史 attempt/封存产物、fallback 目录与 `/tmp/` 下一切内容；② **删除墓碑**：tracked-but-missing（`git ls-files` 列出但磁盘不存在，即 WIP 删除）路径集落 `manifest/deleted-paths.txt`；③ 输出 `manifest/input-paths.txt`（存在的输入文件全集，一旦生成即冻结，此后新增的审计产物永不进入输入集）与 `manifest/file-hash-manifest.txt`（仅对存在文件 hash，`sha256sum -c` 兼容格式）。**脏基线判定** = 存在 tracked 已修改（脚本顺带输出与 HEAD 版本的差异表）、untracked 非忽略文件、或墓碑非空——此时经 §0.1 `resolve_active_namespace('baseline-snapshot')` **惰性启用**该命名空间（干净基线不创建实体目录，`namespaces.json` 记 `state:"unused"`）并将输入文件全集复制到活动命名空间（恢复分支——禁止覆盖既有内容，不完整残留 → `recovery-<n>` 新子命名空间；**墓碑清单一并保存到副本**；复制完成后对副本全量 sha256 复验与 manifest 一致并写 `COMPLETE` 哨兵），后续一切基线读取优先走该副本（§0.4 例外规则）；(c) **ignored 副产物基线**：`git status --ignored --porcelain` 输出存 `manifest/ignored-baseline.txt`（C13 对照用；egg-info 零容忍规则见 §0.1）。
- [ ] **U0.3 环境自检与运行模式持久化**：按 §0.1 三检判据判定运行模式（工作区 / venv-source 副本——后者按 §0.1 冻结流程构建：`<VENV_NS>` 完整仓库树副本 + 副本内 venv + editable 安装 + sha256 复验一致 + `namespace-manifest.json` + `COMPLETE`，惰性启用与恢复规则见 §0.1）；判定完成后将结果持久化为 `manifest/runtime-mode.json`（schema 冻结见 §0.1：schema_version/mode/state/check_root/python/check_argv/env_template），**本单元写入 `state:"provisional"`**，更新走自建文件原子替换规则；final 化在 U0.4 完成。当前模式 venv 的 `pip list` 与 `pip check` 输出存 `logs/pip-list.txt`、`logs/pip-check.txt`（A15 输入）。
- [ ] **U0.4 绿色基线**：读 `manifest/runtime-mode.json`（provisional 态），按 `check_argv`/`check_root`/`env_template`（经统一执行器展开为本次 env）以 §0.1 冻结执行方式运行（工作区模式 = 仓库根；副本模式 = `<VENV_NS>` 副本根运行该副本脚本——副本与基线逐字节一致，结果即基线结果）。完整输出存 `logs/check-baseline.txt`。期望全绿。**失败处理（严格按 §0.1 失败分类算法，最多一次转换）**：环境类故障（解释器/pip/依赖解析失败，或 pytest 本身不可导入）→ 构建 venv-source 副本并以副本重跑一次——通过则原子更新 runtime-mode.json 为 `mode:"venv_copy", state:"final"` 继续；产品缺陷类（副本 `pip check` 干净而同源码 `import oc_slimapi` 仍失败）→ **不转换**，按 §3.3 记缺陷发现（红基线），以可达范围继续；真实测试失败/一致性检查失败 → 按 §3.3 记缺陷发现（是否 P0 走决策表），以工作区现状为基线继续。本单元正常结束时 runtime-mode.json 必须为 `final` 态；若本单元整体 BLOCKED，state 停留 `provisional` 并在 blockers 记录，§10.2 按该 blocker 豁免 final 要求。
- [ ] **U0.5 耗时登记**：check.sh 总时长与测试计数（后续 V4 复跑对照）。
- [ ] **U0.6 机器可读 inventory（唯一数字源）**：生成 `01-explore/inventory.json`，此后一切单元引用数字只准引此文件或现场重跑命令。最小 schema：
  ```json
  {"generated_at": "...", "head": "<full-hash>",
   "src_files": [{"path": "...", "lines": N, "top_level_symbols": ["..."]}],
   "tests": {"files": N, "test_functions": N},
   "routes": [{"method": "GET", "path": "/slimapi/...", "defined_at": "file:line"}],
   "env_vars_read": ["OC_SLIMAPI_..."],
   "error_codes": ["..."],
   "todo_markers": [{"path": "...", "line": N, "text": "..."}],
   "tracked_executables": [{"path": "scripts/...|deploy/...", "lines": N}]}
  ```
  生成方式：python 脚本 `<PROBES_NS>/gen_inventory.py`（probes 命名空间登记规则适用）+ rg/AST 解析；同时把生成脚本路径与 sha256 记入 inventory。§1.2 A/B 组数字与之对照，差异记入 baseline（B 组失真不再修正原文，只声明以 inventory 为准）。
- [ ] **U0.7 上游与部署文件自检**：验证 H2 路径下附录 B 文件存在；读取 `deploy/oc-slimapi.service` 全文（§1.1 E-II 事实的再锚定）+ `deploy/stunnel.conf` + `deploy/actions.manifest.example.toml` 存在性。
- [ ] **U0.8 时间戳标记**：`date -Iseconds` 记入 baseline。
- [ ] **U0.9 symlink 验证记录归档**：将 U0.0(a) 的验证结果正式写入 baseline（若 U0.0 已触发 fallback，此处记录事件链）。
- [ ] **U0.10 hash manifest 归位**：确认 U0.2(b) 的 `freeze_baseline.py` 已产出 `manifest/input-paths.txt` + `manifest/deleted-paths.txt` + `manifest/file-hash-manifest.txt` 三件套（存在文件 hash + 删除墓碑，NUL-safe；禁止退化回 `git ls-files | xargs sha256sum` 的 tracked-only 无墓碑版本）；这是 §7 V0 边界重验的基准。
- [ ] **U0.11 计数复核命令块**（输出存 baseline 附录）：
  ```bash
  find src -name '*.py' | wc -l; find src -name '*.py' | xargs wc -l | tail -1
  rg -c "def test_" tests/ | awk -F: '{s+=$2} END {print s}'
  rg -n "TODO|FIXME|XXX|HACK" src/ || true
  rg -n "sqlite3\.(connect|execute)" src/
  rg -n "@router\.(get|post|patch|delete|put)" src/oc_slimapi/ -c
  git ls-files | wc -l
  ```
- [ ] **U0.12 阻塞清单初始化**：`01-explore/exploration-log.md` 建 blockers 节（空表：单元号/错误原文/已尝试手段/降级决定）。

**Phase 0 完成判据**：`00-baseline.md`、`manifest/*`、`inventory.json` 齐备且相互引用一致；README 进度表初始化。机器可读产物（inventory.json 等）同受 §8.1 二态约束：因 blocker 无法生成时必须落 BLOCKED-STUB（stub schema 见 §8.1），本判据对 STUB 的要求 = stub schema 合法 + blocker 关联 + coverage-degraded 传播。

---

## 5. Phase 1 —— 全量探索（E1–E8）

> 目标：为 Phase 2 提供完整、可引用、可复核的地图。只记录事实与疑问点（疑问点记 draft 发现），不下结论。产物落 `01-explore/`。**所有数字引用 inventory.json。**

### E1 文件级精读（file-cards.md）

**范围**：(a) `src/oc_slimapi/` 全部 .py（以 inventory 为准，预计 71 个）；(b) 全部 tracked 可执行/部署资产：`scripts/check.sh`、`scripts/release.sh`、`scripts/check_routes_doc.py`、`scripts/eqp_matrix.py`、`deploy/oc-slimapi.service`、`deploy/stunnel.conf`、`deploy/actions.manifest.example.toml`（以 inventory `tracked_executables` 为准）、`pyproject.toml`。

卡片模板：

```markdown
### <路径>（<行数，引 inventory>）
- 职责：<一句>
- 对外符号：<类/函数/常量清单，含签名>
- 依赖：内部 imports
- 被依赖：rg 反查主要调用方
- 状态/可变性：<长生命周期对象、锁、executor、task>
- 错误路径：<异常抛出/捕获/转换>
- 疑问点：<可疑处 → draft 发现>
```

执行纪律：
- 按依赖自底向上读（顺序建议：`versioning`/`features`/`errors` → `config`/`upstream*`/`gzip_util`/`directory` → `skeleton`/`envelope`/`etag`/`transform`/`singleflight`/`catalog_cache` → `traffic*`/`access_log`/`selector`/`readiness`/`providers_projection`/`turn_registry`/`qp_sweep`/`discovery`/`actions` → `dbaux/*` → `sse/*`（先 `hub_types`）→ `routes/*`（先 `_read_passthrough`/`_catalog_common`）→ `middleware/*` → `proxy.py` → `app.py`；资产文件穿插在相关主题后读，如 `release.sh`/`check.sh` 在读 `app.py` 前后）。
- **>500 行的文件必须全文读完**，禁止抽样（**>500 行集合完全由 inventory 行数动态生成**，不从本方案或任何固定清单取；生成命令：对 `src_files` 过滤 `lines > 500` 输出排序列表，存 file-cards.md 开头）。
- `app.py` lifespan 启动/关停序列单独列表（每个 `app.state.*` 资源：创建点/关停点/顺序/超时，行号现场锚定）。
- 每读完 10 个文件在 README 打中间进度。

**完成判据**：inventory 中 `src_files` + `tracked_executables` 的每个条目都有卡片；>500 行文件的卡片含逐方法清单。

### E2 路由普查（route-census.md）

对每条路由一行记录（**行全集 = 联合主键：inventory `routes`（实现侧）∪ v3-contract §10 路由表 ∪ v4-contract §10 路由表（规范侧）**——契约声明而实现完全缺失的路由同样占行；并与 `docs/specs/INTERFACE_MAP.md` 声称数对账）：

**一概念一列（列名即 CSV header，禁复合列）**：

| 列名 | 说明 |
|---|---|
| `method` | HTTP 方法（大写） |
| `path` | 路由模板原文（含 `{param}` 占位） |
| `actual_defined_at` | 实现侧装饰器 file:line；`MISSING` = 实现不存在 |
| `contract_refs` | 契据节号（`v3:§10.x` / `v4:§10` 形式，多个 `\|` 连接字典序）或 `NONE` |
| `presence` | `actual_only` / `contract_only` / `both`——**contract_only 行强制生成「契约要求但实现缺失」contract-violation draft 发现**，actual 侧集合列（supported_versions / error_codes local+inherited / feature_gate / has_boundary）记 `MISSING`（受限保留字，见各列行；生成器规范化为空集合/NO），照常进入 expected-keys 生成；行为类单值列（directory_consumption / upstream_call / projection / cache_dedup / traffic_bucket / test_files 等）**冻结为规范侧单值列**——契约有定义时填契约侧值，实现缺失语义由 `actual_defined_at=MISSING` 承载（不拆双列）；validator 校验 presence 与上述值的组合合法性 |
| `wire_version_face` | 速览字段：`v3` / `v4` / `dual` / `none`（规范期望以下两列为准） |
| `directory_consumption` | `query` / `header` / `both` / `none` / `tolerant` |
| `upstream_call` | 上游目标 path+方法或 `NO_UPSTREAM_IO` |
| `projection` | `skeleton` / `envelope` / `providers_v4` / `dbaux` / `none` |
| `etag_vary` | 简述有无及域（自由短文本） |
| `cache_dedup` | `catalog_cache` / `singleflight` / `none` |
| `traffic_bucket` | 桶名或 `NONE` |
| `interface_map_refs` | INTERFACE_MAP 行号或 `NONE` |
| `test_files` | 覆盖该路由的测试文件（`\|` 连接字典序）或 `NONE` |
| `actual_supported_versions` / `contract_supported_versions` | （两列）结构化枚举 `v3` / `v4` / `v3\|v4` / `none`——`actual_` 从路由实现中 `?v=` 校验与 selector 分支逐条判定；`contract_` 从契约（v3/v4-contract §10 路由表）判定；**两侧差集在 E2 当场生成 contract-violation draft 发现**，禁止以实现侧缩小规范期望；本列组另允许保留字 `MISSING`（**仅** `presence=contract_only` 行合法，validator 强制，生成器规范化为空集合） |
| `actual_error_codes_local` / `contract_error_codes_local` | （两列）**route-local 层**错误码全集（`\|` 连接字典序）——`actual_` 来源 = 路由处理器内错误构造点；`contract_` 来源 = 契约该路由节错误表；差集当场生成 draft |
| `actual_error_codes_inherited` / `contract_error_codes_inherited` | （两列）**继承全局层**错误码——**适用性按「E2 继承错误适用表」机械判定，禁止概括规则**。适用表 = 全局错误族 × 维度（path 前缀 / method / wire 版本 / readiness feature 态）的显式布尔矩阵，初始五族：① selector 版本 400 族——适用 path 前缀 `/slimapi` **且不在 selector 豁免清单内**（豁免清单至少含 `/slimapi/versions`，以 `selector.py` 实读为准填全）；② directory 消费错误族——适用 `directory_consumption ≠ none` 的路由；③ T3 准入族——适用 SSE 类路由（events / token stream）；④ `method_not_applicable` 405 族——适用契约 §16 声明的 POST 等效路由族 × `?v=4` 面（readiness 组合态决定可达性：actual 侧按当前 satisfied 态、contract 侧按契约声明全集）；⑤ middleware 通用族——request-id / 流量记账中间件不产生 HTTP 错误，如实记 `NONE`。E2 执行时先实读 `selector.py`/`directory.py`/middleware/registry 把适用表补全为**逐路由布尔格**并随 census 归档（`01-explore/inherited-error-applicability.csv`，**schema 冻结**：`method,path,family,side,wire_version,feature_id,feature_state,applicable,error_codes,evidence_ref`——`side ∈ {actual,contract}`；`wire_version ∈ {v3,v4,none}`；`feature_id` = readiness ID 或 `NONE`；`feature_state ∈ {on,off,none}`（actual 侧按当前 satisfied 态对受 §16 族影响者展开 on/off 两行、contract 侧按契约声明全集）；`applicable ∈ {0,1}`；`error_codes` = 该 (family,side) 生效的具体码集 `|` 连接字典序或 `NONE`；`evidence_ref` = `file:line`（actual）或契据节号（contract）；**主键 = 前七列全组**；行按主键字典序；**跨表 validator（并入 validate_route_census.py）**：census 每 (method,path) 每 side 的继承码集合 == 表中 applicable=1 行按 side 聚合的 error_codes 并集）；`actual_` 按实现构造点、`contract_` 按契约总则节（§2/§5/§8/§16）核定；差集当场生成 draft |
| `actual_feature_gate` / `contract_feature_gate` | （两列）readiness feature ID 或 `NONE`——`actual_` = 实现中 readiness 检查点；`contract_` = 契约 §3.3 门控声明；差集当场生成 draft；本列组另允许保留字 `MISSING`（仅 `presence=contract_only` 行合法，validator 强制，生成器规范化为空集合） |
| `actual_has_boundary` / `contract_has_boundary` | （两列）`YES` / `NO`——判定规则（双侧各自应用）：路由处理器（actual 侧）/ 契约该路由节（contract 侧）存在数值上限、枚举边界、空/缺省输入分支即为 `YES`；本列组另允许保留字 `MISSING`（仅 `presence=contract_only` 行合法，validator 强制，生成器规范化为 `NO`） |

执行：`rg -n "@router\.(get|post|patch|delete|put|options|head)" src/oc_slimapi/` 得全集 → 逐条补列 → 与 INTERFACE_MAP 逐行对账（多出/缺失/描述不符记 draft 发现）→ 读 `scripts/check_routes_doc.py` 判定逻辑（它是部分对账，审计做全量人工对账）。**重复注册处理**：同一 `(method, path)` 出现多个装饰器/注册点 → 单独 draft 发现（可能即缺陷），census 合并为单行（`actual_defined_at` 等多值字段 `|` 连接），不阻塞 E2 完成。

**E2 收尾必须产出 `01-explore/expected-keys.csv`（versioned 期望键集合，A1/A12 共用）**，生成函数冻结如下（对 E2 每行依次应用，输出列 `method,path,behavior`，RFC 4180）：

1. 每路由固定生成 `happy_path` 一行；
2. **版本面取双侧并集** `union = actual_supported_versions ∪ contract_supported_versions`：含 v3 且不含 v4 → 生成 `v3_face`；含 v4 且不含 v3 → 生成 `v4_face`；同时含两者 → **两者都生成**；`{none}` → 两个都不生成（并集为 {none} 时）——**禁止以 actual 侧缩小期望集**；
3. **错误码取双侧两层并集** `union = (actual_local ∪ actual_inherited) ∪ (contract_local ∪ contract_inherited)`，对并集**逐码**生成 `error_<code>` 行（只含该路由自身+机械继承的码，不做全局笛卡尔积）；
4. `actual_feature_gate ∪ contract_feature_gate ≠ {NONE}`（即任一侧有 feature）→ 生成 `feature_off` 一行；
5. `actual_has_boundary = YES 或 contract_has_boundary = YES` → 生成 `boundary` 一行（`MISSING` 规范化为 `NO` 后参与判定）。

输入列中的保留字 `MISSING`（仅 contract_only 行 actual 侧合法）在生成器入口一律规范化为**空集合**（boundary 为 `NO`）后再参与上述并集；`presence` 列承载缺失语义并驱动 E2 的缺实现 draft，期望键生成不直接消费 `MISSING`。

生成脚本放 `<PROBES_NS>/gen_expected_keys.py`（活动命名空间经 §0.1 `resolve_active_namespace('probes')` 解析；归档与调用前 hash 校验规则同 freeze_baseline.py）。**输入唯一真值源 = `01-explore/route-census.csv`（强制机器可读侧车，schema 冻结：header = 上表列名原样、列序 = 上表出现顺序，即 `method,path,actual_defined_at,contract_refs,presence,wire_version_face,directory_consumption,upstream_call,projection,etag_vary,cache_dedup,traffic_bucket,interface_map_refs,test_files,actual_supported_versions,contract_supported_versions,actual_error_codes_local,contract_error_codes_local,actual_error_codes_inherited,contract_error_codes_inherited,actual_feature_gate,contract_feature_gate,actual_has_boundary,contract_has_boundary`；RFC 4180 转义；集合字段以 `|` 连接按字典序；行按 (method, path) 字典序；空值显式 `NONE`）**；E2 收尾前用 `<PROBES_NS>/validate_route_census.py`（基础验证脚本，归档规则同前）校验 header/列序/枚举/排序/主键唯一 + applicability 跨表一致性，**原子写出两份验证产物**：`01-explore/route-census-validation.log`（census 四项 + applicability schema/主键校验）与 `01-explore/inherited-error-applicability.validation.txt`（10 列 schema + 主键七列 + 与 census 继承列聚合一致性的跨表结果）；`route-census.md` 仅由 CSV 渲染供人阅读，生成器只接受 CSV 输入。输出写入 `01-explore/expected-keys.csv` + 行数记录；此后 A1 矩阵与 A12 矩阵的期望键集合**只能**引用此文件，禁止各自重新推导。

**完成判据（双计数）**：① `presence ∈ {actual_only, both}` 的**唯一 `(method, path)` 数** = inventory routes 的唯一 (method, path) 集合大小（重复注册已按上述规则合并并另记 draft，不阻塞）；② 总行数 = 联合主键数（实现 ∪ v3 契约 ∪ v4 契约）——两个数字均在 route-census.md 开头声明；每列非空（可 N/A 但显式）；`route-census.csv` 通过 validate_route_census.py；`expected-keys.csv` 存在且生成脚本可重放（重跑输出逐字节一致）；与 INTERFACE_MAP 差异单独成节；每个 contract_only 行均有对应 draft 发现编号。

### E3 配置普查（config-census.md）

1. 精读 `config.py` 全部字段：名称/默认值/校验/失效行为/文档位置/CHANGELOG 首现版本。
2. `rg -n "OC_SLIMAPI_" src/ tests/ docs/ deploy/ -o | sort -u` 建 env 全集；**四方对账**：Settings 字段 ↔ 代码读取点 ↔ operations.md ↔ deploy/oc-slimapi.service（后者已知含废弃 env 残留，§1.4）。
3. fail-closed 语义逐字段标注：非法值启动即拒 vs 静默回退；契约声称 fail-closed 的（`ACCEPTED_CLIENT_VERSIONS` 钉死 (3,4)、REPLAY 三参数等）逐一验证测试存在。
4. 废弃 env（`OC_SLIMAPI_SERVER_API_VERSION`「warning+忽略」）验证实现与测试；deploy 残留值 `2,2` 在钉死语义下的实际启动行为（推演 + 测试证据，不实机改配置）。

**完成判据**：env 全集表 + 四向差异清单。

### E4 状态机清单（state-machines.md，初版）

逐一形式化（状态集/事件集/转移函数含守卫/终态/不变量/超时/持久化/恢复语义），模块与主题以实读为准，至少覆盖：

1. selector（`selector.py`）；2. GlobalHub（`sse/global_hub.py`：上游订阅生命周期、digest/sticky/G1、sid 表逐出）；3. Subscriber（`sse/hub_types.py`）；4. TokenStreamHub/Registry + flush loop 双账本；5. ReplayLog + replay_wire（epoch/seq/barrier/四类 resync）；6. DbAuxiliarySource（断路器/inode 探测/generation）；7. SingleFlight（plain/leased、grace、三分支取消、关停收敛）；8. TransformPool（admission/absorb）；9. CatalogCache（三预算）；10. TurnRegistry（盘上计数）；11. QpSweepShadow；12. app lifespan（资源编排序）；13. dbaux cursor（`{t,i,f}` 编解码/校验）；14. **catch-all 404 终端边界**（`proxy.py`：selector 405/400 优先链 → 404 的到达条件矩阵，含 WebSocket 501）。

**完成判据**：≥14 张卡片（以实读发现增补为准），每张含状态集+转移+不变量初稿。

### E5 关键场景数据流追踪（dataflows.md）

12 个场景端到端追踪（HTTP 入 → 响应/SSE 帧出，逐步列 file:line 与数据形态变化）：

1. `GET /slimapi/messages/{sid}?v=3`（skeleton+ETag+单飞）
2. `?v=3&mode=merged`（placeholder 填槽+fanout+预算）
3. `GET /slimapi/messages/{sid}/full/{mid}`（single-flight+transform 池+absorb）
4. `GET /slimapi/messages/{sid}/expand/{category}/{mid}[/{partID}]`（§4b 求值序）
5. `GET /slimapi/sessions?v=3` vs `?v=4`（上游+envelope+ETag vs dbaux SQL+降级矩阵）
6. `GET /slimapi/config/providers?v=3` vs `?v=4`（透传 vs §12 投影+限额+指纹 v2）
7. `POST /slimapi/session/{sid}`（v4 POST≡PATCH）与 `.../archive`（octet 缺省判据）
8. `GET /slimapi/events`（v3 vs v4 握手 + id: 序列 + Last-Event-ID 四级分类 + barrier）
9. `GET /slimapi/sessions/{sid}/stream`（token 流 per-sid 序列、tombstone、预算）
10. **catch-all 终局链路**：非 `/slimapi` 路径与 `/slimapi` 未收编路径经 selector（405/400 优先链）→ `proxy.py` 404 `thin_route_not_found` 的完整链路（含 traffic 记账维度 `not_applicable` 的现状、WebSocket 501 分支）——验证「3.0.0 关闭 catch-all」终态的实现与契约 §8 一致性，以及历史 SSE 透传记账面是否随之消亡。
11. `GET /slimapi/metrics` 与 access log/snapshot 写盘链路（RETAIN_DAYS 清理）
12. dbaux 全环：发现→连接→查询→断路→恢复

**完成判据**：12 节，每节含函数级调用序列表。

### E6 文档与部署面精读（docs-notes.md）

1. 逐节精读 v3-contract §0–§12、v4-contract §0–§17+附录（每节 3-8 行：冻结了什么/关键不变量/可测试断言）。
2. 精读 `AGENTS.md`、`docs/release.md`、`docs/operations.md`（重点 §部署姿态/§5.5 去重）、`docs/develop.md`、`docs/specs/CLIENT_CHANGES.md`、`docs/specs/INTERFACE_MAP.md`、`docs/manual/traffic-accounting.md`。
3. **精读 `deploy/oc-slimapi.service` 全文**：与 operations.md、config.py 三方对账（env 名/默认值/废弃状态；已知残留 §1.4）。
4. 扫读 design-* / v2-contract / system-architecture-proposal（若存在）：只提取与现行契约冲突/滞后条目。
5. 建「契约节 ↔ 实现模块 ↔ 测试 ↔ 设计文档」四方索引表。

**完成判据**：两契约每节有摘要；deploy 三方对账表；四方索引表。

### E7 测试普查（test-census.md）

1. 每测试文件一行：行数/test 数/覆盖路由或模块/fixture（upstream_factory/v4_fixture/自建）/双态锁定（v3+v4、feature 开关）/金样驱动。
2. `pytest -q --collect-only` 实际收集数与 inventory 对照。
3. 专项标记：时间敏感（sleep/jitter/epoch 随机/真实时钟）、真实文件系统、真实 sqlite、mock 边界（respx vs MockTransport）。
4. 最大 15 与最小 15 个文件清单。

**完成判据**：inventory `tests.files` 全部成表；时间敏感清单独立。

### E8 上游源码对照（upstream-notes.md）

按附录 B 精读 opencode v1.18.18：每个主题一节「上游事实」（字段/形状/边界行为清单，带 ts file:line）。主题：消息分页+cursor、session handlers（列表/单查/PATCH 双 shape/DELETE 递归/`time.archived`）、event/SSE handlers（`/global/event` 帧形、`session.status` 形状、part 形状）、event groups（事件类型全集）、session 核心/schema（Session 字段全集=v4 §13 parity 真值、`list()` 行为）、config/providers（响应形状真值）。

**规则**：sidecar 任何对上游形状的假设（isinstance/键存在性/枚举值）都必须在此找到出处；找不到出处的假设 = draft 发现。

**完成判据**：≥6 主题事实清单 + 假设↔出处对照表（E1 疑问点在此回填）。

**Phase 1 总完成判据**：E1–E8 九文件齐备；draft 发现已按 §3.2 落文件。

---

## 6. Phase 2 —— 专项审计（A1–A15）

> 每项产出 `03-reports/DNN-*.md` + 关联发现。顺序建议 A1→A4→A2→A3（契约线）→A5–A9（风险线）→A10–A12（质量线）→A13–A15（配套线）。每项开头列「输入依赖」与「完成判据」。**有限全集一律全量对账，禁止抽样**（抽样只允许用于风格性观察且须记录确定性抽样算法与样本/总量）。

### A1 v4 完备性矩阵（D01）

**问题**：v4 面是否覆盖 v3 面全部能力？哪些 v3 行为在 v4 缺失、变形或被有意退役？

**方法**：
1. 以 E2 表为底构建能力矩阵：**行 = 期望键 `(method, path, behavior)`**（behavior 取值同 A12 枚举：`happy_path,error_<code>,v3_face,v4_face,feature_off,boundary`，期望键集合**唯一来源 = `expected-keys.csv`**（E2 生成 / U0 冻结，先于填格声明）），列 = `?v=3` 实际行为 / `?v=4` 实际行为 / 差异性质（等价|增强|变形|退役|缺失）/ 契据（v4-contract §10 差异列）。**全量填充，不抽样**；差异性质判据补充：E2 `presence=contract_only` 的期望键差异性质恒为 `缺失`（对应实际行为格子记 `MISSING`）。
2. 逐格从代码与契约双侧填证。
3. 核对已声明退役面（sessions directory 400、`/events?tokens=1` 400、SSE 握手抑制、resync 值域冻结、非冻结 reason 终结连接）。
4. 核对 v4 新增面实现完整度：readiness 10 features 与 `readiness.py`/`features.py` 同源一致性；`capabilities["4"]` 静态性的测试锁定。
5. 输出三清单：v4 已全覆盖 / 有差距（阻碍 v3 消费方迁移，逐项标阻塞度）/ 有意不覆盖（设计内）。

**完成判据**：矩阵实际键集合与期望键集合**完全相等**（集合差双向为空，校验方法见 A12 的 python csv 校验）；三清单每条有契约+代码双证。

### A2 v3 可淘汰性分析（D02 → `v3-retirement-plan.md`）

**问题**（双口径，§0.3 约束）：

**方法**：
1. **口径 a（迁移评估）**：以 A1 矩阵为基础，从客户端视角列迁移 checklist：消费方（ocdroid@v3）已用的每个 v3 能力 → v4 等价物 → 必改点 → 风险。特别覆盖：envelope 差异（v4 sessions 无 ETag/304）、SSE 重连模型（Last-Event-ID + resync 四值 + 无 snapshot → 必须 HTTP 对齐）、directory 退役（v4 全局面无 per-directory 列表——**重点分析对多工作目录客户端是否能力缺口**）、limit≤500、422/400 新错误码、503 降级处理。oc-webui 已在 v4（H3）——收集其已踩过的坑（CHANGELOG 4.4.0 动因即一例）作为迁移风险实证。
2. **口径 b（拆除成本模型）**：`rg` 定位 v3-only 代码路径（预计热点：selector 双版本表、sessions v3 面、messages envelope v3、SSE v3 握手/resync legacy reason 五路径、ETag `wire=v3` 域等——以实读为准），每项估：涉及文件数/测试数/契约节修订数/客户端破坏面。**明确标注：本口径是成本模型，不是政策建议。**
3. 阻塞项识别：ocdroid 锁定 v3、(3,4) 永久双版本裁决、v3 冻结回归基线的作用（拆除后回归网变薄的风险量化）。
4. 结论模板（固定）：「现状评估 / 若迁移 v4 需要什么 / 若（假设性）退役 v3 需要什么 / 永久双版本维持成本量化（双面测试数、双语义分支的代码行数估计）」。

**完成判据**：双口径成节；v3-only 路径清单每条带 file:line（快照声明）；维持成本有量化数字。

### A3 legacy / 透传遗留审计（D03）

**问题**：还有哪些 legacy 壳、透传路径、死代码、兼容 shim？存在理由是否成立？

**方法**：
1. **proxy.py 终局审计**（现况 51 行，以实读为准）：验证其纯 404 边界性质与契约 §8 的一致性；docstring 声称「已随 forwarder 删除」的各职责（turn-fence S2、shell/PTY deny list、directory 校验、raw-query 转发、SSE 透传记账）在仓内是否确无残余实现；`install_proxy` 挂载点与 selector 优先链（405/400 先于 404）的到达条件矩阵。
2. **shim 清查**：`sse/hub.py`、`sse/token_hub.py`（行数以实读为准）——re-export 表、真实使用者 rg 反查，可否下线。
3. **v2 残余**：`rg -n -i "\bv2\b" src/`（人工过滤误报）：注释/变量名/观测枚举（`selectorResult`、traffic `wireVersion` 取值域）/`X-Slimapi-Version` 不解读的实现/`v2-contract.md` 历史定位标注。
4. 宽容路由（`/slimapi/api/session/active`、`/slimapi/global/health`）语义与匿名消费方关系。
5. passthrough 记账现存来源（traffic bucket 分类逻辑 + catch-all 关闭后的残余面）。
6. `actions.py`/actions_registry 死配置检查；`qp_sweep` 阶段 1 shadow 的存在价值（metrics 暴露 ≠ 消费；阶段 2 是否仍是计划）。

**完成判据**：每项遗留物一行结论（保留理由成立/不成立/建议处置）；proxy 终局链路矩阵完整。

### A4 契约清晰性与完整性（D04）

**方法**：
1. **逐节全量审读** v4-contract §0–§17，每节四问：(i) 可测试断言是否明确（输入→期望可机械判定）？(ii) 错误路径是否穷尽（malformed 每种形态有归宿）？(iii) 与 v3-contract 对应节是否无组合矛盾？(iv) **实现是否照做——每节抽全部关键断言对照代码与测试（禁止只抽 2-3 条；「关键断言」= 该节所有可机械判定句）**。
2. 同法全量审 v3-contract §0–§12（重点 §2 selector、§5+§5.7a、§7 SSE、§11 测试矩阵 ↔ tests/ 实际对应）。
3. **错误码全集三向对账（全量）**：`rg -n "code[\"']\s*[:=]" src/` 抽出实现全部错误码 ↔ v3/v4 契约错误表 ↔ CHANGELOG 提及码：无主码/幽灵码/文档码三类差异全列。
4. **硬不变量清单化（全量）**：从两契约提取全部「恒/绝无/逐字节/恰好」类硬不变量（预计 ≥40 条），每条标注：实现位置 + 测试锁定状态（rg 测试名/断言）。**缺测试锁定的定级走 §3.3 gap 行**（消费方直接使用的面才 P1）。
5. 契约内部一致性：版本双轨、readiness U=10、排序冻结、ETag 指纹域清单（wire=v4 / providers-projection-v2）等跨节引用自洽性。
6. CLIENT_CHANGES.md 时效性。

**完成判据**：两契约逐节四问表（全量）；错误码三向对账表（对 inventory `error_codes` 全集）；硬不变量清单含测试锁定列。

### A5 并发与 singleflight/缓存审计（D05）

**方法**：
1. 精读 `singleflight.py` 全文逐分支：lead 失败 follower 错误信封、三分支取消、shield-join、grace、leased exactly-once 释放（1.5.0 修复回归）、共享 raw bytes（1.5.0 questions/permissions 修复）。
2. `transform.py`：admission 先于上游 GET 的顺序保证；absorb 预算与 503 形状；池满公平性；`max_transforms=1` 默认吞吐分析。
3. `catalog_cache.py`：三预算原子性；并发 miss 风暴（有无 per-key 去重？若无，对照 CHANGELOG「join-first」覆盖范围判定设计 vs 遗漏）。
4. 关停收敛：lifespan 关停序列中各资源的顺序/超时/在途请求与 SSE 订阅者归宿；死锁窗口排查（`rg -n "asyncio\.(Lock|Semaphore|Event|shield)" src/` 全清单画持有图）。
5. 取消语义：`rg -n "CancelledError" src/` 逐点判定；取消误映射为 503 信封外泄给 follower 的可能性。
6. 事件循环阻塞扫描：`rg -n "\.result\(|time\.sleep|sha256|gzip\.|realpath|orjson\.dumps|\.write\(" src/` 逐点人工判定 async 上下文内的同步 IO/CPU 重活与 offload 边界覆盖。

**完成判据**：每项机制+风险判定；锁/信号量持有图；阻塞点清单（含正面确认项）。

### A6 SSE 状态机与正确性深审（D06）

**方法**：
1. **GlobalHub 全文**：上游 SSE 解析帧分类全集（对照 E8——找「上游会发但 sidecar 不处理」的帧）；digest 合并/flush 时序；G1 busy 清 sticky（3.3.1 回归）；`normalize_session_status` 覆盖面（rg 全部消费点）；sid 表逐出与 digest 正确性交互；q/p IMMEDIATE 条件与丢失窗口；allowlist 丢帧对 digest 完整性影响。
2. **TokenStreamHub 全文**（现况 ~2190 行，全文必读）：per-session 预算与 flush 窗口；tombstone 回放；空闲/删除/逐出；events-token 保活双账本一致性；全部 TODO 标记（inventory `todo_markers`）逐个对照 E8 上游真实形状评估实质风险。
3. **replay 体系**：id 语法生成/解析对称性；四级重连分类短路序 vs 契约 §7；barrier 水位原子性（上游断连瞬间并发重连竞态）；环形覆盖（count/bytes/TTL）正确性与被覆盖帧×在途重连的交互；epoch 唯一性（boot nonce 冲突概率）；「窗口内无空洞补发」×「背压溢出帧仍入日志」组合语义。
4. 背压与公平：Subscriber 队列上限、溢出断连恢复路径（v3 resync vs v4 终结）两 hub 一致性；慢客户端头部阻塞。
5. 泄漏审计：断连清理全路径（正常/异常/关停/上游断）；`rg -n "create_task|ensure_future" src/oc_slimapi/sse/` 每个 task 的引用持有与异常吞噬。

**完成判据**：三子系统状态机转移表 + 未定义转移清单 + 泄漏结论。

### A7 dbaux 深审（D07）

**方法**：
1. 只读双保险验证（`mode=ro`+`PRAGMA query_only`，行号实锚）；路径解析链每步；URI 构造注入面（路径含 `?`/`#`/空格的 `file:` URI 转义——rg urllib/quote/as_uri）。
2. 单线程 executor 纪律；线程存活/回收；关停时在途查询归宿。
3. 断路器（滑窗/P99 阈值/half-open/generation）与 metrics `dbaux` 块字段一一对照；断路器打开瞬间在途请求语义。
4. SQL 审计（全量 SELECT）：参数化（`search` LIKE 转义 `%`/`_`/`\` 实现）；`archived`/`parent` 过滤 vs 契约 §4 参数矩阵；排序与 cursor `{t,i,f}` keyset 一致性（同秩 tie-break 封闭性）；schema gate 对上游演进的容忍度。
5. 活库读取一致性：上游 journal 模式（E8 确认 WAL 与否）+ `busy_timeout` 行为；inode 探测 × 文件替换竞态；快照隔离缺失对 v4 翻页一致性的影响（跨查询写入 → 丢/重行分析，`f` 指纹防跨过滤组合但防不住时间旅行——验证并定级）。
6. 敏感信息：DB 路径泄入错误体/日志的逐 except 分支验证。

**完成判据**：只读证据链；SQL 逐条审计表；断路器转移表；活库一致性结论。

### A8 安全审计（D08）——三层入口威胁模型

**威胁模型（固定）**：按 §1.1 三入口分别评估每个攻击面——E-I/E-III（认证由 stunnel/loopback 提供，客户端输入仍不可信）、**E-II（0.0.0.0 明文，依赖 ACL；ACL 不可实机验证 → 该入口下所有结论标「部署边界未验证」）**。上游响应半可信；本地文件系统/DB 半可信。**方法（9 项清单式逐项给结论，每项按入口分层）**：
1. header 注入/请求走私：转发白名单；重复 header/控制字符；CL vs TE 冲突的 httpx 行为；`X-Opencode-Directory` CRLF。
2. directory canonicalization：realpath TOCTOU；相对路径；`..`/尾斜杠；allowlist 未启用默认放行 × E-II 入口的组合风险（标部署边界未验证）。
3. 解码放大：`read_with_cap` 作用在线上字节还是解压后字节？httpx 自动解压 × 413 判定的攻击面；merged/expand/full 的层级 cap 复核。
4. JSON 深度/大小炸弹：orjson 深嵌套/大整数行为（orjson 文档语义 + /tmp/opencode 探针）；transform 池最坏 CPU × 8 MiB cap。
5. DoS 面：SSE 订阅上限（T3 具体值）；replay per-sid 域基数（sid 可否被客户端枚举注册）；cursor/指纹解析成本；expand invalid 桶；singleflight/catalog 键基数与 TTL。
6. 信息泄露：错误体 DB 路径/schema/allowlist（逐 except 验证）；metrics/log 载荷目录名；providers **v3 透传面**对消费方暴露的字段全集（对照上游真值——API key 类字段是否存在；v4 白名单投影的动机反推 v3 面风险论证，按入口分层定级）。
7. SSRF/路径穿越：file 三端点 directory 授权后的路径参数穿越（子树约束在上游还是 sidecar——上游 handler 语义佐证）；上游 URL 全部拼接点清查。
8. secrets 卫生：**全 tracked 文件**扫描（`git ls-files` 全集，非仅 src/）：`git grep -n -i -E 'api[_-]?key|secret|password|token' -- .`（git grep 原生仅匹配 tracked 文件）或等价 python 方案（`git ls-files -z` NUL 分隔逐文件扫描，禁 shell 空白分词与 ARG_MAX 依赖）；归档实际扫描路径数与失败文件数；逐点判定；log 记录敏感 query（search 内容）评估。
9. 依赖面（转 A15 深化，此处只做接口层）：四直接依赖的攻击面接触点清单。

**完成判据**：9 项 × 入口分层结论（安全/风险+定级/部署边界未验证）；每个风险有攻击前置条件与利用路径。

### A9 性能与资源审计（D09）

**方法**：
1. **内存上界表**：每个长生命周期结构一行，值域冻结为 `bounded(<来源：config 值/常量>) | unbounded | measured(<探针数据>) | unknown(<理由>)`——unknown 必须给理由，禁止编造精确值。unbounded 项走 §3.3 定级（先证可增长路径+生产影响）。结构清单：catalog cache、replay 三维、GlobalHub sid 表、token 预算、singleflight 保留 body、transform 队列、access log 缓冲、snapshot 累积器、subscriber 队列（以 E1/E4 实读增补）。
2. 热路径成本：messages 列表（投影+sha256+gzip+ETag）CPU 主成本点；v4 sessions SQL 索引假设（对照 `scripts/eqp_matrix.py` 用途与测试固化情况）；ETag canonical hash 输入规模。
3. 连接与池：httpx limits 配置核对；上游 SSE 长连接 × 普通请求共享池影响；单 worker 事件循环饱和风险点。
4. 延迟预算表：全部人为延迟/超时常量（grace/absorb/busy_timeout/Retry-After/TTL…，rg 常量清单）+ 契约依据 + 合理性评估。
5. 启动/关停时长：lifespan 资源串行成本；smoke test 内容；关停超时总值。

**完成判据**：内存表无空格（四值域）；延迟常量表完整。

### A10 代码质量审计（D10）

**方法**：
1. 重复代码探测（手工三步）：(i) 全部 read-group handler 与 write endpoint 逐个摘「结构指纹」（校验→admission→GET→映射前 5 行模式）——**全量不抽样**；(ii) `_read_passthrough.py` 共享管线与各 handler 自有代码的共享率估算；(iii) `rg -n "def _" src/oc_slimapi/routes/` 私有助手全清单标复用。产出重复热区 Top5+合并方向。
2. **错误处理一致性（全量）**：`rg -n "raise|JSONResponse|HTTPException" src/oc_slimapi/` 全部错误构造点逐点审：错误体形状/日志伴随/no-store/命名风格。数量大时按「构造点类型」分组全审（每组全量），仅风格观察允许抽样并记录抽样算法。
3. 命名与契约对齐漂移（`degraded`/`partial`/`skeleton`/`envelope` 等）。
4. 死代码：E1「被依赖=∅」符号 rg 验证 + A3 shim 清查。
5. inventory `todo_markers` 全部处置评估（对照 E8 判定是否已构成行为错误）。
6. 魔法数清单：`rg -n "[^a-zA-Z_\"']([0-9]{3,})" src/` 过滤未具名常量 vs config 化。
7. 类型注解：核心模块注解密度抽样（确定性算法：按 inventory 字母序每第 3 个模块，记录样本/总量）。

**完成判据**：重复热区表；错误构造点全量分组审表；死代码清单（rg 证据）；TODO 处置结论。

### A11 模块化与可维护性（D11）——数据驱动

**方法**：
1. **分层违规**：E1 依赖图验证单向性；`rg -n "from oc_slimapi.routes|from ..routes|from ..app" src/oc_slimapi/{sse,dbaux} src/oc_slimapi/*.py` 反向依赖清查。
2. **上帝文件候选 = 数据驱动**（禁用固定白名单）：对 inventory 全部 `src_files` 计算——行数、顶层符号数（职责数代理）、内部扇入/扇出（import 边）、可变状态数（E1 卡片）、变更频率（`git log --oneline --since=2026-08-01 --name-only` 聚合）。**入围规则（全部满足任一即入）**：行数 >500；或内部扇出 Top10；或变更频率 Top10。入围文件**每个**必须产出「拆分或保持」论证：职责清单（复用 E1）→ 若拆分：目标文件集（路径+行数估计）+迁移风险（测试锚点稳定性）+收益；若保持：内聚性论证（参照 `singleflight.py`/`hub_types.py` 类先例）。候选池预计含 `tokenstream/hub.py`、`messages.py`、`config.py`、`global_hub.py`、`app.py` 等——以计算结果为准。
3. 耦合度量：`config.py` Settings 扇出与拆分组可行性；`hub_types.py` 公共类型聚合点合理性；`app.state` service locator 隐式契约面（rg 全清单 → DI 化建议可行性）。
4. 变更热点 × 缺陷关联：高频改动文件对照 CHANGELOG 修复条目定位。

**完成判据**：入围文件清单（附指标值）+ 每文件拆分/保持论证；反向依赖清单；app.state 清单。

### A12 测试质量审计（D12）

**方法**：
1. **路由×行为×测试矩阵（全量）**：以 E2 表为行。**CSV schema 冻结**（`04-final/test-gap-matrix.csv`）：
   - **完整 header（固定顺序，8 列）**：`method,path,behavior,v3_test,v4_test,feature_off_test,boundary_test,gap_severity`；**主键 = 前三列构成的三元组 `(method, path, behavior)`**（三列独立存在，不合并为单列）；
   - 转义规则：RFC 4180（含逗号/引号/换行的字段用双引号包裹，内部双引号加倍）；测试引用填 `tests/file.py::test_name` 或 `NONE`；`gap_severity ∈ {P1,P2,P3,NONE}`（按 §3.3 gap 行）；
   - behavior 枚举：`happy_path,error_<code>,v3_face,v4_face,feature_off,boundary`——错误码行以 `expected-keys.csv` 实际包含的 `error_<code>` 行为准（inventory `error_codes` 仅用于校验码合法性，不增行）；
   - **唯一性/合法性校验（python csv 模块，命令见附录 A）**：① 三元组主键无重复；② behavior ∈ 枚举全集；③ 8 列齐全且无空格（`NONE` 显式）；④ 实际行集合 == 期望键集合（期望集合唯一来源 = `expected-keys.csv` 全集，行数在 D12 开头声明）；
   - 校验结果（含期望/实际集合差）存 `04-final/test-gap-matrix.validation.txt`。
2. **断言强度全量审**：A4 硬不变量清单的**全部**锁定测试逐个人工判读（断言锁死行为 vs 只断言不 crash）；重点「逐字节等价」类声明的测试实现方式（exact bytes? subset?）。
3. flaky 风险：E7 时间敏感清单复核（`rg -n "sleep|random|datetime\.now|time\(\)" tests/`）。
4. 金样体系：`tests/golden/` 再生成机制、漂移检测、评审成本；v4_fixture（行数以实读为准）可维护性。
5. 测试反模式：`rg -n "assert True|pytest.skip|xfail" tests/`；mock 过度判定。

**完成判据**：CSV 按 schema 完整且唯一性校验通过；断言强度全量表；flaky 清单。

### A13 可观测性与运维审计（D13）

**方法**：
1. metrics 完备性：`GET /slimapi/metrics` 全字段 × E2 路由表：每路由每类失败可否归因；静默路径清单。
2. access log 实用性：字段全集 vs traffic-accounting.md 口径；`recordType` 过滤陷阱的文档显眼度；RETAIN_DAYS=3 × snapshot retain 30（deploy :45-46）的对账价值。
3. **runbook 审计**：operations.md 对每个 env、每个 503/degraded 场景、断路器恢复、replay 调优、allowlist 运维的动作完备性；**新增：E-II 部署姿态（0.0.0.0+ACL）的运维指引是否成文**；缺失场景清单。
4. **deploy/oc-slimapi.service 对账**：与 operations.md、config.py 三方（E6 已建表，此处裁决漂移清单并逐条定级——已知残留 §1.4）。
5. 告警建议（advisory）：断路器 trips、replay barriers、sweep skips 等阈值建议。

**完成判据**：metrics↔路由覆盖表；runbook 缺口清单；deploy 对账裁决表。

### A14 文档漂移审计（D14）

**方法**：
1. check_routes_doc.py 保障范围外的漂移面：design-v2.md、INTERFACE_MAP 描述列、develop.md/operations.md 命令与 env、CLIENT_CHANGES.md、AGENTS.md 本身。
2. 三向抽查（**全量**）：CHANGELOG 4.2.0–4.4.0 全部 Added/Changed/Removed 条目逐条核对实现与测试存在性。
3. 死链扫描：`rg -n "docs/specs/[a-z0-9-]*\.md" -o docs/ *.md | sort -u` 对照实际存在性。

**完成判据**：漂移条目表；4.2.0–4.4.0 逐条核对表。

### A15 发布与供应链审计（D15，新增）

**范围**：`src/` 之外全部 tracked 可执行/部署/构建资产（以 inventory `tracked_executables` + `git ls-files` 为准）。

**方法**：
1. **资产枚举**：`git ls-files` 全清单分类——脚本（scripts/*）、部署（deploy/*：service/stunnel.conf/actions manifest）、构建（pyproject.toml、build-system 配置）、其他可执行；每项一行职责（复用 E1 卡片）。
2. **`scripts/release.sh` 审读**：失败关闭行为（changelog 校验缺失时是否中止）、版本一致性检查（pyproject ↔ CHANGELOG ↔ tag）、与 docs/release.md 规范的逐条符合性、危险操作清点（tag/commit 的防误触机制）。
3. **`scripts/check.sh` + `check_routes_doc.py` 审读**：检查项全集 vs AGENTS.md 声称；check_routes_doc 的对账覆盖盲区（它查什么、不查什么——A14 依赖此结论）。
4. **`scripts/eqp_matrix.py`**：用途、被测试引用方式、失效风险。
5. **依赖与安装面**：pyproject 四直接依赖 + 三测试依赖的版本窗口；`pip list`（U0.3 存档）中的实际解析版本；`pip check` 结果；**传递依赖清单**（`pip list` 全表即传递闭包快照）；lock/哈希/来源固定机制的有无（预期无 lockfile——记录为供应链缺口并定级）；build backend（setuptools>=75）与包发现配置审读；`pip install -e` 安装路径面。
6. **CVE/ advisories**：无外网查询条件下**只裁决本地可证事项**（版本窗口是否过宽、已知有问题的本地版本）；「未能联网核查 CVE」在报告元数据中列为**覆盖限制**，不得据此给出安全结论。
7. **secrets 全仓扫描结果消费**（A8.8 的 `git ls-files` 全集扫描在此复核并归档）。

**完成判据**：资产枚举表；release/check 脚本审读表（失败关闭结论逐条）；依赖面记录（含传递闭包快照引用 + lock 缺失定级）；覆盖限制声明。

---

## 7. Phase 3 —— 交叉复核与自我证伪

> 产出 `04-final/verification-log.md`。

- [ ] **V0 快照边界重验（每个 Phase 边界执行，含本次；唯一入口 = `<PROBES_NS>/verify_baseline.py`，调用前 hash 校验规则见 §0.1；禁止旁路）**：脚本内部逻辑冻结为——① `git rev-parse HEAD` 与 `git status --porcelain` 采集；② 对 `manifest/file-hash-manifest.txt` 全量 `sha256sum -c`（工作目录=仓库根）；③ **集合双向比对（与 freeze 共享同一份 `collect_present_input_paths()` 归档实现，含相同冻结排除前缀与「仅磁盘存在」过滤）**：重采当前路径集与 `manifest/input-paths.txt` 双向比较——出现清单外新增路径 = 漂移；消失路径仅由墓碑核对：属 `manifest/deleted-paths.txt` 且仍不存在 = 合法，墓碑外消失或墓碑内路径复现 = 漂移；任一不满足 → 判定快照漂移，触发 §0.4 流程。结果（命令 + 退出码 + 双向比对结论）写 `manifest/phase-verify/<seq>.json`（seq = 目录内现有最大序号+1；一条一文件排他创建，中断残留的半成品不影响既有记录），随后原子重建 `manifest/phase-verify/index.json`（读全部记录 → 临时文件 → `os.replace`，旧 index 归档 logs/superseded/），事件同步记 verification-log。
- [ ] **V1 全量复核**：每个 F-NNN 重走证据链：重开 `file:line`（或 `git show <BASELINE_HEAD>:<path>`）确认代码未变、重执行关键 rg、重对照契约原文。结论写回「复核记录」节。
- [ ] **V2 自我证伪（双轨规则，§0.2）**：对每个 P0/P1 发现主动构造反证：(a) rg 该符号全部出现点，搜索遗漏的守卫/配置开关；(b) 检查契约他处是否豁免该情形；(c) 检查测试是否锁定该行为——**测试通过只能证明「实现确实如此」（轨二事实），不能豁免规范违反（轨一）**：若发现属 contract-violation 类且代码+测试一致偏离契约 → 维持 confirmed，注明「测试锁定偏离，修复需同步改测试」；若发现属 defect 类且测试锁定的是契约一致行为 → 说明实现误判，销案（refuted）并记录。
- [ ] **V3 一致性冲突消解**：不同报告对同一事实描述不同时——规范类以轨一权威为准，行为类以可复现实验为准；修正另一方并在 verification-log 记录消解。
- [ ] **V4 基线复跑**：读 `manifest/runtime-mode.json`，按 `check_argv`/`check_root`/`env_template`（经统一执行器展开为本次 env）以 §0.1 冻结执行方式复跑（与 U0.4 同一执行环境），输出存 `logs/check-final.txt`，确认与基线一致（绿→绿 / 红→红无新增失败）；不一致 → `git status --porcelain` 定位审计是否意外污染仓库，如实记录，**禁止擅自修复**。
- [ ] **V5 数字定稿**：全部报告计数以 Phase 3 重跑为准（与 inventory 对照），差异需解释。
- [ ] **V6 覆盖自查**：对照 §11 验收清单逐项打勾；未完成项如实标注（含 BLOCKED 项的降级影响）。

---

## 8. Phase 4 —— 汇总与最终报告

### 8.1 汇总前条件

Phase 1–3 全部单元状态 ∈ {DONE, N/A+理由, BLOCKED+完整降级记录（blockers 节有对应条目）}；全部发现 ∈ {confirmed, refuted, unverified_due_to_blocker}（无 draft / verified 残留；unverified_due_to_blocker 须满足 §3.2 的关联要求且在报告 §0 声明其覆盖影响）。

**机器可读交付物终态二选一**（route-census.csv / expected-keys.csv / test-gap-matrix.csv / 各 validation log 同规则）：`VALID`（通过对应 validator）或 `BLOCKED-STUB`——机器可读占位，**stub schema 分型冻结**：JSON 型（inventory.json / runtime-mode.json 等）= 顶层五键 `status:"BLOCKED"` / `blocker_id` / `missing_inputs[]` / `completed_scope` / `affected_decisions[]`；CSV 型（route-census / expected-keys / test-gap-matrix / applicability）= header `status,blocker_id,missing_inputs,completed_scope,affected_decisions` + 恰一单行（列表值用 `|` 连接，禁裸逗号）；txt 型（validation log）= 单行 `BLOCKED <blocker_id> <missing_inputs>`；生成 BLOCKED-STUB 的单元所辖裁决与结论一律标 `coverage-degraded`（§10.3 传播）。禁止以 draft 或空文件充当完成态。

### 8.2 `04-final/AUDIT-REPORT.md` 固定结构

```markdown
# oc-slimapi v4 全面审计报告（2026-08-20）
## 0. 执行摘要（范围方法引用 / 总体结论 / 发现统计 P0-P3×类别矩阵 + confirmed/refuted 计数 + BLOCKED 单元对覆盖面的影响声明）
## 1. 核心问题裁决（§8.3 七问逐条）
## 2. P0/P1 发现详述（摘要 + F-NNN 指针）
## 3. v4 取代 v3：结论与差距清单（D01/D02）
## 4. v3 退役：双口径结论（v3-retirement-plan.md；口径 b 标注为成本模型）
## 5. legacy/透传遗留处置建议（D03）
## 6. 契约质量：逐节评分与修订建议（D04）
## 7. 工程质量总览：安全/并发/性能/模块化/测试（D05–D12 executive summary；安全节含入口分层与「部署边界未验证」汇总）
## 8. 整改 backlog Top20（refactor-backlog.md）
## 9. 审计过程元数据（BASELINE_HEAD、attempt 沿革、check.sh 首尾结果、耗时、覆盖自查表、覆盖限制声明——含 CVE 未联网核查）
## 10. 附录：产物索引（相对 AUDIT_ROOT）
```

### 8.3 核心问题裁决模板（离散刻度，禁止含糊、禁止越权）

| 问题 | 裁决刻度（固定取值） |
|---|---|
| v4 能否全面取代 v3（供现有 v3 消费方迁移）？ | 已具备 / 基本具备（附残留差距+每项阻塞度）/ 不具备（附差距清单） |
| v3 协议可淘汰性？ | 口径 a 迁移完备度（同上三值）+ 口径 b 拆除成本（低/中/高+量化）。**现行裁决下唯一政策输出：维持 (3,4) 永久双版本**；另列「可启动的机械性迁移前置准备」清单（仅限补测试/补文档/补观测类，不含语义变更） |
| legacy/透传遗留是否清理完毕？ | 已清净 / 有残留（清单+必要性评估）/ 存在无理由残留（清单） |
| 契约是否清晰完整？ | 逐契约每节 1-5 分（锚点：5=断言可测且穷尽且实现一致；3=主要路径清晰但有未归宿输入；1=关键语义依赖实现自证）+ 全局（清晰且完整 / 有缺口清单 / 有矛盾清单） |
| 代码质量/可维护性？ | 每维 A-E 档（A=优秀范例 / B=小瑕疵 / C=有明显债务但可控 / D=债务影响迭代 / E=需立即重构）+ Top 问题清单 |
| 安全性？ | 按入口分层（E-I/E-II/E-III）各自：无高危 / 风险清单（按严重度）；E-II 结论须附「部署边界未验证」标注状态 |
| 状态机健全性？ | 每状态机：健全 / 有未定义转移清单 / 有泄漏或竞态清单 |

### 8.4 backlog 评分（refactor-backlog.md，锚点冻结）

`score = severity_weight × impact ÷ cost`：
- `severity_weight`：P0=10 / P1=6 / P2=3 / P3=1（取该 backlog 关联发现最高severity）；
- `impact` ∈ {3=消费方直接触达面或核心数据路径, 2=内部可靠性/运维, 1=局部}（判定依据须引用 E2 路由行或 ops 场景）；
- `cost` ∈ {1=单文件纯机械, 2=多文件机械或单文件有语义风险, 3=跨面语义变更需契约/测试同步}；
- tie-break：score 相同按 severity 高者先，再按 impact 高者先。
输出排序表 + 依赖关系（必须先行的项）。

---

## 9. 自由探索章程（有边界授权）

完成规定单元后，允许并鼓励围绕以下方向自主深挖（同样只读、同样产出到 `01-explore/exploration-log.md` + 发现文件）：

1. **git 考古**：`git log -p` 抽查 3.3.1（sticky 修复）、4.0.0（双版本）、4.1.0（singleflight 合并）三次大改 diff，验证 CHANGELOG「逐字节不变/等价」声称在 diff 层面的可信度。
2. **边界实验**（/tmp/opencode，不改 tests/）：构造契约未明说的输入（伪造 cursor 指纹、SSE Last-Event-ID 边界值、超长 sid、空目录字符串）观察行为；每个实验记「输入→实际行为→契约是否有归宿」；无归宿 = 契约缺口发现。
3. **对照实验**：同一 mock 上游数据下 `?v=3` vs `?v=4` 响应 diff（messages/sessions/providers/session 单查），归因到契约条款。
4. **反直觉追踪**：任何意外行为追到契约或上游源码锚点为止。
5. **省流目标复盘**：从 traffic-accounting.md 口径评估现有记账能否回答「sidecar 到底省了多少流量」；不能则指出缺什么聚合维度。

**边界**：不修改仓库；不提问；不引入新依赖；实验脚本全部放 probes 活动命名空间（§0.1，逐脚本登记 probes-manifest.json）；每个探索在 exploration-log.md 留「假设→方法→结果→结论」四行记录，无结果也记录（防重复劳动）。

---

## 10. 阻塞处理与终止条件

### 10.1 阻塞规则（逐级降级，永不停止整体）

1. 单元受阻：重试 1 次 → 换等价只读手段（Read 工具 ↔ bash cat；rg ↔ python 遍历）→ 仍失败则记入 exploration-log.md blockers 节（单元号+错误原文+已尝试手段+降级决定），该单元标 BLOCKED，继续下一单元。
2. 工具环境整体故障（pytest 无法运行）：静态审计照常；测试相关单元以代码级证据替代并在报告元数据声明降级。
3. 磁盘/权限异常（含 §0.1 symlink 违例）：AUDIT_ROOT 切换 `/tmp/opencode/audit-fallback/`（同 no-clobber/attempt 规则），README 顶部声明。
4. 快照漂移（V0 检出）：按 §0.4 封存/重跑；无法建立一致快照时——**仅清单内干净的 tracked 文件**允许改用 `git show <BASELINE_HEAD>:<path>` 读取基线内容；**脏 tracked / untracked 文件只允许读取 `/tmp/opencode/baseline-snapshot/` 已验证副本**；副本亦不可用时，依赖该证据的单元 BLOCKED、相关发现转 `unverified_due_to_blocker`；以上降级在报告元数据声明。
5. **禁止的解锁手段**：修改源码/测试/契约/配置/deploy、跳过 check.sh 失败、删除报错文件、对任何端点/服务发起写操作。

### 10.2 终止条件（全部满足才可收尾；路径均相对 AUDIT_ROOT）

- Phase 0–4 全部单元 ∈ {DONE, N/A+理由, BLOCKED+blockers 记录}；
- `02-findings/INDEX.md` 与实际文件数一致，全部发现 ∈ {confirmed, refuted, unverified_due_to_blocker}；
- `04-final/` 五个交付物存在且符合 §8.2/A2/§8.4/A12 schema；
- README.md 进度表完整；`git status --porcelain` 仅显示 AUDIT_ROOT、`/tmp` 产物不影响仓库、本方案文件（`docs/ocmar/plans/2026-08-20-v4-comprehensive-audit.md`）的未跟踪新增（及 H7 记录的既有改动）；
- 基线 check 最终态一致（按 `manifest/runtime-mode.json` 的 `check_argv` / `check_root` / `env_template` 经统一执行器复跑；绿→绿；基线红则红→红无新增失败）。
- **强制机器产物终态二选一（VALID / BLOCKED-STUB，规则见 §8.1）**：`manifest/` 七件（baseline-head.txt / input-paths.txt / deleted-paths.txt / file-hash-manifest.txt / ignored-baseline.txt / runtime-mode.json / namespaces.json）+ `manifest/bootstrap-env.json` 与 `phase-verify/`（index.json + 记录数与实际 Phase 边界数一致）存在且合法（runtime-mode.json 须为 `final` 态，除非 U0.4 BLOCKED 已记录）；`01-explore/route-census.csv` + validation log、`expected-keys.csv`、`inherited-error-applicability.csv` + 其 validation log 均为 VALID 或 BLOCKED-STUB（applicability 与 census 继承列的跨表一致性校验同二态）；`04-final/test-gap-matrix.csv` + `04-final/test-gap-matrix.validation.txt` 为 VALID 或 BLOCKED-STUB；`logs/check-baseline.txt` 非空、`logs/check-final.txt` 非空（V4 BLOCKED 时按 blocker 记录豁免并标注）；`logs/probes/` 归档含**已创建**的全部六个基础脚本副本 + hash（分期创建者以实际创建并登记为准，未达创建期的脚本随所属单元状态说明）。全部 BLOCKED-STUB 必须已传播为 `coverage-degraded` 标注。

### 10.3 BLOCKED 的机械影响（必须在最终报告 §0 声明）

每个 BLOCKED 单元在报告中列出：单元号 → 覆盖面损失（哪些审计问题/验收清单项受影响）→ 受影响结论的置信度降级标注（该范围内结论标 `coverage-degraded`）。不允许无标注地略过。

---

## 11. 验收清单（最终自查，写入报告 §9）

| # | 项 | 判据 |
|---|---|---|
| C1 | 七个核心问题全部有离散刻度裁决 | §8.3 表填满，且无越权输出 |
| C2 | 每个发现有 file:line 证据 + 快照声明 | 抽查 INDEX 前 20 条可回溯 |
| C3 | 负向断言有 rg 证据 | 抽查 10 条「不存在 X」类 |
| C4 | P0/P1 全部经 V2 双轨自我证伪；unverified_due_to_blocker 发现均关联 blockers 条目 | verification-log 覆盖 |
| C5 | A1 矩阵键集合与期望键集合完全相等；E2 BLOCKED 时 `expected-keys.csv` 为合法 BLOCKED-STUB 且 A1 单元状态为 BLOCKED、关联同一 blocker | 集合差双向为空；或 expected-keys.csv STUB schema + A1 单元 blocker 关联一致 |
| C6 | 错误码三向对账全量 | D04 表 vs inventory `error_codes` |
| C7 | 硬不变量清单全量（≥40 或说明实际总数）且带测试锁定列 | D04 |
| C8 | 状态机卡片 ≥14（或说明实际数） | state-machines.md + D06 深化 |
| C9 | A8 九项 × 三入口分层结论（E-II 标注部署边界未验证状态） | D08 |
| C10 | test-gap-matrix.csv 四项校验通过；或 A12 BLOCKED 时为合法 BLOCKED-STUB | validation.txt 四项全过；或 STUB schema + blocker 关联 + coverage-degraded 传播校验通过 |
| C11 | backlog 有锚点冻结评分与排序 | refactor-backlog.md |
| C12 | check.sh 首尾一致 | logs/ 两份输出 |
| C13 | 工作区零污染（AUDIT_ROOT/白名单外无改动，含 ignored 副产物） | git status + manifest 复验 + `git status --ignored --porcelain` 与 `manifest/ignored-baseline.txt` diff（**`src/*.egg-info` 零新增，无豁免**） |
| C14 | CHANGELOG 4.2.0–4.4.0 条目逐条核对 | D14 表 |
| C15 | 全部数字引用可追溯到 inventory 或现场命令 | 抽查 D01–D15 各 3 处 |
| C16 | A15 资产枚举 = git ls-files 可执行/部署/构建全集；覆盖限制（CVE 未联网）已声明 | D15 |

---

## 附录 A：命令速查（审计全程只用这些模式，避免临场发明）

```bash
# 只读 git
git rev-parse HEAD; git status --porcelain; git log --oneline -5
git log --oneline --since=2026-08-01 --name-only
git show <rev> -- <path>; git show <BASELINE_HEAD>:<path>
git ls-files
# 快照 manifest（唯一入口 = 冻结脚本；内部逻辑正文 U0.2 冻结，禁止旁路手工拼管道；调用前先校验脚本 hash 与 AUDIT_ROOT/logs/probes/ 归档一致）
python3 <PROBES_NS>/freeze_baseline.py --repo <REPO_ROOT> --out <AUDIT_ROOT>/manifest
#   产出：input-paths.txt / deleted-paths.txt（删除墓碑）/ file-hash-manifest.txt；脏基线时另产 baseline-snapshot 副本
# 脏基线快照副本（resolve_active_namespace('baseline-snapshot') 惰性启用；活动路径见 manifest/namespaces.json；namespace-manifest.json 全量验证 + COMPLETE 后才算完成）
# ignored 副产物基线（C13 对照）
git status --ignored --porcelain   # 采集后经 python open(...,'x') no-clobber 写入 manifest/ignored-baseline.txt（恢复态已存在则 hash 比对一致跳过）
# V0 边界重验唯一入口（全量三查：hash / 墓碑仍不存在 / 无清单外新文件；工作目录=仓库根）
python3 <PROBES_NS>/verify_baseline.py --repo <REPO_ROOT> --manifest <AUDIT_ROOT>/manifest
# 搜索
rg -n "<pattern>" src/ tests/ docs/ deploy/ scripts/
rg -c "def test_" tests/
rg -n "@router\.(get|post|patch|delete)" src/oc_slimapi/
# 计数
find src -name '*.py' | wc -l; find src -name '*.py' | xargs wc -l | tail -1
# 基线/复跑 check、三检、副本构建与定点 pytest：一律经统一执行器（禁止裸 shell 执行 python/pytest/pip/导入 src）
python3 <PROBES_NS>/run_isolated.py --bootstrap <AUDIT_ROOT>/manifest/bootstrap-env.json -- <argv>          # bootstrap 阶段（三检/venv 构建）
python3 <PROBES_NS>/run_isolated.py --runtime <AUDIT_ROOT>/manifest/runtime-mode.json -- <argv>             # final 态（check 复跑/定点 pytest）
# 展开后 env 形如（由执行器组装，RUN_ID 每次经 run-sequence 原子分配、唯一递增）：
#   PYTHONPYCACHEPREFIX=<RUNTIME_NS>/runs/<RUN_ID>/pycache/   TMPDIR=<RUNTIME_NS>/runs/<RUN_ID>/tmp/
#   PIP_CACHE_DIR=<RUNTIME_NS>/pip-cache/   PYTEST_ADDOPTS="-p no:cacheprovider --basetemp=<RUNTIME_NS>/runs/<RUN_ID>/pytest/"
# cwd=check_root（工作区模式=仓库根；副本模式=<VENV_NS>，活动路径见 manifest/namespaces.json）
# run_isolated 调用规则：--bootstrap / --runtime 二选一显式传入；--runtime 目标文件缺失即非零退出（禁止猜测回落）
# freeze/verify/gen/validate 系脚本为纯 stdlib 文本处理：不导入 src、不执行 pytest/pip，允许系统 python3 直接运行（本附录各命令即此形态）
# <PROBES_NS> = resolve_active_namespace('probes') 返回的活动命名空间路径（§0.1，已持久化 manifest/namespaces.json；调用基础脚本前先校验 hash 与 AUDIT_ROOT/logs/probes/ 归档一致）
# 命名空间创建 = 排他创建（python os.makedirs(..., exist_ok=False) 包裹恢复分支；已存在且校验通过 = 复用），禁止 mkdir -p 直通
# 产物归档复制（强制成功语义）：python shutil.copy2；目标已存在 → hash 比对一致跳过、不一致记 blocker；任何归档失败必须进 blocker，禁止 `|| true` 吞错
# A12/C10 CSV 校验（python csv 模块：主键三元组唯一、behavior 枚举、8 列非空、期望集合相等）
python3 <PROBES_NS>/validate_gap_matrix.py <AUDIT_ROOT>/04-final/test-gap-matrix.csv <AUDIT_ROOT>/01-explore/expected-keys.csv
# E2 期望键集合生成（A1/A12 共用；重跑须逐字节一致）
python3 <PROBES_NS>/gen_expected_keys.py <AUDIT_ROOT>/01-explore/route-census.csv <AUDIT_ROOT>/01-explore/expected-keys.csv
```

## 附录 B：上游 v1.18.18 对照锚点（U0.7 验证存在后使用；行号以实读为准）

| 主题 | 路径（相对 opencode-src/current/） |
|---|---|
| 消息分页+cursor | `packages/opencode/src/session/message-v2.ts` |
| session handlers | `packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`、`packages/server/src/handlers/session.ts` |
| event/SSE handlers | 同结构 `handlers/event.ts`、`packages/server/src/handlers/event.ts` |
| event groups | `packages/opencode/src/server/routes/instance/httpapi/groups/event.ts`、`packages/protocol/src/v1/`（rg 定位 event 类型定义） |
| session 核心 | `packages/core/src/session.ts`（勘误后 `list()` :261-299，以实读为准） |
| session schema | `packages/schema/src/v1/session.ts` |
| providers | rg 定位（`rg -n "providers" packages/server/src/ packages/core/src/ --type ts -l`） |

## 附录 C：事实速查（A 组=已验证可直接引用；用到处仍须 U0.6 复核）

- **A（已验证 2026-08-20，HEAD=0b836e7）**：src 71 文件/26,452 行；`tokenstream/hub.py` 2190 / `routes/messages.py` 1643 / `config.py` 1158 / `proxy.py` 51（纯 404 边界）/ `sse/token_hub.py` 23 / `versioning.py` 44（`SERVER_API_VERSION=4`、`ACCEPTED_CLIENT_VERSIONS=(3,4)`，无 `FALLBACK_API_VERSION`）；deploy service 绑 `0.0.0.0:4097` 且残留 `OC_SLIMAPI_SERVER_API_VERSION=2`、`OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2`；ocdroid@v3、oc-webui@v4；`proxy.py` 自述 catch-all 反代已退役、turn-fence 移至 `write_groups._write_passthrough`。
- **B（契约事实，引用时锚定节号）**：4.2.0 五项修订面（readiness 10 features/providers §12 投影/session parity §13/expand href §14/表示层 §15+method 边界 §16）；4.3.0 POST 等效动作族 + §17 non-goals 收紧；4.4.0 providers limit 恢复（指纹 providers-projection-v2）；v4 SSE id 语法 `g:<epoch>:<seq>`/`t:<sid>:<epoch>:<seq>`、resync 值域冻结四值、v4 不发 snapshot、握手抑制；replay 三维默认 2048 帧/域、65536 KiB、900s；v3 语义冻结 = 回归基线；3.3.1 `normalize_session_status()`。
- **C（默认值——用到时从 config.py 取真值，勿信本附录）**：transform absorb 2.5s、catalog TTL 300s、merged fanout 8/16 fulls/8 MiB、单消息 32 MiB、expand 8 MiB、providers 256/1024/64/8MiB。

## 附录 D：执行节奏建议（非强制，防上下文过载）

- 按 E1 顺序分批读码，每批 ≤10 文件即时写卡片；>500 行文件单独成批。
- 每完成一个 A 项立即写报告骨架再填内容。
- 发现随手落 F-NNN，不依赖记忆。
- 全程不向任何人类确认；唯一对话对象是文件系统与只读命令。

## 附录 E：已接受的残余风险与人工核验点（rev9 定稿声明，owner 裁决 2026-08-20）

> 以下风险经 owner 裁决**接受**，不构成阻塞，**执行者不得为消除它们而扩展本方案范围或停下等待**；每项附「成果里易于核验」的验收侧检查点，供人事后抽查。

| # | 已接受的风险 | 为什么可接受 | 事后核验点（在交付物中） |
|---|---|---|---|
| E1 | 继承错误适用表初始五族可能漏族（以 E2 实读 selector.py 等补全为准） | 漏族只会少列期望键，D04 报告会显式声明覆盖范围 | `01-explore/inherited-error-applicability.csv` 行数 > 0 且 `evidence_ref` 列非空抽查 5 行 |
| E2 | 契约侧路由/错误码提取自契约散文，含人工判定成分 | E2 双计数 + contract_only draft 使漏项在 census 开头数字上可见 | route-census.md 开头双计数声明 + INDEX 中 contract 类 draft 数 |
| E3 | runtime-cache 旧 run 目录只读封存不清理，磁盘单调增长 | 只影响磁盘占用，不影响正确性；attempt 结束后随 /tmp 清理 | 无需核验（接受） |
| E4 | BLOCKED-STUB → coverage-degraded 传播依赖执行者纪律 | §10.2 有机械验收句，漏标会在终止检查暴露 | 报告 §0 的 BLOCKED 清单 vs 全文 `coverage-degraded` 标注数一致 |
| E5 | venv-source 排除 glob 若遇 pip 新写入文件类型，恢复复验会误判不完整并走 recovery（自愈但重装一次） | 自愈路径已冻结，代价是一次重建 | `logs/` 中 recovery 事件次数 ≤ 2 |
| E6 | 深度边界（如 SQLite 并发语义、SSE 背压极限值）只做静态推演，不做压力实测 | 实测超出只读审计边界（需起真实上游），A5/A6 已要求以代码证据+契据定级 | D05/D06 每条结论带 file:line 或契据锚点 |
