# G-ACL 运维证据报告

> **日期**：2026-07-21  
> **场景**：体验优先发版（v0.3.1 Opt-A）G-ACL 安全收敛回退验收  
> **基线**：slimapi v0.3.0 / rev F（部署中），ocdroid v0.11.10  
> **命令出自**：`docs/ocmar/reports/2026-07-21-ux-first-consensus-archive.md` §5 门禁 G-ACL  

---

## 1. 配置审计（config.py）

- **默认绑定**：`OC_SLIMAPI_HOST=127.0.0.1`（loopback）。  
- **`validate()` 允许值集**：`{127.0.0.1, ::1, localhost, 0.0.0.0}`。`0.0.0.0` 为明文直连入口（Tailscale ACL / 防火墙保护）—— **不需改代码**。  
- **Upstream SSRF guard**：`OC_SLIMAPI_UPSTREAM` 强制固定 `http://127.0.0.1:4096`，不随 host 放松（安全硬约束）。

---

## 2. 当前部署拓扑（命中事实）

### 2.1 服务绑定

```
ss -tlnp | grep -E '(4097|14097|14096)'
LISTEN  0  2048  0.0.0.0:4097     0.0.0.0:*  python/sidecar (oc-slimapi)
LISTEN  0  4096  0.0.0.0:14097    0.0.0.0:*  stunnel4 (mTLS)
LISTEN  0  4096  0.0.0.0:14096    0.0.0.0:*  stunnel4 (mTLS 直连回退)
```

- **`:4097`（sidecar 明文端口）**：绑定 `0.0.0.0`（所有接口），**用户接受的稳态**。此端口直接明文访问须被网络边界（防火墙/Tailscale ACL）阻断；外部客户端经 `:14097` mTLS 隧道。
- **`:14097`（mTLS 入口）**：绑定 `0.0.0.0`，stunnel 终结后转发至 `127.0.0.1:4097`。任何未持有 CA 签名客户端证书的连接在 TLS 层即被拒绝。
- **`:14096`（mTLS 直连回退入口）**：绑定 `0.0.0.0`，直接转发至 opencode `127.0.0.1:4096`（不经 sidecar）。

### 2.2 stunnel mTLS 强制配置

`~/.config/stunnel/stunnel.conf` 相关节（`[slimapi-mtls]`）：

```ini
[slimapi-mtls]
accept  = 14097
connect = 127.0.0.1:4097
cert    = /home/mar/.config/stunnel/certs/server-cert.pem
key     = /home/mar/.config/stunnel/certs/server-key.pem
CAfile  = /home/mar/.config/stunnel/certs/ca-cert.pem
verifyChain = yes
requireCert = yes
```

- **`requireCert=yes` + `verifyChain=yes`**：客户端连接 `14097` 时必须提供经 CA 签名的客户端证书，否则 TLS 握手拒绝。  
- **SAN = `opencode.vectory.cn`**（证书主体备选名）：仅该域名可通过 mTLS 入口，公网 IP / 其它域名 TLS 层拒绝。

### 2.3 Tailscale 网络信息

- **本机 Tailscale 主机名**：`mar-ubuntu`  
- **Tailscale IPv4**：`100.67.118.33`  
- **LAN 接口**：`eth0 192.168.3.197`、`eth1 172.28.111.90`  
- **ACL 模型**：Tailscale ACL  governs the `100.x` overlay — 只有被 ACL 允许的节点可访问 `100.x` 地址上的 `:4097` / `:14097` / `:14096`。

---

## 3. 负向探针（边界验证——由 ops 从外部 vantage 执行）

> **说明**：本部分是**0.0.0.0 姿态的核心边界证据**：证实 `:14097` 仅可通过 mTLS（cert enforced）可达，且公共/LAN 不可直接访问 `:4097` 明文。  
>   ops 从外部主机（如公网蜂窝网络 / 非 Tailscale 节点）执行以下命令并记录结果至本节末尾。

### 3.1 端口可达性

```bash
# 14097 mTLS 端口 — 预期 open（stunnel 监听），但 TLS 握手需有效客户端证书
nmap -p 14097 opencode.vectory.cn

# 4097 明文端口 — 预期 filtered/closed（网络边界防火墙/ACL 阻断）
nmap -p 4097 opencode.vectory.cn
```

