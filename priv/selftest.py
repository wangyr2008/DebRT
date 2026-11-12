#!/usr/bin/env python3
"""自检（v0.1.80 从 dert-priv 抽出）。

抽出的理由：这段本来就是纯 Python，嵌在 bash 的 heredoc 里既拉长了 dert-priv，
又让它可以被行为测试直接跑（`python3 priv/selftest.py` + DERT_FSROOT）。
逻辑一字未改，只是换了住处。
"""
import os as _o
# 从 DERT_FSROOT 自己推导数据根：这样本模块既能被 dert-priv 调用（那里 lib.sh
# 也导出 DERT_ETC 等），也能直接 `python3 priv/xxx.py` 跑（测试用），两边一致。
_FS = _o.environ.get("DERT_FSROOT", "")
E = _FS + "/etc/dert"
V = _FS + "/var/lib/dert"
R = _FS + "/run/dert"
import json, os, shutil, subprocess, sys
checks = []
def add(name, ok, hint=""):
    checks.append({"name": name, "ok": bool(ok), "hint": hint})
try:
    json.load(open(E + "/config.json"))
    add("config.json 存在且可解析", True)
except Exception as e:
    add("config.json 存在且可解析", False, f"解析失败：{e}；可从 /var/lib/dert/lastgood 恢复")
if os.path.exists("/etc/resolv.conf"):
    rc = open("/etc/resolv.conf").read()
    add("/etc/resolv.conf 指向本机 dnsmasq", "nameserver 127.0.0.1" in rc,
        "被 dhcpcd 等覆写；重跑安装或收敛会自动修复")
add("secrets.json 存在", os.path.exists("/etc/dert/secrets.json"))
for c in ("nft", "dnsmasq", "pppd", "curl", "python3", "ip"):
    add(f"命令可用: {c}", bool(shutil.which(c)), f"apt install {c}" if c in ("nft", "dnsmasq", "pppd") else "")
add("netgen 校验", subprocess.run(["python3", "/usr/local/lib/dert/netgen.py", "--check"], capture_output=True).returncode == 0)
try:
    out = subprocess.run(["nft", "list", "table", "inet", "dert_base"], capture_output=True).returncode
    add("nft 表 dert_base 已加载", out == 0)
except Exception:
    add("nft 表 dert_base 已加载", False)
add("sing-box 不受本脚本管理（仅检测）", True)
bad = [c for c in checks if not c["ok"]]
print(json.dumps({"ok": not bad, "checks": checks, "summary": f"{len(checks) - len(bad)}/{len(checks)} 通过"}, ensure_ascii=False))
