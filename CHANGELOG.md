# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)（`主版本.次版本.修订号`），
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

当前处于 `0.x` 阶段：次版本号可能包含不兼容变更，升级前请阅读对应条目。

## [Unreleased]

## [0.1.142] - 2026-11-27

> 修 `dns-split-prepare` 的属主修改（安全），并订正过时注释。

### Fixed

- **`mkstemp` 之后用 shell `chown "$DNSJSON"` 仍可被软链利用**（审计附 A1，
  第二次）：`/etc/sing-box` 属于 sing-box，被攻破的进程可以在 `os.replace`
  和 `chown` 之间把目标换成指向 `/etc/shadow` 的软链，root 就会把那个文件的
  属主改掉。改为在 Python 里对 fd 调 `os.fchown`（不经过路径、不跟随软链），
  删掉 shell 那行。
- `dns-split-sync` / `dns-split-prepare` 的注释订正：v0.1.140 把「其余 → :53」
  移到 `dert_base` 后，三处注释还在讲旧规则，改成「`@wl` 为空时本链不匹配，
  查询交给 dert_base 的 dstnat 劫持到 :53」。

### 说明

- CHANGELOG 0.1.141 的「`br_local_out` 抓不到」解释撤回（内核没有这个路径），
  改成「原因未查明」；计数器证明包已从 br-lan 发出。

## [0.1.141] - 2026-11-27

> 删掉无效的 `MaxRtrAdvIntervalSec`（networkd 不认这个键）。

### Fixed

- **`20-lan.network` 里的 `MaxRtrAdvIntervalSec=200` 是无效键**（v0.1.105 起一直
  被静默忽略）：networkd 的 `[IPv6SendRA]` 段不支持它，真机日志
  `Unknown key 'MaxRtrAdvIntervalSec' in section [IPv6SendRA], ignoring`。
  实际 RA 间隔是 RFC 4861 默认的 200–600 秒随机。删掉它避免误导，注释里
  写明「不能配置 RA 间隔」，`RouterLifetimeSec=1800` 保留（≥ 3 倍默认上限）。

### 说明

- **`reconfigure` 会重建 RA 引擎并发 RA**：debug 日志确认（`Stopping IPv6
  Router Advertisement daemon` → `Started` → `Sent unsolicited Router
  Advertisement` ×3）。内核计数（`Icmp6OutRouterAdvertisements` +4、
  `Ip6OutMcastPkts` +7、tun0 不变）证明包确实从 br-lan 发出了，没被
  nft/策略路由改走，也没有 tc 丢弃。
- **之前为什么 tcpdump 抓不到，原因未查明**。最初写的「网桥 `br_local_out`
  路径抓不到」是错的——内核没有这个路径，本机发出的包（单播和多播）都走
  `dev_queue_xmit` → `br_dev_xmit`，tcpdump 的抓包点就在这两步之间。以后
  想查清楚，可以在 br-lan 连 AP 的**成员口**上抓：`tcpdump -ni <成员口>
  'ip6 dst ff02::1'`。
- 现状：PPPoE 重拨换 PD 前缀后，networkd 自己会发一轮 RA（PD 到手时），
  钩子的 reconfigure 再发一轮。**正常重拨（PD 变一次）时，客户端（有线和
  Wi-Fi）都能在几秒内拿到新前缀**（真机对照验证：有线 en0 与 Wi-Fi en1
  同时拿到新前缀，`Assigned prefix` 后 4 条 RA 依次发出）。
- 已知限制：**连续重拨导致 PD 短时间内多次变化**时，客户端可能错过某一轮
  RA（第一轮宣告的前缀已被下一轮取代），需发 RS 触发（关/开 Wi-Fi 即可）。
  这是极端场景（真机连续重拨 3 次、PD 变 4 次才复现），正常重拨不会碰到。

## [0.1.140] - 2026-11-27

> 局域网 DNS 劫持从 `dert_dns` 移到 `dert_base`，不再依赖 sing-box 状态。

### Changed

- **非白名单设备的 DNS 劫持移到 `dert_base` 的 `dstnat` 链**（审计 §3）：
  这条规则原来在 `dert_dns` 的 `pre` 链里，只在 sing-box 运行时存在。sing-box
  一停，设备手动设 `8.8.8.8` 就直连出去——`filter-AAAA`（IPv6 关闭时生效）
  管不到，设备拿到 AAAA 后尝试 v6 连接失败（真机用 netns 模拟验证过：
  sing-box 运行时返回局域网地址，停止后超时）。
  现在由 `dert_base` 负责，只看开关不看 sing-box 状态：
  `iifname $LAN_BR fib daddr type != local meta l4proto { tcp, udp } th dport 53 counter redirect to :53`
  - `fib daddr type != local`：查路由器自身的查询本来就会到 dnsmasq，不需要再 NAT
  - 优先级：`dert_dns pre`（−102）先于本链（−100），白名单设备被先 redirect
    并建立 conntrack 绑定，本链不会二次 NAT（真机计数器验证）
  - 新增 `firewall.dns_hijack` 开关（默认 `true`）；`validate.norm_firewall`
    缺省时保留原值，面板保存不会把它悄悄恢复
- `dert_dns` 的 `pre` 链只剩白名单一条规则（删掉「非白名单 → :53」）——
  这张表现在只含依赖 sing-box 的规则，删掉就完全等于没装 sing-box。

### 测试

- `test_netgen_artifacts.py`：`dns_hijack` 缺省/true 时 dstnat 链有劫持规则，
  false 时没有。
- `test_dns_split.py`：`pre` 链不再有 `redirect to :53`。

## [0.1.139] - 2026-11-27

> nft 分流表结构简化：白名单改命名集合、`pre` 链始终生成；`landns` 解耦
> LAN 地址；5353 就绪改查 TCP；临时文件加固。

### Changed

- **白名单改为命名集合，`pre` 链始终生成**（审计 §1）：
  `dns-split-sync` 删掉「白名单空 × br-lan 有无 v4 × 有无 uid」的组合判断，
  表结构固定为 `set wl` + `chain pre` + `chain out`。空集省略 `elements`
  （写成 `elements = { }` 是语法错误）。第二条规则不再写 `ether saddr != @wl`
  ——`redirect` 是终结判决，白名单已被第一条处理（真机复核：两条都能匹配时，
  第一条计数 3、第二条 0）。
  收益：`netwatch` 不用再重复实现一遍白名单判断，两套规则合一——这正是
  §C2、§C3 两次问题的根因。
- **`landns` 改为 `127.0.0.1`**（审计 §2）：片段不再依赖 br-lan 的 IPv4，
  改 LAN IP 不会因为「恰好读不到地址」触发多余的 sing-box 重启。
- **5353 就绪判断改查 TCP**（审计 §5）：mDNS 也占 UDP 5353，`ss -lnu` 会在
  sing-box 没就绪时误判。`dns-in` 同时监听 TCP+UDP，mDNS 不用 TCP。
  三处同步：`dns-split-sync`、`netwatch`、`health.py`。
- **`dns-split-prepare` 的临时文件改用 `tempfile.mkstemp`**（审计附 A1）：
  `/etc/sing-box` 被 chown 给 sing-box，固定名字的 tmp 允许被攻破的进程
  预放软链，把 root 的写入引到任意路径。

### 测试

- `test_dns_split.py`：白名单非空断言 `set wl` + `elements` + `@wl`；
  空白名单断言仍有 `set wl`（无 `elements`）和 `pre`；无 LAN4 断言仍有
  `pre`；新增「片段不随 LAN 地址变化」（逐字节相同）；删除「两条都不建
  就删表」。
- `test_netwatch.py`：删除依赖 `whitelist_macs` 的 4 个用例，改为 4 个结构
  判定（表不存在 / 固定结构 / 旧 dnat / 5353 未监听）。
- `test_health.py`：mock 的 `ss` 按参数区分（TCP / UDP）。

## [0.1.138] - 2026-11-26

> 修 `dns_table_stale` 的误判：只有 `out` 链时会被当成「表是新的」。

### Fixed

- **`netwatch` 的 `dns_table_stale` 漏判「缺 pre 链」**（审计 §C3）：
  旧判据只要表里有 `redirect to :` 就认为表是新的，而 v0.1.136 新增的 `out`
  链本身就含这一行。于是 sing-box 启动时 br-lan 若还没有 IPv4（表里只建了
  `out`），等地址出现后 `pre` 链也不会补上，白名单设备一直拿不到 fakeip，
  直到下次 sing-box 重启。现在额外判断「白名单非空 + br-lan 已有 v4 + 表里
  没有 `chain pre`」→ 重建。新增 `whitelist_macs()`（带 `DERT_SB_CONFIG`
  测试缝）和 `tests/test_netwatch.py` 的 4 个用例。
- **脚本里补上 `out` 链的优先级依赖说明**：本链 `dstnat-2`（−102）**晚于**
  sing-box 的 `output_udp_icmp`（`mangle+2` = −148），依赖后者先把
  `127.0.0.0/8` return 掉。建表后新增检查：`nft get element` 确认
  `inet4_local_address_set` 含 `127.0.0.1`、`inet6_local_address_set` 含 `::1`，
  缺失时记 `event=local_set_missing`（只记日志，不阻断）。

## [0.1.137] - 2026-11-26

> MTU 改回 1492（PPPoE 标准值），v6 的 MSS 也改用 `rt mtu`。

### Changed

- **`PPPOE_MTU` 1480 → 1492**：v0.1.105 改 1480 的理由是「1480 让客户端不发
  接近上限的包，减少 QUIC 的 PTB」，但这个论证不成立——客户端是 1500 MTU，
  QUIC 按 1500 发，**1480 和 1492 都会超**（超 20 字节 vs 8 字节），PTB 一样
  触发。代价是每包少 12 字节、包数多约 0.8%。
- **v6 的 MSS 从硬编码 1420 改成 `rt mtu`**：真机对照实验确认 `rt mtu` 在 v6 下
  生效（客户端 SYN `mss 1440` → ppp0 出口 `mss 1432`，MTU 1492）。早期注释里
  「rt mtu 在 v6 下压不下来」的结论，在当前内核 + sing-box 1.14 下复现不出来。
- 统一用 `rt mtu` 后，MTU 一变 MSS 自动跟随，不需要再同步改一个常量。
- 删除 `v6_mss` 变量及其计算逻辑。

### 说明

- **v6 也走 PPPoE**（ppp0 有全局 v6 地址、enp4s0 没有任何 v6 地址、默认路由
  `dev ppp0`），所以 v6 的 MTU 上限同样是 1492，不存在「v6 走原生 1500」。
  DebRT 的 `ipv6.mode = "native"` 指的是「运营商原生分配前缀」，不是链路类型。
- 已知缺口（不在本次范围）：不支持「v4 PPPoE + v6 原生以太网」的双栈分离部署，
  v6 链路目前强制跟随 v4。需要时加 `ipv6.link` 选项。

### Fixed

- **老设备升级后 `resolv.conf` 不会被改过来**（审计 §C1）：
  `sync_dns_upstream()` 原来只看「文件里有没有 `nameserver 127.0.0.1`」，
  而 v0.1.135 的文件第二行正好就是它，于是被判成「已同步」永不改写。升级后
  第一行仍是已失效的 `127.0.0.2`，每次查询都要先失败一次才落到第二行。
  现在改成「有效行与期望不一致就重写」，写入内容只有一行
  `nameserver 127.0.0.1`（不再带 `options timeout/attempts`）。
  守卫 **P3-41** 的检查范围加上 `priv/applyops.py`。
- **`out` 链不该依赖白名单和 LAN 口**（审计 §C2）：`dns-split-sync` 原来在
  「白名单为空」或「br-lan 没有 IPv4」时直接 `exit 0`，把 `out` 链一起跳过了，
  本机 DNS 就悄悄退回运营商。v0.1.135 的本机 DNS 并不依赖这两者，属于行为
  回退。现在这两个条件只决定 `pre` 链；只有 `pre` 和 `out` 都生成不了时才删表。
  补了三个用例：空白名单只有 out、无 LAN4 只有 out、两者都没有才删表。
- 测试注释把优先级顺序写反了（`mangle+2` = −148 **早于** `dstnat-2` = −102，
  是 sing-box 的链先执行）。补上这条隐式依赖的说明，并加断言：`skuid` 排除
  必须写在两条 redirect 之前（顺序反了就是死循环）。

## [0.1.136] - 2026-11-25

> 本机 DNS 从「独立入口 127.0.0.2」改成「和局域网共用 nft 分流」。
> 这不是修 bug，是去掉一套重复机制；顺带补上一直缺的本机 v6 DNS。

### Changed

- **本机 DNS 改由 `dert_dns` 的 `out` 链引流**：
  - `priv/dns-split-sync` 建表时新增 `out` 链（`type nat hook output
    priority dstnat - 2`），把本机发往 `127.0.0.1:53` / `[::1]:53` 的查询
    `redirect to :5353`，与白名单设备共用 `dns-in`。
  - 该链**排除 sing-box 自身的 uid**（建表时 `id -u sing-box` 现查）。
    config.json 模板里有 `type: local` 的 DNS server，它会读 `resolv.conf`
    再查 `127.0.0.1`——不排除就是「查询 → redirect 5353 → 再查 127.0.0.1」
    的死循环（真机用 `setpriv` 复现过）。**查不到 uid 时不建 out 链**，
    本机退回 dnsmasq（等于"没有 sing-box"），而不是把 DNS 指向没人监听的端口。
  - `priv/dns-split-prepare` 删除 `dns-self` inbound（`127.0.0.2:53`）及其
    `hijack-dns` 规则。两处必须同进同退：删 inbound 留规则会让 `sing-box
    check` 因引用不存在的 inbound 失败。
  - 三处 `resolv.conf` 写入点（`install.sh`、`network/ppp/ip-up.d/dert`、
    `ip-down.d/dert`）改为只写 `nameserver 127.0.0.1`，去掉
    `options timeout:2 attempts:1`。

### 收益

- **机制从两套变一套**：本机不再需要 127.0.0.2 这个"魔法地址"，排查时
  本机 DNS 就是 `127.0.0.1`，和没装 sing-box 的路由器一样。
- **补上本机 v6 DNS**：`dns-self` 只监听 v4，以前 `resolv.conf` 不能写
  `::1`（写了会绕回 dnsmasq、污染照旧），本机 v6 查询一直缺一条通道；
  现在 `ip6 daddr ::1` 也 redirect 到 5353。
- **回落路径统一且与 resolver 实现无关**：sing-box 停止 → `ExecStopPost`
  删表 → 本机和白名单设备一起回到 dnsmasq。不再依赖 glibc 的多 nameserver
  行为（Go 的 netgo、musl、c-ares 各有差异，"2 秒自动兜底"只对一部分程序成立）。
- **故障域更清楚**：谁用代理谁受代理影响。非白名单设备与 sing-box 零耦合。

### 真机验证

| 测试 | 结果 |
|---|---|
| `dig @127.0.0.1 www.google.com` | `198.19.0.12`（fakeip） |
| `dig @::1 www.google.com` | `198.19.0.12`（v6 通道生效） |
| `dig +tcp @127.0.0.1 www.google.com` | `198.19.0.12`（TCP DNS） |
| `dig @127.0.0.1 <设备>.lan` | 局域网地址（landns 未受影响） |
| `setpriv --reuid=986 ... dig @127.0.0.1 www.google.com` | 真实 IP（skuid 排除生效） |
| `nft list chain inet dert_dns out` | v4 / v6 两条 redirect 计数器均增长 |

### 测试

- `tests/test_dns_split.py`：删 `dns-self` 的两个用例，改为
  `test_no_self_inbound`（断言 inbound 与 hijack 同步删除）和
  `test_whitelist_entry_intact`；新增 `test_local_output_chain`
  （out 链、skuid、v4/v6 两条规则）与 `test_no_uid_skips_output_chain`
  （uid 查不到时不建链）。
- 守卫 **P3-41** 反转：三处 `resolv.conf` 必须写 `127.0.0.1`，
  **不得**再出现 `127.0.0.2`。

### CI

- **修 `P-03 升级冒烟`**：v0.1.133 起 `upgrade-apply` 的
  `require_signature` 默认值改成 `true`（强制拒绝无签名包），而 smoke 的
  P-03 只生成 `tar.gz` + `sha256`、从未生成 `.sig`，于是 v0.1.133 / 134 /
  135 的 smoke 全部红在 `UPG_SIG`——和产品代码无关，是测试没跟上策略变更。
  现在用一次性 ED25519 密钥对签包（私钥只存在于 Release 的
  `DERT_SIGNING_KEY`，smoke 必须自造），公钥装到 `/usr/local/lib/dert/`
  供验签；验签发生在换目录之前，临时公钥有效。

## [0.1.135] - 2026-11-22

> 0.1.134 升级完发现线上还是旧片段——改了生成逻辑，但没有任何东西触发 sing-box
> 重启。这类「改动静默不生效」比报错难查得多，补上。

### Fixed

- **`00-dert-dns.json` 的生成逻辑改了，升级后不生效**。
  这个片段只在 sing-box 启动时由 `ExecStartPre` 生成，而且**不在 netgen 的清单里**
  ——所以它既不参与漂移判定，也不会让 `run_actions()` 重启 sing-box。0.1.134 的
  Tailscale 规则就撞上了：升级成功、版本号变了、线上片段还是旧的，tailnet 的 v6
  照样超时，而日志里一个字都没有。

  现在 `apply_all()` 在两条路径（无漂移 / 全量）都会调 `refresh_dns_fragment()`：
  重生成片段 → 内容变了就 `try-restart sing-box`（配置只在启动时读）→ 内容没变
  就什么都不做（不白打断代理连接）。

### Notes

- 这是「文件不在清单里 → 不产生漂移 → 不重启」这个机制的盲区。清单只覆盖
  netgen 的产物，而 `ExecStartPre` 生成的片段、`prefix-retire.py` 写的
  `90-retired-prefixes.conf` 都不在内。后者本来就由前缀钩子自己管，前者现在补上。
- 新增 `DnsFragmentRefresh`（3 用例）：内容变→重启、内容没变→不重启、脚本缺失→不报错。
- 守卫 P3-48 加第 14 条（数「调用行」而不是子串——`def refresh_dns_fragment():`
  里也含 `refresh_dns_fragment()`，子串陷阱第 8 次）。`reverse_check.sh` 同步扩到 14 项。
- 全量 466 用例绿，字符串断言预算仍 133。

## [0.1.134] - 2026-11-22

> 对方复核了我的修复，指出两处：一处是我修得不彻底（提权仍可绕过），一处是我
> 引入的回归（手动上传升级不可用）。都成立，已改。另外把 Tailscale 的 IPv6
> 问题在 DebRT 侧修掉了——不用动订阅模板。

### Security

- **`backup-create` 的路径校验可被软链目录绕过**（上一版修得不彻底）。
  v0.1.133 用 `realpath` 比对目录，而 `/var/lib/dert` 属主是 `dert`，面板用户
  可以把 `backups` 整个换成指向 `/etc` 的软链——两边都解析成 `/etc`，校验通过，
  root 就往 `/etc/ld.so.preload` 写文件并 `chown` 给 `dert`，提权依旧成立。

  现在改成：目录用 `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` 打开（软链直接 `ELOOP`），
  文件用 `O_CREAT|O_EXCL|O_NOFOLLOW` 在**那个 dirfd** 上创建，`fchown`/`fchmod`
  作用在已打开的 fd 上。这样连「校验完成到 tar 打开文件之间换软链」的 TOCTOU
  也一并消掉——句柄一旦拿到，路径再怎么变都影响不到它。
  路径比较也改回**字面比较**（不用 realpath，那正是被绕过的那一步）。

### Fixed

- **手动上传升级包在默认强制验签后一律被拒**（v0.1.133 引入的回归）。
  上传接口只收 `.tar.gz`，不可能带 `.sig`，而验签改成默认强制 → 每次上传都报
  `UPG_SIG`，提示还写着「当前配置要求必须验签」，用户从没设置过这一项。

  现在升级包选择框支持**多选**：同时选 `.tar.gz` 与它的 `.sig` 即可。前端把签名
  读成 base64 放在 `X-Sig-B64`，后端校验大小（≤8KB）后落在包旁边，`upgrade-apply`
  的 `_verify_signature()` 按 `<pkg>.sig` 取用，用完连同包一起删。

- **Tailscale 的 IPv6 被 bypass，tailnet 的 v6 目标全部超时**（审计 P4，真机确认）。
  `config.json` 的 bypass 判据是 `ip_is_private`，它覆盖 `fc00::/7`，而
  `fd7a:115c:a1e0::/48` 正好在里面；三条 `ts-ep` 规则却排在 bypass 之后。
  于是发往 tailnet 的 v6 包被判 bypass、交回内核，而 endpoint 是用户态实现、
  主路由表里没有 `fd7a::/48` 的路由 → 从 WAN 发出去 → 超时。

  实测（路由器本机发起，读 `inet sing-box` 的计数器差值）：

  | 目标 | bypass | redirect | 结果 |
  |---|---|---|---|
  | `fd7a:115c:a1e0::1:443` | **+1** | +0 | 超时 |
  | `100.100.100.100:443` | +0 | **+1** | 成功 |
  | `fc00::2e:443`（fakeip v6） | +0 | **+1** | 成功 |

  fakeip 之所以幸免，是因为预匹配会把它**还原成域名**再匹配，`ip_is_private`
  对域名不生效；Tailscale 是真实地址，没有这一步。

  修法**不改订阅模板**：`dns-split-prepare` 生成的 `00-dert-dns.json` 会被
  sing-box 的目录合并排到**最前面**（用 `sing-box merge` 实测：合并后片段的
  `hijack-dns` 在 [0][1]，config.json 的 bypass 被挤到 [2]）。所以在片段里前置
  一条 `{"ip_cidr": ["fd7a:115c:a1e0::/48"], "outbound": "<tag>"}` 即可，tag 从
  `endpoints` 里现读（不写死），没有 tailscale endpoint 时**不加**（否则
  `sing-box check` 会因为引用了不存在的 outbound 而失败、起不来）。

### Notes

- **更正我上一轮的一个错误判断**：我曾说草图里「`dns` 链第一条 `redirect` 缺
  `return`，会被第二条覆盖」是致命问题。**这不成立**——`redirect` 本身就是
  终结性 verdict（`nft_redir_eval()` 把 NAT 结果写进 `regs->verdict.code`）。
  真机实测：两条都匹配同一端口的规则，第一条计数 3、第二条 0。对方指出得对。
- `backup-create` 的目录替换攻击只能由**能写 `/var/lib/dert` 的人**发起，也就是
  面板用户本身；所以它的实际含义是「面板被攻破后能否拿到 root」，不是远程漏洞。
  但这一层隔离正是 `dert` 用户存在的理由，不能破。
- 新增用例 11 个（备份软链目录、上传签名 5 个、Tailscale 规则 3 个等）。
- 守卫 P3-48 扩到 14 条，`reverse_check.sh` 同步扩到 13 项，全部「破坏必红、
  恢复必绿」。
- 全量 463 用例绿，字符串断言预算仍 133。

## [0.1.133] - 2026-11-22

