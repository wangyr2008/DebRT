"""产物行为测试：断言 netgen 生成的**文件内容**，而不是源码长什么样。

这些断言在重构（改结构、改语言、改文件划分）后应当依然成立，
只要生成出来的配置语义没变。
"""
import re
import unittest

import support


class TestPPPoENetwork(unittest.TestCase):
    """25-ppp0.network 的语义（真机踩过两个坑）。"""

    @classmethod
    def setUpClass(cls):
        cls.ng = support.load_netgen()
        cls.cfg = support.pppoe_config()
        cls.body = cls.ng.gen_ppp_network(cls.cfg)

    def test_matches_ppp0(self):
        self.assertIn("Name=ppp0", self.body)

    def test_ipv6_client_enabled(self):
        # 只写 DHCPPrefixDelegation=yes 不会拉起 DHCPv6 客户端（真机 546/547 为零）
        self.assertIn("DHCP=ipv6", self.body)
        self.assertIn("DHCPPrefixDelegation=yes", self.body)
        self.assertIn("WithoutRA=solicit", self.body)

    def test_keep_configuration(self):
        # 缺这条时 networkd 重配 link 会删掉 pppd 的 IPv4 → WAN 断网（真机实测）
        self.assertIn("KeepConfiguration=yes", self.body)

    def test_default_route_without_scope(self):
        # 运营商不发 RA、DHCPv6 不带默认路由，必须自己建
        self.assertIn("Destination=::/0", self.body)
        # IPv6 路由没有 scope 概念，写了 networkd 会忽略并每次开机刷警告
        self.assertNotIn("Scope=", self.body)


class TestPppdPeer(unittest.TestCase):
    """peers/dert 的 MTU。

    1492 是 PPPoE 理论上限，但 RA 宣告 1500 + v6 的 MSS 钳制失效，会让每个
    新连接都先吃一次 PTB（真机 2026-11-13：PTB 20 秒 +5，改后 30 秒 +0）。
    """

    def setUp(self):
        self.ng = support.load_netgen()

    def test_mtu_matches_pppoe_constant(self):
        body = self.ng.gen_pppd(support.pppoe_config(), "pw")
        self.assertIn("mtu %d" % self.ng.PPPOE_MTU, body)
        self.assertIn("mru %d" % self.ng.PPPOE_MTU, body)

    def test_mtu_is_1480(self):
        # 实测偏好：1480 时网页明显更快，1492 时偏慢（ping -M do 证明 1492 可通，
        # 所以不是路径 MTU 问题，而是别让客户端发接近上限的包）。
        self.assertEqual(self.ng.PPPOE_MTU, 1480)


class TestLANNetwork(unittest.TestCase):

    def setUp(self):
        self.ng = support.load_netgen()

    def test_forwarding_on(self):
        body = self.ng.gen_lan_network(support.pppoe_config())
        self.assertIn("IPForward=yes", body)
        self.assertIn("Address=192.168.10.1/24", body)

    def test_ipv6_on(self):
        body = self.ng.gen_lan_network(support.pppoe_config())
        self.assertIn("DHCPPrefixDelegation=yes", body)
        self.assertIn("IPv6SendRA=yes", body)

    def test_ipv6_off(self):
        cfg = support.pppoe_config()
        cfg["ipv6"]["enabled"] = False
        body = self.ng.gen_lan_network(cfg)
        self.assertNotIn("IPv6SendRA=yes", body)
        self.assertNotIn("DHCPPrefixDelegation=yes", body)
        self.assertNotIn("SubnetId", body)

    def test_pd_subnet_id_avoids_wan_prefix(self):
        """LAN 必须从 PD 里取**第二个** /64，避开 WAN 自己的地址。

        运营商委派的 PD 常常是 /60，而 WAN 的 IA_NA 地址就落在 PD 的第一个
        /64 里。networkd 默认（SubnetId=auto）把第一个 /64 分给 LAN，于是
        WAN 与 LAN 同前缀：路由表两条相同的 /64，选路优先 ppp0，发往 LAN
        客户端的包全被送进 WAN（真机 2026-11-14 08:39：ppp0 与 br-lan 都是
        ...:1fe0::/64，`ip -6 route get <客户端>` 返回 dev ppp0，LAN 的 v6 全断）。
        """
        body = self.ng.gen_lan_network(support.pppoe_config())
        self.assertIn("[DHCPPrefixDelegation]", body)
        self.assertIn("UplinkInterface=ppp0", body)
        self.assertIn("SubnetId=1", body)
        # 段必须独立：不能把 IPv6SendRA=yes 之类吞进 [DHCPPrefixDelegation]
        seg = body.split("[DHCPPrefixDelegation]", 1)[1]
        self.assertNotIn("IPv6SendRA=yes", seg)

    def test_pd_uplink_follows_wan_mode(self):
        cfg = support.pppoe_config()
        cfg["wan"]["mode"] = "dhcp"
        body = self.ng.gen_lan_network(cfg)
        wan = next(p["name"] for p in cfg["ports"] if p.get("wan"))
        self.assertIn("UplinkInterface=%s" % wan, body)

    def test_ra_lifetime_outlasts_interval(self):
        """RA 的寿命必须远大于发送间隔。

        真机 2026-11-13：寿命 600 秒、间隔上限也是 600 秒，15:41 发过一条之后
        15:51 全屋 v6 突然断（默认路由到期，下一条 RA 恰好还没到），
        断开 Wi-Fi 重连才恢复——因为 RS 会立刻触发一次 RA。
        按 RFC 4861 取 3 倍关系。
        """
        body = self.ng.gen_lan_network(support.pppoe_config())
        life = int(re.search(r"RouterLifetimeSec=(\d+)", body).group(1))
        gap = int(re.search(r"MaxRtrAdvIntervalSec=(\d+)", body).group(1))
        self.assertGreaterEqual(life, gap * 3,
                                "RA 寿命 %d 秒 vs 间隔 %d 秒，余量不足" % (life, gap))

    def test_ipv6_mtu_matches_pppoe(self):
        """br-lan 的 v6 MTU 要与 ppp0 对齐。

        注意它**不会**进 RA 的 MTU 选项（抓包验证：RA 长度 80，有选项会是 88），
        只影响本机转发和 PTB 里报的值。RA 照常发送——早前"设了它就不发 RA"
        是抓包工具不可用导致的误判。
        """
        body = self.ng.gen_lan_network(support.pppoe_config())
        self.assertIn("IPv6MTUBytes=%d" % self.ng.PPPOE_MTU, body)


