"""`priv/status.py` / `priv/selftest.py` 的行为测试（v0.1.80 / P2-1）。

这两个模块从 `dert-priv` 里抽出来（原本是嵌在 bash heredoc 里的纯 Python）。
抽出的直接好处：现在可以喂假根 + 假命令真跑一遍，断言它**输出什么**，
而不是断言「源码里有没有某个字符串」。

用假 `ip` 控制网络状态，用 `DERT_FSROOT` 控制配置与 /run 标记。
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import support

FAKE_IP = """#!/bin/sh
# 用 $FAKE_ROUTE_JSON / $FAKE_ADDR_JSON 控制输出，方便构造场景
case "$*" in
  *"route show default"*) printf '%s' "${FAKE_ROUTE_JSON:-[]}" ;;
  *"route show dev"*)     printf '%s' "${FAKE_DEVROUTE_JSON:-[]}" ;;
  *"-4 addr show"*)       printf '%s' "${FAKE_ADDR_JSON:-[]}" ;;
  *"-6 addr show"*)       printf '%s' "${FAKE_ADDR6_JSON:-[]}" ;;
  *"route show type unreachable"*) printf '%s' "${FAKE_UNREACH:-}" ;;
  *)                      printf '[]' ;;
esac
exit 0
"""


class Env:
    def __init__(self, cfg, run_markers=(), route=None, addr=None, unreach=""):
        self.td = tempfile.mkdtemp(prefix="dert-status-")
        self.root = os.path.join(self.td, "root")
        self.bin = os.path.join(self.td, "bin")
        os.makedirs(os.path.join(self.root, "etc", "dert"))
        os.makedirs(os.path.join(self.root, "run", "dert"))
        os.makedirs(self.bin)
        with open(os.path.join(self.root, "etc", "dert", "config.json"), "w") as f:
            json.dump(cfg, f)
        for m in run_markers:
            open(os.path.join(self.root, "run", "dert", m), "w").close()
        p = os.path.join(self.bin, "ip")
        with open(p, "w") as f:
            f.write(FAKE_IP)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.route = json.dumps(route or [])
        self.addr = json.dumps(addr or [])
        self.unreach = unreach

    def upstream(self, text):
        """写 /run/dert-dns/upstream.conf（dispatcher 维护的上游清单）。"""
        d = os.path.join(self.root, "run", "dert-dns")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "upstream.conf"), "w") as f:
            f.write(text)

    def run(self, mod, env_extra=None):
        env = dict(os.environ)
        env["DERT_FSROOT"] = self.root
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["FAKE_ROUTE_JSON"] = self.route
        env["FAKE_ADDR_JSON"] = self.addr
        env["FAKE_UNREACH"] = self.unreach
        if env_extra:
            env.update(env_extra)
        r = subprocess.run([support.PY, os.path.join(support.PRIV, mod)],
                           capture_output=True, text=True, env=env, timeout=60)
        return r

    def json(self, mod, **kw):
        r = self.run(mod, **kw)
        try:
            return json.loads(r.stdout)
        except Exception:
            raise AssertionError(f"{mod} 输出不是 JSON：{r.stdout!r} / {r.stderr!r}")

    def close(self):
        shutil.rmtree(self.td, True)


def base_cfg(**over):
    cfg = {
        "version": 1,
        "ports": [{"name": "enp1s0", "wan": True, "lan": False},
                  {"name": "enp2s0", "wan": False, "lan": True}],
        "wan": {"mode": "dhcp"},
        "ipv6": {"enabled": False, "mode": "native"},
        "lan": {"ip": "192.168.10.1", "mask": "255.255.255.0"},
        "dhcp": {"enabled": True, "pool_start": 100, "pool_end": 200, "lease": 120},
        "static": [], "qos": {},
        "firewall": {"wan_input_drop": True, "portfw": []},
        "upgrade": {"source": ""},
    }
    cfg.update(over)
    return cfg


class StatusShape(unittest.TestCase):
    def setUp(self):
        self.e = Env(base_cfg())
        self.addCleanup(self.e.close)

    def test_returns_ok_with_expected_keys(self):
        d = self.e.json("status.py")
        self.assertTrue(d["ok"])
        for k in ("wan", "ipv6", "ports", "lan", "version",
                  "converge_failed", "disk_temp", "leases_count"):
            self.assertIn(k, d)

    def test_wan_down_when_no_default_route(self):
        d = self.e.json("status.py")
        self.assertEqual(d["wan"]["state"], "down")
        self.assertEqual(d["wan"]["ip"], "")

    def test_wan_up_with_route_and_addr(self):
        e = Env(base_cfg(), route=[{"dev": "enp1s0", "gateway": "192.168.1.1"}],
                addr=[{"addr_info": [{"local": "100.64.1.2", "prefixlen": 24}]}])
        self.addCleanup(e.close)
        d = e.json("status.py")
        self.assertEqual(d["wan"]["state"], "up")
        self.assertEqual(d["wan"]["ip"], "100.64.1.2")
        self.assertEqual(d["wan"]["mask"], "255.255.255.0")
        self.assertEqual(d["wan"]["gw"], "192.168.1.1")

    def test_pppoe_connecting_flag(self):
        e = Env(base_cfg(wan={"mode": "pppoe"}))
        self.addCleanup(e.close)
        d = e.json("status.py")
        self.assertTrue(d["wan"]["connecting"],
                        "PPPoE 且没有默认路由时，应告诉面板「正在连接」而不是「断了」")

    def test_converge_failed_marker(self):
        e = Env(base_cfg(), run_markers=("converge-failed",))
        self.addCleanup(e.close)
        self.assertTrue(e.json("status.py")["converge_failed"])

    def test_nat6_shows_ula(self):
        e = Env(base_cfg(ipv6={"enabled": True, "mode": "nat6"}))
        self.addCleanup(e.close)
        d = e.json("status.py")
        self.assertIn("ULA", d["ipv6"]["addr"],
                      "NAT6 语义是 LAN 侧 ULA + 出向伪装，概览应显示 ULA")

    def test_ports_reflect_roles(self):
        d = self.e.json("status.py")
        roles = {p["name"]: p["role"] for p in d["ports"]}
        self.assertEqual(roles["enp1s0"], "wan")
        self.assertEqual(roles["enp2s0"], "lan")


class Selftest(unittest.TestCase):
    def test_reports_checks_and_summary(self):
        e = Env(base_cfg())
        self.addCleanup(e.close)
        d = e.json("selftest.py")
        self.assertIn("checks", d)
        self.assertTrue(d["checks"], "自检至少要给出几条")
        for c in d["checks"]:
            self.assertIn("name", c)
            self.assertIn("ok", c)
        self.assertIn("summary", d)

    def test_config_check_passes_with_valid_config(self):
        e = Env(base_cfg())
        self.addCleanup(e.close)
        d = e.json("selftest.py")
        hit = [c for c in d["checks"] if "config.json" in c["name"]]
        self.assertTrue(hit and hit[0]["ok"],
                        "配置存在且可解析时，该项自检必须为真")


class WanDns(unittest.TestCase):
    """概览「DNS 服务器」的取值。

    真机 2026-11-19：这一栏一直是 —。根因不在解析逻辑，而在**谁去读文件**——
    面板以 dert 用户运行，直接 open(/etc/dert/config.json) 抛 PermissionError，
    被 except 吞掉后返回空列表，不报错。现在由 status.py（root）负责取，
    所以这些用例同时钉住「值对」和「不需要读配置也能给出值」。
    """

    def test_auto_mode_reads_upstream_conf(self):
        e = Env(base_cfg(wan={"mode": "pppoe", "dns_mode": "auto"}))
        self.addCleanup(e.close)
        e.upstream("# 由 networkd-dispatcher 生成\n"
                   "server=211.138.164.6\n"
                   "server=221.176.88.95\n"
                   "server=2409:805e:2000::8888\n")
        d = e.json("status.py")
        self.assertEqual(d["wan"]["dns"],
                         ["211.138.164.6", "221.176.88.95", "2409:805e:2000::8888"])

    def test_auto_mode_dedupes(self):
        e = Env(base_cfg())
        self.addCleanup(e.close)
        e.upstream("server=1.1.1.1\nserver=1.1.1.1\n")
        self.assertEqual(e.json("status.py")["wan"]["dns"], ["1.1.1.1"])

    def test_missing_file_is_empty_not_error(self):
        e = Env(base_cfg())
        self.addCleanup(e.close)
        d = e.json("status.py")
        self.assertTrue(d["ok"])
        self.assertEqual(d["wan"]["dns"], [], "文件不存在时给空列表，不能让整个 status 失败")

    def test_manual_mode_uses_config(self):
        e = Env(base_cfg(wan={"mode": "pppoe", "dns_mode": "manual",
                              "dns": ["114.114.114.114", "223.5.5.5"]}))
        self.addCleanup(e.close)
        e.upstream("server=211.138.164.6\n")
        self.assertEqual(e.json("status.py")["wan"]["dns"],
                         ["114.114.114.114", "223.5.5.5"],
                         "手动模式必须用手配的，不能被 upstream.conf 覆盖")

    def test_dns_key_always_present(self):
        """字段必须总在——面板据此判断「有没有值」，缺键会退化成 —。"""
        e = Env(base_cfg())
        self.addCleanup(e.close)
        self.assertIn("dns", e.json("status.py")["wan"])


class PdAndConflict(unittest.TestCase):
    """两条「配置看着正常、实际不通」的运行期告警。

    RT-05：PD 只有 /64 时 LAN 分不到独立前缀（子网 0 被 WAN 的 IA_NA 占用）。
    RT-17：DHCP 拿到的 WAN 地址落在 LAN 网段内 → 路由冲突。
    两者都只能运行期判断，而且用户不会去翻日志，所以要在概览页直接说出来。
    """

    def _env(self, **kw):
        cfg = kw.pop("cfg", None) or base_cfg(ipv6={"enabled": True, "mode": "native"})
        e = Env(cfg, **kw)
        self.addCleanup(e.close)
        return e

    def test_slash64_flagged(self):
        e = self._env(unreach="unreachable 2409:8a5e:a57:6060::/64 dev lo proto dhcp metric 1024\n")
        d = e.json("status.py")
        self.assertEqual(d["ipv6"]["pd_prefixlen"], 64)
        self.assertTrue(d["ipv6"]["pd_too_small"],
                        "/64 委派下 LAN 的 SubnetId=1 越界，必须告警")

    def test_slash60_is_fine(self):
        e = self._env(unreach="unreachable 2409:8a5e:a57:6060::/60 dev lo proto dhcp metric 1024\n")
        d = e.json("status.py")
        self.assertEqual(d["ipv6"]["pd_prefixlen"], 60)
        self.assertFalse(d["ipv6"]["pd_too_small"])

    def test_slash63_is_fine(self):
        """/63 有两个子网，子网 1 存在。"""
        e = self._env(unreach="unreachable 2409:8a5e:a57:6060::/63 dev lo proto dhcp metric 1024\n")
        self.assertFalse(e.json("status.py")["ipv6"]["pd_too_small"])

    def test_multiple_pd_takes_widest(self):
        """多条 PD 时不能只看第一条，也不能取「最长前缀」。

        审计 N2：同时拿到 /60 和 /64 时，/60 已经够 LAN 用——取最长（/64）
        会误报「PD 太短」。
        """
        e = self._env(unreach="unreachable 2409:8a5e:a57:6060::/64 dev lo proto dhcp metric 1024\n"
                              "unreachable 2409:8a5e:a57:6060::/60 dev lo proto dhcp metric 1025\n")
        d = e.json("status.py")
        self.assertEqual(d["ipv6"]["pd_prefixlen"], 60, "展示应取最宽的那个")
        self.assertFalse(d["ipv6"]["pd_too_small"], "/60 够用，不该报警")

    def test_all_pd_too_small_flagged(self):
        e = self._env(unreach="unreachable 2409:8a5e:a57:6060::/64 dev lo proto dhcp metric 1024\n"
                              "unreachable 2409:8a5e:a58:6060::/64 dev lo proto dhcp metric 1025\n")
        self.assertTrue(e.json("status.py")["ipv6"]["pd_too_small"])

    def test_no_pd_not_flagged(self):
        """还没拿到 PD（开机初期）不能误报——那时本来就什么都没有。"""
        d = self._env().json("status.py")
        self.assertEqual(d["ipv6"]["pd_prefixlen"], 0)
        self.assertFalse(d["ipv6"]["pd_too_small"])

    def test_nat6_not_checked(self):
        """NAT6 用 ULA，不依赖 PD 长度，不该报。"""
        cfg = base_cfg(ipv6={"enabled": True, "mode": "nat6"})
        e = self._env(cfg=cfg, unreach="unreachable 2409:8a5e:a57:6060::/64 dev lo proto dhcp metric 1024\n")
        self.assertFalse(e.json("status.py")["ipv6"]["pd_too_small"])

    def test_wan_in_lan_subnet_flags_conflict(self):
        e = self._env(route=[{"dev": "enp1s0"}],
                      addr=[{"addr_info": [{"local": "192.168.10.5", "prefixlen": 24}]}])
        d = e.json("status.py")
        self.assertTrue(d["wan"]["lan_conflict"],
                        "WAN 落在 LAN 网段内必须告警（路由会冲突）")

    def test_wan_outside_lan_no_conflict(self):
        e = self._env(route=[{"dev": "enp1s0"}],
                      addr=[{"addr_info": [{"local": "100.64.1.2", "prefixlen": 24}]}])
        self.assertFalse(e.json("status.py")["wan"]["lan_conflict"])


class SingboxApiRisk(unittest.TestCase):
    """sing-box 的 API 是否对局域网开放 + 弱口令（审计 §5.2）。

    这份配置属 sub-store，DebRT 只读，所以不改它——但「面板看着正常、局域网里
    任何设备都能改代理节点」这种情况必须能看见。
    """

    def _env(self, conf):
        e = Env(base_cfg())
        self.addCleanup(e.close)
        d = os.path.join(e.root, "etc", "sing-box")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump(conf, f)
        return e

    def test_open_listen_with_weak_secret_flagged(self):
        e = self._env({"services": [{"type": "api", "listen": "0.0.0.0",
                                     "listen_port": 9090, "secret": "passwd"}]})
        d = e.json("status.py")["singbox_api"]
        self.assertTrue(d["weak"])
        self.assertEqual(d["port"], 9090)

    def test_loopback_listen_not_flagged(self):
        e = self._env({"services": [{"type": "api", "listen": "127.0.0.1",
                                     "listen_port": 9090, "secret": "passwd"}]})
        self.assertFalse(e.json("status.py")["singbox_api"]["weak"])

    def test_strong_secret_not_flagged(self):
        e = self._env({"services": [{"type": "api", "listen": "0.0.0.0",
                                     "listen_port": 9090, "secret": "Q7!k2mZp9vRt"}]})
        self.assertFalse(e.json("status.py")["singbox_api"]["weak"])

    def test_legacy_clash_api_form(self):
        """老写法 experimental.clash_api 也要认。"""
        e = self._env({"experimental": {"clash_api": {
            "external_controller": "0.0.0.0:9090", "secret": "admin"}}})
        d = e.json("status.py")["singbox_api"]
        self.assertTrue(d["weak"])
        self.assertEqual(d["port"], 9090)

    def test_missing_config_is_quiet(self):
        e = Env(base_cfg())
        self.addCleanup(e.close)
        self.assertFalse(e.json("status.py")["singbox_api"]["weak"])


if __name__ == "__main__":
    unittest.main()
