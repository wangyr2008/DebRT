#!/usr/bin/env python3
"""SeedRou 配置校验与规范化。

用法: validate.py SECTION   （JSON 从 stdin 读入，规范化结果输出到 stdout；失败时输出 JSON 错误并 exit 1）
所有面板写入的配置都必须经过这里；错误信息必须可直接指导用户操作。
"""
import ipaddress
import json
import re
import sys

ERR = {"ok": False, "code": "VALIDATE", "message": "", "hint": ""}


def deterministic_id(seed: str) -> str:
    """端口转发规则的稳定 id：跨进程一致（Python 内置 hash 每进程随机，不能用）。"""
    import hashlib
    return "r" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def fail(code, msg, hint=""):
    ERR.update(code=code, message=msg, hint=hint)
    print(json.dumps(ERR, ensure_ascii=False))
    sys.exit(1)


def need(cond, code, msg, hint=""):
    if not cond:
        fail(code, msg, hint)


def as_str(v, name):
    if v is None:
        return ""
    if not isinstance(v, str):
        fail("VALIDATE", f"{name} 必须是文本", "请勿手工构造请求，请从管理页面提交")
    return v.strip()


def is_ipv4(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def is_mask(s):
    try:
        ipaddress.IPv4Network(f"0.0.0.0/{s}", strict=False)
        return isinstance(ipaddress.ip_address(s), ipaddress.IPv4Address) and s.isdigit() is False
    except ValueError:
        return False


def mask_prefix(s):
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{s}", strict=False).prefixlen
    except ValueError:
        return 0


def mask_ok(s):
    """点分十进制连续掩码，如 255.255.255.0"""
    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", s or ""):
        return False
    parts = [int(x) for x in s.split(".")]
    if any(p > 255 for p in parts):
        return False
    bits = "".join(f"{p:08b}" for p in parts)
    seen_zero = False
    for b in bits:
        if b == "0":
            seen_zero = True
        elif seen_zero:
            return False
    return "1" in bits


MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\-. ]{1,32}$", re.UNICODE)
PORT_RE = re.compile(r"^\d{1,5}$")
RANGE_RE = re.compile(r"^(\d{1,5})-(\d{1,5})$")


def ports_overlap(a, b):
    """a/b: '443' 或 '8000-8010'"""
    def rng(x):
        m = RANGE_RE.match(x)
        return (int(m.group(1)), int(m.group(2))) if m else (int(x), int(x))
    ra, rb = rng(a), rng(b)
    return ra[0] <= rb[1] and rb[0] <= ra[1]


def norm_ports(section, data):
    if not isinstance(data, list):
        fail("VALIDATE", "网口数据格式不正确", "请从管理页面重新提交")
    out, seen = [], set()
    wan_n = lan_n = 0
    for p in data:
        if not isinstance(p, dict) or "name" not in p:
            fail("VALIDATE", "网口数据缺少名称")
        name = as_str(p.get("name"), "网口名")
        need(re.fullmatch(r"[a-z0-9]{2,15}", name), "VALIDATE", f"网口名 {name!r} 不合法")
        if name in seen:
            fail("VALIDATE", f"网口 {name} 重复出现")
        seen.add(name)
        wan, lan = bool(p.get("wan")), bool(p.get("lan"))
        need(not (wan and lan), "VALIDATE", f"网口 {name} 不能同时属于 WAN 与 LAN", "同一网口只能属于一个角色")
        wan_n += wan
        lan_n += lan
        out.append({"name": name, "wan": wan, "lan": lan})
    need(wan_n >= 1, "BIND_NO_WAN", "至少需要保留 1 个 WAN 口", "请在「网口绑定」中勾选一个 WAN 口")
    need(lan_n >= 1, "BIND_NO_LAN", "至少需要保留 1 个 LAN 口", "局域网设备需要至少一个 LAN 口")
    need(wan_n == 1, "BIND_MULTI_WAN",
         "当前版本仅支持单 WAN", f"检测到 {wan_n} 个 WAN 口；多宽带负载均衡尚未支持，请仅保留一个 WAN 口，其余口请改勾 LAN 或留空")
    return out


