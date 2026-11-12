"""行为测试公共设施。

原则：**不需要 root、不碰真机、不读 /etc**。
所有断言针对「生成的产物内容」或「CLI 的输出」，不针对源码文本——
源码文本断言会在重构时误报（改结构但行为没变），这类断言留给
`.github/scripts/test_guards.py` 里的"架构约束"部分。
"""
import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIV = os.path.join(ROOT, "priv")
PY = sys.executable


# --------------------------------------------------------------------------
# 配置样本
# --------------------------------------------------------------------------

def base_config():
    """仓库自带的默认配置（config/config.json）。"""
    with open(os.path.join(ROOT, "config", "config.json"), encoding="utf-8") as f:
        return json.load(f)


def pppoe_config(**over):
    """PPPoE + IPv6 Native（国内家宽最常见形态）。"""
    cfg = base_config()
    cfg["wan"].update(mode="pppoe", username="13800000000")
    cfg["ipv6"].update(enabled=True, mode="native")
    cfg["ports"] = [
        {"name": "enp1s0", "wan": False, "lan": True},
        {"name": "enp2s0", "wan": False, "lan": True},
        {"name": "enp4s0", "wan": True, "lan": False},
    ]
    cfg.update(over)
    return cfg


def dhcp_config(**over):
    """DHCP 上网 + IPv6 关闭。"""
    cfg = base_config()
    cfg["wan"].update(mode="dhcp")
    cfg["ports"] = [
        {"name": "enp1s0", "wan": True, "lan": False},
        {"name": "enp2s0", "wan": False, "lan": True},
    ]
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------
# validate.py 黑盒调用
# --------------------------------------------------------------------------

def run_validate(section, data, current=None):
    """跑 validate.py 的 CLI，返回 (rc, 输出 dict)。

    validate.py 的错误通过 stdout 的 JSON 表达（fail() 打印后 sys.exit(1)），
    所以这里可以直接断言 code，不需要捕获异常。
    """
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as td:
        if current is not None:
            p = os.path.join(td, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(current, f)
            env["DERT_VALIDATE_CONFIG"] = p
        else:
            env["DERT_VALIDATE_CONFIG"] = os.path.join(td, "absent.json")
        r = subprocess.run(
            [PY, os.path.join(PRIV, "validate.py"), section],
            input=json.dumps(data), capture_output=True, text=True,
            env=env, timeout=30,
        )
    out = (r.stdout or "").strip()
    if not out:
        return r.returncode, {}
    try:
        return r.returncode, json.loads(out)
    except json.JSONDecodeError:
        return r.returncode, {"_raw": out}


# --------------------------------------------------------------------------
# netgen.py 调用
# --------------------------------------------------------------------------

def load_netgen():
    """把 priv/ 加进 sys.path 后导入 netgen（纯函数，导入无副作用）。"""
    if PRIV not in sys.path:
        sys.path.insert(0, PRIV)
    import netgen  # noqa: E402
    return netgen


def run_netgen_full(cfg, secrets=None):
    """跑完整的 netgen main()，返回 manifest dict。

    走真实入口（含 schema_check / cross_validate / 落 stage 目录），
    比单独调生成函数更接近生产路径。
    """
    netgen = load_netgen()
    with tempfile.TemporaryDirectory() as td:
        cpath = os.path.join(td, "config.json")
        spath = os.path.join(td, "secrets.json")
        stage = os.path.join(td, "stage")
        os.makedirs(stage)
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(secrets or {}, f)

        old_c, old_s, old_argv = netgen.CONFIG_PATH, netgen.SECRETS_PATH, sys.argv
        netgen.CONFIG_PATH, netgen.SECRETS_PATH = cpath, spath
        sys.argv = ["netgen.py", "--stage", stage]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                netgen.main()
        finally:
            netgen.CONFIG_PATH, netgen.SECRETS_PATH = old_c, old_s
            sys.argv = old_argv
        return json.loads(buf.getvalue())


def run_netgen_expect_fail(cfg, secrets=None):
    """跑 netgen main()，返回它拒绝配置时的 {code, message, hint}。

    netgen 的 fail() 是「打印 JSON 到 stdout + sys.exit(1)」，所以这里捕获
    SystemExit 再解析 stdout。用于断言收敛侧的字段级安全校验。
    """
    netgen = load_netgen()
    with tempfile.TemporaryDirectory() as td:
        cpath = os.path.join(td, "config.json")
        spath = os.path.join(td, "secrets.json")
        stage = os.path.join(td, "stage")
        os.makedirs(stage)
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(secrets or {}, f)

        old_c, old_s, old_argv = netgen.CONFIG_PATH, netgen.SECRETS_PATH, sys.argv
        netgen.CONFIG_PATH, netgen.SECRETS_PATH = cpath, spath
        sys.argv = ["netgen.py", "--stage", stage]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                netgen.main()
        except SystemExit:
            pass
        finally:
            netgen.CONFIG_PATH, netgen.SECRETS_PATH = old_c, old_s
            sys.argv = old_argv
        return json.loads(buf.getvalue())


def manifest_text(man):
    """把 manifest 的 content_b64 解成 {相对路径: 文本}。"""
    out = {}
    for f in man["files"]:
        out[f["path"]] = base64.b64decode(f["content_b64"]).decode("utf-8")
    return out


def manifest_modes(man):
    return {f["path"]: f["mode"] for f in man["files"]}


# --------------------------------------------------------------------------
# validate.cross_validate 直接调用（针对已规范化的结果做跨段校验）
# --------------------------------------------------------------------------

def load_validate():
    if PRIV not in sys.path:
        sys.path.insert(0, PRIV)
    import validate  # noqa: E402
    return validate


def load_tarcheck():
    """升级包成员校验（priv/tarcheck.py）。"""
    if PRIV not in sys.path:
        sys.path.insert(0, PRIV)
    import tarcheck  # noqa: E402
    return tarcheck


def load_cleanup():
    """陈旧生成物清理逻辑（priv/cleanup.py），支持 --root 注入以便纯内存测试。"""
    if PRIV not in sys.path:
        sys.path.insert(0, PRIV)
    import cleanup  # noqa: E402
    return cleanup


def cross_error(section, result, cur):
    """调用 cross_validate，返回被拒时的错误码；通过则返回 None。"""
    v = load_validate()
    v.ERR.update(code="", message="", hint="")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            v.cross_validate(section, result, cur)
    except SystemExit:
        return v.ERR.get("code") or "_NO_CODE"
    return None
