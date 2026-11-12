#!/usr/bin/env python3
"""升级包成员校验（v0.1.76）。

从 `dert-priv` 的内联 Python 抽出来，因为这段逻辑此前**只有源码字符串断言**
（`assert 'if "/" not in p: continue' in src`）——而它是「以 root 解压之前」
的最后一道防线，路径穿越就靠它挡。字符串断言证明不了它真的拦得住。

抽出来之后可以用真实恶意 tar 成员名做行为测试（见 tests/test_tarcheck.py）。

规则（与原实现逐条对应）：
  · 绝对路径、或任一含 `..` 的路径 → 拒绝
  · 目录条目（以 / 结尾）→ 放行（解压时由 tar 自己建）
  · 不含 `/` 的顶层文件 → 放行：它们只能落在包根，无法穿越，也不会被部署到
    系统路径（后续只把 priv/* 铺到 LIB_DIR）。
    **这一条不能改成"允许清单"**：一旦新版本新增文档（LICENSE/CHANGELOG…），
    旧版的白名单就会拒掉新包——而执行校验的恰是旧版代码，升级通道自我阻塞，
    无法自愈（v0.1.62 踩过）。
  · 其余必须落在已知顶层目录下

用法：
    tar -tzf pkg.tar.gz | tarcheck.py       # 通过退出 0，否则退出 1 并打印原因
    tarcheck.py --names a/b c/d             # 直接给成员名（调试用）
"""

import json
import sys

# 允许的顶层目录。新增顶层目录前务必确认：**旧版本**的白名单里也得有它，
# 否则旧版会拒绝新包（升级通道自我阻塞）。
ALLOW_PREFIX = (
    "panel/", "priv/", "config/", "systemd/", "network/", "singbox/",
    "sudoers/", ".github/",
)


def check(names):
    """返回 (ok, 说明)。说明仅在 ok=False 时有意义。"""
    for raw in names:
        p = raw.rstrip("\n")
        if not p:
            continue
        # 绝对路径与 `..` 一律拒绝（含 a/../../b 这种中段穿越）
        if p.startswith("/") or ".." in p.split("/"):
            return False, f"升级包含危险路径：{p}"
        if p.endswith("/"):
            continue                      # 目录条目
        if "/" not in p:
            continue                      # 顶层文件：落在包根，安全
        if p.startswith(ALLOW_PREFIX):
            continue
        return False, f"升级包含不认识的条目：{p}"
    return True, ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--names":
        names = argv[1:]
    else:
        names = sys.stdin.read().split("\n")

    ok, reason = check(names)
    if not ok:
        print(json.dumps({"ok": False, "code": "UPG_BAD", "reason": reason},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
