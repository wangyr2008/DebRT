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
  *)                      printf '[]' ;;
esac
exit 0
"""


class Env:
    def __init__(self, cfg, run_markers=(), route=None, addr=None):
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

    def run(self, mod, env_extra=None):
        env = dict(os.environ)
        env["DERT_FSROOT"] = self.root
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["FAKE_ROUTE_JSON"] = self.route
        env["FAKE_ADDR_JSON"] = self.addr
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


if __name__ == "__main__":
    unittest.main()
