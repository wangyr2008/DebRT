#!/usr/bin/env python3
"""升级通道（v0.1.89 / P2-9）：check / download / apply。

这是**唯一会替换自身**的路径：`upgrade-apply` 把 `/usr/local/lib/dert` 整个换掉，
包括正在运行的这份代码。所以这里的顺序不是风格问题，每一步都在防一种具体的
坏结局：

  1. **先验来源再验结构**（签名 → 成员白名单 → 版本）。sha256 只能证明"没被改坏"，
     不能证明"是谁发的"——镜像被劫持时可以同时替换包和哈希。
  2. **成员清单先取到变量再喂校验器**。不能 `tar -tzf | tarcheck`：tar 对含 `..`
     的成员会往 stderr 打 "Removing leading ..."，合并进管道后那行会被当成成员名，
     于是拒绝原因变成 tar 的警告原文（真机 2026-11-12 实测踩到）。
  3. **暂存区必须在被替换目录之外**：放里面会随 mv 一起改名，第二步 mv 必 ENOENT。
  4. **换目录用 mv 而不是就地覆盖**：`mv old → dert.prev.$$` 成功后才 `mv stg → dert`，
     失败可原路退回（UPG_SWAP 分支）。
  5. **布局对齐**：install.sh 是扁平布局，升级包保持 `priv/` 子目录，换目录后必须
     把 `priv/*` 拷回根，否则 sudo 调用的 `/usr/local/lib/dert/dert-priv` 直接消失。
  6. **升级后立即收敛**：生成物是 netgen 的产物，等最长 6h 的 timer 才重生成，
     期间旧键/旧语法还在跑（真机实录：v0.1.44 升级后 IPv6"升级了也没修好"）。
  7. **延迟重启面板**：先把本次响应送达客户端，再 3 秒后重启。
"""

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import privlib
from privlib import CONFIG_JSON, RUN, VAR, die, ok_out

HERE = os.path.dirname(os.path.abspath(__file__))
# 代码/安装根：生产固定 /usr/local/lib/dert。DERT_LIB 只为测试缝存在（与 privlib 一致），
# 未设时与生产逐字符等价。
LIB = os.environ.get("DERT_LIB", "/usr/local/lib/dert")
SHARE = LIB + "/share"
PROXIES = ("", "https://gh-proxy.org/", "https://gh-proxy.com/")
MAX_PKG = 268435456          # 256MB，远大于实际包体
PANEL_UNIT = "dert-panel.service"


