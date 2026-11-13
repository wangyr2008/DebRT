"""`priv/backupops.py` 的行为测试（v0.1.86 / P2-6）。

这三条路径都是**不可逆**的：恢复会覆盖 /etc/dert，出厂重置会清宽带密码并重建
配置。所以这里重点锁三类行为：

  1. 拒绝条件必须**先判后动**——含 secrets、含链接条目、结构不合法的包，
     一个字节都不能落到数据根下。
  2. 备份不含 secrets 的后果要**如实反映**到 `wan.has_password`，否则界面会
     显示"已设密码"而 pppd 手里没有凭据。
  3. 出厂重置**保留升级源**、清掉宽带密码、保留管理密码。

全部用 DERT_FSROOT 指向临时根，不碰真机 /etc。
"""
import importlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout

import support


class Sandbox:
    """临时数据根 + 干净的模块实例（privlib 在导入时读 DERT_FSROOT）。"""

    def __init__(self):
        self.td = tempfile.mkdtemp(prefix="dert-bk-")
        self.old = os.environ.get("DERT_FSROOT")
        os.environ["DERT_FSROOT"] = self.td
        for m in ("backupops", "privlib", "backupcheck"):
            sys.modules.pop(m, None)
        if support.PRIV not in sys.path:
            sys.path.insert(0, support.PRIV)
        self.b = importlib.import_module("backupops")
        # 测试不要求 root：权限检查是 bash 入口的职责，模块内这层在这里放行
        self.b.privlib.require_root = lambda: None
        self.cfg = os.path.join(self.td, "etc/dert/config.json")
        self.sec = os.path.join(self.td, "etc/dert/secrets.json")
        os.makedirs(os.path.dirname(self.cfg), exist_ok=True)
        self.applied = []
        self.b._apply_all = lambda: (self.applied.append(1) or (True, ""))

    def write_cfg(self, cfg):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def write_sec(self, sec):
        with open(self.sec, "w", encoding="utf-8") as f:
            json.dump(sec, f)

    def make_pkg(self, entries, links=None, name="b.tar.gz"):
        """按 {成员名: 内容} 打包；links 为 {成员名: 指向} 的软链条目。"""
        path = os.path.join(self.td, name)
        with tarfile.open(path, "w:gz") as tf:
            for member, text in entries.items():
                data = text.encode("utf-8")
                info = tarfile.TarInfo(member)
                info.size = len(data)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(data))
            for member, target in (links or {}).items():
                info = tarfile.TarInfo(member)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                tf.addfile(info)
        return path

    def close(self):
        if self.old is None:
            os.environ.pop("DERT_FSROOT", None)
        else:
            os.environ["DERT_FSROOT"] = self.old
        for m in ("backupops", "privlib", "backupcheck"):
            sys.modules.pop(m, None)


def run(fn, *a):
    """跑一个 CLI 动作，返回 (退出码, 输出 JSON 或原文)。"""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*a)
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    out = buf.getvalue()
    try:
        return rc, json.loads(out.strip().split("\n")[-1])
    except Exception:
        return rc, out


class RestoreGuard(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.write_cfg(support.dhcp_config())
        self.s.write_sec({"session_secret": "x"})

    def tearDown(self):
        self.s.close()

    def test_secrets_package_rejected(self):
        pkg = self.s.make_pkg({
            "etc/dert/config.json": json.dumps(support.dhcp_config()),
            "etc/dert/secrets.json": "{}",
        })
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "BACKUP_SECRETS")
        self.assertEqual(self.s.applied, [])

    def test_dot_slash_secrets_still_rejected(self):
        # 归一前会漏掉的变体（复审 N-01）
        pkg = self.s.make_pkg({
            "etc/dert/config.json": json.dumps(support.dhcp_config()),
            "etc/dert/./secrets.json": "{}",
        })
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "BACKUP_SECRETS")

    def test_outside_whitelist_rejected(self):
        pkg = self.s.make_pkg({
            "etc/dert/config.json": json.dumps(support.dhcp_config()),
            "etc/shadow": "x",
        })
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "BACKUP_BAD")

    def test_symlink_entry_rejected(self):
        pkg = self.s.make_pkg(
            {"etc/dert/config.json": json.dumps(support.dhcp_config())},
            links={"etc/dert/hostname": "/etc/hostname"})
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "BACKUP_BAD")
        self.assertIn("链接", out["message"])

    def test_empty_package_rejected(self):
        pkg = self.s.make_pkg({})
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "BACKUP_BAD")

    def test_truncated_package_rejected(self):
        pkg = os.path.join(self.s.td, "broken.tar.gz")
        with open(pkg, "wb") as f:
            f.write(b"\x1f\x8b\x08\x00 not really gzip")
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "BACKUP_BAD")

    def test_missing_file(self):
        rc, out = run(self.s.b.backup_restore, os.path.join(self.s.td, "nope.tar.gz"))
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "NO_FILE")


