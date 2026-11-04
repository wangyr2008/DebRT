#!/usr/bin/env bash
# ============================================================
#  sing-box 简易管理脚本（Debian 13）—— SeedRou 下发增强版
#
#  相对原版的变化（SeedRou v0.1.32 起）：
#  - update 结果原子写入 /var/lib/sing-box/update-state.json（面板读它显示
#    "上次成功更新时长"与持久失败原因；cron 与手动共用同一状态源）
#  - sb auto on [HH:MM] 可指定每日更新时间（原版固定 04:00）
#  - sb seturl <URL> 支持非交互传参（面板/脚本用）
#  - 新增 sb uninstall（卸载内核+订阅数据，/var/lib/seedrou 留一份备份）
#  - install 补 IPv6 转发；cron 写入改原子替换
#
#  第一次使用（会自动装成 /usr/local/bin/sb）：
#      sudo bash sb.sh
#  以后：
#      sb                 打开菜单
#      sb update          只更新配置（cron 就是这么调用的）
#      sb seturl          替换 Sub-Store 订阅链接
# ============================================================

set -uo pipefail

SB_BIN="/usr/local/bin/sb"                       # 快捷命令位置
CONF_DIR="/etc/sing-box"
CONF_FILE="${CONF_DIR}/config.json"
URL_FILE="${CONF_DIR}/config.url"
SVC_NAME="sing-box"
SVC_USER="sing-box"
CRON_FILE="/etc/cron.d/sing-box"
INSTALL_URL="https://gh-proxy.org/https://raw.githubusercontent.com/SagerNet/sing-box/stable/docs/installation/tools/install.sh"

# ---------------- 输出小工具 ----------------
sb_info() { printf '\033[32m[信息]\033[0m %s\n' "$*"; }
sb_warn() { printf '\033[33m[注意]\033[0m %s\n' "$*"; }
sb_err()  { printf '\033[31m[错误]\033[0m %s\n' "$*"; }

SB_SEP="------------------------------------------------------------"

# 动作开始：画一条标题分隔带，让每步操作的输出自成一块
sb_action_header() {
    echo
    echo "$SB_SEP"
    printf '  >>> %s\n' "$1"
    echo "$SB_SEP"
    echo
}

# 动作结束：带退出码和耗时，一眼看出是成功还是失败
sb_action_footer() {
    local rc="$1" secs="$2"
    echo
    echo "$SB_SEP"
    if [[ "$rc" -eq 0 ]]; then
        printf '  <<< 完成（耗时 %s 秒）\n' "$secs"
    else
        printf '  <<< 未完成，请查看上面的提示（退出码 %s，耗时 %s 秒）\n' "$rc" "$secs"
    fi
    echo "$SB_SEP"
    echo
}

# 菜单里统一走这里：标题 -> 执行 -> 收尾 -> 暂停
sb_run_action() {
    local title="$1" start rc
    shift
    sb_action_header "$title"
    start=$SECONDS
    "$@"
    rc=$?
    sb_action_footer "$rc" "$((SECONDS - start))"
    read -rp "按回车返回菜单..." _ || true
}

# ---------------- 0. 权限 / 自安装 ----------------
sb_require_root() {
    if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        sb_warn "需要 root 权限，正在用 sudo 重新运行..."
        exec sudo -E bash "$0" "$@"
    fi
    sb_err "请用 root 运行：sudo bash $0"
    exit 1
}

sb_self_install() {
    local self target
    self="$(readlink -f -- "$0" 2>/dev/null || true)"
    if [[ -z "$self" || ! -f "$self" ]]; then
        sb_warn "无法确定脚本自身路径，跳过安装快捷命令 sb"
        return 0
    fi
    target="$(readlink -f -- "$SB_BIN" 2>/dev/null || echo "$SB_BIN")"
    [[ "$self" == "$target" ]] && return 0

    if install -m 0755 -- "$self" "$SB_BIN"; then
        sb_info "已安装快捷命令：${SB_BIN}（以后直接输入 sb 就能打开菜单）"
    else
        sb_warn "快捷命令安装失败，不影响本次使用"
    fi
}

# ---------------- 公共步骤 ----------------
sb_ensure_curl() {
    command -v curl >/dev/null 2>&1 && return 0
    sb_info "未检测到 curl，正在安装..."
    apt-get update -y >/dev/null 2>&1 || true
    if ! apt-get install -y curl; then
        sb_err "curl 安装失败，请检查网络或软件源"
        return 1
    fi
    command -v curl >/dev/null 2>&1
}

