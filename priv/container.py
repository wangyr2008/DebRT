#!/usr/bin/env python3
"""容器（podman）域 —— 从 `dert-priv` 抽出（v0.1.82 / P2）。

行为与原 bash 实现**逐条对应**，包括这些踩过坑的细节：
  · 只读操作（status/list-*/compose-get）**不进排他锁**——pull 最长 660s，
    拉大镜像期间若占锁，开容器页会 504（真机实测）。
  · `list-images` 按**镜像 ID** 匹配使用状态，不按名字：容器 `.Image` 与
    images `.Names` 首个 tag 经常拼写不一致，按名字会误报"未使用"。
  · `container update` 只回放端口/卷/环境变量/重启策略。用自定义网络或
    privileged 的容器**前置拒绝**并指路 Compose——否则重建"成功"但配置丢了，
    用户要到连不上才发现。
  · `compose-deploy` 遇到 cgroup slice 竞态（OCI "No such file or directory"
    且容器 State=created）重试一次。

用法（与 bash 版子命令一一对应）：
    container.py status
    container.py list-containers | list-images | compose-get
    container.py container NAME start|stop|restart|remove|update
    container.py pull IMAGE | image-rm IMAGE
    container.py compose-set | compose-deploy | compose-stop
"""

import os
import sys

import privlib as P

READ_ONLY = {"status", "list-containers", "list-images", "compose-get"}
CT_ACTIONS = {"start", "stop", "restart", "remove", "update"}
CT_LABEL = {"start": "已启动", "stop": "已停止", "restart": "已重启",
            "remove": "已删除", "update": "已更新"}
# podman 会自动注入这些环境变量，回放时带上没意义还会污染
ENV_SKIP = ("PATH=", "TERM=", "container=", "HOSTNAME=", "HOME=")


def podman_available():
    return P.run(["sh", "-c", "command -v podman"]) is not None \
        and bool(P.run_out(["sh", "-c", "command -v podman"]).strip())


def act_status():
    ver = P.run_out(["podman", "--version"]).split()
    ver = ver[2] if len(ver) >= 3 else ""

    running = False
    if P.run_out(["podman", "ps", "-q"]).strip():
        running = True
    else:
        for svc in ("podman.service", "podman.socket"):
            r = P.run(["systemctl", "is-active", "--quiet", svc])
            if r is not None and r.returncode == 0:
                running = True
                break
    autostart = P.run(["systemctl", "is-enabled", "--quiet",
                       "podman-restart.service"]) is not None and \
        P.run(["systemctl", "is-enabled", "--quiet",
               "podman-restart.service"]).returncode == 0
    P.ok_out(installed=True, version=ver, running=running, autostart=autostart)


def act_list_containers():
    out = P.run_out(["podman", "ps", "-a", "--format",
                     "{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Ports}}"])
    rows = []
    for line in out.splitlines():
        parts = (line.rstrip("\n").split("\t") + [""] * 4)[:4]
        rows.append({"name": parts[0], "image": parts[1], "state": parts[2],
                     "ports": [p for p in parts[3].split(",") if p]})
    P.ok_out(containers=rows)


def act_list_images():
    # 按镜像 ID 前 12 位匹配使用状态（不按名字，见模块 docstring）
    used = {}
    for line in P.run_out(["podman", "ps", "-a", "--format", "{{.ImageID}}"]).splitlines():
        k = (line.strip() or "")[:12]
        if k:
            used[k] = used.get(k, 0) + 1

    imgs, order = {}, []
    out = P.run_out(["podman", "images", "--format", "{{.ID}}\t{{.Names}}\t{{.Size}}"])
    for line in out.splitlines():
        iid, names, size = (line.rstrip("\n").split("\t") + [""] * 3)[:3]
        k = (iid or "")[:12]
        if not k or k in imgs:
            continue
        first_tag = (names.split(",")[0] if names else "") or "<none>"
        imgs[k] = {"name": first_tag, "id": k, "size_text": size,
                   "used_by": used.get(k, 0)}
        order.append(k)
    P.ok_out(images=[imgs[k] for k in order])


def act_container(name, cact):
    if not name or not cact:
        P.die("USAGE", "缺少容器名或操作")
    if cact not in CT_ACTIONS:
        P.die("USAGE", "容器操作需为 start|stop|restart|update|remove")

    if cact in ("start", "restart"):
        r = P.run(["podman", cact, name])
        if r is None or r.returncode != 0:
            # OCI/cgroup 阶段的失败 logs 是空的，真实原因在 State.Error（真机实测）
            serr = P.run_out(["podman", "inspect", "-f", "{{.State.Error}}", name])[:200]
            P.die("CT_FAIL", f"容器 {name} {cact} 失败",
                  f"原因：{serr or f'无（可执行 podman logs --tail 20 {name} 查看）'}")
    elif cact == "stop":
        r = P.run(["podman", "stop", "-t", "10", name])
        if r is None or r.returncode != 0:
            P.die("CT_FAIL", f"容器 {name} 停止失败", "")
    elif cact == "remove":
        r = P.run(["podman", "rm", "-f", name])
        if r is None or r.returncode != 0:
            P.die("CT_FAIL", "删除容器失败", "请确认容器名正确")
    elif cact == "update":
        podman_update(name)

    P.ok_out(message=f"容器 {name} {CT_LABEL[cact]}")


