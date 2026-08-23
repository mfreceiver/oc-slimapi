# oc-slimapi 发版规范

> **本文件是本仓库发版流程的权威说明。**  
> 借鉴 ocdroid（`/home/mar/personal_projects/ocdroid`）的 `scripts/release.sh` + `.opencode/policies/versioning.md` 模式，按 **Python sidecar** 特点适配。  
> Agent 入口索引见根目录 [`AGENTS.md`](../AGENTS.md)。

---

## 0. 与 ocdroid 的对照（原则借鉴，实现不同）

| 维度 | ocdroid（参考） | oc-slimapi（本仓库） |
|---|---|---|
| 产物 | 签名 APK + Gitea Release | **git annotated tag** + 本仓 `CHANGELOG.md`（无 APK） |
| 版本来源 | **纯 git 派生**（`versionName`/`versionCode` 不写文件） | **semver 写在** `pyproject.toml` **且** 打 git tag `vX.Y.Z`（Python 包惯例） |
| 发版入口 | `./scripts/release.sh <patch\|minor\|major>` | **同名同用法** `./scripts/release.sh <patch\|minor\|major>` |
| 质量门禁 | `./scripts/check.sh`（compile + unit） | `./scripts/check.sh`（`pytest tests/`） |
| Changelog | 发版时从 conventional commits **生成**到 `APK/*.md`（无根 CHANGELOG） | **维护根目录 [`CHANGELOG.md`](../CHANGELOG.md)**（接口行为，给 ocdroid）；发版脚本要求目标版本节已存在 |
| 对外发布 | 人工 `git push` + `upload-release.sh`（Gitea API 传 APK） | 人工 `git push origin main && git push origin vX.Y.Z`（可选：Gitea Release notes 贴 CHANGELOG 节） |
| Wire 协议版本 | N/A（客户端） | **独立轨道**：`?v=` selector + `GET /slimapi/versions` 发现（破坏性才 bump，见契约 §1） |

**保留的 ocdroid 纪律**：

- 发版必须在 **`main`**、工作区已跟踪文件干净。
- 里程碑发版只走 **`release.sh`**，禁止随手 `git tag`。
- 脚本打印 push 命令，**不自动 push**（人工确认）。
- 质量门禁失败则中止发版。

---

## 1. 版本语义

### 1.1 包版本（semver / git tag）

| 类型 | 变化 | 何时使用 |
|---|---|---|
| `patch` | `0.1.0 → 0.1.1` | Bug 修复、内部重构、测试/文档、无客户端行为变化 |
| `minor` | `0.1.0 → 0.2.0` | **加性** wire 能力（新可选字段/新端点、旧客户端可忽略）；以及经 owner 批准、wire 大版本不变但需消费方同步承接的行为修订；版本窗收窄先例为 4.8.0 `(3,4)→(4,4)` |
| `major` | `0.1.0 → 1.0.0` | **与 wire 协议版本绑定**（owner 决策 2026-08-17）：仅当 wire `ACCEPTED_CLIENT_VERSIONS` bump（协议大版本升级）时使用；减性/破坏性 wire 变更若不 bump wire 协议版本，不发 major |

Tag 格式：**`v` + semver**（例：`v0.1.0`），与 ocdroid 一致。

### 1.2 Wire API 版本（`?v=` selector + 发现端点）

- 版本协商 = **`?v=` selector** + **`GET /slimapi/versions`** 发现端点；请求头通道已于 3.0.0 删除（出现不解读）。
- 当前接受区间：见 `src/oc_slimapi/versioning.py`（`ACCEPTED_CLIENT_VERSIONS`，现为 (4,4)）与 `docs/specs/v4-contract.md` §0（权威契约，4.8.0 起 (4,4) v4-only 单版本窗口；`v3-contract.md` §1/§2 为 ≤4.7.0 历史存档）。
- **仅破坏性**变更 bump；加性变更 **同版本**。
- Bump 时必须同步：`versioning.py`、`docs/specs/v4-contract.md`（v4-only 窗下版本窗相关变更仅触及 v4 契约；`v3-contract.md` 已为历史存档，不再同步修订）、`CHANGELOG.md`（写明客户端必改点）。

### 1.3 历史：双版本期（wire (3,4)）

> 本节仅解释 4.0.0–4.7.0 的发版历史；当前 `(4,4)` 不按本节做协商。

4.0.0（P3）起 sidecar 曾进入 wire **双版本期**（路线见 `docs/system-architecture-proposal-2026-08-17.md` §7）：

- `GET /slimapi/versions` 曾报 `available: [3, 4]`、`current: 4`（4.0.0–4.7.0 v3/v4 并存；4.8.0 起收窄为 `available: [4]`）。
- **major 与 wire 协议版本绑定铁律不变**：wire `ACCEPTED_CLIENT_VERSIONS` bump（协议大版本升级）才发 major。
- accepted-range 收窄 `(3,4) → (4,4)`（v3 退役）按 owner 2026-08-21 裁定发 **minor（4.8.0 先例）**——收窄不 bump 协议大版本（4 系窗口内变化），major 只跟协议大版本走。

