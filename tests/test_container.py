"""`priv/container.py` 的行为测试（v0.1.82 / P2-2）。

容器域从 `dert-priv` 抽出后，可以用**打桩**的方式验证编排逻辑：
替换 `privlib.run/run_out` 返回构造好的 podman 输出，断言模块**做了什么**
（调了哪些命令、输出什么 JSON），而不去启动真容器。

重点覆盖两条真机踩过的坑：
  · `list-images` 必须按**镜像 ID** 匹配使用状态（按名字会误报"未使用"）
  · `container update` 遇到自定义网络 / privileged 必须**前置拒绝**
    （否则重建"成功"但配置丢了，用户到连不上才发现）
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

import support


def load_container():
    if support.PRIV not in sys.path:
        sys.path.insert(0, support.PRIV)
    import privlib  # noqa: E402
    import container  # noqa: E402
    return privlib, container


class Base(unittest.TestCase):
    def setUp(self):
        self.P, self.C = load_container()
        # 测试机不是 root；真正的 require_root 在 main() 里，这里直接调 act_*
        self.calls = []
        self.outputs = {}

    def stub(self, mapping, default=""):
        """把 run_out 换成按命令前缀返回构造好的输出；记录所有调用。"""
        def fake_run_out(cmd, timeout=30):
            key = " ".join(cmd)
            self.calls.append(key)
            for prefix, val in mapping.items():
                if key.startswith(prefix):
                    return val
            return default
        self.P.run_out = fake_run_out

    def stub_rc(self, rc_map):
        """run() 的返回码控制（用于 pull/create 等失败分支）。"""
        class R:
            def __init__(self, rc, out=""):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""
        def fake_run(cmd, timeout=30, **kw):
            key = " ".join(cmd)
            self.calls.append(key)
            rc = 0
            for prefix, v in rc_map.items():
                if key.startswith(prefix):
                    rc = v
                    break
            return R(rc)
        self.P.run = fake_run

    def capture(self, fn, *a, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*a, **kw)
        return json.loads(buf.getvalue())


class Status(Base):
    def test_installed_and_running(self):
        self.stub({"podman --version": "podman version 4.9.3\n",
                   "podman ps -q": "abc123\n"})
        self.stub_rc({})
        d = self.capture(self.C.act_status)
        self.assertTrue(d["installed"])
        self.assertEqual(d["version"], "4.9.3")
        self.assertTrue(d["running"])

    def test_running_by_service_when_no_containers(self):
        # 零容器的机器启动服务后仍应显示"运行中"（用户实测）
        self.stub({"podman --version": "podman version 4.9.3\n",
                   "podman ps -q": ""})
        self.stub_rc({"systemctl is-active --quiet podman.service": 0})
        self.assertTrue(self.capture(self.C.act_status)["running"])

    def test_not_running(self):
        self.stub({"podman --version": "podman version 4.9.3\n",
                   "podman ps -q": ""})
        self.stub_rc({})   # 所有 systemctl 都返回 0? 需显式让 is-active 失败
        # 让 systemctl 一律失败
        orig = self.P.run

        def fail_systemctl(cmd, timeout=30, **kw):
            class R:
                returncode = 1
                stdout = stderr = ""
            return R()
        self.P.run = fail_systemctl
        self.assertFalse(self.capture(self.C.act_status)["running"])


class ListContainers(Base):
    def test_parses_tab_separated_rows(self):
        self.stub({"podman ps -a": "sub-store\timg:1\trunning\t0.0.0.0:3001->3000/tcp\n"})
        d = self.capture(self.C.act_list_containers)
        self.assertEqual(len(d["containers"]), 1)
        c = d["containers"][0]
        self.assertEqual(c["name"], "sub-store")
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["ports"], ["0.0.0.0:3001->3000/tcp"])

    def test_handles_short_rows(self):
        # podman 某些版本端口为空，字段数不足时不能崩
        self.stub({"podman ps -a": "solo\timg:1\n"})
        d = self.capture(self.C.act_list_containers)
        self.assertEqual(d["containers"][0]["state"], "")
        self.assertEqual(d["containers"][0]["ports"], [])


class ListImages(Base):
    def test_matches_usage_by_image_id_not_name(self):
        """容器 .Image 与 images .Names 拼写常不一致 —— 必须按 ID 前 12 位匹配。"""
        self.stub({
            # 容器用的是 ID 前缀 abc123456789，名字是 myimg:latest
            "podman ps -a --format {{.ImageID}}": "abc123456789\n",
            "podman images": (
                "abc123456789\tdocker.io/library/other:1\t120 MB\n"
                "def987654321\tdocker.io/library/unused:1\t80 MB\n"
            ),
        })
        d = self.capture(self.C.act_list_images)
        by_id = {i["id"]: i for i in d["images"]}
        self.assertEqual(by_id["abc123456789"]["used_by"], 1,
                         "被使用的镜像必须标出 used_by=1（按名字匹配会漏）")
        self.assertEqual(by_id["def987654321"]["used_by"], 0)

    def test_dedupes_same_image_id(self):
        self.stub({
            "podman ps -a --format {{.ImageID}}": "",
            "podman images": (
                "abc123456789\timg:1\t1 MB\n"
                "abc123456789\timg:2\t1 MB\n"
            ),
        })
        d = self.capture(self.C.act_list_images)
        self.assertEqual(len(d["images"]), 1, "同一镜像 ID 只能出现一次")


class UpdateGuards(Base):
    """更新只回放端口/卷/环境变量/重启策略——特殊配置必须前置拒绝。"""

    def test_custom_network_rejected(self):
        self.stub({"podman inspect -f {{.HostConfig.NetworkMode}}": "mynet\n"})
        with self.assertRaises(SystemExit) as cm:
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.C.podman_update("web")
        self.assertEqual(cm.exception.code, 1)
        d = json.loads(buf.getvalue())
        self.assertEqual(d["code"], "CT_UPDATE_SKIP")
        self.assertIn("自定义网络", d["message"])

    def test_privileged_rejected(self):
        self.stub({"podman inspect -f {{.HostConfig.NetworkMode}}": "bridge\n",
                   "podman inspect -f {{.HostConfig.Privileged}}": "true\n"})
        buf = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(buf):
                self.C.podman_update("web")
        self.assertEqual(json.loads(buf.getvalue())["code"], "CT_UPDATE_SKIP")

    def test_bridge_network_allowed(self):
        # 默认网络不应被拒（否则正常容器无法更新）
        self.stub({"podman inspect -f {{.HostConfig.NetworkMode}}": "bridge\n",
                   "podman inspect -f {{.HostConfig.Privileged}}": "false\n",
                   "podman inspect -f {{.ImageName}}": "img:1\n",
                   "podman inspect -f {{.HostConfig.RestartPolicy.Name}}": "always\n"})
        self.stub_rc({})
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.C.podman_update("web")      # 成功时无输出，由调用方 ok_out
        self.assertEqual(buf.getvalue(), "")
        self.assertTrue(any("podman create" in c for c in self.calls),
                        "应走创建新容器再替换的路径")
        # 先建后删：create 必须在 stop 旧容器之前（失败时旧容器不受影响）
        self.assertLess(
            next(i for i, c in enumerate(self.calls) if "podman create" in c),
            next(i for i, c in enumerate(self.calls) if "podman stop -t 30" in c),
            "必须先创建新容器再停旧容器（审计 B-19）")


class ComposeGet(Base):
    def test_reads_compose_from_fake_root(self):
        import tempfile, shutil
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        os.makedirs(os.path.join(td, "etc", "dert"))
        with open(os.path.join(td, "etc", "dert", "compose.yaml"), "w") as f:
            f.write("services:\n  web:\n    image: nginx\n")
        old = self.P.ETC
        self.P.ETC = os.path.join(td, "etc", "dert")
        try:
            d = self.capture(self.C.act_compose_get)
        finally:
            self.P.ETC = old
        self.assertIn("nginx", d["content"])


if __name__ == "__main__":
    unittest.main()
