"""One explicitly confirmed real buy, durable once-only receipt, no auto-close.

This is independent of strategy state. It never retries an order POST, changes
leverage, starts a service, or submits a protective/closing order. Reconciliation
performs exchange reads only. Acknowledgement is never reported as a fill.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import os
from pathlib import Path
import uuid

from .config import Attention, safe_error
from .market import INSTRUMENT, account_snapshot, specification, ticker
from .store import process_lock, read_status
from .okx import APIError, SafetyError

ALGO_KINDS = ('conditional,oco', 'trigger', 'move_order_stop', 'chase', 'twap', 'smart_iceberg', 'iceberg')
BUDGET = Decimal('1')
NOTIONAL = Decimal('15')


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise Attention('小额测试收到非法数值，未提交新订单。') from None
    if not result.is_finite():
        raise Attention('小额测试收到非法数值，未提交新订单。')
    return result


def _text(value):
    return format(value, 'f')


def _floor(value, step):
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _live(api, cfg):
    if cfg['mode'] != 'live' or getattr(api, 'mode', None) != 'live':
        raise Attention('该测试只用于你明确选择的真钱账户，必须使用live配置和真钱API。')
    if cfg['instrument'] != INSTRUMENT or cfg['leverage'] != 15:
        raise Attention('小额测试固定为BTC-USDT永续、逐仓15倍。')


def receipt_path(uid):
    digest = hashlib.sha256(('live:' + str(uid)).encode()).hexdigest()
    return Path.home() / '.local/state/btc-v53-live/smoke' / digest / 'receipt.json'


def _secure_directory(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.mkdir(exist_ok=True, mode=0o700)
    for parent in (path, path.parent):
        if parent.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
            raise Attention('小额测试记录目录权限不安全，请保留记录并检查目录所有者及权限。')


def _write(path, value, first=False):
    _secure_directory(path.parent)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    if first:
        target = path
    else:
        target = path.with_name('.receipt-' + uuid.uuid4().hex + '.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if not first:
            os.replace(target, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if not first and target.exists():
            target.unlink()


def _load(path, cfg, uid):
    if not path.exists():
        if path.is_symlink():
            raise Attention('小额测试记录是符号链接，不能继续。')
        return None
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise Attention('小额测试记录须属于当前用户并保持600权限；不要删除记录。')
    if path.stat().st_size > 65536:
        raise Attention('小额测试记录格式异常；请保留记录并人工核查。')
    try:
        receipt = json.loads(path.read_text())
        r = receipt['request']
        valid = (receipt['schema'] == 1 and receipt['uid'] == str(uid)
                 and receipt['mode'] == 'live' and receipt['origin'] == cfg['origin']
                 and r['instId'] == INSTRUMENT and r['tdMode'] == 'isolated'
                 and r['side'] == 'buy' and r['posSide'] == 'net' and r['ordType'] == 'ioc'
                 and r['clOrdId'].isalnum() and len(r['clOrdId']) <= 32
                 and _decimal(r['sz']) > 0 and _decimal(r['px']) > 0
                 and _decimal(receipt['ct_value']) > 0
                 and _decimal(r['sz']) * _decimal(r['px']) * _decimal(receipt['ct_value']) <= NOTIONAL)
        if not valid:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Attention('小额测试记录与账户或配置不符；不重试下单，不删除记录。') from None
    return receipt


def _no_orders(api):
    if api.read('/api/v5/trade/orders-pending', {'limit': '100'}, private=True):
        raise Attention('账户有未完成订单；请先在欧易核对，不进行本次小额测试。')
    for kind in ALGO_KINDS:
        if api.read('/api/v5/trade/orders-algo-pending', {'ordType': kind, 'limit': '100'}, private=True):
            raise Attention('账户有算法或条件订单；请先在欧易核对，不进行本次小额测试。')


def _preflight(api, cfg):
    api.sync_time()
    snap = account_snapshot(api, cfg)
    if snap['position'] is not None:
        raise Attention('小额测试要求账户空仓，避免加到原有仓位。')
    _no_orders(api)
    spec = specification(api)
    q = ticker(api, cfg)  # Most recent request: account scans may take seconds.
    return snap, spec, q


def _plan(spec, q, cfg):
    tick, lot, minimum, ct = [_decimal(spec[k]) for k in ('tick', 'lot_contracts', 'min_contracts', 'ct_value')]
    if min(tick, lot, minimum, ct) <= 0:
        raise Attention('合约规格不合法。')
    price = _floor(_decimal(q['ask']) * Decimal('1.001'), tick)
    if price < _decimal(q['ask']):
        raise Attention('价格步长无法组成合适的小额买单。')
    size = _floor(min(NOTIONAL / (price * ct), _decimal(spec['max_contracts'])), lot)
    if size < minimum or size <= 0:
        raise Attention('交易所最小下单量超过约1U保证金预算；本测试停止，不自动加大订单。')
    notional = price * ct * size
    return {'mode': 'live', 'instrument': INSTRUMENT, 'side': 'buy', 'direction': 'long',
            'margin_mode': 'isolated', 'leverage': 15, 'contracts': _text(size),
            'btc_quantity': _text(size * ct), 'limit_price': _text(price),
            'max_notional_usdt': _text(notional), 'estimated_margin_usdt': _text(notional / 15),
            'fee_reserve_usdt': _text(notional * _decimal(cfg['fee']) * 2),
            'automatic_stop_or_close': False,
            'message': '真钱买入做多；IOC可能部分成交或不成交。无自动止损或平仓，成交后由你在欧易手动平仓。'}


def _validate_terminal(receipt, row):
    request = receipt['request']
    if not isinstance(row, dict) or any(row.get(k) != request[k] for k in ('instId', 'clOrdId', 'side', 'posSide', 'ordType', 'tdMode')):
        raise Attention('订单查询结果身份或类型不匹配；不要重新下单，请在欧易核查。')
    if not str(row.get('ordId', '')).isdigit():
        raise Attention('订单编号不合法；请在欧易核查。')
    if _decimal(row.get('sz')) != _decimal(request['sz']) or _decimal(row.get('px')) != _decimal(request['px']):
        raise Attention('订单数量或限价与持久记录不匹配；请在欧易核查。')
    if row.get('state') not in ('filled', 'canceled'):
        return None
    filled = _decimal(row.get('accFillSz'))
    size = _decimal(request['sz'])
    if filled < 0 or filled > size or (row['state'] == 'filled' and filled != size):
        raise Attention('累计成交数量异常；请在欧易核查。')
    avg = _decimal(row.get('avgPx') or '0')
    fee = _decimal(row.get('fee') or '0')
    if filled and (avg <= 0 or avg > _decimal(request['px']) or row.get('feeCcy') != 'USDT'):
        raise Attention('实际成交价或手续费币种异常；请在欧易核查。')
    return {'order_id': str(row['ordId']), 'order_state': row['state'],
            'filled_contracts': _text(filled), 'average_price': _text(avg),
            'fee_usdt': _text(fee), 'terminal': True}


def _summary(receipt):
    terminal = receipt.get('terminal')
    out = {'mode': 'live', 'receipt': str(receipt_path(receipt['uid'])),
           'client_order_id': receipt['request']['clOrdId'], 'phase': receipt['phase'],
           'orders_attempted': 1, 'automatic_stop_or_close': False,
           'safe_to_arm_strategy': bool(receipt.get('cleared')), 'plan': receipt['plan']}
    if receipt.get('definitive_no_order'):
        out['submitted'] = False
        out['rejection_code'] = receipt['rejection_code']
        out['message'] = ('本次订单被明确拒绝或未发送，且最新检查确认空仓、无挂单；不会重试测试单。'
                          if receipt.get('cleared') else '本次订单被明确拒绝或未发送；还需确认账户空仓且无挂单。')
    elif terminal:
        out.update(terminal)
        out['message'] = ('测试订单已结束，且最新检查确认账户空仓、无挂单。' if receipt.get('cleared') else
            '测试订单已结束；若有成交仓位，请在欧易手动平仓后再运行test-status。')
    else:
        out['message'] = '下单结果尚未确认，可能已有成交。请在欧易查看持仓和订单；仅运行test-status查询，不会重试下单。'
    if receipt.get('last_error'):
        out['last_error'] = receipt['last_error']
    return out


def _reconcile(api, cfg, uid, path, receipt):
    receipt['cleared'] = False
    receipt.pop('last_error', None)
    try:
        if not receipt.get('terminal') and not receipt.get('definitive_no_order'):
            row = api.order(receipt['request']['clOrdId'])
            terminal = _validate_terminal(receipt, row) if row is not None else None
            if terminal:
                receipt['terminal'] = terminal
                receipt['phase'] = 'terminal'
            else:
                receipt['phase'] = 'uncertain'
        if receipt.get('terminal') or receipt.get('definitive_no_order'):
            snap = account_snapshot(api, cfg)
            if snap['uid'] != str(uid):
                raise Attention('查询账户身份变化；请核对密钥。')
            if snap['position'] is None:
                _no_orders(api)
                receipt['cleared'] = True
        receipt['checked_at'] = api.now()
    except Exception as exc:
        receipt['last_error'] = safe_error(exc)
    _write(path, receipt)
    return _summary(receipt)


def buy(api, cfg, confirm):
    """Callback receives a safe preview. True means one human-approved attempt."""
    from .config import account_lock_path, assert_account_unbound, data_dir
    _live(api, cfg)
    if not getattr(api, 'allow_orders', False):
        raise Attention('小额测试下单权限未启用。')
    api.sync_time()
    identity = account_snapshot(api, cfg)
    uid = identity['uid']
    with process_lock(account_lock_path('live', uid)):
        assert_account_unbound('live', uid)
        if read_status(data_dir(cfg))['state'] is not None:
            raise Attention('本目录主策略已启用，不能再运行独立小额测试。')
        path = receipt_path(uid)
        receipt = _load(path, cfg, uid)
        if receipt is not None:
            return _reconcile(api, cfg, uid, path, receipt)
        snap, spec, q = _preflight(api, cfg)
        if snap['uid'] != uid:
            raise Attention('预检查账户身份变化。')
        plan = _plan(spec, q, cfg)
        required = _decimal(plan['estimated_margin_usdt']) + _decimal(plan['fee_reserve_usdt'])
        if _decimal(snap['available']) < required:
            raise Attention('可用USDT不足测试保证金和手续费预算。')
        if confirm(dict(plan)) is not True:
            return {'submitted': False, 'orders_attempted': 0, 'message': '已取消；没有提交订单。'}
        # Human interaction may take minutes. Repeat all read-only gates and
        # fetch a fresh quote; never increase approved size or price.
        fresh, fresh_spec, q = _preflight(api, cfg)
        assert_account_unbound('live', uid)
        if fresh['uid'] != uid or fresh_spec != spec:
            raise Attention('确认后账户或合约规格发生变化；本次未下单。')
        candidate = _plan(spec, q, cfg)
        price = min(_decimal(plan['limit_price']), _decimal(candidate['limit_price']))
        size = min(_decimal(plan['contracts']), _decimal(candidate['contracts']))
        if _decimal(q['ask']) > price:
            raise Attention('确认后行情已超过展示的限价；本次未下单，请重新检查。')
        notional = size * price * _decimal(spec['ct_value'])
        if notional > NOTIONAL or notional / 15 > BUDGET:
            raise Attention('测试金额超出1U保证金预算，未提交。')
        required = notional / 15 + notional * _decimal(cfg['fee']) * 2
        if _decimal(fresh['available']) < required:
            raise Attention('确认后账户可用USDT不足，未提交。')
        request = {'instId': INSTRUMENT, 'tdMode': 'isolated', 'side': 'buy', 'posSide': 'net',
                   'ordType': 'ioc', 'sz': _text(size), 'px': _text(price),
                   'clOrdId': 'v53test' + uuid.uuid4().hex[:24]}
        receipt = {'schema': 1, 'uid': uid, 'mode': 'live', 'origin': cfg['origin'],
                   'created_at': api.now(), 'phase': 'sent_or_unknown', 'request': request,
                   'ct_value': spec['ct_value'], 'plan': plan, 'cleared': False}
        _write(path, receipt, first=True)  # fsync before the ONLY order POST.
        try:
            deadline = min(api.now() + 5000, int(q['ts']) + 5000)
            api.write('/api/v5/trade/order', request, exp_time=deadline)
            receipt['phase'] = 'acknowledged_not_fill'
            _write(path, receipt)
        except (APIError, SafetyError) as exc:
            # These client exceptions prove a single order was rejected by
            # OKX, or prevented locally BEFORE transport. Never retry even so.
            receipt['phase'] = 'not_submitted' if isinstance(exc, SafetyError) else 'rejected'
            receipt['definitive_no_order'] = True
            receipt['rejection_code'] = safe_error(exc)
            _write(path, receipt)
        except Exception as exc:
            receipt['phase'] = 'uncertain'
            receipt['last_error'] = safe_error(exc)
            _write(path, receipt)
        return _reconcile(api, cfg, uid, path, receipt)


def status(api, cfg):
    """Exchange reads only; updates the local receipt with confirmed evidence."""
    from .config import account_lock_path
    _live(api, cfg)
    api.sync_time()
    snap = account_snapshot(api, cfg)
    uid = snap['uid']
    with process_lock(account_lock_path('live', uid)):
        path = receipt_path(uid)
        receipt = _load(path, cfg, uid)
        if receipt is None:
            return {'mode': 'live', 'orders_attempted': 0, 'message': '本账户没有小额测试记录；本指令不会下单。'}
        return _reconcile(api, cfg, uid, path, receipt)


def ensure_smoke_cleared(api, cfg, uid):
    """Called by arm-live while it already holds the shared account lock."""
    _live(api, cfg)
    path = receipt_path(uid)
    receipt = _load(path, cfg, uid)
    if receipt is None:
        return
    result = _reconcile(api, cfg, uid, path, receipt)
    if not result['safe_to_arm_strategy']:
        raise Attention('小额测试尚未确认结束且空仓；请先用test-status核对，并在欧易手动平仓。结果未知时不能启动主策略。')
