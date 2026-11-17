#!/usr/bin/env python3
"""调试模式：记录每台设备的转发路径（v0.1.119）。

## 它解决什么

出问题时最常见的两个问题：「这台设备到底走没走代理」和「它连的是哪个地址」。
平时 sing-box 只记它自己接管的流量（而且 `action: bypass` 的流量连日志都没有），
非白名单设备更是完全不可见。这个模式把**每条新连接**的结论写进内核日志。

## 怎么判路径

nft 在 `dstnat + 10` 打点（见 netgen.flow_chain_lines 的注释），此时包上已经
带着决策结果，于是路径就是「哪条规则匹配上」：

    dns-sb   DNS 被 DNAT 到 sing-box:5353（白名单设备）
    dns      DNS 被 DNAT 到 dnsmasq:53（其余设备）
    singbox  TCP 被重定向到回环（进 sing-box，再按域名分流）
    tun      UDP/ICMP 打上 0x2023（进 tun）
    direct   其余（非白名单，或白名单但命中 geoip-cn bypass）

## 为什么不用 sing-box 的日志

1. `bypass` 的流量在 sing-box 里完全不可见——那恰恰是最需要排查的一类；
2. 它的日志里没有源设备，只有目标地址，对不上「哪台设备」；
3. 日志格式归 sub-store 的配置管，不是 DebRT 能承诺的东西。

## 开销

一条连接一行，`ct state new` 保证不是每包一条，另加 20/秒限速防洪泛。
默认关闭，关闭时 nft 链根本不存在。

用法：
    flowops.py on                开启
    flowops.py off               关闭
    flowops.py status            当前状态（含链是否真的存在、命中包数）
    flowops.py show [分钟] [--no-dns]   最近的记录
"""

import json
import os
import re
import subprocess
import sys
import time

_FS = os.environ.get("DERT_FSROOT", "")
E = _FS + "/etc/dert"
CFG = E + "/config.json"

RE_KV = re.compile(r"([A-Za-z_]+)=([^\s]+)")


def run(args, timeout=15, stdin=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           input=stdin)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception:
        return 127, ""


def log_event(event, **fields):
    parts = ["event=" + event]
    for k in sorted(fields):
        if fields[k] is None:
            continue
        parts.append("%s=%s" % (k, fields[k]))
    try:
        subprocess.run(["logger", "-t", "dert-flow", " ".join(parts)], timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置读写（原子）
# ---------------------------------------------------------------------------
def read_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def flow_enabled(cfg=None):
    cfg = read_cfg() if cfg is None else cfg
    # 只认布尔 True：写 "on"/"1" 这类真值会被 schema_check 拦下，
    # 这里也保持一致，避免「以为关了其实开着」
    return (cfg.get("debug") or {}).get("flow") is True


def write_cfg(cfg):
    """原子写，保留 600 权限。写一半的 config.json 会让收敛直接失败。"""
    d = os.path.dirname(CFG)
    os.makedirs(d, exist_ok=True)
    tmp = CFG + ".tmp." + os.urandom(3).hex()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, CFG)


# ---------------------------------------------------------------------------
# nft 链
# ---------------------------------------------------------------------------
def chain_present():
    rc, _ = run(["nft", "list", "chain", "inet", "dert_base", "flowlog"])
    return rc == 0


def chain_packets():
    """flow 链里各条规则的命中包数，用来确认「开着但一条都没匹配」这种假象。"""
    rc, out = run(["nft", "list", "chain", "inet", "dert_base", "flowlog"])
    if rc != 0:
        return {}
    res = {}
    for line in out.splitlines():
        m = re.search(r'log prefix "dertflow ([^"]+)"', line)
        if not m:
            continue
        c = re.search(r"counter packets (\d+)", line)
        res[m.group(1).strip()] = int(c.group(1)) if c else 0
    return res


