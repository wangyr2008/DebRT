#!/usr/bin/env python3
"""DebRT 配置生成器。

用法:
  netgen.py --check                 仅校验 /etc/dert/config.json 并输出结果
  netgen.py --stage STAGE_DIR       校验并生成全部原生配置到 STAGE_DIR（保留相对路径）
输出（stdout）为 JSON：{ok, files:[{path,mode,content_b64}], networkd:bool, pppoe:bool, cake:bool, nft:bool, summary}
永不写真实路径——写盘由 dert-priv 用原子方式完成。
"""
import base64
import ipaddress
import json
import os
import re
import sys

# 数据路径跟随 DERT_FSROOT 测试缝（v0.1.87）：未设时与生产路径逐字符相同。
# 加这个是为了让"生成 → 预检 → 部署"整条流水线能在临时根里真跑一遍，
# 而不是只能把 CONFIG_PATH 打桩（打桩就绕过了真实入口的 schema_check）。
_FS = os.environ.get("DERT_FSROOT", "")
CONFIG_PATH = _FS + "/etc/dert/config.json"
SECRETS_PATH = _FS + "/etc/dert/secrets.json"
ULA_PREFIX = "fda1:9e3a:8b7f"   # DebRT 固定默认 ULA（见 COEXISTENCE.md，避开 sing-box fdfe:dcba::/126）
# PPPoE 链路的 MTU。取 1480 而不是 1492，是**实测偏好**：
#   同一台设备、同一批客户端，1480 时网页打开明显更快，1492 时偏慢。
# 用 ping6 -M do 从 LAN 源测到阿里/Cloudflare，1492 是通的，所以这不是
# "路径 MTU 更小"，而是 1480 让客户端从一开始就不发接近上限的包——
# QUIC/UDP 没有 MSS 钳制，1500 字节的包只能靠 PTB 纠正，重传一次就够慢。
# 代价：v4 每包少 12 字节（包数约多 0.8%），实测无感。
PPPOE_MTU = 1480

ERR = {"ok": False, "code": "NETGEN", "message": "", "hint": ""}


def fail(code, msg, hint=""):
    ERR.update(code=code, message=msg, hint=hint)
    print(json.dumps(ERR, ensure_ascii=False))
    sys.exit(1)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def mask_prefix(mask):
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}", strict=False).prefixlen
    except ValueError:
        fail("NETGEN", f"子网掩码 {mask!r} 无效")


def wan_ifaces(cfg):
    return [p["name"] for p in cfg.get("ports", []) if p.get("wan")]


def lan_ifaces(cfg):
    return [p["name"] for p in cfg.get("ports", []) if p.get("lan")]


def lan_net(cfg):
    lan = cfg["lan"]
    return ipaddress.IPv4Network(f"{lan['ip']}/{lan['mask']}", strict=False)


