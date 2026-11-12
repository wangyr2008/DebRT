"""陈旧生成物清理（v0.1.71 / P0-2）。

这组用例直接盯住真机踩过的那个故障：
    端口从 LAN 改成 WAN 后，旧的 `30-<口>.network`（内容仍是 `Bridge=br-lan`）
    没被删掉，与 `10-wan.network` 并存 —— 靠字典序先匹配才没把 WAN 口塞进 LAN 桥。

断言方式：构造一个假根目录（`--root` 注入），把「磁盘现状」和「本次 manifest」
摆好，断言 `plan()` 给出的删除候选恰好是预期的那几个。不碰真实 /etc。
"""
import json
import os
import shutil
import tempfile
import unittest

import support

MARK = "# 由 DebRT 生成 — 请勿手工修改\n"


class CleanupCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dert-cleanup-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cleanup = support.load_cleanup()
        self.netdir = os.path.join(self.root, "etc", "systemd", "network")
        os.makedirs(self.netdir)
        os.makedirs(os.path.join(self.root, "etc", "ppp", "peers"))
        os.makedirs(os.path.join(self.root, "etc", "dnsmasq.d"))
        os.makedirs(os.path.join(self.root, "etc", "nftables.d"))

    # -- 辅助 ----------------------------------------------------------
    def put(self, rel, body):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    def manifest(self, paths):
        p = os.path.join(self.root, "manifest.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ok": True, "files": [{"path": r, "mode": 420,
                                              "content_b64": ""} for r in paths]}, f)
        return p

    def plan(self, man_path):
        got = self.cleanup.plan(man_path, root=self.root)
        return sorted(os.path.relpath(p, self.root) for p in got)


class PortRoleChange(CleanupCase):
    def test_lan_to_wan_removes_stale_bridge_file(self):
        # 磁盘上：enp4s0 还留着「加入 br-lan」的旧文件（这就是真机那个故障）
        self.put("etc/systemd/network/30-enp4s0.network",
                 MARK + "[Match]\nName=enp4s0\n\n[Network]\nBridge=br-lan\n")
        # 本次 manifest：enp4s0 已成 WAN，生成器不再产出它的 30-*.network
        man = self.manifest([
            "etc/systemd/network/10-wan.network",
            "etc/systemd/network/20-lan.netdev",
            "etc/systemd/network/20-lan.network",
            "etc/systemd/network/30-enp1s0.network",
        ])
        self.assertEqual(self.plan(man),
                         ["etc/systemd/network/30-enp4s0.network"])

    def test_lan_port_still_in_manifest_is_kept(self):
        self.put("etc/systemd/network/30-enp1s0.network",
                 MARK + "[Match]\nName=enp1s0\n\n[Network]\nBridge=br-lan\n")
        man = self.manifest(["etc/systemd/network/30-enp1s0.network"])
        self.assertEqual(self.plan(man), [])

    def test_removed_port_is_cleaned(self):
        self.put("etc/systemd/network/30-enp9s9.network",
                 MARK + "[Match]\nName=enp9s9\n")
        man = self.manifest(["etc/systemd/network/30-enp1s0.network"])
        self.assertEqual(self.plan(man),
                         ["etc/systemd/network/30-enp9s9.network"])


class PppArtifacts(CleanupCase):
    def test_non_pppoe_removes_dial_artifacts(self):
        for rel in ("etc/ppp/peers/dert", "etc/ppp/chap-secrets", "etc/ppp/pap-secrets"):
            self.put(rel, MARK + "user * pass *\n")
        self.put("etc/systemd/network/25-ppp0.network", MARK + "[Match]\nName=ppp0\n")
        man = self.manifest(["etc/dnsmasq.d/dert.conf"])
        self.assertEqual(self.plan(man), [
            "etc/ppp/chap-secrets",
            "etc/ppp/pap-secrets",
            "etc/ppp/peers/dert",
            "etc/systemd/network/25-ppp0.network",
        ])

    def test_pppoe_keeps_dial_artifacts(self):
        self.put("etc/ppp/peers/dert", MARK + "plugin rp-pppoe.so\n")
        self.put("etc/systemd/network/25-ppp0.network", MARK + "[Match]\nName=ppp0\n")
        man = self.manifest(["etc/ppp/peers/dert",
                             "etc/systemd/network/25-ppp0.network"])
        self.assertEqual(self.plan(man), [])


