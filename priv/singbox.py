#!/usr/bin/env python3
"""sing-box 域 —— 从 `dert-priv` 抽出（v0.1.85 / P2-5）。

10 个子动作：status / ctl / autostart / install / sub-set / sub-update /
schedule / uninstall / dash-set / check。

**DebRT 不管理 sing-box 的流量路径**（分流由 nft + 配置决定，见 COEXISTENCE.md），
这里只做服务开关、订阅地址、自动更新、面板地址这些"控制面"的事。

数据路径：
  · `/etc/sing-box`、`/var/lib/sing-box`、`/etc/cron.d/sing-box` 是 sing-box
    自己的目录，同样跟随 `DERT_FSROOT`（测试用；未设时与生产逐字符等价）。
  · `sb` 命令走 `/usr/local/bin/sb`，可用 `DERT_SB_BIN` 覆盖（仅测试用）。

几个踩过的坑，逐条保留：
  · `clash_api` 在新版是 `services[type=api]`，且 **listen 与 listen_port 是两个
    字段**——只取 listen 会拼出 `host:0.0.0.0` 死链（N-01）。
  · `sub-set` 的 URL 要过 `validate.url_host_blacklisted`，否则可以指向内网。
  · `schedule` 写 `/etc/cron.d/sing-box` 要保留其它行，只替换 sb update 那一行。
"""

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import privlib as P  # noqa: E402
import validate  # noqa: E402

_FS = os.environ.get("DERT_FSROOT", "")
SB = _FS + "/etc/sing-box"
SB_VAR = _FS + "/var/lib/sing-box"
CRON = _FS + "/etc/cron.d/sing-box"
SB_BIN = os.environ.get("DERT_SB_BIN", "/usr/local/bin/sb")


def _run(cmd, timeout=10):
    return P.run(cmd, timeout=timeout)


def _out(cmd, timeout=10):
    r = _run(cmd, timeout=timeout)
    return (r.stdout or "") if r else ""


