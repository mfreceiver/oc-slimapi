# ocdroid 集成 `GET /slimapi/directories`（全局 directory catalog）— 交接提示词

> 将下方「提示词正文」整段交给 ocdroid 侧 agent/开发者。
> 本端点 oc-slimapi 侧**已实现并已发版 v1.2.0**（本机服务已替换运行中，实测 200 + 真实数据通过）。
> 权威契约：[`../../specs/v2-contract.md`](../../specs/v2-contract.md) §2 + §2「`/slimapi/directories` envelope」。
> 客户端配套说明：[`../../specs/CLIENT_CHANGES.md`](../../specs/CLIENT_CHANGES.md)「全局 directory catalog」小节（含降级方案）。

---

## 端点一句话

`GET /slimapi/directories` 列出 opencode 已知的工作目录（directory），供客户端渲染"项目切换器"。**加性新增，wire 版本不 bump（仍 `X-Slimapi-Version: 2`）**。已在 oc-slimapi v1.2.0 出货。

---

## 提示词正文

```text
请为 ocdroid 集成 oc-slimapi v1.2.0 新增的 GET /slimapi/directories 端点（全局 directory catalog，用于"项目切换器"UI）。本端点 sidecar 侧已实现并已部署，下面是接口用法、字段语义、降级方案与集成约束。先只读分析，确认理解后再改代码。

== 一、接口用法（slim 模式，sidecar ≥ v1.2.0）==

请求：
  GET /slimapi/directories
  Headers:
    X-Slimapi-Version: 2          # 必带（所有 /slimapi/** 通用）
    Accept-Encoding: gzip         # 建议（envelope 可 gzip）
  无 query 参数（全局发现语义，干脆不接受 directory——与 /command、/agent 的 no-op directory 不同，本端点连参数都没有，避免误导）

响应（200，envelope 对象，非裸数组）：
  {
    "items": [
      {
        "directory": "/home/mar/personal_projects/ocdroid",   # 归一后的 workdir 绝对路径（去尾斜杠，根 / 保留）
        "title": "最近活跃顶层 session 的标题",                # winner session 的 title；非非空 string → null
        "lastUpdated": 1786187854486,                          # winner session 的 time.updated（epoch-ms）
        "rootSessionCount": 77,                                # 该 dir 顶层 session 总数（roots=true，parentID==null）
        "activeRootSessionCount": 3,                           # 未归档（time.archived 非数字）的数
        "archivedRootSessionCount": 74,                        # 已归档（time.archived 是数字）的数
        "archivedOnly": false                                  # activeRootSessionCount == 0 → true（全归档）
      },
      ...
    ],
    "discoveryComplete": true                                  # len(发现页) < 10000 → true；roots=true 量级≈workdir 数，实际恒 true
  }
  响应头：Cache-Control: no-store、（gzip 协商时）Content-Encoding: gzip + Vary: Accept-Encoding

排序：items 按 lastUpdated DESC，tie-break directory ASC（最近活跃在前，不是按 session 数）。
空结果（无任何 session）：{ "items": [], "discoveryComplete": true }（权威空）。

何时调用：冷启动拉一次渲染项目切换器；用户打开切换器时刷新。无需轮询（无增量机制）；如需感知新 workdir，重拉即可（开销小）。

winner 规则（title + lastUpdated 必须来自同一 session）：按 (time.updated, time.created, id) 字典序取 max 的那个 session；数字字段缺失/非数字→0 排最后。客户端无需自己算——直接消费 sidecar 给的 title/lastUpdated 即可。

== 二、降级方案（用户特别要求：非 slim 版本如何降级）==

/slimapi/directories 仅在 sidecar ≥ v1.2.0 的 slim 模式下可用。两种降级场景：

【场景 1】旧 sidecar（< v1.2.0，slim 模式）：调 /slimapi/directories → HTTP 404 {"code":"thin_route_not_found"} → 降级。探测结果缓存（feature flag / 一次性 probe），勿每连接重复探测。

【场景 2】非 slim 模式（ocdroid 直连 opencode，不经 sidecar）：无 /slimapi/** 路由面 → 直接走降级实现。

降级实现（按推荐度，可叠加）：

方案 A（推荐，覆盖面与 slim 版相同）—— 直连上游自聚合：
  ocdroid 直接请求 opencode GET /experimental/session?roots=true&archived=true&limit=10000
  （走 ocdroid 的 direct 配置端口，经 :14096 mTLS），客户端自行 group-by-directory 聚合。
  聚合算法必须与 sidecar 一致：
    - group key = session.directory（去尾斜杠，根 / 保留）；
    - 每 dir：rootSessionCount = 顶层 session 总数；activeRootSessionCount = time.archived 非数字的数；archivedRootSessionCount = time.archived 是数字的数（排除 bool）；archivedOnly = active==0；
    - winner = 按 (time.updated, time.created, id) 取 max 的 session；title 取 winner.title（非非空 string→null），lastUpdated 取 winner.time.updated；
    - items 排序 lastUpdated DESC + directory ASC。
  代价：拉完整 Session.Info 对象（不省流；roots=true 只返顶层 session，量级≈workdir 数，单次通常可接受）+ 客户端自实现聚合。
  依赖：opencode ≥ v1.18.x 提供 /experimental/session（experimental 端点，跨版本兼容性需注意；若 opencode 不支持或返 4xx → 退方案 C）。

方案 B（legacy，不足以做跨目录切换器）—— GET /session per-Location：
  受 X-Opencode-Directory 路由，只能看当前 workdir，无法跨目录发现。仅适合"确认当前 workdir 可达"，不能渲染跨目录项目切换器。不推荐作为切换器数据源。

方案 C（兜底，零网络依赖）—— 本地维护 directory 列表：
  客户端持久化"用户访问过的 workdir 路径"（历史记录 + 手动添加 + 首次建会话时记录 directory）。完全不依赖服务端发现，最可靠；但不自动发现新 workdir。
  建议与方案 A 叠加：A 拉到的 directory 并入本地列表；A 不可用时退回本地列表。

推荐组合：slim 版用 /slimapi/directories；降级时方案 A 为主、方案 C 兜底。

== 三、集成约束（必须遵守）==

1. fallback 规则（关键）：
   - 仅 HTTP 404 + body code=="thin_route_not_found"（场景 1）或确认处于非 slim 模式（场景 2）才走降级。
   - 绝不对 503（upstream_unavailable / transform_busy）/413/timeout/版本错误（400 version_required|version_incompatible）/鉴权错误降级——这些不是"不支持"，是临时故障；走 circuit breaker + 重试（transform_busy 看 Retry-After:2 头）。误降级会流量翻倍 + 掩盖问题。

2. total failure 处理：
   - 发现调用失败 → 整体 503 {"code":"upstream_unavailable"}（无 envelope）。客户端保留既有项目列表并重试，绝不据此推断"无任何 workdir"或清空 UI。

3. discoveryComplete 语义：
   - true 时 items 是当前已知 workdir 的完整快照（可 replace-all 本地列表）；false（发现页填满 10000，实际几乎不发生）时仅作参考，保留本地既有列表不 replace。

4. archivedOnly 的 UI 含义：
   - true = 该 workdir 所有顶层 session 已归档（用户在此无活跃会话）。建议弱化/折叠该条目，但不要隐藏（用户可能想切回去继续）。

5. 被动发现局限（必须让产品/UX 知晓）：
   - 本端点仅覆盖"至少有一条顶层 session 的 workdir"。从未在 opencode 里建过 session 的全新 workdir 不可见——用户"新建项目/打开文件夹"仍需走既有本地文件选择 + 发首条消息建 session 的路径，建会话后该 workdir 才会出现在下一次 /slimapi/directories 响应里。
   - 不扫文件系统；返回的 directory 不代表目录仍存在于文件系统（workdir 可能已被删除，旧 session 仍记录其 path）。客户端切换到一个 stale directory 时会收到上游 404/不可达，按 stale 处理（提示用户 + 保留列表）。

6. capability 探测：
   - 加性路由 ≠ 旧 sidecar 支持。不要只看 sidecar 版本号；用一次性 404 probe 或 health feature flag 判定。探测结果缓存。

== 四、错误码表（thin 路由统一 {"code":"..."}）==

  400 version_required / version_incompatible   # 版本头问题，修客户端
  404 thin_route_not_found                      # 旧 sidecar 无此路由 → 降级信号（场景 1）
  503 upstream_unavailable                      # 发现 total failure / 严格 schema 守卫失败 / 超 cap / 网络 5xx；无 envelope；重试
  503 transform_busy (+ Retry-After:2)          # 转换池饱和；按 Retry-After 重试
  422                                           # FastAPI 参数错误（本端点无参数，理论上不触发）
  注意：discovery 4xx 也映射为 upstream_unavailable（不泄漏 upstream status——experimental 端点 4xx 意 opencode 不支持）。

== 五、请确认 ==

1. 项目切换器 UI 计划展示哪些字段？（directory 必显；title/lastUpdated/rootSessionCount/archivedOnly 哪些进卡片？）
2. archivedOnly 的条目 UI 处理：折叠 / 弱化 / 隐藏 / 用户可配置？
3. 降级策略选哪个组合？（推荐 A+C；若 opencode 版本不支持 /experimental/session 则只能 C）
4. 是否需要 capability probe（一次性 404 探测）还是靠 feature flag 灰度？
5. 本地 directory 列表的持久化策略（DataStore？合并去重规则？上限？）

== 六、实施顺序建议 ==

1. slim 版直连 /slimapi/directories + 404 fallback 探测 + feature flag
2. 降级方案 A（直连 /experimental/session 自聚合）——与 slim 版共享同一聚合 UI 渲染层
3. 方案 C（本地列表持久化）兜底
4. 灰度：先开 flag 给少数设备，监控 404 fallback 率 / 503 重试率 / stale directory 命中率

sidecar 侧已就绪（v1.2.0 已部署）。ocdroid 侧实现完成后回传改动文件清单 + 测试矩阵。
```

