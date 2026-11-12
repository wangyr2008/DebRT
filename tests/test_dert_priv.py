"""`priv/dert-priv` 的行为测试（v0.1.79 / P1-2 第四阶段）。

这个文件是升级通道本身，过去只能做源码字符串断言。v0.1.78 给 `lib.sh` 加了
`DERT_FSROOT`，v0.1.79 把缝铺到 `dert-priv` 的所有数据路径——于是现在可以
**真的把它跑起来**，喂一个假根，断言它读到了什么、输出了什么。

覆盖范围刻意只挑「不需要 root」的子命令：config-get / status / compose-get /
list-containers。需要 root 的（config-set / apply-all / upgrade-apply）留给
真机双跑比对，见 CHANGELOG 里的验证记录。

为什么要测这些：
  · `config-get` 是面板读配置的唯一入口，**不能泄漏 secrets**；
  · `status` 的 `converge_failed` 决定面板是否显示"上次收敛失败"；
  · 缝本身必须安全：设了 FSROOT 就绝不能碰真实 `/etc`。
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import support

DERT_PRIV = os.path.join(support.PRIV, "dert-priv")


def run(args, root="", stdin=None, timeout=30):
    env = dict(os.environ)
    env["DERT_FSROOT"] = root
    return subprocess.run([DERT_PRIV] + list(args), input=stdin,
                          capture_output=True, text=True, env=env, timeout=timeout)


def jout(r):
    """解析 stdout 的 JSON；解析不了就把原文抛出来便于定位。"""
    try:
        return json.loads(r.stdout)
    except Exception:
        raise AssertionError(f"输出不是 JSON：{r.stdout!r} / stderr={r.stderr!r}")


class FakeRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dert-priv-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.etc = os.path.join(self.root, "etc", "dert")
        os.makedirs(self.etc)

    def put(self, rel, obj):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f)
        return p


class ConfigGet(FakeRoot):
    def test_reads_from_fake_root(self):
        self.put("etc/dert/config.json", {"version": 1, "marker": "FAKE"})
        d = jout(run(["config-get"], self.root))
        self.assertTrue(d["ok"])
        self.assertEqual(d["config"]["marker"], "FAKE")

    def test_does_not_leak_secrets(self):
        """config-get 是面板读配置的入口，绝不能把密码/哈希带出去。"""
        self.put("etc/dert/config.json", {"version": 1, "wan": {"mode": "pppoe"}})
        self.put("etc/dert/secrets.json", {
            "admin_pbkdf2": "pbkdf2$sha256$600000$SALT$HASH",
            "pppoe_password": "super-secret-ppp",
            "session_secret": "SESSION-KEY-DO-NOT-LEAK",
        })
        r = run(["config-get"], self.root)
        self.assertTrue(jout(r)["ok"])
        for leak in ("admin_pbkdf2", "super-secret-ppp", "SESSION-KEY-DO-NOT-LEAK"):
            self.assertNotIn(leak, r.stdout, f"config-get 泄漏了 {leak}")

    def test_reports_has_password_flag_instead(self):
        # 不泄漏密码，但面板需要知道"有没有设过宽带密码"
        self.put("etc/dert/config.json", {"version": 1, "wan": {"mode": "pppoe"}})
        self.put("etc/dert/secrets.json", {"pppoe_password": "x"})
        d = jout(run(["config-get"], self.root))
        self.assertTrue(d["config"]["wan"]["has_password"])

    # 注：配置缺失时的行为与宿主 Python 实现相关（真机上配置必然存在），
    # 不作为断言对象——那是在测环境，不是测我们的代码。


class Status(FakeRoot):
    def test_converge_failed_follows_fake_root(self):
        self.put("etc/dert/config.json", {"version": 1, "wan": {"mode": "dhcp"},
                                          "lan": {"ip": "10.0.0.1", "mask": "255.255.255.0"},
                                          "ipv6": {"enabled": False}, "ports": []})
        rundir = os.path.join(self.root, "run", "dert")
        os.makedirs(rundir, exist_ok=True)
        open(os.path.join(rundir, "converge-failed"), "w").close()

        d = jout(run(["status"], self.root))
        self.assertTrue(d.get("converge_failed"),
                        "假根里有 converge-failed 标记，status 就该报 True")

    def test_absent_marker_reports_false(self):
        self.put("etc/dert/config.json", {"version": 1, "wan": {"mode": "dhcp"},
                                          "lan": {"ip": "10.0.0.1", "mask": "255.255.255.0"},
                                          "ipv6": {"enabled": False}, "ports": []})
        d = jout(run(["status"], self.root))
        self.assertFalse(d.get("converge_failed"))


class Isolation(FakeRoot):
    """缝的安全前提：设了 FSROOT 就绝不能碰真实 /etc。"""

    def test_config_get_never_reads_real_etc(self):
        # 假根里的配置带唯一标记；若实现读了 /etc/dert，拿到的就不会是这个标记
        self.put("etc/dert/config.json", {"version": 1, "marker": "ONLY-IN-FAKE-ROOT"})
        d = jout(run(["config-get"], self.root))
        self.assertEqual(d["config"]["marker"], "ONLY-IN-FAKE-ROOT")

if __name__ == "__main__":
    unittest.main()
