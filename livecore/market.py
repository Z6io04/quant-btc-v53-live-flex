"""Validated public data; gaps delay decisions, never manufacture candles."""
from decimal import Decimal
import math
import time

from .config import Attention

MINUTE, HOUR, DAY = 60000, 3600000, 86400000
INSTRUMENT = 'BTC-USDT-SWAP'


def number(value, positive=False):
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise Attention('交易所数值缺失或不合法；暂停新增风险。') from exc
    if not math.isfinite(value) or (positive and value <= 0):
        raise Attention('交易所数值缺失或不合法；暂停新增风险。')
    return value


def specification(api):
    rows = api.read('/api/v5/public/instruments', {'instType':'SWAP','instId':INSTRUMENT})
    if len(rows) != 1:
        raise Attention('无法唯一确认BTC合约规格。')
    r = rows[0]
    if (r.get('instId') != INSTRUMENT or r.get('state') != 'live'
            or r.get('ctType') != 'linear' or r.get('ctValCcy') != 'BTC'
            or r.get('settleCcy') != 'USDT'):
        raise Attention('合约状态或计价方式不受本版本支持。')
    tiers = api.read('/api/v5/public/position-tiers',
                    {'instType':'SWAP','tdMode':'isolated','instFamily':'BTC-USDT'})
    first = [x for x in tiers if str(x.get('tier')) == '1']
    if len(first) != 1:
        raise Attention('无法确认第一档保证金要求。')
    t = first[0]
    if number(t['maxLever'], True) < 15:
        raise Attention('交易所第一档不允许本策略的15倍杠杆。')
    number(r.get('ctVal'), True)
    number(r.get('ctMult') or '1', True)
    number(t.get('maxSz'), True)
    number(r.get('maxLmtSz'), True)
    out = {'ct_value':str(Decimal(r['ctVal'])*Decimal(r.get('ctMult') or '1')),
           'lot_contracts':r['lotSz'],'min_contracts':r['minSz'],
           'max_contracts':str(min(Decimal(t['maxSz']),Decimal(r['maxLmtSz']))),
           'tick':r['tickSz'],'mmr':t['mmr']}
    from .decisions import validate_spec
    try:
        validate_spec(out)
    except (ValueError, KeyError) as exc:
        raise Attention('合约张数、价格步长或保证金参数不合法。') from exc
    return out


def ticker(api, cfg):
    rows = api.read('/api/v5/market/ticker', {'instId':INSTRUMENT})
    if len(rows) != 1 or rows[0].get('instId') != INSTRUMENT:
        raise Attention('没有有效的BTC买卖报价。')
    r = rows[0]
    q = {'bid':number(r['bidPx'],True),'ask':number(r['askPx'],True),
         'last':number(r['last'],True),'ts':int(number(r.get('ts')))}
    if q['bid'] > q['ask'] or not -1000 <= api.now()-q['ts'] <= cfg['quote_age_seconds']*1000:
        raise Attention('买卖报价过期或交叉；不提交新订单。')
    return q


