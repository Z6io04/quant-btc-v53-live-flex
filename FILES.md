# 文件用途

- `launch.py`：统一操作入口。
- `config.json`：原始初始本金、亏损停止阈值、固定策略参数和代理。
- `livecore/capital.py`：识别到账转入、收集账单、手动同步本金。
- `livecore/execution.py`：订单与止损、实际成交记账、资金费和风险检查。
- `livecore/runtime.py`：持续循环、账户校验、控制指令、行情推进。
- `livecore/okx.py`：签名网络请求和受限下单接口。
- `livecore/market.py`：行情与账户只读检查。
- `livecore/store.py`：SQLite 持久化，委托记录、成交证据、状态和资金账单。
- `livecore/status.py` / `status_view.py`：查看报告。
- `livecore/smoke.py`：一次性小单测试；已启用策略账户不能借此再开小单。
- 六个策略源文件及 `frozen-source-manifest.json`：保留原 v53 信号不变。
- `deploy/`：用户后台服务。
- `tests/`：离线测试，不用真实密钥。
- `docs/`：清理、部署、日常管理与代理说明。
- `tools/`：发布检查、旧版清理及独立代理配置示例，不含代理二进制和节点信息。

`credentials.env`、`var/` 在你运行配置/启用命令后创建，不在交付源码中。源码仓库与账户运行目录不要混为同一份上传。
