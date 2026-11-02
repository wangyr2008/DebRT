#!/usr/bin/env python3
"""CI 冒烟：校验 /api/config 真实链路（ok、默认 LAN、has_password 派生字段）。"""
import json
import sys

d = json.load(sys.stdin)
assert d.get("ok"), d
cfg = d["config"]
assert cfg["wan"]["mode"] == "dhcp", cfg["wan"]
assert cfg["lan"]["ip"] == "192.168.10.1", cfg["lan"]
assert "has_password" in cfg["wan"], cfg["wan"]
print("config-get OK")
