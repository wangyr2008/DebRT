# 贡献指南

感谢愿意改进 DebRT。这个项目面向"把一台 x86 小主机变成有线路由器"的场景，
改动会直接影响真实网络的连通性，所以流程上偏保守。

## 报告问题

开 issue 时请附上：

- DebRT 版本（`cat /usr/local/lib/dert/share/VERSION`，或面板右下角）
- Debian 版本（`cat /etc/os-release`）
- 复现步骤与预期/实际行为
- 相关日志：
  ```sh
  journalctl -u dert-panel -n 50 --no-pager
  journalctl -u dert-apply -n 50 --no-pager
  journalctl -u sing-box -n 50 --no-pager
  sudo nft list ruleset
  ```
- 面板「状态」页的导出（**注意先脱敏**：宽带账号、公网 IP、节点地址）

**请勿**在 issue 里贴 `/etc/dert/secrets.json` 或 `/etc/ppp/chap-secrets`。

## 提交改动

1. Fork 并新建分支，分支名建议 `fix/<简述>` 或 `feat/<简述>`
2. 改动尽量聚焦——一个 PR 解决一个问题，便于审查和回滚
3. 提交前在本地跑完验证（见下）
4. PR 描述里写清楚：**为什么改**、改了哪里、怎么验证的

### 提交信息

沿用仓库现有风格：

```
<type>: v<版本>——<一句话摘要>

<详细说明：根因、方案、影响面>
```

`type` 取 `feat` / `fix` / `release` / `docs` / `refactor`。

涉及版本发布的提交请同时更新 `VERSION`（CI 会校验 tag 与 `VERSION` 一致）。

## 本地验证

改动**必须**在提交前跑通这三项（CI 也会跑）：

```sh
python3 .github/scripts/test_guards.py      # 审计守卫（几百条断言）
python3 .github/scripts/check_inline_py.py  # 所有内嵌 Python 段可编译
python3 .github/scripts/test_leases.py      # 租约解析单测
```

Shell 语法：

```sh
for f in install.sh uninstall.sh priv/* network/dispatcher/*; do
  [ -f "$f" ] && bash -n "$f"
done
```

> 守卫测试请**裸跑**看真实退出码，不要用 `| tail` 之类的管道——管道会掩盖退出码，
> 这个坑在本项目历史上已经踩过两次（假绿放行）。

### 加守卫断言

任何"修好了"的结论都应当有对应断言，否则下一个人重构时很容易改回去。
断言写在 `.github/scripts/test_guards.py`，格式参考现有条目：

```python
assert re.search(r'关键实现片段', _file_content), \
    "P3-XX 回归：<一句话说明破坏后的后果>"
```

断言的报错信息要写**后果**，而不是"某函数不存在"——排查时前者能直接定位影响。

## 代码风格

- **Shell**：独立脚本用 `#!/usr/bin/env bash` + `set -u`；被 `source` 的库
  （`priv/lib.sh`）**不要**设 `set -e`，避免影响调用者的 shell 选项。
- **注释写"为什么"**，不写"是什么"。例如不要写"遍历 ports"，而要写
  "PD 委派与 routable 事件常同秒竞争，br-lan 可能还没拿到新地址，所以这里要等"。
- **错误处理**：对外部命令失败要有明确分支，`|| true` 只在"失败也无所谓"时使用，
  并就近写一行注释说明为什么可以忽略。
- **路径**：一律用绝对路径；脚本内的路径常量集中在文件头部。
- **Python**：标准库优先，不引入第三方依赖（目标机是 Debian minimal）。

## 目标机测试

改动涉及网络配置时，建议在真机验证。**测试前先备份**：

```sh
sudo cp -a /etc/dert /root/dert-backup-$(date +%F)
sudo nft list ruleset > /root/nft-backup-$(date +%F).nft
```

涉及 DNS 分流的改动，可用 network namespace 模拟一台"非白名单设备"，不要冒用真实
设备的 MAC（会造成局域网 MAC 翻动）：

```sh
ip netns add t1
ip link add t1h type veth peer name t1n
ip link set t1n netns t1
ip link set t1h address 02:aa:bb:cc:dd:ee    # 本地管理位，确保不与真实设备冲突
ip link set t1h master br-lan && ip link set t1h up
ip netns exec t1 ip link set lo up
ip netns exec t1 ip link set t1n up
ip netns exec t1 ip addr add 192.168.10.240/24 dev t1n
ip netns exec t1 ip route add default via 192.168.10.1
ip netns exec t1 dig +short github.com @192.168.10.1
ip netns del t1 && ip link del t1h                   # 用完清理
```

## 发版

`release.yml` 由 **tag** 触发（`on: push: tags: ["v*"]`），所以推送方式很关键：

```sh
printf '0.2.0\n' > VERSION          # 必须与 tag 完全一致，否则 workflow 第一步就退出
# 在 CHANGELOG.md 的 [Unreleased] 下补一条
git add -A && git commit -m "release: v0.2.0"
git tag -a v0.2.0 -m "v0.2.0：一句话说明"
git push origin main
git push origin v0.2.0              # 显式推 tag
```

**两个坑**（都踩过）：

1. **`git push --follow-tags` 只推附注标签**（`git tag -a`）。用轻量标签
   （`git tag v0.2.0`）时它什么都不会推，于是 tag 只在本地、`release` 工作流
   一次都不会触发——但 `smoke` 照常跑绿，很容易误以为"发版成功了"。
   要么用 `-a`，要么像上面那样显式 `git push origin v0.2.0`。
2. **`VERSION` 与 tag 必须一致**，`release.yml` 会校验；不一致时不会出包，
   只在日志里留一句 tag/VERSION 不一致。

发布产物应包含三个附件：`dert-<ver>.tar.gz`、`.sha256`、`.sig`。缺 `.sig`
说明仓库 Secret `DERT_SIGNING_KEY` 没配好（日志里会有 `::warning::未配置`）。

### 签名密钥

私钥**只存在两处**：你的本机（`~/.dert-signing/dert-signing.key.pem`，600）
与 GitHub Secret `DERT_SIGNING_KEY`（base64 的 PEM，单行）。设备端只有公钥
`priv/dert-signing.pub.pem`。CI 用私钥签、设备用公钥验，私钥永不进镜像。

本机手工签一个包（不走 CI 时）：

```sh
openssl pkeyutl -sign -inkey ~/.dert-signing/dert-signing.key.pem -rawin \
  -in dert-0.2.0.tar.gz -out dert-0.2.0.tar.gz.sig
```

设备端策略由 `upgrade.require_signature` 控制：`true` 时无签名的包直接拒绝，
`false`（默认）时只警告放行——这是"存量发布还没签名"的过渡开关。

## 许可证

提交贡献即表示同意以本项目的许可证（见 [LICENSE](LICENSE)）授权你的贡献。
