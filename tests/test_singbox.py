"""`priv/singbox.py` 的行为测试（v0.1.85 / P2-5）。

sing-box 是"控制面"域（服务开关、订阅地址、自动更新、面板地址），
DebRT 不管它的流量路径。这里用假根 + 假命令验证它**读对了什么、写对了什么**。

重点覆盖三个踩过的坑：
  · `clash_api` 新版在 `services[type=api]`，且 listen/listen_port 是两个字段
  · `sub-set` 的 URL 必须拒绝内网地址
  · `schedule` 写 cron 要保留其它行，只替换 sb update 那一行
"""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import support


def load():
    if support.PRIV not in sys.path:
        sys.path.insert(0, support.PRIV)
    import privlib  # noqa: E402
    import singbox  # noqa: E402
    return privlib, singbox


class Base(unittest.TestCase):
    def setUp(self):
        self.P, self.S = load()
        self.td = tempfile.mkdtemp(prefix="dert-sb-")
        self.addCleanup(shutil.rmtree, self.td, True)
        self.root = os.path.join(self.td, "root")
        self.bin = os.path.join(self.td, "bin")
        for d in ("etc/sing-box", "var/lib/sing-box", "etc/cron.d",
                  "var/lib/dert", "etc/dert"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        os.makedirs(self.bin, exist_ok=True)

        # 让模块内的 _FS / SB / CRON 指向假根。
        # **必须保存原值并在 tearDown 恢复**：这些是模块级变量，不恢复会污染
        # 其它测试模块（privlib 是共享的）。
        self._saved = {
            "S._FS": self.S._FS, "S.SB": self.S.SB,
            "S.SB_VAR": self.S.SB_VAR, "S.CRON": self.S.CRON,
            "P.VAR": self.P.VAR, "P.ETC": self.P.ETC,
            "PATH": os.environ.get("PATH", ""),
        }
        self.S._FS = self.root
        self.S.SB = self.root + "/etc/sing-box"
        self.S.SB_VAR = self.root + "/var/lib/sing-box"
        self.S.CRON = self.root + "/etc/cron.d/sing-box"
        self.P.VAR = self.root + "/var/lib/dert"
        self.P.ETC = self.root + "/etc/dert"

    def tearDown(self):
        sv = self._saved
        self.S._FS = sv["S._FS"]
        self.S.SB = sv["S.SB"]
        self.S.SB_VAR = sv["S.SB_VAR"]
        self.S.CRON = sv["S.CRON"]
        self.P.VAR = sv["P.VAR"]
        self.P.ETC = sv["P.ETC"]
        os.environ["PATH"] = sv["PATH"]

    def fake_bin(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def write(self, rel, content):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def call(self, fn, *a, stdin=None):
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin or "")
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                fn(*a)
        finally:
            sys.stdin = old_stdin
        return json.loads(buf.getvalue())


class Status(Base):
    def setUp(self):
        super().setUp()
        self.fake_bin("sing-box", 'echo "sing-box version 1.14.4"; echo')
        # systemctl show --property=MainPID 要有输出，否则 uptime 取不到
        self.fake_bin("systemctl",
                      'case "$*" in *"MainPID"*) echo 4242 ;; esac; exit 0')
        self.fake_bin("ps", 'echo 3600')
        os.environ["PATH"] = self.bin + os.pathsep + os.environ.get("PATH", "")

    def test_reports_installed_and_active(self):
        self.write("etc/sing-box/config.json", {})
        d = self.call(self.S.act_status)
        self.assertTrue(d["installed"])
        self.assertTrue(d["active"])
        self.assertEqual(d["version"], "1.14.4")
        self.assertEqual(d["uptime_s"], 3600)

    def test_api_controller_from_services_and_port(self):
        """新版把 API 放 services[type=api]，且 listen 与 listen_port 是两个字段。"""
        self.write("etc/sing-box/config.json", {
            "services": [{"type": "api", "listen": "0.0.0.0", "listen_port": 9090}],
        })
        d = self.call(self.S.act_status)
        self.assertEqual(d["dashboard"]["controller"], "0.0.0.0:9090",
                         "只取 listen 会拼出 host:0.0.0.0 死链")

    def test_api_controller_from_legacy_clash_api(self):
        self.write("etc/sing-box/config.json", {
            "experimental": {"clash_api": {"external_controller": "127.0.0.1:9090"}},
        })
        d = self.call(self.S.act_status)
        self.assertEqual(d["dashboard"]["controller"], "127.0.0.1:9090")

    def test_subscription_url_masked(self):
        self.write("etc/sing-box/config.url",
                   "https://sub.example.com/link/abcdefghijklmnop\n")
        d = self.call(self.S.act_status)
        self.assertTrue(d["url_set"])
        self.assertIn("sub.example.com", d["url_masked"])
        self.assertIn("…", d["url_masked"], "路径中段应被省略")
        self.assertNotIn("abcdefghijklmnop", d["url_masked"])

    def test_autoupdate_time_parsed_from_cron(self):
        self.write("etc/cron.d/sing-box",
                   "SHELL=/bin/sh\n0 4 * * * root /usr/local/bin/sb update\n")
        d = self.call(self.S.act_status)
        self.assertTrue(d["autoupdate"]["on"])
        self.assertEqual(d["autoupdate"]["time"], "04:00")

    def test_tailscale_endpoint_detected(self):
        self.write("etc/sing-box/config.json", {
            "endpoints": [{"type": "tailscale", "hostname": "ts-node"}],
        })
        d = self.call(self.S.act_status)
        self.assertTrue(d["endpoint"]["present"])
        self.assertEqual(d["endpoint"]["hostname"], "ts-node")


class SubSet(Base):
    def test_accepts_public_https(self):
        d = self.call(self.S.act_sub_set, stdin=json.dumps({"url": "https://sub.example.com/x"}))
        self.assertTrue(d["ok"])
        self.assertEqual(self.read("etc/sing-box/config.url").strip(),
                         "https://sub.example.com/x")

    def test_strips_spaces(self):
        self.call(self.S.act_sub_set, stdin=json.dumps({"url": " https://a.example.com/x "}))
        self.assertEqual(self.read("etc/sing-box/config.url").strip(),
                         "https://a.example.com/x")

    def test_rejects_non_http(self):
        with self.assertRaises(SystemExit):
            buf = io.StringIO()
            with redirect_stdout(buf):
                old = sys.stdin
                sys.stdin = io.StringIO(json.dumps({"url": "ftp://x/y"}))
                try:
                    self.S.act_sub_set()
                finally:
                    sys.stdin = old
        self.assertEqual(json.loads(buf.getvalue())["code"], "SB_URL_BAD")

    def test_rejects_private_host(self):
        """URL 指向内网必须拒绝——否则路由器会替内网地址发请求。"""
        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(buf):
                old = sys.stdin
                sys.stdin = io.StringIO(json.dumps({"url": "https://192.168.1.1/x"}))
                try:
                    self.S.act_sub_set()
                finally:
                    sys.stdin = old
        self.assertEqual(json.loads(buf.getvalue())["code"], "SB_URL_BAD")

    def test_rejects_empty(self):
        with self.assertRaises(SystemExit):
            self.call(self.S.act_sub_set, stdin=json.dumps({"url": ""}))


class Schedule(Base):
    def test_sets_cron_entry(self):
        d = self.call(self.S.act_schedule, stdin=json.dumps({"time": "04:30"}))
        self.assertEqual(d["time"], "04:30")
        txt = self.read("etc/cron.d/sing-box")
        self.assertIn("30 4 * * * root /usr/local/bin/sb update", txt)
        self.assertIn("SHELL=/bin/sh", txt)

    def test_off_removes_only_our_line(self):
        self.write("etc/cron.d/sing-box",
                   "SHELL=/bin/sh\n5 6 * * * root /usr/bin/other-job\n"
                   "0 4 * * * root /usr/local/bin/sb update\n")
        self.call(self.S.act_schedule, stdin=json.dumps({"time": "off"}))
        txt = self.read("etc/cron.d/sing-box")
        self.assertNotIn("sb update", txt)
        self.assertIn("other-job", txt, "其它任务的 cron 行必须保留")

    def test_off_deletes_file_when_only_ours(self):
        self.write("etc/cron.d/sing-box", "0 4 * * * root /usr/local/bin/sb update\n")
        self.call(self.S.act_schedule, stdin=json.dumps({"time": "off"}))
        self.assertFalse(os.path.exists(os.path.join(self.root, "etc/cron.d/sing-box")))

    def test_rejects_bad_time(self):
        for bad in ("25:00", "04:60", "4:5", "0400", "abc"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    self.call(self.S.act_schedule, stdin=json.dumps({"time": bad}))


class DashSet(Base):
    def test_sets_url(self):
        self.call(self.S.act_dash_set, stdin=json.dumps({"url": "https://dash.example.com"}))
        self.assertEqual(self.read("var/lib/dert/singbox-dash-url").strip(),
                         "https://dash.example.com")

    def test_clear_removes_file(self):
        self.write("var/lib/dert/singbox-dash-url", "https://x\n")
        d = self.call(self.S.act_dash_set, stdin=json.dumps({"url": ""}))
        self.assertIn("已清除", d["message"])
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "var/lib/dert/singbox-dash-url")))

    def test_rejects_non_http(self):
        with self.assertRaises(SystemExit):
            self.call(self.S.act_dash_set, stdin=json.dumps({"url": "javascript:alert(1)"}))


if __name__ == "__main__":
    unittest.main()
