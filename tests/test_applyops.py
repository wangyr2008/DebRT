"""`priv/applyops.py` 的行为测试（v0.1.87 / P2-7）。

这是**开机路径**：`dert-apply.service` 每次启动跑一遍，`dert-converge.timer`
每 6 小时跑一遍。所以这里的用例重点锁"顺序"和"提前返回"这两类容易在重构中
悄悄变掉的行为：

  1. 无漂移分支**绝不能**重启 networkd/dnsmasq（真机实测：开机全量重启会让
     LAN 客户端闪断），且**必须**删 stage（漏了就把明文 chap-secrets 留在
     /run/dert，真机 2026-11-09 踩到过）。
  2. 生成失败要留 `converge-failed` 标记并把生成器 stderr 摘要带进 hint。
  3. 预检失败要透传 `nft -c` / `dnsmasq --test` 的原文，不能吞成一句"失败"。
  4. 存量 `/26` 掩码要自动迁回 `/24` 后重试，且迁移前留快照。

全部在临时根（DERT_FSROOT）+ 假 systemctl/nft/dnsmasq 里跑，不碰真机。
"""
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import support

FAKE = """#!/bin/sh
n=$(basename "$0")
echo "$n $*" >> "$FAKELOG"
if [ "$n" = systemctl ] && [ "${1:-}" = list-unit-files ]; then exit 1; fi
[ -f "$FAKEDIR/rc.$n" ] && exit "$(cat "$FAKEDIR/rc.$n")"
exit 0
"""


