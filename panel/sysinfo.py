#!/usr/bin/env python3
"""SeedRou 本机信息采集（面板用户即可读：/proc、/sys、租约文件、ARP 表）。

特权/聚合数据走 seedrou-priv status；本模块只做无特权读取 + mock 数据。
"""
import json
import os
import re
import time

MOCK = False
_lease_file = "/var/lib/misc/dnsmasq.leases"   # Debian dnsmasq 默认租约路径（审计 B-04）
_notes_file = "/var/lib/seedrou/notes.json"

_cpu_prev = None  # (ts, idle, total)
_cpu_last = None  # 上一次成功计算的占用率（采样间隔过短时复用）


def init(mock=False):
    global MOCK
    MOCK = mock


# ---------------- mock 数据 ----------------
_MOCK_STATUS = {
    "ok": True,
    "converge_failed": False,
    "wan": {"mode": "dhcp", "state": "up", "ip": "219.142.78.166", "mask": "255.255.255.0",
            "gw": "219.142.78.1", "session_s": 2 * 86400 + 14 * 3600, "connecting": False,
            "dns": ["223.5.5.5", "119.29.29.29"]},
    "ipv6": {"enabled": True, "mode": "nat6", "addr": "fda1:9e3a:8b7f::1（ULA）", "dns": ["240c::6666"]},
    "ports": [
        {"name": "eth0", "role": "wan", "link": True, "speed": "1000 Mbps"},
        {"name": "eth1", "role": "lan", "link": True, "speed": "1000 Mbps"},
        {"name": "eth2", "role": "lan", "link": True, "speed": "100 Mbps"},
        {"name": "eth3", "role": "lan", "link": False, "speed": ""},
        {"name": "eth4", "role": "none", "link": False, "speed": ""},
    ],
    "lan": {"ip": "192.168.10.1", "mask": "255.255.255.0"},
    "version": "0.1.1",
    "system_ok": True,
    "leases_count": 5,
}

_MOCK_CONFIG = {
    "ports": [
        {"name": "eth0", "wan": True, "lan": False},
        {"name": "eth1", "wan": False, "lan": True},
        {"name": "eth2", "wan": False, "lan": True},
        {"name": "eth3", "wan": False, "lan": True},
        {"name": "eth4", "wan": False, "lan": False},
    ],
    "wan": {"mode": "dhcp", "username": "", "dns_mode": "auto", "dns": [],
            "ip": "", "mask": "", "gw": "", "has_password": True},
    "ipv6": {"enabled": True, "mode": "nat6", "dns_mode": "auto", "dns": [], "addr": "", "gw": "", "prefix": ""},
    "lan": {"ip": "192.168.10.1", "mask": "255.255.255.0"},
    "dhcp": {"enabled": True, "pool_start": 100, "pool_end": 200, "lease": 120, "dns1": "", "dns2": "", "gw": ""},
    "static": [
        {"name": "iPhone 15 Pro", "ip": "192.168.10.23", "mac": "F0:18:98:2A:7C:41"},
        {"name": "群晖 NAS", "ip": "192.168.10.10", "mac": "00:11:32:4D:8C:2E"},
    ],
    "qos": {"enabled": True, "down": 500, "up": 50},
    "firewall": {"wan_input_drop": True, "portfw": [
        {"id": "r1", "name": "NAS 外网访问", "proto": "tcp", "wport": "443", "ip": "192.168.10.10", "lport": "443", "enabled": True},
    ]},
    "upgrade": {"source": "yourname/seedrou"},
}

_MOCK_LEASES = [
    {"name": "iPhone 15 Pro", "ip": "192.168.10.23", "mac": "F0:18:98:2A:7C:41", "static": True, "online": True, "lease_text": "剩余 1 天 23 小时", "note": "老王的手机"},
    {"name": "MacBook Pro", "ip": "192.168.10.35", "mac": "A4:83:E7:1B:09:D6", "static": False, "online": True, "lease_text": "剩余 3 天 4 小时", "note": "工作笔记本"},
    {"name": "群晖 NAS", "ip": "192.168.10.10", "mac": "00:11:32:4D:8C:2E", "static": True, "online": True, "lease_text": "剩余 6 天 12 小时", "note": "家庭文件服务器"},
    {"name": "小米音箱", "ip": "192.168.10.42", "mac": "64:09:80:5C:11:A9", "static": False, "online": False, "lease_text": "剩余 5 小时", "note": ""},
]

_MOCK_CONTAINERS = [
    {"name": "nginx-proxy", "image": "docker.io/library/nginx:1.27-alpine", "state": "running", "ports": ["8080 → 80"]},
    {"name": "home-assistant", "image": "ghcr.io/home-assistant/home-assistant:2025.10", "state": "running", "ports": ["8123 → 8123"]},
    {"name": "pihole", "image": "docker.io/pihole/pihole:2025.08.0", "state": "exited", "ports": ["53 → 53"]},
]

_MOCK_IMAGES = [
    {"name": "docker.io/library/nginx:1.27-alpine", "size_text": "52 MB", "used_by": 1},
    {"name": "ghcr.io/home-assistant/home-assistant:2025.10", "size_text": "1.8 GB", "used_by": 1},
    {"name": "docker.io/pihole/pihole:2025.08.0", "size_text": "310 MB", "used_by": 1},
    {"name": "docker.io/library/debian:bookworm-slim", "size_text": "74 MB", "used_by": 0},
]