# ---------------------------------------------------------------- status
def act_status():
    installed = bool(_out(["sh", "-c", "command -v sing-box"]).strip())
    sb_present = os.access(SB_BIN, os.X_OK)
    active = enabled = False
    version = ""
    try:
        with open(SB + "/config.json", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    if installed:
        r = _run(["systemctl", "is-active", "--quiet", "sing-box.service"])
        active = bool(r and r.returncode == 0)
        r = _run(["systemctl", "is-enabled", "--quiet", "sing-box.service"])
        enabled = bool(r and r.returncode == 0)
        v = _out(["sing-box", "version"]).split("\n")[0]
        m = re.search(r"([0-9]+\.[0-9]+\.[0-9]+)", v or "")
        version = m.group(1) if m else ""

    # 订阅地址（脱敏展示）
    url_set = False
    url_masked = ""
    try:
        with open(SB + "/config.url", encoding="utf-8") as f:
            u = f.read().strip().splitlines()[0].strip()
    except Exception:
        u = ""
    if u:
        url_set = True
        from urllib.parse import urlparse
        p = urlparse(u)
        path = p.path or ""
        if len(path) > 14:
            path = path[:8] + "…" + path[-4:]
        url_masked = p.scheme + "://" + p.netloc + path

    # 自动更新（/etc/cron.d/sing-box 里的 sb update 行）
    auto_on = False
    auto_time = ""
    try:
        with open(CRON, encoding="utf-8") as f:
            for line in f:
                if "/usr/local/bin/sb update" in line:
                    fields = line.split()
                    if len(fields) >= 2:
                        auto_time = fields[1].zfill(2) + ":" + fields[0].zfill(2)
                        auto_on = True
                    break
    except Exception:
        pass

    upd = {"ok": None, "ts": None, "error": "", "trigger": ""}
    try:
        with open(SB_VAR + "/update-state.json", encoding="utf-8") as f:
            d = json.load(f)
        upd = {"ok": bool(d.get("ok")), "ts": int(d.get("ts") or 0),
               "error": str(d.get("error") or ""), "trigger": str(d.get("trigger") or "")}
    except Exception:
        pass

    ep_present, ep_host = False, ""
    for e in (cfg.get("endpoints") or []):
        if e.get("type") == "tailscale":
            ep_present = True
            ep_host = str(e.get("hostname") or "")
            break

    clash = (cfg.get("experimental") or {}).get("clash_api") or {}
    controller = str(clash.get("external_controller") or "")
    if not controller:
        # A-06：新版把 API 放在 services[type=api]；N-01：listen 与 listen_port
        # 是两个字段，只取 listen 会拼出 host:0.0.0.0 死链
        for svc in (cfg.get("services") or []):
            if svc.get("type") == "api":
                host = str(svc.get("listen") or "127.0.0.1")
                port = svc.get("listen_port")
                controller = f"{host}:{port}" if port else host
                break

    ui = False
    ui_dir = str(clash.get("external_ui") or "")
    if ui_dir:
        full = ui_dir if ui_dir.startswith("/") else os.path.join(SB, ui_dir)
        ui = os.path.isdir(full)

    custom = ""
    try:
        with open(P.VAR + "/singbox-dash-url", encoding="utf-8") as f:
            custom = f.read().strip()
    except Exception:
        custom = ""

    up_s = None
    if active:
        pid = _out(["systemctl", "show", "sing-box.service",
                    "--property=MainPID", "--value"]).strip()
        if pid and pid != "0":
            try:
                up_s = int(_out(["ps", "-o", "etimes=", "-p", pid]).strip() or 0)
            except ValueError:
                up_s = None

    print(json.dumps({"ok": True, "installed": installed, "sb": sb_present,
                      "active": active, "enabled": enabled, "version": version,
                      "uptime_s": up_s,
                      "url_set": url_set, "url_masked": url_masked,
                      "autoupdate": {"on": auto_on, "time": auto_time},
                      "update": upd,
                      "endpoint": {"present": ep_present, "hostname": ep_host},
                      "dashboard": {"controller": controller, "ui": ui,
                                    "custom": custom}}, ensure_ascii=False))


# ---------------------------------------------------------------- 服务开关
def _require_unit():
    if _run(["systemctl", "list-unit-files", "sing-box.service"]) is None or \
            _run(["systemctl", "list-unit-files", "sing-box.service"]).returncode != 0:
        P.die("SB_NOT_INSTALLED", "sing-box 未安装", "请在「其他工具」页一键安装")


def act_ctl(act):
    _require_unit()
    if act not in ("start", "stop", "restart"):
        P.die("USAGE", "singbox-ops ctl 需要 start|stop|restart")
    P.run(["systemctl", act, "sing-box.service"])
    P.ok_out(message=f"sing-box {act} 完成")


def act_autostart(on):
    _require_unit()
    if on not in ("on", "off"):
        P.die("USAGE", "singbox-ops autostart 需要 on|off")
    if on == "on":
        P.run(["systemctl", "enable", "sing-box.service"])
    else:
        P.run(["systemctl", "disable", "sing-box.service"])
    P.ok_out(message="开机自启已" + ("开启" if on == "on" else "关闭"))


# ---------------------------------------------------------------- sb 相关
def _require_sb():
    if not os.access(SB_BIN, os.X_OK):
        P.die("SB_NO_SB", "未找到 sb 命令",
              "sb.sh 由 DebRT 安装包下发（/usr/local/bin/sb）；请重装 DebRT")


def act_install():
    _require_sb()
    r = _run([SB_BIN, "install"], timeout=900)
    if r is None or r.returncode != 0:
        if r and r.stdout:
            sys.stderr.write("\n".join(r.stdout.splitlines()[-5:]) + "\n")
        P.die("SB_INSTALL_FAIL", "sing-box 安装/升级失败",
              "以上为 sb 输出尾部；GitHub 被墙时可稍后重试（sb 走 gh-proxy 回退）")
    tail = "\n".join((r.stdout or "").splitlines()[-3:])
    P.ok_out(message="sing-box 内核已就绪", tail=tail)


def act_sub_update():
    _require_sb()
    env = dict(os.environ)
    env["SB_UPDATE_TRIGGER"] = "manual"
    r = _run([SB_BIN, "update"], timeout=900)
    rc = r.returncode if r else 1

    st_ok = ""
    try:
        with open(SB_VAR + "/update-state.json", encoding="utf-8") as f:
            st_ok = "1" if json.load(f).get("ok") else "0"
    except Exception:
        st_ok = "1" if rc == 0 else "0"

    if st_ok != "1":
        st_err = ""
        try:
            with open(SB_VAR + "/update-state.json", encoding="utf-8") as f:
                st_err = str(json.load(f).get("error") or "")
        except Exception:
            out = (r.stdout or "") if r else ""
            st_err = " ".join(out.splitlines()[-2:])[:200]
        P.die("SB_UPDATE_FAIL", f"配置更新失败：{st_err or '原因未记录'}",
              "失败原因会持续显示在本页；旧配置仍在运行，可稍后重试")
    P.ok_out(message="配置已更新，sing-box 已重启加载新配置")


def act_uninstall():
    if not os.access(SB_BIN, os.X_OK):
        P.die("SB_NOT_INSTALLED", "sing-box 未安装")
    r = _run([SB_BIN, "uninstall", "--yes"], timeout=600)
    if r is None or r.returncode != 0:
        if r and r.stdout:
            sys.stderr.write("\n".join(r.stdout.splitlines()[-5:]) + "\n")
        P.die("SB_UNINSTALL_FAIL", "卸载失败",
              "以上为 sb 输出尾部；可稍后重试或在控制台执行 sb uninstall")
    P.ok_out(message="sing-box 已卸载（原配置备份在 /var/lib/dert/）")


def act_check():
    cur = ""
    m = re.search(r"[0-9]+\.[0-9]+\.[0-9]+", _out(["sing-box", "version"]))
    if m:
        cur = m.group(0)
    if not cur:
        P.die("SB_NOT_INSTALLED", "sing-box 未安装", "请在「其他工具」页一键安装")

    # 与 dert-priv 的 github_latest_ver 同源：直连失败时走 gh-proxy
    latest = ""
    for prefix in ("", "https://gh-proxy.org/", "https://gh-proxy.com/"):
        r = _run(["curl", "-sIL", "--connect-timeout", "8", "--max-time", "30",
                  "-o", "/dev/null", "-w", "%{url_effective}",
                  prefix + "https://github.com/SagerNet/sing-box/releases/latest"],
                 timeout=40)
        eff = (r.stdout or "") if r else ""
        m = re.search(r"/tag/v([0-9]+\.[0-9]+\.[0-9]+)", eff)
        if m:
            latest = m.group(1)
            break
    if not latest:
        P.die("SB_NET", "无法获取 sing-box 最新版本", "GitHub 可能限流，可稍后重试")

    if latest == cur:
        P.ok_pairs("up_to_date", "true", "current", cur, "latest", latest,
                   "message", f"已是最新版本 v{cur}")
    else:
        P.ok_pairs("up_to_date", "false", "current", cur, "latest", latest,
                   "message", f"发现新版本 v{latest}（当前 v{cur}）")


# ---------------------------------------------------------------- 订阅地址
def act_sub_set():
    try:
        body = json.loads(sys.stdin.read() or "{}")
    except Exception:
        P.die("SB_SUB_BAD", "请求格式错误")
    url = str(body.get("url", "")).strip().replace(" ", "")
    if not url:
        P.die("SB_SUB_BAD", "配置地址为空", "请填写配置来源链接后保存")
    if not (url.startswith("https://") or url.startswith("http://")):
        P.die("SB_URL_BAD", "配置地址需以 http(s):// 开头",
              "示例：https://sub.example.com/link/xxx")

    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host or validate.url_host_blacklisted(host):
        P.die("SB_URL_BAD", "配置地址不能指向内网或主机名无效",
              "请使用公网可访问的配置地址")

    os.makedirs(SB, exist_ok=True)
    P.write_atomic(SB + "/config.url", url + "\n", 0o600)
    P.ok_out(message="配置地址已保存（页面上显示脱敏值）")


# ---------------------------------------------------------------- 自动更新
def act_schedule():
    try:
        body = json.loads(sys.stdin.read() or "{}")
    except Exception:
        P.die("SB_SCHED_BAD", "请求格式错误")
    tm = str(body.get("time", "")).strip()

    if tm in ("off", ""):
        if os.path.exists(CRON):
            try:
                with open(CRON, encoding="utf-8") as f:
                    kept = [l for l in f if "/usr/local/bin/sb update" not in l]
            except OSError:
                kept = []
            if any(l.strip() for l in kept):
                P.write_atomic(CRON, "".join(kept), 0o644)
            else:
                try:
                    os.unlink(CRON)
                except OSError:
                    pass
        P.ok_out(message="自动更新已关闭")
        return

    if not re.fullmatch(r"([0-9]|0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]", tm):
        P.die("SB_SCHED_BAD", "时间格式应为 HH:MM（24 小时制）", "示例：04:00、06:30")

    hh = str(int(tm.split(":")[0]))
    mm = str(int(tm.split(":")[1]))
    for svc in ("cron.service", "cron"):
        r = _run(["systemctl", "enable", "--now", svc])
        if r and r.returncode == 0:
            break

    os.makedirs(os.path.dirname(CRON), exist_ok=True)
    body_txt = ("SHELL=/bin/sh\n"
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
                f"{mm} {hh} * * * root /usr/local/bin/sb update\n")
    P.write_atomic(CRON, body_txt, 0o644)
    P.ok_out(time=tm, message=f"配置自动更新已设为每天 {tm}")


# ---------------------------------------------------------------- Dashboard
def act_dash_set():
    try:
        body = json.loads(sys.stdin.read() or "{}")
    except Exception:
        P.die("SB_DASH_BAD", "请求格式错误")
    url = str(body.get("url", "")).strip()

    os.makedirs(P.VAR, exist_ok=True)
    if not url:
        try:
            os.unlink(P.VAR + "/singbox-dash-url")
        except OSError:
            pass
        P.ok_out(message="Dashboard 地址已清除（回到按配置自动推导）")
        return

    if not (url.startswith("https://") or url.startswith("http://")):
        P.die("SB_DASH_BAD", "Dashboard 地址需以 http(s):// 开头",
              "示例：https://metacubexd.pages.dev")

    P.write_atomic(P.VAR + "/singbox-dash-url", url + "\n", 0o644)
    P.ok_out(message="Dashboard 地址已保存")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else ""
    rest = argv[1:]

    P.require_root()

    if action == "status":
        act_status()
    elif action == "ctl":
        act_ctl(rest[0] if rest else "")
    elif action == "autostart":
        act_autostart(rest[0] if rest else "")
    elif action == "install":
        act_install()
    elif action == "sub-set":
        act_sub_set()
    elif action == "sub-update":
        act_sub_update()
    elif action == "schedule":
        act_schedule()
    elif action == "uninstall":
        act_uninstall()
    elif action == "dash-set":
        act_dash_set()
    elif action == "check":
        act_check()
    else:
        P.die("USAGE", "singbox-ops 需要 status|ctl|autostart|install|check|"
                       "dash-set|sub-set|sub-update|schedule|uninstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
