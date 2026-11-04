# SeedRou —— Debian 13 有线路由器系统

把 x86 工控机上的 **Debian 13 minimal（CD 最小化安装）** 变成有线路由器，并附带
Apple 风格的 Web 管理后台（登录页 + 7 页：系统概览 / 网络设置 / 设备列表 /
安全中心 / 容器管理 / 其他工具 / 备份与升级）。

- **仅支持有线路由**（PPPoE / DHCP / 静态 IP 三种 WAN 方式）。
- 面板零第三方依赖：后端 Python 3 标准库，前端原生 HTML/CSS/JS。
- **sing-box 管理**：v0.1.32 起面板接管 sing-box 的安装/卸载/状态/订阅/自动更新时间（sb.sh 增强版随包下发到 `/usr/local/bin/sb`，sing-box 本体默认不装）；tun + auto_route +
  auto_redirect），底座与其共存契约见 [singbox/COEXISTENCE.md](singbox/COEXISTENCE.md)。

## 1. Debian 13 CD 安装选项（约定）

工控机用官方 netinst/CD 安装，选择与下表一致可以让 install.sh 一次成功：

| 安装项 | 选择 |
|---|---|
| 桌面环境 / 打印服务器等 tasksel 项 | **全部不选**，仅勾选 `SSH server` + `standard system utilities` |
| 分区 | 整盘即可；`/` 建议 ≥ 8 GB（Podman 镜像占用空间） |
| root 密码 | 自定（控制台排障用） |
| 普通用户 | 可不建（面板有独立账号体系） |
| 安装中网络 | 若先接了 WAN 口，保持 DHCP 即可 |

装完进入系统后确认能上网（`ping -c2 223.5.5.5`），然后按第 2 节安装。

## 2. 安装

```bash
# 把 seedrou 目录拷到目标机后：
sudo bash install.sh            # 含 Podman
sudo bash install.sh --no-podman # 不装 Podman（面板容器页会提示未安装）
```

安装脚本会：
1. 安装 `nftables dnsmasq ppp pppoe python3 networkd-dispatcher` 等（可选 podman/podman-compose）；
2. 创建无 shell 的系统用户 `seedrou`，面板以其身份运行，特权操作经
   **sudo 白名单唯一入口** `seedrou-priv`；
3. 自动发现网口：**第 1 个口 = WAN，其余 = LAN**；LAN IP `192.168.10.1`，
   DHCP 池 `.100–.200`（可在面板修改）；
4. 生成随机管理密码（只打印一次），或安装时交互输入；
5. 禁用 Debian 自带 `nftables.service`（它开机 `flush ruleset`，会杀掉 sing-box 的表——
   我们改用只管理自己表的 `seedrou-apply`）；
6. 首次收敛 + 自检（`seedrou-priv selftest`）。

接线：**上级网络（光猫桥接 / 上级路由 LAN）→ 第 1 个网口（WAN）**，电脑接其余口，
浏览器打开 `http://192.168.10.1/` 登录。

### 卸载

```bash
sudo bash uninstall.sh          # 保留 /etc/seedrou 配置
sudo bash uninstall.sh --purge  # 连配置一起删
```

## 3. 架构速览

```
浏览器 ──HTTP:80──▶ seedrou-panel (用户 seedrou, Python3 标准库)
                        │  sudo 白名单（/etc/sudoers.d/seedrou）
                        ▼
                   seedrou-priv (root, 子命令白名单)
                        │  flock 串行 + 原子写 + lastgood 回滚
                        ├──▶ /etc/seedrou/config.json   ← 唯一事实源（无明文密码）
                        ├──▶ /etc/seedrou/secrets.json  ← 管理密码 pbkdf2 / PPPoE 密码 (root 0600)
                        └──▶ netgen.py 生成原生配置并原子落盘：
                              /etc/systemd/network/*.network (WAN + br-lan 桥)
                              /etc/dnsmasq.d/seedrou.conf    (LAN DHCP/DNS)
                              /etc/nftables.d/seedrou-base.nft (自有表)
                              /etc/ppp/peers/seedrou + chap/pap-secrets (PPPoE)
```

**关键机制（对应工程要求）：**

