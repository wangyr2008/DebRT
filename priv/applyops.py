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


def drift_list(man_path, stage):
    """逐文件比对生成物与线上，返回漂移的相对路径列表。

    `[]` = 完全一致；`None` = 清单读不出来（调用方必须当作"全部漂移"——
    宁可多部署一次，也不能因为读不到清单就宣布"无漂移"）。

    内容与权限都比：只改 chmod 不改内容也算漂移。以前只比内容，权限被改坏
    （比如 chap-secrets 从 600 变 644）会一直"无漂移"地跑下去——那是明文密码。
    """
    files = manifest_files(man_path)
    if files is None:
        return None
    out = []
    for rel, mode in files:
        dest = _fs("/" + rel)
        if not os.path.isfile(dest):
            out.append(rel)
            continue
        try:
            if (os.stat(dest).st_mode & 0o777) != (mode & 0o777):
                out.append(rel)
                continue
            with open(dest, "rb") as f1, open(os.path.join(stage, rel), "rb") as f2:
                if f1.read() != f2.read():
                    out.append(rel)
        except OSError:
            out.append(rel)
    return out


# 重启顺序有依赖：networkd 先、pppoe 依赖 ppp0 的 .network、dnsmasq 读 servers-file
_ACTION_ORDER = ("networkd", "pppoe", "dnsmasq", "nft", "cake")


def actions_for(drift):
    """按「到底哪些生成物变了」推导要重启的服务。

    以前全量路径无条件跑 ACTIONS（networkd pppoe dnsmasq nft cake）：真机
    2026-11-20，**一个 nft 文件的内容差异**就把已连续 44 小时的 PPPoE 会话
    打断，WAN 掉线数秒、所有设备的连接全断——代价和原因完全不成比例。
    只改 nft 就只重载 nft；只有 WAN 口的 .network 或 ppp 配置变了才重拨。
    """
    if drift is None:
        return ACTIONS
    need = {"nft", "cake"}          # 幂等且便宜，总是做
    for rel in drift:
        if rel.startswith("etc/systemd/network/"):
            need.add("networkd")
            if rel.endswith("25-ppp0.network") or rel.endswith("10-wan.network"):
                need.add("pppoe")
        elif rel.startswith("etc/dnsmasq"):
            need.add("dnsmasq")
        elif rel.startswith("etc/ppp/"):
            need.add("pppoe")
    return " ".join(a for a in _ACTION_ORDER if a in need)


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


# DebRT 自己的 systemd 单元白名单：(单元名, 是否应开机自启)。
#
# 为什么需要这份对账：**新增单元在"引入它的那个版本"里装不上**。
# 升级时跑的是**旧版**代码——旧版的通用拷贝循环会把新的 .service/.timer
# 落到 /etc/systemd/system（真机实测文件在），但 `enable` 写在新版代码里，
# 而新版代码要等**下一次**升级才运行。结果就是：升级完文件在、timer 是
# disabled，面板一直显示「暂无数据」，且不报错。
# 收敛（开机 / 6h / 保存配置 / 升级后）都走这里，所以最迟一次收敛即自愈，
# 也能修掉被误 disable 的单元。
OWN_UNITS = (
    ("dert-panel.service", True),
    ("dert-dns-sync.service", True),
    ("dert-converge.timer", True),
    ("dert-pppoe-watchdog.service", False),
    ("dert-pppoe-watchdog.timer", True),
    ("dert-netwatch.service", True),
    ("dert-wanq.service", False),
    ("dert-wanq.timer", True),
)


