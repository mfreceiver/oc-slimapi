# D08 — A8 安全审计（三层入口威胁模型）

> Phase 2 / A8 产物，2026-08-20。只读审计：未改任何 src/ 代码、未实机部署、未对外网络操作。快照 `0b836e7`（v4.4.0）。
> 证据格式 `file:line`（相对仓库根）；上游证据以 `opencode-src/current/` 为根并显式标注。
> 威胁模型（固定）：**E-I** loopback 127.0.0.1:4097 + stunnel mTLS 14097（sidecar 仅见本机来源）；**E-II** 0.0.0.0:4097 明文（deploy 单元现实，依赖 Tailscale ACL——**ACL 不可实机验证，该入口一切结论标注「部署边界未验证」**）；**E-III** stunnel 14097→loopback（认证由 stunnel verifyChain 提供，sidecar 自身无任何认证）。
> 信任假设（固定）：客户端输入不可信；上游响应半可信；本地文件系统/DB 半可信。

---

## 0. 入口模型与部署事实（锚点）

| 入口 | 事实锚点 | 认证 | 加密 |
|---|---|---|---|
| E-I | config.py:356（默认 host=127.0.0.1）+ stunnel.conf:18-20（accept 127.0.0.1:14097 → connect 127.0.0.1:4097） | stunnel mTLS（verifyChain=yes，stunnel.conf:11） | sidecar↔stunnel 明文本机 |
| E-II | **deploy/oc-slimapi.service:28 `OC_SLIMAPI_HOST=0.0.0.0`**（注释 :26-27 自认「:4097 为明文，远程暴露必须靠 stunnel mTLS / Tailscale ACL 隔离」） | **无**（sidecar 零认证、零鉴权） | 无 |
| E-III | stunnel.conf:18-29 | stunnel 证书链校验 | mTLS 于 14097；14097→4097 明文 loopback |

附带部署事实：deploy:33 `ACCEPTED_CLIENT_VERSIONS=2,2` 使该模板**按原样永远起不来**（config-census §3 crash-loop 推演；F-004 已立）——安全角度这是 fail-closed 的正面案例（版本钉死 config.py:817-822 不可经 env 放宽），但也意味着「实际生产 unit 与模板的偏离程度」不可从仓库验证（哪些 env 行被运维手工改过未知）→ E-II 结论的「部署边界未验证」同时涵盖 ACL 与实际生效配置两层。deploy:70 `MemoryMax=384M` 是 T3/T4 内存面的 cgroup 兜底。

---

## 1. 结论矩阵（9 项 × 3 入口）

