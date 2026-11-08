#!/usr/bin/env python3
"""SeedRou 面板 API 路由。

安全模型：
- 除 /api/session、/api/login 外全部要求有效会话（HttpOnly HMAC cookie）。
- 特权动作全部经 `sudo -n seedrou-priv <白名单子命令>`，面板永不直接写 /etc。
- 错误统一 {ok:false, code, message, hint}，message=原因、hint=下一步，中文可操作。
"""
import io
import json
import os
import re
import secrets as pysecrets
import subprocess
import sys
import tarfile
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
    if not (p.stdout or "").strip():
        # 无输出：把 stderr 尾部与退出码带给前端/日志（真机排障关键，2026-11 首装实测）
        err_tail = "; ".join((p.stderr or "").strip().splitlines()[-3:])
        raise ApiError(500, "PRIV_ERROR", "特权服务无输出",
                       f"exit={p.returncode} stderr: {err_tail[:300]}")
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        err_tail = "; ".join((p.stderr or "").strip().splitlines()[-3:])
        raise ApiError(500, "PRIV_MALFORMED", "特权服务返回异常",
                       f"stderr: {err_tail[:300]}")
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
        # 开发预览直接读仓库根的 VERSION，永不漂移（复审 R-06）
        try:
            return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")).read().strip()
        except OSError:
            return "0.0.0"
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
_status_cache = {"ts": 0.0, "data": None}


def handle(method, path, body: bytes, headers, client_ip=""):
    path = path.split("?", 1)[0]        # 带查询串的 /api/xxx 一律 404 属于埋雷（审计 3）
    route = path.rstrip("/") or "/"
    q = route[len("/api"):] if route.startswith("/api") else route

    # ---- 免登录 ----
    if q == "/session" and method == "GET":
        return 200, {"ok": True, "logged": auth.logged_in(headers),
                     "version": _version()}, None
    if q == "/login" and method == "POST":
        return do_login(body, headers, client_ip)

    # ---- 登录态 ----
    if not auth.logged_in(headers):
        raise ApiError(401, "UNAUTH", "登录已过期", "请重新登录")

    if method == "HEAD" and q == "/backup":
        # HEAD 会走与 GET 相同的处理器——/api/backup 每次 HEAD 都会 root 打包一次（审计 2）
        raise ApiError(405, "METHOD_NOT_ALLOWED", "HEAD 不支持", "扫描器请使用 GET")

    # HEAD 按 GET 语义查表（鉴权/405 正确），body 由 _send 吞掉
    lookup_method = "GET" if method == "HEAD" else method

    if lookup_method == "GET":
        table = {
            "/status": get_status, "/config": get_config, "/leases": get_leases,
            "/podman/status": lambda b, h: (200, priv("podman-ops", "status"), None),
            "/podman/containers": lambda b, h: (200, priv("podman-ops", "list-containers", timeout=60), None),
            "/podman/images": lambda b, h: (200, priv("podman-ops", "list-images", timeout=60), None),
            "/podman/compose": lambda b, h: (200, priv("podman-ops", "compose-get"), None),
            "/singbox": lambda b, h: (200, priv("singbox-ops", "status"), None),
            "/backup": get_backup,
        }
        fn = table.get(q)
        if fn:
            return fn(body, headers)
    elif lookup_method == "POST":
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
            "/podman/pull": post_podman_pull,
            "/podman/image-rm": post_podman_image_rm,
            "/podman/compose": post_compose,
            "/singbox/check": lambda b, h: (200, priv("singbox-ops", "check", timeout=90), None),
            "/singbox/dash": post_singbox_dash,
            "/singbox/ctl": post_singbox_ctl,
            "/singbox/autostart": post_singbox_autostart,
            "/singbox/install": post_singbox_install,
            "/singbox/upgrade": post_singbox_upgrade,
            "/singbox/sub": post_singbox_sub,
            "/singbox/schedule": post_singbox_schedule,
            "/singbox/uninstall": post_singbox_uninstall,
            "/version/check": lambda b, h: (200, priv("upgrade-check", timeout=90), None),
            "/upgrade/online": post_upgrade_online,
            "/upgrade": post_upgrade_upload,
            "/restore": post_restore,
            "/factory-reset": post_factory_reset,
        }
        fn = table.get(q)
        if fn:
            return fn(body, headers)

    raise ApiError(404, "NOT_FOUND", "接口不存在")


