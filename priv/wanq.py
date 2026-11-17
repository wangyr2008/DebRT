#!/usr/bin/env python3
"""DebRT WAN 出口质量探测（v0.1.119）。

为什么单独做：health.py 只判「ppp0 存不存在」，不测质量。2026-11-17 那次
故障里 ppp0 一直在、sing-box 没重启、conntrack 不满，但出口劣化了约 3 分钟，
health 一个字都没记，最后只能翻十几轮 journal 才定位到运营商侧。

判据按**地址族分开**（v4 / v6 各一组目标）：
  · 丢包率 >= 20%  或  平均 RTT >= 200ms  → degraded
  · 100% 丢包同样归 degraded（不是「没数据」而是「完全不通」）

**多目标取最好**：单个目标可能自己屏蔽 ICMP（很多公共 DNS 的 v6 地址就是），
按总和算丢包会把「目标不回」误判成「链路丢包」。所以每个地址族探多个目标，
取丢包最低的那个（并列时取 RTT 最低），再拿它做判据。

**只在状态跳变时写事件**，不刷日志：60s 一轮，一天 1440 条样本，全写事件
既没用又会把 journal 冲掉。跳变时才写 wan_quality_drop / wan_quality_ok。

用法：
    wanq.py probe     探一轮，更新状态文件（systemd timer 调用）
    wanq.py show      输出当前状态 JSON（面板用）
"""

import json
import os
import re
import subprocess
import sys
import time

_FS = os.environ.get("DERT_FSROOT", "")
E = _FS + "/etc/dert"
V = _FS + "/var/lib/dert"
STATE = V + "/wanq.json"

# 目标：各运营商/公共 DNS，都是国内地址（境外目标会被墙，测出来的是墙不是链路）
V4_TARGETS = ("223.5.5.5", "119.29.29.29")
V6_TARGETS = ("2400:3200::1", "2402:4e00::")

LOSS_BAD = 20          # %
RTT_BAD = 200          # ms
SAMPLES_KEEP = 60      # 环形历史条数（60s 一轮 → 1 小时）

