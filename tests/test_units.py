"""`applyops.ensure_own_units()` 的行为测试（v0.1.119）。

钉的是一个真机踩到的坑：**新增单元在"引入它的那个版本"里装不上**。
升级时跑的是旧版代码，旧版的通用拷贝循环会把新单元文件落到 /etc（文件在），
但 `enable` 写在新版代码里，而新版要等下一次升级才运行 —— 结果 timer 是
disabled，面板一直显示「暂无数据」，而且不报错。

所以收敛时统一对账一次。这里断言对账的四个性质：只碰白名单、内容相同不重装、
已启用的不重复 enable、daemon-reload 在 enable 之前。
"""
import importlib
import os
import stat
import sys
import tempfile
import unittest

import support

FAKE_SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$FAKELOG"
if [ "$1" = "is-enabled" ]; then
  exit ${FAKE_ENABLED_RC:-1}
fi
exit 0
"""


class UnitsCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dert-units-")
        self.old_fs = os.environ.get("DERT_FSROOT")
        self.old_path = os.environ.get("PATH")
        os.environ["DERT_FSROOT"] = self.td
        sys.modules.pop("applyops", None)
        if support.PRIV not in sys.path:
            sys.path.insert(0, support.PRIV)

        self.bindir = os.path.join(self.td, "bin")
        os.makedirs(self.bindir)
        self.log = os.path.join(self.td, "calls.log")
        os.environ["FAKELOG"] = self.log
        for name in ("systemctl", "logger"):
            p = os.path.join(self.bindir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(FAKE_SYSTEMCTL if name == "systemctl" else "#!/bin/sh\nexit 0\n")
            os.chmod(p, 0o755)
        os.environ["PATH"] = self.bindir + ":" + (self.old_path or "")

        # 假的安装目录：只有 systemd/ 下的单元
        self.lib = os.path.join(self.td, "lib")
        self.unitdir = os.path.join(self.lib, "systemd")
        os.makedirs(self.unitdir)
        self.etc = os.path.join(self.td, "etc/systemd/system")
        os.makedirs(self.etc)

        self.a = importlib.import_module("applyops")
        self.a.LIB = self.lib
        self.a._log = lambda *a, **k: None

    def tearDown(self):
        if self.old_fs is None:
            os.environ.pop("DERT_FSROOT", None)
        else:
            os.environ["DERT_FSROOT"] = self.old_fs
        os.environ["PATH"] = self.old_path or ""
        sys.modules.pop("applyops", None)

    def unit(self, name, body="[Unit]\nDescription=x\n"):
        p = os.path.join(self.unitdir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [l.strip() for l in f if l.strip()]
        except OSError:
            return []

    # ---------- ----------

    def test_copies_and_enables_new_timer(self):
        self.unit("dert-wanq.service")
        self.unit("dert-wanq.timer")
        changed, newly = self.a.ensure_own_units()
        self.assertTrue(changed)
        self.assertIn("dert-wanq.timer", newly)
        self.assertTrue(os.path.isfile(os.path.join(self.etc, "dert-wanq.timer")))
        self.assertIn("systemctl enable dert-wanq.timer", self.calls())
        # service 是 static（want=False），不该被 enable
        self.assertNotIn("systemctl enable dert-wanq.service", self.calls())

    def test_no_reload_when_content_same(self):
        self.unit("dert-wanq.timer")
        self.a.ensure_own_units()
        open(self.log, "w").close()
        changed, _ = self.a.ensure_own_units()
        self.assertFalse(changed, "内容没变还覆盖 + daemon-reload 是纯噪音")
        self.assertNotIn("systemctl daemon-reload", self.calls())

    def test_does_not_reenable_already_enabled(self):
        self.unit("dert-wanq.timer")
        os.environ["FAKE_ENABLED_RC"] = "0"
        try:
            self.a.ensure_own_units()
        finally:
            os.environ.pop("FAKE_ENABLED_RC", None)
        self.assertNotIn("systemctl enable dert-wanq.timer", self.calls())

    def test_reload_precedes_enable(self):
        self.unit("dert-wanq.timer")
        self.a.ensure_own_units()
        calls = self.calls()
        self.assertLess(calls.index("systemctl daemon-reload"),
                        calls.index("systemctl enable dert-wanq.timer"),
                        "先 enable 再 reload 会拿到旧的 unit 缓存")

    def test_ignores_units_outside_whitelist(self):
        self.unit("podman.service")
        self.unit("sing-box.service")
        self.a.ensure_own_units()
        self.assertFalse(os.path.exists(os.path.join(self.etc, "podman.service")),
                         "别人的单元绝不能被覆盖")
        self.assertFalse(os.path.exists(os.path.join(self.etc, "sing-box.service")))

    def test_missing_source_is_skipped(self):
        changed, newly = self.a.ensure_own_units()
        self.assertFalse(changed)
        self.assertEqual(newly, [], "没源文件时不该尝试 enable")

    def test_systemctl_failure_never_raises(self):
        self.unit("dert-wanq.timer")
        # 把 systemctl 换成直接失败的脚本：收敛不能被这一步带崩
        p = os.path.join(self.bindir, "systemctl")
        with open(p, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 127\n")
        os.chmod(p, 0o755)
        self.a.ensure_own_units()   # 不抛就算通过


if __name__ == "__main__":
    unittest.main()