> 第三方审计 v0.1.132 报告里确认的问题。报告本身质量很高（它自己标了「没上真机」），
> 我逐条核下来 5 条成立、1 条半成立，另有 3 处判断不成立（见 Notes）。

### Security

- **`backup-create` 可以写任意路径 → 面板用户提权到 root**（§4 #1）。
  sudoers 给的是 `backup-create *`（任意参数），而这个子命令以 root 身份写文件、
  写完还 `chown` 给 `dert`。于是
  `sudo -u dert dert-priv backup-create /etc/ld.so.preload`
  就等于「把任意 root 会读取的文件交给面板用户改写」。现在输出路径必须落在
  `/var/lib/dert/backups`（用 `realpath` 比对，软链指到目录外也拦住）。

- **升级验签默认关闭**（§4 #2）。以前只在配置里显式写了
  `upgrade.require_signature=true` 才强制；攻击者只要让镜像不提供 `.sig`，
  校验就被静默跳过。现在默认强制，确实要绕过时需**主动**写
  `require_signature=false`。

- **sing-box 的 mixed 入站（7890）对局域网无认证开放**（§5.2）。
  白名单设备走的是透明代理（tun + redirect），根本不需要这个端口；开着等于给
  局域网里任何设备（访客、被入侵的 IoT）一个开放代理。防火墙现在默认丢掉
  LAN→7890（`firewall.allow_lan_proxy=true` 可打开）。
  真机确认升级前 7890/9090 均无活动连接。

- **安装 sing-box 不再 `curl | sh`**（§4 #23）。管道里 sh 读到的可能是截断的
  脚本，且执行过什么没有留痕。改为先落盘、校验非空且有 shebang、把 sha256
  写进 journal 再执行。上游脚本仍是「信任首次获取」——彻底解决要改成下载
  .deb + 发布页 checksums 校验，那会动到安装机制，单独评估。

### Fixed

- **N1（中）保存 LAN 设置时，WAN/LAN 重叠校验用的是旧 LAN 值**。
  `cross_validate()` 无论哪一段都拿 `cur` 里的旧 LAN 比对，本次提交的 `result`
  完全没参与。两个方向都错：把 LAN 改成 WAN 所在网段会**漏报**；存量已经重叠时
  把 LAN 改到别的网段想脱困会被**误拒**——用户没有任何办法通过面板改出来。

- **N6（中）`singbox-cleanup` 删表漏写地址族**。
  `nft delete table sing-box` 省略族名时默认 `ip`，而 sing-box 建的是
  `inet sing-box`，命令报 `No such file or directory` 又被 `2>/dev/null` 吞掉
  ——这条清理**从来没生效过**（真机实测退出码 1，表仍在）。sing-box 被
  SIGKILL/OOM 后重定向规则残留，白名单设备的连接被送到没人监听的端口，
  「崩溃后回落直连」失效。顺手修掉注释里过时的「DNAT 到 172.19.0.x」。

- **N4（低中）收敛失败标记被下一次无漂移收敛清掉**。
  开机 `dert-apply` 失败会置标记，15 分钟后首次收敛因为「文件早就部署出去了」
  判定无漂移，走到提前返回分支无条件 `clear_converge_failed()`——告警消失而
  问题还在。现在清标记前检查 `systemd-networkd` / `dnsmasq` /（PPPoE 时）
  `dert-pppoe` 是否 `is-active`，不在运行就保留标记并记 `converge_degraded`。

- **N3（低）兜底防火墙没有 forward 链**。只拦到本机的入站时，Native/PD 模式下
  LAN 设备的 IPv6 GUA 会直接暴露给公网（转发路径是 policy accept）。
  补一条最小 forward 链：`ct state established,related accept` + 从 WAN 新发起
  的丢弃。另加一个用真 `nft -c` 校验两种输出的用例（以前 `FailsafeNft` 把 nft
  也 mock 掉了，证明不了规则能加载）。

- **N5（低）`wan-addr-sync` 的 flush 与 add 分两条命令**。中间那一瞬 `wan_v4`
  是空的，新建的回流连接拿不到 DNAT；注释写的「地址没变就不动集合」在实现里
  根本不存在。现在一个 python 进程同时算「应有/现有」，只在有变化时输出
  **一条事务**（flush + add 一起提交）。

- **N2（低）PD 长度只看第一条路由**。运营商下发多个 PD、或切换上网方式时旧路由
  还没清掉，会读到错的长度。现在收集全部 PD 路由，展示取最宽的那个，
  「够不够用」按**存在任一放得下**判断。

  > 报告建议「取所有匹配项里最长的前缀长度」，方向是反的：同时拿到 /60 和 /64 时
  > 取最长（/64）会误报「PD 太短」，而 /60 已经够 LAN 用了。

- **`allow_lan_proxy` 会被面板保存静默重置**。面板的防火墙表单没有这个字段，
  `norm_firewall` 以前会把它重置成默认值——与 v0.1.74 修过的
  `require_signature` 同一类问题。现在缺省时保留原值。

### Notes

- 报告里**不成立**的三处，已实测核对：
  - **N3-1「通配符集合语法没验证过」**：`nft -c -f` 两种输出都退出 0，语法没问题。
  - **P3「v6 fakeip 可能被 bypass 误放行」**：实测 fakeip `fc00::2e:443` 走
    `redirect`（计数 +1）、`bypass` 计数 +0；对照的真实 CN 公网 v6 才 bypass（+7）。
  - **P4「Tailscale 可能被误放行」**：真机没有 `tailscale0`，模板里的 `ts-ep`
    未启用，不适用。
- **9090 只告警不拦**。那份配置属于 sub-store（DebRT 只读），改监听地址会与
  订阅更新互相覆盖；而 Clash dashboard 是实际在用的功能，拦掉就是功能回退。
  所以 `status.py` 新增 `singbox_api` 检测（监听非回环 + 弱口令），概览页给出
  告警并指出该改 `services[].secret`。真机当前：`0.0.0.0:9090` + `passwd` → 会亮。
- 新增用例 26 个（N1 两条、N2 两条、N3 两条、N4 三条、备份路径两条、
  7890 两条、API 风险五条、allow_lan_proxy 两条、验签默认一条、其它）。
- 守卫 **P3-48** 覆盖上述 11 条，**逐条反向验证**（`.github/scripts/reverse_check.sh`，
  这次是「破坏后必须变红 + 恢复后必须变绿」双向校验）。
- 反向验证过程本身踩到两次教训，都已修：
  - 用上一轮残留的 `/tmp/b1` 做恢复，把 `validate.py` 写成了别的脚本；
    第一轮 10 个「抓到」其实都是语法错误——**恢复必须校验**。
  - 子串陷阱第 6、7 次：`def degraded_services():` 含 `degraded_services()`、
    `_singbox_api_risk` 含 `singbox_api`，检查改成精确表达式才抓得住。
- 全量 454 用例绿，字符串断言预算仍 133。

## [0.1.132] - 2026-11-22

> 审计最后两条。**至此 21 条全部处理完毕。**

### Fixed

- **RT-05 PD 只有 /64 时 br-lan 拿不到前缀**。根因不是 `SubnetId=1` 写死，而是
  **委派里只有一个子网，而那一个已经被 WAN 的 IA_NA 占着**——所以把 `SubnetId`
  改成 0 或 auto 都会让 WAN 与 LAN 同前缀（真机 2026-11-14 踩过：两条相同的
  `/64`，选路优先 ppp0，发往 LAN 客户端的包全被送进 WAN）。

  因此**没有**按报告的建议去改 `SubnetId`（那对 /64 无效），而是：

  - `status.py` 从 networkd 给 PD 放的那条
    `unreachable <prefix>/60 dev lo proto dhcp` 路由读出**实际前缀长度**，
    判断「子网号是否放得下」：`subnet_id >= 2^(64-plen)` 即越界。
  - 越界时概览页直接给告警：「运营商只委派了 /X 前缀，局域网分不到独立网段
    （子网 0 已被 WAN 占用），请改用 NAT6」。NAT6 用 ULA + NAT66，不依赖 PD 长度，
    是这类线路唯一可行的方案。
  - `SubnetId` 顺带做成可配置（`ipv6.subnet_id`，默认 1，范围 0–15），
    用于「WAN 不落在子网 0」的特殊布局；非法值退回 1。生成器与运行期判断
    共用同一个默认值，否则会出现「生成物说子网 1、告警按子网 0 判」的矛盾。

- **RT-17 的另一半：DHCP 拿到的 WAN 地址与 LAN 同网段**。配置层只能校验静态 WAN，
  DHCP 的地址只有运行时才知道。`status.py` 现在计算 `wan.lan_conflict`，
  概览页显示「WAN 地址 x.x.x.x 落在 LAN 网段内，路由会冲突」。

### Notes

- 两条告警都是「配置看着正常、实际不通」的类型，用户不会去翻日志，所以放在
  概览页的「当前上网信息」卡片下方（`ru-callout warn` 样式，实测渲染正常）。
- 新增用例：`PdAndConflict` 7 个（/64 告警、/60 与 /63 不告警、无 PD 不误报、
  NAT6 不检查、WAN 在 LAN 网段内/外）+ `subnet_id` 产物测试 1 个。
- 守卫 P3-47 扩到 RT-05/RT-17。反向验证时又踩到子串陷阱（第 5 次）：检查
  `"pd_too_small" in src` 时，初始化那行 `= False` 仍含原字符串；改成查
  `v6["pd_too_small"] = True` 与 `wan["lan_conflict"] = ipaddress` 才有效。
- `app.js?v=14` → `16`：这次改了 JS，只升 CSS 的版本号不够。
- 全量 433 用例绿。

## [0.1.131] - 2026-11-22

> 审计 P3 批次：剩下的 9 条（3 条校验、6 条运行时）。都是低风险，但多数是
> 「特定配置下悄悄不对」——校验类的三条已经在真机上实测复现过。

### Fixed

- **RT-15 设备名带空格/中文时无法保存静态绑定**。真机实测 dnsmasq 2.91（未编译
  IDN）只接受 `[A-Za-z0-9._-]`：`My PC` 与 `我的电脑` 都报 `bad DHCP host name`，
  而校验层允许这两类名字 → 保存时报「DHCP/DNS 配置校验未通过」，用户无从下手。
  现在写进 dnsmasq 前把非法字符折叠成 `-`，折叠后为空（中文名）就不写主机名
  ——MAC→IP 绑定照常，只是没有 `.lan` 名字。

- **RT-16 端口范围转发全部落到同一个端口**。外部 `8000-8010` + 内部 `80` 会生成
  `tcp dport 8000-8010 dnat to <ip>:80`，11 个外部端口全转到 80，而校验层既不拦
  也不提示。现在要求「外部是范围时内部端口必须等于起始端口」，否则报
  `FW_PORT_BAD` 并说明范围转发是逐一映射。

- **RT-17 LAN IP 能设成 127.x / 169.254.x；WAN 与 LAN 同网段不报错**。
  Python 的 `is_private` 对这两类地址也返回 True，于是能保存成功、保存后整机
  不可达。现在显式拒绝回环/链路本地/组播/未指定；静态 WAN 与 LAN 网段重叠
  报 `WAN_LAN_OVERLAP`（DHCP 拿到的 WAN 地址只能在运行期判断，暂未覆盖）。

- **RT-13 br-lan 没设 `ConfigureWithoutCarrier`**。只插 WAN 线开机、或唯一的 AP
  重启时，br-lan 短暂无载波就会撤掉地址——那期间 dnsmasq 没有可监听的 LAN 地址，
  `dns-split-prepare` 读不到 LAN IP 会回退成写死的 `192.168.10.1`（改过 LAN IP
  就写错）。加 `ConfigureWithoutCarrier=yes` + `IgnoreCarrierLoss=yes`。

- **RT-21 `IPForward=` 自 systemd 256 起弃用**（每次 networkd 加载都打一条警告）。
  拆成 `IPv4Forwarding=yes` / `IPv6Forwarding=yes`，语义不变（转发本来也由
  `70-dert.conf` 的 sysctl 全局开启，真机确认 v4/v6 都是 1）。

- **RT-19 每次 routable 事件都拆掉并重建全部 qdisc**。`80-dert-cake` 不过滤接口，
  br-lan / 每个 LAN 口 / tun0 的事件都会触发一次；而 `qdisc-apply` 会把所有网口的
  qdisc 删了重建——于是每次事件都有一段「不限速窗口」，统计计数也被清零。
  现在钩子只在 WAN 口 / ppp0 上动手；`qdisc-apply` 的清理循环跳过当前 WAN 与
  ifb0（改用 `replace` 覆盖），ingress 也改成 `add || replace`。

- **RT-20 拔 WAN 线期间临时定时器越积越多**。`systemd-run --on-active=180s`
  没带单元名，每收到一条 netlink 事件就多一个。现在固定
  `--unit=dert-pppoe-recheck`，同名单元存在时 systemd-run 直接失败，天然去重。

- **RT-18 网口按字符串排序，自动选出的 WAN 口可能不对**。
  `enp10s0` 会排在 `enp2s0` 前面，被选成「第 1 个口」。`install.sh` 与
  `backupops.default_ports()` 都改成自然排序。

- **RT-14 热插拔钩子只看 IPv4**。纯 IPv6 的 PPPoE 线路上 ppp0 永远没有 v4 地址，
  每次 routable 都会 `try-restart` → 无限重拨。改成「IPv4 或全局 IPv6 任一即可」，
  与看门狗、`wait-wan` 的 P3-06 口径对齐。

- **RT-12 DHCP 续约换了 WAN IP 后 `wan_v4` 集合不更新**。接口状态一直是 routable，
  dispatcher 不会再触发钩子，hairpin 访问自己的端口转发会失败。`netwatch` 的
  `reconcile()` 现在顺带跑一次 `wan-addr-sync`（幂等）。

### Notes

- 新增用例：`TestFirewallRules`(3)、`TestWanLanOverlap`(2)、LAN 回环/链路本地(1)、
  `dhcp_hostname` 三条、`default_ports_natural_order`、`test_survives_carrier_loss`。
- 守卫 P3-47 扩到 RT-12/14/19/20；P3-29 的断言放宽为「有 `systemd-run` 且带
  `--on-active=180s`」（前面多了 `--unit=`）。字符串断言预算仍 133。
- 反向验证时又踩到子串陷阱：`X_IS_WAN=` 里仍含 `IS_WAN`，检查改成行首正则。
- 全量 425 用例绿。

## [0.1.130] - 2026-11-22

> 审计 P2 批次：4 条「特定配置下静默失效」。其中 RT-08 是这一批里影响面最大的
> ——只要 br-lan 没有全局 v6，局域网的 v6 DNS 就全部超时。

### Fixed

- **RT-08 分流表把 IPv6 DNS DNAT 到 `[::1]`**。`br-lan` 取不到全局 v6 时
  （PD 未下发、或只有 /64 委派）`LAN6` 兜底成 `::1`，而 IPv6 **不允许把从外部
  接口进来的包转发到回环**——DNAT 后的包被内核丢掉，局域网所有 v6 DNS 查询
  超时，解析要等回落到 v4 才完成，表现为「解析变慢」。白名单和非白名单都受影响。

  改用 `redirect`：只改端口、地址由内核按**入接口**选，`LAN4/LAN6` 那套计算
  连同 `::1` 兜底一起删掉。真机实测：`dig @192.168.10.1` 与
  `dig @fe80::…%en1` 都正确落到 sing-box:5353（都拿到 fakeip），
  清理临时表后常规路径仍返回真实 IP。
  连带简化 `netwatch`：表里不再写死地址，`lan4()/lan6()/lan6_pick()` 全删，
  对账逻辑改成「表还是旧的 dnat 写法才重建」。

- **RT-04 回流伪装把端口转发的来源 IP 改成了路由器地址**。
  `oifname $LAN_BR ct status dnat masquerade` 本意只处理 hairpin，但**从 WAN
  进来、被 DNAT 到 LAN** 的连接出接口同样是 `br-lan`、`ct status dnat` 同样为真，
  于是公网访问的来源 IP 全变成 `192.168.10.1`——服务端日志、fail2ban、
  按来源 IP 的访问控制全部失效。加 `iifname != $WAN_IFS` 限定为「从 LAN 进来」。

- **RT-09 白名单 MAC 未规范化**。`include_mac_address` 来自 sub-store，
  写法不受控（`AA-BB-CC-DD-EE-FF` / `aabb.ccdd.eeff`），而 nft 只认冒号分隔；
  不规范化整张表建不起来，且报错被吞掉、静默回落到 dnsmasq——白名单设备
  拿不到 fakeip，分流悄悄失效。现在统一成小写冒号格式、去重，认不出的
  MAC 走 stderr 进 journal（丢掉一台白名单设备必须能查到）。

- **RT-10 改 LAN IP 后不重启 sing-box**。`00-dert-dns.json` 里的 `landns` 是
  启动时按当时的 LAN IP 写死的，auto_redirect 的本机地址集合也只在启动时构建；
  不重启的话白名单设备解析 `*.lan` 会失败、访问新地址也异常，而面板只提示
  「请用新 IP 重新登录」。现在保存 LAN 设置后 `try-restart sing-box`。

### Notes

- 测试更新：`test_dns_split` 从「四条 DNAT」改为「两条 redirect」，并新增
  `test_mac_is_normalized`（两种写法归一后去重）；`test_netgen_artifacts`
  新增 `TestNftNatRules`（hairpin 必须排除 WAN 入向）。
- 守卫 P3-19 改成守「别再回去比地址」；新增 P3-47 守 RT-10。
  RT-04/RT-09 已由行为测试覆盖，不再重复占字符串断言预算（棘轮仍是 133）。
- 全量 417 用例绿。

## [0.1.129] - 2026-11-22

> 字标按真机反馈收小，标签页图标改成 iOS 主屏那种白底圆角样式。

### Changed

- **字标尺寸**：侧栏 34px → **26px**，登录页 58px → **44px**，窄屏头部 32px → **24px**。
  三处版本号随之做底边对齐微调（`padding-bottom` 4/5px → 3px）。
- **标签页图标改成白底圆角 + 细描边**（与 iOS 主屏图标一致），描边按各尺寸
  调到约 1 个物理像素：`favicon.ico`（16/32/48）、`favicon-16/32.png`、
  `apple-touch-icon.png`，内联 SVG favicon 同步换新。
- 图标链接加 `?v=2`：**Safari 按图标 URL 长期缓存**，只换文件内容它不会重新拉取
  ——不换 URL 的话用户会一直看到旧图标，且清缓存都不一定管用。

### Notes

- `app.css?v=14` → `?v=15`。两份补丁各自带了 `13→14`、`14→15`，合并后直接到 15。
- 守卫 P3-16 的 href 断言改为允许 `?v=` 缓存参数（断言数不变）。
- 合并时 `index.html` 是手工替换的：补丁基于 `?v=13` 生成，而仓库里已经是 `?v=14`，
  `git apply` 在这一段对不上（`--3way` 缺基础 blob）。其余文件按补丁原样应用。
- 核对方式与上次一致：SVG 过 XML 解析、真实 CSS 渲染三处布局截图、
  `sips` 核对像素尺寸，另把四个图标按 16/32/180 + SVG 64/16/32 并排渲染看一致性。
- 全量 415 用例绿，断言棘轮未上调。

## [0.1.128] - 2026-11-22

> 第三方静态审计（21 条）里确认的 6 条：2 条高、4 条中。全部是「不报错、只悄悄
> 降级」的类型——读不到前缀就把在用的登记为废弃、服务没起来还报成功。

### Fixed

- **RT-02（高）读不到当前前缀时，把正在用的前缀登记为废弃**。
  开机时 `dert-apply` 只排在 `systemd-networkd` 之后，不等 PPPoE 也不等 PD，
  此刻 `current_prefix()` 为空；旧代码 `items = [p for p in seen if p != new]`
  于是把 `seen-prefixes` 里 2 小时内的**所有**前缀都登记为废弃，包括上次开机
  正在用的那个 → 转发链 `ip6 saddr @retired_v6 reject` 直接打断 LAN 的 IPv6，
  要等 `converge.timer` 的 `OnBootSec=15min` 才恢复。
  现在 `new` 为空直接 `{"skipped": "no_current_prefix"}` 返回；并在
  `90-dert-lan-prefix` 的「基线为空」分支补一次带参数的调用，PD 一到就自愈。
  顺带让 `prefix-retire.py` 把 `argv[1]` 当**集合**处理——换前缀的瞬间 br-lan
  会同时有两个全局地址，旧代码按单个字符串比，两个都会被废弃。

- **RT-03（高）开机收敛失败时 nft 表完全不加载**：没有 masquerade（LAN 上不了网），
  也没有 `wan_in drop`（WAN 上的面板 80、SSH、53 全暴露给上游）。
  新增 `failsafe_nft()`，在三个失败分支的 `die()` 之前调用：表还在就不动 →
  否则装磁盘上那份（上次成功部署的，当时过了预检）→ 再没有就写一份内置最小规则
  （LAN 出网 + WAN 入站默认拒绝，配置读不出来时用通配排除法，避免把
  tailscale0 / podman 的入站一起拦掉）。

- **RT-06（中）服务没起来也报成功**：全量路径把 `run_actions()` 的返回值丢掉、
  `wait_dnsmasq()` 的结果也不看，然后照常 `clear_converge_failed()` + `ok_out`。
  备份恢复和恢复出厂都靠这条流水线的退出码决定是否回滚，所以永远不会回滚——
  「恢复了一份网口名不匹配的备份」会提示「已恢复并生效」，设备直接失联。
  现在失败项汇总后 `mark_converge_failed()` + `die("APPLY_PARTIAL")`。

- **RT-07（中）ip-down 整份删掉 upstream.conf**，把 P3-17 的 v6 回退机制作废：
  重拨后 ip-up 从旧文件 grep v6 会扑空，dispatcher 的回退也落空，面板
  「IPv6 DNS」变空。现在只删 v4 行，v6 留给回拨回退。

- **RT-11（低）调试模式开关不持锁**，会与 `config-set`（最长等 45 秒）交错，
  把刚保存的配置写回旧值。更糟的是 `read_cfg()` 异常时返回 `{}`，`on/off` 紧接着
  写回一份「只有 debug 段」的配置——结合 RT-03 就是开机没有防火墙。
  现在 `flow on|off` 持 `apply.lock`；`read_cfg()` 读不出来直接抛，
  `on/off` 捕获后返回 `CONFIG_UNREADABLE` 且**不写任何东西**；
  `flow_enabled()` 这类只读接口仍按「关」处理，不因坏配置报错。

- **RT-01（高，本机不触发）前缀钩子解析压缩写法的 IPv6 出错**：
  `awk` 取前 4 段在 `fda1:9e3a:8b7f::1` 上会得到 `fda1:9e3a:8b7f:::/64`（非法值），
  与 `dns-split-sync` 写的基线永远对不上 → 判成「前缀变了」→ 废弃正在用的前缀
  + 反复重启 sing-box。改用 `ipaddress` 算网段，并让 `dns-split-sync` 写基线时
  用**同一套算法**（两处各算一套，在 br-lan 有两个前缀时也会互相判成「变了」）。

### Notes

