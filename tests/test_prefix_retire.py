"""prefix-retire.py 的行为测试：登记/撤销/过期/上限，以及生成的 drop-in 内容。

这些断言对应真机 2026-11-13 的故障：前缀变了但旧前缀没被废弃，macOS 12 一直
拿旧 GUA 当源地址发包 → "IPv6 打不开，重连 Wi-Fi 才好"。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import support


class Retire(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dert-retire-")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        os.makedirs(os.path.join(self.root, "var/lib/dert"), exist_ok=True)
        self.state = os.path.join(self.root, "var/lib/dert/retired-prefixes")
        self.seen = os.path.join(self.root, "var/lib/dert/seen-prefixes")
        self.dropin = os.path.join(
            self.root, "etc/systemd/network/20-lan.network.d/90-retired-prefixes.conf")

    def run_retire(self, *args):
        env = dict(os.environ, DERT_FSROOT=self.root)
        r = subprocess.run([sys.executable, os.path.join(support.PRIV, "prefix-retire.py")] + list(args),
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def dropin_text(self):
        with open(self.dropin, encoding="utf-8") as f:
            return f.read()

    def write_seen(self, items):
        with open(self.seen, "w", encoding="utf-8") as f:
            for ts, p in items:
                f.write("%d %s\n" % (ts, p))

    def test_retires_old_prefix_with_zero_lifetime(self):
        out = self.run_retire("2409:8a5e:a57:8570::/64", "2409:8a5e:a56:a2f1::/64")
        self.assertTrue(out["changed"])
        body = self.dropin_text()
        self.assertIn("Prefix=2409:8a5e:a56:a2f1::/64", body)
        self.assertIn("PreferredLifetimeSec=0", body)
        self.assertIn("ValidLifetimeSec=0", body)
        self.assertIn("Assign=no", body)
        # 当前前缀绝不能被写成"已废弃"——否则客户端会把正在用的地址停掉
        self.assertNotIn("Prefix=2409:8a5e:a57:8570::/64", body)

    def test_idempotent(self):
        self.run_retire("2409:8a5e:a57:8570::/64", "2409:8a5e:a56:a2f1::/64")
        first = self.dropin_text()
        out = self.run_retire("2409:8a5e:a57:8570::/64", "2409:8a5e:a56:a2f1::/64")
        self.assertFalse(out["changed"], "重复调用不该重写 drop-in（会白白重发 RA）")
        self.assertEqual(self.dropin_text(), first)

    def test_same_prefix_returns_is_unretired(self):
        # 运营商把同一段又分回来：必须从"已废弃"里移除，否则同一前缀会同时
        # 以正常和废弃两种身份出现在 RA 里，客户端直接停用。
        self.run_retire("2409:8a5e:a57:8570::/64", "2409:8a5e:a56:a2f1::/64")
        out = self.run_retire("2409:8a5e:a56:a2f1::/64", "2409:8a5e:a57:8570::/64")
        self.assertNotIn("2409:8a5e:a56:a2f1::/64", out["retired"])
        self.assertIn("Prefix=2409:8a5e:a57:8570::/64", self.dropin_text())

    def test_expired_entries_dropped(self):
        old = int(time.time()) - 25 * 3600
        self.write_seen([(old, "2409:8a5e:a50:5b20::/64")])
        out = self.run_retire("2409:8a5e:a57:8570::/64", "2409:8a5e:a56:a2f1::/64")
        self.assertNotIn("2409:8a5e:a50:5b20::/64", out["retired"], "超过 24 小时应淘汰")

    def test_missed_event_self_heals(self):
        """事件漏收也能自愈：见过但已不是当前的前缀会被登记为已废弃。

        真机 2026-11-14：a54:13e0 → a54:1c10 这次换前缀没触发钩子，旧前缀
        没登记，macOS 继续拿它当源地址 → 黑洞。收敛时兜底即可自愈。
        """
        now = int(time.time())
        self.write_seen([(now, "2409:8a5e:a54:13e0::/64"),
                         (now, "2409:8a5e:a54:1c10::/64")])
        out = self.run_retire("2409:8a5e:a54:1c10::/64")   # 故意不传旧前缀
        self.assertIn("2409:8a5e:a54:13e0::/64", out["retired"])
        self.assertNotIn("2409:8a5e:a54:1c10::/64", out["retired"])
        self.assertIn("Prefix=2409:8a5e:a54:13e0::/64", self.dropin_text())

    def test_capped_at_eight(self):
        items = [(int(time.time()) - i, "2409:8a5e:%x::/64" % i) for i in range(12)]
        self.write_seen(items)
        out = self.run_retire("2409:8a5e:a57:8570::/64")
        self.assertLessEqual(len(out["retired"]), 8)

    def test_empty_state_renders_header_only(self):
        out = self.run_retire("2409:8a5e:a57:8570::/64")
        self.assertEqual(out["retired"], [])
        body = self.dropin_text()
        self.assertIn("由 DebRT 生成", body)
        self.assertNotIn("[IPv6Prefix]", body, "没有废弃前缀时不能留空的 section")


if __name__ == "__main__":
    unittest.main()
