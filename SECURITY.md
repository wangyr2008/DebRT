# 安全策略

## 报告安全问题

**请不要开公开 issue。** 请通过以下任一方式私下联系：

- GitHub Security Advisories：仓库页 → Security → Report a vulnerability
- 邮件：yunrui.wong@gmail.com（标题前缀 `[DebRT Security]`）

请在报告中包含：

- 受影响版本（`cat /usr/local/lib/dert/share/VERSION`）
- 问题类型与影响（如提权、认证绕过、凭据泄漏、远程命令执行）
- 复现步骤或 PoC
- 你评估的严重程度

**请勿附带**真实的 `/etc/dert/secrets.json`、`/etc/ppp/chap-secrets` 或生产环境节点信息。

## 响应

| 阶段 | 目标 |
|---|---|
| 确认收到 | 7 天内 |
| 初步评估与严重度定级 | 14 天内 |
| 修复与发布 | 视严重度，通常 30 天内 |

修复发布后会在 [CHANGELOG](CHANGELOG.md) 中记录，并在 GitHub Release 说明里标注安全影响。

## 支持范围

只对最新发布版本提供安全修复。`0.x` 阶段不维护旧版本分支，请保持升级。

## 设计上的已知边界（非漏洞）

以下是有意为之的设计，不作为安全问题受理，但**部署时请知悉**：

- **面板为 HTTP 明文**：面向局域网使用。如需公网访问，请自行在前置反向代理加 TLS，
  并限制来源 IP。
- **面板密码使用 PBKDF2 存储**，但会话 Cookie 在 HTTP 下可被同网段嗅探。
- **sudoers 白名单**：面板用户 `dert` 只能通过 `/usr/local/lib/dert/dert-priv` 执行特权操作，
  子命令在脚本内部二次校验。绕过需先取得 `dert` 用户权限。
- **`/etc/ppp/chap-secrets` 含宽带账号与明文密码**（pppd 要求明文，权限 0600）。
  卸载脚本会清除它；转手设备前请确认已卸载或手动删除。
- **备份包不含 secrets**：恢复后需重新输入宽带密码与管理密码。

## 加固建议

- 面板不要暴露到公网；确需远程访问请走 VPN（如 Tailscale / WireGuard）
- 定期 `apt upgrade`，尤其关注 `dnsmasq`、`pppd`、`sing-box` 的安全通告
- 管理密码使用 12 位以上随机串（安装时可自动生成）
- 开启 SSH 密钥登录并禁用密码登录（`/etc/ssh/sshd_config`）