| # | 项 | E-I | E-II（部署边界未验证） | E-III |
|---|---|---|---|---|
| 1 | header 注入/请求走私 | **通过**：上游请求头白名单（仅 X-Opencode-Directory+X-Request-ID+content-type），directory 经 validate_directory 控制字符拒绝、request-id 经 printable-ASCII≤128 门；CL/TE 由 h11+httpx 单栈各自权威化，sidecar 不透传 framing 头 | 同 E-I（明文不改变 header 构造面）；唯 openapi.json/docs 穿透（F-137）在 E-II 变成免认证 schema 全暴露 | **通过**（同 E-I） |
| 2 | directory canonicalization | **通过**（受信消费方；normalize+validate 拒 `..`/控制字符/超长；canonical 转发闭环 symlink-swap TOCTOU） | **高危 F-251**：allowlist 默认 None=零过滤 × 无认证 → 任意目录可用（/file/content 等）；另 F-252：即使启用 allowlist 覆盖面也不完整 | **通过**（受信消费方；F-252 覆盖面缺口在受信方下降为 P3） |
| 3 | 解码放大 | **通过**（cap=解压后字节，早停≤cap+64KiB；上游信任环内） | 同机制；MemoryMax=384M cgroup 兜底 OOM；F-255 记录 cap 语义/记账失真 | **通过** |
| 4 | JSON 深度/大小炸弹 | **通过**：orjson 自带深度上限（实测 1000 过/100000 拒）；stdlib json（providers v4）在 Py3.14 非递归实现无 RecursionError；parse 前有字节 cap | 同 E-I（transform 池 max_transforms=1 串行化 CPU；raw 族靠 cap+连接池） | **通过** |
| 5 | DoS 面 | **可控**：SSE 双账本上限（8/16+8/64）、replay 三维上限、域壳只随上游活动增长、singleflight/catalog 有界；F-013 超长 seq 500 已立 | 同机制但攻击者免认证；订阅上限（8/16）挡连接洪峰的「占位」面，非认证面 | **可控** |
| 6 | 信息泄露 | **低**：错误体无路径/schema 泄漏、metrics/health 无目录名、access log 仅 path 无 query、clientId 默认 HMAC；**F-017 providers v3 明文密钥中转（受信方，P2）** | **F-017 升 HIGH**（免认证拿 provider api/key）+ F-137 openapi（MEDIUM）+ F-251 附带目录枚举（/directories、q/p 聚合） | 同 E-I（受信方）；providers v3 密钥面同样存在（任何持证书消费方） |
| 7 | SSRF/路径穿越 | **通过**：file 三端点子树约束在**上游**双重闭环（resolve+contains 与 realpath+contains）；上游 URL host 恒为 config 钉死 loopback | `%3F` 路径参数 query 注入 F-254（低危，被 session.directory 优先级对冲）；上游自身 workspace=remote 代理面为 opencode 固有、sidecar 不放大 | **通过** |
| 8 | secrets 卫生 | **通过**：260 tracked 文件扫描 0 真阳性（143 路径/3210 关键词命中全为术语）；stunnel.conf 仅证书路径 | 同 E-I（无差异面） | **通过** |
| 9 | 依赖面（接口层，转 A15） | 4 直接依赖接触点清单（§10）；无锁文件/无哈希钉死属 A15 议题 | 同 E-I | 同 E-I |

---

## 2. 逐项分析

### 2.1 T1 — header 注入 / 请求走私

**转发头白名单（核心结论：客户端头从不整集转发上游）**
- 上游请求头构造点全集只有三类：`forward_directory_headers`（upstream.py:113-114）、`forward_upstream_headers`（upstream.py:117-136，directory+X-Request-ID）+ write 族 verbatim content-type（write_groups.py:160-169）。rg 全 routes/ 的 `headers=` 构造点复核无第四类（questions.py:334、permissions.py:352、messages.py:329/498、_catalog_common.py:76-83、health.py:118-122 全走同一白名单）。客户端 Authorization/Cookie/X-Forwarded-* 等**根本不进**上游请求。
- `strip_hop_by_hop`（upstream.py:49-111）生产零消费者（F-024 已立）——因整集转发已不存在，该死代码不构成实际风险面，仅为清理项。

**X-Opencode-Directory 注入面**
- 值来源两个：selector stash（`?directory=` 经 `validate_directory`，directory.py:23-52 拒 `..`/`.` 段、NUL、ord<0x20/0x7f 控制字符、>4096）与 write/read 路由的 `_resolve` 二次 validate（read_groups.py:106-112、write_groups.py:106-109）。**CRLF 注入被字符级拒绝**；且 header 通道对客户端已退役（selector.py:702-704 presence 即 400）。
- 残余通道：read_groups.py:109-111 在 selector-less 栈会读客户端 header——生产栈 selector 恒在（app.py:747），不可达。

**重复 header / 控制字符（入口侧）**
- 入站头由 uvicorn(h11 0.16.0) 解析：控制字符在请求解析层即拒绝；`X-Request-ID` 透传前再过 printable-ASCII(0x20-0x7e)≤128 白名单（middleware/request_id.py:29-60，P1-15）；客户端身份头（X-Client-*）同样控制字符/长度拒收（middleware/traffic_accounting.py:110-144）。
- 重复 `X-Opencode-Directory`：selector 只取首值判定 presence（selector.py:448-458）→ 400 retired，不产生歧义解析。

