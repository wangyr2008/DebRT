#!/usr/bin/env python3
"""系统收敛流水线（v0.1.87 / P2-7）。

`do_apply_all` 是**开机路径**：`dert-apply.service` 每次启动跑一遍，
`dert-converge.timer` 每 6 小时跑一遍，`config-set` / 备份恢复 / 升级后也各跑一遍。
所以这次改写的第一原则是**逐条对齐**，不是"顺便优化"。

保留的每条规则，都是踩过坑才有的：

  1. **生成器 stderr 必须留档**（`netgen.err`）。结构损坏时 manifest 是空的，
     没有 stderr 就只剩"配置生成失败"+一句指向空白的 hint（P3-12）。
  2. **收敛模式无漂移时提前返回**：只确认运行时（nft 表 / cake / 服务存活），
     绝不重启 networkd/dnsmasq——开机全量重启会让 LAN 客户端闪断（真机实测）。
  3. **提前返回前必须删 stage**。漏了就是每次开机 / 6h 收敛在 /run/dert 留一份
     完整生成物，其中 `etc/ppp/chap-secrets` 是明文宽带密码（真机 2026-11-09）。
  4. **无漂移分支不能 start dnsmasq**：收敛单元 `Before=dnsmasq`，同步 start 会与
     排序中的启动任务互相等待形成死锁（真机实测）。崩溃恢复交给 Restart=on-failure。
  5. **预检失败要透传具体报错**（`nft -c` / `dnsmasq --test` 的原文），
     否则用户只看到"无法生成合法的系统配置"，无从下手（P3-12b）。
  6. **存量配置迁移**（`LAN_MASK_TOO_WIDE` → 掩码回 /24）：迁移后重试一次，
     仍失败才报错；任何其他错误原样透传，保证不部署半套配置。

数据根跟随 `DERT_FSROOT`（测试缝）；代码路径跟随 `DERT_LIB`
（生产即 /usr/local/lib/dert，与 bash 的 DERT_ROOT 一致）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import privlib
from privlib import CONFIG_JSON, RUN, die, ok_out

FS = os.environ.get("DERT_FSROOT", "")
ROOT = FS or "/"
# 代码路径：本模块所在目录（生产即 /usr/local/lib/dert，与 bash 的 DERT_ROOT/PRIV_DIR 一致）。
# 用 __file__ 而不是硬编码，是为了让仓库里的副本也能真跑（测试与现场排查）。
LIB = os.path.dirname(os.path.abspath(__file__))
NFT_TABLE = "inet dert_base"
ACTIONS = "networkd pppoe dnsmasq nft cake"


def _fs(path):
    return path if ROOT == "/" else ROOT.rstrip("/") + path


def _sh(cmd, timeout=60, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except Exception:
        return None


def _tail(path, lines=2, width=240):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read().strip().split("\n")
    except OSError:
        return ""
    return " ".join(data[-lines:])[:width]


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _log(event, **fields):
    """结构化事件日志（格式见 privlib.log_event）。"""
    privlib.log_event("apply", event, **fields)


def mark_converge_failed():
    os.makedirs(RUN, exist_ok=True)
    open(RUN + "/converge-failed", "a").close()


def clear_converge_failed():
    try:
        os.unlink(RUN + "/converge-failed")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 生成与部署
# ---------------------------------------------------------------------------
def generate(stage):
    """跑 netgen 生成到 stage。返回 (ok, manifest 内容, stderr 摘要)。"""
    man = os.path.join(stage, "manifest.json")
    err = os.path.join(stage, "netgen.err")
    try:
        with open(man, "w", encoding="utf-8") as mf, open(err, "w", encoding="utf-8") as ef:
            r = subprocess.run([sys.executable, os.path.join(LIB, "netgen.py"), "--stage", stage],
                               stdout=mf, stderr=ef, timeout=180)
        ok = r.returncode == 0
    except Exception as e:
        with open(err, "a", encoding="utf-8") as ef:
            ef.write(str(e))
        ok = False
    return ok, _read(man), _tail(err)


def manifest_files(man_path):
    """[(相对路径, mode)]。读不出来返回 None（宁可不动，不可半套）。"""
    try:
        with open(man_path, encoding="utf-8") as f:
            man = json.load(f)
        return [(f_["path"], int(f_["mode"])) for f_ in man["files"]]
    except Exception:
        return None


def apply_stage_files(stage, man_path):
    files = manifest_files(man_path)
    if files is None:
        die("IO", "生成清单无法解析", "请重试；若持续出现请反馈生成器输出")
    for rel, mode in files:
        dest = _fs("/" + rel)
        d = os.path.dirname(dest)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dert-tmp.", dir=d)
        os.close(fd)
        try:
            shutil.copyfile(os.path.join(stage, rel), tmp)
            os.chmod(tmp, mode)
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            die("IO", "部署生成文件失败：%s" % dest)


def cleanup_stale(man_path):
    """按 manifest 对账删陈旧生成物（逻辑在 cleanup.py，这里只记日志）。"""
    script = os.path.join(LIB, "cleanup.py")
    if not os.path.isfile(script):
        _log("cleanup_skipped", reason="script_missing", path=script)
        return
    cmd = [sys.executable, script, man_path, "--apply"]
    if ROOT != "/":
        cmd += ["--root", ROOT]   # 测试缝：与其余模块一致，生产不传
    r = _sh(cmd, timeout=120)
    if r is None or r.returncode != 0:
        return
    for line in (r.stdout or "").split("\n"):
        if line.strip():
            _log("cleanup", path=line.strip())


def preflight(stage):
    """语法级预检（nft -c / dnsmasq --test）。返回 (ok, 错误 JSON 或空串)。"""
    nft_file = os.path.join(stage, "etc/nftables.d/dert-base.nft")
    dns_file = os.path.join(stage, "etc/dnsmasq.d/dert.conf")
    if shutil.which("nft") and os.path.isfile(nft_file):
        r = _sh(["nft", "-c", "-f", nft_file], timeout=60)
        if r is None or r.returncode != 0:
            err = " ".join(((r.stderr if r else "") or "").strip().split("\n")[-3:])
            return False, json.dumps({
                "ok": False, "code": "NFT_SYNTAX",
                "message": "防火墙规则语法校验未通过：%s" % err,
                "hint": "本次修改未生效；请把页面上的操作内容反馈给开发者",
            }, ensure_ascii=False)
    if shutil.which("dnsmasq") and os.path.isfile(dns_file):
        r = _sh(["dnsmasq", "--test", "-C", dns_file], timeout=60)
        if r is None or r.returncode != 0:
            err = " ".join(((r.stderr if r else "") or "").strip().split("\n")[-2:])
            return False, json.dumps({
                "ok": False, "code": "DNSMASQ_SYNTAX",
                "message": "DHCP/DNS 配置校验未通过：%s" % err,
                "hint": "本次修改未生效；请检查刚填写的 DHCP 参数",
            }, ensure_ascii=False)
    return True, ""


# ---------------------------------------------------------------------------
# 运行时动作
# ---------------------------------------------------------------------------
def cleanup_ifupdown():
    """接管网口时清掉 ifupdown 残留（dhcpcd 会在网桥端口跑 DHCP 并覆写 resolv.conf）。"""
    path = _fs("/etc/network/interfaces")
    if not os.path.isfile(path):
        return
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            names = [p["name"] for p in json.load(f).get("ports", [])]
    except Exception:
        return
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return
    drop = []
    for n in names:
        drop += ["allow-hotplug %s" % n, "iface %s inet dhcp" % n]
    out = [ln for ln in lines if ln.strip() not in drop]
    if out != lines:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        _log("ifupdown_cleanup", ports=len(names))
    _sh(["pkill", "-x", "dhcpcd"], timeout=10)


def sync_dns_upstream():
    """按 config 的 WAN 口重建 DNS 上游（幂等、尽力而为；等待职责在专用单元）。"""
    hook = _fs("/etc/networkd-dispatcher/routable.d/70-dert-dns")
    if not (os.path.isfile(hook) and os.access(hook, os.X_OK)):
        return
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    ifs = [p["name"] for p in cfg.get("ports", []) if p.get("wan")]
    if cfg.get("wan", {}).get("mode") == "pppoe":
        ifs.append("ppp0")  # PPPoE 的承载口是 ppp0（v4 由 ip-up.d，v6 由 DHCPv6）
    if not ifs:
        return
    for if_ in ifs:
        v4 = _sh(["ip", "-4", "addr", "show", if_], timeout=10)
        v6 = _sh(["ip", "-6", "addr", "show", if_], timeout=10)
        has4 = v4 is not None and "inet " in (v4.stdout or "")
        has6 = v6 is not None and "scope global" in (v6.stdout or "")
        if has4 or has6:  # v6-only 接入同样要触发（B-13）
            _sh([hook, if_, "routable"], timeout=30)
        resolv = _fs("/etc/resolv.conf")
        try:
            cur = open(resolv, encoding="utf-8", errors="replace").read()
        except OSError:
            cur = ""
        if "nameserver 127.0.0.1" not in cur:
            try:
                with open(resolv, "w", encoding="utf-8") as f:
                    f.write("nameserver 127.0.0.1\noptions timeout:1 attempts:2\n")
            except OSError:
                pass


D10 = """# 由 DebRT 安装/收敛自动维护：sing-box 启动前等 WAN 就绪（防 auto_route 死循环）
[Unit]
OnFailure=dert-singbox-cleanup.service
[Service]
ExecStartPre=/usr/local/lib/dert/wait-wan
TimeoutStartSec=210
Restart=on-failure
RestartSec=3
"""

D20 = """# 由 DebRT 安装/收敛自动维护：DNS 按 MAC 分流（见 COEXISTENCE.md §DNS 分流）
# 启动前生成 00-dert-dns.json；启动后派生 nft 表；停止时删表回落 dnsmasq
[Service]
ExecStartPre=/usr/local/lib/dert/dns-split-prepare
ExecStartPost=+/usr/local/lib/dert/dns-split-sync
ExecStopPost=+/usr/local/lib/dert/dns-split-teardown
"""


def maintain_singbox_dropin():
    """sing-box 存在但缺 drop-in 时补挂（覆盖"先装 DebRT 后装 sing-box"）。"""
    # 官方 deb 把单元装在 /usr/lib/systemd/system，/etc 下永远没有——
    # 必须用机制级判断，文件路径检测会让兜底机制在默认安装路径上失效（A-2）
    r = _sh(["systemctl", "list-unit-files", "sing-box.service"], timeout=30)
    if r is None or r.returncode != 0:
        return
    d = _fs("/etc/systemd/system/sing-box.service.d")
    os.makedirs(d, exist_ok=True)
    changed = False
    pairs = ((d + "/10-dert-wait-wan.conf", D10, "wait-wan", "WAN 就绪"),
             (d + "/20-dert-dns-split.conf", D20, "dns-split", "DNS 分流"))
    for path, body, name, what in pairs:
        if os.path.isfile(path):
            continue
        src = os.path.join(LIB, name + ("-sync" if name == "dns-split" else ""))
        if not os.path.isfile(src):
            continue
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError:
            continue
        # 走 journald，不往 stdout 打——apply-all 的 stdout 是面板解析的 JSON，
        # 多一行人类可读文本就会变成 PRIV_MALFORMED（backup-restore 内部也调它）
        _log("singbox_dropin", dropin=name, what=what)
        changed = True
    if changed:
        _sh(["systemctl", "daemon-reload"], timeout=60)


def svc_restart_networkd():
    _sh(["systemctl", "daemon-reload"], timeout=60)
    r = _sh(["systemctl", "restart", "systemd-networkd.service"], timeout=120)
    if r is None or r.returncode != 0:
        r = _sh(["systemctl", "restart", "systemd-networkd"], timeout=120)
    return r


def svc_nft():
    """重载 nft 表 + 补 WAN 地址。nft 失败不能被后面的 wan-addr-sync 掩盖（R-02）。"""
    rc = 0
    nft = _fs("/etc/nftables.d/dert-base.nft")
    if os.path.isfile(nft):
        r = _sh(["nft", "-f", nft], timeout=60)
        if r is None or r.returncode != 0:
            rc = 1
    sync = os.path.join(LIB, "wan-addr-sync")
    if os.access(sync, os.X_OK):
        _sh([sync], timeout=60)
    return rc


def svc_cake():
    q = os.path.join(LIB, "qdisc-apply")
    if os.access(q, os.X_OK):
        _sh([q], timeout=60)


def svc_pppoe():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            mode = json.load(f).get("wan", {}).get("mode", "")
    except Exception:
        mode = ""
    if mode == "pppoe":
        _sh(["systemctl", "enable", "--quiet", "dert-pppoe.service"], timeout=30)
        r = _sh(["systemctl", "restart", "dert-pppoe.service"], timeout=120)
        return 0 if (r and r.returncode == 0) else 1
    _sh(["systemctl", "disable", "--quiet", "dert-pppoe.service"], timeout=30)
    _sh(["systemctl", "stop", "dert-pppoe.service"], timeout=30)
    return 0


def run_actions(acts):
    """按动作串重启服务，返回失败动作名列表。"""
    failed = []
    for act in acts.split():
        if act == "networkd":
            r = svc_restart_networkd()
            if r is None or r.returncode != 0:
                failed.append("networkd")
        elif act == "dnsmasq":
            # servers-file 对 dnsmasq 是启动期 fatal：确保文件存在（空文件合法），
            # 上游由钩子/ip-up 稍后写入并 HUP（P1-3 保险，兼消 PPPoE 首拨竞态）
            os.makedirs(_fs("/run/dert-dns"), exist_ok=True)
            up = _fs("/run/dert-dns/upstream.conf")
            if not os.path.isfile(up):
                try:
                    open(up, "a").close()
                except OSError:
                    pass
            r = _sh(["systemctl", "restart", "dnsmasq.service"], timeout=120)
            if r is None or r.returncode != 0:
                failed.append("dnsmasq")
        elif act == "nft":
            if svc_nft() != 0:
                failed.append("nft")
        elif act == "cake":
            svc_cake()
        elif act == "pppoe":
            if svc_pppoe() != 0:
                failed.append("pppoe")
    return failed


def wait_dnsmasq(seconds):
    for _ in range(seconds):
        r = _sh(["systemctl", "is-active", "--quiet", "dnsmasq.service"], timeout=10)
        if r is not None and r.returncode == 0:
            return True
        time.sleep(1)
    return False


def _migrate_mask():
    """存量 LAN_MASK_TOO_WIDE → 掩码迁回 /24（保留 IP 与地址池）。返回说明或空串。"""
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return ""
    fixes = []
    if cfg.get("lan", {}).get("mask") != "255.255.255.0":
        cfg.setdefault("lan", {})["mask"] = "255.255.255.0"
        fixes.append("LAN 掩码迁移为 /24")
    privlib.write_atomic(CONFIG_JSON, json.dumps(cfg, ensure_ascii=False, indent=1), 0o644)
    return "；".join(fixes)


def _snapshot(tag):
    """配置快照——实现在 privlib（与 configops/backupops 共用一份）。"""
    return privlib.snapshot(tag)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def apply_all(convergent=False):
    privlib.require_root()
    os.makedirs(_fs("/run/dert-dns"), exist_ok=True)
    os.makedirs(RUN, exist_ok=True)

    stage = tempfile.mkdtemp(prefix="stage.", dir=RUN)
    man = os.path.join(stage, "manifest.json")
    migrated = ""
    ok, out, gen_err = generate(stage)
    if not ok:
        # 存量不兼容配置的自动纠正（四审 M-01/M-02/M-03）：只认 LAN_MASK_TOO_WIDE。
        # 原先还有 "V6_NATIVE_PPPPOE_UNSUPPORTED → 改 NAT6"，v0.1.41 起 PPPoE 已真支持
        # IPv6（ppp0 上跑 DHCPv6），该错误码不再产生；留着会让人误以为规则相反。
        if "LAN_MASK_TOO_WIDE" in out:
            _snapshot("m%d" % int(time.time()))
            fix = _migrate_mask()
            if fix:
                migrated = "已自动修正不兼容的存量配置（%s）" % fix
                shutil.rmtree(stage, ignore_errors=True)
                stage = tempfile.mkdtemp(prefix="stage.", dir=RUN)
                man = os.path.join(stage, "manifest.json")
                ok2, out2, gen_err2 = generate(stage)
                if not ok2:
                    sys.stdout.write(out2)
                    mark_converge_failed()
                    shutil.rmtree(stage, ignore_errors=True)
                    die("CONFIG_CORRUPT", "自动迁移后仍无法生成配置",
                        "请按上方错误信息在管理页面修正" +
                        ("。生成器输出：%s" % gen_err2 if gen_err2 else ""))
        if not migrated:
            sys.stdout.write(out)  # 透传 netgen 的可操作错误（复审 R-03）
            mark_converge_failed()
            shutil.rmtree(stage, ignore_errors=True)
            die("CONFIG_CORRUPT", "配置生成失败",
                "请按上方错误信息在管理页面修正；或从 /var/lib/dert/lastgood 恢复上一份可用配置" +
                ("。生成器输出：%s" % gen_err if gen_err else ""))

    if convergent:
        files = manifest_files(man)
        # 清单读不出来 → 当作有漂移，走全量部署。宁可多部署一次，
        # 也不能因为读不到清单就宣布"无漂移"（那样等于什么都没做还说成功了）。
        drift = files is None
        files = files or []
        for rel, _mode in files:
            dest = _fs("/" + rel)
            if not os.path.isfile(dest):
                drift = True
                break
            try:
                with open(dest, "rb") as f1, open(os.path.join(stage, rel), "rb") as f2:
                    if f1.read() != f2.read():
                        drift = True
                        break
            except OSError:
                drift = True
                break
        if not drift:
            # 无漂移：nft 规则不跨重启，缺表则只重建防火墙+QoS；绝不重启
            # networkd/dnsmasq（开机全量重启会让 LAN 客户端闪断——真机实测）
            r = _sh(["nft", "list", "table"] + NFT_TABLE.split(), timeout=30)
            if r is None or r.returncode != 0:
                svc_nft()
            svc_cake()
            cleanup_ifupdown()
            sync_dns_upstream()
            # 此处**不能** start dnsmasq：收敛单元 Before=dnsmasq，同步 start 会与
            # 排序中的启动任务互相等待形成死锁（真机实测）。崩溃恢复靠 Restart=on-failure。
            maintain_singbox_dropin()
            clear_converge_failed()
            # stage 必须删：这是"无漂移"的提前返回，早于下方全量路径的清理。
            # 漏了它 = 每次开机/6h 收敛/每次升级都在 /run/dert 留一份完整生成物，
            # 其中 etc/ppp/chap-secrets 是明文宽带密码（真机 2026-11-09 实测）。
            shutil.rmtree(stage, ignore_errors=True)
            ok_out(message="无漂移，运行时已确认", converged=True)
            return

    good, pf_out = preflight(stage)
    if not good:
        mark_converge_failed()
        shutil.rmtree(stage, ignore_errors=True)
        # 透传预检的具体原因（nft -c / dnsmasq --test 的报错行）
        if pf_out:
            sys.stdout.write(pf_out + "\n")
            sys.exit(1)
        die("STAGE_BROKEN", "现有配置无法生成合法的系统配置",
            "请在管理页面检查网络设置；或恢复出厂设置")

    apply_stage_files(stage, man)
    cleanup_stale(man)
    cleanup_ifupdown()
    shutil.rmtree(stage, ignore_errors=True)
    run_actions(ACTIONS)
    wait_dnsmasq(15)
    sync_dns_upstream()
    maintain_singbox_dropin()
    clear_converge_failed()
    ok_out(message="已按配置完成系统收敛" + ("。" + migrated if migrated else ""))


def main(argv):
    arg = argv[1] if len(argv) > 1 else ""
    apply_all(convergent=(arg == "--convergent"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