---

## 2. 发版前清单（人工 / agent）

1. **行为变更**是否已写入 `CHANGELOG.md` 的 `[Unreleased]` 或目标版本节？  
   - 路径、状态码、头字段、SSE 帧、错误 `code`、gzip/SSE 行为、资源限制默认值。
2. 若破坏性：契约 + wire 版本协商（`src/oc_slimapi/versioning.py` / `docs/specs/v4-contract.md` §0）是否已按 §1.2 处理？
3. `main` 已包含全部要发的提交；本地 `./scripts/check.sh` 绿。
4. （可选）对照 ocdroid `docs/slim-mode-api-routing.md`：客户端文档是否需同步（由 ocdroid 仓维护；本仓以 CHANGELOG 通知）。
5. **directory allowlist 部署状态确认（[3.3.0] 起，B4-4b 联合门槛）**：发版时记录生产环境 `OC_SLIMAPI_DIRECTORY_ALLOWLIST` 三态结论——
   - **未配置**（默认）：零行为变化，无客户端联动要求，直接放行；
   - **显式空**（机制启用，`/slimapi/file/**` 全 403）或 **非空**（子树过滤 + SSE 帧过滤）：**前置条件** = 确认 ocdroid 已适配 `/file` 403 `directory_not_allowed` 语义或不依赖这些端点（对照当时 ocdroid 版本的 `docs/slim-mode-api-routing.md` / CHANGELOG 回执），未确认前**不得**在生产启用该 env（机制为部署事项：sidecar 默认不启用，启用与否由运维按本门槛决定）。
6. **deploy 模板 env 对账**：`deploy/oc-slimapi.service` 的 env 集 ⊆ `src/oc_slimapi/config.py` 读取 env 集，且值合法（示例值不得与 `config.validate()` fail-closed 规则冲突——版本窗等已钉死项不得出现在模板）。

### 2.1 历史：P3 major（4.0.0）前置 checklist（n5）

> 已完成的 4.0.0 发布记录，不是当前 release checklist。

给 **major（P3，4.0.0）** 的发布前置门槛（在 §2 通用清单之上追加；冻结点见 `docs/refactor-plans/slimapi-refactor-plan.md` §4.2）：

- [ ] ocdroid **B5a 兼容版已发布**（capabilities["4"] 探测 + v3 回退）
- [ ] webui **B5a 兼容版已发布**

→ 消费者兼容版就绪后**方可执行 sidecar major**；B3a 不得早于消费者兼容版。

---

## 3. 发版步骤（规范）

### 3.0 首次 tag 引导（仅一次）

仓库首次只有 commit、尚无任何 `v*` tag 时：若当前 `pyproject.toml` 已是 `0.1.0` 且 `CHANGELOG.md` 已有 `## [0.1.0]`，可**一次性**手动：

```bash
./scripts/check.sh
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin main && git push origin v0.1.0
```

之后里程碑发版一律走 `release.sh`（禁止再随手 tag）。

### 3.1 唯一入口

```bash
# 工作区：仓库根
./scripts/check.sh                  # 可先单独跑
./scripts/release.sh patch          # 或 minor | major
```

### 3.2 `release.sh` 应完成的事（实现约定）

1. 校验当前分支 == `main`。
2. 校验已跟踪文件工作区干净（允许 untracked，如本地 secret 路径备忘）。
3. 跑 `./scripts/check.sh`。
4. 读 `pyproject.toml` 当前 `version`，按 patch|minor|major 推算下一版本 `X.Y.Z`。
5. **要求** `CHANGELOG.md` 中存在 `## [X.Y.Z]` 节（或把 `[Unreleased]` 在发版说明里要求人工先折叠进去——脚本应失败并提示若缺失目标版本标题）。
6. 写回 `pyproject.toml` 的 `version = "X.Y.Z"`。
7. `git add pyproject.toml CHANGELOG.md`（及本次发版必要的契约文件，若有）并 **commit**：`release: vX.Y.Z`（conventional）。
8. 创建 **annotated tag** `vX.Y.Z`（注释可用 CHANGELOG 该节摘要）。
9. **打印**人工执行命令（不自动 push）：

```bash
git push origin main && git push origin vX.Y.Z
```

### 3.3 发版后

1. **push**（脚本不自动 push）：
   ```bash
   git push origin main && git push origin vX.Y.Z
   ```