# 服务以 sing-box 用户运行，用户不存在的话 systemd 直接起不来
sb_ensure_user() {
    id "$SVC_USER" >/dev/null 2>&1 && return 0
    sb_info "创建系统用户 ${SVC_USER} ..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
        || sb_warn "创建用户 ${SVC_USER} 失败，请手动检查"
}

# 更新结果落盘（面板数据源，cron 与手动共用）：原子写 JSON
sb_record_update() {
    local ok="$1" err="$2"
    mkdir -p /var/lib/sing-box
    SEEDROU_SB_OK="$ok" SEEDROU_SB_ERR="$err" SEEDROU_SB_TRIGGER="${SB_UPDATE_TRIGGER:-auto}" \
    python3 - <<'PYEOF'
import json, os
state = {"ok": os.environ["SEEDROU_SB_OK"] == "1",
         "ts": int(__import__("time").time()),
         "error": os.environ["SEEDROU_SB_ERR"],
         "trigger": os.environ["SEEDROU_SB_TRIGGER"]}
path = "/var/lib/sing-box/update-state.json"
with open(path + ".tmp", "w") as f:
    json.dump(state, f, ensure_ascii=False)
os.replace(path + ".tmp", path)
PYEOF
}

# 每次重启前都必须修权限，否则 sing-box 用户读不到配置
sb_fix_perms() {
    mkdir -p "$CONF_DIR"
    chown -R "${SVC_USER}:${SVC_USER}" "$CONF_DIR" 2>/dev/null \
        || sb_warn "chown 失败（可能还没有 ${SVC_USER} 用户），请先执行第 1 项安装"
}

sb_restart_service() {
    sb_fix_perms
    sb_info "正在重启 ${SVC_NAME} ..."
    systemctl restart "$SVC_NAME" || sb_err "systemctl restart 执行失败"
    sleep 2
    if systemctl is-active --quiet "$SVC_NAME"; then
        sb_info "${SVC_NAME} 运行中"
    else
        sb_err "${SVC_NAME} 没有起来，最近日志："
        journalctl -u "$SVC_NAME" -n 20 --no-pager
        return 1
    fi
}

# ---------------- 1. 安装 / 升级 ----------------

# 取最新版本号：用 releases/latest 的 302 跳转，不碰 api.github.com
# （未认证的 GitHub API 每小时只有 60 次，很容易 rate limit）
sb_latest_version() {
    local u out ver=""
    for u in "https://github.com/SagerNet/sing-box/releases/latest" \
             "https://gh-proxy.org/https://github.com/SagerNet/sing-box/releases/latest"; do
        out="$(curl -sIL --connect-timeout 15 --max-time 40 -o /dev/null \
                    -w '%{url_effective}' "$u" 2>/dev/null || true)"
        ver="$(printf '%s' "$out" | sed -n 's#.*/tag/v\([0-9][0-9.]*\).*#\1#p')"
        [[ -n "$ver" ]] && break
    done
    printf '%s' "$ver"
}

# 取本地已安装的版本号（没装则输出空）
sb_local_version() {
    command -v sing-box >/dev/null 2>&1 || return 0
    local v
    v="$(sing-box version 2>/dev/null | head -n 1 | awk '{print $3}')"
    printf '%s' "${v#v}"
}