def ensure_own_units():
    """同步自有单元文件并保证该自启的已自启。返回 (changed, newly_enabled)。

    只碰 OWN_UNITS 里的文件——/etc/systemd/system 下还有 sing-box、podman、
    tailscale 等**别人的**单元，绝不能"看到 service 就覆盖"。
    任何异常都吞掉并记事件：收敛不能因为这一步失败而整体失败。
    """
    changed, newly = False, []
    try:
        # 第一遍：只同步内容（先落盘，再 daemon-reload，最后 enable——
        # 顺序反了的话 enable 一个刚写的单元会拿到旧的 unit 缓存）
        for name, _want in OWN_UNITS:
            src = os.path.join(LIB, "systemd", name)
            if not os.path.isfile(src):
                continue
            dst = _fs("/etc/systemd/system/" + name)
            # 读源与读目标必须分开 try：合并写会把「目标不存在」误当成
            # 「源读不出来」，从而把空内容写进去（写完下次比对又"有变化"，
            # 每次收敛都白跑一次 daemon-reload）。
            try:
                with open(src, "rb") as f1:
                    new = f1.read()
            except OSError:
                continue
            try:
                with open(dst, "rb") as f2:
                    same = f2.read() == new
            except OSError:
                same = False
            if same:
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(new)
                os.chmod(dst, 0o644)
                changed = True
            except OSError:
                continue
        if changed:
            _sh(["systemctl", "daemon-reload"], timeout=60)
        for name, want in OWN_UNITS:
            if not want or not os.path.isfile(_fs("/etc/systemd/system/" + name)):
                continue
            r = _sh(["systemctl", "is-enabled", "--quiet", name], timeout=15)
            if r is not None and r.returncode == 0:
                continue
            _sh(["systemctl", "enable", name], timeout=60)
            newly.append(name)
        if newly:
            _log("units_enabled", units=",".join(newly))
        # journald 上限：不是 unit 文件，单独同步一次（内容变了才重启 journald）。
        # 走这里而不是只写在 install.sh 里，是因为**升级不跑 install.sh**，
        # 而升级后紧跟的收敛跑的是新装好的 dert-priv——正好补上这一步。
        try:
            src = os.path.join(LIB, "systemd", "journald.conf.d", "dert.conf")
            dst = _fs("/etc/systemd/journald.conf.d/dert.conf")
            if os.path.isfile(src):
                with open(src, "rb") as f:
                    want = f.read()
                try:
                    with open(dst, "rb") as f:
                        same = f.read() == want
                except OSError:
                    same = False
                if not same:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(want)
                    os.chmod(dst, 0o644)
                    _sh(["systemctl", "restart", "systemd-journald"], timeout=60)
                    _log("journald_conf_applied")
        except Exception as e:                  # noqa: BLE001
            _log("journald_conf_failed", error=str(e)[:80])
    except Exception as e:                      # noqa: BLE001 —— 收敛不允许被这里带崩
        _log("units_sync_failed", error=str(e)[:80])
    return changed, newly


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


def _minimal_nft():
    """内置最小防火墙：LAN 出网 + WAN 入站默认拒绝。

    只在「生成/预检失败，且磁盘上连上一份生成物都没有」时用（首次安装就失败）。
    配置读不出来时用通配排除法——不能用「!= br-lan 就拦」，那会把
    tailscale0 / podman 的入站一起拦掉。
    """
    wan, br, net = [], "br-lan", None
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        wan = [p["name"] for p in cfg.get("ports", []) if p.get("wan")]
        if cfg.get("wan", {}).get("mode") == "pppoe":
            wan.append("ppp0")          # PPPoE 下真正的 WAN 承载口
        lan = cfg.get("lan", {})
        if lan.get("ip") and lan.get("mask"):
            import ipaddress
            net = str(ipaddress.IPv4Network("%s/%s" % (lan["ip"], lan["mask"]),
                                            strict=False))
    except Exception:
        pass
    if wan:
        define = '  define WAN_IFS = { %s }\n' % ", ".join('"%s"' % w for w in wan)
        match = "iifname $WAN_IFS"
    else:
        define = ""
        match = ('iifname != { "lo", "br-lan", "tun*", "tailscale*",'
                 ' "veth*", "podman*", "docker*" }')
    nat = ("\n  chain srcnat {\n"
           "    type nat hook postrouting priority srcnat; policy accept;\n"
           "    ip saddr $LAN_NET oifname $WAN_IFS masquerade\n"
           "  }\n") if (wan and net) else ""
    lan_def = '  define LAN_NET = %s\n' % net if net else ""
    return (
        "#!/usr/sbin/nft -f\n"
        "# DebRT 兜底防火墙：生成/预检失败且没有可用的上一份生成物时临时加载\n"
        "table inet dert_base\n"
        "flush table inet dert_base\n\n"
        "table inet dert_base {\n"
        "%s  define LAN_BR = \"%s\"\n%s\n"
        "  chain input {\n"
        "    type filter hook input priority filter; policy accept;\n"
        "    ct state established,related accept\n"
        "    %s jump wan_in\n"
        "  }\n\n"
        "  chain wan_in {\n"
        "    ct state invalid drop\n"
        "    ip protocol icmp accept\n"
        "    meta l4proto ipv6-icmp accept\n"
        "    udp dport 68 accept\n"
        "    meta nfproto ipv6 udp dport 546 accept\n"
        "    drop\n"
        "    return\n"
        "  }\n\n"
        # forward 链：只挡「从 WAN 新发起」的连接，LAN 出网与回程都不受影响。
        # 没有这条时，Native/PD 模式下 LAN 设备的 IPv6 GUA 会直接暴露给公网
        # ——兜底规则只拦了到本机的入站，转发路径是 policy accept（审计 N3）。
        "  chain forward {\n"
        "    type filter hook forward priority filter; policy accept;\n"
        "    ct state established,related accept\n"
        "    %s drop\n"
        "  }\n%s}\n" % (define, br, lan_def, match, match, nat))


