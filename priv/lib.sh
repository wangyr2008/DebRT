#!/usr/bin/env bash
# DebRT 特权库 —— 仅被 dert-priv / 钩子脚本 source。
# 约定：所有用户可见错误一律以 JSON 输出到 stdout（die），可操作（原因 + 下一步）。
# 原子写：同目录临时文件 → fsync → rename，任何时刻磁盘上都只有完整文件。
#
# set -u：本文件被 source，因此这条会作用到调用方的 shell。当前三个调用方
# （dert-priv / qdisc-apply / wan-addr-sync）本来就都有 set -u，所以实际行为不变；
# 写在这里是为了让"直接 source 本文件调试"时也不会静默吞掉未定义变量。
set -u

# 代码路径（不随测试缝变化）：这些是"程序在哪"，不是"数据在哪"
DERT_ROOT="/usr/local/lib/dert"
DERT_SHARE="${DERT_ROOT}/share"

# 数据路径：v0.1.78 起支持测试缝。
#   FSROOT="${DERT_FSROOT:-}" 未设时是空串 → "${FSROOT}/etc/dert" 与 "/etc/dert"
#   逐字符等价，生产行为一字不变；设了就指向临时根，让原子写/快照/清理这些
#   逻辑能在无 root 环境里被真正跑一遍（P2 拆 dert-priv 的安全网）。
FSROOT="${DERT_FSROOT:-}"
DERT_ETC="${FSROOT}/etc/dert"
DERT_VAR="${FSROOT}/var/lib/dert"
DERT_RUN="${FSROOT}/run/dert"

# 导出给内联 Python 用：heredoc 是 <<'PYEOF' 形式，shell 不展开变量，
# 所以 Python 侧统一用 os.environ["DERT_ETC"] 取路径。
export DERT_ETC DERT_VAR DERT_RUN
CONFIG_JSON="${DERT_ETC}/config.json"
SECRETS_JSON="${DERT_ETC}/secrets.json"
VERSION_FILE="${DERT_SHARE}/VERSION"
LOCK_FILE="${DERT_RUN}/apply.lock"

PANEL_UNIT="dert-panel.service"
NFT_TABLE="inet dert_base"

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

# ---------------- 锁 ----------------
# 幂等：已持锁（嵌套调用，如 backup-restore 内部再跑 apply-all）直接返回，避免释放重取的竞态窗口
acquire_lock() {
  [ -n "${_DERT_LOCK_HELD:-}" ] && return 0
  mkdir -p "$DERT_RUN" 2>/dev/null || true
  exec 9>"$LOCK_FILE" || die "LOCK" "无法打开锁文件 $LOCK_FILE"
  if ! flock -n 9; then
    die "BUSY" "另一项配置变更正在进行中" "请稍等几秒后重试；若频繁出现请检查是否有其他管理员同时在操作"
  fi
  _DERT_LOCK_HELD=1
}

# ---------------------------------------------------------------------------
# 说明（v0.1.88）：这里只剩「协议 + 权限 + 锁」。
#
# 曾经的 apply 流水线（apply_stage_files / preflight_stage / run_actions /
# svc_* / wait_wan / plan_for_section / snapshot_lastgood / cleanup_* 等）
# 已整体搬进 priv/applyops.py 与 priv/configops.py。**故意不保留 bash 副本**：
# 同一套判断留两份实现，迟早漂移——本项目已经因为"清理条件与生成器各写一遍"
# 出过一次 LAN→WAN 的端口角色事故（v0.1.71）。
#
# 留在 bash 的只有三件事：子命令分发、root 检查、排他锁。锁必须留在这一层，
# 因为 exec 之后子进程继承 fd 9，_DERT_LOCK_HELD 让嵌套调用跳过重取，
# 这样"配置变更串行化"的语义与拆分前逐字一致。
# ---------------------------------------------------------------------------