def norm_wan(section, data):
    mode = as_str(data.get("mode"), "上网方式")
    need(mode in ("pppoe", "dhcp", "static"), "VALIDATE", f"上网方式 {mode!r} 不支持", "可选：pppoe / dhcp / static")
    username = as_str(data.get("username"), "宽带账号")
    password = data.get("password")
    password = None if password in (None, "") else str(password)
    if mode == "pppoe":
        need(username, "WAN_NO_USER", "请填写宽带账号", "宽带账号一般由运营商提供，常见为手机号或区号+号码")
        if password is not None:
            need(1 <= len(password) <= 128, "VALIDATE", "宽带密码长度需在 1–128 位之间")
    dns_mode = as_str(data.get("dns_mode"), "DNS 配置") or "auto"
    need(dns_mode in ("auto", "manual"), "VALIDATE", "DNS 配置仅支持 自动/手动")
    dns = []
    if dns_mode == "manual":
        d1 = as_str(data.get("dns1"), "DNS1")
        need(is_ipv4(d1), "WAN_DNS_BAD", f"DNS 服务器 1 {d1!r} 不是有效的 IPv4 地址", "示例：223.5.5.5 或 119.29.29.29")
        dns.append(d1)
        d2 = as_str(data.get("dns2"), "DNS2")
        if d2:
            need(is_ipv4(d2), "WAN_DNS_BAD", f"DNS 服务器 2 {d2!r} 不是有效的 IPv4 地址")
            dns.append(d2)
    ip = mask = gw = ""
    if mode == "static":
        ip = as_str(data.get("ip"), "IP 地址")
        mask = as_str(data.get("mask"), "子网掩码")
        gw = as_str(data.get("gw"), "默认网关")
        need(is_ipv4(ip), "WAN_STATIC_BAD", f"IP 地址 {ip!r} 无效", "请填写运营商分配的 IPv4 地址")
        need(mask_ok(mask), "WAN_STATIC_BAD", f"子网掩码 {mask!r} 无效", "示例：255.255.255.0 或 255.255.255.252")
        need(is_ipv4(gw), "WAN_STATIC_BAD", f"默认网关 {gw!r} 无效", "请填写运营商网关地址")
    return {"mode": mode, "username": username, "dns_mode": dns_mode, "dns": dns,
            "ip": ip, "mask": mask, "gw": gw, "password": password}


def norm_ipv6(section, data):
    enabled = bool(data.get("enabled"))
    mode = as_str(data.get("mode"), "IPv6 方式") or "native"
    need(mode in ("native", "nat6", "static"), "VALIDATE", f"IPv6 方式 {mode!r} 不支持")
    dns_mode = as_str(data.get("dns_mode"), "IPv6 DNS") or "auto"
    need(dns_mode in ("auto", "manual"), "VALIDATE", "IPv6 DNS 仅支持 自动/手动")
    dns = []
    if enabled and dns_mode == "manual":
        d1 = as_str(data.get("dns1"), "IPv6 DNS1")
        need(":" in d1, "V6_DNS_BAD", f"IPv6 DNS {d1!r} 无效", "示例：240c::6666")
        dns.append(d1)
        d2 = as_str(data.get("dns2"), "IPv6 DNS2")
        if d2:
            need(":" in d2, "V6_DNS_BAD", f"IPv6 DNS 2 {d2!r} 无效")
            dns.append(d2)
    addr = as_str(data.get("addr"), "IPv6 地址")
    gw = as_str(data.get("gw"), "IPv6 网关")
    prefix = as_str(data.get("prefix"), "LAN IPv6 前缀")
    if enabled and mode == "static":
        try:
            ipaddress.IPv6Interface(addr)
        except ValueError:
            fail("V6_STATIC_BAD", f"IPv6 地址 {addr!r} 无效", "格式示例：2001:db8:1::2/64")
        need(":" in gw, "V6_STATIC_BAD", f"IPv6 网关 {gw!r} 无效")
        try:
            net = ipaddress.IPv6Network(prefix, strict=False)
            need(net.prefixlen in (48, 52, 56, 60, 64), "V6_STATIC_BAD",
                 "LAN IPv6 前缀长度需为 /48、/52、/56、/60 或 /64",
                 "填写运营商分配的前缀，如 2001:db8:abcd::/64")
        except ValueError:
            fail("V6_STATIC_BAD", f"LAN IPv6 前缀 {prefix!r} 无效", "格式示例：2001:db8:abcd::/64")
    if not enabled:
        mode, dns_mode, dns, addr, gw, prefix = "native", "auto", [], "", "", ""
    return {"enabled": enabled, "mode": mode, "dns_mode": dns_mode, "dns": dns,
            "addr": addr, "gw": gw, "prefix": prefix}