**CL vs TE**
- sidecar 是「ASGI 应用 + httpx 客户端」而非字节级代理：入口 framing 由 uvicorn/h11 权威化（request.stream() 语义），出口由 httpx 重建（build_request 统一 Content-Length）。无 CL/CL、CL/TE 重放窗口——请求走私在结构上不可达。`Transfer-Encoding` 在响应侧不透传（_read_passthrough.py:71-77 白名单仅 content-type/location/retry-after/x-request-id/last-request-id；Content-Encoding 亦不透传，:66-70 注释言明 httpx 已解码+sidecar 自有 gzip 域）。

**响应头白名单**：`_PASSTHROUGH_UPSTREAM_HEADERS`（_read_passthrough.py:71-77）——Set-Cookie 永不下发。**通过**。

### 2.2 T2 — directory canonicalization

- 归一化/校验：`normalize_directory` 剥尾斜杠保根（directory.py:12-20）；`validate_directory` 拒遍历段/NUL/控制字符/超长（directory.py:23-52）。相对路径**不在此拒**——相对 directory 会进入上游（上游按自身语义处理）。
- allowlist（启用时）：`candidate_canonical` 每次实时 realpath、相对候选 fail-closed None（config.py:294-318）；roots 按值缓存（config.py:268-291，root symlink 重定目标需 config 重载才重解析——**文档化 ops 语义**，config.py:237-241）；匹配为边界对齐前缀（config.py:321-338）。file 三路由转发 **canonical 形态**（read_groups.py:123-143，rev-2 sub-3：授权对象=访问对象，check-后-symlink-swap 不能改靶）。
- **TOCTOU 残余**（记录级，不立 F）：①roots 缓存窗口（文档化）；②non-strict realpath 保留「不存在的尾段」——授权后、上游使用前在该尾段创建 symlink 的窗口存在，但需本地 FS 半可信攻击者配合，且转发的是 canonical 串（新 symlink 不影响已解析前缀，只影响尾段中尚不存在的部分——该部分对 upstream 也是首次解析，等价于 directory 本身含 symlink 的常态情形）。上游侧另有 realpath+contains 二重闸（见 T7）兜底 file 三端点。
- **allowlist 三态语义**：None=不过滤（**默认**，config.py:498 `field(default_factory=_directory_allowlist_env)`、config-census §1 #34）；`""`=[]（/file 路由 reject-all、SSE hub 放行——不对称，config-census D9 已记）。deploy unit **未设置** allowlist → 生产（照模板）= 不过滤。
- **覆盖面缺口（F-252，新立）**：allowlist 只在 5 处生效——file 三路由（read_groups.py:123-143）、SSE 帧过滤（global_hub.py:572-586，None/空放行）、v4 sessions 降级矩阵（sessions.py:488-491 非空→503）、health 的 enabled 回显（health.py:90-101）、cursor 指纹（dbaux/cursor.py:100-113）。**vcs×3 / find / providers / session-single / messages 族 / todo / children / diff / 全部 write 路由 / questions / permissions 均不查 allowlist**——启用 allowlist 并不构成完整目录授权边界。
- **× E-II 组合（F-251，新立，P1，部署边界未验证）**：0.0.0.0 明文 + sidecar 零认证 + allowlist 默认 None ⇒ 任意可达者可对任意 directory 调 /file/content?path=…（上游在该 directory 子树内服务任意文件内容）、跨目录聚合（questions.py/permissions.py 按全局会话目录扇出）、/directories 列出全部工作目录绝对路径（directories.py:17-108）、全部写路由（POST/DELETE session、prompt_async…）、以及（若 manifest 启用）actions exec。唯一前置是 Tailscale ACL——**不可实机验证**。

### 2.3 T3 — 解码放大

