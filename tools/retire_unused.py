#!/usr/bin/env python3
"""Retire ONLY the never-traded original v53 deployment; no orders are sent.

Intended before a clean flex install. Real strategy activity refuses deletion.
The separately stored pre-arm test-buy receipt is preserved. No API credentials
are printed or copied. Production CLI is Linux, non-root, fixed home path only.
"""
import argparse

from contextlib import ExitStack, contextmanager

import getpass

import hashlib

import importlib

import json

import os

from pathlib import Path

import re

import sqlite3

import stat

import subprocess

import sys

import time

from types import SimpleNamespace

import uuid

import warnings

UNIT = 'btc-v53-live.service'

SCHEMA = {
    'meta': ('key', 'payload'),
    'events': ('id', 'kind', 'payload', 'created_at'),
    'intents': ('client_id', 'action_key', 'kind', 'status', 'request', 'context', 'result', 'created_at', 'updated_at'),
    'bars': ('timestamp', 'payload'),
    'records': ('id', 'namespace', 'record_key', 'payload', 'created_at'),
    'sqlite_sequence': ('name', 'seq'),
}

class RetirementError(RuntimeError):
    """Only fixed, non-secret messages are displayed by the CLI."""

def require(condition, message):
    if not condition:
        raise RetirementError(message)

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def sha(value):
    return hashlib.sha256(value).hexdigest()

def safe_path(path, *, exists=True, private=False, directory=False):
    """Reject symlinks in every existing path component, including ancestors."""
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode), '路径含符号链接；停止清理。')
    if exists:
        info = path.stat()
        require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), '文件类型不正确；停止清理。')
        if not directory:
            require(info.st_nlink == 1, '文件存在硬链接；停止清理。')
        if private:
            require(info.st_uid == os.getuid() and not info.st_mode & 0o077,
                    '私有文件或目录必须属于当前用户，且其他用户不可访问。')
    return path

def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def check_service(project, runner=subprocess.run):
    fields = ('Id', 'Names', 'LoadState', 'ActiveState', 'UnitFileState', 'MainPID', 'FragmentPath', 'DropInPaths', 'TriggeredBy')
    result = runner(['systemctl', '--user', 'show', '--all', '--no-pager',
                     '--property=' + ','.join(fields), '--', UNIT],
                    capture_output=True, text=True, timeout=10)
    values = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition('=')
        require(sep and key in fields and key not in values, '无法确认用户服务状态。')
        values[key] = value
    require(result.returncode == 0 and set(values) == set(fields), '无法确认用户服务状态。')
    require(values['Id'] == UNIT and values['Names'].split() == [UNIT], '服务名称或别名不明确。')
    require(values['LoadState'] in ('loaded', 'masked') and values['ActiveState'] == 'inactive'
            and values['MainPID'] == '0' and values['UnitFileState'] in ('disabled', 'masked'),
            '实战用户服务必须已停止，且已禁用或屏蔽；本工具不会操作服务。')
    require(not values['DropInPaths'] and not values['TriggeredBy'], '服务存在附加配置或触发器；停止清理。')
    fragment = values['FragmentPath']
    if values['LoadState'] == 'masked':
        require(fragment in ('', '/dev/null') and values['UnitFileState'] == 'masked', '屏蔽服务状态不明确。')
    else:
        require(bool(fragment), '无法确认服务文件位置。')
        source = safe_path(fragment)
        expected = safe_path(Path(project) / 'deploy' / UNIT)
        require(source.read_bytes() == expected.read_bytes(), '用户服务内容与原实战模板不同。')
        # This release has a fixed %h/btc-v53-live unit; custom --project may
        # inspect/rotate an archive, but never imply it is the service target.
    return values

@contextmanager
def open_database(path, writable=False):
    path = safe_path(path, private=True)
    for suffix in ('-wal', '-shm', '-journal'):
        companion = Path(str(path) + suffix)
        if companion.exists() or companion.is_symlink():
            safe_path(companion, private=True)
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True,
                         timeout=0, isolation_level=None)
    try:
        if writable:
            db.execute('PRAGMA synchronous=FULL')
        else:
            db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()

