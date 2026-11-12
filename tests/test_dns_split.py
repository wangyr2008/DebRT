"""DNS 按 MAC 分流的**行为**测试（v0.1.75 / P1-2）。

为什么要有这一层：原来这些结论是用「源码里有没有某个字符串」断言的
（`assert "dnat ip to" in src`），那种断言在重构时会红、但行为可能没变，
而且看不出规则**到底生成了什么**。

这里改用假根 + 假命令把脚本真跑一遍：
  · `DERT_FSROOT` 指向临时目录（脚本里 `FSROOT="${DERT_FSROOT:-}"`，
    未设时与生产路径完全等价）
  · PATH 前置一组假 `systemctl`/`ss`/`ip`/`nft`/`logger`
  · 假 `nft` 把 stdin 原样录下来 —— 那就是脚本**实际会下发**的规则

断言针对录下来的规则文本，不看源码。
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest

import support

FAKE = {
    # sing-box 必须"活着"，否则脚本直接退出
    "systemctl": '#!/bin/sh\n[ "$1" = "is-active" ] && echo active\nexit 0\n',
    # 5353 必须"在监听"，否则脚本等待 10 秒后退出
    "ss": '#!/bin/sh\necho "UNCONN 0 0 *:5353 *:*"\nexit 0\n',
    "logger": '#!/bin/sh\nexit 0\n',
}

FAKE_IP = """#!/bin/sh
# ip -4 addr show dev br-lan  /  ip -6 addr show dev br-lan scope global
case "$*" in
  *"-4"*) echo "    inet 192.168.10.1/24 brd 192.168.10.255 scope global br-lan" ;;
  *"-6"*) echo "    inet6 2409:8a5e:a57:a080:4083:aaff:fe47:3121/64 scope global dynamic mngtmpaddr"
          echo "    inet6 2409:8a5e:a57:a080:1111:2222:3333:4444/64 scope global temporary" ;;
esac
exit 0
"""

# 假 nft：把 stdin 追加到 $NFT_LOG，并记录参数
FAKE_NFT = """#!/bin/sh
printf '=== ARGV: %s\\n' "$*" >> "$NFT_LOG"
if [ "$1" = "-f" ] || [ "$1" = "-" ] || [ "$2" = "-" ]; then
  cat >> "$NFT_LOG"
