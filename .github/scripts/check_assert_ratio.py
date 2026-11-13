#!/usr/bin/env python3
"""守卫断言的「字符串化率」棘轮（v0.1.75 / P1-2）。

背景：`.github/scripts/test_guards.py` 里大量断言是
`assert "某段文本" in 源码` —— 它断言的是**代码长什么样**，不是**行为对不对**。
这类断言的固有问题：注释里提到就误报、重构改名就误报、清理死代码时会被
自己的说明文字绊倒（本项目已踩过 4 次）。

P1-2 的方向是把它逐步换成「跑一遍、断言产物」的行为测试。但迁移是渐进的，
所以这里放一个**棘轮**：字符串断言的数量只能降不能升。新增断言若必须是
源码断言（例如"三处通道都要装同一个文件"这种跨文件约定），需要同时下调
预算并在注释里说明理由——这会强迫作者先想一下"能不能行为化"。

用法：
    python3 .github/scripts/check_assert_ratio.py          # 只报告
    python3 .github/scripts/check_assert_ratio.py --check  # 超预算则退出 1
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARDS = os.path.join(ROOT, ".github", "scripts", "test_guards.py")

# 当前基线（2026-11-12 / v0.1.76：132 条。已行为化的两批：DNS 分流 14 条、
# 升级包白名单 2 条。剩余多为"通道同步"类跨文件约定，属真正该留的架构约束）。
# 只允许下调。上调必须在这里写明理由。
# v0.1.77：132 → 133。新增的是「tar 的 stderr 不得合进成员清单管道」——
# 该 bug 只在 upgrade-apply 以 root 跑且包含 `..` 时显现（真机实测踩到），
# 没有不依赖 root 的行为测试办法；它会让安全错误提示变成 tar 的警告原文，
# 属于必须锁住的坑。
# v0.1.81：133 → 134。新增的是「install.sh 必须用 priv/*.py 通配部署」——
# 这是部署通道的架构约束（改成逐个列举就会漏装新模块），没有不跑安装脚本的
# 行为测试办法。同批还加了"dert-priv 引用的 priv 文件必须存在"的结构检查
# （那条不走字符串断言，不计入）。
# v0.1.87：134 → 133（只降不升）。apply-all 搬进 applyops.py 时，P3-12b / P3-13
# 两条"对 bash 源码做正则"的断言被**行为化**了：现在直接跑一遍收敛流水线，
# 断言"预检报错原样透传""无漂移分支删了 stage 且没重启 networkd/dnsmasq"。
# 这比看源码文本可靠——那两条原本就踩过"注释里提到命令导致假绿"的坑。
# v0.1.90：132 → 133。新增的是「dert-converge.service 必须 Restart=on-failure」——
# 这是 systemd 单元级的约定，只能靠读单元文件断言：它要防的是"升级进程还持锁时
# 被安排重启的收敛必然 BUSY 退出"，而那次退出只有在真机的 systemd 上才复现
# （本机无法在无 root 环境里跑 systemd 单元）。真机 2026-11-13 实测到过，
# 表现是 health 的 failed_units 变红。
STRING_ASSERT_BUDGET = 133


def count():
    src = open(GUARDS, encoding="utf-8").read()
    total = len(re.findall(r"^assert ", src, re.M))
    # 字符串包含式：assert ... in / not in
    str_in = len(re.findall(r"^assert .+ (?:not )?in ", src, re.M))
    regex = len(re.findall(r"^assert re\.", src, re.M))
    return total, str_in, regex


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="超预算则失败")
    args = ap.parse_args()

    total, str_in, regex = count()
    pct = (str_in * 100 // total) if total else 0
    print(f"守卫断言：共 {total} 条")
    print(f"  字符串包含式 {str_in} 条（{pct}%）  预算 {STRING_ASSERT_BUDGET}")
    print(f"  正则式       {regex} 条")
    print(f"  其他         {total - str_in - regex} 条")

    if not args.check:
        return 0

    if str_in > STRING_ASSERT_BUDGET:
        print(f"\n❌ 字符串断言增加到 {str_in}，超过预算 {STRING_ASSERT_BUDGET}。")
        print("   请优先写成行为断言（跑脚本/生成器，断言产物）；")
        print("   确实只能源码断言的，请在 check_assert_ratio.py 里说明理由并调整预算。")
        return 1

    if str_in < STRING_ASSERT_BUDGET:
        print(f"\n💡 已低于预算 {STRING_ASSERT_BUDGET} 条，可把预算下调到 {str_in} 锁定成果。")

    print("\n✅ 字符串断言未超预算")
    return 0


if __name__ == "__main__":
    sys.exit(main())
