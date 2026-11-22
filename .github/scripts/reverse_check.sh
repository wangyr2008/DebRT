#!/bin/bash
# 反向验证 P3-48 的每一条：破坏 → 守卫必须变红 → 恢复 → 必须变绿
# 每个文件先备份到独立的临时目录，恢复后校验语法，避免再用到上一轮的残留文件。
set -u
cd "$(dirname "$0")/../.." || exit 1
D=$(mktemp -d /tmp/dert-rev.XXXXXX) || exit 1
trap 'rm -rf "$D"' EXIT

fail=0
check() { # $1=label  $2=file  $3=perl 表达式
  local label="$1"
  local f="$2"
  local expr="$3"
  local bak
  bak="$D/$(echo "$f" | tr / _)"
  cp "$f" "$bak" || { echo "  $label → 备份失败"; fail=1; return; }
  perl -pi -e "$expr" "$f"
  if cmp -s "$f" "$bak"; then
    echo "  $label → ⚠️ 破坏没生效（perl 表达式没匹配）"
    fail=1
  else
    python3 .github/scripts/test_guards.py >/dev/null 2>&1
    if [ $? -eq 0 ]; then echo "  $label → ❌ 守卫没抓到"; fail=1
    else echo "  $label → ✅ 抓到"; fi
  fi
  cp "$bak" "$f"
  cmp -s "$f" "$bak" || { echo "     ⚠️ 恢复失败"; fail=1; }
}

check "1  N1 用旧 LAN"        priv/validate.py    's/_lan_src = \(result if section == "lan" else cur\.get\("lan"\)\) or \{\}/_lan_src = cur.get("lan") or {}/'
check "2  N6 删表漏族"        priv/singbox-cleanup 's/nft delete table inet sing-box/nft delete table sing-box/'
check "3  备份路径不限制"      priv/backupops.py   's/"BACKUP_PATH"/"X_PATH"/'
check "4  N4 不查服务"        priv/applyops.py    's/_bad = degraded_services\(\)/_bad = []/'
check "5  N3 无 forward 链"   priv/applyops.py    's/"  chain forward \{/"  chain fwd {/'
check "6  N5 非事务"          priv/wan-addr-sync  's/\| nft -f -/| cat >\/dev\/null/'
check "7  N2 只看一条"        priv/status.py      's/def _pd_prefixlens\(\)/def _pd_prefixlen_one()/'
check "8  验签默认关"          priv/upgradeops.py  's/get\("require_signature", True\)/get("require_signature")/'
check "9  7890 不拦"          priv/netgen.py      's/dport 7890 drop/dport 7890 accept/g'
check "10 不报 API 风险"      priv/status.py      's/"singbox_api": _api_risk/"sb_api": _api_risk/'
check "11 备份目录用 realpath"  priv/backupops.py      's/os\.O_NOFOLLOW/os.O_RDONLY/'
check "12 上传不收签名"        panel/api.py          's/X-Sig-B64/X-Sig-XX/'
check "13 片段不前置 ts 规则"   priv/dns-split-prepare 's/"rules": extra \+ \[/"rules": [/'
check "14 不刷新片段"          priv/applyops.py      's/^\s+refresh_dns_fragment\(\)\s*$//'

echo "--- 恢复后整体 ---"
python3 -m py_compile priv/validate.py priv/applyops.py priv/status.py priv/backupops.py priv/upgradeops.py priv/netgen.py \
  && bash -n priv/singbox-cleanup && bash -n priv/wan-addr-sync && echo "  语法 OK"
python3 .github/scripts/test_guards.py >/dev/null 2>&1
echo "  守卫退出码: $?"
exit $fail