class SafetyBoundaries(CleanupCase):
    def test_user_file_without_marker_is_kept(self):
        # 用户手写的同名文件（没有生成标记）绝不能被删
        self.put("etc/systemd/network/30-enp4s0.network",
                 "[Match]\nName=enp4s0\n\n[Network]\nDHCP=yes\n")
        man = self.manifest(["etc/systemd/network/10-wan.network"])
        self.assertEqual(self.plan(man), [])

    def test_file_outside_whitelist_is_untouched(self):
        # 同样在 networkd 目录，但不符合生成模式的（备份文件、用户配置）不碰
        self.put("etc/systemd/network/30-enp4s0.network.bak", MARK + "x\n")
        self.put("etc/systemd/network/40-custom.network", MARK + "x\n")
        self.put("etc/dnsmasq.d/my-own.conf", MARK + "x\n")
        man = self.manifest(["etc/systemd/network/10-wan.network"])
        self.assertEqual(self.plan(man), [])

    def test_missing_manifest_deletes_nothing(self):
        self.put("etc/systemd/network/30-enp4s0.network", MARK + "x\n")
        missing = os.path.join(self.root, "nope.json")
        self.assertEqual(self.cleanup.plan(missing, root=self.root), [])

    def test_corrupt_manifest_deletes_nothing(self):
        self.put("etc/systemd/network/30-enp4s0.network", MARK + "x\n")
        bad = self.put("manifest.json", "{not json")
        self.assertEqual(self.cleanup.plan(bad, root=self.root), [])

    def test_manifest_without_files_key_deletes_nothing(self):
        self.put("etc/systemd/network/30-enp4s0.network", MARK + "x\n")
        p = self.put("manifest.json", '{"ok": false, "code": "CONFIG_SCHEMA"}')
        self.assertEqual(self.cleanup.plan(p, root=self.root), [])

    def test_shebang_marker_on_second_line(self):
        # dert-base.nft 首行是 shebang，标记在第二行 —— 也要认出来
        self.put("etc/nftables.d/dert-base.nft",
                 "#!/usr/sbin/nft -f\n" + MARK + "table inet dert_base\n")
        man = self.manifest(["etc/dnsmasq.d/dert.conf"])
        self.assertEqual(self.plan(man), ["etc/nftables.d/dert-base.nft"])


class ApplyMode(CleanupCase):
    def _run(self, args):
        """跑 main()，吞掉它打印的路径（这些用例只关心副作用）。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.cleanup.main(args)
        return rc, buf.getvalue()

    def test_apply_removes_and_reports(self):
        p = self.put("etc/systemd/network/30-enp4s0.network", MARK + "x\n")
        man = self.manifest(["etc/systemd/network/10-wan.network"])
        rc, out = self._run([man, "--root", self.root, "--apply"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(p), "带 --apply 应真的删掉")
        self.assertIn("30-enp4s0.network", out, "应把删除的路径报出来供 shell 记日志")

    def test_default_mode_only_reports(self):
        p = self.put("etc/systemd/network/30-enp4s0.network", MARK + "x\n")
        man = self.manifest(["etc/systemd/network/10-wan.network"])
        rc, out = self._run([man, "--root", self.root])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(p), "不带 --apply 不能删任何东西")
        self.assertIn("30-enp4s0.network", out, "观察期应能看到候选清单")


if __name__ == "__main__":
    unittest.main()