def mock_status():
    import copy
    st = copy.deepcopy(_MOCK_STATUS)
    st["stats"] = {"devices": 12, "cpu": 12, "cpu_temp": 45, "mem": 38, "disk_temp": 41,
                   "uptime_s": 15 * 86400 + 6 * 3600}
    return st


def mock_config():
    import copy
    return copy.deepcopy(_MOCK_CONFIG)


# ---------------- 真实采集 ----------------
def cpu_pct():
    global _cpu_prev, _cpu_last
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
    except Exception:
        return _cpu_last
    now = time.time()
    if _cpu_prev and now - _cpu_prev[0] >= 0.5:
        d_idle = idle - _cpu_prev[1]
        d_total = total - _cpu_prev[2]
        _cpu_prev = (now, idle, total)
        if d_total > 0:
            _cpu_last = max(0, min(100, round(100 * (1 - d_idle / d_total))))
        return _cpu_last
    if _cpu_prev is None:
        # 首次调用：短间隔补采一次，立即出值而不是显示 "-"（用户实测反馈）
        time.sleep(0.2)
        try:
            with open("/proc/stat") as f:
                parts2 = f.readline().split()[1:]
            vals2 = [int(x) for x in parts2]
            idle2 = vals2[3] + (vals2[4] if len(vals2) > 4 else 0)
            total2 = sum(vals2)
        except Exception:
            _cpu_prev = (now, idle, total)
            return None
        d_idle = idle2 - idle
        d_total = total2 - total
        _cpu_prev = (now, idle2, total2)
        if d_total > 0:
            _cpu_last = max(0, min(100, round(100 * (1 - d_idle / d_total))))
        return _cpu_last
    # 采样间隔 <0.5s（快速切页/连续调用）：刷新基准并返回上次计算值
    # （复审 P3-2：原注释"保留基准"与实现不符；尾部 return None 是死代码）
    _cpu_prev = (now, idle, total)
    return _cpu_last


def mem_pct():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return round(100 * (total - avail) / total)
    except Exception:
        return None


def temp_cpu():
    for base in ("/sys/class/thermal",):
        try:
            zones = sorted(os.listdir(base))
        except Exception:
            return None
        best = None
        for z in zones:
            try:
                ztype = open(f"{base}/{z}/type").read().strip()
            except Exception:
                continue
            if re.search(r"x86_pkg_temp|cpu|soc", ztype, re.I) and best is None:
                try:
                    best = round(int(open(f"{base}/{z}/temp").read().strip()) / 1000)
                except Exception:
                    pass
        return best
    return None


def uptime_s():
    try:
        return int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        return None


def arp_online_macs():
    """近期在线（ARP 表项完整）的 MAC 集合。"""
    macs = set()
    try:
        with open("/proc/net/arp") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) >= 6 and parts[2] == "0x2" and parts[3] != "00:00:00:00:00:00":
                    macs.add(parts[3].upper())
    except Exception:
        pass
    return macs


def load_notes():
    try:
        with open(_notes_file) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notes(notes: dict):
    os.makedirs(os.path.dirname(_notes_file), exist_ok=True)
    # P3-08：tmp 名唯一化——ThreadingHTTPServer 下并发"保存备注"若共用固定 .tmp，
    # 两个 "w" 句柄会交错写坏 JSON，load_notes 静默返回 {} 全部备注消失
    # （与 auth._persist_revoked 同类问题，v0.1.50 修了那处，这里补齐）
    tmp = f"{_notes_file}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w") as f:
        json.dump(notes, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _notes_file)


def fmt_lease_left(expiry: int, now: int) -> str:
    left = max(0, expiry - now)
    d, h = left // 86400, left % 86400 // 3600
    if d:
        return f"剩余 {d} 天 {h} 小时"
    m = left % 3600 // 60
    if h:
        return f"剩余 {h} 小时 {m} 分钟"
    return f"剩余 {m} 分钟"


def leases(static_list):
    """合并 dnsmasq 租约 + 静态绑定 + 备注 + 在线状态。"""
    now = int(time.time())
    notes = load_notes()
    online = arp_online_macs()
    items = {}
    try:
        with open(_lease_file) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    exp = int(parts[0])           # 损坏行直接跳过，不让整页 500
                except ValueError:
                    continue
                mac, ip, host = parts[1].upper(), parts[2], parts[3]
                # expiry=0 是 dnsmasq 的无限租约，显示"永久"
                left = "永久" if exp == 0 else fmt_lease_left(exp, now)
                items[mac] = {"name": None if host == "*" else host, "ip": ip, "mac": mac,
                              "static": False, "lease_text": left}
    except FileNotFoundError:
        pass
    for s in static_list:
        mac = str(s.get("mac") or "").upper()   # None/缺字段安全（str(None)="NONE" 的坑）
        if not mac:
            continue
        it = items.get(mac)
        if it:
            # 已有租约：只补静态标记与绑定信息，保留租期文本（复审 P1-2）
            it.update({"static": True, "name": s.get("name") or it.get("name"),
                       "ip": s.get("ip") or it.get("ip")})
        else:
            # 离线静态绑定：无租约也进列表
            items[mac] = {"name": s.get("name", ""), "ip": s.get("ip", ""), "mac": mac,
                          "static": True, "lease_text": ""}
    out = []
    for mac in sorted(items):
        it = items[mac]
        it["online"] = mac in online
        it["note"] = notes.get(mac, "")
        out.append(it)
    return out
