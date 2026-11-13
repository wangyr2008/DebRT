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
import shutil
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


# ---------------------------------------------------------------------------
# 配置快照 / 回滚（v0.1.99）
# ---------------------------------------------------------------------------
# 这段逻辑原来在三个地方各写了一遍：configops（保存前）、applyops（存量迁移前）、
# backupops（恢复/出厂前）。三份实现当时是一致的，但"一致"是靠人盯着的——
# 本项目已经因为"清理条件与生成器各写一遍"出过一次 LAN→WAN 的事故。
# 轮转规则（只留最近 5 份）尤其容易改漏一处，所以统一到这里。
def snapshot(tag, keep=5):
    """快照 config.json + secrets.json 到 lastgood/，只保留最近 keep 份。"""
    d = VAR + "/lastgood"
    os.makedirs(d, exist_ok=True)
    for src, name in ((CONFIG_JSON, "config.json"), (SECRETS_JSON, "secrets.json")):
        if os.path.isfile(src):
            try:
                shutil.copy2(src, "%s/%s.pre-%s" % (d, name, tag))
            except OSError:
                pass
    try:
        snaps = sorted((f for f in os.listdir(d) if f.startswith("config.json.pre-")),
                       key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)
    except OSError:
        return
    for f in snaps[keep:]:
        for name in ("config.json.pre-", "secrets.json.pre-"):
            try:
                os.unlink(os.path.join(d, name + f[len("config.json.pre-"):]))
            except OSError:
                pass


def restore(tag):
    """从 lastgood 回滚。没有对应快照时返回 False——调用方不能当成功。"""
    d = VAR + "/lastgood"
    cfg = "%s/config.json.pre-%s" % (d, tag)
    if not os.path.isfile(cfg):
        return False
    try:
        shutil.copy2(cfg, CONFIG_JSON)
        sec = "%s/secrets.json.pre-%s" % (d, tag)
        if os.path.isfile(sec):
            shutil.copy2(sec, SECRETS_JSON)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# 结构化事件日志（v0.1.95 / P4-2）
# ---------------------------------------------------------------------------
# 统一格式：`event=<snake_case> key=value …`
#
# 为什么改：原来全是自由文本（"已清理陈旧的生成文件：/etc/..."），想回答
# "最近 24 小时签名失败几次""哪些生成物被清理过"只能靠正则猜，而且中文标点
# 一变就猜不到。改成固定字段后 `journalctl -t dert-apply | grep event=cleanup`
# 就能直接聚合。人类可读的说明仍保留在 msg= 字段里，不丢信息。
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:@%+-,=")


def _kv(value):
    """值转成单行 token：安全字符原样，其余加双引号（日志里不能出现换行）。"""
    text = str(value).replace("\r", " ").replace("\n", " ")
    if text and all(c in _SAFE for c in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _emit(tag, line):
    """真正写 journald。抽出来是为了测试能替换掉（不污染本机 syslog）。"""
    try:
        subprocess.run(["logger", "-t", "dert-" + tag, line],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def log_event(tag, event, **fields):
    """写一条结构化事件日志。tag 会自动加 `dert-` 前缀。

    字段按字母序输出，保证同样的内容永远得到同一行——便于 diff 与断言。
    """
    parts = ["event=" + _kv(event)]
    for key in sorted(fields):
        if fields[key] is None:
            continue
        parts.append("%s=%s" % (key, _kv(fields[key])))
    _emit(tag, " ".join(parts))


def write_bytes(path, data, mode=0o644):
    """字节级原子写：备份包里的文件按原样落盘，不经文本编解码。"""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".dert-tmp." + os.urandom(4).hex())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        die("IO", "写入失败：%s" % e, "请检查 %s 的权限与磁盘空间" % d)


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
