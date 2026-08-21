# v4 selector 双版本结构设计（design-v4-selector）

> **状态**：B0-2 设计文档（2026-08-17，B0 规范先行批次）。落地于 B3a-A（selector 双版本结构性改造，独立 rev gate）。
> **依据**：`docs/system-architecture-proposal-2026-08-17.md`（v2.2）行 244-249（版本轨道）、行 254 §2（v4-contract 双版本章节要求）、行 66/136（v4 sessions directory 拒绝）；`docs/specs/v3-contract.md` §2/§8.3（现行选择器状态机与优先级链）；`src/oc_slimapi/selector.py` 代码事实。
> **纪律**：本设计不新增决策，全部裁决可回溯 v2.2 行号；与 v4-contract §2（wire 状态表）配套——本文件面向实现（B3a-A），契约面向消费者。
> **写域**：B0 批产出；实现改动（selector.py/versioning.py/routes）属 B3a-A，本文件只冻结设计。

---

## 0. 目标与范围

v2.2 行 245 定案 sidecar 4.0.0 = wire `(3,4)` 双版本：`?v=3` 语义逐字节保持现状，`?v=4` 启用全局门面（DB 投影源 sessions / SSE id: 重放 / directory 退役）。selector 是双版本的**分派点**——本设计冻结：

1. 版本域常量从单值 `(3,3)` 扩为 `(3,4)`（§1/§2）；
2. request-scope `wireVersion` 的存储与读取（§2.2）；
3. directory 消费集的版本分叉——v4 全局 sessions 列表退出消费集（§2.3/§2.4）；
4. 跨版本错误优先级真值表（§3，S-B04 冻结项，同步进 v4-contract §8）；
5. 观测维度扩展（§4）。

不在范围：v4 sessions 数据面（DB 投影/降级矩阵/cursor——见 `design-v4-dbaux.md`）、SSE 重放（见 `design-v4-sse-replay.md`）、versions/health 双视图载荷（B3a-A3，契约 §3）。

---

## 1. 现状锚点（代码事实，3.x 单版本终态）

| 锚点 | 现状 | 位置 |
|---|---|---|
| 版本域 | `ACCEPTED_CLIENT_VERSIONS = (3, 3)`（min,max 对，config.validate fail-closed 钉死） | `src/oc_slimapi/versioning.py` |
| selector 单版本常量 | `SUPPORTED_WIRE_VERSION = ACCEPTED_CLIENT_VERSIONS[1]`（恒 3）；`unsupported_version` 响应 `supported:[3]` | `selector.py:110`、`:427` |
| wire 视图 | `wire_view_from_scope()` 恒返 3（v3-only 终态；selector 已在上游拒绝一切非 v3） | `selector.py:229-238` |
| v 值判定 | 词法 `^[1-9][0-9]*$`（`selector.py:103`）；无 v / v∉支持集 → 400 `unsupported_version`（`:385-406`）；词法非法或多值不同 → 400 `invalid_version_selector`（`:392-399`）；多值**同值**宽容折叠 | `selector.py:385-406` |
| directory 消费集 | `_DIRECTORY_CONSUMING_PATTERNS` 28 条正则（messages×5 / sessions×6 / agent / command / 读组×10 / 写组×5） | `selector.py:116-161` |
| directory 消费逻辑 | `_consume_v3_directory()`：①多值不同→`invalid_directory_selector` ②query+header 归一化不同→`directory_conflict` ③header 出现（header-only/双呈同值）→`directory_header_retired` ④query 单值→校验+stash（`V3_DIRECTORY_STATE_KEY`）+剥离；stream 路由 case④为 no-op | `selector.py:432-497` |
| versions 豁免 | `/slimapi/versions` 非 GET → 405+`Allow: GET`（优先于一切）；GET 无条件豁免 selector | `selector.py:367-383` |
| 观测枚举 | `selectorResult`: `v3|rejected|exempt|not_applicable`（`absent`/`v2` 历史值保留不产出）；`wireVersion`: "3"|None | `selector.py:77-85`、`:48-54` |
| 优先级链 | 405 versions → selector 400（version 族）→ directory 400（directory 族）→ 路由（404/422 等） | v3-contract §8.3 |

---

## 2. 目标状态：(3,4) 双版本

### 2.1 版本域常量（B3a-A1 落地）

