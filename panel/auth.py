#!/usr/bin/env python3
"""SeedRou 面板认证：会话签名（HMAC）、Cookie、CSRF、登录限速。

- 管理密码的校验在 seedrou-priv（root）内完成，面板只拿 {ok} 结果，不接触哈希。
- 会话令牌 = base64url(payload) + "." + HMAC-SHA256(会话密钥)；12 小时有效。
- 会话密钥从 seedrou-priv session-secret 获取（root 属主 secrets.json 内）。
"""
import base64
import hashlib
import hmac
import json
import time

MOCK = False
_secret_cache = None
_secret_ts = 0
SESSION_TTL = 7 * 24 * 3600   # 7 天：局域网面板 + HttpOnly cookie，频繁过期得不偿失
COOKIE = "sr_sess"

# 面板侧登录限速（按来源 IP，内存态；priv 侧还有全局兜底限速，双层防御 —— 审计 B-18）
_fails = {}
FAIL_LIMIT, LOCK_S = 5, 60

# 已登出令牌的撤销表（内存态，进程重启即清；无状态 HMAC 会话的补偿机制，
# 否则登出后旧 cookie 重放仍然有效直到过期）。条目 12h 自动过期（令牌最长寿命），
# 并设总量上限，避免长期运行缓慢增长（复审 N-08a）。
_revoked = {}
_REVOKE_TTL = 12 * 3600
_REVOKE_MAX = 4096


class AuthError(Exception):
    def __init__(self, status, code, message, hint=""):
        self.status, self.code, self.message, self.hint = status, code, message, hint


def init(mock=False):
    global MOCK
    MOCK = mock


def _secret():
    global _secret_cache, _secret_ts
    if MOCK:
        return b"mock-session-secret"
    now = time.time()
    if _secret_cache and now - _secret_ts < 60:  # 短缓存：改密轮换密钥后旧会话尽快失效
        return _secret_cache
    import subprocess
    out = subprocess.run(
        ["sudo", "-n", "/usr/local/lib/seedrou/seedrou-priv", "session-secret"],
        capture_output=True, text=True, timeout=15)
    data = json.loads(out.stdout or "{}")
    if not data.get("ok") or not data.get("secret"):
        raise AuthError(500, "NO_SECRET", "无法获取会话密钥", "请确认已运行 install.sh；journalctl -u seedrou-panel 查看详情")
    _secret_cache = data["secret"].encode()
    _secret_ts = now
    return _secret_cache


def _sign(payload: bytes) -> str:
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def make_token() -> str:
    payload = json.dumps({"u": "admin", "e": int(time.time()) + SESSION_TTL}).encode()
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"{b64}.{_sign(payload)}"


def verify_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception:
        return False
    if not hmac.compare_digest(_sign(payload), sig):
        return False
    try:
        data = json.loads(payload)
    except Exception:
        return False
    return data.get("e", 0) > time.time()


def _cookie_val(headers) -> str:
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def logged_in(headers) -> bool:
    tok = _cookie_val(headers)
    if not tok:
        return False
    h = hashlib.sha256(tok.encode()).hexdigest()
    if _revoked.get(h, 0) > time.time():
        return False
    return verify_token(tok)


def revoke(token: str):
    if not token:
        return
    now = time.time()
    _revoked[hashlib.sha256(token.encode()).hexdigest()] = now + _REVOKE_TTL
    if len(_revoked) > _REVOKE_MAX:
        expired = [k for k, v in _revoked.items() if v < now]
        for k in expired:
            _revoked.pop(k, None)
        while len(_revoked) > _REVOKE_MAX:
            _revoked.pop(next(iter(_revoked)), None)


def session_cookie(token: str) -> str:
    return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"


def clear_cookie() -> str:
    return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def csrf_ok(headers) -> bool:
    """变更请求：自定义头必带；Origin 存在时必须同源（防跨站）。"""
    if headers.get("X-SeedRou") != "1":
        return False
    origin = headers.get("Origin")
    if origin:
        try:
            from urllib.parse import urlsplit
            host = headers.get("Host") or ""
            return urlsplit(origin).netloc.lower() == host.lower()
        except Exception:
            return False
    return True


# ---------------- 登录 ----------------
def login_locked(client_ip: str):
    rec = _fails.get(client_ip or "unknown")
    if rec and rec.get("lock_until", 0) > time.time():
        return int(rec["lock_until"] - time.time()) + 1
    return 0


def login_fail(client_ip: str):
    rec = _fails.setdefault(client_ip or "unknown", {"fails": 0, "lock_until": 0})
    rec["fails"] += 1
    if rec["fails"] >= FAIL_LIMIT:
        rec["lock_until"] = time.time() + LOCK_S
        rec["fails"] = 0
    # 顺手清理过期项，避免长期运行内存增长
    now = time.time()
    for k in [k for k, v in _fails.items() if v.get("lock_until", 0) < now and v["fails"] == 0]:
        _fails.pop(k, None)


def login_ok(client_ip: str):
    _fails.pop(client_ip or "unknown", None)


def priv_verify_password(password: str):
    """调用 priv auth-verify；返回 (ok, err_dict|None)。密码只走 stdin。"""
    if MOCK:
        if password == "admin":
            return True, None
        return False, {"code": "AUTH_BAD", "message": "管理密码不正确",
                       "hint": "mock 模式密码为 admin"}
    import subprocess
    try:
        p = subprocess.run(
            ["sudo", "-n", "/usr/local/lib/seedrou/seedrou-priv", "auth-verify"],
            input=password + "\n", capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        raise AuthError(504, "TIMEOUT", "密码校验超时", "请稍后重试")
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        raise AuthError(500, "AUTH_INTERNAL", "密码校验服务异常", "请在系统上执行 seedrou-priv selftest 检查")
    return data.get("ok", False), (None if data.get("ok") else data)
