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
assert "Scope=link" in ppp_net, "PPPoE ppp0 缺 ::/0 默认路由（运营商不发 RA，只有地址没有出口）"
# R-02：ppp0 必须被 DNS 钩子认作 WAN，且与 ip-up.d 写入的 IPv4 上游合并而非覆盖
_hook = open(os.path.join(ROOT, "network", "dispatcher", "70-seedrou-dns"), encoding="utf-8").read()
assert 'wans.append("ppp0")' in _hook, "R-02 回归：DNS 钩子不认 ppp0（PPPoE 的 IPv6 上游进不来）"
assert "ppp*)" in _hook, "R-02 回归：ppp0 缺少 v4/v6 合并分支"
# R-01：升级同步块必须覆盖所有 /etc 钩子（曾漏 v0.1.43 新增的热插拔钩子，
# 导致"插网线自愈"只对全新安装生效）
_priv_src = open(os.path.join(ROOT, "priv", "seedrou-priv"), encoding="utf-8").read()
assert "75-seedrou-pppoe-hotplug" in _priv_src, "R-01 回归：升级同步漏了 PPPoE 热插拔钩子"
# PPPoE 看门狗：pppd 对"接口被外部删除"不敏感，服务 active 但 ppp0 消失时必须能自愈
_wd = open(os.path.join(ROOT, "priv", "pppoe-watchdog"), encoding="utf-8").read()
assert "ppp0" in _wd and "systemctl restart" in _wd, "看门狗缺少 ppp0 探测或重启逻辑"
assert os.path.exists(os.path.join(ROOT, "systemd", "seedrou-pppoe-watchdog.timer")), "看门狗 timer 缺失"
assert "seedrou-pppoe-watchdog.timer" in _priv_src, "升级同步未覆盖看门狗 timer"
# 看门狗健康判据必须同时认 IPv4 与全局 IPv6（只认 v4 会误杀 IPv6-only 链路，
# 每 3 分钟重启一次拨号，比卡死更糟）
assert "scope global" in _wd, "B-01 回归：看门狗只认 IPv4"
# C-6：看门狗必须有载波探测——无线缆时 pppd persist 本就在重试，
# 此刻重启只是无意义搅动（每 ~4min 一次）
assert "carrier" in _wd, "C-6 回归：看门狗缺载波探测（无线缆时空转重启拨号）"
# A-1：热插拔钩子必须有 WAN 模式守卫——DHCP/静态模式下载波事件绝不能拉起
# 拨号服务（pppd 会用残留 peers 真实拨号，双默认路由劫持 DHCP 模式）。
# watchdog 与 hotplug 是同一逻辑两处存放，守卫必须两侧断言（"改一半"第八次的教训）
_hot = open(os.path.join(ROOT, "network", "dispatcher", "75-seedrou-pppoe-hotplug"), encoding="utf-8").read()
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
assert "_SEEDROU_LOCK_HELD=1 /usr/local/lib/seedrou/seedrou-priv apply-all --convergent" in _priv_src, \
    "P3-01 回归：升级内联收敛缺锁标记（子进程必 BUSY）"
# P3-02：升级通道必须同步 network/systemd/seedrou-pppoe.service（A-04/R-01 同族第三处）
assert "network/systemd/seedrou-pppoe.service" in _priv_src, \
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
_ipup = open(os.path.join(ROOT, "network", "ppp", "ip-up.d", "seedrou"), encoding="utf-8").read()
_hook2 = open(os.path.join(ROOT, "network", "dispatcher", "70-seedrou-dns"), encoding="utf-8").read()
assert "$$.tmp" in _ipup and "$$.tmp" in _hook2, "观察② 回归：upstream.conf 双写者仍共用固定 .tmp"
assert "grep ':'" in _ipup, "观察② 回归：ip-up 仍全量覆盖（会冲掉 dispatcher 写入的 v6 上游）"
# 观察③：容器更新必须前置拒绝自定义网络 / privileged（静默丢参数后用户连不上才发现）
assert "CT_UPDATE_SKIP" in _priv_src, "观察③ 回归：容器更新缺前置拒绝"
# P3-11：硬盘温度只取 temperature.current（遍历全部值会把 drive_trip 阈值当温度）
assert 'temps.get("current")' in _priv_src, "P3-11 回归：硬盘温度仍遍历全部 temperature 值"
# CI 冒烟网关：升级通道的内联收敛与延迟收敛重启必须按 /run/seedrou/smoke-mode 跳过
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
# 漏了就是每次开机 / 6h 收敛 / 每次升级在 /run/seedrou 留一份完整生成物，其中
# etc/ppp/chap-secrets 是明文宽带密码（真机 2026-11-09 开机后实测到 stage.XXXXXX）
# 注意：必须按"整行命令"匹配——块里的注释也提到过这条命令，子串匹配会假绿
# （本守卫第一版就是这么写错的，变异测试当场抓到）
_nodrift = re.search(r'if \[ "\$drift" = "0" \]; then(.*?)\n    fi\n', _priv_src, re.S)
assert _nodrift, "P3-13 回归：找不到无漂移分支（源码结构变了？）"
assert re.search(r'^ +rm -rf "\$stage" *$', _nodrift.group(1), re.M), \
    "P3-13 回归：无漂移分支未清理 stage（每次收敛泄漏一份含凭据的生成物）"
# P3-14：dnsmasq 启动前必须预建 servers-file——/run 是 tmpfs，重启后文件不存在，
# 每次开机都会在 dnsmasq 日志里留一条 "cannot read /run/seedrou-dns/upstream.conf"
_dd = open(os.path.join(ROOT, "systemd", "dnsmasq.service.d", "10-seedrou-restart.conf"),
           encoding="utf-8").read()
assert "ExecStartPre" in _dd and "upstream.conf" in _dd and "|| : >" in _dd, \
    "P3-14 回归：dnsmasq 缺 servers-file 预建（开机报 cannot read upstream.conf）"

print("审计守卫: 撤销表/546/PD新键/nat6矩阵/跨段组合/静态v6落盘/PPPoE-IPv6/热插拔守卫/B-2保留/C-6载波/"
      "P2-01清扫/P3-01锁标记/P3-02升级同步/P3-06三处v6/P3-03~05校验/P3-10控制字符/P3-07卸载/P3-08tmp/"
      "P3-12结构兜底/P3-13stage清理/P3-14servers预建/观察②③ 全部通过")
