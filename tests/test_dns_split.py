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
    # v0.1.139 起脚本查 TCP（-Hltn）而不是 UDP（-lnu）：mDNS 也占 UDP 5353，
    # 用 UDP 判断会在 sing-box 没就绪时误判。这里按参数区分，模拟真实行为。
    "ss": ('#!/bin/sh\n'
           'case "$*" in\n'
           '  *ltn*) echo "LISTEN 0 128 *:5353 *:*" ;;\n'
           '  *) echo "UNCONN 0 0 *:5353 *:*" ;;\n'
           'esac\nexit 0\n'),
    "logger": '#!/bin/sh\nexit 0\n',
    # v0.1.136：out 链要排除 sing-box 自己的 uid，脚本用 id -u 查
    "id": '#!/bin/sh\n[ "$1" = "-u" ] && echo 986\nexit 0\n',
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

    def test_creates_two_redirect_rules(self):
        """v0.1.130 起用 redirect 而不是 dnat to <LAN 地址>。

        旧写法要把 LAN4/LAN6 算进规则里，而 br-lan 没有全局 v6 时 LAN6 兜底成
        ::1——IPv6 不允许把外部进来的包转发到回环，DNAT 后的包被内核丢掉，
        局域网的 v6 DNS 全部超时（审计 RT-08）。redirect 只改端口、地址由内核
        按入接口选，一类问题从根上没有了。
        """
        rc, log, _ = self.h.run(self.SCRIPT)
        self.assertEqual(rc, 0)
        # 按行尾断言：字符串包含判断会把 "redirect to :5353" 也算成 ":53"
        ends = [l.rstrip() for l in log.splitlines()]
        self.assertEqual(sum(1 for l in ends if l.endswith("redirect to :5353")), 3,
                         "白名单一条 + 本机 v4/v6 各一条")
        # v0.1.140：非白名单劫持移到 dert_base 的 dstnat 链，本表不再有 :53
        self.assertEqual(sum(1 for l in ends if l.endswith("redirect to :53")), 0,
                         "非白名单劫持已移到 dert_base")
        self.assertNotIn("dnat ip to", log)
        self.assertNotIn("dnat ip6 to", log)
        # 只看规则行：注释里提到 "::1 兜底" 是解释历史，不算
        rule_lines = [l for l in log.splitlines() if l.strip().startswith("iifname")]
        self.assertFalse(any("::1" in l for l in rule_lines), "规则里不能再用 ::1 兜底")

    def test_whitelist_goes_to_5353_non_whitelist_to_53(self):
        _, log, _ = self.h.run(self.SCRIPT)
        macs = "0c:19:f8:64:7c:ac,44:59:e3:bb:f4:d6"
        # 白名单写成命名集合的 elements，规则用 @wl 引用
        self.assertIn(f"elements = {{ {macs} }}", log)
        self.assertIn("ether saddr @wl", log)
        self.assertIn("redirect to :5353", log)
        # 非白名单 → 53（dnsmasq → 运营商 → 真实 IP）。
        # 不再写 `ether saddr != @wl`：redirect 是终结判决，白名单已被第一条处理
        # （真机复核：两条都能匹配时，第一条计数 3、第二条 0）。
        # v0.1.140 起这条也移到了 dert_base 的 dstnat 链，本表只剩白名单一条。
        self.assertNotIn("ether saddr != ", log)
        ends = [l.rstrip() for l in log.splitlines()]
        self.assertFalse(any(l.endswith("redirect to :53") for l in ends),
                         "劫持已移到 dert_base（v0.1.140）")

    def test_local_output_chain(self):
        """v0.1.136：本机 DNS 也走 5353，和局域网共用一条路。

        以前用 127.0.0.2 的 dns-self inbound，本机要读两个 nameserver、
        靠 glibc 收 ICMP 端口不可达回落；现在是同一条 nft 链路，
        resolv.conf 只剩 127.0.0.1。
        """
        _, log, _ = self.h.run(self.SCRIPT)
        self.assertIn("chain out {", log)
        # 必须排除 sing-box 自己，否则它的 local DNS server 查 127.0.0.1 会死循环
        self.assertIn("meta skuid 986 return", log)
        # v4 和 v6 各一条（v6 是 dns-self 时代缺的通道）
        self.assertIn("ip  daddr 127.0.0.1", log)
        self.assertIn("ip6 daddr ::1", log)
        # 优先级 dstnat-2（-102）**晚于** sing-box 的 output_udp_icmp（mangle+2 = -148），
        # 所以 sing-box 的链先执行、把本地地址（127.0.0.0/8）return 掉，本机 DNS
        # 才会落到这里。这条依赖是隐式的，写在这里免得以后误改成更早的优先级。
        self.assertIn("type nat hook output priority dstnat - 2", log)
        # skuid 排除必须写在两条 redirect 之前——顺序反了就是死循环
        lines = [l.strip() for l in log.splitlines()]
        i_skuid = next(i for i, l in enumerate(lines) if l.startswith("meta skuid"))
        i_v4 = next(i for i, l in enumerate(lines) if "daddr 127.0.0.1" in l)
        i_v6 = next(i for i, l in enumerate(lines) if "daddr ::1" in l)
        self.assertLess(i_skuid, i_v4, "skuid 排除必须在 v4 redirect 之前")
        self.assertLess(i_skuid, i_v6, "skuid 排除必须在 v6 redirect 之前")

    def test_no_uid_skips_output_chain(self):
        """查不到 sing-box 用户时不建 out 链。

        本机退回 dnsmasq（等于"没有 sing-box"），而不是把 DNS 指向没人监听的
        5353。否则用户被删/改名时本机会静默失去 DNS。
        """
        self.h.close()
        self.h = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(self.h.close)
        self.h._write_bin("id", "#!/bin/sh\nexit 1\n")
        _, log, _ = self.h.run(self.SCRIPT)
        self.assertNotIn("chain out", log)
        # pre 链仍要建：白名单设备的 DNS 不该被这个降级影响
        self.assertIn("chain pre", log)

    def test_mac_is_normalized(self):
        """MAC 来自 sub-store，写法不受控；nft 只认冒号分隔。

        AA-BB-CC-DD-EE-FF / aabb.ccdd.eeff 若不规范化，整张表建不起来，
        而且报错被吞掉、静默回落到 dnsmasq（审计 RT-09）。
        """
        self.h.close()
        self.h = Harness(["AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "0c:19:f8:64:7c:ac"])
        self.addCleanup(self.h.close)
        _, log, _ = self.h.run(self.SCRIPT)
        # 两种写法归一后是同一个 MAC → 去重，集合里只出现一次
        self.assertIn("{ aa:bb:cc:dd:ee:ff,0c:19:f8:64:7c:ac }", log)
        self.assertEqual(log.count("aa:bb:cc:dd:ee:ff"), 1,
                         "只在 set wl 的 elements 里出现一次（规则用 @wl 引用）")

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
    """白名单为空 / 无 LAN4：pre 链仍生成，白名单集合为空。

    v0.1.139 起表结构固定（`set wl` + `chain pre` + `chain out`），不再用
    「组合条件决定建哪条链」——那套判断出过两次问题（审计 §C2、§C3）。
    """

    def test_empty_whitelist_still_has_pre_and_empty_set(self):
        h = Harness([])
        self.addCleanup(h.close)
        rc, log, _ = h.run(os.path.join(support.PRIV, "dns-split-sync"))
        self.assertEqual(rc, 0)
        # set wl 必须存在，但不带 elements（不能写成 elements = { }，那是语法错误）
        self.assertIn("set wl { type ether_addr; }", log)
        self.assertNotIn("elements", log)
        # pre 链仍生成：空集不匹配任何包，查询落到「其余 → :53」
        self.assertIn("chain pre", log)
        self.assertIn("chain out", log)

    def test_no_lan4_still_has_pre(self):
        """br-lan 没有 IPv4 时 pre 链仍生成。

        redirect 找不到目标时内核丢包，但这时客户端本来也连不上路由器，
        不需要单独判断（审计 §1.2）。
        """
        h = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(h.close)
        h._write_bin("ip", "#!/bin/sh\nexit 0\n")   # 任何 ip 查询都返回空
        rc, log, _ = h.run(os.path.join(support.PRIV, "dns-split-sync"))
        self.assertEqual(rc, 0)
        self.assertIn("chain pre", log)
        self.assertIn("chain out", log)


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
        self.assertIn('"server": "127.0.0.1"', text,
                      "landns 应固定指向 127.0.0.1（不依赖 LAN 地址）")
        self.assertIn('"::"', text, "必须监听双栈：白名单设备的 v6 DNS 也要能进来")
        self.assertIn("hijack-dns", text, "5353 入口要 hijack-dns 才能下发 fakeip")
        self.assertIn("landns", text, ".lan 本地解析不能丢（局域网主机名）")
        # nft 把白名单的 { tcp, udp } dport 53 都 DNAT 到 5353，所以监听侧
        # 不能只绑 UDP：只绑 UDP 时 TCP DNS 直接 connection refused，
        # 而且客户端不会自动退回 UDP（真机 2026-11-13 实测 dig +tcp 被拒）。
        self.assertNotEqual(cfg["inbounds"][0].get("network"), "udp",
                            "5353 不能只监听 UDP（nft 把 TCP DNS 也 DNAT 过来了）")
        # v0.1.64 起按源 IP 分流的规则已整体移除（改由 nft 按 MAC 分流）
        self.assertNotIn("source_ip_cidr", text)
        # v0.1.65 起不再读运营商 DNS（非白名单已由 nft 直连 dnsmasq）
        self.assertNotIn("carrier", text.lower())

    def test_fragment_independent_of_lan_address(self):
        """片段不依赖 br-lan 的 IPv4：有地址和没地址生成的字节完全相同。

        landns 改成 127.0.0.1 后，改 LAN IP 不会因为「恰好读不到地址」而让
        片段变化、触发多余的 sing-box 重启（审计 §2.5）。
        """
        h1 = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(h1.close)
        h1.run(os.path.join(support.PRIV, "dns-split-prepare"))
        with open(os.path.join(h1.root, "etc", "sing-box", "00-dert-dns.json"), "rb") as f:
            with_addr = f.read()

        h2 = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(h2.close)
        h2._write_bin("ip", "#!/bin/sh\nexit 0\n")   # 任何 ip 查询都返回空
        h2.run(os.path.join(support.PRIV, "dns-split-prepare"))
        with open(os.path.join(h2.root, "etc", "sing-box", "00-dert-dns.json"), "rb") as f:
            without_addr = f.read()

        self.assertEqual(with_addr, without_addr, "片段不应随 LAN 地址变化")

    def _prepare_with(self, cfg):
        h = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(h.close)
        with open(os.path.join(h.root, "etc", "sing-box", "config.json"), "w") as f:
            json.dump(cfg, f)
        rc, _, _ = h.run(os.path.join(support.PRIV, "dns-split-prepare"))
        self.assertEqual(rc, 0)
        with open(os.path.join(h.root, "etc", "sing-box", "00-dert-dns.json")) as f:
            return json.load(f)

    def test_tailscale_rule_prepended(self):
        """Tailscale 的 v6 必须抢在 bypass 之前（审计 P4 / 复核 §2）。

        config.json 的 bypass 判据是 ip_is_private，覆盖 fc00::/7；Tailscale 的
        fd7a:115c:a1e0::/48 正好在里面，而三条 ts-ep 规则排在 bypass 之后 →
        发往 tailnet 的 v6 包被交回内核、主路由表没这条路由 → 从 WAN 发出去超时。
        本片段的规则会被 sing-box 的目录合并排到**最前面**，所以在这里补一条。
        """
        cfg = self._prepare_with({"endpoints": [{"type": "tailscale", "tag": "ts-ep"}]})
        rules = cfg["route"]["rules"]
        self.assertEqual(rules[0].get("ip_cidr"), ["fd7a:115c:a1e0::/48"])
        self.assertEqual(rules[0].get("outbound"), "ts-ep")
        self.assertLess(rules.index(rules[0]),
                        next(i for i, r in enumerate(rules) if r.get("inbound") == "dns-in"),
                        "这条规则必须排在最前面")

    def test_no_tailscale_no_extra_rule(self):
        """没有 tailscale endpoint 时不能凭空引用一个不存在的 tag——
        那会让 sing-box check 直接失败、起不来。"""
        cfg = self._prepare_with({"inbounds": []})
        for r in cfg["route"]["rules"]:
            self.assertNotIn("fd7a", json.dumps(r), "不该有无主的 tailscale 规则")

    def test_tailscale_tag_is_discovered(self):
        """tag 不能写死：模板里叫什么就用什么。"""
        cfg = self._prepare_with({"endpoints": [{"type": "tailscale", "tag": "my-ts"}]})
        self.assertEqual(cfg["route"]["rules"][0].get("outbound"), "my-ts")

    def test_no_self_inbound(self):
        """v0.1.136：不再生成 dns-self（127.0.0.2）。

        本机 DNS 改由 dert_dns 的 out 链 redirect 到 5353。inbound 和 hijack 规则
        必须同进同退：删 inbound 留规则，sing-box check 会因为引用不存在的
        inbound 失败；删规则留 inbound，则是死代码。
        """
        h = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(h.close)
        rc, _, _ = h.run(os.path.join(support.PRIV, "dns-split-prepare"))
        self.assertEqual(rc, 0)
        p = os.path.join(h.root, "etc", "sing-box", "00-dert-dns.json")
        with open(p) as fh:
            cfg = json.load(fh)

        tags = {i.get("tag") for i in cfg["inbounds"]}
        self.assertNotIn("dns-self", tags, "dns-self 已由 out 链取代")
        self.assertEqual(tags, {"dns-in"}, tags)
        # hijack 规则也只能剩 dns-in，不能有引用不存在 inbound 的残留
        hijacked = {r.get("inbound") for r in cfg["route"]["rules"]
                    if r.get("action") == "hijack-dns"}
        self.assertEqual(hijacked, {"dns-in"}, hijacked)

    def test_whitelist_entry_intact(self):
        """去掉 dns-self 后，白名单入口的 5353/双栈/hijack 一个都不能少。"""
        h = Harness(["0c:19:f8:64:7c:ac"])
        self.addCleanup(h.close)
        rc, _, _ = h.run(os.path.join(support.PRIV, "dns-split-prepare"))
        self.assertEqual(rc, 0)
        p = os.path.join(h.root, "etc", "sing-box", "00-dert-dns.json")
        with open(p) as fh:
            cfg = json.load(fh)
        tags = {i.get("tag"): i for i in cfg["inbounds"]}
        self.assertEqual(tags["dns-in"].get("listen"), "::")
        self.assertEqual(tags["dns-in"].get("listen_port"), 5353)
        inbounds_hijacked = {r.get("inbound") for r in cfg["route"]["rules"]
                             if r.get("action") == "hijack-dns"}
        self.assertEqual(inbounds_hijacked, {"dns-in"}, inbounds_hijacked)


if __name__ == "__main__":
    unittest.main()