sb_do_install() {
    local force="${1:-}"
    sb_ensure_curl || return 1
    sb_ensure_user

    local cur="" ver="" ver_args="" did_install=0
    cur="$(sb_local_version)"
    if [[ -n "$cur" ]]; then
        sb_info "本地版本：v${cur}"
    else
        sb_info "本地版本：未安装"
    fi

    sb_info "正在获取最新版本号..."
    ver="$(sb_latest_version)"
    if [[ -n "$ver" ]]; then
        sb_info "最新版本：v${ver}"
        ver_args="--version ${ver}"
    else
        sb_warn "没能自动取到版本号，交给官方脚本自己查（可能遇到 API 限流）"
    fi

    # 版本已经一样就不重复下载了
    if [[ -n "$cur" && -n "$ver" && "$cur" == "$ver" && "$force" != "--force" ]]; then
        sb_info "已是最新版本 v${cur}，跳过下载"
        sb_info "如需强制重装：sb install --force"
    else
        if [[ -n "$cur" && -n "$ver" ]]; then
            if [[ "$cur" == "$ver" ]]; then
                sb_info "强制重装 v${cur}（官方脚本，走 gh-proxy 加速）..."
            else
                sb_info "发现新版本，开始升级 v${cur} -> v${ver} ..."
            fi
        elif [[ -z "$cur" ]]; then
            sb_info "开始安装 sing-box（官方脚本，走 gh-proxy 加速）..."
        else
            sb_info "开始重新安装（官方脚本，走 gh-proxy 加速）..."
        fi

        if ! curl -fsSL "$INSTALL_URL" \
            | sed 's#https://github.com/#https://gh-proxy.org/https://github.com/#' \
            | sh -s -- $ver_args; then
            sb_err "安装脚本执行失败，请检查网络后重试"
            sb_warn "若提示 API rate limit exceeded，说明 GitHub API 配额用完，"
            sb_warn "可等 1 小时再试，或用 GITHUB_TOKEN=xxx sb install 提高配额"
            return 1
        fi
        sb_info "sing-box 已就绪"
        did_install=1
    fi

    sb_info "设置开机自启..."
    systemctl enable "$SVC_NAME" || sb_warn "systemctl enable 失败，请稍后检查"

    sb_info "开启 IPv4/IPv6 转发..."
    mkdir -p /etc/sysctl.d
    printf 'net.ipv4.ip_forward=1\nnet.ipv6.conf.all.forwarding=1\n' > /etc/sysctl.d/99-sing-box.conf
    if sysctl --system >/dev/null 2>&1; then
        sb_info "net.ipv4.ip_forward=1 已生效"
    else
        sb_warn "sysctl --system 有报错，可手动执行 sysctl --system 查看"
    fi

    # 换过二进制就必须重启，否则进程里跑的还是旧版本（旧文件已被删，只是进程还占着）
    local rc=0
    if [[ "$did_install" -eq 1 ]] && systemctl is-active --quiet "$SVC_NAME"; then
        sb_info "重启服务以加载新版本..."
        sb_restart_service || rc=1
    elif [[ "$did_install" -eq 1 ]] && [[ -s "$CONF_FILE" ]]; then
        # 首次安装且已有配置（SeedRou 一键安装路径）：直接拉起
        sb_restart_service || rc=1
    elif [[ "$did_install" -eq 1 ]]; then
        sb_info "内核已就绪但还没有配置（${CONF_FILE} 不存在）"
        sb_info "下一步：设置订阅链接后执行 sb update 生成配置并自动启动"
    fi

    sb_info "第 1 项处理结束。如需导入节点，请选择第 2 项。"
    return "$rc"
}

