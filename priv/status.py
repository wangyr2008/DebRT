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
import ipaddress, json, os, re, subprocess, time

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


def _wan_dns():
    """概览「DNS 服务器」展示用的上游。

    放在这里而不是面板里，是因为 **config.json 是 root-only**（或至少不该假设
    非 root 可读），而面板以 dert 用户运行。以前面板直接 open() 配置，读失败
    被 except 吞掉返回空列表，概览就一直显示 —，不报错（真机 2026-11-19）。
    本模块由 dert-priv 以 root 调用，读配置与 /run 都没有权限问题。

    手动模式取配置里的 dns；自动模式取 dispatcher 维护的 upstream.conf
    （dnsmasq 的 servers-file，与它实际使用的上游是同一份）。
    """
    w = cfg.get("wan", {})
    if w.get("dns_mode") == "manual" and w.get("dns"):
        return list(w["dns"])
    out = []
    try:
        with open(_FS + "/run/dert-dns/upstream.conf", encoding="utf-8") as f:
            for line in f:
                if line.startswith("server="):
                    d = line.strip()[len("server="):]
                    if d and d not in out:
                        out.append(d)
    except OSError:
        pass
    return out


wan["dns"] = _wan_dns()

# WAN 地址是否落在 LAN 网段内（审计 RT-17 的运行期部分）。
# 配置层只能校验「静态 WAN」，DHCP 拿到的地址只有运行时才知道。
# 两者同网段时路由会冲突：发往 LAN 客户端的包可能被送去 WAN，整机不通，
# 而面板上一切显示正常——最难查的那类。
wan["lan_conflict"] = False
try:
    _lan = cfg.get("lan", {})
    if wan.get("ip") and _lan.get("ip") and _lan.get("mask"):
        _net = ipaddress.IPv4Network("%s/%s" % (_lan["ip"], _lan["mask"]),
                                     strict=False)
        wan["lan_conflict"] = ipaddress.IPv4Address(wan["ip"]) in _net
except Exception:
    pass

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

# 委派前缀长度：networkd 为 PD 在 lo 上放一条 unreachable 路由
#   unreachable 2409:8a5e:a57:6060::/60 dev lo proto dhcp metric 1024
# 这是「本机实际拿到多长的前缀」最直接的证据（比猜 links 文件里的字段可靠）。
def _pd_prefixlens():
    """所有 PD 路由的前缀长度。

    运营商可能下发多个 PD，切换上网方式时旧路由也可能还没清掉，所以**不能
    只看第一条**（审计 N2）。返回空列表 = 还没拿到 PD。
    """
    try:
        out = run(["ip", "-6", "route", "show", "type", "unreachable"])
    except Exception:
        return []
    lens = []
    for line in out.splitlines():
        if " dev lo " in line and " proto dhcp" in line:
            m = re.search(r"/(\d+)", line)
            if m:
                lens.append(int(m.group(1)))
    return lens


v6["pd_prefixlen"] = 0
v6["pd_too_small"] = False
if v6["enabled"] and v6["mode"] == "native":
    _lens = _pd_prefixlens()
    # 展示用最宽的那个（子网最多），与「够不够用」的判断分开
    v6["pd_prefixlen"] = min(_lens) if _lens else 0
    # LAN 取 PD 里的第 subnet_id 个子网（netgen 默认 1，避开 WAN 的 IA_NA 地址）。
    # /64 委派只有子网 0，而那一个已经被 WAN 占了——此时 LAN 拿不到任何独立
    # 前缀，调 SubnetId 也挤不出来，只能改用 NAT6（审计 RT-05）。
    # 与 netgen.lan_subnet_id() 同一套默认值与范围（0 是合法值，不能写 `or 1`）
    try:
        _sid = int(v6cfg.get("subnet_id", 1))
    except (TypeError, ValueError):
        _sid = 1
    if not 0 <= _sid <= 15:
        _sid = 1
    # 需要多少位子网号：sid=1 → 1 位（两个子网），sid=2..3 → 2 位，以此类推。
    # **只要有一条 PD 放得下就不算太短**——取「最长前缀」会误报：
    # 同时拿到 /60 和 /64 时，/60 已经够用了（审计 N2 的建议方向反了）。
    _need = _sid.bit_length()
    if _lens and not any((64 - p) >= _need for p in _lens):
        v6["pd_too_small"] = True

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
# sing-box 的 API / dashboard 是否对局域网开放、口令是否太弱。
# 这份配置属于 sub-store（DebRT 只读），所以这里不改它，只把风险说出来——
# 面板会据此显示一条告警，并指出该改哪个字段（审计 §5.2）。
_WEAK_SECRETS = {"", "passwd", "password", "123456", "admin", "singbox", "clash", "dert"}


def _singbox_api_risk():
    try:
        with open(_FS + "/etc/sing-box/config.json", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {"listen": "", "port": 0, "weak": False}
    listen, port, secret = "", 0, ""
    for svc in cfg.get("services") or []:
        if svc.get("type") == "api":
            listen = str(svc.get("listen") or "0.0.0.0")
            port = int(svc.get("listen_port") or 0)
            secret = str(svc.get("secret") or "")
            break
    else:
        ca = (cfg.get("experimental") or {}).get("clash_api") or {}
        ctl = str(ca.get("external_controller") or "")
        if not ctl:
            return {"listen": "", "port": 0, "weak": False}
        listen, _, p = ctl.partition(":")
        port = int(p) if p.isdigit() else 0
        secret = str(ca.get("secret") or "")
    # 只听回环就不算暴露（127.0.0.1 / ::1 / localhost）
    exposed = listen not in ("127.0.0.1", "::1", "localhost")
    weak = secret.lower() in _WEAK_SECRETS or len(secret) < 8
    return {"listen": listen, "port": port, "weak": bool(exposed and weak)}


_api_risk = _singbox_api_risk()

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
    "singbox_api": _api_risk,
}, ensure_ascii=False))
