#!/usr/bin/env python3
"""清理本次不再生成的陈旧文件（v0.1.71）。

背景：`apply_stage_files` 只写清单里的文件、从不删除。于是端口角色一变，
旧的 `30-<口>.network` 就永久留在 `/etc/systemd/network/` 里。真机
2026-11-12 踩到过：`30-enp4s0.network`（把 WAN 口声明为 `Bridge=br-lan`）
与 `10-wan.network` 并存，靠 `10-` 字典序先匹配才没把 WAN 口塞进 LAN 桥。

为什么按 manifest 对账、而不是按配置重算：
    旧实现判断「`30-<口>.network` 的口名不在 ports 里就删」。但生成器对
    **WAN 口不生成** `30-*.network`，所以「端口从 LAN 改成 WAN」时口名仍在
    ports 里 → 旧文件不删 → 正是上面那个故障。按配置重算还有个毛病：它把
    生成器的判断逻辑抄了一遍，两边迟早漂移。
    拿「本次要部署的 manifest」当唯一事实来源，就没有这个问题。

三道安全边界：
    1. 只在白名单目录 + 白名单文件名模式里找，绝不遍历整个 /etc；
    2. 只删首两行带「由 DebRT 生成」标记的文件——用户手写的同名文件不动；
    3. 清单读不出来就返回空列表（宁可留垃圾，不可误删）。

用法：
    cleanup.py <manifest.json>            列出应删除的绝对路径
    cleanup.py <manifest.json> --root DIR 以 DIR 为根（测试用）
"""

import argparse
import glob
import json
import os
import sys

# 生成物可能出现的全部位置。目录与文件名模式都只覆盖 DebRT 自己的产物，
# 不碰 dnsmasq.d 里用户自己的配置、也不碰 /etc/systemd/network 下别的东西。
PATTERNS = (
    "etc/systemd/network/30-*.network",
    "etc/systemd/network/25-ppp0.network",
    "etc/dnsmasq.d/dert.conf",
    "etc/nftables.d/dert-base.nft",
    "etc/ppp/peers/dert",
    "etc/ppp/chap-secrets",
    "etc/ppp/pap-secrets",
)

MARKER = "由 DebRT 生成"


def is_generated(path):
    """只认 DebRT 生成的文件：标记在首两行。

    `dert-base.nft` 首行是 `#!/usr/sbin/nft -f`，标记在第二行，所以看两行。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.readline() + fh.readline()
    except OSError:
        return False
    return MARKER in head


def plan(manifest_path, root="/"):
    """返回应删除的绝对路径列表（保持稳定顺序）。

    root 只用于测试注入；生产环境恒为 "/"。
    """
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            man = json.load(fh)
        files = man["files"]
        # 必须是非空列表：能 json.load 不等于能当清单用。错误输出（如
        # {"ok":false,"code":"CONFIG_SCHEMA"}）也满足 json.load，若此时把
        # keep 当成空集，就会把磁盘上所有生成物一起删掉。
        if not isinstance(files, list) or not files:
            return []
        keep = {os.path.join(root, f["path"]) for f in files}
    except Exception:
        # 清单不可用（缺失/损坏/结构不符）→ 一个都不删
        return []

    stale = []
    for pat in PATTERNS:
        for f in sorted(glob.glob(os.path.join(root, pat))):
            if f in keep:
                continue
            if not is_generated(f):
                continue
            stale.append(f)
    return stale


def main(argv=None):
    ap = argparse.ArgumentParser(description="清理不再生成的陈旧文件")
    ap.add_argument("manifest", help="本次要部署的 manifest.json")
    ap.add_argument("--root", default="/", help="以该目录为根（测试用）")
    ap.add_argument("--apply", action="store_true",
                    help="实际删除；不带此参数只打印候选（观察期用）")
    args = ap.parse_args(argv)

    targets = plan(args.manifest, args.root)

    if not args.apply:
        for f in targets:
            print(f)
        return 0

    for f in targets:
        try:
            os.remove(f)
        except OSError as e:
            # 删不掉不算失败：留下垃圾比中断部署安全
            print("skip %s (%s)" % (f, e), file=sys.stderr)
            continue
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
