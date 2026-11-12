# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)（`主版本.次版本.修订号`），
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

当前处于 `0.x` 阶段：次版本号可能包含不兼容变更，升级前请阅读对应条目。

## [Unreleased]

## [0.1.82] - 2026-11-12

> 工业化重构 **P2-2**：容器（podman）域抽成独立模块。

### Changed

- **`priv/container.py`（335 行）**：`do_podman_ops` + `do_podman_update` 的实现。
  `dert-priv` 从 **1567 行降到 1409 行**（−158）。

  这不是字面搬迁——原实现是 bash 编排，改写成了 Python，**逐条对应**这些细节：
  - 只读操作（`status`/`list-*`/`compose-get`）**不进排他锁**：pull 最长 660s，
    拉大镜像期间若占锁，开容器页会 504（真机实测）
  - `list-images` 按**镜像 ID** 匹配使用状态，不按名字（容器 `.Image` 与
    images `.Names` 首个 tag 常拼写不一致，按名字会误报"未使用"）
  - `container update` 只回放端口/卷/环境变量/重启策略；**自定义网络或
    privileged 前置拒绝**并指路 Compose（否则重建"成功"但配置丢了）
  - 先建后删（审计 B-19）：新容器创建成功才动旧容器
  - `compose-deploy` 遇 cgroup slice 竞态（OCI "No such file or directory"
    且 State=created）重试一次

- **`priv/privlib.py`（共用基础设施）**：JSON 协议（`die`/`ok_out`）、数据根推导、
  `require_root`、`pod_lock`（flock + 超时）、`write_atomic`。域模块共用，
  避免各写一遍后漂移。

### Added

- `tests/test_container.py`（11 个用例）：用打桩替换 `podman` 调用，
  断言模块**调了哪些命令、输出什么 JSON**。重点覆盖上面两条坑：
  按 ID 匹配使用状态、特殊配置前置拒绝；另断言「create 必须在 stop 旧容器之前」。

### 验证

- **真机新旧双跑**：`podman-ops` 的 4 个只读子命令（status / list-containers /
  list-images / compose-get）与抽取前的实现**逐字节一致**。

### Notes

- 守卫「观察③ 容器更新缺前置拒绝」的断言位置改指向 `container.py`。
- 三个文件（dert-priv / container.py / privlib.py）**一次性部署**——
  v0.1.81 就是先传调用方后传模块把 status 弄坏过一次。

## [0.1.81] - 2026-11-12

> **P4（可观测）** + 一条防止"拆分漏部署"的结构守卫。

### Added

- **`dert-priv health`**（`priv/health.py`）：一条命令给出结构化健康状态，
  **只读、不改任何东西**，退出码 0/1 供监控直接消费（systemd timer / cron /
  Zabbix / node_exporter textfile 都能用）。

  14 项检查，每一项都对应一个真机踩过的坑：
  | 检查 | 为什么要有 |
  |---|---|
  | 5 个服务 active | 基本存活 |
  | 失败单元数 = 0 | 别只看单个服务 |
  | `dert_dns` 规则数 = 4 | 直接写 table 定义是**追加**，真机曾堆到 20 条 |
  | nft v6 目标 = 当前 GUA | 前缀变了没跟上 → 白名单 v6 DNS 全落空（437 个包） |
  | 5353 在监听 | fakeip 不可用时白名单 DNS 直接坏 |
  | `resolv.conf` 指向 127.0.0.1 | 被 pppd 改写后 `.lan` 解析失败 |
  | 收敛失败标记 | 面板要显示"上次收敛失败" |
  | 磁盘余量 ≥ 1GB | 升级包需要空间 |
  | 配置可解析 / 版本可读 | 基础 |

- `tests/test_health.py`（9 个用例）：用假 `systemctl`/`nft`/`ss`/`ip` 把
  **每一种不健康场景**单独构造出来，断言被正确识别且退出码为 1。

### Fixed

- **`dert-priv` 引用了不存在的模块时会直接坏掉**（真机实测踩到）。
  拆分后 `do_status` 变成 `exec python3 "${PRIV_DIR}/status.py"`，而我手工部署时
  先传了 `dert-priv`、后传模块，中间那段时间 `status` 报
  `can't open file '/usr/local/lib/dert/status.py'`。
  **升级通道本身是整目录替换，不会出这个问题**——是手工部署才踩到。

  新增守卫 P3-32：`dert-priv` 里 `${PRIV_DIR}/xxx` 引用的每个文件都必须在
  `priv/` 下真实存在；反向也断言 `install.sh` 仍用 `priv/*.py` 通配部署
  （改成逐个列举就会漏装新模块）。

## [0.1.80] - 2026-11-12

> 工业化重构 **P2-1（结构分层，第一个域）**：把 `status` / `selftest` 从
> `dert-priv` 抽成独立 Python 模块。

### Changed

- **`priv/status.py`（238 行）与 `priv/selftest.py`（33 行）**。这两段本来就是
  纯 Python，只是嵌在 bash 的 `<<'PYEOF'` 里——抽出来是**字面搬迁，逻辑一字未改**。
  `dert-priv` 从 **1822 行降到 1565 行**（−257）。

  抽出的直接收益：可以 `DERT_FSROOT=<假根> python3 priv/status.py` 真跑一遍，
  断言**输出什么**，而不是断言源码里有没有某个字符串。

- 模块自己从 `DERT_FSROOT` 推导数据根（`E/V/R`），不依赖 `lib.sh` 导出——
  这样既能被 `dert-priv` 调用，也能独立跑，两条路径结果一致。

### Added

- `tests/test_status.py`（9 个行为用例）：用假 `ip` 命令控制网络状态，覆盖
  WAN up/down、掩码换算、PPPoE「正在连接」、`converge_failed` 标记、
  NAT6 显示 ULA、端口角色、自检项与汇总。

### 验证

- **与抽取前逐字节比对**：`git archive HEAD` 取旧版，同一假根下
  `status` / `selftest` 输出完全一致。
- 模块独立跑 vs 经 `dert-priv` 调用，输出也完全一致。

### Notes

- 抽走后守卫 P3-11（硬盘温度取 `current`）指向的源码位置变了，改成读
  `priv/status.py`。**这正是 P1-2 想消除的脆弱性**——但这类断言只剩个位数，
  棘轮保证它不会变多。
- shellcheck 抓到 `SC2120`：两个子命令不接受参数，`"$@"` 是多余的，已去掉。

## [0.1.79] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第四阶段）**：把测试缝铺到 `dert-priv`。
> 这是本项目风险最高的一次改动——它是升级通道本身。

### Added

- **`dert-priv` 全部数据路径支持 `DERT_FSROOT`**。57 处硬编码 `/etc/dert` /
  `/var/lib/dert` / `/run/dert` 改为走 `DERT_ETC` / `DERT_VAR` / `DERT_RUN`
  （由 `lib.sh` 统一提供，未设时为空串 → 与原路径**逐字符等价**）。

  两类上下文分别处理：
  - **shell 上下文**（18 处）：直接引用变量。
  - **`<<'PYEOF'` 的 Python heredoc**（38 处）：引号 heredoc 不展开 shell 变量，
    每个块顶部注入一行 `E/V/R = os.environ.get(...)` 前置声明，字面量改为
    `E + "/config.json"` 形式。
  - **单引号 `python3 -c '...'`**（2 处）：同样走 `os.environ`。