def cross_validate(cfg):
    net = lan_net(cfg)
    if net.prefixlen != 24:
        # P3-05：与 validate/UI/README 对齐——仅支持 /24。沿用 LAN_MASK_TOO_WIDE
        # 错误码：do_apply_all 的既有迁移逻辑按它触发，/25–/30 存量与 /16 等超宽
        # 掩码一样自动迁回 /24（收敛即自愈，用户不进网络页也能修好）
        fail("LAN_MASK_TOO_WIDE",
             f"LAN 子网掩码 /{net.prefixlen} 不受支持（历史遗留配置）",
             "当前版本仅支持 /24（255.255.255.0）——地址池按「IP 末段」配置的语义决定；"
             "收敛将自动迁移为 /24，也可在「网络设置 → 局域网 IP」手动改选")
    lan_ip = ipaddress.IPv4Address(cfg["lan"]["ip"])
    # 静态绑定必须在 LAN 子网内且不与路由器 IP 冲突
    for s in cfg.get("static", []):
        ip = ipaddress.IPv4Address(s["ip"])
        if ip not in net:
            fail("STATIC_NOT_IN_LAN",
                 f"静态绑定「{s['name']}」的 IP {s['ip']} 不在 LAN 网段 {net} 内",
                 "请在「DHCP 静态 IP 分配」中改用 LAN 子网内的地址")
        if ip == lan_ip:
            fail("STATIC_NOT_IN_LAN", f"静态绑定「{s['name']}」的 IP 与路由器 LAN IP 相同",
                 f"请改用 {net} 内其他未占用地址")
    # DHCP 池必须在子网内（掩码 >= /24 由 validate 保证，末段拼法才成立）
    dh = cfg.get("dhcp", {})
    if dh.get("enabled"):
        pre = str(net.network_address).rsplit(".", 1)[0]
        p0 = ipaddress.IPv4Address(f"{pre}.{dh['pool_start']}")
        p1 = ipaddress.IPv4Address(f"{pre}.{dh['pool_end']}")
        if p0 not in net or p1 not in net:
            fail("DHCP_NOT_IN_LAN",
                 f"DHCP 地址池 {p0}-{p1} 不在 LAN 网段 {net} 内",
                 "修改 LAN IP 后地址池会自动按新子网计算；请检查地址池末段是否正确")
    # 端口转发目标必须在 LAN 子网内
    for r in cfg.get("firewall", {}).get("portfw", []):
        if not r.get("enabled"):
            continue
        try:
            ip = ipaddress.IPv4Address(r["ip"])
        except ValueError:
            fail("FW_IP_BAD", f"转发规则「{r['name']}」目标 IP {r['ip']!r} 无效")
        if ip not in net:
            fail("FW_IP_NOT_LAN",
                 f"转发规则「{r['name']}」目标 {r['ip']} 不在 LAN 网段 {net} 内",
                 "端口转发只能指向局域网设备；如需访问路由器自身请改用管理页面相关设置")
    # WAN iface 与 LAN iface 不得重叠（validate 已保证，这里兜底）
    w, l = set(wan_ifaces(cfg)), set(lan_ifaces(cfg))
    if w & l:
        fail("NETGEN", f"网口角色冲突：{sorted(w & l)}")
    # ---- 以下与 validate 同规则的收敛期防线（保存层由 validate 拦；这里拦存量坏数据，
    # 否则会静默生成"看着正常、实际起不来/空转"的配置——B-02 同族教训，两侧同断言）----
    # P3-05：LAN IP 不能是网段/广播地址（按掩码算，/25–/30 下字符串判定失效）
    _laddr = ipaddress.IPv4Address(cfg["lan"]["ip"])
    if _laddr in (net.network_address, net.broadcast_address):
        fail("LAN_IP_BAD", f"LAN IP {cfg['lan']['ip']} 是 {net} 的网段/广播地址",
             "请改用网段内的普通主机地址（如 .1）")
    # P3-03：静态 WAN 网关必须在同网段（跨网段网关 networkd 拒配，WAN 起不来）
    if cfg.get("wan", {}).get("mode", "") == "static":
        try:
            _wnet = ipaddress.IPv4Network(f"{cfg['wan'].get('ip', '')}/{cfg['wan'].get('mask', '')}", strict=False)
            if ipaddress.IPv4Address(cfg["wan"].get("gw", "")) not in _wnet:
                fail("WAN_STATIC_BAD", f"静态网关 {cfg['wan'].get('gw')} 不在 WAN 网段 {_wnet} 内",
                     "网关必须与 WAN IP 同网段，通常是该网段的 .1 或 .254")
        except ValueError:
            pass   # 字段本身非法由 gen_wan_network / mask_prefix 报错
    # P3-04：静态 v6 WAN 地址必须带前缀长度（/128 让同网段网关不可达）
    # 注意：本段在 v6 矩阵段之前执行，v6/wm 尚未赋值——一律从 cfg 现取
    _v6c = cfg.get("ipv6", {})
    if _v6c.get("enabled") and _v6c.get("mode") == "static" and _v6c.get("addr") \
            and "/" not in str(_v6c.get("addr")):
        fail("V6_STATIC_BAD", f"静态 IPv6 地址 {_v6c.get('addr')!r} 缺少前缀长度",
             "请补上 /nn（如 2001:db8:1::2/64）；无前缀长度会按 /128 处理，同网段网关不可达")
    # P3-10：账号含控制字符会拆断 peers 行（shell_quote 只转义 \ 和 "）
    if re.search(r"[\x00-\x1f\x7f]", str(cfg.get("wan", {}).get("username", "") or "")):
        fail("PPPOE_FIELD_BAD", "宽带账号包含控制字符（换行/制表符等不可见字符）",
             "请在「上网设置」重新输入宽带账号并保存")
    # IPv6 × WAN 组合的收敛期防线（与 validate.cross_validate 保持同一套规则）。
    # 保存路径由 validate 拦，但收敛（开机/定时/升级后）直接读 config.json——
    # 存量非法配置若无这道防线，会静默生成"看着正常、实际空转"的配置
    # （v0.1.35 审计 A-01/A-02 的同类问题；v0.1.42 曾因只改一处漏掉 netgen 侧）。
    v6 = cfg.get("ipv6", {})
    wm = cfg.get("wan", {}).get("mode", "")
    if v6.get("enabled"):
        if wm == "static" and v6.get("mode") == "native":
            fail("V6_NATIVE_STATIC_WAN", "静态 WAN 不支持 IPv6 Native（PD）",
                 "静态接入没有 DHCPv6 客户端，拿不到前缀委派；请改用「静态 IPv6」模式或关闭 IPv6")
        if wm == "static" and v6.get("mode") == "nat6":
            fail("V6_NAT6_STATIC_UNSUPPORTED", "静态 WAN 不支持 IPv6 NAT6",
                 "静态线路请改用「静态 IPv6」模式（填写 WAN IPv6 地址与网关），NAT6 无出口地址可用")
        if wm == "pppoe" and v6.get("mode") == "static":
            fail("V6_STATIC_PPPPOE_UNSUPPORTED", "PPPoE 线路不支持静态 IPv6",
                 "PPPoE 的 IPv6 地址由拨号与 DHCPv6 协商，不支持静态表单；请改用 Native 或 NAT6")


def gen_wan_network(cfg):
    wifs = wan_ifaces(cfg)
    match = " ".join(wifs)
    v6 = cfg.get("ipv6", {})
    lines = ["# 由 DebRT 生成 — 请勿手工修改", "[Match]", f"Name={match}", "", "[Network]"]
    mode = cfg["wan"]["mode"]
    if mode == "dhcp":
        lines += ["DHCP=yes", "IPv6AcceptRA=yes"]
        if v6.get("enabled") and v6.get("mode") == "native":
            # A-08：PD 的正确位置（systemd ≥250 把它上移到 [Network]）。旧写法
            # [DHCPv6] PrefixDelegation= 在 257 上被静默忽略（Unknown key），PD 客户端
            # 一个请求都不发，LAN 永远拿不到前缀——真机 journalctl 实录
            lines.append("DHCPPrefixDelegation=yes")
    elif mode == "static":
        lines += ["DHCP=no", "IPv6AcceptRA=no",
                  f"Address={cfg['wan']['ip']}/{mask_prefix(cfg['wan']['mask'])}",
                  f"Gateway={cfg['wan']['gw']}"]
    else:  # pppoe — 底层口只做链路，IP 由 ppp0 承载；不给 IPv4 link-local（避免 WAN 二层暴露面）
        lines += ["DHCP=no", "IPv6AcceptRA=no", "LinkLocalAddressing=no", "ConfigureWithoutCarrier=yes"]
    # 静态 IPv6 WAN 地址（P2-3+N-02）：此前整个块挂在 IPv4 static 分支里，
    # dhcp+静态v6 是合法可保存的配置却永不落盘。dhcp 保留 IPv6AcceptRA=yes
    # （RA 与静态地址并存）；pppoe 由 validate 拒绝——v6 应落 ppp0，以太网口无承载点
    if mode in ("dhcp", "static") and v6.get("enabled") and v6.get("mode") == "static":
        # B-04：缺字段时给结构化错误，而不是 KeyError traceback
        _v6addr = v6.get("addr") or ""
        _v6gw = v6.get("gw") or ""
        if not _v6addr:
            fail("V6_STATIC_NEEDS_ADDR", "静态 IPv6 模式缺少 WAN IPv6 地址",
                 "请在「网络设置 → IPv6 设置」填写 WAN IPv6 地址（如 2001:db8:1::2/64）")
        lines.append(f"Address={_v6addr}")
        if _v6gw:
            lines.append(f"Gateway={_v6gw}")
    if mode == "dhcp":
        lines += ["", "[DHCPv4]", "UseDNS=yes", "UseGateway=yes", "UseNTP=no", "UseHostname=no"]
        if v6.get("enabled") and v6.get("mode") == "native":
            # B-14：UseDNS=yes——IPv6-only/MAP-E 运营商只经 DHCPv6 下发 DNS（RA 不带
            # RDNSS），写 no 等于主动放弃；学到的地址由 A-09 的钩子统一落盘
            lines += ["", "[DHCPv6]", "WithoutRA=solicit", "UseDNS=yes"]
    return "\n".join(lines) + "\n"


