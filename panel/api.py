#!/usr/bin/env python3
"""SeedRou 面板 API 路由。

安全模型：
- 除 /api/session、/api/login 外全部要求有效会话（HttpOnly HMAC cookie）。
- 特权动作全部经 `sudo -n seedrou-priv <白名单子命令>`，面板永不直接写 /etc。
- 错误统一 {ok:false, code, message, hint}，message=原因、hint=下一步，中文可操作。
"""
import json
import os
import re
import secrets as pysecrets
import subprocess
import time

import auth
import sysinfo

MOCK = False
PRIV = "/usr/local/lib/seedrou/seedrou-priv"
UPLOAD_DIR = "/var/lib/seedrou/uploads"
BACKUP_DIR = "/var/lib/seedrou/backups"
CONFIG_FILE = "/etc/seedrou/config.json"
VERSION_FILE = "/usr/local/lib/seedrou/share/VERSION"

STATUS_MAP = {"BUSY": 409, "UNAUTH": 401, "AUTH_LOCKED": 423, "AUTH_BAD": 401,
              "TOO_LARGE": 413, "UPG_NO_SOURCE": 400, "CSRF": 403}


class ApiError(Exception):
    def __init__(self, status, code, message, hint=""):
        self.status, self.code, self.message, self.hint = status, code, message, hint

    def as_dict(self):
        return {"ok": False, "code": self.code, "message": self.message, "hint": self.hint}


def init(mock=False):
    global MOCK, UPLOAD_DIR, BACKUP_DIR
    MOCK = mock
    if mock:
        # mock 模式运行在开发机（无 /var/lib 写权限），用临时目录
        import tempfile
        base = os.path.join(tempfile.gettempdir(), "seedrou-mock")
        UPLOAD_DIR = os.path.join(base, "uploads")
        BACKUP_DIR = os.path.join(base, "backups")
    for d in (UPLOAD_DIR, BACKUP_DIR):
        os.makedirs(d, exist_ok=True)


# ---------------- priv 调用 ----------------
def priv(*args, input_text=None, timeout=90):
    """执行 seedrou-priv 子命令；返回解析后的 JSON dict；{ok:false} 抛 ApiError。"""
    if MOCK:
        return mock_priv(args, input_text)
    try:
        p = subprocess.run(["sudo", "-n", PRIV, *args], input=input_text,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "TIMEOUT", "操作超时", "任务可能仍在后台进行，请稍后刷新页面确认结果")
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        raise ApiError(500, "PRIV_MALFORMED", "特权服务返回异常",
                       "请在系统上执行 seedrou-priv selftest 检查；journalctl -u seedrou-panel 查看日志")
    if not data.get("ok", False):
        raise ApiError(STATUS_MAP.get(data.get("code", ""), 400),
                       data.get("code", "PRIV_ERROR"), data.get("message", "操作失败"),
                       data.get("hint", ""))
    return data


def _json_body(body: bytes):
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "BAD_JSON", "请求数据不是有效的 JSON", "请刷新页面后重试")
    if not isinstance(data, dict):
        raise ApiError(400, "BAD_JSON", "请求数据格式不正确")
    return data


def _version():
    if MOCK:
        return "0.1.0"
    try:
        return open(VERSION_FILE).read().strip()
    except Exception:
        return "0.0.0"


def _lan_ip():
    if MOCK:
        return "192.168.10.1"
    try:
        cfg = json.load(open(CONFIG_FILE))
        return cfg.get("lan", {}).get("ip")
    except Exception:
        return None


def _save_upload(body: bytes, prefix: str) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, f"{prefix}-{pysecrets.token_hex(6)}")
    with open(path, "wb") as f:
        f.write(body)
    return path


