#!/usr/bin/env bash
# ============================================================================
# DebRT 安装脚本 —— 在目标机（Debian 13 minimal，root）上执行。
#
#   sudo bash install.sh [--no-podman]
#
# 做什么：装依赖 → 建 dert 用户 → 部署面板与特权脚本 → 生成初始配置
#         （自动发现网口：第一个口 WAN，其余 LAN，LAN IP 192.168.10.1）
#         → 启用服务 → 首次收敛 → 自检。
# 不做什么：不安装、不配置、不触碰 sing-box（由你自己的 sb.sh 管理，见 COEXISTENCE.md）。
# ============================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
NO_PODMAN=0
SMOKE=0
for ARG in "$@"; do
  case "$ARG" in
    --no-podman) NO_PODMAN=1 ;;
    --smoke)     SMOKE=1 ;;
  esac
done

LIB_DIR="/usr/local/lib/dert"
SHARE_DIR="${LIB_DIR}/share"
ETC_DIR="/etc/dert"
VAR_DIR="/var/lib/dert"
RUN_DIR="/run/dert"

c_info()  { printf '\033[32m[安装]\033[0m %s\n' "$*"; }

c_warn()  { printf '\033[33m[注意]\033[0m %s\n' "$*"; }
c_err()   { printf '\033[31m[错误]\033[0m %s\n' "$*"; }
die() { c_err "$*"; exit 1; }

[ "$(id -u)" = "0" ] || die "请用 root 运行：sudo bash $0"
. /etc/os-release
[ "${ID:-}" = "debian" ] || c_warn "非 Debian 系统（${ID:-?}），未在 Debian 13 上验证，继续但风险自负"

# ---------------- 1. 依赖 ----------------
c_info "安装软件包（nftables dnsmasq ppp python3 等，视网络情况 1-5 分钟）..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || c_warn "apt update 失败（软件源可能暂不可达），尝试直接安装"
PKGS=(nftables dnsmasq ppp pppoe python3 curl ca-certificates sudo ethtool networkd-dispatcher iproute2 smartmontools)
[ "$NO_PODMAN" = "0" ] && PKGS+=(podman podman-compose uidmap)
apt-get install -y -qq "${PKGS[@]}" || die "软件包安装失败，请检查软件源（/etc/apt/sources.list）与网络"

# ---------------- 2. 用户与目录 ----------------
id dert >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin dert
mkdir -p "$LIB_DIR/panel" "$SHARE_DIR" "$ETC_DIR" "$VAR_DIR/backups" "$VAR_DIR/lastgood" \
         "$VAR_DIR/uploads" "$RUN_DIR" /etc/nftables.d /var/lib/misc
chown -R dert:dert "$VAR_DIR"
chmod 750 "$VAR_DIR"

# ---------------- 3. 部署程序文件 ----------------
c_info "部署程序文件..."
install -m 755 "$SRC_DIR/priv/dert-priv"   "$LIB_DIR/dert-priv"
install -m 644 "$SRC_DIR/priv/lib.sh"         "$LIB_DIR/lib.sh"
# 全部 .py 按 glob 部署：逐个列举会漏新文件（pwhash.py 就漏过一次，被 CI 抓到）
install -m 644 "$SRC_DIR/priv/"*.py           "$LIB_DIR/"
install -m 755 "$SRC_DIR/priv/qdisc-apply"    "$LIB_DIR/qdisc-apply"
install -m 755 "$SRC_DIR/priv/wait-wan"       "$LIB_DIR/wait-wan"
install -m 755 "$SRC_DIR/priv/wait-dns-upstream" "$LIB_DIR/wait-dns-upstream"
install -m 755 "$SRC_DIR/priv/wan-addr-sync"  "$LIB_DIR/wan-addr-sync"
install -m 755 "$SRC_DIR/priv/pppoe-watchdog" "$LIB_DIR/pppoe-watchdog"
install -m 755 "$SRC_DIR/priv/netwatch"       "$LIB_DIR/netwatch"
install -m 644 "$SRC_DIR/panel/"*.py          "$LIB_DIR/panel/"
mkdir -p "$LIB_DIR/panel/static"
install -m 644 "$SRC_DIR/panel/static/"*      "$LIB_DIR/panel/static/"
install -m 644 "$SRC_DIR/VERSION"             "$SHARE_DIR/VERSION"
# sb.sh 管理器随 DebRT 下发（sing-box 本体默认不安装，由面板一键安装或控制台 sb install）
if [ -f "$SRC_DIR/singbox/sb.sh" ]; then
    install -m 755 "$SRC_DIR/singbox/sb.sh" /usr/local/bin/sb
    echo "[安装] 已下发 sing-box 管理器：/usr/local/bin/sb（sing-box 本体未安装，需要时在面板一键安装）"
