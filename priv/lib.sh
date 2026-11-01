#!/usr/bin/env bash
# SeedRou 特权库 —— 仅被 seedrou-priv / 钩子脚本 source。
# 约定：所有用户可见错误一律以 JSON 输出到 stdout（die），可操作（原因 + 下一步）。
# 原子写：同目录临时文件 → fsync → rename，任何时刻磁盘上都只有完整文件。

SEEDROU_ROOT="/usr/local/lib/seedrou"
SEEDROU_SHARE="${SEEDROU_ROOT}/share"
SEEDROU_ETC="/etc/seedrou"
SEEDROU_VAR="/var/lib/seedrou"
SEEDROU_RUN="/run/seedrou"
CONFIG_JSON="${SEEDROU_ETC}/config.json"
SECRETS_JSON="${SEEDROU_ETC}/secrets.json"
VERSION_FILE="${SEEDROU_SHARE}/VERSION"
LOCK_FILE="${SEEDROU_RUN}/apply.lock"

PANEL_UNIT="seedrou-panel.service"
NFT_TABLE="inet seedrou_base"

# ---------------- 错误协议 ----------------
json_out() { printf '%s\n' "$1"; }
die() { # die CODE MESSAGE [HINT]
  json_out "{\"ok\":false,\"code\":\"$1\",\"message\":$(py_quote "$2"),\"hint\":$(py_quote "${3:-}")}"
  exit 1
}
py_quote() { python3 -c 'import json,sys;print(json.dumps(sys.argv[1],ensure_ascii=False))' "$1"; }
ok_out() { # ok_out [KEY VAL ...] -> {"ok":true,...}（值尝试按 JSON 解析，true/false/数字为真类型）
  python3 - "$@" <<'PYEOF'
import json, sys
d = {"ok": True}
a = sys.argv[1:]
for i in range(0, len(a), 2):
    k, v = a[i], a[i + 1]
    try:
        d[k] = json.loads(v)
    except Exception:
        d[k] = v
print(json.dumps(d, ensure_ascii=False))
PYEOF
}

require_root() {
  [ "$(id -u)" = "0" ] || die "PERM" "需要 root 权限" "请通过面板操作或用 sudo 执行"
}

# ---------------- 原子写 ----------------
# write_atomic PATH MODE < content-on-stdin
write_atomic() {
  local path="$1" mode="${2:-644}" tmp dir
  dir="$(dirname "$path")"
  mkdir -p "$dir"
  tmp="$(mktemp "${dir}/.seedrou-tmp.XXXXXXXX")" || die "IO" "无法创建临时文件" "请检查磁盘空间（df -h）"
  cat > "$tmp" || { rm -f "$tmp"; die "IO" "写入临时文件失败"; }
  chmod "$mode" "$tmp"
  sync -f "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$path" || { rm -f "$tmp"; die "IO" "原子替换失败：$path"; }
  sync -f "$dir" 2>/dev/null || true
}

# ---------------- 锁 ----------------
# 幂等：已持锁（嵌套调用，如 backup-restore 内部再跑 apply-all）直接返回，避免释放重取的竞态窗口
acquire_lock() {
  [ -n "${_SEEDROU_LOCK_HELD:-}" ] && return 0
  mkdir -p "$SEEDROU_RUN" 2>/dev/null || true
  exec 9>"$LOCK_FILE" || die "LOCK" "无法打开锁文件 $LOCK_FILE"
  if ! flock -n 9; then
    die "BUSY" "另一项配置变更正在进行中" "请稍等几秒后重试；若频繁出现请检查是否有其他管理员同时在操作"
  fi
  _SEEDROU_LOCK_HELD=1
}

# 清理已从配置中移除的网口对应的 networkd 文件（apply 只写不删的遗留）
cleanup_stale_port_networks() {
  python3 - <<'PYEOF' | while read -r f; do [ -n "$f" ] && rm -f "$f"; done
import json, glob, os
try:
    cfg = json.load(open("/etc/seedrou/config.json"))
    names = {p["name"] for p in cfg.get("ports", [])}
except Exception:
    raise SystemExit
for f in glob.glob("/etc/systemd/network/30-*.network"):
    base = os.path.basename(f)[3:-len(".network")]
    if base not in names:
        print(f)
PYEOF
}