- **`tests/test_dert_priv.py`（6 个行为用例）**：这个文件过去只能做源码断言，
  现在可以真的跑起来喂假根：
  - `config-get` 从假根读取、**不泄漏 secrets**（admin_pbkdf2 / pppoe_password /
    session_secret 都不得出现在输出里）、以 `has_password` 布尔代替明文
  - `status` 的 `converge_failed` 跟随假根（决定面板是否显示"上次收敛失败"）
  - 缝的隔离性：设了 FSROOT 就绝不能读到真实 `/etc`

### 验证方式（关键）

这类改动**不能只靠单测**——单测证明不了 shell 接线对不对（v0.1.76 就是这么翻车的）。
这次用**新旧并排双跑**：

1. 先把新文件放到 `/usr/local/lib/dert/dert-priv.new`，**不替换现役**
2. 同一批只读子命令两边各跑一次，逐字节比对：
   `config-get` / `status` / `compose-get` / `list-containers` / `list-images` / `version`
   → **6/6 完全一致**
3. 再造一个假根验证缝真的生效：
   - 新版 + `DERT_FSROOT` → 读到假根里的 `marker: FAKE-ROOT`、`converge_failed: true`
   - 新版 无 FSROOT → 读到真实配置、`converge_failed: false`
   - 现役 无 FSROOT → 与新版一致
4. 确认无误后才替换现役文件

### Notes

- **转换脚本本身踩了一个坑，值得记**：初版按「`<<TAG` 就是 Python heredoc」处理，
  结果把 `<<SBURL` / `<<SBDASH`（写 URL 文件）和 `<<'DPEOF'`（写 systemd unit）
  也当成 Python，往里注入了 `import os` 一行——那会直接把垃圾写进配置文件和
  unit。是「逐块做 Python 语法检查」发现的：3 个块报 SyntaxError。
  修正为**只有命令行含 `python3` 的 heredoc 才当 Python**。
- 中文提示文案（如 `"在系统上执行：rm /etc/dert/secrets.json ..."`）全部保留原样，
  替换时按「行内含 CJK 就跳过」规避。
- 棘轮预算不变（133）——本轮新增的全是行为断言。

## [0.1.78] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第三阶段）**：给 `lib.sh` 加测试缝，
> 让配置事务的核心函数第一次有运行时测试。

### Added

- **`DERT_FSROOT` 测试缝扩到 `lib.sh`**。只作用于**数据**路径
  （`DERT_ETC` / `DERT_VAR` / `DERT_RUN`），代码路径（`DERT_ROOT` / `DERT_SHARE`）
  不变——「程序在哪」和「数据在哪」是两件事。

  未设时是空串，`"${FSROOT}/etc/dert"` 与 `"/etc/dert"` **逐字符等价**，
  生产行为一字不变；设了就指向临时根。三个变量同时 `export`，供内联
  Python 用 `os.environ` 取（heredoc 是 `<<'PYEOF'` 形式，shell 不展开变量）。

- **`tests/test_lib_functions.py`（16 个行为用例）**。这些函数此前**没有任何
  运行时测试**，只有源码字符串断言：
  - `write_atomic`：内容/权限/自动建父目录/**不残留临时文件**/覆盖写
  - `snapshot_lastgood` / `restore_lastgood`：快照落盘、回滚还原、
    快照不存在时必须返回非零（不能静默成功）、**轮转只留 5 份**
  - `plan_for_section`：9 个配置段的重启映射（改错会导致"改了没生效"或
    "多断一次网"）、未知段回落全量、IPv6 变更不带 cake
  - `ok_out` / `die`：面板依赖的 JSON 协议，类型强转与字段完整性
  - **缝不生效**：未设 / 设成空串时，解析出的路径必须与生产完全一致

### Notes

- **为什么这版没动 `dert-priv`**：它有 70 处硬编码 `/etc|/run|/var/lib`，
  其中 41 处在 `<<'PYEOF'` 的 Python heredoc 里（shell 不展开变量，必须改成
  `os.environ`），而且里面混着**提示文案**（如 `"在系统上执行：rm /etc/dert/..."`），
  盲替换会改错。这是升级通道本身，值得单独一版、单独验证。
- 真机验证：部署后 `CONFIG_JSON` / `DERT_VAR` / `DERT_RUN` 解析与改动前
  完全一致；`config-get`、`status`、`apply-all --convergent` 均正常；
  `/etc/dert` 无残留临时文件；lastgood 仍是 5 份。
- 棘轮预算不变（133）——本轮新增的全是行为断言。

## [0.1.77] - 2026-11-12

> 修一个 **v0.1.76 自己在真机上暴露的 bug**，并补上锁住它的守卫。

### Fixed

