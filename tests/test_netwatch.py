"""netwatch 的 dns_table_stale 判定（审计 §1.1、§5）。

v0.1.139 起表结构固定（`set wl` + `chain pre` + `chain out`），判定简化为
「表在不在、是不是旧结构」——不再读白名单推断「该有 pre 却没有」，那套判断
和 dns-split-sync 重复实现过一遍，两边规则还不一致。

5353 就绪判断也从 UDP 改成 TCP（mDNS 也占 UDP 5353）。
"""
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


TABLE_FIXED = """table inet dert_dns {
    set wl { type ether_addr; elements = { 0c:19:f8:64:7c:ac } }
    chain pre {
        iifname "br-lan" ether saddr @wl meta l4proto { tcp, udp } th dport 53 counter redirect to :5353
    }
    chain out {
        ip daddr 127.0.0.1 meta l4proto { tcp, udp } th dport 53 counter redirect to :5353
    }
}"""

TABLE_OLD_DNAT = """table inet dert_dns {
    chain pre {
        iifname "br-lan" ether saddr { 0c:19:f8:64:7c:ac } meta l4proto { tcp, udp } th dport 53 counter dnat ip to 192.168.10.1:5353
    }
}"""


class DnsTableStale(unittest.TestCase):
    def _run(self, table=None, tcp_listening=True):
        td = tempfile.mkdtemp(prefix="dert-nw-")
        self.addCleanup(shutil.rmtree, td, True)
        bindir = os.path.join(td, "bin")
        os.makedirs(bindir)

        def put(name, body):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        put("ss", '#!/bin/sh\necho "LISTEN 0 128 *:5353 *:*"\nexit 0\n'
                  if tcp_listening else '#!/bin/sh\nexit 0\n')
        put("nft", "#!/bin/sh\ncat <<'T'\n%s\nT\n" % (table or ""))
        put("logger", '#!/bin/sh\nexit 0\n')

        funcs = _extract_funcs(NETWATCH, ["dns_table_stale"])
        env = dict(os.environ)
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        r = subprocess.run(["bash", "-c", funcs + "\ndns_table_stale"],
                           env=env, capture_output=True, text=True, timeout=30)
        return r.returncode   # 0 = stale（需要重建），1 = 不需要

    def test_missing_table_is_stale(self):
        """表不存在（nft 输出为空）→ 需要重建。"""
        self.assertEqual(self._run(table=None), 0)

    def test_fixed_structure_is_not_stale(self):
        """有 set wl 的固定结构 → 不需要重建。"""
        self.assertEqual(self._run(TABLE_FIXED), 1)

    def test_old_dnat_is_stale(self):
        """旧的 dnat 写法（升级前）→ 需要重建。"""
        self.assertEqual(self._run(TABLE_OLD_DNAT), 0)

    def test_no_tcp_listener_is_not_stale(self):
        """5353 没在监听（TCP）→ 不谈重建，直接返回「不 stale」。

        mDNS 占 UDP 5353，所以这里只认 TCP（审计 §5）。
        """
        self.assertEqual(self._run(TABLE_FIXED, tcp_listening=False), 1)


if __name__ == "__main__":
    unittest.main()
