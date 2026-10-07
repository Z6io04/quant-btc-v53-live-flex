"""Read-only transfer evidence and explicitly confirmed additions to strategy capital.

Only OKX trading-account type=1/subType=11 positive USDT transfer-ins are
eligible. Public specification: https://www.okx.com/docs-v5/#trading-account-rest-api-get-bill-types
The running monitor archives bills but never increases the budget itself.
"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from . import config
from .config import Attention, AuditPending
from .okx import OKXClient, INSTRUMENT
from .store import Store, process_lock, _json

DAY = 86400000
EPS = Decimal('0.00000001')  # Legacy execution cash/fees are binary floats.
BILL_FIELDS = ('billId','type','subType','ccy','balChg','fee','pnl','instId',
               'instType','ordId','ts','mgnMode')
UNIT = 'btc-v53-live.service'


def require(condition, message):
    if not condition:
        raise Attention(message)


def number(value):
    try:
        require(not isinstance(value,bool), '资金核对数值不合法。')
        result = Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):
        raise Attention('资金核对数值缺失或不合法。') from None
    require(result.is_finite(), '资金核对数值不合法。')
    return result


def text(value):
    return format(number(value),'f')


def finite_float(value):
    value = number(value)
    result = float(value)
    require(math.isfinite(result) and abs(Decimal(str(result))-value) <= EPS,
            '资金数值超过账本精度；没有同步。')
    return result


def validate_metadata(state, cfg):
    require(isinstance(state,dict), '缺少资金账本。')
    initial, added, capital = (number(cfg['capital_usdt']),
                              number(state.get('capital_added_usdt')),
                              number(state.get('capital_usdt')))
    require(initial > 0 and added >= 0 and capital > 0 and
            abs(capital-initial-added) <= EPS, '累计本金与原本金、已核对注资不一致。')
    anchor,cursor = state.get('capital_anchor_at'),state.get('capital_bill_cursor')
    require(type(anchor) is int and type(cursor) is int and
            0 <= state['initialized_at'] <= anchor <= cursor, '注资账单时间记录缺失或不合法；不能自行重建。')
    require(number(state.get('capital_exchange_offset')) >= -EPS, '账户现金基准缺失或不合法。')
    ids = state.get('capital_baseline_bill_ids')
    require(isinstance(ids,list) and all(isinstance(x,str) and x.isascii() and x.isdigit() for x in ids)
            and len(ids)==len(set(ids)), '启用时账单边界记录不合法。')
    return capital


def normalize_bill(raw):
    require(isinstance(raw,dict), '账单格式不合法。')
    require(all(isinstance(raw.get(k,''),str) for k in BILL_FIELDS), '账单字段必须是交易所原始文本。')
    row = {k:raw.get(k,'') for k in BILL_FIELDS}
    require(all(row[k].isascii() and row[k].isdigit() for k in ('billId','ts','type','subType')),
            '账单编号或时间不合法。')
    number(row['balChg'])
    return row


def flat_cash(api):
    """Actual cash, not equity or available equity, on a dedicated flat account."""
    positions = api.read('/api/v5/account/positions',private=True)
    require(isinstance(positions,list) and not any(number(p.get('pos')) != 0 or
            number(p.get('margin') or 0) != 0 or number(p.get('liab') or 0) != 0 for p in positions),
            '账户仍有持仓、逐仓保证金或负债；必须全部结束后同步。')
    require(not api.read('/api/v5/trade/orders-pending',{'limit':'100'},private=True),
            '账户仍有普通挂单，不能同步资金。')
    for kind in ('conditional','oco','trigger','move_order_stop','chase','twap','smart_iceberg','iceberg'):
        require(not api.read('/api/v5/trade/orders-algo-pending',{'ordType':kind,'limit':'100'},private=True),
                '账户仍有算法挂单，不能同步资金。')
    rows = api.read('/api/v5/account/balance',private=True)
    require(isinstance(rows,list) and len(rows)==1 and isinstance(rows[0].get('details'),list),
            '无法取得完整账户余额。')
    details,seen = rows[0]['details'],set()
    result = None
    for row in details:
        ccy = row.get('ccy')
        require(isinstance(ccy,str) and ccy and ccy not in seen, '币种余额重复或不合法。')
        seen.add(ccy)
        cash,equity,available = number(row.get('cashBal')),number(row.get('eq')),number(row.get('availBal'))
        require(cash >= 0 and equity >= 0 and available >= 0 and
                number(row.get('liab') or 0)==0 and number(row.get('upl') or 0)==0,
                '账户余额、负债或未实现盈亏不符合空仓条件。')
        if ccy == 'USDT':
            require(abs(cash-equity) <= EPS, '空仓USDT现金和权益不一致。')
            result = {'cash':cash,'available':available}
        else:
            require(cash == equity == available == 0, '存在其他币种资产；注资只支持独立USDT账户。')
    require(result is not None, '账户没有可核对的USDT现金余额。')
    return result


def bootstrap(api, state_or_store, cfg, *, now=None):
    """Fresh arm only. Return metadata without changing the ledger or adopting excess cash.

    Caller owns local + UID locks and has already verified the account/identity.
    Two equal flat cash snapshots surrounding the fixed bill boundary prevent a
    concurrent balance change from being silently included in the baseline.
    """
    state = deepcopy(state_or_store if isinstance(state_or_store,dict) else state_or_store.get_state())
    require(isinstance(state,dict) and not state.get('position') and not state.get('closing'),
            '只有空仓账本可以建立注资核对基准。')
    require('capital_anchor_at' not in state, '已有注资基准，不能覆盖或重新归零。')
    before = flat_cash(api)
    end = api.now() if now is None else now
    require(type(end) is int and state['initialized_at'] <= end <= api.now(), '启用时间不合法。')
    bills = [normalize_bill(r) for r in api.account_bills(max(0,end-DAY),end)]
    after = flat_cash(api)
    require(before==after, '启用检查期间账户余额变化；请保持账户不操作后重试。')
    require(after['cash']+EPS >= number(state['cash']) and after['available']+EPS >= number(state['cash']),
            '实际可用资金不足原策略现金；不能以重建基准掩盖差额。')
    state.update(capital_usdt=finite_float(cfg['capital_usdt']),capital_added_usdt=0.,
                 capital_anchor_at=end,capital_bill_cursor=end,
                 capital_exchange_offset=text(max(Decimal(0),after['cash']-number(state['cash']))),
                 capital_baseline_bill_ids=sorted({r['billId'] for r in bills if int(r['ts'])==end}),
                 capital_pending_usdt='0')
    validate_metadata(state,cfg)
    return state


def is_deposit(row):
    return (row['type']=='1' and row['subType']=='11' and row['ccy']=='USDT' and
            row['instId']=='' and row['ordId']=='' and number(row['balChg'])>0 and
            number(row['fee'] or 0)==0 and number(row['pnl'] or 0)==0)


def classify(row, fills, funding):
    if is_deposit(row):
        return 'deposit'
    require(row['ccy']=='USDT' and row['instId']==INSTRUMENT and row['mgnMode']=='isolated',
            '发现非本策略转账、提现、其他币种或账户活动；暂停新增风险，保留原账本。')
    if row['type']=='2' and row['subType'] in ('3','4','5','6'):
        fill = fills.get(row['billId'])
        if fill is None:
            raise AuditPending('交易账单尚未匹配已核对逐笔成交；不增加风险，等待完整记录。')
        require(fill.get('ordId')==row['ordId'] and fill.get('instId')==INSTRUMENT and
                fill.get('feeCcy')=='USDT' and number(fill.get('fee'))==number(row['fee']) and
                number(fill.get('fillPnl'))==number(row['pnl']), '成交账单与原逐笔成交记录冲突。')
        return 'trade'
    if row['type']=='8' and row['subType'] in ('173','174'):
        recorded = funding.get(row['billId'])
        if recorded is None:
            raise AuditPending('资金费账单尚未完成原账本核对；等待后重试。')
        require(recorded.get('instId')==row['instId'] and recorded.get('ccy')=='USDT' and
                str(recorded.get('subType'))==row['subType'] and number(recorded.get('pnl'))==number(row['pnl'])
                and number(row['fee'] or 0)==0, '资金费账单与原记录冲突。')
        return 'funding'
    raise Attention('发现不支持的账户账单（如转出、人工保证金调整或强平）；不能将其视为注资。')


def _record(db, namespace, row, stamp):
    encoded = _json(row)
    old = db.execute('SELECT payload FROM records WHERE namespace=? AND record_key=?',(namespace,row['billId'])).fetchone()
    if old:
        require(old[0]==encoded,'同一账单编号内容冲突；原记录保留。')
        return False
    db.execute('INSERT INTO records(namespace,record_key,payload,created_at) VALUES (?,?,?,?)',
               (namespace,row['billId'],encoded,stamp))
    return True


def uncredited(store):
    credited = {r['billId'] for r in store.records('capital_credit')}
    return [r for r in store.records('capital_bill') if is_deposit(r) and r['billId'] not in credited]


def collect_bills(api, store, cfg, *, check_flat=True):
    """Periodic durable evidence only; caller has completed fills/funding audits first.

    Five days offline is the conservative limit: the one-day overlap must still
    fit in the six-day API window. Never repair a missing interval by resetting.
    """
    state = store.get_state()
    validate_metadata(state,cfg)
    end = api.now()
    require(0 <= end-state['capital_bill_cursor'] <= 5*DAY,
            '注资账单核对中断超过5天或时钟异常；保留账本，需人工核对缺失区间。')
    begin = max(state['capital_anchor_at'],state['capital_bill_cursor']-DAY)
    rows = api.account_bills(begin,end)
    require(isinstance(rows,list), '没有取得完整注资账单。')
    fills = {r['billId']:r for r in store.records('fill')}
    funding = {r['billId']:r for r in store.records('funding')}
    baseline = set(state['capital_baseline_bill_ids'])
    candidates = {}
    for raw in rows:
        row = normalize_bill(raw)
        require(begin <= int(row['ts']) <= end, '注资账单超出完整核对时间段。')
        if row['billId'] in baseline and int(row['ts'])==state['capital_anchor_at']:
            continue
        if row['billId'] in candidates:
            require(candidates[row['billId']]==row, '同一账单ID重复且内容冲突。')
        classify(row,fills,funding)
        candidates[row['billId']] = row
    combined = {r['billId']:r for r in store.records('capital_bill')}
    for identifier,row in candidates.items():
        require(identifier not in combined or combined[identifier]==row, '同一账单编号内容冲突；原记录保留。')
        combined[identifier]=row
    credited = {r['billId'] for r in store.records('capital_credit')}
    pending = sum((number(r['balChg']) for r in combined.values()
                   if is_deposit(r) and r['billId'] not in credited),Decimal(0))
    if check_flat and not state.get('position') and not store.unresolved():
        actual = flat_cash(api)
        expected = number(state['cash'])+number(state['capital_exchange_offset'])+pending
        if abs(actual['cash']-expected)>EPS:
            raise AuditPending('实际USDT现金与已核对现金及转入账单不一致；等待完整账单，不增加风险。')
        require(actual['available']+EPS >= number(state['cash']), '实际可用USDT不足策略当前现金；不增加风险。')
    # Commit only after complete classification AND (when flat) cash proof.
    # Missing or late bills cannot age out behind an advanced audit cursor.
    with store.transaction():
        require(store.get_state()==state,'注资账单核对期间本地账本已变化。')
        for row in candidates.values():
            _record(store.db,'capital_bill',row,end)
        state['capital_bill_cursor'] = end
        state['capital_pending_usdt'] = text(pending)
        store._put_encoded('state',_json(state))
    return {'pending_usdt':text(pending),'checked_at':end}


def service_stopped():
    try:
        result = subprocess.run(['systemctl','--user','show','--no-pager',UNIT,
                                 '-p','LoadState','-p','ActiveState','-p','SubState','-p','MainPID'],
                                capture_output=True,text=True,timeout=10)
        values = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        valid = (result.returncode in (0,4) and set(values)=={'LoadState','ActiveState','SubState','MainPID'} and
                 values['LoadState'] in ('loaded','not-found') and values['ActiveState'] in ('inactive','failed') and
                 values['SubState'] in ('dead','failed') and values['MainPID']=='0')
    except (OSError,ValueError,subprocess.TimeoutExpired):
        valid=False
    require(valid,'请在空仓时停止v53服务，再转入USDT；无法确认服务停止时不允许同步。')


def existing_ledger(directory):
    path = Path(directory)/'state.sqlite3'
    require(path.is_file() and not path.is_symlink() and path.stat().st_uid==os.getuid(),
            '未发现本人持有的已启用账本；资金同步不会初始化账户。')
    for suffix in ('-wal','-shm','-journal'):
        require(not Path(str(path)+suffix).is_symlink(),'账本附属文件不能是链接。')
    db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        require(db.execute('PRAGMA user_version').fetchone()[0]==1,'未知账本版本。')
        names = set(db.execute("SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall())
        require(names=={('table',n) for n in ('meta','events','intents','bars','records')},'未知账本结构。')
        require({'identity','state'} <= {r[0] for r in db.execute('SELECT key FROM meta')},'缺少原账户启用账本。')
    finally:
        db.close()


def eligible_state(store,cfg):
    state = store.get_state()
    capital = validate_metadata(state,cfg)
    require(state.get('armed') is True and not state.get('halted'),'尚未启用或已有风控停止；注资不能清除风控。')
    require(not state.get('position') and not state.get('closing') and not state.get('protection') and
            not store.unresolved(),'存在仓位、止损或待核对订单，不能同步资金。')
    require(all(r['key'] in state.get('control_ids',[]) for r in store.record_entries('control')),
            '仍有未处理的暂停/恢复/平仓请求，不能同步资金。')
    cash,peak,equity = number(state['cash']),number(state['peak_equity']),number(state['last_equity'])
    require(cash>0 and peak>=equity and abs(equity-cash)<=EPS,'空仓现金与净值记录不一致。')
    require(capital-equity < number(cfg['max_loss_usdt']) and
            (peak-equity)/peak < number(cfg['halt_dd']) and number(state['max_drawdown']) < number(cfg['halt_dd']),
            '原资金已达到亏损或回撤停止条件；不能用充值稀释后恢复。')
    return state


def _plan(api,store,cfg):
    collect_bills(api,store,cfg)
    state = eligible_state(store,cfg)
    rows = sorted(uncredited(store),key=lambda r:(int(r['ts']),int(r['billId'])))
    amount = sum((number(r['balChg']) for r in rows),Decimal(0))
    actual = flat_cash(api)
    require(abs(actual['cash']-number(state['cash'])-number(state['capital_exchange_offset'])-amount)<=EPS,
            '实际现金变化与已到账转入账单不一致；没有同步。')
    require(actual['available']+EPS >= number(state['cash'])+amount,'可用资金不足同步后的策略现金。')
    require(amount>=0,'注资金额不合法。')
    # Validate float representability BEFORE obtaining final confirmation.
    for key in ('capital_usdt','capital_added_usdt','cash','peak_equity','last_equity'):
        finite_float(number(state[key])+amount)
    return {'rows':rows,'amount':text(amount),'cash':text(actual['cash']),'available':text(actual['available']),
            'capital':text(number(state['capital_usdt'])+amount)},state


def sync_capital(cfg, confirm=input):
    """Never moves funds, submits/cancels orders, starts service or clears a halt."""
    from . import runtime
    from .execution import Broker
    require(sys.platform.startswith('linux') and os.geteuid()!=0,'请在Debian普通用户下执行，不要sudo。')
    service_stopped()
    directory = config.data_dir(cfg)
    existing_ledger(directory)
    with ExitStack() as stack:
        stack.enter_context(process_lock(directory/'runtime.lock'))
        existing_ledger(directory)
        store = Store(directory)
        stack.callback(store.close)
        consent = runtime.validate_live_consent(store.get_state(),cfg)
        stack.enter_context(process_lock(config.account_lock_path(cfg['mode'],consent['uid'])))
        config.validate_account_binding(cfg['mode'],consent['uid'],directory,consent['binding_id'])
        creds = config.credentials()
        api = OKXClient(mode=cfg['mode'],origin=cfg['origin'],proxy=cfg['proxy'],credentials=creds,allow_orders=False)
        spec,quote,snap = runtime.preflight(api,cfg)
        saved = store.identity()
        require(saved==runtime.identity(cfg,creds,snap['uid'],spec) and snap['uid']==consent['uid'],
                '原代码、配置、规格或API账户身份不一致；没有同步。')
        eligible_state(store,cfg)
        flat_cash(api)
        broker = Broker(api,store,cfg,spec)
        broker.audit_pending()
        broker.audit_fills()
        broker.update_funding()
        broker.equity(quote)  # Evaluate OLD capital/loss before adding any funds.
        plan,state = _plan(api,store,cfg)
        if number(plan['amount'])==0:
            return {'passed':True,'changed':False,'added_usdt':'0','capital_usdt':state['capital_usdt'],'orders_sent':0}
        prompt = ('已核对转入 %s U；v53累计本金 %s → %s U；亏损限额仍为 %s U。\n'
                  '只计入预算，不转账、不下单、不启动服务。确认请输入 SYNC V53：' %
                  (plan['amount'],text(state['capital_usdt']),plan['capital'],text(cfg['max_loss_usdt'])))
        require(confirm(prompt).strip()=='SYNC V53','未确认同步，本金未修改。')
        service_stopped()
        # Re-read both identity/config files and server account after user input.
        require(config.load_config()==cfg and config.credentials()==creds,'确认期间配置或密钥已改变。')
        spec2,_,snap2 = runtime.preflight(api,cfg)
        require(saved==runtime.identity(cfg,creds,snap2['uid'],spec2) and store.identity()==saved,'确认期间账户身份改变。')
        fresh,new_state = _plan(api,store,cfg)
        require(fresh==plan,'确认期间账单或余额改变；请重新核对。')
        # collect_bills may update only its audit cursor between two plans.
        require({k:v for k,v in state.items() if k!='capital_bill_cursor'} ==
                {k:v for k,v in new_state.items() if k!='capital_bill_cursor'},'确认期间原账本改变。')
        service_stopped()
        updated = deepcopy(new_state)
        amount = number(plan['amount'])
        for key in ('capital_usdt','capital_added_usdt','cash','peak_equity','last_equity'):
            updated[key] = finite_float(number(updated[key])+amount)
        updated['capital_pending_usdt']='0'
        updated['last_capital_sync_at']=api.now()
        with store.transaction():
            # Controls are separate rows, not part of state until the loop
            # processes them. Recheck inside the write transaction as well.
            require(eligible_state(store,cfg)==new_state and store.identity()==saved,'提交前账本改变。')
            for row in plan['rows']:
                require(_record(store.db,'capital_credit',row,api.now()),'转入账单已同步，拒绝重复增加。')
            store._put_encoded('state',_json(updated))
            store.db.execute('INSERT INTO events(kind,payload,created_at) VALUES (?,?,?)',
                             ('capital_added',_json({'added_usdt':plan['amount'],'capital_usdt':updated['capital_usdt'],
                              'bill_ids':[r['billId'] for r in plan['rows']],'orders_sent':0}),api.now()))
        return {'passed':True,'changed':True,'added_usdt':plan['amount'],'capital_usdt':updated['capital_usdt'],
                'max_loss_usdt':cfg['max_loss_usdt'],'orders_sent':0,
                'note':'已同步注资；原盈亏、风控和暂停状态保留，服务尚未启动。'}
