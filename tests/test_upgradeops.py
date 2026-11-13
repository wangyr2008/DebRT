"""`priv/upgradeops.py` 的行为测试（v0.1.89 / P2-9）。

升级通道是**唯一会替换自身**的路径，所以这里重点锁"拒绝"而不是"成功"：

  1. 降级必须拒（同版本重装允许）；
  2. 成员白名单必须拦路径穿越，链接条目必须拒；
  3. 缺少 VERSION / panel/ / priv/ 必须拒；
  4. 清单里的 url 只接受 https（curl 会照做 file://）；
  5. sha256 对不上必须拒。

**成功路径不在这里做单测**：它会把 /usr/local/lib/dert 整个换掉、还会写 /etc。
那一步的验收方式是"每次发版都在真机上真的升一次级"——比单测更接近事实。
这里只把"换目录之前"的判断全部覆盖住。
"""
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout

import support

FAKE_CURL = """#!/bin/sh
out=""; prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
url=""
for a in "$@"; do
  case "$a" in http*|file*) url="$a";; esac
done
echo "curl $url" >> "$FAKELOG"
if [ -n "$out" ]; then
  case "$url" in
    *.sha256|*.sig) [ -f "$FAKEDIR/small" ] || exit 22 ;;
  esac
  if [ -f "$FAKEDIR/asset" ]; then cp "$FAKEDIR/asset" "$out"; else exit 22; fi
else
  if [ -f "$FAKEDIR/body" ]; then cat "$FAKEDIR/body"; else exit 22; fi
fi
"""