# ---------------- 配置读写 ----------------
config_read() { python3 -c 'import json;print(json.dumps(json.load(open("'"$CONFIG_JSON"'")),ensure_ascii=False))' 2>/dev/null; }

secrets_read() { python3 -c 'import json;print(json.dumps(json.load(open("'"$SECRETS_JSON"'")),ensure_ascii=False))' 2>/dev/null; }

# 快照与回滚（只针对 config.json + secrets.json；原生配置全部可再生）
snapshot_lastgood() {
  local tag="$1" dir
  dir="${SEEDROU_VAR}/lastgood"
  mkdir -p "$dir"
  cp -a "$CONFIG_JSON" "${dir}/config.json.pre-${tag}" 2>/dev/null || true
  [ -f "$SECRETS_JSON" ] && cp -a "$SECRETS_JSON" "${dir}/secrets.json.pre-${tag}" 2>/dev/null
  # 轮转：仅保留最近 5 份
  ls -1t "${dir}"/config.json.pre-* 2>/dev/null | tail -n +6 | while read -r f; do
    rm -f "$f" "${f/config.json.pre-/secrets.json.pre-}"
  done
}
restore_lastgood() {
  local tag="$1" dir="${SEEDROU_VAR}/lastgood"
  local c="${dir}/config.json.pre-${tag}" s="${dir}/secrets.json.pre-${tag}"
  [ -f "$c" ] || return 1
  cp -a "$c" "$CONFIG_JSON"
  [ -f "$s" ] && cp -a "$s" "$SECRETS_JSON"
  return 0
}

# ---------------- 应用流水线 ----------------
# apply_stage_files STAGE_DIR MANIFEST_JSON_FILE -> 原子部署（生成文件已在 stage 内）
apply_stage_files() {
  local stage="$1" manfile="$2" line rel mode dest tmp dest_dir
  while IFS=$'\t' read -r rel mode; do
    [ -n "$rel" ] || continue
    dest="/${rel}"
    dest_dir="$(dirname "$dest")"
    mkdir -p "$dest_dir"
    tmp="$(mktemp "${dest_dir}/.seedrou-tmp.XXXXXXXX")"
    cp "${stage}/${rel}" "$tmp" && chmod "$mode" "$tmp" && mv -f "$tmp" "$dest" \
      || { rm -f "$tmp"; die "IO" "部署生成文件失败：$dest"; }
  done < <(python3 - "$manfile" <<'PYEOF'
import json, sys
man = json.load(open(sys.argv[1]))
for f in man["files"]:
    print(f"{f['path']}\t{f['mode']:04o}")
PYEOF
)
}

# netgen 预检（在暂存目录上做语法级校验；不触网、不退出进程）
# 失败时向 stdout 输出错误 JSON 并 return 1
preflight_stage() {
  local stage="$1" nft_file dnsmasq_file err
  nft_file="${stage}/etc/nftables.d/seedrou-base.nft"
  dnsmasq_file="${stage}/etc/dnsmasq.d/seedrou.conf"
  if command -v nft >/dev/null 2>&1 && [ -f "$nft_file" ]; then
    if ! nft -c -f "$nft_file" >/dev/null 2>&1; then
      err="$(nft -c -f "$nft_file" 2>&1 | tail -n 3 | tr '\n' ' ')"
      json_out "{\"ok\":false,\"code\":\"NFT_SYNTAX\",\"message\":$(py_quote "防火墙规则语法校验未通过：${err}"),\"hint\":$(py_quote "本次修改未生效；请把页面上的操作内容反馈给开发者")}"
      return 1
    fi
  fi
  if command -v dnsmasq >/dev/null 2>&1 && [ -f "$dnsmasq_file" ]; then
    if ! dnsmasq --test -C "$dnsmasq_file" >/dev/null 2>&1; then
      err="$(dnsmasq --test -C "$dnsmasq_file" 2>&1 | tail -n 2 | tr '\n' ' ')"
      json_out "{\"ok\":false,\"code\":\"DNSMASQ_SYNTAX\",\"message\":$(py_quote "DHCP/DNS 配置校验未通过：${err}"),\"hint\":$(py_quote "本次修改未生效；请检查刚填写的 DHCP 参数")}"
      return 1
    fi
  fi
  return 0
}

