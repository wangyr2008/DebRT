"""`priv/health.py` 的行为测试（v0.1.81 / P4）。

健康检查的价值在于**判据本身**：哪几项算健康、哪几项算不健康。
这些判据每一条都对应一个真机踩过的坑，所以用假命令把坑逐个构造出来断言。
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import support

GUA = "2409:8a5e:a57:a080:4083:aaff:fe47:3121"


def fake(name, body):
    return name, "#!/bin/sh\n" + body + "\nexit 0\n"


class Env:
    """假根 + 假命令。默认全部健康，用例只改自己关心的那一项。"""

    def __init__(self, *, services_up=True, nft_rules=4, v6_target=GUA,
                 listening=True, resolv=True, converge_failed=False):
        self.td = tempfile.mkdtemp(prefix="dert-health-")
        self.root = os.path.join(self.td, "root")
        self.bin = os.path.join(self.td, "bin")
        os.makedirs(os.path.join(self.root, "etc", "dert"))
        os.makedirs(os.path.join(self.root, "run", "dert"))
        os.makedirs(self.bin)

        with open(os.path.join(self.root, "etc", "dert", "config.json"), "w") as f:
            json.dump({"version": 1}, f)
        os.makedirs(os.path.join(self.root, "usr", "local", "lib", "dert"))
        with open(os.path.join(self.root, "usr", "local", "lib", "dert", "VERSION"), "w") as f:
            f.write("0.1.81\n")
        with open(os.path.join(self.root, "etc", "resolv.conf"), "w") as f:
            f.write("nameserver 127.0.0.1\n" if resolv else "nameserver 211.138.164.6\n")
        if converge_failed:
            open(os.path.join(self.root, "run", "dert", "converge-failed"), "w").close()

        # 真机上「规则数」统计的是所有含 "dnat ip" 的行（v4 的 "dnat ip to" 与
        # v6 的 "dnat ip6 to" 都算），总数应为 4。这里按总数生成。
        nft_out = "table inet dert_dns {\n"
        v4 = nft_rules - (1 if v6_target else 0)
        for _ in range(max(v4, 0)):
            nft_out += "        iifname \"br-lan\" dnat ip to 192.168.10.1\n"
        if v6_target:
            nft_out += ("        iifname \"br-lan\" dnat ip6 to [%s]:5353\n" % v6_target)
        nft_out += "}\n"

        fakes = [
            fake("systemctl", 'exit %d' % (0 if services_up else 1)),
            fake("nft", 'printf "%s" "$FAKE_NFT"'),
            fake("ss", 'printf "%s" "$FAKE_SS"'),
            # 按接口返回不同内容：br-lan 与 ppp0 的前缀要能分别断言
            fake("ip", 'case "$*" in *ppp0*) printf "%s" "$FAKE_IP_WAN" ;; '
                       '*) printf "%s" "$FAKE_IP" ;; esac'),
        ]
        for name, body in fakes:
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

        self.env_extra = {
            "FAKE_NFT": nft_out,
            "FAKE_SS": "*:5353 *:*\n" if listening else "*:53 *:*\n",
            "FAKE_IP": ("    inet6 %s/64 scope global dynamic mngtmpaddr\n" % GUA),
            # WAN 默认给一个**不同**的 /64（真机正常态）
            "FAKE_IP_WAN": ("    inet6 2409:8a5e:a54:1fe0:98e6:5c85:448d:46a0/64 "
                            "scope global dynamic mngtmpaddr\n"),
        }

    def run(self):
        env = dict(os.environ)
        env["DERT_FSROOT"] = self.root
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env.update(self.env_extra)
        r = subprocess.run([support.PY, os.path.join(support.PRIV, "health.py")],
                           capture_output=True, text=True, env=env, timeout=60)
        return r

    def report(self):
        r = self.run()
        try:
            return json.loads(r.stdout), r.returncode
        except Exception:
            raise AssertionError(f"输出不是 JSON：{r.stdout!r} / {r.stderr!r}")

    def failed_names(self):
        return set(self.report()[0]["failed"])

    def close(self):
        shutil.rmtree(self.td, True)


class AllHealthy(unittest.TestCase):
    def test_reports_healthy_and_exit_zero(self):
        e = Env()
        self.addCleanup(e.close)
        d, rc = e.report()
        self.assertTrue(d["healthy"], f"应全绿，实际失败：{d['failed']}")
        self.assertEqual(rc, 0)
        self.assertEqual(d["failed"], [])
        self.assertEqual(d["passed"], d["total"])


class DetectsProblems(unittest.TestCase):
    """每一项失败都要被单独识别出来——这些判据各自对应一个真机踩过的坑。"""

    def _failed(self, **kw):
        e = Env(**kw)
        self.addCleanup(e.close)
        d, rc = e.report()
        self.assertFalse(d["healthy"])
        self.assertEqual(rc, 1, "不健康时退出码必须是 1，监控才看得到")
        return e.failed_names()

    def test_service_down(self):
        self.assertIn("service:dnsmasq", self._failed(services_up=False))

    def test_nft_rule_count_wrong(self):
        # 直接写 table 定义是「追加」不是「替换」，真机曾堆到 20 条
        self.assertIn("nft:dert_dns_rules", self._failed(nft_rules=8))

    def test_v6_target_mismatch(self):
        # 前缀变了但 nft 没跟上 → 白名单 v6 DNS 全打向不存在的地址
        self.assertIn("nft:v6_target",
                      self._failed(v6_target="2409:8a5e:dead::1"))

    def test_lan_wan_same_prefix(self):
        """LAN 与 WAN 同前缀必须报红。

        真机 2026-11-14 08:39：运营商委派 PD 是 /60，WAN 的 IA_NA 地址落在
        第一个 /64 里，networkd 又把这个 /64 分给 br-lan。路由表两条相同的
        /64，选路优先 ppp0，发往 LAN 客户端的包全被送进 WAN，LAN 的 v6 全断。
        这种状态从服务状态、nft、DNS 上看都"正常"，只有比对前缀才看得出来。
        """
        e = Env()
        self.addCleanup(e.close)
        # WAN 与 LAN 用同一个 /64
        e.env_extra["FAKE_IP_WAN"] = (
            "    inet6 2409:8a5e:a57:a080:98e6:5c85:448d:46a0/64 "
            "scope global dynamic mngtmpaddr\n")
        d, rc = e.report()
        self.assertIn("ipv6:lan_wan_prefix", d["failed"])
        self.assertEqual(rc, 1)

    def test_5353_not_listening(self):
        self.assertIn("dns:5353_listening", self._failed(listening=False))

    def test_resolv_conf_not_pointing_local(self):
        # pppd 的 usepeerdns 会改写 resolv.conf → 路由器自身绕过 dnsmasq
        self.assertIn("dns:resolv_conf", self._failed(resolv=False))

    def test_converge_failed_marker(self):
        self.assertIn("state:converge_failed", self._failed(converge_failed=True))


class OutputContract(unittest.TestCase):
    def test_has_expected_fields(self):
        e = Env()
        self.addCleanup(e.close)
        d, _ = e.report()
        for k in ("ok", "healthy", "passed", "total", "failed", "checks", "checked_at"):
            self.assertIn(k, d)
        for c in d["checks"]:
            self.assertIn("name", c)
            self.assertIn("ok", c)

    def test_quiet_mode_prints_only_failures(self):
        e = Env(services_up=False)
        self.addCleanup(e.close)
        env = dict(os.environ)
        env["DERT_FSROOT"] = e.root
        env["PATH"] = e.bin + os.pathsep + env.get("PATH", "")
        env.update(e.env_extra)
        r = subprocess.run([support.PY, os.path.join(support.PRIV, "health.py"), "--quiet"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("service:", r.stdout)
        self.assertNotIn("{", r.stdout, "--quiet 不应输出 JSON")


if __name__ == "__main__":
    unittest.main()