- **tar 的 stderr 被合进了成员清单管道**。v0.1.76 抽 `tarcheck.py` 时写成了
  `tar -tzf "$pkg" 2>&1 | tarcheck.py`：tar 对含 `..` 的成员会往 stderr 打
  `Removing leading 'priv/../../' from member names`，合并后那行被当成成员名，
  于是拒绝原因变成 **tar 的警告原文**而不是真实条目：

  ```
  "具体：升级包含危险路径：tar: Removing leading `priv/../../' from member names"
  ```

  两个方向都错：含 `..` 的包提示错位；反过来，**正常包若有任何 tar 警告也会
  被误判为危险条目**。改成先把清单取到变量、再喂给校验器，并单独处理
  「包读不出来」的情况。

### Notes

- 守卫补一条：`tar -tzf "$pkg" 2>&1 | python3` 不得出现。棘轮预算 132 → 133，
  理由写在 `check_assert_ratio.py` 里——这个 bug 只在以 root 跑 `upgrade-apply`
  且包含 `..` 时显现，没有不依赖 root 的行为测试办法。
- **发现过程值得记**：本地单测全绿（`tarcheck.check()` 逻辑是对的），
  是**真机跑一遍**才暴露出来——问题不在模块，在 shell 的管道接线。
  这也说明"抽模块 + 行为测试"之后仍需真机验证，两者不能互相替代。

## [0.1.76] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第二阶段）**：把「以 root 解压之前」的最后
> 一道防线抽成可测模块。

### Security

- **`priv/tarcheck.py`**：升级包成员校验从 `dert-priv` 的内联 Python 抽出。
  这段逻辑此前只有源码字符串断言（`assert 'if "/" not in p: continue' in src`）
  ——而它挡的是路径穿越，字符串断言**证明不了它真的拦得住**。

  抽出来后用**真实恶意 tar** 做行为测试：用 Python `tarfile` 造一个成员名为
  `priv/../../etc/cron.d/evil` 的包（GNU tar 创建时会自己剥掉 `..`，造不出来，
  但攻击者可以用别的工具造），走 `tar -tzf | tarcheck.py` 这条生产路径，
  断言被拒绝且错误里带上具体条目名。

  顺带改善可诊断性：拒绝时不再只说「包含不认识的条目」，而是带上
  `具体：升级包含危险路径：priv/../../etc/cron.d/evil`。

### Added

- `tests/test_tarcheck.py`（10 个用例）：绝对路径、`..`（含中段穿越）、
  未知顶层目录、目录条目、顶层文件、空名，以及两个端到端恶意 tar 用例。

### Changed

- 守卫里两条白名单断言换成一条「接线」约束（dert-priv 必须调用该模块）。
  语义断言交给行为测试，源码断言只留「有没有接上」这种架构约定。

### Notes

- 棘轮预算从 133 下调到 **132**（锁定成果）。
- **关于剩余断言的判断修正**：我原来给 P1-2 定的目标是「字符串断言降到 40%
  以下」，现在看这个目标定错了。剩下的 132 条里约 30 条是「通道同步」
  （安装/升级/卸载三处都要装同一个文件）——这类断言的是**跨文件约定**，
  行为化要跑完整升级链，成本高且失真；它本来就该留在源码层。
  真实可迁移的大概还有 40–50 条，集中在 `dert-priv` 与 shell 钩子，
  前提是给它们加 `DERT_FSROOT`——那正是 P2 需要的能力。
- 真机验证：升级到 0.1.76 后，用构造的恶意包跑 `upgrade-apply`，
  被拒绝且提示里带出了穿越路径；正常包不受影响。

## [0.1.75] - 2026-11-12

> 工业化重构 **P1-2（守卫分层，第一阶段）**。
> 目标是把「`assert "某段文本" in 源码`」这类断言换成「跑一遍、断言产物」的
> 行为测试——前者断言代码长相，重构时容易误报（本项目已踩过 4 次）。

### Added

- **测试缝 `DERT_FSROOT`**（`dns-split-sync` / `dns-split-prepare`）。未设时是
  空串，`${FSROOT}/etc/...` 与原绝对路径**逐字符等价**，生产行为不变；设了就
  指向临时根，能在无 root 环境跑完整逻辑。

  这一步是必需的：这两个脚本原来把 `/etc/sing-box/config.json` 写死，
  根本没有办法在没有 root 的情况下验证它们**实际生成了什么规则**——
  这正是过去只能做源码字符串断言的原因。

- **`tests/test_dns_split.py`（8 个行为用例）**。用假根 + 假命令把脚本真跑一遍：
  PATH 前置假 `systemctl`/`ss`/`ip`/`nft`/`logger`，假 `nft` 把 stdin 原样录下来
  ——那就是脚本**实际会下发**的规则。断言针对录下来的规则文本：
  - 白名单 → `:5353`（fakeip 路径）、非白名单 → br-lan 的 `53`（真实 IP 路径）
  - v6 目标优先取 `dynamic`（PD 委派），不选 `temporary` 隐私地址
  - `add table` + `delete table` 同事务原子替换（直接写 table 定义是**追加**）
  - 建表失败 → 删表回落 dnsmasq，而不是留半张表
  - 空白名单 → 完全不建表
  - 前缀基线 `/run/dert/lan-prefix` 写成 `…/64`
  - prepare 产物：5353 双栈监听、`hijack-dns`、`.lan` 本地解析、无 `source_ip_cidr`

- **断言棘轮 `check_assert_ratio.py`**。字符串断言数量**只能降不能升**，
  CI 里 `--check` 超预算即失败。新增断言若必须是源码断言（例如"三处通道都要
  装同一个文件"这种跨文件约定），需要同时调整预算并写明理由——强迫先想一下
  能不能行为化。

### Changed

- 删掉 14 条已被上述行为测试覆盖的源码字符串断言（`_dss`/`_dsp` 那批）。

### Notes

- 守卫新增 P3-32 之前先做了棘轮（独立脚本，避免自引用计数）。
- **本阶段只完成了 P1-2 的一部分**：字符串断言 133 条（占 83%），主要剩
  `dert-priv`（22 条）、各 shell 钩子（约 40 条）、通道同步类（约 30 条）。
  后两类里，通道同步属于真正该留在源码断言的「架构约束」；
  钩子那批需要同样加 `DERT_FSROOT` 才能行为化——而那正是 P2 需要的能力，
  所以下一步是把测试缝扩展到 `lib.sh` + `dert-priv`，两件事是同一个工程。
- 真机验证：重启 sing-box 触发 `ExecStartPost` → 分流表重建 4 条、v6 目标等于
  当前 GUA、前缀基线写入 `2409:8a5e:a52:45e0::/64`，与改动前一致。

## [0.1.74] - 2026-11-12

> 工业化重构 **P3（发布签名）**。这是审计报告列为 P0 的一项，也是本项目
> **唯一无需登录即可触达的攻击面**：升级包以 root 执行，而包与 `.sha256`
> 走同一条通道（含 `gh-proxy` 第三方镜像回退）——镜像被劫持时可以同时
> 替换包和哈希，sha256 形同虚设。

### Security

- **Ed25519 detached signature**。选它而不是 minisign/GPG 的理由：
  - 设备端用 **openssl 原生验签**（`pkeyutl -verify -rawin`），不引入任何
    新二进制、不自己实现密码学。Debian 13 自带 OpenSSL 3.5 已实测支持
    Ed25519 签名/验签/篡改检测。
  - 私钥**不进 CI**：存在仓库 Secret `DERT_SIGNING_KEY`（base64 的 PEM）。
    没配 Secret 时发布流程只告警不失败，避免 fork 后一打 tag 就红。
  - CI 签名后**立刻自验一次**（用仓库里的公钥），签错了在发布前就发现，
    而不是等设备端拒绝。

- **验签在解压之前**：先确认来源真实性，再碰包内容。签名不匹配直接拒绝，
  不做任何改动。

- **过渡策略**：包带 `.sig` 就**必须**验过（fail closed）；不带 `.sig` 时
  默认只记警告放行（存量发布还没签名，一刀切会锁死升级通道），
  配 `upgrade.require_signature=true` 则拒绝。这是"只验不拦 → 强制"的开关。

- 公钥放在 **`priv/`** 而不是新增顶层目录：升级包的目录白名单由**旧版代码**
  校验，新增顶层目录会让旧版拒绝新包——正是 v0.1.62「升级包自我阻塞」的
  同类坑。`priv/` 本来就在白名单内。

### Fixed

- `norm_upgrade` 保存升级设置时会**丢掉 `require_signature`**（它只返回
  `{"source"}`）。面板表单里没有这个字段，所以用户点一次保存就把强制验签
  静默关掉了——这类"静默降级"比没有开关更危险。现在未提供时保留原值。

### Notes

- 守卫新增 P3-31（公钥存在且位置正确、CI 必须签名、验签必须在解压之前、
  策略开关存在）。行为测试新增 3 个用例锁住"保存不丢开关"。
- 本地已验证：签名→用仓库公钥验签通过；改一个字节→验签失败。
- **仍需你操作一次**：把 `~/.dert-signing/dert-signing.key.pem` 的内容
  base64 后填入仓库 Secret `DERT_SIGNING_KEY`，之后的发布才会自动签名。
  填好并确认一次带签名的发布成功后，再把 `upgrade.require_signature` 打开。

## [0.1.73] - 2026-11-12

> 工业化重构 **P0-3（全脚本补 `set -u`）**。
> 未定义变量在 shell 里默认是空字符串，`rm -rf "$DIR"/` 这种写法在 `DIR`
> 拼错时会变成 `rm -rf /`。`set -u` 是唯一能挡住这类事故的开关。

### Security

- **6 个脚本补 `set -u`**：`priv/lib.sh`、`network/ppp/ip-up.d/dert`、
  `network/ppp/ip-down.d/dert`、`network/dispatcher/70-dert-dns`、
  `75-dert-pppoe-hotplug`、`80-dert-cake`。至此仓库内全部 20 个 shell 脚本
  都有该开关。

### Changed

- **钩子的位置参数改为 `${1:-}` 形式**。pppd 与 networkd-dispatcher 一定会
  传参，但手工调试时不带参数也不该因 `-u` 直接退出——保持"缺参就是空值"
  的原有行为，只是把静默变成显式。

- `lib.sh` 的 `set -u` 会作用到 source 它的 shell。当前三个调用方
  （`dert-priv` / `qdisc-apply` / `wan-addr-sync`）本来就都有 `set -u`，
  所以**实际行为不变**；写在这里是为了让"直接 source 本文件调试"时也不会
  静默吞掉未定义变量。

### 真机验证

- 5 个钩子**不带参数**逐个执行：全部 `rc=0`，无 `unbound variable`
- 真实重拨（触发 ip-down → ip-up 全链路）：journald 里无任何 `-u` 相关错误
- 钩子副作用正常：会话起点已记录、上游 DNS 已写入、`resolv.conf` 指向本机
- 五服务 active、0 失败单元、DNS 双路径正常、`.lan` 可解析、v4/v6/面板 200

### Notes

- 守卫新增 P3-30：**所有** shell 脚本必须有 `set -u`（按 shebang 自动收集，
  新增脚本自动纳入）。判定会剥掉行尾注释，避免说明文字里的 `set -u` 骗过断言
  —— 这个坑本轮又踩了一次（初版把 `"u" in tokens` 当成匹配，结果全部误报）。

## [0.1.72] - 2026-11-12

> 工业化重构 **P0-1（PPPoE 看门狗改为事件优先）**。
> 这是当前唯一还在持续产生无谓开销的地方：`dert-pppoe-watchdog.timer`
> 每分钟跑一次，**真机实测 24h 执行 1275 次**，每次起 1 个 unit + 1 个 shell
> + **2 个 python3 进程**（读同一份 `config.json`）。

### Changed

- **ppp0 消失改为事件驱动**。`dert-netwatch` 本来就订阅 rtnetlink，link 消失
  它是能立刻收到的，只是之前没接这一路。现在：
  - 收到「ppp0 不存在」→ 立刻调一次看门狗（打缺失标记）
  - 同时安排**一次** 180s 后的一次性复查（`systemd-run --on-active=180s --collect`）
  - 复查时若已恢复 → 清标记、什么都不做；仍缺失 → 重启拨号

  为什么还要 180s：拨号建立本身要时间，ppp0 刚消失就重启会打断正在重试的
  pppd。这是「给定宽限期后做判定」，不是猜「什么时候能好」——与 v0.1.65
  那个被撤掉的 `--on-active=20s` 有本质区别，后者是在猜外部事件的发生时刻。

- **timer 周期 1 分钟 → 15 分钟**，退为纯兜底（netwatch 挂掉或事件丢失时仍能
  收敛）。24h 执行次数从 1275 降到约 96。

- **看门狗里两次 `python3` 合并为一次**（读同一份 JSON 取「上网方式」和
  「WAN 口名」）。路径改为走 argv，不再拼进 Python 源码字符串。

- **升级通道补 `systemctl restart dert-pppoe-watchdog.timer`**：
  `enable --now` 对已在运行的 timer 是空操作，不重启的话新周期要等到下次
  开机才生效——这类「改了配置但没生效」最难排查。

### 真机验证

停掉 `dert-pppoe` 模拟 ppp0 消失：

```
22:39:11  netwatch: ppp0 消失，标记缺失并安排 180s 后复查
22:39:11  watchdog: 拨号服务未运行，拉起
22:39:15  恢复（4 秒）
22:42:16  180s 一次性复查执行 → 发现健康 → 不做任何事 → --collect 自动清理
          服务未被多余重启（启动时间仍是 22:39:11）
