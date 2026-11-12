#!/usr/bin/env python3
"""状态采集（v0.1.80 从 dert-priv 抽出）。

抽出的理由：这段本来就是纯 Python，嵌在 bash 的 heredoc 里既拉长了 dert-priv，
又让它可以被行为测试直接跑（`python3 priv/status.py` + DERT_FSROOT）。
逻辑一字未改，只是换了住处。
"""
import os as _o
# 从 DERT_FSROOT 自己推导数据根：这样本模块既能被 dert-priv 调用（那里 lib.sh
# 也导出 DERT_ETC 等），也能直接 `python3 priv/xxx.py` 跑（测试用），两边一致。
_FS = _o.environ.get("DERT_FSROOT", "")
E = _FS + "/etc/dert"
V = _FS + "/var/lib/dert"
R = _FS + "/run/dert"
import json, os, subprocess, time

cfg = json.load(open(E + "/config.json"))
ULA_PREFIX_STATIC = "fda1:9e3a:8b7f"
wans = [p["name"] for p in cfg.get("ports", []) if p.get("wan")]
wans_set = set(wans) | ({"ppp0"} if cfg.get("wan", {}).get("mode") == "pppoe" else set())

def run(args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""

# WAN
mode = cfg.get("wan", {}).get("mode", "")
wan = {"mode": mode, "state": "down", "ip": "", "mask": "", "gw": "", "session_s": None, "connecting": False}
try:
    routes = json.loads(run(["ip", "-j", "route", "show", "default"]) or "[]")
except Exception:
    routes = []
wan_dev = next((r.get("dev") for r in routes if r.get("dev") in wans_set), "")
if wan_dev:
    wan["state"] = "up"
    try:
        addrs = json.loads(run(["ip", "-j", "-4", "addr", "show", "dev", wan_dev]) or "[]")
    except Exception:
        addrs = []
    for a in addrs:
        info = (a.get("addr_info") or [{}])[0]
        wan["ip"] = info.get("local", "")
        # B-10：映射表缺 /26 /27 /20 等 → 概览显示 —；直接用 ipaddress 全量计算
        try:
            import ipaddress as _ipa
            wan["mask"] = str(_ipa.IPv4Network(f"0.0.0.0/{int(info.get('prefixlen', 32))}").netmask)
        except Exception:
            wan["mask"] = ""
    wan["gw"] = next((r.get("gateway", "") for r in routes if r.get("dev") == wan_dev), "")
    if mode == "pppoe":
        # 真机反馈：换绑 WAN 口后旧 ppp0 会话要等 LCP 超时（约 60s）才消失，期间概览
        # 仍显示"已联网"。以 WAN 口载波为准：无载波立即判未连接（不再吃陈旧路由的老本）
        _wport = next((p.get("name") for p in cfg.get("ports", []) if p.get("wan")), "")
        _car = ""
        try:
            _car = open(f"/sys/class/net/{_wport}/carrier").read().strip()
        except Exception:
            _car = ""   # 口子不存在（刚解绑）或未 up：视为未连接
        if _car != "1":
            wan["state"] = "down"
            wan_dev = ""
            wan["ip"] = wan["mask"] = wan["gw"] = ""
    if mode == "pppoe" and not wan["gw"]:
        # PPPoE 点对点链路没有网关概念（default dev ppp0 scope link），概览显示对端地址
        try:
            _pr = json.loads(run(["ip", "-j", "-4", "route", "show", "dev", "ppp0"]) or "[]")
            wan["gw"] = next((r.get("dst") for r in _pr if r.get("dst") and r.get("dst") != "default"), "")
        except Exception:
            pass
    if mode == "pppoe":
        # 会话起点由 pppd ip-up.d 钩子写入（掉线重拨即归零），不再用服务启动时间（persist 下不准，审计 B-22）
        try:
            st = os.stat(R + "/ppp-session-start")
            wan["session_s"] = max(0, int(time.time() - st.st_mtime))
        except OSError:
            wan["session_s"] = None
else:
    wan["connecting"] = mode == "pppoe"

# IPv6 概览
v6cfg = cfg.get("ipv6", {})
v6 = {"enabled": v6cfg.get("enabled", False), "mode": v6cfg.get("mode", "native"), "addr": "", "dns": []}
if v6["enabled"]:
    if v6["mode"] == "nat6":
        # NAT6 的语义就是 LAN 侧 ULA + 出向伪装，概览显示 ULA（五审 P-10）
        v6["addr"] = f"{ULA_PREFIX_STATIC}::1（ULA）"
    else:
        dev = wan_dev or "br-lan"
        out = run(["ip", "-j", "-6", "addr", "show", "dev", dev, "scope", "global"])
        try:
            for a in json.loads(out or "[]"):
                for ai in a.get("addr_info", []):
                    if not ai.get("local", "").startswith("fe80") and not ai.get("local", "").startswith("fda1:9e3a"):
                        v6["addr"] = ai.get("local", ""); break
                if v6["addr"]: break
        except Exception:
            pass
    # B-02：概览"IPv6 DNS"此前永远为 —（dns 初始化后再未赋值，mock 有值真机空）——
    # 从 networkd links 文件取 RA/DHCPv6 学到的 IPv6 DNS
    try:
        _wdev = wan_dev or "br-lan"
        _idx = open(f"/sys/class/net/{_wdev}/ifindex").read().strip()
        for _line in open(f"/run/systemd/netif/links/{_idx}"):
            if _line.startswith("DNS="):
                for _d in _line.strip()[4:].split(" "):
                    if _d and ":" in _d and _d not in v6["dns"]:
                        v6["dns"].append(_d)
    except Exception:
        pass

# 网口
ports = []
for p in cfg.get("ports", []):
    n = p["name"]
    base = f"/sys/class/net/{n}"
    oper = ""
    try:
        oper = open(base + "/operstate").read().strip()
    except Exception:
        pass
    speed = ""
    try:
        speed = open(base + "/speed").read().strip()
        speed = f"{speed} Mbps" if speed and speed != "-1" else ""
    except Exception:
        speed = ""
    ports.append({"name": n, "role": "wan" if p.get("wan") else ("lan" if p.get("lan") else "none"),
                  "link": oper == "up", "speed": speed})

# 系统
def rc(args, timeout=5):
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout).returncode
    except Exception:
        return 1
