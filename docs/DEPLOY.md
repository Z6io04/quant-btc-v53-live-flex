# 安装新版（先清理，再导入）

本教程对应现有 Debian 用户 `dvens`、Tailscale 地址 `100.77.57.40`、已运行的 Mihomo。默认初始本金 30U，累计亏损停止阈值 30U；累计本金可通过确认充值持续增加。

## 1. 清理旧版

先完成 [CLEAN-OLD.md](CLEAN-OLD.md)。不要先覆盖旧文件夹；清理工具需要读取旧代码、旧账本和旧 API 来确认没有策略成交和未完成订单。清理不会替你平仓，也不会撤销欧易网站上的 API。

## 2. 传新版压缩包

将交付的 `btc-v53-live-flex.zip` 放在 Mac 下载文件夹。在 Mac 本地终端执行：

```bash
scp "$HOME/Downloads/btc-v53-live-flex.zip" dvens@100.77.57.40:/home/dvens/
ssh dvens@100.77.57.40
```

如果压缩包放在其他地方，将前一条命令的文件路径换成实际位置。

在服务器终端确认旧目录已经不存在，再解压：

```bash
ls -ld "$HOME/btc-v53-live"
```

这时应显示“没有那个文件或目录”。若还存在，不要覆盖，先完成清理。

```bash
unzip "$HOME/btc-v53-live-flex.zip" -d "$HOME"
cd "$HOME/btc-v53-live"
```

`unzip` 只解压，不会运行策略。Python 3.11 以上即可，无第三方 Python 依赖。此前已安装的系统组件可以继续用。

## 3. 检查代理

```bash
systemctl --user is-active mihomo-proxy.service
python3 "$HOME/btc-v53-live/launch.py" check
```

第一条应返回 `active`；第二条应有 `passed: true`。代理地址已经写在 `config.json` 中，不需要 `.env` 或另建 `network.json`。详见 [PROXY.md](PROXY.md)。

## 4. 配置真实 API 和账户

在欧易登录 v53 专用账户；F3 使用另一个子账户。v53 合约是 **BTC-USDT-SWAP，USDT 本位**，不是 F3 的 BTC-USD-SWAP 币本位。账户设为合约模式、买卖/净持仓、该合约逐仓 15 倍。

API 仅开启读取与交易权限。已有的同账户 API 仍正常可继续使用；删除本机目录不会在欧易撤销 API。输入仅留在服务器，不发送到聊天或 GitHub。白名单应使用实际代理出口 IP，不能盲填固定示例地址，参见代理说明。

```bash
python3 "$HOME/btc-v53-live/launch.py" setup-credentials
python3 "$HOME/btc-v53-live/launch.py" account-check
```

第二条是只读账户检查，不下单。必须确认账户空仓、没有普通或策略挂单，交易账户可用 USDT 足够覆盖初始 30U。

## 5. 启用账本，再启动

```bash
python3 "$HOME/btc-v53-live/launch.py" arm-live
```

阅读提示后，在终端输入 `LIVE 30U`。这一步建立真实账户账本和初始预算，但尚未运行交易循环。30U 是初始分配，不是以后不能超过的上限。如果账户已有额外 USDT，初次启用默认也只分配 30U；额外余额不会冒充新充值自动纳入。需要不同初始本金时，应在第一次启用前明确设置 `config.json` 的 `capital_usdt`，确认词随金额变化；不要通过改配置给已启用账本加钱。

安装并启动用户服务：

```bash
bash "$HOME/btc-v53-live/deploy/install-service.sh"
systemctl --user enable --now btc-v53-live.service
python3 "$HOME/btc-v53-live/launch.py" status
```

**启动服务后会按 v53 信号提交真钱订单。** 初期可能准备行情，之后应有近期更新、对账结果和网络结果。服务 active 只说明进程运行，仍需看状态输出与欧易账户。

你此前已经设置过用户退出后继续运行；可检查：

```bash
loginctl show-user dvens -p Linger
```

返回 `Linger=yes` 即可。若确实是 no，才需要一次 `sudo loginctl enable-linger dvens`。

## 6. 此后追加资金

不要重新执行 `arm-live`，不要修改 config.json 的初始本金，不要删除账本。按 [OPERATIONS.md](OPERATIONS.md) 的顺序：暂停新增、等空仓、停服务、转 USDT、`sync-capital`、重新启动并恢复。