- 本机实测：IPv6 是 native/PD，br-lan 地址是 EUI-64（4 段非空），所以 RT-01 的
  错误值从未产生（`NRestarts=0`、`retired_v6` 为空）；RT-02 的条件（重启前 2 小时内
  有过收敛）也还没凑齐。两条都是「潜在但一旦触发就很难查」。
- `gen_sudoers.py` 的分发表解析器加固：先按行剥掉注释再切分。注释里出现 `;;`
  会把分支切碎，报「无法识别的分支」这种看不出因果的错（这次真踩到了，
  守卫自己也踩了一次）。
- 新增 `FailsafeNft`(5)、`PartialFailure`(3)、`prefix-retire`(+3)、
  `TestFlowCfgSafety`(4) 共 15 个用例；守卫 P3-46 覆盖上述 6 条，
  六处破坏逐条反向验证变红。全量 415 用例绿。

## [0.1.127] - 2026-11-22

> 换上新 logo（吉祥物 d + ebrt 字标），替换 Seedrou 时期的种子图案。

### Changed

- **面板品牌标识换成新字标**。侧栏、窄屏头部、登录页三处内联同一份 SVG：
  吉祥物 `d` 与 `ebrt` 连成一个词，右上角绿点表示「在线」。
  - 文字用 `currentColor` 跟随主题（浅色 `#0071e3` / 深色 `#3aa0ff`）；
  - 绿灯在浅色下是**镂空**（白圈会和白底融在一起显小），深色下是白圈——
    两组图形按 `prefers-color-scheme` 切换；
  - 三处尺寸 34px / 32px / 58px，共用 `.ru-logo` 一套规则。
- 侧栏版本号从「DebRT 下方的小字」改为**字标右侧、底对齐**；窄屏头部同理。
- 移除已不用的 `.ru-brandicon` / `.ru-brand b` / `.ru-mtitle`（JS 里已无引用）。
- 图标全套重做：`favicon.ico`（16/32/48）、`favicon-16/32.png`、
  `apple-touch-icon.png`，新增 `icon-192.png` / `icon-512.png` 并写入 `manifest.json`
  （PWA 装到桌面/主屏时不再用拉伸的小图）。

### Notes

- `app.css?v=13` → `?v=14`：样式变了必须换缓存键，否则老客户端会拿旧 CSS
  把新字标渲染成没有颜色的黑块。
- 合并前做了三件事：SVG 逐份过 XML 解析（三处内联 + favicon 的 data URI 全部合法）、
  用真实 CSS 渲染浅色/深色各尺寸截图核对、`sips` 核对每个图标的像素尺寸
  （16/32/180/192/512，ico 内含 16/32/48）。
- 全量 400 用例与守卫（P3-16 favicon、P3-37 PWA、P3-40 窄屏版本号）均通过。

## [0.1.126] - 2026-11-20

> 06:09 宽带自己重拨了一次。原因不是运营商，是**关掉调试模式**留下的一个文件差异。

### Fixed

- **调试模式开关不同步 `/etc/nftables.d/dert-base.nft`**。`flow on/off` 只改内核
  里的 nft 链，而 netgen 的输出取决于 `debug.flow`——盘上的生成物于是与配置不一致。
  下一次收敛必然判定「漂移」，走全量路径，把 PPPoE 重启掉。

  真机时间线：11-19 23:58 升级（当时 flow 还是开的，盘上带链）→ 11-20 00:01
  关掉调试模式（盘上没改）→ **06:09 的 6 小时收敛发现差异**，已连续 **44 小时**
  的 PPPoE 会话被 SIGTERM 打断（`Connect time 2658.9 minutes`）。
  现在开关会把该文件一并重新生成（0644，与清单里的 mode 一致）。

- **收敛全量路径无条件重启 networkd / pppoe / dnsmasq**。`ACTIONS` 是写死的
  五项，所以哪怕只有一个 nft 文件的内容不同，也会重拨宽带、让所有设备的连接
  全断。现在由「到底哪些生成物变了」推导动作：

  | 漂移的文件 | 重启 |
  |---|---|
  | `etc/nftables.d/*.nft` | 只重载 nft（+ cake） |
  | `etc/dnsmasq.d/*` | dnsmasq |
  | `etc/systemd/network/*` | networkd（仅 `10-wan` / `25-ppp0` 才连带 pppoe） |
  | `etc/ppp/*`（含 chap-secrets） | pppoe |

  清单读不出来（`drift=None`）或非收敛调用时才退回全量——那是「不知道改了什么」。

### Notes

- 漂移判定抽成 `drift_list()`，与动作推导共用同一份结论：先算清楚变了什么，
  再决定重启什么，而不是两处各算一遍。
- 新增 `tests/test_applyops.py::DriftActions`（9 用例，含「刚部署完无漂移」
  「只改权限也算漂移」）与 `tests/test_flowops.py::TestFlowNftSync`（4 用例）。
- 旧用例 `test_drift_triggers_full_deploy` 原本断言「dnsmasq 配置漂移会重启
  networkd」——那正是本次要改掉的粗放行为，已改为断言相反。
- 守卫 P3-45：收敛不得无条件跑 `ACTIONS`、开关必须同步生成物（两处都要）。
  三处破坏反向验证均变红。全量 400 用例绿。

## [0.1.125] - 2026-11-19

> 概览「DNS 服务器」一直显示 `—`。两个缺陷叠在一起：一个把配置权限写窄了，
> 一个假设自己读得到那个文件。

### Fixed

- **`config.json` 权限被写成 0600**。`configops` 写 0644，而 `flowops.write_cfg`
  （调试模式开关用的）硬编码 0600——于是**开一次调试模式**就把配置文件降成
  root-only，且没有任何提示。密码本来就在 `secrets.json`（0600），
  `config.json` 只有 `has_password` 布尔，0644 才是设计意图。
  现在权限只有一处定义：`privlib.CONFIG_MODE`，两个写者都引用它。

- **面板直接读 `/etc/dert/config.json` 取 DNS 上游**。面板以 `dert` 用户运行，
  读不到就抛 `PermissionError`，被 `except` 吞掉后返回空列表——界面显示 `—`，
  **日志里一个字都没有**。这与上面的权限变更叠加，就成了「重启后一直显示 —」。
  现在由 `priv/status.py`（以 root 运行）把 `wan.dns` 一起带出来；
  面板侧只保留一个读 `upstream.conf`（全局可读）的兜底，供旧版 status 使用。

- 删掉 `panel/api.py::_lan_ip()`：同样的直接读配置、同样的静默 `None`，
  且已经是死代码——留着就是下一个坑。

### Notes

- 手动 DNS 模式仍然优先用配置里的地址（由 status.py 读取），
  不会被 `upstream.conf` 覆盖。
- 新增 `tests/test_status.py::WanDns`（5 用例）：自动模式取 upstream.conf、
  去重、文件缺失时给空列表而非报错、手动模式优先、`dns` 字段始终存在。
- 守卫 P3-44：面板不得直接读 config.json、status 必须输出 `wan.dns`、
  flowops/configops 不得各自硬编码权限。三处破坏均反向验证变红。
- 全量 387 用例绿。

## [0.1.124] - 2026-11-18

> 修调试模式的一个盲区：**`singbox` 路径看不到真实目标**。

### Fixed

- **`flow show` 里走代理的连接只显示「重定向到 192.168.10.1:34601」**。
  原因是 nft 在 `dstnat + 10` 打点，此时 TCP 已被 redirect 到 sing-box 的本地
  端口（真机实测 34601，**每次重启都会变**），所以日志里的 DST/DPT 是重定向
  目标，不是真实目的地。只看内核日志会把「访问 googlevideo」读成「连路由器」。

  修法不动防火墙：sing-box 自己的日志里 `inbound redirect connection from IP:PORT`
  与 `outbound connection to 域名` **共享同一个连接 ID**，而内核日志有 `SPT`，
  按 (源IP, 源端口) 精确 join 即可。真机实测 192 条 singbox 记录匹配上 187 条。

  于是现在能看到：
  ```
  21:41 电视 192.168.10.117 → rr5---sn-i3belney.googlevideo.com:443 [香港 02]
                            → i.ytimg.com:443                    [香港 02]
  ```

- **join 一开始一条都匹配不上**：sing-box 的日志带 ANSI 颜色码，连接 ID 被包成
  `\x1b[38;5;45m789896436\x1b[0m`，正则静默失配。现在先剥转义序列再解析。

- **IPv6 两边写法不同**：内核打印 `2409:8a5e:0a50:5bc1:...`（每组补零），
  sing-box 打印 `2409:8a5e:a50:5bc1:...`（压缩）。不规范化的话 v6 永远匹配不上，
  而且不报错。现在两边都过 `ipaddress` 归一。

### Added

- `flow show --from 21:40 --until 21:45 [--limit N]`：按时间窗回查。
  以前只能给「最近 N 分钟」，而结果默认只取 200 条——真机上 5 分钟就有 2817 条，
  中间那一段根本看不到（今天两次排查都被这个截断坑过）。给了时间窗时默认放宽到 5000。

### Notes

- 真机回查 21:41 那次「YouTube 加载慢」，电视 47 条代理连接里只有 **2 条**打到
  视频 CDN，倒有 **17 条**是 `tripdata.app`（电视上另一个 App），全部走香港 02。
- `tests/test_flowops.py` 增至 37 用例：新增 join、ANSI、v6 归一、时间窗参数、
  匹配不上时留空不瞎填。全量 382 用例绿。

## [0.1.123] - 2026-11-18

> 0.1.122 的 journald 上限没生效——又是「升级跑旧代码」那个坑。

### Fixed

- **`/etc/systemd/journald.conf.d/dert.conf` 升级后不存在**（真机 0.1.122 实测）。
  0.1.122 把安装写在 `install.sh` 和 `upgradeops` 里，但升级不跑 install.sh，
  而 upgradeops 那一步是**旧版**代码执行的——文件进了 `/usr/local/lib/dert/`
  却没进 `/etc`，上限形同虚设。

  修法：把它并入 `applyops.ensure_own_units()`（收敛期对账）。升级后紧跟的收敛
  跑的是**新装好的** `dert-priv`（`subprocess.run([LIB + "/dert-priv", ...])`），
  所以这一次就能补上，不用等下一个版本。

- `ensure_own_units` 里内容未变时**不重启 journald**：重启会让日志出现一个
  短暂空档，没变化就不该抖（`test_journald_dropin_not_restarted_when_same`）。

### Notes

- 守卫 P3-42 增加两条：`applyops` 必须同步 drop-in、仓库里必须有该文件。
- `tests/test_units.py` 增至 9 用例。

## [0.1.122] - 2026-11-18

> 面板只留开关；顺手量清了「SSD 写入到底是谁造成的」。

### Changed

- **面板不再展示调试记录**，只保留一个开关。实时刷表格既费电也没人盯着看，
  真排查时 `dert-priv flow show 10` 比翻网页快。开关旁保留一行状态：
  「记录中」/「已开启·未生效」/「已关闭」，以及按路径汇总的命中数——
  「开着却一条没记」是最难查的状态，必须能一眼看出来。
- `GET /api/flow` 改为读 `flow status`（只查配置与 nft 链），不再解析日志。

### Added

- **journald 占用上限**（`SystemMaxUse=200M` / `SystemMaxFileSize=50M`）。
  真机上 journal 长到 **4.0 GB**，而根分区只有 55 GB。

### Notes（为什么没做「日志写内存」）

按要求评估过把记录挪出磁盘，最后没做，原因是有数据：

| 来源 | 自开机（21.5 h）累计写入 |
|---|---|
| systemd-journald | **18.8 GB** |
| systemd (pid 1) | 3.1 GB |
| jbd2 | 637 MB |
| sing-box（cache.db 8 MB 反复重写） | 347 MB |

- 稳态整盘写入 **24 KB/s ≈ 2 GB/天**；
- 调试记录只在**开启时**才产生，量级约 1 KB/s，占不到 1%；
- journald 的写放大实测约 **89 倍**（120 秒写 0.89 MB 盘，同期只产生 0.01 MB 文本）。

要真正把记录挪出磁盘，只有两条路，代价都不划算：

1. **NFLOG + 自研收集器**：`nft log group N` 走 netlink，不进内核日志。
   实测 `tcpdump -i nflog:1` 读得到包但**读不到 nft 的 prefix**（那正是路径标记），
   只能自己解 nfnetlink 配置协议（BIND/MODE）。设备上既无
   `libnetfilter_log`、也无内核头文件，枚举值只能靠猜——在活路由器上反复试错
   去省 0.1% 的写入，不划算。
2. **`Storage=volatile`**（journal 全放内存）：写入归零，但**重启后日志全丢**。
   路由器排障恰恰依赖重启前的日志，这个代价比省下的写入大得多。

所以选择：限制占用 + 少记（组播不记、DNS 默认不显示），并在文档里写明
「用完记得关」。真要再降写入，下一个杠杆是 sing-box 的 `log.level`（INFO 每天
约 2.5 万行），那属于 sub-store 的配置，DebRT 不动。

### Tests

- 前端 jsdom 复验：面板无记录表格、徽标三态、开关回弹、按路径汇总。
- 守卫 P3-43 增加两条反向约束：面板不得再出现 `flow-tbody`、
  `/api/flow` 不得再调 `flow show`。
- 全量 371 用例绿，断言棘轮未上调。

## [0.1.121] - 2026-11-18

> 调试模式在真机上跑通后改的三处——都是**不会报错、只会给出错误结论**的那种。

### Fixed

- **「进 sing-box」永远记不到**（真机 60 秒对照实验抓出）。原判据是
  `ip daddr 127.0.0.0/8`，实测命中 **0** 次；真正的判据是 `ct status dnat`
  （命中 45 次）。原因是 nft 的 `redirect` 把目的地址改到**入接口的本机地址**
  （`192.168.10.1`，命中 264 次），不是回环。用地址段判会让这一类记录永远为空，
  而面板只会显示「最近 10 分钟没有记录」——看起来像没流量，其实是判据错了。
- **同一条包被记 4 次**。nft 的 `log` 语句**不终止规则匹配**：一条 DNS 包会依次
  命中 dns-sb → singbox → tun → direct，刷出四行不同路径的记录，日志直接失去意义。
  改为每条路径「log 规则 + 同条件的 return 规则」两条一组。
  `return` 必须单独一条：`limit` 是匹配式的，超速时同一规则里的 `return` 也会被
  跳过，高峰期反而会掉到兜底分支记错路径。
- **非白名单 DNS 被记成「直连」**。DHCP 下发给客户端的 DNS 就是路由器自己
  （`192.168.10.1`），DNAT 是恒等变换，`ct status dnat` 为假。改用
  `th dport 53 fib daddr type local` 判「发给路由器自己」，同时天然排除
  mDNS（`224.0.0.251:5353` 不是本机地址）。
- **组播把日志刷满**。SSDP（`239.255.255.250:1900`）与 mDNS 20 秒能刷上百行，
  真机 45 秒 555 行里 DNS 占 383 行（69%）。现在组播/广播在链首直接 `return`
  不记录，DNS 默认也不进 `show` 的输出（`--dns` 才看）。

### Notes

- 三条都是「产物能加载、服务能跑、就是不给你对的数据」，单测无法凭空想到，
  只能靠真机对照实验（挂一条只带 `counter` 的实验链，数 60 秒的命中次数）。
  实验方法与结论都写进了 `netgen.flow_chain_lines()` 的注释。
- `tests/test_flowops.py` 增至 28 用例：新增 `test_singbox_uses_dnat_not_loopback`、
  `test_every_log_rule_terminates`、`test_multicast_skipped_before_logging`、
  `test_dns_hidden_by_default`，反向验证过。

## [0.1.120] - 2026-11-18

> 修 0.1.119 的一个部署死角：**新增单元在"引入它的那个版本"里装不上**。

### Fixed

- **`dert-wanq.timer` 升级后是 `disabled`**（真机升级 0.1.119 后实测）。
  升级跑的是**旧版**代码：旧版的通用拷贝循环把新的 `.service`/`.timer` 落到了
  `/etc/systemd/system`（文件确实在），但 `enable` 写在新版代码里，而新版要等
  **下一次**升级才运行。结果是文件在、定时器不跑、面板一直显示「暂无数据」，
  而且**不报任何错**。

  修法是让收敛自己再对账一次：`applyops.ensure_own_units()` 按 `OWN_UNITS`
  白名单同步自有单元（只碰 DebRT 自己的，绝不覆盖 sing-box / podman 的），
  内容变了才 `daemon-reload`，没启用的才 `enable`。收敛有两条路径（无漂移提前
  返回 / 全量部署），两处都调用——最迟一次收敛即自愈，也能修掉被误 disable 的单元。

- `ensure_own_units` 里"读源"与"读目标"必须分开 `try`：合并写会把「目标不存在」
  当成「源读不出来」，从而写入空内容，之后每次收敛都判定"有变化"并白跑一次
  `daemon-reload`（由 `test_no_reload_when_content_same` 抓出来）。

### Notes

- 新增 `tests/test_units.py`（7 用例）：只碰白名单、内容相同不重装、已启用不重复
  enable、`daemon-reload` 必须在 `enable` 之前、systemctl 失败不影响收敛。
- 守卫 P3-42 增加三条：`ensure_own_units` 存在、两条收敛路径都调用、
  `OWN_UNITS` 含 `dert-wanq.timer`。

## [0.1.119] - 2026-11-18

> 两件事：**出口质量终于有历史了**（v4/v6 分开测），以及一个**默认关闭**的
> 调试模式——打开后每台设备的每条新连接都会记下「走了哪条路」。

### Added

- **WAN 出口质量探测**（`priv/wanq.py` + `dert-wanq.timer`，每 60 秒一轮）。
  `health` 只判「ppp0 存不存在」，测不出「在但劣化」——2026-11-17 那次出口
  劣化了 3 分钟，日志里一个字都没有，最后翻了十几轮 journal 才定位到运营商侧。

  - v4 / v6 **分开判定**，v6 未启用时完全不发 v6 探测包；
  - 每个地址族探多个目标（网关 + 两个国内 DNS）**取最好的那个**。按总和算会
    把「某个目标屏蔽 ICMP」误判成「链路丢一半」——公共 DNS 的 v6 地址普遍如此；
  - 判据：丢包 ≥20% 或平均 RTT ≥200ms → `degraded`；
  - **只在状态跳变时写事件**（`wan_quality_drop` / `wan_quality_ok`，恢复事件
    带 `down_s`）。60 秒一轮一天 1440 条，全写事件只会把 journal 冲掉；
  - 快照落 `/var/lib/dert/wanq.json`（含最近 60 个样本，约 1 小时），
    概览页新增「出口质量」卡片直接读它。

- **调试模式 · 流量路径**（`priv/flowops.py`，默认关闭）。
  打开后 nft 在 `dstnat + 10` 给每条**新连接**记一行内核日志，路径由「哪条规则
  匹配上」决定：

  | 路径 | 含义 |
  |---|---|
  | `direct` | 直连（非白名单，或白名单但命中 `geoip-cn` 的 `bypass`） |
  | `tun` | UDP/ICMP 打了 `0x2023`，进 tun 走代理 |
  | `singbox` | TCP 被重定向到回环，进 sing-box 再按域名分流 |
  | `dns` / `dns-sb` | DNS 走 dnsmasq / 走 sing-box |

  - 为什么不用 sing-box 的日志：`action: bypass` 的流量在它里面**完全不可见**
    （那恰恰是最难查的一类），而且它的日志没有源设备，只有目标地址；
  - 为什么放在 `dstnat + 10`：实测 `dert_dns` 在 `dstnat - 2`、sing-box 的
    TCP 重定向在 `dstnat + 2`、UDP/ICMP 打标在 `dstnat + 3`，晚于它们才能读到结论；
  - **端口不写死**：`auto_redirect` 的端口每次启动都变（真机实测 45217 → 39883），
    判据只能用「被 DNAT 到回环」；
  - DNS 规则带 `ct status dnat`：mDNS 也是 5353 端口但走组播不做 DNAT，
    不加这个条件会把 mDNS 记成「DNS 走了 sing-box」；
  - 每条连接一行（`ct state new`）+ 20/秒限速，关闭时 nft 链**根本不存在**。

### Fixed

- `priv/dert-priv` 的 `health` 分支缺 `;;`（上一个版本留下的语法隐患，
  新增分支时被 `bash -n` 抓到）。

### Notes

- 调试模式的两个坑是 `nft -c` 实测出来的，都不是运行期报错而是**语法错误**：
  `chain flow` 的 `flow` 是 nft 保留字（flowtable），改名 `flowlog`；
  `limit rate 20/second burst 40` 缺 `packets` 单位。
- 新增单元在升级路径上不会自动安装：`upgradeops` 显式补装并 `enable --now`，
  守卫 P3-42 钉住这件事（否则老设备升级完只有文件没有定时器，且不报错）。

## [0.1.118] - 2026-11-16

> 让**路由器自身**的 DNS 也走 sing-box：以前它走运营商 DNS，被污染的域名
> （`www.google.com` 解析成 Twitter 的 IP）走代理也连不上。

### Fixed

- **路由器自身访问境外域名失败，而手机正常**。原因不是代理坏了，是 DNS：

  | | DNS 走哪 | `www.google.com` 解析到 | 结果 |
  |---|---|---|---|
  | 手机（白名单） | nft DNAT 到 sing-box `:5353` | fakeip `198.19.0.214` | ✅ |
  | 路由器自身 | dnsmasq → 运营商 DNS | **`104.244.42.197`**（Twitter 的 IP） | ❌ |

  运营商把 `www.google.com` 污染成了别家 IP（AAAA 则是 Teredo `2001::1`），
  curl 拿着假地址走代理，TLS 对不上自然超时。而 `google.com`（不带 www）
  是干净的——所以 `dig` 看着正常，一访问就中招。

  现在给路由器自身开独立 DNS 入口：`00-dert-dns.json` 增加 `dns-self`
  inbound（`127.0.0.2:53`）+ 对应 `hijack-dns` 规则，本机 `resolv.conf`
  首选它。选 `127.0.0.2` 而不是 `127.0.0.1` 是因为 dnsmasq 还占着后者，
  留着做兜底：sing-box 没装或停掉时该端口无人监听，UDP 收到 ICMP 端口不可达，
  glibc 会**立即**换下一个 nameserver（不等超时），所以不会拖慢解析。

  绑 53 端口靠 systemd 单元已有的 `cap_net_bind_service`（sing-box 以
  `sing-box` 用户运行，不是 root）。

- `resolv.conf` 不再写 `nameserver ::1`：`dns-self` 只监听 v4，留着 `::1`
  会让本机 v6 查询绕回 dnsmasq，污染照旧。三处写入点（`install.sh`、
  `ip-up.d/dert`、`ip-down.d/dert`）同步改。

### 说明

- **ICMP 仍然不走代理**，这是 sing-box 的固有边界：`output` 链只接管 TCP，
  而且它的路由规则里 `network: icmp → Direct`。所以 `ping google` 永远不通，
  测代理请用 `curl -x http://127.0.0.1:7890` 或直接 `curl`。
- **本机 UDP 仍未接管**（`output` 链无 udp 规则），QUIC 类流量走直连。