| 要求 | 实现 |
|---|---|
| 配置写入原子性 | 所有 /etc 写入经 `write_atomic`（同目录 mktemp → fsync → rename）；应用前 `nft -c` / `dnsmasq --test` 预检；失败自动回滚 lastgood（保留 5 份） |
| root/面板属主交接 | 面板无 root；唯一 sudo 入口 `seedrou-priv`（内部再校验子命令与参数）；secrets 面板读不到，验密/改密经 stdin |
| 错误可操作 | 全链路 `{code, message, hint}` 中文错误（原因 + 下一步），如 PPPoE 区分认证失败/无响应/占用 |
| 敏感信息不回显 | GET 永不返回密码/哈希/密钥；UI 显示"已保存，留空保持不变"；Cookie HttpOnly+SameSite=Strict，登录 5 次失败锁 60 秒；变更请求校验 X-SeedRou 头 + Origin |
| 重启后状态收敛 | `seedrou-apply.service` 开机幂等重放 + `seedrou-converge.timer` 开机 15 分钟后首跑、之后每 6 小时防漂移；一切原生配置都是 config.json 的生成物 |

## 4. 功能页 ↔ 实现映射

| 页面 | 说明（相对 demo 设计稿的修正） |
|---|---|
| 系统概览 | 真实 WAN 状态/会话时长、CPU/内存/温度（smartctl 未装时硬盘温度显示 —）、只读上网信息（含 IPv6）、网口角色/速率/插拔；8 秒轮询 |
| 网络设置 | 网口绑定按真实 NIC 动态渲染；**仅支持单 WAN**（demo 提示的多 WAN 负载均衡未实现，多勾会明确报错）；IPv6 四模式：Native-PD / NAT6（自动 ULA）/ 静态 / 关；改 LAN IP 有二次确认，失败自动回滚旧 IP 保证可管理 |
| 设备列表 | dnsmasq 租约 + 静态绑定合并，在线状态来自 ARP 表；备注按 MAC 存储并与静态分配同步 |
| 安全中心 | demo 的"防火墙放行规则"实为**端口转发（DNAT）**，已正名并补齐增删改/启停/冲突校验；"防火墙总开关"= WAN 入站默认拦截（关闭时红色警示） |
| 容器管理 | rootful Podman：容器启停/重启/删除/更新（拉最新镜像后按原端口、卷、环境变量重建）；镜像引用检查后删除；Compose 保存到 /etc/seedrou/compose.yaml，`podman-compose` 部署，失败日志可见 |
| 其他工具 | sing-box 服务卡（状态/开机自启/重启/停止/打开 Dashboard/检查更新/一键安装/卸载）；订阅卡（地址保存、更新订阅、每日自动更新时间可设定、成功显示时长、失败原因持久显示）；Tailscale 只读卡（sing-box endpoint 托管，未配置自动隐藏） |
| 备份与升级 | 在线检查更新走 GitHub **releases/latest 302 解析**（不碰 api.github.com，规避限流，与 sb.sh 同思路，带 gh-proxy 镜像回退）；升级包 `.tar.gz`（sha256 校验 → 暂存自检 → 原子换目录 → 延迟重启面板 → 失败回滚）；备份/恢复 /etc/seedrou；恢复出厂只重置路由配置（LAN 恢复 192.168.10.1，管理密码与升级源保留） |

## 5. 发布与自动升级（GitHub）

仓库自带 `.github/workflows/release.yml`：推送 `v*` tag 即自动打包
`seedrou-<ver>.tar.gz` + `.sha256` 并发到 Release。然后在面板
「备份与升级 → 升级源」填 `wangyr2008/SeedRou`（本仓库），「检查更新」即可在线升级。

手动升级：Releases 页下载 `seedrou-x.y.z.tar.gz`，面板上传即可（仅支持 .tar.gz；
Debian 没有 .bin/.img 固件的概念）。

## 6. 升级与回滚

- **在线升级**：面板「备份与升级」→ 检查更新 → 一键升级；或上传 `.tar.gz`。
  升级只替换 `/usr/local/lib/seedrou`（面板+特权脚本），配置 `/etc/seedrou` 不动。