# ---------------- 2. 更新配置 ----------------
sb_ensure_url() {
    mkdir -p "$CONF_DIR"
    [[ -s "$URL_FILE" ]] && return 0

    # cron 里没有终端，不能卡在等待输入
    if [[ ! -t 0 ]]; then
        sb_err "还没有保存订阅链接（${URL_FILE}），请先手动运行一次 sb 并选择第 2 项"
        return 1
    fi

    local input=""
    while :; do
        read -rp "请粘贴 Sub-Store 订阅链接: " input || return 1
        input="${input//[[:space:]]/}"
        [[ "$input" =~ ^https?:// ]] && break
        sb_warn "链接需要以 http:// 或 https:// 开头，请重新输入"
    done
    printf '%s\n' "$input" > "$URL_FILE"
    chmod 600 "$URL_FILE"
    sb_info "链接已保存到 ${URL_FILE}（以后不用再输入）"
}

sb_do_update() {
    if ! sb_ensure_url; then
        sb_record_update 0 "还没有保存订阅链接（${URL_FILE}）"
        return 1
    fi

    if ! command -v sing-box >/dev/null 2>&1; then
        sb_err "没有找到 sing-box 命令，请先执行第 1 项安装"
        sb_record_update 0 "没有找到 sing-box 命令，尚未安装内核"
        return 1
    fi

    local url tmp
    url="$(head -n 1 "$URL_FILE" | tr -d '\r' | tr -d '[:space:]')"
    tmp="$(mktemp /tmp/sing-box.XXXXXX.json)" || { sb_err "创建临时文件失败"; return 1; }

    sb_info "正在下载配置..."
    if ! curl -fsSL --connect-timeout 20 --retry 2 -A "sing-box" -o "$tmp" "$url"; then
        sb_err "下载失败，已保留原配置，服务未重启"
        rm -f "$tmp"
        sb_record_update 0 "订阅下载失败（网络或链接问题），已保留原配置"
        return 1
    fi
    if [[ ! -s "$tmp" ]]; then
        sb_err "下载到的是空文件，已保留原配置，服务未重启"
        rm -f "$tmp"
        sb_record_update 0 "订阅下载到空文件，已保留原配置"
        return 1
    fi

    sb_info "正在校验配置（sing-box check）..."
    if ! sing-box check -c "$tmp"; then
        sb_err "配置校验未通过，已保留原配置，服务未重启"
        rm -f "$tmp"
        sb_record_update 0 "订阅内容未通过 sing-box check 校验，已保留原配置"
        return 1
    fi

    install -m 0600 -- "$tmp" "$CONF_FILE"
    rm -f "$tmp"
    sb_info "新配置已写入 $CONF_FILE"

    if sb_restart_service; then
        sb_record_update 1 ""
    else
        sb_record_update 0 "新配置已写入但服务重启失败（见 journalctl -u sing-box）"
        return 1
    fi
}

# ---------------- 3. 替换订阅链接 ----------------
sb_set_url() {
    local given="${1:-}"
    mkdir -p "$CONF_DIR"

    # 非交互传参模式：sb seturl <URL>（面板/脚本用）
    if [[ -n "$given" ]]; then
        given="${given//[[:space:]]/}"
        if [[ ! "$given" =~ ^https?:// ]]; then
            sb_err "链接需要以 http:// 或 https:// 开头"
            return 1
        fi
        printf '%s\n' "$given" > "$URL_FILE"
        chmod 600 "$URL_FILE"
        sb_info "链接已保存到 ${URL_FILE}"
        return 0
    fi

    if [[ ! -t 0 ]]; then
        sb_err "替换链接需要交互输入，请在终端里运行 sb 后选择该项"
        return 1
    fi

    local old="" input=""
    if [[ -s "$URL_FILE" ]]; then
        old="$(head -n 1 "$URL_FILE")"
        echo "当前链接: $old"
    else
        echo "当前还没有保存订阅链接"
    fi

    while :; do
        read -rp "请输入新的 Sub-Store 链接（直接回车=取消）: " input || return 1
        input="${input//[[:space:]]/}"
        if [[ -z "$input" ]]; then
            sb_warn "已取消，链接没有改动"
            return 0
        fi
        [[ "$input" =~ ^https?:// ]] && break
        sb_warn "链接需要以 http:// 或 https:// 开头，请重新输入"
    done

    printf '%s\n' "$input" > "$URL_FILE"
    chmod 600 "$URL_FILE"
    sb_info "链接已更新，保存在 $URL_FILE"

    local ans="y"
    read -rp "现在就用新链接拉一次配置？[Y/n]: " ans || ans="n"
    if [[ ! "$ans" =~ ^[Nn] ]]; then
        sb_do_update
    fi
}

# ---------------- 4. 重启 ----------------
sb_do_restart() {
    sb_restart_service
}

# ---------------- 5. 看日志 ----------------
sb_show_log() {
    journalctl -u "$SVC_NAME" -n 50 --no-pager
}

# ---------------- 6. 每天 4 点自动更新（开关） ----------------
# 判断当前是否已开启：cron.d 文件存在且包含那条任务
sb_autoupdate_on() {
    [[ -f "$CRON_FILE" ]] && grep -qF '/usr/local/bin/sb update' "$CRON_FILE"
}

sb_enable_autoupdate() {
    local want="${1:-}" hh=4 mm=0
    if [[ -n "$want" ]]; then
        if [[ ! "$want" =~ ^([0-9]|0[0-9]|1[0-9]|2[0-3]):([0-5][0-9])$ ]]; then
            sb_err "时间格式应为 HH:MM（24 小时制），例如 06:30"
            return 1
        fi
        hh="${want%%:*}"; mm="${want##*:}"
        hh=$((10#$hh)); mm=$((10#$mm))
    fi
    if sb_autoupdate_on; then
        sb_info "自动更新已是开启状态；如需改时间请先 sb auto off 再 sb auto on HH:MM"
        return 0
    fi

    if ! command -v crontab >/dev/null 2>&1; then
        sb_info "未检测到 cron，正在安装..."
        apt-get update -y >/dev/null 2>&1 || true
        if ! apt-get install -y cron; then
            sb_err "cron 安装失败，请检查网络或软件源"
            return 1
        fi
    fi
    systemctl enable --now cron >/dev/null 2>&1 || sb_warn "cron 服务启动失败，请手动检查"

    [[ -x "$SB_BIN" ]] || sb_self_install

    # 原子写（tmp+mv），时间取自参数
    local tmpc
    tmpc="$(mktemp "${CRON_FILE}.tmp.XXXXXX")"
    {
        printf 'SHELL=/bin/sh\n'
        printf 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
        printf '%d %d * * * root /usr/local/bin/sb update\n' "$mm" "$hh"
    } > "$tmpc"
    chmod 644 "$tmpc"
    mv -f "$tmpc" "$CRON_FILE"

    sb_info "已开启：每天 $(printf '%02d:%02d' "$hh" "$mm") 自动更新配置（${CRON_FILE}）"
}

sb_disable_autoupdate() {
    if [[ ! -f "$CRON_FILE" ]]; then
        sb_info "自动更新本来就是关闭的"
        return 0
    fi
    rm -f "$CRON_FILE"
    sb_info "已关闭：每天 04:00 的自动更新（已删除 ${CRON_FILE}）"
}

# 菜单第 6 项：开着就关，关着就开
sb_toggle_autoupdate() {
    if sb_autoupdate_on; then
        sb_disable_autoupdate
    else
        sb_enable_autoupdate
    fi
}

# ---------------- 卸载（面板双确认后调用；控制台需 --yes） ----------------
sb_do_uninstall() {
    local sure="${1:-}"
    if [[ "$sure" != "--yes" ]]; then
        sb_info "将要卸载 sing-box：停止并禁用服务、删除每日自动更新、删除 ${SB_BIN} 与 ${CONF_DIR}（内核、订阅与全部节点数据）。"
        sb_info "卸载前会在 /var/lib/seedrou/ 留一份 ${CONF_DIR} 的 tar 备份。"
        if [[ ! -t 0 ]]; then
            sb_warn "非交互环境：请带 --yes 明确执行（sb uninstall --yes）"
            return 1
        fi
        local ans=""
        read -rp "确认卸载？输入 yes 继续: " ans || return 1
        [[ "$ans" == "yes" ]] || { sb_info "已取消"; return 1; }
    fi

    systemctl disable --now "$SVC_NAME" 2>/dev/null || true
    rm -f "$CRON_FILE"
    if [[ -d "$CONF_DIR" ]]; then
        mkdir -p /var/lib/seedrou
        local bak="/var/lib/seedrou/singbox-uninstall-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
        tar -czf "$bak" -C / etc/sing-box 2>/dev/null && chmod 600 "$bak" \
            && sb_info "原配置已备份：${bak}"
        rm -rf "$CONF_DIR"
    fi
    rm -f /etc/sysctl.d/99-sing-box.conf
    # A-05：不删 $SB_BIN——它是 SeedRou 下发的管理入口，不属于"内核+订阅数据"；
    # 删掉它面板就永远无法重装（全新安装机器点一次卸载即永久卡死）
    sb_info "sing-box 已卸载（SeedRou 面板将回到「未安装」状态，可随时一键重装）"
}

# ---------------- 状态面板 ----------------
sb_ver_text() {
    local v
    v="$(sb_local_version)"
    if [[ -n "$v" ]]; then
        printf 'v%s' "$v"
    elif command -v sing-box >/dev/null 2>&1; then
        printf '已安装（版本未知）'
    else
        printf '\033[33m未安装\033[0m'
    fi
}

sb_run_text() {
    local s
    s="$(systemctl is-active "$SVC_NAME" 2>/dev/null || true)"
    case "$s" in
        active)     printf '\033[32m运行中\033[0m' ;;
        inactive)   printf '\033[31m已停止\033[0m' ;;
        failed)     printf '\033[31m启动失败\033[0m' ;;
        activating) printf '\033[33m启动中\033[0m' ;;
        "")         printf '\033[90m未知（可能未安装）\033[0m' ;;
        *)          printf '\033[90m%s\033[0m' "$s" ;;
    esac
}

sb_boot_text() {
    local s
    s="$(systemctl is-enabled "$SVC_NAME" 2>/dev/null || true)"
    case "$s" in
        enabled)  printf '\033[32m已开启\033[0m' ;;
        disabled) printf '\033[33m未开启\033[0m' ;;
        "")       printf '\033[90m未知\033[0m' ;;
        *)        printf '%s' "$s" ;;
    esac
}

