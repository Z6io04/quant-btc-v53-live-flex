# 把本项目保存到 GitHub

这份 ZIP 是一个完整的源码仓库。GitHub 用来保存和管理代码；上传成功不会自动启动服务器，也不会自动下单。这份实盘项目单独建仓库，不和 F3 混放。

## 1. 先在 Mac 解压

双击 ZIP，得到 `btc-v53-live` 文件夹。里面有 `launch.py`、`livecore`、`README.md`、`docs` 等内容。

以下例子假设你把这个文件夹放到了 Mac 桌面。在 **Mac 本地终端** 执行，不是在已登录 Debian 的 SSH 窗口：

```bash
cd "$HOME/Desktop/btc-v53-live"
python3 tools/check_repo.py
```

此检查会寻找凭据文件、代理订阅、运行账本等常见误上传内容，不会启动策略，不会连接欧易，也不会删除文件。它不能识别所有形式的密钥；提交前仍需查看文件内容。请用这里交付的干净源码建仓库，不要把服务器正在运行的整个文件夹直接上传。

## 2. 在 GitHub 新建空仓库

登录 GitHub，选择 **New repository**，名称填写 `btc-v53-live`，先选择 **Private**（私有）。不要勾选自动创建 README、`.gitignore` 或许可证，这份包已经带有前两项；许可证是否添加由你决定。

私有仓库也不应该存放 API 密钥、机场订阅或交易账本。

## 3. 用 Git 上传整个目录

仍在上述 Mac 文件夹内执行：

```bash
git init -b main
git add .
python3 tools/check_repo.py --staged
git status --short
git diff --cached --stat
```

- `git init`：让当前文件夹成为 Git 仓库，主分支叫 `main`。
- `git add .`：把当前目录里未被忽略的文件放入“准备提交”区。此时还没有上传。
- `--staged`：检查准备提交的实际文件内容，避免只检查到修改后的本地副本。
- `git status` 和 `git diff --cached --stat`：查看准备提交的文件名单与改动数量。需要逐行查看时用 `git diff --cached`，按 `q` 退出查看。

确认检查通过，名单里没有真实密钥、订阅、`var` 账本、日志后，再执行：

```bash
git commit -m "Import v53 source and setup guides"
git remote add origin https://github.com/YOUR_GITHUB_NAME/btc-v53-live.git
git push -u origin main
```

把 `YOUR_GITHUB_NAME` 换成你的 GitHub 用户名；仓库名称必须与你创建的一致。如果 Git 首次提示配置提交者姓名/邮箱，按提示设置后重新提交。如果推送要求登录，请按 GitHub 的认证流程完成；HTTPS 推送不能直接使用 GitHub 账户密码。[GitHub 官方导入说明](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)

`git commit` 是在 Mac 上保存一个版本，`git push` 才把版本上传到 GitHub。

## 4. 如果选择网页上传

使用仓库中的 **Add file → Upload files**，上传解压后的文件内容。直接上传 ZIP 只会保存一个压缩包，GitHub 不会自动把 ZIP 展开成源码目录。

注意 `.gitignore`、`.gitattributes`、`.github` 是隐藏文件，Mac Finder 可以按 `Command + Shift + .` 显示。不能只上传你能看到的普通文件而遗漏这些文件。使用上面的 Git 命令更容易保持目录完整。

## 5. 以后怎么修改

在 Mac 的源码副本里修改后，先测试，再提交：

```bash
python3 -B -m unittest discover -s tests -q
python3 tools/check_repo.py
git add .
python3 tools/check_repo.py --staged
git diff --cached
git commit -m "Describe your change"
git push
```

仅当检查通过且改动正确时继续。`.gitignore` 只能帮助忽略尚未被 Git 跟踪的文件，不能自动移除已经上传过的密钥；如果密钥误上传，应立即在欧易撤销并重新创建。

**不要在正在运行真钱交易的服务器目录直接执行 `git pull` 或用新 ZIP 覆盖。** 程序的授权记录会绑定代码、配置和账户，直接覆盖会造成版本与账本不一致。升级前应按[运行管理说明](OPERATIONS.md)核对持仓、委托、账本与停止状态。

## 6. 自动测试与本机信息

`.github/workflows/offline-tests.yml` 会在 GitHub 的 Actions 中运行离线测试和文件检查。工作流不部署服务器、不启用任何交易服务，不需要添加 API Secrets。它使用 Python 3.11/3.13；交付时的实际本地测试结果见 [TEST-REPORT.md](../TEST-REPORT.md)。GitHub 上的结果要在你上传后才能看到。

这些内容只应该保留在服务器本地：

| 内容 | 作用 |
|---|---|
| `credentials.env` | API Key、Secret、Passphrase |
| `var/` | 交易账本、运行状态等 |
| `~/mihomo/private/` | 代理节点配置和日志 |
| 订阅 YAML / 订阅链接 | 机场账号的代理连接信息 |
| `~/.local/state/btc-v53-live/` | 账户锁、确认记录等，不能用源码覆盖 |

GitHub 上的源码不是服务器账本备份。目录和各文件作用见 [FILES.md](../FILES.md)；重新部署请从 [DEPLOY.md](DEPLOY.md) 开始。
