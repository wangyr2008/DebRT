#!/usr/bin/env python3
"""审计守卫（2026-11 全量审计四.4 建议，防 P1 类"改一半"回归）：
1) 会话/撤销表一致性：_REVOKE_TTL 必须 ≥ SESSION_TTL（P1-1：曾 12h vs 7d，登出形同虚设）
2) netgen 生成配置引用完整性：三种 WAN 模式 × DNS 模式（P1-2/P1-3 类回归）
3) IPv6 矩阵：WAN 模式 × IPv6 模式的可用/拒绝组合（A-01/A-02/N-02 类回归）
4) PPPoE IPv6：光猫桥接 + 拨号场景（IA_NA/IA_PD 经 networkd DHCPv6 客户端获取）
"""
import os
import re
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
    assert "use-stale-cache" in dns and "cache-size=5000" in dns, "乐观缓存/缓存容量回归"
    assert "servers-file" not in dns, f"{mode}+manual 不应引用 servers-file"
    assert "udp dport 546 accept" in netgen.gen_nft(cfg), f"{mode} wan_in 缺 DHCPv6 546 放行（P1-2 回归）"

# ---- 2) 自动 DNS：dhcp/pppoe 引用 servers-file；static+auto 被拒绝；static 存量兜底 ----
for mode in ("dhcp", "pppoe"):
    assert "servers-file=/run/dert-dns/upstream.conf" in netgen.gen_dnsmasq(base_cfg(mode, "auto")), \
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
# 默认路由必须存在（运营商不发 RA、DHCPv6 也不带默认路由，缺了就只有地址没有出口）。
# 但**不能**靠 Scope=link 来实现：IPv6 路由没有 scope 概念，networkd 每次开机
# 都报 "Scope= is specified for IPv6 route. It will be ignored."（真机 2026-11-12 实测），
# 实际建起默认路由的是 Destination=::/0。
assert "Destination=::/0" in ppp_net, \
    "PPPoE ppp0 缺 ::/0 默认路由（运营商不发 RA，只有地址没有出口）"
assert "Scope=link" not in ppp_net, \
    "P3-22 回归：ppp0 的 IPv6 路由又写了 Scope=link（IPv6 无 scope 概念，networkd 会忽略并刷警告）"

# P3-22：ppp0 必须 KeepConfiguration=yes。
# networkd 重新配置 link 时默认会删掉"不是自己配的"地址，而 ppp0 的 IPv4 是 pppd
# 用 ioctl 加的（外来配置）。真机 2026-11-12 实测：`networkctl reconfigure ppp0`
# 之后 ppp0 只剩 DHCPv6 地址、IPv4 与默认路由全消失，v4 出网 000，必须重拨恢复。
# 触发场景不止手工命令——apply-all 在网络设置变更时会重启 networkd，同样踩到。
assert "KeepConfiguration=yes" in ppp_net, \
    "P3-22 回归：ppp0 缺 KeepConfiguration=yes（networkd 重配/重启会清掉 pppd 的 IPv4，WAN 断）"

# P3-22：本机 resolv.conf 必须指回 dnsmasq。
# pppd 的 usepeerdns 会让 /etc/ppp/ip-up.d/0000usepeerdns 把 resolv.conf 改写成
# 运营商 DNS，路由器自身于是绕过 dnsmasq —— 局域网主机名解析不了（真机 2026-11-12
# 实测：dig <host>.lan 返回空，dig @127.0.0.1 <host>.lan 正常）、缓存也吃不到。
for _h in ("ip-up.d", "ip-down.d"):
    _hs = open(os.path.join(ROOT, "network", "ppp", _h, "dert"), encoding="utf-8").read()
    assert "127.0.0.1" in _hs and "/etc/resolv.conf" in _hs, \
        f"P3-22 回归：{_h}/dert 没把 resolv.conf 指回 dnsmasq（本机将绕过 dnsmasq，.lan 解析失败）"
    # 必须原子写：tmp+mv，避免 dnsmasq/glibc 读到半个文件
    assert "resolv.conf.dert." in _hs and "mv -f" in _hs, \
        f"P3-22 回归：{_h}/dert 写 resolv.conf 不是原子写（可能被读到半个文件）"

# P3-22：面板不能依赖 network-online.target。
# network-online 要等 systemd-networkd-wait-online 判定所有受管接口就绪，
# br-lan 在网线没插时永远不 routable → 面板被拖到超时（默认 90s+）才起来。
# 面板只服务局域网，WAN 通不通与它无关（sing-box drop-in 的 wait-wan 同理）。
_panel_unit = open(os.path.join(ROOT, "systemd", "dert-panel.service"), encoding="utf-8").read()
assert "network-online" not in _panel_unit, \
    "P3-22 回归：面板单元又依赖 network-online.target（无载波时启动被拖到超时）"
assert "After=network.target" in _panel_unit, \
    "P3-22 回归：面板单元丢了 After=network.target"
# R-02：ppp0 必须被 DNS 钩子认作 WAN，且与 ip-up.d 写入的 IPv4 上游合并而非覆盖
_hook = open(os.path.join(ROOT, "network", "dispatcher", "70-dert-dns"), encoding="utf-8").read()
assert 'wans.append("ppp0")' in _hook, "R-02 回归：DNS 钩子不认 ppp0（PPPoE 的 IPv6 上游进不来）"
assert "ppp*)" in _hook, "R-02 回归：ppp0 缺少 v4/v6 合并分支"
# R-01：升级同步块必须覆盖所有 /etc 钩子（曾漏 v0.1.43 新增的热插拔钩子，
# 导致"插网线自愈"只对全新安装生效）
_priv_src = open(os.path.join(ROOT, "priv", "dert-priv"), encoding="utf-8").read()
assert "75-dert-pppoe-hotplug" in _priv_src, "R-01 回归：升级同步漏了 PPPoE 热插拔钩子"
# PPPoE 看门狗：pppd 对"接口被外部删除"不敏感，服务 active 但 ppp0 消失时必须能自愈
_wd = open(os.path.join(ROOT, "priv", "pppoe-watchdog"), encoding="utf-8").read()
assert "ppp0" in _wd and "systemctl restart" in _wd, "看门狗缺少 ppp0 探测或重启逻辑"
assert os.path.exists(os.path.join(ROOT, "systemd", "dert-pppoe-watchdog.timer")), "看门狗 timer 缺失"
assert "dert-pppoe-watchdog.timer" in _priv_src, "升级同步未覆盖看门狗 timer"
# 看门狗健康判据必须同时认 IPv4 与全局 IPv6（只认 v4 会误杀 IPv6-only 链路，
# 每 3 分钟重启一次拨号，比卡死更糟）
assert "scope global" in _wd, "B-01 回归：看门狗只认 IPv4"
# C-6：看门狗必须有载波探测——无线缆时 pppd persist 本就在重试，
# 此刻重启只是无意义搅动（每 ~4min 一次）
assert "carrier" in _wd, "C-6 回归：看门狗缺载波探测（无线缆时空转重启拨号）"
# A-1：热插拔钩子必须有 WAN 模式守卫——DHCP/静态模式下载波事件绝不能拉起
# 拨号服务（pppd 会用残留 peers 真实拨号，双默认路由劫持 DHCP 模式）。
# watchdog 与 hotplug 是同一逻辑两处存放，守卫必须两侧断言（"改一半"第八次的教训）
_hot = open(os.path.join(ROOT, "network", "dispatcher", "75-dert-pppoe-hotplug"), encoding="utf-8").read()
assert '[ "$MODE" = "pppoe" ] || exit 0' in _hot, "A-1 回归：热插拔缺 WAN 模式守卫"
assert '[ "$MODE" != "pppoe" ]' in _wd, "A-1 回归：看门狗缺 WAN 模式守卫（同一逻辑两处）"
# B-2：DHCP 关闭态必须保留表单数值——此前 disabled 分支返回出厂默认，
# 关一次开关地址池/租期/DNS 就被静默清空（norm_qos 同分支保留，非本意设计）
_d = validate.norm_dhcp("dhcp", {"enabled": False, "pool_start": 55, "pool_end": 150,
                                 "lease": 60, "dns1": "223.5.5.5", "dns2": "", "gw": ""})