### Added

- 守卫 **P3-41**：三处 `resolv.conf` 写入点必须首选 `127.0.0.2`，且不得再写
  `nameserver ::1`。
- `test_dns_split.py` 新增 2 个用例：`dns-self` 的监听地址/端口/类型/hijack
  规则，以及「加了 dns-self 后白名单入口一个都不能少」。

## [0.1.117] - 2026-11-16

> 手机版头部补上版本号；顺带修掉侧栏里写死的 `V0.1.0`。

### Fixed

- **移动版看不到版本号**。`.ru-narrow .ru-side{display:none}` 把整个侧边栏藏掉，
  而版本号只写在侧栏的 `ru-sidever` 上——手机端从头到尾没有版本元素。

  现在窄屏头部 `DebRT` 标题右侧加 `ru-mver`（12.5px 灰字、与标题底部对齐），
  由新增的 `setVersion()` 统一写入三处（侧栏 / 窄屏头部 / 登录页副标题），
  避免以后再出现「只改一处」。

- **侧栏版本号写死 `V0.1.0`**。`index.html` 里是硬编码，登录后靠状态轮询覆盖，
  但轮询没跑之前（或落在非「概览」页）显示的就是 `V0.1.0`，升级也不变。
  现在初始留空，由 `/api/session` 返回的版本直接写入——登录后停在设备列表
  这类页面也不会空着。

### Changed

- 静态资源版本号 `?v=10` → `?v=11`。

### Added

- 守卫 **P3-40**：窄屏版本元素、`.ru-mver` 样式、`setVersion` 统一写入函数
  三件套齐全，且 `index.html` 不得再出现硬编码 `V0.1.0`。

## [0.1.116] - 2026-11-16

> 设备列表补上「静态 IP 设备」——概览统计到的 mesh 主 AP，以前在列表里看不到。

### Fixed

- **设备列表看不到手工配静态 IP 的设备**。列表的数据源只有 dnsmasq 租约 +
  静态绑定，而 mesh 主节点这类设备**从不走 DHCP**，于是它只出现在概览的
  「已连接设备」（数的是 `/proc/net/arp` 条目）里，两个数字永远对不上
  （真机实测：概览 12 台、列表 11 条，差额就是主 mesh AP `192.168.10.128`）。

  现在把「ARP 在线但既无租约也无静态绑定」的设备合并进列表，租约列显示灰色
  「静态 IP」徽标，`no_dhcp` 字段固定出现（第三方消费方不用猜有没有这个键）。

- **`sysinfo.arp_online_macs()` 缺 `br-lan` 过滤**。`priv/status.py` 里同样的
  判据有过滤（B-05 注释明确要求两处一致），panel 侧漏了。当前 WAN 是 ppp0
  （点对点、无 ARP）所以没暴露；WAN 换成 DHCP 网口时，上游网关会被标成在线内网设备。

### Changed

- 设备列表标题改成「当前共 N 条记录（在线 M 台）」，**在线数直接对应概览的
  「已连接设备」**。记录数会多于在线数——离线但租约未过期的设备仍留在表里
  （真机：13 条记录 / 12 台在线）。

### Added

- 守卫 **P3-39**：喂假租约 + 假 ARP 跑一遍 `leases()`，断言静态 IP 设备进列表，
  且 WAN 侧接口、未解析（flags 0x0）的 ARP 条目不得混入。
- `tests/test_sysinfo.py`：15 个用例覆盖合并优先级（租约 / 静态绑定 / 仅 ARP 三者
  重叠）、字段契约、文件缺失、损坏行等边界。

## [0.1.115] - 2026-11-16

> 修 iOS 上「点密码框整页放大、登录后还是放大的」；连不上路由器时给出路，不再掉进登录页。

### Fixed

- **iOS 点密码框整页放大，且登录进后台后不缩回**。Safari 在聚焦 `font-size`
  小于 16px 的输入框时，会把整页放大到等效 16px；失焦后经常不还原，于是
  「登录页点一下密码框 → 页面变大 → 登录成功进后台，还是放大的」。

  面板的输入框是 13px（登录页 14px），必然触发。修法是两层：

  1. **根治**——触屏设备上把输入类控件提到 16px：
     `@media (any-pointer:coarse){ .ru-in{font-size:16px} ... }`。
     用 `any-pointer` 而不是 `hover`/`pointer`，是为了让带妙控键盘的 iPad、
     触屏笔记本也命中；桌面端保持设计稿字号，版面不变。
  2. **兜底**——`resetViewportZoom()`：输入框失焦时把 viewport 临时改成
     `maximum-scale=1` 强制缩回 1.0，150ms 后恢复，正常双指缩放不受影响；
     登录成功切进后台时也主动调一次，清掉登录页留下的放大状态。

- **静态资源发了两个 `Cache-Control` 头**。`_send()` 无条件写
  `no-store`，`_file()` 又通过 `extra` 追加一个——响应里出现两条，
  浏览器按最保守的 `no-store` 处理，`_file()` 里的 `max-age`/`no-cache`
  等于白写。现在 `_send()` 用 `cache=` 参数，只发一条；面板自身的
  `.html/.js/.css` 一律 `no-cache`（文件名不带 hash，只有手写的 `?v=N`，
  给 `max-age` 会让升级后仍跑旧样式）。静态资源版本号同步升到 `?v=10`。

### Added

- **启动探测失败不再直接掉进登录页**。以前 `/api/session` 一失败就
  `setLogged(false)`：路由器刚升级/重启时，用户面对的是一个「密码怎么输
  都不对」的登录框——其实是网络层压根没通。

  现在按错误类型分开处理：

  | 情况 | 行为 |
  |---|---|
  | 有 `code`（面板活着，4xx/5xx） | 进登录页，与以前一致 |
  | 无 `code`（`fetch` 直接 reject） | 留在启动卡上，按 2/3/5/8/10 秒退避自动重试，约 28 秒覆盖一次重启 |

  自动重试仍不通，则显示「无法连接到路由器」+ 原因 + **重新连接**按钮。
  登录页提交时的网络失败也单独提示「无法连接到路由器」，不再谎报密码错误。

- 守卫 **P3-38**：触屏输入框字号必须 ≥16px（取实际数值，写 15px 也拦）、
  `app.js` 必须有视口复位且挂在 `focusout` 上、启动卡三件套（文案/提示/重试
  按钮）齐全、`Cache-Control` 只能发一条。这几条都是「删了不报错、行为悄悄
  退化」的类型。

## [0.1.114] - 2026-11-15

> 回撤 v0.1.113 的图标改动；修「iOS 添加到主屏后仍在 Safari 里打开」。

### Reverted

- **v0.1.113 的 logo 替换整体回撤**（commit `6b1ab8f` 已 revert，tag 与 Release
  均已删除）。面板恢复原来的内联 SVG 图标。改动本身没有质量问题，只是先不发。

### Fixed

- **iOS 添加到主屏后仍然开在 Safari 里**。原因是 `index.html` 里**完全没有**
  `apple-mobile-web-app-capable`——没有这条，iOS 只会把页面存成一个书签，
  点开还是 Safari（带地址栏、每次重新加载），而不是独立窗口。

  补齐四条声明 + 一份 manifest：

  ```html
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="DebRT">
  <link rel="manifest" href="/static/manifest.json">
  ```

  `status-bar-style` 用 `default` 而不是 `black-translucent`：后者会让内容
  顶到刘海下面，需要额外处理 `env(safe-area-inset-*)`，而现有布局用的是
  `min-height:100vh`，改动面大。`default` 让内容从状态栏下方开始，零风险。

### Added

- 守卫 **P3-37**：`index.html` 必须带那四条声明、`manifest.json` 必须存在且
  `display=standalone`、引用的图标必须真实存在。这类 meta 最容易被"清理无用
  标签"时误删——删了不报错，只是行为悄悄退化，所以钉死。


## [0.1.112] - 2026-11-14

> 修"LAN 的 v6 又全断了"：这次是 **WAN 和 LAN 拿到同一个 /64**。

### Fixed

- **br-lan 显式指定 `SubnetId=1`**。运营商委派的 PD 是 **/60**（够 16 个 /64），
  而 WAN 自己的 IA_NA 地址就落在 PD 的**第一个** /64 里：

  ```
  ppp0: DHCP: received delegated prefix 2409:8a5e:a54:1fe0::/60
  WAN 地址:  2409:8a5e:a54:1fe0:98e6:5c85:448d:46a0/64   ← 在第一个 /64
  LAN 前缀:  2409:8a5e:a54:1fe0::/64                      ← networkd 也取了第一个
  ```

  `SubnetId=` 默认是 `auto`，networkd 取第一个 /64 给 LAN，于是同一 /64 挂在
  两个接口上。路由表出现两条相同前缀，`ip -6 route get <客户端地址>` 直接返回
  `dev ppp0`——**发往 LAN 客户端的包全被送进 WAN**，LAN 的 v6 彻底不通。
  改取第二个 /64（`1fe1`）即避开 WAN 所在的那一个。

  真机验证：改前 br-lan = `1fe0::/64`（与 ppp0 相同，选路走 ppp0）；
  改后 br-lan = `1fe1::/64`，`1fe0::/64` 路由只剩 ppp0 一条，选路恢复 `dev br-lan`。

- **`health` 新增 `ipv6:lan_wan_prefix`**：比对 br-lan 与 ppp0 的 GUA 是否同 /64。
  这种状态从服务状态、nft 表、DNS 上看全都"正常"，只有比对前缀才看得出来。

### 说明

- 只对 `native` 模式生效；`nat6` / `static` 有自己的固定前缀。
- DHCP 模式下 `UplinkInterface` 取 WAN 网口名，PPPoE 下固定 `ppp0`。
- **已知限制**：如果哪天运营商只委派一个 /64（没有第二个 /64 可切），
  `SubnetId=1` 无处可取，LAN 会拿不到 GUA。那时需要退回 ULA + NAT66，
  这条先记着——当前线路是 /60，不触发。


## [0.1.111] - 2026-11-14

> 0.1.110 引入的回归：关闭 IPv6 时 `25-ppp0.network` 会被删掉。

### Fixed

- `25-ppp0.network` 此前只在 IPv6 启用（native/nat6）时生成。0.1.110 让
  「关闭 IPv6 只重启 networkd、不重拨」之后，关闭那一步会让这个文件从清单里
  消失 → `cleanup.py` 按清单对账把它删掉 → ppp0 失去
  `KeepConfiguration=yes`。后果是**下一次 networkd 重启会把 pppd 加的 IPv4
  一起清掉，WAN 直接断**（真机 2026-11-12 踩过同样的坑）。

  现在 PPPoE 下始终生成该文件；关闭 IPv6 时内容只剩
  `IPv6AcceptRA=no` + `KeepConfiguration=yes`。


## [0.1.110] - 2026-11-14

> 开关一次 IPv6 就多出一堆地址：不是客户端的问题，是我们让它换了两轮前缀。

### Fixed

- **开关 IPv6 不再重拨**。真机日志（一次"关→开"）：

  ```
  01:58:34 config-set ipv6（关闭）→ 01:58:39 重拨 → 01:58:59 前缀 a54:13e0
  01:58:58 config-set ipv6（打开）→ 01:59:03 重拨 → 01:59:03 前缀 a54:1c10
  ```

  两次重拨换了两批 PD 前缀，客户端每个前缀生成 2 条地址（稳定 + 隐私，
  RFC 4941），于是三组共 6 条。用户看到的就是"开关一次，地址翻一倍"。

  三处改动：

  1. `peers/dert` 的 `+ipv6` **常开**——IPv6CP 协商与"要不要分配地址"是两件事；
  2. `25-ppp0.network` 的 `DHCP=ipv6` / `DHCPPrefixDelegation` / `[Route] ::/0`
     只在 IPv6 启用时生成，关闭时连 DHCPv6 客户端都不启动（此前关了 IPv6
     仍在向运营商要前缀）；
  3. `configops` 去掉「`section=ipv6` 且 PPPoE → 重拨」这条规则。

  现在开关只重启 networkd，PPP 会话不动，前缀不变，客户端不会再新增地址。

### Added

- `test_ipv6_toggle_does_not_redial`：开关 IPv6 时不得出现 `restart dert-pppoe`。


## [0.1.109] - 2026-11-14

> 按 RFC 7084 补齐 IPv6 前缀变更的处理：补上 L-14，把 L-13 的窗口收回 2 小时。

### 背景

工业界（RFC 7084，IPv6 CE Router Requirements）对"委派前缀变了"有两条 MUST：

- **L-13**：立即用 RA 通告旧前缀，Preferred Lifetime = 0，Valid Lifetime 取 0
  或 min(当前值, 2 小时)。
- **L-14**：对**使用已失效前缀作源地址**的转发包，回 ICMPv6 Destination
  Unreachable（code 5，源地址未通过出入口策略）。

我们此前只做了 L-13，而且是"持续广播 24 小时"的加强版；L-14 完全没做——
这解释了为什么客户端可以一直拿旧地址发包：路由器只是"通知"过它，没有纠正。

### Added

- **L-14**：`dert_base` 新增集合 `retired_v6`（interval），forward 链**最前面**
  加 `ip6 saddr @retired_v6 reject with icmpx type no-route`。必须排在
  `ct state established,related accept` 之前，否则旧源地址上的既有连接会被
  conntrack 放行，永远收不到纠正。集合由 `priv/prefix-retire.py` 在每次
  前缀变更/收敛时同步。

### Changed

- L-13 的保留窗口 24 小时 → **2 小时**（RFC 允许的 min(当前值, 2h)）。
  有 L-14 兜底后不需要长窗口：客户端就算错过全部 RA，只要还在用旧前缀发包，
  立刻会收到 ICMPv6 不可达，连接失败后按 RFC 6724 重新选源。

### 已知偏差

- 本机 nft 不支持 `icmpx type policy-fail`（对应 code 5），用的是
  `no-route`（ICMPv6 type 1 code 0）。对客户端的实际效果一致：连接立刻失败
  并重试，而不是静默丢包等超时。


## [0.1.108] - 2026-11-14

> v0.1.107 的废弃机制只在"事件准时到达"时生效，真机漏了一次。

### Fixed

- **换前缀的 netlink 事件会漏收，导致旧前缀没被登记**。真机日志：

  ```
  01:58:59  event=prefix_change old=a50:7711 new=a54:13e0   ← 这次收到了
  （此后 a54:13e0 → a54:1c10 换过一次，没有任何日志）
  ```

  结果 `a54:13e0` 一直不在撤销表里，macOS 仍拿它的地址当源地址发包 → 黑洞。
  用户在 Mac 上实测：默认源地址（a54:13e0）100% 丢包，换用当前前缀（a54:1c10）
  立刻通。

  改为**自愈式**：持久化「见过的所有前缀」（`/var/lib/dert/seen-prefixes`），
  每次调用都把"不是当前"的全部登记为已废弃，不再依赖某一次事件是否收到。
  调用点有三处：前缀钩子、`apply-all` 的收敛路径（含"无漂移"分支）、以及
  每次配置变更。最坏 6 小时（converge.timer）自愈。

- 迁移：升级时把 v0.1.107 的 `retired-prefixes` 并入 seen 列表，不丢已有记录。


## [0.1.107] - 2026-11-14

> 修"重拨后 macOS 12 的 IPv6 打不开、必须重连 Wi-Fi"：换前缀时只换新的、
> 没废旧的。

### Fixed

- **前缀变更时持续废弃旧前缀**。PPPoE 重拨（以及开关 IPv6 触发的重拨）会让
  运营商下发新的 PD 前缀，但客户端手里的旧前缀 GUA **不会自己失效**——最后一次
  RA 给的是 1 小时 valid。macOS 12 会继续拿旧地址作源地址发包，而上游早已不
  路由该前缀 → 表现为"IPv6 打不开，断开 Wi-Fi 重连才好"。

  重连之所以有效，是因为 RS 让 networkd 立刻回一条 RA；但那条 RA **只含新前缀，
  不会废弃旧前缀**（实测告别 RA 长度只有 24 字节，不带 PIO）。

  现在 `90-dert-lan-prefix` 在检测到前缀变更时调用 `priv/prefix-retire.py`：
  把旧前缀登记到 `/var/lib/dert/retired-prefixes`，并生成
  `/etc/systemd/network/20-lan.network.d/90-retired-prefixes.conf`。networkd 会把
  这些前缀以 `valid/preferred = 0` 写进**每一条** RA（真机抓包确认）。

- **为什么用 drop-in 而不是一次性发一条撤销 RA**：睡眠中的设备会错过一次性
  报文；RA 是周期性的，客户端醒来发 RS 也能立刻拿到。TTL 取 24 小时，覆盖
  "笔记本关机过夜"这种最常见的错过场景。

### 说明

- 同一前缀又被分回来时自动撤销登记——否则同一前缀会同时以"正常"和"已废弃"
  两种身份出现在 RA 里，客户端会直接把正在用的地址停掉。
- 最多保留 8 条，按时间淘汰；状态放在 `/var/lib/dert/`，重启后仍生效。


## [0.1.106] - 2026-11-13

> 修 CAKE 的两个参数：路由器上"每台设备公平"其实没生效。

### Fixed

- **cake 缺 `nat`**。tc 显示 `nonat`：路由器做 masquerade，cake 拿不到内网源地址，
  整条 LAN 被当成**一台主机**——单台设备下载就能吃满带宽，`triple-isolate`
  的公平排队等于白设。现在显式带 `nat`，cake 靠 conntrack 还原真实内网主机。

- **PPPoE 补 `overhead 8`**（6 字节 PPPoE 头 + 2 字节 PPP 协议）。此前
  `overhead 0` 让整形速率系统性偏高约 0.5%，延迟控制也跟着偏。

### 说明

- 两个参数都写成"先带 `nat` 试，失败退回不带 `nat`"：宁可少一个优化，
  也不能因为一个可选参数让整条整形没建起来（那才是真的 QoS 静默失效）。


## [0.1.105] - 2026-11-13

> 按实测恢复 MTU 1480，并保留已验证有效的 RA 参数。

### Changed

- `PPPOE_MTU` 1492 → **1480**，br-lan 同步 `IPv6MTUBytes=1480`。
  用 `ping6 -M do` 从 LAN 源地址测阿里与 Cloudflare，1492 是**通**的，
  所以这不是"路径 MTU 更小"；真正的原因是 1480 让客户端从一开始就不发接近
  上限的包——QUIC/UDP 没有 MSS 钳制，1500 字节的包只能靠 PTB 纠正，
  重传一次就足以让人感觉"慢"。
- 保留 `MaxRtrAdvIntervalSec=200` / `RouterLifetimeSec=1800`。

### 更正（v0.1.103/104 的错误结论）

- "设 `IPv6MTUBytes` 会导致 networkd 停发 RA"是**误判**。当时设备上没装
  tcpdump，`timeout 30 tcpdump ...` 直接 `No such file or directory` 失败，
  空输出被我当成了"没有 RA"。装上 tcpdump 后实测：RA 每 200 秒一条、
  lifetime 1800s、报文长度 80 字节（若有 MTU 选项会是 88），
  与 `IPv6MTUBytes` 无关。
- 15:51 那次全屋 v6 断，最可能的原因是 `RouterLifetimeSec=600` 与发送间隔
  上限 600 秒相同：客户端在下一条 RA 到达前就把默认路由过期了。


## [0.1.104] - 2026-11-13

> 真正的元凶：**RA 的寿命和发送间隔都是 600 秒**，客户端在下一条 RA 到达前
> 就把默认路由过期了。v0.1.103 的 MTU 回滚只是排除了一个变量，不是修复。

### Fixed

- `[IPv6SendRA]` 增加 `MaxRtrAdvIntervalSec=200`，`RouterLifetimeSec` 600 → 1800。
  按 RFC 4861 的建议取 3 倍关系（寿命 ≥ 3 × 最大间隔）。

  真机证据链：15:41 networkd 发过一条 RA → **15:51 全屋 v6 断**（正好 600 秒，
  等于 RouterLifetimeSec）→ 断开 Wi-Fi 重连立刻恢复（RS 会触发一次 RA）。
  回滚 MTU 并把 `/proc/sys/net/ipv6/conf/br-lan/mtu` 复位到 1500 后，
  30 秒抓包仍然没有 RA——说明与 MTU 无关，是 RA 的定时余量不足。

### 说明

- 寿命 1800 秒意味着路由器真的挂掉时，客户端会多等最多 30 分钟才放弃这条
  默认路由。考虑到局域网内通常还有 v4 可用，这个代价可以接受。


## [0.1.103] - 2026-11-13

> **回滚 v0.1.102 的 MTU 改动**：给 br-lan 设 `IPv6MTUBytes=1480` 之后
> networkd 不再发 RA，全屋 v6 在 600 秒后一起断。

### Fixed（回滚）

- `PPPOE_MTU` 回到 **1492**，`20-lan.network` 不再写 `IPv6MTUBytes`。
- **保留** v0.1.102 里真正有用的那部分：`forward_mss` 按协议族拆开，v6 显式
  钳到 `1432`（=1492-60）。原来的 `rt mtu` 在 v6 下压不住客户端的 1440，
  导致每条新连接都要先挨一次 PTB。

### 事故经过

15:38 升级 0.1.102 → 15:40 重拨 → 15:41 networkd 拿到 PD 并发了 RA →
**15:51 全屋 v6 断**（正好 600 秒，等于 `RouterLifetimeSec`）。断开 Wi-Fi
重连立刻恢复——因为 RS 会触发一次 RA。

排查证据：`/proc/sys/net/ipv6/conf/br-lan/mtu` = 1480，而
`/run/systemd/netif/links/6` 里**没有 `IPV6_SEND_RA`**；重启 networkd 后
25 秒抓包也没有任何 RA。回滚并重启 networkd 后 RA 恢复。

### 教训

- 改 MTU 这类"看起来只影响效率"的参数，必须验 RA/ND 这类控制面，
  不能只验 ping 通不通。
- 判断路径 MTU 不能用 `ping6 -s N` 直接算：N 是数据长度，总长要加
  8（ICMPv6 头）+ 40（IPv6 头）。v0.1.102 的"1484 边界"就是漏算这 8 字节
  得出的错误结论，据此做的改动本身就是错的。


## [0.1.102] - 2026-11-13

> 修"非白名单设备 IPv6 有点慢"：RA 宣告的 MTU 与实际链路不一致，叠加 v6 的
> MSS 钳制失效，导致**每条新连接都白丢一个 RTT**。

### Fixed

- **v6 每条新连接都要挨一次 ICMPv6 PTB**。三个事实叠在一起：

  1. br-lan 的 RA 没有宣告 MTU，客户端按链路 MTU **1500** 发包
  2. ppp0 只有 **1492**，路由器只能回 PTB 让它降到 1492
  3. `forward_mss` 用的是 `tcp option maxseg size set rt mtu`，在 v6 下算出来的
     值比客户端自己的 1440 还大 → **钳制形同虚设**，客户端继续发 1500

  结果：每个新建的 TCP 连接、每个 QUIC 连接都要先丢一次包再重传，一个页面上
  几十个连接就是几十次往返。真机实测 `Icmp6OutPktTooBigs` 改前 20 秒 **+5**，
  改后 30 秒 **+0**。

  **白名单设备不受影响**：它们的流量进 tun，由 sing-box 用户态接管，MTU 走
  sing-box 自己的逻辑，不依赖内核这条路径——这也解释了为什么只有非白名单慢。