def _taker_fee_cost(api):
    """Resolve this USDT swap's current fee group; never guess a legacy rate."""
    rows = api.read('/api/v5/public/instruments', {'instType':'SWAP','instId':INSTRUMENT})
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise Attention('手续费校验无法唯一确认 public/instruments；暂停新增风险。')
    instrument = rows[0]
    for field, expected in (('instId', INSTRUMENT), ('instType', 'SWAP'),
                            ('instFamily', 'BTC-USDT'), ('settleCcy', 'USDT'),
                            ('ctType', 'linear'), ('ctValCcy', 'BTC'), ('state', 'live')):
        if instrument.get(field) != expected:
            raise Attention('手续费校验无法确认 public/instruments.'+field+'；暂停新增风险。')
    group_id = instrument.get('groupId')
    if not isinstance(group_id, str) or not group_id or group_id.strip() != group_id:
        raise Attention('交易所字段 public/instruments.groupId 缺失或不合法；暂停新增风险。')

    # Keep instFamily so OKX returns any applicable pair-specific incentive rate.
    # groupId and instFamily must not be passed together in this request.
    fees = api.read('/api/v5/account/trade-fee',
                    {'instType':'SWAP','instFamily':'BTC-USDT'}, private=True)
    if not isinstance(fees, list) or len(fees) != 1 or not isinstance(fees[0], dict):
        raise Attention('手续费校验无法唯一确认 trade-fee；暂停新增风险。')
    if fees[0].get('instType') != 'SWAP':
        raise Attention('交易所字段 trade-fee.instType 不符；暂停新增风险。')
    groups = fees[0].get('feeGroup')
    if not isinstance(groups, list) or not groups:
        raise Attention('交易所字段 trade-fee.feeGroup 缺失或不合法；暂停新增风险。')
    indexed = {}
    for group in groups:
        if not isinstance(group, dict):
            raise Attention('交易所字段 trade-fee.feeGroup 不合法；暂停新增风险。')
        key = group.get('groupId')
        if (not isinstance(key, str) or not key or key.strip() != key or key in indexed):
            raise Attention('交易所字段 trade-fee.feeGroup.groupId 缺失、重复或不合法；暂停新增风险。')
        indexed[key] = group
    if group_id not in indexed:
        raise Attention('交易所字段 trade-fee.feeGroup.groupId 未匹配合约；暂停新增风险。')
    value = indexed[group_id].get('taker')
    try:
        if not isinstance(value, str):
            raise Attention('invalid fee type')
        taker = number(value)
    except Attention:
        raise Attention('交易所字段 trade-fee.feeGroup.taker 缺失或不合法；暂停新增风险。') from None
    # OKX negative means a charge; positive means a rebate. Never budget rebates.
    return max(0., -taker)


def account_snapshot(api, cfg, check_settings=True):
    """Read-only. Never change leverage, position/account mode or move funds."""
    configs = api.read('/api/v5/account/config', private=True)
    if len(configs) != 1 or not configs[0].get('uid'):
        raise Attention('无法确认账户身份。')
    a = configs[0]
    if a.get('acctLv') != '2' or a.get('posMode') != 'net_mode':
        raise Attention('请在欧易手动设为合约模式（原单币种保证金）、买卖/净持仓模式；本程序不自动修改账户。')
    if 'withdraw' in str(a.get('perm','')).lower():
        raise Attention('该API带有提币权限；请改用仅读取和交易权限的独立密钥。')
    if 'trade' not in {p.strip() for p in str(a.get('perm','')).lower().split(',')}:
        raise Attention('该API没有可确认的交易权限；请使用本程序专用的读取和交易密钥。')
    if check_settings:
        lever = api.read('/api/v5/account/leverage-info',
                         {'instId':INSTRUMENT,'mgnMode':'isolated'}, private=True)
        if (not lever or any(x.get('instId') != INSTRUMENT or x.get('mgnMode') != 'isolated'
                or number(x['lever']) != cfg['leverage'] for x in lever)):
            raise Attention('请在欧易为BTC-USDT永续手动设置逐仓15倍，然后重新检查。')
        if _taker_fee_cost(api) > cfg['fee']+1e-12:
            raise Attention('实际吃单费率高于策略费用预算；需要重新评估，不能静默改参数。')
    positions = api.read('/api/v5/account/positions', private=True)
    active = [p for p in positions if number(p.get('pos')) != 0]
    if any(p.get('instId') != INSTRUMENT or p.get('mgnMode') != 'isolated'
           or p.get('posSide') != 'net' or number(p.get('lever') or 0) != cfg['leverage'] for p in active):
        raise Attention('账户存在其他合约、其他持仓模式或杠杆；只允许本程序专用账户。')
    if len(active) > 1:
        raise Attention('账户出现多笔不兼容仓位。')
    rows = api.read('/api/v5/account/balance', {'ccy':'USDT'}, private=True)
    details = [d for row in rows for d in row.get('details',[]) if d.get('ccy') == 'USDT']
    if len(details) != 1:
        raise Attention('无法读取USDT可用保证金。')
    d = details[0]
    available = number(d.get('availBal'))
    if available < 0:
        raise Attention('USDT可用余额为负；不支持借贷账户状态。')
    return {'uid':str(a['uid']),'position':active[0] if active else None,
            # For this acctLv=2 isolated implementation, use available balance,
            # never total equity/available cross equity as spendable collateral.
            'available':available,
            'exchange_equity':number(d.get('eq'))}


