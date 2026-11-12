#!/usr/bin/env python3
"""备份包成员校验（v0.1.84 / P2-4）。

从 `dert-priv` 的 `do_backup_restore` 抽出。这段是**恢复前的最后一道校验**，
而且踩过一类很隐蔽的绕过：

    `etc/dert/./secrets.json`、`etc/dert//secrets.json`

它们能同时绕过「secrets 精确匹配」和「前缀白名单」，而 tar 解压时又会自动
规范化成 `etc/dert/secrets.json` —— 端到端可以写穿（复审 N-01）。
所以判定必须**先 `posixpath.normpath` 归一，再比对**。

三种判定：
  OK       —— 全部成员都是 etc/dert 下或 var/lib/dert/notes.json
  SECRETS  —— 含 secrets.json（密码哈希/会话密钥不随备份流转）
  BAD      —— 绝对路径、含 `..`、或任何白名单外的条目

用法：
    tar -tzf backup.tar.gz | backupcheck.py      # 输出判定词，OK 时退出 0
"""

import posixpath
import sys

# 备份只包含这两处：配置目录 + 备注文件
ALLOW_DIR = "etc/dert"
ALLOW_FILE = "var/lib/dert/notes.json"
FORBIDDEN = "etc/dert/secrets.json"


def check(names):
    """返回 'OK' / 'SECRETS' / 'BAD'。"""
    for raw in names:
        p = raw.rstrip("\n")
        if not p:
            continue
        # 绝对路径与 `..` 一律拒绝（含 a/../../b 这种中段穿越）
        if p.startswith("/") or ".." in p.split("/"):
            return "BAD"
        # 归一之后再判断：tar 解压会规范化，不归一会漏掉 `./` 与 `//` 变体
        np = posixpath.normpath(p)
        if np == FORBIDDEN:
            return "SECRETS"
        if not (np == ALLOW_DIR or np.startswith(ALLOW_DIR + "/") or np == ALLOW_FILE):
            return "BAD"
    return "OK"


def main(argv=None):
    names = sys.stdin.read().split("\n")
    verdict = check(names)
    print(verdict)
    return 0 if verdict == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