# ---------------- 登录/登出 ----------------
def do_login(body, headers, client_ip=""):
    locked = auth.login_locked(client_ip)
    if locked:
        return 423, {"ok": False, "code": "AUTH_LOCKED", "message": f"尝试次数过多，请 {locked} 秒后重试",
                     "hint": "密码不会因此失效；稍等即可再次尝试"}, None
    data = _json_body(body)
    password = str(data.get("password", ""))
    if not password:
        return 400, {"ok": False, "code": "AUTH_EMPTY", "message": "请输入管理密码"}, None
    ok, err = auth.priv_verify_password(password)
    if not ok:
        auth.login_fail(client_ip)
        err = err or {}
        return (423 if err.get("code") == "AUTH_LOCKED" else 401), {
            "ok": False, "code": err.get("code", "AUTH_BAD"),
            "message": err.get("message", "管理密码不正确"), "hint": err.get("hint", "")}, None
    auth.login_ok(client_ip)
    token = auth.make_token()
    return 200, {"ok": True}, {"Set-Cookie": auth.session_cookie(token)}


def do_logout(body, headers):
    # 服务端同时撤销该令牌：登出后旧 cookie 重放不再有效
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == auth.COOKIE:
            auth.revoke(v)
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
    # 3 秒缓存：前端 8s 轮询 + 每次内部 ~9 个子进程，缓存显著降载且无感（移交清单性能项）
    now = time.time()
    if now - _status_cache["ts"] < 3 and _status_cache["data"] is not None:
        st = _status_cache["data"]
    else:
        st = sysinfo.mock_status() if MOCK else priv("status", timeout=30)
        if not MOCK:
            st.setdefault("wan", {}).setdefault("dns", _wan_dns())
        _status_cache["ts"] = now
        _status_cache["data"] = st
    st["version"] = _version()  # mock/真实统一由 VERSION 文件决定（复审 R-06）
    stats = {
        "devices": st.get("leases_count"),
        "cpu": None if MOCK else sysinfo.cpu_pct(),
        "cpu_temp": None if MOCK else sysinfo.temp_cpu(),
        "mem": None if MOCK else sysinfo.mem_pct(),
        "disk_temp": st.get("disk_temp"),  # 由 priv status 以 root 采集（复审 N-05）
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
    # 载荷包含表格里的全部行：空值 = 用户清除了该备注，需要落盘删除（审计 B-10）
    clean = {}
    for k, v in notes.items():
        if not re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", str(k)) or not isinstance(v, str):
            continue
        if len(v.strip()) > 32:
            raise ApiError(400, "BAD_NOTE", "备注最长 32 字")
        if v.strip():
            clean[k.upper()] = v.strip()
    if MOCK:
        return 200, {"ok": True}, None
    sysinfo.save_notes(clean)  # save_notes 是整体替换：清除的备注自然消失
    return 200, {"ok": True, "message": "备注已保存"}, None


# ---------------- 安全中心 ----------------
def post_portfw(body, headers):
    data = _json_body(body)
    out = priv("config-set", "firewall",
               input_text=json.dumps({"portfw": data.get("list", [])}, ensure_ascii=False), timeout=300)
    return 200, {"ok": True, "message": out.get("message", "规则已保存")}, None


def post_password(body, headers):
    data = _json_body(body)
    try:
        out = priv("passwd-set",
                   input_text=json.dumps({"old": str(data.get("old", "")), "new": str(data.get("new", ""))}),
                   timeout=60)
    except ApiError as e:
        if e.code == "AUTH_BAD":
            # P2-1：AUTH_BAD 全局映射 401 会把改密页用户踢出会话（401 触发前端登出）。
            # 原密码错误是表单校验级错误，用 400 表达
            raise ApiError(400, "PW_OLD_BAD", "原密码不正确",
                           "请输入当前使用的管理密码；忘记密码请在控制台执行 seedrou-priv factory-reset")
        raise
    return 200, {"ok": True, "message": out.get("message", "密码已修改")}, None


# ---------------- 容器 ----------------
def _check_image(img):
    # B-07：镜像名此前完全不校验，前导 - 可注入 podman 参数
    img = str(img or "").strip()
    if not img or img.startswith("-") or len(img) > 256 \
            or not re.fullmatch(r"[A-Za-z0-9._/:%\-]+(\@sha256:[a-f0-9]+)?", img):
        raise ApiError(400, "BAD_IMAGE", "镜像名不合法", "示例：docker.io/library/nginx:1.27-alpine")
    return img


def post_podman_pull(body, headers):
    img = _check_image(_json_body(body).get("image"))
    return 200, priv("podman-ops", "pull", img, timeout=660), None


def post_podman_image_rm(body, headers):
    img = _check_image(_json_body(body).get("image"))
    return 200, priv("podman-ops", "image-rm", img, timeout=120), None


def post_podman_container(body, headers):
    data = _json_body(body)
    name = str(data.get("name", ""))[:64]
    action = str(data.get("action", ""))
    if not re.fullmatch(r"[\w.\-]+", name) or not name or name.startswith("-"):
        # B-07：前导 - 会让 --all 被当成 podman 的参数（podman stop -t 10 --all）
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


# ---------------- sing-box / 配置（Tailscale 自 0.1.32 起由 sing-box endpoint 托管） ----------------
def post_singbox_ctl(body, headers):
    data = _json_body(body)
    action = data.get("action", "")
    if action not in ("start", "stop", "restart"):
        raise ApiError(400, "BAD_ACTION", "不支持的 sing-box 操作")
    return 200, priv("singbox-ops", "ctl", action, timeout=120), None


def post_singbox_autostart(body, headers):
    data = _json_body(body)
    return 200, priv("singbox-ops", "autostart", "on" if data.get("on") else "off", timeout=60), None


def post_singbox_install(body, headers):
    data = _json_body(body)
    url = str(data.get("url", "")).strip()
    priv("singbox-ops", "install", timeout=960)
    if url:
        priv("singbox-ops", "sub-set", input_text=json.dumps({"url": url}), timeout=60)
        priv("singbox-ops", "sub-update", timeout=700)
        return 200, {"ok": True, "message": "sing-box 已安装，配置已更新并启动服务"}, None
    return 200, {"ok": True, "message": "sing-box 内核已安装；下一步请填写配置地址并更新配置"}, None


def post_factory_reset(body, headers):
    # B-06：与 singbox uninstall 同级护栏——前端有双确认，API 层也要 confirm
    if not _json_body(body).get("confirm"):
        raise ApiError(400, "CONFIRM", "缺少二次确认", "恢复出厂不可撤销，请先在弹窗确认")
    return 200, priv("factory-reset", timeout=300), None


def post_singbox_dash(body, headers):
    data = _json_body(body)
    out = priv("singbox-ops", "dash-set", input_text=json.dumps({"url": str(data.get("url", ""))}), timeout=60)
    return 200, {"ok": True, "message": out.get("message", "Dashboard 地址已更新")}, None


def post_singbox_upgrade(body, headers):
    priv("singbox-ops", "install", timeout=960)
    return 200, {"ok": True, "message": "内核已升级（配置与节点数据保留）"}, None


def post_singbox_sub(body, headers):
    data = _json_body(body)
    op = data.get("op", "")
    if op == "set":
        out = priv("singbox-ops", "sub-set", input_text=json.dumps({"url": str(data.get("url", ""))}), timeout=60)
        return 200, {"ok": True, "message": out.get("message", "配置地址已保存")}, None
    if op == "update":
        out = priv("singbox-ops", "sub-update", timeout=700)
        return 200, {"ok": True, "message": out.get("message", "配置已更新")}, None
    raise ApiError(400, "BAD_ACTION", "不支持的配置操作")


def post_singbox_schedule(body, headers):
    data = _json_body(body)
    t = str(data.get("time", "")).strip() or ("off" if data.get("off") else "")
    if t != "off" and not re.fullmatch(r"([0-9]|0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]", t):
        raise ApiError(400, "SB_SCHED_BAD", "时间格式应为 HH:MM（24 小时制）",
                       "示例：04:00、06:30；关闭自动更新请把时间清空后保存")
    out = priv("singbox-ops", "schedule", input_text=json.dumps({"time": t}), timeout=60)
    return 200, {"ok": True, "message": out.get("message", "自动更新时间已更新"), "time": out.get("time", "")}, None


def post_singbox_uninstall(body, headers):
    data = _json_body(body)
    if not data.get("confirm"):
        raise ApiError(400, "CONFIRM", "缺少二次确认", "卸载不可恢复，请先勾选确认")
    return 200, priv("singbox-ops", "uninstall", timeout=300), None


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
    if (headers.get("X-SeedRou-Confirm") or "") != "1":
        # B-06：恢复会整体覆盖配置，API 层要求确认头（前端上传时携带）
        raise ApiError(400, "CONFIRM", "缺少二次确认", "恢复将覆盖当前配置，请先在弹窗确认")
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
        # mock 也要产出真 tar.gz：前端按 .tar.gz 落盘，假 gzip 会得到损坏文件（2026-11 自测）
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            payload = b"SeedRou mock backup (preview build)\n"
            info = tarfile.TarInfo("seedrou-backup.txt")
            info.size = len(payload)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(payload))
        with open(out_path, "wb") as f:
            f.write(buf.getvalue())
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
    except OSError:
        raise ApiError(500, "BACKUP_FAIL", "备份文件读取失败", "请检查磁盘空间（df -h）")
    # 保留最近 10 份（生成即删会让轮转形同虚设，审计 C-08）
    return 200, data, {
        "Content-Type": "application/gzip",
        "Content-Disposition": f'attachment; filename="{name}"',
    }