fi
exit ${NFT_RC:-0}
"""


class Harness:
    """一次性把假根 + 假命令搭好，跑脚本，返回 (rc, nft 记录, 根目录)。"""

    def __init__(self, whitelist, v6=True):
        self.td = tempfile.mkdtemp(prefix="dert-dns-")
        self.root = os.path.join(self.td, "root")
        self.bin = os.path.join(self.td, "bin")
        self.log = os.path.join(self.td, "nft.log")
        os.makedirs(os.path.join(self.root, "etc", "sing-box"))
        os.makedirs(self.bin)
        open(self.log, "w").close()

        for name, body in FAKE.items():
            self._write_bin(name, body)
        self._write_bin("ip", FAKE_IP)
        self._write_bin("nft", FAKE_NFT)

        cfg = {"inbounds": [{"type": "tun", "tag": "tun-in",
                             "include_mac_address": whitelist}]}
        with open(os.path.join(self.root, "etc", "sing-box", "config.json"), "w") as f:
            json.dump(cfg, f)

    def _write_bin(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def run(self, script, env_extra=None):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["DERT_FSROOT"] = self.root
        env["NFT_LOG"] = self.log
        if env_extra:
            env.update(env_extra)
        r = subprocess.run(["bash", script], capture_output=True, text=True,
                           env=env, timeout=60)
        with open(self.log) as f:
            return r.returncode, f.read(), r

    def close(self):
        import shutil
        shutil.rmtree(self.td, True)


class SyncBehavior(unittest.TestCase):
    """dns-split-sync 生成的 nft 规则。"""

    SCRIPT = os.path.join(support.PRIV, "dns-split-sync")

    def setUp(self):
        self.h = Harness(["0c:19:f8:64:7c:ac", "44:59:e3:bb:f4:d6"])
        self.addCleanup(self.h.close)

    def test_creates_four_dnat_rules(self):
        rc, log, _ = self.h.run(self.SCRIPT)
        self.assertEqual(rc, 0)
        self.assertEqual(log.count("dnat ip to"), 2, "v4 应有白名单/非白名单两条")
        self.assertEqual(log.count("dnat ip6 to"), 2, "v6 应有白名单/非白名单两条")

    def test_whitelist_goes_to_5353_non_whitelist_to_53(self):
        _, log, _ = self.h.run(self.SCRIPT)
        macs = "0c:19:f8:64:7c:ac,44:59:e3:bb:f4:d6"
        # 白名单 → 5353（sing-box direct inbound → hijack-dns → fakeip）
        self.assertIn(f'ether saddr {{ {macs} }}', log)
        self.assertIn("dnat ip to 192.168.10.1:5353", log)
        # 非白名单 → br-lan 的 53（dnsmasq → 运营商 → 真实 IP）
        self.assertIn(f'ether saddr != {{ {macs} }}', log)
        self.assertTrue(
            any(l.rstrip().endswith("dnat ip to 192.168.10.1")
                for l in log.split("\n")),
            "非白名单应 DNAT 到 br-lan 的 53（不带端口即 53）")

    def test_v6_target_prefers_dynamic(self):
        _, log, _ = self.h.run(self.SCRIPT)
        self.assertIn("[2409:8a5e:a57:a080:4083:aaff:fe47:3121]:5353", log)
        self.assertNotIn("1111:2222:3333:4444", log, "temporary 隐私地址不能被选为目标")

    def test_atomic_replace_idiom(self):
        # 直接写 table 定义对已存在的表是"追加"而非"替换"（真机堆到 20 条）
        _, log, _ = self.h.run(self.SCRIPT)
        self.assertIn("add table inet dert_dns", log)
        self.assertIn("delete table inet dert_dns", log)
        self.assertLess(log.index("add table inet dert_dns"),
                        log.index("table inet dert_dns {"),
                        "add/delete 必须在建表之前，且同一事务内")

    def test_writes_prefix_baseline(self):
        self.h.run(self.SCRIPT)
        p = os.path.join(self.h.root, "run", "dert", "lan-prefix")
        self.assertTrue(os.path.isfile(p), "应写前缀基线供 90-dert-lan-prefix 比对")
        with open(p) as f:
            self.assertEqual(f.read().strip(), "2409:8a5e:a57:a080::/64")

    def test_failure_falls_back_to_dnsmasq(self):
        # 建表失败必须删表回落，而不是留半张表把 DNS 指到没人听的端口
        _, log, r = self.h.run(self.SCRIPT, {"NFT_RC": "1"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("delete table inet dert_dns", log)


class EmptyWhitelist(unittest.TestCase):
    """白名单为空 → 不建表（全部走 dnsmasq 拿真实 IP）。"""

    def test_no_table_created(self):
        h = Harness([])
        self.addCleanup(h.close)
        rc, log, _ = h.run(os.path.join(support.PRIV, "dns-split-sync"))
        self.assertEqual(rc, 0)
        self.assertNotIn("dnat ip", log, "空白名单不该建任何 DNAT 规则")


class PrepareBehavior(unittest.TestCase):
    """dns-split-prepare 生成的 00-dert-dns.json。"""

    def test_listens_on_5353_with_lan_addr(self):
        h = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(h.close)
        rc, _, _ = h.run(os.path.join(support.PRIV, "dns-split-prepare"))
        self.assertEqual(rc, 0)
        p = os.path.join(h.root, "etc", "sing-box", "00-dert-dns.json")
        self.assertTrue(os.path.isfile(p), "应生成 00-dert-dns.json")
        with open(p) as fh:
            cfg = json.load(fh)
        text = json.dumps(cfg, ensure_ascii=False)
        self.assertIn("5353", text, "白名单 DNS 入口应监听 5353")
        self.assertIn("192.168.10.1", text, "应使用 br-lan 的 v4 地址")
        self.assertIn('"::"', text, "必须监听双栈：白名单设备的 v6 DNS 也要能进来")
        self.assertIn("hijack-dns", text, "5353 入口要 hijack-dns 才能下发 fakeip")
        self.assertIn("landns", text, ".lan 本地解析不能丢（局域网主机名）")
        # v0.1.64 起按源 IP 分流的规则已整体移除（改由 nft 按 MAC 分流）
        self.assertNotIn("source_ip_cidr", text)
        # v0.1.65 起不再读运营商 DNS（非白名单已由 nft 直连 dnsmasq）
        self.assertNotIn("carrier", text.lower())


if __name__ == "__main__":
    unittest.main()
