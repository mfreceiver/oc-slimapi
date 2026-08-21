# Wave 2 实施设计：性能 offload（F-201/F-271/F-202 族）— 评审包

> 状态：待评审（门禁 ≥9.0）。评审通过后本文档 §6 验收清单逐项执行。
> 依据：`docs/ocmar/plans/2026-08-21-batch3-full-rollout.md` §Wave 2（:71-80）+ N3 冻结规格；
> 发现详情 `docs/audits/2026-08-20/02-findings/F-201.md` / `F-271.md` / `F-202.md` / `F-203.md` / `F-204.md` / `F-205.md` / `F-206.md`。
> 基线：`520a3b5`（main）。行号以该基线为准。

## 0. 写域声明

计划冻结写域：`src/oc_slimapi/routes/messages.py`、read 路径（`src/oc_slimapi/routes/_read_passthrough.py`）、`src/oc_slimapi/gzip_util.py`（本设计**零改动**）、`src/oc_slimapi/sse/registry.py`（核查后无 skeleton 哈希，**零改动**）、`tests/`。

本设计**不新增任何 src 文件**（两个 worker 函数分别落在各自路由模块内，避免跨路由新依赖与循环 import——`etag.py` 不在写域且 `gzip_util.py` 反向 import `etag` 会成环）。单泳道串行执行，无并行写域冲突面。

## 1. F-201/F-271：messages 列表/merged 两尾部 → `pool.offload`

### 1.1 现状（两个同构尾部）

- lease 尾 `routes/messages.py:924-974`（`_messages_via_lease` 内，`async with lease:` 中、`async with pool:` 外）；
- 直连尾 `routes/messages.py:1120-1170`（`messages()` 内，`async with pool:` 块外）。

尾部在事件循环上执行的三段 CPU 工作：
1. `etag_mod.judge_conditional(identity, inm, rep_version, accept_encoding=…)`——全量 identity sha256（304 判定；`*`/gzip 候选分支可能再 sha256 一次）；
2. `compress_if_beneficial(identity, ae)`——gzip level-6（merged 页 identity 上界 8 MiB，≈130-400ms 停摆）；
3. `compute_etag(identity, actual, rep_version)`——200 尾第二次 sha256。

`messages.py:1559` 的 `compress_if_beneficial` 在 `_expand_fragment_worker` 内部（本已 worker 化）——**非整改对象**。`_merge_fulls_and_pack`（:727，经 :721 offload）亦已在 worker。故整改面**只有上述两个尾部**。

### 1.2 设计

在 `messages.py` 新增模块级 worker 函数（两条路径共用）：

```python
def _judge_pack_tail(identity, *, accept_encoding, if_none_match, rep_version):
    """(F-201/F-271) off-loop 尾部：304 判定 + gzip + 验证器。

    纯 CPU（sha256 ×≤2 + gzip-6），经 pool.offload 执行；输入全部为
    可序列化标量/bytes（worker 线程不触 request/scope）。返回
    ``(verdict, encoded, coding_headers, etag)``：
      verdict None → 200：encoded/coding_headers 就绪，etag 为 200 实际
        coding 的验证器（rep_version None 时 etag None——与现行
        etag_enabled=false 路径一致）；
      verdict "*" → 304：压缩一次取实际 coding，etag = 实际 coding tag；
      verdict str  → 304：etag = verdict（零压缩）。
    字节等价：三段调用与原尾部逐行同序、同输入、同函数。
    """
    verdict = None
    if rep_version is not None:
        verdict = etag_mod.judge_conditional(
            identity, if_none_match, rep_version,
            accept_encoding=accept_encoding)
        if verdict == "*":
            _, c = compress_if_beneficial(identity, accept_encoding)
            actual = "gzip" if "Content-Encoding" in c else "identity"
            return ("*", None, None,
                    etag_mod.compute_etag(identity, actual, rep_version))
        if verdict is not None:
            return (verdict, None, None, verdict)
    encoded, c_headers = compress_if_beneficial(identity, accept_encoding)
    etag = None
    if rep_version is not None:
        actual = ("gzip" if "Content-Encoding" in c_headers else "identity")
        etag = etag_mod.compute_etag(identity, actual, rep_version)
    return (None, encoded, c_headers, etag)
```

