#!/usr/bin/env python3
"""租约合并单测：损坏行跳过 / exp=0 永久 / 静态绑定与租约同 MAC 时保留徽标（十审 P1-2 回归守卫）。"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)                      # repo 根
sys.path.insert(0, os.path.join(_ROOT, "panel"))
import sysinfo  # noqa: E402

d = tempfile.mkdtemp()
sysinfo._lease_file = os.path.join(d, "leases")
with open(sysinfo._lease_file, "w") as f:
    f.write("1793600000 00:11:22:33:44:55 192.168.10.10 nas *\n")   # 正常
    f.write("broken-line 00:11:22:33:44:56 192.168.10.11 x *\n")    # 损坏行 → 跳过
    f.write("0 00:11:22:33:44:57 192.168.10.12 infinite *\n")       # exp=0 → 永久

static = [{"name": "nas", "ip": "192.168.10.10", "mac": "00:11:22:33:44:55"}]
r = sysinfo.leases(static)

assert len(r) == 2, f"应只有 2 条（损坏行跳过）: {r}"
nas = next(x for x in r if x["mac"] == "00:11:22:33:44:55")
assert nas["static"] is True, f"静态徽标丢失（P1-2 回归）: {nas}"
assert nas["lease_text"] == "剩余 0 分钟" or "分钟" in nas["lease_text"], nas
inf = next(x for x in r if x["mac"] == "00:11:22:33:44:57")
assert inf["lease_text"] == "永久", inf
assert all(x["mac"] != "00:11:22:33:44:56" for x in r), "损坏行混入"
print("leases 单测 3/3 通过：损坏行跳过 / 永久 / 静态徽标保留")
