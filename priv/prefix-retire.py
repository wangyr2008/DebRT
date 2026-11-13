#!/usr/bin/env python3
"""登记「已撤销」的 IPv6 前缀，并让 RA 持续把它们宣告为寿命 0。

为什么需要（真机 2026-11-13）：
    前缀一变（PPPoE 重拨、或开关 IPv6 触发的重拨），客户端手里仍留着旧前缀的
    GUA。它不会立刻失效——最后一次 RA 给它的是 1 小时 valid。macOS 12 会继续
    拿旧地址作源地址发包，而上游早已不路由这个前缀 → 表现为"IPv6 打不开，
    断开 Wi-Fi 重连才好"。重连之所以有效，是因为 RS 让 networkd 立刻回一条 RA，
    客户端按新前缀重建；但那条 RA **只含新前缀，并不会废弃旧前缀**。

    所以必须由我们持续把旧前缀以 valid/preferred = 0 的形式塞进 RA。用 networkd
    的 `[IPv6Prefix] Assign=no` 写成 drop-in，好处是：
      · 每条 RA 都带，客户端哪怕当时在睡眠、几小时后才醒，也会在下一次 RA/RS 收到；
      · 不需要自己拼 ICMPv6 报文（手写 RA 容易在 hop limit / 校验和上出错）；
      · 不改 netgen 的主文件，清理逻辑也不会误删（cleanup.py 只扫 30-*.network 等）。

用法：
    prefix-retire.py <当前前缀> <旧前缀> [<旧前缀> ...]

    旧前缀写进状态文件后，drop-in 会被重写；内容有变化时 reload + reconfigure
    br-lan，让新的 RA 立刻生效。重复调用是幂等的。
"""

import json
import os
import subprocess
import sys
import time

FS = os.environ.get("DERT_FSROOT", "")
STATE = FS + "/var/lib/dert/retired-prefixes"
DROPIN_DIR = FS + "/etc/systemd/network/20-lan.network.d"
DROPIN = os.path.join(DROPIN_DIR, "90-retired-prefixes.conf")

# 保留 24 小时：覆盖"笔记本关机过夜"这种最常见的错过场景。
# 旧地址自身最多 1 小时就过期，24 小时是充裕的余量。
TTL = 24 * 3600
# 上限 8 条：一天重拨十几次也不会把 drop-in 撑爆，最老的自然淘汰。
MAX = 8
MARKER = "# 由 DebRT 生成"


def load():
    items = []
    try:
        with open(STATE, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit():
                    items.append((int(parts[0]), parts[1]))
    except OSError:
        pass
    return items


def save(items):
    d = os.path.dirname(STATE)
    os.makedirs(d, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for ts, prefix in items:
            f.write("%d %s\n" % (ts, prefix))
    os.replace(tmp, STATE)


def render(items):
    lines = [MARKER + " —— 已撤销的 IPv6 前缀，寿命置 0 让客户端立刻停用",
             "# 由 priv/prefix-retire.py 维护，请勿手工修改"]
    for _ts, prefix in items:
        lines += ["", "[IPv6Prefix]", "Prefix=" + prefix, "Assign=no",
                  "PreferredLifetimeSec=0", "ValidLifetimeSec=0"]
    return "\n".join(lines) + "\n"


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def reload_networkd():
    """让新的 drop-in 生效并立刻重发 RA。

    reconfigure 会先发一条 lifetime=0 的告别 RA 再发正常的，这是 networkd 的
    既定行为；此刻本来就在换前缀，客户端也在重新 SOLICIT，属于同一个窗口。
    """
    if FS:                     # 测试缝：不碰真实 systemd
        return
    subprocess.run(["networkctl", "reload"], timeout=30,
                   capture_output=True)
    subprocess.run(["networkctl", "reconfigure", "br-lan"], timeout=30,
                   capture_output=True)


def main(argv):
    new = argv[1].strip() if len(argv) > 1 else ""
    olds = [a.strip() for a in argv[2:] if a.strip()]
    now = int(time.time())

    items = [(ts, p) for ts, p in load() if now - ts < TTL]
    # 同一前缀又变回"当前"了（运营商把同一段重新分下来）→ 撤销登记，
    # 否则会同时以"正常"和"已废弃"两种身份出现在 RA 里，客户端直接停用。
    items = [(ts, p) for ts, p in items if p != new]
    seen = {p for _ts, p in items}
    for p in olds:
        if p == new or p in seen:
            continue
        items.append((now, p))
        seen.add(p)
    items = items[-MAX:]

    save(items)
    body = render(items)
    changed = body != read_text(DROPIN)
    if changed:
        os.makedirs(DROPIN_DIR, exist_ok=True)
        tmp = DROPIN + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, DROPIN)
        reload_networkd()

    print(json.dumps({"ok": True, "current": new,
                      "retired": [p for _ts, p in items],
                      "changed": changed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