def _chain_file():
    """用 netgen 生成同一份链文本——两份实现迟早漂移。

    `$LAN_BR` 是**文件级** define：nft 不会从已存在的表里继承变量，漏了这行
    整份文件会以 "unknown variable" 失败（链建不起来，但配置已经写着 on，
    面板就会一直显示「已开启·链未生效」）。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import netgen
    body = "\n".join(netgen.flow_chain_lines())
    return ('add table inet dert_base\n'
            'define LAN_BR = "br-lan"\n'
            'table inet dert_base {\n%s\n}\n' % body)


def apply_chain():
    """建链。先删后建，避免重复定义；nft 批量加载是原子的。"""
    if chain_present():
        run(["nft", "delete", "chain", "inet", "dert_base", "flowlog"])
    rc, out = run(["nft", "-f", "-"], stdin=_chain_file())
    if rc != 0:
        return False, out.strip()[:300]
    return True, ""


def remove_chain():
    if not chain_present():
        return True, ""
    rc, out = run(["nft", "delete", "chain", "inet", "dert_base", "flowlog"])
    return rc == 0, out.strip()[:300]


# ---------------------------------------------------------------------------
# 日志解析
# ---------------------------------------------------------------------------
def device_map():
    """IP → 设备名。复用面板的 sysinfo（备注优先），不另写一套租约解析。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, os.path.join(here, "panel"))
        import sysinfo
        cfg = read_cfg()
        rows = sysinfo.leases(cfg.get("static") or [])
        out = {}
        for r in rows:
            ip = r.get("ip")
            if ip:
                out[ip] = r.get("note") or r.get("name") or ""
        return out
    except Exception:
        return {}


def parse_log(text, devs=None):
    """把内核日志行解析成结构化记录。只看带 dertflow 前缀的行。

    devs 为 None 时自己去取设备名（IP → 备注/主机名），便于单独调用。
    """
    if devs is None:
        devs = device_map()
    recs = []
    for line in (text or "").splitlines():
        i = line.find("dertflow ")
        if i < 0:
            continue
        rest = line[i:]
        head = rest.split(" ")
        if len(head) < 2:
            continue
        path = head[1]
        kv = dict(RE_KV.findall(rest))
        src = kv.get("SRC", "")
        ts = line.split(" ", 1)[0]
        recs.append({
            "t": ts[11:19] if len(ts) >= 19 else ts,
            "path": path,
            "dev": devs.get(src, ""),
            "src": src,
            "dst": kv.get("DST", ""),
            "dpt": kv.get("DPT", ""),
            "proto": kv.get("PROTO", ""),
        })
    return recs


def read_journal(minutes):
    rc, out = run(["journalctl", "-k", "--since", "-%dmin" % minutes,
                   "--no-pager", "-o", "short-iso"], timeout=30)
    return out if rc == 0 else ""


def show(minutes=10, include_dns=True, limit=200):
    recs = parse_log(read_journal(minutes))
    if not include_dns:
        recs = [r for r in recs if r["path"] not in ("dns", "dns-sb")]
    counts = {}
    for r in recs:
        counts[r["path"]] = counts.get(r["path"], 0) + 1
    # 新的在前：排查时关心的是「刚才发生了什么」
    recs = list(reversed(recs))[:limit]
    return {"ok": True, "minutes": minutes, "enabled": flow_enabled(),
            "chain_present": chain_present(), "total": len(recs),
            "counts": counts, "records": recs}


# ---------------------------------------------------------------------------
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "status"

    if action == "on":
        cfg = read_cfg()
        cfg.setdefault("debug", {})["flow"] = True
        write_cfg(cfg)
        ok, err = apply_chain()
        if not ok:
            print(json.dumps({"ok": False, "code": "NFT", "mode": "on",
                              "message": "配置已写入，但 nft 链建立失败",
                              "hint": err or "请检查 dert_base 表是否存在"}, ensure_ascii=False))
            return 1
        log_event("flow_on")
        print(json.dumps({"ok": True, "mode": "on", "chain": chain_present()},
                         ensure_ascii=False))
        return 0

    if action == "off":
        cfg = read_cfg()
        cfg.setdefault("debug", {})["flow"] = False
        write_cfg(cfg)
        ok, err = remove_chain()
        if not ok:
            print(json.dumps({"ok": False, "code": "NFT", "mode": "off",
                              "message": "配置已写入，但 nft 链删除失败",
                              "hint": err}, ensure_ascii=False))
            return 1
        log_event("flow_off")
        print(json.dumps({"ok": True, "mode": "off", "chain": chain_present()},
                         ensure_ascii=False))
        return 0

    if action == "status":
        print(json.dumps({"ok": True, "enabled": flow_enabled(),
                          "chain_present": chain_present(),
                          "packets": chain_packets()}, ensure_ascii=False))
        return 0

    if action == "show":
        minutes = 10
        include_dns = "--no-dns" not in argv
        for a in argv[1:]:
            if a.isdigit():
                minutes = max(1, min(1440, int(a)))
        print(json.dumps(show(minutes, include_dns), ensure_ascii=False))
        return 0

    print(json.dumps({"ok": False, "code": "USAGE",
                      "message": "未知动作：%s" % action,
                      "hint": "合法动作：on / off / status / show [分钟] [--no-dns]"},
                     ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