- **cap 作用域判定：解压后（实体）字节，非线上字节**。`read_with_cap` 迭代 `response.aiter_bytes()`（transform.py:184-192），httpx 0.28 在该迭代内做 content-encoding 解码（httpx 语义：aiter_bytes 产出解码后块）。sidecar 从不显式发 accept-encoding 上游，但 httpx 默认头自动声明 `gzip, deflate`（venv 无 brotli/zstd，pip-list 快照佐证——见 §10）。
- 推论三则：
  1. **413 判定按解压后字节**：上游 gzip 炸弹（如 64KB wire → 64MiB 解压）能以极小 wire 成本触发 cap——但 cap-bail 在 `cap+64KiB` 处早停（transform.py:189-190），单请求解码 CPU 与缓冲均有界；这是正确的设计取向（限制的是 sidecar 实际负担而非上游出口流量）。
  2. **upIn 记账失真（F-255）**：`stash_up_in(len(chunk))` 计的是解压后字节——gzip 响应的 upIn 系统性大于 wire 字节（traffic-accounting.md 的「省流」口径在 gzip 上游响应上失真；上游 opencode /session 族实际是否回 gzip 未实机验证——上游 Effect/http 服务端压缩行为未知，标注）。
  3. **raw 透传族无 transform 准入**：providers v3/file/vcs/find/active/health/context（project=None，_read_passthrough.py:190-192）不占池，每请求可缓冲至 `max_response_bytes`(64MiB)+64KiB；并发上界=httpx 池 `max_connections=32`（upstream.py:44）⇒ 最坏瞬态 ~2GiB 量级——由 deploy `MemoryMax=384M`（oc-slimapi.service:66-70，注释自述「cgroup-enforced OOM kill protects the host」）兜底为 OOM-kill 自保护而非主机耗尽。配置上限抬到 256MiB（config.py:850 校验域）时该乘积最坏 ~8GiB，仍靠 cgroup。合并入 F-255（P3 记录）。
- **cap 层级复核**（与 dataflows §附-2 一致，无新缺口）：`max_response_bytes` 64MiB（列表/透传，transform.py:143+调用点）/ `max_message_bytes` 32MiB（单消息+请求体，write_groups.py:135-144、messages.py:544-547）/ `max_expand_response_bytes` 8MiB（expand 序列化后，messages.py:1523-1527）/ providers 投影 8MiB 常量（providers_projection.py:57）；merged 8MiB 预算+后验硬上限（messages.py:650-686，B6）。层级完整、判定点均在 parse 之前（除 expand 的序列化 cap 与 providers 的投影 cap，两者位置正确）。

### 2.4 T4 — JSON 深度/大小炸弹

- orjson 3.11.9 文档语义 + 只读实测（.venv 内库级探针，无仓库写入）：深度 >~1024 拒（`array and object recursion depth exceeded`）——**深嵌套炸弹被 parser 原生拒绝**；大整数 >64 位静默转 float（有损但无异常）；`1e999`/`NaN`/`Infinity` 字面量拒（JSONDecodeError）。
- stdlib json（仅 providers v4 `_loads_strict`，providers_projection.py:149-155）：Py3.14 C 扫描器实测 50000 层无 RecursionError（非递归实现）——`except (JSONDecodeError, UnicodeDecodeError, ValueError)` 覆盖域足够；重复成员名 fail-closed 502（:137-146）。
- **transform 池最坏 CPU × 8MiB cap**：`max_transforms=1`（默认）+ 同尺寸 ThreadPoolExecutor（transform.py:206-212）⇒ 解析/投影/gzip -6 串行化；单 job 输入 ≤64MiB（或 32MiB 消息），或json.dumps+gzip level 6 为主要成本——最坏单 job 数秒级，被 `transform_wait_seconds=2` 准入超时（503 transform_busy，transform.py:220-245）隔离为排队而非事件循环阻塞。SSE 心跳不经池（transform.py:28-29）。expand 的 worker（`_expand_fragment_worker`）同池同门。**通过**。

### 2.5 T5 — DoS 面

