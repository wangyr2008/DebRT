"""`priv/configops.py` 的行为测试（v0.1.88 / P2-8）。

`config-set` 是面板每一次"保存"的后端，也是**唯一**会把用户输入直接写进
系统配置的路径。这里锁的是几条"错了会很难查"的规则：

  1. 密码只进 `secrets.json`，且 `has_password` 要跟着置位；
  2. **只在必要时重拨 PPPoE**——改个 DNS 就断线重连是不可接受的；
  3. 失败必须**回滚到修改前**并如实报错，而不是留下半套配置；
  4. `firewall` 段允许只提交一部分（面板可能只改端口转发）。

跑法同 test_applyops：临时根 + 假 systemctl/nft/dnsmasq/ip。
"""
import io
import json
import os
import sys
import unittest

from test_applyops import Sandbox, run


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.c = self.s.a  # 同一份沙箱：applyops.LIB 已指向假目录
        import configops
        self.co = configops
        self.old_stdin = sys.stdin
        self.sec = os.path.join(self.s.td, "etc/dert/secrets.json")

    def tearDown(self):
        sys.stdin = self.old_stdin
        self.s.close()

    def set_section(self, section, body):
        sys.stdin = io.StringIO(json.dumps(body) if not isinstance(body, str) else body)
        return run(self.co.config_set, section)

    def cfg(self):
        return self.s.cfg()

    def fake_ip(self, dev):
        """让 `ip -j route show default` 返回指定 dev，供 wan_route_ok 判定。"""
        p = os.path.join(self.s.bindir, "ip")
        with open(p, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "$ip $*" >> "$FAKELOG"\n'
                    'case "$*" in *route*) echo \'[{"dev":"%s"}]\';; esac\n' % dev)
        os.chmod(p, 0o755)


class Guards(Base):
    def test_missing_section(self):
        rc, out = self.set_section("", {})
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "USAGE")

    def test_invalid_json_body(self):
        rc, out = self.set_section("wan", "{not json")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "VALIDATE")

    def test_unknown_section_rejected_by_validator(self):
        rc, out = self.set_section("nonsense", {"a": 1})
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])


class Wan(Base):
    def test_dhcp_save_applies_and_keeps_secrets(self):
        self.fake_ip("enp1s0")
        self.s.write_sec({"session_secret": "keep", "pppoe_password": "old"})
        rc, out = self.set_section("wan", {"mode": "dhcp", "username": ""})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["message"], "配置已保存并生效")
        self.assertEqual(self.cfg()["wan"]["mode"], "dhcp")
        with open(self.sec, encoding="utf-8") as f:
            sec = json.load(f)
        self.assertEqual(sec["session_secret"], "keep")
        self.assertEqual(sec["pppoe_password"], "old")  # 没填新密码就不动旧密码
        # 部署确实发生了
        self.assertTrue(os.path.isfile(os.path.join(self.s.td, "etc/dnsmasq.d/dert.conf")))

    def test_password_goes_to_secrets_only(self):
        self.fake_ip("ppp0")
        rc, out = self.set_section("wan", {"mode": "pppoe", "username": "13800000000",
                                           "password": "s3cret"})
        self.assertEqual(rc, 0, out)
        cfg = self.cfg()
        self.assertTrue(cfg["wan"]["has_password"])
        self.assertNotIn("s3cret", json.dumps(cfg))          # 不进 config.json
        with open(self.sec, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pppoe_password"], "s3cret")
        # 填了新密码 → 必须重拨
        self.assertTrue(any("restart dert-pppoe" in c for c in self.s.calls()))

    def test_dns_only_change_does_not_redial(self):
        # 已是 pppoe + 同账号，只改 DNS：绝不能断线重连
        cfg = self.cfg()
        cfg["wan"].update(mode="pppoe", username="13800000000")
        self.s.write_cfg(cfg)
        self.fake_ip("ppp0")
        self.s.clear_calls()
        rc, out = self.set_section("wan", {"mode": "pppoe", "username": "13800000000",
                                           "dns_mode": "manual", "dns1": "223.5.5.5",
                                           "dns2": ""})
        self.assertEqual(rc, 0, out)
        self.assertFalse(any("restart dert-pppoe" in c for c in self.s.calls()),
                         "只改 DNS 不应重拨 PPPoE")

    def test_ipv6_toggle_does_not_redial(self):
        """开关 IPv6 不该重拨。

        真机 2026-11-14：一次"关→开"触发两次重拨、换了两批 PD 前缀，客户端
        因此攒下三组地址（每组稳定+隐私两条），表现为"开关一次 IPv6 就多出
        一堆地址"。+ipv6 改成常开、DHCPv6 段落按开关生成后，只需重启 networkd。
        """
        cfg = self.cfg()
        cfg["wan"].update(mode="pppoe", username="13800000000")
        self.s.write_cfg(cfg)
        self.fake_ip("ppp0")
        self.s.clear_calls()
        rc, out = self.set_section("ipv6", {"enabled": True, "mode": "native"})
        self.assertEqual(rc, 0, out)
        self.assertFalse(any("restart dert-pppoe" in c for c in self.s.calls()),
                         "开关 IPv6 不应重拨 PPPoE（每次重拨都会换一个 PD 前缀）")

    def test_mode_change_redials(self):
        self.fake_ip("ppp0")
        self.s.clear_calls()
        rc, out = self.set_section("wan", {"mode": "pppoe", "username": "13800000000"})
        self.assertEqual(rc, 0, out)
        self.assertTrue(any("restart dert-pppoe" in c for c in self.s.calls()))

    def test_username_change_redials(self):
        # P2-2：改账号此前不重拨，pppd 仍持旧凭据会话
        cfg = self.cfg()
        cfg["wan"].update(mode="pppoe", username="old-user")
        self.s.write_cfg(cfg)
        self.fake_ip("ppp0")
        self.s.clear_calls()
        rc, out = self.set_section("wan", {"mode": "pppoe", "username": "new-user"})
        self.assertEqual(rc, 0, out)
        self.assertTrue(any("restart dert-pppoe" in c for c in self.s.calls()))

    def test_wan_unreachable_rolls_back(self):
        # WAN 段无条件验证：连不上就回滚到修改前，并给出可操作提示
        self.fake_ip("tun0")          # 默认路由不在 WAN 口上
        self.co.wait_wan = lambda t: False
        rc, out = self.set_section("wan", {"mode": "dhcp", "username": ""})
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "APPLY_ROLLED_BACK")
        self.assertIn("WAN", out["hint"])