fi
# networkd-dispatcher 钩子
mkdir -p /etc/networkd-dispatcher/routable.d /etc/networkd-dispatcher/no-carrier.d /etc/networkd-dispatcher/carrier.d
install -m 755 "$SRC_DIR/network/dispatcher/70-dert-dns" /etc/networkd-dispatcher/routable.d/
install -m 755 "$SRC_DIR/network/dispatcher/80-dert-cake" /etc/networkd-dispatcher/routable.d/
install -m 755 "$SRC_DIR/network/dispatcher/90-dert-lan-prefix" /etc/networkd-dispatcher/routable.d/
install -m 755 "$SRC_DIR/network/dispatcher/70-dert-dns" /etc/networkd-dispatcher/no-carrier.d/
install -m 755 "$SRC_DIR/network/dispatcher/75-dert-pppoe-hotplug" /etc/networkd-dispatcher/carrier.d/
install -m 755 "$SRC_DIR/network/dispatcher/75-dert-pppoe-hotplug" /etc/networkd-dispatcher/routable.d/
install -m 755 "$SRC_DIR/network/ppp/ip-up.d/dert" /etc/ppp/ip-up.d/dert
install -m 755 "$SRC_DIR/network/ppp/ip-down.d/dert" /etc/ppp/ip-down.d/dert
# systemd / sysctl / modules
install -m 644 "$SRC_DIR/systemd/dert-panel.service"   /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/dert-apply.service"   /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/dert-converge.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/dert-converge.timer"  /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/dert-dns-sync.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/dert-pppoe-watchdog.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/dert-pppoe-watchdog.timer" /etc/systemd/system/
install -m 644 "$SRC_DIR/systemd/dert-netwatch.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/network/systemd/dert-pppoe.service" /etc/systemd/system/
install -m 644 "$SRC_DIR/network/sysctl/70-dert.conf"  /etc/sysctl.d/
install -m 644 "$SRC_DIR/network/modules/dert.conf"    /etc/modules-load.d/
# sudo 审计日志轮转（复审 N-07）
install -m 644 "$SRC_DIR/network/logrotate/dert" /etc/logrotate.d/dert
# sudoers（先语法检查再落盘）
install -m 644 "$SRC_DIR/sudoers/dert" /etc/sudoers.d/dert.tmp
visudo -c -f /etc/sudoers.d/dert.tmp >/dev/null || { rm -f /etc/sudoers.d/dert.tmp; die "sudoers 语法校验失败"; }
mv /etc/sudoers.d/dert.tmp /etc/sudoers.d/dert && chmod 440 /etc/sudoers.d/dert

# ---------------- 4. 配置初始化（保留既有配置） ----------------
if [ ! -f "$ETC_DIR/config.json" ]; then
  c_info "生成初始配置（自动发现网口：第 1 个口 = WAN，其余 = LAN；LAN 192.168.10.1）..."
  python3 - <<'PYEOF'
import json, os, re
ports = []
for n in sorted(os.listdir("/sys/class/net")):
    if re.match(r"^(eth\d+|en[psox]\S*)$", n):
        ports.append({"name": n, "wan": len(ports) == 0, "lan": len(ports) > 0})
if not ports:
    ports = [{"name": "eth0", "wan": True, "lan": False}, {"name": "eth1", "wan": False, "lan": True}]
cfg = {
    "version": 1, "ports": ports,
    "wan": {"mode": "dhcp", "username": "", "dns_mode": "auto", "dns": [], "ip": "", "mask": "", "gw": "", "has_password": False},
    "ipv6": {"enabled": False, "mode": "native", "dns_mode": "auto", "dns": [], "addr": "", "gw": "", "prefix": ""},
    "lan": {"ip": "192.168.10.1", "mask": "255.255.255.0"},
    "dhcp": {"enabled": True, "pool_start": 100, "pool_end": 200, "lease": 120, "dns1": "", "dns2": "", "gw": ""},
    "static": [], "qos": {"enabled": False, "down": 0, "up": 0},
    "firewall": {"wan_input_drop": True, "portfw": []},
    "upgrade": {"source": ""},
}
json.dump(cfg, open("/etc/dert/config.json", "w"), ensure_ascii=False, indent=1)
PYEOF
  c_info "发现网口：$(python3 -c 'import json;print(", ".join(p["name"]+("(WAN)" if p["wan"] else "(LAN)") for p in json.load(open("'$ETC_DIR'/config.json"))["ports"]))')"