两条尾部改为（以直连尾为例；lease 尾同构）：

```python
verdict, encoded, c_headers, etag_val = await pool.offload(
    _judge_pack_tail, identity,
    accept_encoding=request.headers.get("accept-encoding"),
    if_none_match=request.headers.get("if-none-match"),
    rep_version=rep_version,
)
if verdict == "*":
    return etag_mod.not_modified_response(etag_val, vary_value, aux=None)
if verdict is not None:
    return etag_mod.not_modified_response(verdict, vary_value, aux=None)
final_headers = dict(c_headers)
final_headers["Vary"] = vary_value
if etag_val is not None:
    final_headers["ETag"] = etag_val
return Response(encoded, status_code=200, media_type="application/json",
                headers={**base_headers, **final_headers})
```

- 请求头（accept-encoding / if-none-match）与 `rep_version`/`vary_value` 在 loop 上取值后作参数传入——worker 不触 request 对象。
- Response 构造、headers dict 组装顺序与现状完全一致（wire 字节面不变）。
- `rep_version is None`（etag 关）路径：worker 内 judge 跳过、etag None——与现行一致。

### 1.3 admission 语义分析（不持 admission 的 offload）

尾部 offload 发生在 transform admission 释放**之后**（维持现状控制流；若改为先取 admission 再做尾部，会在池饱和时把原本能 200 的已投影请求变成 503 `transform_busy`——那是 wire 行为变化，违反字节等价门）。

- **无死锁**：offload 即 submit+await；每个调用方在途 job ≤1；纯 CPU 必然完成。
- **无新 TransformBusy 路径**：`transform_busy` 仅产生于 admission 域（`pool.acquire` 超时 + messages `/full`、expand 的 absorb 预算耗尽 `raise TransformBusy()`，messages.py:1240/:1608）；尾部 offload 不经 acquire、offload 本体不抛 TransformBusy（transform.py:261-274）。副作用仅为：尾部 job 与在册 transform job 共享 executor（max_workers=max_transforms），饱和时在册请求的 offload 排队时延增加（其 admission 持有时间随之拉长）。对照现状：同一 gzip 工作原本在事件循环上冻结全部协程（含 SSE 心跳）——迁移后 loop 不再停摆；503 面的理论增量来源是：现状下 on-loop 冻结期间 `asyncio.wait_for` 定时器同样冻结（admission 等待时钟不走），迁移后等待真实流逝，极端饱和下 acquire 超时才真正可能触发；503 形状不变。
- **仓内先例**：无 admission 的 offload 已有先例——`routes/questions.py:260-269`、`routes/permissions.py:278-286`（均附论证注释）。`transform.py:264-266` offload docstring 的「queueing naturally bounded by admission」描述随本改动进一步偏离，transform.py 不在 W2 写域，docstring 修正记入 follow-up backlog。
- **关停**：尾部 offload 与既有投影 offload 同暴露面（pool.shutdown cancel_futures），无新类目。

## 2. F-202：read_passthrough 尾部 → 阈值 + `asyncio.to_thread`

### 2.1 现状

`_read_passthrough.py:245-277`：`judge_conditional`（sha256）→ `*` 分支压缩 → `compress_if_beneficial`（gzip-6）→ `compute_etag`，全在 loop。raw 路由（`project=None`）按 §10.a 冻结**不占 transform 池**（:39-41 模块注释），body 上界 64 MiB。

### 2.2 设计