sb_url_text() {
    if [[ -s "$URL_FILE" ]]; then
        head -n 1 "$URL_FILE"
    else
        printf '\033[33m未设置（选第 3 项设置）\033[0m'
    fi
}

sb_auto_text() {
    if sb_autoupdate_on; then
        local t
        t="$(awk '/\/usr\/local\/bin\/sb update/{print $1" "$2; exit}' "$CRON_FILE" 2>/dev/null)"
        printf '\033[32m已开启（每天 %02d:%02d）\033[0m' "$((10#${t#* }))" "$((10#${t%% *}))"
    else
        printf '\033[33m已关闭\033[0m'
    fi
}

sb_show_status() {
    echo "============ sing-box 状态 ============"
    printf ' 版本     : %s\n' "$(sb_ver_text)"
    printf ' 运行状态 : %s\n' "$(sb_run_text)"
    printf ' 开机自启 : %s\n' "$(sb_boot_text)"
    printf ' 订阅链接 : %s\n' "$(sb_url_text)"
    printf ' 自动更新 : %s\n' "$(sb_auto_text)"
    echo "======================================="
}

# ---------------- 菜单 ----------------
sb_show_menu() {
    local choice="" auto_item=""
    while :; do
        if sb_autoupdate_on; then
            auto_item="关闭每天 04:00 自动更新配置"
        else
            auto_item="开启每天 04:00 自动更新配置"
        fi

        echo
        sb_show_status
        echo
        echo "============= sing-box 管理 ============="
        echo "  1) 安装 / 升级 sing-box"
        echo "  2) 更新配置（Sub-Store 订阅）"
        echo "  3) 替换 Sub-Store 订阅链接"
        echo "  4) 重启 sing-box"
        echo "  5) 查看最近 50 行日志"
        echo "  6) ${auto_item}"
        echo "  0) 退出"
        echo "========================================="
        read -rp "请输入序号: " choice || exit 0
        case "$choice" in
            1) sb_run_action "安装 / 升级 sing-box" sb_do_install ;;
            2) sb_run_action "更新配置（Sub-Store 订阅）" sb_do_update ;;
            3) sb_run_action "替换 Sub-Store 订阅链接" sb_set_url ;;
            4) sb_run_action "重启 sing-box" sb_do_restart ;;
            5) sb_run_action "查看最近 50 行日志" sb_show_log ;;
            6) sb_run_action "每日自动更新（切换开关）" sb_toggle_autoupdate ;;
            0|q|Q) echo "再见"; exit 0 ;;
            *) sb_warn "请输入 0-6 之间的数字" ;;
        esac
    done
}