- `ACCEPTED_CLIENT_VERSIONS: (3, 3) → (3, 4)`（v2.2 行 245）。
- `SERVER_API_VERSION` 单一常量 → **按请求视图取值**：v3 视图=3、v4 视图=4、同源同值、禁止错配组合（v3-contract §3a 双视图冻结先例；S-B04）。
- **`Settings.server_api_version` env 影响力废除（S-B04 config 迁移）**：`OC_SLIMAPI_SERVER_API_VERSION`（config.py:272-278 读取 + :562-588 校验）双版本期不再影响视图——单值 config 无法表达双视图，保留 env 只会制造 3/4 错配风险；设置时启动 warning 忽略。`config.validate` fail-closed 钉死语义不变（env 不可放宽/收窄 accepted 区间）。
- `/slimapi/versions`：`available:[3,4]`、`current: 4`（S-B04 冻结——(3,4) 期 current 恒为最新主版本）。

### 2.2 request-scope wireVersion

- selector 判定 v 合法后，将 `wireVersion`（"3"|"4"）写入 `scope["state"]`（既有 `SELECTOR_STATE_KEY` 载荷的 `wire` 字段即为此值——现状 v3 路径已写 "3"，`selector.py:409`；v4 路径对称写 "4"）。
- `wire_view_from_scope()`（`selector.py:229-238`）改为：读 `scope["state"]` 中 selector stash 的 `wire` 值；**无 stash（测试直调路由）缺省返 3**（v3 视图缺省 = 向后兼容：现有全部测试与直调路径零改动；v4 路由代码显式断言 wire==4）。缺省值选 3 而非 4 的理由：v4 是新表面，任何未走 selector 的既有调用者语义都是 v3；v4 语义只能经 selector 显式进入（fail-safe：v4 能力不会被旁路意外激活）。
- v3 视图与 v4 视图**同源同值**：同一次请求内 health 的 `schema.version`/`server.api_version`、selector stash、路由分派三者读同一 scope 值（S-B04 禁止错配组合）。

### 2.3 directory 消费集版本分叉

v4 从消费集**仅移除一条**：`^/slimapi/sessions$`（全局会话列表，v2.2 行 66/136——v4 该路由零 directory 参数，数据源为 DB 投影/全局降级）。

- **不移除**（明确列出，防扩大化）：
  - `^/slimapi/sessions/status$`——status 本就全局（上游返回全局内存 map，v2.2 行 24），但路由仍转发上游，directory 仍是其合法路由输入（v4 语义未变，消费保留）；
  - `^/slimapi/sessions/[^/]+/todo|children|diff|stream$`——per-session 路由，上游 InstanceContext 仍按 directory 定向，消费保留；
  - 其余全部（messages/agent/command/读组/写组）——v4 无新语义，消费保留。
- 实现形态：`_is_directory_consuming(path)` → `_directory_consuming_for(path, wire_version)`；v4 集 = v3 集 − {`^/slimapi/sessions$`}。**集合差分而非重复定义**（两集共享同一 pattern 元组源，v4 差分表独立常量，防两表漂移）。
- 机制含义（对照 v3 行为逐 case）：

| v4 请求 `GET /slimapi/sessions` 携带 directory | v3 同请求 | v4 行为 |
|---|---|---|
| query 单值 | 消费+剥离+stash | **400 `directory_retired_in_v4`** |
| query 多值不同 | 400 `invalid_directory_selector` | **400 `directory_retired_in_v4`**（退役错误优先于多值校验——参数本身已非法，无需再判多值） |
| header 出现（任何形式） | 400 `directory_header_retired`（消费集内） | **400 `directory_retired_in_v4`**（同上：v4 首要事实是"该路由不吃 directory"，header 退役错误是 v3 消费集语义） |
| query+header 混合 | `directory_conflict` / `directory_header_retired` | **400 `directory_retired_in_v4`**（同上） |

  设计论证：v4 该路由的 directory 语义是**整体退役**而非**形式规范化**——四种形态收敛为单一错误码，客户端无需区分形式即可定位（ocdroid DirectoryHeaderInterceptor 豁免适配的靶点唯一，v2.2 行 66）。错误优先级上 `directory_retired_in_v4` 是 directory 族在 v4 sessions 路由上的**整体替换**（不是追加）。

### 2.4 错误码定义（进 v4-contract §8）

```json
// 400
{"code": "directory_retired_in_v4",
 "hint": "v4 sessions is a global facade; remove the directory parameter (and the X-Opencode-Directory header). Token/per-session routes still accept ?directory=."}
```

- 拦截层：**selector 层**（dispatch），先于路由（refactor-plan B3a-B4 冻结）；不泄露目录存在性（与 403 族统一错误体原则一致，v2.2 行 188）。

---

## 3. 跨版本错误优先级真值表（S-B04 冻结，进 v4-contract §8）

优先级总链（v4 期，自 `v3-contract §8.3` 扩展）：