### Changed

- `PPPOE_MTU = 1480`（`priv/netgen.py`），三处共用同一个常量：

  1. `peers/dert` 的 `mtu` / `mru` → PTB 从此说 1480，转发也被限在 1480
  2. `20-lan.network` 的 `IPv6MTUBytes=1480` → RA 直接宣告，客户端第一个包
     就用 1480，不再有 PTB 往返（仅 PPPoE + v6 启用时加）
  3. `forward_mss` 按协议族拆开：v4 继续 `rt mtu`，v6 显式 `1420`（=1480-60）

### 说明

- 为什么取 1480 而不是 1492：留 12 字节余量给少数路径更小的目的地（部分
  运营商链路会过滤 PTB）。代价是 v4 每包少 12 字节，包数约多 0.8%。
- 生效需要重拨一次 PPPoE（peer 文件只在拨号时读）；RA 的新 MTU 要等下一次
  RA 周期才到达客户端。
- 排查记录：一开始把 `ping6 -s 1444` 通、`1445` 不通误判成"路径 MTU 1484"，
  实际是漏算了 ICMPv6 的 8 字节头——1444+8+40 = 1492，正好等于当时的接口
  MTU，边界一直在本地而不在链路上。

## [0.1.101] - 2026-11-13

> 修一个只在 TCP DNS 上暴露的缺陷：白名单入口只监听了 UDP。

### Fixed

- **`00-dert-dns.json` 的 `dns-in` inbound 写死了 `"network": "udp"`**，
  而 `inet dert_dns` 里白名单那两条规则是 `meta l4proto { tcp, udp } th dport 53`
  ——TCP 也被 DNAT 到 5353，可那个端口上没有 TCP 监听。真机实测：

  ```
  ss -lntup | grep 5353              → 只有 udp
  dig +tcp @192.168.10.1 -p 5353 ... → Connection refused
  ```

  客户端在响应被截断后重试、或强制走 TCP 时，解析直接失败且**不会自动退回
  UDP**，表现是"某个域名解析不了"，很难定位。因为 fakeip 应答很小、
  `HTTPS/SVCB` 查询又被 reject，触发概率低，但真碰上就是怪问题。

  去掉 `network` 字段后 sing-box 默认同时监听 tcp+udp（在设备上用临时实例
  在 15353 端口验证过：`udp UNCONN` + `tcp LISTEN` 同时存在），nft 规则不用改。

### Added

- `test_dns_split` 增加断言：5353 入口不得把 `network` 固定成 `udp`。

### 注意

- 这个修复**只在 sing-box 启动时生效**：片段由 `ExecStartPre` 生成，而升级流程
  按契约不会重启 sing-box。升级后需要手动 `systemctl restart sing-box` 一次
  （实测：升级完成但未重启时，5353 仍只有 UDP；重启后 tcp+udp 同时在听）。


## [0.1.100] - 2026-11-13

> 复查遗留项 2、3：漂移判定补上权限；恢复/出厂失败会回滚。

### Fixed

- **漂移判定只看内容，不看权限**。`apply-all --convergent` 逐文件 `cmp` 内容，
  权限被改坏（比如 `chap-secrets` 从 600 变 644——那是明文宽带密码）会一直报
  "无漂移"，永远不修。现在同时比 `st_mode & 0o777`，不一致就按漂移走全量部署
  （部署本身会 `chmod` 回清单里的 mode）。

- **`backup-restore` / `factory-reset` 收敛失败时不回滚**。配置已经换成备份里
  那份（或出厂默认），但没生效——下次开机会拿它去收敛，设备停在半坏状态。
  现在失败会：退回快照 → 重新生成并部署 → 重启受影响服务；仍失败才报
  `APPLY_FAIL` 并指向 `lastgood`。

  报错格式也修了：原来把 apply-all 的 JSON 原样 `print` 出去，与自己的错误
  JSON 叠成两行，面板 `json.loads` 会直接失败（`PRIV_MALFORMED`）。现在只输出
  一条 JSON，底层原因（`NETGEN：网口 enp9s0 不存在`）放进 `hint`，信息不丢。

### Added

- `test_mode_drift_triggers_full_deploy`：只改权限也要被判定为漂移并修回。
- `test_apply_failure_rolls_back` / `test_factory_apply_failure_rolls_back`：
  收敛失败后配置必须回到操作前，且 `hint` 里带底层错误码。


## [0.1.99] - 2026-11-13

> 复查遗留项 1：`snapshot` / `restore` 三份实现合并到 `privlib`。

### Changed

- **快照/回滚逻辑从三处合并成一份**。原来 `configops`（保存前）、`applyops`
  （存量迁移前）、`backupops`（恢复/出厂前）各写了一遍，逻辑当时一致——但
  "一致"是靠人盯着的，轮转规则（只留最近 5 份）尤其容易改漏一处。本项目已经
  因为"清理条件与生成器各写一遍"出过一次 LAN→WAN 的事故，同类问题不再留。
  现在实现在 `privlib.snapshot()` / `privlib.restore()`，三处都是三行委托。

  行为不变：`shutil.copy2`（等价 bash 的 `cp -a`，保留 mode/mtime）、按 mtime
  轮转到 5 份、`restore` 没有快照时返回 `False`（调用方不能当成功）。

### Changed（测试）

- `test_lib_functions.Lastgood` 改为直接测 `privlib` 的那份实现；三个调用点的
  行为仍由各自的用例覆盖（configops 回滚断言 `pre-s`、applyops 迁移断言 `pre-m`、
  backupops 出厂断言 `pre-factory-`）。


## [0.1.98] - 2026-11-13

> 全量复查（pyflakes + 逐模块对照 bash 原实现 + 真机跑未覆盖路径）修掉的问题。

### Fixed

- **`config-set firewall` 在老配置上会抛 traceback**。只提交 `portfw` 时走的是
  `cfg["firewall"]["portfw"]`，而配置里没有 `firewall` 段就 KeyError——面板只能
  看到"特权服务返回异常"，看不出是缺段。两处都改成 `setdefault`。
  （bash 原实现同样有此问题，属于存量缺陷。）

- **`apply-all` 往 stdout 打人类可读文本**。补 sing-box drop-in 时
  `maintain_singbox_dropin()` 会 `print("wait-wan: 已为 sing-box 安装…")`，
  而 apply-all 的 stdout 是面板解析的 JSON——多一行就变成 `PRIV_MALFORMED`
  （`backup-restore` 内部也会调它）。改成 `log_event` 走 journald。

- **`upgrade-apply` 的版本号解析**：`VERSION` 里若带 CR/内部空白，`strip()`
  去不掉，会拿脏字符串去比较版本。改成与 bash 的 `tr -d '[:space:]'` 等价。

- **升级包成员判定过松**：`VERSION` 用的是前缀匹配，`VERSION.old` 也算通过。
  改成精确匹配（`panel/`、`priv/` 仍用前缀）。

- **`gen_sudoers` 会静默漏掉解析不出的分支**。新加 `fooBar)` / `foo_bar)` 这类
  标签时，sudoers 少一条 → 面板"命令不允许"，而生成器一声不吭。现在除兜底
  `*)` 外，解析不出标签直接报错。

### Changed

- 清掉 pyflakes 报的死代码：`applyops`/`upgradeops`/`miscops`/`backupops`/
  `selftest`/`singbox` 的未使用导入，`container.py` 里赋值后从未使用的 `retried`。

### Added

- `test_firewall_without_existing_section`：老配置缺 `firewall` 段时只提交
  `portfw` 必须成功（而不是 traceback）。

### 真机复查（0.1.97 → 0.1.98 前）

| 路径 | 结果 |
|---|---|
| `config-set dhcp` / `static` 原值重存 | ok，config.json 哈希不变 |
| `config-set lan` 非法掩码 | `LAN_MASK_BAD`，配置未变 |
| 备份创建 + 恢复往返 | ok，哈希不变 |
| health | 14/14，0 失败单元 |


## [0.1.97] - 2026-11-13

> 审计第 8 条：sudoers 从 `dert-priv *` 收紧成逐子命令列举，**由分发表生成**。

### Changed

- **`sudoers/dert` 改为生成文件**：`.github/scripts/gen_sudoers.py` 读
  `priv/dert-priv` 的 `case "$CMD" in` 分发表，产出 `Cmnd_Alias`。19 个子命令
  逐个列举；不带参数的只允许裸形式（传参直接不匹配），带参数的额外给一条 ` *`。

  为什么用生成而不是手写：收紧白名单的经典风险是**漂移**——新增子命令忘了同步，
  面板就整条断链，而"新增子命令"恰好发生在升级通道里，用户没法从面板自救。
  现在唯一事实来源是分发表，守卫 **P3-36** 比对生成结果，CI 阶段就拦住。

- 三个会读 `sudoers/dert` 的通道都走 visudo 校验后再落盘：`install.sh`、
  `upgrade-apply`（`_sync_etc_hooks`）、卸载。语法错进不去。

### Added

- 守卫 **P3-36**：`sudoers/dert` 必须等于从分发表生成的结果；顺带断言分发表
  至少能解析出 19 个子命令（防止解析器失效后"空对空"通过）。


## [0.1.96] - 2026-11-13

> **P4-3 收尾**：剩下 4 个 root 单元也硬化，`dert-pppoe` 是唯一碰 WAN 的。

### Changed

- `dert-pppoe` / `dert-dns-sync` / `dert-pppoe-watchdog` / `dert-singbox-cleanup`
  补上同一套硬化（`ProtectSystem=true`、`ProtectHome`、`PrivateTmp`、
  `NoNewPrivileges`、`RestrictSUIDSGID`、`LockPersonality`、`ProtectControlGroups`、
  `ProtectKernelLogs`）。到此前 7 个 root 单元全部覆盖，面板按设计仍不硬化。

  `dert-pppoe` 的风险点单独确认过：`pppd` 只读 `/etc/ppp/peers/dert` 与
  `chap-secrets`，写的是 `/run/ppp*.pid`；`ProtectSystem=true` 只锁 `/usr`、`/boot`，
  不影响读 `/etc`、也不影响 netlink。真机重启拨号验证：ppp0 正常回来。

### Changed（守卫）

- **P3-34 改为自动枚举**：任何没有 `User=` 的 `.service` 都算 root 单元，
  必须带 `ProtectSystem=true` + `NoNewPrivileges=true` 且不得用 `strict`/`full`。
  新增单元忘了硬化会直接红——不用再手工维护名单。


## [0.1.95] - 2026-11-13

> 工业化重构 **P4-2**：关键事件结构化日志。

### Changed

- 所有事件日志统一成 **`event=<snake_case> key=value …`**，前缀仍是
  `dert-<域>`。Python 侧新增 `privlib.log_event(tag, event, **fields)`
  （字段按字母序、值含空格/中文自动加引号、换行替换成空格），bash 侧手写同格式。

  为什么值得改：原来全是自由文本（`"DNS 分流表已重建：白名单 3 台"`），
  想回答"最近 24 小时重建了几次分流表""哪些生成物被清理过"只能拿正则猜中文，
  标点一改就猜不到。现在：

  ```sh
  journalctl -t dert-dns-split --since -24h | grep -c 'event=table_rebuilt'
  journalctl -t dert-upgrade  --since -7d  | grep 'event=sig_reject'
  ```

  改动覆盖 9 个文件、16 个日志点：`applyops`（清理/ifupdown）、`upgradeops`
  （验签通过/拒绝/无签名/selftest/收敛）、`netwatch`、`dns-split-sync`、
  `dns-split-teardown`、`pppoe-watchdog`、`singbox-cleanup`、
  `90-dert-lan-prefix`。

### Added

- 守卫 **P3-35**：任何 `logger -t dert-*` 行必须带 `event=`，自由文本不许回潮。
- `tests/test_lib_functions.py::LogEvent`（6 个用例）：字段序稳定、含空格加引号、
  换行不能漏进日志、空值/None、数字不加引号。
- `CONTRIBUTING.md` 新增「日志约定」：格式、三条硬规则、聚合示例。


## [0.1.94] - 2026-11-13

> 修 CI：`upgradeops` 的暂存目录没走测试缝，本地绿、CI 红。

### Fixed

- **`upgrade-apply` 的暂存/回退目录写死了 `/usr/local/lib`**。它本该是
  "安装根的父目录"（同一文件系统才能原子 rename），写死之后测试缝 `DERT_LIB`
  对它不起作用：测试跑起来会去写**真实**的 `/usr/local/lib`。macOS 上那个目录
  恰好可写，所以本地 270 个用例全绿；CI 是普通用户，三个用例直接
  `PermissionError: '/usr/local/lib/.dert.upgrade.2344'`。

  改为 `STAGE_PARENT = dirname(LIB)`：生产仍是 `/usr/local/lib`（逐字符等价），
  测试则落在临时根的父目录里。

- **顺带把 `/etc`、`/usr/local/bin`、`/var/log` 的写入也接上 `DERT_FSROOT`**。
  此前这些路径在测试里是硬编码的，靠"写失败被 `except OSError` 吞掉"蒙混过去；
  现在测试跑完不会在真实系统上留任何痕迹。延迟重启（`systemd-run`）在测试缝下
  直接跳过，不再往真实 systemd 里排任务。

### Added

- `test_never_writes_outside_install_root`：用 `os.makedirs` 的探针记录整条换目录
  流程碰过的路径，断言**没有任何一条**落在 `/usr/local/lib` 下。这条在 macOS 上也
  会红——它不依赖"目标目录是否可写"，直接锁住"不许写出安装根"这个不变量。


## [0.1.93] - 2026-11-13

> 首个带签名的发布：验证 GitHub Actions 用 `DERT_SIGNING_KEY` 产出 `.sig` 的完整链路。
> 无代码改动。

## [0.1.92] - 2026-11-13

> 工业化重构 **P4-3**：root 单元硬化。

### Changed

- **`dert-apply` / `dert-converge` / `dert-netwatch` 加硬化**：
  `ProtectSystem=true`、`ProtectHome=true`、`PrivateTmp=true`、
  `NoNewPrivileges=true`、`RestrictSUIDSGID=true`、`LockPersonality=true`、
  `ProtectControlGroups=true`、`ProtectKernelLogs=true`。

  **为什么只到 `ProtectSystem=true`**：`full`/`strict` 会把 `/etc` 挂成只读，
  而这三个单元的整条流水线就是在写 `/etc`（networkd / dnsmasq / nft / ppp）；
  更麻烦的是 sudo 调起的子进程会继承同一挂载命名空间，等于把配置写入整条断掉
  （这条此前真机验证过）。`true` 只锁 `/usr` 与 `/boot`，挡的是"改二进制/改单元"，
  不碰配置——收益明确，风险为零。

- **面板单元删掉了一条空操作**。`ReadWritePaths=/var/lib/dert /run/dert` 在没有
  `ProtectSystem` 时**不生效**（该指令只对已被挂成只读的路径起作用），留着等于
  给了假的安全感。面板**不能**加 `ProtectSystem`：sudo 调起的升级要整目录替换
  `/usr/local/lib/dert`；也不能加 `NoNewPrivileges`：会废掉 setuid sudo。
  这两条都写进注释和守卫了，避免以后有人"顺手补齐"。

### Added

- 守卫 **P3-34**：三个 root 单元必须带 `ProtectSystem=true` + `NoNewPrivileges=true`
  且**不得**出现 `strict`/`full`；面板反之（不得出现这两个指令）。按"非注释行"
  匹配——注释里解释过这些值，子串匹配会假绿。


## [0.1.91] - 2026-11-13

> **P2 收官**：入口脚本只剩分发、权限与锁。

### Changed

- **`dert-priv` 246 → 76 行**（相对最初的 1,822 行是 **−96%**）。剩下的内容只有：
  子命令分发表、`require_root`、`acquire_lock`、每个子命令一行 `exec`。
- **`priv/miscops.py`（约 130 行）**：`session-secret` / `config-get` /
  `service-ctl`（podman / tailscale 的服务开关）。这三段此前是内联 Python 与
  case 分支，现在是可测试的模块。
  - `session-secret` 缺失时**报错而不是返回空串**：空密钥签出的 cookie 谁都能伪造
  - `config-get` 派生 `wan.has_password`（明文密码不在 config.json 里），
    并剔除 `notes`（备注只随备份流转，不回显给前端）
  - `service-ctl podman stop` 先停容器再停服务，否则先关 socket 会留下孤儿容器

### Added

- `tests/test_miscops.py`（12 个用例）：会话密钥缺失报错、`has_password` 派生、
  `notes` 不外泄、podman 启停顺序、enable 覆盖 `podman-restart.service`、
  未安装 Tailscale 的明确错误码。

### 说明

- 为什么不把入口也换成 Python：sudoers 白名单、升级通道的自我替换、真机已部署
  的路径都指向这个文件——**入口本身不能成为重构的风险点**。这一层的存在是设计，
  不是没做完。


## [0.1.90] - 2026-11-13

> 升级通道换 Python 实现后的第一轮真机验收，抓到一个**跨版本的既有缺陷**。

### Fixed

- **升级后的延迟重启会让收敛失败一次**。`upgrade-apply` 在返回前安排"3 秒后
  重启面板 + 收敛"，但此时升级进程仍持有 `apply.lock`（fd9，随 `exec` 继承）。
  收敛起来后 `flock -n` 必然 BUSY → 退出 1 → `dert-converge.service` 被标 failed
  → health 的 `failed_units` 检查直接变红（真机 2026-11-13 实测，日志：
  `run-p*.service: Job for dert-converge.service failed because the control process
  exited with error code`）。

  BUSY 不是真故障，是"另一次配置变更正在进行"。给收敛单元加
  `Restart=on-failure` + `RestartSec=15`（15 秒后升级进程早已退出），并用
  `StartLimitIntervalSec=0` 关掉启动频率限制，避免"重试几次后被判 start-limit-hit"
  这种更难查的失败。同一把锁被面板保存 / 备份恢复占用时，收敛也会自愈。


## [0.1.89] - 2026-11-13

> 工业化重构 **P2-9**：升级通道（check / download / apply）抽成独立模块。
> 这是最后一个域，也是最危险的一个——它是**唯一会替换自身**的路径。

### Changed

- **`priv/upgradeops.py`（约 460 行）**：`upgrade-check` / `upgrade-download` /
  `upgrade-apply`。`dert-priv` 从 **617 行降到 246 行**（−371）。

  逐条保留的顺序（每一步都在防一种具体的坏结局）：
  1. **先验来源再验结构**：签名 → 成员白名单 → 版本。sha256 只能证明"没被改坏"，
     不能证明"是谁发的"——镜像被劫持时可以同时替换包和哈希
  2. **成员清单先取到变量再喂校验器**。不能 `tar -tzf | tarcheck`：tar 对含 `..`
     的成员会往 stderr 打 "Removing leading ..."，合并进管道后那行会被当成成员名
     （真机 2026-11-12 实测踩到，两个方向都错）
  3. **暂存区必须在被替换目录之外**：放里面会随 mv 一起改名，第二步 mv 必 ENOENT
  4. **换目录用 mv 两步**：`mv dert → dert.prev.$$` 成功后才 `mv stg → dert`，
     失败可原路退回（UPG_SWAP）
  5. **布局对齐**：install.sh 是扁平布局、升级包保持 `priv/` 子目录，换目录后必须
     把 `priv/*` 铺回根，否则 sudo 调用的 `/usr/local/lib/dert/dert-priv` 直接消失
  6. **升级后立即收敛**：生成物是 netgen 的产物，等最长 6h 的 timer 才重生成，
     期间旧键/旧语法还在跑（真机实录：v0.1.44 升级后 IPv6"升级了也没修好"）
  7. **延迟重启面板**：先把本次响应送达客户端，3 秒后再重启

- `download` 的 sha256 改用 `hashlib` 计算（原来 shell 出去调 `sha256sum`）；
  `.sha256` 按 `errors="replace"` 读——伪造/损坏的校验文件是二进制时，
  要走到"不匹配"，不能抛异常。

### Added

- `tests/test_upgradeops.py`（24 个用例）：版本比较、清单式检查、非 https 拒绝、
  缺 sha256 拒绝、sha 不匹配拒绝、降级拒绝、路径穿越/未知目录/软链/缺
  VERSION·panel·priv/包内 Python 语法错误 全部拒绝、缺公钥但带签名拒绝，
  以及**换目录 + 扁平化布局**的正向验证（在临时安装根里真跑一遍）。

### 说明

- `dert-priv` 从 1,822 行降到 246 行（−86%），`lib.sh` 从 395 行降到 86 行。
  剩下的 bash 只有：子命令分发、root 检查、排他锁、几个一行的 exec 包装。


## [0.1.88] - 2026-11-13

> 工业化重构 **P2-8**：配置写入流水线（config-set）抽成独立模块，
> 并把 `lib.sh` 里已经没人调用的 apply 副本**整段删掉**。

### Changed

- **`priv/configops.py`（约 300 行）**：校验 → 快照 → 合并 → 落盘 → 生成 →
  预检 → 部署 → 重启 → 验证 → 失败回滚。`dert-priv` 从 **809 行降到 617 行**。

  逐条保留的判断（每条都对应一次事故或一次投诉）：
  - **先落盘再生成**：netgen 读固定路径，不先写盘就会用上一版配置生成
    （复审 v0.1.2 P-01：改了不生效，界面却显示保存成功）
  - **`pre_wan` 在写盘前采样**，语义是"改动前 WAN 是否可用"；`section == wan`
    时无条件验证（首次配 PPPoE 时 pre_wan=no，恰恰最需要报错）
  - **PPPoE 只在必要时重拨**：模式变 / 账号变 / 重绑网口 / PPPoE 下改 IPv6 /
    本次填了新密码。改个 DNS 就断线重连是不可接受的
  - **失败回滚并重放旧配置**，PPPoE 失败还要从 journald 区分"认证被拒"与
    "服务器无响应"——这两种的处理动作完全不同
  - 密码只进 `secrets.json`（不进 config.json、不进 argv）

- **`priv/lib.sh` 395 → 86 行**：只剩「JSON 协议 + root 检查 + 排他锁」。
  apply 流水线（`apply_stage_files` / `preflight_stage` / `run_actions` / `svc_*` /
  `wait_wan` / `plan_for_section` / `snapshot_lastgood` / `cleanup_*` 等）在
  v0.1.87 搬到 Python 后已无调用方，**故意不保留 bash 副本**：同一套判断留两份
  实现，迟早漂移——本项目就因为"清理条件与生成器各写一遍"出过 LAN→WAN 事故。

- `privlib.write_bytes()`：字节级原子写，`backupops` 与 `configops` 共用。

### Fixed

- **重绑网口后不重拨 PPPoE**（重构时照出来的既有缺陷）。`plan_for_section("bind")`
  的动作串里没有 `pppoe`，而代码里又有「bind + PPPoE → pppoe_needed」的判断——
  判了却执行不到。后果：`/etc/ppp/peers/dert` 里的 `nic-<口>` 已按新绑定改写，
  pppd 仍握旧口（`pppd call dert` 只在启动时读 peer 文件）。已把 `pppoe` 补进
  bind 的动作串，重拨仍由 `pppoe_needed` 把关，非 PPPoE 模式不会多一次重启。

