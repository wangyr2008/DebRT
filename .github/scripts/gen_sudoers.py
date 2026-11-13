#!/usr/bin/env python3
"""从 `priv/dert-priv` 的分发表生成 sudoers 白名单（v0.1.97 / 审计第 8 条）。

为什么用生成而不是手写：

原来的规则是 `dert-priv *` —— 子命令白名单只在脚本内部。审计的意见是"参数面
比必要的大"：面板用户理论上可以给任何子命令传任何参数。收紧成逐个列举的风险
众所周知——**白名单会漂移**：新增子命令忘了同步 sudoers，面板就整条断链，
而"新增子命令"恰恰发生在升级通道里，用户没法从面板上自救。

所以这里的做法是：**唯一事实来源是 `dert-priv` 的 case 分发表**，这个脚本把它
翻译成 sudoers 片段，CI 守卫（P3-36）比对"仓库里的 sudoers/dert 是否等于生成
结果"。漂移在 CI 阶段就被拦住，不用靠人记。

规则：
  · 不带参数的子命令 → 只允许裸形式（`dert-priv status`），传参直接不匹配
  · 带参数的子命令   → 同时允许裸形式与 ` *`（sudo 的 `*` 匹配整串参数）

用法：
    gen_sudoers.py            # 打印到 stdout
    gen_sudoers.py --write    # 写回 sudoers/dert
    gen_sudoers.py --check    # 与仓库文件不一致时退出 1（CI 用）
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRIV = os.path.join(ROOT, "priv", "dert-priv")
TARGET = os.path.join(ROOT, "sudoers", "dert")
BIN = "/usr/local/lib/dert/dert-priv"

HEADER = """\
# DebRT：面板用户仅可经此唯一入口执行特权操作。
#
# 本文件由 .github/scripts/gen_sudoers.py 从 priv/dert-priv 的分发表生成，
# **请勿手工编辑**——CI 守卫 P3-36 会比对生成结果，改了就红。
# 新增子命令后跑一次：python3 .github/scripts/gen_sudoers.py --write
#
# 为什么不用 `dert-priv *`：那样任何子命令、任何参数都放行，子命令白名单只在
# 脚本内部；逐个列举把"能跑什么"提前到内核前的策略层。带参数的子命令同时保留
# 裸形式与 ` *`，避免面板传空参时反而匹配不上（真机上出现过）。
Defaults:dert !requiretty
# sudo 审计写入专用日志文件（/etc/logrotate.d/dert 负责轮转）。
# 注意：不能写 "!syslog, logfile=..." —— sudoers 里 syslog 是取值选项而非布尔旗标，
# 加 ! 会被 visudo 拒绝（CI 冒烟实测），因此采用双重记录；面板轮询已放宽到 8 秒。
Defaults:dert logfile=/var/log/dert-sudo.log
"""


def subcommands(path=PRIV):
    """从分发表里取 (子命令, 是否带参数)。

    只认 `case "$CMD" in` 到 `esac` 之间、形如 `  名称)  ... ;;` 的行。
    """
    text = open(path, encoding="utf-8").read()
    m = re.search(r'case "\$CMD" in(.*?)\nesac', text, re.S)
    if not m:
        raise SystemExit("找不到分发表（case \"$CMD\" in）")
    body = m.group(1)
    out = []
    for chunk in body.split(";;"):
        if not chunk.strip():
            continue
        # 每个分支前面可能带一段注释（`# ---- 认证 ----`），不能整块丢掉；
        # 只认第一处出现在行首的 `名称)` 标签。
        mm = re.search(r"^[ \t]*([a-z][a-z0-9_-]*)\)", chunk, re.M)
        if not mm:
            # 兜底分支 `*)` 允许没有名字；其余分支解析不出来就直接报错。
            # 否则新加一个 `fooBar)` 或 `foo_bar)` 会被**静默漏掉**，
            # sudoers 少一条 → 面板"命令不允许" → 用户只能干瞪眼。
            if re.search(r"^[ \t]*\*\)", chunk, re.M):
                continue
            raise SystemExit("分发表里有无法识别的分支：%r" % chunk.strip()[:60])
        name = mm.group(1)
        # 参数面：分发表里出现 $1 / ${1:-} / $@ 就认为它接受参数
        takes_args = bool(re.search(r'\$\{?1|\$@', chunk))
        out.append((name, takes_args))
    return out


def render(subs, bin_path=BIN):
    lines = []
    for name, takes_args in subs:
        lines.append("%s %s" % (bin_path, name))
        if takes_args:
            lines.append("%s %s *" % (bin_path, name))
    # 一行一条、每条都以 `, \` 续行（sudoers 要求**每一行**都带续行符，
    # 只给部分行加会报 syntax error——第一版就是这么写错的）
    alias = "Cmnd_Alias DERT_PRIV = " + ", \\\n    ".join(lines)
    return HEADER + alias + "\n\ndert ALL=(root) NOPASSWD: DERT_PRIV\n"


def main(argv):
    subs = subcommands()
    text = render(subs)
    if "--write" in argv:
        with open(TARGET, "w", encoding="utf-8") as f:
            f.write(text)
        print("已写入 %s（%d 个子命令）" % (TARGET, len(subs)))
        return 0
    if "--check" in argv:
        try:
            cur = open(TARGET, encoding="utf-8").read()
        except OSError:
            print("sudoers/dert 缺失，请运行 --write")
            return 1
        if cur != text:
            print("sudoers/dert 与分发表不一致——新增/删除了子命令？"
                  "跑一次 gen_sudoers.py --write")
            return 1
        print("sudoers 与分发表一致（%d 个子命令）" % len(subs))
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