def parse_candles(rows, now):
    result = []
    for r in rows:
        if len(r) != 9 or str(r[-1]) not in ('0','1'):
            raise Attention('分钟K线格式不符。')
        if str(r[-1]) == '0':
            continue
        ts = int(r[0])
        o,h,l,c,v = [number(x) for x in r[1:6]]
        if (ts % MINUTE or ts+MINUTE > now or not 0 < l <= min(o,c) <= max(o,c) <= h or v < 0):
            raise Attention('分钟K线数值或确认时间不合法。')
        result.append([ts,o,h,l,c,v])
    if len({r[0] for r in result}) != len(result):
        raise Attention('行情响应含重复分钟。')
    return sorted(result)


class Market:
    def __init__(self, api, store, cfg):
        self.api,self.store,self.cfg = api,store,cfg
        self.last_fetch = 0
        self.frames = None
        self.frame_end = None

    def update(self):
        now = self.api.now()
        wanted = now//MINUTE*MINUTE-MINUTE
        start = self.store.get_state()['warm_start']
        if not isinstance(start, int) or start < 0 or start % (4*HOUR):
            raise Attention('预热起点必须固定且对齐4小时边界。')
        if start > wanted:
            return None
        # At most two history requests per loop; account protection gets time
        # even when months of data need downloading after a long outage.
        for _ in range(2):
            bars = self.store.bars()
            after = None
            if not bars:
                after = wanted+MINUTE
            elif bars[0][0] > start:
                after = bars[0][0]
            else:
                gap = next((b[0] for a,b in zip(bars,bars[1:]) if b[0]-a[0] != MINUTE),None)
                if gap is not None:
                    after = gap
                elif bars[-1][0] < wanted or self.last_fetch == 0 or time.monotonic()-self.last_fetch > 55:
                    after = wanted+MINUTE
            if after is None:
                break
            rows = self.api.read('/api/v5/market/history-candles',
                  {'instId':INSTRUMENT,'bar':'1m','after':after,'limit':'300'})
            parsed = parse_candles(rows,self.api.now())
            if any(r[0] >= after for r in parsed):
                raise Attention('历史行情分页越界；不使用该批数据。')
            new = [r for r in parsed if r[0] >= start]
            if not new:
                raise Attention('历史行情返回空段，不能完成预热或补齐缺口。')
            old = {r[0]:r for r in bars}
            if any(r[0] in old and r != old[r[0]] for r in new):
                raise Attention('已经缓存的已确认行情被交易所修订；暂停新增风险并人工核查。')
            self.store.upsert_bars(new)
            self.last_fetch = time.monotonic()
        bars = self.store.bars()
        # A request can cross a minute/hour boundary. Readiness must be assessed
        # against the CURRENT time, not the time before two slow page requests.
        # Never feed a cached future candle into the indicators after clock
        # correction. Recovered history only warms indicators; we return solely
        # the latest completed-hour frame, never a queue of historical actions.
        now = self.api.now()
        wanted = now//MINUTE*MINUTE-MINUTE
        bars = [b for b in bars if start <= b[0] <= wanted]
        ready = bool(bars and bars[0][0] == start and bars[-1][0] >= wanted
            and all(b[0]-a[0] == MINUTE for a,b in zip(bars,bars[1:])))
        if not ready:
            return None
        end = (wanted+MINUTE)//HOUR*HOUR
        if end-start < self.cfg['warmup_days']*DAY:
            return None
        if end != self.frame_end:
            from variants import prepare_variants
            frames = prepare_variants(bars)['v53']['frames']
            self.frames = frames.get(end)
            self.frame_end = end
        return self.frames