2. **Gitea Release**（可选，无 APK）：在 `https://git.vectory.cn:18443/mfreceiver/oc-slimapi` 为 tag 建 Release，body 贴 `CHANGELOG.md` 对应节。CLI 示例：
   ```bash
   # 从 CHANGELOG 抽出 ## [X.Y.Z] 节到临时文件后：
   tea releases create vX.Y.Z \
     --title "vX.Y.Z — <一句话摘要>" \
     --note-file /tmp/oc-slimapi-vX.Y.Z-notes.md \
     --repo mfreceiver/oc-slimapi
   ```
3. **本机 / 生产部署（editable install 必做 reinstall）**：
   ```bash
   git pull
   .venv/bin/pip install -e '.[test]'   # 刷新 dist-info；否则 health.sidecar.version 仍报旧版
systemctl --user restart oc-slimapi
   curl -s 'http://127.0.0.1:4097/slimapi/health?v=4'
   # 期望 sidecar.version == X.Y.Z
   ```
   原因：`__version__` 读自已安装包的 dist-info（`importlib.metadata`），不是运行时读 `pyproject.toml`。详见 [`operations.md`](operations.md) §2 / §4。
4. 通知 ocdroid：指向本仓 `CHANGELOG.md` 该版本；若路径/头/错误码有变，同步改 ocdroid 对接代码与 `docs/slim-mode-api-routing.md`。
5. 打开新的 `## [Unreleased]` 空节（若 release 脚本未自动加）。

### 3.4 同版本族热修（不打新 tag 时）

- 仅文档/测试/无 wire 变化：正常 commit 即可，不必 `release.sh`。
- 有 wire 行为修复：必须走至少 **patch** 发版，并写 CHANGELOG（ocdroid 需要可引用的版本锚点）。

---

## 4. 质量门禁（`scripts/check.sh`）

最小集合（当前）：

```bash
.venv/bin/python -m pytest tests/ -q
```

可选扩展（后续）：`compileall`、ruff/mypy、安装包可导入检查。  
**默认门禁失败 → 禁止 tag。**

---

## 5. Changelog 与 ocdroid 的使用方式

| 读者 | 用法 |
|---|---|
| ocdroid 开发 | 每个 slimapi 发版后读 `CHANGELOG.md` 新节，对照改客户端；路径/头以契约 + CHANGELOG 为准 |
| oc-slimapi 开发 | 改 wire 前先想好 CHANGELOG 条目；发版前节必须齐 |
| Agent | 禁止发版不写 CHANGELOG；破坏性变更禁止只改代码不改契约 |

**不要**依赖 conventional commit 自动生成作为唯一记录：本项目 **接口语义**（如 `(info.time.updated or info.time.created) >= ts`、`archived` 为 epoch ms）必须用人工可读的行为描述，自动生成仅可作辅助。

---

## 6. 禁止事项

- 禁止在非 `main` 上 `release.sh`。
- 禁止跳过版本号（如 `0.1.0 → 0.1.2`）除非用户明确要求。
- 禁止复用已 push 的 tag。
- 禁止把 secret、证书、本机绝对密钥路径提交进仓。
- 禁止只 bump `pyproject.toml` 而不打 tag / 不写 CHANGELOG（拆开发版状态）。

---

## 7. 文件职责一览

| 文件 | 职责 |
|---|---|
| [`AGENTS.md`](../AGENTS.md) | Agent 入口索引；引用本文件与 CHANGELOG |
| [`docs/release.md`](release.md) | **本文件**：发版规范 |
| [`CHANGELOG.md`](../CHANGELOG.md) | 接口行为变更记录 |
| [`scripts/check.sh`](../scripts/check.sh) | 质量门禁 |
| [`scripts/release.sh`](../scripts/release.sh) | 发版唯一入口 |
| [`pyproject.toml`](../pyproject.toml) | 包版本号源 |
| [`docs/specs/v3-contract.md`](specs/v3-contract.md) | ≤4.7.0 历史 wire 契约存档；`v2-contract.md` 为 ≤2.x 历史存档，不再作为现行规范源 |
| [`docs/specs/v4-contract.md`](specs/v4-contract.md) | **现行 wire 契约权威**（4.8.0 起 v4-only 自包含；版本窗相关变更必同步） |
| `src/oc_slimapi/versioning.py` | Wire API 接受区间 |

---

## 8. 参考（ocdroid，只读对照）

| ocdroid 路径 | 用途 |
|---|---|
| `AGENTS.md` | Agent 入口索引模式（本仓仿此结构） |
| `scripts/release.sh` | 发版脚本骨架（分支/干净/门禁/semver/tag/不自动 push） |
| `scripts/check.sh` | 改动校验入口 |
| `.opencode/policies/versioning.md` | 版本纪律与禁止项 |
| `docs/build-apk.md` | 发版细节长文（本仓无 APK，仅借鉴结构） |
| `scripts/upload-release.sh` | Gitea 上传（本仓可选；无 APK 时多半不用） |