```
① 405 /slimapi/versions 非 GET（豁免路由方法错误，优先于一切）
② selector 400：version 族（invalid_version_selector / unsupported_version[supported:[3,4]]）
③ selector 400：directory 族（v4 sessions → directory_retired_in_v4；
   其余消费集路由 → 既有 v3 三码）
④ 路由 422：参数版本不匹配（v4 收 roots/start；v3 收 archived/parent/cursor）
⑤ 路由 400：invalid_cursor（cursor 语法/指纹——校验先于降级判定）
⑥ 路由 503：auxiliary_unavailable（降级矩阵末端）
⑦ 路由 404 / 其余
```

逐组合裁决（S-B04 四组 + 补充）：

| 组合 | 裁决 | 理由 |
|---|---|---|
| malformed cursor vs auxiliary unavailable | **400 `invalid_cursor` 优先** | cursor 语法是请求自身缺陷，与 DB 状态无关；先校验语法再判降级——即使 DB 恢复，该请求也永远非法 |
| 指纹不匹配 vs 熔断 | **400 `invalid_cursor` 优先**（指纹校验在查询前） | 同上：指纹比对是纯内存计算，不依赖 DB 可用性；503 属"能力暂缺"，400 属"请求永久非法"，永久判定优先 |
| `directory_retired_in_v4` vs 参数错误（roots/start） | **400 directory 族优先**（selector 层 ③ 先于路由层 ④） | selector 在 dispatch 层，天然先于路由参数绑定；与 v3 期 directory 400 先于路由错误的既有次序一致 |
| repeated v vs 路由错误 | **多值同值折叠 → 正常路由**（不因重复而 400） | 沿袭 v3 §2 冻结语义（`selector.py:392` `len(set(values)) != 1` 判定）；重复合法值不是错误 |
| v=4 + directory 于**非** sessions-list 路由 | 正常消费（v3 语义不变） | 消费集分叉仅一条（§2.3） |
| v=3 + archived/parent/cursor 于 sessions | **422**（未知参数，显式声明不依赖 FastAPI 默认忽略） | v2.2 行 136；与 v4 收 roots/start 对称，同归"参数版本不匹配"族 |
| 无 v（任何 /slimapi 路由） | 400 `unsupported_version` `supported:[3,4]` | v3-only 终态行为扩展 supported 集；端点存在不 404（v3 §2 退役后语义延续） |

---

## 4. 观测扩展（selectorResult=v4）

- `selectorResult` 枚举增 `v4`（`SELECTOR_V4 = "v4"`；历史值 absent/v2 保留不产出的口径不变）——产出条件与 v3 对称：`?v=4` 词法合法且 ∈ 支持集。
- `wireVersion` 维度增 "4"（access log / traffic snapshot / SSE active 维度 `SSE_RESULT_DIMS` 同步扩 `"v4"`）。
- 兼容性：维度取值集扩大，schema 形状不变——旧快照/旧日志行可继续解读（新值仅在新请求出现）。v3 流量退役判据（P4，v2.2 行 248）以 `wireVersion` 维度聚合：v3 归零 + SSE active 无 v3 连接 → 4.8.0 (4,4)。

---

## 5. 落地映射与测试清单（B3a-A）

| 设计节 | 落地任务 | 测试面（B3a-A 验收） |
|---|---|---|
| §2.1 版本域 | B3a-A1（versioning.py + config 迁移） | 版本门启动校验；旧 env `OC_SLIMAPI_SERVER_API_VERSION` 设置 → warning 忽略路径；config.validate fail-closed 保持 |
| §2.2 wireVersion scope | B3a-A1/A2 | 直调缺省返 3；v4 经 selector stash 读 4；health 双视图同源同值 |
| §2.3 消费集分叉 | B3a-A2 | v4 sessions × directory 四形态 → 全部 `directory_retired_in_v4`；v3 sessions × directory → 现状三码不变；v4 非 sessions-list 路由 × directory → 正常消费 |
| §3 真值表 | B3a-A2（+B3a-B4 路由侧） | §3 表逐组合参数化断言（7 组合 × v3/v4） |
| §4 观测 | B3a-A4 | access log selectorResult=v4 / wireVersion="4" 断言 |

既有 selector 测试全绿为回归基线（v3 语义逐字节不变 = B3a-A 前置验收）。

---

## 6. 开放问题

无（本设计范围内无新增待裁决项；S-B04 语义已由 refactor-plan §8.2 存档冻结）。

---

*（完）B0-2 产出；wire 可见状态表见 `docs/specs/v4-contract.md` §2，与本文件 §2-§3 同源。*