```

改动前最坏要 4 分钟（1 分钟轮询 + 3 分钟宽限），现在 **4 秒**。

### Notes

- 守卫新增 P3-29（timer 周期、netwatch 的 ppp0 分支、python3 合并为 1 处、
  升级必须重启 timer）。四项变异测试全部捕获。

## [0.1.71] - 2026-11-12

> 工业化重构 **P0-2（陈旧生成物回收）**。这一项要**修正一个此前的判断错误**：
> 我先前说「`apply_stage_files` 只写不删」——不准确。清理逻辑其实一直存在
> （`cleanup_stale_port_networks`），第三方审计报告也没注意到它。
> **真正的 bug 在那个函数的判定条件上**。

### Fixed

- **端口从 LAN 改成 WAN 后，旧的 `30-<口>.network` 不被清理**（真机 2026-11-12
  踩到过）。原判定是「口名不在 `ports` 里就删」，但生成器对 **WAN 口不产出**
  `30-*.network` —— 所以「仍在 `ports` 里、但角色变成 WAN」的口会被漏掉。
  残留文件内容是 `Bridge=br-lan`，于是 **WAN 口被塞进 LAN 桥**；当天是靠
  `10-wan.network` 字典序先匹配才没出事，属于随时会炸的定时炸弹。

### Changed

- **清理改为「按 manifest 对账」**，逻辑抽到 `priv/cleanup.py`（可单测）：
  以本次要部署的清单为唯一事实来源，清单里没有的生成物就是陈旧的。
  不再重复生成器的判断逻辑——两边各写一遍迟早会漂移，这次漂移的就是它。

  三道安全边界：
  1. 只在白名单目录 + 白名单文件名模式里找（`30-*.network`、`25-ppp0.network`、
     `dert.conf`、`dert-base.nft`、`peers/dert`、`chap-secrets`、`pap-secrets`），
     绝不遍历整个 `/etc`；
  2. 只删**首两行带「由 DebRT 生成」标记**的文件 —— 用户手写的同名文件不动
     （`dert-base.nft` 首行是 shebang，标记在第二行，所以看两行）；
  3. 清单读不出来就一个都不删。

  每条删除写 journald（`logger -t dert-apply`），事后可倒查。清理脚本缺失时
  也会留一条日志，避免"清理悄悄失效"长期无人发现。

- 清理时机不变，仍在 `apply_stage_files` **之后**（部署成功才删）：
  `do_config_set` 正常路径、回滚重放路径、`do_apply_all` 三处。

### Added

- `tests/test_cleanup.py`（13 个用例，用 `--root` 注入假根目录，不碰真实 `/etc`）：
  覆盖 LAN→WAN 角色变更、端口移除、PPPoE 残留、**无标记的用户文件不被删**、
  白名单外文件不动、清单缺失/损坏/结构不符时零删除，以及 `--apply` 与
  观察模式的行为差异。

### Notes

- **测试抓到一个真安全漏洞**：初版 `plan()` 用 `man.get("files", [])`，于是
  「能 `json.load` 但没有 `files` 键」的清单（例如错误输出
  `{"ok":false,"code":"CONFIG_SCHEMA"}`）会让 `keep` 为空 → **把磁盘上所有
  生成物一起删掉**。已改为要求 `files` 是非空列表，否则零删除。
- 守卫新增 P3-28（旧函数不得回归、三个调用点、`cleanup.py` 存在且保留
  三道边界）。四项变异测试全部捕获。
- 真机验证：造「带标记的陈旧文件」+「无标记的用户文件」，dry-run 只列出前者、
  不删任何东西；`--apply` 只删前者并留日志；随后制造配置漂移跑 `apply-all`，
  陈旧文件在部署后被清理，网络与服务无影响。

## [0.1.70] - 2026-11-12

> 工业化重构 **P0.5（审计修复）**。来自一份第三方独立审计报告的复核结果：
> 我们逐条核对后确认其结论可信（行号与代码全部对得上），据此修掉
> 字段级校验单点化、下载加固、静态分析缺失等问题。**运行时行为无变化**。

### Security

- **字段级安全断言下沉到收敛侧**（审计 F-02）。`validate.py` 的字段校验只在
  「面板写入」这一条路径生效，而备份恢复、回滚、升级后收敛都是直接读
  `config.json`——这些字段会被原样拼进 dnsmasq / nft / network 文件，
  一个换行就能注入新指令（dnsmasq 的 `dhcp-script=` 会以 root 执行脚本）。
  现在 `netgen.schema_check` 补三层断言，它在 `--schema` / `--check` /
  `--stage` 与所有收敛路径上都会跑，一处覆盖全部入口：
  1. 递归扫描整份配置，拒绝任何含控制字符（`\x00-\x1f\x7f`）的字符串；
  2. `static[].name` 走与 validate 相同的字符集（**逗号**会错位
     `dhcp-host={mac},{ip},{name}` 的字段），`mac` 走 MAC 正则；
  3. `portfw` 的 `wport/lport` 限定为数字与 `-`（会被直接拼进 nft 表达式）。

  刻意**只拦会导致注入的字符、不碰语义**：存量 `/25` 掩码之类仍放行，
  否则 `backup-restore` 会因噎废食拒绝恢复（该取舍在原代码注释里已论证）。
  因此也**没有**把 `--schema` 升级为完整 `--check`——那会拒绝合法的旧备份。

- **升级下载加固**（审计 F-01 部分）。清单里的 `url` 强制 `https://`
  （`curl` 会照做 `file://` 等协议，被篡改的清单可借此把任意本地文件
  当成"升级包"）；下载加 `--max-filesize 268435456`，恶意/错误源不能写满磁盘。
  注意：**发布签名（真实性锚点）仍未做**，属于 P3，需先定密钥方案。

