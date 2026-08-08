# ocdroid 集成 `GET /slimapi/directories` — 双向确认

> 续 [`2026-08-08-ocdroid-directories-handoff-prompt.md`](2026-08-08-ocdroid-directories-handoff-prompt.md)。
> 本文档记录 ocdroid 项目组对 5 个确认问题的答复 + 附带决策，以及 oc-slimapi 侧的对齐核对结论。
> **结论：所有决策与 sidecar 契约一致，oc-slimapi 侧无需任何代码/契约改动；契约冻结，ocdroid 可进入实现。**

---

## 一、ocdroid 答复 + sidecar 对齐核对

| 项 | ocdroid 决策 | sidecar 对齐 | 备注 |
|---|---|---|---|
| **Q1 字段消费** | `directory`（basename + 全路径，必显）、`lastUpdated`（相对时间、排序键，必显）、`title`（可选次要提示）、`activeRootSessionCount`（可选小徽标）；`archivedRootSessionCount` 不单显；`archivedOnly` 不作文本字段 | ✅ 一致 | envelope 已提供全部字段，客户端选择性消费；契约不变 |
| **Q2 archivedOnly** | 不专门处理；全归档项目仍合法，靠 `activeRootSessionCount==0` 自然表达「休眠」，不禁选不隐藏 | ✅ 一致（数学等价） | `archivedOnly` 定义即 `activeRootSessionCount==0`；sidecar 仍下发该字段（契约不变），客户端不消费而已 |
| **Q3 降级组合** | slim 走 `/slimapi/directories` + 404 sticky flag；legacy（slim=false）隐藏整个功能；**MRU 作降级地板**（slim + 旧 sidecar）+ 离线兜底；不批量灌入 MRU（仅用户选中时并入）；**不上方案 A**（`/experimental/session` 自聚合代价过高，且 legacy 已隐藏功能无需它） | ✅ 一致（客户端选择） | sidecar 无需改动；CLIENT_CHANGES 已更新标注 ocdroid 实际采用 MRU、方案 A 降为参考 |
| **Q4 probe vs flag** | 一次性 404 probe + sticky flag（复用 `ServerCompatProfile`，新增 `supportsSlimDirectories`）；不引入 feature flag 系统 | ✅ 一致 | 与既有 catalog 端点 fallback 模式同构 |
| **Q5 持久化** | 复用既有 `recentWorkdirs` MRU（EncryptedSharedPreferences，per-fingerprint，cap 30）；禁 DataStore（项目规范硬约束）；MRU 仅用户选中时并入 | ✅ 一致 | 纯客户端决策 |

## 二、ocdroid 附带关键决策

| 决策 | sidecar 对齐 |
|---|---|
| **入口**：首页「添加项目」按钮左侧新图标，仅 slim 模式显示；旧 sidecar 时图标仍在、sheet 降级 MRU 标「本机最近项目」 | ✅ 客户端 UI |
| **已连接禁选集**：`normalize(recentWorkdirs) ∪ normalize(draftWorkdir)`，防重复添加 | ⚠️ 见下「normalize 一致性」提醒 |
| **transform_busy / Retry-After**：ocdroid 已有 `retryAfterHeaderToMs` + 重试范式，本端点复用，无 gap（已纠正原回执误判） | ✅ sidecar 仍发 `Retry-After:2`，无改动 |
| **集成约束**（fallback 纪律 / total failure / discoveryComplete / 被动发现局限） | ✅ 全部接受，与契约一致 |

## 三、需 ocdroid 留意的技术点

### normalize 一致性（关键）

ocdroid 用 `normalize()` 做「已连接禁选集」去重（`normalize(recentWorkdirs) ∪ normalize(draftWorkdir)`）。该 `normalize()` **必须**与 sidecar `normalize_directory` 语义一致：

```text
sidecar:  normalize_directory(s) = s.rstrip("/") or "/"
```

- 去掉**所有**尾部斜杠（`/a/b///` → `/a/b`）；
- 根 `/` 保留（`/` → `/`，空串 → `/`）；
- **不做** realpath / `..` 解析 / 大小写归一（sidecar 只 strip trailing slash）。

`/slimapi/directories` 下发的 `directory` 字段已由此函数归一。若客户端 `normalize()` 规则不同（如保留尾斜杠、或额外做 casefold），会导致 sidecar 返回的 `directory` 与客户端禁选集 key 不匹配 → 重复添加或漏判。建议客户端禁选集直接复用同一 `s.rstrip("/") or "/"` 规则。

> 校验方法：sidecar 实测响应里 `directory` 字段已是归一形态（无尾斜杠、根为 `/`），客户端可直接以其为 key，无需二次 normalize；若仍要 normalize 防御，务必用相同规则。

### transform_busy 误判纠正（acknowledge）

ocdroid 指出「原回执误判」——ocdroid 已有 `retryAfterHeaderToMs` + `Retry-After` 重试范式，`transform_busy` 路径无 gap。本端点 `503 transform_busy + Retry-After:2` 直接复用该范式即可，sidecar 行为不变。确认对齐，无 sidecar 侧动作。

## 四、oc-slimapi 侧结论

- **无需任何代码 / 契约改动**。v1.2.0 已发版部署，envelope 形状、字段、错误码、fallback 信号均满足 ocdroid 集成需求。
- **被动发现局限**已被 ocdroid 接受：全新 workdir 须先发起会话才可见；ocdroid 的「新建项目/打开文件夹」走既有本地路径，建会话后 workdir 出现在下一次 `/slimapi/directories` 响应。
- 契约冻结。ocdroid 可进入实现；实现完成后回传改动文件清单 + 测试矩阵。

## 五、相关文档

- 交接提示词：[`2026-08-08-ocdroid-directories-handoff-prompt.md`](2026-08-08-ocdroid-directories-handoff-prompt.md)
- 客户端配套（含降级方案 + ocdroid 实际选择标注）：[`../../specs/CLIENT_CHANGES.md`](../../specs/CLIENT_CHANGES.md)「全局 directory catalog」
- 权威契约：[`../../specs/v2-contract.md`](../../specs/v2-contract.md) §2 + §2「`/slimapi/directories` envelope」