# ---------------- mock 特权实现 ----------------
# mock：sing-box / 配置 / Tailscale endpoint 的固定场景（数值与设计稿一致）
_SBOX = {
    "installed": True, "active": True, "enabled": True, "version": "1.14.4",
    "uptime_s": 3 * 86400 + 14 * 3600,
    "url": "https://sub.example.com/link/9f3c8d2e", "url_set": True,
    "auto_on": True, "auto_time": "04:00",
    "update": {"ok": True, "ts": int(time.time()) - 180, "error": "", "trigger": "auto"},
    "endpoint": {"present": True, "hostname": "seedrou"},
    "dashboard": {"controller": "0.0.0.0:9090", "ui": True},
    "dash_url": "",
}


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
        cfg = sysinfo._MOCK_CONFIG  # 直接改模块态：mock 保存可持久（审计 C-09）
        data = json.loads(input_text or "{}")
        # mock 也过一遍真实校验（审计 6：此前 mock 下校验分支全部不可达）；
        # validate.py 读 /etc/seedrou/config.json 取 cur，缺失时以 None 安全跳过交叉校验
        import copy as _copy
        import os as _os
        import tempfile as _tempfile
        vpy = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "priv", "validate.py")
        if _os.path.exists(vpy):
            # B-09：把当前 mock 配置快照传给 validate，交叉校验（pppoe×v6、池越界等）在 mock 下可达
            _tmp = _tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(_copy.deepcopy(cfg), _tmp, ensure_ascii=False)
            _tmp.close()
            p = subprocess.run([sys.executable, vpy, section], input=json.dumps(data, ensure_ascii=False),
                               capture_output=True, text=True, timeout=20,
                               env={**_os.environ, "SEEDROU_VALIDATE_CONFIG": _tmp.name})
            try:
                _os.unlink(_tmp.name)
            except OSError:
                pass
            if p.returncode != 0:
                try:
                    err = json.loads(p.stdout or "{}")
                except json.JSONDecodeError:
                    err = {}
                raise ApiError(400, err.get("code", "VALIDATE"),
                               err.get("message", "校验失败"), err.get("hint", ""))
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
    if cmd == "singbox-ops":
        sub = rest[0] if rest else "status"
        sb = _SBOX
        if sub == "check":
            return {"ok": True, "up_to_date": False, "current": "1.14.4", "latest": "1.14.6",
                    "message": "（mock）发现新版本 v1.14.6（当前 v1.14.4）"}
        if sub == "status":
            from urllib.parse import urlparse as _up
            u = sb["url"]
            pm = _up(u)
            path = pm.path or ""
            if len(path) > 14:
                path = path[:8] + "…" + path[-4:]
            masked = (pm.scheme + "://" + pm.netloc + path) if u else ""
            upd = dict(sb["update"])
            if upd.get("ts"):
                upd["ts"] = int(time.time()) if not upd["ok"] else upd["ts"]
            return {"ok": True, "installed": sb["installed"], "sb": True,
                    "active": sb["active"], "enabled": sb["enabled"], "version": sb["version"],
                    "uptime_s": sb.get("uptime_s"),
                    "url_set": sb["url_set"], "url_masked": masked,
                    "autoupdate": {"on": sb["auto_on"], "time": sb["auto_time"]},
                    "update": dict(sb["update"]),
                    "endpoint": dict(sb["endpoint"]),
                    "dashboard": dict(sb["dashboard"], custom=sb.get("dash_url", ""))}
        if sub == "ctl":
            act = rest[1] if len(rest) > 1 else ""
            if act == "stop":
                sb["active"] = False
            elif act in ("start", "restart"):
                sb["active"] = True
            return {"ok": True, "message": f"（mock）sing-box {act} 完成"}
        if sub == "autostart":
            sb["enabled"] = (rest[1] if len(rest) > 1 else "off") == "on"
            return {"ok": True, "message": "（mock）开机自启已切换"}
        if sub == "install":
            sb["installed"] = True
            sb["active"] = True
            sb["enabled"] = True
            return {"ok": True, "message": "（mock）sing-box 内核已就绪", "tail": "mock install tail"}
        if sub == "sub-set":
            sb["url"] = str((json.loads(input_text or "{}")).get("url") or sb["url"]).strip() or sb["url"]
            sb["url_set"] = True
            return {"ok": True, "message": "（mock）配置地址已保存"}
        if sub == "sub-update":
            sb["update"] = {"ok": True, "ts": int(time.time()) - 3, "error": "", "trigger": "manual"}
            return {"ok": True, "message": "（mock）配置已更新，sing-box 已重启加载新配置"}
        if sub == "dash-set":
            sb["dash_url"] = str((json.loads(input_text or "{}")).get("url") or "").strip()
            return {"ok": True, "message": "（mock）Dashboard 地址已更新"}
        if sub == "schedule":
            t = str((json.loads(input_text or "{}")).get("time") or "").strip()
            if t == "off" or not t:
                sb["auto_on"] = False
                return {"ok": True, "message": "（mock）自动更新已关闭"}
            sb["auto_on"] = True
            sb["auto_time"] = t
            return {"ok": True, "message": f"（mock）配置自动更新已设为每天 {t}", "time": t}
        if sub == "uninstall":
            sb.update({"installed": False, "active": False, "enabled": False,
                       "url_set": False, "url": "",
                       "endpoint": {"present": False, "hostname": ""},
                       "update": {"ok": None, "ts": None, "error": "", "trigger": ""}})
            return {"ok": True, "message": "（mock）sing-box 已卸载"}
        return {"ok": True, "message": f"（mock）{sub} 完成"}
    if cmd == "upgrade-check":
        return {"ok": True, "up_to_date": False, "current": "0.1.0", "latest": "0.2.0", "message": "（mock）发现新版本"}
    if cmd in ("upgrade-download", "upgrade-apply", "backup-create", "backup-restore",
               "factory-reset", "passwd-set", "service-ctl", "apply-all"):
        return {"ok": True, "message": f"（mock）{cmd} 完成", "path": "/tmp/mock"}
    return {"ok": True}