# ---------------- 入口 ----------------
sb_require_root "$@"
sb_self_install

case "${1:-}" in
    ""|menu)   sb_show_menu ;;
    update)    sb_do_update ;;
    seturl|url) sb_set_url "${2:-}" ;;
    install)   sb_do_install "${2:-}" ;;
    restart)   sb_do_restart ;;
    log)       sb_show_log ;;
    status)    sb_show_status ;;
    uninstall) sb_do_uninstall "${2:-}" ;;
    auto)
        case "${2:-toggle}" in
            on|enable)   sb_enable_autoupdate "${3:-}" ;;
            off|disable) sb_disable_autoupdate ;;
            *)           sb_toggle_autoupdate ;;
        esac
        ;;
    -h|--help|help)
        echo "用法: sb            打开菜单"
        echo "      sb update     只更新配置（给 cron 用）"
        echo "      sb seturl [URL] 替换订阅链接（带 URL 参数则非交互保存）"
        echo "      sb uninstall [--yes] 卸载 sing-box（删除内核/订阅/节点，留备份）"
        echo "      sb install    安装 / 升级（已是最新则跳过下载）"
        echo "      sb install --force  强制重装当前最新版"
        echo "      sb restart    重启服务"
        echo "      sb log        查看日志"
        echo "      sb status     只看状态信息"
        echo "      sb auto       切换每天 4 点自动更新（开->关，关->开）"
        echo "      sb auto on    开启自动更新"
        echo "      sb auto off   关闭自动更新"
        ;;
    *)
        sb_err "未知参数: $1"
        echo "用法: sb [update|seturl|install|restart|log|status|auto [on|off]]"
        exit 1
        ;;
esac