### Added

- **CI 接入 shellcheck**（审计 F-07）。1,744 行 bash 是全项目最大风险面
  （历史 bug 注释大多出自这里），此前只有 `bash -n` 语法检查。
  基线已清零（`--severity=warning`），因此按 warning 级门控；
  `SC2034`（未使用变量，12 处历史常量声明）暂时排除，留待单独清理。
  脚本清单由 shebang 自动收集，新增脚本不会漏检。
- 行为测试新增 `tests/test_netgen_safety.py`（11 个用例）：静态绑定换行/
  逗号/MAC 格式、转发端口注入、深层路径控制字符、以及"合法值不能被误拦"
  的正向用例（小写 MAC、端口区间、正常静态绑定）。

### Changed

- 删除 `netgen` 输出的 `restart` 字段——它与 `dert-priv` 里
  `plan_for_section` 语义重复，全仓库无消费者（审计小项）。
- `dert-priv:1071` 的 `local x="$(...)"` 拆成声明与赋值两行（`SC2155`：
  同行写法会掩盖命令替换的返回码）。

### Fixed

- `install.sh` 中 `systemctl enable --now dert-netwatch.service` 的缩进
  与相邻语句不一致（v0.1.67 引入，能跑但误导）。

### Notes

- 守卫新增 P3-25（字段安全断言存在且对整份配置生效）、P3-26（下载加固）、
  P3-27（CI 必须含 shellcheck 且自动收集脚本）。四项变异测试全部捕获。
- **审计报告漏掉的一项**：`apply_stage_files` 只写不删（端口角色变更后旧
  `30-<口>.network` 永久残留，真机已发生过一次冲突）。这一项**本轮未修**，
  已登记为 P0-2 并上调优先级，将紧随本版之后处理——它的删除逻辑必须在
  字段级断言就位之后做，否则误删风险更高。

## [0.1.69] - 2026-11-12

> 工业化重构 P0-4 + P1：**运行时行为零变化**，只补测试地基与去掉一处坏味道。

### Added

- **行为测试层 `tests/`**（45 个用例，`python3 -m unittest discover -s tests -t .`）：
  断言的是**生成产物的内容**和 **CLI 的输出**，不再对源码做字符串包含断言。
  覆盖：`25-ppp0.network` 的 `KeepConfiguration`/默认路由、`dnsmasq` 的
  `bind-dynamic`、`nft` 的 `dns_bypass` 与 `wan_in`、端口角色到 `30-*.network`
  的映射、manifest 契约（相对路径 / 无穿越 / 每个文件有 mode 与内容），
  以及 validate 的跨段组合与各字段拒绝码。
  设计约束：**不需要 root、不读写 `/etc`、不依赖真机**。
- CI 拆成两段：`fast-tests`（秒级，无 root）先跑，`real-path-smoke`
  通过 `needs: fast-tests` 依赖它 —— 快测挂了不再浪费 25 分钟的集成跑。

### Changed

- `dert-priv` 解析升级清单不再用 `eval`：改为 python 输出两行、bash 用
  `read` 读取。原写法虽有 `shlex.quote` 保护，但「执行动态生成的代码」本身
  是坏味道，将来谁改了引号处理就变成命令执行。改后全程不经 shell 求值。
  行为等价（URL 与 sha256 不可能含换行，去掉 `\r\n` 是安全的），
  已用 7 种边界输入验证（正常 / 缺字段 / 非法 JSON / 类型异常 / 注入载荷）。

### Notes

- 守卫新增 P3-23（行为测试层存在、不得依赖 root、CI 必须 fast 门控）
  与 P3-24（`dert-priv` 不得再用 `eval`）。后者**只检查非注释行**——
  源码文本断言被自己的说明文字绊倒已经踩过三次，这次一并修掉判定方式。
- 本轮不改任何生成配置、不改任何服务单元、不动 sing-box/Tailscale/podman
  的调用逻辑。

## [0.1.68] - 2026-11-12

### Fixed

- **路由器自身解析不了局域网主机名**（真机 2026-11-12 实测）：pppd 的 `usepeerdns`
  会让 `/etc/ppp/ip-up.d/0000usepeerdns` 把 `/etc/resolv.conf` 改写成运营商 DNS，
  于是**路由器自身绕过 dnsmasq**——`dig <host>.lan` 返回空、`getent hosts` 空，
  而 `dig @127.0.0.1 <host>.lan` 正常；同时吃不到 dnsmasq 的 5000 条缓存。
  运营商上游本来就已由钩子写进 `/run/dert-dns/upstream.conf` 交给 dnsmasq，
  所以 `ip-up.d/dert` 与 `ip-down.d/dert` 现在把 resolv.conf 原子写回
  `nameserver 127.0.0.1` + `::1`（tmp+mv，避免读到半个文件）。
  两个钩子按字母序都排在 `0000usepeerdns` 之后，能覆盖它。
- **`ppp0` 的 IPv4 会被 networkd 清掉**（真机 2026-11-12 实测，**会导致 WAN 断网**）：
  networkd 重新配置 link 时默认删除「不是自己配的」地址，而 ppp0 的 IPv4 是 pppd
  用 ioctl 加的（外来配置）。实测 `networkctl reconfigure ppp0` 之后 ppp0 只剩
  DHCPv6 地址、IPv4 与默认路由全消失，v4 出网直接 `000`，必须重拨才恢复
  （pppd 进程始终存活，它自己不会重加地址）。
  触发场景不止手工命令——`apply-all` 在**仅 LAN 设置**变更时只重启 networkd
  不重启 pppoe（`plan_for_section("lan")` 的动作串里没有 pppoe），用户改个
  LAN IP 就会把 WAN 弄断，很难联想到根因。
  修法：`25-ppp0.network` 加 `KeepConfiguration=yes`。实测 reconfigure 后
  IPv4 保留、v4/v6 出网均 200。
- **面板启动被 `network-online.target` 拖住**：该目标要等
  `systemd-networkd-wait-online` 判定所有受管接口就绪，而 br-lan 在网线没插时
  永远不 routable → 面板被硬拖到超时（默认 90s+）才起来。
  面板只服务局域网，WAN 通不通与它无关，改为只依赖 `network.target`
  （与 sing-box drop-in 里 `wait-wan` 的判断一致）。

