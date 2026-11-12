"""`priv/authops.py` 的行为测试（v0.1.83 / P2-3）。

这些是**安全语义**，此前只有源码字符串断言。抽成模块后可以真跑：
喂假根、构造锁定状态、断言返回码与计数变化。

覆盖：
  · 密码正确 / 错误
  · 连续失败 20 次锁 300 秒（全局兜底）
  · 锁定未到期 / 已到期（到期必须清零计数，否则会累积成"错一次就锁"）
  · 状态文件损坏不能导致登录不可用
  · 密码记录损坏要与"密码错误"区分（AUTH_RECORD_BAD）
  · 改密：原密码错 5 次锁、新密码长度、成功后轮换 session_secret
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import support


def load_pwhash():
    if support.PRIV not in sys.path:
        sys.path.insert(0, support.PRIV)
    import pwhash  # noqa: E402
    return pwhash


import sys  # noqa: E402


class Env:
    def __init__(self, password="correct-horse"):
        self.td = tempfile.mkdtemp(prefix="dert-auth-")
        self.root = os.path.join(self.td, "root")
        self.etc = os.path.join(self.root, "etc", "dert")
        self.var = os.path.join(self.root, "var", "lib", "dert")
        os.makedirs(self.etc)
        os.makedirs(self.var)
        self.pw = password
        rec = load_pwhash().hash_new(password)
        with open(os.path.join(self.etc, "secrets.json"), "w") as f:
            json.dump({"admin_pbkdf2": rec, "session_secret": "OLD-SESSION"}, f)

    def run(self, action, stdin="", timeout=60):
        env = dict(os.environ)
        env["DERT_FSROOT"] = self.root
        r = subprocess.run([support.PY, os.path.join(support.PRIV, "authops.py"), action],
                           input=stdin, capture_output=True, text=True,
                           env=env, timeout=timeout)
        try:
            return json.loads(r.stdout), r.returncode
        except Exception:
            raise AssertionError(f"输出不是 JSON：{r.stdout!r} / {r.stderr!r}")

    def state(self, name="auth-state.json"):
        p = os.path.join(self.var, name)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def put_state(self, obj, name="auth-state.json"):
        with open(os.path.join(self.var, name), "w") as f:
            json.dump(obj, f)

    def secrets(self):
        with open(os.path.join(self.etc, "secrets.json")) as f:
            return json.load(f)

    def close(self):
        shutil.rmtree(self.td, True)


class Verify(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.addCleanup(self.e.close)

    def test_correct_password(self):
        d, rc = self.e.run("verify", "correct-horse")
        self.assertTrue(d["ok"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.e.state(), {"fails": 0, "lock_until": 0})

    def test_wrong_password_counts_failure(self):
        d, rc = self.e.run("verify", "nope")
        self.assertFalse(d["ok"])
        self.assertEqual(d["code"], "AUTH_BAD")
        self.assertEqual(rc, 1)
        self.assertEqual(self.e.state()["fails"], 1)

    def test_empty_password(self):
        d, rc = self.e.run("verify", "")
        self.assertEqual(d["code"], "AUTH_EMPTY")
        self.assertEqual(rc, 1)

    def test_locks_after_20_failures(self):
        # 全局兜底限速：阈值放宽到 20，避免被用来把管理员锁在门外（B-18）
        for _ in range(19):
            self.e.run("verify", "nope")
        self.assertEqual(self.e.state()["fails"], 19)
        d, _ = self.e.run("verify", "nope")
        self.assertEqual(d["code"], "AUTH_LOCKED")
        st = self.e.state()
        self.assertGreater(st["lock_until"], int(time.time()))

    def test_locked_rejects_even_correct_password(self):
        self.e.put_state({"fails": 20, "lock_until": int(time.time()) + 120})
        d, _ = self.e.run("verify", "correct-horse")
        self.assertEqual(d["code"], "AUTH_LOCKED")
        self.assertIn("秒后重试", d["message"])

    def test_expired_lock_resets_counter(self):
        """锁过期后必须把 fails 清零，否则跨天累积成「错一次立刻锁 5 分钟」。"""
        self.e.put_state({"fails": 19, "lock_until": int(time.time()) - 10})
        d, _ = self.e.run("verify", "nope")   # 密码错
        self.assertEqual(d["code"], "AUTH_BAD", "过期锁不应立刻再锁")
        self.assertEqual(self.e.state()["fails"], 1, "计数应从 0 重新开始")

    def test_corrupt_state_does_not_break_login(self):
        # P3-1：状态损坏时限速计数可丢，登录不能丢
        with open(os.path.join(self.e.var, "auth-state.json"), "w") as f:
            f.write("{not json")
        d, rc = self.e.run("verify", "correct-horse")
        self.assertTrue(d["ok"])
        self.assertEqual(rc, 0)

    def test_corrupt_record_reports_distinct_code(self):
        # 记录损坏与"密码错误"必须分开，否则排查时误导
        with open(os.path.join(self.e.etc, "secrets.json"), "w") as f:
            json.dump({"admin_pbkdf2": "garbage"}, f)
        d, rc = self.e.run("verify", "correct-horse")
        self.assertEqual(d["code"], "AUTH_RECORD_BAD")
        self.assertEqual(rc, 1)


class PasswdSet(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.addCleanup(self.e.close)

    def body(self, old, new):
        return json.dumps({"old": old, "new": new})

    def test_success_rotates_session_secret(self):
        before = self.e.secrets()["session_secret"]
        d, rc = self.e.run("passwd-set", self.body("correct-horse", "new-password-1"))
        self.assertTrue(d["ok"])
        self.assertEqual(rc, 0)
        after = self.e.secrets()["session_secret"]
        self.assertNotEqual(before, after,
                            "改密后必须轮换 session_secret（被盗 cookie 立即失效，B-17）")
        # 新密码可用
        d2, _ = self.e.run("verify", "new-password-1")
        self.assertTrue(d2["ok"])

    def test_wrong_old_rejected(self):
        d, rc = self.e.run("passwd-set", self.body("wrong", "new-password-1"))
        self.assertEqual(d["code"], "AUTH_BAD")
        self.assertEqual(rc, 1)
        self.assertEqual(self.e.secrets()["session_secret"], "OLD-SESSION",
                         "失败时不能改动 secrets")

    def test_locks_after_5_wrong_old(self):
        for _ in range(4):
            self.e.run("passwd-set", self.body("wrong", "new-password-1"))
        d, _ = self.e.run("passwd-set", self.body("wrong", "new-password-1"))
        self.assertEqual(d["code"], "AUTH_LOCKED")
        st = self.e.state("passwd-fails.json")
        self.assertGreater(st["lock_until"], int(time.time()))

    def test_short_new_password_rejected(self):
        d, _ = self.e.run("passwd-set", self.body("correct-horse", "short"))
        self.assertEqual(d["code"], "PW_LEN")

    def test_success_resets_fail_counter(self):
        """原密码验证通过即清零——否则零散输错累计 5 次也会误触锁。"""
        self.e.put_state({"fails": 3, "lock_until": 0}, "passwd-fails.json")
        self.e.run("passwd-set", self.body("correct-horse", "new-password-1"))
        st = self.e.state("passwd-fails.json")
        self.assertEqual(st["fails"], 0)

    def test_missing_secrets_reports_error(self):
        os.unlink(os.path.join(self.e.etc, "secrets.json"))
        d, rc = self.e.run("passwd-set", self.body("x", "new-password-1"))
        self.assertEqual(d["code"], "NO_SECRETS")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