### Added

- `tests/test_configops.py`（14 个用例）：密码只进 secrets、只改 DNS 不重拨、
  改账号/模式/重绑要重拨、WAN 不通回滚、firewall 部分提交、升级源只保存不应用、
  服务失败回滚到修改前配置并留快照。
- `tests/test_lib_functions.py` 改写：原子写/快照/轮转/动作计划的断言迁到
  Python 侧（`privlib` / `configops`），bash 侧只留协议与路径测试。


## [0.1.87] - 2026-11-13

> 工业化重构 **P2-7**：系统收敛流水线（apply-all）抽成独立模块。
> 这是**开机路径**——`dert-apply.service` 每次启动跑一遍，`dert-converge.timer`
> 每 6 小时跑一遍，config-set / 备份恢复 / 升级后也各跑一遍。所以这一版的原则
> 是逐条对齐，不是顺手优化。

### Changed

- **`priv/applyops.py`（约 420 行）**：生成 → 漂移判定 → 预检 → 部署 → 清理 →
  重启 → 复检的完整流水线，连带 `run_actions` / `svc_*` / `preflight` /
  `cleanup_ifupdown` / `sync_dns_upstream` / `maintain_singbox_dropin`。
  `dert-priv` 从 **913 行降到 809 行**（−104）。

  逐条保留的规则（每条都对应一次真机事故）：
  - 生成器 stderr 留档（`netgen.err`）：结构损坏时 manifest 是空的，没 stderr
    就只剩"配置生成失败"和一句指向空白的 hint
  - **无漂移分支绝不重启 networkd/dnsmasq**：开机全量重启会让 LAN 客户端闪断
  - **无漂移分支必须删 stage**：漏了就在 `/run/dert` 留一份完整生成物，
    其中 `etc/ppp/chap-secrets` 是明文宽带密码（真机 2026-11-09 实测）
  - **无漂移分支不能 start dnsmasq**：收敛单元 `Before=dnsmasq`，同步 start 会与
    排序中的启动任务互相等待形成死锁（真机实测）
  - 预检失败透传 `nft -c` / `dnsmasq --test` 原文
  - 存量 `/26` 掩码自动迁回 `/24` 后重试，迁移前留快照
  - 锁与 root 检查留在 bash 入口：`exec` 后子进程继承 fd，`_DERT_LOCK_HELD`
    让嵌套调用跳过重取，锁语义与拆分前完全一致

- **`priv/netgen.py` 支持 `DERT_FSROOT`**：数据路径跟随测试缝（未设时逐字符等价）。
  有了它，"生成 → 预检 → 部署"整条流水线能在临时根里真跑，不必把 CONFIG_PATH
  打桩（打桩就绕过了真实入口的 schema_check）。

### Added

- `tests/test_applyops.py`（17 个用例）：临时根 + 假 `systemctl`/`nft`/`dnsmasq`，
  真跑 netgen 与 cleanup，覆盖预检两种失败、部署 mode、ifupdown 残留清理、
  动作顺序与失败归集、PPPoE 启停、全量部署、生成失败打标记、预检透传、
  `/26` 迁移、无漂移（断言**没有**重启 networkd/dnsmasq 且 stage 已删）、
  有漂移触发全量。

### Changed（守卫）

- P3-12b / P3-13 两条"对 bash 源码做正则"的断言**行为化**：直接跑流水线再断言
  副作用。字符串断言棘轮 134 → 132。


## [0.1.86] - 2026-11-13

> 工业化重构 **P2-6**：备份 / 恢复 / 恢复出厂抽成独立模块。

### Changed

- **`priv/backupops.py`（约 300 行）**：`backup-create` / `backup-restore` /
  `factory-reset` 三条不可逆路径。`dert-priv` 从 **1035 行降到 913 行**（−122）。

  这次不是"搬代码"，重点是把**判定顺序**显式写出来（原来散在 140 行 bash 里，
  读代码时很难确认"到底先校验还是先落盘"）：

  1. 成员白名单（`backupcheck.py`，先 `normpath` 归一）
  2. 拒绝 `[lh]` 链接条目（防包内软链指向 /etc 写穿）
  3. `netgen --schema` 结构预检 —— **必须在落盘之前**
  4. 原子落盘（已有文件沿用原 mode，新文件 600）
  5. 按 secrets 真实内容重算 `wan.has_password`
  6. 收敛；收敛失败**原样透传**输出（netgen 的 KeyError 不能被吞成"失败"）

- **`apply-all` 改为子进程调用**：Python 侧用 `_DERT_LOCK_HELD=1` 让子进程跳过
  重取锁（`lib.sh:acquire_lock` 本就为嵌套调用留了这个缝）。锁仍由外层持有，
  没有引入第二个锁文件。

### Added

- `tests/test_backupops.py`（18 个用例）：临时数据根 + 构造 tar 包，覆盖
  secrets 包 / `./` 归一绕过 / 白名单外成员 / 软链条目 / 空包 / 截断包 /
  结构非法 / 收敛失败透传 / `has_password` 纠偏 / PPPoE 缺密码提示 /
  出厂保留升级源且清宽带密码保留管理密码 / 网口发现。

### Fixed

- **macOS 上恢复必失败**：`tar --no-overwrite-dir` 是 GNU tar 选项，bsdtar 不认，
  整条恢复直接报"文件可能损坏"。改为**能力探测**：支持就用，不支持少用一条，
  而不是把选项写死（生产 Debian 行为不变）。


## [0.1.85] - 2026-11-12

> 工业化重构 **P2-5**：sing-box 控制面域抽成独立模块。

### Changed

- **`priv/singbox.py`（405 行）**：10 个子动作（status / ctl / autostart /
  install / sub-set / sub-update / schedule / uninstall / dash-set / check）。
  `dert-priv` 从 **1284 行降到 1035 行**（−249）。

  注意边界：DebRT **不管 sing-box 的流量路径**（分流由 nft + 配置决定），
  这里只有"控制面"——服务开关、订阅地址、自动更新、面板地址。

  逐条保留踩过的坑：
  - `clash_api` 在新版是 `services[type=api]`，且 **listen 与 listen_port 是两个
    字段**——只取 listen 会拼出 `host:0.0.0.0` 死链（N-01）
  - `sub-set` 的 URL 必须过 `validate.url_host_blacklisted`，否则可指向内网
  - `schedule` 写 `/etc/cron.d/sing-box` 只替换 sb update 那一行，**其它任务的行
    必须保留**；只剩我们这行时整文件删除

### Added

- `tests/test_singbox.py`（18 个用例）：假根 + 假 `sing-box`/`systemctl`/`ps`，
  覆盖 API controller 的两种来源（services / clash_api）、订阅地址脱敏、
  cron 时间解析、内网 URL 拒绝、cron 行的精确增删、时间格式校验等。

### Fixed

- **测试跨模块污染**。`test_singbox` 改了 `os.environ["PATH"]` 与 `privlib`
  的模块级路径变量，`test_container` 打了 `privlib.run` 的桩——都**没恢复**，
  导致全量跑时互相干扰（单模块跑全绿、合起来 1 个失败）。已加 `tearDown` 恢复。
  这是"共享模块 + 打桩"必然会遇到的坑，值得记。

### 验证

- **真机双跑**：`singbox-ops status` 新旧实现 **13 个字段完全一致**
  （含 API controller、订阅脱敏、cron 时间、tailscale endpoint、dashboard）。

## [0.1.84] - 2026-11-12

> 工业化重构 **P2-4**：备份包成员校验抽成可测模块。

### Security

- **`priv/backupcheck.py`**：从 `do_backup_restore` 抽出的恢复前校验。
  这段踩过一类很隐蔽的绕过：

  ```
  etc/dert/./secrets.json     ← 绕过「secrets 精确匹配」
  etc/dert//secrets.json      ← 同上
  ```

  而 tar 解压时会自动规范化成 `etc/dert/secrets.json`，端到端可以写穿（复审 N-01）。
  判定必须**先 `posixpath.normpath` 归一，再比对**。

  抽出来之后这两个绕过有了专门的行为用例锁住——此前只有源码字符串断言，
  证明不了它真的拦得住。

### Added

- `tests/test_backupcheck.py`（11 个用例）：正常备份、secrets（含 `./` 与 `//`
  两种绕过变体）、绝对路径、`..` 穿越、白名单外文件、notes.json 的精确放行、
  空清单、行尾换行，以及 CLI 退出码。

### Notes

- `dert-priv` 里那段内联 Python 换成一行调用；恢复流程其余部分（解压、预检、
  应用）仍在 bash，属后续域。

## [0.1.83] - 2026-11-12

> 工业化重构 **P2-3**：认证域抽成独立模块。这批是**安全语义**，
> 此前只有源码字符串断言，现在可以真跑了。

### Changed

- **`priv/authops.py`（190 行）**：`do_auth_verify` + `do_passwd_set` 的实现。
  `dert-priv` 从 **1409 行降到 1284 行**（−125）。这两段原本就是纯 Python，
  属于**字面搬迁**，限速/锁定/容错语义一字未改。

### Added

- `tests/test_authops.py`（14 个用例）。此前这些只有字符串断言，现在能验证：
  - 连续失败 **20 次锁 300 秒**（全局兜底，阈值放宽以免被用来锁死管理员）
  - **锁过期必须清零计数**——否则跨天累积成「错一次立刻锁 5 分钟」
  - 状态文件损坏不能导致登录不可用（限速计数可丢，登录不能丢）
  - 密码记录损坏要报 `AUTH_RECORD_BAD`，与「密码不正确」区分开
  - 改密成功**必须轮换 `session_secret`**（被盗 cookie 立即失效）
  - 原密码错 5 次锁 5 分钟；验证通过即清零计数
  - 新密码长度、secrets 缺失等边界

### 验证

- **真机语义比对**：用假根构造已知密码，`auth-verify` 的正确/错误/空密码
  三种输入，新旧实现输出的 JSON 语义一致、退出码一致、**失败计数副作用一致**。
- 真实 secrets 下冒烟：返回 `AUTH_BAD`、计数 +1、面板 200。

### Notes

- **JSON 格式化风格统一了**：旧实现两条错误路径风格不一致（bash `die` 是紧凑
  无空格，`json.dumps` 是带空格 + 转义中文）。新实现统一为带空格 + 中文，
  面板走 `json.loads`，语义无差别。这也是双跑改成「语义比对」而非「逐字节」的原因。

## [0.1.82] - 2026-11-12

> 工业化重构 **P2-2**：容器（podman）域抽成独立模块。

### Changed

- **`priv/container.py`（335 行）**：`do_podman_ops` + `do_podman_update` 的实现。
  `dert-priv` 从 **1567 行降到 1409 行**（−158）。

  这不是字面搬迁——原实现是 bash 编排，改写成了 Python，**逐条对应**这些细节：
  - 只读操作（`status`/`list-*`/`compose-get`）**不进排他锁**：pull 最长 660s，
    拉大镜像期间若占锁，开容器页会 504（真机实测）
  - `list-images` 按**镜像 ID** 匹配使用状态，不按名字（容器 `.Image` 与
    images `.Names` 首个 tag 常拼写不一致，按名字会误报"未使用"）
  - `container update` 只回放端口/卷/环境变量/重启策略；**自定义网络或
    privileged 前置拒绝**并指路 Compose（否则重建"成功"但配置丢了）
  - 先建后删（审计 B-19）：新容器创建成功才动旧容器
  - `compose-deploy` 遇 cgroup slice 竞态（OCI "No such file or directory"
    且 State=created）重试一次

- **`priv/privlib.py`（共用基础设施）**：JSON 协议（`die`/`ok_out`）、数据根推导、
  `require_root`、`pod_lock`（flock + 超时）、`write_atomic`。域模块共用，
  避免各写一遍后漂移。

### Added

- `tests/test_container.py`（11 个用例）：用打桩替换 `podman` 调用，
  断言模块**调了哪些命令、输出什么 JSON**。重点覆盖上面两条坑：
  按 ID 匹配使用状态、特殊配置前置拒绝；另断言「create 必须在 stop 旧容器之前」。

### 验证

- **真机新旧双跑**：`podman-ops` 的 4 个只读子命令（status / list-containers /
  list-images / compose-get）与抽取前的实现**逐字节一致**。

### Notes

- 守卫「观察③ 容器更新缺前置拒绝」的断言位置改指向 `container.py`。
- 三个文件（dert-priv / container.py / privlib.py）**一次性部署**——
  v0.1.81 就是先传调用方后传模块把 status 弄坏过一次。

## [0.1.81] - 2026-11-12

> **P4（可观测）** + 一条防止"拆分漏部署"的结构守卫。

### Added

- **`dert-priv health`**（`priv/health.py`）：一条命令给出结构化健康状态，
  **只读、不改任何东西**，退出码 0/1 供监控直接消费（systemd timer / cron /
  Zabbix / node_exporter textfile 都能用）。

  14 项检查，每一项都对应一个真机踩过的坑：
  | 检查 | 为什么要有 |
  |---|---|
  | 5 个服务 active | 基本存活 |
  | 失败单元数 = 0 | 别只看单个服务 |
  | `dert_dns` 规则数 = 4 | 直接写 table 定义是**追加**，真机曾堆到 20 条 |
  | nft v6 目标 = 当前 GUA | 前缀变了没跟上 → 白名单 v6 DNS 全落空（437 个包） |
  | 5353 在监听 | fakeip 不可用时白名单 DNS 直接坏 |
  | `resolv.conf` 指向 127.0.0.1 | 被 pppd 改写后 `.lan` 解析失败 |
  | 收敛失败标记 | 面板要显示"上次收敛失败" |
  | 磁盘余量 ≥ 1GB | 升级包需要空间 |
  | 配置可解析 / 版本可读 | 基础 |

- `tests/test_health.py`（9 个用例）：用假 `systemctl`/`nft`/`ss`/`ip` 把
  **每一种不健康场景**单独构造出来，断言被正确识别且退出码为 1。

### Fixed

- **`dert-priv` 引用了不存在的模块时会直接坏掉**（真机实测踩到）。
  拆分后 `do_status` 变成 `exec python3 "${PRIV_DIR}/status.py"`，而我手工部署时
  先传了 `dert-priv`、后传模块，中间那段时间 `status` 报
  `can't open file '/usr/local/lib/dert/status.py'`。
  **升级通道本身是整目录替换，不会出这个问题**——是手工部署才踩到。

  新增守卫 P3-32：`dert-priv` 里 `${PRIV_DIR}/xxx` 引用的每个文件都必须在
  `priv/` 下真实存在；反向也断言 `install.sh` 仍用 `priv/*.py` 通配部署
  （改成逐个列举就会漏装新模块）。

## [0.1.80] - 2026-11-12

> 工业化重构 **P2-1（结构分层，第一个域）**：把 `status` / `selftest` 从
> `dert-priv` 抽成独立 Python 模块。

### Changed

- **`priv/status.py`（238 行）与 `priv/selftest.py`（33 行）**。这两段本来就是
  纯 Python，只是嵌在 bash 的 `<<'PYEOF'` 里——抽出来是**字面搬迁，逻辑一字未改**。
  `dert-priv` 从 **1822 行降到 1565 行**（−257）。

  抽出的直接收益：可以 `DERT_FSROOT=<假根> python3 priv/status.py` 真跑一遍，
  断言**输出什么**，而不是断言源码里有没有某个字符串。

- 模块自己从 `DERT_FSROOT` 推导数据根（`E/V/R`），不依赖 `lib.sh` 导出——
  这样既能被 `dert-priv` 调用，也能独立跑，两条路径结果一致。

### Added

- `tests/test_status.py`（9 个行为用例）：用假 `ip` 命令控制网络状态，覆盖
  WAN up/down、掩码换算、PPPoE「正在连接」、`converge_failed` 标记、
  NAT6 显示 ULA、端口角色、自检项与汇总。

### 验证

- **与抽取前逐字节比对**：`git archive HEAD` 取旧版，同一假根下
  `status` / `selftest` 输出完全一致。
- 模块独立跑 vs 经 `dert-priv` 调用，输出也完全一致。

### Notes

- 抽走后守卫 P3-11（硬盘温度取 `current`）指向的源码位置变了，改成读
  `priv/status.py`。**这正是 P1-2 想消除的脆弱性**——但这类断言只剩个位数，
  棘轮保证它不会变多。
- shellcheck 抓到 `SC2120`：两个子命令不接受参数，`"$@"` 是多余的，已去掉。

## [0.1.79] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第四阶段）**：把测试缝铺到 `dert-priv`。
> 这是本项目风险最高的一次改动——它是升级通道本身。

### Added

- **`dert-priv` 全部数据路径支持 `DERT_FSROOT`**。57 处硬编码 `/etc/dert` /
  `/var/lib/dert` / `/run/dert` 改为走 `DERT_ETC` / `DERT_VAR` / `DERT_RUN`
  （由 `lib.sh` 统一提供，未设时为空串 → 与原路径**逐字符等价**）。

  两类上下文分别处理：
  - **shell 上下文**（18 处）：直接引用变量。
  - **`<<'PYEOF'` 的 Python heredoc**（38 处）：引号 heredoc 不展开 shell 变量，
    每个块顶部注入一行 `E/V/R = os.environ.get(...)` 前置声明，字面量改为
    `E + "/config.json"` 形式。
  - **单引号 `python3 -c '...'`**（2 处）：同样走 `os.environ`。

- **`tests/test_dert_priv.py`（6 个行为用例）**：这个文件过去只能做源码断言，
  现在可以真的跑起来喂假根：
  - `config-get` 从假根读取、**不泄漏 secrets**（admin_pbkdf2 / pppoe_password /
    session_secret 都不得出现在输出里）、以 `has_password` 布尔代替明文
  - `status` 的 `converge_failed` 跟随假根（决定面板是否显示"上次收敛失败"）
  - 缝的隔离性：设了 FSROOT 就绝不能读到真实 `/etc`

### 验证方式（关键）

这类改动**不能只靠单测**——单测证明不了 shell 接线对不对（v0.1.76 就是这么翻车的）。
这次用**新旧并排双跑**：

1. 先把新文件放到 `/usr/local/lib/dert/dert-priv.new`，**不替换现役**
2. 同一批只读子命令两边各跑一次，逐字节比对：
   `config-get` / `status` / `compose-get` / `list-containers` / `list-images` / `version`
   → **6/6 完全一致**
3. 再造一个假根验证缝真的生效：
   - 新版 + `DERT_FSROOT` → 读到假根里的 `marker: FAKE-ROOT`、`converge_failed: true`
   - 新版 无 FSROOT → 读到真实配置、`converge_failed: false`
   - 现役 无 FSROOT → 与新版一致
4. 确认无误后才替换现役文件

### Notes

- **转换脚本本身踩了一个坑，值得记**：初版按「`<<TAG` 就是 Python heredoc」处理，
  结果把 `<<SBURL` / `<<SBDASH`（写 URL 文件）和 `<<'DPEOF'`（写 systemd unit）
  也当成 Python，往里注入了 `import os` 一行——那会直接把垃圾写进配置文件和
  unit。是「逐块做 Python 语法检查」发现的：3 个块报 SyntaxError。
  修正为**只有命令行含 `python3` 的 heredoc 才当 Python**。
- 中文提示文案（如 `"在系统上执行：rm /etc/dert/secrets.json ..."`）全部保留原样，
  替换时按「行内含 CJK 就跳过」规避。
- 棘轮预算不变（133）——本轮新增的全是行为断言。

## [0.1.78] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第三阶段）**：给 `lib.sh` 加测试缝，
> 让配置事务的核心函数第一次有运行时测试。

### Added

- **`DERT_FSROOT` 测试缝扩到 `lib.sh`**。只作用于**数据**路径
  （`DERT_ETC` / `DERT_VAR` / `DERT_RUN`），代码路径（`DERT_ROOT` / `DERT_SHARE`）
  不变——「程序在哪」和「数据在哪」是两件事。

  未设时是空串，`"${FSROOT}/etc/dert"` 与 `"/etc/dert"` **逐字符等价**，
  生产行为一字不变；设了就指向临时根。三个变量同时 `export`，供内联
  Python 用 `os.environ` 取（heredoc 是 `<<'PYEOF'` 形式，shell 不展开变量）。

- **`tests/test_lib_functions.py`（16 个行为用例）**。这些函数此前**没有任何
  运行时测试**，只有源码字符串断言：
  - `write_atomic`：内容/权限/自动建父目录/**不残留临时文件**/覆盖写
  - `snapshot_lastgood` / `restore_lastgood`：快照落盘、回滚还原、
    快照不存在时必须返回非零（不能静默成功）、**轮转只留 5 份**
  - `plan_for_section`：9 个配置段的重启映射（改错会导致"改了没生效"或
    "多断一次网"）、未知段回落全量、IPv6 变更不带 cake
  - `ok_out` / `die`：面板依赖的 JSON 协议，类型强转与字段完整性
  - **缝不生效**：未设 / 设成空串时，解析出的路径必须与生产完全一致

### Notes

- **为什么这版没动 `dert-priv`**：它有 70 处硬编码 `/etc|/run|/var/lib`，
  其中 41 处在 `<<'PYEOF'` 的 Python heredoc 里（shell 不展开变量，必须改成
  `os.environ`），而且里面混着**提示文案**（如 `"在系统上执行：rm /etc/dert/..."`），
  盲替换会改错。这是升级通道本身，值得单独一版、单独验证。
- 真机验证：部署后 `CONFIG_JSON` / `DERT_VAR` / `DERT_RUN` 解析与改动前
  完全一致；`config-get`、`status`、`apply-all --convergent` 均正常；
  `/etc/dert` 无残留临时文件；lastgood 仍是 5 份。
- 棘轮预算不变（133）——本轮新增的全是行为断言。

## [0.1.77] - 2026-11-12

> 修一个 **v0.1.76 自己在真机上暴露的 bug**，并补上锁住它的守卫。

### Fixed

