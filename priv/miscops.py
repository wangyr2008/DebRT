#!/usr/bin/env python3
"""零散但必须存在的特权动作（v0.1.91 / P2-10）。

从 `dert-priv` 的最后三段内联 Python / case 分支搬出来：

  * `session-secret` —— 面板签会话 cookie 用的密钥。**必须只经 stdout 返回**，
    不进 argv、不落日志。
  * `config-get`    —— 面板首屏读的整份配置。这里派生 `wan.has_password`
    （明文密码永远不在 config.json 里，面板只能看到"有没有设过"），
    并剔除 `notes`（备注只在备份里流转，不回显给前端）。
  * `service-ctl`   —— podman / tailscale 的启停与开机自启。这两者都是
    "DebRT 装了但不管流量"的外部件，所以只做服务级开关，不碰它们的配置。
"""

import json
import os
import shutil
import subprocess
import sys

import privlib
from privlib import CONFIG_JSON, SECRETS_JSON, die, ok_out


def _sh(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def session_secret():
    try:
        with open(SECRETS_JSON, encoding="utf-8") as f:
            secret = json.load(f).get("session_secret", "")
    except Exception:
        die("NO_SECRET", "secrets.json 缺失或损坏", "请重新运行 install.sh")
    ok_out(secret=secret)


def config_get():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        die("NO_CONFIG", "config.json 缺失或损坏",
            "可从 /var/lib/dert/lastgood 恢复上一份可用配置，或重新运行 install.sh")
    try:
        with open(SECRETS_JSON, encoding="utf-8") as f:
            has_pw = bool(json.load(f).get("pppoe_password"))
    except Exception:
        has_pw = False
    cfg.setdefault("wan", {})["has_password"] = has_pw
    cfg.pop("notes", None)   # 备注只在备份里流转，不回显给前端
    ok_out(config=cfg)


PODMAN_UNITS = ("podman.socket", "podman.service", "podman-restart.service")


def _podman(action):
    if action == "start":
        _sh(["systemctl", "start", "podman.socket", "podman.service"])
        _sh(["podman", "start", "--all"], timeout=300)
    elif action == "stop":
        _sh(["podman", "stop", "--all"], timeout=300)
        _sh(["systemctl", "stop", "podman.service", "podman.socket"])
    elif action == "enable":
        r = _sh(["systemctl", "enable", "--quiet"] + list(PODMAN_UNITS))
        if r is None or r.returncode != 0:
            die("SVC", "设置开机自启失败", "请确认已安装 podman：apt install podman")
    elif action == "disable":
        _sh(["systemctl", "disable", "--quiet"] + list(PODMAN_UNITS))
    else:
        die("USAGE", "service-ctl podman 需要 start|stop|enable|disable")


def _tailscale(action):
    if not shutil.which("tailscale"):
        die("TS_NOT_INSTALLED", "Tailscale 尚未安装", "请在「其他工具」中先一键安装")
    unit = "tailscaled.service"
    if action in ("start", "stop"):
        _sh(["systemctl", action, unit])
    elif action in ("enable", "disable"):
        _sh(["systemctl", action, "--quiet", unit])
    else:
        die("USAGE", "service-ctl tailscale 需要 start|stop|enable|disable")


def service_ctl(name, action):
    privlib.require_root()
    if name == "podman":
        _podman(action)
    elif name == "tailscale":
        _tailscale(action)
    else:
        die("USAGE", "不支持的服务：%s" % (name or "（空）"))
    ok_out(message="已完成 %s %s" % (name, action))


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    if action == "session-secret":
        session_secret()
    elif action == "config-get":
        config_get()
    elif action == "service-ctl":
        service_ctl(argv[2] if len(argv) > 2 else "", argv[3] if len(argv) > 3 else "")
    else:
        die("USAGE", "未知动作：%s" % (action or "（空）"),
            "合法动作：session-secret / config-get / service-ctl")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
