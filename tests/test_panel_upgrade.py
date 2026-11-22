"""面板「手动上传升级包」的签名传递（v0.1.134 / 审计 §2.2）。

v0.1.133 把验签改成默认强制之后，上传接口只收 `.tar.gz`、不可能带 `.sig`，
于是手动升级**一律**报 UPG_SIG——在线升级正常、手动升级不可用，属于功能回归。
现在前端把 `.sig` 一起读成 base64 放在 `X-Sig-B64` 里，后端落在包旁边，
`upgrade-apply` 的 `_verify_signature()` 按 `<pkg>.sig` 取用。

这里只测「签名有没有正确落到包旁边」与「坏输入是否被挡在 priv 之前」，
不测升级本身（那在 test_upgradeops 里）。
"""
import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "panel"))

import api  # noqa: E402


class UploadSig(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="upgsig-")
        self.pkg = os.path.join(self.td, "dert-9.9.9.tar.gz")
        self.calls = []

        def fake_save_upload(body, prefix):
            with open(self.pkg, "wb") as f:
                f.write(body)
            return self.pkg

        def fake_priv(*args, **kw):
            # 记录「调用那一刻包旁边有没有 .sig、内容对不对」
            sig = self.pkg + ".sig"
            seen = None
            if os.path.isfile(sig):
                with open(sig, "rb") as f:
                    seen = f.read()
            self.calls.append({"args": args, "sig": seen})
            return {"ok": True, "message": "已升级"}

        self._orig = (api._save_upload, api.priv)
        api._save_upload = fake_save_upload
        api.priv = fake_priv

    def tearDown(self):
        api._save_upload, api.priv = self._orig

    def _post(self, headers=None):
        return api.post_upgrade_upload(b"fake-tar", headers or {"X-Filename": "p.tar.gz"})

    def test_sig_written_next_to_package(self):
        raw = b"S" * 256
        code, out, _ = self._post({"X-Filename": "p.tar.gz",
                                   "X-Sig-B64": base64.b64encode(raw).decode()})
        self.assertEqual(code, 200)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["sig"], raw,
                         "upgrade-apply 运行时 .sig 必须已经在包旁边")
        self.assertFalse(os.path.exists(self.pkg + ".sig"), "用完要删掉")

    def test_no_sig_still_calls_apply(self):
        """没带签名时交给 upgrade-apply 判定（配置允许时仍可安装）。"""
        self._post()
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0]["sig"])

    def test_bad_base64_rejected_before_priv(self):
        with self.assertRaises(api.ApiError) as cm:
            self._post({"X-Filename": "p.tar.gz", "X-Sig-B64": "not base64!!"})
        self.assertEqual(cm.exception.code, "BAD_SIG")
        self.assertEqual(self.calls, [], "坏签名不能进入升级流程")

    def test_oversized_sig_rejected(self):
        big = base64.b64encode(b"x" * 20000).decode()
        with self.assertRaises(api.ApiError) as cm:
            self._post({"X-Filename": "p.tar.gz", "X-Sig-B64": big})
        self.assertEqual(cm.exception.code, "BAD_SIG")
        self.assertEqual(self.calls, [])

    def test_empty_sig_treated_as_absent(self):
        self._post({"X-Filename": "p.tar.gz", "X-Sig-B64": "   "})
        self.assertIsNone(self.calls[0]["sig"])


if __name__ == "__main__":
    unittest.main()
