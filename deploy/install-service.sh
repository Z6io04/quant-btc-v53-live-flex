#!/usr/bin/env bash
# Install the user unit only. No keys, arming, network requests or trading here.
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo '请以服务器普通用户运行，不要使用 sudo bash。' >&2
  exit 1
fi
if [[ "$(uname -s)" != Linux ]]; then
  echo '这个服务安装脚本仅在 Debian/Linux 服务器上运行。' >&2
  exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_dir="$(cd -- "$script_dir/.." && pwd -P)"
expected_dir="$HOME/btc-v53-live"
if [[ ! -d "$expected_dir" || -L "$expected_dir" ]]; then
  echo '请先将完整程序文件夹放到 ~/btc-v53-live；不要使用符号链接。' >&2
  exit 1
fi
expected_real="$(cd -- "$expected_dir" && pwd -P)"
if [[ "$project_dir" != "$expected_real" ]]; then
  echo '此安装脚本只能安装 ~/btc-v53-live 中的版本。' >&2
  exit 1
fi
for item in launch.py status_view.py config.json livecore/store.py deploy/btc-v53-live.service; do
  if [[ ! -f "$project_dir/$item" ]]; then
    echo "程序包缺少文件：$item；尚未安装服务。" >&2
    exit 1
  fi
done
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11 或以上版本"'
command -v systemctl >/dev/null
systemctl --user show-environment >/dev/null

unit_name=btc-v53-live.service
active_state="$(systemctl --user show "$unit_name" --property=ActiveState --value 2>/dev/null || true)"
case "$active_state" in
  active|activating|deactivating|reloading)
    echo '服务正在运行或切换状态。先核对账户与订单，再停止服务；本脚本不会覆盖运行中的配置。' >&2
    exit 1
    ;;
esac

unit_dir="$HOME/.config/systemd/user"
mkdir -p -- "$unit_dir"
if [[ -L "$unit_dir/$unit_name" ]]; then
  echo '目标服务文件是符号链接，请先人工核对；本脚本不会覆盖它。' >&2
  exit 1
fi
if [[ -e "$unit_dir/$unit_name" ]]; then
  if ! cmp -s -- "$script_dir/$unit_name" "$unit_dir/$unit_name"; then
    echo '已存在不同的 btc-v53-live.service。保留原文件，请先核对差异后再安装。' >&2
    exit 1
  fi
else
  install -m 0644 -- "$script_dir/$unit_name" "$unit_dir/$unit_name"
fi
systemctl --user daemon-reload
systemctl --user enable "$unit_name"

echo '用户服务已安装并设置为开机自动启动；本次没有启动程序，也没有允许下单。'
echo '先完成 check 和 account-check，再由你执行 arm-live，最后执行：'
echo 'systemctl --user start btc-v53-live.service'
echo '退出 SSH 后及开机自动运行还需要 Linger=yes；若未设置，请执行：'
echo 'sudo loginctl enable-linger "$(id -un)"'
