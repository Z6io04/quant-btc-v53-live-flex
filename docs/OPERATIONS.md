# 日常查看、停启与追加本金

以下均在服务器终端执行。路径是 `~/btc-v53-live`；服务是用户级 `btc-v53-live.service`。

## 查看

```bash
python3 "$HOME/btc-v53-live/launch.py" status
```

同时显示持仓、盈亏、初始本金、累计追加本金、当前累计本金、运行新鲜度、Mihomo 和经代理访问欧易的公开网络检查。

```bash
python3 "$HOME/btc-v53-live/launch.py" status --local
systemctl --user status btc-v53-live.service --no-pager
journalctl --user -u btc-v53-live.service -n 80 --no-pager
python3 "$HOME/btc-v53-live/launch.py" check
python3 "$HOME/btc-v53-live/launch.py" account-check
```

依次为：只看本地记录；看服务；看最近日志；只读公开接口检查；只读账户检查。公开网络成功不能证明订单、持仓或止损已核对。

## 暂停、恢复、平仓与停止不是同一件事

```bash
python3 "$HOME/btc-v53-live/launch.py" pause
python3 "$HOME/btc-v53-live/launch.py" resume
python3 "$HOME/btc-v53-live/launch.py" flatten
systemctl --user stop btc-v53-live.service
systemctl --user start btc-v53-live.service
```

这是五个独立动作，**不要整段连续执行**。

- pause：停止新增开仓/补仓，继续管理已有仓位。命令进入队列，下次运行循环才处理。
- resume：恢复信号条件检查，不强行开仓；已触发的风控停止不会被此命令清除。
- flatten：提出真实减仓平仓请求；返回不代表成交，必须再看欧易是否空仓。
- stop：只停止 Python 进程，不替你平仓。
- start：启动原账本继续运行，不会清除暂停或风控状态。

## 追加资金：每次照这个顺序

资金是转入 **v53 所属账户的交易账户 USDT 余额**。给主账户充值、但未划转到这个交易账户，不算到账。本命令不替你划转或下单，只核对已到账的钱。

### A. 在转钱前，暂停并确认空仓

```bash
python3 "$HOME/btc-v53-live/launch.py" pause
python3 "$HOME/btc-v53-live/launch.py" status
```

等显示“新开仓/补仓：暂停”。如果还有持仓，先等待正常平仓，或者自行决定是否执行上面的 flatten；不要直接停止持仓管理。确认欧易空仓且普通/策略挂单均为空后：

```bash
systemctl --user stop btc-v53-live.service
systemctl --user is-active btc-v53-live.service
```

应返回 inactive。然后去欧易完成 USDT 转入。不要在停止之前转钱后又人工交易。

### B. 同步新到账的钱

```bash
python3 "$HOME/btc-v53-live/launch.py" sync-capital
```

程序核对同一账户、持仓、订单、成交与资金费、转入账单和实际 USDT 余额，显示本次可增加的本金。确认无误输入 `SYNC V53`。

例如初始 30U，已实现净亏损 2U，后来转入 50U：同步后累计投入 80U，账本净值约 78U，历史亏损仍是 2U。不会把 50U 充值写成盈利，也不会重置原来 30U 的累计净亏损停止阈值。重复执行不会重复加钱。

默认不把其他币种、转出、无法解释的余额变化或人工交易当成可用追加本金；失败时停止在原步骤，保留账本核对。连续超过 5 天无法完成账单核对，也会拒绝直接同步，不能靠改时间或删记录绕过。

### C. 启动并恢复

```bash
systemctl --user start btc-v53-live.service
python3 "$HOME/btc-v53-live/launch.py" resume
python3 "$HOME/btc-v53-live/launch.py" status
```

确认累计本金正确、运行记录继续更新、无风控停止、对账和网络检查正常。

## 重启检查

应先在空仓无挂单时做一次退出 SSH 后重新连接检查，再做服务器正常重启检查。重启后执行 status，确认行情时间持续推进；不要在真钱持仓时用智能插座断电测试故障。服务器、Mihomo 和 Tailscale 可长期运行，不需要例行强制断电。

## 新版首次运行边界

这次只有离线测试，不代表在你的账户完整验证了部分成交、止损触发、重启和故障恢复。真实持仓最终以欧易为准。程序状态过期、委托未决或对账失败时，不删除数据、不重复 arm-live 或 test-buy。