def validate_schema(db):
    require(db.execute('PRAGMA user_version').fetchone()[0] == 1, '账本版本不受支持。')
    require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], '账本完整性检查未通过。')
    objects = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    require({name for kind, name, _ in objects if kind == 'table'} == set(SCHEMA), '账本表结构不受支持。')
    require(all(kind == 'table' or (kind == 'index' and name.startswith('sqlite_autoindex_') and sql is None)
                for kind, name, sql in objects), '账本存在未知索引、触发器或视图。')
    for table, columns in SCHEMA.items():
        require(tuple(row[1] for row in db.execute('PRAGMA table_info(' + table + ')')) == columns,
                '账本字段不受支持。')

def database_digest(db):
    """Logical exactness includes every original row, payload byte and schema."""
    digest = hashlib.sha256()
    digest.update(encoded(db.execute('PRAGMA user_version').fetchone()).encode())
    digest.update(encoded(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()).encode())
    for table in sorted(SCHEMA):
        digest.update(table.encode())
        for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid'):
            values = [{'blob': value.hex()} if isinstance(value, bytes) else value for value in row]
            digest.update(encoded(values).encode() + b'\n')
    return digest.hexdigest()

def verify_sources(project, identity):
    files = identity.get('files')
    require(isinstance(files, dict) and files and 'launch.py' in files and 'livecore/config.py' in files,
            '源代码身份清单缺失。')
    actual_core = {'launch.py'} | {str(path.relative_to(project)) for path in (project / 'livecore').glob('*.py')}
    manifest_path = safe_path(project / 'frozen-source-manifest.json')
    manifest = json.loads(manifest_path.read_text())
    require(isinstance(manifest, dict) and set(files) == actual_core | set(manifest), '源代码清单与原账本不同。')
    for name, digest in files.items():
        require(isinstance(name, str) and not Path(name).is_absolute() and '..' not in Path(name).parts,
                '代码清单路径不合法。')
        path = safe_path(project / name)
        require(sha(path.read_bytes()) == digest, '原实战源代码发生变化；不执行清理。')
    require(all(files[name] == value for name, value in manifest.items()), '冻结代码清单与原账本不同。')

def load_modules(project):
    require(not any(name == 'livecore' or name.startswith('livecore.') for name in sys.modules),
            '进程已加载另一份实战程序；请使用独立命令运行。')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(project))
    names = ('config', 'runtime', 'market', 'okx', 'store')
    modules = {name: importlib.import_module('livecore.' + name) for name in names}
    for module in modules.values():
        require(Path(module.__file__).resolve().is_relative_to(project), '加载的实战模块位置不匹配。')
    return SimpleNamespace(**modules)

PINNED_CODE = {'capture.py': '3a901f970f4e251c883dc78286d36cdfb1d68e8baf551718481eb6f98bbebec7', 'confirmation.py': 'e59564b5099aa87d5792fd2b94dbe098aaba062dfad6fcc401b82388cd3d8888', 'launch.py': '416a3c84382a9f18264c517d341eeb1e54513038a854ce7da2f81c405d9451b3', 'livecore/__init__.py': 'f56ef0185f5d4b5bc72c4684573b03180586ab38638594626d927639c09fe813', 'livecore/config.py': 'a5f9067f567e5c0ab82e166ecbc70a17dc63717786abb6c8851eb06f3f002fcc', 'livecore/decisions.py': '9e97c3a427357c678bdbdcaca73edb2de0c71a9a748598fe5d290f151cf1bd2e', 'livecore/execution.py': '75485f5ef88c32f208848b40fbd498900c17d51a60ec27822842219ea9d538ad', 'livecore/market.py': '193329f5927b44b6df362e8f33bc2732bea46fa7d305ca1a12cbd7222cbc5d60', 'livecore/okx.py': 'aa22c879d1938326579843bfc909742d06383dd1fe0c22b5d043b8538f962fbb', 'livecore/runtime.py': 'daaa5853bc63507f8a463b0691cd989e8df626eded84b9ca7767cde3bcad5c1f', 'livecore/smoke.py': 'e84e860779756adcfccb839948558f178b331e3c85ab34e42e3807638c60bb84', 'livecore/status.py': 'a1e890530998e9f24fffe80a195d0f5dbeaa7d75bf280b5f4fd707237db069e9', 'livecore/store.py': '55b1306a100ff2ad3ab8e4caa2ad62e9c023eac16fdfa8785df451d953d35eef', 'model_engine.py': 'd1415a85b97c5696bc1dfe499c5ed57a0bba43a7dafe8e819ea6bedbba06ea23', 'quality.py': 'da0e08e9f725b9b9fa71f0227a45065fd2e773886749e047bbde034e90294211', 'strategy.py': '20e3426e6d76d2a05946385a7ebbdbe438921ac8e90056e192665986cf8b9ea6', 'variants.py': '9c635115624bbeb8725220118635d3541759740d4265e4658e44be42319eb4bc'}
PINNED_SERVICE = 'a454374b3311ff01a07561f39a78c1745b29dc677a2fcf6f6e7576caa4fe8e34'