def failsafe_nft():
    """保证「有 NAT、WAN 入站是关的」——失败路径上唯一必须做到的事。

    为什么必须做：nft 规则不跨重启，install.sh 又禁用了 Debian 的
    nftables.service，开机时唯一加载 inet dert_base 的地方就是这条流水线。
    它在 svc_nft() 之前退出的话，设备会同时「没有 masquerade（LAN 上不了网）」
    和「没有 wan_in drop（WAN 入站全开）」——比正常工作时更危险，而用户只会
    以为「网坏了」，不会想到面板和 SSH 正暴露在上游网络里。

    顺序：表还在就不动 → 否则装磁盘上那份（上一次成功部署的，当时过了预检）
    → 再没有就用内置最小规则。返回实际走到的分支，便于排查。
    """
    r = _sh(["nft", "list", "table"] + NFT_TABLE.split(), timeout=30)
    if r is not None and r.returncode == 0:
        return "present"
    nft = _fs("/etc/nftables.d/dert-base.nft")
    if os.path.isfile(nft):
        svc_nft()
        r = _sh(["nft", "list", "table"] + NFT_TABLE.split(), timeout=30)
        if r is not None and r.returncode == 0:
            _log("nft_failsafe", source="deployed")
            return "deployed"
    body = _minimal_nft()
    p = os.path.join(RUN, "failsafe.nft")
    try:
        os.makedirs(RUN, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        _sh(["nft", "-f", p], timeout=30)
    except OSError:
        _log("nft_failsafe", source="write_failed")
        return "none"
    r = _sh(["nft", "list", "table"] + NFT_TABLE.split(), timeout=30)
    ok = r is not None and r.returncode == 0
    _log("nft_failsafe", source="minimal" if ok else "minimal_failed")
    return "minimal" if ok else "none"


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


def degraded_services():
    """返回没在运行的核心服务名列表。

    只用于「无漂移」路径判断失败标记能不能清：那条路径不重启任何服务，
    如果开机时 networkd/dnsmasq/pppoe 根本没起来，这里必须看得出来。
    读配置失败时按「不检查 pppoe」处理——宁可少报一个服务，
    也不能因为读不到配置就把标记永久留着（那会让告警永远下不去）。
    """
    names = ["systemd-networkd", "dnsmasq"]
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            mode = (json.load(f).get("wan") or {}).get("mode")
    except Exception:
        mode = None
    if mode == "pppoe":
        names.append("dert-pppoe")
    bad = []
    for n in names:
        r = _sh(["systemctl", "is-active", "--quiet", n + ".service"], timeout=10)
        if r is None or r.returncode != 0:
            bad.append(n)
    return bad


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
def retire_old_prefixes():
    """把「见过但现在不是当前」的 IPv6 前缀登记为已废弃（自愈兜底）。

    换前缀的 netlink 事件可能漏收——真机 2026-11-14 01:58 之后那一次
    a54:13e0 → a54:1c10 就没触发钩子，旧前缀没被登记，macOS 继续拿它当源
    地址发包 → 黑洞，表现为"重拨后 IPv6 打不开，重连 Wi-Fi 才好"。
    这里每次收敛兜一次底：最坏 6 小时（converge.timer）自愈，配置变更时立刻。
    """
    script = os.path.join(LIB, "prefix-retire.py")
    if os.path.isfile(script):
        _sh([sys.executable, script], timeout=30)


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
                    failsafe_nft()
                    shutil.rmtree(stage, ignore_errors=True)
                    die("CONFIG_CORRUPT", "自动迁移后仍无法生成配置",
                        "请按上方错误信息在管理页面修正" +
                        ("。生成器输出：%s" % gen_err2 if gen_err2 else ""))
        if not migrated:
            sys.stdout.write(out)  # 透传 netgen 的可操作错误（复审 R-03）
            mark_converge_failed()
            failsafe_nft()
            shutil.rmtree(stage, ignore_errors=True)
            die("CONFIG_CORRUPT", "配置生成失败",
                "请按上方错误信息在管理页面修正；或从 /var/lib/dert/lastgood 恢复上一份可用配置" +
                ("。生成器输出：%s" % gen_err if gen_err else ""))

    drift = None          # None = 不知道改了什么（非收敛调用 / 清单读不出来）
    if convergent:
        drift = drift_list(man, stage)
        if drift == []:
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
            ensure_own_units()
            # 清标记前必须确认服务真的在跑。开机时 dert-apply 失败（标记已置上），
            # 15 分钟后首次收敛因为「文件早就部署出去了」判定无漂移，走到这里——
            # 无条件清标记就会把「服务没起来」的告警抹掉，而问题一点没变（审计 N4）。
            _bad = degraded_services()
            if _bad:
                mark_converge_failed()
                _log("converge_degraded", services=",".join(_bad))
            else:
                clear_converge_failed()
            # stage 必须删：这是"无漂移"的提前返回，早于下方全量路径的清理。
            # 漏了它 = 每次开机/6h 收敛/每次升级都在 /run/dert 留一份完整生成物，
            # 其中 etc/ppp/chap-secrets 是明文宽带密码（真机 2026-11-09 实测）。
            shutil.rmtree(stage, ignore_errors=True)
            retire_old_prefixes()
            ok_out(message="无漂移，运行时已确认", converged=True)
            return

    good, pf_out = preflight(stage)
    if not good:
        mark_converge_failed()
        failsafe_nft()
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
    failed = run_actions(actions_for(drift))
    if not wait_dnsmasq(15):
        failed.append("dnsmasq")
    sync_dns_upstream()
    maintain_singbox_dropin()
    ensure_own_units()
    retire_old_prefixes()
    # 服务没起来就不能报成功：备份恢复/恢复出厂都靠这条流水线的退出码决定
    # 是否回滚。以前这里把 run_actions 的返回值丢掉、wait_dnsmasq 的结果也不看，
    # 于是「恢复了一份网口名不匹配的备份」会提示「已恢复并生效」，设备直接失联。
    if failed:
        mark_converge_failed()
        die("APPLY_PARTIAL", "部分服务未能启动：%s" % ",".join(failed),
            "journalctl -u systemd-networkd -u dnsmasq -n 50 查看原因；"
            "若刚恢复了备份，请换一份与本机网口匹配的备份重试")
    clear_converge_failed()
    ok_out(message="已按配置完成系统收敛" + ("。" + migrated if migrated else ""))


def main(argv):
    arg = argv[1] if len(argv) > 1 else ""
    apply_all(convergent=(arg == "--convergent"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