def _sh(cmd, timeout=60, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except Exception:
        return None


def _log(msg):
    _sh(["logger", "-t", "dert-upgrade", msg], timeout=10)


def _cfg():
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def cur_version():
    for p in (SHARE + "/VERSION", LIB + "/VERSION"):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return "0.0.0"


def parse_ver(v):
    m = re.match(r"v?([0-9]+(?:\.[0-9]+)*)", (v or "").strip())
    return [int(x) for x in m.group(1).split(".")] if m else [0]


def ver_ge(a, b):
    x, y = parse_ver(a), parse_ver(b)
    n = max(len(x), len(y))
    return x + [0] * (n - len(x)) >= y + [0] * (n - len(y))


# ---------------------------------------------------------------------------
# 取内容（带镜像回退）
# ---------------------------------------------------------------------------
def fetch_body(url, timeout=30):
    for prefix in PROXIES:
        r = _sh(["curl", "-fsSL", "--connect-timeout", "8", "--max-time", str(timeout),
                 prefix + url], timeout=timeout + 15)
        if r is not None and r.returncode == 0:
            return r.stdout
    return None


def fetch_file(url, out, timeout=300):
    for prefix in PROXIES:
        r = _sh(["curl", "-fsSL", "--connect-timeout", "10", "--max-time", str(timeout),
                 "--max-filesize", str(MAX_PKG), prefix + url, "-o", out],
                timeout=timeout + 20)
        if r is not None and r.returncode == 0:
            return True
    return False


def fetch_small(url, out, timeout=30):
    """取小文件（.sha256 / .sig）；空文件算失败。"""
    for prefix in PROXIES:
        r = _sh(["curl", "-fsSL", "--connect-timeout", "10", "--max-time", str(timeout),
                 prefix + url, "-o", out], timeout=timeout + 20)
        if r is not None and r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
            return True
    return False


def github_latest_ver(repo):
    for prefix in PROXIES:
        r = _sh(["curl", "-sIL", "--connect-timeout", "8", "--max-time", "30", "-o", os.devnull,
                 "-w", "%{url_effective}",
                 prefix + "https://github.com/%s/releases/latest" % repo], timeout=45)
        if r is None:
            continue
        m = re.search(r"/tag/v?([0-9][0-9.]*)", r.stdout or "")
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def check():
    src = (_cfg().get("upgrade") or {}).get("source", "")
    if not src:
        die("UPG_NO_SOURCE", "尚未配置升级源",
            "请在「备份与升级」页填写 GitHub 仓库（如 yourname/dert）并保存")
    cur = cur_version()
    if "://" in src:
        body = fetch_body(src)
        if body is None:
            die("UPG_NET", "无法访问升级源清单",
                "请检查升级源 URL 与网络连通性；也可稍后重试")
        try:
            d = json.loads(body)
            latest = d.get("version") or d.get("tag_name") or ""
        except Exception:
            latest = ""
        if not latest:
            die("UPG_MANIFEST", "升级源清单格式不正确", "清单需为 JSON 且含 version 字段")
    else:
        latest = github_latest_ver(src)
        if not latest:
            die("UPG_NET", "无法获取 GitHub 最新版本",
                "请检查仓库名（yourname/dert）与网络；GitHub 可能限流，可稍后重试")
    latest = latest.lstrip("v")
    if not ver_ge(latest, cur) or latest == cur:
        ok_out(up_to_date=True, current=cur, latest=latest,
               message="已是最新版本 V%s" % cur)
    else:
        ok_out(up_to_date=False, current=cur, latest=latest, message="发现新版本")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------
def _manifest_urls(body):
    """从清单里取 (url, sha256)。值原样传递，不经 shell 求值（注入面为零）。"""
    try:
        d = json.loads(body)
    except Exception:
        return "", ""
    if not isinstance(d, dict):
        return "", ""

    def clean(v):
        return v.replace("\r", "").replace("\n", "") if isinstance(v, str) else ""

    return clean(d.get("url")), clean(d.get("sha256"))


def download(ver):
    if not ver:
        die("USAGE", "缺少版本号")
    src = (_cfg().get("upgrade") or {}).get("source", "")
    ver = ver.lstrip("v")
    os.makedirs(VAR + "/staging", exist_ok=True)
    out = VAR + "/staging/pkg.tar.gz"
    for p in (out, out + ".sha256", out + ".sig"):
        try:
            os.unlink(p)
        except OSError:
            pass

    base = "https://github.com"
    if "://" in src:
        body = fetch_body(src)
        if body is None:
            die("UPG_NET", "无法访问升级源")
        asset, sha256 = _manifest_urls(body)
        if not asset:
            die("UPG_MANIFEST", "清单缺少 url 字段")
        # P3-26：只接受 https://。curl 会照做 file:// 等协议，被篡改的清单
        # 可借此把任意本地文件当成"升级包"喂进后续解压/校验流程。
        if not asset.startswith("https://"):
            die("UPG_MANIFEST", "升级源地址必须以 https:// 开头",
                "清单里的 url 字段格式不正确（只允许 https）；当前值前 40 字符：%s"
                % asset[:40])
        ok = fetch_file(asset, out)
        if ok:
            if sha256:
                privlib.write_atomic(out + ".sha256", "%s  %s\n" % (sha256, os.path.basename(out)))
            else:
                fetch_small(asset + ".sha256", out + ".sha256")
            if not (os.path.isfile(out + ".sha256") and os.path.getsize(out + ".sha256") > 0):
                os.unlink(out)
                die("UPG_MANIFEST", "清单式升级源未提供 sha256，拒绝安装",
                    "请在清单 JSON 中加入 sha256 字段，或提供 <url>.sha256 文件")
            # 签名可选：取不到不算失败，是否强制由 upgrade.require_signature 决定
            fetch_small(asset + ".sig", out + ".sig")
    else:
        rel = "%s/%s/releases/download/v%s/dert-%s.tar.gz" % (base, src, ver, ver)
        ok = fetch_file(rel, out)
        if ok:
            fetch_small(rel + ".sha256", out + ".sha256")
            fetch_small(rel + ".sig", out + ".sig")

    if not ok or not (os.path.isfile(out) and os.path.getsize(out) > 0):
        die("UPG_NET", "下载升级包失败",
            "请检查升级源与网络；也可在页面手动上传升级包")
    # P2-4：.sha256 下载失败曾静默跳过校验——升级源支持第三方镜像，代理正是篡改风险点
    if not (os.path.isfile(out + ".sha256") and os.path.getsize(out + ".sha256") > 0):
        os.unlink(out)
        die("UPG_SHA", "升级源未提供 sha256 校验文件",
            "为防篡改，缺少 .sha256 时不安装；请确认发布页包含 dert-<版本>.tar.gz.sha256")
    # .sha256 记录的是原始资产名，不能直接 -c；提取期望哈希与实际文件比对
    try:
        # errors="replace"：损坏/伪造的 .sha256 可能是二进制，读崩了也要走到"不匹配"
        with open(out + ".sha256", encoding="utf-8", errors="replace") as f:
            want = (f.read().split() or [""])[0]
    except OSError:
        want = ""
    # 用 hashlib 而不是 sha256sum：少一个外部命令依赖，也不受 PATH 影响
    h = hashlib.sha256()
    try:
        with open(out, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        got = h.hexdigest()
    except OSError:
        got = ""
    if not want or want != got:
        die("UPG_SHA", "升级包校验失败（sha256 不匹配）",
            "下载可能被篡改或损坏，请重试；连续失败请检查升级源")
    ok_out(path=out)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def _verify_signature(pkg):
    """返回 '已验签' / '未签名'；失败直接 die。"""
    pub = LIB + "/dert-signing.pub.pem"
    if os.path.isfile(pkg + ".sig") and os.path.getsize(pkg + ".sig") > 0:
        if not os.path.isfile(pub):
            die("UPG_SIG", "升级包带签名，但本机缺少验签公钥",
                "请确认 %s 存在；可从仓库 signing/dert-signing.pub.pem 复制" % pub)
        r = _sh(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", pub, "-rawin",
                 "-in", pkg, "-sigfile", pkg + ".sig"], timeout=60)
        if r is None or r.returncode != 0:
            _log("签名校验失败，拒绝安装：%s" % pkg)
            die("UPG_SIG", "升级包签名校验失败",
                "包可能被篡改，或不是用 DebRT 官方密钥签发的；已拒绝安装（未做任何改动）")
        _log("签名校验通过：%s" % pkg)
        return "已验签"
    # 没带签名：默认只警告（存量发布尚无签名，一刀切会锁死升级通道）
    need = bool((_cfg().get("upgrade") or {}).get("require_signature"))
    if need:
        die("UPG_SIG", "升级源未提供签名，已拒绝",
            "当前配置要求必须验签（upgrade.require_signature=true）；请使用带 .sig 的官方发布")
    _log("升级包未带签名，仅做 sha256 校验（可通过 upgrade.require_signature 强制要求）：%s" % pkg)
    return "未签名"


def _tar_list(pkg, verbose=False):
    r = _sh(["tar", "-tvzf" if verbose else "-tzf", pkg], timeout=180)
    return (r.stdout or "") if (r is not None and r.returncode == 0) else None


def _has_link(pkg):
    out = _tar_list(pkg, verbose=True)
    return bool(out) and any(ln[:1] in ("l", "h") for ln in out.split("\n"))


def _tar_flags():
    flags = []
    for f in ("--no-same-owner", "--no-overwrite-dir"):
        r = _sh(["tar", f, "-tf", os.devnull], timeout=10)
        if r is not None and "not supported" not in (r.stderr or "").lower():
            flags.append(f)
    return flags


def _check_structure(pkg, names):
    """成员白名单 + 必备目录。names 是已经取到变量的成员清单。"""
    sys.path.insert(0, HERE)
    import tarcheck
    ok, reason = tarcheck.check(names)
    if not ok:
        die("UPG_BAD", "升级包含不认识的条目",
            "请确认上传的是 DebRT 官方发布的 .tar.gz 升级包" +
            ("。具体：%s" % reason if reason else ""))
    if _has_link(pkg):
        die("UPG_BAD", "升级包含链接条目，已拒绝", "为防路径穿越，升级包只允许普通文件与目录")
    for need, msg in (("VERSION", "升级包缺少 VERSION 文件"),
                      ("panel/", "升级包缺少 panel/ 目录"),
                      ("priv/", "升级包缺少 priv/ 目录")):
        if not any(ln == need or ln.startswith(need) for ln in names):
            die("UPG_BAD", msg, "请确认上传的是 DebRT 官方发布的 .tar.gz 升级包")


def _pkg_version(pkg):
    r = _sh(["tar", "-xOzf", pkg, "VERSION"], timeout=60)
    return ((r.stdout or "").strip() if r else "")


def _syntax_check(stg):
    for f in glob.glob(stg + "/priv/*"):
        if not os.path.isfile(f):
            continue
        if f.endswith(".py"):
            r = _sh([sys.executable, "-m", "py_compile", f], timeout=60)
        else:
            r = _sh(["bash", "-n", f], timeout=60)
        if r is None or r.returncode != 0:
            shutil.rmtree(stg, ignore_errors=True)
            kind = "Python 脚本" if f.endswith(".py") else "脚本语法"
            die("UPG_BAD", "升级包内%s异常：%s" % (kind, os.path.basename(f)))
    r = _sh([sys.executable, "-m", "py_compile"] + glob.glob(stg + "/panel/*.py"), timeout=120)
    if r is None or r.returncode != 0:
        shutil.rmtree(stg, ignore_errors=True)
        die("UPG_BAD", "升级包内面板程序异常（py_compile 失败）")
    for need in ("panel/static/index.html", "panel/static/app.js", "panel/static/app.css",
                 "panel/static/icons.js", "panel/static/favicon.ico"):
        if not os.path.isfile(os.path.join(stg, need)):
            shutil.rmtree(stg, ignore_errors=True)
            die("UPG_BAD", "升级包缺少 %s" % need)


def _install(src, dst, mode=None, optional=True):
    """对应 `install -m MODE SRC DST`：失败不抛（bash 侧全是 || true）。"""
    try:
        shutil.copyfile(src, dst)
        if mode is not None:
            os.chmod(dst, mode)
        return True
    except OSError:
        if not optional:
            raise
        return False


def _mkdir(path, mode=0o755):
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)
    except OSError:
        pass