# ---------------- 路由 ----------------
def handle(method, path, body: bytes, headers):
    route = path.rstrip("/") or "/"
    q = route[len("/api"):] if route.startswith("/api") else route

    # ---- 免登录 ----
    if q == "/session" and method == "GET":
        return 200, {"ok": True, "logged": auth.logged_in(headers),
                     "lan_ip": _lan_ip(), "version": _version()}, None
    if q == "/login" and method == "POST":
        return do_login(body, headers)

    # ---- 登录态 ----
    if not auth.logged_in(headers):
        raise ApiError(401, "UNAUTH", "登录已过期", "请重新登录")

    if method == "GET":
        table = {
            "/status": get_status, "/config": get_config, "/leases": get_leases,
            "/podman/status": lambda b, h: (200, priv("podman-ops", "status"), None),
            "/podman/containers": lambda b, h: (200, priv("podman-ops", "list-containers", timeout=60), None),
            "/podman/images": lambda b, h: (200, priv("podman-ops", "list-images", timeout=60), None),
            "/podman/compose": lambda b, h: (200, priv("podman-ops", "compose-get"), None),
            "/tailscale": lambda b, h: (200, priv("tailscale-ops", "status"), None),
            "/backup": get_backup,
        }
        fn = table.get(q)
        if fn:
            return fn(body, headers)
    else:
        m = re.fullmatch(r"/config/(bind|wan|ipv6|lan|dhcp|static|qos|firewall|upgrade)", q)
        if m:
            return post_config_section(m.group(1), body)
        table = {
            "/notes": post_notes,
            "/portfw": post_portfw,
            "/admin/password": post_password,
            "/logout": do_logout,
            "/podman/ctl": lambda b, h: (200, priv("service-ctl", "podman", _json_body(b).get("action", ""), timeout=180), None),
            "/podman/container": post_podman_container,
            "/podman/pull": lambda b, h: (200, priv("podman-ops", "pull", _json_body(b).get("image", ""), timeout=660), None),
            "/podman/image-rm": lambda b, h: (200, priv("podman-ops", "image-rm", _json_body(b).get("image", ""), timeout=120), None),
            "/podman/compose": post_compose,
            "/tailscale/install": lambda b, h: (200, priv("tailscale-ops", "install", timeout=300), None),
            "/tailscale/ctl": post_ts_ctl,
            "/version/check": lambda b, h: (200, priv("upgrade-check", timeout=90), None),
            "/upgrade/online": post_upgrade_online,
            "/upgrade": post_upgrade_upload,
            "/restore": post_restore,
            "/factory-reset": lambda b, h: (200, priv("factory-reset", timeout=300), None),
        }
        fn = table.get(q)
        if fn:
            return fn(body, headers)

    raise ApiError(404, "NOT_FOUND", "接口不存在")


# ---------------- 登录/登出 ----------------
def do_login(body, headers):
    locked = auth.login_locked(headers)
    if locked:
        return 423, {"ok": False, "code": "AUTH_LOCKED", "message": f"尝试次数过多，请 {locked} 秒后重试",
                     "hint": "密码不会因此失效；稍等即可再次尝试"}, None
    data = _json_body(body)
    password = str(data.get("password", ""))
    if not password:
        return 400, {"ok": False, "code": "AUTH_EMPTY", "message": "请输入管理密码"}, None
    ok, err = auth.priv_verify_password(password)
    if not ok:
        auth.login_fail(headers)
        err = err or {}
        return (423 if err.get("code") == "AUTH_LOCKED" else 401), {
            "ok": False, "code": err.get("code", "AUTH_BAD"),
            "message": err.get("message", "管理密码不正确"), "hint": err.get("hint", "")}, None
    auth.login_ok(headers)
    token = auth.make_token()
    return 200, {"ok": True}, {"Set-Cookie": auth.session_cookie(token)}


def do_logout(body, headers):
    return 200, {"ok": True}, {"Set-Cookie": auth.clear_cookie()}


# ---------------- 概览 / 配置 ----------------
def _wan_dns():
    """概览页展示用上游 DNS：手动模式取配置；自动模式取 dispatcher 维护的 upstream.conf。"""
    try:
        cfg = json.load(open(CONFIG_FILE))
    except Exception:
        return []
    wan = cfg.get("wan", {})
    if wan.get("dns_mode") == "manual" and wan.get("dns"):
        return wan["dns"]
    dns = []
    try:
        with open("/run/seedrou-dns/upstream.conf") as f:
            for line in f:
                if line.startswith("server="):
                    dns.append(line.strip()[len("server="):])
    except OSError:
        pass
    return dns


def get_status(body, headers):
    st = sysinfo.mock_status() if MOCK else priv("status", timeout=30)
    if not MOCK:
        st.setdefault("wan", {}).setdefault("dns", _wan_dns())
    stats = {
        "devices": st.get("leases_count"),
        "cpu": None if MOCK else sysinfo.cpu_pct(),
        "cpu_temp": None if MOCK else sysinfo.temp_cpu(),
        "mem": None if MOCK else sysinfo.mem_pct(),
        "disk_temp": None,  # 需 smartctl；默认不采集，避免依赖
        "uptime_s": None if MOCK else sysinfo.uptime_s(),
    }
    if MOCK:
        stats = sysinfo.mock_status()["stats"]
    st["stats"] = stats
    return 200, st, None


def get_config(body, headers):
    if MOCK:
        return 200, sysinfo.mock_config(), None
    data = priv("config-get", timeout=30)
    return 200, data.get("config", {}), None