def podman_update(cname):
    """重建容器：只回放端口/卷/环境变量/重启策略。

    观察③：自定义网络与 privileged 会静默丢失，而重建成功还返回"已更新"，
    用户要到连不上才发现 —— 所以这里前置拒绝并指路 Compose。
    """
    nm = P.run_out(["podman", "inspect", "-f", "{{.HostConfig.NetworkMode}}", cname]).strip()
    if nm not in ("", "default", "bridge"):
        P.die("CT_UPDATE_SKIP", f"容器 {cname} 使用自定义网络（{nm}），面板更新会丢失该配置",
              "此类容器请改用 Compose 管理，或用 podman CLI 手工重建")

    pv = P.run_out(["podman", "inspect", "-f", "{{.HostConfig.Privileged}}", cname]).strip()
    if pv == "true":
        P.die("CT_UPDATE_SKIP", f"容器 {cname} 以 privileged 运行，面板更新会丢失该配置",
              "此类容器请改用 Compose 管理，或用 podman CLI 手工重建")

    img = P.run_out(["podman", "inspect", "-f", "{{.ImageName}}", cname]).strip()
    if not img:
        P.die("CT_FAIL", f"无法读取容器 {cname} 的镜像信息")

    restart = P.run_out(["podman", "inspect", "-f",
                         "{{.HostConfig.RestartPolicy.Name}}", cname]).strip()
    if not restart or restart == "no":
        restart = "unless-stopped"

    r = P.run(["podman", "pull", "--timeout", "300s", img], timeout=360)
    if r is None or r.returncode != 0:
        P.die("PULL_FAIL", f"拉取 {img} 最新版本失败", "请检查网络与镜像仓库可达性")

    args = []
    # 端口：{{.HostIp}}:{{.HostPort}}:{{容器端口}}
    for line in P.run_out(["podman", "inspect", "-f",
                           "{{range $k,$v := .HostConfig.PortBindings}}{{range $v}}"
                           "{{.HostIp}}:{{.HostPort}}:{{$k}}{{\"\\n\"}}{{end}}{{end}}",
                           cname]).splitlines():
        if line.strip():
            args += ["-p", line.strip()]
    for line in P.run_out(["podman", "inspect", "-f",
                           "{{range .HostConfig.Binds}}{{.}}{{\"\\n\"}}{{end}}",
                           cname]).splitlines():
        if line.strip():
            args += ["-v", line.strip()]
    for line in P.run_out(["podman", "inspect", "-f",
                           "{{range .Config.Env}}{{.}}{{\"\\n\"}}{{end}}",
                           cname]).splitlines():
        s = line.strip()
        if s and not s.startswith(ENV_SKIP):
            args += ["-e", s]

    # 先建后删（审计 B-19）：新容器创建成功才动旧容器；失败保留旧容器原地运行
    tmp_name = f"{cname}.new.{os.getpid()}"
    r = P.run(["podman", "create", "--name", tmp_name, "--restart", restart] + args + [img])
    if r is None or r.returncode != 0:
        P.run(["podman", "rm", "-f", tmp_name])
        P.die("CT_REBUILD_FAIL", f"容器 {cname} 重建失败（旧容器未受影响）",
              "更新只保留端口/卷/环境变量/重启策略；使用特殊参数（--network/--privileged 等）"
              "的容器请改用 Compose 管理")

    P.run(["podman", "stop", "-t", "30", cname])
    P.run(["podman", "rm", cname])
    r = P.run(["podman", "rename", tmp_name, cname])
    if r is None or r.returncode != 0:
        P.die("CT_REBUILD_FAIL", "新容器已就绪但改名失败",
              f"可在系统上执行 podman start {tmp_name} 手工接管")
    P.run(["podman", "start", cname])


def act_pull(img):
    if not img:
        P.die("USAGE", "缺少镜像名")
    r = P.run(["podman", "pull", "--timeout", "300s", img], timeout=360)
    if r is None or r.returncode != 0:
        P.die("PULL_FAIL", f"镜像 {img} 拉取失败",
              "请检查镜像名与标签；若服务器在墙外，请确认网络可直连镜像仓库，或先配置镜像加速")
    P.ok_out(message=f"镜像 {img} 已拉取")