---

## 本仓已推进的部分（不阻塞 ocdroid）

- `GET /slimapi/directories` 路由已实现（`src/oc_slimapi/routes/directories.py`），复用 `src/oc_slimapi/discovery.py` 共享发现 helper（`/slimapi/questions` 也用它）。
- 已发版 **v1.2.0**（git tag `v1.2.0`，minor bump；`X-Slimapi-Version` 不 bump，仍 2）。
- 本机服务已替换运行（`health.sidecar.version == 1.2.0`），实测 `GET /slimapi/directories` 返回 200 + 17 个真实 directory（覆盖 git repo + 非-git + git worktree 子目录）。
- 契约 / INTERFACE_MAP / CLIENT_CHANGES / CHANGELOG / design-v2 全部同步；`./scripts/check.sh` 全绿（1272 测试 + 13 路由↔文档一致性）。

## 关键设计决策（供 ocdroid 理解背景）

- **为什么新开端点而不是用 `/slimapi/sessions`**：`/slimapi/sessions` 透传 legacy `/session`，是 per-Location 的（受 `X-Opencode-Directory` 路由），无法可靠发现所有 directory。本端点用全局 `/experimental/session?roots=true` 弥补此 gap，并做 group-by-directory 聚合（N 个 session → M 个 directory，M≪N）。
- **为什么用 envelope 而非裸数组**：`discoveryComplete` 必须随 items 一起下发，否则客户端无法区分"完整快照"与"可能截断"。
- **为什么 strict schema 守卫**（坏 session → 503 而非静默跳过）：静默跳过会让"看似完整"的列表缺目录；与 `/slimapi/questions` 的 lenient 模式不同（questions 有 `authoritativeDirectories` partial 机制保护，directories 没有）。
- **评审来源**：本端点经 rev-ogpt 评审（裁定"修正后可执行"），4 个 blocker（envelope 完整性 / 严格守卫 / archived 语义 / TransformPool 资源边界）已全部落地。

## 相关文档

- 权威契约：[`../../specs/v2-contract.md`](../../specs/v2-contract.md) §2 端点表 + §2「`/slimapi/directories` envelope」+ §7 discovery 例外
- 客户端配套：[`../../specs/CLIENT_CHANGES.md`](../../specs/CLIENT_CHANGES.md)「全局 directory catalog」小节（含完整降级方案）
- 接口映射：[`../../specs/INTERFACE_MAP.md`](../../specs/INTERFACE_MAP.md) §1 `/slimapi/directories` 行
- 变更记录：[`../../../CHANGELOG.md`](../../../CHANGELOG.md) `[1.2.0]` 节
