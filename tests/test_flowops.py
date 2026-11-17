"""调试模式（flowlog 链 + flowops）的行为测试（v0.1.119）。

这里钉的几条都是**真机上真的踩到过的**：
  · `chain flow` 不行 —— flow 是 nft 保留字（flowtable），`nft -c` 直接报
    "unexpected flow, expecting string or last"，整条链建不起来；
  · `burst 40` 不行 —— 缺 `packets` 单位，报 "unexpected log, expecting packets"；
  · DNS 规则必须带 `ct status dnat` —— 否则 mDNS（也是 5353 端口，但走组播
    不做 DNAT）会被记成「DNS 走了 sing-box」，排查时直接被误导。
这三条都不会让程序报错，只会让功能静默失效或给出错误结论，所以必须有断言。
"""
import importlib
import json
import os
import sys
import tempfile
import unittest

import support


class TestFlowChainArtifact(unittest.TestCase):
    """nft 产物：开关决定有没有链，链内容决定分类对不对。"""

    @classmethod
    def setUpClass(cls):
        cls.ng = support.load_netgen()
        cls.cfg = support.pppoe_config()

    def body(self, flow):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["debug"] = {"flow": flow}
        return self.ng.gen_nft(cfg)

    def test_off_by_default_has_no_chain(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg.pop("debug", None)
        body = self.ng.gen_nft(cfg)
        self.assertNotIn("chain flowlog", body,
                         "默认关闭时不该生成任何 flow 链——产物必须与旧版一致")

    def test_explicit_false_has_no_chain(self):
        self.assertNotIn("chain flowlog", self.body(False))

    def test_enabled_has_chain(self):
        body = self.body(True)
        self.assertIn("chain flowlog", body)
        self.assertIn("priority dstnat + 10", body,
                      "探针必须晚于 dstnat+2/+3 的决策点，否则读到的是半成品状态")

    def test_chain_name_is_not_reserved_word(self):
        # 回归：`chain flow {` 是 nft 语法错误（flow 为保留字）
        self.assertNotIn("chain flow {", self.body(True))
        self.assertNotIn('"  chain flow {"', self.body(True))

    def test_burst_has_packets_unit(self):
        body = self.body(True)
        self.assertIn("burst 40 packets", body,
                      "burst 缺 packets 单位会让整份 nft 文件语法错误")

    def test_all_paths_present(self):
        body = self.body(True)
        for p in ("dns-sb", "dns", "singbox", "tun", "direct"):
            self.assertIn('log prefix "dertflow %s "' % p, body)

    def test_singbox_uses_dnat_not_loopback(self):
        """真机对照实验（60 秒）：redirect 之后 `ip daddr 127.0.0.0/8` 命中 0 次，
        `ct status dnat` 命中 45 次——redirect 是把目的改到入接口的本机地址，
        不是回环。用地址段判会让「进 sing-box」永远是 0。"""
        body = self.body(True)
        self.assertNotIn("127.0.0.0/8", body,
                         "redirect 不改到回环，按地址段判永远命中不了")
        seg = [l for l in body.splitlines() if 'dertflow singbox' in l]
        self.assertEqual(len(seg), 1)
        self.assertIn("ct status dnat", seg[0])

    def test_every_rule_scoped_to_lan(self):
        body = self.body(True)
        seg = body[body.index("chain flowlog"):]
        seg = seg[:seg.index("\n  }")]
        rules = [l for l in seg.splitlines() if "log prefix" in l]
        self.assertEqual(len(rules), 5)
        for r in rules:
            self.assertIn("iifname $LAN_BR", r,
                          "不限 br-lan 会把路由器自身与 tun 回程也记进来")

    def test_every_log_rule_terminates(self):
        """log 不终止匹配：真机实测同一条包被记了 4 次（四条路径全命中）。
        每条 log 规则后面必须紧跟一条同条件的 return。"""
        lines = self.body(True).splitlines()
        idx = [i for i, l in enumerate(lines) if "log prefix" in l]
        self.assertEqual(len(idx), 5)
        for i in idx:
            nxt = lines[i + 1]
            self.assertIn("return", nxt, "log 规则后没有 return：同一条包会被多次记录")
            self.assertIn("iifname $LAN_BR", nxt)

    def test_multicast_skipped_before_logging(self):
        """SSDP/mDNS 是周期性广播，20 秒能刷上百行，会把要看的连接埋掉；
        它们也永远不可能走代理，所以第一条规则就该放过。"""
        body = self.body(True)
        seg = body[body.index("chain flowlog"):]
        first_rules = [l.strip() for l in seg.splitlines()[2:5]]
        self.assertIn("224.0.0.0/4", first_rules[0])
        self.assertIn("return", first_rules[0])
        self.assertIn("ff00::/8", first_rules[2])
        for r in first_rules:
            self.assertNotIn("log prefix", r, "组播不该被记录")

    def test_dns_rules_require_local_dest(self):
        """mDNS 也用 5353 端口但目标是 224.0.0.251 组播——不限定「发给本机」
        就会被记成「DNS 走了 sing-box」，排查时直接被误导。"""
        body = self.body(True)
        for line in body.splitlines():
            if 'dertflow dns' in line:
                self.assertIn("fib daddr type local", line)

    def test_flow_chain_function_is_shared(self):
        """flowops 直接用 netgen.flow_chain_lines()，不允许各写一份。"""
        lines = self.ng.flow_chain_lines()
        self.assertTrue(any("chain flowlog" in l for l in lines))
        self.assertTrue(any("dertflow direct" in l for l in lines))


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dert-flow-")
        self.old_fs = os.environ.get("DERT_FSROOT")
        os.environ["DERT_FSROOT"] = self.td
        for m in ("flowops", "netgen"):
            sys.modules.pop(m, None)
        if support.PRIV not in sys.path:
            sys.path.insert(0, support.PRIV)
        self.f = importlib.import_module("flowops")
        self.etc = os.path.join(self.td, "etc/dert")
        os.makedirs(self.etc, exist_ok=True)
        self.cfgp = os.path.join(self.etc, "config.json")
        with open(self.cfgp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "debug": {"flow": False}}, f)
        self.calls = []
        self.present = False
        self.f.log_event = lambda *a, **k: None
        self.f.run = self._run

    def tearDown(self):
        if self.old_fs is None:
            os.environ.pop("DERT_FSROOT", None)
        else:
            os.environ["DERT_FSROOT"] = self.old_fs
        for m in ("flowops", "netgen"):
            sys.modules.pop(m, None)

    def _run(self, args, timeout=15, stdin=None):
        self.calls.append(list(args))
        if args[:3] == ["nft", "list", "chain"]:
            return (0, "chain flowlog { }") if self.present else (1, "")
        if args[:3] == ["nft", "delete", "chain"]:
            self.present = False
            return 0, ""
        if args[:2] == ["nft", "-f"]:
            self.present = True
            return 0, ""
        if args[0] == "journalctl":
            return 0, self.journal
        return 0, ""

    def cfg(self):
        with open(self.cfgp, encoding="utf-8") as f:
            return json.load(f)