def act_image_rm(img):
    if not img:
        P.die("USAGE", "缺少镜像名")
    r = P.run(["podman", "rmi", img])
    if r is None or r.returncode != 0:
        P.die("IMG_FAIL", "删除镜像失败",
              "该镜像可能正被容器使用，或名称不完整（需含 tag）")
    P.ok_out(message="镜像已删除")


def act_compose_get():
    p = P.ETC + "/compose.yaml"
    try:
        with open(p, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        content = ""
    P.ok_out(content=content)


def act_compose_set():
    content = sys.stdin.read()
    P.write_atomic(P.ETC + "/compose.yaml", content, 0o644)
    P.ok_out(message="Compose 文件已保存")


def act_compose_deploy():
    if not os.path.isfile(P.ETC + "/compose.yaml"):
        P.die("COMPOSE_EMPTY", "尚未保存 Compose 内容", "请先在文本框中填写并部署")
    if P.run(["sh", "-c", "command -v podman-compose"]) is None or \
            not P.run_out(["sh", "-c", "command -v podman-compose"]).strip():
        P.die("COMPOSE_MISSING", "未安装 podman-compose",
              "在系统上执行 apt install podman-compose 后重试")

    os.makedirs(P.RUN, exist_ok=True)
    logf = os.path.join(P.RUN, "compose." + os.urandom(4).hex())
    with open(logf, "w") as lf:
        r = P.run(["podman-compose", "-f", P.ETC + "/compose.yaml", "-p", "dert",
                   "up", "-d"], timeout=600)
        if r:
            lf.write(r.stdout or "")
            lf.write(r.stderr or "")

        if r is None or r.returncode != 0:
            # pod 建立时 cgroup slice 竞态：报 OCI "No such file or directory"
            # 且容器 State=created —— 重试一次即好（真机实测一次）
            logtext = open(logf, encoding="utf-8", errors="replace").read()
            retried = False
            if "no such file or directory" in logtext.lower() or "OCI runtime" in logtext:
                names = P.run_out(["podman", "ps", "-a", "--filter",
                                   "label=com.docker.compose.project=dert",
                                   "--format", "{{.Names}}"]).split()
                if names:
                    st = P.run_out(["podman", "inspect", "-f",
                                    "{{.State.Status}} {{.State.Error}}", names[0]])
                    if "created" in st:
                        import time
                        time.sleep(2)
                        r2 = P.run(["podman-compose", "-f", P.ETC + "/compose.yaml",
                                    "-p", "dert", "up", "-d"], timeout=600)
                        if r2:
                            lf.write(r2.stdout or "")
                            lf.write(r2.stderr or "")
                        retried = True

    names = P.run_out(["podman", "ps", "-a", "--filter",
                       "label=com.docker.compose.project=dert",
                       "--format", "{{.Names}}"]).split()
    if not names:
        tail = " ".join(open(logf, encoding="utf-8", errors="replace")
                        .read().splitlines()[-4:])[:260]
        try:
            os.unlink(logf)
        except OSError:
            pass
        P.die("COMPOSE_FAIL", "Compose 部署失败",
              f"常见原因：语法错误、镜像名/标签不存在、端口被占用。日志：{tail}")

    tail = " ".join(open(logf, encoding="utf-8", errors="replace")
                    .read().splitlines()[-3:])[:200]
    try:
        os.unlink(logf)
    except OSError:
        pass
    P.ok_out(message="Compose 已部署", log_tail=tail)


def act_compose_stop():
    P.run(["podman-compose", "-f", P.ETC + "/compose.yaml", "-p", "dert", "down"],
          timeout=300)
    P.ok_out(message="Compose 已停止（数据卷保留）")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else ""
    rest = argv[1:]

    P.require_root()

    if not podman_available():
        if action == "status":
            # 未安装也给前端结构化结果；不能落到 die（双 JSON 会让面板解析失败）
            P.ok_out(installed=False, running=False, autostart=False)
            return 0
        P.die("PODMAN_MISSING", "Podman 未安装", "请在系统上执行 apt install podman podman-compose")

    # B-08：只读操作不进排他区
    if action not in READ_ONLY:
        P.pod_lock()

    if action == "status":
        act_status()
    elif action == "list-containers":
        act_list_containers()
    elif action == "list-images":
        act_list_images()
    elif action == "container":
        act_container(rest[0] if rest else "", rest[1] if len(rest) > 1 else "")
    elif action == "pull":
        act_pull(rest[0] if rest else "")
    elif action == "image-rm":
        act_image_rm(rest[0] if rest else "")
    elif action == "compose-get":
        act_compose_get()
    elif action == "compose-set":
        act_compose_set()
    elif action == "compose-deploy":
        act_compose_deploy()
    elif action == "compose-stop":
        act_compose_stop()
    else:
        P.die("USAGE", f"podman-ops 未知操作：{action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
