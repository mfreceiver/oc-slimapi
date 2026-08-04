# ocdroid 集成告知：catalog skeleton 路由（command / agent）已上线

> 本仓 sidecar 已上线两个加性省流路由：`GET /slimapi/command` 与 `GET /slimapi/agent`。
> 本文件是给 **ocdroid 侧**的完整、自洽集成告知材料。下方「提示词正文」整段可转发给 ocdroid 开发者/agent。
> 上游契约权威：[`../../specs/v2-contract.md`](../../specs/v2-contract.md)；ocdroid 改动清单（已同步）：[`../../specs/CLIENT_CHANGES.md`](../../specs/CLIENT_CHANGES.md)。
> 背景（评估证据链 + 实测）：[`../specs/2026-08-05-slimapi-candidate-interfaces-assessment.md`](../specs/2026-08-05-slimapi-candidate-interfaces-assessment.md)。

## 本仓侧状态

- **已合并并推送** `origin/main`（commit `4374ec4 feat(catalog)`）。
- **本地 systemd 服务已重启并 live smoke 通过**：两个端点对真实 opencode 返回 200 + 正确 skeleton。
- **Wire 版本未 bump**（仍 `X-Slimapi-Version: 2`，纯加性）。
- **check.sh**：1004 passed，路由↔文档一致性 11/11；**rev-gpt 评审**：初轮 FAIL（9 项发现）→ 全部修复 → 复审 **PASS**。

---

## 提示词正文

