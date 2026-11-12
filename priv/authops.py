#!/usr/bin/env python3
"""认证域 —— 从 `dert-priv` 抽出（v0.1.83 / P2-3）。

两段逻辑原本就是纯 Python，只是嵌在 bash heredoc 里，所以这是**字面搬迁**：
限速、锁定、记录格式容错、改密轮换会话密钥的语义一字未改。

抽出的直接好处：这些**安全语义**现在能被行为测试覆盖（此前只有源码字符串断言）：
  · 失败 20 次锁 300 秒（全局兜底，阈值放宽以免被用来把管理员锁在门外）
  · 锁过期即清零计数（否则跨天累积成"错一次立刻锁 5 分钟"）
  · 密码记录损坏要与"密码错误"区分（AUTH_RECORD_BAD），便于排查
  · 改密成功必须轮换 session_secret（被盗 cookie 立即失效）
  · 状态文件一律 tmp + os.replace（断电不会打碎 auth-state.json）

用法：
    authops.py verify      从 stdin 读一行密码，输出判定 JSON
    authops.py passwd-set  从 stdin 读 JSON {"old","new"}
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import privlib as P  # noqa: E402
import pwhash  # noqa: E402

# 全局兜底限速（按来源 IP 的限速在面板层做）。阈值放宽，避免单计数器被用来
# 把管理员锁在门外（审计 B-18）。
AUTH_MAX_FAILS = 20
AUTH_LOCK_SECONDS = 300

# 原密码暴破限速（持有会话的攻击者此前可无限尝试）——5 次锁 5 分钟
PW_MAX_FAILS = 5


def _out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(code)


def verify():
    pw = sys.stdin.read(4096).rstrip("\n")
    if not pw:
        _out({"ok": False, "code": "AUTH_EMPTY", "message": "请输入管理密码",
              "hint": ""}, 1)

    state_path = P.VAR + "/auth-state.json"
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        # P3-1：状态损坏兜底——限速计数可丢，登录不能丢
        state = {"fails": 0, "lock_until": 0}

    now = int(time.time())
    if state.get("lock_until", 0) > now:
        _out({"ok": False, "code": "AUTH_LOCKED",
              "message": f"尝试次数过多，请 {state['lock_until'] - now + 1} 秒后重试",
              "hint": "密码不会因此失效；稍等即可再次尝试"}, 0)
    elif state.get("lock_until", 0):
        # P2-5：锁已过期即归零——否则 fails 跨天累积，演变成"错一次立刻锁 5 分钟"
        state["fails"] = 0
        state["lock_until"] = 0

    try:
        with open(P.ETC + "/secrets.json", encoding="utf-8") as f:
            rec = json.load(f)["admin_pbkdf2"]
        ok = pwhash.verify(pw, rec)
    except ValueError:
        # 记录格式坏：与"密码不正确"区分开，CI/排查一眼定位（六审建议）
        _out({"ok": False, "code": "AUTH_RECORD_BAD",
              "message": "密码记录无法解析（secrets.json 可能损坏或来自旧版本）",
              "hint": "在系统上执行：rm /etc/dert/secrets.json && sudo bash install.sh 重新生成密码"}, 1)
    except Exception:
        ok = False

    if ok:
        state = {"fails": 0, "lock_until": 0}
        result = {"ok": True}
    else:
        state["fails"] = int(state.get("fails", 0)) + 1
        if state["fails"] >= AUTH_MAX_FAILS:
            state["lock_until"] = now + AUTH_LOCK_SECONDS
            result = {"ok": False, "code": "AUTH_LOCKED",
                      "message": "连续多次密码错误，已临时锁定 5 分钟",
                      "hint": "若忘记密码：长按机身 Reset 键 8 秒恢复出厂，或在控制台执行 dert-priv factory-reset"}
        else:
            result = {"ok": False, "code": "AUTH_BAD",
                      "message": "管理密码不正确", "hint": "请检查密码后重试"}

    # P3-1：全项目 /etc 写入都是 tmp+os.replace，唯独这里曾直写——断电写一半会
    # 打碎 auth-state.json，下次 json.loads 抛异常 → 无输出 → 登录不可用
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, state_path)

    _out(result, 0 if ok else 1)


def passwd_set():
    try:
        body = json.loads(sys.stdin.read() or "{}")
    except Exception:
        _out({"ok": False, "code": "BAD_BODY", "message": "请求体不是合法 JSON",
              "hint": ""}, 1)

    old, new = body.get("old", ""), body.get("new", "")
    try:
        with open(P.ETC + "/secrets.json", encoding="utf-8") as f:
            secrets = json.load(f)
    except Exception as e:
        _out({"ok": False, "code": "NO_SECRETS", "message": f"读取 secrets.json 失败：{e}",
              "hint": "请重新运行 install.sh"}, 1)

    fails_path = P.VAR + "/passwd-fails.json"

    def pf_load():
        try:
            with open(fails_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"fails": 0, "lock_until": 0}

    def pf_save(st):
        tmp = fails_path + ".tmp"
        os.makedirs(os.path.dirname(fails_path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, fails_path)

    now = int(time.time())
    st = pf_load()
    if st.get("lock_until", 0) > now:
        _out({"ok": False, "code": "AUTH_LOCKED",
              "message": f"尝试次数过多，请 {st['lock_until'] - now + 1} 秒后重试",
              "hint": "密码不会因此失效"}, 1)

    def check(pw, rec):
        try:
            return pwhash.verify(pw, rec)
        except Exception:
            return False

    if not check(old, secrets.get("admin_pbkdf2", "")):
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] >= PW_MAX_FAILS:
            st["lock_until"] = now + 300
            st["fails"] = 0
            pf_save(st)
            _out({"ok": False, "code": "AUTH_LOCKED",
                  "message": "原密码连续错误次数过多，已临时锁定 5 分钟",
                  "hint": "密码不会因此失效"}, 1)
        pf_save(st)
        _out({"ok": False, "code": "AUTH_BAD", "message": "原密码不正确",
              "hint": "请输入当前使用的管理密码"}, 1)

    # C-2 收尾：原密码验证通过即清零暴破计数——否则陈旧计数无限期累积，
    # 零散输错累计 5 次（哪怕中间成功改过密）也会误触锁
    pf_save({"fails": 0, "lock_until": 0})

    if not (8 <= len(new) <= 32):
        _out({"ok": False, "code": "PW_LEN", "message": "新密码需为 8–32 位",
              "hint": ""}, 1)

    secrets["admin_pbkdf2"] = pwhash.hash_new(new)
    # 轮换会话密钥：改密后所有旧会话（含可能被盗的 cookie）立即失效（审计 B-17）
    secrets["session_secret"] = os.urandom(32).hex()

    tmp = P.ETC + "/.secrets.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(secrets, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, P.ETC + "/secrets.json")
    _out({"ok": True, "message": "管理密码已更新"}, 0)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else ""
    if action == "verify":
        return verify()
    if action == "passwd-set":
        return passwd_set()
    _out({"ok": False, "code": "USAGE", "message": f"未知操作：{action}", "hint": ""}, 1)


if __name__ == "__main__":
    main()
