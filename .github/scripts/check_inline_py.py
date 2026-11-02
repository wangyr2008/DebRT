#!/usr/bin/env python3
"""扫描 shell 脚本里的内嵌 python heredoc（python3 - <<'MARK'），逐段 py_compile。
十一审 P1 的教训：bash -n 看不见 heredoc 里的 Python，语法错误会静默吞掉整个函数。"""
import glob
import os
import re
import subprocess
import sys
import tempfile

files = []
for pat in ("priv/*", "install.sh", "uninstall.sh", "network/ppp/*/*", "network/dispatcher/*"):
    files += [f for f in sorted(glob.glob(pat)) if os.path.isfile(f)]

pat = re.compile(r"python3\s+-\s*\S*\s*<<'(\w+)'\n(.*?)\n\1\b", re.S)
bad, total = [], 0
for f in files:
    src = open(f, encoding="utf-8").read()
    for m in pat.finditer(src):
        total += 1
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
            tf.write(m.group(2))
            path = tf.name
        r = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True)
        os.unlink(path)
        if r.returncode:
            err = (r.stderr or "compile failed").strip().splitlines()[-1]
            bad.append((f, err))
        elif r.stderr and r.stderr.strip():
            bad.append((f, "stderr 非空: " + r.stderr.strip().splitlines()[-1]))

print(f"检查了 {total} 段内嵌 Python")
for f, err in bad:
    print(f"::error file={f}::{err}")
sys.exit(1 if bad else 0)