dnsmasq_ok = rc(["systemctl", "is-active", "--quiet", "dnsmasq.service"]) == 0
networkd_ok = rc(["systemctl", "is-active", "--quiet", "systemd-networkd.service"]) == 0
nft_ok = rc(["nft", "list", "table", "inet", "dert_base"]) == 0
# 概览"已连接设备"：只算 br-lan 上当前在线的邻居。
# 旧逻辑直接数 dnsmasq.leases 行数，把"已下线但租约未过期"的设备也计入，
# 数字只增不减（用户反馈：统计的是所有上过线的设备）。
# 判据取舍（真机实测 2026-11-05）：租约剩余时间无法区分在线/离线——13 条租约里
# 剩余 119 分钟的没在线、剩余 90 分钟的在线的都有（dnsmasq 租期 120 分钟，离线
# 设备也会保留到接近到期）。
# B-05：与面板设备列表（sysinfo.arp_online_macs）统一用 /proc/net/arp 的 flags
# 0x2（ARP 完整，含 STALE）——两套实现将来调判据容易改一处漏一处；这里额外按
# br-lan 过滤，避免把 WAN 侧网关算成"已连接设备"。
leases = 0
try:
    _live = set()
    with open("/proc/net/arp") as _af:
        next(_af, None)
        for _line in _af:
            _p = _line.split()
            if len(_p) >= 6 and _p[2] == "0x2" and _p[3] != "00:00:00:00:00:00" and _p[5] == "br-lan":
                _live.add(_p[3].upper())
    leases = len(_live)
except Exception:
    # 退化路径：/proc 不可读时仍给个数（宁可粗也不能空）
    try:
        with open("/var/lib/misc/dnsmasq.leases") as f:
            leases = sum(1 for _ in f)
    except Exception:
        pass

# 硬盘温度：smartctl 需要 root（复审 N-05），随 status 一并采集；
# 60 秒文件缓存——5 秒轮询下不至于持续唤醒机械盘（复审 R-05）
disk_temp = None
try:
    import os as _os
    cache = R + "/disk-temp"
    now = time.time()
    cached = None
    try:
        if now - _os.stat(cache).st_mtime < 60:
            cached = open(cache).read().strip()
            disk_temp = int(cached) if cached.lstrip("-").isdigit() else None
    except (OSError, ValueError):
        pass
    if cached is None:
        import shutil as _sh
        devices = []
        if _sh.which("smartctl"):  # P3-3：扫描只做一次（此前 elif 与下方 if 各扫一次）
            devices = json.loads(subprocess.run(["smartctl", "--scan", "-j"], capture_output=True, text=True, timeout=8).stdout or "{}").get("devices") or []
        # C-10：诊断标记内容不变不重写——status 每 3s 轮询（缓存 miss 即走到这里），
        # smartctl 长期缺失时无条件重写是纯写放大
        if not _sh.which("smartctl"):
            _note = "smartmontools 未安装"
        elif not devices:
            _note = "未找到磁盘"
        else:
            _note = ""
        if _note:
            try:
                _unchanged = open(R + "/disk-unavailable").read() == _note
            except OSError:
                _unchanged = False
            if not _unchanged:
                with open(R + "/disk-unavailable", "w") as f:
                    f.write(_note)
        else:
            # P3-1：采集正常时清掉历史诊断标记——否则补装 smartmontools 后
            # tooltip 仍显示"未安装"（/run 只在重启时才清）
            try:
                os.remove(R + "/disk-unavailable")
            except OSError:
                pass
        if devices:
            dev = (devices or [{}])[0].get("name", "")
            if dev:
                info = subprocess.run(["smartctl", "-A", "-j", dev], capture_output=True, text=True, timeout=8).stdout
                temps = json.loads(info or "{}").get("temperature", {})
                # P3-11：只取 current——temperature 字典里还有 drive_trip（阈值，
                # 常见 60/70），current 缺失时遍历全部值会把"阈值"当"当前温度"显示
                _cur = temps.get("current")
                if isinstance(_cur, (int, float)) and 0 < _cur < 120:
                    disk_temp = int(_cur)
        _os.makedirs(R, exist_ok=True)
        with open(cache + ".tmp", "w") as f:
            f.write(str(disk_temp if disk_temp is not None else ""))
        _os.replace(cache + ".tmp", cache)
except Exception:
    disk_temp = None

ver = "0.0.0"
for vp in ("/usr/local/lib/dert/share/VERSION", "/usr/local/lib/dert/VERSION"):
    try:
        ver = open(vp).read().strip()
        break
    except OSError:
        continue
print(json.dumps({
    "ok": True,
    "converge_failed": os.path.exists(R + "/converge-failed"),
    "wan": wan, "ipv6": v6, "ports": ports,
    "lan": cfg.get("lan", {}),
    "version": ver,
    "system_ok": bool(dnsmasq_ok and networkd_ok and nft_ok),
    "disk_temp": disk_temp,
    "disk_temp_note": (open(R + "/disk-unavailable").read().strip() if os.path.exists(R + "/disk-unavailable") else ""),
    "leases_count": leases,
}, ensure_ascii=False))