_RE_LOSS = re.compile(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received")
_RE_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")


def run(args, timeout=12):
    """跑一条命令，返回 (rc, stdout+stderr)。异常一律当失败，不抛。"""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception:
        return 127, ""


def parse_ping(out):
    """解析 iputils ping -q 输出 → (tx, rx, loss_pct, avg_ms)。

    avg_ms 在 100% 丢包时为 None（没有 rtt 行）。解析不出来返回 None，
    调用方按「探测失败」处理，不把解析失败当成链路好。
    """
    m = _RE_LOSS.search(out or "")
    if not m:
        return None
    tx, rx = int(m.group(1)), int(m.group(2))
    if tx <= 0:
        return None
    loss = round((tx - rx) * 100.0 / tx, 1)
    r = _RE_RTT.search(out or "")
    avg = float(r.group(2)) if r else None
    return tx, rx, loss, avg


def probe_target(family, target, runner=None):
    """探一个目标。返回 {target, tx, rx, loss, avg_ms} 或 None（探测本身失败）。"""
    runner = runner or run
    flag = "-6" if family == "v6" else "-4"
    # -n 不做反解（避免把 DNS 也拖进判据）、-q 只要汇总、-i 0.2 快速连发
    rc, out = runner(["ping", flag, "-n", "-q", "-c", "5", "-i", "0.2", "-W", "1", target])
    st = parse_ping(out)
    if st is None:
        return None
    tx, rx, loss, avg = st
    return {"target": target, "tx": tx, "rx": rx, "loss": loss, "avg_ms": avg}


def best_of(results):
    """多个目标里取最好的：先比丢包，再比 RTT。

    为什么要取最好而不是求平均：公共 DNS 的 v6 地址普遍屏蔽 ICMP，按平均算
    会把「某个目标不回」当成「整条链路丢包 50%」，天天误报。
    """
    ok = [r for r in results if r]
    if not ok:
        return None
    return min(ok, key=lambda r: (r["loss"], r["avg_ms"] if r["avg_ms"] is not None else 1e9))


def judge(probe):
    """单个地址族的判定：ok / degraded / off。"""
    if probe is None:
        return "off"
    if probe.get("loss", 0) >= LOSS_BAD:
        return "degraded"
    avg = probe.get("avg_ms")
    if avg is not None and avg >= RTT_BAD:
        return "degraded"
    return "ok"


def gateway(family, runner=None):
    """默认路由的网关。PPPoE 的 v4 网关是运营商对端，v6 是链路本地地址。"""
    runner = runner or run
    flag = "-6" if family == "v6" else "-4"
    rc, out = runner(["ip", flag, "route", "show", "default"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if "via" in parts:
            i = parts.index("via")
            if i + 1 < len(parts):
                gw = parts[i + 1]
                # v6 链路本地网关必须带出口设备名，否则 ping 会报 "need scope"
                if family == "v6" and gw.lower().startswith("fe80") and "dev" in parts:
                    gw = gw + "%" + parts[parts.index("dev") + 1]
                return gw
    return None


def ipv6_enabled():
    try:
        with open(E + "/config.json", encoding="utf-8") as f:
            return bool((json.load(f).get("ipv6") or {}).get("enabled"))
    except Exception:
        return False


def probe_all(runner=None, v6_on=True):
    """探一轮，返回 {v4: probe|None, v6: probe|None}。

    v6 关闭时**完全不发 v6 探测包**：没有 v6 默认路由时 `ping -6` 每个目标要
    白等 5 秒超时，一轮拖到十几秒，还平白把定时器撑长。
    """
    runner = runner or run
    out = {}
    fams = (("v4", V4_TARGETS), ("v6", V6_TARGETS)) if v6_on else (("v4", V4_TARGETS),)
    for fam, targets in fams:
        gw = gateway(fam, runner)
        # 网关排在最前：它最能反映「本机到运营商」这一段，且不经过任何 DNS
        cand = ([gw] if gw else []) + list(targets)
        out[fam] = best_of([probe_target(fam, t, runner) for t in cand])
    return out


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(d):
    os.makedirs(V, exist_ok=True)
    tmp = STATE + ".tmp." + os.urandom(3).hex()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, STATE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def log_event(event, **fields):
    parts = ["event=" + event]
    for k in sorted(fields):
        if fields[k] is None:
            continue
        parts.append("%s=%s" % (k, fields[k]))
    try:
        subprocess.run(["logger", "-t", "dert-wanq", " ".join(parts)], timeout=5)
    except Exception:
        pass


def summary(probe):
    """事件字段用的紧凑摘要。"""
    if not probe:
        return {"loss": None, "avg": None}
    avg = probe.get("avg_ms")
    return {"loss": probe.get("loss"), "avg": None if avg is None else round(avg)}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "probe"

    if action == "show":
        st = load_state()
        print(json.dumps(st, ensure_ascii=False))
        return 0

    if action != "probe":
        print(json.dumps({"ok": False, "message": "未知动作：%s" % action}, ensure_ascii=False))
        return 2

    v6_on = ipv6_enabled()
    res = probe_all(v6_on=v6_on)
    v4, v6 = res.get("v4"), (res.get("v6") if v6_on else None)

    s4, s6 = judge(v4), judge(v6)
    # v6 关闭时 s6 == "off"，不参与总判定
    overall = "degraded" if "degraded" in (s4, s6) else "ok"

    prev = load_state()
    now = int(time.time())
    prev_state = prev.get("state")
    since = prev.get("since") if prev_state == overall else now
    if prev_state != overall:
        since = now

    sample = {"t": now, "v4_loss": (v4 or {}).get("loss"), "v4_avg": summary(v4)["avg"],
              "v6_loss": (v6 or {}).get("loss"), "v6_avg": summary(v6)["avg"]}
    samples = [s for s in (prev.get("samples") or []) if isinstance(s, dict)][-(SAMPLES_KEEP - 1):]
    samples.append(sample)

    state = {
        "state": overall,
        "since": since,
        "updated": now,
        "v4": dict(s4=s4, **(v4 or {"loss": None, "avg_ms": None})),
        "v6": dict(s6=s6, **(v6 or {"loss": None, "avg_ms": None})),
        "v6_probed": v6_on,
        "samples": samples,
    }
    save_state(state)

    # 只在跳变时写事件：首轮（prev 为空）也写一条，便于确认探测真的在跑
    if prev_state != overall:
        fields = {"v4_loss": sample["v4_loss"], "v4_avg": sample["v4_avg"],
                  "v6_loss": sample["v6_loss"], "v6_avg": sample["v6_avg"]}
        if overall == "degraded":
            log_event("wan_quality_drop", **fields)
        else:
            down = None
            if prev_state == "degraded" and prev.get("since"):
                down = now - int(prev["since"])
            log_event("wan_quality_ok", down_s=down, **fields)

    print(json.dumps({"ok": True, "state": overall, "v4": s4, "v6": s6}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