- `_read_passthrough.py` 新增同构 worker `_tail_encode(body, *, accept_encoding, if_none_match, rep_version)`——逻辑逐行镜像现尾部三段（含 `rep_version is not None and body` 的空体守卫与两处条件），返回同 §1.2 四元组。
- **通道选择**：`asyncio.to_thread`（**非** pool.offload）——raw 路由不得占用/排队 transform 池（§10.a admission 冻结）；to_thread 走默认 executor，与维护路径（access_log compress/prune、snapshotter 建议）同纪律。
- **import 形态冻结（M4）**：实现须 `import asyncio` 并以属性调用 `asyncio.to_thread(...)`（运行时模块属性查找）——`from asyncio import to_thread` 会使测试的 `monkeypatch.setattr(asyncio, "to_thread", …)` spy 拦截失效。
- **阈值**：`_TAIL_OFFLOAD_MIN_BYTES = 1 << 20`（1 MiB）。`len(body) >= 阈值` → `await asyncio.to_thread(_tail_encode, …)`；否则直接同步调用**同一函数**（inline）。依据：gzip-6 单核 ≈20-60 MB/s → 1 MiB ≈ 17-50ms（值得下放）；低于 1 MiB 全链（sha256 ≤0.7ms/MiB + gzip）为个位数 ms，executor 往返不划算（F-201 建议方向的「低配替代」阈值判别）。两分支调用同一纯函数 → 字节等价。
- Response 构造与 headers 组装（passthrough 集 + Vary 覆写 + ETag）留在 loop，逐行不变。
- **模块注释同步（M3）**：`_read_passthrough.py:39-41` 中 "hashing and gzip run inline like ``routes/sessions.py``" 一句随改动更新为阈值+to_thread 的新描述（保留「raw 不占池」句）。

## 3. F-203/F-204/F-205/F-206：逐项豁免/延期记录（零代码改动）

计划允许「统一处置或逐项记录豁免理由」。四项所在文件均**不在 W2 冻结写域**（sessions.py / write_groups.py / access_log.py / traffic_snapshot.py），且量级评估：

| 项 | 位置 | 量级 | 处置 |
|---|---|---|---|
| F-203 | sessions.py:101-135/:636-650 | envelope KB~低百 KB；双 dumps+sha256+gzip 合计 <数 ms | **豁免+延期**：量级小；最小修法（json_response 接受现成 identity bytes 消 double-dumps）记入 follow-up backlog，归后续触及 sessions.py 的泳道 |
| F-204 | write_groups.py:235 | POST 回显 KB 级 + MIN_GZIP_BYTES 门 | **豁免**：现实量级最小；同上随写路由泳道再议 |
| F-205 | access_log.py:172-199 | 本地盘 write+flush µs~亚 ms/请求；慢盘才达 ms | **延期**：QueueHandler/QueueListener 改造触及 logging 语义与时序，风险>收益，P3 列档 follow-up |
| F-206 | traffic_snapshot.py:421/:400 | 300s 一次 + 关停一次的 KB 写 | **延期**：`_write_once` 包 to_thread 为一行改动，归后续触及该文件的泳道（避免本波写域外溢） |

豁免/延期记录落点：`docs/ocmar/plans/2026-08-21-follow-up-backlog.md` 追加 W2 处置节 + CHANGELOG [4.6.2] 注记。

## 4. N3 字节等价门（冻结规格的实现）

新测试 `tests/test_offload_equivalence.py` + 金样 `tests/golden/offload-baseline-v1.json`。

- **录制/回放双模式**：`OC_SLIMAPI_TEST_RECORD_GOLDEN=1` 时录制落盘（在 **offload 前**的基线代码上跑一次并提交金样）；默认模式回放逐项 hash 相等。哈希对象 = `sha256(status + 排序后冻结头子集 + body)`；头子集 = `content-type / content-encoding / etag / vary / cache-control / retry-after`（动态头如 date 不进金样）。gzip 产物已实证时间确定（Python 3.14 `gzip.compress` mtime=0），body 可直接哈希。
- **用例矩阵**（N3 全列举 + 修订 M1/N8）：
  1. messages 列表 200（直连路径）：identity / gzip / `*` / `gzip;q=0` / `x-gzip` 五种 Accept-Encoding 态；
  2. merged 200（mode=merged，含 full 展开）：gzip + identity 两态；
  3. ETag（直连路径）：identity tag 命中 304 / gzip tag 命中 304 / `If-None-Match: *` 304 / 错 tag 回 200；
  4. 边界：空 items / 单条 / 16 条页（merged fanout 边界）；
  5. 错误体：422（非法 limit）/ 503（上游 5xx）；
  6. read-group（F-202）：`/slimapi/vcs`（raw）200 gzip/identity + 304；`/slimapi/session/{sid}`（投影）200；大于 1 MiB 的 raw 200（走 to_thread 分支）；
  7. **lease 路径（M1，coalesce_enabled=true + registry fixture）**：列表 200-gzip / tag 命中 304 / `If-None-Match: *` 304 各 ≥1——lease 尾（messages.py:924-974）是独立改写面，且现有 `test_messages_coalesce.py` 零 ETag 断言，不经金样门则其 304/ETag 分支改错会静默通过；**各用例附 lease-实走断言（MINOR-10：spy `_messages_via_lease` 或 LeasedSingleFlight 单-GET 观测量，防 fixture 预算不当静默退化直连）**；
  8. **etag_enabled=false（N8 加固）**：rep_version=None 金样一条（fixture 模式见 test_etag.py:612）。