- **SSE 订阅上限（T3 具体值）**：控制面 `max_subscribers_per_directory=8`/`max_total_subscribers=16`（config.py:978-980 校验、registry.py:209-229 单无 await 临界区检查+add；溢出→503+Retry-After:5）；token 流独立账本 `token_stream_max_subscribers=8`/queue 64/512KiB/帧 1MiB（config.py:1004-1015、subscriber.py:625+）；handshake 独立预算 2048 项/8MiB（subscriber.py:302-303）。双账本合计最多 24 并发 SSE 连接——占位型 DoS 上限明确。
- **replay per-sid 域基数**：域仅在 `append` 时惰性创建（replay_log.py:369-372），append 只由上游事件驱动（global_hub._replay_publish / tokenstream hub）——**客户端枚举 sid 订阅不创建域**；`replay()`/`classify_reconnect` 纯读。域壳进程内不删（:251-257 注释自认）但基数=有上游活动的 sid 数（上游半信任域）。token hub `_subs_by_sid` 空集即 pop（hub.py:1340-1353）——sid 轮换不膨胀。三维上限：每域 2048 帧/全进程 64MiB/TTL 900s + barrier 元数据免逐出（:93-95、:472-493）。
- **cursor/指纹解析成本**：v4 cursor 解码全语法校验（dbaux/cursor.py:165-179，字母表/长度/键集/类型），超长但合法的 cursor 接受——成本线性、入参受 h11 头上限约束；Last-Event-ID 超长 seq `int()` ValueError→500 已立 **F-013**（replay_wire.py:164-166 `int(seq_text)` 无长度门；同一解析器两路由共用）。
- **expand invalid 桶**：非法 category 折叠 `"invalid"` 计数桶（traffic.py:68-81、_record→record_expand traffic_accounting.py:391-397）——仅计数维度，无资源占用（400 先于准入，messages.py:1538-1544）。
- **singleflight/catalog 键基数与 TTL**：plain profile `_MAX_RETAINED_ENTRIES` 硬帽（singleflight.py:97/330-332）+ grace 默认 1s（:97）+ leased 字节预算（raw_fetch_max_bytes，app.py:385-388）；catalog 双帽 16 entries/16MiB+TTL 300s（catalog_cache.py:55-68）+ 序列点逐出（:149-155）。键含客户端可控 sid/mid，但留存窗口≤1s grace、基数帽独立存在。**可控**。

### 2.6 T6 — 信息泄露

- **错误体逐 except 验证（负向结论：干净）**：`CodedHTTPException` 渲染仅 `{code, **fields}`（errors.py:44-52）；上游错误映射只产结构码+status（upstream_errors.py 全文）；dbaux 一切异常→503/降级、SQLite 分类/路径/schema 只进日志（lifecycle.py:448-460、sessions.py fail-closed 族；`_reason_detail` 仅 log，lifecycle.py:554-557）；`Disabled(path_ambiguous)` 的候选列表 detail 只入 startup 日志（lifecycle.py:733，_log_startup）。**DB 路径/schema/allowlist 条目零 wire 泄漏**。
- **metrics/log 载荷**：metrics 无 DB path（metrics.py:48-60 显式剔除 path 仅留 source）、无目录名（hub/clients 块只有计数与 subscriberId，registry.py:327-367）、replay 块仅计数/尺寸+epoch（metrics.py:85-107）；access log 行=method+path（**无 query string**——search 内容/directory 值不入日志，access_log.py:333-364）+clientId 默认 HMAC-sha256 截断（access_log.py:92-103；`client_id_hash=false` 静默明文回退属隐私弱点，config-census D10 已记）。
- **providers v3 透传（F-017 主辖复核，按入口分层）**：`GET /slimapi/config/providers?v=3` 逐字节透传（read_groups.py:402-409 fallback；_read_passthrough.py:157-277），上游 Info 含 `api/key/env/options` 字段全集（upstream-notes §6：provider.ts:1053-1062；key 仅 connected provider 携带，:1282-1289）。**分层定级：E-I/E-III= P2（受信消费方集合内暴露——任何持 stunnel 证书者可得明文 provider key，密钥经 sidecar 中转扩大了可取面）；E-II= HIGH（免认证直接可得，部署边界未验证）**。v4 面 §12 投影已剔除该字段族（providers_projection.py:295+白名单）。E-III 附加注：stunnel CA 签发的**每个**客户端证书持有者都是该密钥面的消费方——证书受众范围=泄露范围（仓库不可验证）。
- **actions query 输出回显面**：`query` 动作回显 manifest 命令 stdout（≤1MiB 硬帽，actions.py:57），manifest 固定 argv、默认禁用（deploy:60 注释）、owner-only-write+chmod 0600 校验；`confirm` 是客户端自报 bool（routes/actions.py:144）——**非授权机制**（actions.py:12-19 自述 risk-accepted）。E-I/E-III 可接受；E-II 下若 manifest 启用则并入 F-251 放大项。
- **/directories、/questions、/permissions**：响应含全部工作目录绝对路径（directories.py:187-195）与逐目录问题/权限内容——功能即面；E-I/E-III 受信 OK，E-II 并入 F-251。
- SSE 错误帧自带秘密清洗器（hub_types.py:41-57 `_SECRET_RE`——access_token/api_key/password 等 13 类词形 redact+512 截断+首行化）——session.error 帧不泄上游错误文本中的密钥形字符串。
- **F-137 安全角度补充（FastAPI docs 穿透）**：`/openapi.json` 暴露 54 路由全集+模型形状。分层：E-I/E-III=LOW（schema 本就是契约公开物 docs/specs/v3-contract.md 的子集）；**E-II=MEDIUM**（免认证侦察面：错误模型/参数域/路径全集一次性枚举，降低攻击成本）；另计费面：4 路由 200 记入 passthrough 桶破坏哨兵（F-137 原证）。维持 P2（defect 主定级），安全维度不升级编号。