def norm_lan(section, data, cur=None):
    ip = as_str(data.get("ip"), "LAN IP")
    mask = as_str(data.get("mask"), "LAN 子网掩码")
    need(is_ipv4(ip), "LAN_IP_BAD", f"LAN IP {ip!r} 无效", "示例：192.168.10.1")
    need(mask_ok(mask), "LAN_MASK_BAD", f"LAN 子网掩码 {mask!r} 无效", "示例：255.255.255.0")
    # DHCP 池按「IP 末段」配置（UI 语义），仅支持 /24 及更长子网（审计 B-07）
    need(24 <= mask_prefix(mask) <= 30, "LAN_MASK_BAD",
         f"LAN 子网掩码 {mask} 过宽",
         "DHCP 地址池按末段配置，仅支持 /24–/30（255.255.255.0 及更细）；如需更大网段请拆分为多个 /24")
    need(not ip.endswith(".0") and not ip.endswith(".255"), "LAN_IP_BAD", "LAN IP 不能是网段地址或广播地址")
    need(ipaddress.IPv4Address(ip).is_private, "LAN_IP_PUBLIC",
         "LAN IP 不能使用公网地址", f"{ip} 属于公网段；请使用 192.168.x.x / 10.x.x.x / 172.16–31.x.x 等私网地址")
    # 网段冲突：tun / fakeip / 常见代理网段
    net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
    reserved = ["172.19.0.0/30", "198.19.0.0/16", "100.64.0.0/10", "10.88.0.0/16"]
    for r in reserved:
        rnet = ipaddress.IPv4Network(r)
        if net.overlaps(rnet):
            fail("LAN_CONFLICT",
                 f"LAN 网段 {net} 与 {r} 冲突",
                 f"{r} 被 sing-box / Tailscale / Podman 使用；请为 LAN 选择其他网段，例如 192.168.{(int(net.network_address)+1)%256 if False else 11}.1")
    return {"ip": ip, "mask": mask}


def norm_dhcp(section, data, lan=None):
    enabled = bool(data.get("enabled"))
    out = {"enabled": enabled, "pool_start": 100, "pool_end": 200, "lease": 120, "dns1": "", "dns2": "", "gw": ""}
    if not enabled:
        return out
    ps, pe, lease = data.get("pool_start"), data.get("pool_end"), data.get("lease")
    need(isinstance(ps, int) and isinstance(pe, int), "DHCP_POOL_BAD", "地址池末段必须是数字")
    need(2 <= ps <= 254 and 2 <= pe <= 254, "DHCP_POOL_BAD", "地址池末段需在 2–254 之间",
         "0 为网段地址、1 通常为路由器、255 为广播地址")
    need(ps <= pe, "DHCP_POOL_BAD", "地址池起始末段不能大于结束末段")
    need(isinstance(lease, int) and 1 <= lease <= 10080, "DHCP_LEASE_BAD", "租期需在 1–10080 分钟（7 天）之间")
    d1, d2, gw = as_str(data.get("dns1"), "DHCP DNS1"), as_str(data.get("dns2"), "DHCP DNS2"), as_str(data.get("gw"), "DHCP 网关")
    if d1:
        need(is_ipv4(d1), "DHCP_DNS_BAD", f"DHCP DNS1 {d1!r} 无效")
    if d2:
        need(is_ipv4(d2), "DHCP_DNS_BAD", f"DHCP DNS2 {d2!r} 无效")
    if gw:
        need(is_ipv4(gw), "DHCP_GW_BAD", f"DHCP 网关 {gw!r} 无效")
    out.update(pool_start=ps, pool_end=pe, lease=lease, dns1=d1, dns2=d2, gw=gw)
    return out


