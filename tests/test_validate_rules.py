"""校验规则行为测试：断言「输入 → 拒绝码」，不断言源码。

覆盖两类：
  · CLI 级（raw 输入经 norm_* 规范化）—— 用 run_validate 走真实入口
  · 跨段级（已规范化结果）—— 直接调 cross_validate
"""
import unittest

import support

V6_OFF = {"enabled": False, "mode": "native", "dns_mode": "auto", "dns": []}
V6_NATIVE = {"enabled": True, "mode": "native", "dns_mode": "auto", "dns": []}
V6_NAT6 = {"enabled": True, "mode": "nat6", "dns_mode": "auto", "dns": []}
V6_STATIC = {"enabled": True, "mode": "static", "dns_mode": "auto", "dns": [],
             "addr": "2001:db8:1::2/64", "gw": "2001:db8:1::1"}


class TestCrossValidate(unittest.TestCase):
    """组合合法性：上网方式 × IPv6 模式。"""

    def test_native_requires_non_static_wan(self):
        self.assertEqual(
            support.cross_error("ipv6", V6_NATIVE, {"wan": {"mode": "static"}}),
            "V6_NATIVE_STATIC_WAN")

    def test_nat6_rejected_on_static_wan(self):
        self.assertEqual(
            support.cross_error("ipv6", V6_NAT6, {"wan": {"mode": "static"}}),
            "V6_NAT6_STATIC_UNSUPPORTED")

    def test_static_ipv6_rejected_on_pppoe(self):
        self.assertEqual(
            support.cross_error("ipv6", V6_STATIC, {"wan": {"mode": "pppoe"}}),
            "V6_STATIC_PPPPOE_UNSUPPORTED")

    def test_switching_to_pppoe_blocked_by_static_ipv6(self):
        self.assertEqual(
            support.cross_error("wan", {"mode": "pppoe"}, {"wan": {}, "ipv6": V6_STATIC}),
            "V6_STATIC_PPPPOE_UNSUPPORTED")

    def test_valid_combo_passes(self):
        self.assertIsNone(
            support.cross_error("ipv6", V6_NATIVE, {"wan": {"mode": "pppoe"}}))
        self.assertIsNone(
            support.cross_error("wan", {"mode": "pppoe"}, {"wan": {}, "ipv6": V6_NATIVE}))


class TestLANRules(unittest.TestCase):

    def test_network_address_rejected(self):
        rc, err = support.run_validate("lan", {"ip": "192.168.10.0", "mask": "255.255.255.0"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "LAN_IP_BAD")

    def test_broadcast_address_rejected(self):
        rc, err = support.run_validate("lan", {"ip": "192.168.10.255", "mask": "255.255.255.0"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "LAN_IP_BAD")

    def test_non_24_mask_rejected(self):
        rc, err = support.run_validate("lan", {"ip": "192.168.10.1", "mask": "255.255.255.192"})
        self.assertNotEqual(rc, 0)
        self.assertIn(err.get("code"), ("LAN_MASK_BAD", "LAN_MASK_TOO_WIDE"))

    def test_valid_lan_accepted(self):
        rc, err = support.run_validate("lan", {"ip": "192.168.10.1", "mask": "255.255.255.0"})
        self.assertEqual(rc, 0, f"合法 LAN 被拒：{err}")


class TestDHCPRules(unittest.TestCase):

    def test_pool_start_must_be_int(self):
        rc, err = support.run_validate("dhcp", {"enabled": True, "pool_start": "abc",
                                                "pool_end": 200, "lease": 120})
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "DHCP_POOL_BAD")

    def test_pool_bounds(self):
        rc, err = support.run_validate("dhcp", {"enabled": True, "pool_start": 0,
                                                "pool_end": 200, "lease": 120})
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "DHCP_POOL_BAD")

    def test_valid_pool_accepted(self):
        rc, err = support.run_validate("dhcp", {"enabled": True, "pool_start": 100,
                                                "pool_end": 200, "lease": 120})
        self.assertEqual(rc, 0, f"合法地址池被拒：{err}")


class TestWANRules(unittest.TestCase):

    def test_control_chars_in_username_rejected(self):
        rc, err = support.run_validate("wan", {"mode": "pppoe", "username": "user\nname"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "WAN_USER_BAD")

    def test_static_wan_requires_dns_or_manual(self):
        rc, err = support.run_validate("wan", {
            "mode": "static", "ip": "10.10.10.2", "mask": "255.255.255.0",
            "gw": "10.10.10.1", "dns_mode": "auto", "dns": [],
        })
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "WAN_DNS_STATIC_AUTO")

    def test_static_gateway_must_be_in_subnet(self):
        rc, err = support.run_validate("wan", {
            "mode": "static", "ip": "10.10.10.2", "mask": "255.255.255.0",
            "gw": "192.168.1.1", "dns_mode": "manual",
            "dns1": "223.5.5.5", "dns2": "119.29.29.29",
        })
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "WAN_STATIC_BAD")


class TestIPv6Rules(unittest.TestCase):

    def test_static_addr_requires_prefix_len(self):
        rc, err = support.run_validate("ipv6", {
            "enabled": True, "mode": "static", "addr": "2001:db8:1::2",
            "gw": "2001:db8:1::1", "dns_mode": "auto", "dns": [],
        })
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "V6_STATIC_BAD")


class TestFirewallRules(unittest.TestCase):

    def test_unknown_proto_rejected(self):
        rc, err = support.run_validate("firewall", {
            "wan_input_drop": True,
            "portfw": [{"name": "web", "proto": "sctp", "wport": "8080",
                        "ip": "192.168.10.5", "lport": "80", "enabled": True}],
        })
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.get("code"), "FW_BAD")


class TestUpgradeSourceRules(unittest.TestCase):

    def test_private_host_blacklisted(self):
        v = support.load_validate()
        for host in ("127.0.0.1", "192.168.10.1", "localhost", "10.0.0.1", "::1"):
            self.assertTrue(v.url_host_blacklisted(host), f"{host} 应被拒")

    def test_public_host_allowed(self):
        v = support.load_validate()
        for host in ("github.com", "example.com", "1.1.1.1"):
            self.assertFalse(v.url_host_blacklisted(host), f"{host} 应放行")


if __name__ == "__main__":
    unittest.main()