assert (_d["pool_start"], _d["pool_end"], _d["lease"], _d["dns1"]) == (55, 150, 60, "223.5.5.5"), \
    "B-2 回归：关闭 DHCP 重置了地址池/租期/DNS"
# enabled 侧池校验不得被连带放松
try:
    validate.norm_dhcp("dhcp", {"enabled": True, "pool_start": 1, "pool_end": 200, "lease": 120})
    raise AssertionError("B-2 回归：enabled 侧池边界校验失效")
except SystemExit:
    assert validate.ERR.get("code") == "DHCP_POOL_BAD", validate.ERR.get("code")
# B-4：撤销表必须落盘——内存态撤销表在面板重启后清零，
# 已登出 cookie 可重放到 7 天过期（登出承诺失效）
assert getattr(auth, "_REVOKE_PATH", "") and hasattr(auth, "_persist_revoked"), \
    "B-4 回归：撤销表落盘缺失（重启后登出承诺失效）"
assert "revoked" in (auth._REVOKE_PATH or ""), "B-4 回归：撤销表路径异常"
# B-02：netgen 的收敛期防线必须与 validate 覆盖同一套 IPv6 × WAN 规则
# （v0.1.42 曾因只改 validate 漏掉 netgen，导致保存放行、收敛回滚）
_V6_STATIC_FULL = {"enabled": True, "mode": "static", "dns_mode": "auto", "dns": [],
                   "prefix": "2001:db8:abcd::/64", "addr": "2001:db8:1::2/64", "gw": "2001:db8:1::1"}
for _wm, _vm, _dm in (("static", "native", "manual"), ("static", "nat6", "manual"), ("pppoe", "static", "auto")):
    _v6 = dict(_V6_STATIC_FULL) if _vm == "static" else {"enabled": True, "mode": _vm, "dns_mode": "auto", "dns": []}
    try:
        netgen.cross_validate(base_cfg(_wm, _dm, v6=_v6))
    except SystemExit:
        pass
    else:
        raise AssertionError("B-02 回归：netgen 未拦截 %s × %s（收敛期会生成空转配置）" % (_wm, _vm))
assert "+ipv6" in netgen.gen_pppd(ppp_cfg, "pw"), "pppd +ipv6 缺失"
assert "UplinkInterface=:self" in ppp_net, "N-05 回归：缺 UplinkInterface=:self（客户端永不启动的真凶）"
lan_ppp = netgen.gen_lan_network(ppp_cfg)
assert "DHCPPrefixDelegation=yes" in lan_ppp, "PPPoE native LAN 侧未消费 PD"
assert "+ipv6" in netgen.gen_pppd(base_cfg("pppoe", "manual", v6=V6NAT), "pw"), "pppd nat6 下 +ipv6 缺失"
# netgen 内嵌 cross_validate（收敛期防线）不得再拦 pppoe×native/nat6——v0.1.41 曾因
# 它漏改导致 validate 放行后 netgen 仍拒绝（同类规则两处存放的教训）
netgen.cross_validate(base_cfg("pppoe", "manual", v6=V6N))
netgen.cross_validate(base_cfg("pppoe", "manual", v6=V6NAT))

# ==================== v0.1.51 外部审计修复守卫（P2-01/P3-01~11/观察②③） ====================
# P2-01：QoS 清扫必须遍历「全部配置网口 + ppp0」——只清当前 WAN_DEV 会在换口后
# 把旧口的 cake/ingress 留在原地（双重整形 / 关闭后仍限速，真机 2026-11-06 实录）
_qa = open(os.path.join(ROOT, "priv", "qdisc-apply"), encoding="utf-8").read()
assert "for d in $CLEAN_DEVS" in _qa and '"ppp0","ifb0"' in _qa, \
    "P2-01 回归：qdisc 清扫未按全部网口+ppp0 遍历"
# P3-01：升级内联收敛必须带锁标记——子进程 acquire_lock 重开 fd9 必 BUSY，
# 无标记时该调用 100% 失败（死代码），收敛全靠 3s 后的 converge.service 兜底
assert "_DERT_LOCK_HELD=1 /usr/local/lib/dert/dert-priv apply-all --convergent" in _priv_src, \
    "P3-01 回归：升级内联收敛缺锁标记（子进程必 BUSY）"
# P3-02：升级通道必须同步 network/systemd/dert-pppoe.service（A-04/R-01 同族第三处）
assert "network/systemd/dert-pppoe.service" in _priv_src, \
    "P3-02 回归：升级通道漏同步 PPPoE 单元"
# P3-06：三处 WAN 就绪判据必须同时认 IPv4 与全局 IPv6——同一逻辑三处存放，
# 只改看门狗漏掉 wait-wan 会让 sing-box 在 IPv6-only 链路上永远起不来
for _f in ("priv/wait-wan", "priv/pppoe-watchdog", "priv/wait-dns-upstream"):
    assert "scope global" in open(os.path.join(ROOT, _f), encoding="utf-8").read(), \
        f"P3-06 回归：{_f} 的 WAN 就绪判据只认 IPv4"
# P3-03：静态 WAN 网关必须同网段（跨网段网关 networkd 拒配，回滚提示误导）
try:
    validate.norm_wan("wan", {"mode": "static", "ip": "10.10.10.2", "mask": "255.255.255.0",
                              "gw": "192.168.1.1", "dns_mode": "manual", "dns1": "223.5.5.5"})
    raise AssertionError("P3-03 回归：跨网段静态网关被放行")
except SystemExit:
    assert validate.ERR.get("code") == "WAN_STATIC_BAD", validate.ERR.get("code")
_bad_gw = base_cfg("static", "manual")
_bad_gw["wan"]["gw"] = "192.168.1.1"
try:
    netgen.cross_validate(_bad_gw)
    raise AssertionError("P3-03 回归：netgen 未拦截跨网段静态网关")
except SystemExit:
    assert netgen.ERR.get("code") == "WAN_STATIC_BAD", netgen.ERR.get("code")
# P3-04：静态 IPv6 地址必须带前缀长度（无 /nn 落成 /128，网关不可达）
try:
    validate.norm_ipv6("ipv6", {"enabled": True, "mode": "static", "addr": "2001:db8:1::2",
                                "gw": "2001:db8:1::1", "prefix": "2001:db8:abcd::/64"})
    raise AssertionError("P3-04 回归：无前缀长度的静态 v6 地址被放行")
except SystemExit:
    assert validate.ERR.get("code") == "V6_STATIC_BAD", validate.ERR.get("code")
_no_pfx = base_cfg("static", "manual", v6=dict(V6S, addr="2001:db8:1::2"))
try:
    netgen.cross_validate(_no_pfx)
    raise AssertionError("P3-04 回归：netgen 未拦截无前缀长度的静态 v6 地址")
except SystemExit:
    assert netgen.ERR.get("code") == "V6_STATIC_BAD", netgen.ERR.get("code")
