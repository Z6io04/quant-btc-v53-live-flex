# Mihomo 配置辅助工具

完整操作见 [代理安装、检查与切换节点](../../docs/PROXY.md)。两个量化仓库使用相同工具，可以共用服务器的一个 Mihomo 服务。

| 文件 | 用途 |
| --- | --- |
| `configure.py` | 读取本地 Clash/Mihomo 订阅，选择一个节点，生成候选配置。不是官方 Mihomo 程序，也不会启动代理或量化。 |
| `mihomo-proxy.service` | 用户后台服务模板；读取 `~/mihomo/bin/mihomo` 和 `~/mihomo/private/config.yaml`。 |
| `.gitignore` | 忽略本目录中可能误放的二进制、私有配置和日志。已经提交的秘密不会因此从 Git 历史消失。 |

`configure.py` 与服务模板从已有 `quant-server-kit/tools/mihomo` 原样复制，未修改。Mihomo 二进制应按指南另从官方获取；本仓库不打包二进制、真实订阅、节点凭据或日志。

**把工具复制到仓库外的 `~/mihomo/` 再运行。** 配置生成位置取决于脚本自身所在目录，不是终端当前目录；不要直接在仓库 `tools/mihomo` 下生成配置。

脚本要求订阅含内嵌 `proxies` 列表。只提供 `proxy-providers`、Base64 链接列表或网页的订阅不适用。它固定使用一个节点和 `127.0.0.1:17890`，不复制订阅中的节点组及分流规则，不自动订阅更新或故障换线。若已存在正式 `private/config.yaml`，会拒绝覆盖；切换步骤请按完整指南执行。
