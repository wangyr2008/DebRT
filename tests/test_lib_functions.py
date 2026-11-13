"""`priv/lib.sh` 与它拆出来的 Python 实现的行为测试（v0.1.78 起，v0.1.88 改写）。

v0.1.88 起 lib.sh 只剩「协议 + 权限 + 锁」：`write_atomic` / `snapshot_lastgood` /
`restore_lastgood` / `plan_for_section` 都搬到了 Python（`privlib.py` /
`configops.py`）。测试跟着搬家，但**断言的东西没变**——原子写不残留临时文件、
快照能回滚、轮转只留 5 份、改哪个段重启哪些服务。

测试缝：`DERT_FSROOT` 未设时为空串，路径与生产**逐字符等价**；设了就指向
临时根，于是这些逻辑可以在无 root 环境里真跑一遍。
"""
import json
import os
import shutil
import stat
import subprocess
import sys
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


def load(root, module):
    """在指定数据根下导入 Python 模块（privlib 在导入时读 DERT_FSROOT）。"""
    os.environ["DERT_FSROOT"] = root
    for m in ("privlib", "applyops", "configops", "netgen"):
        sys.modules.pop(m, None)
    if support.PRIV not in sys.path:
        sys.path.insert(0, support.PRIV)
    return __import__(module)


class AtomicWrite(unittest.TestCase):
    """原子写：同目录临时文件 → fsync → rename，任何时刻磁盘上都只有完整文件。"""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dert-lib-")
        self.addCleanup(lambda: shutil.rmtree(self.td, True))
        self.addCleanup(lambda: os.environ.pop("DERT_FSROOT", None))
        self.p = load(self.td, "privlib")

    def test_writes_content_and_mode(self):
        p = os.path.join(self.td, "sub", "a.conf")
        self.p.write_atomic(p, "hello", 0o600)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_creates_parent_dir(self):
        p = os.path.join(self.td, "deep", "nested", "x")
        self.p.write_atomic(p, "x")
        self.assertTrue(os.path.isfile(p))

    def test_no_temp_files_left_behind(self):
        self.p.write_atomic(os.path.join(self.td, "b.conf"), "x")
        leftovers = [f for f in os.listdir(self.td) if f.startswith(".dert-tmp")]
        self.assertEqual(leftovers, [], "原子写后不应残留临时文件")

    def test_overwrite_replaces_content(self):
        p = os.path.join(self.td, "c.conf")
        self.p.write_atomic(p, "old")
        self.p.write_atomic(p, "new")
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new")

    def test_bytes_variant_keeps_bytes(self):
        # 备份包里的文件按字节落盘，不能过文本编解码
        p = os.path.join(self.td, "raw.bin")
        self.p.write_bytes(p, b"\x00\xff\xfe", 0o600)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), b"\x00\xff\xfe")


class Lastgood(unittest.TestCase):
    """快照 / 回滚：配置事务失败后能退回上一份可用配置，全靠这两个函数。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dert-lg-")
        self.addCleanup(lambda: shutil.rmtree(self.root, True))
        self.addCleanup(lambda: os.environ.pop("DERT_FSROOT", None))
        self.etc = os.path.join(self.root, "etc", "dert")
        os.makedirs(self.etc)
        self.cfg = os.path.join(self.etc, "config.json")
        self.sec = os.path.join(self.etc, "secrets.json")
        self.write(self.cfg, '{"v": 1}')
        self.write(self.sec, '{"pw": "x"}')
        self.c = load(self.root, "configops")

    def write(self, path, body):
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_snapshot_creates_pre_copy(self):
        self.c.snapshot("t1")
        p = os.path.join(self.root, "var", "lib", "dert", "lastgood",
                         "config.json.pre-t1")
        self.assertTrue(os.path.isfile(p))
        self.assertEqual(self.read(p), '{"v": 1}')

    def test_restore_brings_back_old_content(self):
        self.c.snapshot("t1")
        self.write(self.cfg, '{"v": 2}')
        self.assertTrue(self.c.restore("t1"))
        self.assertEqual(self.read(self.cfg), '{"v": 1}', "应恢复为快照内容")
        self.assertEqual(self.read(self.sec), '{"pw": "x"}')

    def test_restore_missing_snapshot_returns_false(self):
        self.assertFalse(self.c.restore("nope"), "没有快照时不能静默成功")

    def test_rotation_keeps_five(self):
        # 轮转：只保留最近 5 份，避免 /var/lib/dert 无限增长
        for i in range(8):
            self.c.snapshot("s%d" % i)
        d = os.path.join(self.root, "var", "lib", "dert", "lastgood")
        n = len([f for f in os.listdir(d) if f.startswith("config.json.pre-")])
        self.assertLessEqual(n, 5, "快照未轮转，当前 %d 份" % n)


class PlanForSection(unittest.TestCase):
    """改哪个配置段要重启哪些服务——改错会导致"改了没生效"或"多断一次网"。"""

    EXPECT = {
        # v0.1.88：bind 补上 pppoe——重绑 WAN 口后 peer 文件里的 nic-<口> 已变，
        # 不重拨 pppd 还握着旧口（这是重构时照出来的既有缺陷）
        "bind": "networkd pppoe dnsmasq nft cake",
        "wan": "networkd pppoe dnsmasq nft cake",
        "ipv6": "networkd pppoe dnsmasq nft",
        "lan": "networkd dnsmasq nft cake",
        "dhcp": "dnsmasq",
        "static": "dnsmasq",
        "qos": "cake",
        "firewall": "nft",
        "upgrade": "",
    }

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="dert-plan-")
        self.addCleanup(lambda: shutil.rmtree(self.td, True))
        self.addCleanup(lambda: os.environ.pop("DERT_FSROOT", None))
        self.c = load(self.td, "configops")

    def test_mapping(self):
        for section, want in self.EXPECT.items():
            with self.subTest(section=section):
                self.assertEqual(self.c.plan_for_section(section), want)

    def test_unknown_section_falls_back_to_full_set(self):
        # 未知段不能静默什么都不重启——宁可多重启，也不能"改了没生效"
        self.assertEqual(self.c.plan_for_section("nosuch"),
                         "networkd dnsmasq nft cake")

    def test_ipv6_does_not_restart_cake(self):
        # IPv6 变更与 QoS 无关；带上 cake 会白断一次限速
        self.assertNotIn("cake", self.c.plan_for_section("ipv6"))


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