def gen_ppp_network(cfg):
    # PPPoE 的 IPv6：pppd 只协商 IPv6CP（链路本地），地址与前缀由 networkd 在 ppp0 上
    # 跑 DHCPv6 客户端获取（IA_NA 出口地址 / IA_PD 委派前缀）——国内家宽标准形态。
    # DHCP=ipv6 是客户端的启动开关：只写 DHCPPrefixDelegation=yes 不会拉起 DHCPv6
    # 客户端（真机 ppp0 抓 546/547 为零已证实），没有它 WithoutRA/UplinkInterface
    # 都无从生效。
    # [Route] ::/0 是必需的：国内运营商在 PPPoE 链路上不发 RA，DHCPv6 又不携带
    # 默认路由，缺它就只有地址没有出口——表现为"能拿到 IPv6 地址但 ping6 报
    # Network is unreachable"。
    # 但**不能**写 Scope=link：IPv6 路由没有 scope 概念，networkd 每次开机都会报
    # "/etc/systemd/network/25-ppp0.network:17: Scope= is specified for IPv6 route.
    #  It will be ignored."，而默认路由实际是靠 Destination=::/0 建起来的。
    #
    # v0.1.110：DHCPv6 相关段落随「IPv6 开关」生成。关闭时不启动 DHCPv6 客户端，
    # 也就不会去要前缀——这一步只靠 networkd 重启生效，**不需要重拨**。
    v6_on = bool(cfg.get("ipv6", {}).get("enabled"))
    lines = ["# 由 DebRT 生成 — 请勿手工修改", "[Match]", "Name=ppp0", "",
             "[Network]", "IPv6AcceptRA=no"]
    if v6_on:
        lines += ["DHCP=ipv6", "DHCPPrefixDelegation=yes"]
    # KeepConfiguration=yes 是必需的，否则 pppd 分配的 IPv4 会被 networkd 清掉。
    # networkd 重新配置/重启某个 link 时，默认（KeepConfiguration=no）会把
    # "不是自己配的"地址一并删除；而 ppp0 的 IPv4 是 pppd 用 ioctl 加的，
    # 对 networkd 来说是外来配置。真机 2026-11-12 实测：`networkctl reconfigure
    # ppp0` 之后 ppp0 只剩 DHCPv6 地址、IPv4 与默认路由全消失，v4 出网直接 000。
    lines += ["KeepConfiguration=yes", ""]
    if v6_on:
        lines += ["[DHCPv6]", "WithoutRA=solicit", "UseDNS=yes", "",
                  "[DHCPPrefixDelegation]", "UplinkInterface=:self", "",
                  "[Route]", "Destination=::/0"]
    return "\n".join(lines) + "\n"


def gen_lan_netdev(cfg):
    return "# 由 DebRT 生成 — 请勿手工修改\n[NetDev]\nName=br-lan\nKind=bridge\n"