# These are read-only enumeration queries, deliberately separate conditional
# and OCO; an unsupported query must abort, never mean an empty account.
ALGO_TYPES = ('conditional', 'oco', 'trigger', 'move_order_stop', 'chase', 'twap', 'smart_iceberg', 'iceberg')


def unused_snapshot(db):
    validate_schema(db)
    rows = dict(db.execute('SELECT key,payload FROM meta'))
    require('identity' in rows and 'state' in rows, '原账本缺失；不能自动判断可否删除。')
    ident, state = json.loads(rows['identity']), json.loads(rows['state'])
    require(isinstance(ident, dict) and set(ident) == {'config','files','uid','mode','instrument','key_fingerprint','spec'},
            '未知原账本身份结构；没有删除。')
    require(ident['mode'] == 'live' and ident['instrument'] == 'BTC-USDT-SWAP'
            and isinstance(ident['uid'], str) and bool(ident['uid'])
            and isinstance(ident['key_fingerprint'], str)
            and re.fullmatch('[0-9a-f]{64}', ident['key_fingerprint']), '原账本身份无效。')
    require(isinstance(state, dict) and type(state.get('schema')) is int and state['schema'] == 1
            and state.get('armed') is True, '仅支持原版已启用的 v53 账本。')
    require(state.get('position', 'missing') is None and state.get('protection', 'missing') is None
            and state.get('closing') is False, '本地仍有持仓、止损记录或平仓流程；没有删除。')
    require(state.get('halted', 'missing') is None, '存在风控停止记录；不能通过重装重置。')
    zero = ('realized_pnl', 'fees_signed', 'funding', 'max_drawdown', 'closed_trades', 'cooldown_until')
    require(all(type(state.get(k)) in (int, float) and state[k] == 0 for k in zero),
            '发现策略交易、费用、资金费或风险历史；不能按未交易版本清理。')
    capital = ident['config'].get('capital_usdt')
    require(type(capital) in (int, float) and capital == 30
            and all(type(state.get(k)) in (int, float) and state[k] == capital
                    for k in ('cash','peak_equity','last_equity')),
            '原账本资金与未交易的 30U 初始状态不同；没有删除。')
    require(not db.execute('SELECT 1 FROM intents LIMIT 1').fetchone(),
            '策略曾登记下单或止损请求；不能自动删除，需要保留账本核查。')
    require(not db.execute("SELECT 1 FROM records WHERE namespace IN ('fill','funding') LIMIT 1").fetchone(),
            '策略账本已含成交或资金费记录；没有删除。')
    require(type(state.get('initialized_at')) is int and state['initialized_at'] > 0,
            '原账本启用时间无效。')
    require(isinstance(state.get('fill_audit'), dict) and state['fill_audit'].get('ok') is True,
            '原版最近一次成交核对没有通过；先核查，不能删除。')
    return {'identity': ident, 'state': state, 'digest': database_digest(db)}


def validate_tree(root):
    """Fail before any deletion if this root contains mounts or links."""
    import shutil
    require(shutil.rmtree.avoids_symlink_attacks, '当前系统不支持安全删除目录。')
    root = safe_path(root, directory=True)
    device = root.stat().st_dev
    for directory, dirs, files in os.walk(root, followlinks=False):
        for path in (Path(directory), *(Path(directory)/n for n in dirs + files)):
            info = path.lstat()
            require(info.st_uid == os.getuid() and info.st_dev == device and not os.path.ismount(path),
                    '原目录存在其他用户文件或挂载点；没有删除。')
            require(not stat.S_ISLNK(info.st_mode), '原目录存在符号链接；没有删除。')
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode),
                    '原目录包含特殊文件；没有删除。')
            require(stat.S_ISDIR(info.st_mode) or info.st_nlink == 1,
                    '原目录包含硬链接；没有删除。')


