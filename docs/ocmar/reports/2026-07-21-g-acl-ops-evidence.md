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

- **`:4097`（sidecar 明文端口）**：绑定 `0.0.0.0`（所有接口），**非 loopback**。  
  这是 **Tailscale direct-entry fallback**（共识 §5 row 5 之 `0.0.0.0` 明文直连入口），非严格 loopback 拓扑。  
- **`:14097`（mTLS 入口）**：绑定 `0.0.0.0`，stunnel 终结后转发至 `127.0.0.1:4097`。  
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

## 3. 负向探针（验证步骤——由 ops 手动在外围执行）

> **说明**：以下是从**外部 vantage**（如手机蜂窝网络 / 公网主机）验证 G-ACL 强制的操作指南。当前 orchestrator 无外部网络访问能力，故仅记录过程逻辑。

### 3.1 公网扫描

```bash
# 从外网（非 Tailscale 节点）扫描 14097，预期 filtered/closed
nmap -p 14097 opencode.vectory.cn

# 从外网扫描 4097（明文），预期 filtered/closed（防火墙阻断）
nmap -p 4097 opencode.vectory.cn
```

### 3.2 TLS 连接测试

```bash
# 从外网尝试 mTLS 连接，预期 TLS 握手失败（无有效客户端证书）
curl -v https://opencode.vectory.cn:14097/slimapi/health

# 从外网尝试明文连接 4097，预期拒绝/超时（防火墙或 sidecar 拒绝非 loopback 的明文 HTTP）
curl http://opencode.vectory.cn:4097/slimapi/health
```

### 3.3 内部验证（从本机）

```bash
# sidecar 健康（loopback 明文，默认允许）
curl -s -H 'X-Slimapi-Version: 1' http://127.0.0.1:4097/slimapi/health

# mTLS 回环（须带客户端证书，但本机 stunnel 为自己签名时可略验证）
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

### 4.2 部署选项

ops 可选择：

- **(a) 保持现状**（当前拓扑）：  
  `0.0.0.0:4097` 明文直连 + Tailscale ACL + `14097` mTLS 双入口。  
  安全依赖：Tailscale ACL 控制 `100.x` 访问 + LAN 防火墙封锁非 Tailscale 节点对 `:4097` 的 direct TCP 连接。  
   **风险**：LAN 内非 Tailscale 节点（如无线家庭子网）若防火墙未严格限制，可直连 `:4097` 明文 HTTP。

- **(b) 收紧为严格 loopback**（G-ACL 推荐）：  
   ```diff
   + Environment=OC_SLIMAPI_HOST=127.0.0.1
   ```
   然后重启 sidecar。此时 `:4097` 仅 loopback 可达，外网只能经 `:14097` mTLS 访问。  
   **优点**：安全域缩小至单机 loopback，无需依赖 LAN 防火墙 / Tailscale ACL。  
   **代价**：Tailscale 客户端不能直接走 `:4097` 明文（须改为 `:14097` mTLS），见 ocdroid profile 迁移（共识 §5 G-ACL 门禁）。

### 4.3 推荐 → 已批（omni approved option A）

**批准结论**：omni 已批准 **option A（严格 loopback + 14097 mTLS）** 作为 v0.3.1 的默认 hardened 部署姿态。  
理由：
1. 与 G-ACL 门禁「4097 bind loopback」一致。  
2. 消除明文直连入口对 LAN 防火墙的依赖，安全模型更明确。  
3. ocdroid 端已有 TOFU/pinning 迁移支持（见 ocdroid 侧共识门禁）。  
4. 收紧步骤详见 `docs/operations.md` §10「G-ACL 收紧 runbook」。

**附加说明**：当前部署（`0.0.0.0:4097`）为预收紧基线；硬化目标态为 `127.0.0.1:4097`（loopback）。负向探针结果（§3）待 ops 从外部 vantage 执行后记录于此。

若选择 (a)，须在文档 `docs/operations.md` 中明确指出安全边界由 Tailscale ACL + LAN 防火墙保障，并在 ops-runbook §3.3 中记录负向探针步骤。

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
