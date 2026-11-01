# SeedRou × sing-box / Tailscale / Podman 共存契约

> 本文描述 SeedRou 路由底座与机器上其他网络组件的资源占用边界。
> **SeedRou 面板不管理 sing-box**（你的 `sb.sh` 全权负责），底座只保证自己不踩它。

## 1. 资源占用矩阵

| 资源 | sing-box（你的配置占用） | SeedRou 底座占用 |
|---|---|---|
| nftables 表 | `sing-box`（tun `auto_redirect` 自动生成） | 仅 `inet seedrou_base` |
| fwmark / 路由表 | mark `0x2023`、表 `2022`（tun `auto_route`） | 不用任何 fwmark，不添加 ip rule |
| tun 网段 | `172.19.0.1/30`、`fdfe:dcba:9876::/126` | LAN 默认 `192.168.10.0/24` |
| fakeip 网段 | `198.19.0.0/16`、`fc00::/18` | 不使用（LAN 配置若与之重叠会被拒绝保存） |
| NAT6 ULA | — | `fda1:9e3a:8b7f::/48`（固定默认，避开 fdfe:dcba） |
| sysctl 文件 | `/etc/sysctl.d/99-sing-box.conf`（ip_forward=1） | `/etc/sysctl.d/70-seedrou.conf`（ip_forward=1、rp_filter=0） |
| systemd 单元 | `sing-box.service` | `seedrou-panel / seedrou-apply / seedrou-converge.timer / seedrou-pppoe` |
| 定时任务 | `/etc/cron.d/sing-box`（每日 04:00 拉订阅） | `seedrou-converge.timer`（systemd timer，30 分钟收敛一次） |
| 文件 | `/etc/sing-box/`（sing-box:sing-box 0600） | `/etc/seedrou/`（root）、`/var/lib/seedrou/`（seedrou） |
| 端口 | 7890（mixed）、9090（API/dashboard） | 80（面板）、53/67（dnsmasq，仅 LAN+lo） |

## 2. 底座的行为承诺

1. **永不 `nft flush ruleset`**。应用配置时只 `delete table inet seedrou_base` 后重建自己的表；
   `sing-box`、podman（netavark）、tailscale 的表原样保留。
2. **永不 restart / disable `sing-box.service`**，也不读写 `/etc/sing-box/`。
   面板与收敛定时器（`apply-all --convergent`）都不触碰它。
3. **不加 ip rule、不占 fwmark**。你的 `auto_route` 规则（priority 9000 表 2022）不受干扰；
   到 LAN 子网、fakeip 段的路由决策与底座 NAT 各行其道。
4. **转发策略**：`established` 放行；LAN→WAN 放行；WAN→LAN 仅端口转发（DNAT）命中放行；
   LAN↔tun/tailscale0/podman0 默认放行。唯一收紧点是 WAN 入站默认丢弃——
   这同时保护了 7890/9090 不暴露到公网。
5. **rp_filter=0**（`70-seedrou.conf`）：tproxy/tun 场景标准要求；与 `99-sing-box.conf` 数值一致。
6. **健康检查直连**：底座的 WAN 探测基于主表默认路由 + WAN 接口地址，
   sing-box 重启/故障不会误报"断网"，底座也绝不"自动修复" sing-box 相关状态。
7. **底座 NAT 始终健在**：你的配置里 CN 流量 `bypass` 后回到内核路由，
   依赖 `seedrou_base` 的 masquerade 出网——这就是第 1 条承诺的原因。

## 3. 已知交互（属预期行为，非故障）

- **LAN 客户端 DNS 到 192.168.10.1 仍走 dnsmasq**：目标是本机地址，走 main 表本地投递，不入 tun。
- **dnsmasq 的上游查询会被 sing-box 劫持**（你的配置 `port 53 / protocol dns → hijack-dns`），
  这是你的分流策略的一部分；sing-box 停止后自动回落直连解析。
- **WAN 方式切换后**：sing-box 的 `auto_detect_interface` 会跟踪新默认路由；
  面板在保存上网设置后会提示执行 `sb restart` 以立即重绑出口接口（不执行通常也会自愈）。
- **`sb update`（每日 04:00）重启 sing-box**：仅影响经 sing-box 的流量数秒，直连流量无感。

## 4. 冲突自检清单

若出现"开了 sing-box 后某类流量异常"，按顺序检查：

1. `nft list tables` —— 确认 `seedrou_base` 与 `sing-box` 两表都在；
2. `ip rule` / `ip route show table 2022` —— sing-box 的 auto_route 是否正常挂载；
3. `ip route show default` —— 主表默认路由是否在 WAN 口（ppp0 或 ethX）；
4. 面板「系统概览」—— 底座侧 WAN 状态；若底座显示已连接而代理流量异常，问题在 sing-box 侧；
5. LAN 网段改动后：确认不与 172.19.0.0/30、198.19.0.0/16、fc00::/18、10.88.0.0/16 冲突
   （面板保存时会自动校验并拒绝）。

## 5. SeedRou 侧为共存做的默认值

| 默认值 | 原因 |
|---|---|
| LAN 192.168.10.0/24 | 避开 tun/fakeip/podman 常用段 |
| NAT6 ULA `fda1:9e3a:8b7f::/48` | 避开 tun 的 fdfe:dcba::/126 |
| 面板端口 80 | 与 7890/9090/53/67 无冲突 |
| 不劫持 53 端口转发规则 | DNS 分流权完全归 sing-box |