def gen_lan_network(cfg):
    net = lan_net(cfg)
    v6 = cfg.get("ipv6", {})
    lines = ["# 由 DebRT 生成 — 请勿手工修改",
             "[Match]", "Name=br-lan", "",
             "[Network]",
             f"Address={cfg['lan']['ip']}/{net.prefixlen}",
             "IPForward=yes"]
    # 让 br-lan 的 v6 MTU 与 ppp0 对齐（1480）。注意：systemd-networkd **不会**
    # 把 IPv6MTUBytes 写进 RA 的 MTU 选项（真机抓包验证：RA 长度 80 字节，
    # 有选项时会变成 88），所以它只影响本机转发与 PTB 里报的 MTU，不影响客户端
    # 从 RA 读到的值。RA 照常发送——早前"设了它就不发 RA"的判断是抓包工具
    # 不可用造成的误判。
    if cfg["wan"].get("mode") == "pppoe" and v6.get("enabled"):
        lines.append(f"IPv6MTUBytes={PPPOE_MTU}")
    # LAN 桥成员口由 30-<name>.network 绑定（见 gen_port_networks）
    # IPv6：RA 统一由 networkd 发布（dnsmasq 不再发 RA，避免双 RA 源，见审计 B-21）
    if v6.get("enabled") and v6.get("mode") == "static" and not v6.get("prefix"):
        fail("V6_STATIC_NEEDS_PREFIX", "静态 IPv6 模式缺少 LAN IPv6 前缀",
             "请在「网络设置 → IPv6 设置」补填 LAN IPv6 前缀（如 2001:db8:abcd::/64），或改用 NAT6")
    # PPPoE×native：PD 消费与 RA 均走 networkd（v0.1.44 起 ppp0 上以
    # [DHCPPrefixDelegation] UplinkInterface=:self 启动 DHCPv6 客户端——man 257 语义：
    # WithoutRA=solicit 仅在该段存在时生效，v0.1.41 缺它导致客户端永不启动）
    if v6.get("enabled"):
        if v6.get("mode") == "nat6":
            lines += [f"Address={ULA_PREFIX}::1/64"]
        elif v6.get("mode") == "static" and v6.get("prefix"):
            pnet = ipaddress.IPv6Network(v6["prefix"], strict=False)
            host = ipaddress.IPv6Address(int(pnet.network_address) + 1)  # 网段 ::1
            lines += [f"Address={host}/{pnet.prefixlen}"]
        # A-08：[IPv6PrefixDelegation] 段（段名与键均自 systemd ≥255 废弃，257 静默忽略）
        # 已删除——PD 模型两端都要开：WAN 请求前缀，LAN 在 [Network] 里声明消费
        # （N-02：只开 WAN 前缀拿到也没人用，br-lan 依然只有 fe80），
        # 拿到的前缀由 networkd 经 IPv6SendRA 自动宣告（man 原文同）
        if v6.get("mode") == "native" and cfg["wan"].get("mode") in ("dhcp", "pppoe"):
            lines.append("DHCPPrefixDelegation=yes")
        # RA 的两个时间必须留足余量：RouterLifetimeSec 只比发送间隔大一点点时，
        # 客户端会在下一条 RA 到达前就把默认路由过期掉——表现是"用着好好的，
        # 十几分钟后全屋 v6 突然上不去，重连 Wi-Fi 又好了（RS 会触发一次 RA）"。
        # 真机 2026-11-13 15:51 就这么断过一次：15:41 发过一条 RA，
        # 600 秒后客户端到期，而下一条恰好也在这个点。
        # 按 RFC 4861 的建议取 3 倍关系：间隔 ≤200 秒，寿命 1800 秒。
        lines += ["IPv6SendRA=yes", "", "[IPv6SendRA]",
                  "MaxRtrAdvIntervalSec=200",
                  "RouterLifetimeSec=1800",
                  "EmitDNS=yes"]
        if v6.get("mode") in ("nat6", "static"):
            # NAT6/静态有确定的 LAN v6 地址，直告本机（dnsmasq 在 br-lan 上服务 DNS）
            lan_v6 = f"{ULA_PREFIX}::1" if v6.get("mode") == "nat6" else \
                str(ipaddress.IPv6Address(int(ipaddress.IPv6Network(v6["prefix"], strict=False).network_address) + 1))
            lines += [f"DNS={lan_v6}"]
        if v6.get("mode") == "native":
            # native 的 LAN 地址来自上游 PD、运行期才确定——宣告本机 link-local
            lines.append("DNS=_link_local")
        if v6.get("mode") == "nat6":
            lines += ["", "[IPv6Prefix]", f"Prefix={ULA_PREFIX}::/64"]
        elif v6.get("mode") == "static" and v6.get("prefix"):
            lines += ["", "[IPv6Prefix]", f"Prefix={v6['prefix']}"]
        # 必须显式指定 SubnetId。运营商委派的 PD 常常是 /60，而 WAN 自己的
        # IA_NA 地址就落在 PD 的**第一个** /64 里；networkd 默认（SubnetId=auto）
        # 把第一个 /64 分给 LAN，于是 WAN 与 LAN 同前缀：路由表出现两条相同的
        # /64，选路优先 ppp0，发往 LAN 客户端的包全被送进 WAN，LAN 的 v6 彻底
        # 不通（真机 2026-11-14 08:39 实测：ppp0 = ...:1fe0::/64，br-lan 也被
        # 分到 ...:1fe0::/64，`ip -6 route get <客户端>` 直接返回 dev ppp0）。
        # 取第二个 /64 就避开了 WAN 所在的那一个。
        if v6.get("mode") == "native" and cfg["wan"].get("mode") in ("dhcp", "pppoe"):
            uplink = "ppp0" if cfg["wan"]["mode"] == "pppoe" else \
                next((p["name"] for p in cfg.get("ports", []) if p.get("wan")), "")
            if uplink:
                lines += ["", "[DHCPPrefixDelegation]",
                          f"UplinkInterface={uplink}",
                          "SubnetId=1"]
    return "\n".join(lines) + "\n"


def gen_port_networks(cfg):
    """LAN 成员口与未使用口：LAN 口加入 br-lan。"""
    out = []
    for p in cfg.get("ports", []):
        name = p["name"]
        if p.get("lan"):
            body = ("# 由 DebRT 生成 — 请勿手工修改\n"
                    f"[Match]\nName={name}\n\n[Network]\nBridge=br-lan\n")
        elif p.get("wan"):
            continue  # WAN 口由 10-wan.network 管理
        else:
            body = ("# 由 DebRT 生成 — 请勿手工修改\n"
                    f"[Match]\nName={name}\n\n[Network]\nLinkLocalAddressing=no\nIPv6AcceptRA=no\nDHCP=no\n")
        out.append((f"etc/systemd/network/30-{name}.network", body))
    return out