def post_config_section(section, body):
    data = _json_body(body)
    payload = data
    if section == "static":
        payload = {"list": data.get("list", [])}
    elif section == "bind":
        payload = {"ports": data.get("ports", [])}
    out = priv("config-set", section, input_text=json.dumps(payload, ensure_ascii=False), timeout=300)
    return 200, {"ok": True, "message": out.get("message", "已保存"),
                 "hint_singbox": out.get("hint_singbox", "")}, None


# ---------------- 设备 / 备注 ----------------
def get_leases(body, headers):
    if MOCK:
        return 200, {"leases": sysinfo._MOCK_LEASES, "static_count": 2}, None
    cfg = priv("config-get", timeout=30).get("config", {})
    leases = sysinfo.leases(cfg.get("static", []))
    return 200, {"leases": leases, "static_count": len(cfg.get("static", []))}, None


def post_notes(body, headers):
    data = _json_body(body)
    notes = data.get("notes", {})
    if not isinstance(notes, dict):
        raise ApiError(400, "BAD_JSON", "备注格式不正确")
    clean = {}
    for k, v in notes.items():
        if re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", str(k)) and isinstance(v, str) and len(v) <= 32:
            clean[k.upper()] = v.strip()
    if MOCK:
        return 200, {"ok": True}, None
    sysinfo.save_notes(clean)
    return 200, {"ok": True, "message": "备注已保存"}, None


# ---------------- 安全中心 ----------------
def post_portfw(body, headers):
    data = _json_body(body)
    out = priv("config-set", "firewall",
               input_text=json.dumps({"portfw": data.get("list", [])}, ensure_ascii=False), timeout=300)
    return 200, {"ok": True, "message": out.get("message", "规则已保存")}, None


def post_password(body, headers):
    data = _json_body(body)
    out = priv("passwd-set",
               input_text=json.dumps({"old": str(data.get("old", "")), "new": str(data.get("new", ""))}),
               timeout=60)
    return 200, {"ok": True, "message": out.get("message", "密码已修改")}, None


# ---------------- 容器 ----------------
def post_podman_container(body, headers):
    data = _json_body(body)
    name = str(data.get("name", ""))[:64]
    action = str(data.get("action", ""))
    if not re.fullmatch(r"[\w.\-]+", name) or not name:
        raise ApiError(400, "BAD_NAME", "容器名不合法")
    if action not in ("start", "stop", "restart", "update", "remove"):
        raise ApiError(400, "BAD_ACTION", "不支持的容器操作")
    return 200, priv("podman-ops", "container", name, action,
                     timeout=660 if action == "update" else 180), None


def post_compose(body, headers):
    data = _json_body(body)
    action = data.get("action", "deploy")
    if action == "deploy":
        content = data.get("content", "")
        if not str(content).strip():
            raise ApiError(400, "COMPOSE_EMPTY", "Compose 内容为空", "请填写 services 配置后再部署")
        priv("podman-ops", "compose-set", input_text=str(content), timeout=30)
        return 200, priv("podman-ops", "compose-deploy", timeout=600), None
    if action == "stop":
        return 200, priv("podman-ops", "compose-stop", timeout=600), None
    raise ApiError(400, "BAD_ACTION", "不支持的 Compose 操作")


# ---------------- Tailscale ----------------
def post_ts_ctl(body, headers):
    data = _json_body(body)
    action = data.get("action", "")
    if action not in ("start", "stop", "login", "logout"):
        raise ApiError(400, "BAD_ACTION", "不支持的 Tailscale 操作")
    return 200, priv("tailscale-ops", action, timeout=120 if action != "install" else 300), None


# ---------------- 升级 / 备份 ----------------
def post_upgrade_online(body, headers):
    data = _json_body(body)
    ver = str(data.get("version", "")).strip()
    if not re.fullmatch(r"v?[0-9]+(\.[0-9]+)+", ver):
        raise ApiError(400, "BAD_VERSION", "版本号格式不正确")
    dl = priv("upgrade-download", ver, timeout=660)
    return 200, priv("upgrade-apply", dl.get("path", ""), timeout=300), None


def post_upgrade_upload(body, headers):
    if not body:
        raise ApiError(400, "EMPTY", "升级包内容为空")
    name = headers.get("X-Filename") or "pkg.tar.gz"
    if not re.search(r"\.tar\.gz$|\.tgz$", name, re.I):
        raise ApiError(400, "BAD_TYPE", "仅支持 .tar.gz 升级包",
                       "Debian 系统没有 .bin/.img 固件概念；请使用官方发布的升级包")
    path = _save_upload(body, "upg")
    try:
        out = priv("upgrade-apply", path, timeout=300)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return 200, out, None