class Retirement:
    def __init__(self, *, home=None, modules=None, service_check=None, reload=None,
                 accepted_code=None, accepted_service=None, fault=None):
        # Test dependencies are Python-only injection points, never CLI options.
        if modules is None:
            require(sys.platform.startswith('linux') and os.getuid() != 0,
                    '请在 Debian 服务器以原普通用户运行，不要使用 sudo。')
        self.home = safe_path(home or Path.home(), directory=True)
        self.project = safe_path(self.home / 'btc-v53-live', directory=True)
        self.directory = safe_path(self.project / 'var' / 'live', directory=True)
        self.database = self.directory / 'state.sqlite3'
        self.credential_file = self.project / 'credentials.env'
        self.unit = self.home / '.config' / 'systemd' / 'user' / UNIT
        self.modules = modules
        self.service_check = service_check or (lambda: check_service(self.project))
        self.reload = reload or (lambda: subprocess.run(['systemctl','--user','daemon-reload'],
                                capture_output=True, text=True, timeout=15))
        self.accepted_code = PINNED_CODE if accepted_code is None else accepted_code
        self.accepted_service = PINNED_SERVICE if accepted_service is None else accepted_service
        self.fault = fault or (lambda point: None)

    def _service(self):
        values = self.service_check()
        require(values.get('UnitFileState') == 'disabled', '请先停止并禁用原 v53 用户服务。')
        require(values.get('ActiveState') == 'inactive' and values.get('MainPID') == '0',
                '原 v53 服务仍在运行；没有删除。')
        require(Path(values.get('FragmentPath','')) == self.unit,
                '原服务位置与预期不同；没有删除。')
        unit = safe_path(self.unit)
        require(sha(unit.read_bytes()) == self.accepted_service, '原服务文件不是受支持版本。')

    def _local(self):
        with open_database(self.database) as db:
            db.execute('BEGIN')
            snapshot = unused_snapshot(db)
        require(snapshot['identity']['files'] == self.accepted_code,
                '原代码版本不在本清理工具支持范围；没有删除。')
        verify_sources(self.project, snapshot['identity'])
        safe_path(self.credential_file, private=True)
        if self.modules is None:
            self.modules = load_modules(self.project)
        cfg = self.modules.config.load_config()
        creds = self.modules.config.credentials(self.credential_file)
        expected = self.modules.runtime.identity(cfg, creds, snapshot['identity']['uid'], snapshot['identity']['spec'])
        require(expected == snapshot['identity'], '原配置、代码或密钥与账本绑定不同；没有删除。')
        consent = self.modules.runtime.validate_live_consent(snapshot['state'], cfg)
        require(consent['uid'] == snapshot['identity']['uid'], '账户启用记录不匹配。')
        self.modules.config.validate_account_binding('live', consent['uid'], self.directory, consent['binding_id'])
        binding = safe_path(self.modules.config.account_binding_path('live', consent['uid']), private=True)
        # Account registries are shared with F3. Only this one exact, validated
        # record may be removed; its parent directory is never deleted.
        expected_binding = self.home / '.local/state/btc-v53-live/accounts' / (sha(('live:'+consent['uid']).encode()) + '.json')
        require(binding == expected_binding, '账户绑定位置不匹配；没有删除。')
        return snapshot, cfg, creds, binding, binding.read_bytes()

    @contextmanager
    def _locks(self, uid):
        with ExitStack() as stack:
            for path in (self.directory/'runtime.lock', self.modules.config.account_lock_path('live', uid)):
                safe_path(path, exists=False)
                if path.exists():
                    safe_path(path, private=True)
                stack.enter_context(self.modules.store.process_lock(path))
            yield

    def _remote(self, snapshot, cfg, creds):
        api = self.modules.okx.OKXClient(mode='live', origin=cfg['origin'], proxy=cfg['proxy'],
                                        credentials=creds, allow_orders=False)
        spec, _, account = self.modules.runtime.preflight(api, cfg, require_flat=False)
        require(account['uid'] == snapshot['identity']['uid'], 'API 所属账户与原 v53 不同；没有删除。')
        require(spec == snapshot['identity']['spec'], '合约规格变化；先核查，不能删除。')
        require(account['position'] is None, '交易所仍有持仓；没有删除。')
        require(not api.read('/api/v5/trade/orders-pending', {'limit':'100'}, private=True),
                '交易所仍有普通挂单；没有删除。')
        for kind in ALGO_TYPES:
            require(not api.read('/api/v5/trade/orders-algo-pending', {'ordType':kind,'limit':'100'}, private=True),
                    '交易所仍有条件或算法挂单；没有删除。')
        # The original client's complete-history reader fails closed on partial
        # pagination, unreadable history or >88 days. Pre-arm test fills are
        # allowed; any fill after arming contradicts "never strategy traded".
        rows = api.trade_fills(snapshot['state']['initialized_at'])
        require(isinstance(rows, list), '无法完整读取成交历史；没有删除。')
        for row in rows:
            require(isinstance(row, dict) and str(row.get('fillTime','')).isdigit(), '成交时间格式不明确；没有删除。')
            require(int(row['fillTime']) < snapshot['state']['initialized_at'],
                    '启用策略后出现过真实成交；不能删除旧账本。')
        funding = api.funding_bills(snapshot['state']['initialized_at'])
        require(isinstance(funding, list), '无法完整读取资金费历史；没有删除。')
        for row in funding:
            require(isinstance(row,dict) and str(row.get('ts','')).isdigit(), '资金费历史时间不明确；没有删除。')
            require(int(row['ts']) < snapshot['state']['initialized_at'],
                    '启用策略后有资金费记录；不能删除旧账本。')
        return {'private_account_checked':True, 'orders_sent':0}

    def run(self, confirm=None):
        self._service()
        snapshot, cfg, creds, binding, binding_bytes = self._local()
        with self._locks(snapshot['identity']['uid']):
            snapshot, cfg, creds, binding, binding_bytes = self._local()
            credential_hash = sha(self.credential_file.read_bytes())
            self._remote(snapshot, cfg, creds)
            self._service()
            validate_tree(self.project)
            if confirm is None:
                return {'ready':True, 'orders_sent':0, 'deleted':False, 'path':str(self.project)}
            require(confirm() == 'DELETE V53', '未确认删除；所有原文件保留。')
            # Do not trust account checks made before the human confirmation.
            self._remote(snapshot, cfg, creds)
            self._service()
            current, cfg2, _, binding2, bytes2 = self._local()
            require(current['digest'] == snapshot['digest'] and cfg2 == cfg and binding2 == binding
                    and bytes2 == binding_bytes and sha(self.credential_file.read_bytes()) == credential_hash,
                    '检查后账本、密钥或绑定发生变化；没有删除。')
            validate_tree(self.project)
            self.fault('before_delete')
            # Atomically move the already-confirmed directory out of the old
            # service path before removing its exact account binding. A tiny
            # private receipt lets a later invocation finish an interrupted
            # deletion without guessing paths or restoring any trading state.
            pending, receipt = prepare_retirement(self.home, self.project, binding, binding_bytes,
                                                   self.unit, self.accepted_service)
            self.fault('receipt_prepared')
            finish_retirement_files(self.home, pending, receipt, fault=self.fault)
            try:
                refreshed = self.reload()
                reloaded = refreshed.returncode == 0
            except Exception:
                reloaded = False
            return {'deleted':True, 'orders_sent':0, 'daemon_reloaded':reloaded,
                    'path':str(self.project), 'proxy_unchanged':True, 'f3_unchanged':True}