else
  c_warn "检测到既有配置 $ETC_DIR/config.json，保留不动"
fi

# secrets（管理密码 + 会话密钥 + PPPoE 密码位）
if [ ! -f "$ETC_DIR/secrets.json" ]; then
  c_info "设置管理密码（输入时不回显；直接回车则自动生成随机密码）..."
  ADMIN_PW="${DERT_ADMIN_PW:-}"   # CI 冒烟可经环境变量注入（--smoke 配套）
  if [ -z "$ADMIN_PW" ] && [ -t 0 ]; then
    read -r -s -p "管理密码（8-32 位，回车=自动生成）: " ADMIN_PW || true
    echo
  fi
  if [ -z "$ADMIN_PW" ]; then
    ADMIN_PW="$(python3 -c 'import secrets;print("".join(secrets.choice("abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(12)))')"
    GENERATED=1
  fi
  # 密码经环境变量传入（/proc environ 0400 root），不进 argv（五审 P-05）
  ADMIN_PW="$ADMIN_PW" python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, "/usr/local/lib/dert")
import pwhash   # 唯一实现（六审 P-02）
pw = os.environ["ADMIN_PW"]
if not (8 <= len(pw) <= 32):
    print(json.dumps({"ok": False, "code": "PW_LEN", "message": "管理密码需为 8–32 位"}))
    sys.exit(1)   # C-3：与面板改密同一约束，安装路径不设 1 位弱密码
data = {
    "admin_pbkdf2": pwhash.hash_new(pw),
    "session_secret": os.urandom(32).hex(),
    "pppoe_password": "",
}
json.dump(data, open("/etc/dert/secrets.json", "w"), indent=1)
os.chmod("/etc/dert/secrets.json", 0o600)
PYEOF
  if [ "${GENERATED:-0}" = "1" ]; then
    c_info "已自动生成管理密码（只显示这一次，请立即记录）：$ADMIN_PW"
  fi
fi
chmod 600 "$ETC_DIR/secrets.json" 2>/dev/null || true
chmod 644 "$ETC_DIR/config.json" 2>/dev/null || true

# 租约文件用 Debian dnsmasq 默认路径 /var/lib/misc/dnsmasq.leases（审计 B-04：
# dnsmasq 启动期以 root 打开该文件，面板作为 dert 可读 644 的租约用于设备列表）

# 路由器自身 DNS → 本机 dnsmasq（备份原文件）
if [ ! -f /etc/resolv.conf.dert.bak ]; then
  cp -a /etc/resolv.conf /etc/resolv.conf.dert.bak 2>/dev/null || true
fi
if [ ! -L /etc/resolv.conf ]; then
  printf 'nameserver 127.0.0.1\noptions timeout:1 attempts:2\n' > /etc/resolv.conf
  c_info "/etc/resolv.conf 已指向本机 dnsmasq（原文件备份为 resolv.conf.dert.bak）"
else
  c_warn "/etc/resolv.conf 是符号链接（可能由 systemd-resolved 管理），未改动；路由器自身 DNS 需自行确认"
fi

# ---------------- 5. 服务 ----------------
c_info "启用服务..."
# sing-box 共存：检测到 sing-box.service 时，自动挂"等 WAN 就绪"drop-in
# （防止 auto_route 在 WAN 未就绪时形成路由死循环；收敛流程也会维护，见 COEXISTENCE.md）
if systemctl list-unit-files sing-box.service >/dev/null 2>&1; then
  mkdir -p /etc/systemd/system/sing-box.service.d
  install -m 644 "$SRC_DIR/systemd/sing-box.service.d/10-dert-wait-wan.conf" \
    /etc/systemd/system/sing-box.service.d/10-dert-wait-wan.conf
  # DNS 分流：启动前生成 00-dert-dns.json、启动后派生 nft 表、停止时删表
  install -m 644 "$SRC_DIR/systemd/sing-box.service.d/20-dert-dns-split.conf" \
    /etc/systemd/system/sing-box.service.d/20-dert-dns-split.conf
  c_info "检测到 sing-box：已安装 WAN 就绪 + DNS 分流 drop-in"
