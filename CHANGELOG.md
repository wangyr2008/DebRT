# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)（`主版本.次版本.修订号`），
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

当前处于 `0.x` 阶段：次版本号可能包含不兼容变更，升级前请阅读对应条目。

## [Unreleased]

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
