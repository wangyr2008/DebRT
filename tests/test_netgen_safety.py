"""收敛侧的字段级安全断言（v0.1.70 / P3-25）。

背景：`validate.py` 的字段校验只在「面板写入」这一条路径生效。备份恢复、
回滚、升级后收敛都直接读 `config.json`，绕过了它——而这些字段会被原样拼进
dnsmasq / nft / network 文件。一个换行就能注入新指令（dnsmasq 的
`dhcp-script=` 会以 root 执行脚本）。

所以断言放在 `netgen.schema_check`：它在 `--schema`、`--check`、`--stage`
以及所有收敛路径上都会跑，一处覆盖全部入口。

这些用例全部是**行为断言**：喂一份恶意配置，断言它被拒绝并给出错误码，
不去看源码长什么样。
"""
import unittest

import support


class StaticBindingSafety(unittest.TestCase):
    """静态绑定会拼成 dnsmasq 的 dhcp-host={mac},{ip},{name}。"""

    def _cfg(self, name="laptop", mac="AA:BB:CC:DD:EE:01"):
        cfg = support.pppoe_config()
        cfg["static"] = [{"name": name, "ip": "192.168.10.50", "mac": mac}]
        return cfg

    def test_valid_binding_accepted(self):
        # 先确认基线能过——否则下面的"被拒绝"可能是别的原因造成的
        man = support.run_netgen_full(self._cfg())
        text = support.manifest_text(man)["etc/dnsmasq.d/dert.conf"]
        self.assertIn("dhcp-host=AA:BB:CC:DD:EE:01,192.168.10.50,laptop", text)

    def test_newline_in_name_rejected(self):
        # 换行会把一条 dhcp-host 拆成两条指令，后者可注入 dhcp-script=
        err = support.run_netgen_expect_fail(self._cfg(name="laptop\ndhcp-script=/tmp/x"))
        self.assertEqual(err.get("code"), "CONFIG_SCHEMA")
        self.assertIn("static[0].name", err.get("message", ""))

    def test_comma_in_name_rejected(self):
        # 逗号会让 dhcp-host 的字段错位（mac,ip,name → 多出一个字段）
        err = support.run_netgen_expect_fail(self._cfg(name="a,b"))
        self.assertEqual(err.get("code"), "CONFIG_SCHEMA")

    def test_bad_mac_rejected(self):
        err = support.run_netgen_expect_fail(self._cfg(mac="AA:BB:CC:DD:EE"))
        self.assertEqual(err.get("code"), "CONFIG_SCHEMA")

    def test_mac_lowercase_accepted(self):
        # 小写 MAC 是合法的，别把正常配置拦掉
        man = support.run_netgen_full(self._cfg(mac="aa:bb:cc:dd:ee:02"))
        text = support.manifest_text(man)["etc/dnsmasq.d/dert.conf"]
        self.assertIn("aa:bb:cc:dd:ee:02", text)


class PortForwardSafety(unittest.TestCase):
    """转发规则的端口会被直接拼进 nft 表达式。"""

    def _cfg(self, wport="8080", lport="80"):
        cfg = support.pppoe_config()
        cfg["firewall"] = {
            "wan_input_drop": True,
            "portfw": [{"name": "web", "proto": "tcp", "wport": wport,
                        "ip": "192.168.10.50", "lport": lport, "enabled": True}],
        }
        return cfg

    def test_valid_rule_accepted(self):
        man = support.run_netgen_full(self._cfg())
        nft = support.manifest_text(man)["etc/nftables.d/dert-base.nft"]
        self.assertIn("tcp dport 8080 dnat ip to 192.168.10.50", nft)

    def test_port_range_accepted(self):
        # wport 支持 8080-8090 区间，不能被当成非法值拦掉
        man = support.run_netgen_full(self._cfg(wport="8080-8090", lport="80"))
        nft = support.manifest_text(man)["etc/nftables.d/dert-base.nft"]
        self.assertIn("8080-8090", nft)

    def test_injection_in_port_rejected(self):
        err = support.run_netgen_expect_fail(self._cfg(wport="80; reboot"))
        self.assertEqual(err.get("code"), "CONFIG_SCHEMA")


class ControlCharSweep(unittest.TestCase):
    """任意字段里的控制字符都要拦——不只静态绑定那几处。"""

    def test_newline_in_lan_ip_rejected(self):
        cfg = support.pppoe_config()
        cfg["lan"]["ip"] = "192.168.10.1\nAddress=10.0.0.1/24"
        err = support.run_netgen_expect_fail(cfg)
        self.assertEqual(err.get("code"), "CONFIG_SCHEMA")
        self.assertIn("lan.ip", err.get("message", ""))

    def test_tab_in_wan_username_rejected(self):
        cfg = support.pppoe_config()
        cfg["wan"]["username"] = "138\t0000"
        err = support.run_netgen_expect_fail(cfg)
        self.assertEqual(err.get("code"), "CONFIG_SCHEMA")

    def test_nested_list_field_rejected(self):
        # 深层路径也要能报出来（ports[] 里的 name 会进 network 文件名）
        cfg = support.pppoe_config()
        cfg["ports"][0]["name"] = "enp1s0\n"
        err = support.run_netgen_expect_fail(cfg)
        self.assertEqual(err.get("code"), "CONFIG_SCHEMA")
        self.assertIn("ports[0].name", err.get("message", ""))


if __name__ == "__main__":
    unittest.main()