fi
# singbox-cleanup 单元/脚本无条件安装（幂等无害；后装 sing-box 的用户由 converge 补 drop-in）
install -m 755 "$SRC_DIR/priv/singbox-cleanup" "$LIB_DIR/singbox-cleanup"
install -m 644 "$SRC_DIR/systemd/dert-singbox-cleanup.service" /etc/systemd/system/
# DNS 分流三件套（prepare/sync/teardown）——只由 sing-box 的 drop-in 调用，
# 没有 sing-box 时不会被触发，安装无害
install -m 755 "$SRC_DIR/priv/dns-split-prepare"  "$LIB_DIR/dns-split-prepare"
install -m 755 "$SRC_DIR/priv/dns-split-sync"     "$LIB_DIR/dns-split-sync"
install -m 755 "$SRC_DIR/priv/dns-split-teardown" "$LIB_DIR/dns-split-teardown"
# 接管网口：清理 ifupdown/dhcpcd 残留——否则 dhcpcd 会在网桥端口上跑 DHCP 客户端、
# 覆写 /etc/resolv.conf、留下指向 link-local 的死默认路由（真机首装实测）
if [ -f /etc/network/interfaces ] && [ -f "$ETC_DIR/config.json" ]; then
  for IF in $(python3 -c 'import json;print(" ".join(p["name"] for p in json.load(open("'$ETC_DIR'/config.json")).get("ports",[])))' 2>/dev/null); do
    sed -i "\#^allow-hotplug ${IF}\$#d; \#^iface ${IF} inet #d" /etc/network/interfaces 2>/dev/null || true
  done
fi
pkill -x dhcpcd 2>/dev/null || true
# Debian 自带 nftables.service 开机 flush ruleset —— 与 sing-box 表冲突，禁用之（见 COEXISTENCE.md）
systemctl disable nftables.service >/dev/null 2>&1 || true
if [ "$SMOKE" = "1" ]; then
  # CI 冒烟模式：不重启 networkd/dnsmasq、不跑 apply-all（避免断掉 runner 网络）
  touch "$RUN_DIR/smoke-mode"   # CI 冒烟网关：升级通道据此跳过内联收敛与延迟收敛重启
  systemctl daemon-reload
  systemctl enable --now dert-panel.service >/dev/null 2>&1 || true
else
  systemctl daemon-reload
  systemctl enable --now dnsmasq.service >/dev/null 2>&1 || true
  systemctl enable --now networkd-dispatcher.service >/dev/null 2>&1 || true
  # dnsmasq 崩溃秒级拉起（事件机制；全网 DNS 不能等 30 分钟 timer）
  mkdir -p /etc/systemd/system/dnsmasq.service.d
  install -m 644 "$SRC_DIR/systemd/dnsmasq.service.d/10-dert-restart.conf" \
    /etc/systemd/system/dnsmasq.service.d/10-dert-restart.conf
  systemctl enable --now dert-dns-sync.service >/dev/null 2>&1 || true
  systemctl enable dert-apply.service >/dev/null 2>&1
  systemctl enable --now dert-converge.timer >/dev/null 2>&1
  # dert 命令：特权 CLI 的用户入口（sudo 提权后转发；命令清单见 `dert --help`）
  ln -sf "$LIB_DIR/dert-priv" /usr/local/bin/dert
  # 主机名：仅当仍为出厂名 debian 时设为 dert，不覆盖用户自定义
  if [ "$(hostname)" = "debian" ]; then
    hostnamectl set-hostname dert 2>/dev/null || hostname dert 2>/dev/null || true
    c_info "主机名已设为 dert"
  fi
  # PPPoE 看门狗：pppd 卡在"服务 active 但 ppp0 消失"时无人自愈（真机实录）
  systemctl enable --now dert-pppoe-watchdog.timer >/dev/null 2>&1 || true
  systemctl restart dert-pppoe-watchdog.timer >/dev/null 2>&1 || true
  systemctl enable --now dert-netwatch.service >/dev/null 2>&1 || true
  systemctl enable --now dert-panel.service >/dev/null 2>&1

  c_info "首次配置收敛（生成网桥/防火墙/DHCP 配置并应用）..."
  "$LIB_DIR/dert-priv" apply-all || c_warn "首次收敛有告警，请查看上方输出；不影响面板访问"
fi

# ---------------- 6. 自检与收尾 ----------------
c_info "自检："
"$LIB_DIR/dert-priv" selftest || true

LAN_IP="$(python3 -c 'import json;print(json.load(open("'$ETC_DIR'/config.json"))["lan"]["ip"])' 2>/dev/null || echo 192.168.10.1)"
echo
echo "============================================================"
echo " DebRT 安装完成"
echo "   管理地址 : http://${LAN_IP}/   （从局域网电脑访问）"
echo "   网络建议 : 上级网络接第 1 个网口（WAN），电脑接其余口（LAN）"
echo "   服务状态 : systemctl status dert-panel dert-apply"
if systemctl list-unit-files | grep -q '^sing-box\.service'; then
  echo "   sing-box : 已检测到（由你的 sb.sh 管理，本系统不触碰；参见 singbox/COEXISTENCE.md）"
fi
echo "============================================================"