```text
oc-slimapi 已上线两个加性 catalog skeleton 路由，请按本告知在 ocdroid 侧接入。先只读分析现有 command/agent 调用点与 UI 消费字段，确认无依赖被裁字段后再切流；灰度启用，异常关 flag 即回退。不要凭假设改协议。

== 一、已上线端点（live，本 sidecar 已 smoke 验证返回真实数据）==

1) GET /slimapi/command
   - 必带头：X-Slimapi-Version: 2
   - 建议头：Accept-Encoding: gzip（进一步省流）
   - query：directory（可选；仅作 X-Opencode-Directory 头转发，catalog 全局、上游忽略；不传也行）
   - 200 返回：裸数组，每项白名单 {name, description, agent?, hints?}
     · agent / hints 为可选字段（少数 command 才有；agent 常为 null）→ 缺则不出现，不补键
     · 已丢弃：template（约占 97.7% 字节）、source、model、subtask
   - live 实测样例：[{"description":"guided AGENTS.md setup","name":"init","hints":["$ARGUMENTS"]}, ...]

2) GET /slimapi/agent
   - 必带头：X-Slimapi-Version: 2
   - 建议头：Accept-Encoding: gzip
   - query：directory（可选，同上）
   - 200 返回：裸数组，每项白名单 {name, description, mode, hidden?, native?}
     · hidden / native 可选（可能为 null / false）→ 缺则不出现
     · 已丢弃：prompt（约占 34.7%）、permission（约占 61.2%，是 Permission.Ruleset 规则集，不是 pending permission card）、topP、temperature、color、variant、options、steps、model
   - live 实测样例：[{"description":"AI coding orchestrator...","name":"orchestrator","hidden":null,"mode":"primary","native":false}, ...]

实测省流（opencode v1.18.13 数据，raw / gzip）：
  - command: 292KB → 7.25KB（raw 省 97.6%）/ 3.18KB（gzip 省 97.0%）
  - agent:   250KB → 10.7KB（raw 省 95.8%）/ 3.57KB（gzip 省 89.4%）
  注：agent 的 gzip 有消解——permission 是重复 rule 串、压缩率高；验收省流比必须用 gzip / downOut 口径，不能用 raw。

== 二、错误码（thin 路由统一形状 {"code":"..."}，code 是机器可读判别字段）==

  - 400 version_required（缺 X-Slimapi-Version）/ version_incompatible（越界）
  - 400 invalid_directory（directory 含 .. / 控制字符 / 超长）
  - 404 thin_route_not_found —— 旧 sidecar 没有此路由（这是「不支持」的唯一信号，见 fallback 规则）
  - 413 response_too_large（+ limit 字段；catalog 上限 max_response_bytes，默认 64MiB，正常不会触发）
  - 502 upstream_http_N（上游 4xx，非 404）
  - 503 upstream_unavailable（上游 5xx / 网络 / 坏 JSON / 非 list body / 读流中途断开）
  - 503 transform_busy（+ Retry-After: 2 头；转换池饱和）
  - 422（FastAPI 参数校验错误）
  catalog 端点不是 session 级，没有 session_not_found。

== 三、ocdroid 需要做的（按顺序）==

1. 新增两个 SlimApi client 方法（GET /slimapi/command、GET /slimapi/agent），自动附 X-Slimapi-Version: 2 + Accept-Encoding: gzip。复用现有 thin route 的错误体 {code} 解析与 circuit breaker。

2. 能力探测（capability gating）——关键：
   · 这是加性路由，旧 sidecar 没有它。不能只看版本号判断「支持」。
   · 推荐做法：health feature flag 或一次性 404 探测——首次请求收到 404 thin_route_not_found 即标记「该 sidecar 不支持 catalog slim」，后续直接走 passthrough，不重复探测。
   · 探测结果缓存（进程内 / DataStore），避免每次连接重复探测。

3. fallback 规则（必须严格遵守，否则会流量翻倍 + 掩盖问题）：
   · 【唯一回退触发】404 thin_route_not_found → 回退到 catch-all 透传 GET /command、GET /agent（旧路径，行为不变）。
   · 【绝不回退】503（upstream_unavailable / transform_busy）、413、timeout、version 错误、鉴权错误——这些是「暂时故障 / 配置错误」，不是「不支持」。回退会把流量打回大接口，使省流失效并掩盖真实问题。正确处理：503 走 circuit breaker + 按 Retry-After 重试；413 极不可能，若发生当异常上报。

4. 切流（灰度）：
   · command 面板 / agent 选择器 UI 改为优先消费 slim 端点（feature flag 控制）。
   · 解析时只读白名单字段：command 读 name/description/agent/hints；agent 读 name/description/mode/hidden/native。
   · native 字段（agent）务必解析（白名单保留它就是为客户端消费）。
   · flag 关闭 = 立即回退 passthrough，无需改代码（passthrough /command /agent 一直可用）。

5. 监控（本仓已提供）：
   · GET /slimapi/metrics.traffic 现在有独立 command / agent 桶——可看到每个端点的 upIn / downOut / 省流比。
   · sidecar access log 每条带 bucket 字段（command / agent），可按桶聚合验证收益。

== 四、ocdroid 绝对不能做的 ==

1. 不要从 slim 端点期望被丢弃的字段：
   - command：不要依赖 template / source / model / subtask
   - agent：不要依赖 prompt / permission / topP / temperature / color / variant / options / steps / model
   · 若某 UI 真需要这些字段（见下方待确认问题），用 passthrough GET /command、GET /agent（直连，不经 slim）。本批没有 /slimapi/command/full 或 /slimapi/agent/full。

2. 不要把 503 / 413 / timeout 当「不支持」触发回退（见 fallback 规则）。

3. 不要用版本号判「支持」（加性路由；用 404 或 feature flag）。

4. 不要期望 catalog 条目带 hasFull / omitted——那是 message part 的概念；catalog 条目是扁平白名单，无 per-entry expand 端点。

5. 不要在 directory 上做假设——catalog 是全局的，传或不传 directory 返回相同的全集（这是预期行为，不是 bug）。

== 五、待 ocdroid 确认（影响是否可全量切流）==

1. 是否有任何 UI 需要完整 command.template（命令渲染 / 预览 / 本地解释）？→ 若有，该入口必须走 passthrough。
2. 是否有任何 UI 需要完整 agent.prompt 或 agent.permission（如 agent 详情页）？→ 若有，走 passthrough（本批无 slim full 变体）。
3. 确认 command.agent / command.hints / agent.native 当前仍在消费（白名单保留它们正是为此）——若已弃用请告知，可进一步裁剪。
4. command / agent 是否随目录 / 项目配置 / 用户身份变化？（影响客户端缓存策略；catalog 当前是全局全集）

== 六、兼容性（双向）==

1. wire 版本未 bump，仍 X-Slimapi-Version: 2。
2. 旧 sidecar（本 commit 之前）→ 404 thin_route_not_found → ocdroid 透明回退 passthrough，零回归。
3. 旧 ocdroid（未接入）→ 继续走 passthrough GET /command、GET /agent，行为完全不变。
4. 加性：ocdroid 可在任意版本 sidecar 上运行——新 sidecar 享省流，旧 sidecar 走透传。

== 七、交付证据要求 ==

1. 现有 command/agent 调用点 + UI 消费字段审计结论（证明无依赖被裁字段）。
2. ocdroid 改动文件清单 + 接口差异（新增 SlimApi 方法、feature flag、404 回退、capability 缓存）。
3. 对第五节 4 个确认问题的答复。
4. 灰度计划 + 回退方案（关 flag 即回退）。
5. 监控观测：接入后 command/agent 桶的 downOut / 省流比、404 回退次数、transform_busy 次数。

在确认前：不要把 503 当删除；不要把回退当默认；不要单方改 wire 字段。
```

---

## 本仓侧已完成的（不阻塞 ocdroid）

- `GET /slimapi/command` skeleton 路由（whitelist `{name,description,agent,hints}`，stream + cap + admission + gzip）
- `GET /slimapi/agent` skeleton 路由（whitelist `{name,description,mode,hidden,native}`）
- `skeleton_commands` / `skeleton_agents` 投影（过滤非 dict 元素，防 malformed 上游 500）
- traffic 桶 `command` / `agent`（`/slimapi/metrics.traffic` 可见）
- `INTERFACE_MAP.md` / `CHANGELOG.md` / `CLIENT_CHANGES.md` 同步
- 旧路径 `GET /command`、`GET /agent`（catch-all 透传）保持不变（灰度回退锚点）

## 尚未做（待 ocdroid 确认需求后再定）

- `GET /slimapi/command/full`、`GET /slimapi/agent/full`（仅当确认 UI 真需要 template/prompt/permission 时再做；优先方案是「按名查单条详情」而非全量 full）
- `hints` 单项 / 总量大小 cap（当前原样保留；live 值 14B，省流比为实测非保证；列为 follow-up）
