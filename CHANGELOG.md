# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)（`主版本.次版本.修订号`），
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

当前处于 `0.x` 阶段：次版本号可能包含不兼容变更，升级前请阅读对应条目。

## [Unreleased]

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

[Unreleased]: https://github.com/wangyr2008/DebRT/compare/v0.1.59...HEAD
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
