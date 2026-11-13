"""`priv/miscops.py` 的行为测试（v0.1.91 / P2-10）。

这三个动作都不大，但都直接对面板负责：

  * `session-secret`：面板签会话 cookie 用。缺失必须给出可操作错误，
    不能返回空串——空密钥签出的 cookie 谁都能伪造。
  * `config-get`：面板首屏数据。必须**派生** `wan.has_password`（明文密码不在
    config.json 里），并且**不能回显 `notes`**（备注只随备份流转）。
  * `service-ctl`：podman / tailscale 的服务开关，只碰 systemd 单元。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import support


class Sandbox:
    def __init__(self):
        self.td = tempfile.mkdtemp(prefix="dert-misc-")
        self.old = {k: os.environ.get(k) for k in ("DERT_FSROOT", "PATH")}
        os.environ["DERT_FSROOT"] = self.td
        self.bindir = os.path.join(self.td, "bin")
        os.makedirs(self.bindir)
        self.log = os.path.join(self.td, "calls.log")
        self._fake("systemctl")
        self._fake("podman")
        os.environ["PATH"] = self.bindir + ":" + (self.old["PATH"] or "")
        for m in ("miscops", "privlib"):
            sys.modules.pop(m, None)
        if support.PRIV not in sys.path:
            sys.path.insert(0, support.PRIV)
        import miscops
        self.m = miscops
        self.m.privlib.require_root = lambda: None
        self.etc = os.path.join(self.td, "etc/dert")
        os.makedirs(self.etc, exist_ok=True)

    def _fake(self, name):
        p = os.path.join(self.bindir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "%s $*" >> "$FAKELOG"\nexit 0\n'
                    % name)
        os.chmod(p, 0o755)
        os.environ["FAKELOG"] = self.log

    def write(self, name, obj):
        with open(os.path.join(self.etc, name), "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return []

    def close(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for m in ("miscops", "privlib"):
            sys.modules.pop(m, None)
        shutil.rmtree(self.td, ignore_errors=True)


def run(fn, *a):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*a)
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    out = buf.getvalue().strip()
    try:
        return rc, json.loads(out.split("\n")[-1])
    except Exception:
        return rc, out


class SessionSecret(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_returns_secret(self):
        self.s.write("secrets.json", {"session_secret": "abc123"})
        rc, out = run(self.s.m.session_secret)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["secret"], "abc123")

    def test_missing_secrets_is_an_error(self):
        # 返回空串会让面板用空密钥签 cookie——必须报错，不能静默
        rc, out = run(self.s.m.session_secret)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "NO_SECRET")


class ConfigGet(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_derives_has_password(self):
        self.s.write("config.json", {"wan": {"mode": "pppoe"}, "lan": {"ip": "192.168.10.1"}})
        self.s.write("secrets.json", {"pppoe_password": "x"})
        rc, out = run(self.s.m.config_get)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["config"]["wan"]["has_password"])

    def test_has_password_false_without_secret(self):
        self.s.write("config.json", {"wan": {"mode": "dhcp"}})
        rc, out = run(self.s.m.config_get)
        self.assertEqual(rc, 0, out)
        self.assertFalse(out["config"]["wan"]["has_password"])

    def test_notes_not_exposed(self):
        # 备注只随备份流转，不回显给前端
        self.s.write("config.json", {"wan": {"mode": "dhcp"}, "notes": "运维备注"})
        rc, out = run(self.s.m.config_get)
        self.assertNotIn("notes", out["config"])
        self.assertNotIn("运维备注", json.dumps(out, ensure_ascii=False))

    def test_missing_config_is_an_error(self):
        rc, out = run(self.s.m.config_get)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "NO_CONFIG")


class ServiceCtl(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_podman_start(self):
        rc, out = run(self.s.m.service_ctl, "podman", "start")
        self.assertEqual(rc, 0, out)
        calls = self.s.calls()
        self.assertTrue(any("start podman.socket podman.service" in c for c in calls))
        self.assertTrue(any(c.startswith("podman start --all") for c in calls))

    def test_podman_stop_order(self):
        # 先停容器再停服务，否则 podman.socket 先没了会留下孤儿容器
        run(self.s.m.service_ctl, "podman", "stop")
        calls = self.s.calls()
        i_pod = next(i for i, c in enumerate(calls) if c.startswith("podman stop"))
        i_svc = next(i for i, c in enumerate(calls) if "stop podman.service" in c)
        self.assertLess(i_pod, i_svc)

    def test_podman_enable_covers_restart_unit(self):
        # 少了 podman-restart.service，重启后容器不会自动起来
        run(self.s.m.service_ctl, "podman", "enable")
        self.assertTrue(any("podman-restart.service" in c for c in self.s.calls()))

    def test_unknown_action(self):
        rc, out = run(self.s.m.service_ctl, "podman", "explode")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "USAGE")

    def test_unknown_service(self):
        rc, out = run(self.s.m.service_ctl, "nginx", "start")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "USAGE")

    def test_tailscale_not_installed(self):
        os.environ["PATH"] = self.s.bindir   # 没有 tailscale 可执行文件
        rc, out = run(self.s.m.service_ctl, "tailscale", "start")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "TS_NOT_INSTALLED")


if __name__ == "__main__":
    unittest.main()