### 2.7 T7 — SSRF / 路径穿越

- **file 三端点子树约束：在上游，双重闭环（佐证）**。opencode-src `handlers/file.ts`：`/file/content` → `path.resolve(directory, path)` + `FSUtil.contains(directory, file)` 词典法（file.ts:96-99，escape→`Effect.die`）；更底层 core FileSystem `resolve`：resolve+contains 后再 `realPath`+contains(root)（`core/src/filesystem.ts:68-77`）——**词法与 symlink 两道子树闸都在上游**，`/file/list`/`/file/status` 同走该服务。sidecar 的职责只有 directory 选择权（allowlist）——上游 containment 不依赖 sidecar。
- **上游 URL 拼接点清查（`rg f"/` 全集，见任务清单输出）**：全部为「字面路径 + `{sid}/{mid}/{session_id}/{permission_id}/{request_id}` 插值」或「固定路径+verbatim raw query」（_read_passthrough.py:103-116）。**host 不可注入**：base_url 钉死 `http://127.0.0.1:4096` 且 config 校验拒凭证/query/fragment（config.py:779-781）——SSRF 目标不可控，非 SSRF。
- **插值缺陷两处（F-253/F-254，新立）**：
  1. **控制字符→裸 500（F-253）**：uvicorn 将 `%0A` 等解码进 `scope["path"]`，Starlette `{sid}`=`[^/]+` 放行，f-string 路径含 `\n` 时 `httpx.build_request` 抛 `InvalidURL`（httpx 0.28.1 实测：`Invalid non-printable ASCII character in URL`）——而所有转发点的 build 均在 try 之外（_catalog_common.py:79-91、write_groups.py:171-192、messages.py:496-505、read_groups 经 _read_passthrough→stream_upstream 同构），InvalidURL 非 RequestError → 逃逸成 ServerErrorMiddleware 裸 500。影响族：session/messages/todo/children/diff/write/question/permission 全插值路由。推导级证据（uvicorn 解码行为为标准实现事实，未实机起服务验证——审计纪律）。
  2. **`%3F` 解码→上游 query 注入（F-254）**：httpx 实测 `build_request("GET", "/session/x?limit=0")` → raw_path `/session/x?limit=0`（`?` 成为分隔符，不重编码）。可注入 `?directory=`/`?workspace=` 到上游 URL。**对冲事实**：上游 workspace-routing 对 session 形路径先取 session 自身 directory（`opencode-src .../middleware/workspace-routing.ts:182`：`session?.directory || defaultDirectory(...)`）——sid 存在时注入无效、不存在时请求本身 404；非 session 形上游路径无 sid 插值。故实际影响低（P3）：主要为上游 4xx/5xx 形状扰动 + 理论 workspace=remote 代理触发（opencode 固有面，直连同样可达）。上游 query directory 优先于 header 的语义（workspace-routing.ts:86-88 `url.searchParams.get("directory") || header || cwd`）已录为佐证——这正是 selector 剥 query directory（selector.py:713-716）必要的上游依据。