def gen_dnsmasq(cfg):
    net = lan_net(cfg)
    lan_ip = cfg["lan"]["ip"]
    dh = cfg.get("dhcp", {})
    v6 = cfg.get("ipv6", {})
    wan = cfg.get("wan", {})
    lines = ["# 由 DebRT 生成 — 请勿手工修改",
             "interface=br-lan",
             "interface=lo",
             # 必须是 bind-dynamic 而不是 bind-interfaces：
             # bind-interfaces 只在启动瞬间静态绑定当时的地址，之后不再跟踪。
             # 开机时序是 dnsmasq 先起（此时 br-lan 只有 fe80::），4 秒后 PD 委派
             # 的 GUA 才出现——GUA 上的 53 端口永远不会被监听。而 dns-split 的 nft
             # 表把白名单设备的 v6 DNS DNAT 到该 GUA，于是全部 connection refused，
             # iPhone 每次新域名先撞一次 v6 失败再回落 v4（真机 2026-11-11 复现）。
             # bind-dynamic 会跟踪接口地址变化，GUA 出现/换前缀后自动补绑。
             "bind-dynamic",
             "dhcp-authoritative",
             "domain=lan",
             "local=/lan/",
             "cache-size=5000",
             # A-09 后上游含 IPv6：all-servers 并行查询，避免串行被不通的 v6 上游拖慢
             "all-servers",
             # 乐观缓存（RFC 8767，dnsmasq 2.86+）：过期条目先应答再后台刷新——
             # PPPoE 重拨/上游切换瞬间 LAN 解析无感（真机 2.91 支持该选项）
             "use-stale-cache",
             # Debian 默认租约路径：dnsmasq 启动期以 root 打开，包内目录权限即保证可写（审计 B-04）
             "dhcp-leasefile=/var/lib/misc/dnsmasq.leases"]
    if dh.get("enabled"):
        # 掩码 >= /24 时 network_address 末段必为 0，前三段即池前缀（validate 已限制掩码 >= /24）
        pre = str(net.network_address).rsplit(".", 1)[0]
        lines.append(f"dhcp-range={pre}.{dh['pool_start']},{pre}.{dh['pool_end']},{dh.get('lease', 120)}m")
        gw = dh.get("gw") or lan_ip
        lines.append(f"dhcp-option=option:router,{gw}")
        dns = [d for d in (dh.get("dns1"), dh.get("dns2")) if d] or [lan_ip]
        lines.append("dhcp-option=option:dns-server," + ",".join(dns))
    for s in cfg.get("static", []):
        lines.append(f"dhcp-host={s['mac']},{s['ip']},{s['name']}")
    if not v6.get("enabled"):
        # IPv6 关闭时过滤 AAAA：上游可能返回 AAAA，但本机无 v6 路由只会让客户端
        # 拿着不可达的 v6 地址连接失败（真机首装实测， China Mobile 上游带 v6 DNS）
        lines.append("filter-AAAA")
    lines.append("")
    lines.append("# 上游 DNS")
    lines.append("no-resolv")
    if wan.get("dns_mode") == "manual" and wan.get("dns"):
        for d in wan["dns"]:
            lines.append(f"server={d}")
        v6d = v6.get("dns") if v6.get("enabled") else []
        for d in (v6d or []):
            lines.append(f"server={d}")
    elif wan.get("mode") == "static":
        # N-03：存量 static+auto 配置（v0.1.29 前可保存）没有上游生产者，
        # no-resolv+空 servers-file = DNS 全挂。写公共 DNS 兜底保证可用；
        # 新配置已在 validate 层拒绝该组合（WAN_DNS_STATIC_AUTO）
        lines.append("server=223.5.5.5")
        lines.append("server=119.29.29.29")
        v6d = v6.get("dns") if v6.get("enabled") else []
        for d in (v6d or []):
            lines.append(f"server={d}")
    else:
        # servers-file 会在 SIGHUP 时重读（conf-dir 只在启动时读——dispatcher 钩子写完
        # 上游后 HUP dnsmasq 才能生效，真机首装实测）
        lines.append("servers-file=/run/dert-dns/upstream.conf")
        # B-01：WAN DNS 自动时，IPv6 卡片手填的 DNS 也必须落盘（此前被静默丢弃）
        v6d = v6.get("dns") if v6.get("enabled") and v6.get("dns_mode") == "manual" else []
        for d in (v6d or []):
            lines.append(f"server={d}")
    # 注意：RA 统一由 networkd 发布（见 gen_lan_network），dnsmasq 不发 RA（审计 B-21 单一 RA 源）
    return "\n".join(lines) + "\n"