- **事件循环退出证明（N3 ③）**：spy `TransformPool.offload`——每条 messages 列表请求（200 与 304、直连与 lease）断言提交函数为 `_judge_pack_tail` 的 offload ≥1 次；monkeypatch `asyncio.to_thread`（前提：实现用 `import asyncio` + 属性调用形态）计数——≥1 MiB read-group 请求断言 ≥1 次、小 body 断言 0 次（阈值双向证明）。
- **录制时点与金样头**：金样在**改动前** HEAD 录制并随改动同一 commit 提交（reviewers 可用 `git stash` 复核录制基线）。金样 JSON 头部含 `_meta` 行：录制基线 commit、`gzip mtime=0` 实证注记、**同环境告诫**（deflate 输出随 zlib 构建版本变化，录制与回放须同机同 venv——本仓单机 check.sh 满足）。

## 5. 风险与回退

- 全部改动为零 wire 字节变更（纯线程归属迁移 + 阈值分支）；回退 = revert 单 commit。
- 主要风险 = 尾部 offload 与 admission 的排队交互（§1.3 已析）与 to_thread 默认 executor 占用（与其他 to_thread 使用者共享，本量级 1 MiB+ 才触发，频率低）。
- flaky 先例不涉（无时序敏感断言；offload 计数为确定性 spy）。

## 6. 验收清单

- [x] 金样在基线代码录制（`OC_SLIMAPI_TEST_RECORD_GOLDEN=1`）并提交（含 `_meta` 头：基线 commit / gzip mtime=0 / 同环境告诫）；
- [x] 实现后回放全矩阵 hash 相等（含 lease 路径 7 与 etag_enabled=false 8）；
- [x] offload/to_thread 计数证明（messages ≥1/请求，直连与 lease；read-group 大体量 ≥1、小体量 0）；
- [x] 更新 `_read_passthrough.py:39-41` 模块注释（"run inline" 句）；
- [x] 定向 pytest：test_messages_routes / test_messages_merged / test_messages_coalesce / read-group 相关（test_read_groups*） / test_etag / test_gzip_negotiation + 新测试全绿；
- [x] `./scripts/check.sh` 全量绿（**编排者收尾**）；
- [x] follow-up-backlog.md 追加 F-203..206 处置节 + transform.py offload docstring 偏离注记（N6）；CHANGELOG [4.6.2] 条目（**编排者**）；
- [x] `./scripts/release.sh patch` → push → 本机部署 → 四件套终验（**编排者**）。

## 7. 评审记录

- **rev1（2026-08-21，独立评审 agent，对抗式只读）**：FAIL **8.7**——MAJOR-1（N3 矩阵漏 lease 尾部路径：现有测试惯例 coalesce off，lease 尾 304/ETag 分支改错将静默通过）+ MINOR-2/3/4（TransformBusy 措辞、_read_passthrough 注释同步、to_thread import 形态）+ NOTE-5..9。修订已全部吸收进本文（M1 §4.7/§6、M2/N5/N6 §1.3、M3 §2.2、M4 §2.2/§4、N7 §4、N8 §4.8、N9 §6）。
- **rev2（同评审 agent 复审）**：**PASS 9.5**（正确性 3.9 / 完备性 2.75 / 合规 2.0 / 可验收性 0.85）。遗留 MINOR-10（lease 实走断言，已补入 §4.7）与 NOTE-11..13（化妆/调参级，不阻塞）。结论：设计可直接进入实施。
- 评审通道说明：交接指定的 rev-cgpt/review_prep 基建在本会话不可用，以独立 general-purpose 只读评审 agent 等价替代（同等门禁 9.0、对抗式核验含运行时实证）。