def norm_static(section, data):
    if not isinstance(data, list):
        fail("VALIDATE", "静态绑定数据格式不正确")
    out, seen_mac, seen_ip = [], set(), set()
    for s in data:
        if not isinstance(s, dict):
            fail("VALIDATE", "静态绑定条目格式不正确")
        name = as_str(s.get("name"), "设备名")
        ip = as_str(s.get("ip"), "IP")
        mac = as_str(s.get("mac"), "MAC").upper()
        need(NAME_RE.match(name), "STATIC_NAME_BAD", f"设备名 {name!r} 不合法", "支持 1–32 位中英文、数字、点、横线、空格")
        need(is_ipv4(ip), "STATIC_IP_BAD", f"IP {ip!r} 无效")
        need(ipaddress.IPv4Address(ip).is_private, "STATIC_IP_BAD",
             f"IP {ip} 不在私网段", "静态分配 IP 必须位于 LAN 子网内")
        need(MAC_RE.match(mac), "STATIC_MAC_BAD", f"MAC {mac!r} 格式不正确", "格式示例：AA:BB:CC:DD:EE:FF")
        need(mac not in seen_mac, "STATIC_DUP", f"MAC {mac} 重复绑定")
        need(ip not in seen_ip, "STATIC_DUP", f"IP {ip} 重复绑定")
        seen_mac.add(mac)
        seen_ip.add(ip)
        out.append({"name": name, "ip": ip, "mac": mac})
    return out


def norm_qos(section, data):
    enabled = bool(data.get("enabled"))
    down, up = data.get("down"), data.get("up")
    out = {"enabled": enabled, "down": down if isinstance(down, int) else 0, "up": up if isinstance(up, int) else 0}
    if not enabled:
        return out
    need(isinstance(down, int) and 1 <= down <= 10000, "QOS_BAD", "下行带宽需为 1–10000 Mbps 的整数",
         "请按宽带实际速率填写；速率未知可先填大致值")
    need(isinstance(up, int) and 1 <= up <= 10000, "QOS_BAD", "上行带宽需为 1–10000 Mbps 的整数")
    return out