class Sandbox:
    """临时安装根 + 假 curl。

    DERT_LIB 指向临时目录，所以"当前版本"与"换目录"都发生在沙箱里；
    但 apply 的成功路径仍会碰 /etc，因此测试只走到失败分支为止。
    """

    def __init__(self, version="0.1.88", source="acme/dert"):
        self.td = tempfile.mkdtemp(prefix="dert-upg-")
        self.old = {k: os.environ.get(k) for k in ("DERT_LIB", "DERT_FSROOT", "PATH")}
        self.lib = os.path.join(self.td, "lib")
        os.makedirs(self.lib)
        with open(os.path.join(self.lib, "VERSION"), "w", encoding="utf-8") as f:
            f.write(version + "\n")
        os.environ["DERT_LIB"] = self.lib
        os.environ["DERT_FSROOT"] = self.td

        self.bindir = os.path.join(self.td, "bin")
        os.makedirs(self.bindir)
        self.log = os.path.join(self.td, "calls.log")
        os.environ["FAKELOG"] = self.log
        os.environ["FAKEDIR"] = self.bindir
        p = os.path.join(self.bindir, "curl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(FAKE_CURL)
        os.chmod(p, 0o755)
        os.environ["PATH"] = self.bindir + ":" + (self.old["PATH"] or "")

        for m in ("upgradeops", "privlib"):
            sys.modules.pop(m, None)
        if support.PRIV not in sys.path:
            sys.path.insert(0, support.PRIV)
        import upgradeops
        self.u = upgradeops
        self.u.privlib.require_root = lambda: None
        self.write_cfg({"upgrade": {"source": source}})

    def write_cfg(self, cfg):
        d = os.path.join(self.td, "etc/dert")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def body(self, text):
        with open(os.path.join(self.bindir, "body"), "w", encoding="utf-8") as f:
            f.write(text)

    def asset(self, path):
        shutil.copyfile(path, os.path.join(self.bindir, "asset"))

    def close(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for m in ("upgradeops", "privlib"):
            sys.modules.pop(m, None)
        shutil.rmtree(self.td, ignore_errors=True)


def run(fn, *a):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            fn(*a)
        rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    out = buf.getvalue().strip()
    try:
        return rc, json.loads(out.split("\n")[-1])
    except Exception:
        return rc, out


def make_pkg(path, version="0.1.90", extra=None, links=None, omit=()):
    """造一个最小的合法升级包。"""
    files = {
        "VERSION": version + "\n",
        "panel/api.py": "import json\n",
        "panel/static/index.html": "<html></html>\n",
        "panel/static/app.js": "//\n",
        "panel/static/app.css": "/* */\n",
        "panel/static/icons.js": "//\n",
        "panel/static/favicon.ico": "x\n",
        "priv/dert-priv": "#!/usr/bin/env bash\ntrue\n",
    }
    files.update(extra or {})
    with tarfile.open(path, "w:gz") as tf:
        for name, body in files.items():
            if any(name.startswith(o) for o in omit):
                continue
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return path


class Version(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_compare(self):
        ge = self.s.u.ver_ge
        self.assertTrue(ge("0.1.88", "0.1.88"))
        self.assertTrue(ge("0.1.90", "0.1.88"))
        self.assertFalse(ge("0.1.9", "0.1.10"))     # 按数字比，不是字典序
        self.assertTrue(ge("v0.2", "0.1.99"))
        self.assertTrue(ge("1.0", "1.0.0"))

    def test_cur_version_from_lib(self):
        self.assertEqual(self.s.u.cur_version(), "0.1.88")


class Check(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_no_source(self):
        self.s.write_cfg({"upgrade": {}})
        rc, out = run(self.s.u.check)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_NO_SOURCE")

    def test_manifest_newer(self):
        self.s.write_cfg({"upgrade": {"source": "https://example.com/m.json"}})
        self.s.body('{"version": "0.1.90"}')
        rc, out = run(self.s.u.check)
        self.assertEqual(rc, 0, out)
        self.assertFalse(out["up_to_date"])
        self.assertEqual(out["latest"], "0.1.90")

    def test_manifest_same_version(self):
        self.s.write_cfg({"upgrade": {"source": "https://example.com/m.json"}})
        self.s.body('{"version": "0.1.88"}')
        rc, out = run(self.s.u.check)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["up_to_date"])

    def test_manifest_bad_format(self):
        self.s.write_cfg({"upgrade": {"source": "https://example.com/m.json"}})
        self.s.body("not json")
        rc, out = run(self.s.u.check)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_MANIFEST")


class Download(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.write_cfg({"upgrade": {"source": "https://example.com/m.json"}})

    def tearDown(self):
        self.s.close()

    def test_non_https_asset_rejected(self):
        # curl 会照做 file://，被篡改的清单可借此把本地文件当升级包
        self.s.body('{"url": "file:///etc/shadow", "sha256": "x"}')
        rc, out = run(self.s.u.download, "0.1.90")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_MANIFEST")
        self.assertIn("https", out["message"])

    def test_missing_sha256_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"))
        self.s.asset(pkg)
        self.s.body('{"url": "https://example.com/dert-0.1.90.tar.gz"}')
        rc, out = run(self.s.u.download, "0.1.90")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_MANIFEST")
        self.assertIn("sha256", out["message"])

    def test_sha_mismatch_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"))
        self.s.asset(pkg)
        self.s.body('{"url": "https://example.com/x.tar.gz", "sha256": "%s"}' % ("0" * 64))
        rc, out = run(self.s.u.download, "0.1.90")
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_SHA")

    def test_sha_match_ok(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"))
        self.s.asset(pkg)
        import hashlib
        with open(pkg, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.s.body('{"url": "https://example.com/x.tar.gz", "sha256": "%s"}' % digest)
        rc, out = run(self.s.u.download, "0.1.90")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])


class ApplyGuards(unittest.TestCase):
    """换目录之前的全部拒绝分支。"""

    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_downgrade_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"), version="0.1.10")
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_DOWNGRADE")

    def test_same_version_reinstall_allowed(self):
        # 同版本重装允许（L3：目标已存在时 cp -a 会嵌套成 app-<ver>/dert/）
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"), version="0.1.88")
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 0, out)

    def test_swap_and_flatten_layout(self):
        """换目录 + 布局对齐：包里的 priv/* 必须铺到安装根。

        这一条对应真机踩过的坑：install.sh 是扁平布局，升级包保持 priv/ 子目录，
        不铺回根的话 sudo 调用的 /usr/local/lib/dert/dert-priv 直接消失。
        """
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"), version="0.1.89")
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.s.lib, "VERSION"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "0.1.89")
        self.assertTrue(os.path.isfile(os.path.join(self.s.lib, "dert-priv")),
                        "priv/* 没有铺回安装根")
        self.assertFalse(os.path.isdir(os.path.join(self.s.lib, "priv")),
                         "扁平化后不该保留 priv/ 子目录（会有「哪份生效」的歧义）")
        self.assertFalse([d for d in os.listdir(os.path.join(self.s.td))
                          if d.startswith("lib.prev.")], "旧目录未清理")

    def test_path_traversal_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"),
                       extra={"../evil.sh": "rm -rf /\n"})
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")

    def test_unknown_top_dir_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"),
                       extra={"weird/dir/file": "x\n"})
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")

    def test_symlink_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"),
                       links={"priv/hostname": "/etc/hostname"})
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")
        self.assertIn("链接", out["message"])

    def test_missing_version_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"), omit=("VERSION",))
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")

    def test_missing_panel_dir_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"), omit=("panel/",))
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")
        self.assertIn("panel", out["message"])

    def test_missing_priv_dir_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"), omit=("priv/",))
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")
        self.assertIn("priv", out["message"])

    def test_broken_python_in_package_refused(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"),
                       extra={"priv/broken.py": "def f(:\n"})
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")
        self.assertIn("broken.py", out["message"])

    def test_corrupt_archive_refused(self):
        p = os.path.join(self.s.td, "bad.tar.gz")
        with open(p, "wb") as f:
            f.write(b"not a tarball")
        rc, out = run(self.s.u.apply, p)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_BAD")

    def test_missing_package(self):
        rc, out = run(self.s.u.apply, os.path.join(self.s.td, "nope.tar.gz"))
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "NO_FILE")

    def test_signature_required_but_absent(self):
        self.s.write_cfg({"upgrade": {"source": "acme/dert", "require_signature": True}})
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"))
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_SIG")

    def test_signature_present_but_no_pubkey(self):
        pkg = make_pkg(os.path.join(self.s.td, "p.tar.gz"))
        with open(pkg + ".sig", "wb") as f:
            f.write(b"fake-signature")
        rc, out = run(self.s.u.apply, pkg)
        self.assertEqual(rc, 1)
        self.assertEqual(out["code"], "UPG_SIG")
        self.assertIn("公钥", out["message"])


if __name__ == "__main__":
    unittest.main()