- **tar 的 stderr 被合进了成员清单管道**。v0.1.76 抽 `tarcheck.py` 时写成了
  `tar -tzf "$pkg" 2>&1 | tarcheck.py`：tar 对含 `..` 的成员会往 stderr 打
  `Removing leading 'priv/../../' from member names`，合并后那行被当成成员名，
  于是拒绝原因变成 **tar 的警告原文**而不是真实条目：

  ```
  "具体：升级包含危险路径：tar: Removing leading `priv/../../' from member names"
  ```

  两个方向都错：含 `..` 的包提示错位；反过来，**正常包若有任何 tar 警告也会
  被误判为危险条目**。改成先把清单取到变量、再喂给校验器，并单独处理
  「包读不出来」的情况。

### Notes

- 守卫补一条：`tar -tzf "$pkg" 2>&1 | python3` 不得出现。棘轮预算 132 → 133，
  理由写在 `check_assert_ratio.py` 里——这个 bug 只在以 root 跑 `upgrade-apply`
  且包含 `..` 时显现，没有不依赖 root 的行为测试办法。
- **发现过程值得记**：本地单测全绿（`tarcheck.check()` 逻辑是对的），
  是**真机跑一遍**才暴露出来——问题不在模块，在 shell 的管道接线。
  这也说明"抽模块 + 行为测试"之后仍需真机验证，两者不能互相替代。

## [0.1.76] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第二阶段）**：把「以 root 解压之前」的最后
> 一道防线抽成可测模块。

### Security

- **`priv/tarcheck.py`**：升级包成员校验从 `dert-priv` 的内联 Python 抽出。
  这段逻辑此前只有源码字符串断言（`assert 'if "/" not in p: continue' in src`）
  ——而它挡的是路径穿越，字符串断言**证明不了它真的拦得住**。

  抽出来后用**真实恶意 tar** 做行为测试：用 Python `tarfile` 造一个成员名为
  `priv/../../etc/cron.d/evil` 的包（GNU tar 创建时会自己剥掉 `..`，造不出来，
  但攻击者可以用别的工具造），走 `tar -tzf | tarcheck.py` 这条生产路径，
  断言被拒绝且错误里带上具体条目名。

  顺带改善可诊断性：拒绝时不再只说「包含不认识的条目」，而是带上
  `具体：升级包含危险路径：priv/../../etc/cron.d/evil`。

### Added

- `tests/test_tarcheck.py`（10 个用例）：绝对路径、`..`（含中段穿越）、
  未知顶层目录、目录条目、顶层文件、空名，以及两个端到端恶意 tar 用例。

### Changed

- 守卫里两条白名单断言换成一条「接线」约束（dert-priv 必须调用该模块）。
  语义断言交给行为测试，源码断言只留「有没有接上」这种架构约定。

### Notes

- 棘轮预算从 133 下调到 **132**（锁定成果）。
- **关于剩余断言的判断修正**：我原来给 P1-2 定的目标是「字符串断言降到 40%
  以下」，现在看这个目标定错了。剩下的 132 条里约 30 条是「通道同步」
  （安装/升级/卸载三处都要装同一个文件）——这类断言的是**跨文件约定**，
  行为化要跑完整升级链，成本高且失真；它本来就该留在源码层。
  真实可迁移的大概还有 40–50 条，集中在 `dert-priv` 与 shell 钩子，
  前提是给它们加 `DERT_FSROOT`——那正是 P2 需要的能力。
- 真机验证：升级到 0.1.76 后，用构造的恶意包跑 `upgrade-apply`，
  被拒绝且提示里带出了穿越路径；正常包不受影响。

## [0.1.75] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第一阶段）**。
> 目标是把「`assert "某段文本" in 源码`」这类断言换成「跑一遍、断言产物」的
> 行为测试——前者断言代码长相，重构时容易误报（本项目已踩过 4 次）。

### Added

- **测试缝 `DERT_FSROOT`**（`dns-split-sync` / `dns-split-prepare`）。未设时是
  空串，`${FSROOT}/etc/...` 与原绝对路径**逐字符等价**，生产行为不变；设了就
  指向临时根，能在无 root 环境跑完整逻辑。

  这一步是必需的：这两个脚本原来把 `/etc/sing-box/config.json` 写死，
  根本没有办法在没有 root 的情况下验证它们**实际生成了什么规则**——
  这正是过去只能做源码字符串断言的原因。

- **`tests/test_dns_split.py`（8 个行为用例）**。用假根 + 假命令把脚本真跑一遍：
  PATH 前置假 `systemctl`/`ss`/`ip`/`nft`/`logger`，假 `nft` 把 stdin 原样录下来
  ——那就是脚本**实际会下发**的规则。断言针对录下来的规则文本：
  - 白名单 → `:5353`（fakeip 路径）、非白名单 → br-lan 的 `53`（真实 IP 路径）
  - v6 目标优先取 `dynamic`（PD 委派），不选 `temporary` 隐私地址
  - `add table` + `delete table` 同事务原子替换（直接写 table 定义是**追加**）
  - 建表失败 → 删表回落 dnsmasq，而不是留半张表
  - 空白名单 → 完全不建表
  - 前缀基线 `/run/dert/lan-prefix` 写成 `…/64`
  - prepare 产物：5353 双栈监听、`hijack-dns`、`.lan` 本地解析、无 `source_ip_cidr`

- **断言棘轮 `check_assert_ratio.py`**。字符串断言数量**只能降不能升**，
  CI 里 `--check` 超预算即失败。新增断言若必须是源码断言（例如"三处通道都要
  装同一个文件"这种跨文件约定），需要同时调整预算并写明理由——强迫先想一下
  能不能行为化。

### Changed

- 删掉 14 条已被上述行为测试覆盖的源码字符串断言（`_dss`/`_dsp` 那批）。

### Notes

- 守卫新增 P3-32 之前先做了棘轮（独立脚本，避免自引用计数）。
- **本阶段只完成了 P1-2 的一部分**：字符串断言 133 条（占 83%），主要剩
  `dert-priv`（22 条）、各 shell 钩子（约 40 条）、通道同步类（约 30 条）。
  后两类里，通道同步属于真正该留在源码断言的「架构约束」；
  钩子那批需要同样加 `DERT_FSROOT` 才能行为化——而那正是 P2 需要的能力，
  所以下一步是把测试缝扩展到 `lib.sh` + `dert-priv`，两件事是同一个工程。
- 真机验证：重启 sing-box 触发 `ExecStartPost` → 分流表重建 4 条、v6 目标等于
  当前 GUA、前缀基线写入 `2409:8a5e:a52:45e0::/64`，与改动前一致。

## [0.1.74] - 2026-11-12

> 工业化重构 **P3（发布签名）**。这是审计报告列为 P0 的一项，也是本项目
> **唯一无需登录即可触达的攻击面**：升级包以 root 执行，而包与 `.sha256`
> 走同一条通道（含 `gh-proxy` 第三方镜像回退）——镜像被劫持时可以同时
> 替换包和哈希，sha256 形同虚设。

### Security

- **Ed25519 detached signature**。选它而不是 minisign/GPG 的理由：
  - 设备端用 **openssl 原生验签**（`pkeyutl -verify -rawin`），不引入任何
    新二进制、不自己实现密码学。Debian 13 自带 OpenSSL 3.5 已实测支持
    Ed25519 签名/验签/篡改检测。
  - 私钥**不进 CI**：存在仓库 Secret `DERT_SIGNING_KEY`（base64 的 PEM）。
    没配 Secret 时发布流程只告警不失败，避免 fork 后一打 tag 就红。
  - CI 签名后**立刻自验一次**（用仓库里的公钥），签错了在发布前就发现，
    而不是等设备端拒绝。

- **验签在解压之前**：先确认来源真实性，再碰包内容。签名不匹配直接拒绝，
  不做任何改动。

- **过渡策略**：包带 `.sig` 就**必须**验过（fail closed）；不带 `.sig` 时
  默认只记警告放行（存量发布还没签名，一刀切会锁死升级通道），
  配 `upgrade.require_signature=true` 则拒绝。这是"只验不拦 → 强制"的开关。

- 公钥放在 **`priv/`** 而不是新增顶层目录：升级包的目录白名单由**旧版代码**
  校验，新增顶层目录会让旧版拒绝新包——正是 v0.1.62「升级包自我阻塞」的
  同类坑。`priv/` 本来就在白名单内。

### Fixed

- `norm_upgrade` 保存升级设置时会**丢掉 `require_signature`**（它只返回
  `{"source"}`）。面板表单里没有这个字段，所以用户点一次保存就把强制验签
  静默关掉了——这类"静默降级"比没有开关更危险。现在未提供时保留原值。

### Notes

- 守卫新增 P3-31（公钥存在且位置正确、CI 必须签名、验签必须在解压之前、
  策略开关存在）。行为测试新增 3 个用例锁住"保存不丢开关"。
- 本地已验证：签名→用仓库公钥验签通过；改一个字节→验签失败。
- **仍需你操作一次**：把 `~/.dert-signing/dert-signing.key.pem` 的内容
  base64 后填入仓库 Secret `DERT_SIGNING_KEY`，之后的发布才会自动签名。
  填好并确认一次带签名的发布成功后，再把 `upgrade.require_signature` 打开。

## [0.1.73] - 2026-11-12

> 工业化重构 **P0-3（全脚本补 `set -u`）**。
> 未定义变量在 shell 里默认是空字符串，`rm -rf "$DIR"/` 这种写法在 `DIR`
> 拼错时会变成 `rm -rf /`。`set -u` 是唯一能挡住这类事故的开关。

### Security

- **6 个脚本补 `set -u`**：`priv/lib.sh`、`network/ppp/ip-up.d/dert`、
  `network/ppp/ip-down.d/dert`、`network/dispatcher/70-dert-dns`、
  `75-dert-pppoe-hotplug`、`80-dert-cake`。至此仓库内全部 20 个 shell 脚本
  都有该开关。

### Changed

- **钩子的位置参数改为 `${1:-}` 形式**。pppd 与 networkd-dispatcher 一定会
  传参，但手工调试时不带参数也不该因 `-u` 直接退出——保持"缺参就是空值"
  的原有行为，只是把静默变成显式。

- `lib.sh` 的 `set -u` 会作用到 source 它的 shell。当前三个调用方
  （`dert-priv` / `qdisc-apply` / `wan-addr-sync`）本来就都有 `set -u`，
  所以**实际行为不变**；写在这里是为了让"直接 source 本文件调试"时也不会
  静默吞掉未定义变量。

### 真机验证

- 5 个钩子**不带参数**逐个执行：全部 `rc=0`，无 `unbound variable`
- 真实重拨（触发 ip-down → ip-up 全链路）：journald 里无任何 `-u` 相关错误
- 钩子副作用正常：会话起点已记录、上游 DNS 已写入、`resolv.conf` 指向本机
- 五服务 active、0 失败单元、DNS 双路径正常、`.lan` 可解析、v4/v6/面板 200

### Notes

- 守卫新增 P3-30：**所有** shell 脚本必须有 `set -u`（按 shebang 自动收集，
  新增脚本自动纳入）。判定会剥掉行尾注释，避免说明文字里的 `set -u` 骗过断言
  —— 这个坑本轮又踩了一次（初版把 `"u" in tokens` 当成匹配，结果全部误报）。

## [0.1.72] - 2026-11-12

> 工业化重构 **P0-1（PPPoE 看门狗改为事件优先）**。
> 这是当前唯一还在持续产生无谓开销的地方：`dert-pppoe-watchdog.timer`
> 每分钟跑一次，**真机实测 24h 执行 1275 次**，每次起 1 个 unit + 1 个 shell
> + **2 个 python3 进程**（读同一份 `config.json`）。

### Changed

- **ppp0 消失改为事件驱动**。`dert-netwatch` 本来就订阅 rtnetlink，link 消失
  它是能立刻收到的，只是之前没接这一路。现在：
  - 收到「ppp0 不存在」→ 立刻调一次看门狗（打缺失标记）
  - 同时安排**一次** 180s 后的一次性复查（`systemd-run --on-active=180s --collect`）
  - 复查时若已恢复 → 清标记、什么都不做；仍缺失 → 重启拨号

  为什么还要 180s：拨号建立本身要时间，ppp0 刚消失就重启会打断正在重试的
  pppd。这是「给定宽限期后做判定」，不是猜「什么时候能好」——与 v0.1.65
  那个被撤掉的 `--on-active=20s` 有本质区别，后者是在猜外部事件的发生时刻。

- **timer 周期 1 分钟 → 15 分钟**，退为纯兜底（netwatch 挂掉或事件丢失时仍能
  收敛）。24h 执行次数从 1275 降到约 96。

- **看门狗里两次 `python3` 合并为一次**（读同一份 JSON 取「上网方式」和
  「WAN 口名」）。路径改为走 argv，不再拼进 Python 源码字符串。

- **升级通道补 `systemctl restart dert-pppoe-watchdog.timer`**：
  `enable --now` 对已在运行的 timer 是空操作，不重启的话新周期要等到下次
  开机才生效——这类「改了配置但没生效」最难排查。

### 真机验证

停掉 `dert-pppoe` 模拟 ppp0 消失：

```
22:39:11  netwatch: ppp0 消失，标记缺失并安排 180s 后复查
22:39:11  watchdog: 拨号服务未运行，拉起
22:39:15  恢复（4 秒）
22:42:16  180s 一次性复查执行 → 发现健康 → 不做任何事 → --collect 自动清理
          服务未被多余重启（启动时间仍是 22:39:11）
```

改动前最坏要 4 分钟（1 分钟轮询 + 3 分钟宽限），现在 **4 秒**。

### Notes

- 守卫新增 P3-29（timer 周期、netwatch 的 ppp0 分支、python3 合并为 1 处、
  升级必须重启 timer）。四项变异测试全部捕获。

## [0.1.71] - 2026-11-12

> 工业化重构 **P0-2（陈旧生成物回收）**。这一项要**修正一个此前的判断错误**：
> 我先前说「`apply_stage_files` 只写不删」——不准确。清理逻辑其实一直存在
> （`cleanup_stale_port_networks`），第三方审计报告也没注意到它。
> **真正的 bug 在那个函数的判定条件上**。

### Fixed

- **端口从 LAN 改成 WAN 后，旧的 `30-<口>.network` 不被清理**（真机 2026-11-12
  踩到过）。原判定是「口名不在 `ports` 里就删」，但生成器对 **WAN 口不产出**
  `30-*.network` —— 所以「仍在 `ports` 里、但角色变成 WAN」的口会被漏掉。
  残留文件内容是 `Bridge=br-lan`，于是 **WAN 口被塞进 LAN 桥**；当天是靠
  `10-wan.network` 字典序先匹配才没出事，属于随时会炸的定时炸弹。

### Changed

- **清理改为「按 manifest 对账」**，逻辑抽到 `priv/cleanup.py`（可单测）：
  以本次要部署的清单为唯一事实来源，清单里没有的生成物就是陈旧的。
  不再重复生成器的判断逻辑——两边各写一遍迟早会漂移，这次漂移的就是它。

  三道安全边界：
  1. 只在白名单目录 + 白名单文件名模式里找（`30-*.network`、`25-ppp0.network`、
     `dert.conf`、`dert-base.nft`、`peers/dert`、`chap-secrets`、`pap-secrets`），
     绝不遍历整个 `/etc`；
  2. 只删**首两行带「由 DebRT 生成」标记**的文件 —— 用户手写的同名文件不动
     （`dert-base.nft` 首行是 shebang，标记在第二行，所以看两行）；
  3. 清单读不出来就一个都不删。

  每条删除写 journald（`logger -t dert-apply`），事后可倒查。清理脚本缺失时
  也会留一条日志，避免"清理悄悄失效"长期无人发现。

- 清理时机不变，仍在 `apply_stage_files` **之后**（部署成功才删）：
  `do_config_set` 正常路径、回滚重放路径、`do_apply_all` 三处。

### Added

- `tests/test_cleanup.py`（13 个用例，用 `--root` 注入假根目录，不碰真实 `/etc`）：
  覆盖 LAN→WAN 角色变更、端口移除、PPPoE 残留、**无标记的用户文件不被删**、
  白名单外文件不动、清单缺失/损坏/结构不符时零删除，以及 `--apply` 与
  观察模式的行为差异。

### Notes

- **测试抓到一个真安全漏洞**：初版 `plan()` 用 `man.get("files", [])`，于是
  「能 `json.load` 但没有 `files` 键」的清单（例如错误输出
  `{"ok":false,"code":"CONFIG_SCHEMA"}`）会让 `keep` 为空 → **把磁盘上所有
  生成物一起删掉**。已改为要求 `files` 是非空列表，否则零删除。
- 守卫新增 P3-28（旧函数不得回归、三个调用点、`cleanup.py` 存在且保留
  三道边界）。四项变异测试全部捕获。
- 真机验证：造「带标记的陈旧文件」+「无标记的用户文件」，dry-run 只列出前者、
  不删任何东西；`--apply` 只删前者并留日志；随后制造配置漂移跑 `apply-all`，
  陈旧文件在部署后被清理，网络与服务无影响。

## [0.1.70] - 2026-11-12

> 工业化重构 **P0.5（审计修复）**。来自一份第三方独立审计报告的复核结果：
> 我们逐条核对后确认其结论可信（行号与代码全部对得上），据此修掉
> 字段级校验单点化、下载加固、静态分析缺失等问题。**运行时行为无变化**。

### Security

- **字段级安全断言下沉到收敛侧**（审计 F-02）。`validate.py` 的字段校验只在
  「面板写入」这一条路径生效，而备份恢复、回滚、升级后收敛都是直接读
  `config.json`——这些字段会被原样拼进 dnsmasq / nft / network 文件，
  一个换行就能注入新指令（dnsmasq 的 `dhcp-script=` 会以 root 执行脚本）。
  现在 `netgen.schema_check` 补三层断言，它在 `--schema` / `--check` /
  `--stage` 与所有收敛路径上都会跑，一处覆盖全部入口：
  1. 递归扫描整份配置，拒绝任何含控制字符（`\x00-\x1f\x7f`）的字符串；
  2. `static[].name` 走与 validate 相同的字符集（**逗号**会错位
     `dhcp-host={mac},{ip},{name}` 的字段），`mac` 走 MAC 正则；
  3. `portfw` 的 `wport/lport` 限定为数字与 `-`（会被直接拼进 nft 表达式）。

  刻意**只拦会导致注入的字符、不碰语义**：存量 `/25` 掩码之类仍放行，
  否则 `backup-restore` 会因噎废食拒绝恢复（该取舍在原代码注释里已论证）。
  因此也**没有**把 `--schema` 升级为完整 `--check`——那会拒绝合法的旧备份。

- **升级下载加固**（审计 F-01 部分）。清单里的 `url` 强制 `https://`
  （`curl` 会照做 `file://` 等协议，被篡改的清单可借此把任意本地文件
  当成"升级包"）；下载加 `--max-filesize 268435456`，恶意/错误源不能写满磁盘。
  注意：**发布签名（真实性锚点）仍未做**，属于 P3，需先定密钥方案。

### Added

- **CI 接入 shellcheck**（审计 F-07）。1,744 行 bash 是全项目最大风险面
  （历史 bug 注释大多出自这里），此前只有 `bash -n` 语法检查。
  基线已清零（`--severity=warning`），因此按 warning 级门控；
  `SC2034`（未使用变量，12 处历史常量声明）暂时排除，留待单独清理。
  脚本清单由 shebang 自动收集，新增脚本不会漏检。
- 行为测试新增 `tests/test_netgen_safety.py`（11 个用例）：静态绑定换行/
  逗号/MAC 格式、转发端口注入、深层路径控制字符、以及"合法值不能被误拦"
  的正向用例（小写 MAC、端口区间、正常静态绑定）。

### Changed

- 删除 `netgen` 输出的 `restart` 字段——它与 `dert-priv` 里
  `plan_for_section` 语义重复，全仓库无消费者（审计小项）。
- `dert-priv:1071` 的 `local x="$(...)"` 拆成声明与赋值两行（`SC2155`：
  同行写法会掩盖命令替换的返回码）。

### Fixed

- `install.sh` 中 `systemctl enable --now dert-netwatch.service` 的缩进
  与相邻语句不一致（v0.1.67 引入，能跑但误导）。

### Notes

- 守卫新增 P3-25（字段安全断言存在且对整份配置生效）、P3-26（下载加固）、
  P3-27（CI 必须含 shellcheck 且自动收集脚本）。四项变异测试全部捕获。
- **审计报告漏掉的一项**：`apply_stage_files` 只写不删（端口角色变更后旧
  `30-<口>.network` 永久残留，真机已发生过一次冲突）。这一项**本轮未修**，
  已登记为 P0-2 并上调优先级，将紧随本版之后处理——它的删除逻辑必须在
  字段级断言就位之后做，否则误删风险更高。

## [0.1.69] - 2026-11-12

> 工业化重构 P0-4 + P1：**运行时行为零变化**，只补测试地基与去掉一处坏味道。

### Added

- **行为测试层 `tests/`**（45 个用例，`python3 -m unittest discover -s tests -t .`）：
  断言的是**生成产物的内容**和 **CLI 的输出**，不再对源码做字符串包含断言。
  覆盖：`25-ppp0.network` 的 `KeepConfiguration`/默认路由、`dnsmasq` 的
  `bind-dynamic`、`nft` 的 `dns_bypass` 与 `wan_in`、端口角色到 `30-*.network`
  的映射、manifest 契约（相对路径 / 无穿越 / 每个文件有 mode 与内容），
  以及 validate 的跨段组合与各字段拒绝码。
  设计约束：**不需要 root、不读写 `/etc`、不依赖真机**。
- CI 拆成两段：`fast-tests`（秒级，无 root）先跑，`real-path-smoke`
  通过 `needs: fast-tests` 依赖它 —— 快测挂了不再浪费 25 分钟的集成跑。

### Changed

- `dert-priv` 解析升级清单不再用 `eval`：改为 python 输出两行、bash 用
  `read` 读取。原写法虽有 `shlex.quote` 保护，但「执行动态生成的代码」本身
  是坏味道，将来谁改了引号处理就变成命令执行。改后全程不经 shell 求值。
  行为等价（URL 与 sha256 不可能含换行，去掉 `\r\n` 是安全的），
  已用 7 种边界输入验证（正常 / 缺字段 / 非法 JSON / 类型异常 / 注入载荷）。

### Notes

- 守卫新增 P3-23（行为测试层存在、不得依赖 root、CI 必须 fast 门控）
  与 P3-24（`dert-priv` 不得再用 `eval`）。后者**只检查非注释行**——
  源码文本断言被自己的说明文字绊倒已经踩过三次，这次一并修掉判定方式。
- 本轮不改任何生成配置、不改任何服务单元、不动 sing-box/Tailscale/podman
  的调用逻辑。

## [0.1.68] - 2026-11-12

### Fixed

- **路由器自身解析不了局域网主机名**（真机 2026-11-12 实测）：pppd 的 `usepeerdns`
  会让 `/etc/ppp/ip-up.d/0000usepeerdns` 把 `/etc/resolv.conf` 改写成运营商 DNS，
  于是**路由器自身绕过 dnsmasq**——`dig <host>.lan` 返回空、`getent hosts` 空，
  而 `dig @127.0.0.1 <host>.lan` 正常；同时吃不到 dnsmasq 的 5000 条缓存。
  运营商上游本来就已由钩子写进 `/run/dert-dns/upstream.conf` 交给 dnsmasq，
  所以 `ip-up.d/dert` 与 `ip-down.d/dert` 现在把 resolv.conf 原子写回
  `nameserver 127.0.0.1` + `::1`（tmp+mv，避免读到半个文件）。
  两个钩子按字母序都排在 `0000usepeerdns` 之后，能覆盖它。