- **自动回滚**：升级包校验失败/解压失败/自检失败均不会落盘，原版本原地保留；
  每次升级前还会把当前版本备份到 `/var/lib/seedrou/lastgood/app-<版本>`。
- **手动回退旧版本**：从 Releases 下载旧版 `.tar.gz` 走手动升级即可（系统拒绝降级时，
  用 `lastgood/app-旧版本` 目录手工替换后 `systemctl restart seedrou-panel`）。
- sudo 审计双重记录（journal + `/var/log/seedrou-sudo.log`，weekly 轮转），介意 journal
  噪音可在 `journald.conf` 按 `SYSLOG_IDENTIFIER=sudo` 过滤。

## 7. 目标机冒烟清单

```bash
seedrou-priv selftest                       # 组件自检
systemctl status seedrou-panel seedrou-apply dnsmasq
nft list tables                             # 应看到 seedrou_base；开 sing-box 后还有 sing-box 表
nft list table inet seedrou_base            # NAT / 转发规则
ip route show default                       # 主表默认路由应在 WAN 口（sing-box 的默认路由在 table 2022）
cat /var/lib/seedrou/dnsmasq.leases         # 租约
# 重启收敛对比：
nft list ruleset > /tmp/before && reboot
nft list ruleset > /tmp/after && diff /tmp/before /tmp/after
# sing-box 共存：
sb install && sb update     # LAN 仍可上网、面板可登录
systemctl stop sing-box && ping 223.5.5.5   # 直连自动恢复
# 端口转发回环：从公网侧（或手机热点）访问 WAN_IP:外部端口 → 应到达 LAN 目标
```

## 8. 目录结构

```
seedrou/
├── install.sh / uninstall.sh     # 目标机部署
├── VERSION                       # 当前版本号（beta 从 0.1 起版）
├── panel/                        # 管理面板（Python3 标准库 + 静态前端）
│   ├── seedrou_panel.py api.py auth.py sysinfo.py
│   └── static/ index.html app.css app.js icons.js
├── priv/                         # 特权层（root）
│   ├── seedrou-priv              # sudo 唯一入口（子命令白名单）
│   ├── lib.sh                    # 原子写 / lastgood / flock / JSON 错误协议
│   ├── validate.py               # 配置校验与规范化
│   ├── netgen.py                 # 由 config.json 生成全部原生配置
│   └── qdisc-apply               # cake QoS
├── config/config.json            # 默认配置模板（安装时按真实网口初始化）
├── systemd/                      # 面板 / 开机收敛 / 防漂移定时器
├── network/                      # sysctl、模块加载、dispatcher/pppd 钩子、PPPoE 单元
├── singbox/COEXISTENCE.md        # 共存契约（必读）
├── sudoers/seedrou
└── .github/workflows/release.yml # tag → tar.gz + sha256 → Release
```

## 9. 开发预览（无目标机也能看 UI）

```bash
python3 panel/seedrou_panel.py --mock --port 8080
# 浏览器打开 http://127.0.0.1:8080  （mock 密码：admin）
```

`--mock` 模式用固定数据驱动全部页面交互，便于改前端样式；真实模式不传 `--mock`。

## 10. 已知边界

- 仅单 WAN；多宽带负载均衡未实现（勾选多个 WAN 口会明确报错）。
- IPv6 Native 模式要求**上级路由支持并开启 DHCPv6 前缀委派（PD）**——上级自身为 Stateless RA / 未开 PD 时拿不到前缀，此场景请用 NAT6（要求 WAN 侧有全局 IPv6 地址）。NAT6 在 PPPoE/静态线路下不可用（无出口地址），面板会明确拒绝。
- 「更新」容器按端口/卷/环境变量重建，特殊参数容器建议用 Compose 管理。
- 硬盘温度需要 `smartmontools`（未安装时显示 —，不影响其他功能）。
- 面板为 HTTP 明文（局域网惯例）；如需 HTTPS 可自行在前置反代加 TLS。
- 备份包不含 secrets（管理密码/宽带密码），恢复后如需 PPPoE 拨号需重新输入密码。
- LAN 子网掩码支持 /24–/30（DHCP 地址池按「IP 末段」配置的语义决定）。
