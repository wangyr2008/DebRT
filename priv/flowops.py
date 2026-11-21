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
    flowops.py show [分钟] [--dns]     最近的记录（DNS 默认不显示，太吵）
    flowops.py show --from 21:40 --until 21:45 [--limit N]
                                     回查某个时间窗（只给分钟数会被条数上限截断）
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

# 权限的定义只有一处（privlib.CONFIG_MODE = 0644）。这里曾硬编码 0600，
# 于是「开一次调试模式」就把 config.json 降成 root-only，而面板以 dert 用户
# 运行、直接读它取 DNS 上游——读失败被 except 吞掉，概览「DNS 服务器」从此
# 一直显示 —，且不报任何错（真机 2026-11-19 复现）。
try:
    from privlib import CONFIG_MODE
except Exception:                    # 极端情况（privlib 不在同目录）下退回同值
    CONFIG_MODE = 0o644

RE_KV = re.compile(r"([A-Za-z_]+)=([^\s]+)")
# 真机实测：sing-box 的日志里带 ANSI 颜色码，连接 ID 会被包成
# `\x1b[38;5;45m789896436\x1b[0m`，不剥掉的话正则会静默匹配不到——
# 表现为「join 一条都匹配不上」，而且不报任何错。
RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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
    """读 config.json；**读不出来就抛**，不返回空字典。

    以前异常时返回 {}，而 on/off 紧接着 `cfg.setdefault("debug",{})["flow"]=True`
    再 write_cfg —— 等于把整份配置改写成「只有 debug 段」，下一次收敛就生成不出
    任何东西（连带开机没有防火墙）。读失败必须让调用方停下来。
    """
    with open(CFG, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict) or not d:
        raise ValueError("config.json 内容不是有效对象")
    return d


def flow_enabled(cfg=None):
    """读不出配置时按「关」处理——status/show 是只读接口，不该因为
    配置损坏就整个报错（面板会因此显示不出开关状态）。"""
    try:
        cfg = read_cfg() if cfg is None else cfg
    except Exception:
        return False
    # 只认布尔 True：写 "on"/"1" 这类真值会被 schema_check 拦下，
    # 这里也保持一致，避免「以为关了其实开着」
    return (cfg.get("debug") or {}).get("flow") is True


def write_cfg(cfg):
    """原子写，权限用 CONFIG_MODE（0644，与 configops 一致）。

    写一半的 config.json 会让收敛直接失败，所以走临时文件 + rename。
    权限**不能**在这里单独决定：两个写者各写各的，就会出现「开个开关把文件
    变成 root-only」这种谁也没想到的副作用。
    """
    d = os.path.dirname(CFG)
    os.makedirs(d, exist_ok=True)
    tmp = CFG + ".tmp." + os.urandom(3).hex()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp, CONFIG_MODE)
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


NFT_FILE = _FS + "/etc/nftables.d/dert-base.nft"


