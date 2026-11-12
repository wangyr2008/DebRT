#!/usr/bin/env python3
"""DebRT 健康检查（v0.1.81 / P4）。

一条命令给出结构化健康状态，供面板、外部监控（systemd timer / cron / Zabbix /
node_exporter textfile）直接消费。**只读，不改任何东西。**

为什么单独做一个子命令，而不是让调用方自己拼 `systemctl` / `nft`：
  1. "健康"的判据散落在多处（服务、nft 表、v6 目标是否跟当前前缀、DNS 双路径、
     收敛失败标记、磁盘余量），每处都有坑（见下面各 check 的注释）；
  2. 判据要能**一起演进**——比如以后新增一项检查，所有调用方自动受益；
  3. 需要**可测试**：喂假根 + 假命令就能断言判定结果。

退出码：0 = 全部通过；1 = 有失败项（监控可以直接看退出码）。

用法：
    health.py            输出 JSON
    health.py --quiet    只输出失败项
"""

import json
import os
import shutil
import subprocess
import sys
import time

# 从 DERT_FSROOT 推导数据根（与 lib.sh 的推导方式一致，未设时即生产路径）
_FS = os.environ.get("DERT_FSROOT", "")
E = _FS + "/etc/dert"
V = _FS + "/var/lib/dert"
R = _FS + "/run/dert"

SERVICES = ("dert-panel", "dert-pppoe", "dnsmasq", "sing-box", "dert-netwatch")


def run(args, timeout=8):
    """跑一条命令，返回 (rc, stdout)。任何异常都当成失败，不抛。"""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception:
        return 127, ""


def check(checks, name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    return bool(ok)


def service_checks(checks):
    for s in SERVICES:
        rc, _ = run(["systemctl", "is-active", "--quiet", s])
        check(checks, f"service:{s}", rc == 0, "active" if rc == 0 else "inactive")


def failed_units(checks):
    rc, out = run(["systemctl", "--failed", "--no-legend", "--plain"])
    n = len([l for l in out.splitlines() if l.strip()])
    check(checks, "systemd:failed_units", n == 0, f"{n} 个失败单元")


def nft_checks(checks):
    """分流表存在且规则数正确。

    规则数必须正好 4：`nft -f` 里直接写 table 定义对已存在的表是**追加**而非
    替换（真机曾堆到 20 条），所以条数本身就是个回归信号。
    """
    rc, out = run(["nft", "list", "table", "inet", "dert_dns"])
    if rc != 0:
        check(checks, "nft:dert_dns", False, "表不存在（DNS 分流未生效）")
        return
    n = sum(1 for l in out.splitlines() if "dnat ip" in l)
    check(checks, "nft:dert_dns_rules", n == 4, f"{n} 条（应为 4）")


def prefix_checks(checks):
    """nft 里的 v6 DNAT 目标必须等于 br-lan 当前 GUA。

    前缀变化后若没跟上，白名单设备的 v6 DNS 会全部打向不存在的地址
    （真机 2026-11-12 实测 437 个包落空）。
    """
    rc, out = run(["ip", "-6", "addr", "show", "dev", "br-lan", "scope", "global"])
    gua = ""
    for line in out.splitlines():
        if "inet6" in line and "dynamic" in line:
            gua = line.split()[1].split("/")[0]
            break
    if not gua:
        for line in out.splitlines():
            if "inet6" in line and "temporary" not in line:
                gua = line.split()[1].split("/")[0]
                break
    if not gua:
        check(checks, "ipv6:br_lan_gua", True, "无全局 v6（可能已关闭 v6）")
        return

    rc, tbl = run(["nft", "list", "table", "inet", "dert_dns"])
    targets = set()
    for line in tbl.splitlines():
        if "dnat ip6 to" in line:
            seg = line.split("dnat ip6 to", 1)[1].strip()
            targets.add(seg.strip("[]").split("]")[0])
    if not targets:
        check(checks, "nft:v6_target", False, "分流表里没有 v6 目标")
        return
    check(checks, "nft:v6_target", targets == {gua},
          f"表={sorted(targets)} 当前={gua}")


def dns_checks(checks):
    """5353 在监听 + resolv.conf 指回本机 dnsmasq。

    resolv.conf 被 pppd 的 usepeerdns 改写后，路由器自身会绕过 dnsmasq，
    局域网主机名（.lan）解析不了（真机 2026-11-12 实测）。
    """
    rc, out = run(["ss", "-lnu"])
    check(checks, "dns:5353_listening", ":5353" in out,
          "白名单 DNS 入口在监听" if ":5353" in out else "5353 未监听（fakeip 不可用）")

    try:
        with open(_FS + "/etc/resolv.conf", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        txt = ""
    check(checks, "dns:resolv_conf", "nameserver 127.0.0.1" in txt,
          "指向本机 dnsmasq" if "nameserver 127.0.0.1" in txt
          else "未指向 127.0.0.1（.lan 解析会失败）")


def state_checks(checks):
    """收敛失败标记 + 磁盘余量。"""
    check(checks, "state:converge_failed",
          not os.path.exists(R + "/converge-failed"),
          "上次收敛失败" if os.path.exists(R + "/converge-failed") else "正常")

    try:
        du = shutil.disk_usage(_FS or "/")
        free_gb = du.free / (1024 ** 3)
        check(checks, "disk:free", free_gb >= 1.0, f"{free_gb:.1f} GB 可用")
    except Exception:
        pass


def config_check(checks):
    """配置可解析 + 版本可读。"""
    try:
        with open(E + "/config.json", encoding="utf-8") as f:
            json.load(f)
        check(checks, "config:parsable", True)
    except Exception as e:
        check(checks, "config:parsable", False, str(e)[:80])

    ver = ""
    # 先看假根下的路径（测试用），再退回真实安装路径
    for p in (_FS + "/usr/local/lib/dert/VERSION", "/usr/local/lib/dert/VERSION"):
        try:
            with open(p, encoding="utf-8") as f:
                ver = f.read().strip()
            break
        except OSError:
            continue
    check(checks, "version:readable", bool(ver), ver or "读不到 VERSION")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in argv

    checks = []
    service_checks(checks)
    failed_units(checks)
    nft_checks(checks)
    prefix_checks(checks)
    dns_checks(checks)
    state_checks(checks)
    config_check(checks)

    bad = [c for c in checks if not c["ok"]]
    healthy = not bad

    if quiet:
        for c in bad:
            print(f"{c['name']}: {c['detail']}")
    else:
        print(json.dumps({
            "ok": True,
            "healthy": healthy,
            "checked_at": int(time.time()),
            "passed": len(checks) - len(bad),
            "total": len(checks),
            "failed": [c["name"] for c in bad],
            "checks": checks,
        }, ensure_ascii=False))

    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
