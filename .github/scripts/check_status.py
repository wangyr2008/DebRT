#!/usr/bin/env python3
"""CI 冒烟：校验 /api/status 真实链路（ok、WAN 模式、统计字段存在）。
注意：system_ok 在 --smoke 模式恒为 false（刻意不建 nft 表，避免断 runner SSH），不作断言。"""
import json
import sys

d = json.load(sys.stdin)
assert d.get("ok"), d
assert d["wan"]["mode"] == "dhcp", d
print("HTTP 登录 + status OK，leases:", d["stats"]["devices"])
