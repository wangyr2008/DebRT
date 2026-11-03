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

print("审计守卫: 撤销表/546/static+auto 拒绝/静态 v6 落盘 全部通过")
