"""`priv/backupcheck.py` 的行为测试（v0.1.84 / P2-4）。

这段是恢复前的最后一道校验，而且踩过一类很隐蔽的绕过：

    etc/dert/./secrets.json      ← 绕过「secrets 精确匹配」
    etc/dert//secrets.json       ← 同上

tar 解压时会自动规范化成 `etc/dert/secrets.json`，端到端可以写穿（复审 N-01）。
所以判定必须先 normpath 归一，再比对——这些用例就是锁住它。
"""
import os
import sys
import unittest

import support


def load():
    if support.PRIV not in sys.path:
        sys.path.insert(0, support.PRIV)
    import backupcheck  # noqa: E402
    return backupcheck


class Verdict(unittest.TestCase):
    def setUp(self):
        self.b = load()

    def test_normal_backup_ok(self):
        self.assertEqual(self.b.check([
            "etc/dert/", "etc/dert/config.json", "etc/dert/compose.yaml",
            "var/lib/dert/notes.json",
        ]), "OK")

    def test_secrets_rejected(self):
        self.assertEqual(self.b.check(["etc/dert/config.json",
                                       "etc/dert/secrets.json"]), "SECRETS")

    def test_dot_slash_bypass_rejected(self):
        # 这是真实踩过的绕过：精确匹配漏掉它，而 tar 解压会规范化
        self.assertEqual(self.b.check(["etc/dert/./secrets.json"]), "SECRETS")

    def test_double_slash_bypass_rejected(self):
        self.assertEqual(self.b.check(["etc/dert//secrets.json"]), "SECRETS")

    def test_absolute_path_rejected(self):
        self.assertEqual(self.b.check(["/etc/dert/config.json"]), "BAD")

    def test_dotdot_rejected(self):
        self.assertEqual(self.b.check(["etc/dert/../../etc/shadow"]), "BAD")

    def test_outside_whitelist_rejected(self):
        self.assertEqual(self.b.check(["etc/passwd"]), "BAD")
        self.assertEqual(self.b.check(["var/lib/dert/lastgood/x"]), "BAD")

    def test_notes_json_allowed_but_other_files_not(self):
        self.assertEqual(self.b.check(["var/lib/dert/notes.json"]), "OK")
        self.assertEqual(self.b.check(["var/lib/dert/other.json"]), "BAD")

    def test_empty_listing_is_ok(self):
        # 空包会被后续步骤拒绝（缺 VERSION 之类），这里不重复判
        self.assertEqual(self.b.check([]), "OK")

    def test_trailing_newlines_ignored(self):
        self.assertEqual(self.b.check(["etc/dert/config.json\n", "\n"]), "OK")


class Cli(unittest.TestCase):
    def test_exit_code_reflects_verdict(self):
        import subprocess
        b = load()
        env = dict(os.environ)
        for names, want, rc in (
            (["etc/dert/config.json"], "OK", 0),
            (["etc/dert/secrets.json"], "SECRETS", 1),
            (["etc/passwd"], "BAD", 1),
        ):
            r = subprocess.run([support.PY, os.path.join(support.PRIV, "backupcheck.py")],
                               input="\n".join(names), capture_output=True,
                               text=True, env=env)
            self.assertEqual(r.stdout.strip(), want)
            self.assertEqual(r.returncode, rc)


if __name__ == "__main__":
    unittest.main()
