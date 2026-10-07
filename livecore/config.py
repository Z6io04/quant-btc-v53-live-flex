"""Explicit local configuration. No credentials enter strategy state or logs."""
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

ROOT = Path(__file__).resolve().parent.parent

DEFAULT = {
    'mode': 'live', 'instrument': 'BTC-USDT-SWAP',
    'origin': 'https://openapi.okx.com', 'proxy': 'http://127.0.0.1:17890',
    'capital_usdt': 30, 'leverage': 15, 'max_loss_usdt': 30,
    'risk': .06, 'max_notional_multiple': 5, 'reserve': .5,
    'fee': .0005, 'slip': .0003, 'halt_dd': .6, 'cooldown_hours': 6,
    'poll_seconds': 10, 'signal_ttl_seconds': 90, 'quote_age_seconds': 5,
    'warmup_days': 14
}


def load_config(path=None):
    cfg = json.loads(Path(path or ROOT/'config.json').read_text())
    if set(cfg) != set(DEFAULT):
        raise ValueError('配置字段不完整或存在未知字段。')
    if cfg['mode'] != 'live' or cfg['instrument'] != 'BTC-USDT-SWAP':
        raise ValueError('本实盘交付仅支持 live 和 BTC-USDT-SWAP；模拟版须使用原独立目录。')
    # These are the original assigned principal and the absolute loss limit.
    # Later deposits are recorded in the ledger, never by editing this identity.
    for key in ('capital_usdt','max_loss_usdt'):
        if type(cfg[key]) not in (int,float) or not math.isfinite(cfg[key]) or cfg[key] <= 0:
            raise ValueError('初始本金和累计亏损限额必须是有限的正数。')
    for key in ('leverage','risk','max_notional_multiple',
                'reserve','fee','slip','halt_dd','cooldown_hours','warmup_days'):
        if type(cfg[key]) not in (int,float) or cfg[key] != DEFAULT[key]:
            raise ValueError('v53的杠杆、信号和风控比例规则固定；不允许静默更改策略。')
    if type(cfg['poll_seconds']) is not int or not 5 <= cfg['poll_seconds'] <= 30:
        raise ValueError('轮询间隔须为5至30秒。')
    if cfg['signal_ttl_seconds'] != 90 or cfg['quote_age_seconds'] != 5:
        raise ValueError('本版本固定信号和报价过期规则。')
    if cfg['origin'] not in ('https://openapi.okx.com','https://www.okx.com'):
        raise ValueError('接口地址不在全球站允许列表中。区域账户需另行适配，不能自动换域名。')
    if cfg['proxy'] != 'http://127.0.0.1:17890':
        raise ValueError('本交付仅允许已配置的本机Mihomo入口。')
    return cfg


def credentials(path=None):
    path = Path(path or ROOT/'credentials.env')
    if not path.is_file() or path.is_symlink():
        raise ValueError('请在服务器本地创建 credentials.env。不要把密钥发到聊天中。')
    if os.name != 'nt' and (path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid()):
        raise ValueError('credentials.env必须属于当前用户，权限为600。')
    values = {}
    allowed = {'OKX_API_KEY','OKX_SECRET_KEY','OKX_PASSPHRASE'}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, val = line.partition('=')
        if not sep or key not in allowed or key in values or not val:
            raise ValueError('credentials.env格式错误；每行名称=值，不加引号。')
        values[key] = val
    if set(values) != allowed or any(v.startswith('FILL_') for v in values.values()):
        raise ValueError('密钥配置未完成。')
    return {'api_key':values['OKX_API_KEY'], 'secret_key':values['OKX_SECRET_KEY'],
            'passphrase':values['OKX_PASSPHRASE']}


def data_dir(cfg):
    return ROOT/'var'/cfg['mode']


def code_identity(cfg):
    names = [ROOT/'launch.py'] + sorted((ROOT/'livecore').glob('*.py'))
    manifest = json.loads((ROOT/'frozen-source-manifest.json').read_text())
    for name, expected in manifest.items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != expected:
            raise ValueError('冻结的v53信号文件发生变化。')
        names.append(ROOT/name)
    return {'config':cfg,'files':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in names}}


def safe_error(exc):
    if isinstance(exc, (ValueError, RuntimeError)) and str(exc):
        # Our public messages never include raw API sMsg, response bodies, or headers.
        from .okx import OKXError
        if isinstance(exc, OKXError):
            return str(exc)
        if getattr(exc, 'safe_message', False):
            return str(exc)
    return type(exc).__name__ + '；请查看脱敏状态并核对服务、代理及账户设置。'


class Attention(RuntimeError):
    safe_message = True


class AuditPending(Attention):
    """Fill evidence is incomplete: abort this tick, then retry without unpausing."""


def account_key(mode, uid):
    if mode not in ('demo', 'live') or not isinstance(uid, str) or not uid:
        raise Attention('账户标识无效，不能取得账户锁。')
    return hashlib.sha256((mode+':'+uid).encode()).hexdigest()


def account_lock_path(mode, uid):
    return Path.home()/'.local/state/btc-v53-live/locks'/(account_key(mode,uid)+'.lock')


def account_binding_path(mode, uid):
    return Path.home()/'.local/state/btc-v53-live/accounts'/(account_key(mode,uid)+'.json')


def account_binding(mode, uid):
    path = account_binding_path(mode,uid)
    if path.is_symlink():
        raise Attention('账户绑定文件不能是链接；请保留文件并检查。')
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        if (value['schema'] != 1 or value['account'] != account_key(mode,uid)
                or value['mode'] != mode or value['phase'] not in ('reserved','active')
                or not isinstance(value['binding_id'],str) or not value['binding_id']
                or not Path(value['directory']).is_absolute()):
            raise ValueError()
        return value
    except (ValueError,KeyError,TypeError):
        raise Attention('账户绑定记录损坏；不能以新账本覆盖，请保留文件并检查。') from None


def account_is_bound(mode, uid):
    return account_binding(mode,uid) is not None


def assert_account_unbound(mode, uid):
    if account_is_bound(mode,uid):
        raise Attention('该账户已绑定v53交易账本，不能另开小额测试；请使用原账本核对持仓。')


def claim_account_binding(mode, uid, directory):
    """Reserve once under account_lock_path. No operation can reset an active binding."""
    previous = account_binding(mode,uid)
    directory = str(Path(directory).resolve())
    if previous is not None:
        if previous['directory'] != directory:
            raise Attention('该账户已绑定另一个目录；禁止换目录重新初始化预算或绕过原账本。')
        return previous
    value = dict(schema=1,account=account_key(mode,uid),mode=mode,directory=directory,
                 phase='reserved',binding_id=uuid.uuid4().hex)
    path = account_binding_path(mode,uid)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
        json.dump(value,stream,sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)
    return value


def _sync_directory(path):
    descriptor = os.open(path,os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def activate_account_binding(mode, uid, directory, binding_id):
    value = validate_account_binding(mode,uid,directory,binding_id,require_active=False)
    if value['phase'] == 'active':
        return
    value['phase'] = 'active'
    path = account_binding_path(mode,uid)
    temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    try:
        with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            json.dump(value,stream,sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
        _sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_account_binding(mode, uid, directory, binding_id, require_active=True):
    value = account_binding(mode,uid)
    if (value is None or value['directory'] != str(Path(directory).resolve())
            or value['binding_id'] != binding_id
            or (require_active and value['phase'] != 'active')):
        raise Attention('账户与本地账本绑定不一致或启用未完成；禁止自动重建。')
    return value