# P3-05：LAN 网段/广播地址按掩码判定（ipaddress 计算，不再只看 .0/.255 字符串）
try:
    validate.norm_lan("lan", {"ip": "192.168.10.0", "mask": "255.255.255.0"})
    raise AssertionError("P3-05 回归：/24 网段地址被放行")
except SystemExit:
    assert validate.ERR.get("code") == "LAN_IP_BAD", validate.ERR.get("code")
# P3-05（更新版）：掩码全站对齐 /24——A-07 只收窄了 UI，validate/README/存量回显
# 三处必须同步（保存层拒非 /24）
try:
    validate.norm_lan("lan", {"ip": "192.168.10.1", "mask": "255.255.255.192"})
    raise AssertionError("P3-05 回归：/26 掩码在保存层被放行")
except SystemExit:
    assert validate.ERR.get("code") == "LAN_MASK_BAD", validate.ERR.get("code")
# netgen 收敛侧同规则：非 /24 → LAN_MASK_TOO_WIDE（do_apply_all 既有迁移自动改为 /24，
# /25–/30 存量的池越界问题随收敛自愈）
_bad_mask = base_cfg("static", "manual")
_bad_mask["lan"]["mask"] = "255.255.255.192"
try:
    netgen.cross_validate(_bad_mask)
    raise AssertionError("P3-05 回归：netgen 未拦非 /24 LAN 掩码（存量 /25–/30 不会被迁移）")
except SystemExit:
    assert netgen.ERR.get("code") == "LAN_MASK_TOO_WIDE", netgen.ERR.get("code")
# P3-10：宽带账号/密码拒绝控制字符（防 peers/chap-secrets 拆行注入）
for _bad in ("user\nplugin evil.so", "pw\rx"):
    try:
        validate.norm_wan("wan", {"mode": "pppoe", "username": _bad, "password": "x" * 8, "dns_mode": "auto"})
        raise AssertionError("P3-10 回归：控制字符账号被放行: %r" % _bad)
    except SystemExit:
        assert validate.ERR.get("code") == "WAN_USER_BAD", validate.ERR.get("code")
# P3-07：卸载清单必须清宽带凭据（chap/pap-secrets 明文密码）与 ppp0 生成物
_un = open(os.path.join(ROOT, "uninstall.sh"), encoding="utf-8").read()
assert "chap-secrets" in _un and "pap-secrets" in _un and "25-ppp0.network" in _un, \
    "P3-07 回归：卸载清单缺 chap/pap-secrets 或 25-ppp0.network（凭据泄漏点）"
# P3-08：notes.json 落盘 tmp 名必须唯一化（并发保存写坏 JSON → 备注全丢）
_si = open(os.path.join(ROOT, "panel", "sysinfo.py"), encoding="utf-8").read()
assert "time.time_ns()" in _si, "P3-08 回归：save_notes 仍用固定 .tmp（并发保存写坏 JSON）"
# 观察②：upstream.conf 双写者的 tmp 必须唯一，ip-up 必须保留 v6 上游（合并而非覆盖）
_ipup = open(os.path.join(ROOT, "network", "ppp", "ip-up.d", "dert"), encoding="utf-8").read()
_hook2 = open(os.path.join(ROOT, "network", "dispatcher", "70-dert-dns"), encoding="utf-8").read()
assert "$$.tmp" in _ipup and "$$.tmp" in _hook2, "观察② 回归：upstream.conf 双写者仍共用固定 .tmp"
assert "grep ':'" in _ipup, "观察② 回归：ip-up 仍全量覆盖（会冲掉 dispatcher 写入的 v6 上游）"
# P3-15：dispatcher 的 ppp* 分支必须同时读 /etc/ppp/resolv.conf——只从 CONF 保留 v4，
# 一旦 CONF 丢了 v4 就再也回不来（真机 2026-11-09：CONF 只剩 v6 后，每次 ppp0 事件
# 都只写 v6，面板「DNS 服务器」里运营商 v4 DNS 一直是空的，而 resolv.conf 里两条都在）
# 必须匹配"命令本身"而不是路径字符串——注释里也提到过这个文件，子串匹配会假绿
# （P3-13 的守卫第一版就这么错过一次，这里同款）
assert "grep -E '^nameserver' /etc/ppp/resolv.conf" in _hook2, \
    "P3-15 回归：ppp* 分支没读 /etc/ppp/resolv.conf（CONF 丢 v4 后无法自愈）"
# P3-17：ppp0 重拨时 routable 事件常早于 DHCPv6 DNS 学到，v6 上游只能从 links 取，
# 一旦 CONF 在链路消失时被清掉就永久丢失（真机 2026-11-09 17:35：links 里两条 v6
# 一直在，CONF 却只剩 v4，面板与 dnsmasq 都看不到 v6，直到下次手工补）。
# 两处必须同时存在：① 链路消失分支不删 CONF；② v4/v6 取不到时回退 CONF 旧值。
_nc = re.search(r"\n  \*\)\n(.*?)\n\s*exit 0 ;;\n", _hook2, re.S)
assert _nc and "rm -f" not in _nc.group(1), \
    "P3-17 回归：链路消失分支又清空了 upstream.conf（v6 上游失去回退来源）"
assert re.search(r'\[ -n "\$V4" \] \|\| V4=\$\(grep', _hook2), \
    "P3-17 回归：ppp* 分支 v4 为空时没有回退 CONF 旧值"
assert re.search(r'\[ -n "\$V6" \] \|\| V6=\$\(grep', _hook2), \
    "P3-17 回归：ppp* 分支 v6 为空时没有回退 CONF 旧值（重拨后运营商 v6 DNS 丢失）"

# P3-18：WAN 重拨换 IPv6 前缀后有两处陈旧状态必须修——
#   ① sing-box 的 auto_redirect 只在启动时构建 nft 集合 inet6_local_address_set，
#     换前缀后集合仍是旧 /64，发往新 LAN 前缀（含路由器自身新地址）的 v6 被判成
#     "非本地"被劫持进 tun（真机 2026-11-09 17:35：白名单设备"代理全挂"，手动重启
#     sing-box 才恢复）；
#   ② br-lan 上旧前缀的 RA 路由（proto kernel + expires ≈60h）不会被自动清掉。
_hook3 = open(os.path.join(ROOT, "network", "dispatcher", "90-dert-lan-prefix"),
              encoding="utf-8").read()
assert re.search(r'^ +systemctl try-restart sing-box\.service', _hook3, re.M), \
    "P3-18 回归：前缀变更钩子不再重启 sing-box（本地地址集合停在旧前缀）"
# v0.1.59：重启条件从"auto_redirect 在用"改为"sing-box 在运行"。
# v0.1.64 起 00-dert-dns.json 已无前缀相关字段，但重启仍是必需的——
# auto_redirect 只在启动时构建 nft 集合 inet6_local_address_set，换前缀后集合里
# 仍是旧 /64，发往新 LAN 前缀的 v6 会被当成代理流量劫持进 tun（真机 2026-11-09 复现）。
assert re.search(r'systemctl is-active --quiet sing-box\.service', _hook3), \
    "P3-18 回归：重启 sing-box 前未确认它在运行"