# WAN 可用性：主表默认路由是否落在 WAN 口上（排除 tun 等）
wan_route_ok() {
  python3 - <<'PYEOF'
import json, subprocess, sys
try:
    cfg = json.load(open("/etc/seedrou/config.json"))
except Exception:
    print("no"); sys.exit()
wans = {p["name"] for p in cfg.get("ports", []) if p.get("wan")}
if cfg.get("wan", {}).get("mode") == "pppoe":
    wans.add("ppp0")
try:
    out = subprocess.run(["ip", "-j", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
    routes = json.loads(out or "[]")
except Exception:
    print("no"); sys.exit()
for r in routes:
    if r.get("dev") in wans:
        print("yes"); sys.exit()
print("no")
PYEOF
}

wait_wan() { # wait_wan TIMEOUT_S -> 0=ok
  local t="$1" i=0
  while [ "$i" -lt "$t" ]; do
    [ "$(wan_route_ok)" = "yes" ] && return 0
    sleep 2; i=$((i + 2))
  done
  return 1
}

wait_dnsmasq() {
  local t="${1:-10}" i=0
  while [ "$i" -lt "$t" ]; do
    systemctl is-active --quiet dnsmasq.service && return 0
    sleep 1; i=$((i + 1))
  done
  return 1
}

# 生成的服务清单（按 section 决定重启范围）—— 输出以空格分隔的动作串
plan_for_section() { # plan_for_section SECTION -> echo "actions"
  case "$1" in
    bind)     echo "networkd dnsmasq nft cake" ;;
    wan)      echo "networkd pppoe dnsmasq nft cake" ;;
    ipv6)     echo "networkd dnsmasq nft" ;;
    lan)      echo "networkd dnsmasq nft cake" ;;
    dhcp)     echo "dnsmasq" ;;
    static)   echo "dnsmasq" ;;
    qos)      echo "cake" ;;
    firewall) echo "nft" ;;
    upgrade)  echo "" ;;
    *)        echo "networkd dnsmasq nft cake" ;;
  esac
}

svc_restart_networkd() {
  systemctl daemon-reload
  systemctl restart systemd-networkd.service 2>/dev/null || systemctl restart systemd-networkd
}
svc_nft() {
  [ -f /etc/nftables.d/seedrou-base.nft ] && nft -f /etc/nftables.d/seedrou-base.nft
  [ -x "${SEEDROU_ROOT}/wan-addr-sync" ] && "${SEEDROU_ROOT}/wan-addr-sync"
}
svc_cake() {
  [ -x "${SEEDROU_ROOT}/qdisc-apply" ] && "${SEEDROU_ROOT}/qdisc-apply"
}
svc_pppoe() { # enable|disable|restart 依据当前配置
  local mode; mode="$(python3 -c 'import json;print(json.load(open("'"$CONFIG_JSON"'")).get("wan",{}).get("mode",""))' 2>/dev/null || echo "")"
  if [ "$mode" = "pppoe" ]; then
    systemctl enable --quiet seedrou-pppoe.service 2>/dev/null || true
    systemctl restart seedrou-pppoe.service
  else
    systemctl disable --quiet seedrou-pppoe.service 2>/dev/null || true
    systemctl stop seedrou-pppoe.service 2>/dev/null || true
  fi
}
run_actions() { # run_actions "action ..." -> 输出失败动作名；全成功输出空
  local act out=""
  for act in $1; do
    case "$act" in
      networkd) svc_restart_networkd || out="$out networkd" ;;
      dnsmasq)  systemctl restart dnsmasq.service || out="$out dnsmasq" ;;
      nft)      svc_nft || out="$out nft" ;;
      cake)     svc_cake || true ;;
      pppoe)    svc_pppoe || out="$out pppoe" ;;
    esac
  done
  echo "$out"
}