### Changed

- `25-ppp0.network` 去掉 `Scope=link`：IPv6 路由没有 scope 概念，networkd 每次开机
  都报 `Scope= is specified for IPv6 route. It will be ignored.`，实际建起默认路由的
  是 `Destination=::/0`。去掉后默认路由仍正常建立（实测 `default dev ppp0 proto static`）。
- `uninstall.sh` 对 `LIB_DIR`/`ETC_DIR`/`VAR_DIR` 加 `:?` 断言：这三个变量后面直接
  喂给 `rm -rf`，为空或为根时后果不可逆。

### Notes

- 守卫新增 P3-22：resolv.conf 必须指回 dnsmasq 且原子写（ip-up/ip-down 各一条）、
  面板不得依赖「在线」目标、ppp0 必须有 `KeepConfiguration=yes`、不得有 `Scope=link`。
  四项变异测试全部捕获。
- 真机一次性清理（不进代码，属改名迁移与另一项目的历史残留）：
  `dnsmasq.service.d/10-seedrou-restart.conf`（与 dert 版重复，`ExecStartPre` 跑两次）、
  `network/30-enp4s0.network`（把 **WAN 口 enp4s0 声明为 br-lan 成员**，靠
  `10-wan.network` 字典序先匹配才没出事）、以及 7 个 `linux-router` 项目的残留文件。
  已归档到 `/root/leftover-cleanup-*.tar.gz` 后删除。

## [0.1.67] - 2026-11-12

### Changed

- **IPv6 前缀变化改为真正的事件驱动**：新增 `priv/netwatch`（常驻订阅 rtnetlink）
  与 `systemd/dert-netwatch.service`，`br-lan` 地址一变就立刻对账——重新生成
  DNS 分流配置、刷新 nft 表里白名单的 v6 DNAT 目标、必要时重启 sing-box 重建
  `inet6_local_address_set`。
  v0.1.65 的做法是在 ppp `ip-up` 里 `systemd-run --on-active=20s` 延迟调钩子，
  本质是**把时序不确定性换成一个猜出来的常数**（真机实测 PD 委派耗时随 BRAS 波动）。
  现在不再猜任何时间。

### Removed

- `network/ppp/ip-up.d/dert` 里的 `systemd-run --on-active=20s` 延迟调用。
- `90-dert-lan-prefix` 里 `while [ -z "$NEW" ]; sleep 1` 的 15 秒轮询。
  该钩子现在由 netwatch 在地址事件里调用，前缀必然已在位；而它若被
  networkd-dispatcher 在 PD 委派前调到，直接返回即可（地址落地后 netwatch 会再调）。
  保留等待的害处是：netwatch 的对账是串行的，钩子阻塞 15 秒会把后续事件一起拖住。

### Fixed

- **`dns-split-sync` 重复运行会累积 nft 规则**（预先存在的 bug，被 netwatch 暴露）：
  `nft -f` 里直接写 `table inet dert_dns { ... }` 对**已存在**的表是**追加规则**
  而不是替换，每跑一次就多 4 条。真机实测 netwatch 在运行期触发几次后表里堆到
  **20 条**，最后几条还指向已删除的临时地址。改为在同一个事务里
  `add table` + `delete table` + 重新定义（add 是幂等的，表在不在都成功），
  既保持原子性又做到真替换。实测连续跑 5 次稳定在 4 条。
  之前没暴露是因为 sync 只在 sing-box 启动时跑，而重启会先 `teardown` 删表。
- **v6 DNAT 目标地址选取不确定**：原来用 `awk ... exit` 取「第一个」全局地址，
  但 br-lan 上可能不止一个（多前缀委派、手工加的静态地址、隐私地址），谁先出现
  取决于内核返回顺序——地址集合一变就可能选中另一个，导致表反复重建，甚至指向
  一个随后消失的地址。改为：排除 `temporary`（RFC 4941 隐私地址会轮换）→
  优先 `dynamic`（PD 委派来的，有 `valid_lft`）→ 同档内按字典序取第一个。
  `netwatch` 的 `lan6()` 与 `dns-split-sync` 用完全相同的规则（否则「当前地址」
  与「表里的目标」会永远判为不一致）。
- **`dns-split-sync` 的等待判据错位**：原来等 `tun0` 出现（最多 20 秒），但本表
  的目标地址全在 `br-lan` 上，根本不引用 tun0；tun 建好不等于 5353 已 bind，
  反过来 5353 也可能先于 tun 就绪。改为等 **5353 真正在监听**（最多 10 秒）。

### Notes

- **为什么不用 networkd-dispatcher**：它只看链路的 operational state，而 PD 委派
  只改地址、不改状态。Debian 包描述原文：*"much more limited in the types of events
  it supports due to the limited nature of systemd-networkd"*。真机日志里 ppp0 重拨
  时它只打 `WARNING:Unknown index 56 seen, reloading interface list`，不派发钩子。
- **为什么不用 systemd `.path` 盯 `/run/systemd/netif/links/<ifindex>`**：实测确实
  能触发（networkd 原地重写该文件、inode 不变），但 systemd 的稳定性承诺
  （systemd.io/PORTABILITY_AND_STABILITY）把 `/run/systemd/*` 列为内部协议，
  不在稳定接口清单里——依赖它是押注实现细节。业界（OpenWrt netifd、NetworkManager、
  dhcpcd）一律订阅 rtnetlink，那是内核稳定 ABI。
- **`dert-pppoe-watchdog.timer` 的每分钟轮询保留不动**：它做的是「服务活着但资源
  没了」的**健康检查**，周期性是该模式的正当形态（kubelet probe、`WatchdogSec`、
  monit 都如此），不属于"猜时序"。
- netwatch 的对账是**幂等**的：每条事件都重新读一遍真实状态，不解析事件类型、
  也不按秒去抖。重拨的事件序列是「旧地址删除 → 新地址 tentative → 新地址
  scope global」，按时间去抖会把最后那个「地址可用」事件丢掉，前缀就永远补不上
  ——那正是 v0.1.65 之前那个 bug 的形态。
- 守卫 P3-19 重写：断言 ip-up 不得再出现延迟调用、netwatch 必须订阅 rtnetlink
  且对账里不得有 sleep、单元必须 `Restart=always`、三通道（安装/升级/卸载）必须
  同步、sync 必须等 5353 而非 tun 接口、nft 必须做原子替换（`add`+`delete` 同事务）、
  v6 目标必须确定性选取且优先 `dynamic`。七项变异测试全部捕获。

## [0.1.66] - 2026-11-12

### Fixed

- **`90-dert-lan-prefix` 的 `cfg_covers()` 恒为假**（v0.1.64 重构遗留）：它比对的是
  `00-dert-dns.json` 里 `dns.rules[].source_ip_cidr`，而 v0.1.64 已把该字段整个删除
  （白名单 DNS 改由 nft 按 MAC 分流，不再靠源 IP 判断）。结果是「基线缺失」分支
  **每次都会无谓地重启一次 sing-box**——`/run` 是 tmpfs，重启后基线由
  `dns-split-sync` 在 ExecStartPost 重建，钩子抢先运行时就会走到该分支。
  改为：写入基线后只调 `dns-split-sync` 刷新 nft 表（它按当时的 br-lan 地址重算
  v6 DNAT 目标），既不 `prepare` 也不重启。配置里已无任何前缀相关字段，
  重启的唯一必要理由是重建 `local_address_set`，那只在**前缀真的变了**时才需要。

### Changed