class Sandbox:
    def __init__(self, cfg=None):
        self.td = tempfile.mkdtemp(prefix="dert-apply-")
        self.old_fs = os.environ.get("DERT_FSROOT")
        self.old_path = os.environ.get("PATH")
        os.environ["DERT_FSROOT"] = self.td
        for m in ("applyops", "privlib", "netgen", "configops"):
            sys.modules.pop(m, None)
        if support.PRIV not in sys.path:
            sys.path.insert(0, support.PRIV)

        self.bindir = os.path.join(self.td, "bin")
        os.makedirs(self.bindir)
        self.log = os.path.join(self.td, "calls.log")
        os.environ["FAKELOG"] = self.log
        os.environ["FAKEDIR"] = self.bindir
        for name in ("systemctl", "nft", "dnsmasq", "ip", "logger", "pkill",
                     "wan-addr-sync", "qdisc-apply", "wait-wan", "dns-split-sync"):
            p = os.path.join(self.bindir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        # 真实的生成器与清理器用软链接进来：让整条流水线真跑，而不是打桩
        for name in ("netgen.py", "cleanup.py", "validate.py"):
            os.symlink(os.path.join(support.PRIV, name), os.path.join(self.bindir, name))
        os.environ["PATH"] = self.bindir + ":" + (self.old_path or "")

        self.a = importlib.import_module("applyops")
        self.a.LIB = self.bindir
        self.a.privlib.require_root = lambda: None
        self.etc = os.path.join(self.td, "etc/dert")
        os.makedirs(self.etc, exist_ok=True)
        self.write_cfg(cfg or support.dhcp_config())

    def write_sec(self, sec):
        p = os.path.join(self.td, "etc/dert/secrets.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(sec, f)

    def write_cfg(self, cfg):
        with open(os.path.join(self.etc, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def cfg(self):
        with open(os.path.join(self.etc, "config.json"), encoding="utf-8") as f:
            return json.load(f)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return []

    def clear_calls(self):
        open(self.log, "w").close()

    def rc(self, name, code):
        with open(os.path.join(self.bindir, "rc." + name), "w") as f:
            f.write(str(code))

    def close(self):
        if self.old_fs is None:
            os.environ.pop("DERT_FSROOT", None)
        else:
            os.environ["DERT_FSROOT"] = self.old_fs
        if self.old_path is not None:
            os.environ["PATH"] = self.old_path
        for m in ("applyops", "privlib", "netgen", "configops"):
            sys.modules.pop(m, None)
        shutil.rmtree(self.td, ignore_errors=True)


def w(path, text):
    """写文件（测试里到处 open(...).write(...) 会留下未关闭句柄的告警）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def run(fn, *a, **kw):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*a, **kw)
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    out = buf.getvalue().strip()
    try:
        return rc, json.loads(out.split("\n")[-1])
    except Exception:
        return rc, out


class Preflight(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def _stage(self):
        stage = os.path.join(self.s.td, "stage")
        os.makedirs(os.path.join(stage, "etc/nftables.d"), exist_ok=True)
        os.makedirs(os.path.join(stage, "etc/dnsmasq.d"), exist_ok=True)
        w(os.path.join(stage, "etc/nftables.d/dert-base.nft"), "table inet x {}\n")
        w(os.path.join(stage, "etc/dnsmasq.d/dert.conf"), "port=53\n")
        return stage

    def test_ok(self):
        ok, out = self.s.a.preflight(self._stage())
        self.assertTrue(ok)
        self.assertEqual(out, "")

    def test_nft_syntax_error_passthrough(self):
        self.s.rc("nft", 1)
        ok, out = self.s.a.preflight(self._stage())
        self.assertFalse(ok)
        self.assertEqual(json.loads(out)["code"], "NFT_SYNTAX")

    def test_dnsmasq_syntax_error_passthrough(self):
        self.s.rc("dnsmasq", 1)
        ok, out = self.s.a.preflight(self._stage())
        self.assertFalse(ok)
        self.assertEqual(json.loads(out)["code"], "DNSMASQ_SYNTAX")


class StageDeploy(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_deploy_writes_content_and_mode(self):
        stage = os.path.join(self.s.td, "st")
        os.makedirs(os.path.join(stage, "etc/dert"))
        w(os.path.join(stage, "etc/dert/x.conf"), "hello\n")
        man = os.path.join(stage, "manifest.json")
        w(man, json.dumps({"files": [{"path": "etc/dert/x.conf", "mode": 0o600}]}))
        self.s.a.apply_stage_files(stage, man)
        dest = os.path.join(self.s.td, "etc/dert/x.conf")
        with open(dest, encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello\n")
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o600)

    def test_broken_manifest_refused(self):
        stage = os.path.join(self.s.td, "st")
        os.makedirs(stage)
        man = os.path.join(stage, "manifest.json")
        w(man, "{not json")
        rc, out = run(self.s.a.apply_stage_files, stage, man)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "IO")


class IfupdownCleanup(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_removes_only_managed_port_lines(self):
        p = os.path.join(self.s.td, "etc/network/interfaces")
        os.makedirs(os.path.dirname(p))
        w(p, "auto lo\niface lo inet loopback\n"
          "allow-hotplug enp1s0\niface enp1s0 inet dhcp\n"
          "allow-hotplug wlan9\niface wlan9 inet dhcp\n")
        self.s.a.cleanup_ifupdown()
        with open(p, encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn("enp1s0", body)
        self.assertIn("wlan9", body)   # 非接管网口不动
        self.assertIn("auto lo", body)


class Actions(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def _nft_file(self):
        p = os.path.join(self.s.td, "etc/nftables.d/dert-base.nft")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("table inet dert_base {}\n")

    def test_all_ok(self):
        self._nft_file()
        self.assertEqual(self.s.a.run_actions("networkd pppoe dnsmasq nft cake"), [])
        calls = self.s.calls()
        self.assertTrue(any("restart systemd-networkd" in c for c in calls))
        self.assertTrue(any("restart dnsmasq.service" in c for c in calls))
        self.assertTrue(any(c.startswith("nft -f") for c in calls))
        self.assertTrue(any(c.startswith("qdisc-apply") for c in calls))

    def test_failed_actions_reported(self):
        self._nft_file()
        self.s.rc("nft", 1)
        self.assertIn("nft", self.s.a.run_actions("nft"))

    def test_dnsmasq_upstream_file_precreated(self):
        # servers-file 对 dnsmasq 是启动期 fatal：文件不存在会直接起不来
        self.s.a.run_actions("dnsmasq")
        self.assertTrue(os.path.isfile(os.path.join(self.s.td, "run/dert-dns/upstream.conf")))

    def test_pppoe_mode_enables_and_restarts(self):
        cfg = self.s.cfg()
        cfg["wan"]["mode"] = "pppoe"
        self.s.write_cfg(cfg)
        self.s.clear_calls()
        self.assertEqual(self.s.a.svc_pppoe(), 0)
        calls = self.s.calls()
        self.assertTrue(any("enable --quiet dert-pppoe" in c for c in calls))
        self.assertTrue(any("restart dert-pppoe" in c for c in calls))

    def test_dhcp_mode_disables(self):
        self.s.clear_calls()
        self.assertEqual(self.s.a.svc_pppoe(), 0)
        calls = self.s.calls()
        self.assertTrue(any("disable --quiet dert-pppoe" in c for c in calls))
        self.assertFalse(any("restart dert-pppoe" in c for c in calls))


class FullApply(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_deploys_everything_and_reports_ok(self):
        rc, out = run(self.s.a.apply_all)
        self.assertEqual(rc, 0, out)
        self.assertIn("已按配置完成系统收敛", out["message"])
        # manifest 里的文件必须全部落地
        man = os.path.join(self.s.td, "run/dert")
        self.assertFalse([d for d in os.listdir(man) if d.startswith("stage.")], "stage 未清理")
        self.assertTrue(os.path.isfile(os.path.join(self.s.td, "etc/dnsmasq.d/dert.conf")))
        self.assertFalse(os.path.exists(os.path.join(self.s.td, "run/dert/converge-failed")))

    def test_generate_failure_marks_and_reports(self):
        bad = self.s.cfg()
        bad["ports"] = []
        self.s.write_cfg(bad)
        rc, out = run(self.s.a.apply_all)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "CONFIG_CORRUPT")
        self.assertTrue(os.path.exists(os.path.join(self.s.td, "run/dert/converge-failed")))
        self.assertFalse([d for d in os.listdir(os.path.join(self.s.td, "run/dert"))
                          if d.startswith("stage.")])

    def test_preflight_failure_passthrough(self):
        self.s.rc("nft", 1)
        rc, out = run(self.s.a.apply_all)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "NFT_SYNTAX")   # 原文透传，不包装
        self.assertTrue(os.path.exists(os.path.join(self.s.td, "run/dert/converge-failed")))

    def test_legacy_mask_migrated_then_deployed(self):
        cfg = self.s.cfg()
        cfg["lan"]["mask"] = "255.255.255.192"
        self.s.write_cfg(cfg)
        rc, out = run(self.s.a.apply_all)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.s.cfg()["lan"]["mask"], "255.255.255.0")
        self.assertIn("已自动修正", out["message"])
        # 迁移前必须留快照
        lg = os.path.join(self.s.td, "var/lib/dert/lastgood")
        self.assertTrue([f for f in os.listdir(lg) if f.startswith("config.json.pre-m")])


class Convergent(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        rc, out = run(self.s.a.apply_all)          # 先全量部署一次
        assert rc == 0, out
        self.s.clear_calls()

    def tearDown(self):
        self.s.close()

    def test_no_drift_skips_restarts_and_cleans_stage(self):
        rc, out = run(self.s.a.apply_all, convergent=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("无漂移", out["message"])
        self.assertTrue(out.get("converged"))
        calls = self.s.calls()
        self.assertFalse(any("restart systemd-networkd" in c for c in calls),
                         "无漂移分支不得重启 networkd（开机闪断）")
        self.assertFalse(any("restart dnsmasq" in c for c in calls),
                         "无漂移分支不得 start/restart dnsmasq（与启动排序死锁）")
        self.assertFalse([d for d in os.listdir(os.path.join(self.s.td, "run/dert"))
                          if d.startswith("stage.")], "无漂移提前返回前必须删 stage")

    def test_drift_triggers_full_deploy(self):
        target = os.path.join(self.s.td, "etc/dnsmasq.d/dert.conf")
        w(target, "# 被改坏了\n")
        rc, out = run(self.s.a.apply_all, convergent=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("已按配置完成系统收敛", out["message"])
        with open(target, encoding="utf-8") as f:
            self.assertNotEqual(f.read(), "# 被改坏了\n")
        self.assertTrue(any("restart systemd-networkd" in c for c in self.s.calls()))


if __name__ == "__main__":
    unittest.main()
