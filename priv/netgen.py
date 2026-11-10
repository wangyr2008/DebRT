#!/usr/bin/env python3
"""DebRT 配置生成器。

用法:
  netgen.py --check                 仅校验 /etc/dert/config.json 并输出结果
  netgen.py --stage STAGE_DIR       校验并生成全部原生配置到 STAGE_DIR（保留相对路径）
输出（stdout）为 JSON：{ok, files:[{path,mode,content_b64}], restart:[...], pppoe:bool, cake:bool, nft:bool, summary}
永不写真实路径——写盘由 dert-priv 用原子方式完成。
"""
import base64
import ipaddress
import json
import os
import re
import sys

CONFIG_PATH = "/etc/dert/config.json"
SECRETS_PATH = "/etc/dert/secrets.json"
ULA_PREFIX = "fda1:9e3a:8b7f"   # DebRT 固定默认 ULA（见 COEXISTENCE.md，避开 sing-box fdfe:dcba::/126）

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
    # 跑 DHCPv6 客户端获取（IA_NA 出口地址 / IA_PD 委派前缀）——国内家宽标准形态
    # DHCP=ipv6 是客户端的启动开关：只写 DHCPPrefixDelegation=yes 不会拉起 DHCPv6
    # 客户端（真机 ppp0 抓 546/547 为零已证实），没有它 WithoutRA/UplinkInterface
    # 都无从生效
    # [Route] ::/0 Scope=link 也是必需的：国内运营商在 PPPoE 链路上不发 RA（真机抓
    # 22 秒 RA 为零），DHCPv6 又不携带默认路由，缺它就只有地址没有出口——表现为
    # "能拿到 IPv6 地址但 ping6 报 Network is unreachable"
    return ("# 由 DebRT 生成 — 请勿手工修改\n[Match]\nName=ppp0\n\n[Network]\n"
            "IPv6AcceptRA=no\n"
            "DHCP=ipv6\n"
            "DHCPPrefixDelegation=yes\n\n"
            "[DHCPv6]\nWithoutRA=solicit\nUseDNS=yes\n\n"
            "[DHCPPrefixDelegation]\nUplinkInterface=:self\n\n"
            "[Route]\nDestination=::/0\nScope=link\n")


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
        lines += ["IPv6SendRA=yes", "", "[IPv6SendRA]", "RouterLifetimeSec=600", "EmitDNS=yes"]
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
             "bind-interfaces",
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
        "    ct state established,related accept",
        "    ct state invalid drop",
        "    ct status dnat accept",
        "    iifname $WAN_IFS drop",
        "    # 其余转发（LAN→WAN、LAN↔tun/tailscale/podman、LAN hairpin 等）按 policy 放行",
        "  }",
        "",
        "  chain forward_mss {",
        "    type filter hook forward priority mangle - 5; policy accept;",
        "    oifname $WAN_IFS tcp flags syn tcp option maxseg size set rt mtu",
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
        "mtu 1492",
        "mru 1492",
        "nopcomp",
        "novj",
        "novjccomp",
        "linkname dert",
    ]
    if v6.get("enabled") and v6.get("mode") in ("native", "nat6"):
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
        # UplinkInterface=:self 段是客户端启动的必要条件（man 257 语义），见 gen_ppp_network
        _v6 = cfg.get("ipv6", {})
        if _v6.get("enabled") and _v6.get("mode") in ("native", "nat6"):
            files.append(("etc/systemd/network/25-ppp0.network", 0o644, gen_ppp_network(cfg)))

    # 变更影响面
    restart = ["dnsmasq"]
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
        "restart": restart,
        "networkd": True,
        "pppoe": wan_changed_if,
        "cake": bool(cfg.get("qos", {}).get("enabled")),
        "summary": f"mode={cfg['wan']['mode']} lan={cfg['lan']['ip']}/{lan_net(cfg).prefixlen} v6={'on/'+cfg['ipv6']['mode'] if cfg['ipv6']['enabled'] else 'off'}",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
