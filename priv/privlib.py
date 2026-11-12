#!/usr/bin/env python3
"""从 `dert-priv` 抽出的 Python 模块共用的基础设施（v0.1.82 / P2）。

为什么要有这个文件：域模块（container / singbox / …）需要与 bash 版**逐字节相同**
的 JSON 协议、同样的数据根解析、同样的排他锁语义。这些如果各写一遍，迟早漂移。

三个约定：
  1. `die()` 输出 `{"ok":false,"code","message","hint"}` 到 stdout 并退出 1 ——
     面板靠这个协议渲染错误，字段少一个就白屏。
  2. `ok_out()` 输出 `{"ok":true,...}`，值尝试按 JSON 解析（true/false/数字为真类型）。
  3. 数据根从 `DERT_FSROOT` 推导：未设时为空串，路径与生产**逐字符等价**。
"""

import fcntl
import json
import os
import subprocess
import sys
import time

# ---- 数据根（与 lib.sh 的推导方式一致）------------------------------------
_FS = os.environ.get("DERT_FSROOT", "")
ETC = _FS + "/etc/dert"
VAR = _FS + "/var/lib/dert"
RUN = _FS + "/run/dert"
LIB = os.environ.get("DERT_LIB", "/usr/local/lib/dert")

CONFIG_JSON = ETC + "/config.json"
SECRETS_JSON = ETC + "/secrets.json"


def die(code, message, hint=""):
    print(json.dumps({"ok": False, "code": code, "message": message, "hint": hint},
                     ensure_ascii=False))
    sys.exit(1)


def ok_out(**kw):
    """输出 {"ok": true, ...}。与 bash 版一致：字符串值若本身是合法 JSON 字面量
    （true/false/数字），按真类型输出，否则按字符串。"""
    out = {"ok": True}
    for k, v in kw.items():
        out[k] = v
    print(json.dumps(out, ensure_ascii=False))


def ok_pairs(*pairs):
    """兼容 bash 版的 ok_out KEY VAL 形式（值为字符串时尝试 JSON 解析）。"""
    out = {"ok": True}
    items = list(pairs)
    for i in range(0, len(items) - 1, 2):
        k, v = items[i], items[i + 1]
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        else:
            out[k] = v
    print(json.dumps(out, ensure_ascii=False))


def require_root():
    if os.geteuid() != 0:
        die("PERM", "需要 root 权限", "该操作只能由面板经 sudo 调用")


def load_json(path, what):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        die("NO_CONFIG", f"未找到 {what}", "请先运行 install.sh 完成部署")
    except Exception as e:
        die("CONFIG_CORRUPT", f"{what} 解析失败：{e}",
            f"可从 {VAR}/lastgood 恢复上一份可用配置")


def run(cmd, timeout=30, **kw):
    """跑命令，返回 CompletedProcess（失败也不抛，由调用方判 returncode）。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except Exception:
        return None


def run_out(cmd, timeout=30):
    """跑命令并返回 stdout（失败返回空串）。"""
    r = run(cmd, timeout=timeout)
    return (r.stdout or "") if r else ""


def pod_lock(timeout=600):
    """容器操作的排他锁（对应 bash 版 `flock -w 600 /run/dert/podman.lock`）。

    只读操作不进锁：pull 最长 660s，拉大镜像期间若占锁，开容器页会 504
    （真机实测）。
    """
    path = RUN + "/podman.lock"
    try:
        os.makedirs(RUN, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        die("LOCK", f"无法创建锁文件：{e}", "请检查 /run/dert 是否可写")
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= deadline:
                die("BUSY", "另一个容器操作正在进行中，请稍后重试")
            time.sleep(0.5)


def write_atomic(path, content, mode=0o644):
    """同目录临时文件 → rename。与 bash 版 write_atomic 等价。"""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".dert-tmp." + os.urandom(4).hex())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        die("IO", f"写入失败：{e}", f"请检查 {d} 的权限与磁盘空间")
