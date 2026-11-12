"""`priv/lib.sh` 的行为测试（v0.1.78 / P1-2 第三阶段）。

为什么先做 lib.sh：`write_atomic` / `snapshot_lastgood` / `restore_lastgood`
是配置事务的核心——「原子写」「失败回滚」全靠它们。这些函数此前没有任何
运行时测试，只有源码字符串断言。

测试缝：`DERT_FSROOT` 未设时为空串，路径与生产**逐字符等价**；设了就指向
临时根，于是这些函数可以在无 root 环境里真跑一遍。
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest

import support


def run(script, root="", stdin=None):
    """在 bash 里 source lib.sh 后执行一段脚本，返回 CompletedProcess。"""
    env = dict(os.environ)
    env["DERT_FSROOT"] = root
    cmd = 'set -u; source "%s/lib.sh"; %s' % (support.PRIV, script)
    return subprocess.run(["bash", "-c", cmd], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60)


class WriteAtomic(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dert-lib-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.td, True))

    def test_writes_content_and_mode(self):
        p = os.path.join(self.td, "sub", "a.conf")
        r = run('printf "hello" | write_atomic "%s" 600' % p)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(p) as f:
            self.assertEqual(f.read(), "hello")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_creates_parent_dir(self):
        p = os.path.join(self.td, "deep", "nested", "x")
        r = run('printf x | write_atomic "%s"' % p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(p))

    def test_no_temp_files_left_behind(self):
        p = os.path.join(self.td, "b.conf")
        run('printf "x" | write_atomic "%s"' % p)
        leftovers = [f for f in os.listdir(self.td) if f.startswith(".dert-tmp")]
        self.assertEqual(leftovers, [], "原子写后不应残留临时文件")

    def test_overwrite_replaces_content(self):
        p = os.path.join(self.td, "c.conf")
        run('printf "old" | write_atomic "%s"' % p)
        run('printf "new" | write_atomic "%s"' % p)
        with open(p) as f:
            self.assertEqual(f.read(), "new")


class Lastgood(unittest.TestCase):
    """快照 / 回滚：配置事务失败后能退回上一份可用配置，全靠这两个函数。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dert-lg-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, True))
        self.etc = os.path.join(self.root, "etc", "dert")
        os.makedirs(self.etc)
        self.cfg = os.path.join(self.etc, "config.json")
        self.sec = os.path.join(self.etc, "secrets.json")
        self.write(self.cfg, '{"v": 1}')
        self.write(self.sec, '{"pw": "x"}')

    def write(self, path, body):
        with open(path, "w") as f:
            f.write(body)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_snapshot_creates_pre_copy(self):
        r = run('snapshot_lastgood t1', self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        p = os.path.join(self.root, "var", "lib", "dert", "lastgood", "config.json.pre-t1")
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(self.read(p), '{"v": 1}')

    def test_restore_brings_back_old_content(self):
        run('snapshot_lastgood t1', self.root)
        self.write(self.cfg, '{"v": 2}')
        r = run('restore_lastgood t1', self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(self.cfg), '{"v": 1}', "应恢复为快照内容")

    def test_restore_missing_snapshot_returns_nonzero(self):
        r = run('restore_lastgood nope', self.root)
        self.assertNotEqual(r.returncode, 0, "没有快照时不能静默成功")

    def test_rotation_keeps_five(self):
        # 轮转：只保留最近 5 份，避免 /var/lib/dert 无限增长
        for i in range(8):
            run('snapshot_lastgood s%d' % i, self.root)
        d = os.path.join(self.root, "var", "lib", "dert", "lastgood")
        n = len([f for f in os.listdir(d) if f.startswith("config.json.pre-")])
        self.assertLessEqual(n, 5, f"快照未轮转，当前 {n} 份")


class PlanForSection(unittest.TestCase):
    """改哪个配置段要重启哪些服务——改错会导致"改了没生效"或"多断一次网"。"""

    EXPECT = {
        "bind": "networkd dnsmasq nft cake",
        "wan": "networkd pppoe dnsmasq nft cake",
        "ipv6": "networkd pppoe dnsmasq nft",
        "lan": "networkd dnsmasq nft cake",
        "dhcp": "dnsmasq",
        "static": "dnsmasq",
        "qos": "cake",
        "firewall": "nft",
        "upgrade": "",
    }

    def test_mapping(self):
        for section, want in self.EXPECT.items():
            with self.subTest(section=section):
                r = run('plan_for_section %s' % section)
                self.assertEqual(r.returncode, 0)
                self.assertEqual(r.stdout.strip(), want)

    def test_unknown_section_falls_back_to_full_set(self):
        # 未知段不能静默什么都不重启——宁可多重启，也不能"改了没生效"
        r = run('plan_for_section nosuch')
        self.assertEqual(r.stdout.strip(), "networkd dnsmasq nft cake")

    def test_ipv6_does_not_restart_cake(self):
        # IPv6 变更与 QoS 无关；带上 cake 会白断一次限速
        r = run('plan_for_section ipv6')
        self.assertNotIn("cake", r.stdout)


class JsonProtocol(unittest.TestCase):
    """面板靠这两个函数的输出通信，字段少一个就白屏。"""

    def test_ok_out_coerces_types(self):
        r = run('ok_out n 42 b true s hello')
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d, {"ok": True, "n": 42, "b": True, "s": "hello"})

    def test_die_emits_code_message_hint(self):
        r = run('die TEST_CODE "出了点问题" "这样做"')
        self.assertEqual(r.returncode, 1)
        d = json.loads(r.stdout)
        self.assertFalse(d["ok"])
        self.assertEqual(d["code"], "TEST_CODE")
        self.assertEqual(d["message"], "出了点问题")
        self.assertEqual(d["hint"], "这样做")

    def test_die_hint_optional(self):
        r = run('die C "m"')
        d = json.loads(r.stdout)
        self.assertEqual(d["hint"], "")


class SeamInert(unittest.TestCase):
    """测试缝必须在生产环境下完全不起作用——这是整个方案的前提。

    用行为断言而不是源码断言：直接看变量解析出来的值。
    """

    def test_unset_root_resolves_to_production_paths(self):
        r = run('echo "$CONFIG_JSON|$DERT_VAR|$DERT_RUN"', root="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),
                         "/etc/dert/config.json|/var/lib/dert|/run/dert")

    def test_empty_root_is_same_as_unset(self):
        # 显式传空串（有些调用方会 export 空值）也必须等价
        r = run('echo "$CONFIG_JSON"', root="")
        self.assertEqual(r.stdout.strip(), "/etc/dert/config.json")


if __name__ == "__main__":
    unittest.main()
