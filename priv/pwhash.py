#!/usr/bin/env python3
"""SeedRou 管理密码哈希 —— 唯一实现（auth-verify / passwd-set / install.sh 共用）。

记录格式（admin_pbkdf2 字段）：
  新  pbkdf2$sha256$<iters>$<salt-hex>$<hash-hex>   （算法显式）
  旧  pbkdf2$<iters>$<salt-hex>$<hash-hex>          （兼容 v0.1.4 及更早安装）
历史缺陷（五审 P-02 → 六审 P-02）：旧实现把首段标签 "pbkdf2" 当成摘要算法名传给
pbkdf2_hmac，必抛 UnsupportedDigestmodError → 真实模式永远登录失败。本模块是它的最终修复。
"""
import hashlib
import hmac
import os


def parse_record(rec: str):
    """解析记录，返回 (iters, salt_hex, want_hex)；格式不合法抛 ValueError。"""
    p = rec.split("$")
    if len(p) == 5:                      # pbkdf2$sha256$iters$salt$hash
        fmt, algo, iters, salt, want = p
        if fmt != "pbkdf2" or algo != "sha256":
            raise ValueError("unsupported record")
    elif len(p) == 4:                    # pbkdf2$iters$salt$hash（旧格式）
        fmt, iters, salt, want = p
        if fmt != "pbkdf2":
            raise ValueError("unsupported record")
    else:
        raise ValueError("unsupported record")
    return int(iters), salt, want


def verify(pw: str, rec: str) -> bool:
    iters, salt, want = parse_record(rec)
    got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iters).hex()
    return hmac.compare_digest(got, want)


def hash_new(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 600000).hex()
    return f"pbkdf2$sha256$600000${salt.hex()}${h}"
