#!/usr/bin/env python3
"""备份 / 恢复 / 恢复出厂（v0.1.86 / P2-6）。

从 `dert-priv` 的 `do_backup_create` / `do_backup_restore` / `do_factory_reset`
抽出。这三条路径的共同点是**不可逆**：恢复写的是 /etc/dert，出厂重置会清掉
宽带密码并重建配置。所以这次改写只做"搬运 + 显式化"，不顺手改语义。

保持不变的几件事（都是踩过坑才有的）：

  1. **备份不含 secrets.json**。密码哈希与会话密钥不随备份流转，恢复后按
     secrets.json 的真实内容重算 `wan.has_password`（复审 N-04）——否则会出现
     "界面显示已设密码、实际拨号无凭据"的假状态。
  2. **解压前后各一道校验**：成员白名单（backupcheck.py，先 normpath 归一）
     + 拒绝 `[lh]` 链接条目（防包内软链指向 /etc 写穿，审计 5.3）。
  3. **落盘前跑 schema 预检**。"能 json.load" ≠ "能用"：裁剪过的配置会让收敛
     在 netgen 里 traceback，或静默生成 `wan.mode` 为空、WAN 口无地址的配置。
  4. **tar 仍走 CLI**，不用 tarfile 重写。`--no-same-owner --no-overwrite-dir`
     的语义、gzip 边界行为都由 tar 保证；自己实现等于重造一个安全关键组件。

与 bash 版的唯一差异：`apply-all` 改为**子进程调用**（`_apply_all()`），
不再同进程函数调用。锁由外层持有，子进程用 `_DERT_LOCK_HELD=1` 跳过重取
（`lib.sh:acquire_lock` 本就为嵌套调用留了这个缝）。

用法：
    dert-priv backup-create  <out.tar.gz>
    dert-priv backup-restore <in.tar.gz>
    dert-priv factory-reset
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import applyops
import privlib
from privlib import CONFIG_JSON, RUN, SECRETS_JSON, VAR, die, ok_out

# 生产环境为 "/"；测试可用 DERT_FSROOT 指向临时根。未设时与 bash 版逐字符等价。
ROOT = os.environ.get("DERT_FSROOT", "") or "/"
PRIV_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_ALLOW_DIR = "etc/dert"
NOTES = VAR + "/notes.json"
_TAR_CAPS = {}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _fs(path):
    """把绝对路径映射到数据根下（DERT_FSROOT 未设时原样返回）。"""
    if ROOT == "/":
        return path
    return ROOT.rstrip("/") + path


def _tar_list(src, verbose=False):
    """跑 tar 列成员。失败返回 None（区分"空包"与"读不出来"）。"""
    cmd = ["tar", "-tvzf" if verbose else "-tzf", src]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _tar_supports(flag):
    """探测 tar 是否支持某个长选项。

    生产是 Debian GNU tar，两个安全选项都在。开发机（macOS bsdtar）不认
    `--no-overwrite-dir`，直接跑会整条恢复失败——所以按能力探测，缺哪个少用
    哪个，而不是把选项写死或整体降级。结果缓存，只探一次。
    """
    if flag not in _TAR_CAPS:
        try:
            r = subprocess.run(["tar", flag, "-tf", os.devnull],
                               capture_output=True, text=True, timeout=10)
            _TAR_CAPS[flag] = "not supported" not in (r.stderr or "").lower()
        except Exception:
            _TAR_CAPS[flag] = False
    return _TAR_CAPS[flag]


def _extract(stage, src):
    """解压到 stage。GNU tar 上带齐两道保护；不支持的平台少用一条。"""
    flags = [f for f in ("--no-same-owner", "--no-overwrite-dir") if _tar_supports(f)]
    try:
        return subprocess.run(["tar", "-C", stage] + flags + ["-xzf", src],
                              capture_output=True, timeout=300)
    except Exception:
        return None


def _why(out):
    """从 apply-all 的输出里抠出可读原因（它是 JSON；解析不了就取最后一行）。"""
    for line in reversed((out or "").strip().split("\n")):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict):
            return ("%s：%s" % (d.get("code", ""), d.get("message", ""))).strip("：")[:240]
    tail = (out or "").strip().split("\n")
    return tail[-1][:240] if tail and tail[-1] else ""


def _reapply():
    """用当前 config.json 重新生成并部署（回滚后重放用）。

    失败不抛：调用方已经在报错路径上，再抛只会把真正的原因盖掉。
    """
    os.makedirs(RUN, exist_ok=True)
    stage = tempfile.mkdtemp(prefix="stage.", dir=RUN)
    man = os.path.join(stage, "manifest.json")
    ok, _out, _err = applyops.generate(stage)
    if ok:
        try:
            applyops.apply_stage_files(stage, man)
            applyops.cleanup_stale(man)
            applyops.run_actions(applyops.ACTIONS)
        except SystemExit:
            ok = False
    shutil.rmtree(stage, ignore_errors=True)
    return ok


def _rollback(tag):
    """把配置退回快照并重放。返回 True 表示回滚成功。"""
    if not privlib.restore(tag):
        return False
    return _reapply()


def _apply_all():
    """跑一次幂等收敛（对应 bash 的 `do_apply_all 2>&1`）。

    返回 (ok, 输出)。失败时调用方把输出原样透传给面板——收敛失败的细节
    （netgen 的 KeyError / 服务起不来）必须让用户看到，不能吞成一句"失败"。
    """
    env = dict(os.environ)
    env["_DERT_LOCK_HELD"] = "1"  # 外层已持锁，子进程跳过重取
    try:
        r = subprocess.run([os.path.join(PRIV_DIR, "dert-priv"), "apply-all"],
                           capture_output=True, text=True, timeout=300, env=env)
    except Exception as e:
        return False, json.dumps({"ok": False, "code": "APPLY_FAIL",
                                  "message": f"收敛未能执行：{e}", "hint": ""},
                                 ensure_ascii=False)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out


def _snapshot(tag):
    """配置快照——实现在 privlib（与 configops/applyops 共用一份）。"""
    return privlib.snapshot(tag)


def backup_create(out_path):
    privlib.require_root()
    if not out_path:
        die("USAGE", "缺少输出路径")
    subprocess.run(
        ["tar", "-C", ROOT, "-czf", out_path,
         "--exclude=var/lib/dert/backups", "--exclude=var/lib/dert/lastgood",
         "--exclude=etc/dert/secrets.json",
         "etc/dert", "var/lib/dert/notes.json"],
        capture_output=True)
    if not (os.path.isfile(out_path) and os.path.getsize(out_path) > 0):
        die("BACKUP_FAIL", "备份打包失败", "请检查磁盘空间（df -h）")
    # 备份不含 secrets（已剔除）：640 归面板用户，仅供下载
    try:
        shutil.chown(out_path, "dert", "dert")
    except Exception:
        pass
    os.chmod(out_path, 0o640)
    ok_out(path=out_path)


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------
def _verdict(src):
    """成员白名单判定：OK / SECRETS / BAD（backupcheck 先 normpath 归一）。

    读不出来、或包内一个成员都没有，都算 BAD —— 与 bash 版一致：空包过了白名单
    也过不了解压，不如在这里就给出同一个错误码。
    """
    names = _tar_list(src)
    if names is None or not names.strip():
        return "BAD"
    sys.path.insert(0, PRIV_DIR)
    import backupcheck
    return backupcheck.check(names.split("\n"))


def _has_link_entry(src):
    """包内是否含符号/硬链接。tar 详细列表首字符为 l/h 即命中。"""
    verbose = _tar_list(src, verbose=True)
    if not verbose:
        return False
    for line in verbose.split("\n"):
        if line[:1] in ("l", "h"):
            return True
    return False


def _schema_ok(cfg_path):
    """结构预检：只查结构不查语义（存量 /25 掩码由收敛期迁移负责）。"""
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(PRIV_DIR, "netgen.py"),
             "--schema", "--config", cfg_path],
            capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, f"netgen schema 检查未能执行：{e}"
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def _deploy_tree(stage):
    """把 stage/etc/dert 下的文件原子落盘；已有文件沿用原 mode，新文件 600。"""
    base = os.path.join(stage, BACKUP_ALLOW_DIR)
    if not os.path.isdir(base):
        return
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, stage).replace(os.sep, "/")
            dest = _fs("/" + rel)
            try:
                mode = os.stat(dest).st_mode & 0o777
            except OSError:
                mode = 0o600
            with open(src, "rb") as f:
                privlib.write_bytes(dest, f.read(), mode)


def _sync_has_password():
    """备份不含 secrets：按 secrets.json 真实内容重算 has_password（N-04）。"""
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        with open(SECRETS_JSON, encoding="utf-8") as f:
            sec = json.load(f)
    except Exception:
        return
    cfg.setdefault("wan", {})["has_password"] = bool(sec.get("pppoe_password"))
    privlib.write_atomic(CONFIG_JSON, json.dumps(cfg, ensure_ascii=False, indent=1), 0o644)


def _pppoe_password_missing():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            wan = json.load(f).get("wan", {})
    except Exception:
        return False
    return wan.get("mode") == "pppoe" and not wan.get("has_password")


def backup_restore(src):
    privlib.require_root()
    if not src:
        die("USAGE", "缺少备份文件路径")
    if not os.path.isfile(src):
        die("NO_FILE", "备份文件不存在")

    # 1) 成员白名单（先归一，`./` 与 `//` 变体同样拦得住）
    verdict = _verdict(src)
    if verdict == "SECRETS":
        die("BACKUP_SECRETS", "备份包含 secrets.json，已拒绝恢复",
            "secrets（密码哈希/会话密钥）不随备份流转；如需重置密码请在控制台执行 dert-priv")
    if verdict != "OK":
        die("BACKUP_BAD", "备份文件内容不符合 DebRT 备份格式",
            "请确认上传的是本系统生成的备份包（.tar.gz）")
    # 2) 拒绝链接条目：防"备份内软链指向 /etc"穿越写盘（审计 5.3）
    if _has_link_entry(src):
        die("BACKUP_BAD", "备份包内含链接条目，已拒绝",
            "为防路径穿越，备份包只允许普通文件与目录")

    tag = "r%d" % int(time.time())
    _snapshot(tag)
    stage = os.path.join(RUN, "restore." + os.urandom(4).hex())
    os.makedirs(stage, exist_ok=True)
    try:
        # --no-same-owner：root 解压不保留包内属主；--no-overwrite-dir 防目录覆盖类穿越
        r = _extract(stage, src)
        if r is None or r.returncode != 0:
            die("BACKUP_BAD", "备份文件解压失败（文件可能损坏）")
        cfg_path = os.path.join(stage, "etc/dert/config.json")
        try:
            with open(cfg_path, encoding="utf-8") as f:
                json.load(f)
        except Exception:
            die("BACKUP_BAD", "备份中的配置无法解析")
        ok, out = _schema_ok(cfg_path)
        if not ok:
            print(out)
            sys.exit(1)
        _deploy_tree(stage)
        notes = os.path.join(stage, "var/lib/dert/notes.json")
        if os.path.isfile(notes):
            with open(notes, "rb") as f:
                privlib.write_bytes(NOTES, f.read(), 0o644)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    try:
        os.chmod(SECRETS_JSON, 0o600)
    except OSError:
        pass
    _sync_has_password()
    warn = ""
    if _pppoe_password_missing():
        warn = ("注意：备份不含宽带密码，PPPoE 拨号尚未可用——"
                "请在「网络设置 → 上网设置」重新填写宽带密码并保存")

    ok, out = _apply_all()
    if not ok:
        # 配置已经换成备份里的那份，但没能生效。只报错不回滚的话，下次开机会
        # 拿这份"能解析但跑不起来"的配置去收敛，等于把设备留在半坏状态。
        why = _why(out)
        if _rollback(tag):
            die("APPLY_ROLLED_BACK", "从备份恢复后收敛失败，已回滚到恢复前的配置",
                why or "请检查备份包与当前硬件是否匹配（网口名、WAN 方式）")
        die("APPLY_FAIL", "从备份恢复后收敛失败，且回滚未成功",
            why or "请从 /var/lib/dert/lastgood 手工恢复，或重新运行 install.sh")
    ok_out(message="配置已从备份恢复并生效" + ("。" + warn if warn else ""))


# ---------------------------------------------------------------------------
# 恢复出厂
# ---------------------------------------------------------------------------
DEFAULT_LAN = "192.168.10.1"
PORT_RE = re.compile(r"^(eth\d+|en[psox]\S*)$")


def default_ports(sysfs=None):
    """发现网口：eth*/en* 排除 ppp/tun/br/veth。第一个当 WAN，其余 LAN。"""
    base = sysfs or _fs("/sys/class/net")
    try:
        names = sorted(n for n in os.listdir(base) if PORT_RE.match(n))
    except OSError:
        names = []
    if not names:
        return [{"name": "eth0", "wan": True, "lan": False},
                {"name": "eth1", "wan": False, "lan": True}]
    return [{"name": n, "wan": i == 0, "lan": i > 0} for i, n in enumerate(names)]


def default_config(ports, upgrade_source=""):
    return {
        "version": 1,
        "ports": ports,
        "wan": {"mode": "dhcp", "username": "", "dns_mode": "auto", "dns": [],
                "ip": "", "mask": "", "gw": "", "has_password": False},
        "ipv6": {"enabled": False, "mode": "native", "dns_mode": "auto", "dns": [],
                 "addr": "", "gw": "", "prefix": ""},
        "lan": {"ip": DEFAULT_LAN, "mask": "255.255.255.0"},
        "dhcp": {"enabled": True, "pool_start": 100, "pool_end": 200,
                 "lease": 120, "dns1": "", "dns2": "", "gw": ""},
        "static": [],
        "qos": {"enabled": False, "down": 0, "up": 0},
        "firewall": {"wan_input_drop": True, "portfw": []},
        "upgrade": {"source": upgrade_source},
    }


def _drop_pppoe_password():
    """宽带密码属于路由配置：一并清除；管理密码保留。"""
    try:
        with open(SECRETS_JSON, encoding="utf-8") as f:
            sec = json.load(f)
    except Exception:
        return
    sec.pop("pppoe_password", None)
    privlib.write_atomic(SECRETS_JSON,
                         json.dumps(sec, ensure_ascii=False, indent=1), 0o600)


def factory_reset():
    privlib.require_root()
    tag = "f%d" % int(time.time())
    _snapshot(tag)
    prebak = f"{VAR}/lastgood/pre-factory-{int(time.time())}.tar.gz"
    os.makedirs(os.path.dirname(prebak), exist_ok=True)
    subprocess.run(["tar", "-C", ROOT, "-czf", prebak,
                    "--exclude=etc/dert/secrets.json", "etc/dert"],
                   capture_output=True)
    try:
        os.chmod(prebak, 0o600)
    except OSError:
        pass

    src = ""
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            src = json.load(f).get("upgrade", {}).get("source", "")
    except Exception:
        pass
    cfg = default_config(default_ports(), src)  # 升级源保留
    privlib.write_atomic(CONFIG_JSON, json.dumps(cfg, ensure_ascii=False, indent=1), 0o644)
    _drop_pppoe_password()
    privlib.run(["systemctl", "disable", "--quiet", "dert-pppoe.service"], timeout=30)

    ok, out = _apply_all()
    if not ok:
        # 与备份恢复同理：配置已重置但没生效，必须退回快照并重放，
        # 否则设备停在"出厂配置 + 没生效"的状态，下次开机才炸。
        why = _why(out)
        if _rollback(tag):
            die("APPLY_ROLLED_BACK", "恢复出厂后收敛失败，已回滚到出厂前的配置",
                why or "请检查硬件是否满足出厂配置（至少一个可用网口）")
        die("APPLY_FAIL", "恢复出厂后收敛失败，且回滚未成功",
            why or "请从 /var/lib/dert/lastgood 手工恢复，或重新运行 install.sh")
    ok_out(message="已恢复出厂设置（LAN %s，DHCP 池 .100–.200）" % DEFAULT_LAN)


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    arg = argv[2] if len(argv) > 2 else ""
    if action == "create":
        backup_create(arg)
    elif action == "restore":
        backup_restore(arg)
    elif action == "factory":
        factory_reset()
    else:
        die("USAGE", "未知动作：%s" % (action or "（空）"),
            "合法动作：create / restore / factory")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