# v0.1.66：首次记录分支不再比对 v6 前缀。
# v0.1.59 引入的 cfg_covers() 读的是 00-dert-dns.json 的 dns.rules[].source_ip_cidr，
# 而 v0.1.64 把该字段整个删掉了（白名单 DNS 改由 nft 按 MAC 分流，不再靠源 IP 判断）
# → 该函数恒返回失败 → 「基线缺失」分支每次都无谓地重启一次 sing-box。
# 现在配置里已无任何前缀相关字段，唯一可能过期的是 nft 表里的 v6 DNAT 目标，
# 由 dns-split-sync 按当时地址重算即可。
assert "cfg_covers" not in _hook3, \
    "P3-19 回归：钩子又用 cfg_covers 比对 v6 前缀（v0.1.64 起配置里已无 source_ip_cidr，" \
    "该判断恒失败 → 基线缺失时无谓重启 sing-box）"
assert "source_ip_cidr" not in _hook3, \
    "P3-19 回归：钩子又引用 source_ip_cidr（v0.1.64 起 00-dert-dns.json 不再含该字段）"
assert re.search(r'^    /usr/local/lib/dert/dns-split-sync', _hook3, re.M), \
    "P3-19 回归：首次记录分支没刷新 nft 分流表（v6 DNAT 目标可能停在建表时的旧 GUA）"
# 旧前缀清理只碰带 expires 的 RA 路由，且必须跳过当前前缀（静态/当前前缀路由不能动）
assert re.search(r"awk '/expires/ && \$1 ~ /:/", _hook3), \
    "P3-18 回归：旧前缀路由清理未限定 RA 路由（会误删静态路由）"
assert re.search(r'case " \$NEW " in \*" \$r "\*\) continue', _hook3), \
    "P3-18 回归：旧前缀清理会误删当前前缀的路由"
# exec 的重定向是持久的：这行带 2>/dev/null 会吞掉整个脚本后续的 stderr（第一版踩过）
assert not re.search(r'^exec 9>.*2>/dev/null', _hook3, re.M), \
    "P3-18 回归：锁文件的 exec 重定向吞掉了脚本 stderr"
# 三处通道必须同步（A-04/R-01/P3-02 同族：升级用户拿不到新钩子，面板版本号却是最新）
_inst = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
assert '90-dert-lan-prefix" /etc/networkd-dispatcher/routable.d/' in _inst, \
    "P3-18 回归：install.sh 未安装 90-dert-lan-prefix"
assert "90-dert-lan-prefix /etc/networkd-dispatcher/routable.d/" in _priv_src, \
    "P3-18 回归：升级通道未同步 90-dert-lan-prefix（升级用户永远拿不到）"
assert "routable.d/90-dert-lan-prefix" in _un, \
    "P3-18 回归：卸载清单缺 90-dert-lan-prefix"
# 观察③：容器更新必须前置拒绝自定义网络 / privileged（静默丢参数后用户连不上才发现）
assert "CT_UPDATE_SKIP" in _priv_src, "观察③ 回归：容器更新缺前置拒绝"
# P3-11：硬盘温度只取 temperature.current（遍历全部值会把 drive_trip 阈值当温度）
assert 'temps.get("current")' in _priv_src, "P3-11 回归：硬盘温度仍遍历全部 temperature 值"
# CI 冒烟网关：升级通道的内联收敛与延迟收敛重启必须按 /run/dert/smoke-mode 跳过
# （runner 的网络不能动——CI 实测步骤全绿但 agent 收尾信号传不回，job 被超时标 cancelled）
assert "smoke-mode" in _priv_src \
    and "smoke-mode" in open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read(), \
    "CI 冒烟回归：升级通道收敛未按 smoke-mode 标记跳过"

# P3-12：收敛期结构兜底——残缺 config 不许 traceback，也不许静默生成空转配置。
# 曾经的两种坏结局：缺 lan → KeyError（stderr 被丢弃，用户只看到"配置生成失败"）；
# wan.mode="" → 落进 pppoe 分支，产出 DHCP=no 且无 Address 的 10-wan.network。
def _schema_fail(cfg, label):
    try:
        netgen.schema_check(cfg)
    except SystemExit:
        assert netgen.ERR.get("code") == "CONFIG_SCHEMA", f"{label}: {netgen.ERR.get('code')}"
    else:
        raise AssertionError(f"P3-12 回归：{label} 未被 schema 拦截")


netgen.schema_check(base_cfg("dhcp", "auto"))          # 合法配置必须放行
netgen.schema_check(base_cfg("pppoe", "manual"))
for _mk, _label in (
    (lambda c: c["wan"].__setitem__("mode", ""), "wan.mode 为空"),
    (lambda c: c["wan"].__setitem__("mode", "wireless"), "wan.mode 非法"),
    (lambda c: c.pop("lan"), "缺 lan 段"),
    (lambda c: c.pop("ipv6"), "缺 ipv6 段"),
    (lambda c: c["ports"].__setitem__(0, {"wan": True, "lan": False}), "网口缺 name"),
    (lambda c: c.__setitem__("static", [{"name": "x", "mac": "AA:BB:CC:DD:EE:FF"}]), "静态绑定缺 ip"),
    (lambda c: c.__setitem__("firewall", {"wan_input_drop": True, "portfw": [
        {"name": "x", "proto": "sctp", "wport": "80", "ip": "192.168.10.5", "lport": "80", "enabled": True}]}),
     "转发协议非法"),
    (lambda c: c["lan"].__setitem__("ip", None), "lan.ip 为 null"),
    (lambda c: c["dhcp"].__setitem__("pool_start", "abc"), "DHCP 池非整数"),
):
    _c = base_cfg("dhcp", "auto")
    _c["dhcp"] = {"enabled": True, "pool_start": 100, "pool_end": 200, "lease": 120,
                  "dns1": "", "dns2": "", "gw": ""}
    _mk(_c)
    _schema_fail(_c, _label)
# 收敛路径必须保留生成器 stderr（否则只剩"配置生成失败"，hint 里的"上方错误信息"是空的）
assert "netgen.err" in _priv_src, "P3-12 回归：收敛路径又丢弃了 netgen 的 stderr"
# 备份恢复必须落盘前做结构预检（只校验 json.load 会把坏配置装进系统）
assert "--schema --config" in _priv_src, "P3-12 回归：备份恢复缺结构预检"
# P3-12b：两处 preflight 失败分支（config-set / apply-all）都必须把 pf_out
# （nft -c / dnsmasq --test 的具体报错）透传出去，否则用户只看到"无法生成合法的系统配置"
_pf = re.findall(r'if ! pf_out="\$\(preflight_stage "\$stage"\)"; then(.*?)\n  fi\n', _priv_src, re.S)
assert _pf and all("printf" in g for g in _pf), "P3-12b 回归：预检失败又吞掉了具体原因"

# P3-13：收敛"无漂移"分支必须清理 stage——它在全量路径的 rm -rf 之前就 exit 0，
# 漏了就是每次开机 / 6h 收敛 / 每次升级在 /run/dert 留一份完整生成物，其中
# etc/ppp/chap-secrets 是明文宽带密码（真机 2026-11-09 开机后实测到 stage.XXXXXX）
# 注意：必须按"整行命令"匹配——块里的注释也提到过这条命令，子串匹配会假绿
# （本守卫第一版就是这么写错的，变异测试当场抓到）
_nodrift = re.search(r'if \[ "\$drift" = "0" \]; then(.*?)\n    fi\n', _priv_src, re.S)
assert _nodrift, "P3-13 回归：找不到无漂移分支（源码结构变了？）"
assert re.search(r'^ +rm -rf "\$stage" *$', _nodrift.group(1), re.M), \
    "P3-13 回归：无漂移分支未清理 stage（每次收敛泄漏一份含凭据的生成物）"