- **`ppp0` 的 IPv4 会被 networkd 清掉**（真机 2026-11-12 实测，**会导致 WAN 断网**）：
  networkd 重新配置 link 时默认删除「不是自己配的」地址，而 ppp0 的 IPv4 是 pppd
  用 ioctl 加的（外来配置）。实测 `networkctl reconfigure ppp0` 之后 ppp0 只剩
  DHCPv6 地址、IPv4 与默认路由全消失，v4 出网直接 `000`，必须重拨才恢复
  （pppd 进程始终存活，它自己不会重加地址）。
  触发场景不止手工命令——`apply-all` 在**仅 LAN 设置**变更时只重启 networkd
  不重启 pppoe（`plan_for_section("lan")` 的动作串里没有 pppoe），用户改个
  LAN IP 就会把 WAN 弄断，很难联想到根因。
  修法：`25-ppp0.network` 加 `KeepConfiguration=yes`。实测 reconfigure 后
  IPv4 保留、v4/v6 出网均 200。
- **面板启动被 `network-online.target` 拖住**：该目标要等
  `systemd-networkd-wait-online` 判定所有受管接口就绪，而 br-lan 在网线没插时
  永远不 routable → 面板被硬拖到超时（默认 90s+）才起来。
  面板只服务局域网，WAN 通不通与它无关，改为只依赖 `network.target`
  （与 sing-box drop-in 里 `wait-wan` 的判断一致）。

### Changed

- `25-ppp0.network` 去掉 `Scope=link`：IPv6 路由没有 scope 概念，networkd 每次开机
  都报 `Scope= is specified for IPv6 route. It will be ignored.`，实际建起默认路由的
  是 `Destination=::/0`。去掉后默认路由仍正常建立（实测 `default dev ppp0 proto static`）。
- `uninstall.sh` 对 `LIB_DIR`/`ETC_DIR`/`VAR_DIR` 加 `:?` 断言：这三个变量后面直接
  喂给 `rm -rf`，为空或为根时后果不可逆。

### Notes

- 守卫新增 P3-22：resolv.conf 必须指回 dnsmasq 且原子写（ip-up/ip-down 各一条）、
  面板不得依赖「在线」目标、ppp0 必须有 `KeepConfiguration=yes`、不得有 `Scope=link`。
  四项变异测试全部捕获。
- 真机一次性清理（不进代码，属改名迁移与另一项目的历史残留）：
  `dnsmasq.service.d/10-seedrou-restart.conf`（与 dert 版重复，`ExecStartPre` 跑两次）、
  `network/30-enp4s0.network`（把 **WAN 口 enp4s0 声明为 br-lan 成员**，靠
  `10-wan.network` 字典序先匹配才没出事）、以及 7 个 `linux-router` 项目的残留文件。
  已归档到 `/root/leftover-cleanup-*.tar.gz` 后删除。

## [0.1.67] - 2026-11-12

### Changed

- **IPv6 前缀变化改为真正的事件驱动**：新增 `priv/netwatch`（常驻订阅 rtnetlink）
  与 `systemd/dert-netwatch.service`，`br-lan` 地址一变就立刻对账——重新生成
  DNS 分流配置、刷新 nft 表里白名单的 v6 DNAT 目标、必要时重启 sing-box 重建
  `inet6_local_address_set`。
  v0.1.65 的做法是在 ppp `ip-up` 里 `systemd-run --on-active=20s` 延迟调钩子，
  本质是**把时序不确定性换成一个猜出来的常数**（真机实测 PD 委派耗时随 BRAS 波动）。
  现在不再猜任何时间。

### Removed

- `network/ppp/ip-up.d/dert` 里的 `systemd-run --on-active=20s` 延迟调用。
- `90-dert-lan-prefix` 里 `while [ -z "$NEW" ]; sleep 1` 的 15 秒轮询。
  该钩子现在由 netwatch 在地址事件里调用，前缀必然已在位；而它若被
  networkd-dispatcher 在 PD 委派前调到，直接返回即可（地址落地后 netwatch 会再调）。
  保留等待的害处是：netwatch 的对账是串行的，钩子阻塞 15 秒会把后续事件一起拖住。

### Fixed

- **`dns-split-sync` 重复运行会累积 nft 规则**（预先存在的 bug，被 netwatch 暴露）：
  `nft -f` 里直接写 `table inet dert_dns { ... }` 对**已存在**的表是**追加规则**
  而不是替换，每跑一次就多 4 条。真机实测 netwatch 在运行期触发几次后表里堆到
  **20 条**，最后几条还指向已删除的临时地址。改为在同一个事务里
  `add table` + `delete table` + 重新定义（add 是幂等的，表在不在都成功），
  既保持原子性又做到真替换。实测连续跑 5 次稳定在 4 条。
  之前没暴露是因为 sync 只在 sing-box 启动时跑，而重启会先 `teardown` 删表。
- **v6 DNAT 目标地址选取不确定**：原来用 `awk ... exit` 取「第一个」全局地址，
  但 br-lan 上可能不止一个（多前缀委派、手工加的静态地址、隐私地址），谁先出现
  取决于内核返回顺序——地址集合一变就可能选中另一个，导致表反复重建，甚至指向
  一个随后消失的地址。改为：排除 `temporary`（RFC 4941 隐私地址会轮换）→
  优先 `dynamic`（PD 委派来的，有 `valid_lft`）→ 同档内按字典序取第一个。
  `netwatch` 的 `lan6()` 与 `dns-split-sync` 用完全相同的规则（否则「当前地址」
  与「表里的目标」会永远判为不一致）。
- **`dns-split-sync` 的等待判据错位**：原来等 `tun0` 出现（最多 20 秒），但本表
  的目标地址全在 `br-lan` 上，根本不引用 tun0；tun 建好不等于 5353 已 bind，
  反过来 5353 也可能先于 tun 就绪。改为等 **5353 真正在监听**（最多 10 秒）。

### Notes

- **为什么不用 networkd-dispatcher**：它只看链路的 operational state，而 PD 委派
  只改地址、不改状态。Debian 包描述原文：*"much more limited in the types of events
  it supports due to the limited nature of systemd-networkd"*。真机日志里 ppp0 重拨
  时它只打 `WARNING:Unknown index 56 seen, reloading interface list`，不派发钩子。
- **为什么不用 systemd `.path` 盯 `/run/systemd/netif/links/<ifindex>`**：实测确实
  能触发（networkd 原地重写该文件、inode 不变），但 systemd 的稳定性承诺
  （systemd.io/PORTABILITY_AND_STABILITY）把 `/run/systemd/*` 列为内部协议，
  不在稳定接口清单里——依赖它是押注实现细节。业界（OpenWrt netifd、NetworkManager、
  dhcpcd）一律订阅 rtnetlink，那是内核稳定 ABI。
- **`dert-pppoe-watchdog.timer` 的每分钟轮询保留不动**：它做的是「服务活着但资源
  没了」的**健康检查**，周期性是该模式的正当形态（kubelet probe、`WatchdogSec`、
  monit 都如此），不属于"猜时序"。
- netwatch 的对账是**幂等**的：每条事件都重新读一遍真实状态，不解析事件类型、
  也不按秒去抖。重拨的事件序列是「旧地址删除 → 新地址 tentative → 新地址
  scope global」，按时间去抖会把最后那个「地址可用」事件丢掉，前缀就永远补不上
  ——那正是 v0.1.65 之前那个 bug 的形态。
- 守卫 P3-19 重写：断言 ip-up 不得再出现延迟调用、netwatch 必须订阅 rtnetlink
  且对账里不得有 sleep、单元必须 `Restart=always`、三通道（安装/升级/卸载）必须
  同步、sync 必须等 5353 而非 tun 接口、nft 必须做原子替换（`add`+`delete` 同事务）、
  v6 目标必须确定性选取且优先 `dynamic`。七项变异测试全部捕获。

## [0.1.66] - 2026-11-12

### Fixed

- **`90-dert-lan-prefix` 的 `cfg_covers()` 恒为假**（v0.1.64 重构遗留）：它比对的是
  `00-dert-dns.json` 里 `dns.rules[].source_ip_cidr`，而 v0.1.64 已把该字段整个删除
  （白名单 DNS 改由 nft 按 MAC 分流，不再靠源 IP 判断）。结果是「基线缺失」分支
  **每次都会无谓地重启一次 sing-box**——`/run` 是 tmpfs，重启后基线由
  `dns-split-sync` 在 ExecStartPost 重建，钩子抢先运行时就会走到该分支。
  改为：写入基线后只调 `dns-split-sync` 刷新 nft 表（它按当时的 br-lan 地址重算
  v6 DNAT 目标），既不 `prepare` 也不重启。配置里已无任何前缀相关字段，
  重启的唯一必要理由是重建 `local_address_set`，那只在**前缀真的变了**时才需要。

### Changed

- 清理 `90-dert-lan-prefix` 里两处过时注释：仍写着「`source_ip_cidr` 里含旧前缀，
  所以要在重启前重写配置」，与实际不符。

### Notes

- 守卫 P3-19 的三条断言反转：`cfg_covers` 与 `source_ip_cidr` 必须**不出现**在钩子里，
  且首次记录分支必须调用 `dns-split-sync`。两项变异测试全部捕获。

## [0.1.65] - 2026-11-12

### Fixed

- **PPPoE 重拨换前缀后 nft 的 v6 DNAT 目标停在旧前缀**（真机 2026-11-12 复现）：
  `90-dert-lan-prefix` 钩子依赖 networkd-dispatcher 的 `routable` 事件，但 ppp0
  重拨并不产生该事件（24 小时内只执行过 2 次，都在上一次开机）。重拨后 br-lan
  前缀从 `a50:3d81` 变为 `a53:bf71`，nft 表里的 `dnat ip6 to` 仍是旧前缀，白名单
  设备的 v6 DNS 全部打向不存在的地址，实测 437 个包落空。
  修法：在 `network/ppp/ip-up.d/dert` 里用 `systemd-run --on-active=20s` 主动调用
  一次前缀检查。延迟 20 秒是因为 ip-up 时 PD 还没委派到 br-lan；用 `systemd-run`
  而不是 `&` 是因为 pppd 退出会连带杀掉子进程；`--collect` 让 unit 跑完自动清理，
  避免每次重拨堆积一个 unit。

### Changed

- `priv/dns-split-sync` 不再读 tun 的 DNS 地址（`TUN4`/`TUN6` 死代码）。v0.1.64 起
  白名单的 DNS 目标是 `br-lan:5353`，与非白名单的 `br-lan:53` 同机不同端口，与 tun
  内部地址无关。原来那句 `[ -n "$TUN4" ] || exit 0` 只在订阅结构异常时才会误退出，
  属逻辑过时而非功能缺陷。
- `priv/dns-split-prepare` 移除 `LAN6`、`CARRIER` 及相关参数。`direct` inbound 监听
  `::` 双栈，不绑具体 v6 地址；非白名单已改为 nft 直连 dnsmasq，不再需要
  「按源 IP 分流到运营商」的规则。
- 修正两处过时注释：`dns-split-sync` 头部的「行为矩阵」在 v0.1.64 反转方向后仍写着
  旧语义（白名单走 dnsmasq、其余走 sing-box），与实际相反；`dns-split-prepare` 的
  版本区间标注不一致。

### Notes

- 守卫 P3-19 新增 4 条断言：`ip-up` 必须引用 `90-dert-lan-prefix` 且用
  `systemd-run --on-active`；`dns-split-sync` 不得再出现 `TUN4`/`TUN6`；
  `dns-split-prepare` 不得再出现 `CARRIER`。四项变异测试全部捕获。

## [0.1.64] - 2026-11-12

### Changed

- **DNS 按 MAC 分流改为完全在 nft 层完成**。白名单设备的 53 端口由 nft 的
  `ether saddr` 匹配后 DNAT 到 `192.168.10.1:5353`（sing-box 的 direct inbound），
  非白名单 DNAT 到 `dnsmasq:53`。`00-dert-dns.json` 随之简化到只剩
  `hijack-dns` 和 `.lan → landns` 两条，`source_ip_cidr` 与 `carrier` 全部移除。
  判定依据从「源 IP」换成「二层 MAC」，不受 DHCP 换 IP、邻居表过期影响。
  真机验证：停掉 sing-box 后非白名单设备 DNS 与出网全部正常（`dert_dns` 表由
  teardown 自动删除，DNS 回落 dnsmasq）。

### Added

- `priv/netgen.py` 生成 `dns_bypass` 链：给 dnsmasq 的上游 DNS 查询打 `0x2024`。
  不加这条的话，dnsmasq 的查询会被 sing-box 的 `output` 链末尾打上 `0x2023`，
  再经 `ip rule 9001 → table 2022 → tun0` 送进 tun，被 `hijack-dns` 拦截后返回
  fakeip —— 非白名单设备拿到假地址直接断网（真机复现，dnsmasq 日志显示它以为
  自己在问运营商）。

### Fixed

- **5353 只监听 IPv4**：nft 把白名单设备的 v6 DNS DNAT 到 `[br-lan GUA]:5353`，
  但 inbound 只绑了 `192.168.10.1`，v6 查询全部 `connection refused`
  （真机实测 437 个包落空）。改为 `listen: "::"` 双栈监听。
- **`hijack-dns` 带 `protocol` 条件导致无限自环**：`direct` inbound 不做 sniff，
  `protocol: "dns"` 永远识别不出来，规则静默不匹配后走默认 `direct` outbound，
  把查询原样转发回 `192.168.10.1:5353`（它自己）——真机实测 CPU 130%、
  2 分钟 7414 次查询、持续上传。去掉 `protocol`，只匹配 `inbound`。

### Notes

- **需要手动改 substore 模板**：tun inbound 需加 `"dns_mode": "disabled"`。
  它关掉的是 `auto_redirect` 自动安装的 `ip saddr @local_address_set dport 53
  dnat`（prerouting 和 output 链各一条），不影响 TCP redirect 与流量代理。
  不改的话 substore 同步后白名单 DNS 会被 `auto_redirect` 抢走。
- 守卫 P3-19 同步更新：断言双栈监听、`hijack-dns` 不带 `protocol`、
  `source_ip_cidr`/`carrier` 已移除、`dns_bypass` 存在且打 `0x2024`
  （三项变异测试全部捕获）。

## [0.1.63] - 2026-11-11

### Fixed

- **dnsmasq 在 PD 委派的 GUA 上不监听 53 端口**（真机 2026-11-11 复现）：`bind-interfaces`
  只在启动瞬间静态绑定当时的地址，而开机时序是 dnsmasq 先起（br-lan 仅有 `fe80::`）、
  4 秒后 PD 委派的 GUA 才出现，GUA 上的 53 永久缺失。dns-split 的 nft 表恰好把白名单
  设备的 v6 DNS DNAT 到该 GUA，实测 6281 个包全部 `connection refused` —— iPhone 每次
  新域名先撞一次 v6 失败再回落 v4，表现为「境外站和公众号初次打开偏慢」。
  改为 `bind-dynamic`：跟踪接口地址变化，GUA 出现或 PPPoE 换前缀后自动补绑。
  守卫补 P3-21（含变异测试）。

## [0.1.62] - 2026-11-11

### Fixed

- **升级包自我阻塞**：`do_upgrade_apply` 的成员白名单是「允许清单」，新版本一旦新增顶层
  文档（`LICENSE` / `CHANGELOG.md` 等），旧版代码就会以「包内含不认识的条目」拒绝整包
  ——而执行校验的恰是旧版代码，无法自愈（v0.1.61 真机实测踩到）。
  改为只拦危险路径（绝对路径 / 含 `..` / 未知目录），顶层文件一律放行：
  它们只能落在包根，无法路径穿越，后续也只会 `cp priv/*` 到程序目录，不会被部署到系统路径。

## [0.1.61] - 2026-11-11

### Fixed

- **发布包不含许可证与文档**：`dert-<ver>.tar.gz` 里没有 `LICENSE` / `CHANGELOG.md` /
  `CONTRIBUTING.md` / `SECURITY.md`，下载源码包的人无从得知授权条款。已加入 release
  打包清单，并同步放行升级包白名单（`ALLOW_FILE`）——否则 `upgrade-apply` 会以
  「包内含未知文件」整包拒绝。
- 守卫补 P3-20：打包清单与白名单任一处漏掉上述文件即失败。

## [0.1.60] - 2026-11-11

### Changed

- 移除改名迁移遗留代码：`migrate_seedrou_to_dert()` 及其调用（`install.sh`、`priv/lib.sh`、
  `priv/dert-priv`），以及 `uninstall.sh` 末尾的 seedrou 旧布局清理段。
  新装机器不存在旧布局，这些逻辑已是死代码；`priv/lib.sh` 里那份还引用了未定义的
  `c_info`，本身就有隐患。

### Fixed

- `uninstall.sh` 的 `rm -f` 清单缺续行符，导致命令提前结束——其后以 `\` 结尾的行被当成
  独立命令执行，报错且**后续文件删不掉**。同时清理了与 `rm -rf "$LIB_DIR"` 重复的条目、
  合并了重复出现的 `chap-secrets`。

### Docs

- 新增 `LICENSE`（MIT）、`CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`、`.editorconfig`。
- `README.md` 增加「许可证与相关文档」章节。

## [0.1.59] - 2026-11-11

### Fixed

- **重启或重拨换前缀后 v6 DNS 分流静默失效**：`00-dert-dns.json` 的 `source_ip_cidr` 会只剩
  v4 网段，导致非白名单设备的 AAAA 查询落到 fakeip（`fc00::2`）→ v6 黑洞。设备靠
  happy-eyeballs 回落 v4，表现为"偶发变慢"，极难发现。
  - `dns-split-sync`（ExecStartPost）现在写入 `/run/dert/lan-prefix` 基线——开机时
    dispatcher 的 `routable` 事件常早于 PD 委派，钩子取到空前缀后静默退出，基线会永久缺失；
  - `90-dert-lan-prefix` 首次记录分支不再无条件退出，改为比对配置里的 v6 前缀
    （`cfg_covers`），缺失则重新生成配置并重启 sing-box；
  - 前缀变更钩子的重启条件从「`auto_redirect` 在用」改为「sing-box 在运行」，避免未启用
    `auto_redirect` 时永远读不到新的 v6 `source_ip_cidr`。

## [0.1.58] - 2026-11-11

### Changed

- **项目更名 SeedRou → DebRT**。目录、systemd 单元、dispatcher 钩子、环境变量、nft 表名
  全部随之调整：

  | 项目 | 旧 | 新 |
  |---|---|---|
  | 程序目录 | `/usr/local/lib/seedrou` | `/usr/local/lib/dert` |
  | 配置目录 | `/etc/seedrou` | `/etc/dert` |
  | 数据目录 | `/var/lib/seedrou` | `/var/lib/dert` |
  | 单元/钩子 | `seedrou-*` | `dert-*` |
  | 环境变量 | `SEEDROU_*` | `DERT_*` |
  | nft 表 | `seedrou_base` / `seedrou_dns` | `dert_base` / `dert_dns` |

## [0.1.57] - 2026-11-11

### Added

- **DNS 按 MAC 分流**：nft 层按 `ether saddr` 把白名单设备的 53 端口回导 dnsmasq
  （拿 fakeip → 流量进 tun 走代理），其余设备 DNAT 到 sing-box 的 tun DNS（拿真实 IP）。
  修复非白名单设备拿到 fakeip 却无人接管、连接永久 `SYN_SENT` 的结构性缺口。
- 三个钩子：`dns-split-prepare`（启动前按 PD 前缀生成配置）、`dns-split-sync`
  （启动后派生 nft 表）、`dns-split-teardown`（停止时删表回落 dnsmasq）。

### Fixed

- `singbox-cleanup` 用 `grep "table sing-box"` 匹配 `nft list tables` 的输出
  （实际是 `table inet sing-box`），导致 OnFailure 清理**从未生效**。
- 白名单计数用 `tr | wc -l` 少算最后一台（末项无换行）。

## [0.1.56] - 2026-11-09

### Fixed

- 重拨换 IPv6 前缀后 sing-box 代理失败、旧前缀路由残留：新增 `90-dert-lan-prefix`
  dispatcher 钩子，在前缀变化时清理旧 RA 路由并重启 sing-box 重建本地地址集合。

## [0.1.55] - 2026-11-09

### Fixed

- 重拨后运营商 IPv6 上游 DNS 丢失（no-carrier 事件删除上游文件 + 事件时序双根因）。

## [0.1.54] - 2026-11-09

### Fixed

- Safari 标签页无图标：补齐 PNG/ICO 位图 favicon，面板直接响应 `/favicon.ico`。

## [0.1.53] - 2026-11-09

### Fixed

- 上游 DNS 的 v4 来源补齐：dispatcher 的 `ppp*` 分支同时读 `/etc/ppp/resolv.conf`，
  `CONF` 丢 v4 后能自愈。

## [0.1.52] - 2026-11-09

### Fixed

- 真机重启复盘三项：收敛过程泄漏 stage 目录（含明文宽带密码）、dnsmasq 开机报
  `cannot read upstream.conf`、`apply-all` 预检错误详情被吞。

## [0.1.51] - 2026-11-09

### Fixed

- 外部审计（1 P2 + 11 P3 + 3 观察）逐条核实并修复 13 项。
- 升级通道收敛加 CI 冒烟网关：`install.sh --smoke` 落标记，`do_upgrade_apply` 据此跳过
  内联收敛与延迟重启，避免 runner 上重启 networkd 导致 job 挂死。

## [0.1.50] - 2026-11-09

### Fixed

- v0.1.49 复审收尾五项：磁盘诊断标记写节流、密码验证成功后清零失败计数（避免陈旧计数
  误触锁定）、补五条守卫断言、撤销表 `.tmp` 并发写唯一化、`sb seturl` 内网黑名单补齐。

## [0.1.48] 及更早

见 [GitHub Releases](https://github.com/wangyr2008/DebRT/releases)。

---

[Unreleased]: https://github.com/wangyr2008/DebRT/compare/v0.1.64...HEAD
[0.1.64]: https://github.com/wangyr2008/DebRT/compare/v0.1.63...v0.1.64
[0.1.63]: https://github.com/wangyr2008/DebRT/compare/v0.1.62...v0.1.63
[0.1.62]: https://github.com/wangyr2008/DebRT/compare/v0.1.61...v0.1.62
[0.1.61]: https://github.com/wangyr2008/DebRT/compare/v0.1.60...v0.1.61
[0.1.60]: https://github.com/wangyr2008/DebRT/compare/v0.1.59...v0.1.60
[0.1.59]: https://github.com/wangyr2008/DebRT/compare/v0.1.58...v0.1.59
[0.1.58]: https://github.com/wangyr2008/DebRT/compare/v0.1.57...v0.1.58
[0.1.57]: https://github.com/wangyr2008/DebRT/compare/v0.1.56...v0.1.57
[0.1.56]: https://github.com/wangyr2008/DebRT/compare/v0.1.55...v0.1.56
[0.1.55]: https://github.com/wangyr2008/DebRT/compare/v0.1.54...v0.1.55
[0.1.54]: https://github.com/wangyr2008/DebRT/compare/v0.1.53...v0.1.54
[0.1.53]: https://github.com/wangyr2008/DebRT/compare/v0.1.52...v0.1.53
[0.1.52]: https://github.com/wangyr2008/DebRT/compare/v0.1.51...v0.1.52
[0.1.51]: https://github.com/wangyr2008/DebRT/compare/v0.1.50...v0.1.51
[0.1.50]: https://github.com/wangyr2008/DebRT/compare/v0.1.49...v0.1.50
