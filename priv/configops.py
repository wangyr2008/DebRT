#!/usr/bin/env python3
"""配置写入流水线（v0.1.88 / P2-8）。

`config-set` 是面板上每一次"保存"的后端：校验 → 快照 → 合并 → 落盘 →
生成 → 预检 → 部署 → 重启 → 验证 → 失败回滚。204 行 bash 里有十几条
"为什么这么写"的判断，这次是**逐条搬**，不是重写语义。

必须保留的判断（每条都对应一次事故）：

  1. **先落盘再生成**。netgen 读固定路径的 config.json，不先写盘就会用上一版
     配置生成（复审 v0.1.2 P-01：改了不生效，且"看起来保存成功"）。
  2. **`pre_wan` 在写盘前采样**，语义是"改动前 WAN 是否可用"（审计 B-03）。
     首次配 PPPoE 时它是 no，但此时恰恰最需要验证——所以 `section == wan`
     时无条件验证。
  3. **PPPoE 只在必要时重拨**：模式变、账号变、重绑网口、PPPoE 下改 IPv6、
     或本次填了新密码。改个 DNS 就断线重连是不可接受的。
  4. **失败必须回滚并重放旧配置**，再把原因如实报出来；PPPoE 失败还要从
     journald 里区分"认证被拒"和"服务器无响应"——这两种的处理动作完全不同。
  5. 密码只进 `secrets.json`，不进 config.json、不进 argv（审计 B-15）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import applyops
import privlib
from privlib import CONFIG_JSON, RUN, SECRETS_JSON, die, ok_out

PLANS = {
    # v0.1.88：bind 补上 pppoe。此前这里没有 pppoe，而下面又有
    # 「bind + PPPoE → pppoe_needed」的判断——结果是**判了却没法执行**：
    # 重绑 WAN 口后 /etc/ppp/peers/dert 里的 nic-<口> 已改，pppd 仍握旧口
    # （pppd call dert 只在启动时读 peer 文件）。重拨由 pppoe_needed 把关，
    # 非 PPPoE 模式下会被移除，不会多一次重启。
    "bind": "networkd pppoe dnsmasq nft cake",
    "wan": "networkd pppoe dnsmasq nft cake",
    "ipv6": "networkd pppoe dnsmasq nft",   # PPPoE 下开/关 v6 需重拨协商 IPv6CP
    "lan": "networkd dnsmasq nft cake",
    "dhcp": "dnsmasq",
    "static": "dnsmasq",
    "qos": "cake",
    "firewall": "nft",
    "upgrade": "",
}


def plan_for_section(section):
    return PLANS.get(section, "networkd dnsmasq nft cake")


def _read_cfg(strict=False):
    """读 config.json。strict 用于合并（读不出来就报 MERGE，不能凭空造一份）。"""
    try:
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None if strict else {}


def wan_route_ok():
    """主表默认路由是否落在 WAN 口上（排除 tun 等）。"""
    cfg = _read_cfg()
    wans = {p["name"] for p in cfg.get("ports", []) if p.get("wan")}
    if cfg.get("wan", {}).get("mode") == "pppoe":
        wans.add("ppp0")
    r = applyops._sh(["ip", "-j", "route", "show", "default"], timeout=10)
    if r is None or r.returncode != 0:
        return False
    try:
        routes = json.loads(r.stdout or "[]")
    except Exception:
        return False
    return any(rt.get("dev") in wans for rt in routes)


def wait_wan(seconds):
    waited = 0
    while waited < seconds:
        if wan_route_ok():
            return True
        time.sleep(2)
        waited += 2
    return False


def snapshot(tag):
    """配置快照——实现在 privlib（三处调用共用一份，见那里的注释）。"""
    return privlib.snapshot(tag)


def restore(tag):
    return privlib.restore(tag)


def normalize(section, body):
    """跑 validate.py。返回 (ok, norm 或 错误 JSON 文本)。"""
    env = dict(os.environ)
    env["DERT_VALIDATE_CONFIG"] = CONFIG_JSON   # 生产与默认路径相同；测试缝跟随 FSROOT
    try:
        r = subprocess.run([sys.executable, os.path.join(applyops.LIB, "validate.py"), section],
                           input=body, capture_output=True, text=True, timeout=60, env=env)
    except Exception as e:
        return False, json.dumps({"ok": False, "code": "VALIDATE",
                                  "message": "校验器未能执行：%s" % e, "hint": ""},
                                 ensure_ascii=False)
    if r.returncode != 0:
        return False, (r.stdout or "").strip()
    try:
        return True, json.loads(r.stdout)
    except Exception:
        return False, json.dumps({"ok": False, "code": "VALIDATE",
                                  "message": "校验器输出无法解析", "hint": ""},
                                 ensure_ascii=False)


def merge(section, norm, raw):
    """把规范化结果合进 config（密码只进 secrets）。返回 (cfg, secrets) 或 None。"""
    cfg = _read_cfg(strict=True)
    if cfg is None:
        return None
    try:
        with open(SECRETS_JSON, encoding="utf-8") as f:
            secrets = json.load(f)
    except Exception:
        secrets = {}
    data = norm["data"]
    if section == "bind":
        cfg["ports"] = data
    elif section == "wan":
        pwd = data.pop("password", None)
        for k in ("mode", "username", "dns_mode", "dns", "ip", "mask", "gw"):
            cfg.setdefault("wan", {})[k] = data[k]
        if pwd:
            secrets["pppoe_password"] = pwd
            cfg["wan"]["has_password"] = True
    elif section == "ipv6":
        cfg["ipv6"] = data
    elif section == "lan":
        cfg["lan"] = data
    elif section == "dhcp":
        cfg["dhcp"] = data
    elif section == "static":
        cfg["static"] = data
    elif section == "qos":
        cfg["qos"] = data
    elif section == "firewall":
        # 只改请求里出现的键：面板可能只提交 portfw 或只提交 wan_input_drop。
        # 两处都要 setdefault——老配置里可能没有 firewall 段，直接下标会 KeyError，
        # 而 KeyError 抛出去就是 traceback，面板只会看到"特权服务返回异常"。
        fw = cfg.setdefault("firewall", {})
        if "wan_input_drop" in raw:
            fw["wan_input_drop"] = data["wan_input_drop"]
        if "portfw" in raw:
            fw["portfw"] = data["portfw"]
    elif section == "upgrade":
        cfg["upgrade"] = data
    else:
        return None
    return cfg, secrets


def _dump(cfg):
    return json.dumps(cfg, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def config_set(section):
    if not section:
        die("USAGE", "缺少配置段名称")
    privlib.require_root()
    os.makedirs(RUN, exist_ok=True)
    body = sys.stdin.read()

    # 1) 校验与规范化
    ok, norm = normalize(section, body)
    if not ok:
        sys.stdout.write(norm + "\n")
        sys.exit(1)

    # 1.5) 只保存升级源：不涉及网络应用
    if section == "upgrade":
        merged = merge(section, norm, {})
        if merged is None:
            die("MERGE", "升级源保存失败", "请重试；若持续出现请截图反馈")
        try:
            privlib.write_atomic(CONFIG_JSON, _dump(merged[0]), 0o644)
        except SystemExit:
            die("MERGE", "升级源保存失败", "请重试；若持续出现请截图反馈")
        ok_out(message="升级源已保存")
        return

    # 2) 快照 + 合并
    old = _read_cfg().get("wan", {})
    old_mode, old_user = old.get("mode", ""), old.get("username", "") or ""
    tag = "s%d" % (int(time.time() * 1000) % 10 ** 8)
    snapshot(tag)
    try:
        raw = json.loads(body) if body.strip() else {}
    except Exception:
        raw = {}
    # 必须在 merge 之前取：merge 会把 password 从 norm 里 pop 掉（只进 secrets）
    _d = norm.get("data")
    had_pwd = isinstance(_d, dict) and bool(_d.get("password"))
    merged = merge(section, norm, raw)
    if merged is None:
        restore(tag)
        die("MERGE", "配置合并失败", "本次修改未生效；请刷新页面重试")
    cfg, secrets = merged

    # 3) 采样 pre_wan（写盘前）→ 落盘。必须先落盘，netgen 读的是这个文件
    pre_wan = wan_route_ok()
    try:
        privlib.write_atomic(CONFIG_JSON, _dump(cfg), 0o644)
        if secrets:
            privlib.write_atomic(SECRETS_JSON, _dump(secrets), 0o600)
    except SystemExit:
        restore(tag)
        die("MERGE", "配置写入失败", "本次修改未生效；请检查磁盘空间后重试")

    # 4) 用新配置生成 + 预检（此时原生文件尚未部署，失败回滚后系统与旧配置一致）
    stage = tempfile.mkdtemp(prefix="stage.", dir=RUN)
    man = os.path.join(stage, "manifest.json")
    good, out, _err = applyops.generate(stage)
    if not good:
        sys.stdout.write(out)
        restore(tag)
        shutil.rmtree(stage, ignore_errors=True)
        sys.exit(1)
    good, pf_out = applyops.preflight(stage)
    if not good:
        sys.stdout.write(pf_out + "\n")
        restore(tag)
        shutil.rmtree(stage, ignore_errors=True)
        sys.exit(1)

    # 5) 部署 + 按段决定重启范围
    applyops.apply_stage_files(stage, man)
    applyops.cleanup_stale(man)
    shutil.rmtree(stage, ignore_errors=True)

    acts = plan_for_section(section)
    new = _read_cfg().get("wan", {})
    new_mode, new_user = new.get("mode", ""), new.get("username", "") or ""
    pppoe_needed = (old_mode != new_mode) or (old_user != new_user)
    # P2-2：改账号此前不重拨，pppd 仍持旧凭据会话
    if section == "bind" and new_mode == "pppoe":
        pppoe_needed = True
    # PPPoE 下 IPv6 变更需要重拨：+ipv6/IPv6CP 与 ppp0 上的 DHCPv6 随重拨生效
    if section == "ipv6" and new_mode == "pppoe":
        pppoe_needed = True
    if section == "wan" and had_pwd:
        pppoe_needed = True
    if not pppoe_needed:
        acts = " ".join(a for a in acts.split() if a != "pppoe")
    failed = applyops.run_actions(acts)

    # 6) 验证
    err_hint = ""
    mode = _read_cfg().get("wan", {}).get("mode", "")
    if not applyops.wait_dnsmasq(10):
        err_hint = "dnsmasq 未能启动：journalctl -u dnsmasq -n 30"
        failed.append("dnsmasq")
    elif section == "wan" or pre_wan:
        # 上网设置无条件验证：首次配置 PPPoE 时 pre_wan=no，恰恰最需要报错（审计 B-03）
        tmo = 45 if (section == "wan" and mode == "pppoe") else 25
        if not wait_wan(tmo):
            if mode == "pppoe":
                log = applyops._sh(["journalctl", "-u", "dert-pppoe.service",
                                    "-n", "40", "--no-pager"], timeout=30)
                text = ((log.stdout if log else "") or "").lower()
                if "authentication failed" in text:
                    err_hint = ("PPPoE 拨号被服务器拒绝（认证失败）：请检查宽带账号/密码是否正确；"
                                "若账号正在其他设备上拨号，请先断开再试")
                elif "timeout" in text or "no response" in text:
                    err_hint = ("PPPoE 服务器无响应：请确认光猫已改为桥接模式、"
                                "WAN 线已插在绑定的 WAN 口上")
                else:
                    err_hint = "PPPoE 未能在预期时间内建立连接：journalctl -u dert-pppoe -n 40 查看详情"
            else:
                err_hint = ("应用后 WAN 未能恢复连接：请检查上级网络/网线；"
                            "可尝试在「网络设置」重新保存上网设置")
            failed.append("wan")

    if failed or err_hint:
        # 回滚到修改前配置并重放
        restore(tag)
        stage2 = tempfile.mkdtemp(prefix="stage.", dir=RUN)
        man2 = os.path.join(stage2, "manifest.json")
        ok2, _o2, _e2 = applyops.generate(stage2)
        if ok2:
            applyops.apply_stage_files(stage2, man2)
            applyops.cleanup_stale(man2)
            applyops.run_actions(plan_for_section(section))
        shutil.rmtree(stage2, ignore_errors=True)
        if not err_hint:
            err_hint = "已自动恢复到修改前的可用配置，未生效的修改请检查后重试"
        die("APPLY_ROLLED_BACK", "配置未能安全生效，已自动回滚", err_hint)

    # 7) 回应
    msg, hint_sb = "配置已保存并生效", ""
    if section == "wan":
        r = applyops._sh(["systemctl", "is-enabled", "--quiet", "sing-box.service"], timeout=15)
        if r is not None and r.returncode == 0:
            hint_sb = "若已启用 sing-box，建议执行 sb restart 重新绑定出口接口"
    if section == "lan":
        msg = "LAN 设置已生效；若管理地址已变化，请使用新 IP 重新登录"
    ok_out(message=msg, hint_singbox=hint_sb)


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    if action == "set":
        config_set(argv[2] if len(argv) > 2 else "")
    else:
        die("USAGE", "未知动作：%s" % (action or "（空）"), "合法动作：set")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