def gen_nft(cfg):
    # PPPoE 模式下三层流量在 ppp0 上；但物理口也必须纳入防火墙（input 过滤 + hairpin），
    # 否则其 link-local 地址会暴露面板 80 端口（审计 B-05）
    if cfg["wan"]["mode"] == "pppoe":
        wifs = ["ppp0"] + wan_ifaces(cfg)
    else:
        wifs = wan_ifaces(cfg)
    wan_set = ", ".join(f'"{i}"' for i in wifs)
    # v6 的 MSS 上限 = MTU - IPv6 头(40) - TCP 头(20)。PPPoE 用 PPPOE_MTU（见其注释），
    # 其他接入方式按 1500 算。
    v6_mss = (PPPOE_MTU if cfg["wan"].get("mode") == "pppoe" else 1500) - 60
    net = lan_net(cfg)
    fw = cfg.get("firewall", {})
    v6 = cfg.get("ipv6", {})
    dnat, dnat_lan = [], []
    for r in fw.get("portfw", []):
        if not r.get("enabled"):
            continue
        tgt = r["ip"] + (f':{r["lport"]}' if r["lport"] != r["wport"].split("-")[0] else "")
        if r["proto"] in ("tcp", "both"):
            dnat.append(f'    iifname $WAN_IFS tcp dport {r["wport"]} dnat ip to {tgt}')
            dnat_lan.append(f'    iifname $LAN_BR ip daddr @wan_v4 tcp dport {r["wport"]} dnat ip to {tgt}')
        if r["proto"] in ("udp", "both"):
            dnat.append(f'    iifname $WAN_IFS udp dport {r["wport"]} dnat ip to {tgt}')
            dnat_lan.append(f'    iifname $LAN_BR ip daddr @wan_v4 udp dport {r["wport"]} dnat ip to {tgt}')
    lines = [
        "#!/usr/sbin/nft -f",
        "# DebRT 基础防火墙 —— 只操作本表 inet dert_base，",
        "# 绝不 flush ruleset / 不触碰 sing-box、podman、tailscale 的表（见 COEXISTENCE.md）。",
        "",
        "table inet dert_base",
        "flush table inet dert_base",
        "",
        "table inet dert_base {",
        f"  define WAN_IFS = {{ {wan_set} }}",
        '  define LAN_BR = "br-lan"',
        f"  define LAN_NET = {net.with_prefixlen}",
        "  # 当前 WAN IPv4 地址集合：由 priv/wan-addr-sync（钩子/apply 调用）维护，",
        "  # hairpin DNAT 仅对「目的地址 = 本机 WAN IP」的 LAN 流量生效，绝不劫持其他出站流量",
        "  set wan_v4 { type ipv4_addr; }",
        # RFC 7084 L-14：用已失效前缀作源地址的转发包必须回 ICMPv6 不可达，
        # 让客户端立刻把该源地址判为不可用并重新选源，而不是等它自己老化。
        # 集合由 priv/prefix-retire.py 维护（前缀变更时同步）。
        "  set retired_v6 { type ipv6_addr; flags interval; }",
        "",
        "  chain input {",
        "    type filter hook input priority filter; policy accept;",
        "    ct state established,related accept",
        "    iifname $WAN_IFS jump wan_in",
        "  }",
        "",
        "  chain wan_in {",
        "    ct state invalid drop",
        "    ip protocol icmp accept",
        "    meta l4proto ipv6-icmp accept",
        "    udp dport 68 accept",
        "    meta nfproto ipv6 udp dport 546 accept  # DHCPv6 客户端端口：PD 必需——多播 Solicit 不经 conntrack，服务端回应不算 established（P1-2）",
    ]
    if fw.get("wan_input_drop", True):
        lines.append("    drop")
    lines += [
        "    return",
        "  }",
        "",
        "  chain forward {",
        "    type filter hook forward priority filter; policy accept;",
        "    ip6 saddr @retired_v6 reject with icmpx type no-route",
        "    ct state established,related accept",
        "    ct state invalid drop",
        "    ct status dnat accept",
        "    iifname $WAN_IFS drop",
        "    # 其余转发（LAN→WAN、LAN↔tun/tailscale/podman、LAN hairpin 等）按 policy 放行",
        "  }",
        "",
        "  chain forward_mss {",
        "    type filter hook forward priority mangle - 5; policy accept;",
        "    meta nfproto ipv4 oifname $WAN_IFS tcp flags syn tcp option maxseg size set rt mtu",
        # v6 单独算：MSS = MTU - 40(IPv6 头) - 20(TCP 头)。用显式值而不是 rt mtu，
        # 是因为 rt mtu 在 v6 下取的是路由 MTU，实测没能把客户端的 1440 压下来
        # （PTB 计数一直在涨）。显式 1420 让 SYN 一落地就定死。
        f"    meta nfproto ipv6 oifname $WAN_IFS tcp flags syn tcp option maxseg size set {v6_mss}",
        "  }",
        "",
        "  # dnsmasq 的上游 DNS 查询必须绕过 sing-box 的 auto_redirect。",
        "  # 原因：sing-box 的 output 链末尾会给「未被识别的本机出站流量」打 0x2023，",
        "  # 再经 ip rule 9001 送进 tun0，被 hijack-dns 拦截后返回 fakeip —— 非白名单",
        "  # 设备拿到假地址就断了（真机 2026-11-11 复现，dnsmasq 日志显示它以为在问运营商）。",
        "  # dns_mode: disabled 只关掉 dport 53 的 dnat，管不到这条 mark + 路由链路。",
        "  #",
        "  # 标记借用 sing-box 自己的 0x2024（语义=「本进程发出的流量，走 main 表」）：",
        "  # 它是唯一在 output / output_prematch / prerouting 各链都无条件 return 的值，",
        "  # 也是 ip rule 9000 唯一放行到 main 表的值。升级 sing-box 大版本时需复核该含义。",
        "  chain dns_bypass {",
        "    type filter hook output priority mangle - 5; policy accept;",
        "    meta l4proto { tcp, udp } th dport 53 counter meta mark set 0x00002024",
        "  }",
        "",
    ]
    # hairpin：LAN 内用公网 IP 访问自己 —— LAN 侧也要做 DNAT（审计 C-20）
    lines += ["  chain dstnat {", "    type nat hook prerouting priority dstnat; policy accept;"] + dnat + dnat_lan + ["  }", ""]
    lines += [
        "  chain srcnat {",
        "    type nat hook postrouting priority srcnat; policy accept;",
        "    ip saddr $LAN_NET oifname $WAN_IFS masquerade",
        "    # hairpin 回程伪装：仅作用于被 DNAT 的连接（全量 LAN→LAN 伪装会破坏 mDNS 等，复审 N-02）",
        "    oifname $LAN_BR ct status dnat masquerade",
    ]
    if v6.get("enabled") and v6.get("mode") == "nat6":
        lines.append(f"    ip6 saddr {ULA_PREFIX}::/48 oifname $WAN_IFS masquerade")
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def shell_quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def gen_pppd(cfg, password):
    wan = cfg["wan"]
    v6 = cfg.get("ipv6", {})
    wif = wan_ifaces(cfg)[0]
    lines = [
        "# 由 DebRT 生成 — 请勿手工修改",
        f"user {shell_quote(wan['username'])}",
        f"plugin rp-pppoe.so {wif}",
        "noipdefault",
        "noauth",          # 对端是运营商接入服务器；与 Debian dsl-provider 模板一致（审计 B-20）
        "usepeerdns",
        "defaultroute",
        "persist",
        "maxfail 0",
        "holdoff 5",
        "lcp-echo-interval 20",
        "lcp-echo-failure 3",
        f"mtu {PPPOE_MTU}",
        f"mru {PPPOE_MTU}",
        "nopcomp",
        "novj",
        "novjccomp",
        "linkname dert",
    ]
    # +ipv6 常开。IPv6CP 协商只是把链路本地能力建起来，"要不要分配地址"由
    # networkd 的 DHCP=ipv6 决定（见 gen_ppp_network）。常开之后，开关 IPv6
    # 不必重拨——真机 2026-11-14 实测：一次"关→开"触发两次重拨、换了两批 PD
    # 前缀，客户端因此攒下三组地址（每组稳定+隐私两条）。
    lines.append("+ipv6")
    return "\n".join(lines) + "\n"


def gen_chap(cfg, password):
    user = shell_quote(cfg["wan"]["username"])
    pwd = shell_quote(password or "x")
    return f"# 由 DebRT 生成 — 请勿手工修改\n{user} * {pwd} *\n"