- tolerant 路由（/api/session/active、/global/health）的 directory query verbatim 上传：上游对应组不读 directory query（无 query 协议），无实效——记录不立 F。

### 2.8 T8 — secrets 卫生（扫描归档）

- **扫描方法**：`git grep -n -i -E 'api[_-]?key|secret|password|token' -- .`（全 260 tracked 文件）+ 高熵模式二遍扫（`sk-`/AKIA/ghp_/xox/`-----BEGIN PRIVATE KEY-----`/JWT 形，python git ls-files 逐文件正则）。
- **数字**：关键词命中 **3210 行 / 143 路径**；高熵真阳性模式命中 **0**。字面赋值形（`api_key = "…"` / `password: "…"`）**0**。
- **逐类判定**：
  - `token`（占 >90% 命中）：token stream 领域术语 + `secrets.token_hex`（epoch/subscriberId 生成，hub_types.py:240、replay_log.py:108）——术语误报。
  - `api[_-]key`：仅 2 处——测试红action 断言（test_hub_behavior_lock.py:826-827）与 providers 投影 fixtures 注释（test_providers_projection_v4.py:110 `"env": ["OPENAI_API_KEY"]  # discarded`）——均为净化机制自身的测试，**非 secret**。
  - `secret/password`：logging_config.py:140-146 `redact()` 助手、hub_types.py:41-57 `_SECRET_RE` 清洗器、config.py:781 URL password 拒收校验——防御机制本体。
  - `salt`：client_id_salt 仅 env 输入无默认值（config.py:574），仓内零字面量。
  - stunnel.conf:9-10：证书/私钥**路径**（/etc/stunnel/*）非密钥本体。
  - 隐私注记（非 secret）：tests/fixtures/msg40.json 与 tests/golden/sessions-global-real-v1.18.18.json 含真实采集的会话数据（本地路径 `/home/mar/...`、reasoning 文本）——单用户自有数据、golden 测试设计使然，无第三方/凭证数据。
- **log 记录敏感 query 评估**：access 行只记 `path`（access_log.py:333-364，无 query_string）——`?search=` 内容与 directory 值**不入任何日志**；应用日志侧（journald）无 query/directory 值输出（banner 只记 allowlist 条数，app.py:117-126；qp_sweep 不落目录名）。**通过**。

### 2.9 T9 — 依赖面（接口层清单，转 A15 深化）

4 直接运行时依赖（pyproject.toml:10-15）× 攻击面接触点：

| 依赖（venv 版本） | 接触点（本仓语义） |
|---|---|
| fastapi 0.139.2（+starlette 1.3.1、pydantic 2.13.4 传递） | 全路由装配与 path/query 参数绑定（`{sid}`=`[^/]+` 即 F-253/F-254 的入口语义）；RequestValidationError 422 默认形（`{"detail":…}` 非编码形，F-025 关联）；默认 docs 路由（F-137）；ServerErrorMiddleware 最外层裸 500 渲染（F-023/F-253 出口） |
| httpx 0.28.1（+httpcore 1.0.9、h11 0.16.0） | 上游唯一客户端：URL 解析/合并语义（`?` 分隔、控制字符 InvalidURL——F-253/F-254 载体）、自动 gzip/deflate 声明与解码（T3 cap 口径）、连接池 32 上限（T3 并发界）、`follow_redirects=False`（upstream.py:45） |
| orjson 3.11.9 | 全 JSON parse/dumps：深度上限、64 位外整数→float、NaN/Inf 拒（T4） |
| uvicorn 0.51.0 | ASGI 入口：path 百分号解码进 scope（F-253/F-254 前提）、h11 头尺寸上限（LEI/header 轮廓界）、host 绑定执行者（E-II 0.0.0.0 的落地点 app.py:777）、无内建限速 |

无锁文件/无版本哈希钉死/`>=…,<1` 宽窗口——供应链定级归 A15。

---

## 3. E-II 高危清单（全部「部署边界未验证」）

| # | 条目 | 编号 | 说明 |
|---|---|---|---|
| 1 | 未认证全功能明文面（0.0.0.0:4097 × allowlist 默认 None × F-252 覆盖缺口） | **F-251（新）** | 任意目录文件读（经上游 /file 族）、跨目录聚合、全写路由、条件性 actions exec；唯一屏障=Tailscale ACL（不可验证） |
| 2 | providers v3 密钥透传在 E-II 免认证可达 | **F-017（升级）** | `?v=3` 即得 provider api/key/env/options 全集；E-II 维度定 HIGH |
| 3 | openapi.json/docs 免认证 schema 暴露（次级） | F-137（补充定级） | E-II=MEDIUM 侦察面 |

高危（HIGH/P1）计 **2 条**（F-251、F-017-E-II 维度），均依赖 ACL 缺失才可达——若 ACL 如文档所述生效则降为受信面问题。

## 4. 新发现 / 更新发现索引

| 编号 | 状态 | 严重度 | 标题 |
|---|---|---|---|
| F-251 | draft | P1（E-II 专属；部署边界未验证） | E-II 明文无认证全功能面 |
| F-252 | draft | P2（E-I/E-III 下 P3） | directory allowlist 覆盖面不完整 |
| F-253 | draft | P3 | 路径参数控制字符→httpx InvalidURL→裸 500 |
| F-254 | draft | P3 | %3F 路径参数→上游 query 注入（低实效） |
| F-255 | draft | P3 | cap=解压后字节：upIn 记账失真 + raw 族无准入最坏缓冲（cgroup 兜底） |
| F-017 | 更新（A8 分层定级） | P2（E-I/E-III）/ HIGH（E-II） | providers v3 透传密钥面 |
| F-023 | 更新（A8 补证据+安全角度） | P3 维持 | WS 501 不记账 + ServerErrorMiddleware 500 字节绕过计数（与 F-253 交叠：裸 500 不可观测） |
| F-137 | 更新（A8 安全定级补充） | P2 维持（E-II 维度 MEDIUM） | FastAPI docs 穿透 |

## 5. 负向结论清单（审计过且判定无问题，供 Phase 3 复核）

1. 客户端头整集转发不存在（白名单三类，§2.1）；CRLF 注入面在 validate_directory/request-id 门双重关闭。
2. CL/TE 走私结构上不可达（§2.1）。
3. orjson/stdlib-json 深度炸弹被 parser/版本语义拒绝（§2.4，实测记录）。
4. SSE 双账本+replay 三维+域惰性创建=客户端不可直接膨胀的内存界（§2.5）。
5. 错误体/metrics/health/access-log 四面零路径/schema/allowlist/query 泄漏（§2.6）。
6. SSRF host 不可控（base_url 钉死+config 校验）（§2.7）。
7. tracked 全集 0 真 secret；敏感 query 不入日志（§2.8）。
8. 上游 file 族双重 containment（词法+realpath）——子树约束权威在上游，sidecar 无需复制（§2.7）。

## 6. 转出

- F-253/F-254 的 uvicorn 解码前提、F-255 的上游 gzip 行为：标注「推导级/未实机」——Phase 3 V2 自我证伪若获实机窗口可各花 1 个探针验证。
- 依赖供应链（锁文件/钉版本/CVE 面）→ A15/D15。
- E-II ACL 现场核验（Tailscale 规则与实际 unit 的 host 行）→ 运维动作，超出本审计只读边界。