def receipt_write(path, value):
    path = safe_path(path, exists=False)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded(value).encode()); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists(): temporary.unlink()


def receipt_path(home):
    return home / '.local/state/btc-v53-live/retirements/pending-v53.json'


def prepare_retirement(home, project, binding, binding_bytes, unit, unit_hash):
    pending = safe_path(receipt_path(home), exists=False)
    require(not pending.exists(), '存在未完成清理；请加 --finish-retirement 完成之前的操作。')
    if not pending.parent.exists(): pending.parent.mkdir(mode=0o700)
    safe_path(pending.parent, directory=True, private=True)
    identifier = uuid.uuid4().hex
    info = project.stat()
    tombstone = home / ('.btc-v53-live-retiring-' + identifier)
    require(not tombstone.exists(), '清理临时目录已存在；停止操作。')
    receipt = {'schema':1, 'id':identifier, 'project':str(project), 'tombstone':str(tombstone),
               'device':info.st_dev, 'inode':info.st_ino,
               'binding':str(binding), 'binding_hash':sha(binding_bytes),
               'unit':str(unit), 'unit_hash':unit_hash, 'phase':'confirmed'}
    receipt_write(pending, receipt)
    return pending, receipt


def validate_receipt(home, receipt):
    require(isinstance(receipt, dict) and set(receipt) == {'schema','id','project','tombstone','device','inode',
            'binding','binding_hash','unit','unit_hash','phase'} and receipt['schema'] == 1
            and receipt['phase'] == 'confirmed' and isinstance(receipt['id'],str)
            and re.fullmatch('[0-9a-f]{32}',receipt['id']), '清理收据格式不匹配；不继续删除。')
    require(receipt['project'] == str(home/'btc-v53-live')
            and receipt['tombstone'] == str(home/('.btc-v53-live-retiring-'+receipt['id']))
            and receipt['unit'] == str(home/'.config/systemd/user'/UNIT), '清理收据路径不匹配。')
    binding = Path(receipt['binding'])
    require(binding.parent == home/'.local/state/btc-v53-live/accounts'
            and re.fullmatch(r'[0-9a-f]{64}\.json',binding.name)
            and all(type(receipt[k]) is int and receipt[k] >= 0 for k in ('device','inode')),
            '清理收据账户范围不匹配。')
    require(all(isinstance(receipt[k],str) and re.fullmatch('[0-9a-f]{64}',receipt[k])
                for k in ('binding_hash','unit_hash')), '清理收据校验值不匹配。')