class TestFlowConfig(FlowCase):
    def test_default_false(self):
        self.assertFalse(self.f.flow_enabled())

    def test_only_bool_true_counts(self):
        for bad in ("on", "1", 1, "true"):
            with open(self.cfgp, "w", encoding="utf-8") as f:
                json.dump({"debug": {"flow": bad}}, f)
            self.assertFalse(self.f.flow_enabled(),
                             "只有布尔 true 才算开；%r 会让「关闭」变成「开启」" % (bad,))

    def test_write_cfg_atomic_and_mode(self):
        self.f.write_cfg({"version": 1, "debug": {"flow": True}})
        st = os.stat(self.cfgp)
        self.assertEqual(st.st_mode & 0o777, 0o600, "config.json 含宽带密码，权限必须是 600")
        self.assertTrue(self.f.flow_enabled())

    def test_schema_rejects_non_bool(self):
        ng = support.load_netgen()
        cfg = support.pppoe_config()
        cfg["debug"] = {"flow": "on"}
        with self.assertRaises(SystemExit):
            ng.schema_check(cfg)


class TestFlowChainApply(FlowCase):
    def test_on_creates_chain_when_absent(self):
        rc = self.f.main(["on"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.cfg()["debug"]["flow"])
        self.assertTrue(any(c[:2] == ["nft", "-f"] for c in self.calls))
        self.assertFalse(any(c[:3] == ["nft", "delete", "chain"] for c in self.calls),
                         "链不存在时不该先删")

    def test_on_replaces_existing_chain(self):
        self.present = True
        self.f.main(["on"])
        self.assertTrue(any(c[:3] == ["nft", "delete", "chain"] for c in self.calls),
                        "链已存在时必须先删后建，否则 add 会失败")

    def test_off_removes_chain_and_flag(self):
        self.present = True
        with open(self.cfgp, "w", encoding="utf-8") as f:
            json.dump({"debug": {"flow": True}}, f)
        rc = self.f.main(["off"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.cfg()["debug"]["flow"])
        self.assertTrue(any(c[:3] == ["nft", "delete", "chain"] for c in self.calls))

    def test_off_when_absent_is_noop(self):
        rc = self.f.main(["off"])
        self.assertEqual(rc, 0)
        self.assertFalse(any(c[:3] == ["nft", "delete", "chain"] for c in self.calls))

    def test_status_reports_chain_state(self):
        import contextlib
        import io
        self.present = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.f.main(["status"])
        d = json.loads(buf.getvalue())
        self.assertTrue(d["chain_present"])

    def test_chain_file_defines_lan_br(self):
        """$LAN_BR 是文件级 define，nft 不从已存在的表里继承。"""
        self.assertIn('define LAN_BR = "br-lan"', self.f._chain_file())


class TestFlowLogParse(FlowCase):
    journal = (
        "2026-11-18T00:30:12+0800 dert kernel: dertflow direct IN=br-lan OUT= MAC=aa SRC=192.168.10.142 DST=124.70.183.240 LEN=60 PROTO=TCP SPT=51234 DPT=443 WINDOW=64240\n"
        "2026-11-18T00:30:13+0800 dert kernel: dertflow singbox IN=br-lan OUT= MAC=bb SRC=192.168.10.143 DST=198.19.0.6 LEN=60 PROTO=TCP SPT=51235 DPT=443\n"
        "2026-11-18T00:30:14+0800 dert kernel: dertflow dns IN=br-lan OUT= SRC=192.168.10.144 DST=192.168.10.1 PROTO=UDP SPT=5353 DPT=53\n"
        "2026-11-18T00:30:15+0800 dert kernel: something else entirely\n"
    )

    def setUp(self):
        super().setUp()
        self.f.device_map = lambda: {"192.168.10.142": "iPhone"}

    def test_parse_fields(self):
        recs = self.f.parse_log(self.journal)
        self.assertEqual(len(recs), 3, "无关行必须被忽略")
        r = recs[0]
        self.assertEqual(r["path"], "direct")
        self.assertEqual(r["t"], "00:30:12")
        self.assertEqual(r["src"], "192.168.10.142")
        self.assertEqual(r["dst"], "124.70.183.240")
        self.assertEqual(r["dpt"], "443")
        self.assertEqual(r["proto"], "TCP")
        self.assertEqual(r["dev"], "iPhone")

    def test_unknown_device_falls_back_to_empty(self):
        recs = self.f.parse_log(self.journal)
        self.assertEqual(recs[1]["dev"], "")

    def test_show_newest_first(self):
        d = self.f.show(10, True)
        self.assertEqual(d["total"], 3)
        self.assertEqual(d["records"][0]["path"], "dns", "最新的排在最前")

    def test_dns_hidden_by_default(self):
        """45 秒实测 555 行里 383 行是 DNS（69%），默认必须隐藏。"""
        d = self.f.show(10)
        self.assertEqual(d["total"], 2)
        self.assertTrue(all(r["path"] not in ("dns", "dns-sb") for r in d["records"]))

    def test_dns_included_on_request(self):
        d = self.f.show(10, True)
        self.assertEqual(d["total"], 3)

    def test_show_counts_by_path(self):
        d = self.f.show(10, True)
        self.assertEqual(d["counts"]["direct"], 1)
        self.assertEqual(d["counts"]["dns"], 1)


if __name__ == "__main__":
    unittest.main()