# P3-14：dnsmasq 启动前必须预建 servers-file——/run 是 tmpfs，重启后文件不存在，
# 每次开机都会在 dnsmasq 日志里留一条 "cannot read /run/dert-dns/upstream.conf"
_dd = open(os.path.join(ROOT, "systemd", "dnsmasq.service.d", "10-dert-restart.conf"),
           encoding="utf-8").read()
assert "ExecStartPre" in _dd and "upstream.conf" in _dd and "|| : >" in _dd, \
    "P3-14 回归：dnsmasq 缺 servers-file 预建（开机报 cannot read upstream.conf）"

# P3-16：Safari 不认 SVG favicon（只给 data:image/svg+xml 时后台标签显示通用页面图标），
# 必须有 PNG/ICO 兜底；面板还要直接响应 /favicon.ico（浏览器会绕过 <link> 请求它）
_idx = open(os.path.join(ROOT, "panel", "static", "index.html"), encoding="utf-8").read()
assert 'rel="icon" type="image/png"' in _idx, "P3-16 回归：index.html 缺 PNG favicon（Safari 显示通用图标）"
# 按 href 断言具体文件：只查 type="image/png" 的话，删掉 32x32 那一行仍会被 16x16 顶住（假绿）
assert 'href="/static/favicon-32.png"' in _idx and 'href="/static/favicon-16.png"' in _idx, \
    "P3-16 回归：index.html 缺 32x32/16x16 PNG favicon 链接"
assert 'rel="apple-touch-icon"' in _idx, "P3-16 回归：index.html 缺 apple-touch-icon"
for _ico in ("favicon.ico", "favicon-32.png", "apple-touch-icon.png"):
    assert os.path.exists(os.path.join(ROOT, "panel", "static", _ico)), f"P3-16 回归：缺 {_ico}"
_panel = open(os.path.join(ROOT, "panel", "dert_panel.py"), encoding="utf-8").read()
assert '"/favicon.ico"' in _panel, "P3-16 回归：面板未直接响应 /favicon.ico（会 302 成 HTML）"

# P3-19：DNS 按 MAC 分流三件套（v0.1.57）
# 背景：fakeip 是全局 DNS 行为，tun 拦截按 MAC，两者互斥——非白名单设备拿到
# fakeip 却没人接管 = 黑洞（Bing 图片、游戏登录、VS Code 更新全静默失败）。
# 修法：nft 层按 ether saddr 分两路，白名单走 dnsmasq 拿 fakeip、其余走 sing-box 拿真实 IP。
# 之所以不在 sing-box 里用 source_mac_address：实测即使包经 tun 进来、日志已有
# found neighbor，DNS 规则的 MAC 匹配依然不生效（只有 route 规则认）。
_dsp = open(os.path.join(ROOT, "priv", "dns-split-prepare"), encoding="utf-8").read()
_dss = open(os.path.join(ROOT, "priv", "dns-split-sync"), encoding="utf-8").read()
_dst = open(os.path.join(ROOT, "priv", "dns-split-teardown"), encoding="utf-8").read()

# prepare：必须在 sing-box 启动前生成配置（它只在启动时读），且原子写
assert "os.replace" in _dsp, "P3-19 回归：prepare 不是原子写（sing-box 可能读到半个 JSON）"
assert "domain_suffix" in _dsp and "landns" in _dsp, "P3-19 回归：prepare 丢了 .lan 本地解析"

# v0.1.64：分流完全移到 nft 层，sing-box 只保留「白名单专用 DNS 入口 + hijack-dns」。
# 判定依据从源 IP 换成 nft 的二层 ether saddr —— 不受 DHCP 换 IP、邻居表过期影响。
assert '"listen": "::"' in _dsp, \
    "P3-19 回归：5353 未监听双栈（nft 把 v6 DNS DNAT 到 [GUA]:5353，v6 会 connection refused）"
assert "5353" in _dsp, "P3-19 回归：prepare 没生成白名单专用 DNS 入口端口"
assert "hijack-dns" in _dsp, \
    "P3-19 回归：prepare 丢了 hijack-dns（5353 收到的查询不会交给内部 DNS 模块）"
# 带引号才匹配代码里的 JSON 键，注释里是裸词 source_ip_cidr / carrier，不会误判
assert '"protocol": "dns"' not in _dsp, \
    "P3-19 回归：hijack-dns 带了 protocol 条件（direct inbound 不 sniff → 规则静默不匹配 → " \
    "走默认 direct outbound 转发回 5353 自己，无限循环，真机实测 CPU 130%）"
assert '"source_ip_cidr"' not in _dsp, \
    "P3-19 回归：prepare 又生成了 source_ip_cidr 规则（分流应在 nft 层按 MAC 完成）"
assert '"carrier"' not in _dsp, \
    "P3-19 回归：prepare 又生成了 carrier 上游（非白名单已改为直连 dnsmasq，不再进 sing-box）"

# sync：数据源必须是 config.json 的 include_mac_address（sub-store 维护的唯一来源）
assert "include_mac_address" in _dss, "P3-19 回归：sync 没读 include_mac_address（数据源分裂）"
# 白名单的 DNS 必须 DNAT 到 5353（sing-box 入口），非白名单留在 53（dnsmasq）
assert "5353" in _dss, "P3-19 回归：sync 没把白名单 DNS 指向 5353（会退回进 tun 再分流）"

# netgen：dnsmasq 的上游查询必须绕过 sing-box 的 auto_redirect
_ng_src = open(os.path.join(ROOT, "priv", "netgen.py"), encoding="utf-8").read()
assert "dns_bypass" in _ng_src, \
    "P3-19 回归：netgen 没生成 dns_bypass 链（dnsmasq 上游会被 mark 0x2023 送进 tun → fakeip → 断网）"
assert "meta mark set 0x00002024" in _ng_src, \
    "P3-19 回归：dns_bypass 没打 0x2024（这是唯一能完全绕过 auto_redirect 的标记）"

# v0.1.67：前缀变化改由 dert-netwatch 订阅 rtnetlink 触发，ip-up 不再延迟调用。
# 背景：v0.1.65 用 systemd-run --on-active=20s 去猜 PD 委派耗时——那是把时序
# 不确定性换成猜出来的常数（真机实测委派耗时随 BRAS 波动）。而 networkd-dispatcher
# 结构上做不到：它只看链路 operational state，PD 委派只改地址不改状态
# （Debian 包描述：much more limited in the types of events it supports）。
# 业界（OpenWrt netifd / NetworkManager / dhcpcd）一律订阅 rtnetlink。
_ipup = open(os.path.join(ROOT, "network", "ppp", "ip-up.d", "dert"), encoding="utf-8").read()
assert "systemd-run" not in _ipup and "--on-active" not in _ipup, \
    "P3-19 回归：ip-up 又在用延迟调用猜 PD 委派时序（应交给 dert-netwatch 的事件驱动）"
assert "90-dert-lan-prefix" not in _ipup, \
    "P3-19 回归：ip-up 又直接调前缀钩子（ip-up 时 PD 尚未委派，调了也只会看到旧前缀）"

# netwatch：必须订阅 rtnetlink、必须幂等对账、必须能自愈
_ntw = os.path.join(ROOT, "priv", "netwatch")
assert os.path.isfile(_ntw), "P3-19 回归：priv/netwatch 缺失（前缀变化将无人通知）"
_ntw_src = open(_ntw, encoding="utf-8").read()
assert "ip monitor address link" in _ntw_src, \
    "P3-19 回归：netwatch 没有订阅 rtnetlink（这是唯一能拿到地址变化的稳定接口）"
assert "90-dert-lan-prefix" in _ntw_src and "dns-split-sync" in _ntw_src, \
    "P3-19 回归：netwatch 没把事件分发到前缀钩子 / 分流表重建"