def finish_retirement_files(home, pending, receipt, fault=lambda point:None):
    import shutil
    validate_receipt(home, receipt)
    project = safe_path(receipt['project'], exists=False)
    tombstone = safe_path(receipt['tombstone'], exists=False)
    binding = safe_path(receipt['binding'], exists=False)
    unit = safe_path(receipt['unit'], exists=False)
    require(not (project.exists() and tombstone.exists()), '原路径与清理目录同时存在；不继续删除。')
    # Check all remaining external files before making a further change.
    if binding.exists(): require(sha(safe_path(binding,private=True).read_bytes()) == receipt['binding_hash'],
                                 '账户绑定已改变；不继续删除。')
    if unit.exists(): require(sha(safe_path(unit).read_bytes()) == receipt['unit_hash'], '用户服务已改变；不继续删除。')
    root = project if project.exists() else tombstone
    if root.exists():
        info = root.stat()
        require((info.st_dev,info.st_ino) == (receipt['device'],receipt['inode']),
                '原目录已被替换；不继续删除。')
        validate_tree(root)
    if project.exists():
        os.rename(project,tombstone); fsync_directory(home)
    fault('directory_renamed')
    if binding.exists(): binding.unlink(); fsync_directory(binding.parent)
    fault('binding_deleted')
    if unit.exists(): unit.unlink(); fsync_directory(unit.parent)
    fault('unit_deleted')
    if tombstone.exists(): shutil.rmtree(tombstone); fsync_directory(home)
    fault('directory_deleted')
    completed = pending.with_name(receipt['id']+'.completed.json')
    os.replace(pending,completed); fsync_directory(pending.parent)