- 清理 `90-dert-lan-prefix` 里两处过时注释：仍写着「`source_ip_cidr` 里含旧前缀，
  所以要在重启前重写配置」，与实际不符。

### Notes

- 守卫 P3-19 的三条断言反转：`cfg_covers` 与 `source_ip_cidr` 必须**不出现**在钩子里，
  且首次记录分支必须调用 `dns-split-sync`。两项变异测试全部捕获。

## [0.1.65] - 2026-11-12

### Fixed

- **PPPoE 重拨换前缀后 nft 的 v6 DNAT 目标停在旧前缀**（真机 2026-11-12 复现）：
  `90-dert-lan-prefix` 钩子依赖 networkd-dispatcher 的 `routable` 事件，但 ppp0
  重拨并不产生该事件（24 小时内只执行过 2 次，都在上一次开机）。重拨后 br-lan
  前缀从 `a50:3d81` 变为 `a53:bf71`，nft 表里的 `dnat ip6 to` 仍是旧前缀，白名单
  设备的 v6 DNS 全部打向不存在的地址，实测 437 个包落空。
  修法：在 `network/ppp/ip-up.d/dert` 里用 `systemd-run --on-active=20s` 主动调用
  一次前缀检查。延迟 20 秒是因为 ip-up 时 PD 还没委派到 br-lan；用 `systemd-run`
  而不是 `&` 是因为 pppd 退出会连带杀掉子进程；`--collect` 让 unit 跑完自动清理，
  避免每次重拨堆积一个 unit。

### Changed

- `priv/dns-split-sync` 不再读 tun 的 DNS 地址（`TUN4`/`TUN6` 死代码）。v0.1.64 起
  白名单的 DNS 目标是 `br-lan:5353`，与非白名单的 `br-lan:53` 同机不同端口，与 tun
  内部地址无关。原来那句 `[ -n "$TUN4" ] || exit 0` 只在订阅结构异常时才会误退出，
  属逻辑过时而非功能缺陷。
- `priv/dns-split-prepare` 移除 `LAN6`、`CARRIER` 及相关参数。`direct` inbound 监听
  `::` 双栈，不绑具体 v6 地址；非白名单已改为 nft 直连 dnsmasq，不再需要
  「按源 IP 分流到运营商」的规则。
- 修正两处过时注释：`dns-split-sync` 头部的「行为矩阵」在 v0.1.64 反转方向后仍写着
  旧语义（白名单走 dnsmasq、其余走 sing-box），与实际相反；`dns-split-prepare` 的
  版本区间标注不一致。

### Notes

- 守卫 P3-19 新增 4 条断言：`ip-up` 必须引用 `90-dert-lan-prefix` 且用
  `systemd-run --on-active`；`dns-split-sync` 不得再出现 `TUN4`/`TUN6`；
  `dns-split-prepare` 不得再出现 `CARRIER`。四项变异测试全部捕获。

## [0.1.64] - 2026-11-12

### Changed

- **DNS 按 MAC 分流改为完全在 nft 层完成**。白名单设备的 53 端口由 nft 的
  `ether saddr` 匹配后 DNAT 到 `192.168.10.1:5353`（sing-box 的 direct inbound），
  非白名单 DNAT 到 `dnsmasq:53`。`00-dert-dns.json` 随之简化到只剩
  `hijack-dns` 和 `.lan → landns` 两条，`source_ip_cidr` 与 `carrier` 全部移除。
  判定依据从「源 IP」换成「二层 MAC」，不受 DHCP 换 IP、邻居表过期影响。
  真机验证：停掉 sing-box 后非白名单设备 DNS 与出网全部正常（`dert_dns` 表由
  teardown 自动删除，DNS 回落 dnsmasq）。

### Added

- `priv/netgen.py` 生成 `dns_bypass` 链：给 dnsmasq 的上游 DNS 查询打 `0x2024`。
  不加这条的话，dnsmasq 的查询会被 sing-box 的 `output` 链末尾打上 `0x2023`，
  再经 `ip rule 9001 → table 2022 → tun0` 送进 tun，被 `hijack-dns` 拦截后返回
  fakeip —— 非白名单设备拿到假地址直接断网（真机复现，dnsmasq 日志显示它以为
  自己在问运营商）。

### Fixed

- **5353 只监听 IPv4**：nft 把白名单设备的 v6 DNS DNAT 到 `[br-lan GUA]:5353`，
  但 inbound 只绑了 `192.168.10.1`，v6 查询全部 `connection refused`
  （真机实测 437 个包落空）。改为 `listen: "::"` 双栈监听。
- **`hijack-dns` 带 `protocol` 条件导致无限自环**：`direct` inbound 不做 sniff，
  `protocol: "dns"` 永远识别不出来，规则静默不匹配后走默认 `direct` outbound，
  把查询原样转发回 `192.168.10.1:5353`（它自己）——真机实测 CPU 130%、
  2 分钟 7414 次查询、持续上传。去掉 `protocol`，只匹配 `inbound`。

### Notes

- **需要手动改 substore 模板**：tun inbound 需加 `"dns_mode": "disabled"`。
  它关掉的是 `auto_redirect` 自动安装的 `ip saddr @local_address_set dport 53
  dnat`（prerouting 和 output 链各一条），不影响 TCP redirect 与流量代理。
  不改的话 substore 同步后白名单 DNS 会被 `auto_redirect` 抢走。
- 守卫 P3-19 同步更新：断言双栈监听、`hijack-dns` 不带 `protocol`、
  `source_ip_cidr`/`carrier` 已移除、`dns_bypass` 存在且打 `0x2024`
  （三项变异测试全部捕获）。

## [0.1.63] - 2026-11-11

### Fixed

- **dnsmasq 在 PD 委派的 GUA 上不监听 53 端口**（真机 2026-11-11 复现）：`bind-interfaces`
  只在启动瞬间静态绑定当时的地址，而开机时序是 dnsmasq 先起（br-lan 仅有 `fe80::`）、
  4 秒后 PD 委派的 GUA 才出现，GUA 上的 53 永久缺失。dns-split 的 nft 表恰好把白名单
  设备的 v6 DNS DNAT 到该 GUA，实测 6281 个包全部 `connection refused` —— iPhone 每次
  新域名先撞一次 v6 失败再回落 v4，表现为「境外站和公众号初次打开偏慢」。
  改为 `bind-dynamic`：跟踪接口地址变化，GUA 出现或 PPPoE 换前缀后自动补绑。
  守卫补 P3-21（含变异测试）。

## [0.1.62] - 2026-11-11

### Fixed

- **升级包自我阻塞**：`do_upgrade_apply` 的成员白名单是「允许清单」，新版本一旦新增顶层
  文档（`LICENSE` / `CHANGELOG.md` 等），旧版代码就会以「包内含不认识的条目」拒绝整包
  ——而执行校验的恰是旧版代码，无法自愈（v0.1.61 真机实测踩到）。
  改为只拦危险路径（绝对路径 / 含 `..` / 未知目录），顶层文件一律放行：
  它们只能落在包根，无法路径穿越，后续也只会 `cp priv/*` 到程序目录，不会被部署到系统路径。

## [0.1.61] - 2026-11-11

### Fixed

- **发布包不含许可证与文档**：`dert-<ver>.tar.gz` 里没有 `LICENSE` / `CHANGELOG.md` /
  `CONTRIBUTING.md` / `SECURITY.md`，下载源码包的人无从得知授权条款。已加入 release
  打包清单，并同步放行升级包白名单（`ALLOW_FILE`）——否则 `upgrade-apply` 会以
  「包内含未知文件」整包拒绝。
