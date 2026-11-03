#!/usr/bin/env python3
"""SeedRou 配置生成器。

用法:
  netgen.py --check                 仅校验 /etc/seedrou/config.json 并输出结果
  netgen.py --stage STAGE_DIR       校验并生成全部原生配置到 STAGE_DIR（保留相对路径）
输出（stdout）为 JSON：{ok, files:[{path,mode,content_b64}], restart:[...], pppoe:bool, cake:bool, nft:bool, summary}
永不写真实路径——写盘由 seedrou-priv 用原子方式完成。
"""
import base64
import ipaddress
import json
import os
import sys

CONFIG_PATH = "/etc/seedrou/config.json"
SECRETS_PATH = "/etc/seedrou/secrets.json"
ULA_PREFIX = "fda1:9e3a:8b7f"   # SeedRou 固定默认 ULA（见 COEXISTENCE.md，避开 sing-box fdfe:dcba::/126）

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
    if net.prefixlen < 24:
        fail("LAN_MASK_TOO_WIDE",
             f"LAN 子网掩码 /{net.prefixlen} 过宽（历史遗留配置）",
             "新版仅支持 /24–/30：请进入「网络设置 → 局域网 IP」把子网掩码改为 255.255.255.0 并同步调整 DHCP 地址池")
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
    # PPPoE 线路没有 DHCPv6-PD 客户端，Native 模式实际不可用（存量坏配置在收敛期暴露，复审 R-07）
    if cfg.get("wan", {}).get("mode") == "pppoe" and cfg.get("ipv6", {}).get("enabled") \
            and cfg.get("ipv6", {}).get("mode") == "native":
        fail("V6_NATIVE_PPPPOE_UNSUPPORTED",
             "PPPoE 线路不支持 IPv6 Native（PD）",
             "请进入「网络设置 → IPv6 设置」改为 NAT6 或关闭 IPv6")


def gen_wan_network(cfg):
    wifs = wan_ifaces(cfg)
    match = " ".join(wifs)
    v6 = cfg.get("ipv6", {})
    lines = ["# 由 SeedRou 生成 — 请勿手工修改", "[Match]", f"Name={match}", "", "[Network]"]
    mode = cfg["wan"]["mode"]
    if mode == "dhcp":
        lines += ["DHCP=yes", "IPv6AcceptRA=yes"]
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
        lines += [f"Address={v6['addr']}", f"Gateway={v6['gw']}"]
    if mode == "dhcp":
        lines += ["", "[DHCPv4]", "UseDNS=yes", "UseGateway=yes", "UseNTP=no", "UseHostname=no"]
        if v6.get("enabled") and v6.get("mode") == "native":
            lines += ["", "[DHCPv6]", "PrefixDelegation=yes", "WithoutRA=solicit", "UseDNS=no"]
    return "\n".join(lines) + "\n"


def gen_lan_netdev(cfg):
    return "# 由 SeedRou 生成 — 请勿手工修改\n[NetDev]\nName=br-lan\nKind=bridge\n"


def gen_lan_network(cfg):
    net = lan_net(cfg)
    v6 = cfg.get("ipv6", {})
    lines = ["# 由 SeedRou 生成 — 请勿手工修改",
             "[Match]", "Name=br-lan", "",
             "[Network]",
             f"Address={cfg['lan']['ip']}/{net.prefixlen}",
             "IPForward=yes"]
    # LAN 桥成员口由 30-<name>.network 绑定（见 gen_port_networks）
    # IPv6：RA 统一由 networkd 发布（dnsmasq 不再发 RA，避免双 RA 源，见审计 B-21）
    if v6.get("enabled") and v6.get("mode") == "static" and not v6.get("prefix"):
        fail("V6_STATIC_NEEDS_PREFIX", "静态 IPv6 模式缺少 LAN IPv6 前缀",
             "请在「网络设置 → IPv6 设置」补填 LAN IPv6 前缀（如 2001:db8:abcd::/64），或改用 NAT6")
    if v6.get("enabled"):
        if v6.get("mode") == "nat6":
            lines += [f"Address={ULA_PREFIX}::1/64"]
        elif v6.get("mode") == "static" and v6.get("prefix"):
            pnet = ipaddress.IPv6Network(v6["prefix"], strict=False)
            host = ipaddress.IPv6Address(int(pnet.network_address) + 1)  # 网段 ::1
            lines += [f"Address={host}/{pnet.prefixlen}"]
        lines += ["IPv6SendRA=yes", "", "[IPv6SendRA]", "RouterLifetimeSec=600"]
        if v6.get("mode") in ("nat6", "static"):
            # NAT6/静态有确定的 LAN v6 地址，可直告 DNS；native 的地址来自上游 PD，届时由 DHCPv4 DNS 兜底
            lan_v6 = f"{ULA_PREFIX}::1" if v6.get("mode") == "nat6" else \
                str(ipaddress.IPv6Address(int(ipaddress.IPv6Network(v6["prefix"], strict=False).network_address) + 1))
            lines += [f"DNS={lan_v6}"]
        if v6.get("mode") == "native":
            lines += ["", "[IPv6PrefixDelegation]", "DHCPv6PrefixDelegation=yes"]
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
            body = ("# 由 SeedRou 生成 — 请勿手工修改\n"
                    f"[Match]\nName={name}\n\n[Network]\nBridge=br-lan\n")
        elif p.get("wan"):
            continue  # WAN 口由 10-wan.network 管理
        else:
            body = ("# 由 SeedRou 生成 — 请勿手工修改\n"
                    f"[Match]\nName={name}\n\n[Network]\nLinkLocalAddressing=no\nIPv6AcceptRA=no\nDHCP=no\n")
        out.append((f"etc/systemd/network/30-{name}.network", body))
    return out


