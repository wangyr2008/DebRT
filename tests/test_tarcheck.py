"""升级包成员校验的行为测试（v0.1.76）。

这段逻辑是「以 root 解压之前」的最后一道防线，此前**只有源码字符串断言**
（`assert 'if "/" not in p: continue' in src`）——字符串断言证明不了它真的
拦得住路径穿越。抽成 `priv/tarcheck.py` 之后可以用真实恶意成员名来测。

注意：这里用 Python 的 tarfile 构造包含 `../` 的包，因为 GNU tar **创建**时
会自己剥掉 `..`，造不出这种包；但攻击者可以用别的工具造，所以必须测。
"""
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest

import support


class MemberRules(unittest.TestCase):
    def setUp(self):
        self.tc = support.load_tarcheck()

    def test_normal_members_pass(self):
        ok, _ = self.tc.check([
            "priv/dert-priv", "priv/lib.sh", "panel/api.py",
            "config/config.json", "systemd/dert-panel.service",
            "network/ppp/ip-up.d/dert", "singbox/", "VERSION", "README.md",
        ])
        self.assertTrue(ok)

    def test_absolute_path_rejected(self):
        ok, why = self.tc.check(["priv/a", "/etc/passwd"])
        self.assertFalse(ok)
        self.assertIn("危险路径", why)

    def test_dotdot_rejected(self):
        ok, why = self.tc.check(["priv/../../etc/passwd"])
        self.assertFalse(ok)
        self.assertIn("危险路径", why)

    def test_dotdot_in_middle_rejected(self):
        ok, why = self.tc.check(["a/../../b"])
        self.assertFalse(ok)

    def test_unknown_top_dir_rejected(self):
        ok, why = self.tc.check(["signing/dert-signing.pub.pem"])
        self.assertFalse(ok)
        self.assertIn("不认识的条目", why)

    def test_toplevel_file_allowed(self):
        # 顶层文件只能落在包根，无法穿越。**这一条不能收紧**：一旦新版本新增
        # 文档，旧版的白名单会拒掉新包 → 升级通道自我阻塞（v0.1.62 踩过）
        ok, _ = self.tc.check(["NEWFILE.md", "LICENSE", "VERSION"])
        self.assertTrue(ok)

    def test_directory_entries_allowed(self):
        ok, _ = self.tc.check(["priv/", "panel/", "config/"])
        self.assertTrue(ok)

    def test_empty_names_ignored(self):
        ok, _ = self.tc.check(["", "priv/a", ""])
        self.assertTrue(ok)


class EndToEnd(unittest.TestCase):
    """真的造一个恶意 tar，走 `tar -tzf | tarcheck.py` 这条生产路径。"""

    def _run_on_tar(self, members):
        td = tempfile.mkdtemp(prefix="dert-tar-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, True))
        path = os.path.join(td, "evil.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            for name, data in members:
                buf = io.BytesIO(data)
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, buf)
        with open(path, "rb") as f:
            listing = subprocess.run(["tar", "-tzf", path],
                                     capture_output=True, text=True).stdout
        r = subprocess.run([support.PY, os.path.join(support.PRIV, "tarcheck.py")],
                           input=listing, capture_output=True, text=True)
        return r

    def test_clean_tar_passes(self):
        r = self._run_on_tar([("priv/dert-priv", b"x"), ("VERSION", b"1.0")])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_traversal_tar_rejected_with_reason(self):
        r = self._run_on_tar([("priv/../../etc/cron.d/evil", b"* * * * * root id")])
        self.assertEqual(r.returncode, 1)
        payload = json.loads(r.stderr.strip().split("\n")[-1])
        self.assertEqual(payload["code"], "UPG_BAD")
        self.assertIn("危险路径", payload["reason"])
        self.assertIn("etc/cron.d/evil", payload["reason"])


if __name__ == "__main__":
    unittest.main()