def schema_check(cfg):
    """收敛期结构兜底（P3-12）。

    validate.py 保证的是"面板写进来的配置"，而收敛路径（开机 / 6h timer / 升级后 /
    备份恢复）直接读 /etc/dert/config.json。结构残缺时下游有两种坏结局：
      1) 生成阶段 traceback（KeyError 'lan' / AddressValueError）——do_apply_all 曾把
         stderr 丢掉，用户只看到"配置生成失败"，提示里的"上方错误信息"是空的；
      2) 静默生成空转配置——如 wan.mode="" 会落进 pppoe 分支，产出 DHCP=no 且无
         Address 的 10-wan.network，WAN 口彻底没地址而 apply 返回成功。
    这里在生成前一次性拦住，全部走结构化 JSON 错误。
    """
    hint = ("请在「网络设置」重新保存一次；若已无法进入面板，可从 "
            "/var/lib/dert/lastgood 恢复上一份可用配置")

    def bad(msg, howto=hint):
        fail("CONFIG_SCHEMA", msg, howto)

    if not isinstance(cfg, dict):
        bad("config.json 顶层不是对象", "请从 /var/lib/dert/lastgood 恢复，或重新运行 install.sh")

    # 必填段：下游是直接下标访问（cfg["lan"] / cfg["ipv6"]["mode"] / cfg["ports"]）
    for sec in ("ports", "wan", "lan", "ipv6"):
        if not isinstance(cfg.get(sec), list if sec == "ports" else dict):
            bad(f"配置缺少或损坏的段落：{sec}")

    if not cfg["ports"]:
        bad("网口列表为空", "请进入「网络设置 → 网口绑定」重新勾选 WAN/LAN 口并保存")
    for p in cfg["ports"]:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            bad("网口条目缺少名称", "请进入「网络设置 → 网口绑定」重新保存")

    mode = cfg["wan"].get("mode")
    if mode not in ("dhcp", "pppoe", "static"):
        bad(f"上网方式 {mode!r} 不受支持",
            "请进入「网络设置 → 上网设置」重新选择（PPPoE / DHCP / 静态 IP）并保存")
    if mode == "pppoe" and not str(cfg["wan"].get("username") or "").strip():
        bad("PPPoE 模式缺少宽带账号", "请在「网络设置 → 上网设置」填写宽带账号并保存")
    if mode == "static":
        for k, label in (("ip", "IP 地址"), ("mask", "子网掩码"), ("gw", "默认网关")):
            if not str(cfg["wan"].get(k) or "").strip():
                bad(f"静态 IP 模式缺少{label}", "请在「网络设置 → 上网设置」补全 IP/掩码/网关后保存")

    if not str(cfg["lan"].get("ip") or "").strip() or not str(cfg["lan"].get("mask") or "").strip():
        bad("LAN IP 或子网掩码为空", "请在「网络设置 → 局域网 IP」重新填写并保存")

    v6 = cfg["ipv6"]
    if v6.get("enabled") and v6.get("mode") not in ("native", "nat6", "static"):
        bad(f"IPv6 方式 {v6.get('mode')!r} 不受支持",
            "请进入「网络设置 → IPv6 设置」重新选择并保存")

    dh = cfg.get("dhcp")
    if dh is not None and not isinstance(dh, dict):
        bad("DHCP 段格式损坏", "请在「网络设置 → DHCP 服务」重新保存")
    if isinstance(dh, dict) and dh.get("enabled"):
        for k in ("pool_start", "pool_end", "lease"):
            if not isinstance(dh.get(k), int) or isinstance(dh.get(k), bool):
                bad(f"DHCP 参数 {k} 不是整数", "请在「网络设置 → DHCP 服务」重新填写地址池与租期")

    st = cfg.get("static")
    if st is not None and not isinstance(st, list):
        bad("静态绑定段格式损坏", "请在「网络设置 → DHCP 静态 IP 分配」重新保存")
    for s in (st or []):
        if not isinstance(s, dict) or not all(
                isinstance(s.get(k), str) and s.get(k).strip() for k in ("name", "ip", "mac")):
            bad("静态绑定条目缺少 name/ip/mac", "请在「网络设置 → DHCP 静态 IP 分配」重新保存")

    fw = cfg.get("firewall")
    if fw is not None and not isinstance(fw, dict):
        bad("防火墙段格式损坏", "请在「安全中心」重新保存")
    for r in ((fw or {}).get("portfw") or []):
        if not isinstance(r, dict):
            bad("转发规则条目格式损坏", "请在「安全中心」重新保存转发规则")
        for k in ("name", "proto", "wport", "ip", "lport"):
            if not isinstance(r.get(k), str) or not r.get(k).strip():
                bad(f"转发规则缺少字段 {k}", "请在「安全中心」重新保存转发规则")
        if r.get("proto") not in ("tcp", "udp", "both"):
            bad(f"转发规则协议 {r.get('proto')!r} 不受支持",
                "请在「安全中心」把协议改为 TCP / UDP / TCP+UDP 后保存")

    if cfg.get("qos") is not None and not isinstance(cfg.get("qos"), dict):
        bad("QoS 段格式损坏", "请在「网络设置 → QoS 限速」重新保存")

    # ---- P3-25：字段级安全断言（收敛侧）----
    # validate.py 的字段校验只在「面板写入」这一条路径生效；备份恢复、回滚、
    # 升级后收敛都是直接读 config.json，绕过了它——而这些字段会被原样拼进
    # dnsmasq / nft / network 文件。一个换行就能注入新指令（dnsmasq 的
    # dhcp-script= 会以 root 执行脚本）。
    #
    # 刻意只拦「会导致注入」的字符，不碰语义：存量 /25 掩码之类仍放行，
    # 否则 backup-restore 会因噎废食拒绝恢复（见 dert-priv 中
    # do_backup_restore 的注释）。真正的语义校验交给 validate 与 cross_validate。
    ctrl = re.compile(r"[\x00-\x1f\x7f]")

    def scan(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                scan(x, f"{path}.{k}" if path else str(k))
        elif isinstance(v, list):
            for i, x in enumerate(v):
                scan(x, f"{path}[{i}]")
        elif isinstance(v, str) and ctrl.search(v):
            bad(f"配置字段 {path} 含控制字符（换行/制表符等不可见字符）",
                "请在面板重新填写该项；若来自备份包，请重新配置后再备份")

    scan(cfg, "")

    # 静态绑定会拼成 dnsmasq 的 dhcp-host={mac},{ip},{name}：
    # 逗号会错位字段、换行会拆成两条指令，所以字符集必须与 validate 侧一致
    for i, s in enumerate(cfg.get("static") or []):
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if isinstance(name, str) and not re.fullmatch(
                r"[\w\u4e00-\u9fff\-. ]{1,32}", name, re.UNICODE):
            bad(f"静态绑定第 {i + 1} 条的设备名 {name!r} 含非法字符",
                "设备名支持 1–32 位中英文、数字、点、横线、空格；逗号与换行不可用")
        mac = s.get("mac")
        if isinstance(mac, str) and not re.fullmatch(
                r"(?i)[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac):
            bad(f"静态绑定第 {i + 1} 条的 MAC {mac!r} 格式不正确",
                "格式示例：AA:BB:CC:DD:EE:FF")

    # 转发规则的端口会被直接拼进 nft 表达式
    for i, r in enumerate(((cfg.get("firewall") or {}).get("portfw") or [])):
        if not isinstance(r, dict):
            continue
        for k in ("wport", "lport"):
            v = r.get(k)
            if isinstance(v, str) and not re.fullmatch(r"\d{1,5}(-\d{1,5})?", v):
                bad(f"转发规则第 {i + 1} 条的 {k} {v!r} 不是合法端口",
                    "端口支持 1–65535，或 8080-8090 这样的区间")


def main():
    global CONFIG_PATH
    # --config PATH：让 backup-restore 能在落盘前预检暂存配置（P3-12）
    if "--config" in sys.argv:
        CONFIG_PATH = sys.argv[sys.argv.index("--config") + 1]
    try:
        cfg = load_json(CONFIG_PATH)
    except FileNotFoundError:
        fail("NO_CONFIG", f"未找到 {CONFIG_PATH}", "请先运行 install.sh 完成部署")
    except json.JSONDecodeError as e:
        fail("CONFIG_CORRUPT", f"config.json 解析失败：{e}", "配置文件可能被意外截断；可从 /var/lib/dert/lastgood 恢复上一份可用配置")

    # P3-12：先查结构再查语义——缺段/缺键在这里就变成结构化错误，不会等到生成阶段
    # 抛 traceback，也不会静默产出空转配置
    schema_check(cfg)
    if "--schema" in sys.argv:
        print(json.dumps({"ok": True, "schema": "ok", "config": CONFIG_PATH}, ensure_ascii=False))
        return

    secrets = {}
    try:
        secrets = load_json(SECRETS_PATH)
    except Exception:
        secrets = {}

    cross_validate(cfg)
    mode = "check"
    stage = None
    if "--stage" in sys.argv:
        mode = "stage"
        stage = sys.argv[sys.argv.index("--stage") + 1]

    password = secrets.get("pppoe_password", "")
    # P3-10 收敛侧：密码在 secrets 里、validate 摸不到——存量坏密码在这里拦截，
    # 否则 gen_chap 把 chap-secrets 拆断成多行，拨号永久失败且难排查。
    # 仅 PPPoE 模式检查：其他模式下密码不参与生成，拦了会堵死"改走 DHCP"的逃生路
    if cfg["wan"]["mode"] == "pppoe" and re.search(r"[\x00-\x1f\x7f]", password or ""):
        fail("PPPOE_FIELD_BAD", "已保存的宽带密码包含控制字符（换行/制表符等不可见字符）",
             "请在「上网设置 → 上网设置」重新填写宽带密码并保存")
    files = []  # (relpath, mode_octal, content)

    files.append(("etc/systemd/network/10-wan.network", 0o644, gen_wan_network(cfg)))
    files.append(("etc/systemd/network/20-lan.netdev", 0o644, gen_lan_netdev(cfg)))
    files.append(("etc/systemd/network/20-lan.network", 0o644, gen_lan_network(cfg)))
    for rel, body in gen_port_networks(cfg):
        files.append((rel, 0o644, body))
    files.append(("etc/dnsmasq.d/dert.conf", 0o644, gen_dnsmasq(cfg)))
    files.append(("etc/nftables.d/dert-base.nft", 0o644, gen_nft(cfg)))
    if cfg["wan"]["mode"] == "pppoe":
        files.append(("etc/ppp/peers/dert", 0o644, gen_pppd(cfg, password)))
        files.append(("etc/ppp/chap-secrets", 0o600, gen_chap(cfg, password)))
        files.append(("etc/ppp/pap-secrets", 0o600, gen_chap(cfg, password)))
        # PPPoE 的 IPv6 走 networkd：25-ppp0.network 的 [DHCPPrefixDelegation]
        # UplinkInterface=:self 段是客户端启动的必要条件（man 257 语义），见 gen_ppp_network。
        #
        # **这个文件必须始终生成**，不能只在 IPv6 开启时生成：里面的
        # KeepConfiguration=yes 是 ppp0 的 IPv4 保护伞——networkd 重配/重启时
        # 默认会把"不是自己配的"地址删掉，而 ppp0 的 IPv4 是 pppd 用 ioctl 加的。
        # 真机 2026-11-12 踩过：改个 LAN 设置就断 WAN。关闭 IPv6 时这个文件仍要
        # 存在（此时内容只有 IPv6AcceptRA=no + KeepConfiguration=yes）。
        files.append(("etc/systemd/network/25-ppp0.network", 0o644, gen_ppp_network(cfg)))

    # 变更影响面
    wan_changed_if = cfg["wan"]["mode"] == "pppoe"
    out_files = []
    for rel, fmode, content in files:
        if mode == "stage":
            full = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(full, fmode)
        out_files.append({"path": rel, "mode": fmode, "content_b64": base64.b64encode(content.encode()).decode()})

    print(json.dumps({
        "ok": True,
        "files": out_files,
        "networkd": True,
        "pppoe": wan_changed_if,
        "cake": bool(cfg.get("qos", {}).get("enabled")),
        "summary": f"mode={cfg['wan']['mode']} lan={cfg['lan']['ip']}/{lan_net(cfg).prefixlen} v6={'on/'+cfg['ipv6']['mode'] if cfg['ipv6']['enabled'] else 'off'}",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