class RestoreApply(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.write_cfg(support.dhcp_config())
        self.s.write_sec({"session_secret": "keep-me"})

    def tearDown(self):
        self.s.close()

    def test_files_deployed_and_secrets_untouched(self):
        pkg = self.s.make_pkg({
            "etc/dert/config.json": json.dumps(support.dhcp_config()),
            "etc/dert/compose.yaml": "services: {}\n",
        })
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])
        with open(os.path.join(self.s.td, "etc/dert/compose.yaml"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "services: {}\n")
        # 备份不含 secrets：原有 secrets 必须还在（恢复不能顺手清掉管理密码）
        with open(self.s.sec, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["session_secret"], "keep-me")
        self.assertEqual(len(self.s.applied), 1)

    def test_has_password_follows_secrets(self):
        # 备份里的配置写 has_password=true，但 secrets 里没有宽带密码 → 必须纠正
        cfg = support.dhcp_config()
        cfg["wan"]["has_password"] = True
        pkg = self.s.make_pkg({"etc/dert/config.json": json.dumps(cfg)})
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 0, out)
        with open(self.s.cfg, encoding="utf-8") as f:
            self.assertFalse(json.load(f)["wan"]["has_password"])

    def test_pppoe_without_password_warns(self):
        cfg = support.pppoe_config()
        pkg = self.s.make_pkg({"etc/dert/config.json": json.dumps(cfg)})
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 0, out)
        self.assertIn("宽带密码", out["message"])

    def test_bad_schema_rejected_before_write(self):
        # 结构不合法（网口列表为空）→ netgen schema 拦下，配置不被覆盖
        bad = support.dhcp_config()
        bad["ports"] = []
        pkg = self.s.make_pkg({"etc/dert/config.json": json.dumps(bad)})
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        with open(self.s.cfg, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["ports"])  # 仍是原来的配置

    def test_apply_failure_propagates(self):
        self.s.b._apply_all = lambda: (False, '{"ok": false, "code": "NETGEN"}')
        pkg = self.s.make_pkg({"etc/dert/config.json": json.dumps(support.dhcp_config())})
        rc, out = run(self.s.b.backup_restore, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "NETGEN")  # 收敛失败的细节要原样透传


class BackupCreate(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_package_excludes_secrets(self):
        self.s.write_cfg(support.dhcp_config())
        self.s.write_sec({"pppoe_password": "secret"})
        out_path = os.path.join(self.s.td, "out.tar.gz")
        rc, out = run(self.s.b.backup_create, out_path)
        self.assertEqual(rc, 0, out)
        with tarfile.open(out_path) as tf:
            names = tf.getnames()
        self.assertIn("etc/dert/config.json", names)
        self.assertNotIn("etc/dert/secrets.json", names)
        self.assertEqual(os.stat(out_path).st_mode & 0o777, 0o640)

    def test_missing_output_path(self):
        rc, out = run(self.s.b.backup_create, "")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "USAGE")


class Factory(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_keeps_upgrade_source_drops_pppoe_password(self):
        cfg = support.pppoe_config()
        cfg["upgrade"] = {"source": "https://example.com/manifest.json"}
        self.s.write_cfg(cfg)
        self.s.write_sec({"pppoe_password": "pw", "passwd_hash": "pbkdf2$sha256$1$a$b"})
        rc, out = run(self.s.b.factory_reset)
        self.assertEqual(rc, 0, out)
        with open(self.s.cfg, encoding="utf-8") as f:
            new = json.load(f)
        self.assertEqual(new["upgrade"]["source"], "https://example.com/manifest.json")
        self.assertEqual(new["lan"]["ip"], "192.168.10.1")
        self.assertEqual(new["wan"]["mode"], "dhcp")
        self.assertFalse(new["wan"]["has_password"])
        with open(self.s.sec, encoding="utf-8") as f:
            sec = json.load(f)
        self.assertNotIn("pppoe_password", sec)
        self.assertIn("passwd_hash", sec)  # 管理密码保留
        self.assertEqual(len(self.s.applied), 1)

    def test_pre_factory_backup_written(self):
        self.s.write_cfg(support.dhcp_config())
        rc, _ = run(self.s.b.factory_reset)
        self.assertEqual(rc, 0)
        d = os.path.join(self.s.td, "var/lib/dert/lastgood")
        self.assertTrue([f for f in os.listdir(d) if f.startswith("pre-factory-")])

    def test_default_ports_from_sysfs(self):
        fake = os.path.join(self.s.td, "sysfs")
        os.makedirs(fake)
        for n in ("enp1s0", "enp2s0", "lo", "br-lan", "veth123", "ppp0"):
            os.makedirs(os.path.join(fake, n))
        ports = self.s.b.default_ports(fake)
        self.assertEqual([p["name"] for p in ports], ["enp1s0", "enp2s0"])
        self.assertTrue(ports[0]["wan"])
        self.assertFalse(ports[0]["lan"])
        self.assertTrue(ports[1]["lan"])

    def test_default_ports_fallback(self):
        ports = self.s.b.default_ports(os.path.join(self.s.td, "absent"))
        self.assertEqual([p["name"] for p in ports], ["eth0", "eth1"])


if __name__ == "__main__":
    unittest.main()
