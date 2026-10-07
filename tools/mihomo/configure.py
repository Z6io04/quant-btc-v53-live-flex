"""Build a local-only fixed-node proxy candidate; never execute subscription YAML."""
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parent
LIMIT = 4 * 1024 * 1024
TYPES = {'ss', 'ssr', 'vmess', 'vless', 'trojan', 'hysteria', 'hysteria2', 'tuic', 'snell', 'http', 'socks5'}
DEPENDENCIES = {'dialer-proxy', 'interface-name', 'routing-mark', 'certificate', 'private-key', 'ca'}


def primitive(value, depth=0):
    if depth > 20:
        raise ValueError('配置嵌套过深或存在循环引用。')
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        if len(value) > 10000:
            raise ValueError('配置列表过大。')
        for item in value:
            primitive(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 10000 or not all(isinstance(k, str) for k in value):
            raise ValueError('配置字段不是字符串或数量过多。')
        for item in value.values():
            primitive(item, depth + 1)
        return
    raise ValueError('配置含不支持的数据类型。')


def dependency(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in DEPENDENCIES and item not in (None, '', False, 0):
                return True
            if dependency(item):
                return True
    elif isinstance(value, list):
        return any(dependency(item) for item in value)
    return False


def load_nodes(content):
    if len(content) > LIMIT:
        raise ValueError('配置超过 4 MB。')
    try:
        source = yaml.safe_load(content)
    except (yaml.YAMLError, UnicodeError, ValueError):
        # YAML errors may include a source line with credentials: never print them.
        raise ValueError('无法读取 Clash YAML 配置；请确认复制的是 Clash/Mihomo 订阅。') from None
    if not isinstance(source, dict) or not isinstance(source.get('proxies'), list) or not source['proxies']:
        raise ValueError('订阅未提供内嵌 proxies 节点列表。仅有 proxy-providers、Base64 节点链接或加密客户端配置暂不支持；请使用原生 Clash 配置导出。')
    if len(source['proxies']) > 2000:
        raise ValueError('节点列表超过支持范围。')
    nodes = []
    for node in source['proxies']:
        if not isinstance(node, dict):
            continue
        primitive(node)
        port = node.get('port')
        if isinstance(port, str) and port.isascii() and port.isdigit():
            port = int(port)
        protocol = node.get('type')
        if (not isinstance(protocol, str) or protocol not in TYPES or not isinstance(node.get('server'), str)
                or not node['server'].strip() or not isinstance(port, int) or isinstance(port, bool)
                or not 1 <= port <= 65535 or dependency(node)):
            continue
        node = copy.deepcopy(node)
        node['port'] = port
        nodes.append(node)
    if not nodes:
        raise ValueError('没有找到可独立使用的受支持节点。需要链式代理、指定网卡或外部证书文件的节点不会被直接迁移。')
    return nodes


def build_config(node):
    selected = copy.deepcopy(node)
    primitive(selected)
    if dependency(selected):
        raise ValueError('节点依赖其他配置，不能独立迁移。')
    selected['name'] = 'V53NODE'
    return {
        'mixed-port': 17890,
        'allow-lan': False,
        'bind-address': '127.0.0.1',
        'mode': 'rule',
        'log-level': 'warning',
        'external-controller': '',
        'external-controller-unix': '',
        'dns': {'enable': False},
        'tun': {'enable': False},
        'proxies': [selected],
        'rules': ['MATCH,V53NODE'],
    }


def safe_label(node, index):
    name = node.get('name')
    if not isinstance(name, str):
        name = '节点 ' + str(index)
    name = re.sub(r'[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]', ' ', name)
    return name[:100] + ' [' + str(node.get('type', '')) + ']'


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print('用法：python3 configure.py 订阅文件路径')
        return 2
    private = ROOT/'private'
    if private.is_symlink():
        print('private 是符号链接，已停止。')
        return 2
    if (private/'config.yaml').exists():
        print('已有正式代理配置，保留原文件；本步骤不覆盖。')
        return 2
    try:
        with Path(args[0]).expanduser().open('rb') as stream:
            nodes = load_nodes(stream.read(LIMIT + 1))
        print('选择你在 Mac 上用过且能访问欧易的节点；本次固定使用这一条线路。')
        for index, node in enumerate(nodes, 1):
            print(str(index) + '. ' + safe_label(node, index))
        try:
            choice = int(input('输入节点序号，然后回车：'))
        except ValueError:
            raise ValueError('请输入列表中的数字序号。') from None
        if choice < 1 or choice > len(nodes):
            raise ValueError('节点序号不在列表内。')
        result = build_config(nodes[choice-1])
        # JSON is valid YAML, avoids custom tags and retains nested transport options.
        encoded = (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
        private.mkdir(mode=0o700, exist_ok=True)
        private.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix='.candidate-', dir=str(private))
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, private/'config.candidate.yaml')
        finally:
            if os.path.exists(name):
                os.unlink(name)
    except (KeyboardInterrupt, EOFError):
        print('\n已取消，未替换正式配置。')
        return 2
    except (OSError, ValueError, RecursionError) as exc:
        print('配置准备失败：' + (str(exc) if type(exc) is ValueError else '读取或保存失败，请核对文件及权限。'))
        return 2
    print('候选配置已生成；需由安装脚本验证后启用。节点密码不会显示。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
