#!/usr/bin/env bash
# DebRT 卸载脚本 —— 停服务、摘除组件。默认保留 /etc/dert 配置（--purge 连配置一起删）。
# 注意：卸载会移除生成的网络配置（网桥/DHCP/防火墙），机器将回到未配置状态，请准备好控制台访问。
set -uo pipefail
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

LIB_DIR="/usr/local/lib/dert"
ETC_DIR="/etc/dert"
VAR_DIR="/var/lib/dert"

# 这三个变量后面会被 rm -rf 直接吃掉。任何一处为空/为根都会变成灾难
# （`rm -rf ""` 无害，但 `rm -rf "${LIB_DIR}/"` 在 LIB_DIR 为空时就等于 `rm -rf /`）。
# 用 :? 断言：为空或未定义时立即以非零退出，不会走到 rm。
: "${LIB_DIR:?}" "${ETC_DIR:?}" "${VAR_DIR:?}"
case "$LIB_DIR$ETC_DIR$VAR_DIR" in
    //*|/) echo "路径异常，拒绝执行" >&2; exit 1 ;;
esac

[ "$(id -u)" = "0" ] || { echo "请用 root 运行"; exit 1; }

echo "[卸载] 停止服务..."
systemctl disable --now dert-converge.timer dert-panel.service dert-apply.service dert-pppoe.service dert-dns-sync.service dert-pppoe-watchdog.timer dert-netwatch.service 2>/dev/null || true
systemctl stop dnsmasq.service 2>/dev/null || true
nft delete table inet dert_base 2>/dev/null || true
# B-3：WAN 口上的 cake/ingress 残留——不清理则运行中的机器继续被限速直到重启
if [ -f /etc/dert/config.json ]; then
  for WANP in $(python3 -c "import json;print(' '.join(p['name'] for p in json.load(open('/etc/dert/config.json')).get('ports',[]) if p.get('wan')))" 2>/dev/null); do
    tc qdisc del dev "$WANP" root 2>/dev/null || true
    tc qdisc del dev "$WANP" ingress 2>/dev/null || true
  done
fi
tc qdisc del dev ifb0 root 2>/dev/null || true

echo "[卸载] 移除文件..."
# 程序目录整体移除（其下的 wait-wan / singbox-cleanup / dns-split-* 等都在里面）
rm -rf "$LIB_DIR"
rm -f /etc/sudoers.d/dert /etc/sysctl.d/70-dert.conf /etc/modules-load.d/dert.conf \
      /etc/logrotate.d/dert \
      /etc/systemd/system/dert-panel.service /etc/systemd/system/dert-apply.service \
      /etc/systemd/system/dert-converge.service /etc/systemd/system/dert-converge.timer \
      /etc/systemd/system/dert-pppoe.service \
      /etc/systemd/system/dert-pppoe-watchdog.service /etc/systemd/system/dert-pppoe-watchdog.timer \
      /etc/systemd/system/dert-dns-sync.service /etc/systemd/system/dert-singbox-cleanup.service \
      /etc/systemd/system/dert-netwatch.service \
      /etc/systemd/system/dnsmasq.service.d/10-dert-restart.conf \
      /etc/systemd/system/sing-box.service.d/10-dert-wait-wan.conf \
      /etc/systemd/system/sing-box.service.d/20-dert-dns-split.conf \
      /etc/networkd-dispatcher/routable.d/70-dert-dns /etc/networkd-dispatcher/routable.d/80-dert-cake \
      /etc/networkd-dispatcher/routable.d/75-dert-pppoe-hotplug \
      /etc/networkd-dispatcher/routable.d/90-dert-lan-prefix \
      /etc/networkd-dispatcher/no-carrier.d/70-dert-dns \
      /etc/networkd-dispatcher/carrier.d/75-dert-pppoe-hotplug \
      /etc/ppp/ip-up.d/dert /etc/ppp/ip-down.d/dert \
      /etc/dnsmasq.d/dert.conf /etc/nftables.d/dert-base.nft \
      /etc/systemd/network/10-wan.network /etc/systemd/network/20-lan.netdev /etc/systemd/network/20-lan.network \
      /etc/systemd/network/25-ppp0.network
# P3-07：chap/pap-secrets 含运营商宽带账号与明文密码（netgen 生成，0600）——
# 卸载不清则转手/回收机器时是凭据泄漏点；25-ppp0.network 不清则 networkd
# 继续尝试在 ppp0 上跑 DHCPv6。均为 DebRT 生成物（原文件内容已被覆盖）
rm -f /etc/ppp/peers/dert /etc/ppp/chap-secrets /etc/ppp/pap-secrets
rm -f /etc/systemd/network/30-*.network 2>/dev/null || true
# DNS 分流表：把客户端 DNS DNAT 到 tun 的地址，卸载后没人监听，必须清掉
nft delete table inet dert_dns 2>/dev/null || true
# sb.sh 与其 cron 由 DebRT 下发，一并移除；sing-box 服务本体与 /etc/sing-box 保留运行
if [ -f /usr/local/bin/sb ]; then rm -f /usr/local/bin/sb; echo "[卸载] 已移除 /usr/local/bin/sb"; fi
if [ -f /etc/cron.d/sing-box ]; then rm -f /etc/cron.d/sing-box; echo "[卸载] 已移除 sing-box 每日更新 cron"; fi
if systemctl list-unit-files sing-box.service >/dev/null 2>&1; then
    echo "[卸载] sing-box 服务本体保留运行；如需一并卸载请执行：systemctl disable --now sing-box && rm -rf /etc/sing-box"
fi

if [ "$PURGE" = "1" ]; then
  echo "[卸载] 删除配置与数据（--purge）..."
  rm -rf "$ETC_DIR" "$VAR_DIR"
else
  echo "[卸载] 保留配置：$ETC_DIR、$VAR_DIR（如需彻底删除请加 --purge）"
fi

[ -f /etc/resolv.conf.dert.bak ] && { cp -a /etc/resolv.conf.dert.bak /etc/resolv.conf; echo "[卸载] 已还原 /etc/resolv.conf"; }

userdel dert 2>/dev/null || true
systemctl daemon-reload
echo "[卸载] 完成。网络配置文件已移除，重启后网口将回到未配置状态；如需恢复出厂前的网络，请使用安装包内备份或控制台重新配置。"
