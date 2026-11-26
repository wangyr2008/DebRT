"""netwatch 的 dns_table_stale 判定（审计 §C3）。

背景：v0.1.137 的判据只看到表里有 "redirect to :" 就认为表是新的，而 out 链
本身就含这一行。于是「br-lan 晚拿到地址」时表里只有 out 链，pre 链永远补不上，
白名单设备一直拿不到 fakeip，直到下次 sing-box 重启。

这里不跑整个 netwatch（它是常驻 netlink 循环），只提取 dns_table_stale 和
whitelist_macs 两个函数，配一组假命令直接调用。
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import support

NETWATCH = os.path.join(support.PRIV, "netwatch")


def _extract_funcs(path, names):
    """按大括号配对提取 bash 函数（函数体里有嵌套 {}，不能按行截）。"""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    out = []
    for name in names:
        start = src.index("%s() {" % name)
        depth = 0
        for i in range(start, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append(src[start:i + 1])
                    break
    return "\n".join(out)


TABLE_OUT_ONLY = """table inet dert_dns {
    chain out {
        meta skuid 986 return
        ip daddr 127.0.0.1 meta l4proto { tcp, udp } th dport 53 counter redirect to :5353
        ip6 daddr ::1 meta l4proto { tcp, udp } th dport 53 counter redirect to :5353
    }
}"""

TABLE_BOTH = """table inet dert_dns {
    chain pre {
        iifname "br-lan" ether saddr { 0c:19:f8:64:7c:ac } meta l4proto { tcp, udp } th dport 53 counter redirect to :5353
    }
    chain out {
        ip daddr 127.0.0.1 meta l4proto { tcp, udp } th dport 53 counter redirect to :5353
    }
}"""


class DnsTableStale(unittest.TestCase):
    def _run(self, table, lan4=True, whitelist=("0c:19:f8:64:7c:ac",)):
        td = tempfile.mkdtemp(prefix="dert-nw-")
        self.addCleanup(shutil.rmtree, td, True)
        bindir = os.path.join(td, "bin")
        os.makedirs(bindir)

        cfg = os.path.join(td, "sb.json")
        with open(cfg, "w") as f:
            json.dump({"inbounds": [{"type": "tun", "tag": "tun-in",
                                     "include_mac_address": list(whitelist)}]}, f)

        def put(name, body):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        put("ss", '#!/bin/sh\necho "UNCONN 0 0 *:5353 *:*"\n')
        put("nft", "#!/bin/sh\ncat <<'T'\n%s\nT\n" % table)
        put("ip", '#!/bin/sh\necho "    inet 192.168.10.1/24 brd 192.168.10.255 scope global br-lan"\n'
                  if lan4 else '#!/bin/sh\nexit 0\n')
        put("logger", '#!/bin/sh\nexit 0\n')

        funcs = _extract_funcs(NETWATCH, ["whitelist_macs", "dns_table_stale"])
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        env["DERT_SB_CONFIG"] = cfg
        r = subprocess.run(["bash", "-c", funcs + "\ndns_table_stale"],
                           env=env, capture_output=True, text=True, timeout=30)
        # 0 = stale（需要重建），1 = 不需要
        return r.returncode

    def test_out_only_with_lan4_and_whitelist_is_stale(self):
        """核心用例：只有 out 链，但 br-lan 已有 v4、白名单非空 → 必须重建。

        旧判据看到 out 链里的 "redirect to :5353" 就认为表是新的，pre 链
        永远补不上，白名单设备一直拿不到 fakeip。
        """
        self.assertEqual(self._run(TABLE_OUT_ONLY), 0)

    def test_out_only_without_lan4_is_not_stale(self):
        """br-lan 还没有 v4 时 pre 链本来就不该有——不算 stale。"""
        self.assertEqual(self._run(TABLE_OUT_ONLY, lan4=False), 1)

    def test_out_only_without_whitelist_is_not_stale(self):
        """白名单为空时 pre 链不该有——不算 stale。"""
        self.assertEqual(self._run(TABLE_OUT_ONLY, whitelist=()), 1)

    def test_pre_and_out_is_not_stale(self):
        """两条链都在 → 表是新的。"""
        self.assertEqual(self._run(TABLE_BOTH), 1)


if __name__ == "__main__":
    unittest.main()
