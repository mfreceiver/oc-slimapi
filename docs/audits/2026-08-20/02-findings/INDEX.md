# 发现索引（02-findings/INDEX.md）

> Phase 3 终态（V1/V2 全量复核后机械重建）。状态 ∈ {confirmed, refuted, unverified_due_to_blocker}。
> 冲突消解注记：F-013 以 D06（P3）为准；F-015 以 D09（P2）为准；F-137 P2=E-II 维度 MEDIUM（A3/A8 合流）；F-016 P3（A5 证伪，3.11 残留面）；F-201↔F-271 同主题归并计数；F-151↔F-125、F-102↔F-155 锚点重叠防重复计数。

**统计**：共 173 条——状态：confirmed 170、refuted 3；严重度：P1 4、P2 19、P3 150

| 编号 | 状态 | 严重度 | 类别 | 标题 |
|---|---|---|---|---|
| F-001 | confirmed | P1 | defect | 幽灵事件类型 permission.resolved/permission.v2.resolved——上游实为 permission.replied，真实决议事件被 catch-all 静默丢弃 |
| F-002 | refuted | P3 | defect | 幽灵事件类型 message.appended 为永久死码（上游 88 型事件全集不存在该类型） |
| F-003 | refuted | P3 | defect | normalize_session_status 裸字符串分支永不触发（上游 session.status 恒对象信封） |
| F-004 | confirmed | P1 | contract | deploy 残留 OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS=2,2 与钉死 (3,4) 冲突——按模板部署即启动 RuntimeError crash-loop；operations.md:92-94 声称已清理构成矛盾 |
| F-005 | confirmed | P3 | ops | deploy 残留废弃 env OC_SLIMAPI_SERVER_API_VERSION=2（warning+忽略）+ config 死错误路径点名旧 env |
| F-006 | confirmed | P1 | defect | merged 预算组合退化：默认 max_message_bytes=32MiB > merged_max_bytes=8MiB 时 fanout 确定性退化为每页最多 inline 1 条且其余候选永不重试（测试 pin 小 max_message_bytes 掩盖生产参数组合） |
| F-007 | confirmed | P2 | defect | app.py 关停回调 _stop_qp_sweep 无 try/except 隔离——抛错将跳过其后全部 LIFO 清理（违反 app.py:221-223 声明） |
| F-008 | confirmed | P2 | defect | access-legacy-*.jsonl.gz 迁移档案永不清理（glob 命中但 _ACCESS_LOG_RE 拒配 → continue） |
| F-009 | confirmed | P2 | defect | traffic snapshot 清理仅挂靠 access-log 维护循环——ACCESS_LOG_ENABLED=false 时快照目录无限增长且无告警 |
| F-010 | confirmed | P2 | ops | 关停链最坏时长（uvicorn 5s + 维护排水 30s + dbaux 5s 级）> systemd TimeoutStopSec=15 → SIGKILL 截断最终快照与清理链 |
| F-011 | confirmed | P2 | defect | HubRegistry 宽限拆除 task 失败后 _removal_task 残留 → 后续 arming 永久失效（grace 拆除面失守） |
| F-012 | confirmed | P3 | defect | dbaux not_found 重探路径必 AttributeError（状态机未定义转移） |
| F-013 | confirmed | P3 | defect | Last-Event-ID 超长 seq 数字串（>4300 位）int() 抛 ValueError 未捕获 → 500（非 400） |
| F-014 | confirmed | P3 | defect | global_hub 非 dict JSON 逃逸 publish 会拆整条上游连接（上游异常形状的放大面） |
| F-015 | confirmed | P2 | risk | GlobalHub qp_last_activity 表无界（5 张有界 sid 表之外的例外） |
| F-016 | confirmed | P3 | risk | Python≥3.11 wait_for(semaphore.acquire()) 取消竞态可能永久泄漏 transform 许可 |
| F-017 | confirmed | P2 | security | providers v3 透传面对消费方暴露上游 api/key/env/options 字段全集（对照 E8 真值）——敏感信息经 sidecar 中转 |
| F-018 | confirmed | P3 | quality | pyproject 测试依赖 respx 全仓零引用（死依赖） |
| F-019 | confirmed | P3 | docs | actions 两路由的权威 wire 规范留存于 v2-contract §2（历史契约），v3/v4 契约仅收编清单提及 |
| F-020 | confirmed | P3 | docs | 文档链式漂移：develop.md 钉值过时（3,3 vs 实际 3,4）、check_routes_doc L311 修复提示指向已退役 v2-contract、measure_token_overhead 代码锚点失效（sse_frame 已迁出 hub.py:110） |
| F-021 | refuted | P3 | defect | /full/{mid} 上游 404 误映射为 session_not_found（携带 sessionID）——消息缺失与会话缺失语义混淆 |
| F-022 | confirmed | P3 | gap | sessions v4 degraded 503 观测位不完整——列表 fallback worker 内 503 与单查族全部 503 均未写 slimapi_degraded_503 标记 |
| F-023 | confirmed | P3 | gap | 记账盲区：WS 501 完全不记账；ServerErrorMiddleware 的 500 响应字节绕过计数 |
| F-024 | confirmed | P3 | smell | 死代码/只写不读状态合集：_busy_sids（tokenstream）、last_touch（replay_log）、recycle 近 no-op、directory_source（qp_sweep）、strip_hop_by_hop（upstream.py:49-111 生产零消费者）、build_sessions_query dead import、_V4_PARENT_RESERVED 未使用 |
| F-025 | confirmed | P2 | contract | sessions v4 limit 参数双形状：501-1000 走 422 param_version_mismatch 而 >1000 另形——与 §4.1 参数矩阵 1..500 的边界组合待契约对照 |
| F-026 | confirmed | P3 | smell | gzip_util.json_response 绕过 MIN_GZIP_BYTES/收益门——三路错误构造的全部错误体在小体时仍被压缩 |
| F-027 | confirmed | P3 | defect | dbaux allowlist 谓词不 strip 而 cursor 指纹 strip——同 directory 不同指纹组合可产生 400/false-accept |
| F-028 | confirmed | P3 | risk（原 | dbaux NULL/非 int time_updated 行的 keyset 翻页封闭性破口（同秩 tie-break 失效路径） |
| F-029 | confirmed | P3 | risk | mtime 变化引发 dbaux 频繁 swap 清零熔断统计（inode/mtime 双探测的 mtime 支路过敏感） |
| F-030 | confirmed | P3 | quality | tokenstream 两处 TODO（hub.py:663/:760）properties part/fields key casing——上游真值已定论 camelCase，TODO 可解除但未解除（维护债而非行为错误） |
| F-101 | confirmed | P3 | quality（代码注释漂移；兼 | versions.py 模块 docstring 残留「nine-ID readiness gate」——修订二后实为十 ID，代码注释与冻结契约 §3.3 漂移（wire 正确） |
| F-102 | confirmed | P3 | contract（轨一内部冲突，文档级） | v4-contract §3.2 将 `allowlist:{enabled}` 表述为「v4 视图新增瞬态字段」——该键自 3.3.0 起已在 v3 视图存在且实现两视图无条件发出（两契约节表述互斥，文档级） |
| F-121 | confirmed | P2 | gap | v4 sessions 全局面对多工作目录客户端的能力缺口——per-directory 服务端过滤无 v4 等价物，且 §17 永久 non-goal 堵死服务端补齐路径 |
| F-122 | confirmed | P3 | risk | SSE resync reason 值域封闭性无运行时强制——route 层直发 resync 帧不经 V4_RESYNC_REASONS 门控，log 层新增第五 reason 会未经门控直上 wire |
| F-123 | confirmed | P2 | docs | INTERFACE_MAP.md 全局头仍声明「v3-only 终态」「v=4 不支持 supported:[3]」——与 4.0.0 起 (3,4) 双版本窗口实现矛盾 |
| F-124 | confirmed | P3 | docs | CLIENT_CHANGES.md（ocdroid 对接权威清单）无 v4 迁移章节——滞后于 v4 发布面 4 个版本 |
| F-125 | confirmed | P3 | docs | v3-contract.md §2 表/§3「3.0.0 起 available:[3]」行未随 (3,4) 窗口更新——头部 2026-08-19 注记与正文冻结行不一致 |
| F-126 | confirmed | P3 | risk | v3→v4 规范耦合量化——v4-contract 67 行 v3 引用/13 处显式继承表述 + 12 个双视图测试文件的等价性对照依赖，构成任何未来版本窗收窄的字面化前置工作量锚点 |
| F-136 | confirmed | P3 | smell（兼 | shell/PTY deny-list 死配置残余——`shell_deny_list_enabled` 旋钮无实现可关、启动日志仍广播、INTERFACE_MAP 假 Ops 行 |
| F-137 | confirmed | P2 | defect（装配缝隙，兼观测哨兵破坏/信息暴露面） | FastAPI 默认 docs/openapi 路由穿透——`/docs`、`/redoc`、`/openapi.json`、`/docs/oauth2-redirect` 以 200 落 passthrough 桶 |
| F-138 | confirmed | P3 | smell | 死符号群——SELECTOR_V2/SELECTOR_ABSENT 常量、hub shim 的 `_LAST_UPDATED_AT_BY_SID_MAX` 死 re-export、global_hub 死 `import logging`、hub_types 死 `logger` |
| F-139 | confirmed | P3 | docs | 3.0.0 终局后的观测口径文档漂移——passthrough「未省流查询/透传基线」教学已失真 + sse_observability dim 列表漏 v4 |
| F-140 | confirmed | P3 | risk（机制设计安全，价值面悬置） | qp_sweep 阶段 1 shadow 价值悬置——metrics 暴露零消费方、阶段 2 无排期无取消、`sweep` 桶恒空、`directory_source` 生产死参 |
| F-141 | confirmed | P3 | smell（结构债） | tokenstream/frames.py 复制 `sse_frame`/`_now_ms` 的「import 环」论证失效——hub_types 叶子化后可直连去重 |
| F-142 | confirmed | P3 | contract（契约一致性缝隙，非行为错误） | proxy 终局边界两缝隙——域外方法（TRACE/CONNECT）得 Starlette 裸 405 非 coded 体；WS 501 为「accept 后 1011 close」非握手期 501 |
| F-151 | confirmed | P3 | contract | v3-contract §2/§3/§3a 冻结文本仍载 supported:[3] / [3,3]，未随 (3,4) 双版本窗口加注——轨内文本与现行实现 [3,4] 漂移 |
| F-152 | confirmed | P3 | contract | 405 错误体 code `method_not_allowed`（非 GET /slimapi/versions）无契约归宿——三契约（v2/v3/v4）与 CHANGELOG 均未载 |
| F-153 | confirmed | P3 | contract | WebSocket 501 stub code `websocket_not_supported` 无契约归宿——v3 §8.2 catch-all 关闭条款未提 WS 面 |
| F-154 | confirmed | P3 | contract | actions 第 8 码 `invalid_request_body`（422 POST body 畸形）不在 v2-contract §2 七码表内——三契约皆无的主码 |
| F-155 | confirmed | P3 | contract | v4-contract §3.2 `allowlist` 字段位置措辞歧义——行文可读作根级字段，实现嵌套于 `features.allowlist`（沿 v3 §3a） |
| F-156 | confirmed | P3 | test | v3-contract §11 测试矩阵三处滞后/偏差——11.11 对象已删除、11.16 grep 负向断言未落地、矩阵整体为 2.0.0 门控快照未标注 |
| F-157 | confirmed | P3 | docs | CLIENT_CHANGES.md 时效性滞后五族——权威指针指向已退役 v2 契约、已删除头/信封头、token stream gzip 例外、truncated/partEventRevision 语义 |
| F-201 | confirmed | P2 | defect（性能/事件循环阻塞，违背 | messages 列表/merged 200 尾部 gzip level-6 与 ETag sha256 在事件循环上执行（B1 把压缩移出 worker 的连带副作用） |
| F-202 | confirmed | P3 | risk（事件循环阻塞，结构性无 | read_passthrough 尾部 sha256 判定 + gzip 在事件循环上执行（raw 路由无 admission、cap 64MiB） |
| F-203 | confirmed | P3 | smell/risk（事件循环上的序列化+哈希+压缩；现实量级小） | sessions v3/v4 响应尾 orjson.dumps 双跑 + sha256 + json_response gzip 在事件循环上（envelope 小、纪律不一致） |
| F-204 | confirmed | P3 | smell（事件循环压缩；现实量级小） | write_groups POST 回显 gzip 在事件循环上执行（回显体小） |
| F-205 | confirmed | P3 | risk（事件循环上的同步文件 | access-log DailyAccessHandler.emit 每请求同步 write+flush 在事件循环上（含跨午夜 rollover 的 close/open） |
| F-206 | confirmed | P3 | smell/risk（事件循环上的同步文件 | TrafficSnapshotter._write_once 同步 open+write 在事件循环上（周期 tick + 关停终帧；与维护循环 to_thread 纪律不一致） |
| F-207 | confirmed | P3 | risk（事件循环上的 | candidate_canonical 每判定执行 os.path.realpath（非缓存）在事件循环上——安全 by-design，量级与调用频率记录 |
| F-208 | confirmed | P3 | defect（去重效率缝；无正确性影响） | catalog 同 key straggler 排队超 1s grace 后重复刷新上游（refresh 不复查 lookup；默认 transform_wait_seconds=2s > grace 1s 使窗口现实可达） |
| F-209 | confirmed | P3 | defect（生命周期不变式；无行为影响） | catalog 迟到 leader 的 _store 写回已 shutdown 的缓存——"shutdown 清空"不变式可被打破（CD-1 语义下无害） |
| F-210 | confirmed | P3 | defect（防御纪律缺口；无 | plain `_fail` 按 key 删除缺身份校验——shutdown 交错下旧 leader 失败可误删新一代 entry（去重瞬时丢失；复核降级 P3） |
| F-211 | confirmed | P3 | smell/defect（时钟域不一致；test-only | grace timer 时钟口径混用——plain 用注入 clock 差值喂 loop call_later 且到期用注入 clock 复检；leased 直接绕开注入 clock（生产正确、测试注入面缺口） |
| F-212 | confirmed | P3 | risk（公平性退化；正确性不变） | absorb 重试失去 FIFO 排队位（TransformBusy 后重排队尾）——持续负载下重试者可被反复超越直至预算耗尽 |
| F-213 | confirmed | P3 | risk（资源占用面/去重效率；正确性不变） | join-first lease 跨 transform admission 等待持有 raw_fetch 预算——默认单 flight 预算下独占至 offload 完成（去重效率面，非死锁） |
| F-214 | confirmed | P3 | risk（运维/关停正确性预算） | 关停排空预算与 systemd TimeoutStopSec 不匹配——最坏 45s+ 超 15s 上限，SIGKILL 截断尾部回调（最终快照/access-log flush 丢失） |
| F-215 | confirmed | P3 | risk（保留界语义澄清；无内存上界破坏） | plain `fulls` 在飞数可超 64 条数上限——merged fan-out（≤16）+ 并发 direct /full 叠加下条数界对在飞不成立（仅 grace 保留损失；字节上界仍强制） |
| F-216 | confirmed | P2 | defect（observability） | catch-all 丢弃零观测——76 型上游真实事件静默丢弃，无 per-type 计数/日志，事件集漂移不可检测 |
| F-217 | confirmed | P3 | defect | 上游 SSE 流 EOF 时未终结 data 块（无尾空行）静默丢失 |
| F-218 | confirmed | P3 | observability | emitted_frames_total 计「投递尝试」而非「成功投递」——put() bool 返回值被全局忽略 |
| F-219 | confirmed | P3 | defect | _check_pending_budget 的 had_subs 全局口径——无订阅 sid 的 pending 溢出仅 force-flush 静默丢帧、不逐出不回收（docstring 与实现语义偏差） |
| F-220 | confirmed | P3 | defect | ttl_sweep 退役不写 replay barrier 不发任何帧——与 idle/evict/delete 三处状态失效源不一致 |
| F-221 | confirmed | P3 | risk | 上游 SSE 读超时 read=None——半开连接不可检测，僵死窗口无界 |
| F-222 | confirmed | P3 | defect | hub 侧 stop_after_grace 触发后 stop_task 残留 done 引用——unsubscribe 的 not stop_task 守卫使第二次宽限永不武装 |
| F-223 | confirmed | P3 | risk | _part_revisions LRU cap 逐出后同 key revision 从 0 重计——严格 `>` 客户端丢后续帧；防线为跨模块隐式断言，debug override 只校验一项 |
| F-224 | confirmed | P3 | quality（结构性腐化风险） | 双哨兵/双实现族（STOP×2、sse_frame×2、_now_ms×2）无一致性防护——跨体系误用即 TypeError / 字节漂移 |
| F-225 | confirmed | P3 | risk | token flush_loop 看门狗重建无退避/预算——flush() 确定性异常 + 有消费者 = 10Hz CRITICAL 无限重建刷屏 |
| F-226 | confirmed | P3 | risk | events_tap / events_tokens 双容器账本，清理完全依赖 SSE generator finally（Starlette aclose 外部前提）——失守即 flush loop 永转 + grace 永不挂 |
| F-227 | confirmed | P3 | quality（死状态） | _busy_sids 生产零读者死状态——每次 session.status 事件多付一次 prune |
| F-236 | confirmed | P3 | defect | stop() 的 close submit 无界等待——在途查询可把关停拖出 drain 预算 |
| F-237 | confirmed | P3 | defect | 关停窗口非 sqlite 异常逃逸 → 500（close 后 run_query assert / executor 已 shutdown） |
| F-238 | confirmed | P3 | defect | circuit_open 期间 inode/mtime 校验被早 return 跳过 + SELECT 1 探针不触表——恢复后 ≤30s 读旧库窗口 |
| F-239 | confirmed | P3 | risk（设计冻结口径内的弱点，非实现偏离） | 半开探针单样本即可闭合熔断 + SELECT 1 与投影延迟不同源——恢复证据弱、可振荡 |
| F-240 | confirmed | P3 | defect | inode 基线捕获双缺陷——stat 失败永久盲 + open→stat 间隙换库记新 marker 掩盖旧连接 |
| F-241 | confirmed | P3 | defect | explicit-env 相对路径未拒（连带 XDG_DATA_HOME 相对值未校验）——file: 相对 URI 按进程 cwd 打开 |
| F-242 | confirmed | P3 | defect | 候选 glob 不滤目录——`opencode*.db` 目录名计入候选可致 open_failed 30s 无效重探循环 |
| F-243 | confirmed | P3 | risk | LatencyBreaker 跨线程无锁——worker record vs 事件循环 prune/reset/snapshot 的 metrics 瞬时失真 |
| F-244 | confirmed | P3 | risk | snapshot() 公开返回明文 DB path——未来消费方泄面跟踪点 |
| F-245 | confirmed | P3 | quality | stop 后 start 不可复用无守卫——executor 已 shutdown 后首次 submit 即 RuntimeError |
| F-246 | confirmed | P3 | quality | dbaux 卫生合集——死代码与导出面三策略漂移 |
| F-247 | confirmed | P3 | risk | cursor 解码无长度上限——事件循环内 b64decode+json.loads 的 CPU/内存面 |
| F-248 | confirmed | P3 | defect | `//` 前缀路径经 file://authority URI 语义静默丢首段——UNC 风格手配路径错库 |
| F-249 | confirmed | P3 | defect（test-only | _expanduser 注入 home 语义分歧——`~bob` 在测试注入下产出缺分隔符错误路径 |
| F-251 | confirmed | P1 | security（部署组合面） | E-II 明文无认证全功能面——0.0.0.0:4097 × directory_allowlist 默认 None × sidecar 零认证/零授权（部署边界未验证） |
| F-252 | confirmed | P2 | security/gap | directory allowlist 覆盖面不完整——仅 file 三路由 + SSE 帧过滤 + v4 sessions 降级矩阵受控，其余 directory 敏感路由不查 allowlist |
| F-253 | confirmed | P3 | defect（输入鲁棒性；跨 | 路径参数含解码控制字符（%0A/%0D/%09）→ httpx.InvalidURL 未捕获 → 裸 500（全 f-string 上游转发族） |
| F-254 | confirmed | P3 | security（参数注入，低实效） | %3F 路径参数解码后经 f-string 进上游 URL → httpx 以 `?` 为分隔符 → 上游 query 注入（含 directory/workspace 注入语义；实际影响被上游优先级对冲） |
| F-255 | confirmed | P3 | risk（语义记录 | read_with_cap 的 cap 语义=httpx 解压后实体字节——upIn 记账按解压后口径失真 + raw 透传族无 transform 准入的最坏并发缓冲（MemoryMax cgroup 兜底） |
| F-271 | confirmed | P2 | defect（性能） | messages 列表尾部 ETag sha256（1-2 次）+ gzip-6 在事件循环上执行——大 identity 页面停摆全部 SSE 订阅者 |
| F-272 | confirmed | P3 | risk | ReplayLog per-sid 域外壳（shell）epoch 内永不删除——sid 键基数无 cap |
| F-273 | confirmed | P3 | defect | QpSweepShadow 30 天逐出被 ingest 先行刷新 seen_at 结构性击穿——三张镜像表随 qp_last_activity 有效无界 |
| F-274 | confirmed | P3 | risk | 上游 httpx limits 32/16 硬编码不可调；无 admission 并发面（merged 8 + q 8 + p 8 + 写 + SSE 1）≈25-30 贴近上限 |
| F-275 | confirmed | P3 | risk | 上游 SSE 连接 read=None 无读超时——半开上游连接永久检测不到，帧静默丢失直至 TCP 死亡 |
| F-276 | confirmed | P3 | risk（运维） | lifespan 启动日志维护（migrate/compress/prune）同步阻塞事件循环且无超时上限——大积压延迟启动 |
| F-277 | confirmed | P3 | risk（运维） | 启动冒烟 2×5s + /global/health 5s 串行——上游 hang（非 refused）时服务就绪最坏延迟 ~15s |
| F-278 | confirmed | P3 | gap | v4 sessions SQL 索引假设仅靠手工 eqp_matrix.py 实证——真库索引漂移无 CI 门禁，只靠运行时熔断兜底 |
| F-279 | confirmed | P3 | risk | merged 请求全程持有 raw-fetch lease（跨 fan-out + phase C admission 等待）——默认预算仅容 1 flight，慢 merged 页阻塞其余全部列表去重 |
| F-286 | confirmed | P3 | smell（重复代码） | permissions.py ↔ questions.py 全模块孪生——~89% 结构同一、~320 行重复（含 121 行逐字节调度器 ×2） |
| F-287 | confirmed | P3 | smell（重复代码） | read_groups 两条 v4 分支管线再实现共享读链骨架 + write_groups archive 自读 body 循环同款复制 |
| F-288 | confirmed | P3 | smell（重复 | messages.py 本地复制 `_busy_response`/`_stream_upstream`/`TRANSFORM_RETRY_AFTER_SECONDS`，且其流式变体不转发 X-Request-ID（与 catalog 链行为漂移） |
| F-289 | confirmed | P3 | docs/quality（契约层积；实现无走样） | 同码错误体字段命名层积——`response_too_large` 双字段名（`limit` ×13 vs `limitBytes` ×1）+ snake_case/camelCase body 字段并存（均契约冻结） |
| F-290 | confirmed | P3 | smell（死代码） | token_stream.py gzip 残链死代码——`_accepts_gzip` 零调用、`use_gzip=False` 常量死条件、compressor/encode gzip 分支不可达、gzip 计数器恒 0 上报 |
| F-291 | confirmed | P3 | risk（内存界不一致；上游异常形状放大面） | 上游错误体排水策略三轨并存——catalog/discovery 链 `aread()` 无界排水 vs read/write groups 链 cap 保护（§10.a:141 只覆盖一半） |
| F-292 | confirmed | P3 | quality（卫生合集；单项均零行为影响除特别注明） | A10 卫生合集——global_hub dead import `logging`、messages 陈旧 getattr 回退 8MiB（死默认）、`_strip_directory_query` 逐字节 ×2、resolve/validate stanza ×10、Retry-After 字面量 "5" ×2、无静态门禁 |
| F-301 | confirmed | P2 | design（模块化/上帝文件，A11 | sse/tokenstream/hub.py 2190 行上帝文件——单一类 59 方法承载累积/预算/flush/fanout/tombstone 五族职责 |
| F-302 | confirmed | P2 | design（模块化/上帝文件，A11 | routes/messages.py 1643 行混合三族端点职责（list 投影 / full 合并 / expand 提取器） |
| F-303 | confirmed | P3 | design（模块化，A11 | config.py 1158 行混载三类非 env 职责（TOKEN_* 常量 / allowlist 匹配族 / 420 行 validate） |
| F-304 | confirmed | P2 | design（重复代码，A11 | questions.py 与 permissions.py 复制粘贴双维护（归一化相似度 0.832、302 行匹配块）——修复已出现单边遗漏风险 |
| F-305 | confirmed | P3 | design（耦合度量，A11 | app.state 服务定位器 25 键隐式契约面（21 个文件读取、3 个私有键仅靠下划线约定） |
| F-306 | confirmed | P3 | verification（A11 | 分层单向性验证通过——0 反向依赖、0 环（正向结论）+ 2 条边界注记 |
| F-307 | confirmed | P3 | design（拆分收尾欠账，A11 | 兼容 shim 未退役——sse/hub.py 与 sse/token_hub.py 仍被 src 主路径消费，双导入路径长期并存 |
| F-308 | confirmed | P3 | design（上帝文件，A11 | global_hub.py 1090 行单类 34 方法，publish() 巨方法 ~320 行 if/elif 事件分发链 |
| F-309 | confirmed | P3 | design（耦合度量，A11 | app.py 组合根变更频率全仓第一（27 次/19 天）——lifespan 单体 600 行吸收所有组件接线 churn |
| F-310 | confirmed | P3 | verification（A11 | skeleton.py 1177 行「保持」论证记录——纯函数投影库零扇出、按投影对象自然分节（数据驱动 keep 判定） |
| F-311 | confirmed | P3 | design（A11 | sessions.py 883 行 v3/v4 双投影路径并存——保持至 v3 退役窗口再减半（条件性 keep） |
| F-312 | confirmed | P3 | verification/analysis（热点-缺陷关联） | 变更热点×缺陷关联分析——questions.py 重复缺陷聚集、proxy.py 缩容后缺陷止血（结构性结论） |
| F-313 | confirmed | P3 | design（上帝文件，A11 | read_groups.py 名不副实——630 行承载 12 个端点 4 个资源域（file/vcs/providers/session-misc） |
| F-314 | confirmed | P3 | verification | hub_types.py 公共类型聚合点评估——扇入 4 全部限于 sse 域，聚合合理（正向结论） |
| F-315 | confirmed | P3 | design（A11 | dbaux/lifecycle.py 768 行三职责混载——纯函数错误分类 / LatencyBreaker 熔断器 / DbAuxiliarySource 生命周期状态机 |
| F-316 | confirmed | P3 | test | 35/53 路由的 v4 wire 面无任何 HTTP 级测试（?v=4 从未打到这些路由）；test-census「~35 路由全扫」声明口径失实 |
| F-317 | confirmed | P3 | test | 错误路径锁定大量依赖「家族代表」单端点断言——directory_conflict/multi-value/request-cap/response-cap 等仅在被 parametrize 覆盖之外的 1 条端点上断言 |
| F-318 | confirmed | P3 | test | TDD 脚手架注释声称的 xfail(strict=False) 标记实际不存在——两文件头部与用例 docstring 描述已失效的标记状态 |
| F-319 | confirmed | P3 | test | 恒真断言——test_traffic_ledger.py:588 `assert … or True` 使该行永不失败 |
| F-320 | confirmed | P3 | test | 错误体断言强度两极——selector/providers 族精确整body断言，路由级 5xx/502 大量只断 code 子集（217 处 code-only vs 73 处整body） |
| F-321 | confirmed | P3 | test | flaky 面复核——148 处真实墙钟 sleep（串行合计约 86s）+ 3 文件以 monotonic 计时参与行为断言（高负载假失败风险） |
| F-322 | confirmed | P3 | test | 写组 request_too_large 仅「超限侧」单边锁定——无 at-cap/低于-cap 边界测试（全仓唯一双侧 cap 边界测试在 actions POST） |
| F-323 | confirmed | P3 | test | 自定义 marker `@pytest.mark.integration` 未在 pyproject 注册——pytest≥8 下每运行产生 PytestUnknownMarkWarning，且 `-m integration` 选择语义无守护 |
| F-324 | confirmed | P3 | test | 孤儿 fixture——tests/fixtures/g_f1/（equal_ts_page1.json + README）全仓零引用，README 自述的配套文件不存在 |
| F-325 | confirmed | P3 | test | /slimapi/events 订阅上限错误的 HTTP 层映射（SubscriberCapacityError → 503 错误体）无路由级断言——仅 registry 单元级锁定 |
| F-326 | confirmed | P3 | test | 「byte/verbatim/frozen」命名的测试多数实为字段级断言——真·字节级回归仅 4 族；命名强度与断言强度系统性错位 |
| F-331 | confirmed | P3 | gap（observability） | 观测面无错误码维度——503/502/413 族内部多码不可区分，且 200 载部分失败完全不可见 |
| F-332 | confirmed | P3 | docs（observability | passthrough 桶语义失真——3.0.0 后桶内只剩被拒 404，省流审计口径「按 bucket==passthrough 找未省流」已失效 |
| F-333 | confirmed | P3 | gap（observability） | sweep 观测双盲——bucketize 死桶预留无路由 + metrics sweep 块三文档零记载 + getattr enabled 兜底方向反向 |
| F-334 | confirmed | P3 | ops（docs） | `GET /slimapi/metrics` 受 selector 管辖须带 `?v=`——operations.md §9 排障指引照抄即 400，监控/告警探针同坑 |
| F-335 | confirmed | P3 | docs（工具口径） | recordType 过滤陷阱——聚合函数 aggregate_v3_observability 自身不过滤，§9.4 对账节未重申，SSE 桶计数可被生命周期行放大约 3 倍 |
| F-336 | confirmed | P3 | docs（ops | retain 口径双偏差——「保留 N 天」实存 N+1 个日历日，且 access log 3 天 × snapshot 30 天窗错配使 §9.4 对账在 >4 天窗口系统性失真 |
| F-337 | confirmed | P3 | gap（observability） | 观测面自身健康零观测位——access-log handler disabled / snapshotter inactive / 维护循环停摆只有 journald warning（或完全静默），metrics 无健康块 |
| F-338 | confirmed | P3 | defect（observability） | sse_observability._emit 双 `except Exception: pass`——SSE 生命周期观测丢失零日志零计数 |
| F-339 | confirmed | P2 | ops（runbook | runbook 缺口汇总——operations.md 对 20+ 生产 env 零记载、503/degraded/订阅/重放/allowlist 场景零动作条目（19 条缺口清单） |
| F-340 | confirmed | P3 | defect（observability） | sessionsDegraded 计数器 best-effort 挂载失败静默丢计数——`ensure_sessions_degraded_counters` setattr 失败返回临时实例，degraded 观测恒 0 且无信号 |
| F-341 | confirmed | P3 | defect（observability | status=0/"0xx" 记账黑洞——app 未发 http.response.start 即正常返回时，行/矩阵出现 status 0 且零错误分类（异常路径有 or-500 兜底、正常路径无） |
| F-342 | confirmed | P3 | risk（privacy/observability） | clientId 隐私口径双弱点——无 salt 的 sha256 跨部署可链接 + `CLIENT_ID_HASH=false` 明文回退零告警零 ops 记载 |
| F-343 | confirmed | P3 | defect（observability | 桶归口径两处前缀陷阱——裸 `/slimapi/config` 落 other 桶（providers 语义丢失）、`/slimapi/sessionsfoo` 类过匹配归 sessions 桶（404 污染桶 errors4xx） |
| F-344 | confirmed | P3 | ops（unit | deploy unit 无 start-limit 覆盖、无 OnFailure/Watchdog 告警钩子——任何启动期致命错（含 F-004）以 ~2 次/10s 无限 crash-loop 且零通知 |
| F-345 | confirmed | P3 | quality（observability | 记账三段 best-effort 无对账——access log 行先于 ledger 写（单侧行/账本不一致窗口）+ ledger 段统一异常标签 "record_upstream failed" 误导排障 |
| F-346 | confirmed | P3 | docs | README.md 整体停留 v2 时代——权威指针指向已退役 v2-contract、quick-start 命令使用已删除头（照抄即 400）、catch-all/gzip 描述过时 |
| F-347 | confirmed | P3 | docs | AGENTS.md 入口索引 4.0.0 未跟进族——ACCEPTED_CLIENT_VERSIONS 写 [3,3]、v3-only 表述、check.sh 描述失实、「当前不读 SQLite」自相矛盾 |
| F-348 | confirmed | P3 | docs | operations.md 示例滞后族——health 期望响应 [3,3]/sidecar 1.1.1/缺 allowlist 键、§3.2 unit 示例缺两行、supported:[3] 口径未提 v=4 |
| F-349 | confirmed | P3 | docs | INTERFACE_MAP.md 描述列漂移族——banner/versions/health 行 [3]/[3,3]/current=3 未随 4.0.0 更新、metrics 行要求已删头、九处双值 Vary 与自身 banner 矛盾、catch-all 收编计数过期 |
| F-350 | confirmed | P3 | docs | traffic-accounting.md 版本窗口径文内不一致——头部/§2 写 supported:[3] 而自身 §5.1 字段表已载 wireVersion "4"/selectorResult v4 |
| F-351 | confirmed | P3 | docs | release.md 文件职责表与 §1.2 把 wire 契约权威仅指向 v3-contract，未列 v4-contract |
| F-352 | confirmed | P3 | docs | traffic-route-todo/children 设计稿「PROPOSAL — NOT IMPLEMENTED」状态横幅在路由实现收编后未回写 |
| F-353 | confirmed | P3 | docs | 死链族——v1-contract.md / v1-impl-spec.md（已删除文件的历史引用无「已删除」标注）+ chat-toolcard-investigation.md（跨仓文件无仓限定，含 src 代码注释 1 处） |
| F-361 | confirmed | P3 | quality(ops) | release.sh 中途失败窗口——commit 先于 tag 校验/打 tag，失败残留 release commit 无回滚 |
| F-362 | confirmed | P3 | gap | release.sh 版本一致性前置缺口——不校验 pyproject↔最新 tag、不预检 tag 存在、不验 [Unreleased] 折叠与日期 |
| F-363 | confirmed | P3 | docs | 质量门禁描述三处滞后低估——release.md/AGENTS.md 仍称「pytest 最小集、compileall 后续可选」 |
| F-364 | confirmed | P3 | quality | check.sh MODE 参数校验后置于全套检查之后 + `--full` 死别名 |
| F-365 | confirmed | P3 | quality | check.sh compileall 在 src/ 产生 __pycache__ 副产物无清理（树内残留实证） |
| F-366 | confirmed | P3 | gap | check_routes_doc 对账盲区 8 项合集（A14 消费）——反向漂移/语义白名单 7/54/collector 耦合等 |
| F-367 | confirmed | P3 | defect | eqp_matrix 真库 schema 兼容门失败静默 exit 0——无 else 分支、无 WARNING |
| F-368 | confirmed | P3 | risk（供应链/复现性） | 无 lockfile / 无哈希固定——7 依赖全范围解析，重装不可复现（fastapi 0.115→0.139.2 漂移已发生） |
| F-369 | confirmed | P3 | risk（前向兼容） | requires-python>=3.11 与 setuptools>=75 均无上界——venv 已 3.14，pytest-asyncio 3.16 断点告警已现 |
| F-370 | confirmed | P3 | gap | 测量资产无门禁联动——measure_token_overhead main() 恒 return 0、零测试引用、.md 数字快照无防漂移 |