class TestDnsmasq(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.ng = support.load_netgen()
        cls.body = cls.ng.gen_dnsmasq(support.pppoe_config())

    def test_bind_dynamic_not_interfaces(self):
        # bind-interfaces 只在启动瞬间静态绑定，PD 委派的 GUA 上 53 永远不监听
        # （真机 6281 个包 connection refused）
        self.assertIn("bind-dynamic", self.body)
        self.assertNotIn("bind-interfaces", self.body)

    def test_upstream_from_runtime_file(self):
        # 上游由钩子写 /run/dert-dns/upstream.conf，配置里不能写死
        self.assertIn("no-resolv", self.body)
        self.assertIn("servers-file=/run/dert-dns/upstream.conf", self.body)

    def test_lan_interfaces(self):
        self.assertIn("interface=br-lan", self.body)
        self.assertIn("interface=lo", self.body)

    def test_dhcp_range(self):
        self.assertIn("dhcp-range=192.168.10.100,192.168.10.200", self.body)

    def test_local_domain(self):
        self.assertIn("domain=lan", self.body)
        self.assertIn("local=/lan/", self.body)


class TestNftBase(unittest.TestCase):

    def setUp(self):
        self.ng = support.load_netgen()
        self.body = self.ng.gen_nft(support.pppoe_config())

    def test_table_and_hooks(self):
        for chain in ("input", "forward", "dstnat", "srcnat"):
            self.assertIn(f"chain {chain}", self.body)

    def test_wan_input_dropped(self):
        # 默认 wan_input_drop=true：WAN 侧一律丢弃（LAN 侧由 policy accept 放行）
        self.assertIn("chain wan_in", self.body)
        self.assertIn("drop", self.body)

    def test_dns_bypass_marks_2024(self):
        # dnsmasq 的上游查询必须打 0x2024 绕过 sing-box，否则被 hijack 成 fakeip
        self.assertIn("chain dns_bypass", self.body)
        self.assertIn("0x00002024", self.body)

    def test_forward_mss_clamp(self):
        self.assertIn("chain forward_mss", self.body)
        # v4 沿用 rt mtu；v6 必须显式给值——rt mtu 在 v6 下压不住客户端的 1440，
        # 结果就是每个连接都靠 PTB 纠一次，而部分路径连 PTB 都没有
        self.assertIn("meta nfproto ipv4 oifname $WAN_IFS tcp flags syn "
                      "tcp option maxseg size set rt mtu", self.body)
        self.assertIn("meta nfproto ipv6 oifname $WAN_IFS tcp flags syn "
                      "tcp option maxseg size set %d" % (self.ng.PPPOE_MTU - 60),
                      self.body)

    def test_rejects_retired_v6_source(self):
        """RFC 7084 L-14：用已失效前缀作源地址的转发包要回 ICMPv6 不可达。

        只有 L-13（废弃旧前缀）不够——客户端可以一直装作没看见，继续拿旧地址
        发包，而包到上游就被丢。L-14 让它立刻失败并重新选源。
        """
        self.assertIn("set retired_v6", self.body)
        self.assertIn("ip6 saddr @retired_v6 reject with icmpx type no-route", self.body)
        # 必须排在 established 之前：否则旧源地址上的既有连接会被 conntrack 放行，
        # 永远收不到这条纠正。（input 链里也有同样的 accept，所以要截取 forward 段比较）
        fwd = self.body.split("chain forward {", 1)[1]
        self.assertLess(fwd.index("@retired_v6 reject"),
                        fwd.index("ct state established,related accept"))

    def test_port_forward_dnat(self):
        cfg = support.pppoe_config()
        cfg["firewall"]["portfw"] = [
            {"name": "web", "proto": "tcp", "wport": "8080",
             "ip": "192.168.10.50", "lport": "80", "enabled": True}
        ]
        body = self.ng.gen_nft(cfg)
        self.assertIn("192.168.10.50", body)


class TestPortNetworks(unittest.TestCase):
    """端口角色 → 生成的 30-*.network（真机上曾因残留文件差点把 WAN 口塞进桥）。"""

    def setUp(self):
        self.ng = support.load_netgen()

    def test_wan_port_has_no_bridge_file(self):
        cfg = support.pppoe_config()
        pairs = dict(self.ng.gen_port_networks(cfg))
        # WAN 口由 10-wan.network 管理，绝不能生成 Bridge=br-lan 的 30- 文件
        self.assertNotIn("etc/systemd/network/30-enp4s0.network", pairs)

    def test_lan_ports_join_bridge(self):
        cfg = support.pppoe_config()
        pairs = dict(self.ng.gen_port_networks(cfg))
        for iface in ("enp1s0", "enp2s0"):
            key = f"etc/systemd/network/30-{iface}.network"
            self.assertIn(key, pairs)
            self.assertIn("Bridge=br-lan", pairs[key])

    def test_unused_port_disabled(self):
        cfg = support.pppoe_config()
        cfg["ports"] = [{"name": "enp3s0", "wan": False, "lan": False}]
        pairs = dict(self.ng.gen_port_networks(cfg))
        body = pairs["etc/systemd/network/30-enp3s0.network"]
        self.assertIn("LinkLocalAddressing=no", body)
        self.assertNotIn("Bridge=br-lan", body)


class TestFullPipeline(unittest.TestCase):
    """走真实 main() 入口，验证 manifest 契约（面板与部署都依赖它）。"""

    @classmethod
    def setUpClass(cls):
        cls.man = support.run_netgen_full(support.pppoe_config())

    def test_ok(self):
        self.assertTrue(self.man.get("ok"))

    def test_paths_are_relative_and_safe(self):
        for f in self.man["files"]:
            p = f["path"]
            self.assertFalse(p.startswith("/"), f"路径必须相对：{p}")
            self.assertNotIn("..", p, f"路径不能穿越：{p}")

    def test_every_file_has_content_and_mode(self):
        for f in self.man["files"]:
            self.assertTrue(f["content_b64"], f"空内容：{f['path']}")
            self.assertIsInstance(f["mode"], int)
            self.assertGreater(f["mode"], 0)

    def test_expected_artifacts_present(self):
        paths = {f["path"] for f in self.man["files"]}
        for need in (
            "etc/systemd/network/10-wan.network",
            "etc/systemd/network/20-lan.netdev",
            "etc/systemd/network/20-lan.network",
            "etc/systemd/network/25-ppp0.network",
            "etc/dnsmasq.d/dert.conf",
            "etc/nftables.d/dert-base.nft",
        ):
            self.assertIn(need, paths)

    def test_change_hints(self):
        # v0.1.70 删掉了 `restart` 字段：它与 dert-priv 里 plan_for_section 的
        # 语义重复，且全仓库无消费者（旧断言在这里跟着一起删）
        self.assertTrue(self.man.get("pppoe"), "PPPoE 模式应提示需要重启拨号")
        self.assertTrue(self.man.get("networkd"), "生成物含 networkd 配置，应提示重载")

    def test_no_wan_network_for_pppoe(self):
        # PPPoE 下 10-wan.network 应把物理 WAN 口配成"什么都不做"
        text = support.manifest_text(self.man)["etc/systemd/network/10-wan.network"]
        self.assertIn("DHCP=no", text)
        self.assertIn("ConfigureWithoutCarrier=yes", text)


if __name__ == "__main__":
    unittest.main()