def post_restore(body, headers):
    if not body:
        raise ApiError(400, "EMPTY", "备份文件内容为空")
    path = _save_upload(body, "bak")
    try:
        out = priv("backup-restore", path, timeout=600)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return 200, out, None


def get_backup(body, headers):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = "seedrou-backup-" + time.strftime("%Y%m%d-%H%M%S") + ".tar.gz"
    out_path = os.path.join(BACKUP_DIR, name)
    if MOCK:
        with open(out_path, "wb") as f:
            f.write(b"mock-backup")
    else:
        priv("backup-create", out_path, timeout=120)
    # 清理旧备份，仅保留最近 10 份
    try:
        olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz"))
        for f in olds[:-10]:
            os.remove(os.path.join(BACKUP_DIR, f))
    except OSError:
        pass
    try:
        with open(out_path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass
    return 200, data, {
        "Content-Type": "application/gzip",
        "Content-Disposition": f'attachment; filename="{name}"',
    }


# ---------------- mock 特权实现 ----------------
def mock_priv(args, input_text):
    cmd, rest = args[0], args[1:]
    if cmd == "auth-verify":
        pw = (input_text or "").strip()
        if pw == "admin":
            return {"ok": True}
        return {"ok": False, "code": "AUTH_BAD", "message": "管理密码不正确",
                "hint": "mock 模式密码为 admin"}
    if cmd == "session-secret":
        return {"ok": True, "secret": "mock-session-secret"}
    if cmd == "config-get":
        return {"ok": True, "config": sysinfo.mock_config()}
    if cmd == "status":
        return sysinfo.mock_status()
    if cmd == "config-set":
        section = rest[0]
        cfg = sysinfo.mock_config()
        data = json.loads(input_text or "{}")
        if section == "bind":
            cfg["ports"] = data.get("ports", cfg["ports"])
        elif section == "wan":
            for k in ("mode", "username", "dns_mode", "dns", "ip", "mask", "gw"):
                if k in data:
                    cfg["wan"][k] = data[k]
            if data.get("password"):
                cfg["wan"]["has_password"] = True
        elif section == "ipv6":
            cfg["ipv6"].update(data)
        elif section == "lan":
            cfg["lan"].update(data)
        elif section == "dhcp":
            cfg["dhcp"].update(data)
        elif section == "static":
            cfg["static"] = data.get("list", cfg["static"])
        elif section == "qos":
            cfg["qos"].update(data)
        elif section == "firewall":
            if "wan_input_drop" in data:
                cfg["firewall"]["wan_input_drop"] = data["wan_input_drop"]
            if "portfw" in data:
                cfg["firewall"]["portfw"] = data["portfw"]
        elif section == "upgrade":
            cfg["upgrade"].update(data)
        return {"ok": True, "message": "（mock）已保存并生效", "hint_singbox": ""}
    if cmd == "podman-ops":
        sub = rest[0] if rest else ""
        if sub == "status":
            return {"ok": True, "installed": True, "version": "5.2.5 (mock)", "running": True, "autostart": True}
        if sub == "list-containers":
            return {"ok": True, "containers": sysinfo._MOCK_CONTAINERS}
        if sub == "list-images":
            return {"ok": True, "images": sysinfo._MOCK_IMAGES}
        if sub == "compose-get":
            return {"ok": True, "content": "services:\n  nginx-proxy:\n    image: docker.io/library/nginx:1.27-alpine\n    ports:\n      - \"8080:80\"\n"}
        if sub in ("container", "pull", "image-rm", "compose-set", "compose-deploy", "compose-stop"):
            return {"ok": True, "message": f"（mock）{sub} 完成", "log_tail": "（mock）deploy log tail"}
        return {"ok": True}
    if cmd == "tailscale-ops":
        sub = rest[0] if rest else "status"
        if sub == "status":
            return {"ok": True, "installed": True, "running": True, "logged_in": True,
                    "account": "user@gmail.com", "ip": "100.64.12.34",
                    "hostname": "seedrou", "version": "1.84.3", "auth_url": ""}
        if sub == "login":
            return {"ok": True, "auth_url": "https://login.tailscale.com/a/0123456789abcdef",
                    "message": "（mock）请在浏览器打开授权链接"}
        return {"ok": True, "message": f"（mock）{sub} 完成"}
    if cmd == "upgrade-check":
        return {"ok": True, "up_to_date": False, "current": "0.1.0", "latest": "0.2.0", "message": "（mock）发现新版本"}
    if cmd in ("upgrade-download", "upgrade-apply", "backup-create", "backup-restore",
               "factory-reset", "passwd-set", "service-ctl", "apply-all"):
        return {"ok": True, "message": f"（mock）{cmd} 完成", "path": "/tmp/mock"}
    return {"ok": True}
