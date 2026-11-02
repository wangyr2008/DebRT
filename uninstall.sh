#!/usr/bin/env bash
# SeedRou 卸载脚本 —— 停服务、摘除组件。默认保留 /etc/seedrou 配置（--purge 连配置一起删）。
# 注意：卸载会移除生成的网络配置（网桥/DHCP/防火墙），机器将回到未配置状态，请准备好控制台访问。
set -uo pipefail
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

LIB_DIR="/usr/local/lib/seedrou"
ETC_DIR="/etc/seedrou"
VAR_DIR="/var/lib/seedrou"

[ "$(id -u)" = "0" ] || { echo "请用 root 运行"; exit 1; }

echo "[卸载] 停止服务..."
systemctl disable --now seedrou-converge.timer seedrou-panel.service seedrou-apply.service seedrou-pppoe.service 2>/dev/null || true
systemctl stop dnsmasq.service 2>/dev/null || true
nft delete table inet seedrou_base 2>/dev/null || true
tc qdisc del dev ifb0 root 2>/dev/null || true

echo "[卸载] 移除文件..."
rm -rf "$LIB_DIR"
rm -f /etc/sudoers.d/seedrou /etc/sysctl.d/70-seedrou.conf /etc/modules-load.d/seedrou.conf \
      /etc/systemd/system/seedrou-panel.service /etc/systemd/system/seedrou-apply.service \
      /etc/systemd/system/seedrou-converge.service /etc/systemd/system/seedrou-converge.timer \
      /etc/systemd/system/seedrou-pppoe.service \
      /etc/networkd-dispatcher/routable.d/70-seedrou-dns /etc/networkd-dispatcher/routable.d/80-seedrou-cake \
      /etc/networkd-dispatcher/no-carrier.d/70-seedrou-dns /etc/ppp/ip-up.d/seedrou \
      /etc/ppp/ip-down.d/seedrou /etc/logrotate.d/seedrou \
      /etc/systemd/system/seedrou-dns-sync.service /usr/local/lib/seedrou/wait-dns-upstream \
      /etc/systemd/system/dnsmasq.service.d/10-seedrou-restart.conf \
      /etc/systemd/system/sing-box.service.d/10-seedrou-wait-wan.conf \
      /etc/systemd/system/seedrou-singbox-cleanup.service \
      /usr/local/lib/seedrou/wait-wan /usr/local/lib/seedrou/singbox-cleanup \
      /etc/dnsmasq.d/seedrou.conf /etc/nftables.d/seedrou-base.nft \
      /etc/systemd/network/10-wan.network /etc/systemd/network/20-lan.netdev /etc/systemd/network/20-lan.network \
      /etc/ppp/peers/seedrou
rm -f /etc/systemd/network/30-*.network 2>/dev/null || true

if [ "$PURGE" = "1" ]; then
  echo "[卸载] 删除配置与数据（--purge）..."
  rm -rf "$ETC_DIR" "$VAR_DIR"
else
  echo "[卸载] 保留配置：$ETC_DIR、$VAR_DIR（如需彻底删除请加 --purge）"
fi

[ -f /etc/resolv.conf.seedrou.bak ] && { cp -a /etc/resolv.conf.seedrou.bak /etc/resolv.conf; echo "[卸载] 已还原 /etc/resolv.conf"; }

systemctl daemon-reload
echo "[卸载] 完成。网络配置文件已移除，重启后网口将回到未配置状态；如需恢复出厂前的网络，请使用安装包内备份或控制台重新配置。"
