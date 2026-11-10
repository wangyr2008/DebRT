# DebRT × sing-box / Tailscale / Podman 共存契约

> 本文描述 DebRT 路由底座与机器上其他网络组件的资源占用边界。
> **DebRT 面板不管理 sing-box**（你的 `sb.sh` 全权负责），底座只保证自己不踩它。

## 1. 资源占用矩阵

| 资源 | sing-box（你的配置占用） | DebRT 底座占用 |
|---|---|---|
| nftables 表 | `sing-box`（tun `auto_redirect` 自动生成） | 仅 `inet dert_base` |
| fwmark / 路由表 | mark `0x2023`、表 `2022`（tun `auto_route`） | 不用任何 fwmark，不添加 ip rule |
| tun 网段 | `172.19.0.1/30`、`fdfe:dcba:9876::/126` | LAN 默认 `192.168.10.0/24` |
| fakeip 网段 | `198.19.0.0/16`、`fc00::/18` | 不使用（LAN 配置若与之重叠会被拒绝保存） |
| NAT6 ULA | — | `fda1:9e3a:8b7f::/48`（固定默认，避开 fdfe:dcba） |
| sysctl 文件 | `/etc/sysctl.d/99-sing-box.conf`（ip_forward=1） | `/etc/sysctl.d/70-dert.conf`（ip_forward=1、rp_filter=0） |
| systemd 单元 | `sing-box.service` | `dert-panel / dert-apply / dert-converge.timer / dert-pppoe` |
| 定时任务 | `/etc/cron.d/sing-box`（`sb update` 拉订阅；时间可在面板设定） | `dert-converge.timer`（systemd timer，开机 15 分钟后首跑、之后每 6 小时收敛一次） |
| sb.sh | 管理器本身 | 随 DebRT 安装包下发到 `/usr/local/bin/sb`（增强版：更新结果落盘 `update-state.json`、支持 `seturl URL`/`uninstall`/`auto on HH:MM`）；sing-box 本体默认不安装，面板一键安装 |
| 面板重启 sing-box | 旧版面板永不重启 sing-box | v0.1.32 起面板提供显式「重启/停止」按钮（用户主动点击才执行，收敛仍不碰 sing-box） |
| 前缀变更钩子 | — | `90-dert-lan-prefix`（v0.1.56）：br-lan 前缀真变时才 `try-restart sing-box` 一次，见 §2.2 |
| 文件 | `/etc/sing-box/`（sing-box:sing-box 0600） | `/etc/dert/`（root）、`/var/lib/dert/`（dert） |
| 端口 | 7890（mixed）、9090（API/dashboard） | 80（面板）、53/67（dnsmasq，仅 LAN+lo） |

## 2. 底座的行为承诺

1. **永不 `nft flush ruleset`**。应用配置时只 `delete table inet dert_base` 后重建自己的表；
   `sing-box`、podman（netavark）的表原样保留（tailscale 自 0.1.32 起以 sing-box endpoint 托管，不再有独立表）。
2. **除"LAN IPv6 前缀变更"这一种情形外，永不 restart / disable `sing-box.service`**；
   **永不读写 `/etc/sing-box/config.json`**（那是 sub-store 的地盘）。
   DebRT 只写自己那一个文件 `/etc/sing-box/00-dert-dns.json`——靠 `-C /etc/sing-box`
   的目录合并排在最前面，订阅更新覆盖 `config.json` 时它纹丝不动。
   面板与收敛定时器（`apply-all --convergent`）都不触碰 sing-box 本体。
   唯一例外：`90-dert-lan-prefix` 在 WAN 重拨导致 br-lan 前缀**确实变化**时执行一次
   `systemctl try-restart sing-box`。原因是 `auto_redirect` 的 nft 集合 `inet*_local_address_set`
   只在启动时构建，前缀变了不重建就会把发往新 LAN 前缀（含路由器自身新地址）的流量误判成
   "非本地"并劫持进 tun（2026-11-09 真机：BRAS 拆链重拨后白名单设备"代理全挂"，手动重启才恢复）。
   该钩子只在"前缀与上次记录不同"时动作，且要求 nft 表里确实存在 `local_address_set`；
   开机首次只记录不重启（那时 sing-box 本来就用当前前缀建集合）。
3. **不加 ip rule、不占 fwmark**。你的 `auto_route` 规则（priority 9000 表 2022）不受干扰；
   到 LAN 子网、fakeip 段的路由决策与底座 NAT 各行其道。