# 必须「每条事件都重新读状态」，不能按事件类型解析或按秒去抖：
# 重拨的事件序列是 旧地址删除 → 新地址 tentative → 新地址 scope global，
# 去抖会把最后那个「地址可用」事件丢掉，前缀就永远补不上（v0.1.65 之前的 bug 形态）
assert "reconcile" in _ntw_src, "P3-19 回归：netwatch 丢了幂等对账入口"
assert "sleep" not in _ntw_src.split("reconcile() {")[1].split("}")[0], \
    "P3-19 回归：netwatch 的对账里出现 sleep（事件驱动不该退化成轮询）"
_ntw_unit = os.path.join(ROOT, "systemd", "dert-netwatch.service")
assert os.path.isfile(_ntw_unit), "P3-19 回归：dert-netwatch.service 缺失"
_ntw_unit_src = open(_ntw_unit, encoding="utf-8").read()
assert "Restart=always" in _ntw_unit_src, \
    "P3-19 回归：netwatch 没有 Restart=always（进程一死事件链就断，且无人发现）"
assert "ExecStart=/usr/local/lib/dert/netwatch" in _ntw_unit_src, \
    "P3-19 回归：netwatch 单元没指向安装后的脚本路径"

# 三通道必须同步（A-04/R-01/P3-02/P3-18 同族：升级用户拿不到新单元，面板版本号却最新）
assert "priv/netwatch" in _inst, "P3-19 回归：install.sh 未部署 priv/netwatch"
assert "dert-netwatch.service" in _inst, "P3-19 回归：install.sh 未安装 netwatch 单元"
assert "systemctl enable --now dert-netwatch.service" in _inst, \
    "P3-19 回归：install.sh 未启用 netwatch（装了但不开等于没装）"
assert "dert-netwatch.service" in _priv_src, \
    "P3-19 回归：升级通道未同步 netwatch（升级用户永远拿不到）"
assert "enable dert-netwatch.service" in _priv_src, \
    "P3-19 回归：升级通道未启用 netwatch"
assert "dert-netwatch.service" in _un, "P3-19 回归：卸载清单缺 dert-netwatch.service"

# 分流表的等待判据必须是「5353 在监听」，不是 tun 接口存在——
# 本表目标全在 br-lan 上，不引用 tun，tun 建好也不代表 5353 已 bind
assert "5353" in _dss and "ip link show tun0" not in _dss, \
    "P3-19 回归：sync 又在等 tun 接口而不是等 5353 监听（判据错位，超时也白等）"

# 原子替换：nft -f 里直接写 `table X { ... }` 对已存在的表是**追加规则**，不是替换。
# 真机 2026-11-12 实测：netwatch 在运行期多次触发 sync 后，表里堆到 20 条
# （应为 4 条），最后几条还指向已删除的临时地址。
assert "add table inet dert_dns" in _dss and "delete table inet dert_dns" in _dss, \
    "P3-19 回归：sync 没做 nft 原子替换（add+delete 同事务），重复运行会累积规则"
# v6 目标地址必须确定性选取：br-lan 可能有多个全局地址（多前缀/手工/隐私地址），
# 用 "exit 取第一条" 会随内核返回顺序漂移，导致表反复重建
assert "temporary" in _dss and "sort | head -1" in _dss, \
    "P3-19 回归：sync 的 v6 目标选取不确定（排除 temporary + 字典序取第一）"
assert "lan6_pick dynamic" in _dss, \
    "P3-19 回归：sync 不再优先选 dynamic 地址（会选中手工加的静态地址，随时可能消失）"
assert "temporary" in _ntw_src and "sort | head -1" in _ntw_src, \
    "P3-19 回归：netwatch 的 lan6() 与 sync 选取规则不一致（会反复判为不一致）"
assert "lan6_pick dynamic" in _ntw_src, \
    "P3-19 回归：netwatch 的 lan6() 不再优先选 dynamic 地址（与 sync 规则不一致）"
# v0.1.65 死代码清理后不应再出现
assert "TUN4" not in _dss and "TUN6" not in _dss, \
    "P3-19 回归：sync 又读了 tun DNS 地址（新架构白名单走 LAN4:5353，与 tun 无关）"
assert "CARRIER" not in _dsp, \
    "P3-19 回归：prepare 又读了运营商 DNS（非白名单已改为 nft 直连 dnsmasq，不经 sing-box）"
# sync 必须写 lan-prefix 基线：dispatcher 的 routable 事件常早于 PD 委派，
# 钩子的 lan_prefix() 只取 scope global，此刻为空 → 15 秒等待超时后静默 exit 0，
# 基线永久缺失 → 下次换前缀时 OLD 为空，被误判成"首次记录"→ 只写记录就退出，
# 既不 prepare 也不重启 sing-box → 00-dert-dns.json 停在旧前缀
# （真机 2026-11-11 09:19 重启复现：cidr 只剩 192.168.10.0/24）
assert "/run/dert/lan-prefix" in _dss, \
    "P3-19 回归：sync 未写 lan-prefix 基线（基线缺失会导致换前缀后不重启）"
assert "lan-prefix.tmp" in _dss, \
    "P3-19 回归：sync 写基线不是原子写（钩子可能读到半个前缀）"
# 空白名单必须不建表：空 = 不限 MAC = 全部流量进 tun，此时全部 DNS 该走 fakeip；
# 若仍建表会让所有 DNS 走 carrier，而流量走 fakeip → 有地址但连不上
assert re.search(r'if \[ -z "\$WLS" \]; then.*?exit 0', _dss, re.S), \
    "P3-19 回归：include_mac_address 为空时仍建表（'全部走代理'的语义被破坏）"
# nft -f 批量加载是原子的；逐条 add rule 中途失败会留下半张表（v6 没建 → 黑洞）
assert "nft -f -" in _dss, "P3-19 回归：sync 用逐条 add rule（非原子，可能留半张表）"
assert re.search(r'if ! nft -f -', _dss), "P3-19 回归：建表失败没有回落分支"
# 必须确认 sing-box 真活着才建表，否则表指向没人监听的地址 → 全网断 DNS。
# 且必须接受 activating：ExecStartPost 执行时 unit 还没到 active，
# 只判 active 会让表永远建不起来（真机首测踩到）
assert "systemctl is-active sing-box.service" in _dss, \
    "P3-19 回归：sync 没确认 sing-box 存活就建表（可能指向死地址）"
assert "activating" in _dss, \
    "P3-19 回归：sync 不接受 activating 状态（ExecStartPost 时表建不起来）"

assert "nft delete table inet dert_dns" in _dst, "P3-19 回归：teardown 没删表"
# grep 不能带 "table " 前缀：nft list tables 输出是 "table inet dert_dns"，
# 带前缀永远匹配不到，表会残留导致 DNS 无法回落（真机首测踩到）
assert not re.search(r'grep -q "table ', _dst), \
    "P3-19 回归：teardown 用 'table xxx' 匹配 nft list tables（永远命中不了）"
assert not re.search(r'grep -q "table ', _cu := open(
    os.path.join(ROOT, "priv", "singbox-cleanup"), encoding="utf-8").read()), \
    "P3-19 回归：cleanup 用 'table xxx' 匹配 nft list tables（永远命中不了）"
# 计数不能用 tr|wc -l：最后一项无换行会少算，日志里的台数就会偏小
assert "awk -F, '{print NF}'" in _dss, \
    "P3-19 回归：白名单计数用 wc -l（末项无换行会少算一台）"

