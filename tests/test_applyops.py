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

    def test_mode_drift_triggers_full_deploy(self):
        """只改权限不改内容，也算漂移。

        以前只比内容：有人把 chap-secrets 从 600 改成 644（明文宽带密码），
        收敛会一直报"无漂移"，永远不修。
        """
        target = os.path.join(self.s.td, "etc/dnsmasq.d/dert.conf")
        os.chmod(target, 0o600)
        rc, out = run(self.s.a.apply_all, convergent=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("已按配置完成系统收敛", out["message"])
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o644, "权限没有被修回")

    def test_drift_triggers_full_deploy(self):
        target = os.path.join(self.s.td, "etc/dnsmasq.d/dert.conf")
        w(target, "# 被改坏了\n")
        rc, out = run(self.s.a.apply_all, convergent=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("已按配置完成系统收敛", out["message"])
        with open(target, encoding="utf-8") as f:
            self.assertNotEqual(f.read(), "# 被改坏了\n")
        # 只有 dnsmasq 的配置漂移 → 只重启 dnsmasq。以前这里断言 networkd 也被
        # 重启，那是「无条件全量」的旧行为：代价是 LAN 客户端闪断（还会连带
        # 重拨 PPPoE，见 DriftActions 的注释）。
        self.assertTrue(any("restart dnsmasq.service" in c for c in self.s.calls()))
        self.assertFalse(any("restart systemd-networkd" in c for c in self.s.calls()),
                         "dnsmasq 配置漂移不该重启 networkd")


class DriftActions(unittest.TestCase):
    """全量部署该重启哪些服务，取决于「到底哪些生成物变了」。

    真机 2026-11-20：调试模式开关留下一个 nft 文件的差异，6 小时收敛判定
    漂移后无条件重启 pppoe，把已连 44 小时的宽带打断（WAN 掉线数秒、所有
    设备的连接全断）。代价和原因完全不成比例——所以这里把「nft 差异只重载
    nft」钉死。
    """

    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_none_means_all_actions(self):
        """不知道改了什么（清单读不出来 / 非收敛调用）才退回全量。"""
        self.assertEqual(self.s.a.actions_for(None), self.s.a.ACTIONS)

    def test_nft_only_never_touches_pppoe(self):
        acts = self.s.a.actions_for(["etc/nftables.d/dert-base.nft"])
        self.assertIn("nft", acts)
        for svc in ("pppoe", "networkd", "dnsmasq"):
            self.assertNotIn(svc, acts, "改 nft 不该重启 %s" % svc)

    def test_lan_network_restarts_networkd_only(self):
        acts = self.s.a.actions_for(["etc/systemd/network/20-lan.network"])
        self.assertIn("networkd", acts)
        self.assertNotIn("pppoe", acts, "LAN 侧变化不该把宽带打断")

    def test_wan_network_restarts_pppoe(self):
        for rel in ("etc/systemd/network/10-wan.network",
                    "etc/systemd/network/25-ppp0.network"):
            self.assertIn("pppoe", self.s.a.actions_for([rel]), rel)

    def test_ppp_secrets_restart_pppoe(self):
        for rel in ("etc/ppp/chap-secrets", "etc/ppp/pap-secrets", "etc/ppp/peers/dert"):
            self.assertIn("pppoe", self.s.a.actions_for([rel]), rel)

    def test_dnsmasq_conf_restarts_dnsmasq(self):
        self.assertIn("dnsmasq", self.s.a.actions_for(["etc/dnsmasq.d/dert.conf"]))

    def test_order_preserved(self):
        acts = self.s.a.actions_for([
            "etc/ppp/chap-secrets", "etc/dnsmasq.d/dert.conf",
            "etc/systemd/network/10-wan.network", "etc/nftables.d/dert-base.nft"])
        self.assertEqual(acts, "networkd pppoe dnsmasq nft cake",
                         "重启顺序有依赖，不能被集合顺序打乱")

    def test_manifest_missing_is_none(self):
        self.assertIsNone(self.s.a.drift_list("/nonexistent/manifest.json", "/tmp"))

    def test_drift_detects_content_and_mode(self):
        stage = tempfile.mkdtemp(dir=self.s.td)
        ok, _out, err = self.s.a.generate(stage)
        self.assertTrue(ok, err)
        man = os.path.join(stage, "manifest.json")
        self.s.a.apply_stage_files(stage, man)
        self.assertEqual(self.s.a.drift_list(man, stage), [], "刚部署完不该有漂移")

        nft = os.path.join(self.s.td, "etc/nftables.d/dert-base.nft")
        with open(nft, "a", encoding="utf-8") as f:
            f.write("\n# 手工改一行\n")
        self.assertIn("etc/nftables.d/dert-base.nft", self.s.a.drift_list(man, stage))

        self.s.a.apply_stage_files(stage, man)
        os.chmod(nft, 0o600)
        self.assertIn("etc/nftables.d/dert-base.nft", self.s.a.drift_list(man, stage),
                      "只改权限也算漂移（chap-secrets 从 600 变 644 就是明文密码）")


class FailsafeNft(unittest.TestCase):
    """生成/预检失败时必须保证「有 NAT、WAN 入站是关的」。

    真机核对的结论：nft 规则不跨重启，install.sh 又禁用了 Debian 的
    nftables.service，开机唯一加载 inet dert_base 的就是这条流水线。它在
    svc_nft() 之前退出，设备就同时「上不了网」和「WAN 入站全开」——后者没人会想到。
    """

    def setUp(self):
        self.s = Sandbox()
        self.calls = []
        self.table = False

    def tearDown(self):
        self.s.close()

    def _fake_sh(self, cmd, timeout=60, **kw):
        self.calls.append(list(cmd))

        class R:
            def __init__(self, rc):
                self.returncode = rc
        if cmd[:3] == ["nft", "list", "table"]:
            return R(0 if self.table else 1)
        if cmd[:2] == ["nft", "-f"]:
            self.table = True          # 假装加载成功
            return R(0)
        return R(0)

    def test_noop_when_table_present(self):
        self.table = True
        self.s.a._sh = self._fake_sh
        self.assertEqual(self.s.a.failsafe_nft(), "present")
        self.assertFalse([c for c in self.calls if c[:2] == ["nft", "-f"]],
                         "表还在就不该重装，避免把正在用的规则换成旧版本")

    def test_restores_last_deployed_file(self):
        nft = os.path.join(self.s.td, "etc/nftables.d/dert-base.nft")
        os.makedirs(os.path.dirname(nft), exist_ok=True)
        with open(nft, "w") as f:
            f.write("table inet dert_base {}\n")
        self.s.a._sh = self._fake_sh
        self.assertEqual(self.s.a.failsafe_nft(), "deployed")
        self.assertIn(["nft", "-f", nft], self.calls)

    def test_minimal_when_no_file(self):
        self.s.a._sh = self._fake_sh
        self.assertEqual(self.s.a.failsafe_nft(), "minimal")
        p = os.path.join(self.s.td, "run/dert/failsafe.nft")
        with open(p, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("jump wan_in", body)
        self.assertIn("drop", body, "WAN 入站必须默认拒绝")
        self.assertIn("masquerade", body, "必须有 NAT，否则 LAN 上不了网")
        self.assertIn("flush table inet dert_base", body, "重载要幂等")

    def test_minimal_excludes_tunnel_and_container_ifaces(self):
        """配置读不出来时用通配排除，不能用「!= br-lan 就拦」——
        那会把 tailscale0 / podman 的入站一起拦掉。"""
        self.s.write_cfg({})           # 读得出但不是合法配置
        self.s.a._sh = self._fake_sh
        self.s.a.failsafe_nft()
        with open(os.path.join(self.s.td, "run/dert/failsafe.nft"), encoding="utf-8") as f:
            body = f.read()
        for pat in ("tailscale*", "veth*", "podman*"):
            self.assertIn(pat, body)

    def test_apply_failure_triggers_failsafe(self):
        """端到端：生成失败时确实调了兜底（不是只写在函数里）。"""
        c = self.s.cfg()
        c["wan"]["mode"] = "x"
        self.s.write_cfg(c)
        self.s.a._sh = self._fake_sh
        with self.assertRaises(SystemExit):
            self.s.a.apply_all(convergent=True)
        self.assertTrue(any(c2[:2] == ["nft", "-f"] for c2 in self.calls),
                        "生成失败后必须把防火墙装回去")


class PartialFailure(unittest.TestCase):
    """服务没起来就不能报成功。

    备份恢复 / 恢复出厂都靠 apply-all 的退出码决定是否回滚。以前全量路径把
    run_actions() 的返回值丢掉、wait_dnsmasq() 的结果也不看，于是「恢复了一份
    网口名不匹配的备份」会提示「已恢复并生效」，而 networkd/dnsmasq 根本没起来，
    设备直接失联（审计 RT-06）。
    """

    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_restart_failure_is_not_success(self):
        self.s.rc("systemctl", 1)
        self.s.a.wait_dnsmasq = lambda n: True          # 别真等 15 秒
        rc, out = run(self.s.a.apply_all)
        self.assertEqual(rc, 1, "有服务没起来就不能返回 0")
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("code"), "APPLY_PARTIAL")
        self.assertIn("networkd", out.get("message", ""))
        self.assertTrue(os.path.exists(os.path.join(self.s.td, "run/dert/converge-failed")),
                        "失败要落标记，概览页才显示得出来")

    def test_dnsmasq_not_up_is_not_success(self):
        self.s.a.wait_dnsmasq = lambda n: False
        rc, out = run(self.s.a.apply_all)
        self.assertEqual(rc, 1)
        self.assertIn("dnsmasq", out.get("message", ""))

    def test_all_good_reports_success(self):
        rc, out = run(self.s.a.apply_all)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.get("ok"))
        self.assertFalse(os.path.exists(os.path.join(self.s.td, "run/dert/converge-failed")))


if __name__ == "__main__":
    unittest.main()