- 守卫补 P3-20：打包清单与白名单任一处漏掉上述文件即失败。

## [0.1.60] - 2026-11-11

### Changed

- 移除改名迁移遗留代码：`migrate_seedrou_to_dert()` 及其调用（`install.sh`、`priv/lib.sh`、
  `priv/dert-priv`），以及 `uninstall.sh` 末尾的 seedrou 旧布局清理段。
  新装机器不存在旧布局，这些逻辑已是死代码；`priv/lib.sh` 里那份还引用了未定义的
  `c_info`，本身就有隐患。

### Fixed

- `uninstall.sh` 的 `rm -f` 清单缺续行符，导致命令提前结束——其后以 `\` 结尾的行被当成
  独立命令执行，报错且**后续文件删不掉**。同时清理了与 `rm -rf "$LIB_DIR"` 重复的条目、
  合并了重复出现的 `chap-secrets`。

### Docs

- 新增 `LICENSE`（MIT）、`CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`、`.editorconfig`。
- `README.md` 增加「许可证与相关文档」章节。

## [0.1.59] - 2026-11-11

### Fixed

- **重启或重拨换前缀后 v6 DNS 分流静默失效**：`00-dert-dns.json` 的 `source_ip_cidr` 会只剩
  v4 网段，导致非白名单设备的 AAAA 查询落到 fakeip（`fc00::2`）→ v6 黑洞。设备靠
  happy-eyeballs 回落 v4，表现为"偶发变慢"，极难发现。
  - `dns-split-sync`（ExecStartPost）现在写入 `/run/dert/lan-prefix` 基线——开机时
    dispatcher 的 `routable` 事件常早于 PD 委派，钩子取到空前缀后静默退出，基线会永久缺失；
  - `90-dert-lan-prefix` 首次记录分支不再无条件退出，改为比对配置里的 v6 前缀
    （`cfg_covers`），缺失则重新生成配置并重启 sing-box；
  - 前缀变更钩子的重启条件从「`auto_redirect` 在用」改为「sing-box 在运行」，避免未启用
    `auto_redirect` 时永远读不到新的 v6 `source_ip_cidr`。

## [0.1.58] - 2026-11-11

### Changed

- **项目更名 SeedRou → DebRT**。目录、systemd 单元、dispatcher 钩子、环境变量、nft 表名
  全部随之调整：

  | 项目 | 旧 | 新 |
  |---|---|---|
  | 程序目录 | `/usr/local/lib/seedrou` | `/usr/local/lib/dert` |
  | 配置目录 | `/etc/seedrou` | `/etc/dert` |
  | 数据目录 | `/var/lib/seedrou` | `/var/lib/dert` |
  | 单元/钩子 | `seedrou-*` | `dert-*` |
  | 环境变量 | `SEEDROU_*` | `DERT_*` |
  | nft 表 | `seedrou_base` / `seedrou_dns` | `dert_base` / `dert_dns` |

## [0.1.57] - 2026-11-11

### Added

- **DNS 按 MAC 分流**：nft 层按 `ether saddr` 把白名单设备的 53 端口回导 dnsmasq
  （拿 fakeip → 流量进 tun 走代理），其余设备 DNAT 到 sing-box 的 tun DNS（拿真实 IP）。
  修复非白名单设备拿到 fakeip 却无人接管、连接永久 `SYN_SENT` 的结构性缺口。
- 三个钩子：`dns-split-prepare`（启动前按 PD 前缀生成配置）、`dns-split-sync`
  （启动后派生 nft 表）、`dns-split-teardown`（停止时删表回落 dnsmasq）。

### Fixed

- `singbox-cleanup` 用 `grep "table sing-box"` 匹配 `nft list tables` 的输出
  （实际是 `table inet sing-box`），导致 OnFailure 清理**从未生效**。
- 白名单计数用 `tr | wc -l` 少算最后一台（末项无换行）。

## [0.1.56] - 2026-11-09

### Fixed

- 重拨换 IPv6 前缀后 sing-box 代理失败、旧前缀路由残留：新增 `90-dert-lan-prefix`
  dispatcher 钩子，在前缀变化时清理旧 RA 路由并重启 sing-box 重建本地地址集合。

## [0.1.55] - 2026-11-09

### Fixed

- 重拨后运营商 IPv6 上游 DNS 丢失（no-carrier 事件删除上游文件 + 事件时序双根因）。

## [0.1.54] - 2026-11-09

### Fixed

- Safari 标签页无图标：补齐 PNG/ICO 位图 favicon，面板直接响应 `/favicon.ico`。

## [0.1.53] - 2026-11-09

### Fixed

- 上游 DNS 的 v4 来源补齐：dispatcher 的 `ppp*` 分支同时读 `/etc/ppp/resolv.conf`，
  `CONF` 丢 v4 后能自愈。

## [0.1.52] - 2026-11-09

### Fixed

- 真机重启复盘三项：收敛过程泄漏 stage 目录（含明文宽带密码）、dnsmasq 开机报
  `cannot read upstream.conf`、`apply-all` 预检错误详情被吞。

## [0.1.51] - 2026-11-09

### Fixed

- 外部审计（1 P2 + 11 P3 + 3 观察）逐条核实并修复 13 项。
- 升级通道收敛加 CI 冒烟网关：`install.sh --smoke` 落标记，`do_upgrade_apply` 据此跳过
  内联收敛与延迟重启，避免 runner 上重启 networkd 导致 job 挂死。

## [0.1.50] - 2026-11-09

### Fixed

- v0.1.49 复审收尾五项：磁盘诊断标记写节流、密码验证成功后清零失败计数（避免陈旧计数
  误触锁定）、补五条守卫断言、撤销表 `.tmp` 并发写唯一化、`sb seturl` 内网黑名单补齐。

## [0.1.48] 及更早

见 [GitHub Releases](https://github.com/wangyr2008/DebRT/releases)。

---

[Unreleased]: https://github.com/wangyr2008/DebRT/compare/v0.1.64...HEAD
[0.1.64]: https://github.com/wangyr2008/DebRT/compare/v0.1.63...v0.1.64
[0.1.63]: https://github.com/wangyr2008/DebRT/compare/v0.1.62...v0.1.63
[0.1.62]: https://github.com/wangyr2008/DebRT/compare/v0.1.61...v0.1.62
[0.1.61]: https://github.com/wangyr2008/DebRT/compare/v0.1.60...v0.1.61
[0.1.60]: https://github.com/wangyr2008/DebRT/compare/v0.1.59...v0.1.60
[0.1.59]: https://github.com/wangyr2008/DebRT/compare/v0.1.58...v0.1.59
[0.1.58]: https://github.com/wangyr2008/DebRT/compare/v0.1.57...v0.1.58
[0.1.57]: https://github.com/wangyr2008/DebRT/compare/v0.1.56...v0.1.57
[0.1.56]: https://github.com/wangyr2008/DebRT/compare/v0.1.55...v0.1.56
[0.1.55]: https://github.com/wangyr2008/DebRT/compare/v0.1.54...v0.1.55
[0.1.54]: https://github.com/wangyr2008/DebRT/compare/v0.1.53...v0.1.54
[0.1.53]: https://github.com/wangyr2008/DebRT/compare/v0.1.52...v0.1.53
[0.1.52]: https://github.com/wangyr2008/DebRT/compare/v0.1.51...v0.1.52
[0.1.51]: https://github.com/wangyr2008/DebRT/compare/v0.1.50...v0.1.51
[0.1.50]: https://github.com/wangyr2008/DebRT/compare/v0.1.49...v0.1.50
