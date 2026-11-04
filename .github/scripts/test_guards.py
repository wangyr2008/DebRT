#!/usr/bin/env python3
"""审计守卫（2026-11 全量审计四.4 建议，防 P1 类"改一半"回归）：
1) 会话/撤销表一致性：_REVOKE_TTL 必须 ≥ SESSION_TTL（P1-1：曾 12h vs 7d，登出形同虚设）
2) netgen 生成配置引用完整性：三种 WAN 模式 × DNS 模式（P1-2/P1-3 类回归）
   - wan_in 一律放行 DHCPv6(udp 546)
   - static+auto 必须在 validate 层拒绝（servers-file 无生产者，dnsmasq 启动期 fatal）
   - IPv6 静态 addr/gw 必须落盘到 10-wan.network（P2-3：曾被收集校验但从不使用）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "priv"))
sys.path.insert(0, os.path.join(ROOT, "panel"))

import auth  # noqa: E402  导入即执行模块内撤销表守卫
assert auth._REVOKE_TTL >= auth.SESSION_TTL, "_REVOKE_TTL < SESSION_TTL（P1-1 回归）"

import netgen  # noqa: E402
import validate  # noqa: E402


def base_cfg(mode, dns_mode, v6=None):
    return {
        "ports": [{"name": "eth0", "wan": True, "lan": False},
                  {"name": "eth1", "wan": False, "lan": True}],
        "wan": {"mode": mode, "dns_mode": dns_mode,
                "dns": ["223.5.5.5"] if dns_mode == "manual" else [],
                "username": "user", "password": "pw",
                "ip": "10.10.10.2", "mask": "255.255.255.252", "gw": "10.10.10.1"},
        "lan": {"ip": "192.168.10.1", "mask": "255.255.255.0",
                "dhcp": {"enabled": True, "pool_start": 100, "pool_end": 200, "lease": 120}},
        "static": [],
        "ipv6": v6 or {"enabled": False, "mode": "native", "dns_mode": "auto", "dns": []},
        "firewall": {"wan_input_drop": True, "portfw": []},
    }


# 1) 手动 DNS：三种模式都出 server=；wan_in 一律含 DHCPv6 放行
for mode in ("dhcp", "pppoe", "static"):
    cfg = base_cfg(mode, "manual")
    dns = netgen.gen_dnsmasq(cfg)
    assert "server=223.5.5.5" in dns, f"{mode}+manual 缺 server="
    assert "servers-file" not in dns, f"{mode}+manual 不应引用 servers-file"
    assert "udp dport 546 accept" in netgen.gen_nft(cfg), f"{mode} wan_in 缺 DHCPv6 546 放行（P1-2 回归）"

# 2) 自动 DNS：dhcp/pppoe 引用 servers-file（有生产者）；static 被 validate 拒绝
for mode in ("dhcp", "pppoe"):
    cfg = base_cfg(mode, "auto")
    assert "servers-file=/run/seedrou-dns/upstream.conf" in netgen.gen_dnsmasq(cfg), f"{mode}+auto 缺 servers-file"

try:
    validate.norm_wan("wan", {"mode": "static", "ip": "10.10.10.2", "mask": "255.255.255.252",
                              "gw": "10.10.10.1", "dns_mode": "auto"})
    raise AssertionError("static+auto 未被 validate 拒绝（P1-3 回归）")
except SystemExit:
    assert validate.ERR.get("code") == "WAN_DNS_STATIC_AUTO", \
        f"static+auto 拒绝码不符: {validate.ERR.get('code')}"

# 3) IPv6 静态 addr/gw 落盘（P2-3）
v6 = {"enabled": True, "mode": "static", "dns_mode": "auto", "dns": [],
      "addr": "2001:db8:1::2/64", "gw": "2001:db8:1::1", "prefix": "2001:db8:abcd::/64"}
wan_net = netgen.gen_wan_network(base_cfg("static", "manual", v6=v6))
assert "Address=2001:db8:1::2/64" in wan_net, "IPv6 静态 addr 未落盘（P2-3 回归）"
assert "Gateway=2001:db8:1::1" in wan_net, "IPv6 静态 gw 未落盘（P2-3 回归）"

# 4) N-02：dhcp + 静态 IPv6 也必须落盘；pppoe 组合不得落盘（由 validate 拒绝）
wan_net_dhcp = netgen.gen_wan_network(base_cfg("dhcp", "manual", v6=v6))
assert "Address=2001:db8:1::2/64" in wan_net_dhcp, "dhcp+静态v6 addr 未落盘（N-02 回归）"
assert "Gateway=2001:db8:1::1" in wan_net_dhcp, "dhcp+静态v6 gw 未落盘（N-02 回归）"
wan_net_pppoe = netgen.gen_wan_network(base_cfg("pppoe", "manual", v6=v6))
assert "2001:db8:1::2" not in wan_net_pppoe, "pppoe+静态v6 不应落盘到以太网口（N-02）"

# 5) N-03：存量 static+auto 生成公共 DNS 兜底而非空上游
dns_sa = netgen.gen_dnsmasq(base_cfg("static", "auto"))
assert "server=223.5.5.5" in dns_sa and "servers-file" not in dns_sa, "static+auto 兜底缺失（N-03 回归）"

# 7) nat6 矩阵断言见文件末尾（expect_fail 定义之后）

# 8) A-08/B-14/B-01 生成断言（V6N8 本段自定义：定义顺序在 6) 段之前）
V6N8 = {"enabled": True, "mode": "native", "dns_mode": "auto", "dns": []}
wan_nat = netgen.gen_wan_network(base_cfg("dhcp", "manual", v6=V6N8))
assert "DHCPPrefixDelegation=yes" in wan_nat, "A-08 回归：WAN 侧 PD 新键缺失（[Network] DHCPPrefixDelegation）"
assert "UseDNS=yes" in wan_nat and "UseDNS=no" not in wan_nat, "B-14 回归：DHCPv6 DNS 不应放弃"
lan_nat = netgen.gen_lan_network(base_cfg("dhcp", "manual", v6=V6N8))
assert "[IPv6PrefixDelegation]" not in lan_nat, "A-08 回归：废弃段仍在"
assert "EmitDNS=yes" in lan_nat and "DNS=_link_local" in lan_nat, "A-08 回归：RA DNS 宣告缺失"
v6m = {"enabled": True, "mode": "nat6", "dns_mode": "manual", "dns": ["240c::6666"]}
assert "server=240c::6666" in netgen.gen_dnsmasq(base_cfg("dhcp", "auto", v6=v6m)), "B-01 回归：v6 手动 DNS 在 WAN 自动模式下被丢弃"

# 6) 跨段组合约束（P3-2 + 复审 v0.1.30 观察①）：pppoe×static/native、static×native 双向
V6S = {"enabled": True, "mode": "static", "addr": "2001:db8:1::2/64", "gw": "2001:db8:1::1",
       "prefix": "2001:db8:abcd::/64", "dns_mode": "auto", "dns": []}
V6N = {"enabled": True, "mode": "native", "dns_mode": "auto", "dns": []}

def expect_fail(section, result, cur, code):
    try:
        validate.cross_validate(section, result, cur)
        raise AssertionError(f"{section} 未被拒绝（期望 {code}）")
    except SystemExit:
        assert validate.ERR.get("code") == code, f"拒绝码不符: {validate.ERR.get('code')} != {code}"

expect_fail("ipv6", V6S, {"wan": {"mode": "pppoe"}}, "V6_STATIC_PPPPOE_UNSUPPORTED")
expect_fail("ipv6", V6N, {"wan": {"mode": "pppoe"}}, "V6_NATIVE_PPPPOE_UNSUPPORTED")
expect_fail("ipv6", V6N, {"wan": {"mode": "static"}}, "V6_NATIVE_STATIC_WAN")
expect_fail("wan", {"mode": "pppoe"}, {"wan": {}, "ipv6": V6S}, "V6_STATIC_PPPPOE_UNSUPPORTED")
expect_fail("wan", {"mode": "pppoe"}, {"wan": {}, "ipv6": V6N}, "V6_NATIVE_PPPPOE_UNSUPPORTED")
expect_fail("wan", {"mode": "static"}, {"wan": {}, "ipv6": V6N}, "V6_NATIVE_STATIC_WAN")

# 9) A-01/A-02：nat6 × pppoe/static 双向拒绝（假成功空转的矩阵补全）
V6NAT = {"enabled": True, "mode": "nat6", "dns_mode": "auto", "dns": []}
expect_fail("ipv6", V6NAT, {"wan": {"mode": "pppoe"}}, "V6_NAT6_PPPPOE_UNSUPPORTED")
expect_fail("ipv6", V6NAT, {"wan": {"mode": "static"}}, "V6_NAT6_STATIC_UNSUPPORTED")
expect_fail("wan", {"mode": "pppoe"}, {"wan": {}, "ipv6": V6NAT}, "V6_NAT6_WAN_UNSUPPORTED")
expect_fail("wan", {"mode": "static"}, {"wan": {}, "ipv6": V6NAT}, "V6_NAT6_WAN_UNSUPPORTED")

print("审计守卫: 撤销表/546/PD新键/nat6矩阵/跨段组合/静态v6落盘 全部通过")