class OtherSections(Base):
    def test_bind_with_pppoe_redials(self):
        cfg = self.cfg()
        cfg["wan"].update(mode="pppoe", username="u")
        self.s.write_cfg(cfg)
        self.fake_ip("ppp0")
        self.s.clear_calls()
        rc, out = self.set_section("bind", {"ports": [
            {"name": "enp1s0", "wan": True, "lan": False},
            {"name": "enp2s0", "wan": False, "lan": True}]})
        self.assertEqual(rc, 0, out)
        self.assertTrue(any("restart dert-pppoe" in c for c in self.s.calls()),
                        "PPPoE 下重绑网口必须重拨")

    def test_lan_message_mentions_relogin(self):
        rc, out = self.set_section("lan", {"ip": "192.168.20.1", "mask": "255.255.255.0"})
        self.assertEqual(rc, 0, out)
        self.assertIn("重新登录", out["message"])
        self.assertEqual(self.cfg()["lan"]["ip"], "192.168.20.1")

    def test_firewall_partial_keeps_other_key(self):
        # 只提交 portfw 时，wan_input_drop 必须保持原值
        before = self.cfg()["firewall"]["wan_input_drop"]
        rc, out = self.set_section("firewall", {"portfw": []})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.cfg()["firewall"]["wan_input_drop"], before)
        self.assertEqual(self.cfg()["firewall"]["portfw"], [])

    def test_firewall_without_existing_section(self):
        """老配置里没有 firewall 段时，只提交 portfw 不能崩。

        直接下标 cfg["firewall"]["portfw"] 会 KeyError，而 KeyError 抛出去就是
        traceback——面板只能看到"特权服务返回异常"，看不出是配置缺段。
        """
        cfg = self.cfg()
        cfg.pop("firewall", None)
        self.s.write_cfg(cfg)
        rc, out = self.set_section("firewall", {"portfw": []})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.cfg()["firewall"]["portfw"], [])

    def test_upgrade_section_saves_without_applying(self):
        self.s.clear_calls()
        rc, out = self.set_section("upgrade", {"source": "https://example.com/m.json"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["message"], "升级源已保存")
        self.assertEqual(self.cfg()["upgrade"]["source"], "https://example.com/m.json")
        self.assertEqual(self.s.calls(), [], "保存升级源不应触发任何服务动作")


class Rollback(Base):
    def test_service_failure_rolls_back_config(self):
        old = self.cfg()
        self.s.rc("systemctl", 1)          # 所有 systemctl 调用失败
        rc, out = self.set_section("dhcp", {
            "enabled": True, "pool_start": 50, "pool_end": 60,
            "lease": 120, "dns1": "", "dns2": "", "gw": ""})
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "APPLY_ROLLED_BACK")
        # 回滚后配置必须是修改前那一份
        self.assertEqual(self.cfg()["dhcp"], old["dhcp"])
        # 快照要留档，事后能查"当时是什么样"
        lg = os.path.join(self.s.td, "var/lib/dert/lastgood")
        self.assertTrue([f for f in os.listdir(lg) if f.startswith("config.json.pre-s")])


if __name__ == "__main__":
    unittest.main()
