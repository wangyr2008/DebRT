"""`priv/wanq.py` 的行为测试（v0.1.119）。

原则同其他测试：不读真机、不发真包。`run` 被替换成假命令，喂不同的 ping
输出，断言**判定结果**与**状态文件内容**，不看源码文本。

为什么值得单独测：这段逻辑的失效方式是「静默误报」——比如把某个屏蔽 ICMP
的目标算进丢包率，就会天天报 wan_quality_drop；或者把解析失败当成链路好，
真出问题时反而没有事件。两种都不会报错，只会让日志失去意义。
"""
import importlib
import json
import os
import sys
import tempfile
import unittest

import support

PING_OK = """PING 223.5.5.5 (223.5.5.5) 56(84) bytes of data.

--- 223.5.5.5 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 803ms
rtt min/avg/max/mdev = 3.123/4.567/6.789/1.234 ms
"""

PING_LOSS40 = """--- 223.5.5.5 ping statistics ---
5 packets transmitted, 3 received, 40% packet loss, time 4001ms
rtt min/avg/max/mdev = 10.1/320.5/900.2/120.0 ms
"""

PING_DEAD = """--- 2400:3200::1 ping statistics ---
5 packets transmitted, 0 received, 100% packet loss, time 4003ms
"""

PING_SLOW = """--- 223.5.5.5 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 4000ms
rtt min/avg/max/mdev = 190.0/250.0/300.0/10.0 ms
"""


class WanqCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dert-wanq-")
        self.old_fs = os.environ.get("DERT_FSROOT")
        os.environ["DERT_FSROOT"] = self.td
        sys.modules.pop("wanq", None)
        if support.PRIV not in sys.path:
            sys.path.insert(0, support.PRIV)
        self.w = importlib.import_module("wanq")
        self.etc = os.path.join(self.td, "etc/dert")
        os.makedirs(self.etc, exist_ok=True)
        self.write_cfg(ipv6=False)
        self.events = []
        self.w.log_event = lambda ev, **kw: self.events.append((ev, kw))

    def tearDown(self):
        if self.old_fs is None:
            os.environ.pop("DERT_FSROOT", None)
        else:
            os.environ["DERT_FSROOT"] = self.old_fs
        sys.modules.pop("wanq", None)

    def write_cfg(self, ipv6):
        with open(os.path.join(self.etc, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "ipv6": {"enabled": ipv6}}, f)

    def runner(self, v4_out, v6_out, gw4="100.64.0.1", gw6="fe80::1"):
        """假命令：按地址族返回固定输出，`ip route` 返回网关。"""
        def _run(args, timeout=12):
            if args[0] == "ip":
                fam = "-6" if "-6" in args else "-4"
                if fam == "-6":
                    if not gw6:
                        return 0, ""
                    return 0, "default via %s dev ppp0 proto ra metric 1024\n" % gw6
                if not gw4:
                    return 0, ""
                return 0, "default via %s dev ppp0 proto static\n" % gw4
            out = v6_out if "-6" in args else v4_out
            return (0 if out else 1), (out or "")
        return _run

    # ---------- 解析 ----------

    def test_parse_ok(self):
        self.assertEqual(self.w.parse_ping(PING_OK), (5, 5, 0.0, 4.567))

    def test_parse_100_loss_has_no_rtt(self):
        tx, rx, loss, avg = self.w.parse_ping(PING_DEAD)
        self.assertEqual((tx, rx, loss), (5, 0, 100.0))
        self.assertIsNone(avg, "100% 丢包时不该编造 RTT")

    def test_parse_garbage_is_none(self):
        self.assertIsNone(self.w.parse_ping(""))
        self.assertIsNone(self.w.parse_ping("ping: connect: Network is unreachable"))

    def test_parse_zero_transmitted_is_none(self):
        # tx=0 时按 (tx-rx)/tx 会除零；必须判 None 而不是崩
        self.assertIsNone(self.w.parse_ping("0 packets transmitted, 0 received, 0% packet loss"))

    # ---------- 多目标取最好 ----------

    def test_best_of_prefers_lowest_loss(self):
        a = {"target": "1.1.1.1", "loss": 80.0, "avg_ms": 5.0}
        b = {"target": "2.2.2.2", "loss": 0.0, "avg_ms": 40.0}
        self.assertEqual(self.w.best_of([a, b])["target"], "2.2.2.2")

    def test_best_of_tie_breaks_by_rtt(self):
        a = {"target": "1.1.1.1", "loss": 0.0, "avg_ms": 90.0}
        b = {"target": "2.2.2.2", "loss": 0.0, "avg_ms": 10.0}
        self.assertEqual(self.w.best_of([a, b])["target"], "2.2.2.2")

    def test_best_of_all_none(self):
        self.assertIsNone(self.w.best_of([None, None]))

    def test_blocked_target_does_not_poison_result(self):
        """一个目标 100% 丢包、另一个正常 → 判 ok。

        这是本模块最关键的一条：公共 DNS 的 v6 地址普遍屏蔽 ICMP，
        按总和算会把「目标不回」当成「链路丢一半」，天天误报。
        """
        dead = {"target": "2402:4e00::", "loss": 100.0, "avg_ms": None}
        good = {"target": "2400:3200::1", "loss": 0.0, "avg_ms": 20.0}
        self.assertEqual(self.w.judge(self.w.best_of([dead, good])), "ok")

    # ---------- 判定 ----------

    def test_judge_ok(self):
        self.assertEqual(self.w.judge({"loss": 0.0, "avg_ms": 12.0}), "ok")

    def test_judge_loss_boundary(self):
        self.assertEqual(self.w.judge({"loss": 20.0, "avg_ms": 5.0}), "degraded")
        self.assertEqual(self.w.judge({"loss": 19.9, "avg_ms": 5.0}), "ok")

    def test_judge_rtt_boundary(self):
        self.assertEqual(self.w.judge({"loss": 0.0, "avg_ms": 200.0}), "degraded")
        self.assertEqual(self.w.judge({"loss": 0.0, "avg_ms": 199.0}), "ok")

    def test_judge_none_is_off_not_ok(self):
        # 探测失败不能算「好」，否则链路全断时状态反而是 ok
        self.assertEqual(self.w.judge(None), "off")

    # ---------- 网关 ----------

    def test_gateway_v4(self):
        self.assertEqual(self.w.gateway("v4", self.runner(PING_OK, PING_OK)), "100.64.0.1")

    def test_gateway_v6_needs_scope(self):
        """v6 链路本地网关不带 %dev，ping 会直接报 need scope。"""
        gw = self.w.gateway("v6", self.runner(PING_OK, PING_OK))
        self.assertEqual(gw, "fe80::1%ppp0")

    def test_gateway_absent(self):
        self.assertIsNone(self.w.gateway("v4", self.runner(PING_OK, PING_OK, gw4=None)))

    # ---------- 端到端 ----------

    def test_probe_uses_v6_flag(self):
        seen = []

        def _run(args, timeout=12):
            seen.append(args)
            return 0, PING_OK

        self.w.probe_target("v6", "2400:3200::1", _run)
        self.assertIn("-6", seen[0])
        seen.clear()
        self.w.probe_target("v4", "223.5.5.5", _run)
        self.assertIn("-4", seen[0])

    def test_probe_writes_state_and_logs_first_time(self):
        self.w.run = self.runner(PING_OK, "")
        rc = self.w.main(["probe"])
        self.assertEqual(rc, 0)
        st = json.loads(open(self.w.STATE, encoding="utf-8").read())
        self.assertEqual(st["state"], "ok")
        self.assertEqual(st["v4"]["s4"], "ok")
        self.assertEqual(st["v6"]["s6"], "off", "v6 未启用时必须是 off，不能是 ok")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][0], "wan_quality_ok")

    def test_no_event_without_transition(self):
        self.w.run = self.runner(PING_OK, "")
        self.w.main(["probe"])
        self.w.main(["probe"])
        self.w.main(["probe"])
        self.assertEqual(len(self.events), 1, "状态没变却重复写事件，日志会被刷掉")

    def test_degraded_then_recovered_logs_down_seconds(self):
        self.w.run = self.runner(PING_OK, "")
        self.w.main(["probe"])
        self.w.run = self.runner(PING_LOSS40, "")
        self.w.main(["probe"])
        self.assertEqual(self.events[-1][0], "wan_quality_drop")
        self.w.run = self.runner(PING_OK, "")
        self.w.main(["probe"])
        ev, kw = self.events[-1]
        self.assertEqual(ev, "wan_quality_ok")
        self.assertIsNotNone(kw.get("down_s"), "恢复事件要带断了多久")

    def test_slow_rtt_is_degraded(self):
        self.w.run = self.runner(PING_SLOW, "")
        self.w.main(["probe"])
        st = json.loads(open(self.w.STATE, encoding="utf-8").read())
        self.assertEqual(st["state"], "degraded")

    def test_v6_disabled_skips_probing(self):
        seen = []

        def _run(args, timeout=12):
            seen.append(args)
            if args[0] == "ip":
                return 0, "default via 100.64.0.1 dev ppp0\n"
            return 0, PING_OK

        self.w.run = _run
        self.w.main(["probe"])
        v6_pings = [a for a in seen if a[0] == "ping" and "-6" in a]
        self.assertEqual(v6_pings, [], "v6 关闭时不该发 v6 探测包")

    def test_samples_ring_is_capped(self):
        self.w.run = self.runner(PING_OK, "")
        for _ in range(self.w.SAMPLES_KEEP + 5):
            self.w.main(["probe"])
        st = json.loads(open(self.w.STATE, encoding="utf-8").read())
        self.assertEqual(len(st["samples"]), self.w.SAMPLES_KEEP)

    def test_show_returns_state_json(self):
        self.w.run = self.runner(PING_OK, "")
        self.w.main(["probe"])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.w.main(["show"]), 0)
        d = json.loads(buf.getvalue())
        self.assertIn("state", d)
        self.assertIn("samples", d)


if __name__ == "__main__":
    unittest.main()