# drop-in：ExecStartPost 建表、ExecStopPost 删表。用 ExecStopPost 而不是只靠
# OnFailure——手动 systemctl stop 不触发 OnFailure，表会残留
_d20 = open(os.path.join(ROOT, "systemd", "sing-box.service.d",
                         "20-dert-dns-split.conf"), encoding="utf-8").read()
assert re.search(r'^ExecStartPre=/usr/local/lib/dert/dns-split-prepare', _d20, re.M), \
    "P3-19 回归：drop-in 缺 ExecStartPre（配置在启动后才写，不生效）"
assert re.search(r'^ExecStartPost=\+/usr/local/lib/dert/dns-split-sync', _d20, re.M), \
    "P3-19 回归：ExecStartPost 缺 + 前缀（sing-box 用户无权限写 nft）"
assert re.search(r'^ExecStopPost=\+/usr/local/lib/dert/dns-split-teardown', _d20, re.M), \
    "P3-19 回归：drop-in 缺 ExecStopPost（手动 stop 后表残留 → 全网断 DNS）"

# cleanup：OnFailure 路径的双保险
_cu = open(os.path.join(ROOT, "priv", "singbox-cleanup"), encoding="utf-8").read()
assert "dert_dns" in _cu, "P3-19 回归：singbox-cleanup 没删 DNS 分流表（崩溃后断 DNS）"

# 安装 / 卸载 / 升级通道三处必须同步（A-04 同族，已经踩过四次）
_inst = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
for _f in ("dns-split-prepare", "dns-split-sync", "dns-split-teardown"):
    assert f'priv/{_f}"' in _inst, f"P3-19 回归：install.sh 没安装 {_f}"
assert "20-dert-dns-split.conf" in _inst, "P3-19 回归：install.sh 没装 DNS 分流 drop-in"

_un = open(os.path.join(ROOT, "uninstall.sh"), encoding="utf-8").read()
assert "20-dert-dns-split.conf" in _un, "P3-19 回归：uninstall.sh 没清 drop-in"
assert "nft delete table inet dert_dns" in _un, "P3-19 回归：uninstall.sh 没清 nft 表"
# 程序目录用 rm -rf "$LIB_DIR" 整体删除（dns-split-*、wait-wan、singbox-cleanup 等都在其下）。
# 逐文件列举在整目录删除之后是冗余的，改为断言整目录删除本身存在——
# 一旦有人把这条删掉改成部分清理，守卫会立刻失败。
assert re.search(r'rm -rf "\$LIB_DIR"', _un), \
    "P3-19 回归：uninstall.sh 没整体删程序目录（dns-split-* 等会残留）"

assert "20-dert-dns-split.conf" in _priv_src, "P3-19 回归：升级通道没同步 DNS 分流 drop-in"
assert "dns-split-prepare" in _priv_src, "P3-19 回归：升级通道没同步 dns-split-prepare"

# 前缀变更钩子必须重新生成配置：source_ip_cidr 里含旧 v6 前缀，
# nft 表里白名单的 v6 DNS 目标也指向 dnsmasq 的旧全局地址
assert "dns-split-prepare" in _hook3, \
    "P3-19 回归：前缀变更后没重新生成 DNS 配置（v6 网段停在旧前缀）"

