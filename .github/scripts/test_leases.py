#!/usr/bin/env python3
"""租约合并单测：损坏行跳过 / exp=0 永久 / 静态绑定与租约同 MAC 时保留徽标
（十审 P1-2 回归守卫）；v0.1.116 起还要合并「只在 ARP 里、不走 DHCP」的静态 IP 设备。

两个输入文件都指向临时目录——不读真机 /proc/net/arp，否则 CI runner 的 ARP 表里
有什么设备会让结果条数不确定。
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)                      # repo 根
sys.path.insert(0, os.path.join(_ROOT, "panel"))
import sysinfo  # noqa: E402

d = tempfile.mkdtemp()
sysinfo._lease_file = os.path.join(d, "leases")
sysinfo._arp_file = os.path.join(d, "arp")
sysinfo._notes_file = os.path.join(d, "notes.json")

with open(sysinfo._lease_file, "w") as f:
    f.write("1793600000 00:11:22:33:44:55 192.168.10.10 nas *\n")   # 正常
    f.write("broken-line 00:11:22:33:44:56 192.168.10.11 x *\n")    # 损坏行 → 跳过
    f.write("0 00:11:22:33:44:57 192.168.10.12 infinite *\n")       # exp=0 → 永久

with open(sysinfo._arp_file, "w") as f:
    f.write("IP address       HW type     Flags       HW address            "
            "Mask     Device\n")
    f.write("192.168.10.128   0x1  0x2  8c:de:f9:32:fb:48  *  br-lan\n")   # 静态 IP 设备
    f.write("192.168.10.150   0x1  0x0  aa:bb:cc:dd:ee:03  *  br-lan\n")   # 未解析完整 → 忽略

static = [{"name": "nas", "ip": "192.168.10.10", "mac": "00:11:22:33:44:55"}]
r = sysinfo.leases(static)

assert len(r) == 3, f"应为 3 条（2 条租约 + 1 台静态 IP 设备）: {r}"
nas = next(x for x in r if x["mac"] == "00:11:22:33:44:55")
assert nas["static"] is True, f"静态徽标丢失（P1-2 回归）: {nas}"
assert nas["no_dhcp"] is False, f"有租约的设备不该标 no_dhcp: {nas}"
assert nas["lease_text"] == "剩余 0 分钟" or "分钟" in nas["lease_text"], nas
inf = next(x for x in r if x["mac"] == "00:11:22:33:44:57")
assert inf["lease_text"] == "永久", inf
assert all(x["mac"] != "00:11:22:33:44:56" for x in r), "损坏行混入"

arp_only = next(x for x in r if x["mac"] == "8C:DE:F9:32:FB:48")
assert arp_only["no_dhcp"] is True and arp_only["online"] is True, arp_only
assert arp_only["ip"] == "192.168.10.128", arp_only
assert all(x["mac"] != "AA:BB:CC:DD:EE:03" for x in r), "未解析完整的 ARP 条目混入"

print("leases 单测 5/5 通过：损坏行跳过 / 永久 / 静态徽标保留 / 静态 IP 设备合并 / 未解析 ARP 忽略")