def norm_firewall(section, data):
    out = {"wan_input_drop": True, "portfw": []}
    if "wan_input_drop" in data:
        out["wan_input_drop"] = bool(data.get("wan_input_drop"))
    rules = data.get("portfw")
    if rules is None:
        return out
    need(isinstance(rules, list), "VALIDATE", "转发规则格式不正确")
    used = {}  # (proto, wport) -> name
    for r in rules:
        if not isinstance(r, dict):
            fail("VALIDATE", "转发规则条目格式不正确")
        name = as_str(r.get("name"), "规则名")
        proto = as_str(r.get("proto"), "协议") or "tcp"
        wport = as_str(r.get("wport"), "外部端口")
        ip = as_str(r.get("ip"), "目标 IP")
        lport = as_str(r.get("lport"), "内部端口")
        enabled = bool(r.get("enabled", True))
        need(NAME_RE.match(name), "FW_NAME_BAD", f"规则名 {name!r} 不合法")
        need(proto in ("tcp", "udp", "both"), "FW_BAD", f"协议 {proto!r} 不支持")
        need(RANGE_RE.match(wport) or PORT_RE.match(wport), "FW_PORT_BAD",
             f"外部端口 {wport!r} 无效", "可填 1–65535 单端口或 起始-结束 范围，如 8000-8010")
        def one(p):
            return PORT_RE.match(p) and 1 <= int(p) <= 65535
        if PORT_RE.match(wport):
            need(1 <= int(wport) <= 65535, "FW_PORT_BAD", "外部端口需在 1–65535 之间")
        else:
            m = RANGE_RE.match(wport)
            need(m and 1 <= int(m.group(1)) <= int(m.group(2)) <= 65535, "FW_PORT_BAD",
                 f"外部端口范围 {wport!r} 无效", "需满足 1 ≤ 起始 < 结束 ≤ 65535")
        need(is_ipv4(ip), "FW_IP_BAD", f"目标 IP {ip!r} 无效")
        need(one(lport), "FW_PORT_BAD", f"内部端口 {lport!r} 无效", "内部端口需为 1–65535 的单端口")
        protos = ["tcp", "udp"] if proto == "both" else [proto]
        for pp in protos:
            for (ep, pname) in list(used.items()):
                if ep[0] != pp:
                    continue
                if ports_overlap(wport, ep[1]):
                    fail("FW_CONFLICT", f"外部端口 {wport} 与规则「{pname}」的 {ep[1]} 冲突（{pp.upper()}）",
                         "同一协议的外部端口不能重叠；可改用其他端口或合并规则")
            used[(pp, wport)] = name
        out["portfw"].append({"id": as_str(r.get("id"), "id") or deterministic_id(name + "|" + wport + "|" + proto),
                              "name": name, "proto": proto, "wport": wport, "ip": ip,
                              "lport": lport, "enabled": enabled})
    return out


def norm_upgrade(section, data):
    src = as_str(data.get("source"), "升级源")
    if src:
        # 仅允许两种形态（审计 B-14）：owner/repo（GitHub）或 https:// 清单地址。
        # 拒绝 file://、http://、内网地址等 —— 该值会以 root 身份被 curl 访问。
        if "://" in src:
            need(src.startswith("https://"), "UPG_SOURCE_BAD",
                 "升级源 URL 仅支持 https://",
                 "示例：https://example.com/seedrou-manifest.json；或直接填 GitHub 仓库名")
            host = src.split("/", 3)[2] if len(src.split("/", 3)) > 3 else ""
            need(bool(re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{2,5})?", host)), "UPG_SOURCE_BAD",
                 f"升级源主机名 {host!r} 不合法", "请填正常的 https 地址")
            need(not re.fullmatch(r"(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?|localhost).*",
                                  host, re.I), "UPG_SOURCE_BAD",
                 "升级源不能指向内网地址", "请使用公网 https 地址")
        else:
            need(re.fullmatch(r"[\w.\-]+/[\w.\-]{1,100}", src), "UPG_SOURCE_BAD",
                 f"升级源 {src!r} 格式不正确", "填 GitHub 仓库（如 wangyr2008/SeedRou）或完整 https 清单 URL")
    return {"source": src}


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "code": "USAGE", "message": "缺少 SECTION 参数"}), file=sys.stderr)
        sys.exit(2)
    section = sys.argv[1]
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        fail("VALIDATE", f"请求数据不是有效的 JSON：{e}")
    if not isinstance(data, dict):
        fail("VALIDATE", "请求数据必须是对象")
    cur = None
    try:
        with open("/etc/seedrou/config.json", "r", encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        cur = None
    fns = {
        "bind": lambda: norm_ports(section, data.get("ports", [])),
        "wan": lambda: norm_wan(section, data),
        "ipv6": lambda: norm_ipv6(section, data),
        "lan": lambda: norm_lan(section, data, cur),
        "dhcp": lambda: norm_dhcp(section, data, cur.get("lan") if cur else None),
        "static": lambda: norm_static(section, data.get("list", [])),
        "qos": lambda: norm_qos(section, data),
        "firewall": lambda: norm_firewall(section, data),
        "upgrade": lambda: norm_upgrade(section, data),
    }
    fn = fns.get(section)
    if fn is None:
        fail("VALIDATE", f"未知的配置段 {section!r}")
    print(json.dumps({"ok": True, "section": section, "data": fn()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