def gen_dnsmasq(cfg):
    net = lan_net(cfg)
    lan_ip = cfg["lan"]["ip"]
    dh = cfg.get("dhcp", {})
    v6 = cfg.get("ipv6", {})
    wan = cfg.get("wan", {})
    lines = ["# 由 SeedRou 生成 — 请勿手工修改",
             "interface=br-lan",
             "interface=lo",
             "bind-interfaces",
             "dhcp-authoritative",
             "domain=lan",
             "local=/lan/",
             "cache-size=1000",
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
        lines.append("servers-file=/run/seedrou-dns/upstream.conf")
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
        "# SeedRou 基础防火墙 —— 只操作本表 inet seedrou_base，",
        "# 绝不 flush ruleset / 不触碰 sing-box、podman、tailscale 的表（见 COEXISTENCE.md）。",
        "",
        "table inet seedrou_base",
        "flush table inet seedrou_base",
        "",
        "table inet seedrou_base {",
        f"  define WAN_IFS = {{ {wan_set} }}",
        f'  define LAN_BR = "br-lan"',
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
        "# 由 SeedRou 生成 — 请勿手工修改",
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
        "linkname seedrou",
    ]
    if v6.get("enabled") and v6.get("mode") == "native":
        lines.append("+ipv6")
    return "\n".join(lines) + "\n"


def gen_chap(cfg, password):
    user = shell_quote(cfg["wan"]["username"])
    pwd = shell_quote(password or "x")
    return f"# 由 SeedRou 生成 — 请勿手工修改\n{user} * {pwd} *\n"


def main():
    try:
        cfg = load_json(CONFIG_PATH)
    except FileNotFoundError:
        fail("NO_CONFIG", "未找到 /etc/seedrou/config.json", "请先运行 install.sh 完成部署")
    except json.JSONDecodeError as e:
        fail("CONFIG_CORRUPT", f"config.json 解析失败：{e}", "配置文件可能被意外截断；可从 /var/lib/seedrou/lastgood 恢复上一份可用配置")
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
    files = []  # (relpath, mode_octal, content)

    files.append(("etc/systemd/network/10-wan.network", 0o644, gen_wan_network(cfg)))
    files.append(("etc/systemd/network/20-lan.netdev", 0o644, gen_lan_netdev(cfg)))
    files.append(("etc/systemd/network/20-lan.network", 0o644, gen_lan_network(cfg)))
    for rel, body in gen_port_networks(cfg):
        files.append((rel, 0o644, body))
    files.append(("etc/dnsmasq.d/seedrou.conf", 0o644, gen_dnsmasq(cfg)))
    files.append(("etc/nftables.d/seedrou-base.nft", 0o644, gen_nft(cfg)))
    if cfg["wan"]["mode"] == "pppoe":
        files.append(("etc/ppp/peers/seedrou", 0o644, gen_pppd(cfg, password)))
        files.append(("etc/ppp/chap-secrets", 0o600, gen_chap(cfg, password)))
        files.append(("etc/ppp/pap-secrets", 0o600, gen_chap(cfg, password)))

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
