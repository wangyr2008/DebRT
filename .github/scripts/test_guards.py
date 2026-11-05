#!/usr/bin/env python3
"""审计守卫（2026-11 全量审计四.4 建议，防 P1 类"改一半"回归）：
1) 会话/撤销表一致性：_REVOKE_TTL 必须 ≥ SESSION_TTL（P1-1：曾 12h vs 7d，登出形同虚设）
2) netgen 生成配置引用完整性：三种 WAN 模式 × DNS 模式（P1-2/P1-3 类回归）
3) IPv6 矩阵：WAN 模式 × IPv6 模式的可用/拒绝组合（A-01/A-02/N-02 类回归）
4) PPPoE IPv6：光猫桥接 + 拨号场景（IA_NA/IA_PD 经 networkd DHCPv6 客户端获取）
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


def expect_fail(section, result, cur, code):
    try:
        validate.cross_validate(section, result, cur)
        raise AssertionError(f"{section} 未被拒绝（期望 {code}）")
    except SystemExit:
        assert validate.ERR.get("code") == code, f"拒绝码不符: {validate.ERR.get('code')} != {code}"


# ---- IPv6 配置字典 ----
V6OFF = {"enabled": False, "mode": "native", "dns_mode": "auto", "dns": []}
V6N = {"enabled": True, "mode": "native", "dns_mode": "auto", "dns": []}
V6NAT = {"enabled": True, "mode": "nat6", "dns_mode": "auto", "dns": []}
V6S = {"enabled": True, "mode": "static", "addr": "2001:db8:1::2/64", "gw": "2001:db8:1::1",
       "prefix": "2001:db8:abcd::/64", "dns_mode": "auto", "dns": []}

# ---- 1) 手动 DNS：三种模式都出 server=；wan_in 一律含 DHCPv6 放行 ----
for mode in ("dhcp", "pppoe", "static"):
    cfg = base_cfg(mode, "manual")
    dns = netgen.gen_dnsmasq(cfg)
    assert "server=223.5.5.5" in dns, f"{mode}+manual 缺 server="
    assert "servers-file" not in dns, f"{mode}+manual 不应引用 servers-file"
    assert "udp dport 546 accept" in netgen.gen_nft(cfg), f"{mode} wan_in 缺 DHCPv6 546 放行（P1-2 回归）"

# ---- 2) 自动 DNS：dhcp/pppoe 引用 servers-file；static+auto 被拒绝；static 存量兜底 ----
for mode in ("dhcp", "pppoe"):
    assert "servers-file=/run/seedrou-dns/upstream.conf" in netgen.gen_dnsmasq(base_cfg(mode, "auto")), \
        f"{mode}+auto 缺 servers-file"

try:
    validate.norm_wan("wan", {"mode": "static", "ip": "10.10.10.2", "mask": "255.255.255.252",
                              "gw": "10.10.10.1", "dns_mode": "auto"})
    raise AssertionError("static+auto 未被 validate 拒绝（P1-3 回归）")
except SystemExit:
    assert validate.ERR.get("code") == "WAN_DNS_STATIC_AUTO", \
        f"static+auto 拒绝码不符: {validate.ERR.get('code')}"

assert "server=223.5.5.5" in netgen.gen_dnsmasq(base_cfg("static", "auto")) \
    and "servers-file" not in netgen.gen_dnsmasq(base_cfg("static", "auto")), \
    "static+auto 兜底缺失（N-03 回归）"

# ---- 3) IPv6 静态 addr/gw 落盘（P2-3/N-02）----
wan_net = netgen.gen_wan_network(base_cfg("static", "manual", v6=V6S))
assert "Address=2001:db8:1::2/64" in wan_net, "IPv6 静态 addr 未落盘（P2-3 回归）"
assert "Gateway=2001:db8:1::1" in wan_net, "IPv6 静态 gw 未落盘（P2-3 回归）"
wan_net_dhcp = netgen.gen_wan_network(base_cfg("dhcp", "manual", v6=V6S))
assert "Address=2001:db8:1::2/64" in wan_net_dhcp, "dhcp+静态v6 addr 未落盘（N-02 回归）"
wan_net_pppoe = netgen.gen_wan_network(base_cfg("pppoe", "manual", v6=V6S))
assert "2001:db8:1::2" not in wan_net_pppoe, "pppoe+静态v6 不应落盘到以太网口（N-02）"

# ---- 4) DHCP+native：PD 新键（A-08）+ LAN 消费（N-02）+ RA DNS（EmitDNS）+ B-14 ----
wan_nat = netgen.gen_wan_network(base_cfg("dhcp", "manual", v6=V6N))
assert "DHCPPrefixDelegation=yes" in wan_nat, "A-08 回归：WAN 侧 PD 新键缺失（[Network] DHCPPrefixDelegation）"
assert "UseDNS=yes" in wan_nat and "UseDNS=no" not in wan_nat, "B-14 回归：DHCPv6 DNS 不应放弃"
lan_nat = netgen.gen_lan_network(base_cfg("dhcp", "manual", v6=V6N))
assert "[IPv6PrefixDelegation]" not in lan_nat, "A-08 回归：废弃段仍在"
assert "DHCPPrefixDelegation=yes" in lan_nat, "N-02 回归：LAN 侧未消费 PD"
assert "EmitDNS=yes" in lan_nat and "DNS=_link_local" in lan_nat, "A-08 回归：RA DNS 宣告缺失"

# ---- 5) B-01：WAN 自动 + v6 手动 DNS 落盘 ----
v6m = {"enabled": True, "mode": "nat6", "dns_mode": "manual", "dns": ["240c::6666"]}
assert "server=240c::6666" in netgen.gen_dnsmasq(base_cfg("dhcp", "auto", v6=v6m)), \
    "B-01 回归：v6 手动 DNS 在 WAN 自动模式下被丢弃"

# ---- 6) 跨段组合拒绝矩阵 ----
# 静态 WAN：native/nat6 均无出口（IPv6AcceptRA=no），仅静态 v6 可用
expect_fail("ipv6", V6N, {"wan": {"mode": "static"}}, "V6_NATIVE_STATIC_WAN")
expect_fail("ipv6", V6NAT, {"wan": {"mode": "static"}}, "V6_NAT6_STATIC_UNSUPPORTED")
expect_fail("wan", {"mode": "static"}, {"wan": {}, "ipv6": V6N}, "V6_NATIVE_STATIC_WAN")
expect_fail("wan", {"mode": "static"}, {"wan": {}, "ipv6": V6NAT}, "V6_NAT6_WAN_UNSUPPORTED")
# PPPoE：native/nat6 已放开（networkd DHCPv6 on ppp0）；仅静态表单不可用
expect_fail("ipv6", V6S, {"wan": {"mode": "pppoe"}}, "V6_STATIC_PPPPOE_UNSUPPORTED")
expect_fail("wan", {"mode": "pppoe"}, {"wan": {}, "ipv6": V6S}, "V6_STATIC_PPPPOE_UNSUPPORTED")

# ---- 7) PPPoE IPv6（真机场景：光猫桥接 + 拨号，运营商经 PPP 下发 IA_NA/IA_PD）----
ppp_cfg = base_cfg("pppoe", "manual", v6=V6N)
ppp_net = netgen.gen_ppp_network(ppp_cfg)
assert "Name=ppp0" in ppp_net, "ppp0 匹配缺失"
assert "DHCPPrefixDelegation=yes" in ppp_net and "WithoutRA=solicit" in ppp_net, "PPPoE ppp0 PD 缺失"
assert "DHCP=ipv6" in ppp_net, "PPPoE ppp0 缺 DHCP=ipv6（DHCPv6 客户端不会启动，真机 546/547 为零）"
# R-02：ppp0 必须被 DNS 钩子认作 WAN，且与 ip-up.d 写入的 IPv4 上游合并而非覆盖
_hook = open(os.path.join(ROOT, "network", "dispatcher", "70-seedrou-dns"), encoding="utf-8").read()
assert 'wans.append("ppp0")' in _hook, "R-02 回归：DNS 钩子不认 ppp0（PPPoE 的 IPv6 上游进不来）"
assert "ppp*)" in _hook, "R-02 回归：ppp0 缺少 v4/v6 合并分支"
# R-01：升级同步块必须覆盖所有 /etc 钩子（曾漏 v0.1.43 新增的热插拔钩子，
# 导致"插网线自愈"只对全新安装生效）
_priv_src = open(os.path.join(ROOT, "priv", "seedrou-priv"), encoding="utf-8").read()
assert "75-seedrou-pppoe-hotplug" in _priv_src, "R-01 回归：升级同步漏了 PPPoE 热插拔钩子"
assert "+ipv6" in netgen.gen_pppd(ppp_cfg, "pw"), "pppd +ipv6 缺失"
assert "UplinkInterface=:self" in ppp_net, "N-05 回归：缺 UplinkInterface=:self（客户端永不启动的真凶）"
lan_ppp = netgen.gen_lan_network(ppp_cfg)
assert "DHCPPrefixDelegation=yes" in lan_ppp, "PPPoE native LAN 侧未消费 PD"
assert "+ipv6" in netgen.gen_pppd(base_cfg("pppoe", "manual", v6=V6NAT), "pw"), "pppd nat6 下 +ipv6 缺失"
# netgen 内嵌 cross_validate（收敛期防线）不得再拦 pppoe×native/nat6——v0.1.41 曾因
# 它漏改导致 validate 放行后 netgen 仍拒绝（同类规则两处存放的教训）
netgen.cross_validate(base_cfg("pppoe", "manual", v6=V6N))
netgen.cross_validate(base_cfg("pppoe", "manual", v6=V6NAT))

print("审计守卫: 撤销表/546/PD新键/nat6矩阵/跨段组合/静态v6落盘/PPPoE-IPv6 全部通过")
