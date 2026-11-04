#!/usr/bin/env python3
"""smoke：/api/singbox 状态断言（mock 数据，v0.1.32 工具页回归守卫）。stdin 传 JSON。"""
import json
import sys

d = json.load(sys.stdin)
assert d.get("ok"), d
assert d.get("installed") and d.get("active"), d
assert d.get("version"), d
for k in ("url_set", "autoupdate", "update", "endpoint", "dashboard"):
    assert k in d, k
assert d["autoupdate"]["on"] and d["autoupdate"]["time"], d["autoupdate"]
assert d["endpoint"]["present"], d["endpoint"]
print("singbox status ok:", d["version"], d["autoupdate"])