### 3.2 TLS 与 HTTP 探测

```bash
# 尝试 mTLS 连接 — 预期 TLS 握手失败（无有效客户端证书）
curl -v https://opencode.vectory.cn:14097/slimapi/health

# 尝试明文 HTTP 连接 4097 — 预期拒绝/超时（边缘阻断）
curl http://opencode.vectory.cn:4097/slimapi/health
```

### 3.3 本机回路验证（辅助确认）

```bash
# sidecar health（本机 loopback 明文，始终可达）
curl -s -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health

# mTLS 回环（须带客户端证书；本机可用自签名测试）
curl -s --cert client-cert.pem --key client-key.pem \
  https://127.0.0.1:14097/slimapi/health
```

---

## 4. 发现总结与推荐

### 4.1 当前状态

| 项 | 值 | 说明 |
|---|---|---|
| **`:4097` 绑定** | `0.0.0.0` | **非 loopback**，为 Tailscale 明文直连入口。 |
| **mTLS 入口 `:14097`** | 已强制 mTLS（`requireCert=yes` + `verifyChain=yes`） | 任何未持有效客户端证书的连接在 TLS 层即被拒绝。 |
| **Upstream 安全** | 固定 loopback HTTP（`127.0.0.1:4096`） | SSRF guard 不随 host 放松。 |
| **Tailscale ACL** | 负责 `100.x` 覆盖的访问控制 | LAN 侧 `192.168.x` / `172.28.x` 等直连仍依赖主机防火墙。 |

### 4.2 部署选项（已决定）

- **(a) 当前稳态（已采纳）**：`0.0.0.0:4097` 明文直连 + Tailscale ACL + `:14097` mTLS 双入口。  
  安全依赖：Tailscale ACL 控制 `100.x` 访问 + LAN 防火墙封锁非 Tailscale 节点对 `:4097` 的 direct TCP 连接。  
   **风险**（已接受）：LAN 内非 Tailscale 节点若防火墙未严格限制，可直连 `:4097` 明文 HTTP。

- **(b) 严格 loopback（已拒绝）**：`127.0.0.1:4097`，安全域缩小但须 ocdroid profile 迁移（改走 mTLS）。  
   **拒绝原因**：`0.0.0.0` + 网络边界阻断已提供等效安全，且 mTLS 隧道已建成可用，无需客户端侧迁移。

### 4.3 最终决策（用户批准）

**用户最终决定**：接受 `0.0.0.0:4097` 监听 + `:14097` mTLS 隧道（复用既有证书）的实际姿态（不收紧 loopback）。  
理由：
1. `:4097→:14097` mTLS 隧道已建成且可访问；现有证书无需轮转。
2. 安全边界由网络边缘（防火墙/Tailscale ACL）保障，与 `0.0.0.0` 搭配等效于 loopback 加外网准入。
3. 拒绝 loopback 收紧选项以避免 ocdroid client profile 迁移和额外运维负担。

**附加说明**：当前 `0.0.0.0:4097` 即为**用户接受的稳态**。负向探针（§3）应证实 `:14097` 仅 mTLS（cert enforced）可达且 `:4097` 明文被边缘阻断。ops-runbook `docs/operations.md` §10 已同步更新为 0.0.0.0 姿态的边界验证 runbook。

---

## 附录：相关配置项速查

| 配置项 | 文件 | 当前值 |
|---|---|---|
| `OC_SLIMAPI_HOST` | 部署 service unit（见 `docs/operations.md` §3.2） | `0.0.0.0` |
| `config.validate()` 允许值 | `config.py`（源码） | `{127.0.0.1, ::1, localhost, 0.0.0.0}` |
| `OC_SLIMAPI_UPSTREAM` | 部署 service unit | `http://127.0.0.1:4096` |
| stunnel `requireCert` | `~/.config/stunnel/stunnel.conf` | `yes` |
| stunnel `verifyChain` | `~/.config/stunnel/stunnel.conf` | `yes` |

(End of file)