def sync_nft_file():
    """把 /etc/nftables.d/dert-base.nft 重新生成成与当前配置一致。

    不这么做的话：flow on/off 只改内核里的链、不改盘上的生成物，而 netgen 的
    输出取决于 debug.flow —— 下一次收敛必然判定「漂移」，走全量路径重启
    networkd/pppoe，把好好的宽带会话打断。真机 2026-11-20：23:58 升级时
    flow 还是开的（盘上带链），00:01 关掉调试模式（盘上没改），06:09 的
    6 小时收敛发现差异，把已连 44 小时的 PPPoE 重拨了。

    权限 0644 与 netgen 清单里该文件的 mode 必须一致，否则漂移判定会把
    「权限不同」也算一次漂移。

    返回 (ok, err)。**失败不能把开关卡死**：链照建/照删，只是盘上生成物
    滞后一步，下次收敛会补上（那时只会重载 nft，不会重启 pppoe）。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import netgen
        body = netgen.gen_nft(read_cfg())
        d = os.path.dirname(NFT_FILE)
        os.makedirs(d, exist_ok=True)
        tmp = NFT_FILE + ".tmp." + os.urandom(3).hex()
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        os.replace(tmp, NFT_FILE)
        return True, ""
    except Exception as e:                      # noqa: BLE001
        return False, str(e)[:200]


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
    for line in RE_ANSI.sub("", text or "").splitlines():
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
            "spt": kv.get("SPT", ""),
            "dst": kv.get("DST", ""),
            "dpt": kv.get("DPT", ""),
            "proto": kv.get("PROTO", ""),
            # 仅 singbox 路径会填：内核日志里那个 DST 是重定向目标，见 singbox_map
            "real_dst": "",
            "node": "",
        })
    return recs


def _window(minutes, since=None, until=None):
    """journalctl 的时间参数。

    支持 `--from 21:40 --to 21:45` 回查某个时间窗——只给「最近 N 分钟」时，
    一旦超过 limit 条就被新记录挤掉，事后复盘根本看不到当时那一段
    （今天两次排查都撞上这个）。
    """
    if since:
        args = ["--since", since]
        if until:
            args += ["--until", until]
        return args
    return ["--since", "-%dmin" % minutes]


def read_journal(minutes, since=None, until=None):
    rc, out = run(["journalctl", "-k"] + _window(minutes, since, until)
                  + ["--no-pager", "-o", "short-iso"], timeout=45)
    return out if rc == 0 else ""


# ---------------------------------------------------------------------------
# 真实目标：singbox 路径的内核日志看不到
# ---------------------------------------------------------------------------
# 为什么需要这一段：nft 在 `dstnat + 10` 打点，此时 TCP 已经被 redirect 到
# sing-box 的本地端口（真机实测 34601，每次重启都会变），所以日志里的
# DST/DPT 是**重定向目标**，不是真实目的地。只看内核日志会把
# 「访问 googlevideo」读成「连路由器 192.168.10.1:34601」。
#
# sing-box 自己的日志里两者都全，而且**共享同一个连接 ID**：
#   [3571061780 0ms] inbound/tun[tun-in]: inbound redirect connection from 192.168.10.117:60002
#   [3571061780 1ms] outbound/trojan[香港 02]: outbound connection to rr5---sn-x.googlevideo.com:443
# 而内核日志里有 SPT，于是可以按 (源IP, 源端口) 精确 join —— 不用改防火墙、
# 不增加任何日志量。
#
# 这条路径只用于 singbox 记录；direct/tun 的 DST 本来就是真实的。
RE_CID = re.compile(r"\[(\d+)\s+[\d.]+(?:ms|s)\]")
RE_OUTBOUND = re.compile(r"outbound/([a-z0-9-]+)\[([^\]]*)\]: outbound connection to (\S+)")


def _norm_ip(s):
    """统一 IPv6 写法。

    内核日志打印 `2409:8a5e:0a50:5bc1:...`（每组 4 位补零），sing-box 打印
    `2409:8a5e:a50:5bc1:...`（压缩）——不规范化的话 v6 的 join 永远匹配不上，
    而且不会报错，只会静默显示「真实目标为空」。
    """
    try:
        import ipaddress
        return str(ipaddress.ip_address(s))
    except Exception:
        return s


def _split_hostport(s):
    """`[2409:...]:1234` / `1.2.3.4:1234` → (host, port)。"""
    s = (s or "").strip()
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        return host, rest.lstrip(":")
    host, _, port = s.rpartition(":")
    return host, port


def read_singbox(minutes, since=None, until=None):
    rc, out = run(["journalctl", "-u", "sing-box"] + _window(minutes, since, until)
                  + ["--no-pager", "-o", "cat"], timeout=60)
    return out if rc == 0 else ""


def singbox_map(text):
    """从 sing-box 日志建 {(源IP, 源端口): (真实目标, 出站节点)}。"""
    id_src, out = {}, {}
    for line in RE_ANSI.sub("", text or "").splitlines():
        m = RE_CID.search(line)
        if not m:
            continue
        cid = m.group(1)
        if "inbound" in line and " connection from " in line:
            id_src[cid] = _split_hostport(line.rsplit(" connection from ", 1)[-1])
        else:
            mo = RE_OUTBOUND.search(line)
            if mo:
                out[cid] = (mo.group(3), mo.group(2))
    res = {}
    for cid, src in id_src.items():
        if cid in out and src[0]:
            res[(_norm_ip(src[0]), src[1])] = out[cid]
    return res


def show(minutes=10, include_dns=False, limit=200, since=None, until=None):
    recs = parse_log(read_journal(minutes, since, until))
    # singbox 路径的真实目标不在内核日志里，去 sing-box 日志按连接 ID 补
    if any(r["path"] == "singbox" for r in recs):
        try:
            smap = singbox_map(read_singbox(minutes, since, until))
        except Exception:
            smap = {}
        for r in recs:
            if r["path"] == "singbox":
                r["real_dst"], r["node"] = smap.get((_norm_ip(r["src"]), r["spt"]), ("", ""))
    if not include_dns:
        recs = [r for r in recs if r["path"] not in ("dns", "dns-sb")]
    counts = {}
    for r in recs:
        counts[r["path"]] = counts.get(r["path"], 0) + 1
    # 新的在前：排查时关心的是「刚才发生了什么」
    recs = list(reversed(recs))[:limit]
    return {"ok": True, "minutes": minutes, "since": since or "", "until": until or "",
            "enabled": flow_enabled(),
            "chain_present": chain_present(), "total": len(recs),
            "counts": counts, "records": recs}


# ---------------------------------------------------------------------------
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "status"

    if action in ("on", "off"):
        # 读失败必须停手：后面是「读改写」，读不到就写会清空整份配置
        try:
            cfg = read_cfg()
        except Exception as e:
            print(json.dumps({"ok": False, "code": "CONFIG_UNREADABLE",
                              "message": "读取 config.json 失败，未做任何修改",
                              "hint": str(e)[:120]}, ensure_ascii=False))
            return 1

    if action == "on":
        cfg.setdefault("debug", {})["flow"] = True
        write_cfg(cfg)
        synced, _serr = sync_nft_file()   # 盘上的生成物必须跟着变，否则下次收敛判漂移
        ok, err = apply_chain()
        if not ok:
            print(json.dumps({"ok": False, "code": "NFT", "mode": "on",
                              "message": "配置已写入，但 nft 链建立失败",
                              "hint": err or "请检查 dert_base 表是否存在"}, ensure_ascii=False))
            return 1
        log_event("flow_on")
        print(json.dumps({"ok": True, "mode": "on", "chain": chain_present(),
                          "nft_file_synced": synced},
                         ensure_ascii=False))
        return 0

    if action == "off":
        cfg.setdefault("debug", {})["flow"] = False
        write_cfg(cfg)
        synced, _serr = sync_nft_file()   # 同上：不同步就会在 6 小时收敛时打断宽带
        ok, err = remove_chain()
        if not ok:
            print(json.dumps({"ok": False, "code": "NFT", "mode": "off",
                              "message": "配置已写入，但 nft 链删除失败",
                              "hint": err}, ensure_ascii=False))
            return 1
        log_event("flow_off")
        print(json.dumps({"ok": True, "mode": "off", "chain": chain_present(),
                          "nft_file_synced": synced},
                         ensure_ascii=False))
        return 0

    if action == "status":
        print(json.dumps({"ok": True, "enabled": flow_enabled(),
                          "chain_present": chain_present(),
                          "packets": chain_packets()}, ensure_ascii=False))
        return 0

    if action == "show":
        minutes = 10
        # DNS 默认不显示：真机实测 45 秒里 555 行有 383 行是 DNS（69%），
        # 会把真正要看的 TCP 连接全埋掉。需要时加 --dns。
        include_dns = "--dns" in argv
        since = until = None
        limit = None
        for i, a in enumerate(argv):
            if a.isdigit():
                minutes = max(1, min(1440, int(a)))
            if a in ("--from", "--until", "--limit") and i + 1 < len(argv):
                if a == "--from":
                    since = argv[i + 1]
                elif a == "--until":
                    until = argv[i + 1]
                elif a == "--limit" and argv[i + 1].isdigit():
                    limit = max(1, min(20000, int(argv[i + 1])))
        # 回查某个时间窗时默认放宽上限：默认 200 条只够覆盖最近十几秒，
        # 而复盘恰恰要看中间那一段（今天两次都被这个截断坑过）
        if limit is None:
            limit = 5000 if since else 200
        print(json.dumps(show(minutes, include_dns, limit=limit,
                              since=since, until=until), ensure_ascii=False))
        return 0

    print(json.dumps({"ok": False, "code": "USAGE",
                      "message": "未知动作：%s" % action,
                      "hint": "合法动作：on / off / status / show [分钟] [--dns]"},
                     ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())