def _sync_etc_hooks():
    """/etc 侧钩子/单元同步（A-04 / R-01 / P3-02 / P3-18 同族）。

    升级通道漏装时面板版本号已是最新，排查极误导——所以这里一律**覆盖写**，
    不判断"是否已存在"。
    """
    net = LIB + "/network"
    if not os.path.isdir(net):
        return
    d = "/etc/networkd-dispatcher/routable.d"
    for name in ("70-dert-dns", "80-dert-cake", "90-dert-lan-prefix"):
        _install(net + "/dispatcher/" + name, d + "/" + name, 0o755)
    _install(net + "/dispatcher/70-dert-dns", "/etc/networkd-dispatcher/no-carrier.d/70-dert-dns", 0o755)
    # R-01：PPPoE 热插拔钩子（install.sh/uninstall.sh 都带，升级通道曾漏掉）
    _mkdir("/etc/networkd-dispatcher/carrier.d")
    for dst in ("/etc/networkd-dispatcher/carrier.d/75-dert-pppoe-hotplug",
                d + "/75-dert-pppoe-hotplug"):
        _install(net + "/dispatcher/75-dert-pppoe-hotplug", dst, 0o755)
    _install(net + "/ppp/ip-up.d/dert", "/etc/ppp/ip-up.d/dert", 0o755)
    _install(net + "/ppp/ip-down.d/dert", "/etc/ppp/ip-down.d/dert", 0o755)
    _install(net + "/sysctl/70-dert.conf", "/etc/sysctl.d/70-dert.conf", 0o644)
    _install(net + "/modules/dert.conf", "/etc/modules-load.d/dert.conf", 0o644)
    _install(net + "/logrotate/dert", "/etc/logrotate.d/dert", 0o644)
    # P3-02：PPPoE 拨号单元在 network/systemd/ 下，不在 systemd/ 的 glob 里
    _install(net + "/systemd/dert-pppoe.service", "/etc/systemd/system/dert-pppoe.service", 0o644)
    # v0.1.57：DNS 分流 drop-in（覆盖写，不判断是否存在）
    r = _sh(["systemctl", "list-unit-files", "sing-box.service"], timeout=30)
    if r is not None and r.returncode == 0:
        _mkdir("/etc/systemd/system/sing-box.service.d")
        try:
            with open("/etc/systemd/system/sing-box.service.d/20-dert-dns-split.conf",
                      "w", encoding="utf-8") as f:
                f.write("# 由 DebRT 安装/收敛自动维护：DNS 按 MAC 分流（见 COEXISTENCE.md §DNS 分流）\n"
                        "# 启动前生成 00-dert-dns.json；启动后派生 nft 表；停止时删表回落 dnsmasq\n"
                        "[Service]\n"
                        "ExecStartPre=/usr/local/lib/dert/dns-split-prepare\n"
                        "ExecStartPost=+/usr/local/lib/dert/dns-split-sync\n"
                        "ExecStopPost=+/usr/local/lib/dert/dns-split-teardown\n")
        except OSError:
            pass
        _sh(["systemctl", "daemon-reload"], timeout=60)
    # sudoers：先语法检查再落盘（visudo 拒绝就保留旧的，不能把 sudo 打坏）
    if os.path.isfile(LIB + "/sudoers/dert"):
        tmp = "/etc/sudoers.d/dert.upgrade-tmp"
        if _install(LIB + "/sudoers/dert", tmp, 0o440):
            r = _sh(["visudo", "-c", "-f", tmp], timeout=30)
            if r is not None and r.returncode == 0:
                os.replace(tmp, "/etc/sudoers.d/dert")
            else:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    _sh(["sysctl", "--system"], timeout=120)


