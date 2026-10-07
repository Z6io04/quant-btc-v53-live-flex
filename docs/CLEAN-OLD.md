# 先清理旧 v53，再导入新版

适用于你已确认的情况：只有启用策略前的 1U 小单测试，目前没有持仓和挂单，v53 策略未发生真实成交。清理后旧凭据文件也会被删除，请确保 API Key、Secret、Passphrase 在你自己的安全位置可以找回，或者准备同一账户的新 API。

本机删除 API 配置不会在欧易撤销 API。如果另一个程序也在使用同一组密钥，不要在欧易把它误删。

## 1. 先停旧版（服务器终端）

```bash
systemctl --user disable --now btc-v53-live.service
systemctl --user is-active btc-v53-live.service
```

看到 inactive，表示进程停止。它不代表替你平仓，仍须确认欧易空仓和无普通/算法挂单。

## 2. 只传清理工具，不传新版策略

将单独交付的 `retire_v53_unused.py` 保存到 Mac 下载文件夹。在 **Mac 本地终端**：

```bash
scp "$HOME/Downloads/retire_v53_unused.py" dvens@100.77.57.40:/home/dvens/
```

如果文件在别处，把本地路径替换成实际位置。然后在 **服务器终端** 执行：

```bash
cd "$HOME"
python3 "$HOME/retire_v53_unused.py"
```

工具会使用旧 API 做只读核对，不下单、不平仓。它检查停止状态、旧源代码与账本是否匹配、该账户确实空仓无挂单，以及启用策略后没有成交或资金费。检查通过后，你输入 `DELETE V53` 才执行删除。

删除范围只有：

- `/home/dvens/btc-v53-live/`：旧代码、旧配置、旧密钥文件、尚未交易的账本。
- 原 `btc-v53-live.service` 用户服务文件。
- 该 v53 账户对应的旧绑定记录。

Mihomo、F3、Tailscale、其他账户绑定不在删除范围。启用前独立的小单测试收据与账户锁保留，防止以后把同一次测试重复发送；它不是旧策略账本。

## 3. 确认清理完成

```bash
ls -ld "$HOME/btc-v53-live"
```

应提示该目录不存在。然后再执行 [DEPLOY.md](DEPLOY.md) 的新版导入步骤。

如果工具提示存在真实策略记录、未知版本、无法核对 API 或网络问题，当前情况就与“未交易旧版”不一致，先解决提示；不要另外使用大范围删除命令绕过。若删除中途因磁盘等问题中断，按工具提示进行中断恢复，先不要解压新版到同名目录。
