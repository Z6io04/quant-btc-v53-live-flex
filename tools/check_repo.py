#!/usr/bin/env python3
"""Check common private-file mistakes before committing this source repository.

This is a small packaging guard, not a general secret scanner. --staged checks
the Git index contents, including staged files later changed in the worktree.
"""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {'private', 'var', 'data', 'public-check', 'backups', 'backup',
                'mihomo-migration-backups', 'node_modules', '.venv', 'venv', 'bin'}
IGNORED_DIRS = {'.git', '__pycache__', '.pytest_cache'}
PRIVATE_SUFFIXES = {'.zip', '.gz', '.tgz', '.sqlite', '.sqlite3', '.db', '.log',
                    '.pem', '.key', '.p12', '.pfx', '.bak', '.pyc'}
ENV_KEYS = {'OKX_API_KEY', 'OKX_SECRET_KEY', 'OKX_PASSPHRASE'}
PLACEHOLDERS = {'OKX_API_KEY': 'FILL_API_KEY',
                'OKX_SECRET_KEY': 'FILL_SECRET_KEY',
                'OKX_PASSPHRASE': 'FILL_PASSPHRASE'}


def problem(name, content):
    path = PurePosixPath(name)
    lower_parts = [part.lower() for part in path.parts]
    base = path.name.lower()
    if any(part in PRIVATE_DIRS for part in lower_parts[:-1]):
        return '运行数据、程序二进制或私有目录不能放入源码仓库'
    if ('subscription' in base or base in {'credentials.env', '.env', 'network.json'}
            or base.startswith('.env.') or base.startswith('config.backup')):
        return '疑似本机配置、凭据或订阅文件'
    if path.suffix.lower() in PRIVATE_SUFFIXES:
        return '压缩包、账本、日志或密钥文件不能放入源码仓库'
    if path.suffix.lower() in {'.yaml', '.yml'} and not name.startswith('.github/workflows/'):
        return '请勿把代理节点 YAML 放入仓库；工作流 YAML 是例外'
    try:
        value = content.decode('utf-8')
    except UnicodeDecodeError:
        return '此源码包不应包含二进制文件'
    if name == 'credentials.env.example':
        rows = [line.strip() for line in value.splitlines()
                if line.strip() and not line.lstrip().startswith('#')]
        expected = [key + '=' + val for key, val in PLACEHOLDERS.items()]
        if sorted(rows) != sorted(expected):
            return '凭据示例只能保留 FILL_ 占位文字'
    if re.search(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----', value):
        return '发现私钥标记'
    # Documentation and tests contain variable names and deliberately fake keys.
    # Check real-looking assignment lines in non-example configuration files.
    if path.suffix.lower() in {'.json', '.env', '.toml', '.ini', '.conf'}:
        for key in ENV_KEYS:
            if re.search(r'(?im)(?:^|[{,])\s*[\"\']?' + key + r'[\"\']?\s*[:=]', value):
                return '配置含有 API 凭据字段；请在服务器本地设置'
    return None


def entries(staged):
    if staged:
        paths = subprocess.check_output(['git', 'diff', '--cached', '--name-only',
                                         '--diff-filter=ACMR', '-z'], cwd=ROOT)
        for raw in paths.split(b'\0'):
            if not raw:
                continue
            name = raw.decode('utf-8')
            mode = subprocess.check_output(['git', 'ls-files', '-s', '--', name], cwd=ROOT)
            if mode.startswith(b'120000'):
                raise ValueError('暂存区包含符号链接：' + name)
            yield name, subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
    else:
        for path in sorted(ROOT.rglob('*')):
            rel = path.relative_to(ROOT)
            if any(part in IGNORED_DIRS for part in rel.parts) or path.name == '.DS_Store':
                continue
            if path.is_symlink():
                raise ValueError('源码包包含符号链接：' + rel.as_posix())
            if path.is_file():
                yield rel.as_posix(), path.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true', help='检查将被提交的 Git 暂存区内容')
    args = parser.parse_args()
    errors = []
    count = 0
    try:
        for name, content in entries(args.staged):
            count += 1
            reason = problem(name, content)
            if reason:
                errors.append(name + '：' + reason)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('无法完成检查：' + str(exc), file=sys.stderr)
        return 2
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        print('检查未通过。不要上传；本工具不会删除任何文件。', file=sys.stderr)
        return 1
    print('已检查 %d 个文件，未发现上述常见私有文件问题。' % count)
    print('仍需自行核对 git diff --cached；此工具不能识别所有形式的密钥。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