def _sync_units():
    if not os.path.isdir(LIB + "/systemd"):
        return
    _mkdir("/etc/systemd/system/dnsmasq.service.d")
    _mkdir("/etc/systemd/system/sing-box.service.d")
    for pat in ("/*.service", "/*.timer"):
        for uf in glob.glob(LIB + "/systemd" + pat):
            _install(uf, "/etc/systemd/system/" + os.path.basename(uf), 0o644)
    for sub in ("dnsmasq.service.d", "sing-box.service.d"):
        for uf in glob.glob(LIB + "/systemd/" + sub + "/*"):
            _install(uf, "/etc/systemd/system/%s/%s" % (sub, os.path.basename(uf)), 0o644)
    _sh(["systemctl", "enable", "dert-panel.service", "dert-dns-sync.service",
         "dert-converge.timer"], timeout=120)


def _delayed_restart(units):
    """先把响应送回客户端，3 秒后再重启（systemd-run 优先，退化到 setsid）。"""
    cmd = ["systemctl", "restart"] + units
    r = _sh(["systemd-run", "--on-active=3", "--quiet"] + cmd, timeout=30)
    if r is not None and r.returncode == 0:
        return
    try:
        subprocess.Popen(["setsid", "bash", "-c", "sleep 3; " + " ".join(cmd)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def apply(pkg):
    privlib.require_root()
    if not pkg:
        die("USAGE", "缺少升级包路径")
    if not os.path.isfile(pkg):
        die("NO_FILE", "升级包不存在")

    # 0) 来源真实性
    _verify_signature(pkg)

    # 1) 包结构：成员清单先取到变量，再喂校验器（不能把 tar 的 stderr 合进管道）
    names = _tar_list(pkg)
    if names is None:
        die("UPG_BAD", "升级包无法读取（损坏或不是 tar.gz）", "请重新下载或重新上传升级包")
    _check_structure(pkg, [ln for ln in names.split("\n") if ln])

    # 2) 版本（允许同版本重装，拒绝降级）
    newver = _pkg_version(pkg)
    if not newver:
        die("UPG_BAD", "升级包缺少 VERSION 文件")
    cur = cur_version()
    if not ver_ge(newver, cur):
        die("UPG_DOWNGRADE", "不能从 V%s 降级到 V%s" % (cur, newver),
            "如需回退旧版本，请先恢复出厂前的备份并手动放置旧版包")

    # 3) 解压到暂存区（必须在被替换目录之外）+ 自检
    stg = "/usr/local/lib/.dert.upgrade.%d" % os.getpid()
    shutil.rmtree(stg, ignore_errors=True)
    os.makedirs(stg, exist_ok=True)
    r = _sh(["tar", "-C", stg] + _tar_flags() + ["-xzf", pkg], timeout=300)
    if r is None or r.returncode != 0:
        shutil.rmtree(stg, ignore_errors=True)
        die("UPG_BAD", "升级包解压失败（文件损坏）")
    for root, dirs, _files in os.walk(stg):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
    for pyc in glob.glob(stg + "/**/*.pyc", recursive=True):
        try:
            os.unlink(pyc)
        except OSError:
            pass
    _syntax_check(stg)

    # 4) 备份当前版本 → 原子换目录
    # L3：同版本重装时目标已存在，cp -a 会嵌套成 app-<ver>/dert/，手工回退路径失效
    bak = "%s/lastgood/app-%s" % (VAR, cur_version())
    shutil.rmtree(bak, ignore_errors=True)
    try:
        shutil.copytree(LIB, bak, symlinks=True)
    except Exception:
        pass
    old = "/usr/local/lib/dert.prev.%d" % os.getpid()
    try:
        os.rename(LIB, old)
        os.rename(stg, LIB)
    except OSError:
        try:
            os.rename(old, LIB)
        except OSError:
            pass
        shutil.rmtree(stg, ignore_errors=True)
        die("UPG_SWAP", "升级替换失败，已保留原版本")

    # 布局对齐：install.sh 是扁平布局，升级包保持 priv/ 子目录——
    # 不拷回根，sudo 调用的 /usr/local/lib/dert/dert-priv 会直接消失
    for f in glob.glob(LIB + "/priv/*"):
        if os.path.isfile(f):
            _install(f, LIB + "/" + os.path.basename(f))
    # 机制级：根目录里带 shebang 的一律 755——与包结构（扁平/子目录）无关，
    # 也不依赖白名单
    for f in glob.glob(LIB + "/*"):
        if not os.path.isfile(f):
            continue
        try:
            with open(f, "rb") as fh:
                if fh.read(2) == b"#!":
                    os.chmod(f, 0o755)
        except OSError:
            pass
    for name in ("wait-wan", "wait-dns-upstream", "singbox-cleanup", "qdisc-apply"):
        p = LIB + "/" + name
        if os.path.isfile(p):
            try:
                os.chmod(p, 0o755)
            except OSError:
                pass
    # sb.sh 随升级保持最新：旧 sb 无更新状态记录，面板"上次更新"会一直空
    if os.path.isfile(LIB + "/singbox/sb.sh"):
        _install(LIB + "/singbox/sb.sh", "/usr/local/bin/sb", 0o755)

    _sync_etc_hooks()

    # PPPoE 看门狗：pppd 对"接口被外部删除"不敏感，服务 active 但 ppp0 消失时不自愈
    if os.path.isfile(LIB + "/systemd/dert-pppoe-watchdog.timer"):
        _install(LIB + "/systemd/dert-pppoe-watchdog.service",
                 "/etc/systemd/system/dert-pppoe-watchdog.service", 0o644)
        _install(LIB + "/systemd/dert-pppoe-watchdog.timer",
                 "/etc/systemd/system/dert-pppoe-watchdog.timer", 0o644)
        _sh(["systemctl", "daemon-reload"], timeout=60)
        _sh(["systemctl", "enable", "--now", "dert-pppoe-watchdog.timer"], timeout=60)
        # 周期改过（1min → 15min）必须重启才生效：enable --now 对已在运行的 timer 是空操作
        _sh(["systemctl", "restart", "dert-pppoe-watchdog.timer"], timeout=60)

    # 网络事件分发（v0.1.67）：覆盖写 + enable，不判断"是否已存在"
    if os.path.isfile(LIB + "/systemd/dert-netwatch.service"):
        _install(LIB + "/systemd/dert-netwatch.service",
                 "/etc/systemd/system/dert-netwatch.service", 0o644)
        _sh(["systemctl", "daemon-reload"], timeout=60)
        _sh(["systemctl", "enable", "dert-netwatch.service"], timeout=60)
        _sh(["systemctl", "restart", "dert-netwatch.service"], timeout=60)

    for f in glob.glob(LIB + "/*.py") + [LIB + "/lib.sh"]:
        if os.path.isfile(f):
            try:
                os.chmod(f, 0o644)
            except OSError:
                pass
    for root, dirs, _files in os.walk(LIB):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                dirs.remove(d)
    # 扁平化后 priv/ 子目录是冗余副本，删掉避免"哪份生效"的歧义
    shutil.rmtree(LIB + "/priv", ignore_errors=True)
    _sh(["chown", "-R", "root:root", LIB], timeout=120)

    _sync_units()
    _sh(["systemctl", "daemon-reload"], timeout=60)
    shutil.rmtree(old, ignore_errors=True)
    # VERSION 同时落包根与 share/（目录整体替换后 share/ 会消失，必须重建再拷贝）
    os.makedirs(SHARE, exist_ok=True)
    _install(LIB + "/VERSION", SHARE + "/VERSION", 0o644)

    # 升级后自检并落日志（升级此前只验证面板能起）
    r = _sh([LIB + "/dert-priv", "selftest"], timeout=180)
    try:
        with open("/var/log/dert-upgrade-selftest.log", "a", encoding="utf-8") as f:
            f.write((r.stdout or "") + (r.stderr or ""))
    except OSError:
        pass
    if r is None or r.returncode != 0:
        _log("selftest 有失败项，详见 /var/log/dert-upgrade-selftest.log")

    # 升级后立即收敛：生成物是 netgen 的产物，等最长 6h 的 timer 才重生成，
    # 期间旧键/旧语法还在跑（真机实录：v0.1.44 升级后 IPv6"升级了也没修好"）
    # _DERT_LOCK_HELD=1：本进程持有 apply.lock，子进程重开 fd9 再 flock -n 必然 BUSY
    smoke = os.path.isfile(RUN + "/smoke-mode")
    if not smoke:
        env = dict(os.environ)
        env["_DERT_LOCK_HELD"] = "1"
        try:
            r = subprocess.run([LIB + "/dert-priv", "apply-all", "--convergent"],
                               capture_output=True, text=True, timeout=600, env=env)
            with open("/var/log/dert-upgrade-selftest.log", "a", encoding="utf-8") as f:
                f.write((r.stdout or "") + (r.stderr or ""))
            if r.returncode != 0:
                _log("升级后收敛有报错，详见 /var/log/dert-upgrade-selftest.log")
        except Exception:
            _log("升级后收敛未能执行，详见 /var/log/dert-upgrade-selftest.log")

    # 5) 延迟重启（先让本次响应送达客户端）
    _sh(["systemctl", "daemon-reload"], timeout=60)
    units = [PANEL_UNIT] if smoke else [PANEL_UNIT, "dert-converge.service"]
    _delayed_restart(units)
    ok_out(message="已升级到 V%s，面板即将重启" % newver)


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    arg = argv[2] if len(argv) > 2 else ""
    if action == "check":
        check()
    elif action == "download":
        download(arg)
    elif action == "apply":
        apply(arg)
    else:
        die("USAGE", "未知动作：%s" % (action or "（空）"), "合法动作：check / download / apply")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