# P3-20：发布包必须带许可证与变更记录。
# LICENSE 缺失在法律上等于「保留所有权利」，下载源码包的人无从得知授权条款。
# 升级包白名单必须放行**顶层文件**而不是逐个列举——执行校验的是旧版代码，
# 若用允许清单，新版本一加文档就会被旧白名单拒掉，升级包自我阻塞无法自愈
# （v0.1.61 真机实测踩到：UPG_BAD「升级包含不认识的条目」）。
_rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8").read()
for _f in ("LICENSE", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"):
    assert _f in _rel, f"P3-20 回归：release 打包清单缺 {_f}（源码包不含该文件）"
assert 'if "/" not in p: continue' in _priv_src, \
    "P3-20 回归：升级包白名单未放行顶层文件（新增文档会导致升级被拒）"
assert "ALLOW_FILE" not in _priv_src, \
    "P3-20 回归：白名单退化为顶层文件允许清单（新增文档即自我阻塞）"

# P3-21：dnsmasq 必须用 bind-dynamic，不能用 bind-interfaces。
# 开机时序：dnsmasq 先起（br-lan 只有 fe80::），4 秒后 PD 委派的 GUA 才出现。
# bind-interfaces 是启动瞬间的静态绑定，不会补绑 GUA —— 而 dns-split 的 nft 表把
# 白名单设备的 v6 DNS DNAT 到该 GUA，结果全部 connection refused，iPhone 每次新
# 域名先撞一次 v6 失败再回落 v4（真机 2026-11-11 复现，6281 个包全部落空）。
_ng = open(os.path.join(ROOT, "priv", "netgen.py"), encoding="utf-8").read()
assert '"bind-dynamic"' in _ng, \
    "P3-21 回归：netgen.py 未使用 bind-dynamic（PD 委派的 GUA 上 53 端口不会被监听）"
# 注释里会提到 bind-interfaces 作为反面说明，这里只断言它不是实际输出项
assert '\n             "bind-interfaces"' not in _ng and "\"bind-interfaces\"," not in _ng, \
    "P3-21 回归：netgen.py 仍在生成 bind-interfaces"

# ---- P3-23：行为测试层 + CI 分层（v0.1.69）----
# 这套断言守的是"测试架构"本身：行为测试必须存在、不得依赖 root、
# CI 必须先用秒级的 fast 阶段 gate 住重活。
for _rel in ("tests/support.py", "tests/test_netgen_artifacts.py", "tests/test_validate_rules.py"):
    assert os.path.isfile(os.path.join(ROOT, _rel)), \
        f"P3-23 回归：行为测试缺失 {_rel}"

_sup = open(os.path.join(ROOT, "tests", "support.py"), encoding="utf-8").read()
# 行为测试必须能在无 root 环境跑：不能出现 sudo / 直接写 /etc
assert "sudo" not in _sup, "P3-23 回归：tests/support.py 依赖 sudo（fast 阶段跑不了）"
assert '"/etc/dert' not in _sup.replace('DERT_VALIDATE_CONFIG', ''), \
    "P3-23 回归：tests/support.py 硬编码了 /etc 路径（测试会碰真机配置）"

# 产物式断言优先：新测试文件里不应再出现"对源码做字符串包含"的写法
for _t in ("tests/test_netgen_artifacts.py", "tests/test_validate_rules.py"):
    _src = open(os.path.join(ROOT, _t), encoding="utf-8").read()
    assert "open(os.path.join(ROOT, \"priv\"" not in _src, \
        f"P3-23 回归：{_t} 在直接读源码做字符串断言（应断言产物/输出）"

_smoke = open(os.path.join(ROOT, ".github", "workflows", "smoke.yml"), encoding="utf-8").read()
assert "fast-tests:" in _smoke, "P3-23 回归：CI 缺 fast-tests 阶段"
assert "python3 -m unittest discover -s tests -t ." in _smoke, \
    "P3-23 回归：CI 的 fast 阶段没跑行为测试"
assert "needs: fast-tests" in _smoke, \
    "P3-23 回归：重活阶段没有依赖 fast-tests（快测挂了仍会浪费 25 分钟集成跑）"

# ---- P3-24：升级清单解析不得再用 eval ----
# 只看非注释行：注释里解释"以前用 eval"是合理的，断言源码文本时
# 必须排除注释，否则清理代码时会被自己的说明文字绊倒（已踩过三次）。
_priv_src_all = open(os.path.join(ROOT, "priv", "dert-priv"), encoding="utf-8").read()
_priv_code = [l for l in _priv_src_all.split("\n") if not l.strip().startswith("#")]
assert not any("eval " in l for l in _priv_code), \
    "P3-24 回归：dert-priv 又在用 eval 执行动态生成的代码（改用 read 读值）"

# ---- P3-25：收敛侧字段级安全断言（v0.1.70）----
# validate.py 只在「面板写入」路径生效；备份恢复/回滚/升级后收敛直接读
# config.json，绕过它。而这些字段会被原样拼进 dnsmasq/nft/network 文件，
# 一个换行即可注入指令（dnsmasq 的 dhcp-script= 以 root 执行）。
# 断言落在 schema_check 里，因为它在 --schema/--check/--stage 全路径都会跑。
_ng_code = [l for l in open(os.path.join(ROOT, "priv", "netgen.py"),
                            encoding="utf-8").read().split("\n")
            if not l.strip().startswith("#")]
_ng_join = "\n".join(_ng_code)
assert "def scan(" in _ng_join and "ctrl.search" in _ng_join, \
    "P3-25 回归：schema_check 不再扫描控制字符（备份包可注入 dnsmasq/nft 指令）"
assert "scan(cfg, \"\")" in _ng_join, \
    "P3-25 回归：schema_check 没有对整份配置调用 scan()"
assert "dhcp-host" in _ng_join, \
    "P3-25 回归：静态绑定的生成逻辑不见了（守卫需同步更新）"

# ---- P3-26：升级下载加固（v0.1.70）----
# 1) 清单里的 url 只接受 https：curl 会照做 file:// 等协议
# 2) 单文件大小上限：恶意/错误源不能把磁盘写满
assert "--max-filesize" in _priv_src_all, \
    "P3-26 回归：升级下载又没限制文件大小（恶意源可写满磁盘）"
assert "升级源地址必须以 https:// 开头" in _priv_src_all, \
    "P3-26 回归：清单 url 未强制 https（file:// 会被 curl 照做）"

# ---- P3-27：CI 必须有 shell 静态分析（v0.1.70）----
# 1,744 行 bash 是最大风险面，此前只有 `bash -n` 语法检查。
assert "shellcheck" in _smoke, \
    "P3-27 回归：CI 又丢了 shellcheck（bash 静态分析是本项目性价比最高的单项投入）"
assert "xargs -0 shellcheck" in _smoke, \
    "P3-27 回归：shellcheck 步骤没有自动收集脚本（新增脚本会漏检）"

# ---- P3-28：陈旧生成物必须按 manifest 回收（v0.1.71）----
# 旧实现按配置重算「30-<口>.network 的口名是否还在 ports 里」，但生成器对
# WAN 口不产出该文件 → 端口从 LAN 改成 WAN 时旧文件不删 → WAN 口被塞进 LAN 桥。
# 真机 2026-11-12 踩到过。现在改成按 manifest 对账。
_lib_src = open(os.path.join(ROOT, "priv", "lib.sh"), encoding="utf-8").read()
assert "cleanup_stale_port_networks" not in _lib_src, \
    "P3-28 回归：又出现了按配置重算的清理函数（会漏掉 LAN→WAN 的端口角色变更）"
assert "cleanup_stale_generated" in _lib_src, \
    "P3-28 回归：陈旧生成物清理不见了（旧 30-*.network 会永久残留）"
assert 'cleanup.py" "$man" --apply' in _lib_src, \
    "P3-28 回归：清理没有调用 cleanup.py --apply"

_cl_dp = [l for l in open(os.path.join(ROOT, "priv", "dert-priv"),
                          encoding="utf-8").read().split("\n")
          if not l.strip().startswith("#")]
assert sum(1 for l in _cl_dp if "cleanup_stale_generated" in l) == 3, \
    "P3-28 回归：清理调用点不是 3 处（config-set 正常路径 / 回滚路径 / apply-all）"

assert os.path.isfile(os.path.join(ROOT, "priv", "cleanup.py")), \
    "P3-28 回归：priv/cleanup.py 缺失"
_cl_src = open(os.path.join(ROOT, "priv", "cleanup.py"), encoding="utf-8").read()
# 安全边界：清单不可用时必须一个都不删（错误 JSON 也满足 json.load）
assert "not isinstance(files, list) or not files" in _cl_src, \
    "P3-28 回归：cleanup.py 不再校验 files 非空（错误清单会把所有生成物删光）"
assert "由 DebRT 生成" in _cl_src, \
    "P3-28 回归：cleanup.py 不再检查生成标记（会误删用户手写文件）"
assert "由 DebRT 生成" in _cl_src and "PATTERNS" in _cl_src, \
    "P3-28 回归：cleanup.py 缺白名单模式"

# ---- P3-29：PPPoE 看门狗改为事件优先（v0.1.72）----
# 原来 1 分钟轮询：真机实测 24h 1275 次执行，每次起 1 unit + 1 shell + 2 个 python3
# （读同一份 config.json）。而 ppp0 消失这件事 netlink 能立刻收到，不必轮询。
# 现在：事件 → 立即调看门狗打标记 + 一次性 180s 复查；timer 降到 15 分钟做兜底。
_timer = open(os.path.join(ROOT, "systemd", "dert-pppoe-watchdog.timer"),
              encoding="utf-8").read()
assert "OnUnitActiveSec=15min" in _timer, \
    "P3-29 回归：看门狗 timer 周期被改回去了（1 分钟轮询会每天空转 1440 次）"
assert "OnUnitActiveSec=1min" not in _timer, \
    "P3-29 回归：看门狗 timer 又变回每分钟"

_ntw = open(os.path.join(ROOT, "priv", "netwatch"), encoding="utf-8").read()
assert "ip link show ppp0" in _ntw, \
    "P3-29 回归：netwatch 不再检测 ppp0 消失（快速路径失效，只能等 15 分钟兜底）"
assert "systemd-run --on-active=180s" in _ntw, \
    "P3-29 回归：netwatch 缺 180s 有界复查（ppp0 消失后无人判定是否该重启）"
assert "pppoe-watchdog" in _ntw, \
    "P3-29 回归：netwatch 不再调用看门狗"

# 合并 python3：脚本里只应剩 1 处 python3 调用（原先 2 处读同一份 JSON）
assert _wd.count("python3 -c") == 1, \
    f"P3-29 回归：看门狗里的 python3 调用变成 {_wd.count('python3 -c')} 处（应合并为 1）"

# 周期改动必须重启 timer 才生效：enable --now 对已在运行的 timer 是空操作
assert "systemctl restart dert-pppoe-watchdog.timer" in _priv_src_all, \
    "P3-29 回归：升级通道不重启看门狗 timer（新周期要等下次开机才生效）"

print("审计守卫: 撤销表/546/PD新键/nat6矩阵/跨段组合/静态v6落盘/PPPoE-IPv6/热插拔守卫/B-2保留/C-6载波/"
      "P2-01清扫/P3-01锁标记/P3-02升级同步/P3-06三处v6/P3-03~05校验/P3-10控制字符/P3-07卸载/P3-08tmp/"
      "P3-12结构兜底/P3-13stage清理/P3-14servers预建/P3-16图标兜底/P3-17DNS回退/P3-18前缀钩子/"
      "P3-19DNS按MAC分流/P3-20发布包含规/P3-21dnsmasq绑定/P3-23测试分层/P3-24无eval/"
      "P3-25字段安全/P3-26下载加固/P3-27shellcheck/P3-28陈旧回收/P3-29看门狗事件化/观察②③ 全部通过")
