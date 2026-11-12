"""产物行为测试：断言 netgen 生成的**文件内容**，而不是源码长什么样。

这些断言在重构（改结构、改语言、改文件划分）后应当依然成立，
只要生成出来的配置语义没变。
"""
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