def recover_retirement(*, home=None, service_check=None, reload=None):
    from contextlib import contextmanager
    import fcntl
    if home is None:
        require(sys.platform.startswith('linux') and os.getuid() != 0, '请在 Debian 服务器以原普通用户运行，不要 sudo。')
    home = safe_path(home or Path.home(), directory=True)
    pending = safe_path(receipt_path(home), private=True)
    safe_path(pending.parent, directory=True, private=True)
    receipt = json.loads(pending.read_text())
    validate_receipt(home,receipt)
    def default_service():
        fields = ('LoadState','ActiveState','MainPID','UnitFileState','DropInPaths','TriggeredBy')
        result = subprocess.run(['systemctl','--user','show','--all','--no-pager','--property='+','.join(fields),'--',UNIT],
                                capture_output=True,text=True,timeout=10)
        values = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        require(set(values) == set(fields), '无法核实原服务是否停止；不继续清理。')
        require(values['ActiveState'] == 'inactive' and values['MainPID'] == '0'
                and values['LoadState'] in ('loaded','not-found')
                and (values['UnitFileState'] in ('disabled','') or
                     (not Path(receipt['unit']).exists() and values['UnitFileState'] in ('bad','not-found')))
                and not values['DropInPaths'] and not values['TriggeredBy'], '原服务没有完全停止或已重新启用。')
    check = service_check or default_service
    check()
    lock = home/'.local/state/btc-v53-live/locks'/(Path(receipt['binding']).stem+'.lock')
    safe_path(lock, private=True)
    descriptor = os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        check()
        require(json.loads(pending.read_text()) == receipt, '清理收据在检查期间变化。')
        if Path(receipt['project']).exists():
            require(not Path(receipt['tombstone']).exists(), '原目录和清理目录同时存在；不继续。')
            # Receipt committed, rename not yet performed. The original service
            # may have run since the interruption, so NEVER delete using stale
            # financial proof. Cancel only this marker; a normal new invocation
            # must repeat every ledger/account/network check and confirmation.
            canceled = pending.with_name(receipt['id']+'.canceled.json')
            os.replace(pending,canceled); fsync_directory(pending.parent)
            return 'canceled_recheck'
        finish_retirement_files(home,pending,receipt)
    finally:
        os.close(descriptor)
    if reload is not None: reload()
    else:
        result = subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True,text=True,timeout=15)
        if result.returncode: print('请再执行：systemctl --user daemon-reload')
    return 'completed'


def main(argv=None):
    parser = argparse.ArgumentParser(description='只清理未发生策略交易的原 v53；不会下单或处理 F3、Mihomo。')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true', help='只检查，不删除')
    group.add_argument('--finish-retirement', action='store_true', help='完成先前已确认但被中断的清理')
    args = parser.parse_args(argv)
    try:
        require(not any(name == 'livecore' or name.startswith('livecore.') for name in sys.modules),
                '请单独运行本清理脚本。')
        if args.finish_retirement:
            phase = recover_retirement()
            if phase == 'canceled_recheck':
                print('原路径目前仍有目录，未按旧收据删除它。中断标记已取消；需要清理时请重新正常运行本脚本，完整检查后再确认。')
            else:
                print('先前确认的清理已完成。现在可以解压新版并重新配置。')
            return 0
        if args.check:
            result = Retirement().run()
            print('检查通过：原 v53 没有策略交易，目前空仓、无挂单。尚未删除任何文件。')
        else:
            require(sys.stdin.isatty(), '请在服务器交互终端运行，以便确认删除。')
            def confirm():
                print('即将删除 ' + str(Path.home()/'btc-v53-live') + ' 内的旧代码、旧配置、API 密钥文件及未交易账本。')
                print('同时移除原 v53 用户服务和该账户的旧绑定；不会删除 Mihomo、F3 或测试交易收据。')
                print('请先确保 API 信息可在你自己的安全位置找回，新版需要重新填写。')
                return input('输入 DELETE V53 确认，其他输入取消：').strip()
            result = Retirement().run(confirm)
            print('旧 v53 已清理；Mihomo、F3 和各自文件保持原样。现在可以解压新版并重新配置。')
            if not result['daemon_reloaded']:
                print('请再执行：systemctl --user daemon-reload')
        return 0
    except (KeyboardInterrupt, EOFError):
        print('操作中止。如果尚未确认 DELETE V53，原文件均保留。')
        return 1
    except RetirementError as exc:
        print(str(exc))
        return 1
    except Exception:
        # Never expose credentials, raw exchange responses or exception strings.
        print('清理未完成。若已输入 DELETE V53，可执行同一脚本并加 --finish-retirement，继续先前已确认的清理。')
        print('若尚未确认删除，请保留原文件，核查网络、API 权限和服务状态。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