4. **转发策略**：`established` 放行；LAN→WAN 放行；WAN→LAN 仅端口转发（DNAT）命中放行；
   LAN↔tun/podman0 默认放行。唯一收紧点是 WAN 入站默认丢弃——
   这同时保护了 7890/9090 不暴露到公网。
5. **rp_filter=0**（`70-dert.conf`）：tproxy/tun 场景标准要求；与 `99-sing-box.conf` 数值一致。
6. **健康检查直连**：底座的 WAN 探测基于主表默认路由 + WAN 接口地址，
   sing-box 重启/故障不会误报"断网"，底座也绝不"自动修复" sing-box 相关状态。
7. **底座 NAT 始终健在**：你的配置里 CN 流量 `bypass` 后回到内核路由，
   依赖 `dert_base` 的 masquerade 出网——这就是第 1 条承诺的原因。

## 3. 已知交互（属预期行为，非故障）

- **DNS 按 MAC 分两路（v0.1.57）**：fakeip 是 sing-box 的全局 DNS 行为，而 tun 拦截按
  `include_mac_address` 走——两者不同步时，非白名单设备会拿到 fakeip 却没人接管（黑洞）。
  解法在 nft 层：`inet dert_dns` 把 br-lan 的 53 端口按 `ether saddr` 分成两路，
  白名单回导 dnsmasq（走 fakeip）、其余 DNAT 到 sing-box 的 DNS 监听器（走运营商真实解析）。
  两张表由 `sing-box.service` 的 drop-in 维护：`ExecStartPre` 生成
  `00-dert-dns.json`、`ExecStartPost` 建表、`ExecStopPost` 删表。
  **手动 `systemctl stop sing-box` 后表会被删掉，DNS 自动回落 dnsmasq**——这是刻意的：
  代理挂了不该连带把全网 DNS 拖死。日志：`journalctl -t dert-dns-split`。
- **LAN 客户端 DNS 到 192.168.10.1 仍走 dnsmasq**：目标是本机地址，走 main 表本地投递，不入 tun。
- **dnsmasq 的上游查询会被 sing-box 劫持**（你的配置 `port 53 / protocol dns → hijack-dns`），
  这是你的分流策略的一部分；sing-box 停止后自动回落直连解析。
- **WAN 方式切换后**：sing-box 的 `auto_detect_interface` 会跟踪新默认路由；
  面板在保存上网设置后会提示执行 `sb restart` 以立即重绑出口接口（不执行通常也会自愈）。
- **`sb update`（每日 04:00）重启 sing-box**：仅影响经 sing-box 的流量数秒，直连流量无感。
- **WAN 重拨换了 IPv6 前缀后，sing-box 会被底座重启一次**（`90-dert-lan-prefix`，
  约 0.3 秒，仅影响经代理的流量）：这是重建 `local_address_set` 的唯一手段。
  同时该钩子会清掉 br-lan 上旧前缀的 RA 路由——旧前缀已不被运营商委派，
  留着只会让旧地址的流量继续被当本地直连（源地址出网被 uRPF 丢弃）。
  日志：`journalctl -t dert-lan`。

## 4. 冲突自检清单

若出现"开了 sing-box 后某类流量异常"，按顺序检查：

1. `nft list tables` —— 确认 `dert_base` 与 `sing-box` 两表都在；
2. `ip rule` / `ip route show table 2022` —— sing-box 的 auto_route 是否正常挂载；
3. `ip route show default` —— 主表默认路由是否在 WAN 口（ppp0 或 ethX）；
4. 面板「系统概览」—— 底座侧 WAN 状态；若底座显示已连接而代理流量异常，问题在 sing-box 侧；
5. LAN 网段改动后：确认不与 172.19.0.0/30、198.19.0.0/16、fc00::/18、10.88.0.0/16 冲突
   （面板保存时会自动校验并拒绝）。

## 5. DebRT 侧为共存做的默认值

| 默认值 | 原因 |
|---|---|
| LAN 192.168.10.0/24 | 避开 tun/fakeip/podman 常用段 |
| NAT6 ULA `fda1:9e3a:8b7f::/48` | 避开 tun 的 fdfe:dcba::/126 |
| 面板端口 80 | 与 7890/9090/53/67 无冲突 |
| 不劫持 53 端口转发规则 | 域名分流权完全归 sing-box |
| DNS 分流只动 br-lan 的 53 端口 | 不碰 sing-box 的 7890/9090，也不碰 WAN 入站 |
| `include_mac_address` 为空则不建分流表 | 空 = 不限 MAC = 全部进 tun，此时全部 DNS 走 fakeip 才自洽 |
